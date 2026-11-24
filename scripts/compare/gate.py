#!/usr/bin/env python3
"""The pre-push gate: audio latency and 4K throughput on one local lab, one verdict.

  gate.py run [--quick] [--run-id ID] [--thresholds FILE] [--baseline FILE]
  gate.py baseline RUN_ID [--baseline FILE]     record this machine's audio baseline from a run
  gate.py analyze DIR                           one pass's figures (JSON on stdout)
  gate.py verdict RUN_ID [--thresholds FILE] [--baseline FILE]   judge a run again, offline

`run` brings up the local lab (scripts/edge_lab.py, its id and CPU range from
NEEDLETAIL_EDGE_LAB_ID / NEEDLETAIL_EDGE_LAB_CPUS, binaries from a frozen set as
latency-bench.py requires) once per pass through scripts/compare/latency-bench.py,
keeps it up for a snapshot of every node's counters, and takes it down again:

  audio passes   1 ms Opus Custom (opus1), 1 ms PCM (pcm1), standard 2.5 ms Opus
                 (opus25); clean, 5% loss and burst loss (gemodel 1% 20%) on the
                 listeners' links; 2 UDP clients a site
  4K passes      the referee's clips (needletail-compare's prepare-media rungs,
                 --media DIR holding 2160pNNN/video.h264), sent as the referee
                 sends them (ntcmp flv, filler dropped, over RTMP), or a
                 synthetic rung (--video-mbps) where it has none (300); one 4K
                 UDP viewer a site and the 1 ms Opus listeners beside it, the
                 referee's 1,316 B symbols

`--quick` (about 15 minutes) is opus1 clean, pcm1 5% loss, opus25 burst and the
4K rung of 247 Mbit/s; the full form is every audio pass and every 4K rung.

Every pass's raw data goes into the run archive (~/needletail-data/<run id>/,
kind local-lab; latency-bench.py writes its own files there, this adds the
counters snapshot, the refused-part counts, its figures and the verdict), and
summary.md there is the one-page result. Exit 0 = PASS, 1 = FAIL, 2 = could
not run.

Thresholds are in gate-thresholds.json beside this file; the audio baseline of
this machine (p50/p99 of each audio pass on a known-good build) in
gate-baseline.json, keyed by host name (`gate.py baseline RUN_ID` writes it).

A 4K viewer is judged as the referee judges one (needletail-compare
docs/real-world-tests/2026-11-22-4k-...: a frame is on time when it arrived,
can be decoded (no frame before it since its key frame is missing) and is not
more than 250 ms later than the median; a rung is carried when 99.5% of the
frames are on time and no stall is longer than 500 ms; the first 5 s and the
last 3 s are left out), from the arrival of every part (--arrivals)."""

from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import re
import shlex
import socket
import statistics
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent
ROOT = SCRIPTS.parent
THRESHOLDS = HERE / "gate-thresholds.json"
BASELINE = HERE / "gate-baseline.json"
BENCH = HERE / "latency-bench.py"
# The referee's clips (needletail-compare prepare-media), each DIR/video.h264,
# sent as the referee sends them (ntcmp flv over RTMP: edge_lab.referee_clip).
DEFAULT_MEDIA = pathlib.Path(os.environ.get(
    "NEEDLETAIL_GATE_MEDIA",
    str(pathlib.Path.home() / "wavey.ai" / "needletail-compare" / "target" / "compare-media"
        / "clips6-full")))

# The audio lanes: latency-bench payload and epoch samples.
LANES = {"opus1": ("opus", 48), "pcm1": ("pcm", 48), "opus25": ("opus", 120)}
LOSSES = {"clean": None, "loss5": "loss 5%", "burst": "loss gemodel 1% 20%"}
# The lab settings of every pass, audio and 4K alike, so the audio baseline and
# the audio beside 4K share one mesh configuration: the referee's symbols and a
# g6-dedicated-2 edge's 4 Gbit/s capacity (the lab's own default is 100 Mbit/s).
LAB_SETTINGS = ["mesh_symbol_size=1316", "mesh_max_symbol_size=0",
                "edge_egress_capacity_bps=4000000000"]
REFEREE = {"late_ms": 250.0, "on_time": 0.995, "stall_ms": 500.0, "head_s": 5.0, "tail_s": 3.0}
# A node's CPUs (its quota in the lab, its plan on Linode): a g6-dedicated-4
# contributor, g6-dedicated-2 forwarders and edges.
ROLE_CPUS = {"contributor": 4, "distributor": 2, "edge": 2, "backbone": 2}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------- the plan

def plan(quick: bool, rungs: list[str] | None = None) -> list[dict]:
    """The passes of a run, in order: audio first (the 4K passes leave the
    host warmer), then the 4K rungs from the lowest."""
    if quick:
        audio = [("opus1", "clean"), ("pcm1", "loss5"), ("opus25", "burst")]
        rungs = rungs or ["247"]
    else:
        audio = [(lane, loss) for lane in LANES for loss in LOSSES]
        rungs = rungs or ["25", "80", "164", "247", "300"]
    passes = [{"kind": "audio", "name": f"{lane}-{loss}", "lane": lane, "loss": loss}
              for lane, loss in audio]
    passes += [{"kind": "4k", "name": f"4k-{rung}", "lane": "opus1", "loss": "clean",
                "rung": rung} for rung in rungs]
    return passes


def rung_source(rung: str, media: pathlib.Path) -> list[str]:
    """The bench's 4K source of a rung: the referee's clip when it is there
    (25, 80, 164, 247, 333 = its 2160p25/80/160/240/320), else a synthetic
    clip at that rate (300 has no referee clip)."""
    names = {"25": "2160p25", "80": "2160p80", "164": "2160p160", "247": "2160p240",
             "333": "2160p320"}
    if rung in names and (media / names[rung] / "video.h264").is_file():
        return ["--video-clip", str(media / names[rung])]
    return ["--video-mbps", rung]


def bench_command(item: dict, run_id: str, media: pathlib.Path, seconds: int,
                  extra: list[str]) -> list[str]:
    payload, samples = LANES[item["lane"]]
    command = [sys.executable, str(BENCH), "--run-id", run_id, "--name", item["name"],
               "--seconds", str(seconds), "--payload", payload, "--epoch-samples", str(samples),
               "--udp-clients", "2", "--sample-nodes", "--keep-up"]
    for setting in LAB_SETTINGS:
        command += ["--set", setting]
    if LOSSES[item["loss"]]:
        command += ["--client-loss", LOSSES[item["loss"]]]
    if item["kind"] == "4k":
        command += rung_source(item["rung"], media)
        command += ["--video-readers", "0", "--video-udp-clients", "1"]
    if os.environ.get("NTCMP"):
        command += ["--ntcmp", os.environ["NTCMP"]]
    return command + extra


# --------------------------------------------------------------- one pass's figures

def read_ndjson(path: pathlib.Path) -> list[dict]:
    out = []
    try:
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def node_figures(samples: list[dict], since_s: float, until_s: float, video_mbps: float | None,
                 roles: dict[str, str]) -> dict:
    """Per node: CPU over the window (mean % of one CPU, the p95 and max of the
    per-second figure, % of its own CPUs), CPU seconds per Gbit of 4K carried,
    and the UDP receive-buffer drops of each socket over the whole pass."""
    series: dict[str, list[tuple[float, int]]] = {}
    drops_first: dict[str, dict] = {}
    drops_last: dict[str, dict] = {}
    throttled: dict[str, list[int]] = {}
    for line in samples:
        for name, item in line.items():
            if not isinstance(item, dict) or not item.get("running") or "usage_usec" not in item:
                continue
            series.setdefault(name, []).append((float(item["at"]), int(item["usage_usec"])))
            drops = item.get("udp_socket_drops") or {}
            drops_first.setdefault(name, dict(drops))
            for port, count in drops.items():
                drops_first[name].setdefault(port, count)
            drops_last[name] = {**drops_last.get(name, {}), **drops}
            throttled.setdefault(name, []).append(int(item.get("throttled_usec") or 0))
    out = {}
    for name, points in sorted(series.items()):
        points.sort()
        inside = [p for p in points if since_s - 1.5 <= p[0] <= until_s + 1.5] or points
        rates = [(b[1] - a[1]) / 1e4 / max(b[0] - a[0], 1e-3) for a, b in zip(inside, inside[1:])]
        span = inside[-1][0] - inside[0][0] if len(inside) > 1 else 0.0
        used_s = (inside[-1][1] - inside[0][1]) / 1e6 if len(inside) > 1 else 0.0
        mean = 100.0 * used_s / span if span > 0 else None
        cpus = ROLE_CPUS.get(roles.get(name, ""), 2)
        ordered = sorted(rates)
        socket_drops = {port: drops_last[name].get(port, 0) - drops_first[name].get(port, 0)
                        for port in drops_last.get(name, {})}
        socket_drops = {port: count for port, count in socket_drops.items() if count}
        gbit = (video_mbps or 0) * span / 1000.0
        out[name] = {
            "role": roles.get(name), "cpus": cpus,
            "cpu_percent": None if mean is None else round(mean, 1),
            "cpu_percent_p95": round(ordered[int(len(ordered) * 0.95) - 1], 1) if ordered else None,
            "cpu_percent_max": round(ordered[-1], 1) if ordered else None,
            "cpu_share": None if mean is None else round(mean / (100.0 * cpus), 3),
            "cpu_s_per_gbit": round(used_s / gbit, 3) if gbit > 0 else None,
            "throttled_ms": round((throttled[name][-1] - throttled[name][0]) / 1000.0, 1)
            if throttled.get(name) else 0.0,
            "socket_drops": socket_drops,
            "socket_drops_total": sum(socket_drops.values()),
        }
    return out


def read_arrivals(path: pathlib.Path) -> dict[int, tuple[int, int, int]]:
    """{part: (arrival_us, bytes, published_us)} of a client's --arrivals."""
    out = {}
    try:
        with path.open() as handle:
            for row in csv.DictReader(handle):
                try:
                    part = int(row["part"])
                    out.setdefault(part, (int(row["arrival_unix_us"]), int(row["bytes"]),
                                          int(row["published_unix_us"])))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return out


def key_offset(sizes: dict[int, int], gop: int) -> int | None:
    """The part number (mod gop) of the key frames: the residue whose parts
    are the largest on average."""
    if gop < 2 or len(sizes) < gop:
        return None
    sums = [0.0] * gop
    counts = [0] * gop
    for part, size in sizes.items():
        sums[part % gop] += size
        counts[part % gop] += 1
    means = [sums[i] / counts[i] if counts[i] else 0.0 for i in range(gop)]
    return max(range(gop), key=lambda i: means[i])


def referee_verdict(arrivals: dict[int, tuple[int, int, int]], since_us: int, until_us: int,
                    fps: float, rules: dict = REFEREE, missing_examples=()) -> dict:
    """The referee's figures for one 4K viewer over the window: the frames
    published from 5 s into it to 3 s before its end (one frame a part).
    A missing part makes the frames after it undecodable until the next key
    frame (found from the part sizes, a key frame every 2 s)."""
    if not arrivals or fps <= 0:
        return {"error": "no arrivals", "carried": False}
    lo = since_us + int(rules["head_s"] * 1e6)
    hi = until_us - int(rules["tail_s"] * 1e6)
    inside = {p: v for p, v in arrivals.items() if lo <= v[2] <= hi}
    if not inside:
        return {"error": "no part published in the window", "carried": False}
    first, last = min(inside), max(inside)
    gop = int(round(2 * fps))
    offset = key_offset({p: v[1] for p, v in arrivals.items()}, gop)
    latency = {p: (v[0] - v[2]) / 1000.0 for p, v in inside.items()}
    median = statistics.median(latency.values())
    frame_ms = 1000.0 / fps
    on_time = 0
    late = 0
    undecodable = 0
    missing = 0
    run = longest = 0.0
    broken = False
    for part in range(first, last + 1):
        if offset is not None and part % gop == offset:
            broken = False
        ok = False
        if part not in arrivals:
            missing += 1
            broken = True
        elif broken:
            undecodable += 1
        elif part in latency and latency[part] > median + rules["late_ms"]:
            late += 1
        else:
            ok = True
        if ok:
            on_time += 1
            run = 0.0
        else:
            run += frame_ms
            longest = max(longest, run)
    expected = last - first + 1
    share = on_time / expected
    values = sorted(latency.values())
    return {"expected": expected, "on_time": on_time, "late": late, "missing": missing,
            "undecodable": undecodable, "on_time_fraction": round(share, 5),
            "longest_stall_ms": round(longest, 1), "median_ms": round(median, 2),
            "p99_ms": round(values[min(len(values) - 1, int(len(values) * 0.99))], 2),
            "max_ms": round(values[-1], 2), "tail_ms": round(values[-1] - median, 2),
            "gop": gop, "key_offset": offset,
            "carried": bool(share >= rules["on_time"] and longest <= rules["stall_ms"])}


def counter_figures(snapshot: dict | None) -> dict:
    """Per node from the counters snapshot: the tee's refused copies and the
    fast plane's kernel drops (cumulative since the node started, which is
    this pass's own `up`)."""
    out = {}
    for name, item in ((snapshot or {}).get("nodes") or {}).items():
        mesh = item.get("/api/mesh")
        if not isinstance(mesh, dict):
            continue
        fast = (mesh.get("relay_session") or {}).get("fast_plane") or {}
        picked = {key: fast.get(key) for key in ("tee_dropped", "kernel_dropped",
                                                  "video_primary_kernel_dropped",
                                                  "video_secondary_kernel_dropped",
                                                  "video_received", "duplicates", "would_block")
                  if fast.get(key) is not None}
        if picked:
            out[name] = picked
    return out


def refused_figures(text: str) -> dict:
    """`grep -c 'exceeds configured limit' *.log` lines: parts a node refused
    for want of a slot."""
    out = {}
    for line in text.splitlines():
        name, _, count = line.rpartition(":")
        try:
            if int(count):
                out[name.removesuffix(".log")] = int(count)
        except ValueError:
            continue
    return out


def audio_figures(hops: dict, video_edges: set[str]) -> list[dict]:
    """Every audio UDP client: its site, edge, whether that edge carried 4K
    (a 4K viewer on it), missing parts and its unit latency (source -> client)."""
    out = []
    for host, entry in sorted(((hops.get("udp_clients") or {}).get("hosts") or {}).items()):
        for client in entry.get("clients") or []:
            latency = client.get("unit_latency_ms") or {}
            caches = client.get("caches") or []
            out.append({"host": host, "site": client.get("site") or entry.get("site"),
                        "report": client.get("report"), "edge": caches[-1] if caches else None,
                        "edges": caches, "edge_has_4k": bool(set(caches) & video_edges),
                        "missing": client.get("missing"), "expected": client.get("expected"),
                        "p50_ms": latency.get("p50"), "p99_ms": latency.get("p99"),
                        "max_ms": latency.get("max"), "count": latency.get("count")})
    return out


def analyze(directory: pathlib.Path) -> dict:
    """One pass's figures from its bench directory (or its copy in the
    archive)."""
    directory = pathlib.Path(directory)
    hops = json.loads((directory / "hops.json").read_text())
    window = hops.get("window_us") or {}
    since_us, until_us = int(window.get("since") or 0), int(window.get("until") or 0)
    roles = {name: item.get("role") for name, item in (hops.get("nodes") or {}).items()}
    video = hops.get("video") or None
    source = (video or {}).get("source") or {}
    video_mbps = source.get("video_mbps") or source.get("nominal_mbps")
    fps = float(source.get("fps") or 0) or None
    viewers = []
    video_edges: set[str] = set()
    if video:
        for path in sorted(directory.glob("*.4k.udp*.json")):
            try:
                report = json.loads(path.read_text())
            except (OSError, ValueError):
                report = {}
            caches = report.get("caches") or []
            video_edges.update(caches)
            arrivals = read_arrivals(path.with_name(path.name.replace(".json", ".arrivals.csv")))
            host = path.name.split(".")[0]
            site = "london" if host.startswith("lon") else "newyork" if host.startswith("new") else host
            latency = report.get("publish_to_arrival_ms") or {}
            viewers.append({
                "host": host, "site": site, "report": path.name, "edges": caches,
                "missing_parts": report.get("missing_parts"),
                "expected_parts": report.get("expected_parts"),
                "received_parts": report.get("received_parts"),
                "datagram_loss": report.get("datagram_loss"),
                "publish_to_arrival_ms": {k: latency.get(k) for k in ("p50", "p99", "max")},
                "bench_whole": bool(report.get("missing_parts") == 0 and latency.get("count")
                                    and latency.get("max", 0) - latency.get("p50", 0) <= 100),
                "referee": referee_verdict(arrivals, since_us, until_us, fps or 24.0),
            })
    try:
        samples = read_ndjson(directory / "nodes.ndjson")
    except OSError:
        samples = []
    nodes = node_figures(samples, since_us / 1e6, until_us / 1e6, video_mbps if video else None,
                         roles)
    snapshot = None
    if (directory / "counters.json").is_file():
        try:
            snapshot = json.loads((directory / "counters.json").read_text())
        except ValueError:
            snapshot = None
    refused = refused_figures((directory / "refused.txt").read_text()) \
        if (directory / "refused.txt").is_file() else {}
    bench = hops.get("bench") or {}
    return {"name": bench.get("name") or directory.name, "dir": str(directory),
            "window_s": round((until_us - since_us) / 1e6, 1) if until_us else None,
            "payload": bench.get("payload"), "epoch_samples": bench.get("epoch_samples"),
            "video": None if not video else {"label": source.get("label"), "mbps": video_mbps,
                                             "fps": fps, "viewers": viewers,
                                             "edges": sorted(video_edges)},
            "audio": audio_figures(hops, video_edges), "nodes": nodes,
            "counters": counter_figures(snapshot), "refused": refused}


# --------------------------------------------------------------- the verdict

def host_baseline(baseline: dict, host: str | None = None) -> dict:
    return (baseline.get("hosts") or {}).get(host or socket.gethostname(), {})


def pass_key(result: dict) -> str:
    return result.get("gate_pass") or result.get("name")


def verdict(results: list[dict], thresholds: dict, baseline: dict, host: str | None = None) -> dict:
    """Every check against the thresholds; PASS only when every check passes.
    `results` are analyze() outputs, each with "gate_pass" (its plan name)."""
    checks: list[dict] = []
    base = host_baseline(baseline, host)

    def check(name: str, ok: bool, value, limit, note: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "value": value, "limit": limit,
                       **({"note": note} if note else {})})

    audio_t = thresholds["audio"]
    by_name = {pass_key(item): item for item in results}
    for item in results:
        key = pass_key(item)
        clients = item.get("audio") or []
        if not clients:
            check(f"{key}: audio clients reported", False, 0, ">0")
            continue
        missing = sum(int(c.get("missing") or 0) for c in clients)
        check(f"{key}: audio missing", missing <= audio_t["missing_max"], missing,
              audio_t["missing_max"])
        if item.get("video"):
            continue
        reference = base.get(key)
        for site in sorted({c["site"] for c in clients}):
            p99 = [c["p99_ms"] for c in clients if c["site"] == site and c.get("p99_ms") is not None]
            if not p99:
                continue
            value = round(statistics.median(p99), 3)
            loss = key.split("-")[-1]
            margin = audio_t["p99_margin_ms"].get(loss, audio_t["p99_margin_ms"]["clean"])
            if reference and site in reference:
                limit = round(reference[site]["p99_ms"] + margin, 3)
                check(f"{key}: {site} audio p99", value <= limit, value, limit,
                      f"baseline {reference[site]['p99_ms']} + {margin}")
            else:
                check(f"{key}: {site} audio p99", True, value, None, "no baseline for this host")
    video_t = thresholds["video"]
    for item in results:
        video = item.get("video")
        if not video:
            continue
        key = pass_key(item)
        rung = key.removeprefix("4k-")
        must = rung in [str(r) for r in video_t["must_carry"]]
        for viewer in video["viewers"]:
            referee = viewer.get("referee") or {}
            name = f"{key}: {viewer['site']} 4K carried"
            share = referee.get("on_time_fraction")
            if must:
                check(name, bool(referee.get("carried")), share, video_t["on_time_min"],
                      f"longest stall {referee.get('longest_stall_ms')} ms, missing "
                      f"{viewer.get('missing_parts')}")
            else:
                checks.append({"check": name, "ok": True, "value": share, "limit": None,
                               "note": "reported (carried: "
                                       f"{'yes' if referee.get('carried') else 'no'})"})
        if not must:
            continue
        drops = {name: node["socket_drops_total"] for name, node in item["nodes"].items()
                 if node.get("socket_drops_total")}
        check(f"{key}: socket drops", sum(drops.values()) <= video_t["socket_drops_max"],
              drops or 0, video_t["socket_drops_max"])
        tee = {name: c.get("tee_dropped") for name, c in (item.get("counters") or {}).items()
               if c.get("tee_dropped")}
        check(f"{key}: tee drops", sum(tee.values()) <= video_t["tee_drops_max"], tee or 0,
              video_t["tee_drops_max"])
        refused = item.get("refused") or {}
        check(f"{key}: parts refused", sum(refused.values()) <= video_t["refused_max"],
              refused or 0, video_t["refused_max"])
        over = {name: node["cpu_share"] for name, node in item["nodes"].items()
                if node.get("cpu_share") is not None and node["cpu_share"] > video_t["cpu_share_max"]}
        check(f"{key}: node CPU share", not over, over or "all within", video_t["cpu_share_max"])
        # The audio beside 4K, on an edge that does not carry it, against the
        # same run's audio alone (opus1 clean).
        alone = by_name.get("opus1-clean")
        if alone:
            for site in sorted({c["site"] for c in item["audio"]}):
                beside = [c for c in item["audio"] if c["site"] == site and not c["edge_has_4k"]]
                ref = [c for c in alone["audio"] if c["site"] == site]
                if not beside or not ref:
                    continue
                for field, limit_key in (("p50_ms", "beside_4k_p50_ms"), ("p99_ms", "beside_4k_p99_ms")):
                    a = statistics.median(c[field] for c in beside)
                    b = statistics.median(c[field] for c in ref)
                    delta = round(a - b, 3)
                    check(f"{key}: {site} audio {field[:3]} beside 4K (edge without 4K) - alone",
                          delta <= audio_t[limit_key], delta, audio_t[limit_key])
    ok = all(item["ok"] for item in checks) and bool(checks)
    return {"verdict": "PASS" if ok else "FAIL", "checks": checks,
            "failed": [item for item in checks if not item["ok"]]}


# --------------------------------------------------------------- the summary

def summary_markdown(run_id: str, results: list[dict], judged: dict, meta: dict) -> str:
    lines = [f"# Gate {run_id}: {judged['verdict']}", "",
             f"Host {meta.get('host')}, lab {meta.get('lab_id')} CPUs {meta.get('cpus')}, "
             f"{'quick' if meta.get('quick') else 'full'} form, {meta.get('started')} to "
             f"{meta.get('ended')} UTC; binaries {meta.get('binaries')}.", ""]
    lines += ["## Audio (unit latency source -> UDP client, ms; missing parts)", "",
              "| pass | site | edge | 4K on edge | p50 | p99 | missing |", "| --- | --- | --- | --- | ---: | ---: | ---: |"]
    for item in results:
        for c in item.get("audio") or []:
            lines.append(f"| {pass_key(item)} | {c['site']} | {c['edge']} | "
                         f"{'yes' if c['edge_has_4k'] else 'no'} | {c['p50_ms']} | {c['p99_ms']} | "
                         f"{c['missing']} |")
    videos = [item for item in results if item.get("video")]
    if videos:
        lines += ["", "## 4K (the referee's rule: 99.5% on time, no stall over 500 ms)", "",
                  "| rung | clip Mbit/s | site | carried | on time | missing | longest stall ms | "
                  "p50 / p99 / max ms |", "| --- | ---: | --- | --- | ---: | ---: | ---: | --- |"]
        for item in videos:
            for viewer in item["video"]["viewers"]:
                r = viewer["referee"]
                lines.append(f"| {pass_key(item)} | {item['video']['mbps']} | {viewer['site']} | "
                             f"{'yes' if r.get('carried') else 'NO'} | "
                             f"{r.get('on_time_fraction')} | {viewer.get('missing_parts')} | "
                             f"{r.get('longest_stall_ms')} | {r.get('median_ms')} / {r.get('p99_ms')} "
                             f"/ {r.get('max_ms')} |")
        names = sorted({name for item in videos for name in item["nodes"]})
        lines += ["", "CPU, % of one CPU (CPU s per Gbit of 4K); socket drops", "",
                  "| node | " + " | ".join(pass_key(i) for i in videos) + " |",
                  "| --- |" + " ---: |" * len(videos)]
        for name in names:
            cells = []
            for item in videos:
                node = item["nodes"].get(name) or {}
                cells.append(f"{node.get('cpu_percent')} ({node.get('cpu_s_per_gbit')})"
                             + (f" drops {node['socket_drops_total']}" if node.get("socket_drops_total") else ""))
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Checks", ""]
    for item in judged["checks"]:
        lines.append(f"- {'ok  ' if item['ok'] else 'FAIL'} {item['check']}: {item['value']}"
                     f" (limit {item['limit']}){' - ' + item['note'] if item.get('note') else ''}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- running it

def lab_env() -> dict:
    return dict(os.environ)


def edge_lab(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "edge_lab.py"), *args],
                          capture_output=True, text=True, check=check, env=lab_env())


def snapshot_counters() -> dict:
    sys.path.insert(0, str(SCRIPTS))
    import edge_lab as lab  # noqa: E402
    out = {"at": time.time(), "nodes": {}}
    for name in lab.load("services.json"):
        item = {}
        for path in ("/api/mesh", "/metrics"):
            status, _, body = lab.curl(name, path, timeout=10)
            if status != 200:
                item[path] = {"status": status}
                continue
            try:
                item[path] = json.loads(body)
            except ValueError:
                item[path] = body
        out["nodes"][name] = item
    return out


def refused_counts() -> str:
    sys.path.insert(0, str(SCRIPTS))
    import edge_lab as lab  # noqa: E402
    lines = []
    for path in sorted((lab.WORK / "logs").glob("*.log")):
        try:
            count = path.read_text(errors="replace").count("exceeds configured limit")
        except OSError:
            continue
        lines.append(f"{path.name}:{count}")
    return "\n".join(lines) + "\n"


def bench_dir(name: str) -> pathlib.Path:
    ident = os.environ.get("NEEDLETAIL_EDGE_LAB_ID")
    base = ROOT / "target" / "latency-bench"
    return base / ident / name if ident else base / name


def run_pass(item: dict, run_id: str, media: pathlib.Path, seconds: int, extra: list[str]) -> dict:
    sys.path.insert(0, str(SCRIPTS))
    import run_archive  # noqa: E402
    command = bench_command(item, run_id, media, seconds, extra)
    log(f"pass {item['name']}: {shlex.join(command[2:])}")
    out = bench_dir(item["name"])
    started = time.time()
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=lab_env(), timeout=1800)
        code, text = result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired as error:
        code, text = 124, f"timed out after {error.timeout} s"
    extras = {}
    try:
        extras["counters.json"] = json.dumps(snapshot_counters())
        extras["refused.txt"] = refused_counts()
    except Exception as error:  # noqa: BLE001 - the pass's data is kept whatever failed here
        extras["counters-error.txt"] = repr(error)
    finally:
        edge_lab("down")
    extras["gate-bench.log"] = text
    out.mkdir(parents=True, exist_ok=True)
    archive = run_archive.RunArchive.open(run_id) if (run_archive.data_root() / run_id).is_dir() \
        else run_archive.RunArchive.create(run_id, "local-lab", tool="gate.py")
    for name, body in extras.items():
        (out / name).write_text(body)
        archive.add_file(out / name, f"{item['name']}/{name}", kind="raw",
                         description=f"gate.py: {name} of pass {item['name']}")
    try:
        figures = analyze(out)
    except (OSError, ValueError) as error:
        figures = {"name": item["name"], "error": f"no figures: {error}", "audio": [], "nodes": {}}
    figures.update({"gate_pass": item["name"], "bench_exit": code,
                    "seconds_taken": round(time.time() - started, 1)})
    archive.write_json(f"{item['name']}/gate-figures.json", figures, kind="result",
                       description="gate.py analyze: this pass's figures")
    return figures


def preflight() -> str | None:
    """Refuse to start while a run of this lab id is going, or without a lab id
    and CPU range (the gate shares the host)."""
    if not os.environ.get("NEEDLETAIL_EDGE_LAB_ID") or not os.environ.get("NEEDLETAIL_EDGE_LAB_CPUS"):
        return "set NEEDLETAIL_EDGE_LAB_ID and NEEDLETAIL_EDGE_LAB_CPUS (a lab of its own)"
    ident = os.environ["NEEDLETAIL_EDGE_LAB_ID"]
    busy = subprocess.run(["pgrep", "-af", "latency-bench.py"], capture_output=True, text=True).stdout
    mine = [line for line in busy.splitlines() if f"/{ident}/" in line or f"elab-{ident}" in line]
    if mine:
        return f"a latency-bench pass of lab {ident} is running: {mine[0]}"
    return None


def cmd_run(args) -> int:
    reason = preflight()
    if reason:
        print(f"gate: {reason}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(SCRIPTS))
    import run_archive  # noqa: E402
    thresholds = json.loads(pathlib.Path(args.thresholds).read_text())
    baseline = json.loads(pathlib.Path(args.baseline).read_text()) \
        if pathlib.Path(args.baseline).is_file() else {}
    run_id = args.run_id or time.strftime("gate-%Y%m%dT%H%M%SZ", time.gmtime())
    rungs = args.rungs.split(",") if args.rungs else None
    passes = plan(args.quick, rungs)
    if args.only:
        wanted = set(args.only.split(","))
        passes = [item for item in passes if item["name"] in wanted]
    if args.dry_run:
        for item in passes:
            print(shlex.join(bench_command(item, run_id, pathlib.Path(args.media), args.seconds,
                                           args.bench_args)))
        return 0
    archive = run_archive.RunArchive.create(run_id, "local-lab", tool="gate.py")
    meta = {"host": socket.gethostname(), "lab_id": os.environ.get("NEEDLETAIL_EDGE_LAB_ID"),
            "cpus": os.environ.get("NEEDLETAIL_EDGE_LAB_CPUS"), "quick": args.quick,
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "binaries": os.environ.get("NEEDLETAIL_AV_MESH_BIN"), "passes": [p["name"] for p in passes]}
    archive.write_json("gate-plan.json", {"meta": meta, "passes": passes, "thresholds": thresholds},
                       kind="config", description="gate.py: the plan and the thresholds it judged by")
    results = []
    for item in passes:
        results.append(run_pass(item, run_id, pathlib.Path(args.media), args.seconds, args.bench_args))
    meta["ended"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    judged = verdict(results, thresholds, baseline)
    archive.write_json("gate-verdict.json", {"meta": meta, **judged}, kind="result",
                       description="gate.py: every check and the verdict")
    text = summary_markdown(run_id, results, judged, meta)
    path = bench_dir(f"gate-{run_id}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    archive.add_file(path, "summary.md", kind="summary", description="gate.py: the one-page result")
    archive.finish("complete")
    print(text)
    print(f"gate: {judged['verdict']} ({run_archive.data_root() / run_id})")
    return 0 if judged["verdict"] == "PASS" else 1


def load_run(run_id: str) -> list[dict]:
    sys.path.insert(0, str(SCRIPTS))
    import run_archive  # noqa: E402
    root = run_archive.data_root() / run_id
    plan_file = root / "gate-plan.json"
    names = [p["name"] for p in json.loads(plan_file.read_text())["passes"]] if plan_file.is_file() \
        else sorted(p.parent.name for p in root.glob("*/hops.json"))
    results = []
    for name in names:
        if not (root / name / "hops.json").is_file():
            continue
        figures = analyze(root / name)
        figures["gate_pass"] = name
        results.append(figures)
    return results


def cmd_verdict(args) -> int:
    thresholds = json.loads(pathlib.Path(args.thresholds).read_text())
    baseline = json.loads(pathlib.Path(args.baseline).read_text()) \
        if pathlib.Path(args.baseline).is_file() else {}
    results = load_run(args.run_id)
    judged = verdict(results, thresholds, baseline, args.host)
    print(summary_markdown(args.run_id, results, judged, {"host": args.host or socket.gethostname()}))
    return 0 if judged["verdict"] == "PASS" else 1


def baseline_from(results: list[dict]) -> dict:
    """{pass: {site: {"p50_ms", "p99_ms"}}} of the audio-only passes: the
    median over a site's clients."""
    out = {}
    for item in results:
        if item.get("video"):
            continue
        sites = {}
        for site in sorted({c["site"] for c in item.get("audio") or []}):
            clients = [c for c in item["audio"] if c["site"] == site and c.get("p99_ms") is not None]
            if clients:
                sites[site] = {"p50_ms": round(statistics.median(c["p50_ms"] for c in clients), 3),
                               "p99_ms": round(statistics.median(c["p99_ms"] for c in clients), 3)}
        if sites:
            out[pass_key(item)] = sites
    return out


def cmd_baseline(args) -> int:
    path = pathlib.Path(args.baseline)
    data = json.loads(path.read_text()) if path.is_file() else {}
    host = args.host or socket.gethostname()
    entry = baseline_from(load_run(args.run_id))
    if not entry:
        print("gate: no audio pass in that run", file=sys.stderr)
        return 2
    hosts = data.setdefault("hosts", {})
    hosts[host] = {**hosts.get(host, {}), **entry, "_from_run": args.run_id,
                   "_recorded": time.strftime("%Y-%m-%d", time.gmtime())}
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(json.dumps(hosts[host], indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--quick", action="store_true")
    run.add_argument("--run-id")
    run.add_argument("--rungs", help="4K rungs (default: 247 quick; 25,80,164,247,300 full)")
    run.add_argument("--only", help="only these passes (names of the plan)")
    run.add_argument("--seconds", type=int, default=40)
    run.add_argument("--media", default=str(DEFAULT_MEDIA))
    run.add_argument("--thresholds", default=str(THRESHOLDS))
    run.add_argument("--baseline", default=str(BASELINE))
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("bench_args", nargs=argparse.REMAINDER,
                     help="after --: more latency-bench arguments for every pass")
    base = sub.add_parser("baseline")
    base.add_argument("run_id")
    base.add_argument("--baseline", default=str(BASELINE))
    base.add_argument("--host")
    ana = sub.add_parser("analyze")
    ana.add_argument("dir")
    ver = sub.add_parser("verdict")
    ver.add_argument("run_id")
    ver.add_argument("--thresholds", default=str(THRESHOLDS))
    ver.add_argument("--baseline", default=str(BASELINE))
    ver.add_argument("--host")
    args = parser.parse_args(argv)
    if args.command == "run":
        args.bench_args = [a for a in args.bench_args if a != "--"]
        return cmd_run(args)
    if args.command == "baseline":
        return cmd_baseline(args)
    if args.command == "analyze":
        print(json.dumps(analyze(pathlib.Path(args.dir)), indent=1))
        return 0
    return cmd_verdict(args)


if __name__ == "__main__":
    sys.exit(main())
