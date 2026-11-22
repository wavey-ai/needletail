#!/usr/bin/env python3
"""Tables of the 4K latency phases (compare-run.py baseline, ttff, impair,
fanout) from an evidence file of compare-analyse.py.

  video-phases-report.py EVIDENCE.json [--session NAME]
"""

from __future__ import annotations

import argparse
import json

SITES = [("lon", "London"), ("new", "New York"), ("tok", "Tokyo"), ("syd", "Sydney")]
SYSTEMS = [("nt", "Needletail, LL-HLS"), ("cf", "Cloudflare Realtime"), ("whep", "Cloudflare Stream WHIP/WHEP"),
           ("lkregion", "LiveKit, SFU in each region"), ("lkmulti", "LiveKit, multi-region set"),
           ("lkcloud", "LiveKit Cloud")]
IMPAIR = [("baseline.2160p5", "none"), ("impair.loss1", "1% loss"), ("impair.loss5", "5% loss"),
          ("impair.burst", "burst loss"), ("impair.delay100", "100 ms delay")]


def table(header, rows):
    print("| " + " | ".join(header) + " |")
    print("| " + " | ".join("---" if i == 0 else "---:" for i in range(len(header))) + " |")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print()


def better(step, site):
    probes = [((step.get("hosts") or {}).get(f"{site}-{h}") or {}).get("probes", {}).get("probe") for h in ("nano", "view")]
    probes = [p for p in probes if p]
    return max(probes, key=lambda p: p.get("on_time_percent") or -1) if probes else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence")
    parser.add_argument("--session", default="")
    args = parser.parse_args()
    data = json.load(open(args.evidence))
    session = args.session or list(data["sessions"])[0]
    steps = data["sessions"][session]["steps"]

    print("### Baseline at 5 Mbit/s\n")
    print("Delivery latency, median / 99th percentile (ms), and frames on time (%), the better viewer of each "
          "region; then glass to glass in headless Chrome, median (ms):\n")
    rows = []
    for lane, name in SYSTEMS:
        step = steps.get(f"{lane}.baseline.2160p5")
        if not step:
            continue
        cells = []
        for site, _ in SITES:
            p = better(step, site)
            lat = (p or {}).get("latency_ms") or {}
            cells.append(f"{lat.get('p50', 0):.0f} / {lat.get('p99', 0):.0f}, {p.get('on_time_percent', 0):.1f}%" if p and lat else "–")
        chrome = []
        for site, _ in SITES:
            player = ((step["hosts"].get(f"{site}-view") or {}).get("players") or {}).get("player") or {}
            g = (player.get("glass_to_glass_ms") or {}).get("p50")
            chrome.append(f"{g:.0f}" if g else "–")
        rows.append([name] + cells + [" / ".join(chrome)])
    table(["System"] + [n for _, n in SITES] + ["Chrome, glass to glass"], rows)

    print("### Time to the first frame\n")
    print("A viewer joins the running stream at 5 Mbit/s, eight times in a row in each region: time from its start to "
          "its first complete key frame (ms), lowest and highest:\n")
    rows = []
    for lane, name in SYSTEMS:
        step = steps.get(f"{lane}.ttff.2160p5")
        if not step:
            continue
        cells = []
        for site, _ in SITES:
            values = []
            for h in ("nano", "view"):
                for p in (((step["hosts"].get(f"{site}-{h}") or {}).get("probes")) or {}).values():
                    if p and p.get("first_key_frame_ms") is not None and p["first_key_frame_ms"] >= 0:
                        values.append(p["first_key_frame_ms"])
            cells.append(f"{min(values):.0f}–{max(values):.0f}" if values else "–")
        rows.append([name] + cells)
    table(["System"] + [n for _, n in SITES], rows)

    print("### Loss and delay at the viewer\n")
    print("The same `tc netem` on every viewer host. Frames on time (%) and the longest stall (ms), London / "
          "Sydney:\n")
    rows = []
    for lane, name in SYSTEMS:
        cells = []
        for detail, _ in IMPAIR:
            step = steps.get(f"{lane}.{detail}")
            if not step:
                cells.append("–")
                continue
            parts = []
            for site in ("lon", "syd"):
                p = better(step, site)
                parts.append(f"{p.get('on_time_percent', 0):.0f}%, {p.get('longest_stall_ms') or 0:.0f}" if p else "–")
            cells.append(" / ".join(parts))
        rows.append([name] + cells)
    table(["System"] + [label for _, label in IMPAIR], rows)
    print("Delivery latency, 99th percentile (ms), London / Sydney:\n")
    rows = []
    for lane, name in SYSTEMS:
        cells = []
        for detail, _ in IMPAIR:
            step = steps.get(f"{lane}.{detail}")
            parts = []
            for site in ("lon", "syd"):
                p = better(step, site) if step else None
                lat = (p or {}).get("latency_ms") or {}
                parts.append(f"{lat['p99']:.0f}" if lat.get("p99") is not None else "–")
            cells.append(" / ".join(parts))
        rows.append([name] + cells)
    table(["System"] + [label for _, label in IMPAIR], rows)

    print("### Crowds\n")
    for n in ("10", "100"):
        print(f"{n} viewers ({'3, 3, 2 and 2' if n == '10' else '25 in each region'}) on the larger host of each "
              "region: viewers with video, the median viewer's median delivery latency and the worst viewer's "
              "99th percentile (ms), the median first key frame (ms):\n")
        rows = []
        for lane, name in SYSTEMS:
            step = steps.get(f"{lane}.fanout.{n}")
            if not step:
                continue
            cells = []
            for site, _ in SITES:
                a = (((step["hosts"].get(f"{site}-view") or {}).get("probes") or {}).get("all")) or {}
                if not a:
                    cells.append("–")
                    continue
                cells.append(f"{a.get('viewers_with_video')}/{a.get('viewers')}, {a.get('latency_p50_ms_median_viewer')}"
                             f" / {a.get('latency_p99_ms_worst_viewer')}, {a.get('first_key_frame_ms_median')}")
            rows.append([name] + cells)
        table(["System"] + [n2 for _, n2 in SITES], rows)
    print("Processor of the nodes (% of one vCPU) with 10 and with 100 viewers:\n")
    rows = []
    for lane, node, process, label in (
            ("nt", "contrib-lon", "av-contrib", "Needletail contributor"),
            ("nt", "relay-primary", "av-mesh", "Needletail primary relay"),
            ("nt", "lon-dist-a", "av-mesh", "Needletail distributor, London"),
            ("nt", "lon-edge-1", "av-mesh", "Needletail edge 1, London"),
            ("nt", "lon-edge-2", "av-mesh", "Needletail edge 2, London"),
            ("nt", "syd-edge-1", "av-mesh", "Needletail edge 1, Sydney"),
            ("lkregion", "lon-lk", "livekit-server", "LiveKit SFU, London"),
            ("lkregion", "syd-lk", "livekit-server", "LiveKit SFU, Sydney"),
            ("lkmulti", "lon-lk", "livekit-server", "LiveKit multi-region, London node")):
        cells = []
        for n in ("10", "100"):
            value = ((steps.get(f"{lane}.fanout.{n}") or {}).get("nodes") or {}).get(node, {}).get(f"{process}_cpu_percent")
            cells.append(f"{value:.0f}" if value is not None else "–")
        rows.append([label] + cells)
    table(["Node", "10 viewers", "100 viewers"], rows)


if __name__ == "__main__":
    main()
