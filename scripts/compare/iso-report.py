#!/usr/bin/env python3
"""The 4K isolation lab's report (scripts/linode-4k-isolate.py): one row a
cell, the limit of each configuration and its cause, and the A/B deltas.

    iso-report.py RUN [--out FILE.md] [--json FILE.json]

RUN is a run id (~/needletail-data/<run id>/steps/, NEEDLETAIL_DATA_ROOT) or
a directory holding steps/<cell>/. Each step directory has what the driver
left: cell.json, viewer/ (the referee's probe.uc, probe.h3 and nt-uc
records), nodes/<node>/ (snapshot-start.txt, snapshot-end.txt,
api-node.json, api-mesh-local.json, hops-video.ndjson) and
samplers/<node>/ (the slice of the referee's samplers and the extra
sampler written during the cell).

A row:
  whole          the UDP 4K viewer by the referee's rule (compare-analyse.py
                 analyse_probe, loaded by path from NEEDLETAIL_COMPARE_ROOT;
                 scoreboard-4k.py's `whole`: at least 99.5% of the frames on
                 time, no stall over 500 ms; a frame is on time when it
                 arrived whole, decodable and at most 250 ms after the median)
  on time, p50/p99/max delivery latency, longest stall; the LL-HLS viewer's
                 on time; the audio listener's missing parts and p99
  lateness       each part's arrival at the viewer less its publication,
                 less the median of that: worst and p99 (ms)
  late by hop    the parts more than LATE_MS late, each put on the hop of
                 its path whose time was furthest above that hop's median
                 (the hop journals joined on the part: published ->
                 contributor sent -> forwarder relay_first -> forwarder
                 relay_forward -> edge relay_first -> edge udp_sent ->
                 viewer kernel -> viewer arrival)
  drops          per node, start to end of the cell: each UDP socket's own
                 drop counter (ss -uamn, d), the kernel's Udp RcvbufErrors,
                 SndbufErrors and InErrors (nstat), softnet_stat drops and
                 time squeezes, NIC drops (ethtool -S, ip -s link), qdisc
                 drops (tc -s qdisc)
  CPU            per node over the probes' window: each vCPU's busy and
                 softirq share, each thread's share of a CPU and the CPU it
                 last ran on (the referee's sys sampler)
  tee            the video tee's CPU (video_tee_cpu_us over the window),
                 its lag (video_tee_lag_max_us), tee_dropped and
                 video_tee_parts_shed in the window, the video socket's
                 buffer, the video sockets' kernel drops (/api/mesh/local)
  RSS            each mesh node's av-mesh RSS at the start, the peak and the
                 end of the window (the referee's sampler, 1 s)
  stops          each node's longest heartbeat stop in the window (the
                 referee's SCHED_FIFO 99 heartbeat per vCPU)
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import pathlib
import re
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
CLK_TCK = 100
LATE_MS = 50.0
# A core this busy is saturated; a softirq share this high of one core is
# the receive path's; a heartbeat stop this long is the guest not run.
CORE_FULL_PERCENT = 95.0
SOFTIRQ_HIGH_PERCENT = 40.0
STOP_MS = 100.0
MEMORY_MB = 3200.0
# The path of a part, in order: (label, node key, stage). Node keys are the
# cell's: contrib, forwarder, edge; the viewer's from its records.
PATH = (("published", "viewer", "published"),
        ("contrib.sent", "contrib", "sent"),
        ("forwarder.relay_first", "forwarder", "relay_first"),
        ("forwarder.relay_forward", "forwarder", "relay_forward"),
        ("edge.relay_first", "edge", "relay_first"),
        ("edge.udp_sent", "edge", "udp_sent"),
        ("viewer.kernel", "viewer", "kernel"),
        ("viewer.arrival", "viewer", "arrival"))
SHAPES = {"one": ("lon-dist-a", "lon-edge-1"), "two": ("lon-dist-a", "lon-edge-1"),
          "d4": ("lon-dist-c4", "lon-edge-c4")}
CONTRIBUTOR = "contrib-lon"


def compare_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT",
                                       str(HERE.parent.parent.parent / "needletail-compare")))


_ANALYSE = None


def analyse_module():
    """The referee's compare-analyse.py, by path (None when it is not
    there: the row then has the probe's own counts only)."""
    global _ANALYSE
    if _ANALYSE is None:
        path = compare_root() / "scripts" / "compare" / "compare-analyse.py"
        if not path.is_file():
            _ANALYSE = False
        else:
            spec = importlib.util.spec_from_file_location("iso_compare_analyse", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _ANALYSE = module
    return _ANALYSE or None


def whole(probe: dict) -> bool:
    """scoreboard-4k.py's rule (compare-video-report.py's)."""
    return bool(not probe.get("error") and probe.get("on_time_percent") is not None
                and probe["on_time_percent"] >= 99.5 and (probe.get("longest_stall_ms") or 0) <= 500)


def load_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]


# ------------------------------------------------------------- the viewers

def score_probe(viewer: pathlib.Path, label: str) -> dict:
    summary = load_json(viewer / f"{label}.json")
    if summary is None:
        return {"error": f"no {label}.json"}
    module = analyse_module()
    if module is None:
        return {"error": summary.get("error"), "frames": summary.get("video_frames")}
    return module.analyse_probe(summary, viewer / f"{label}.csv", None)


def audio_figures(viewer: pathlib.Path) -> dict:
    client = load_json(viewer / "nt-uc.uc.json") or {}
    probe = load_json(viewer / "nt-uc.probe.json") or {}
    latency = probe.get("audio_latency_ms") or {}
    return {"audio_missing": client.get("missing_parts"),
            "audio_expected": client.get("expected_parts"),
            "audio_p50_ms": latency.get("p50"), "audio_p99_ms": latency.get("p99")}


def arrivals(viewer: pathlib.Path) -> dict[int, dict]:
    """The UDP 4K viewer's parts: {part: {published, kernel, arrival, edge}}."""
    out = {}
    path = viewer / "probe.uc.arrivals.csv"
    if not path.is_file():
        return out
    with path.open() as handle:
        for row in csv.DictReader(handle):
            try:
                part = int(row["part"])
            except (KeyError, ValueError):
                continue
            item = {}
            for key, column in (("published", "published_unix_us"), ("kernel", "kernel_unix_us"),
                                ("arrival", "arrival_unix_us")):
                try:
                    item[key] = int(row[column])
                except (KeyError, ValueError, TypeError):
                    pass
            item["edge"] = row.get("edge")
            out.setdefault(part, item)
    return out


# ---------------------------------------------------------------- the hops

def hop_events(path: pathlib.Path) -> dict[tuple[str, int], int]:
    """A node's hop journal: {(stage, seq): first t_us}."""
    out: dict[tuple[str, int], int] = {}
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
            key = (str(event["stage"]), int(event["seq"]))
            at = int(event["t_us"])
        except (ValueError, KeyError, TypeError):
            continue
        if key not in out or at < out[key]:
            out[key] = at
    return out


def part_paths(parts: dict[int, dict], journals: dict[str, dict]) -> dict[int, list]:
    """Each part's times along PATH: [(label, t_us)], stages missing left
    out. `journals`: {"contrib": ..., "forwarder": ..., "edge": ...}."""
    out = {}
    for part, viewer in parts.items():
        times = []
        for label, where, stage in PATH:
            at = viewer.get(stage) if where == "viewer" else (journals.get(where) or {}).get(
                (stage, part))
            if at is not None:
                times.append((label, at))
        out[part] = times
    return out


def lateness(paths: dict[int, list]) -> dict:
    """Worst and p99 lateness of the parts (end to end less its median), the
    hops' medians and p99s, and the late parts by the hop that held them."""
    totals = {part: times[-1][1] - times[0][1] for part, times in paths.items()
              if len(times) >= 2 and times[0][0] == "published" and times[-1][0] == "viewer.arrival"}
    if not totals:
        return {}
    median = statistics.median(totals.values())
    late = {part: (value - median) / 1000.0 for part, value in totals.items()}
    hops: dict[str, list[float]] = {}
    for times in paths.values():
        for (a, ta), (b, tb) in zip(times, times[1:]):
            hops.setdefault(f"{a}->{b}", []).append((tb - ta) / 1000.0)
    medians = {hop: statistics.median(values) for hop, values in hops.items()}
    by_hop: dict[str, int] = {}
    for part, value in late.items():
        if value <= LATE_MS:
            continue
        times = paths[part]
        excess = {f"{a}->{b}": (tb - ta) / 1000.0 - medians.get(f"{a}->{b}", 0.0)
                  for (a, ta), (b, tb) in zip(times, times[1:])}
        if excess:
            hop = max(excess, key=excess.get)
            by_hop[hop] = by_hop.get(hop, 0) + 1
    values = list(late.values())
    return {"parts": len(values), "lateness_worst_ms": round(max(values), 1),
            "lateness_p99_ms": round(pct(values, 0.99), 1),
            "late_parts": sum(1 for value in values if value > LATE_MS),
            "late_by_hop": by_hop,
            "hops_ms": {hop: {"p50": round(medians[hop], 2), "p99": round(pct(v, 0.99), 2),
                              "max": round(max(v), 2)} for hop, v in hops.items()}}


# ------------------------------------------------------------ the counters

def sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    name = None
    for line in text.splitlines():
        if line.startswith("== "):
            name = line[3:].strip()
            out.setdefault(name, [])
        elif name is not None:
            out[name].append(line)
    return out


def counters(text: str) -> dict[str, float]:
    """A snapshot's counters, flat: nstat names, softnet.cpuN.dropped /
    .squeezed, sock.<local address>, ethtool.<if>.<name> (drops, errors,
    misses), link.<if>.rx_dropped/rx_overrun, qdisc.dropped."""
    out: dict[str, float] = {}
    parts = sections(text)
    for line in parts.get("nstat", []):
        words = line.split()
        if len(words) >= 2 and words[1].lstrip("-").isdigit():
            out[words[0]] = float(words[1])
    for cpu, line in enumerate(parts.get("softnet", [])):
        words = line.split()
        if len(words) >= 3:
            out[f"softnet.cpu{cpu}.dropped"] = float(int(words[1], 16))
            out[f"softnet.cpu{cpu}.squeezed"] = float(int(words[2], 16))
    lines = parts.get("sockets", [])
    for index, line in enumerate(lines):
        words = line.split()
        if len(words) >= 4 and words[0] in ("UNCONN", "ESTAB"):
            following = lines[index + 1] if index + 1 < len(lines) else ""
            match = re.search(r",d(\d+)\)", following) or re.search(r",d(\d+)\)", line)
            if match:
                key = f"sock.{words[3]}"
                out[key] = out.get(key, 0.0) + float(match.group(1))
    for name, body in parts.items():
        if name.startswith("ethtool "):
            interface = name.split()[1]
            for line in body:
                key, _, value = line.strip().partition(":")
                value = value.strip()
                if value.isdigit() and re.search(r"drop|err|miss|fifo|overrun|full", key):
                    out[f"ethtool.{interface}.{key.strip()}"] = float(value)
        if name.startswith("link "):
            interface = name.split()[1]
            for index, line in enumerate(body):
                if line.strip().startswith("RX:") and index + 1 < len(body):
                    numbers = body[index + 1].split()
                    if len(numbers) >= 4:
                        out[f"link.{interface}.rx_dropped"] = float(numbers[3])
                    if len(numbers) >= 5:
                        out[f"link.{interface}.rx_overrun"] = float(numbers[4])
    dropped = 0.0
    for line in parts.get("qdisc", []):
        match = re.search(r"dropped (\d+)", line)
        if match:
            dropped += float(match.group(1))
    out["qdisc.dropped"] = dropped
    for line in parts.get("iptables", []):
        words = line.split()
        if len(words) >= 3 and words[2] == "DROP" and words[0].isdigit():
            out["iptables.dropped"] = out.get("iptables.dropped", 0.0) + float(words[0])
    return out


def snapshot_rss(text: str) -> dict[str, float]:
    out = {}
    for line in sections(text).get("rss", []):
        words = line.split()
        if len(words) >= 3 and words[2].isdigit():
            out[words[0]] = max(out.get(words[0], 0.0), int(words[2]) / 1024.0)
    return out


def drops(node_dir: pathlib.Path) -> dict[str, float]:
    """The counters that moved over the cell (end less start), the
    contributor's drops at the blind forwarder (iptables) apart."""
    start = node_dir / "snapshot-start.txt"
    end = node_dir / "snapshot-end.txt"
    if not start.is_file() or not end.is_file():
        return {}
    before, after = counters(start.read_text()), counters(end.read_text())
    out = {}
    for key, value in after.items():
        delta = value - before.get(key, 0.0)
        if delta > 0 and (key.startswith(("sock.", "softnet.", "ethtool.", "link.", "qdisc.",
                                          "iptables."))
                          or key in ("UdpRcvbufErrors", "UdpSndbufErrors", "UdpInErrors",
                                     "Udp6RcvbufErrors", "IpExtInNoRoutes")):
            out[key] = delta
    return out


# ------------------------------------------------------------- the samplers

def window_lines(path: pathlib.Path, start: float, end: float):
    if not path.is_file():
        return
    for line in path.read_text(errors="replace").splitlines():
        head = line.split(" ", 1)[0]
        try:
            at = float(head)
        except ValueError:
            continue
        if start - 1.5 <= at <= end + 1.5:
            yield at, line


def cpu_figures(path: pathlib.Path, start: float, end: float) -> dict:
    """Per vCPU busy and softirq percent, and each thread's percent of a CPU
    with the CPU it last ran on, over [start, end]."""
    cpus: dict[str, list] = {}
    tasks: dict[str, list] = {}
    for at, line in window_lines(path, start, end) or []:
        words = line.split()
        if len(words) < 4 or words[1] != "S":
            continue
        if words[2] == "stat" and words[3].startswith("cpu"):
            values = [int(value) for value in words[4:12]]
            cpus.setdefault(words[3], []).append((at, values))
        elif words[2] == "task":
            text = line.split(" task ", 1)[1]
            pid, stat = text.split(" ", 1)
            try:
                comm = stat[stat.index("(") + 1:stat.rindex(")")]
                fields = stat[stat.rindex(")") + 2:].split()
                tid = stat.split(" ", 1)[0]
                ticks = int(fields[11]) + int(fields[12])
                last = int(fields[36])
            except (ValueError, IndexError):
                continue
            tasks.setdefault(f"{comm}/{tid}", []).append((at, ticks, last))
    out_cpus = {}
    for cpu, samples in sorted(cpus.items()):
        inside = [item for item in samples if start <= item[0] <= end] or samples
        first, last = inside[0], inside[-1]
        total = sum(last[1]) - sum(first[1])
        if total <= 0:
            continue
        idle = (last[1][3] + last[1][4]) - (first[1][3] + first[1][4])
        softirq = last[1][6] - first[1][6]
        out_cpus[cpu] = {"busy_percent": round(100.0 * (total - idle) / total, 1),
                         "softirq_percent": round(100.0 * softirq / total, 1),
                         "system_percent": round(100.0 * (last[1][2] - first[1][2]) / total, 1)}
    out_threads = {}
    for name, samples in tasks.items():
        inside = [item for item in samples if start <= item[0] <= end] or samples
        if len(inside) < 2 or inside[-1][0] <= inside[0][0]:
            continue
        share = 100.0 * (inside[-1][1] - inside[0][1]) / CLK_TCK / (inside[-1][0] - inside[0][0])
        if share >= 1.0:
            out_threads[name] = {"percent": round(share, 1), "last_cpu": inside[-1][2]}
    return {"cpus": out_cpus, "threads": dict(sorted(out_threads.items(),
                                                     key=lambda item: -item[1]["percent"]))}


def rss_figures(path: pathlib.Path, start: float, end: float) -> dict:
    """av-mesh's (or av-contrib's) RSS in MB at the start, the peak and the
    end of the window (the referee's sampler)."""
    values = []
    for at, line in window_lines(path, start, end) or []:
        found = re.findall(r"\| (av-mesh|av-contrib) \d+ \d+ (\d+)", line)
        if found:
            values.append((at, max(int(kb) for _, kb in found) / 1024.0))
    if not values:
        return {}
    return {"start_mb": round(values[0][1]), "peak_mb": round(max(v for _, v in values)),
            "end_mb": round(values[-1][1])}


def mesh_local(path: pathlib.Path, start: float, end: float) -> dict:
    """The video tee and sockets from /api/mesh/local samples (first and
    last in the window)."""
    samples = []
    for at, line in window_lines(path, start, end) or []:
        try:
            body = json.loads(line.split(" ", 1)[1])
        except (ValueError, IndexError):
            continue
        fast = ((body.get("relay_session") or {}).get("fast_plane") or {})
        if fast:
            samples.append((at, fast, (body.get("relay_session") or {})))
    if len(samples) < 2:
        return {}
    (t0, first, s0), (t1, last, s1) = samples[0], samples[-1]

    def delta(key, source0=first, source1=last):
        if source1.get(key) is None or source0.get(key) is None:
            return None
        return source1[key] - source0[key]

    wall_us = (t1 - t0) * 1e6
    cpu = delta("video_tee_cpu_us")
    kernel = {key: delta(key) for key in last if key.endswith("kernel_dropped")
              and isinstance(last.get(key), (int, float)) and (delta(key) or 0) > 0}
    return {"tee_cpu_percent": round(100.0 * cpu / wall_us, 1) if cpu is not None and wall_us else None,
            "tee_lag_max_ms": round((last.get("video_tee_lag_max_us") or 0) / 1000.0, 1),
            "tee_dropped": delta("tee_dropped"), "tee_parts_shed": delta("video_tee_parts_shed"),
            "tee_dropped_growing": delta("tee_dropped_growing"),
            "video_socket_buffer_mb": round((last.get("video_socket_buffer_bytes") or 0) / 2**20, 1),
            "video_loop_busy_max_ms": round((last.get("video_loop_busy_max_us") or 0) / 1000.0, 1),
            "kernel_dropped": kernel,
            "failover_promotions": delta("failover_promotions", s0, s1)}


def heartbeat_stop_ms(directory: pathlib.Path, start: float, end: float) -> float | None:
    worst = None
    for path in directory.glob("compare-heartbeat.*.log"):
        for line in path.read_text(errors="replace").splitlines():
            words = line.split()
            if len(words) < 6 or words[0] != "HB":
                continue
            try:
                at = int(words[3]) / 1e9
                late = int(words[5]) / 1e6
            except ValueError:
                continue
            if start <= at <= end:
                worst = max(worst or 0.0, late)
    return None if worst is None else round(worst, 1)


# ---------------------------------------------------------------- the row

def step_row(directory: pathlib.Path) -> dict:
    """One cell's row (see the module's docstring)."""
    directory = pathlib.Path(directory)
    cell = load_json(directory / "cell.json") or {}
    row = {key: cell.get(key) for key in ("name", "group", "shape", "rung", "clip", "mbit", "arm",
                                          "seconds", "scored", "cut", "restart_s")}
    viewer = directory / "viewer"
    udp = score_probe(viewer, "probe.uc")
    hls = score_probe(viewer, "probe.h3")
    latency = udp.get("latency_ms") or {}
    row.update({"whole": whole(udp), "on_time_percent": udp.get("on_time_percent"),
                "longest_stall_ms": udp.get("longest_stall_ms"),
                "frames_missing": udp.get("frames_missing"), "frames_late": udp.get("frames_late"),
                "latency_p50_ms": latency.get("p50"), "latency_p99_ms": latency.get("p99"),
                "latency_max_ms": latency.get("max"), "udp_error": udp.get("error"),
                "hls_whole": whole(hls), "hls_on_time_percent": hls.get("on_time_percent")})
    row.update(audio_figures(viewer))
    forwarder, edge = SHAPES.get(cell.get("shape") or "one", SHAPES["one"])
    row["forwarder"], row["edge"] = forwarder, edge
    nodes = directory / "nodes"
    journals = {"contrib": hop_events(nodes / CONTRIBUTOR / "hops-video.ndjson"),
                "forwarder": hop_events(nodes / forwarder / "hops-video.ndjson"),
                "edge": hop_events(nodes / edge / "hops-video.ndjson")}
    row["hop_events"] = {key: len(value) for key, value in journals.items()}
    row.update(lateness(part_paths(arrivals(viewer), journals)))
    start = cell.get("probes_from_unix") or 0.0
    end = cell.get("probes_to_unix") or start
    row["nodes"] = {}
    for node_dir in sorted(path for path in nodes.glob("*") if path.is_dir()) if nodes.is_dir() else []:
        name = node_dir.name
        samplers = directory / "samplers" / name
        figures = {"drops": drops(node_dir),
                   "cpu": cpu_figures(samplers / "compare-sys.log", start, end),
                   "rss": rss_figures(samplers / "compare-sampler.log", start, end),
                   "tee": mesh_local(samplers / "compare-meshlocal.log", start, end),
                   "stop_ms": heartbeat_stop_ms(samplers, start, end)}
        if not figures["rss"] and (node_dir / "snapshot-end.txt").is_file():
            figures["rss"] = {"end_mb": round(max(snapshot_rss(
                (node_dir / "snapshot-end.txt").read_text()).values() or [0]))}
        row["nodes"][name] = figures
    return row


def run_dir(run: str) -> pathlib.Path:
    path = pathlib.Path(run)
    if (path / "steps").is_dir():
        return path
    root = pathlib.Path(os.environ.get("NEEDLETAIL_DATA_ROOT") or "~/needletail-data").expanduser()
    return root / run


def rows(directory: pathlib.Path) -> list[dict]:
    steps = sorted((pathlib.Path(directory) / "steps").glob("*/cell.json"),
                   key=lambda path: (load_json(path) or {}).get("started_unix") or 0)
    return [step_row(path.parent) for path in steps]


# ------------------------------------------------------- limits and causes

def config_of(row: dict) -> str:
    return f"{row.get('shape')}/{row.get('arm')}"


def on_path(row: dict) -> list[str]:
    return [CONTRIBUTOR, row.get("forwarder") or "", row.get("edge") or ""]


def causes(row: dict) -> list[str]:
    """What the data of one cell shows was short, each with its numbers,
    in order: memory, a socket or kernel buffer, the backlog, the NIC, the
    send queue, the tee, a full core, softirq, a stopped guest, then the
    hop where the late parts went late."""
    out = []
    nodes = row.get("nodes") or {}
    if row.get("cut"):
        out.append(f"memory: {row['cut'].get('node')} RSS {row['cut'].get('rss_mb')} MB, the "
                   "source stopped")
    for name in on_path(row):
        rss = ((nodes.get(name) or {}).get("rss") or {}).get("peak_mb")
        if rss and rss >= MEMORY_MB:
            out.append(f"memory: {name} RSS peak {rss} MB")
    for name in on_path(row):
        figures = nodes.get(name) or {}
        dropped = figures.get("drops") or {}
        sockets = {key[5:]: value for key, value in dropped.items() if key.startswith("sock.")}
        if sockets:
            worst = max(sockets, key=sockets.get)
            out.append(f"socket buffer: {name} {worst} dropped {sockets[worst]:.0f}"
                       + (f" (+{len(sockets) - 1} other sockets)" if len(sockets) > 1 else ""))
        for key in ("UdpRcvbufErrors", "UdpSndbufErrors", "UdpInErrors"):
            if dropped.get(key):
                out.append(f"kernel: {name} {key} {dropped[key]:.0f}")
        backlog = {key: value for key, value in dropped.items()
                   if key.startswith("softnet.") and key.endswith(".dropped")}
        if backlog:
            out.append(f"backlog: {name} softnet drops "
                       + ", ".join(f"{key.split('.')[1]} {value:.0f}" for key, value in backlog.items()))
        nic = {key: value for key, value in dropped.items() if key.startswith(("ethtool.", "link."))}
        if nic:
            out.append(f"NIC: {name} " + ", ".join(f"{key} {value:.0f}" for key, value in nic.items()))
        if dropped.get("qdisc.dropped"):
            out.append(f"send queue: {name} qdisc dropped {dropped['qdisc.dropped']:.0f}")
        tee = figures.get("tee") or {}
        if (tee.get("tee_dropped") or 0) > 0 or (tee.get("tee_parts_shed") or 0) > 0:
            out.append(f"tee: {name} dropped {tee.get('tee_dropped')}, parts shed "
                       f"{tee.get('tee_parts_shed')}, lag max {tee.get('tee_lag_max_ms')} ms, "
                       f"{tee.get('tee_cpu_percent')}% of a CPU")
        if tee.get("kernel_dropped"):
            out.append(f"socket buffer: {name} video sockets "
                       + ", ".join(f"{key} {value}" for key, value in tee["kernel_dropped"].items()))
        cpu = figures.get("cpu") or {}
        threads = cpu.get("threads") or {}
        for core, values in (cpu.get("cpus") or {}).items():
            index = core.removeprefix("cpu")
            on_core = [(thread, item["percent"]) for thread, item in threads.items()
                       if str(item.get("last_cpu")) == index]
            top = max(on_core, key=lambda item: item[1]) if on_core else None
            if values["busy_percent"] >= CORE_FULL_PERCENT:
                out.append(f"core: {name} {core} {values['busy_percent']}% busy (softirq "
                           f"{values['softirq_percent']}%)"
                           + (f", its main thread {top[0]} {top[1]}%" if top else ""))
            elif values["softirq_percent"] >= SOFTIRQ_HIGH_PERCENT:
                out.append(f"softirq: {name} {core} {values['softirq_percent']}% of the core in "
                           f"softirq ({values['busy_percent']}% busy)")
        if (figures.get("stop_ms") or 0) >= STOP_MS:
            out.append(f"stop: {name} heartbeat {figures['stop_ms']} ms late (the guest not run)")
    late = row.get("late_by_hop") or {}
    if late:
        hop = max(late, key=late.get)
        out.append(f"late at: {hop} ({late[hop]} of {row.get('late_parts')} late parts)")
    return out


def limits(table: list[dict]) -> list[dict]:
    """Per configuration (shape/arm): the rungs run, the first not whole,
    the highest whole, and the causes at the first not whole (else at the
    highest rung run, as headroom)."""
    out = []
    by_config: dict[str, list[dict]] = {}
    for row in table:
        if row.get("scored") is False or row.get("group") in ("warmup", "soak"):
            continue
        by_config.setdefault(config_of(row), []).append(row)
    for config, items in by_config.items():
        items = sorted(items, key=lambda row: row.get("mbit") or 0)
        broken = [row for row in items if not row.get("whole")]
        whole_rows = [row for row in items if row.get("whole")]
        at = broken[0] if broken else items[-1]
        out.append({"config": config, "rungs": [row.get("rung") for row in items],
                    "first_not_whole": broken[0].get("rung") if broken else None,
                    "highest_whole": whole_rows[-1].get("rung") if whole_rows else None,
                    "judged_at": at.get("name"), "causes": causes(at)})
    return out


def headline(row: dict) -> dict:
    """The figures an A/B compares, from one row."""
    worst_core = 0.0
    softirq = 0.0
    drops_total = 0.0
    for name in on_path(row):
        figures = (row.get("nodes") or {}).get(name) or {}
        for values in ((figures.get("cpu") or {}).get("cpus") or {}).values():
            worst_core = max(worst_core, values["busy_percent"])
            softirq = max(softirq, values["softirq_percent"])
        drops_total += sum(value for key, value in (figures.get("drops") or {}).items()
                           if not key.startswith(("iptables.", "softnet.")) or key.endswith(".dropped"))
    return {"whole": row.get("whole"), "on_time": row.get("on_time_percent"),
            "p99_ms": row.get("latency_p99_ms"), "lateness_worst_ms": row.get("lateness_worst_ms"),
            "drops": drops_total, "worst_core": worst_core, "softirq": softirq}


def ab_deltas(table: list[dict]) -> list[dict]:
    """Each A/B cell against the default cell of the same rung on one
    forwarder (BASE); the node size against BASE too."""
    base = {row.get("rung"): row for row in table if row.get("group") == "base"}
    out = []
    for row in table:
        if row.get("group") != "ab":
            continue
        reference = base.get(row.get("rung"))
        if reference is None:
            continue
        a, b = headline(reference), headline(row)
        delta = {}
        for key in ("on_time", "p99_ms", "lateness_worst_ms", "drops", "worst_core", "softirq"):
            if a.get(key) is not None and b.get(key) is not None:
                delta[key] = round(b[key] - a[key], 2)
        out.append({"cell": row.get("name"), "arm": row.get("arm"), "rung": row.get("rung"),
                    "default_whole": a["whole"], "arm_whole": b["whole"], "delta": delta,
                    "better": better(a, b)})
    return out


def better(a: dict, b: dict) -> str:
    """Whether the arm is better, worse or no different from the default:
    whole first, then on time, then the worst lateness and the drops."""
    if bool(b["whole"]) != bool(a["whole"]):
        return "better" if b["whole"] else "worse"
    score = 0
    if a.get("on_time") is not None and b.get("on_time") is not None:
        score += (b["on_time"] > a["on_time"] + 0.2) - (b["on_time"] < a["on_time"] - 0.2)
    if a.get("lateness_worst_ms") is not None and b.get("lateness_worst_ms") is not None:
        score += ((b["lateness_worst_ms"] < 0.8 * a["lateness_worst_ms"] - 5)
                  - (b["lateness_worst_ms"] > 1.25 * a["lateness_worst_ms"] + 5))
    score += (b["drops"] < a["drops"]) - (b["drops"] > a["drops"])
    return "better" if score > 0 else "worse" if score < 0 else "same"


# ------------------------------------------------------------- the output

def fmt(value, digits=1) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "W" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def path_summary(row: dict) -> str:
    """The path nodes' worst core, softirq, drops, tee and RSS in one cell."""
    parts = []
    for name in on_path(row):
        figures = (row.get("nodes") or {}).get(name) or {}
        cpus = (figures.get("cpu") or {}).get("cpus") or {}
        busy = max((values["busy_percent"] for values in cpus.values()), default=None)
        soft = max((values["softirq_percent"] for values in cpus.values()), default=None)
        threads = (figures.get("cpu") or {}).get("threads") or {}
        top = next(iter(threads.items()), None)
        dropped = sum(value for key, value in (figures.get("drops") or {}).items()
                      if key.startswith("sock.") or key in ("UdpRcvbufErrors", "UdpSndbufErrors"))
        tee = figures.get("tee") or {}
        rss = (figures.get("rss") or {}).get("peak_mb") or (figures.get("rss") or {}).get("end_mb")
        short = name.replace("lon-", "").replace("contrib-lon", "contrib")
        parts.append(f"{short}: core {fmt(busy, 0)}% si {fmt(soft, 0)}%"
                     + (f" top {top[0].split('/')[0]} {top[1]['percent']:.0f}%@{top[1]['last_cpu']}"
                        if top else "")
                     + f" drops {dropped:.0f}"
                     + (f" tee {fmt(tee.get('tee_cpu_percent'), 0)}%/lag {fmt(tee.get('tee_lag_max_ms'), 0)}ms"
                        if tee else "")
                     + f" rss {fmt(rss)}")
    return "; ".join(parts)


def markdown(table: list[dict]) -> str:
    out = ["| cell | shape | arm | Mbit/s | whole | on time % | p50/p99 ms | stall ms | "
           "lateness worst/p99 ms | late by hop | LL-HLS % | audio missing/p99 | path nodes |",
           "| --- | --- | --- | ---: | --- | ---: | --- | ---: | --- | --- | ---: | --- | --- |"]
    for row in table:
        late = row.get("late_by_hop") or {}
        out.append(
            f"| {row.get('name')} | {row.get('shape')} | {row.get('arm')} | {fmt(row.get('mbit'))} | "
            f"{fmt(row.get('whole')) if row.get('scored') is not False else '(warm-up)'} | "
            f"{fmt(row.get('on_time_percent'), 2)} | {fmt(row.get('latency_p50_ms'))}/"
            f"{fmt(row.get('latency_p99_ms'))} | {fmt(row.get('longest_stall_ms'))} | "
            f"{fmt(row.get('lateness_worst_ms'))}/{fmt(row.get('lateness_p99_ms'))} | "
            + (", ".join(f"{hop} {count}" for hop, count in sorted(late.items(), key=lambda x: -x[1])[:2])
               or "-")
            + f" | {fmt(row.get('hls_on_time_percent'), 2)} | {fmt(row.get('audio_missing'))}/"
            f"{fmt(row.get('audio_p99_ms'))} | {path_summary(row)} |")
    out.append("")
    out.append("## Limit and cause per configuration")
    for item in limits(table):
        out.append(f"- **{item['config']}**: rungs {', '.join(item['rungs'])}; first not whole "
                   f"{item['first_not_whole'] or 'none'}; highest whole {item['highest_whole'] or 'none'}; "
                   f"at {item['judged_at']}: " + ("; ".join(item["causes"]) or "nothing short in the data"))
    out.append("")
    out.append("## A/B against the default (BASE, same rung)")
    for item in ab_deltas(table):
        out.append(f"- {item['cell']} ({item['arm']} at {item['rung']}): default "
                   f"{fmt(item['default_whole'])}, arm {fmt(item['arm_whole'])}, {item['better']}; "
                   + ", ".join(f"{key} {value:+g}" for key, value in item["delta"].items()))
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run")
    parser.add_argument("--out")
    parser.add_argument("--json")
    args = parser.parse_args()
    table = rows(run_dir(args.run))
    if not table:
        print(f"no steps in {run_dir(args.run)}", file=sys.stderr)
        return 1
    text = markdown(table)
    if args.out:
        pathlib.Path(args.out).write_text(text)
    else:
        print(text)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(
            {"rows": table, "limits": limits(table), "ab": ab_deltas(table)}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
