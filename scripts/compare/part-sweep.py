#!/usr/bin/env python3
"""Halve the part while it is faster: run latency-bench.py once for each part
size on the local namespace lab and tabulate the latency of each.

Parts are one AEP1 epoch each, so a part size is an epoch size: the sender
(aep1-48k-probe send --epoch-samples N --payload P) sets it, and the
contributor (--audio-part epoch) and the caches follow. The default sizes
are the lossless halving sweep and the Opus default:

  pcm:48   PCM, 1 ms (the lossless default)
  pcm:24   PCM, 0.5 ms
  pcm:12   PCM, 0.25 ms
  flac:48  FLAC, 1 ms
  flac:32  FLAC, 0.667 ms (FLAC's smallest frame)
  opus:120 Opus, 2.5 ms (the Opus default)

Each size is its own latency-bench run (the lab is started and stopped for
each), named SWEEP-PAYLOAD-SAMPLES, with the same seconds, readers and
service flags. Both kinds of reader keep the same length of time in flight at
every size, at most 400 parts: HTTP/3 --h3-window-ms (default 100 ms), HTTP/2
--h2-window-ms (default 5 ms: one part at 5 ms, 5 at 1 ms, 20 at 0.25 ms). A
reader with one part in flight reads one part per round trip (about 2 ms in
the lab), fewer than 1 ms parts arrive, and would measure its own backlog;
window 1 is kept only as the 5 ms window of a 5 ms part. The windows of each
size are in sweep.json and sweep.txt.

Results: target/latency-bench/SWEEP/{sweep.json,sweep.txt}: for each size,
each reader's p50/p99 latency (ntcmp, last sample spoken to part complete)
and, from the hop journals, source -> listener complete p50/p99 for each
region. The lowest p50 and p99 of each column among the lossless sizes are
marked with *.

  part-sweep.py --name s1 [--seconds 60] [--readers 1] [--sizes pcm:48,pcm:24]
                [--bench-args '--mesh-args="--relay-forward-nonblocking false"']
  part-sweep.py --name s1 --report-only    tabulate the runs already made
  part-sweep.py --name s1 --dry-run        print every command; run nothing
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import shlex
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT_ROOT = ROOT / "target" / "latency-bench"
BENCH = HERE / "latency-bench.py"
SAMPLE_RATE = 48_000
DEFAULT_SIZES = "pcm:48,pcm:24,pcm:12,flac:48,flac:32,opus:120"
LOSSLESS = ("pcm", "flac")
MAX_WINDOW = 400


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def parse_sizes(text: str) -> list[tuple[str, int]]:
    """`payload:samples,...` -> [(payload, samples)]."""
    sizes = []
    for item in [part.strip() for part in text.split(",") if part.strip()]:
        payload, _, samples = item.partition(":")
        if payload not in ("pcm", "flac", "opus") or not samples.isdigit() or int(samples) <= 0:
            raise SystemExit(f"a size is pcm|flac|opus:SAMPLES, not {item!r}")
        sizes.append((payload, int(samples)))
    if not sizes:
        raise SystemExit("no sizes")
    return sizes


def epoch_ms(samples: int) -> float:
    return samples * 1000 / SAMPLE_RATE


def window_parts(samples: int, window_ms: float) -> int:
    """Parts in flight that cover window_ms at this part size, 1 to 400."""
    return max(1, min(MAX_WINDOW, math.ceil(window_ms / epoch_ms(samples) - 1e-9)))


def run_name(sweep: str, payload: str, samples: int) -> str:
    return f"{sweep}-{payload}-{samples}"


def bench_command(args, payload: str, samples: int) -> list[str]:
    command = [sys.executable, str(BENCH), "--name", run_name(args.name, payload, samples),
               "--payload", payload, "--epoch-samples", str(samples),
               "--seconds", str(args.seconds), "--readers", str(args.readers),
               "--h3-window", str(window_parts(samples, args.h3_window_ms)),
               "--h2-window", str(window_parts(samples, args.h2_window_ms))]
    return command + (shlex.split(args.bench_args) if args.bench_args else [])


# ------------------------------------------------------------------ the table

def reader_key(report: str) -> str:
    """`lon-readers.h3.probe.json` -> `lon-readers.h3` (one reader of each
    kind; `.probe_1.json` and so on are further readers of the kind)."""
    return report.split(".probe")[0] + report.split(".probe")[1].rsplit(".json", 1)[0]


def run_row(payload: str, samples: int, hops_json: pathlib.Path,
            windows: dict | None = None) -> dict:
    """One size: its readers' and regions' latency from its hops.json."""
    row = {"payload": payload, "epoch_samples": samples, "part_ms": round(epoch_ms(samples), 4),
           "parts_per_second": round(SAMPLE_RATE / samples, 1), "run": hops_json.parent.name}
    if windows is not None:
        row["windows"] = windows
    try:
        result = json.loads(hops_json.read_text())
    except (OSError, ValueError):
        row["error"] = f"no {hops_json.name}"
        return row
    bench = result.get("bench") or {}
    if bench.get("reader_windows"):
        row["windows"] = bench["reader_windows"]
    readers = {}
    for item in bench.get("readers") or []:
        latency = item.get("latency_ms") or {}
        readers[reader_key(item.get("report", "?"))] = {
            "p50": latency.get("p50"), "p99": latency.get("p99"), "units": item.get("units"),
            "gaps": item.get("gaps"), **({"error": item["error"]} if item.get("error") else {})}
    row["readers"] = readers
    regions = {}
    for region, kinds in (result.get("regions") or {}).items():
        if region == "all":
            continue
        stages = {item["stage"]: item for item in
                  (kinds.get("listener_parts") or {}).get("from_source", [])}
        complete = stages.get("listener.complete")
        if complete:
            regions[region] = {"p50": complete.get("p50"), "p99": complete.get("p99"),
                               "parts": complete.get("count")}
    row["regions"] = regions
    source = result.get("source") or {}
    if source.get("epoch_samples") not in (None, samples):
        row["warning"] = f"the sender sent {source['epoch_samples']}-sample epochs"
    if bench.get("epoch_note"):
        row["warning"] = bench["epoch_note"]
    if bench.get("journal_full"):
        row["journal_full"] = bench["journal_full"]
    return row


def columns(rows: list[dict]) -> list[tuple[str, str]]:
    """(kind, name) of every reader and region seen, in a stable order."""
    readers = sorted({name for row in rows for name in row.get("readers", {})})
    regions = sorted({name for row in rows for name in row.get("regions", {})})
    return [("reader", name) for name in readers] + [("region", name) for name in regions]


def best(rows: list[dict]) -> dict[tuple[str, str, str], float]:
    """The lowest p50 and p99 of each column among the lossless sizes."""
    out = {}
    for kind, name in columns(rows):
        for q in ("p50", "p99"):
            values = [((row.get(f"{kind}s") or {}).get(name) or {}).get(q) for row in rows
                      if row["payload"] in LOSSLESS]
            values = [value for value in values if isinstance(value, (int, float))]
            if values:
                out[(kind, name, q)] = min(values)
    return out


def table(rows: list[dict]) -> str:
    cols = columns(rows)
    marks = best(rows)
    head = ["size", "part ms", "parts/s", "in flight h2/h3"] + [
        ("" if kind == "reader" else "source->listener ") + name for kind, name in cols]
    lines = [head]
    for row in rows:
        windows = row.get("windows") or {}
        line = [f"{row['payload']} {row['epoch_samples']}", f"{row['part_ms']:g}",
                f"{row['parts_per_second']:g}",
                f"{windows.get('h2', '-')}/{windows.get('h3', '-')}"]
        for kind, name in cols:
            item = (row.get(f"{kind}s") or {}).get(name) or {}
            cell = []
            for q in ("p50", "p99"):
                value = item.get(q)
                if not isinstance(value, (int, float)):
                    cell.append("-")
                    continue
                star = "*" if row["payload"] in LOSSLESS and marks.get((kind, name, q)) == value else ""
                cell.append(f"{value:.2f}{star}")
            line.append(" / ".join(cell))
        lines.append(line)
    widths = [max(len(line[i]) for line in lines) for i in range(len(head))]
    text = ["  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
            for line in lines]
    notes = [f"{row['payload']} {row['epoch_samples']}: "
             + "; ".join(str(row[key]) for key in ("error", "warning", "journal_full") if row.get(key))
             for row in rows if any(row.get(key) for key in ("error", "warning", "journal_full"))]
    return ("latency ms, p50 / p99; * the lowest of the lossless sizes; in flight: parts in "
            "flight of each reader lane (source->listener pools both lanes of a region)\n"
            + "\n".join(text) + ("\n\n" + "\n".join(notes) if notes else "") + "\n")


def report(args, sizes: list[tuple[str, int]], runs: dict | None = None) -> dict:
    rows = [run_row(payload, samples, OUT_ROOT / run_name(args.name, payload, samples) / "hops.json",
                    {"h2": window_parts(samples, args.h2_window_ms),
                     "h3": window_parts(samples, args.h3_window_ms)})
            for payload, samples in sizes]
    for row in rows:
        status = (runs or {}).get(row["run"])
        if status is not None:
            row["bench_exit"] = status
    out = OUT_ROOT / args.name
    out.mkdir(parents=True, exist_ok=True)
    result = {"sweep": args.name, "seconds": args.seconds, "readers": args.readers,
              "h3_window_ms": args.h3_window_ms, "h2_window_ms": args.h2_window_ms,
              "bench_args": args.bench_args,
              "sizes": [f"{payload}:{samples}" for payload, samples in sizes], "rows": rows}
    (out / "sweep.json").write_text(json.dumps(result, indent=1) + "\n")
    text = f"part sweep {args.name}: {args.seconds} s per size, {args.readers} reader(s) of each kind\n\n"
    text += table(rows)
    (out / "sweep.txt").write_text(text)
    print(text, end="")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--name", required=True, help="the sweep; runs are NAME-PAYLOAD-SAMPLES")
    parser.add_argument("--sizes", default=DEFAULT_SIZES, help="payload:samples,... (default: %(default)s)")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--readers", type=int, default=1)
    parser.add_argument("--h3-window-ms", type=float, default=100.0,
                        help="time in flight of the HTTP/3 readers at every size")
    parser.add_argument("--h2-window-ms", type=float, default=5.0,
                        help="time in flight of the HTTP/2 readers at every size")
    parser.add_argument("--bench-args", default="", help="more latency-bench.py arguments (one string)")
    parser.add_argument("--report-only", action="store_true", help="tabulate the runs already made")
    parser.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sizes = parse_sizes(args.sizes)
    commands = {run_name(args.name, payload, samples): bench_command(args, payload, samples)
                for payload, samples in sizes}
    if args.dry_run:
        print(json.dumps(commands, indent=1))
        return 0
    runs = {}
    if not args.report_only:
        for name, command in commands.items():
            log(f"{name}: {shlex.join(command[1:])}")
            runs[name] = subprocess.run(command).returncode
            if runs[name] != 0:
                log(f"{name}: latency-bench exited {runs[name]}; the sweep goes on")
    report(args, sizes, runs)
    return 0 if all(code == 0 for code in runs.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
