#!/usr/bin/env python3
"""Which hosts stopped during each step: the per-vCPU heartbeats
(samplers/NODE.heartbeat.T.log, SCHED_FIFO 99, since 472fb47) read against each
step's window.

The lead's hunt lab (hunt11241537, 24 Nov) showed fresh Linode guests not run
for up to 2.3 s while they first touch their memory, measurement hosts
included: the source host's stop silenced every listener in every region for
2.25 s, for every system alike. A step in which the source host or a
listener's own host stopped says nothing about either system, so the
scoreboard marks it (scoreboard.py --host-stops).

A stop is a heartbeat wake at least --threshold-ms late (20 ms by default: on
the hunt lab's warmed edges, one such wake in 57 minutes across three nodes).
Hosts are classed:
  measurement   lon-src (the source of every system), every -view and -nano
                host, and listener (load) hosts
  under test    the Needletail mesh and the LiveKit hosts: their stops are
                part of what is measured, and are listed, not excluded

    host-stops.py SESSION [SESSION ...] [--threshold-ms 20] --out FILE.json > FILE.md
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNS = ROOT / "target" / "compare" / "runs"


def measurement(host: str) -> bool:
    return host == "lon-src" or host.endswith(("-view", "-nano")) or "-load-" in host


def heartbeats(session_dir: pathlib.Path, threshold_ms: float) -> dict:
    """{host: sorted [(unix_s of the late wake, ms late, cpu)]} with each wake
    at least threshold_ms late, and {host: (first, last) unix_s covered}."""
    stops, cover = {}, {}
    for path in sorted((session_dir / "samplers").glob("*.heartbeat.*.log")):
        host = path.name.split(".")[0]
        for line in path.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 6 or parts[0] != "HB":
                continue
            try:
                at, late = int(parts[3]) / 1e9, int(parts[5]) / 1e6
            except ValueError:
                continue
            first, last = cover.get(host, (at, at))
            cover[host] = (min(first, at), max(last, at))
            if late >= threshold_ms:
                stops.setdefault(host, []).append((at, late, parts[1]))
    for values in stops.values():
        values.sort()
    return stops, cover


def score(session: str, threshold_ms: float) -> dict:
    session_dir = RUNS / session
    stops, cover = heartbeats(session_dir, threshold_ms)
    steps = {}
    for meta_path in sorted(session_dir.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        start = meta.get("started_unix")
        if start is None:
            continue
        end = start + float(meta.get("seconds") or 0)
        hosts = {}
        for host, values in stops.items():
            # A wake late by L ms ends a stop that began L ms earlier: it counts
            # for the window if the stop overlaps it.
            inside = [(at, late, cpu) for at, late, cpu in values if at - late / 1000 <= end and at >= start]
            if inside:
                hosts[host] = {"class": "measurement" if measurement(host) else "under test",
                               "stops": len(inside), "worst_ms": round(max(v[1] for v in inside), 1),
                               "worst_at_unix": round(max(inside, key=lambda v: v[1])[0], 3)}
        uncovered = sorted(h for h, (first, last) in cover.items() if first > start or last < end)
        steps[meta_path.parent.name] = {"started_unix": start, "seconds": round(end - start, 1),
                                        "hosts": hosts, "hosts_without_heartbeat_cover": uncovered}
    return {"session": session, "threshold_ms": threshold_ms, "heartbeat_hosts": sorted(cover),
            "steps": steps}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sessions", nargs="+")
    parser.add_argument("--threshold-ms", type=float, default=20.0)
    parser.add_argument("--out")
    parser.add_argument("--runs", default=None, help="the sessions' directory (default target/compare/runs)")
    args = parser.parse_args()
    if args.runs:
        globals()["RUNS"] = pathlib.Path(args.runs)
    result = {"schema": "host-stops.v1", "sessions": {s: score(s, args.threshold_ms) for s in args.sessions}}
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(result, indent=1) + "\n")
    print(f"Heartbeat stops of {args.threshold_ms:g} ms or more within each step "
          "(measurement hosts first; a measurement host's stop voids the step for every system).\n")
    print("| Session | Step | Measurement hosts stopped (worst ms) | Hosts under test stopped (worst ms) |")
    print("| --- | --- | --- | --- |")
    for session, data in result["sessions"].items():
        if not data["heartbeat_hosts"]:
            print(f"| {session} | (no heartbeat files) | – | – |")
            continue
        for step, info in data["steps"].items():
            def cell(kind):
                items = [f"{h} {v['worst_ms']:.0f}" for h, v in sorted(info["hosts"].items()) if v["class"] == kind]
                return ", ".join(items) or "none"
            print(f"| {session} | {step} | {cell('measurement')} | {cell('under test')} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
