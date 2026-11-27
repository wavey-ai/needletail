#!/usr/bin/env python3
"""Print the tables of the 4K comparison as Markdown, from the evidence file
of compare-analyse.py.

A region has carried a rung when the better of its two listeners had at least
99.5% of the frames on time and no stall longer than half a second. A frame is
on time when it arrived whole, can be decoded (no frame before it since the
last key frame is missing) and was not more than 250 ms later than the median.

The better listener, because a fault of the system reaches both listeners of a
region and a fault of one listener host does not: the Sydney viewer host is a
shared plan with steal time.

A cell holds the value of each session, in order, separated by ` / `, or the
median of the sessions with the lowest and the highest in brackets.
"""

from __future__ import annotations

import argparse
import json
import statistics

SITES = [("lon", "London"), ("new", "New York"), ("tok", "Tokyo"), ("syd", "Sydney")]
SYSTEMS = [
    ("nt-uc", "Needletail, RaptorQ FEC over UDP from the edge"),
    ("nt", "Needletail, LL-HLS"),
    ("cf", "Cloudflare Realtime"),
    ("whep", "Cloudflare Stream WHIP/WHEP"),
    ("lkregion", "LiveKit, SFU in each region"),
    ("lkmulti", "LiveKit, multi-region set"),
    ("lkcloud", "LiveKit Cloud"),
]
RUNGS = ["2160p5", "2160p8", "2160p16", "2160p25", "2160p40", "2160p80", "2160p120", "2160p160",
         "2160p240", "2160p320"]


def spread(values: list, digits=1) -> str:
    values = [float(v) for v in values if v is not None]
    if not values:
        return "–"
    middle = statistics.median(values)
    if len(values) == 1 or max(values) - min(values) < 10 ** -digits:
        return f"{middle:.{digits}f}"
    return f"{middle:.{digits}f} ({min(values):.{digits}f}–{max(values):.{digits}f})"


def table(header: list[str], rows: list[list[str]]) -> None:
    if not rows:
        return
    print("| " + " | ".join(header) + " |")
    print("| " + " | ".join("---" if i == 0 else "---:" for i in range(len(header))) + " |")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print()


class Evidence:
    def __init__(self, path: str, media: str | None):
        self.data = json.load(open(path))
        self.sessions = list(self.data["sessions"])
        self.rates = {}
        if media:
            for name, rung in json.load(open(media))["rungs"].items():
                self.rates[name] = rung["kbps"] / 1000.0

    def rate(self, rung: str) -> str:
        return f"{self.rates[rung]:.0f}" if rung in self.rates else rung

    def step(self, session: str, lane: str, phase: str, detail: str) -> dict | None:
        # The UDP client of Needletail runs in the Needletail steps as probe "probe.uc".
        if lane == "nt-uc":
            step = self.data["sessions"][session]["steps"].get(f"nt.{phase}.{detail}")
            if not step:
                return None
            hosts = {}
            for name, host in (step.get("hosts") or {}).items():
                probe = (host.get("probes") or {}).get("probe.uc")
                if probe:
                    hosts[name] = {**host, "probes": {"probe": probe}, "players": {}}
            return {**step, "hosts": hosts}
        return self.data["sessions"][session]["steps"].get(f"{lane}.{phase}.{detail}")

    def probes(self, step: dict | None, site: str, label: str = "probe") -> list[dict]:
        """The probes of a region: the listener host and the viewer host."""
        if not step:
            return []
        out = []
        for host in (f"{site}-nano", f"{site}-view"):
            probe = ((step.get("hosts") or {}).get(host) or {}).get("probes", {}).get(label)
            if probe:
                out.append(probe)
        return out

    def player(self, step: dict | None, site: str, label: str = "player") -> dict | None:
        if not step:
            return None
        return ((step.get("hosts") or {}).get(f"{site}-view") or {}).get("players", {}).get(label)


def whole(probe: dict) -> bool:
    return bool(not probe.get("error") and probe.get("on_time_percent") is not None
                and probe["on_time_percent"] >= 99.5 and (probe.get("longest_stall_ms") or 0) <= 500)


def better(probes: list[dict]) -> dict | None:
    """The listener of a region that fared better."""
    if not probes:
        return None
    return max(probes, key=lambda p: (whole(p), p.get("on_time_percent") if p.get("on_time_percent")
                                      is not None else -1.0))


def carried(probes: list[dict]) -> bool | None:
    best = better(probes)
    return None if best is None else whole(best)


def ceilings(e: Evidence) -> None:
    print("### Highest bit rate carried whole\n")
    print("Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest "
          "with every rung below it carried too. Each session is one sweep.\n")
    rows = []
    for lane, name in SYSTEMS:
        row = [name]
        any_value = False
        for site, _ in SITES:
            cells = []
            for session in e.sessions:
                best = highest = None
                tried = False
                broken = False
                for rung in RUNGS:
                    step = e.step(session, lane, "throughput", rung)
                    if step is None:
                        continue
                    tried = True
                    ok = not step.get("source_failed") and carried(e.probes(step, site))
                    if ok:
                        highest = rung
                        if not broken:
                            best = rung          # this rung and every rung below it
                    else:
                        broken = True
                if tried:
                    cell = e.rate(highest) if highest else "none"
                    if highest != best:
                        cell += f" ({e.rate(best) if best else 'none'})"
                    cells.append(cell)
                    any_value = True
            row.append(" / ".join(cells) if cells else "–")
        if any_value:
            rows.append(row)
    table(["System"] + [name for _, name in SITES], rows)


def detail(e: Evidence) -> None:
    print("### Every rung\n")
    print("Frames on time (%) at the better of the two listeners of each region, "
          "and in brackets the bit rate that listener received (Mbit/s).\n")
    for lane, name in SYSTEMS:
        rows = []
        for rung in RUNGS:
            row = [e.rate(rung)]
            seen = False
            for site, _ in SITES:
                cells = []
                for session in e.sessions:
                    step = e.step(session, lane, "throughput", rung)
                    if step is None:
                        continue
                    seen = True
                    if step.get("source_failed"):
                        cells.append("source failed")
                        continue
                    probes = e.probes(step, site)
                    if not probes:
                        cells.append("–")
                        continue
                    worst = better(probes)
                    if worst.get("on_time_percent") is None:
                        cells.append("0 (%s)" % (f"{worst['kbps'] / 1000:.0f}" if worst.get("kbps") else "0"))
                    else:
                        cells.append(f"{worst['on_time_percent']:.1f} ({(worst.get('kbps') or 0) / 1000:.0f})")
                row.append(" / ".join(cells) if cells else "–")
            if seen:
                rows.append(row)
        if rows:
            print(f"{name}:\n")
            table(["Mbit/s"] + [n for _, n in SITES], rows)


def latency(e: Evidence, rungs: list[str]) -> None:
    print("### Latency of what was carried\n")
    for label, kind in (("Delivery latency at the listener's socket, median (ms): from the moment a frame "
                         "is due at the source to its last byte at the listener", "probe"),
                        ("Glass to glass in headless Chrome, median (ms)", "player"),
                        ("Chrome: time frozen (ms in each minute)", "freeze")):
        print(f"{label}:\n")
        for rung in rungs:
            rows = []
            for lane, name in SYSTEMS:
                row = [name]
                seen = False
                for site, _ in SITES:
                    values = []
                    for session in e.sessions:
                        step = e.step(session, lane, "throughput", rung)
                        if kind == "probe":
                            probes = [p for p in e.probes(step, site) if p.get("latency_ms")]
                            if probes and carried(e.probes(step, site)):
                                values.append(statistics.median(p["latency_ms"]["p50"] for p in probes))
                        else:
                            player = e.player(step, site)
                            if player and player.get("glass_to_glass_ms"):
                                values.append(player["glass_to_glass_ms"]["p50"] if kind == "player"
                                              else player.get("freeze_ms_per_minute"))
                    seen = seen or bool(values)
                    row.append(spread(values, 0))
                if seen:
                    rows.append(row)
            if rows:
                print(f"{e.rate(rung)} Mbit/s:\n")
                table(["System"] + [n for _, n in SITES], rows)


def first_frame(e: Evidence, rung: str) -> None:
    print("### Time to the first frame\n")
    print(f"At {e.rate(rung)} Mbit/s, from the listener's start to its first complete key frame (ms). "
          "The clip has a key frame every two seconds; a WebRTC sender answers a request for one "
          "within a third of a second.\n")
    rows = []
    for lane, name in SYSTEMS:
        row = [name]
        for site, _ in SITES:
            values = []
            for session in e.sessions:
                for probe in e.probes(e.step(session, lane, "throughput", rung), site):
                    if (probe.get("first_key_frame_ms") or -1) >= 0:
                        values.append(probe["first_key_frame_ms"])
            row.append(spread(values, 0))
        rows.append(row)
    table(["System"] + [n for _, n in SITES], rows)


def nodes(e: Evidence) -> None:
    print("### Processor of the nodes\n")
    print("Percent of one vCPU used by the service at each bit rate (the highest session).\n")
    wanted = [("contrib-lon", "av-contrib", "Needletail contributor"),
              ("relay-primary", "av-mesh", "Needletail primary relay"),
              ("relay-secondary", "av-mesh", "Needletail secondary relay"),
              ("lon-dist-a", "av-mesh", "Needletail distributor, London"),
              ("lon-edge-1", "av-mesh", "Needletail edge 1, London"),
              ("syd-dist-a", "av-mesh", "Needletail distributor, Sydney")]
    for lane, items in (("nt", wanted),
                        ("lkregion", [("lon-lk", "livekit-server", "LiveKit node, London"),
                                      ("syd-lk", "livekit-server", "LiveKit node, Sydney")]),
                        ("lkmulti", [("lon-lk", "livekit-server", "LiveKit node, London (serves every region)")])):
        rungs = [rung for rung in RUNGS if any(e.step(s, lane, "throughput", rung) for s in e.sessions)]
        rows = []
        for node, process, name in items:
            row = [name]
            for rung in rungs:
                values = []
                for session in e.sessions:
                    step = e.step(session, lane, "throughput", rung) or {}
                    value = ((step.get("nodes") or {}).get(node) or {}).get(process + "_cpu_percent")
                    if value is not None:
                        values.append(value)
                row.append(f"{max(values):.0f}" if values else "–")
            rows.append(row)
        if rows and rungs:
            print(f"{dict(SYSTEMS)[lane]}:\n")
            table(["Node"] + [e.rate(rung) for rung in rungs], rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("evidence")
    parser.add_argument("--media", default="", help="media.json of the clips, for the bit rates")
    parser.add_argument("--sessions", default="")
    args = parser.parse_args()
    e = Evidence(args.evidence, args.media or None)
    if args.sessions:
        e.sessions = [name for name in args.sessions.split(",") if name in e.sessions]
    print(f"Sessions: {', '.join(e.sessions)}\n")
    ceilings(e)
    detail(e)
    latency(e, ["2160p5", "2160p8", "2160p16", "2160p25"])
    first_frame(e, "2160p5")
    nodes(e)


if __name__ == "__main__":
    main()
