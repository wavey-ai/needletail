#!/usr/bin/env python3
"""Print the tables of the audio comparison as Markdown, from the evidence
file of compare-audio-analyse.py.

A cell holds the median of the sessions and, in brackets, the lowest and the
highest session. Probe figures are from the smaller listener host of each region,
Chrome figures from the viewer host of the region.
"""

from __future__ import annotations

import argparse
import json
import statistics

SITES = [("lon", "London"), ("new", "New York"), ("tok", "Tokyo"), ("syd", "Sydney")]
SYSTEMS = [
    ("nt-uc", "Needletail, RaptorQ FEC over UDP from the edge"),
    ("nt", "Needletail, LL-HLS, reader as shipped"),
    ("nt-w", "Needletail, LL-HLS, pipelined reader, HTTP/2"),
    ("nt-h3", "Needletail, LL-HLS, pipelined reader, HTTP/3"),
    ("cf", "Cloudflare Realtime"),
    ("whep", "Cloudflare Stream WHIP/WHEP"),
    ("lkregion", "LiveKit, SFU in each region"),
    ("lkmulti", "LiveKit, multi-region set"),
    ("lkcloud", "LiveKit Cloud"),
]
NT_LANE = {"opus-2.5": "nt-opus", "opus-5": "nt-opus", "opus-10": "nt-opus", "opus-20": "nt-flac",
           "opus-max510": "nt-flac", "pcm": "nt-pcm"}


def spread(values: list, digits=1) -> str:
    values = [float(v) for v in values if v is not None]
    if not values:
        return "–"
    middle = statistics.median(values)
    if len(values) == 1 or max(values) - min(values) < 10 ** -digits:
        return f"{middle:.{digits}f}"
    return f"{middle:.{digits}f} ({min(values):.{digits}f}–{max(values):.{digits}f})"


def fmt(value, digits=1):
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


class Evidence:
    def __init__(self, path: str):
        self.data = json.load(open(path))
        self.sessions = list(self.data["sessions"])

    def lane(self, system: str, config: str) -> str:
        if system in ("nt-udp", "nt-uc"):
            return system
        return NT_LANE[config] + system[2:] if system.startswith("nt") else system

    def get(self, session: str, step: str, host: str, lane: str, kind: str):
        try:
            return self.data["sessions"][session]["steps"][step]["hosts"][host]["lanes"][lane][kind]
        except KeyError:
            return None

    def values(self, step: str, host: str, lane: str, kind: str, path: list[str]) -> list:
        out = []
        for session in self.sessions:
            value = self.get(session, step, host, lane, kind)
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if isinstance(value, (int, float)):
                out.append(value)
        return out

    def cell(self, step: str, host: str, lane: str, kind: str, path: list[str], digits=1) -> str:
        return spread(self.values(step, host, lane, kind, path), digits)


def table(header: list[str], rows: list[list[str]]) -> None:
    print("| " + " | ".join(header) + " |")
    print("| " + " | ".join("---" if i == 0 else "---:" for i in range(len(header))) + " |")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print()


def baseline(e: Evidence, config: str, title: str) -> None:
    step = f"audio.{config}.baseline.clean"
    print(f"### {title}\n")
    header = ["System", "Frame"] + [name for _, name in SITES]
    for label, path in (("Mouth to ear, played on arrival, median (ms)", ["mouth_to_ear_ms", "p50"]),
                        ("Mouth to ear, played on arrival, p95 (ms)", ["mouth_to_ear_ms", "p95"]),
                        ("Mouth to ear, played on arrival, p99 (ms)", ["mouth_to_ear_ms", "p99"]),
                        ("Jitter: p99 minus median of the unit latency (ms)", ["jitter_p99_minus_p50_ms"]),
                        ("Units missing (%)", ["missing_percent"]),
                        ("Units bit-exact (%)", ["bit_exact_percent"]),
                        ("Time from the listener's start to its first audio (ms)", ["first_audio_ms"]),
                        ("Bit rate at the listener (kbit/s)", ["kbit_per_second"])):
        print(f"{label}:\n")
        rows = []
        for system, name in SYSTEMS:
            lane = e.lane(system, config)
            cells = [e.cell(step, f"{site}-nano", lane, "probe", path, 3 if "percent" in path[0] else 1)
                     for site, _ in SITES]
            if all(cell == "–" for cell in cells):
                continue
            frames = e.values(step, "lon-nano", lane, "probe", ["frame_ms"])
            frame = f"{frames[0]:g}" if frames else "–"
            rows.append([name + (f" ({lane.split('-')[1]})" if system.startswith("nt") else ""), frame + " ms"] + cells)
        table(header, rows)


def chrome(e: Evidence, config: str) -> None:
    step = f"audio.{config}.baseline.clean"
    header = ["System"] + [name for _, name in SITES]
    for label, path in (("Chrome mouth to ear at the marker, median (ms)", ["marker_mouth_to_ear_ms", "p50"]),
                        ("Chrome jitter buffer, mean (ms)", ["jitter_buffer_ms"]),
                        ("Chrome concealed samples (%)", ["concealed_percent"])):
        print(f"{label}:\n")
        rows = []
        for system, name in SYSTEMS[4:]:
            rows.append([name] + [e.cell(step, f"{site}-view", system, "chrome", path,
                                         3 if "percent" in path[0] else 1) for site, _ in SITES])
        table(header, rows)


def sweep(e: Evidence) -> None:
    print("### Frame length\n")
    print("Mouth to ear, played on arrival, median (ms), London / Sydney:\n")
    configs = ["opus-2.5", "opus-5", "opus-10", "opus-20", "opus-max510"]
    rows = []
    for system, name in SYSTEMS:
        row = [name]
        for config in configs:
            lane = e.lane(system, config)
            step = f"audio.{config}.baseline.clean"
            values = []
            for site in ("lon", "syd"):
                per = [e.get(s, step, f"{site}-nano", lane, "probe") for s in e.sessions]
                per = [((p or {}).get("mouth_to_ear_ms") or {}).get("p50") for p in per]
                per = [v for v in per if v is not None]
                values.append(fmt(statistics.median(per)) if per else "–")
            row.append(" / ".join(values) + (f" ({lane.split('-')[1]})" if system.startswith("nt") else ""))
        rows.append(row)
    table(["System", "2.5 ms", "5 ms", "10 ms", "20 ms", "10 ms at 510 kbit/s"], rows)


def impair(e: Evidence, config: str) -> None:
    print(f"### Impairment, {config}\n")
    profiles = [("clean", "baseline", "none"), ("loss1", "impair", "1% loss"), ("loss5", "impair", "5% loss"),
                ("burst", "impair", "burst loss"), ("delay100", "impair", "100 ms delay")]
    for label, kind, path, digits in (
            ("Units missing at the listener (%), London / Sydney", "probe", ["missing_percent"], 2),
            ("Longest gap (ms), London / Sydney", "probe", ["longest_gap_ms"], 1),
            ("Mouth to ear, played on arrival, p99 (ms), London / Sydney", "probe", ["mouth_to_ear_ms", "p99"], 0),
            ("Chrome concealed samples (%), London / Sydney", "chrome", ["concealed_percent"], 2),
            ("Chrome mouth to ear at the marker, median (ms), London / Sydney", "chrome",
             ["marker_mouth_to_ear_ms", "p50"], 0)):
        if kind == "chrome" and config == "pcm":
            continue
        print(f"{label}:\n")
        rows = []
        for system, name in SYSTEMS:
            if kind == "chrome" and system.startswith("nt"):
                continue
            lane = e.lane(system, config)
            row = [name + (f" ({lane.split('-')[1]})" if system.startswith("nt") else "")]
            empty = True
            for detail, phase, _ in profiles:
                step = f"audio.{config}.{phase}.{detail}"
                host = "nano" if kind == "probe" else "view"
                cells = []
                for site in ("lon", "syd"):
                    per = []
                    for s in e.sessions:
                        value = e.get(s, step, f"{site}-{host}", lane, kind)
                        for key in path:
                            value = value.get(key) if isinstance(value, dict) else None
                        if value is not None:
                            per.append(value)
                    cells.append(spread(per, digits))
                    empty = empty and not per
                row.append(" / ".join(cells))
            if not empty:
                rows.append(row)
        table(["System"] + [name for _, _, name in profiles], rows)


def fan_entry(e: Evidence, session: str, config: str, total: str, site: str, lane: str) -> dict | None:
    """The listeners of one region in a fan-out step (either pass)."""
    for phase in ("fanout", "fanoutb"):
        hosts = e.data["sessions"][session]["steps"].get(f"audio.{config}.{phase}.{total}", {}).get("hosts", {})
        entry = (hosts.get(f"{site}-view", {}).get("lanes", {}) or {}).get(lane)
        if not entry:
            continue
        if "listeners" in entry:
            return entry["listeners"]
        if "probe" in entry:
            p = entry["probe"]
            received = p.get("units")
            if received is None and p.get("units_expected") is not None:
                received = p["units_expected"] - (p.get("units_missing") or 0)
            ok = not p.get("error") and (received or 0) > 100 and bool(p.get("unit_latency_ms"))
            return {"listeners": 1, "listeners_with_audio": 1 if ok else 0,
                    "unit_latency_p50_ms_median_listener": (p.get("unit_latency_ms") or {}).get("p50"),
                    "unit_latency_p99_ms_worst_listener": (p.get("unit_latency_ms") or {}).get("p99"),
                    "missing_percent": p.get("missing_percent")}
    return None


def fanout(e: Evidence, config: str) -> None:
    print(f"### Listeners, {config}\n")
    columns = [("1", "lon", "1, London"), ("10", "lon", "10: London"), ("10", "syd", "10: Sydney"),
               ("100", "lon", "100: London"), ("100", "new", "100: New York"),
               ("100", "tok", "100: Tokyo"), ("100", "syd", "100: Sydney")]
    for label, key, digits in (
            ("Listeners with audio, of those started in the region", None, 0),
            ("Unit latency, median listener's median (ms)", "unit_latency_p50_ms_median_listener", 1),
            ("Unit latency, worst listener's p99 (ms)", "unit_latency_p99_ms_worst_listener", 0),
            ("Units missing (%)", "missing_percent", 2)):
        print(f"{label}:\n")
        rows = []
        for system, name in SYSTEMS:
            lane = e.lane(system, config)
            row = [name + (f" ({lane.split('-')[1]})" if system.startswith("nt") else "")]
            empty = True
            for total, site, _ in columns:
                cells, numbers = [], []
                for session in e.sessions:
                    entry = fan_entry(e, session, config, total, site, lane)
                    if entry is None:
                        continue
                    empty = False
                    if key is None:
                        cells.append(f"{entry['listeners_with_audio']}/{entry['listeners']}")
                    else:
                        value = entry.get(key)
                        if value is not None and key == "missing_percent":
                            value = max(0.0, value)     # late arrivals can make the count negative
                        numbers.append(value)
                row.append((", ".join(cells) or "–") if key is None else spread(numbers, digits))
            if not empty:
                rows.append(row)
        table(["System"] + [name for _, _, name in columns], rows)


def nodes(e: Evidence, config: str) -> None:
    print(f"### Processor of the nodes, {config}\n")
    print("Percent of one vCPU used by the service, during the step:\n")
    wanted = [("contrib-lon", "av-contrib", "Needletail contributor"),
              ("relay-primary", "av-mesh", "Needletail backbone relay"),
              ("lon-dist-a", "av-mesh", "Needletail distributor, London"),
              ("lon-edge-1", "av-mesh", "Needletail edge 1, London"),
              ("lon-edge-2", "av-mesh", "Needletail edge 2, London"),
              ("syd-edge-1", "av-mesh", "Needletail edge 1, Sydney"),
              ("lon-lk", "livekit-server", "LiveKit, London node (both servers)"),
              ("syd-lk", "livekit-server", "LiveKit, Sydney node (both servers)")]
    steps = [("baseline.clean", "1 per host"), ("fanout.10", "10"), ("fanout.100", "100, first pass"),
             ("fanoutb.100", "100, second pass")]
    rows = []
    for node, process, name in wanted:
        row = [name]
        for step, _ in steps:
            numbers = []
            for session in e.sessions:
                usage = e.data["sessions"][session]["steps"].get(f"audio.{config}.{step}", {}).get("nodes", {})
                numbers.append((usage.get(node) or {}).get(process + "_cpu_percent"))
            row.append(spread(numbers, 0))
        rows.append(row)
    table(["Node"] + [name for _, name in steps], rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("evidence")
    parser.add_argument("--sessions", default="", help="only these sessions, comma separated")
    args = parser.parse_args()
    e = Evidence(args.evidence)
    if args.sessions:
        e.sessions = [name for name in args.sessions.split(",") if name in e.sessions]
    print(f"Sessions: {', '.join(e.sessions)}\n")
    baseline(e, "opus-2.5", "Opus, shortest frame (2.5 ms; Needletail 5 ms)")
    chrome(e, "opus-2.5")
    sweep(e)
    baseline(e, "pcm", "Lossless (PCM; SFUs by data channel)")
    baseline(e, "opus-20", "Needletail FLAC (beside 20 ms Opus on the SFUs)")
    impair(e, "opus-2.5")
    impair(e, "pcm")
    fanout(e, "opus-2.5")
    fanout(e, "pcm")
    nodes(e, "opus-2.5")
    nodes(e, "pcm")


if __name__ == "__main__":
    main()
