#!/usr/bin/env python3
"""First sessions on fresh local labs (D-hunt.md 2.11, 6): does the first
session after a deploy differ from a warm one, and what did each node's
stall journal see?

    first-session.py run [--labs 5] [--idle-s 990] [--burn-s 20] [--seconds 70]
                         [--payload opus|pcm] [--name NAME] [--dry]
    first-session.py report DIR [--json OUT]

`run` builds --labs fresh labs, one after another, each in the referee's
order (needletail-compare's compare-audio: deploy, the lab idle as long as
the referee's was before its first session, its 20 s burn on every node,
the contributor restarted, the first stream):
 1. `edge_lab.py up` (it takes any earlier lab of this checkout down first);
    `needletail-nodecheck watch` on the host for the lab's life;
 2. --idle-s seconds idle (990, the referee's 16.5 minutes);
 3. `edge_lab.py burn NODE --burn-s` on every mesh node at once
    (workstream C's verb);
 4. the contributor restarted: `edge_lab.py stop contrib-lon`, then
    `edge_lab.py start contrib-lon` (the lab's own verbs; C's CL1 adds no
    `restart`);
 5. the first session: scripts/compare/latency-bench.py --no-up --keep-up
    --skip-seconds 0, Opus 1 ms (or --payload), 2 UDP clients a host and a
    crowd of 40 listeners an edge with 8 sampled, as wave 9's gate ran it;
 6. every node's /api/stalls since the lab's start;
 7. the warm session: the same pass again, and the journals since it began;
 8. `edge_lab.py down`; the lab's files into the run archive.
Every command goes through the lab's own command line; `--dry` prints them
all and runs none. Output in target/first-session/<name>/lab<i>/ (the
passes themselves in target/latency-bench/<name>-lab<i>-first|warm/).

`report` reads such a directory: per lab and session, each UDP host's unit
latency p99 and max and missing (latency-bench's hops.json), first against
warm (wave 9's gate: p99 within 0.3 ms, 0 missing); per node and session
the stall journal's stops by cause and stopped ms; the null of every
reader probe (null-window.py, the referee's arithmetic). Exit 0; `run`
exits 1 when a step failed.
"""

from __future__ import annotations

import argparse
import importlib.util
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
BENCH = HERE / "latency-bench.py"
BENCH_OUT = ROOT / "target" / "latency-bench"
ARCHIVE_TOOL = ROOT / "scripts" / "run_archive.py"
OUT = ROOT / "target" / "first-session"
CONTRIBUTOR = "contrib-lon"
MESH_ROLES = ("contributor", "distributor", "edge")
# What workstream C's CL1 gives the lab (C-operations.md 2.12) and this
# tool needs: a verb of edge_lab.py's command dispatch.
LAB_VERBS = ("burn",)
GATE_P99_MS = 0.3
SESSIONS = ("first", "warm")
NODECHECK = pathlib.Path(os.environ.get(
    "NEEDLETAIL_NODECHECK_BIN", str(lab.MESH_ROOT / "target" / "release" / "needletail-nodecheck")))


def mesh_nodes() -> list[str]:
    """The nodes whose service runs in a default lab: the slots held out
    and the backbone relays (no service in the merged topology) left out."""
    return [name for name in lab.names() if lab.node(name)["role"] in MESH_ROLES
            and name not in lab.DEFAULT_SLOTS]


def stalls_url(name: str, since_us: int) -> str:
    port = lab.CONTRIB_HTTP_PORT if lab.node(name)["role"] == "contributor" else lab.HTTP_PORT
    return f"https://{lab.node(name)['public_ipv4']}:{port}/api/stalls?since_us={since_us}"


def bench_command(name: str, payload: str, seconds: int) -> list[str]:
    return [sys.executable, str(BENCH), "--name", name, "--no-up", "--keep-up",
            "--payload", payload, "--seconds", str(seconds), "--skip-seconds", "0",
            "--udp-clients", "2", "--load-listeners", "40", "--load-assign",
            "--load-args=--sample 8"]


class Steps:
    """Every command of a run: carried out, or with --dry printed only."""

    def __init__(self, dry: bool, sleep=time.sleep) -> None:
        self.dry = dry
        self.sleep = sleep
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

    def together(self, commands: list[list[str]], timeout: float) -> None:
        for command in commands:
            print("$ " + " ".join(shlex.quote(item) for item in command) + " &", flush=True)
        if self.dry:
            return
        running = [subprocess.Popen(command) for command in commands]
        for command, process in zip(commands, running):
            if process.wait(timeout=timeout) != 0:
                self.failed.append(f"{' '.join(command)}: exit {process.returncode}")

    def background(self, command: list[str]) -> subprocess.Popen | None:
        print("$ " + " ".join(shlex.quote(item) for item in command) + " &", flush=True)
        if self.dry:
            return None
        return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait(self, seconds: float, why: str) -> None:
        print(f"# {why}: {seconds:g} s", flush=True)
        if not self.dry:
            self.sleep(seconds)


def preflight() -> list[str]:
    problems = []
    source = EDGE_LAB.read_text()
    for verb in LAB_VERBS:
        # The dispatch of main(), not its usage text.
        if f'command == "{verb}"' not in source:
            problems.append(f"edge_lab.py has no `{verb}` (workstream C's CL1)")
    for path in (NODECHECK, ARCHIVE_TOOL):
        if not path.is_file():
            problems.append(f"missing: {path}")
    return problems


def pull_stalls(steps: Steps, directory: pathlib.Path, since_us: int) -> None:
    for name in mesh_nodes():
        steps.run([sys.executable, str(EDGE_LAB), "sh", name, "curl", "-sk", "--max-time", "60",
                   stalls_url(name, since_us)], stdout=directory / f"{name}.stalls.ndjson",
                  timeout=120)


def one_lab(steps: Steps, args: argparse.Namespace, index: int) -> dict:
    out = OUT / args.name / f"lab{index}"
    if not steps.dry:
        out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    entry = {"lab": index, "started_unix": round(started, 3), "sessions": {}}
    steps.run([sys.executable, str(EDGE_LAB), "up"])
    watch = steps.background([str(NODECHECK), "watch", "--out", str(out / "host-watch.ndjson")])
    steps.wait(args.idle_s, "idle, as the referee's lab before its first session")
    steps.together([[sys.executable, str(EDGE_LAB), "burn", name, str(args.burn_s)]
                    for name in mesh_nodes()], timeout=args.burn_s + 120)
    steps.run([sys.executable, str(EDGE_LAB), "stop", CONTRIBUTOR])
    steps.run([sys.executable, str(EDGE_LAB), "start", CONTRIBUTOR])
    for session in SESSIONS:
        began = time.time()
        # The first session's journals from the lab's start (its idle, the
        # burn and the restart included), the warm one's from its own.
        since = int((started if session == "first" else began) * 1e6)
        bench = f"{args.name}-lab{index}-{session}"
        steps.run(bench_command(bench, args.payload, args.seconds),
                  timeout=args.seconds + 600)
        directory = out / session
        if not steps.dry:
            directory.mkdir(parents=True, exist_ok=True)
        pull_stalls(steps, directory, since)
        entry["sessions"][session] = {"bench": bench, "from_unix": round(began, 3),
                                      "to_unix": round(time.time(), 3), "stalls_since_us": since}
    if watch is not None:
        watch.terminate()
        watch.wait(timeout=30)
    steps.run([sys.executable, str(EDGE_LAB), "down"])
    if not steps.dry:
        (out / "lab.json").write_text(json.dumps(entry, indent=1) + "\n")
    return entry


def command_run(args: argparse.Namespace) -> int:
    problems = preflight()
    if problems and not args.dry:
        sys.exit("the first-session runs do not start:\n  " + "\n  ".join(problems))
    for problem in problems:
        print(f"# a real run would refuse: {problem}")
    steps = Steps(args.dry)
    archive = [sys.executable, str(ARCHIVE_TOOL), "--tool", "first-session"]
    steps.run(archive + ["create", args.name, "--kind", "local-lab", "--code", str(ROOT),
                         "--code", str(lab.MESH_ROOT), "--code", str(lab.CONTRIB_ROOT)])
    labs = []
    for index in range(1, args.labs + 1):
        labs.append(one_lab(steps, args, index))
        steps.run(archive + ["add-tree", args.name, str(OUT / args.name / f"lab{index}"),
                             "--as", f"lab{index}", "--kind", "raw", "--description",
                             "a fresh lab's stall journals and host watch"])
        for session in SESSIONS:
            steps.run(archive + ["add-tree", args.name,
                                 str(BENCH_OUT / f"{args.name}-lab{index}-{session}"),
                                 "--as", f"lab{index}/{session}/bench", "--kind", "raw",
                                 "--description", f"latency-bench's {session} session"])
    if not args.dry:
        record = {"schema": "needletail.first-session.v1", "name": args.name,
                  "payload": args.payload, "seconds": args.seconds, "idle_s": args.idle_s,
                  "burn_s": args.burn_s, "labs": labs}
        (OUT / args.name / "first-session.json").write_text(json.dumps(record, indent=1) + "\n")
        steps.run(archive + ["add", args.name, str(OUT / args.name / "first-session.json"),
                             "--kind", "result"])
    steps.run(archive + ["finish", args.name, "--status",
                         "failed" if steps.failed else "complete"])
    if not args.dry:
        print(format_report(analyse(OUT / args.name)))
    for failure in steps.failed:
        print(f"failed: {failure}")
    return 1 if steps.failed else 0


# ------------------------------------------------------------------ report

def udp_hosts(hops: dict) -> dict[str, dict]:
    """latency-bench's UDP figures per host: p99, max, missing."""
    out = {}
    for host, item in ((hops.get("udp_clients") or {}).get("hosts") or {}).items():
        latency = item.get("unit_latency_ms") or {}
        out[host] = {"p99_ms": latency.get("p99"), "max_ms": latency.get("max"),
                     "missing": item.get("missing")}
    return out


def gate(first: dict, warm: dict) -> dict[str, bool | None]:
    """Wave 9's gate per host: the first session's p99 within 0.3 ms of the
    warm one's, and nothing missing in it."""
    out = {}
    for host, figures in first.items():
        other = warm.get(host) or {}
        if figures.get("p99_ms") is None or other.get("p99_ms") is None:
            out[host] = None
            continue
        out[host] = (figures["p99_ms"] - other["p99_ms"] <= GATE_P99_MS
                     and not figures.get("missing"))
    return out


def journals(directory: pathlib.Path) -> dict[str, dict]:
    out = {}
    for path in sorted(directory.glob("*.stalls.ndjson")):
        events = sr.read_stalls(path)
        out[path.name[:-len(".stalls.ndjson")]] = {
            "stops": sr.stop_counts(events), "stopped_ms": round(sr.stopped_ms(events), 3),
            "own_class_passes": sum(1 for event in events if event.get("kind") == "class_pass"
                                    and event.get("cause") == "own")}
    return out


def nulls(bench: pathlib.Path) -> list[dict]:
    spec = importlib.util.spec_from_file_location("null_window", HERE / "null-window.py")
    null_window = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(null_window)
    frames = sorted(str(path) for path in bench.glob("*.probe.csv"))
    if not frames:
        return []
    return null_window.analyse(null_window.referee(), frames, [], [8], 5.0, 97.5, 120)["probes"]


def analyse(directory, with_null: bool = True) -> dict:
    directory = pathlib.Path(directory)
    labs = []
    for lab_dir in sorted(directory.glob("lab*"), key=lambda path: int(path.name[3:] or 0)):
        record = json.loads((lab_dir / "lab.json").read_text()) if (lab_dir / "lab.json").is_file() \
            else {"lab": int(lab_dir.name[3:]), "sessions": {}}
        entry = {"lab": record["lab"], "sessions": {}}
        for session in SESSIONS:
            bench = BENCH_OUT / ((record["sessions"].get(session) or {}).get("bench") or "")
            hops_path = bench / "hops.json"
            hops = json.loads(hops_path.read_text()) if hops_path.is_file() else {}
            entry["sessions"][session] = {
                "udp": udp_hosts(hops), "journals": journals(lab_dir / session),
                "null": nulls(bench) if with_null and bench.is_dir() else []}
        first, warm = (entry["sessions"][session]["udp"] for session in SESSIONS)
        entry["gate"] = gate(first, warm)
        labs.append(entry)
    return {"schema": "needletail.first-session-report.v1", "labs": labs}


def format_report(result: dict) -> str:
    lines = []
    for entry in result["labs"]:
        lines.append(f"lab {entry['lab']}")
        for session, item in entry["sessions"].items():
            for host, figures in sorted(item["udp"].items()):
                lines.append(f"  {session:<5} {host:<14} p99 {figures['p99_ms']} ms, max "
                             f"{figures['max_ms']} ms, missing {figures['missing']}")
            for node, journal in sorted(item["journals"].items()):
                stops = journal["stops"]["stopped"]
                if journal["stopped_ms"] or journal["own_class_passes"] \
                        or journal["stops"]["starved"]["over_20ms"]:
                    lines.append(f"  {session:<5} {node:<14} stopped {journal['stopped_ms']} ms "
                                 f"(>=20 ms all {stops['over_20ms']['all']} one "
                                 f"{stops['over_20ms']['one']}), starved "
                                 f"{journal['stops']['starved']['over_20ms']}, own class passes "
                                 f"{journal['own_class_passes']}")
            for probe in item["null"]:
                eight = probe.get("lengths", {}).get("8") or {}
                lines.append(f"  {session:<5} null {pathlib.Path(probe['frames']).name}: steady "
                             f"longest silence {probe.get('steady_gap_ms')} ms, 8 s window p99 "
                             f"{eight.get('p99_range_ms')}, referee's null fail rate "
                             f"{eight.get('null_fail_rate')}")
        for host, passed in sorted(entry["gate"].items()):
            lines.append(f"  gate {host}: first within {GATE_P99_MS} ms of warm and 0 missing: "
                         + ("-" if passed is None else "met" if passed else "NOT met"))
    return "\n".join(lines) or "no lab in this directory"


def command_report(args: argparse.Namespace) -> int:
    result = analyse(args.dir)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1) + "\n")
    print(format_report(result))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="N fresh labs, a first and a warm session in each")
    run.add_argument("--labs", type=int, default=5)
    run.add_argument("--idle-s", type=float, default=990.0)
    run.add_argument("--burn-s", type=int, default=20)
    run.add_argument("--seconds", type=int, default=70)
    run.add_argument("--payload", choices=("opus", "pcm"), default="opus")
    run.add_argument("--name", default=time.strftime("first-%m%d%H%M", time.gmtime()))
    run.add_argument("--dry", action="store_true", help="print every command and run none")
    run.set_defaults(handler=command_run)
    report = sub.add_parser("report", help="first against warm, journals and nulls")
    report.add_argument("dir")
    report.add_argument("--json", default=None)
    report.set_defaults(handler=command_report)
    args = parser.parse_args()
    sys.exit(args.handler(args))


if __name__ == "__main__":
    main()
