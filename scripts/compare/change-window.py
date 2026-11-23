#!/usr/bin/env python3
"""Score a change (an edge, distributor, ingress or contributor added,
drained, replaced, upgraded or lost) from the per-unit records of a full
probe that ran through it.

    change-window.py --frames LANE.probe.csv [--frames ...]
                     (--window START END | --marks MARKS.txt)
                     [--before 2] [--after 5] [--skip 5] [--json OUT.json]

Input: the CSV that `ntcmp hls|sub|udp-ingest --frames F.csv` writes, one
line per unit received (kind "a" for audio). The columns used:
  latency_us        mouth to ear of the unit
  complete_unix_us  when the unit was complete at the listener
  first_unix_us     for audio, the unit's number: the Needletail part number
                    (UDP and LL-HLS lanes), or the unwrapped RTP sequence
                    number (LiveKit). Numbers are consecutive in a stream.
Duplicates are not written by the probe; a late unit that fills a gap is.

The window: --window START END in Unix seconds, or --marks, a file of lines
"start UNIX LABEL" / "end UNIX LABEL" (one change per label, several allowed).
The referee writes the marks with `date +%s.%N` on the run host immediately
before the change command starts and after it returns; if the change tool
logs its own phase times, the earliest start and latest end are used. The
window is widened by --before (2 s) and --after (5 s) to take in anything the
change disturbs just outside the command.

A unit belongs to the window by its source time (complete minus latency), so
a unit delayed past the window still counts against it.

Per change and probe:
  missing       numbers never received between the last unit sourced before
                the window and the first sourced after it
  longest_gap   the longest silence at the listener (arrival to arrival)
                among units arriving from window start to window end plus
                1 s, against the longest silence outside the window
  p50/p99/max   mouth to ear inside the window against outside it
                (outside leaves out the first --skip seconds of the probe)
  pass          missing == 0, longest_gap <= outside longest gap + one unit
                spacing, and p99 inside <= p99 outside + 1 ms
"""
import argparse
import csv
import json
import pathlib
import statistics
import sys


def load(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["kind"] != "a":
                continue
            lat = int(r["latency_us"])
            at = int(r["complete_unix_us"])
            rows.append((int(r["first_unix_us"]), at, lat, at - lat))
    return rows


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def longest_gap(arrivals):
    a = sorted(arrivals)
    return max((b - x for x, b in zip(a, a[1:])), default=0)


def score(rows, start_us, end_us, skip_us):
    if not rows:
        return {"error": "no audio units"}
    t0 = min(r[1] for r in rows) + skip_us
    inside = [r for r in rows if start_us <= r[3] <= end_us]
    outside = [r for r in rows if not (start_us <= r[3] <= end_us) and r[1] >= t0]
    before = [r[0] for r in rows if r[3] < start_us]
    after = [r[0] for r in rows if r[3] > end_us]
    if not before or not after:
        return {"error": "the probe does not cover the window with units on both sides"}
    lo, hi = max(before), min(after)
    seen = {r[0] for r in rows if lo < r[0] < hi}
    expected = max(0, hi - lo - 1)
    arr_out = sorted(r[1] for r in outside)
    spacing = statistics.median(b - a for a, b in zip(arr_out, arr_out[1:])) if len(arr_out) > 2 else 0
    gap_in = longest_gap([r[1] for r in rows if start_us <= r[1] <= end_us + 1_000_000])
    # The outside gap is the worst steady-state silence (outside the window).
    gap_out = longest_gap([r[1] for r in outside if r[1] < start_us]) if any(
        r[1] < start_us for r in outside) else 0
    gap_out = max(gap_out, longest_gap([r[1] for r in outside if r[1] > end_us + 1_000_000]))
    lin = [r[2] / 1000 for r in inside]
    lout = [r[2] / 1000 for r in outside]
    out = {
        "window_s": round((end_us - start_us) / 1e6, 3),
        "units_expected": expected,
        "units_received": len(seen),
        "missing": expected - len(seen),
        "unit_spacing_ms": round(spacing / 1000, 3),
        "longest_gap_ms": round(gap_in / 1000, 3),
        "longest_gap_outside_ms": round(gap_out / 1000, 3),
        "inside_ms": {"n": len(lin), "p50": pct(lin, 50), "p99": pct(lin, 99), "max": max(lin, default=None)},
        "outside_ms": {"n": len(lout), "p50": pct(lout, 50), "p99": pct(lout, 99), "max": max(lout, default=None)},
    }
    p99_in, p99_out = out["inside_ms"]["p99"], out["outside_ms"]["p99"]
    out["pass"] = bool(
        out["missing"] == 0
        and gap_in <= gap_out + spacing
        and p99_in is not None and p99_out is not None and p99_in <= p99_out + 1.0
    )
    return out


def windows(args):
    if args.window:
        return {"change": (float(args.window[0]), float(args.window[1]))}
    marks = {}
    for line in pathlib.Path(args.marks).read_text().splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[0] not in ("start", "end"):
            continue
        kind, t, label = parts[0], float(parts[1]), parts[2].strip()
        s, e = marks.get(label, (None, None))
        if kind == "start":
            s = t if s is None else min(s, t)
        else:
            e = t if e is None else max(e, t)
        marks[label] = (s, e)
    bad = [k for k, (s, e) in marks.items() if s is None or e is None or e < s]
    if bad:
        raise SystemExit(f"marks without a start and a later end: {', '.join(bad)}")
    return marks


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--frames", action="append", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--window", nargs=2, metavar=("START", "END"))
    g.add_argument("--marks")
    ap.add_argument("--before", type=float, default=2.0)
    ap.add_argument("--after", type=float, default=5.0)
    ap.add_argument("--skip", type=float, default=5.0)
    ap.add_argument("--json")
    args = ap.parse_args()
    result = {}
    for label, (s, e) in windows(args).items():
        start_us, end_us = int((s - args.before) * 1e6), int((e + args.after) * 1e6)
        result[label] = {f: score(load(f), start_us, end_us, int(args.skip * 1e6)) for f in args.frames}
    text = json.dumps(result, indent=1)
    if args.json:
        pathlib.Path(args.json).write_text(text + "\n")
    print(text)
    ok = all(r.get("pass") for per in result.values() for r in per.values())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
