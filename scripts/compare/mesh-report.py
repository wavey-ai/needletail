#!/usr/bin/env python3
"""Tables of the long-haul session (compare-mesh.py): where each millisecond of
a Needletail part goes, hop by hop, in each region and under each impairment of
the long haul, beside self-hosted LiveKit with the same impairment on its own
long haul (the source's upload to each region's SFU).

  mesh-report.py SESSION [--config pcm]
"""

from __future__ import annotations

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNS = ROOT / "target" / "compare" / "runs"
REGIONS = [("london", "London", "lon"), ("newyork", "New York", "new"), ("tokyo", "Tokyo", "tok"),
           ("sydney", "Sydney", "syd")]
PROFILES = ["clean", "loss1", "loss5", "burst", "delay100"]
GROUPS = [
    ("contributor", ("source -> contrib.src_last", "contrib.src_last -> contrib.packaged",
                     "contrib.packaged -> contrib.sent")),
    ("contributor to primary relay", ("contrib.sent -> primary.relay_first",)),
    ("primary relay", ("primary.relay_first -> primary.relay_forward",)),
    ("long haul: relay to distributor", ("primary.relay_forward -> parent.relay_first",)),
    ("distributor", ("parent.relay_first -> parent.committed", "parent.committed -> parent.watermark",
                     "parent.watermark -> parent.mesh_sent")),
    ("distributor to edge (VLAN)", ("parent.mesh_sent -> edge.mesh_first",)),
    ("edge", ("edge.mesh_first -> edge.edge_committed", "edge.edge_committed -> edge.reply")),
    ("edge to listener (HTTP/3)", ("edge.reply -> listener.complete",)),
]


def hop_table(hops: dict, kind: str) -> None:
    print("| Hop | " + " | ".join(name for _, name, _ in REGIONS) + " |")
    print("| --- | " + " | ".join("---:" for _ in REGIONS) + " |")
    for label, names in GROUPS:
        cells = []
        for region, _, _ in REGIONS:
            report = ((hops.get("regions") or {}).get(region) or {}).get(kind) or {}
            items = {item["hop"]: item for item in report.get("hops", [])}
            got = [items[name] for name in names if name in items]
            if not got:
                cells.append("–")
                continue
            cells.append(f"{sum(i['p50'] for i in got):.2f} / {sum(i['p99'] for i in got):.2f}")
        print(f"| {label} | " + " | ".join(cells) + " |")
    cells = []
    for region, _, _ in REGIONS:
        report = ((hops.get("regions") or {}).get(region) or {}).get(kind) or {}
        stage = {item["stage"]: item for item in report.get("from_source", [])}
        item = stage.get("listener.complete") or stage.get("edge.reply")
        cells.append(f"**{item['p50']:.1f} / {item['p99']:.1f}**" if item else "–")
    print("| source to listener | " + " | ".join(cells) + " |")
    print()


def livekit(session: pathlib.Path, config: str, profile: str) -> list[str]:
    cells = []
    for _, _, site in REGIONS:
        path = session / f"audio.{config}.mesh.{profile}" / "analysis.json"
        if not path.is_file():
            cells.append("–")
            continue
        step = json.loads(path.read_text())
        probes = [((step.get("hosts") or {}).get(f"{site}-{h}") or {}).get("lanes", {}).get("lkregion", {})
                  .get("probe") or {} for h in ("nano", "view")]
        units = [p.get("unit_latency_ms") for p in probes if p.get("unit_latency_ms")]
        missing = [p.get("missing_percent") for p in probes if p.get("missing_percent") is not None]
        cells.append(f"{units[0]['p50']:.1f} / {units[0]['p99']:.1f}, {max(missing):.2f}% lost" if units else "–")
    return cells


def needletail_udp(session: pathlib.Path, config: str, profile: str) -> list[str]:
    cells = []
    for _, _, site in REGIONS:
        path = session / f"audio.{config}.mesh.{profile}" / "analysis.json"
        if not path.is_file():
            cells.append("–")
            continue
        step = json.loads(path.read_text())
        probes = [((step.get("hosts") or {}).get(f"{site}-{h}") or {}).get("lanes", {}).get("nt-uc", {})
                  .get("probe") or {} for h in ("nano", "view")]
        units = [p.get("unit_latency_ms") for p in probes if p.get("unit_latency_ms")]
        missing = [p.get("missing_percent") for p in probes if p.get("missing_percent") is not None]
        cells.append(f"{units[0]['p50']:.1f} / {units[0]['p99']:.1f}, {max(missing):.2f}% lost" if units else "–")
    return cells


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session")
    parser.add_argument("--config", default="pcm")
    args = parser.parse_args()
    session = RUNS / args.session
    for profile in PROFILES:
        path = session / f"mesh.{args.config}.{profile}.hops.json"
        if not path.is_file():
            continue
        hops = json.loads(path.read_text())
        print(f"#### {args.config}, long haul {profile}\n")
        print("Needletail, one-way time of each hop, ms, median / 99th percentile, for the parts that a "
              "listener received (HTTP/3 reader on the smaller host of each region):\n")
        hop_table(hops, "listener_parts")
        print("Unit latency at the listener, median / 99th percentile (ms), and units lost:\n")
        print("| System | " + " | ".join(name for _, name, _ in REGIONS) + " |")
        print("| --- | " + " | ".join("---:" for _ in REGIONS) + " |")
        print("| Needletail, RaptorQ FEC over UDP | " + " | ".join(needletail_udp(session, args.config, profile)) + " |")
        print("| LiveKit, SFU in each region | " + " | ".join(livekit(session, args.config, profile)) + " |")
        print()


if __name__ == "__main__":
    main()
