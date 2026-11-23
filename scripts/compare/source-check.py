#!/usr/bin/env python3
"""The source check (D-hunt.md 2.11, 3.1, 6): the AEP1 sender into the
local lab's contributor with no listener, and E3's table of the delays
before publish, each episode split between the sender's pacer and encode,
the link, the contributor's read and its publish.

    source-check.py run [--seconds 600] [--payload opus|pcm] [--epoch-samples 48]
                        [--name NAME] [--no-up] [--dry]
    source-check.py report DIR [--json OUT]

`run`, in order, through the local lab's own command line
(scripts/edge_lab.py; nothing here starts a namespace itself):
 1. `edge_lab.py up` unless --no-up;
 2. `needletail-nodecheck watch` on the host (the source and the
    contributor share it here: a stopped tick is either's);
 3. the referee's sender in contrib-lon's namespace, at the next 64 s
    boundary at least 8 s away, for --seconds, with --lateness-journal;
 4. the contributor's /api/stalls, /api/hops of the stream and /api/pacing
    from that start on;
 5. the run's files into the run archive (scripts/run_archive.py), then
    the report. The lab stays up (`edge_lab.py down` takes it down).
`--dry` prints every command and runs none. Output in
target/source-check/<name>/ and in the archive.

`report` reads such a directory. A part's lateness is the contributor's
`sent_first` minus its ideal instant (the loop START plus (part + 1)
epochs); its arrival is `src_first` (the kernel's receive stamp with
workstream A's S2). Episodes are runs of parts more than 0.5, 1 and 2 ms
over the median; for each the sender's `late` line of its first part gives
`woke_late` and `encode`, the link is the arrival's lateness less the
sender's (one clock here: the source and the contributor share the host),
`rx_wait` and the publish are the contributor's. Each episode's side is
the first with an event over it: a stopped node, the pacer, the encode,
the link (`src_gap`), the read (`rx_wait`), the publish (`inline_publish`,
`lane_send`). 3.1: every episode over 0.5 ms attributed; the count over
1 ms is the normal-scheduling baseline. Exit 0 (a measurement); `run`
exits 1 when a step failed.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import edge_lab as lab  # noqa: E402
import stall_records as sr  # noqa: E402

EDGE_LAB = ROOT / "scripts" / "edge_lab.py"
ARCHIVE_TOOL = ROOT / "scripts" / "run_archive.py"
OUT = ROOT / "target" / "source-check"
CONTRIBUTOR = "contrib-lon"
INGEST = "127.0.0.1:27100"
SAMPLE_RATE = 48_000
STREAMS = {"opus": 3001, "pcm": 4001}
EPISODE_ABOVE_US = (500, 1000, 2000)
NODECHECK = pathlib.Path(os.environ.get(
    "NEEDLETAIL_NODECHECK_BIN", str(lab.MESH_ROOT / "target" / "release" / "needletail-nodecheck")))


def compare_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT",
                                       str(lab.WORKSPACE / "needletail-compare")))


def epoch_us(samples: int) -> float:
    return samples * 1e6 / SAMPLE_RATE


def loop_start(now: float) -> int:
    """The referee's START: the next multiple of 64 s at least 8 s away."""
    start = (int(now) // 64 + 1) * 64
    return start + 64 if start - now < 8 else start


def sender_command(start: int, seconds: int, payload: str, samples: int, out: pathlib.Path,
                   pcm: pathlib.Path) -> list[str]:
    """The referee's AEP1 sender (compare-audio.py `aep1_command`) with the
    lateness journal of 2.7."""
    session = f"{start}{round(epoch_us(samples) * 1000):09d}"
    return [str(lab.PROBE), "send", "--target", INGEST, "--session-id", session,
            "--group-id", "0", "--duration-seconds", str(seconds), "--payload", payload,
            "--epoch-samples", str(samples), "--channels", "2", "--group-channels", "8",
            "--pcm-s24le-file", str(pcm), "--repair-percent", "12", "--min-repair-symbols", "1",
            "--lateness-journal", str(out / "sender.ndjson")]


def contributor_url(path: str) -> str:
    return f"https://{lab.node(CONTRIBUTOR)['public_ipv4']}:{lab.CONTRIB_HTTP_PORT}{path}"


class Steps:
    """Every command of a run: carried out, or with --dry printed only."""

    def __init__(self, dry: bool) -> None:
        self.dry = dry
        self.failed: list[str] = []

    def run(self, command: list[str], stdout: pathlib.Path | None = None,
            timeout: float | None = None) -> int:
        line = " ".join(shlex.quote(item) for item in command)
        print(f"$ {line}" + (f" >{stdout}" if stdout else ""), flush=True)
        if self.dry:
            return 0
        with open(stdout, "wb") if stdout else open(os.devnull, "wb") as handle:
            status = subprocess.run(command, stdout=handle if stdout else None,
                                    timeout=timeout).returncode
        if status != 0:
            self.failed.append(f"{line}: exit {status}")
        return status

    def background(self, command: list[str]) -> subprocess.Popen | None:
        print("$ " + " ".join(shlex.quote(item) for item in command) + " &", flush=True)
        if self.dry:
            return None
        return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def preflight() -> list[str]:
    problems = []
    for path in (lab.PROBE, NODECHECK, ARCHIVE_TOOL,
                 compare_root() / "target" / "compare-media" / "audio" / "audio.s24le"):
        if not path.is_file():
            problems.append(f"missing: {path}")
    if lab.PROBE.is_file():
        text = subprocess.run([str(lab.PROBE), "send", "--help"], capture_output=True,
                              timeout=30).stdout.decode(errors="replace")
        if "--lateness-journal" not in text:
            problems.append("aep1-48k-probe send has no --lateness-journal (A's S1)")
    return problems


def command_run(args: argparse.Namespace) -> int:
    out = OUT / args.name
    problems = preflight()
    if problems and not args.dry:
        sys.exit("the source check does not start:\n  " + "\n  ".join(problems))
    for problem in problems:
        print(f"# a real run would refuse: {problem}")
    steps = Steps(args.dry)
    if not args.dry:
        out.mkdir(parents=True, exist_ok=True)
    archive = [sys.executable, str(ARCHIVE_TOOL), "--tool", "source-check"]
    steps.run(archive + ["create", args.name, "--kind", "local-lab", "--code", str(ROOT),
                         "--code", str(lab.MESH_ROOT), "--code", str(lab.CONTRIB_ROOT)])
    if not args.no_up:
        steps.run([sys.executable, str(EDGE_LAB), "up"])
    watch = steps.background([str(NODECHECK), "watch", "--out", str(out / "host-watch.ndjson")])
    now = time.time()
    start = loop_start(now)
    since_us = int(now * 1e6)
    record = {"schema": "needletail.source-check.v1", "name": args.name, "start_unix": start,
              "since_us": since_us, "payload": args.payload, "epoch_samples": args.epoch_samples,
              "stream": STREAMS[args.payload], "seconds": args.seconds}
    pcm = compare_root() / "target" / "compare-media" / "audio" / "audio.s24le"
    steps.run([sys.executable, str(EDGE_LAB), "sh", CONTRIBUTOR,
               *sender_command(start, args.seconds, args.payload, args.epoch_samples, out, pcm)],
              stdout=out / "sender.json", timeout=args.seconds + 180)
    for path, file in ((f"/api/stalls?since_us={since_us}", "contributor-stalls.ndjson"),
                       (f"/api/hops?since_us={since_us}&stream={STREAMS[args.payload]}",
                        "contributor-hops.ndjson"),
                       ("/api/pacing", "contributor-pacing.json")):
        steps.run([sys.executable, str(EDGE_LAB), "sh", CONTRIBUTOR, "curl", "-sk",
                   "--max-time", "60", contributor_url(path)], stdout=out / file, timeout=120)
    if watch is not None:
        watch.terminate()
        watch.wait(timeout=30)
    if not args.dry:
        (out / "source-check.json").write_text(json.dumps(record, indent=1) + "\n")
    steps.run(archive + ["add-tree", args.name, str(out), "--kind", "raw", "--description",
                         "the source check: sender journal, the contributor's stalls, hops and "
                         "pacing, the host's watch"])
    steps.run(archive + ["finish", args.name, "--status",
                         "failed" if steps.failed else "complete"])
    if not args.dry:
        print(format_report(analyse(out)))
    for failure in steps.failed:
        print(f"failed: {failure}")
    return 1 if steps.failed else 0


# ------------------------------------------------------------------ report

def contributor_parts(events: list[dict], stream: int) -> dict[int, dict]:
    """seq -> its src_first and sent_first (us) from a hop journal."""
    parts: dict[int, dict] = {}
    for event in events:
        if event.get("stream") != stream or event.get("stage") not in ("src_first", "sent_first"):
            continue
        parts.setdefault(int(event["seq"]), {}).setdefault(event["stage"], int(event["t_us"]))
    return parts


def episode_rows(found: list[dict], parts: dict[int, dict], ideal, sender_lines: list[dict],
                 contributor: list[dict]) -> list[dict]:
    """E3's split of each episode at its first part."""
    late = {int(line["epoch"]): line for line in sender_lines
            if line.get("kind") == "late" and line.get("epoch") is not None}
    arrivals = sorted(part["src_first"] - ideal(seq) for seq, part in parts.items()
                      if "src_first" in part)
    publish = sorted(part["sent_first"] - part["src_first"] for part in parts.values()
                     if "src_first" in part and "sent_first" in part)
    arrival_median = arrivals[(len(arrivals) - 1) // 2] if arrivals else 0
    publish_median = publish[(len(publish) - 1) // 2] if publish else 0
    rows = []
    for episode in found:
        first = episode["first"]
        part = parts.get(first, {})
        line = late.get(first, {})
        sender = int(line.get("late_first_us") or 0)
        arrived = part["src_first"] - ideal(first) - arrival_median if "src_first" in part else None
        waits = [int(event.get("dur_us") or 0) for event in sr.overlapping(
            [event for event in contributor if event.get("kind") == "rx_wait"],
            ideal(first), ideal(episode["last"]) + episode["excess_us"])]
        rows.append({
            "first": first, "parts": episode["parts"], "excess_ms": episode["excess_us"] / 1000,
            "woke_late_ms": int(line.get("woke_late_us") or 0) / 1000,
            "encode_ms": int(line.get("encode_us") or 0) / 1000,
            "link_ms": None if arrived is None else (arrived - sender) / 1000,
            "rx_wait_ms": max(waits, default=0) / 1000,
            "publish_ms": (part["sent_first"] - part["src_first"] - publish_median) / 1000
            if "src_first" in part and "sent_first" in part else None,
            "side": episode.get("side")})
    return rows


def analyse(directory) -> dict:
    directory = pathlib.Path(directory)
    record = json.loads((directory / "source-check.json").read_text())
    ideal_epoch = epoch_us(record["epoch_samples"])

    def ideal(seq: int) -> int:
        return int(record["start_unix"] * 1_000_000 + round((seq + 1) * ideal_epoch))

    parts = contributor_parts(sr.read_ndjson(directory / "contributor-hops.ndjson"),
                              record["stream"])
    lateness = {seq: part["sent_first"] - ideal(seq) for seq, part in parts.items()
                if "sent_first" in part}
    sender = sr.read_ndjson(directory / "sender.ndjson")
    contributor = sr.read_stalls(directory / "contributor-stalls.ndjson")
    watch = sr.read_stalls(directory / "host-watch.ndjson")
    sides = sr.source_sides(sr.sender_events(sender), contributor, watch, CONTRIBUTOR,
                            "the host")
    out = {"schema": "needletail.source-check-report.v1", "name": record["name"],
           "payload": record["payload"], "epoch_samples": record["epoch_samples"],
           "parts": len(lateness)}
    for above in EPISODE_ABOVE_US:
        found = sr.episodes(lateness, above)
        attributed = sr.attribute(found, ideal, sides)
        out[f"over_{above}us"] = {**attributed,
                                  "rows": episode_rows(found, parts, ideal, sender, contributor)}
    out["baseline_over_1ms"] = len(out["over_1000us"]["episodes"])
    out["all_attributed"] = out["over_500us"]["unattributed"] == 0
    return out


def num(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def format_report(result: dict) -> str:
    lines = [f"source check {result['name']}: {result['payload']} {result['epoch_samples']} "
             f"samples, {result['parts']} parts"]
    for above in EPISODE_ABOVE_US:
        item = result[f"over_{above}us"]
        sides = ", ".join(f"{side} {slot['n']} ({slot['ms']:.1f} ms)"
                          for side, slot in sorted(item["by_side"].items()))
        lines.append(f"over {above / 1000:g} ms: {len(item['episodes'])} episodes: {sides or 'none'}")
    lines.append(f"  {'first part':>12} {'parts':>5} {'excess':>8} {'woke late':>9} {'encode':>7} "
                 f"{'link':>7} {'rx wait':>7} {'publish':>8}  side (ms)")
    for row in result["over_500us"]["rows"]:
        lines.append(f"  {row['first']:>12} {row['parts']:>5} {num(row['excess_ms']):>8} "
                     f"{num(row['woke_late_ms']):>9} {num(row['encode_ms']):>7} "
                     f"{num(row['link_ms']):>7} {num(row['rx_wait_ms']):>7} "
                     f"{num(row['publish_ms']):>8}  {row['side']}")
    lines.append(f"3.1: {result['over_500us']['unattributed']} episodes over 0.5 ms unattributed "
                 f"({'met' if result['all_attributed'] else 'NOT met'}); "
                 f"{result['baseline_over_1ms']} over 1 ms (the normal-scheduling baseline)")
    return "\n".join(lines)


def command_report(args: argparse.Namespace) -> int:
    if not (pathlib.Path(args.dir) / "source-check.json").is_file():
        sys.exit(f"{args.dir}/source-check.json is missing: not a source check's directory")
    result = analyse(args.dir)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1) + "\n")
    print(format_report(result))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="the sender into the lab's contributor, then the report")
    run.add_argument("--seconds", type=int, default=600)
    run.add_argument("--payload", choices=sorted(STREAMS), default="opus")
    run.add_argument("--epoch-samples", type=int, default=48)
    run.add_argument("--name", default=time.strftime("source-%m%d%H%M", time.gmtime()))
    run.add_argument("--no-up", action="store_true", help="use the lab that is up")
    run.add_argument("--dry", action="store_true", help="print every command and run none")
    run.set_defaults(handler=command_run)
    report = sub.add_parser("report", help="the table of a run's directory")
    report.add_argument("dir")
    report.add_argument("--json", default=None)
    report.set_defaults(handler=command_report)
    args = parser.parse_args()
    sys.exit(args.handler(args))


if __name__ == "__main__":
    main()
