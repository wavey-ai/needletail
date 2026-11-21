#!/usr/bin/env python3
"""Estimate what the comparison used of Cloudflare and LiveKit Cloud, from the
run records: for each step and each listener of a lane, the bytes it received
and the time it listened (the probes' own reports).

  usage.py SESSION...      (default: every session under target/compare/runs)

Cloudflare Stream bills WHEP by minutes delivered ($1 per 1,000); Cloudflare
Realtime by egress after 1,000 GB a month free ($0.05/GB); LiveKit Cloud's
free plan allows 50 GB of egress a month.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNS = ROOT / "target" / "compare" / "runs"
LANES = {"cf": "cloudflare_realtime", "whep": "cloudflare_stream_whep", "lkcloud": "livekit_cloud"}


def main() -> None:
    sessions = sys.argv[1:] or sorted(p.name for p in RUNS.iterdir() if p.is_dir())
    totals = {name: {"bytes": 0, "listener_seconds": 0.0, "listeners": 0} for name in LANES.values()}
    for session in sessions:
        for record in (RUNS / session).glob("*/*/*.json"):
            name = record.name
            lane = name.split(".")[0]
            if lane not in LANES or (".probe" not in name and ".player" not in name):
                continue
            try:
                data = json.loads(record.read_text())
            except (ValueError, OSError):
                continue
            entry = totals[LANES[lane]]
            count = int(data.get("count") or 1)
            seconds = float(data.get("duration_seconds") or data.get("duration") or 0) * count
            received = (data.get("video_bytes") or 0) + (data.get("audio_bytes") or 0)
            if not received and ".player" in name:
                received = 0      # Chrome reports no byte count; its minutes still count
            entry["bytes"] += int(received)
            entry["listener_seconds"] += seconds
            entry["listeners"] += count
    out = {}
    for name, entry in totals.items():
        gb = entry["bytes"] / 1e9
        minutes = entry["listener_seconds"] / 60.0
        item = {"gigabytes": round(gb, 2), "listener_minutes": round(minutes), "listeners": entry["listeners"]}
        if name == "cloudflare_stream_whep":
            item["list_price_usd"] = round(minutes / 1000.0, 2)
        if name == "cloudflare_realtime":
            item["list_price_usd"] = round(max(0.0, gb - 1000.0) * 0.05, 2)
        out[name] = item
    print(json.dumps({"sessions": len(sessions), "usage": out}, indent=1))


if __name__ == "__main__":
    main()
