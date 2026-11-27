#!/usr/bin/env python3
"""Score a change (an edge, distributor, ingress or contributor added,
drained, replaced, upgraded or lost) from the per-unit records of a full
probe that ran through it, against the same probe's own no-change windows.

    change-window.py --frames LANE.probe.csv [--frames ...]
                     (--window START END | --marks MARKS.txt)
                     [--before 2] [--after 5] [--skip 5] [--percentile 97.5]
                     [--min-null 120] [--json OUT.json]

Input: the CSV that `ntcmp hls|sub|udp-ingest --frames F.csv` writes, one
line per unit received (kind "a" for audio). The columns used:
  latency_us        mouth to ear of the unit
  complete_unix_us  when the unit was complete at the listener
  first_unix_us     for audio, the unit's number: the Needletail part number
                    (UDP and LL-HLS lanes), or the unwrapped RTP sequence
                    number (LiveKit). Numbers are consecutive in a stream.
Duplicates are not written by the probe; a late unit that fills a gap is.

The window: --window START END in Unix seconds, or --marks, a file of lines
"start UNIX LABEL" / "end UNIX LABEL" (one change per label, several allowed;
the earliest start and the latest end of a label are used). The referee
writes them with `date +%s.%N` on the run host around the change command, or
takes the change tool's own lines. The window is widened by --before (2 s)
and --after (5 s).

A unit belongs to a window by its source time (complete minus latency), so a
unit delayed past the window still counts against it.

Measured for a window:
  missing   numbers never received between the last unit sourced before the
            window and the first sourced after it
  gap       the longest silence at the listener (arrival to arrival) among
            arrivals from the window's start to its end plus 1 s
  p99       mouth to ear of the units sourced in the window

The null: every window of the same length, slid by 1 s, over the same probe
outside the change (and after the first --skip seconds), with no change in
it. A change passes when
  missing == 0, and
  gap <= the --percentile (97.5th) of the null windows' gaps, and
  p99 <= the --percentile of the null windows' p99s.
With fewer than --min-null (120) null windows the change is not scored
(pass null): run the probe longer (300 s or more around a change), or take
the null from a dry run of the change tool, scored with --window over it.
Every result carries the null's own fail rate: each no-change window scored
as a change would be, against the null windows that do not overlap it. It is
published beside the change row, for both systems, as the bar's false-fail
rate (on a stationary probe of 300 s or more, about 7% at the 97.5th, 12% at the 95th, plus
any no-change loss; on short probes far more, which is why they are short).

Two checks that the probe was really listening (lead, 24 Nov: a stuck listener,
datagrams arriving but nothing accepted after its first parts, passed with
almost no rows in the window):
  coverage  the units sourced in the window against the window's length over
            the frame (the probe's own report's audio_frame_ms, or --frame-ms);
            under --min-coverage (0.98) fails the change
  report    the probe's own report beside the CSV (LANE.probe.json, and the UDP
            client's LANE.uc.json): units received over the probe's duration,
            and the client's own missing parts. Under --min-coverage of the
            units its duration allows fails every window of the probe
            ("probe_invalid"): a listener that heard almost nothing cannot vouch
            for a change.
Without a report or --frame-ms the frame is inferred from the CSV, and the
result says so.

(Until 23 Nov the bar was "gap <= the single worst silence outside, plus one
unit; p99 <= the outside p99 + 1 ms". About one clean no-change window in
four failed it, for Needletail and LiveKit alike: lead's check, confirmed on
the wave 7 clean probes.)
"""
import argparse
import bisect
import csv
import json
import pathlib
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


class Probe:
    """One probe's units, indexed for window queries."""

    def __init__(self, rows, skip_us):
        rows = sorted(rows, key=lambda r: r[3])
        self.source = [r[3] for r in rows]
        self.lat_ms = [r[2] / 1000 for r in rows]
        numbers = [r[0] for r in rows]
        self.prefix_max, m = [], None
        for n in numbers:
            m = n if m is None else max(m, n)
            self.prefix_max.append(m)
        self.suffix_min, m = [0] * len(numbers), None
        for i in range(len(numbers) - 1, -1, -1):
            m = numbers[i] if m is None else min(m, numbers[i])
            self.suffix_min[i] = m
        self.distinct = sorted(set(numbers))
        self.arrival = sorted(r[1] for r in rows)
        self.start = (self.arrival[0] + skip_us) if rows else 0
        self.end = self.source[-1] if rows else 0
        gaps = [b - a for a, b in zip(self.arrival, self.arrival[1:])]
        self.spacing_ms = sorted(gaps)[len(gaps) // 2] / 1000 if gaps else 0

    def window(self, a, b):
        i, j = bisect.bisect_left(self.source, a), bisect.bisect_right(self.source, b)
        if i == 0 or j >= len(self.source):
            return None
        lo, hi = self.prefix_max[i - 1], self.suffix_min[j]
        seen = bisect.bisect_left(self.distinct, hi) - bisect.bisect_right(self.distinct, lo)
        missing = max(0, hi - lo - 1) - max(0, seen)
        k, l = bisect.bisect_left(self.arrival, a), bisect.bisect_right(self.arrival, b + 1_000_000)
        arr = self.arrival[k:l]
        gap = max((y - x for x, y in zip(arr, arr[1:])), default=0) / 1000
        return {"missing": missing, "gap_ms": round(gap, 3), "p99_ms": pct(self.lat_ms[i:j], 99),
                "p50_ms": pct(self.lat_ms[i:j], 50), "units": j - i}


def own_report(csv_path):
    """The probe's own report beside its CSV: (frame_ms, received fraction over
    its duration, the UDP client's missing parts), each None when absent."""
    path = pathlib.Path(csv_path)
    stem = path.name[:-4] if path.name.endswith(".csv") else path.name
    out = {"frame_ms": None, "received_fraction": None, "client_missing_parts": None, "client_expected_parts": None}
    report = path.with_name(stem + ".json")
    if report.is_file():
        try:
            r = json.loads(report.read_text())
            frame = r.get("audio_frame_ms")
            out["frame_ms"] = float(frame) if frame else None
            if frame and r.get("duration_seconds"):
                out["received_fraction"] = round(
                    (r.get("audio_packets") or 0) / (float(r["duration_seconds"]) * 1000.0 / float(frame)), 4)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    client = path.with_name(stem.split(".probe")[0] + ".uc.json")
    if client.is_file():
        try:
            c = json.loads(client.read_text())
            out["client_missing_parts"] = c.get("missing_parts")
            out["client_expected_parts"] = c.get("expected_parts")
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return out


def score(rows, start_us, end_us, skip_us, percentile=97.5, min_null=120, frame_ms=None, report=None,
          min_coverage=0.98):
    report = report or {}
    if not rows:
        return {"error": "no audio units", "pass": False}
    frame = frame_ms or report.get("frame_ms")
    inferred = False
    if not frame:
        # Source time per number over the whole probe: wrong for a stuck
        # listener, hence said.
        srt = sorted(rows, key=lambda r: r[0])
        span = srt[-1][0] - srt[0][0]
        frame = (srt[-1][3] - srt[0][3]) / 1000.0 / span if span > 0 else None
        inferred = True
    probe = Probe(rows, skip_us)
    invalid = []
    fraction = report.get("received_fraction")
    if fraction is not None and fraction < min_coverage:
        invalid.append(f"the probe's own report: {fraction:.1%} of the units its duration allows")
    missing_parts, expected_parts = report.get("client_missing_parts"), report.get("client_expected_parts")
    if missing_parts and expected_parts and missing_parts > (1 - min_coverage) * expected_parts:
        invalid.append(f"the UDP client's own report: {missing_parts} of {expected_parts} parts missing")
    change = probe.window(start_us, end_us)
    if change is None:
        return {"error": "the probe does not cover the window with units on both sides", "pass": False,
                "probe_invalid": invalid or None}
    length = end_us - start_us
    expected = length / 1000.0 / frame if frame else None
    change["units_expected_by_time"] = round(expected) if expected else None
    change["coverage"] = round(change["units"] / expected, 4) if expected else None
    null = []
    a = probe.start
    while a + length + 1_000_000 < probe.end:
        b = a + length
        # No overlap with the change, its 1 s arrival tail included.
        if b + 1_000_000 < start_us or a > end_us + 1_000_000:
            w = probe.window(a, b)
            if w is not None and w["p99_ms"] is not None:
                w["at"] = a
                null.append(w)
        a += 1_000_000
    out = {"window_s": round(length / 1e6, 3), "unit_spacing_ms": round(probe.spacing_ms, 3),
           "frame_ms": round(frame, 4) if frame else None, "frame_ms_inferred": inferred,
           "probe_report": report or None, "change": change, "null_windows": len(null)}
    if invalid:
        out["probe_invalid"] = invalid
        out["pass"] = False
        return out
    if change["coverage"] is not None and change["coverage"] < min_coverage:
        out["pass"] = False
        out["note"] = (f"window coverage {change['coverage']:.1%}: {change['units']} units sourced in the window "
                       f"against {change['units_expected_by_time']} that its length allows")
        return out
    if len(null) < min_null:
        out["pass"] = None
        out["note"] = f"null too short: {len(null)} no-change windows of this length, {min_null} needed"
        return out
    gap_bar = pct([w["gap_ms"] for w in null], percentile)
    p99_bar = pct([w["p99_ms"] for w in null], percentile)

    def fails(w):
        return w["missing"] > 0 or w["gap_ms"] > gap_bar or w["p99_ms"] > p99_bar

    def rank(key):
        return round(100.0 * sum(1 for w in null if w[key] < change[key]) / len(null), 1)

    # The bar's false-fail rate, honestly: each no-change window scored, as a
    # change would be, against the null windows that do not overlap it.
    loo_fail = 0
    for w in null:
        rest = [v for v in null if abs(v["at"] - w["at"]) > length + 1_000_000]
        if len(rest) < min_null:
            continue
        if (w["missing"] > 0 or w["gap_ms"] > pct([v["gap_ms"] for v in rest], percentile)
                or w["p99_ms"] > pct([v["p99_ms"] for v in rest], percentile)):
            loo_fail += 1
    out.update({
        "percentile": percentile,
        "gap_bar_ms": gap_bar, "p99_bar_ms": p99_bar,
        "gap_rank_in_null": rank("gap_ms"), "p99_rank_in_null": rank("p99_ms"),
        "null_fail_rate": round(loo_fail / len(null), 3),
        "null_missing_windows": sum(1 for w in null if w["missing"] > 0),
        "pass": not fails(change),
    })
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
    ap.add_argument("--percentile", type=float, default=97.5)
    ap.add_argument("--min-null", type=int, default=120)
    ap.add_argument("--frame-ms", type=float, default=None,
                    help="the lane's unit length (default: the probe's own report, else inferred)")
    ap.add_argument("--min-coverage", type=float, default=0.98)
    ap.add_argument("--json")
    args = ap.parse_args()
    loaded = {f: load(f) for f in args.frames}
    result = {}
    for label, (s, e) in windows(args).items():
        start_us, end_us = int((s - args.before) * 1e6), int((e + args.after) * 1e6)
        result[label] = {f: score(rows, start_us, end_us, int(args.skip * 1e6), args.percentile, args.min_null,
                                  args.frame_ms, own_report(f), args.min_coverage)
                         for f, rows in loaded.items()}
    text = json.dumps(result, indent=1)
    if args.json:
        pathlib.Path(args.json).write_text(text + "\n")
    print(text)
    ok = all(r.get("pass") is True for per in result.values() for r in per.values())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
