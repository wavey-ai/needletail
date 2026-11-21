#!/usr/bin/env python3
"""Measure the Needletail UDP mesh on its own, hop by hop.

Every av-mesh node (backbone relays, distributors, edges) keeps its own cache
and stamps each part with its own clock when the part is complete there: the
response header `x-needletail-cache-available-unix-us`. The part number is the
contributor's canonical sequence, so part N is the same object on every node.
Fetching the same parts from every node after the fact therefore gives the
one-way time of each hop, to the precision of the clocks (chrony, within about
0.25 ms on this lab):

  contributor -> relay-primary      A_relay-primary(N) - P(N)
  contributor -> relay-secondary    A_relay-secondary(N) - P(N)
  relay-primary -> distributor      A_distributor(N) - A_relay-primary(N)
  distributor -> edge               A_edge(N) - min(A_dist-a(N), A_dist-b(N))
  contributor -> edge               A_edge(N) - P(N)

P(N) is the moment the contributor published part N. The contributor serves no
media, so P comes from the playlist: `#EXT-X-PROGRAM-DATE-TIME` is the publish
time of the first part of the oldest segment, less its duration, to the
millisecond. Parts are published at a steady cadence, so P(N) is the line
through the samples of the program date and time taken during the run.

For the PCM stream of the AEP1 sender, whose session start is known, the time
from the last sample of a part being spoken to its publication is reported
too (capture -> contributor).

Relays forward every datagram before they decode it, so a distributor's stamp
is not after the relay's on the critical path: relay -> distributor is the
difference of two decode times, and contributor -> distributor is reported as
well.

RaptorQ counters of every node are read before and after, so each hop's FEC
work and recovery is in the result (the counters are per node, not per link).

  mesh-hops.py run --streams 4001,3001,1 --out FILE [--seconds 12]
  mesh-hops.py summary FILE...
"""

from __future__ import annotations

import argparse
import calendar
import json
import pathlib
import statistics
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import small_mesh_common as common  # noqa: E402

CA = "/usr/local/share/ca-certificates/needletail-qualification.crt"
NAME = common.TLS_SERVER_NAME
PART_MS = {"audio": 5.0}
METRICS = (
    "av_mesh_relay_session_datagrams_total", "av_mesh_relay_session_objects_total",
    "av_mesh_relay_session_fec_recovered_source_symbols_total", "av_mesh_relay_session_drops_total",
    "av_mesh_relay_session_forwarded_datagrams_total", "av_mesh_fec_tx_datagrams_total",
    "av_mesh_fec_tx_objects_total", "av_mesh_fec_rx_datagrams_total", "av_mesh_fec_rx_objects_total",
    "av_mesh_fec_rx_repaired_source_datagrams_total", "av_mesh_fec_rx_presumed_lost_source_datagrams_total",
    "av_mesh_fec_rx_late_source_datagrams_total", "av_mesh_relay_failover_transitions_total",
    "av_contrib_relay_session_datagrams_total", "av_contrib_relay_session_send_errors_total",
    "av_contrib_audio_epoch_hls_raptorq_fragments_recovered_total",
)

# Runs on lon-src, inside the lab: the Cloud Firewall admits lab nodes only.
REMOTE = r'''
import concurrent.futures, json, re, subprocess, sys, time
cfg = json.load(sys.stdin)
CA, NAME = cfg["ca"], cfg["name"]

def curl(ip, port, path, headers_only=False, timeout=4):
    cmd = ["curl", "-s", "-m", str(timeout), "--cacert", CA, "--resolve", f"{NAME}:{port}:{ip}",
           f"https://{NAME}:{port}{path}"]
    if headers_only:
        cmd[1:1] = ["-o", "/dev/null", "-D", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout

def metrics(node):
    port = 19443 if node["role"] == "contributor" else 19444
    text = curl(node["ip"], port, "/metrics", timeout=6)
    keep = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        name = line.split("{")[0].split(" ")[0]
        if name in cfg["metrics"]:
            try:
                keep[line.rsplit(" ", 1)[0]] = float(line.rsplit(" ", 1)[1])
            except ValueError:
                pass
    return keep

def playlist(ip, stream):
    text = curl(ip, 19444, f"/live/{stream}/stream.m3u8")
    seq = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
    pdt = re.search(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)", text)
    parts = [int(m) for m in re.findall(r'URI="part(\d+)\.', text)]
    segs = [(float(d), name) for d, name in re.findall(r"#EXTINF:([\d.]+),\s*\n(\S+)", text)]
    durations = [float(d) for d in re.findall(r"#EXT-X-PART:DURATION=([\d.]+)", text)]
    return {"media_sequence": int(seq.group(1)) if seq else None, "pdt": pdt.group(1) if pdt else None,
            "parts": parts, "segments": len(segs), "first_segment_duration": segs[0][0] if segs else None,
            "part_duration": statistics_median(durations), "first_part": parts[0] if parts else None,
            "fetched": time.time()}

def statistics_median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else None

def stamp(node, stream, ext, part):
    head = curl(node["ip"], 19444, f"/live/{stream}/part{part}.{ext}", headers_only=True, timeout=4)
    m = re.search(r"x-needletail-cache-available-unix-us:\s*(\d+)", head, re.I)
    status = re.match(r"HTTP/\S+\s+(\d+)", head)
    return int(m.group(1)) if m else None, int(status.group(1)) if status else 0

def subscribed(node, stream):
    text = curl(node["ip"], 19444, "/api/mesh/local", timeout=6)
    try:
        local = json.loads(text)
    except ValueError:
        return False
    playlists = (local.get("cache_replication") or {}).get("subscribed_playlists") or []
    ids = {p if isinstance(p, int) else (p.get("stream_id") if isinstance(p, dict) else None) for p in playlists}
    return stream in ids

def snapshot(pool):
    return dict(zip(cfg["nodes"], pool.map(metrics, cfg["nodes"].values())))

out = {"streams": {}, "started": time.time()}
pool = concurrent.futures.ThreadPoolExecutor(48)
relay = cfg["nodes"]["relay-primary"]
edges = {n: v for n, v in cfg["nodes"].items() if v["role"] == "edge"}
for stream in cfg["streams"]:
    ext = "mp4" if stream < 2000 else "bin"
    # Edges that hold the stream by a live subscription, before and after the
    # sampling: never ask an edge for a stream it does not hold (that would
    # subscribe it and back-fill parts with late stamps).
    before = {n for n, ok in zip(edges, pool.map(lambda v: subscribed(v, stream), edges.values())) if ok}
    m0 = snapshot(pool)
    samples = []
    end = time.time() + cfg["seconds"]
    while True:
        samples.append(playlist(relay["ip"], stream))
        if time.time() + 1.0 >= end:
            break
        time.sleep(1.0)
    after = {n for n, ok in zip(edges, pool.map(lambda v: subscribed(v, stream), edges.values())) if ok}
    last = playlist(relay["ip"], stream)          # the head just before fetching
    if not last["parts"]:
        out["streams"][str(stream)] = {"error": "no parts at relay-primary"}
        continue
    head = last["parts"][-1]
    duration_ms = (last["part_duration"] or 0.005) * 1000.0
    lo = head - int(cfg["max_age_s"] * 1000 / duration_ms)
    hi = head - int(cfg["min_age_s"] * 1000 / duration_ms)
    step = max(1, (hi - lo) // cfg["samples"])
    wanted = list(range(lo, hi + 1, step))
    nodes = {n: v for n, v in cfg["nodes"].items() if v["role"] in ("backbone", "distributor")}
    nodes.update({n: edges[n] for n in sorted(before & after)})
    jobs = [(name, part) for part in wanted for name in nodes]
    stamps = {}
    for (name, part), (value, status) in zip(jobs, pool.map(
            lambda job: stamp(nodes[job[0]], stream, ext, job[1]), jobs)):
        stamps.setdefault(name, {})[str(part)] = [value, status]
    m1 = snapshot(pool)
    out["streams"][str(stream)] = {"playlists": samples + [last], "parts": wanted, "stamps": stamps,
                                   "part_ms": duration_ms, "nodes": sorted(nodes),
                                   "edges_subscribed": sorted(before & after),
                                   "edges_changed": sorted(before ^ after),
                                   "metrics_before": m0, "metrics_after": m1}
out["ended"] = time.time()
print(json.dumps(out))
'''


def lab_nodes() -> dict:
    state = common.load_state()
    return {name: {"ip": node["public_ipv4"], "role": node["role"], "site": node["site"]}
            for name, node in state["nodes"].items()
            if node["role"] in ("contributor", "backbone", "distributor", "edge")}


def run(args: argparse.Namespace) -> None:
    state = common.load_state()
    source = state["nodes"]["lon-src"]
    cfg = {"ca": CA, "name": NAME, "nodes": lab_nodes(), "streams": [int(s) for s in args.streams.split(",")],
           "seconds": args.seconds, "min_age_s": args.min_age, "max_age_s": args.max_age,
           "samples": args.samples, "metrics": list(METRICS)}
    common.ssh(source, "cat >/var/tmp/mesh-hops-remote.py", stdin=REMOTE.encode())
    text = common.ssh_text(source, "python3 /var/tmp/mesh-hops-remote.py", stdin=json.dumps(cfg).encode(),
                           timeout=600)
    result = json.loads(text)
    result["config"] = {key: value for key, value in cfg.items() if key not in ("ca", "metrics")}
    nt_start = common.ssh_text(source, "cat /var/lib/compare/src/nt.start 2>/dev/null", check=False).strip()
    result["nt_start"] = int(nt_start) if nt_start.isdigit() else None
    result["label"] = args.label
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps(result) + "\n")
    print(json.dumps(summary(result), indent=1)[:6000])


def pdt_line(stream: dict) -> tuple[float, float] | None:
    """Publish time of part N as a + N * part_ms, from the program date and time."""
    points = []
    for sample in stream["playlists"]:
        if not sample.get("pdt") or sample.get("media_sequence") is None:
            continue
        # The tag is the publish time of the first part of the oldest segment,
        # less that part's duration (av-mesh main.rs).
        when = calendar.timegm(time.strptime(sample["pdt"][:19], "%Y-%m-%dT%H:%M:%S"))
        when += float("0." + sample["pdt"][20:23]) if len(sample["pdt"]) > 20 else 0.0
        if stream["part_ms"] < 10:            # audio: 200 parts in a segment
            first = sample["media_sequence"] * 200
        elif sample.get("first_part") is not None:
            continue                            # video: segments are not a fixed number of parts
        else:
            continue
        points.append((first, when * 1e6 + stream["part_ms"] * 1000))
    if not points:
        return None
    part_us = stream["part_ms"] * 1000
    offset = statistics.median(p - n * part_us for n, p in points)
    return offset, part_us


def dist(values: list[float]) -> dict | None:
    if not values:
        return None
    v = sorted(values)
    pick = lambda q: round(v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))], 2)
    return {"n": len(v), "p50": pick(0.5), "p95": pick(0.95), "p99": pick(0.99), "max": round(v[-1], 2),
            "min": round(v[0], 2)}


def summary(result: dict) -> dict:
    nodes = result["config"]["nodes"]
    out = {"label": result.get("label"), "streams": {}}
    for stream_id, stream in result["streams"].items():
        if "error" in stream:
            out["streams"][stream_id] = stream
            continue
        # The program date and time gives the publish time of one part a second,
        # to the millisecond (truncated). A line through it stands for every part
        # only when parts are published at a steady cadence: the AEP1 PCM sender
        # (4001). The DAW source hands parts over in bursts of 512 samples, so for
        # its streams only the hops between nodes are reported.
        line = pdt_line(stream) if stream_id.startswith("400") else None
        stamps = stream["stamps"]
        ok = lambda name, part: (stamps.get(name, {}).get(str(part)) or [None, 0])[0]
        hops: dict[str, list[float]] = {}
        missing: dict[str, int] = {}
        for part in stream["parts"]:
            rp, rs = ok("relay-primary", part), ok("relay-secondary", part)
            publish = line[0] + part * line[1] if line else None
            if publish and rp:
                hops.setdefault("contributor->relay-primary (London, VLAN)", []).append((rp - publish) / 1000)
            if publish and rs:
                hops.setdefault("contributor->relay-secondary (Amsterdam)", []).append((rs - publish) / 1000)
            for site in ("london", "newyork", "tokyo", "sydney"):
                dists = [n for n, v in nodes.items() if v["site"] == site and v["role"] == "distributor"]
                edges = [n for n, v in nodes.items() if v["site"] == site and v["role"] == "edge" and n in stamps]
                dvals = [ok(n, part) for n in dists]
                for n, value in zip(dists, dvals):
                    if value is None:
                        missing[n] = missing.get(n, 0) + 1
                        continue
                    if rp:
                        hops.setdefault(f"relay-primary->distributor ({site})", []).append((value - rp) / 1000)
                    if publish:
                        hops.setdefault(f"contributor->distributor ({site})", []).append((value - publish) / 1000)
                first_dist = min([v for v in dvals if v], default=None)
                for e in edges:
                    value = ok(e, part)
                    if value is None:
                        missing[e] = missing.get(e, 0) + 1
                        continue
                    if first_dist:
                        hops.setdefault(f"distributor->edge ({site})", []).append((value - first_dist) / 1000)
                    if publish:
                        hops.setdefault(f"contributor->edge ({site})", []).append((value - publish) / 1000)
        out["streams"][stream_id] = {
            "part_ms": stream["part_ms"], "parts_sampled": len(stream["parts"]),
            "reference": ("program date and time, truncated to the millisecond: contributor hops "
                          "carry up to 1 ms of bias") if line else "between nodes only",
            "hops_ms": {name: dist(values) for name, values in sorted(hops.items())},
            "parts_missing_at": missing,
        }
    for stream_id, stream in result["streams"].items():
        before, after = stream.get("metrics_before") or {}, stream.get("metrics_after") or {}
        deltas = {}
        for name in after:
            for key, value in after[name].items():
                delta = value - before.get(name, {}).get(key, 0.0)
                if delta:
                    deltas.setdefault(name, {})[key] = round(delta)
        if stream_id in out["streams"] and isinstance(out["streams"][stream_id], dict):
            out["streams"][stream_id]["counter_deltas"] = deltas
            out["streams"][stream_id]["edges_subscribed"] = stream.get("edges_subscribed")
    out["seconds"] = round(result["ended"] - result["started"], 1)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--streams", default="4001,3001")
    r.add_argument("--out", required=True)
    r.add_argument("--label", default="")
    r.add_argument("--seconds", type=int, default=6, help="program date and time samples, one a second")
    r.add_argument("--min-age", type=float, default=1.0)
    r.add_argument("--max-age", type=float, default=9.0)
    r.add_argument("--samples", type=int, default=200)
    r.set_defaults(handler=run)
    s = sub.add_parser("summary")
    s.add_argument("files", nargs="+")
    s.set_defaults(handler=lambda a: [print(json.dumps(summary(json.loads(pathlib.Path(f).read_text())),
                                                       indent=1)) for f in a.files])
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
