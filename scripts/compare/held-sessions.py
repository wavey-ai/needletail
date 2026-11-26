#!/usr/bin/env python3
"""Score a held-sessions step (compare-run.py --held-sessions) one session at a time.

The probes run through several sessions of the source; between sessions only
the video sender restarts (lead, 26 Nov, integration 6 cell 4). A probe's frame
times are on the wall clock (ntcmp numbers a frame by its due time), so each
frame belongs to the session whose window holds its source time (completion
minus latency). Each session's frames are scored as compare-analyse.py scores
a step (the first 5 s after a session's first frame and the last 3 s left out),
and a region carries a session by the record's rule: the better of its two UDP
viewers has at least 99.5% of the frames on time and no stall over 500 ms.

A restarted sender's catch-up burst (lead, 27 Nov: ffmpeg holds the first
~0.66 s of a publish while it probes its input, then sends it 1-3 ms a frame,
so lateness against the frames' due times climbs and drains over the first
second) falls inside each session's first 5 s, which the scoring leaves out as
it does a step's. compare-run.py --sender-start realtime removes the burst at
the source.

    held-sessions.py SESSION_DIR [--step nt.throughput.2160p240] [--json out.json]
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import pathlib
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("compare_analyse", HERE / "compare-analyse.py")
analyse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyse)

SITES = ["lon", "new", "tok", "syd"]
LABELS = {"probe.uc": "UDP", "probe": "LL-HLS"}


def windows(meta: dict, end_unix: float) -> list[tuple[float, float]]:
    """(start, end) Unix seconds of each session."""
    starts = [meta["session1_started_unix"]]
    ends = []
    for restart in sorted(meta.get("restarts") or [], key=lambda r: r.get("session", 0)):
        if "stopped_unix" not in restart:
            continue
        ends.append(restart["stopped_unix"])
        starts.append(restart["started_unix"])
    ends.append(end_unix)
    return list(zip(starts, ends))


def whole(probe: dict) -> bool:
    return bool(not probe.get("error") and probe.get("on_time_percent") is not None
                and probe["on_time_percent"] >= 99.5 and (probe.get("longest_stall_ms") or 0) <= 500)


def score_window(summary: dict, csv_path: pathlib.Path, lo: float, hi: float) -> dict:
    """compare-analyse's probe analysis of the frames whose source time is in [lo, hi) s."""
    with csv_path.open() as handle, tempfile.TemporaryDirectory() as directory:
        reader = csv.DictReader(handle)
        out_path = pathlib.Path(directory) / "window.csv"
        kept = 0
        with out_path.open("w", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                try:
                    source_us = int(row["complete_unix_us"]) - int(row["latency_us"])
                except (KeyError, ValueError):
                    continue
                if lo * 1e6 <= source_us < hi * 1e6:
                    writer.writerow(row)
                    kept += 1
        result = analyse.analyse_probe(dict(summary, error=None), out_path, None)
    result["rows"] = kept
    result["error"] = summary.get("error") if kept == 0 else None
    return result


def score(step_dir: pathlib.Path) -> dict:
    meta = json.loads((step_dir / "meta.json").read_text())
    end = meta.get("started_unix", 0) + meta.get("seconds", 0)
    spans = windows(meta, end)
    out = {"step": meta.get("step"), "session_seconds": meta.get("session_seconds"),
           "windows_unix": spans, "restarts": meta.get("restarts"), "sessions": []}
    for number, (lo, hi) in enumerate(spans, 1):
        regions = {}
        for site in SITES:
            region = {}
            for host in (f"{site}-view", f"{site}-nano"):
                for label in LABELS:
                    path = step_dir / host / f"{label}.csv"
                    if not path.is_file():
                        continue
                    summary = analyse.load(step_dir / host / f"{label}.json") or {}
                    result = score_window(summary, path, lo, hi)
                    region[f"{host}:{label}"] = {k: result.get(k) for k in (
                        "rows", "frames_expected", "frames_missing", "frames_late", "frames_undecodable",
                        "on_time_percent", "longest_stall_ms", "stalls", "latency_ms", "window_s", "error")}
            udp = [v for k, v in region.items() if k.endswith(":probe.uc")]
            best = max(udp, key=lambda p: (whole(p), p.get("on_time_percent") or -1.0)) if udp else None
            regions[site] = {"probes": region, "udp_whole": None if best is None else whole(best),
                             "udp_whole_both": bool(udp) and all(whole(p) for p in udp) and len(udp) == 2}
        out["sessions"].append({"session": number, "start_unix": lo, "end_unix": hi, "regions": regions})
    return out


def table(result: dict) -> str:
    lines = ["| session | " + " | ".join(SITES) + " |", "|---|" + "---|" * len(SITES)]
    for s in result["sessions"]:
        cells = []
        for site in SITES:
            r = s["regions"][site]
            parts = []
            for key, p in sorted(r["probes"].items()):
                host, label = key.split(":")
                parts.append(f"{host.split('-')[1]} {LABELS[label]} {p.get('on_time_percent')}%/"
                             f"{p.get('longest_stall_ms')}ms")
            verdict = {True: "whole", False: "not whole", None: "no data"}[r["udp_whole"]]
            cells.append(f"{verdict} ({'; '.join(parts)})")
        lines.append(f"| {s['session']} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session_dir")
    parser.add_argument("--step", default="nt.throughput.2160p240")
    parser.add_argument("--json")
    args = parser.parse_args()
    result = score(pathlib.Path(args.session_dir) / args.step)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1) + "\n")
    print(table(result))


if __name__ == "__main__":
    main()
