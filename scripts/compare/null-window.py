#!/usr/bin/env python3
"""The null of a probe: how often a window with no change in it fails the
referee's bar (D-hunt.md 2.11 and 3.1; the bar of 23 Nov, notes-stage2.md).

    null-window.py --frames F.csv [--frames ...] [--arrivals A.csv ...]
                   [--lengths 8,15,30] [--skip 5] [--percentile 97.5]
                   [--min-null 120] [--json OUT]

The arithmetic is the referee's own: `load`, `Probe`, `pct` and `score` of
$NEEDLETAIL_COMPARE_ROOT/scripts/compare/change-window.py (default
<workspace>/needletail-compare), imported by path, nothing copied. Since 23
Nov the referee scores a change against the same probe's no-change windows
of the change's length, slid by 1 s after the first --skip seconds:
missing must be 0, the window's longest silence at most the --percentile
of the null windows' silences and its p99 at most the --percentile of their
p99s; with fewer than --min-null null windows nothing is scored. Each
result carries `null_fail_rate`: every no-change window scored as a change
would be against the null windows that do not overlap it.

For every probe and window length this prints the referee's
`null_fail_rate` (from `score`, with the change placed at the end of the
probe) and the same windows split by criterion (missing, silence, p99),
each against the chance floor of the bar (1 - percentile/100 of the
windows fail a criterion by chance on a stationary probe), the bars, the
range of the windows' p99s and the steady longest silence (the longest
arrival gap after the skip). A probe too short for the referee's null is
said so; its ranges are still printed.

--arrivals A.csv (needletail-udp-client --arrivals; the n-th goes with the
n-th --frames) adds the same on the listener host's kernel receive stamp
(mouth to kernel) and on publish to kernel, joined by part number.

The steady state's acceptance (3.1, on the 23 Nov bar): on a probe of
300 s or more, the silence and the p99 criteria each fail no more than
twice their chance floor (5 % at the 97.5th percentile), and missing
never. The windows overlap, so a rate is an estimate from about T/L
independent windows: on a stationary probe of 300 s the referee finds
about 7 % of no-change windows failing one criterion or the other. Exit 0:
a measurement.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent
LENGTHS = (8, 15, 30)
# 3.1: the acceptance is judged on probes this long; a criterion may fail
# up to this many times its chance floor.
ACCEPT_PROBE_S = 300
ACCEPT_FLOORS = 2
# Where the change is placed for the referee's own null: at the end of the
# probe, this far before its last unit.
END_MARGIN_US = 1_500_000


def compare_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT",
                                       str(WORKSPACE / "needletail-compare")))


def referee(path: pathlib.Path | None = None):
    """The referee's change-window.py as a module, from its worktree."""
    path = path or compare_root() / "scripts" / "compare" / "change-window.py"
    if not path.is_file():
        sys.exit(f"{path} is missing: set NEEDLETAIL_COMPARE_ROOT to the referee's worktree")
    spec = importlib.util.spec_from_file_location("referee_change_window", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_arrivals(path) -> dict[int, dict]:
    """needletail-udp-client --arrivals: part -> its published and kernel
    receive stamps (us)."""
    out = {}
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            out[int(row["part"])] = {
                "published_us": int(row.get("published_unix_us") or 0),
                "kernel_us": int(row.get("kernel_unix_us") or 0)}
    return out


def bases(cw, frames, arrivals=None) -> dict[str, list]:
    """The referee's rows (number, complete, latency, source) of a probe,
    and with its arrivals the same on the kernel stamp (mouth to kernel)
    and on publish to kernel (the part's source time is its publish)."""
    rows = cw.load(frames)
    out = {"listener": rows}
    if arrivals:
        stamps = read_arrivals(arrivals)
        kernel, publish = [], []
        for number, _at, _lat, source in rows:
            stamp = stamps.get(number)
            if not stamp or not stamp["kernel_us"]:
                continue
            kernel.append((number, stamp["kernel_us"], stamp["kernel_us"] - source, source))
            if stamp["published_us"]:
                publish.append((number, stamp["kernel_us"],
                                stamp["kernel_us"] - stamp["published_us"],
                                stamp["published_us"]))
        out["kernel"] = kernel
        out["publish_to_kernel"] = publish
    return out


def null_windows(cw, probe, length_us: int, change: tuple[int, int] | None) -> list[dict]:
    """The referee's no-change windows of one length: from the probe's start
    (first arrival + skip) every 1 s while the window and its 1 s arrival
    tail end before the last unit, none overlapping `change`."""
    out = []
    a = probe.start
    while a + length_us + 1_000_000 < probe.end:
        b = a + length_us
        if change is None or b + 1_000_000 < change[0] or a > change[1] + 1_000_000:
            window = probe.window(a, b)
            if window is not None and window["p99_ms"] is not None:
                window["at"] = a
                out.append(window)
        a += 1_000_000
    return out


def split_by_criterion(cw, windows: list[dict], length_us: int, percentile: float,
                       min_null: int) -> dict:
    """Each window scored as the referee scores a change, against the
    windows that do not overlap it: the fails by criterion. A window with
    fewer than `min_null` such windows is not scored (as the referee's)."""
    fails = {"missing": 0, "silence": 0, "p99": 0, "any": 0}
    scored = 0
    for window in windows:
        rest = [other for other in windows if abs(other["at"] - window["at"]) > length_us + 1_000_000]
        if len(rest) < min_null:
            continue
        scored += 1
        missing = window["missing"] > 0
        silence = window["gap_ms"] > cw.pct([other["gap_ms"] for other in rest], percentile)
        p99 = window["p99_ms"] > cw.pct([other["p99_ms"] for other in rest], percentile)
        fails["missing"] += missing
        fails["silence"] += silence
        fails["p99"] += p99
        fails["any"] += missing or silence or p99
    # Over every null window, as the referee's null_fail_rate is.
    return {"scored": scored, "fails": fails,
            "rates": {key: round(value / len(windows), 4) if scored else None
                      for key, value in fails.items()}}


def steady_gap_ms(probe) -> float:
    """The longest silence at the listener after the skip."""
    arrivals = [at for at in probe.arrival if at >= probe.start]
    return round(max((b - a for a, b in zip(arrivals, arrivals[1:])), default=0) / 1000, 3)


def measure(cw, rows: list, probe, length_s: float, skip_s: float, percentile: float,
            min_null: int) -> dict:
    """One probe (its rows, and the referee's Probe of them) at one window
    length."""
    length_us = int(length_s * 1e6)
    skip_us = int(skip_s * 1e6)
    floor = round(1 - percentile / 100, 4)
    out = {"length_s": length_s, "floor": floor}
    end = probe.end - END_MARGIN_US
    change = (end - length_us, end)
    result = cw.score(rows, change[0], change[1], skip_us, percentile, min_null)
    if "error" in result:
        out["error"] = result["error"]
        return out
    windows = null_windows(cw, probe, length_us, change)
    split = split_by_criterion(cw, windows, length_us, percentile, min_null)
    p99s = [window["p99_ms"] for window in windows]
    out.update({
        "windows": result["null_windows"],
        "null_fail_rate": result.get("null_fail_rate"),
        "gap_bar_ms": result.get("gap_bar_ms"), "p99_bar_ms": result.get("p99_bar_ms"),
        "null_missing_windows": result.get("null_missing_windows"),
        "scored": split["scored"], "fails": split["fails"], "rates": split["rates"],
        "p99_range_ms": [min(p99s), max(p99s)] if p99s else None,
        "unit_spacing_ms": result["unit_spacing_ms"]})
    if result.get("pass") is None:
        out["note"] = result.get("note")
    return out


def acceptance(probe_seconds: float, lengths: dict) -> dict:
    """3.1 on the 23 Nov bar: judged on probes of ACCEPT_PROBE_S or more."""
    if probe_seconds < ACCEPT_PROBE_S:
        return {"judged": False,
                "why": f"probe {probe_seconds:.0f} s: acceptance needs {ACCEPT_PROBE_S} s or more"}
    failures = []
    for length, item in lengths.items():
        rates = item.get("rates") or {}
        if item.get("error") or not item.get("scored"):
            failures.append(f"{length} s: not scored")
            continue
        limit = item["floor"] * ACCEPT_FLOORS
        for criterion in ("silence", "p99"):
            if rates[criterion] > limit:
                failures.append(f"{length} s: {criterion} fails {100 * rates[criterion]:.1f}% "
                                f"(at most {100 * limit:.1f}%)")
        if item["fails"]["missing"]:
            failures.append(f"{length} s: {item['fails']['missing']} windows miss units")
    return {"judged": True, "met": not failures, "failures": failures}


def analyse(cw, frames: list[str], arrivals: list[str], lengths, skip_s: float,
            percentile: float, min_null: int) -> dict:
    probes = []
    for index, path in enumerate(frames):
        paired = arrivals[index] if index < len(arrivals) else None
        for basis, rows in bases(cw, path, paired).items():
            if not rows:
                probes.append({"frames": str(path), "basis": basis, "error": "no units"})
                continue
            probe = cw.Probe(rows, int(skip_s * 1e6))
            seconds = round((probe.arrival[-1] - probe.arrival[0]) / 1e6, 1)
            by_length = {str(length): measure(cw, rows, probe, length, skip_s, percentile,
                                              min_null)
                         for length in lengths}
            probes.append({"frames": str(path), "basis": basis, "seconds": seconds,
                           "units": len(rows), "steady_gap_ms": steady_gap_ms(probe),
                           "lengths": by_length, "acceptance": acceptance(seconds, by_length)})
    return {"schema": "needletail.null-window.v1", "percentile": percentile,
            "min_null": min_null, "skip_s": skip_s, "probes": probes}


def pct_text(value) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def format_result(result: dict) -> str:
    lines = []
    for probe in result["probes"]:
        if "error" in probe:
            lines.append(f"{probe['frames']} [{probe['basis']}]: {probe['error']}")
            continue
        lines.append(f"{probe['frames']} [{probe['basis']}]: {probe['seconds']} s, "
                     f"{probe['units']} units, steady longest silence {probe['steady_gap_ms']} ms")
        for length, item in probe["lengths"].items():
            if "error" in item:
                lines.append(f"  {length:>3} s: {item['error']}")
                continue
            rates = item["rates"]
            p99 = item["p99_range_ms"]
            scored = (f"referee's null fail rate {pct_text(item['null_fail_rate'])}; by "
                      f"criterion silence {pct_text(rates['silence'])}, p99 "
                      f"{pct_text(rates['p99'])}, missing {item['fails']['missing']} (floor "
                      f"{pct_text(item['floor'])} a criterion); bars gap {item['gap_bar_ms']} ms, "
                      f"p99 {item['p99_bar_ms']} ms" if item["scored"] else item.get("note"))
            lines.append(f"  {length:>3} s: {item['windows']} null windows, {scored}; window p99 "
                         + (f"{p99[0]:.2f} to {p99[1]:.2f} ms" if p99 else "-"))
        verdict = probe["acceptance"]
        lines.append("  3.1: " + (verdict["why"] if not verdict["judged"] else
                                  "met" if verdict["met"] else "NOT met: "
                                  + "; ".join(verdict["failures"])))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", action="append", required=True,
                        help="a probe's per-unit CSV (ntcmp --frames)")
    parser.add_argument("--arrivals", action="append", default=[],
                        help="the n-th probe's needletail-udp-client --arrivals CSV")
    parser.add_argument("--lengths", default=",".join(str(length) for length in LENGTHS),
                        help="window lengths in seconds")
    parser.add_argument("--skip", type=float, default=5.0, help="the referee's --skip")
    parser.add_argument("--percentile", type=float, default=97.5,
                        help="the referee's --percentile")
    parser.add_argument("--min-null", type=int, default=120, help="the referee's --min-null")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    lengths = [float(item) if "." in item else int(item)
               for item in args.lengths.split(",") if item]
    result = analyse(referee(), args.frames, args.arrivals, lengths, args.skip,
                     args.percentile, args.min_null)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1) + "\n")
    print(format_result(result))


if __name__ == "__main__":
    main()
