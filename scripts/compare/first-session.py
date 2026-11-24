#!/usr/bin/env python3
"""First sessions on fresh local labs (D-hunt.md 2.11, 6): does the first
session after a deploy differ from a warm one, and what did each node's
stall journal see?

    first-session.py run [--labs 5] [--idle-s 990] [--burn-s 20] [--seconds 70]
                         [--payload opus|pcm] [--name NAME] [--dry]
                         [--up-args "rt=1 slots=..."] [--bench-args "..."]
                         [--load-sites london] [--fine-ms 10] [--stack] [--no-warm]
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
`up` takes the settings that latency-bench's own `up` would give the same
pass (its lab_overrides: the payload's formats and epoch, and hop journals
sized to the session, so that the first seconds are kept) and --up-args
(e.g. `rt=1 slots=lon-edge-4,new-edge-3`: edge_lab.py's real time as on
Linode); --bench-args go to every pass. The mesh nodes are the lab's
standing services (a slot held out is not one). The host watch runs on the
lab's CPUs only (NEEDLETAIL_EDGE_LAB_CPUS). With --fine-ms (10, the
default; 0: none) class-thread-sampler.py reads every mesh node's class
thread that often from the contributor's restart on (lab<i>/class-threads
.ndjson: state, wait channel, run and run delay); --stack runs it under
`sudo -n` and keeps the kernel stack of every sample whose thread is not
asleep in its poll (read-only diagnostics on the lab's own processes).
Before `down` the lab's rendered arguments, CPU layout, limits and rt
check are kept in lab<i>/lab-state/.
Every command goes through the lab's own command line; `--dry` prints them
all and runs none. Output in target/first-session/<name>/lab<i>/ (the
passes themselves in target/latency-bench/<name>-lab<i>-first|warm/). The
run archive's run <name> gets each lab's files as the lab ends (lab<i>/),
each latency-bench pass as it ends (the pass's own run id is <name>, so its
files go to <name>-lab<i>-first|warm/), the record, and its end: failed
when a step failed or the run was cut off, a cut-off lab's files kept.

From the contributor's restart to the end of the warm session every mesh
node runs the hunt's 1 s sampler (scripts/hunt-sampler.sh, as on the
Linode lab: the audio class thread's schedstat and switches, every
thread's policy and CPU, vmstat, the fast plane's counters), into
lab<i>/samplers/<node>/.

`report` reads such a directory: per lab and session, each UDP host's unit
latency p99 and max and missing (latency-bench's hops.json), first against
warm (wave 9's gate: p99 within 0.3 ms, 0 missing); per node and session
the stall journal's stops by cause and stopped ms, and the class thread's
stops of 100 ms or more that its sampler shows, each classified as on the
Linode lab (not run, blocked in our code, ...: stall_records.classify); the
null of every reader probe (null-window.py, the referee's arithmetic); per
session and mesh node, every commit stop of 100 ms or more from the pass's
hop journals (hops.raw.ndjson), with the kernel's receive stamps
(relay_first) and the class thread's wakes (relay_woken) over the same
window and, from the 10 ms sampler, what the class thread was doing.
Exit 0; `run` exits 1 when a step failed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import archived_run  # noqa: E402
import edge_lab as lab  # noqa: E402
import stall_records as sr  # noqa: E402

EDGE_LAB = ROOT / "scripts" / "edge_lab.py"
BENCH = HERE / "latency-bench.py"
BENCH_OUT = ROOT / "target" / "latency-bench"
ARCHIVE_TOOL = ROOT / "scripts" / "run_archive.py"
OUT = ROOT / "target" / "first-session"
# A step past its timeout gets SIGTERM, then SIGKILL this much later.
STOP_GRACE_S = 120.0
CONTRIBUTOR = "contrib-lon"
MESH_ROLES = ("contributor", "distributor", "edge")
# What workstream C's CL1 gives the lab (C-operations.md 2.12) and this
# tool needs: a verb of edge_lab.py's command dispatch.
LAB_VERBS = ("burn",)
GATE_P99_MS = 0.3
SESSIONS = ("first", "warm")
SAMPLER = ROOT / "scripts" / "hunt-sampler.sh"
# A class thread's stop that its sampler shows, from this length.
SAMPLER_STOP_MS = 100.0
# A commit stop in the hop journals, from this length.
COMMIT_STOP_MS = 100.0
FINE_SAMPLER = HERE / "class-thread-sampler.py"
NODECHECK = pathlib.Path(os.environ.get(
    "NEEDLETAIL_NODECHECK_BIN", str(lab.MESH_ROOT / "target" / "release" / "needletail-nodecheck")))


def mesh_nodes() -> list[str]:
    """The nodes whose service runs: the lab's standing services once it is
    rendered (a slot held out by `up slots=` is not one); before that, a
    default lab's (the dist-c slots and the backbone relays left out)."""
    if lab.load("rendered.json"):
        return [name for name in lab.standing_services()
                if lab.node(name)["role"] in MESH_ROLES]
    return [name for name in lab.names() if lab.node(name)["role"] in MESH_ROLES
            and name not in lab.DEFAULT_SLOTS]


def split(text: str | None) -> list[str]:
    return shlex.split(text or "")


def bench_module():
    spec = importlib.util.spec_from_file_location("latency_bench", BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def up_command(args: argparse.Namespace) -> list[str]:
    """edge_lab.py up with what latency-bench's own `up` would give the
    first pass (lab_overrides: formats, epoch, hop journals sized to the
    session) and --up-args."""
    bench = bench_module()
    parsed = bench.build_parser().parse_args(
        bench_command("first", args.payload, args.seconds, args.name, args)[2:])
    return [sys.executable, str(EDGE_LAB), "up", *bench.lab_overrides(parsed),
            *split(args.up_args)]


def host_pin() -> list[str]:
    """taskset to the lab's CPU range, when it has one."""
    cpus = os.environ.get("NEEDLETAIL_EDGE_LAB_CPUS")
    return ["taskset", "-c", cpus] if cpus else []


def fine_command(args: argparse.Namespace, out: pathlib.Path) -> list[str] | None:
    """The 10 ms class-thread sampler on every mesh node's service, on the
    lab's own CPUs (its tail), as the user or, with --stack, under sudo."""
    if not args.fine_ms:
        return None
    nodes = [f"{name}={lab.service_pid(name) or 0}" for name in mesh_nodes()]
    rest = lab.load("cpus.json").get("_rest")
    pin = ["taskset", "-c", lab.cpu_list(rest)] if rest else []
    root = ["sudo", "-n"] if args.stack else []
    return [*pin, *root, sys.executable, "-I", str(FINE_SAMPLER),
            str(out / "class-threads.ndjson"), *nodes, "--period-ms", str(args.fine_ms),
            *(["--stack"] if args.stack else [])]


def keep_lab_state(out: pathlib.Path) -> None:
    """The lab's render, layout, limits and rt check, before `down`."""
    target = out / "lab-state"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("rendered.json", "cpus.json", "limits.json", "rt.json", "rt-check.json",
                 "binaries.json", "services.json", "settings.json"):
        source = lab.WORK / name
        if source.is_file():
            (target / name).write_bytes(source.read_bytes())


def stalls_url(name: str, since_us: int) -> str:
    port = lab.CONTRIB_HTTP_PORT if lab.node(name)["role"] == "contributor" else lab.HTTP_PORT
    return f"https://{lab.node(name)['public_ipv4']}:{port}/api/stalls?since_us={since_us}"


def sampler_command(name: str, out: pathlib.Path) -> list[str]:
    """The hunt's sampler of a node, run inside it (`edge_lab.py sh`), on
    the pid its service has now."""
    contributor = lab.node(name)["role"] == "contributor"
    port = lab.CONTRIB_HTTP_PORT if contributor else lab.HTTP_PORT
    pid = lab.service_pid(name)
    return [sys.executable, str(EDGE_LAB), "sh", name, "sh", str(SAMPLER), str(out / name),
            str(pid) if pid else f"<pid of {name}>",
            "audio-hot-0" if contributor else "needletail-fast",
            f"https://{lab.node(name)['public_ipv4']}:{port}",
            "/api/pacing" if contributor else ""]


def bench_command(name: str, payload: str, seconds: int, run_id: str,
                  args: argparse.Namespace | None = None) -> list[str]:
    return [sys.executable, str(BENCH), "--name", name, "--no-up", "--keep-up",
            "--payload", payload, "--seconds", str(seconds), "--skip-seconds", "0",
            "--udp-clients", "2", "--load-listeners", "40", "--load-assign",
            "--load-args=--sample 8", "--run-id", run_id,
            *(["--load-sites", args.load_sites] if args is not None and args.load_sites else []),
            *split(getattr(args, "bench_args", None))]


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
            process = subprocess.Popen(command, stdout=handle if stdout else None)
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # SIGTERM first: a latency-bench pass then keeps its files
                # in the run archive before it ends; SIGKILL after a grace.
                process.terminate()
                try:
                    process.wait(timeout=STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
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
        """A command left running, in a process group of its own (what it
        starts inside a node is stopped with it)."""
        print("$ " + " ".join(shlex.quote(item) for item in command) + " &", flush=True)
        if self.dry:
            return None
        return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)

    def stop(self, process: subprocess.Popen | None) -> None:
        """A background command and its group, ended (SIGTERM)."""
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait(timeout=30)

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
    entry = {"lab": index, "started_unix": round(started, 3), "sessions": {},
             "payload": args.payload,
             "up_args": split(args.up_args), "bench_args": split(args.bench_args),
             "load_sites": args.load_sites, "fine_ms": args.fine_ms, "stack": args.stack,
             "binaries": {key: os.environ.get(key) for key in (
                 "NEEDLETAIL_AV_MESH_BIN", "NEEDLETAIL_AV_CONTRIB_BIN")}}
    steps.run(up_command(args))
    watch = steps.background([*host_pin(), str(NODECHECK), "watch", "--out",
                              str(out / "host-watch.ndjson")])
    steps.wait(args.idle_s, "idle, as the referee's lab before its first session")
    steps.together([[sys.executable, str(EDGE_LAB), "burn", name, str(args.burn_s)]
                    for name in mesh_nodes()], timeout=args.burn_s + 120)
    steps.run([sys.executable, str(EDGE_LAB), "stop", CONTRIBUTOR])
    steps.run([sys.executable, str(EDGE_LAB), "start", CONTRIBUTOR])
    samplers = [steps.background(sampler_command(name, out / "samplers"))
                for name in mesh_nodes()]
    fine = fine_command(args, out) if not steps.dry else None
    if fine:
        samplers.append(steps.background(fine))
    for session in SESSIONS[:1] if args.no_warm else SESSIONS:
        began = time.time()
        # The first session's journals from the lab's start (its idle, the
        # burn and the restart included), the warm one's from its own.
        since = int((started if session == "first" else began) * 1e6)
        bench = f"{args.name}-lab{index}-{session}"
        steps.run(bench_command(bench, args.payload, args.seconds, args.name, args),
                  timeout=args.seconds + 600)
        directory = out / session
        if not steps.dry:
            directory.mkdir(parents=True, exist_ok=True)
        pull_stalls(steps, directory, since)
        entry["sessions"][session] = {"bench": bench, "from_unix": round(began, 3),
                                      "to_unix": round(time.time(), 3), "stalls_since_us": since}
    for process in samplers + [watch]:
        steps.stop(process)
    if not steps.dry:
        keep_lab_state(out)
    steps.run([sys.executable, str(EDGE_LAB), "down"])
    if not steps.dry:
        (out / "lab.json").write_text(json.dumps(entry, indent=1) + "\n")
    return entry


# The options that came after the first version, with their defaults, for a
# caller that builds its own Namespace.
RUN_DEFAULTS = {"up_args": "", "bench_args": "", "load_sites": "", "fine_ms": 10.0,
                "stack": False, "no_warm": False}


def command_run(args: argparse.Namespace) -> int:
    for key, value in RUN_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    problems = preflight()
    if problems and not args.dry:
        sys.exit("the first-session runs do not start:\n  " + "\n  ".join(problems))
    for problem in problems:
        print(f"# a real run would refuse: {problem}")
    steps = Steps(args.dry)
    archive = [sys.executable, str(ARCHIVE_TOOL), "--tool", "first-session"]
    steps.run(archive + ["create", args.name, "--kind", "local-lab",
                         *[item for path in archived_run.code_paths(lab.MESH_ROOT, lab.CONTRIB_ROOT)
                           for item in ("--code", str(path))]])

    def keep(index: int) -> None:
        if steps.dry or (OUT / args.name / f"lab{index}").is_dir():
            steps.run(archive + ["add-tree", args.name, str(OUT / args.name / f"lab{index}"),
                                 "--as", f"lab{index}", "--kind", "raw", "--description",
                                 "a fresh lab's stall journals and host watch"])

    labs = []
    try:
        for index in range(1, args.labs + 1):
            labs.append(one_lab(steps, args, index))
            keep(index)
        if not args.dry:
            record = {"schema": "needletail.first-session.v1", "name": args.name,
                      "payload": args.payload, "seconds": args.seconds, "idle_s": args.idle_s,
                      "burn_s": args.burn_s, "up_args": split(args.up_args),
                      "bench_args": split(args.bench_args), "labs": labs}
            (OUT / args.name / "first-session.json").write_text(json.dumps(record, indent=1) + "\n")
            steps.run(archive + ["add", args.name, str(OUT / args.name / "first-session.json"),
                                 "--kind", "result"])
    finally:
        cut = len(labs) < args.labs
        if cut:
            keep(len(labs) + 1)
        steps.run(archive + ["finish", args.name, "--status",
                             "failed" if steps.failed or cut else "complete",
                             *(["--reason", "the run was cut off"] if cut else [])])
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


def bench_dir(name: str) -> pathlib.Path:
    """A pass's results: target/latency-bench/[ID/]NAME (latency-bench's)."""
    return BENCH_OUT / lab.LAB_ID / name if lab.LAB_ID else BENCH_OUT / name


PAYLOAD_STREAM = {"flac": 2001, "opus": 3001, "pcm": 4001}
JOURNAL_STAGES = ("committed", "relay_first", "relay_woken", "udp_sent", "relay_forward")


def longest_gap(times: list[int], start_us: int, end_us: int) -> float | None:
    """The longest gap (ms) between consecutive `times` (sorted) that
    overlaps [start_us, end_us], the events just outside it included."""
    import bisect
    if not times:
        return None
    low = max(0, bisect.bisect_left(times, start_us) - 1)
    high = min(len(times), bisect.bisect_right(times, end_us) + 1)
    window = times[low:high]
    if len(window) < 2:
        return None
    return round(max(b - a for a, b in zip(window, window[1:])) / 1000, 3)


def journal_times(bench: pathlib.Path, stream: int) -> dict[str, dict[str, list[int]]]:
    """{node: {stage: sorted t_us}} of the stream in a pass's hops.raw.ndjson."""
    out: dict[str, dict[str, list[int]]] = {}
    path = bench / "hops.raw.ndjson"
    if not path.is_file():
        return out
    with open(path) as handle:
        for line in handle:
            if '"stage"' not in line:
                continue
            item = json.loads(line)
            if item.get("stream") != stream or item.get("stage") not in JOURNAL_STAGES:
                continue
            node = item.get("_node") or item.get("node")
            out.setdefault(node, {}).setdefault(item["stage"], []).append(int(item["t_us"]))
    for stages in out.values():
        for values in stages.values():
            values.sort()
    return out


def commit_stops(bench: pathlib.Path, stream: int,
                 min_ms: float = COMMIT_STOP_MS) -> dict[str, dict]:
    """Per node of the pass's hop journals: its commits (count, first and
    last) and every gap of `min_ms` or more between two of them (before the
    stream's end: the earliest last commit of any node), with the
    longest gap of its kernel receive stamps (relay_first: did the input
    stop too?), of its class thread's wakes (relay_woken) and of its sends
    to clients (udp_sent) over the same window."""
    out = {}
    times = journal_times(bench, stream)
    # The stream's end: the earliest last commit of any node (the sender has
    # stopped); a gap that begins after it is the tail, not a stop.
    ends = [stages["committed"][-1] for stages in times.values() if stages.get("committed")]
    end = min(ends) if ends else None
    for node, stages in sorted(times.items()):
        commits = stages.get("committed") or []
        stops = []
        for a, b in zip(commits, commits[1:]):
            if end is not None and a >= end:
                break
            if (b - a) / 1000 >= min_ms:
                stops.append({"from_us": a, "to_us": b, "ms": round((b - a) / 1000, 3),
                              **{f"{stage}_gap_ms": longest_gap(stages.get(stage) or [], a, b)
                                 for stage in ("relay_first", "relay_woken", "udp_sent")}})
        out[node] = {"commits": len(commits), "first_us": commits[0] if commits else None,
                     "last_us": commits[-1] if commits else None, "stops": stops}
    return out


def fine_lines(lab_dir: pathlib.Path) -> list[dict]:
    path = lab_dir / "class-threads.ndjson"
    if not path.is_file():
        return []
    return [item for item in sr.read_ndjson(path) if "t_us" in item]


def fine_view(lines: list[dict], node: str, start_us: int, end_us: int) -> dict | None:
    """What the 10 ms sampler saw of a node's class thread over a stop:
    its states and wait channels, run and run delay grown, switches, and
    the kernel stacks it kept."""
    import collections
    inside = [line for line in lines if start_us <= line["t_us"] <= end_us
              and line["n"].get(node) and line["n"][node][0] is not None]
    before = [line for line in lines if line["t_us"] < start_us and line["n"].get(node)
              and line["n"][node][0] is not None]
    after = [line for line in lines if line["t_us"] > end_us and line["n"].get(node)
             and line["n"][node][0] is not None]
    if not inside and not (before and after):
        return None
    states = collections.Counter(line["n"][node][0] for line in inside)
    waits = collections.Counter(f"{line['n'][node][0]} {line['n'][node][1]}" for line in inside)
    stacks = collections.Counter(" < ".join(line["stack"][node][:6]) for line in inside
                                 if (line.get("stack") or {}).get(node))
    out = {"samples": len(inside), "states": dict(states), "waits": dict(waits.most_common(4)),
           "stacks": dict(stacks.most_common(3))}
    if before and after:
        a, b = before[-1]["n"][node], after[0]["n"][node]
        out.update({"span_ms": round((after[0]["t_us"] - before[-1]["t_us"]) / 1000, 1),
                    "run_ms": round((b[2] - a[2]) / 1e6, 1),
                    "run_delay_ms": round((b[3] - a[3]) / 1e6, 1),
                    "voluntary": None if a[4] is None else b[4] - a[4],
                    "involuntary": None if a[5] is None else b[5] - a[5],
                    "cpus": sorted({line["n"][node][6] for line in before[-1:] + inside + after[:1]})})
    return out


def watch_ticks(lab_dir: pathlib.Path, cpus: list[int], start_us: int, end_us: int) -> dict:
    """The host watch's late ticks on `cpus` that overlap a stop: the
    longest, and how many (a tick thread on the class thread's CPU that ran
    on time says the CPU ran)."""
    path = lab_dir / "host-watch.ndjson"
    found = []
    if path.is_file() and cpus:
        for item in sr.read_ndjson(path):
            if item.get("kind") != "tick" or item.get("cpu") not in cpus:
                continue
            end = int(item["t_us"])
            begin = end - int(item.get("dur_us") or 0)
            if begin <= end_us and end >= start_us:
                found.append(item)
    return {"late_ticks": len(found),
            "longest_ms": round(max((int(item.get("dur_us") or 0) for item in found),
                                    default=0) / 1000, 1),
            "causes": sorted({str(item.get("cause")) for item in found})}


def session_stops(lab_dir: pathlib.Path, bench: pathlib.Path, stream: int,
                  lines: list[dict] | None = None) -> dict[str, dict]:
    """commit_stops of a pass, each stop with the 10 ms sampler's view of
    the node's class thread and the host watch's ticks on its CPUs."""
    lines = fine_lines(lab_dir) if lines is None else lines
    layout = {}
    state = lab_dir / "lab-state" / "cpus.json"
    if state.is_file():
        layout = json.loads(state.read_text())
    out = commit_stops(bench, stream)
    for node, item in out.items():
        for stop in item["stops"]:
            stop["fine"] = fine_view(lines, node, stop["from_us"], stop["to_us"])
            stop["watch"] = watch_ticks(lab_dir, layout.get(node) or [], stop["from_us"],
                                        stop["to_us"])
    return out


def class_stops(directory: pathlib.Path, since_us: int | None,
                until_us: int | None) -> dict[str, list[dict]]:
    """Per node, the class thread's stops of SAMPLER_STOP_MS or more that
    its sampler shows in [since_us, until_us], each classified."""
    out = {}
    for path in sorted(directory.glob("*/sampler.ndjson")):
        name = path.parent.name
        lines = sr.sampler_lines(sr.read_ndjson(path))
        journal = sr.read_stalls(path.parent / "stalls.ndjson")
        found = []
        for start, end in sr.sampler_stops(lines, SAMPLER_STOP_MS):
            if (since_us is not None and end < since_us) or (until_us is not None and end > until_us):
                continue
            found.append({"from_us": start, "to_us": end, "ms": (end - start) / 1000,
                          **sr.classify(start, end, lines, journal)})
        out[name] = found
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
        lines = fine_lines(lab_dir)
        stream = PAYLOAD_STREAM.get(record.get("payload") or "opus", 3001)
        for session in SESSIONS:
            name = (record["sessions"].get(session) or {}).get("bench")
            bench = bench_dir(name) if name else BENCH_OUT / "-"
            hops_path = bench / "hops.json"
            hops = json.loads(hops_path.read_text()) if hops_path.is_file() else {}
            window = record["sessions"].get(session) or {}
            entry["sessions"][session] = {
                "udp": udp_hosts(hops), "journals": journals(lab_dir / session),
                "class_stops": class_stops(
                    lab_dir / "samplers",
                    int(window["from_unix"] * 1e6) if window.get("from_unix") else None,
                    int(window["to_unix"] * 1e6) if window.get("to_unix") else None),
                "null": nulls(bench) if with_null and bench.is_dir() else [],
                "commit_stops": session_stops(lab_dir, bench, stream, lines)
                if bench.is_dir() else {}}
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
            for node, found in sorted(item["class_stops"].items()):
                for stop in found:
                    detail = stop.get("where") or stop.get("wait") or ""
                    lines.append(f"  {session:<5} {node:<14} class thread stopped {stop['ms']:.0f} "
                                 f"ms: {stop['class']}" + (f", {detail}" if detail else "")
                                 + f" [{stop['why']}]")
            for node, found in sorted(item.get("commit_stops", {}).items()):
                for stop in found["stops"]:
                    fine = stop.get("fine") or {}
                    lines.append(
                        f"  {session:<5} {node:<14} commit stop {stop['ms']:.0f} ms at "
                        f"{(stop['from_us'] - (found['first_us'] or stop['from_us'])) / 1e6:.2f} s "
                        f"after its first commit: kernel stamps gap "
                        f"{stop['relay_first_gap_ms']} ms, wakes gap {stop['relay_woken_gap_ms']} "
                        f"ms; class thread {fine.get('states')} {fine.get('waits')} run "
                        f"{fine.get('run_ms')} ms, run delay {fine.get('run_delay_ms')} ms, "
                        f"switches {fine.get('voluntary')}/{fine.get('involuntary')}; watch "
                        f"{stop['watch']}" + (f"; stacks {fine['stacks']}" if fine.get('stacks')
                                              else ""))
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
    run.add_argument("--up-args", default="", help="more settings of edge_lab.py up (rt=1 ...)")
    run.add_argument("--bench-args", default="", help="more arguments of every latency-bench pass")
    run.add_argument("--load-sites", default="", help="latency-bench --load-sites (london)")
    run.add_argument("--fine-ms", type=float, default=10.0,
                     help="period of class-thread-sampler.py (0: not run)")
    run.add_argument("--no-warm", action="store_true",
                     help="the first session only (no warm pass after it)")
    run.add_argument("--stack", action="store_true",
                     help="the 10 ms sampler under sudo -n, with kernel stacks")
    run.set_defaults(handler=command_run)
    report = sub.add_parser("report", help="first against warm, journals and nulls")
    report.add_argument("dir")
    report.add_argument("--json", default=None)
    report.set_defaults(handler=command_report)
    args = parser.parse_args()
    sys.exit(args.handler(args))


if __name__ == "__main__":
    main()
