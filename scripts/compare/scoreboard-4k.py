#!/usr/bin/env python3
"""The 4K scoreboard: Needletail against self-hosted LiveKit (an SFU in each
region), from one compare-run.py throughput session that swept both lanes
(owner, 24 Nov evening: "the main target for today was getting the 4k
throughput back").

Needletail is its UDP client from the edge (the probe.uc listeners), as on
the audio scoreboard; its LL-HLS listeners are shown beside it, not scored.
LiveKit is its WebRTC subscriber. The rule is the 4K record's
(compare-video-report.py): a region carried a rung when the better of its two
listeners (-view and -nano) had at least 99.5% of the frames on time and no
stall over 500 ms; a frame is on time when it arrived whole, decodable and not
more than 250 ms after the median.

Per region:
  highest rung carried whole (Mbit/s, higher is better), and the highest of
    an unbroken run from the bottom;
  at each rung both systems ran, at the better listener:
    frames on time (%, higher is better),
    delay after the frame was due, median (ms, lower is better),
    longest stall (ms, lower is better).

The two lanes are swept one after the other, not at once: with --host-stops
(host-stops.py's JSON) a cell whose own step had a stop of the source host or
of a viewer host of the region is marked ‡ for that system only.

    scoreboard-4k.py ANALYSIS.json --session SESSION [--host-stops HS.json]
                     [--out FILE.json] > FILE.md
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("compare_run", HERE / "compare-run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)

SITES = [("lon", "London"), ("new", "New York"), ("tok", "Tokyo"), ("syd", "Sydney")]
# system -> (step lane, probe label)
SYSTEMS = {"nt": ("nt", "probe.uc"), "nt-hls": ("nt", "probe"), "lkregion": ("lkregion", "probe")}
LANES = {"nt": "Needletail", "lkregion": "LiveKit"}


class Dir:
    def __init__(self, path):
        self.dir = path


def rung_mbit(rung: str) -> int:
    return int(rung.split("p", 1)[1])


def whole(probe: dict) -> bool:
    """compare-video-report.py's rule."""
    return bool(not probe.get("error") and probe.get("on_time_percent") is not None
                and probe["on_time_percent"] >= 99.5 and (probe.get("longest_stall_ms") or 0) <= 500)


def best(step: dict | None, label: str, site: str) -> dict | None:
    """The better of the region's two listeners of a probe label."""
    if not step or step.get("source_failed"):
        return None
    found = []
    for host in (f"{site}-view", f"{site}-nano"):
        probe = (((step.get("hosts") or {}).get(host) or {}).get("probes") or {}).get(label)
        if probe is not None:
            found.append((host, probe))
    if not found:
        return None
    host, p = max(found, key=lambda hp: (whole(hp[1]), hp[1].get("on_time_percent")
                                         if hp[1].get("on_time_percent") is not None else -1.0))
    return {"host": host, "whole": whole(p), "on_time": p.get("on_time_percent"),
            "p50": (p.get("latency_ms") or {}).get("p50"), "stall": p.get("longest_stall_ms"),
            "error": p.get("error")}


def stopped(host_stops: dict | None, session: str, step: str, site: str) -> dict:
    if not host_stops:
        return {}
    info = ((((host_stops.get("sessions") or {}).get(session) or {}).get("steps") or {}).get(step) or {})
    wanted = {"lon-src", f"{site}-view", f"{site}-nano"}
    return {h: v["worst_ms"] for h, v in (info.get("hosts") or {}).items()
            if h in wanted and v.get("class") == "measurement"}


def verdict(nt, lk, higher_better: bool) -> str:
    if nt is None or lk is None:
        return "not measured"
    if nt == lk:
        return "tie"
    return "win" if (nt > lk) == higher_better else "loss"


def rss_by_step(session: str) -> dict:
    """{step: {node: [MB at the step's start, peak MB in it]}}: each mesh
    node's av-mesh resident memory (the 1 s sampler's RSS), so that a memory
    or window fault shows beside the frames it cost (lead, 24 Nov)."""
    import re
    base = run.RUNS / session
    series = {}
    for path in sorted((base / "samplers").glob("*.log")):
        parts = path.name.split(".")
        if len(parts) != 3:          # NODE.UNIX.log only: the CPU sampler
            continue
        for line in path.read_text(errors="replace").splitlines():
            m = re.match(r"(\d+\.\d+) cpu .*?\| av-mesh \d+ \d+ (\d+)", line)
            if m:
                series.setdefault(parts[0], []).append((float(m.group(1)), int(m.group(2)) // 1024))
    out = {}
    for meta_path in sorted(base.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        start = meta.get("started_unix")
        if start is None:
            continue
        end = start + float(meta.get("seconds") or 0)
        nodes = {}
        for node, rows in series.items():
            inside = [mb for t, mb in rows if start <= t <= end]
            before = [mb for t, mb in rows if t <= start]
            if inside:
                nodes[node] = [before[-1] if before else inside[0], max(inside)]
        out[meta_path.parent.name] = nodes
    return out


def board(analysis: dict, session: str, host_stops: dict | None) -> dict:
    steps = analysis["sessions"][session]["steps"]
    rungs = sorted({s["rung"] for s in steps.values() if s.get("phase") == "throughput"}, key=rung_mbit)
    rows, ceilings = [], []
    for site, region in [(s, r) for s, r in SITES if s in run.SITES]:
        whole = {}
        for system, (lane, label) in SYSTEMS.items():
            ran = [r for r in rungs if f"{lane}.throughput.{r}" in steps]
            ok = {r: bool((best(steps[f"{lane}.throughput.{r}"], label, site) or {}).get("whole")) for r in ran}
            unbroken = 0
            for r in ran:
                if not ok[r]:
                    break
                unbroken = rung_mbit(r)
            whole[system] = {"highest": max((rung_mbit(r) for r in ran if ok[r]), default=0),
                             "unbroken": unbroken, "by_rung": ok, "ran": bool(ran)}
        if not (whole["nt"]["ran"] or whole["lkregion"]["ran"]):
            continue
        ceilings.append({"region": region, "needletail": whole["nt"], "livekit": whole["lkregion"],
                         "needletail_llhls": whole["nt-hls"],
                         "result": verdict(whole["nt"]["highest"] if whole["nt"]["ran"] else None,
                                           whole["lkregion"]["highest"] if whole["lkregion"]["ran"] else None,
                                           True)})
        for rung in rungs:
            cells = {}
            for system in ("nt", "lkregion"):
                lane, label = SYSTEMS[system]
                name = f"{lane}.throughput.{rung}"
                figures = best(steps.get(name), label, site)
                if figures is not None:
                    figures["host_stopped"] = stopped(host_stops, session, name, site) or None
                cells[system] = figures
            nt, lk = cells["nt"], cells["lkregion"]
            if nt is None and lk is None:
                continue
            for key, label, higher in (("on_time", "Frames on time (%)", True),
                                       ("p50", "Delay after due, median (ms)", False),
                                       ("stall", "Longest stall (ms)", False)):
                a = nt.get(key) if nt else None
                b = lk.get(key) if lk else None
                rows.append({"region": region, "rung": rung, "metric": key, "label": label,
                             "needletail": a, "livekit": b,
                             "needletail_host": nt.get("host") if nt else None,
                             "livekit_host": lk.get("host") if lk else None,
                             "needletail_stopped": nt.get("host_stopped") if nt else None,
                             "livekit_stopped": lk.get("host_stopped") if lk else None,
                             "result": verdict(None if a is None else round(a, 1),
                                               None if b is None else round(b, 1), higher)})
    return {"session": session, "rungs": rungs, "ceilings": ceilings, "rows": rows,
            "rss_mb": rss_by_step(session)}


def markdown(b: dict) -> str:
    out = []
    count = {}
    for row in b["ceilings"] + b["rows"]:
        count[row["result"]] = count.get(row["result"], 0) + 1
    out.append(f"Needletail against self-hosted LiveKit (an SFU in each region), 4K, session {b['session']}; "
               + ", ".join(f"{count.get(k, 0)} {k}" for k in ("win", "tie", "loss", "not measured"))
               + f" of {len(b['ceilings']) + len(b['rows'])} rows.\n")
    out.append("**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run "
               "from the bottom, where lower):\n")
    out.append("| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |")
    out.append("| --- | ---: | ---: | --- | ---: |")

    def ceiling(c):
        text = str(c["highest"] or "none")
        return text + (f" ({c['unbroken'] or 'none'})" if c["unbroken"] != c["highest"] else "")
    for c in b["ceilings"]:
        out.append(f"| {c['region']} | {ceiling(c['needletail'])} | {ceiling(c['livekit'])} | {c['result']} | "
                   f"{ceiling(c['needletail_llhls'])} |")
    out.append("")
    out.append("**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's "
               "WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or "
               "a viewer host):\n")
    out.append("| Rung | Region | Metric | Needletail | LiveKit | Result |")
    out.append("| --- | --- | --- | ---: | ---: | --- |")

    def cell(value, whole, stop, digits=1):
        if value is None:
            return "–"
        text = f"{value:.{digits}f}"
        if whole:
            text += " W"
        if stop:
            text += " ‡"
        return text
    for row in b["rows"]:
        first = row["metric"] == "on_time"
        out.append(f"| {row['rung'][5:] + ' Mbit/s' if first else ''} | {row['region'] if first else ''} | "
                   f"{row['label']} | "
                   f"{cell(row['needletail'], first and _whole(b, row, 'needletail'), row['needletail_stopped'])} | "
                   f"{cell(row['livekit'], first and _whole(b, row, 'livekit'), row['livekit_stopped'])} | "
                   f"{row['result']} |")
    rss = b.get("rss_mb") or {}
    steps = [f"nt.throughput.{r}" for r in b["rungs"] if rss.get(f"nt.throughput.{r}")]
    if steps:
        nodes = sorted({n for st in steps for n in rss[st]})
        out.append("")
        out.append("**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, "
                   "1 s sampler):\n")
        out.append("| Node | " + " | ".join(st.split(".")[-1][5:] + " Mbit/s" for st in steps) + " |")
        out.append("| --- |" + " ---: |" * len(steps))
        for node in nodes:
            cells = [f"{rss[st][node][0]} → {rss[st][node][1]}" if node in rss[st] else "–" for st in steps]
            out.append(f"| {node} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def _whole(b: dict, row: dict, system: str) -> bool:
    for c in b["ceilings"]:
        if c["region"] == row["region"]:
            return bool(c[system]["by_rung"].get(row["rung"]))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("analysis")
    parser.add_argument("--session", required=True)
    parser.add_argument("--host-stops")
    parser.add_argument("--out")
    args = parser.parse_args()
    analysis = json.loads(pathlib.Path(args.analysis).read_text())
    host_stops = json.loads(pathlib.Path(args.host_stops).read_text()) if args.host_stops else None
    b = board(analysis, args.session, host_stops)
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps({"analysis": args.analysis, **b}, indent=1) + "\n")
    print(markdown(b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
