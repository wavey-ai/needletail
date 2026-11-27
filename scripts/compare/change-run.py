#!/usr/bin/env python3
"""Score one change on the local lab with the referee's own tools.

  change-run.py run --name N [--lanes opus-1,pcm-1] [--seconds 360] [--at 45]
        [--hosts lon-readers,new-readers] [--clients 1] [--load-listeners 40]
        [--readers 4] [--client-loss "loss 1%"] [--wan-loss newyork="loss 1%"]
        [--expect-exit 0] [--repeat N [--null-sleep S]] [--dry] [--no-up]
        [--keep-up] [--nice 10] [--set k=v ...] -- COMMAND...
  change-run.py score DIR     ingest and score again (a run, or a scenario's runs)

The lab is the one of NEEDLETAIL_EDGE_LAB_ID (scripts/edge_lab.py). A run:
1. brings the lab up (unless --no-up) and starts one AEP1 sender per lane as
   the referee sends it (aep1-48k-probe send, session id = START + one
   epoch, START the next 64 s boundary at least 8 s away, the referee's
   audio.s24le), and waits until every probed stream is live;
2. starts, on each host and lane, the referee's probe: the launch time
   written with `date +%s.%N`, then needletail-udp-client --entry <the
   site's dist-a> --stream 3001|4001 --arrivals A --write-parts P for
   --seconds; beside them --load-listeners needletail-load listeners an
   edge (as latency-bench does) and --readers steered needletail-readers;
3. at --at seconds runs COMMAND with the lab's environment. Its marks are
   the lines of its standard output that read `start|end <unix.ns> <label>`;
   a command that prints none is wrapped in `start`/`end` lines labelled
   --name; a label that the command opened and did not close (it died) is
   closed when the command is reaped, and an `aborted` event is kept. The run
   fails when the last end + 6 s is past the probes' end (the referee's
   window ends 5 s after the change, and its silence is measured 1 s more),
   when the command is still running when the probes end (it is stopped as
   an interrupt stops it), and when the command's exit status is not
   --expect-exit (0; a refusal scenario names its own): a change that did not
   happen proves nothing. A `sleep` null is expected to exit 0;
4. after the probes exit, ingests each with the referee's
   `ntcmp udp-ingest` and scores the marks on every probe with the referee's
   `change-window.py`, both called by path in $NEEDLETAIL_COMPARE_ROOT
   (default <workspace>/needletail-compare), where nothing is written.

The referee's rule (needletail-compare ac774b9): a change passes when no unit
is missing, and its longest silence and its p99 are within the 97.5th
percentile of the same probe's no-change windows; with fewer than 120 such
windows it is not scored. That needs about 300 s of steady probe around the
change, so --seconds is 360 or more and the change starts at --at >= 10 s.

The null. The rule fails about one clean window in five by chance on short
probes (D-hunt, departure 1), and some on long ones, so every result is
reported beside the null of the same scenario: the same probes, length and
label with nothing changed. --dry appends --dry-marks to COMMAND (an engine
command: gates and marks, no restart); a COMMAND that is `sleep S` is the
null of a plain kill or crash. Both are kept as null runs of the scenario. A
scenario is proved locally when 10 of 10 changes pass and its null passes 10
of 10 in the same layout; a null that fails at all goes to the referee with
its figures before any change is claimed. Only scored runs (pass or fail)
count: a void, unscored or interrupted run is reported on its own and is no
evidence either way.

SIGINT, SIGTERM and SIGHUP stop a run cleanly: the command gets SIGINT (the
engine resumes a drained node and prints its end) and is killed after 30 s,
the probes are stopped, the links' impairments are taken off, the senders
stopped and the lab taken down (unless --keep-up or --no-up); the run is kept
as `interrupted`, and the runner exits 128 + the signal.

Outside interference (C 2.12). Over the probes' time the run reads the busy
time (user, nice, system) of the lab's CPUs from /proc/stat, less the CPU
time of the lab's own scopes, as a share of the CPUs, and their steal. The
run is void when that share is more than 2 points above the floor that
lab-parity.py measured (target/lab-parity/floor.json), in the layout it was
measured in (the lab's CPU range); in another layout there is no floor and
nothing is void.

Repeats (C 6.2's 10 of 10). --repeat N runs N changes; for an engine command
(roll, restart, replace) without --dry, each change is followed by its dry
null (COMMAND --dry-marks), and with --null-sleep S a plain command's by
`sleep S`: change, null, change, null... The two rates are printed side by
side with the median and range of CW's null_fail_rate over every probe and
label of the runs.

The probes' own reports (local proof 2: CW passed probes that were stuck, or
whose reports counted 629k missing parts). After CW, each probe's uc.json
and frames are read: a probe that wrote no report, was stuck (datagrams more
than 3 times its accepted parts, or fewer than half its duration's parts) or
counts more than 1% of its parts missing is `probe invalid` whatever CW
said; CW's pass of a probe with fewer than 90% of the units its window holds
(probe.csv, 2 s before the start to 5 s after the end), or with fewer legs
carrying datagrams than it asked for, is refused the same way. A run whose
only trouble is an invalid probe is `invalid`: not scored, neither pass nor
fail. The probes start 10 s or more after every probed stream is live (F2:
listeners that join at a session's start can get stuck).

The runner's own cost (F3). The change's command runs at --nice 10. In a CPU
layout the lab's "_rest" is split (edge_lab.rest_split): the sender on the
source host's own CPUs (or the last of the rest), the probes on half of the
rest, the command, the crowd and the readers on the other half; what the
command starts through edge_lab stays there (NEEDLETAIL_EDGE_LAB_TOOL_CPUS).
run.json keeps `cpu_roles` and `nice`.

Results: target/change-run/[ID/]NAME/change-NN or null-NN: run.json (the
plan and the result), marks.txt, command.out, command.err, verdict.json,
the probes' files, nodes-before.json, nodes-after.json; NAME/summary.json.
Exit 0 when every run passed.

Every invocation of `run` is also one run in the run archive
(~/needletail-data/<run id>/, NEEDLETAIL_DATA_ROOT overrides; --run-id,
else change-run-<UTC time>), written as it goes and whatever ends it:
NAME/<change-NN|null-NN>/ each run's directory as it ends (probe CSV and
JSON, launch times, marks, the command's output, the scorer's JSON and log,
the crowd's and readers' output, the nodes before and after) with
node-logs/, what each node's log gained in it; NAME/senders/ what the
senders' logs gained; NAME/node-logs-end/ the logs up to the lab's down;
NAME/summary.json. A run cut off ends its archive run failed.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import archived_run  # noqa: E402
import edge_lab as lab  # noqa: E402
import lab_guard as guard  # noqa: E402
import mesh_ops  # noqa: E402

_spec = importlib.util.spec_from_file_location("latency_bench", HERE / "latency-bench.py")
bench = sys.modules.get("latency_bench") or importlib.util.module_from_spec(_spec)
if "latency_bench" not in sys.modules:
    sys.modules["latency_bench"] = bench
    _spec.loader.exec_module(bench)

# The lanes the referee probes: stream, payload, epoch and ntcmp index
# (compare_lab.py audio makes the indexes; compare-audio.py nt_index).
LANES = {
    "opus-1": {"stream": 3001, "payload": "opus", "epoch_samples": 48, "index": "nt-popus-48s-la"},
    "opus-2.5": {"stream": 3001, "payload": "opus", "epoch_samples": 120,
                 "index": "nt-popus-120s-la"},
    "pcm-1": {"stream": 4001, "payload": "pcm", "epoch_samples": 48, "index": "nt-pcm-48s"},
}
UC_LANE = "nt-uc"
# The referee's change steps run probes of 360 s or more (notes-stage2).
MIN_SECONDS = 360
# CW drops a probe's first 5 s and widens a window 2 s before it.
MIN_AT = 10
SKIP_SECONDS = 5
# CW widens a window 5 s after its end and measures silence 1 s past it.
AFTER_END_S = 6
MARK = re.compile(r"(start|end) [0-9]+\.[0-9]+ \S+")
NAME = re.compile(r"[A-Za-z0-9._-]+")
VOID_POINTS = 2.0
FLOOR_FILE = ROOT / "target" / "lab-parity" / "floor.json"
SAMPLE_INTERVAL_S = 1.0
PROBE_GRACE_S = 90
COMMAND_STOP_S = 30
LIVE_TIMEOUT_S = 60
# Ingest and score of one run, for the senders' bound.
SCORE_MARGIN_S = 300
PROVED_RUNS = 10
# CW scores a change only with this many no-change windows of its length.
MIN_NULL_WINDOWS = 120
SCORED = ("pass", "fail")
STATUSES = ("pass", "fail", "void", "unscored", "invalid", "interrupted")
# Probes start this long after every probed stream is live (local proof 2,
# F2: listeners that joined at a session's start got stuck).
PROBE_AFTER_LIVE_S = 10.0
# A probe's own report (uc.json) against CW's pass (local proof 2: CW passed
# probes whose reports had 629k missing and a 50 s p99): stuck when its
# datagrams outnumber its accepted parts this many times (two legs carry
# about 2), or it accepted less than this share of its duration's parts.
STUCK_DATAGRAMS_PER_PART = 3.0
MIN_RECEIVED_SHARE = 0.5
# Its own missing parts over its whole run: more than this share (or count).
MAX_OWN_MISSING_SHARE = 0.01
MAX_OWN_MISSING = 100
# The units in a label's window: at least this share of what its length holds.
MIN_WINDOW_UNITS_SHARE = 0.9
# CW widens a window this long before its start.
BEFORE_START_S = 2
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class Stopped(BaseException):
    """SIGTERM or SIGHUP: unwinds a run as SIGINT does, so that its
    clean-up runs."""

    def __init__(self, signum: int):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", file=sys.stderr, flush=True)


def now_ns() -> int:
    return time.time_ns()


def now() -> float:
    return now_ns() / 1e9


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def unix_ns_text(ns: int) -> str:
    """Unix seconds with nine decimals, as `date +%s.%N` writes them."""
    return f"{ns // 1_000_000_000}.{ns % 1_000_000_000:09d}"


# ------------------------------------------------------------------ the referee's tools

def compare_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT")
                        or str(lab.WORKSPACE / "needletail-compare"))


def ntcmp() -> pathlib.Path:
    return compare_root() / "target" / "ntcmp"


def change_window() -> pathlib.Path:
    return compare_root() / "scripts" / "compare" / "change-window.py"


def audio_media() -> pathlib.Path:
    return compare_root() / "target" / "compare-media" / "audio"


def out_root() -> pathlib.Path:
    """target/change-run, and a directory of the lab's id when it has one."""
    base = ROOT / "target" / "change-run"
    return base / lab.LAB_ID if lab.LAB_ID else base


# ------------------------------------------------------------------ arguments

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    verbs = parser.add_subparsers(dest="verb", required=True)
    run = verbs.add_parser("run", help="run a change (or its null) through the probes")
    run.add_argument("--name", required=True, help="the scenario: its directory and its label")
    run.add_argument("--lanes", default="opus-1,pcm-1",
                     help=f"probed lanes, of {', '.join(LANES)} (opus-1 and opus-2.5 share "
                          "stream 3001: one of them a pass)")
    run.add_argument("--seconds", type=int, default=MIN_SECONDS,
                     help=f"how long each probe listens (at least {MIN_SECONDS})")
    run.add_argument("--at", type=float, default=45.0,
                     help=f"seconds after the probes start that COMMAND runs (at least {MIN_AT})")
    run.add_argument("--hosts", default="lon-readers,new-readers", help="probe hosts")
    run.add_argument("--clients", type=int, default=1, help="probes a host and lane")
    run.add_argument("--load-listeners", type=int, default=40,
                     help="needletail-load listeners an edge of the probed sites (0: none)")
    run.add_argument("--readers", type=int, default=4,
                     help="steered needletail-reader sessions a host, on the first lane (0: none)")
    run.add_argument("--client-loss", default="", metavar="SPEC",
                     help="netem impairment of the probe hosts' links, in and out, on top of "
                          "their access delay (e.g. \"loss 1%%\")")
    run.add_argument("--wan-loss", action="append", default=[], metavar="SITE=SPEC",
                     help="netem impairment of a site's wide-area link, both directions, on top "
                          "of its delay (e.g. newyork=\"loss 1%%\"); repeat")
    run.add_argument("--expect-exit", type=int, default=0, metavar="N",
                     help="COMMAND's exit status when the change happened as the scenario "
                          "means (a refusal scenario's 3 or 4); another fails the run")
    run.add_argument("--repeat", type=int, default=None, metavar="N",
                     help="N runs, one after another (default 1); for an engine command "
                          "(roll, restart, replace) without --dry, or with --null-sleep, N "
                          "changes alternating with N nulls, their rates printed side by side")
    run.add_argument("--null-sleep", type=float, default=None, metavar="S",
                     help="the null of a plain command (a kill, a crash) for --repeat: "
                          "`sleep S` under the same label")
    run.add_argument("--dry", action="store_true",
                     help="the scenario's null: COMMAND with --dry-marks")
    run.add_argument("--no-up", action="store_true", help="use the lab that is running")
    run.add_argument("--keep-up", action="store_true", help="leave the lab running afterwards")
    run.add_argument("--run-id", default=None,
                     help="the run in the archive (default change-run-<UTC time>)")
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="a lab setting for `up`; repeat")
    run.add_argument("--sender-host", choices=("auto", "contrib-lon", lab.SOURCE_HOST),
                     default="auto",
                     help="where the senders run: auto = the source host, sending every epoch "
                          "to each contributor, when the lab runs more than one (A 6); else "
                          "the contributor's own host, to itself")
    run.add_argument("--nice", type=int, default=mesh_ops.NICE,
                     help="the change's command runs at this nice level (default "
                          f"{mesh_ops.NICE}; 0 leaves it): below the probes and the sender")
    run.add_argument("--crowd-edges", action="store_true",
                     help="the crowd is answered from the edges' list (no entry, no spare) "
                          "instead of asking the site's entry points as a listener does")
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND...")
    score = verbs.add_parser("score", help="ingest and score again")
    score.add_argument("directory", help="a run's directory, or a scenario's")
    return parser


def lanes_of(text: str) -> list[str]:
    """The lanes of --lanes, Opus first (its sender's session id is the loop's
    own: ntcmp reads Opus parts by number)."""
    lanes = []
    for item in [item.strip() for item in text.split(",") if item.strip()]:
        if item not in LANES:
            raise SystemExit(f"--lanes: {item!r} is not one of {', '.join(LANES)}")
        if item not in lanes:
            lanes.append(item)
    if not lanes:
        raise SystemExit("--lanes names no lane")
    streams = [LANES[lane]["stream"] for lane in lanes]
    if len(set(streams)) != len(streams):
        raise SystemExit("--lanes: opus-1 and opus-2.5 are both stream 3001: run them in "
                         "separate passes")
    return sorted(lanes, key=lambda lane: LANES[lane]["payload"] != "opus")


def wan_losses(items: list[str]) -> dict[str, str]:
    """--wan-loss SITE=SPEC: the netem of each site's wide-area link, the
    impairment on top of the link's own delay."""
    out = {}
    for item in items:
        site, _, impairment = item.partition("=")
        site = site.strip()
        if site not in lab.SITE_DELAY_MS or not impairment.strip():
            raise SystemExit(f"--wan-loss {item!r}: SITE=SPEC with a site of "
                             f"{', '.join(lab.SITE_DELAY_MS)}")
        words = impairment.split()
        if "delay" in words or "limit" in words:
            raise SystemExit("--wan-loss names the impairment only; the link keeps its delay")
        out[site] = f"{lab.netem_spec(delay_ms=lab.SITE_DELAY_MS[site])} {' '.join(words)}"
    return out


def command_of(args) -> list[str]:
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    return command


def is_sleep(command: list[str]) -> bool:
    return bool(command) and pathlib.Path(command[0]).name == "sleep"


def change_command(args) -> list[str]:
    """COMMAND as it runs: with --dry-marks for --dry."""
    command = command_of(args)
    return command + ["--dry-marks"] if args.dry else command


def kind_of(args) -> str:
    """`null` for a dry run or a `sleep`, else `change`."""
    return "null" if args.dry or is_sleep(command_of(args)) else "change"


ENGINE_WORDS = ("roll", "restart", "replace")


def alternates(args) -> bool:
    """--repeat alternates changes with nulls: a change that has a null of
    its own, --dry-marks (an engine command) or --null-sleep."""
    command = command_of(args)
    if args.repeat is None or args.dry or is_sleep(command):
        return False
    return args.null_sleep is not None or any(word in ENGINE_WORDS for word in command[1:])


def repeats(args) -> int:
    return args.repeat if args.repeat is not None else 1


def runs_planned(args) -> int:
    return repeats(args) * (2 if alternates(args) else 1)


def null_command(args) -> list[str]:
    """The null of the change: `sleep S` (--null-sleep) or the command with
    --dry-marks."""
    if args.null_sleep is not None:
        return ["sleep", f"{args.null_sleep:g}"]
    return command_of(args) + ["--dry-marks"]


def plans_of(args, plan: dict) -> list[dict]:
    """The runs of one repeat: the plan, and with `alternates` its null."""
    if not alternates(args):
        return [plan]
    null = {**plan, "kind": "null", "command": null_command(args),
            "expect_exit": 0 if args.null_sleep is not None else plan["expect_exit"],
            "label": args.name if args.null_sleep is not None else None}
    return [plan, null]


def expected_exit(args) -> int:
    """The exit status a run's command must have: --expect-exit for the
    change and its dry null (the gates run in both), 0 for a `sleep`."""
    return 0 if is_sleep(command_of(args)) else args.expect_exit


def runnable(program: str) -> bool:
    return shutil.which(program) is not None


def check(args) -> None:
    if not NAME.fullmatch(args.name):
        raise SystemExit("--name is a label: letters, digits, '.', '_' and '-'")
    if not command_of(args):
        raise SystemExit("give the change as -- COMMAND...")
    if not runnable(command_of(args)[0]):
        raise SystemExit(f"{command_of(args)[0]} is not an executable command (a script that "
                         "is not executable runs as `python3 SCRIPT ...`)")
    if args.seconds < MIN_SECONDS:
        raise SystemExit(f"--seconds {args.seconds}: the referee's rule needs {MIN_SECONDS} s "
                         "or more of probe (about 300 s of no-change windows around a change)")
    if args.at < MIN_AT:
        raise SystemExit(f"--at {args.at:g}: the probes start at least {MIN_AT} s before the change")
    if args.at + AFTER_END_S >= args.seconds:
        raise SystemExit("--at leaves no time for the change inside the probes")
    if args.dry and is_sleep(command_of(args)):
        raise SystemExit("--dry: `sleep` is already a null and has no --dry-marks")
    if args.null_sleep is not None and (args.dry or is_sleep(command_of(args))
                                        or args.null_sleep <= 0):
        raise SystemExit("--null-sleep S (S > 0) is the null of a change, not of --dry or a sleep")
    if args.null_sleep is not None and args.repeat is None:
        raise SystemExit("--null-sleep alternates nulls with the changes of --repeat N")
    if min(args.clients, repeats(args)) < 1 or min(args.load_listeners, args.readers) < 0:
        raise SystemExit("--clients and --repeat are 1 or more; --load-listeners and "
                         "--readers 0 or more")
    if args.no_up and args.set:
        raise SystemExit("--no-up keeps the running lab's settings: drop --set or --no-up")
    if args.client_loss:
        bench.client_loss_spec(args.client_loss)
    lanes_of(args.lanes)
    wan_losses(args.wan_loss)
    for host in hosts_of(args):
        try:
            lab.node(host)
        except lab.LabError as error:
            raise SystemExit(f"--hosts: {error}") from None


def hosts_of(args) -> list[str]:
    return [item.strip() for item in args.hosts.split(",") if item.strip()]


def scenario_of(args) -> dict:
    """What every run of one scenario shares, its null's included: the probes,
    their length, the change's place and the lab."""
    return {"lanes": lanes_of(args.lanes), "seconds": args.seconds, "at": args.at,
            "hosts": hosts_of(args), "clients": args.clients,
            "load_listeners": args.load_listeners, "readers": args.readers,
            "client_loss": args.client_loss or None, "wan_loss": wan_losses(args.wan_loss),
            "set": list(args.set), "expect_exit": args.expect_exit}


# ------------------------------------------------------------------ the lab

def lab_overrides(args) -> list[str]:
    """`up` settings: the probed renditions, the senders' epochs, then --set
    (which wins)."""
    lanes = [LANES[lane] for lane in lanes_of(args.lanes)]
    settings = [f"daw_hls_formats={','.join(item['payload'] for item in lanes)}"]
    for item in lanes:
        settings.append(f"{item['payload']}_epoch_samples={item['epoch_samples']}")
    return settings + list(args.set)


def sender_seconds(args, sites: int, start: int) -> int:
    """How long the senders may run: to START, the streams' wait to be live,
    then every repeat's probes, their grace, the command's stop and the
    scoring. They are stopped at the end of the run; the bound holds if the
    runner itself is killed."""
    live = LIVE_TIMEOUT_S * len(lanes_of(args.lanes)) * sites
    per_run = args.seconds + PROBE_GRACE_S + COMMAND_STOP_S + SCORE_MARGIN_S
    return math.ceil(max(0.0, start - now()) + live + PROBE_AFTER_LIVE_S
                     + runs_planned(args) * per_run)


def sender_place(choice: str = "auto") -> tuple[str, list[str]]:
    """The host the senders run on and their targets. With more than one
    contributor in service (the standby, A 2.7) a run's source is the
    source host, sending every epoch to each contributor as lon-src does on
    Linode (A 6): a contributor change then has a source beside it.
    Otherwise the contributor's own host sends to itself, as before."""
    contributors = [name for name in lab.service_names("contributor")
                    if lab.load("services.json").get(name)]
    if choice == lab.SOURCE_HOST or (choice == "auto" and len(contributors) > 1):
        return lab.SOURCE_HOST, lab.contributor_targets()
    return "contrib-lon", ["127.0.0.1:27100"]


def sender_commands(lanes: list[str], start: int, seconds: int,
                    targets: list[str] | None = None) -> list[list[str]]:
    """One AEP1 sender per lane, as the referee's (compare-audio.py
    aep1_command): it sends epoch k when its last sample has been spoken, so
    the session id is START plus one epoch; a second lane's id is one ns more
    (two sessions of one source; PCM is read by fingerprint). Each datagram
    goes to every one of `targets` (default the contributor's own port)."""
    loop = str(audio_media() / "audio.s24le")
    to = [item for target in (targets or ["127.0.0.1:27100"]) for item in ("--target", target)]
    out = []
    for offset, lane in enumerate(lanes):
        item = LANES[lane]
        session = start * 1_000_000_000 + bench.epoch_ns(item["epoch_samples"]) + offset
        out.append([lab.binary("aep1-48k-probe"), "send", *to,
                    "--session-id", str(session), "--group-id", "0",
                    # Stopped at the end of the run (and by the lab's `down`).
                    "--duration-seconds", str(seconds),
                    "--payload", item["payload"], "--epoch-samples", str(item["epoch_samples"]),
                    "--channels", "2", "--group-channels", "8", "--pcm-s24le-file", loop,
                    "--repair-percent", "12", "--min-repair-symbols", "1"])
    return out


def probe_items(args, name: str, directory: pathlib.Path) -> list[dict]:
    """The referee's nt-uc probe (compare-audio.py udp_client) on each host and
    lane: the launch time, then the client with --arrivals and --write-parts,
    its report on standard output."""
    items = []
    for host in hosts_of(args):
        site = lab.node(host)["site"]
        entry = lab.distributors(site)[0]
        for lane in lanes_of(args.lanes):
            for index in range(args.clients):
                key = f"{lane}@{host}" + (f"#{index}" if args.clients > 1 else "")
                stem = directory / f"{host}.{lane}.uc{index}"
                files = {suffix: str(stem) + "." + suffix for suffix in
                         ("launch", "arrivals.csv", "parts.bin", "uc.json", "uc.err",
                          "probe.json", "probe.csv", "ingest.log")}
                client = [str(bench.UDP_CLIENT), "--entry", lab.base_url(entry),
                          "--stream", str(LANES[lane]["stream"]), "--tls-ca", lab.ca_file(),
                          "--server-name", lab.SERVER_NAME,
                          "--duration-seconds", str(args.seconds),
                          "--session", f"change-{name}-{host}-{lane}"
                          + (f"-{index}" if args.clients > 1 else ""),
                          "--arrivals", files["arrivals.csv"], "--write-parts", files["parts.bin"]]
                script = (f"date +%s.%N >{shlex.quote(files['launch'])}; exec {shlex.join(client)} "
                          f">{shlex.quote(files['uc.json'])} 2>{shlex.quote(files['uc.err'])}")
                items.append({"key": key, "host": host, "site": site, "lane": lane,
                              "entry": entry, "files": files, "client": client,
                              "command": ["bash", "-c", script]})
    return items


def reader_items(args, name: str, directory: pathlib.Path) -> list[dict]:
    """--readers steered needletail-reader sessions on each host, the first
    lane's stream, entering through every distributor of the site."""
    if not args.readers:
        return []
    stream = LANES[lanes_of(args.lanes)[0]]["stream"]
    items = []
    for host in hosts_of(args):
        site = lab.node(host)["site"]
        command = [lab.binary("needletail-reader")]
        for entry in lab.distributors(site):
            command += ["--entry", lab.base_url(entry)]
        report = directory / f"{host}.reader.json"
        # --steering: the reader follows the region's Content Steering, as a
        # player does, and not only a draining edge's redirect.
        command += ["--steering", "--stream", str(stream),
                    "--session", f"change-{name}-{host}-reader",
                    "--readers", str(args.readers), "--duration-seconds", str(args.seconds),
                    "--tls-ca", lab.ca_file(), "--server-name", lab.SERVER_NAME,
                    "--report", str(report)]
        items.append({"host": host, "report": str(report), "log": str(report.with_suffix(".log")),
                      "command": command})
    return items


def crowd_items(args, name: str, directory: pathlib.Path) -> list[dict]:
    """--load-listeners an edge of each probed site, latency-bench's crowd on
    the first lane's stream. The listeners ask the site's entry points for
    their legs and spare, as a real listener does (latency-bench
    --load-assign), unless --crowd-edges: from the edges' list there is no
    spare, and an edge that is drained or dead is still on the list."""
    if not args.load_listeners:
        return []
    sites = sorted({lab.node(host)["site"] for host in hosts_of(args)})
    crowd = SimpleNamespace(load_listeners=args.load_listeners, load_ramp="",
                            load_sites=",".join(sites),
                            payload=LANES[lanes_of(args.lanes)[0]]["payload"],
                            seconds=args.seconds, name=f"change-{name}", load_args="",
                            load_assign=not getattr(args, "crowd_edges", False),
                            load_direct=getattr(args, "crowd_edges", False))
    return bench.load_commands(crowd, directory)


def ingest_command(item: dict, label: str) -> list[str]:
    """The referee's `ntcmp udp-ingest` of one probe, with its lane's index."""
    files = item["files"]
    launch = pathlib.Path(files["launch"]).read_text().strip()
    return [str(ntcmp()), "udp-ingest", "--arrivals", files["arrivals.csv"],
            "--parts", files["parts.bin"], "--started", launch,
            "--audio-index", str(audio_media() / f"{LANES[item['lane']]['index']}.index.json"),
            "--skip-seconds", str(SKIP_SECONDS), "--lane", UC_LANE, "--label", label,
            "--client", files["uc.json"], "--report", files["probe.json"],
            "--frames", files["probe.csv"]]


def score_command(items: list[dict], directory: pathlib.Path) -> list[str]:
    command = [sys.executable, str(change_window())]
    for item in items:
        command += ["--frames", item["files"]["probe.csv"]]
    return command + ["--marks", str(directory / "marks.txt"),
                      "--json", str(directory / "verdict.json")]


def layout() -> str:
    """The lab's CPU layout, as `up` was given it."""
    sets = lab.load("cpus.json")
    if "_range" in sets:
        return f"cpus={sets['_range'][0]}-{sets['_range'][1]}"
    if sets.get("_own"):
        # The own-CPU layout: which nodes it holds is part of it.
        return "own_cpus=1 leave_out=" + ",".join(sets.get("_left_out") or [])
    if "_first" in sets:
        return f"node_cpus={sets['_first']}"
    return "host"


def nodes_status() -> dict:
    """/api/node of every running node of the lab."""
    return {name: lab.node_status(name) for name in lab.service_names()
            if lab.service_pid(name)}


# ------------------------------------------------------------------ marks

def marks_of(text: str) -> list[tuple[str, int, str]]:
    """The mark lines of a command's standard output: (kind, unix ns, label)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not MARK.fullmatch(line):
            continue
        kind, stamp, label = line.split()
        seconds, _, fraction = stamp.partition(".")
        out.append((kind, int(seconds) * 1_000_000_000 + int(fraction.ljust(9, "0")[:9]), label))
    return out


def close_marks(marks: list[tuple[str, int, str]], started_ns: int, ended_ns: int,
                label: str) -> tuple[list[tuple[str, int, str]], list[dict]]:
    """The marks file of a run: the command's own marks, or `start`/`end`
    around the command labelled `label` when it printed none; a label left
    open (the command died) is closed when the command was reaped, with an
    `aborted` event (CW scores nothing while one label is open)."""
    if not marks:
        return [("start", started_ns, label), ("end", ended_ns, label)], []
    opened = {label for kind, _t, label in marks if kind == "start"}
    closed = {label for kind, _t, label in marks if kind == "end"}
    out, events = list(marks), []
    for name in sorted(opened - closed):
        out.append(("end", ended_ns, name))
        events.append({"event": "aborted", "label": name, "unix_ns": ended_ns})
    return out, events


def marks_text(marks: list[tuple[str, int, str]]) -> str:
    return "".join(f"{kind} {unix_ns_text(t)} {label}\n" for kind, t, label in marks)


def probe_too_short(marks: list[tuple[str, int, str]], probes_end_ns: int) -> str | None:
    """Why the probes do not cover the change, or None: CW needs units on
    both sides of the window, which ends 5 s after the last end, and reads
    silence 1 s past it."""
    ends = [t for kind, t, _label in marks if kind == "end"]
    if not ends:
        return None
    late_ns = max(ends) + AFTER_END_S * 1_000_000_000 - probes_end_ns
    if late_ns > 0:
        return (f"probe too short: the last end + {AFTER_END_S} s is {late_ns / 1e9:.1f} s past "
                "the probes' end; run it again with a longer --seconds")
    return None


# ------------------------------------------------------------------ outside interference

def read_cpu_times(cpus: list[int], text: str | None = None) -> dict[int, tuple[int, int]]:
    """{cpu: (user + nice + system, steal)} in clock ticks, from /proc/stat."""
    text = pathlib.Path("/proc/stat").read_text() if text is None else text
    wanted = set(cpus)
    out = {}
    for line in text.splitlines():
        words = line.split()
        if not words or not words[0].startswith("cpu") or words[0] == "cpu":
            continue
        cpu = int(words[0][3:])
        if cpu in wanted:
            values = [int(item) for item in words[1:]]
            out[cpu] = (values[0] + values[1] + values[2], values[7] if len(values) > 7 else 0)
    return out


def scope_delta_usec(samples: list[dict]) -> int:
    """CPU time of the lab's scopes over the samples. A scope's counter
    starts again when its service restarts (it goes down, or a scope appears
    that was not there): the new count is time spent since."""
    total = 0
    for before, after in zip(samples, samples[1:]):
        for name, value in after["scopes"].items():
            last = before["scopes"].get(name)
            total += value - last if last is not None and value >= last else value
    return total


def outside_share(samples: list[dict], ticks_per_second: int) -> dict:
    """The share of the lab's CPUs that something other than the lab used
    over the samples, and their steal, in percent of the CPUs' time."""
    if len(samples) < 2:
        return {"error": "fewer than two samples"}
    first, last = samples[0], samples[-1]
    cpus = sorted(set(first["cpu"]) & set(last["cpu"]))
    elapsed_us = (last["t"] - first["t"]) * 1e6
    capacity_us = elapsed_us * len(cpus)
    if capacity_us <= 0:
        return {"error": "no time between the samples"}
    tick_us = 1e6 / ticks_per_second
    busy_us = sum(last["cpu"][cpu][0] - first["cpu"][cpu][0] for cpu in cpus) * tick_us
    steal_us = sum(last["cpu"][cpu][1] - first["cpu"][cpu][1] for cpu in cpus) * tick_us
    lab_us = scope_delta_usec(samples)
    return {"seconds": round(elapsed_us / 1e6, 1), "cpus": len(cpus),
            "busy_pct": round(100 * busy_us / capacity_us, 2),
            "lab_pct": round(100 * lab_us / capacity_us, 2),
            "outside_share_pct": round(100 * max(0.0, busy_us - lab_us) / capacity_us, 2),
            "steal_pct": round(100 * steal_us / capacity_us, 2)}


class Interference:
    """Samples the lab's CPUs and scopes once a second while the probes run."""

    def __init__(self, cpus: list[int]):
        self.cpus = cpus
        self.samples: list[dict] = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def sample(self) -> None:
        self.samples.append({"t": time.monotonic(), "cpu": read_cpu_times(self.cpus),
                             "scopes": lab.scope_cpu_usec()})

    def loop(self) -> None:
        while not self.stopped.wait(SAMPLE_INTERVAL_S):
            self.sample()

    def start(self) -> "Interference":
        self.sample()
        self.thread.start()
        return self

    def stop(self) -> dict:
        self.stopped.set()
        self.thread.join()
        self.sample()
        return outside_share(self.samples, os.sysconf("SC_CLK_TCK"))


def floor() -> dict | None:
    """The accounting floor of lab-parity.py's layout Q1, {outside_share_pct,
    layout}, or None."""
    try:
        record = json.loads(FLOOR_FILE.read_text())
        return {"outside_share_pct": float(record["outside_share_pct"]),
                "layout": str(record["layout"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def floor_for(where: str) -> tuple[float | None, str | None]:
    """The floor that applies to a run in layout `where`, or None and why: a
    floor is the outside share of one CPU range, and means nothing for
    another (node_cpus=0 spreads a lab over the whole host)."""
    record = floor()
    if record is None:
        return None, "no floor measured (lab-parity.py): no run is void"
    if record["layout"] != where:
        return None, (f"no floor for {where} (it was measured on {record['layout']}): "
                      "no run is void")
    return record["outside_share_pct"], None


def void_reason(interference: dict, floor_pct: float | None) -> str | None:
    share = interference.get("outside_share_pct")
    if floor_pct is None or share is None or share <= floor_pct + VOID_POINTS:
        return None
    return (f"void: the outside share was {share:.2f}% of the lab's CPUs, more than "
            f"{VOID_POINTS:g} points above the floor {floor_pct:.2f}%")


# ------------------------------------------------------------------ scoring

def verdict_of(verdict: dict | None, items: list[dict]) -> dict:
    """CW's verdict per label and probe, with the figures that the null is
    judged beside: {label: {probe key: {...}}}."""
    keys = {item["files"]["probe.csv"]: item["key"] for item in items}
    out = {}
    for label, per in (verdict or {}).items():
        out[label] = {}
        for path, result in per.items():
            change = result.get("change") or {}
            out[label][keys.get(path, path)] = {
                "pass": result.get("pass"), "missing": change.get("missing"),
                "gap_ms": change.get("gap_ms"), "gap_bar_ms": result.get("gap_bar_ms"),
                "p99_ms": change.get("p99_ms"), "p99_bar_ms": result.get("p99_bar_ms"),
                "null_fail_rate": result.get("null_fail_rate"),
                "null_windows": result.get("null_windows"), "window_s": result.get("window_s"),
                **{key: result[key] for key in ("note", "error") if key in result}}
    return out


def status_of(labels: dict, reasons: list[str], cw_exit: int | None) -> str:
    """void (no evidence), fail, unscored (CW's null too short), invalid (a
    probe's own report contradicts what CW scored, and nothing else
    failed) or pass. A probe that CW could not score at all (`error`: no
    audio, or the probe does not cover the window) is a failure, not a short
    null, unless the probe itself was stuck (`invalid`)."""
    if any(reason.startswith("void") for reason in reasons):
        return "void"
    results = [result for per in labels.values() for result in per.values()]
    valid = [result for result in results if not result.get("invalid")]
    if (reasons or cw_exit is None or not results
            or any(r["pass"] is False or "error" in r for r in valid)):
        return "fail"
    if len(valid) < len(results):
        return "invalid"
    if any(r["pass"] is not True for r in results):
        return "unscored"
    return "pass" if cw_exit == 0 else "fail"


def unit_ms(lane: str) -> float:
    return LANES[lane]["epoch_samples"] / 48.0


def read_report(item: dict) -> dict | None:
    try:
        report = json.loads(pathlib.Path(item["files"]["uc.json"]).read_text())
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def probe_problems(item: dict, report: dict | None) -> list[str]:
    """What makes a probe's whole run no evidence, from its own report:
    none, stuck (datagrams arrive and parts are not taken: F2), too few
    parts for its length, or its own missing count out of reason (part
    numbers that jumped)."""
    if report is None:
        return ["the probe wrote no report (uc.json)"]
    per_s = 1000.0 / unit_ms(item["lane"])
    duration = report.get("duration_seconds")
    received, datagrams = report.get("received_parts"), report.get("datagrams")
    out = []
    if isinstance(datagrams, (int, float)) and isinstance(received, (int, float)) and \
            datagrams > STUCK_DATAGRAMS_PER_PART * max(1, received):
        out.append(f"stuck: {datagrams} datagrams for {received} accepted parts")
    elif isinstance(received, (int, float)) and isinstance(duration, (int, float)) and \
            received < MIN_RECEIVED_SHARE * duration * per_s:
        out.append(f"{received} parts accepted in {duration:g} s, "
                   f"{duration * per_s:.0f} expected")
    missing = report.get("missing_parts")
    if isinstance(missing, (int, float)) and isinstance(duration, (int, float)) and \
            missing > max(MAX_OWN_MISSING, MAX_OWN_MISSING_SHARE * duration * per_s):
        out.append(f"its own report counts {missing:g} missing parts")
    return out


def window_units(item: dict, span: tuple[int, int]) -> int | None:
    """Units of the probe's frames (ntcmp's probe.csv) inside a label's
    window as CW widens it: 2 s before its start to 5 s after its end."""
    low = span[0] / 1000 - BEFORE_START_S * 1_000_000
    high = span[1] / 1000 + (AFTER_END_S - 1) * 1_000_000
    try:
        with open(item["files"]["probe.csv"], newline="") as handle:
            rows = csv.DictReader(handle)
            return sum(1 for row in rows if row.get("complete_unix_us")
                       and low <= float(row["complete_unix_us"]) <= high)
    except (OSError, ValueError):
        return None


def window_problems(item: dict, report: dict | None, span: tuple[int, int] | None) -> list[str]:
    """What makes a probe's pass of one label no evidence: too few units in
    the window, or fewer legs carrying datagrams than it asked for."""
    out = []
    if span is not None:
        length_s = (span[1] - span[0]) / 1e9 + BEFORE_START_S + AFTER_END_S - 1
        expected = length_s * 1000.0 / unit_ms(item["lane"])
        units = window_units(item, span)
        if units is None:
            out.append("its frames (probe.csv) could not be read")
        elif units < MIN_WINDOW_UNITS_SHARE * expected:
            out.append(f"{units} units inside the window, {expected:.0f} expected")
    paths = (report or {}).get("paths")
    legs = (report or {}).get("legs")
    if isinstance(paths, int) and isinstance(legs, dict):
        carried = sum(1 for leg in legs.values()
                      if isinstance(leg, dict) and (leg.get("datagrams") or 0) > 0)
        if carried < paths:
            out.append(f"{carried} of its {paths} legs carried datagrams")
    return out


def judge_probes(labels: dict, items: list[dict], marks) -> dict:
    """Each probe's own report against CW's verdict (local proof 2): a
    probe whose whole run is no evidence is `invalid` whatever CW said; one
    whose window is no evidence has CW's pass refused as `invalid`.
    {probe key: [reasons]} of the probes found invalid; `labels` is marked
    in place."""
    spans = label_spans(marks)
    by_key = {item["key"]: item for item in items}
    found: dict[str, list[str]] = {}
    for label, per in labels.items():
        for key, result in per.items():
            item = by_key.get(key)
            if item is None:
                continue
            report = read_report(item)
            whole = probe_problems(item, report)
            window = window_problems(item, report, spans.get(label))
            reasons = whole + (window if result.get("pass") is True else [])
            if reasons:
                result["invalid"] = reasons
                found.setdefault(key, [])
                found[key] += [reason for reason in reasons if reason not in found[key]]
    return found


def label_spans(marks: list[tuple[str, int, str]]) -> dict[str, tuple[int, int]]:
    """{label: (earliest start, latest end)} in unix ns, as CW reads them."""
    spans: dict[str, list] = {}
    for kind, t, label in marks:
        span = spans.setdefault(label, [None, None])
        if kind == "start":
            span[0] = t if span[0] is None else min(span[0], t)
        else:
            span[1] = t if span[1] is None else max(span[1], t)
    return {label: (s, e) for label, (s, e) in spans.items() if s is not None and e is not None}


def seconds_needed(span: tuple[int, int], launch_ns: int) -> int:
    """The --seconds that leaves CW its 120 no-change windows after a change
    of this span (its windows are the change's length + 7 s, clear of the
    change and its 1 s tail), counting none before it, with 10 s spare."""
    length_s = (span[1] - span[0]) / 1e9
    end_s = (span[1] - launch_ns) / 1e9
    return 10 * math.ceil((end_s + length_s + 14 + MIN_NULL_WINDOWS + 10) / 10)


def null_fail_rate(result: dict) -> tuple[float | None, str | None]:
    """CW's null_fail_rate of one label and probe, and None with the reason
    when CW could not measure it. CW scores each no-change window against
    the others more than the window's length + 1 s away and skips a window
    with fewer than MIN_NULL_WINDOWS of them, yet divides by every window:
    unless each window can have that many, the rate is low by
    construction (0.0 once no window has them)."""
    rate, windows, length = (result.get("null_fail_rate"), result.get("null_windows"),
                             result.get("window_s"))
    if rate is None or windows is None or length is None:
        return None, "change-window.py gave no null_fail_rate"
    needed = MIN_NULL_WINDOWS + 1 + 2 * math.floor(length + 1)
    if windows < needed:
        return None, (f"not measured: {windows} no-change windows of {length:g} s, and each is "
                      f"scored only against those more than {length + 1:g} s away; "
                      f"{needed} are needed")
    return rate, None


def null_hints(labels: dict, marks: list[tuple[str, int, str]],
               launch_ns: int | None) -> list[str]:
    """For a label CW did not score (null too short): the --seconds it needs."""
    spans = label_spans(marks)
    out = []
    for label, per in labels.items():
        short = any(str(result.get("note", "")).startswith("null too short")
                    for result in per.values())
        if short and label in spans and launch_ns is not None:
            span = spans[label]
            out.append(f"{label}: the change took {(span[1] - span[0]) / 1e9:.0f} s, too long "
                       f"for CW's {MIN_NULL_WINDOWS} no-change windows: give --seconds "
                       f"{seconds_needed(span, launch_ns)} or more, under a new --name (the "
                       "length is part of the scenario)")
    return out


def score(directory: pathlib.Path) -> dict:
    """Ingest the probes of a run with ntcmp and score its marks with CW;
    the result is written into run.json. A run that did not finish (it was
    interrupted, or the runner died) stays `interrupted` and is not scored."""
    run = json.loads((directory / "run.json").read_text())
    if run.get("status") == "interrupted" or not run.get("complete"):
        run.update({"status": "interrupted",
                    "reasons": run.get("reasons") or ["the run did not finish: not scored"]})
        (directory / "run.json").write_text(json.dumps(run, indent=1) + "\n")
        return run
    items = run["probes"]
    reasons = list(run["run_reasons"])
    expected = run["expect_exit"]
    if run["command_exit"] != expected:
        reasons.append(f"the command exited {run['command_exit']}; the scenario expects "
                       f"{expected} (--expect-exit)")
    for item in items:
        for name in ("probe.csv", "probe.json"):
            pathlib.Path(item["files"][name]).unlink(missing_ok=True)
        if not pathlib.Path(item["files"]["launch"]).is_file():
            reasons.append(f"{item['key']}: the probe did not start")
            continue
        with open(item["files"]["ingest.log"], "wb") as handle:
            done = subprocess.run(ingest_command(item, f"{run['name']}-{item['key']}"),
                                  stdout=handle, stderr=subprocess.STDOUT)
        if done.returncode != 0:
            reasons.append(f"{item['key']}: ntcmp udp-ingest exited {done.returncode}")
    (directory / "verdict.json").unlink(missing_ok=True)
    with open(directory / "cw.log", "wb") as handle:
        cw = subprocess.run(score_command(items, directory), stdout=handle,
                            stderr=subprocess.STDOUT)
    try:
        verdict = json.loads((directory / "verdict.json").read_text())
    except (OSError, ValueError):
        verdict = None
        reasons.append(f"change-window.py exited {cw.returncode} with no verdict: "
                       + (directory / "cw.log").read_text(errors="replace")[-300:].strip())
    floor_pct, floor_note = floor_for(run["layout"])
    void = void_reason(run.get("interference") or {}, floor_pct)
    if void:
        reasons.append(void)
    labels = verdict_of(verdict, items)
    launches = [float(pathlib.Path(item["files"]["launch"]).read_text()) for item in items
                if pathlib.Path(item["files"]["launch"]).is_file()]
    marks = marks_of((directory / "marks.txt").read_text())
    invalid = judge_probes(labels, items, marks)
    run.update({"labels": labels, "cw_exit": cw.returncode, "floor_pct": floor_pct,
                "invalid_probes": invalid,
                "floor_note": floor_note, "reasons": reasons,
                "hints": null_hints(labels, marks,
                                    int(min(launches) * 1e9) if launches else None),
                "status": status_of(labels, reasons, cw.returncode if verdict else None)})
    (directory / "run.json").write_text(json.dumps(run, indent=1) + "\n")
    return run


# ------------------------------------------------------------------ the scenario's record

def runs_of(scenario: pathlib.Path) -> list[dict]:
    out = []
    for path in sorted(scenario.glob("*/run.json")):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return out


def check_scenario(scenario: pathlib.Path, plan: dict) -> None:
    """A name holds one scenario: its runs share the probes, the change's
    place and the expected exit, and its changes and dry nulls one command."""
    for run in runs_of(scenario):
        if run["scenario"] != plan["scenario"]:
            raise SystemExit(f"{scenario} holds runs of another set-up "
                             f"({run['directory']}): give another --name")
        if (None not in (run.get("base_command"), plan["base_command"])
                and run["base_command"] != plan["base_command"]):
            raise SystemExit(f"{scenario} holds runs of `{shlex.join(run['base_command'])}`: "
                             "give another --name")


def next_directory(scenario: pathlib.Path, kind: str) -> pathlib.Path:
    count = len(list(scenario.glob(f"{kind}-*")))
    return scenario / f"{kind}-{count + 1:02d}"


def tally(runs: list[dict]) -> dict:
    """Runs of each kind in each layout, by status. A run.json with no
    status (its runner was killed) is an interrupted run."""
    out: dict[str, dict] = {}
    for run in runs:
        counts = out.setdefault(run["layout"], {kind: dict.fromkeys(STATUSES, 0)
                                                for kind in ("change", "null")})
        counts[run["kind"]][run.get("status") or "interrupted"] += 1
    return out


def scored(counts: dict) -> int:
    return sum(counts[status] for status in SCORED)


def count_text(kind: str, counts: dict) -> str:
    """"change 9 of 10 pass (1 void)": passes of the scored runs, and the runs
    that were not scored, which count neither way."""
    rest = [f"{counts[status]} {status}" for status in STATUSES
            if status not in SCORED and counts[status]]
    return (f"{kind} {counts['pass']} of {scored(counts)} pass"
            + (f" (not scored: {', '.join(rest)})" if rest else ""))


def summary(scenario: pathlib.Path) -> tuple[dict, list[str]]:
    """Each change beside its null: passes of the scored runs of both kinds
    in each layout, and whether the scenario is proved locally: 10 or more
    scored runs of each kind, every one a pass (summary.json keeps every
    run's figures)."""
    runs = runs_of(scenario)
    tallies = tally(runs)
    lines = []
    proved = {}
    for where, counts in sorted(tallies.items()):
        change, null = counts["change"], counts["null"]
        proved[where] = all(scored(item) >= PROVED_RUNS and item["fail"] == 0
                            for item in (change, null))
        lines.append(f"{scenario.name} ({where}): {count_text('change', change)}; "
                     + (count_text("null", null) if any(null.values())
                        else "null not run (--dry, or -- sleep S for a plain kind)")
                     + ("; proved locally" if proved[where] else ""))
        if null["fail"]:
            lines.append(f"  WARNING: the null failed {null['fail']} of {scored(null)}: the lab "
                         "or the statistic is at fault; report it to the referee with the "
                         "figures below before any change is claimed")
    record = {"scenario": scenario.name, "tally": tallies, "proved": proved,
              "runs": [{key: run.get(key) for key in ("directory", "kind", "layout", "status",
                                                      "reasons", "command_exit", "labels",
                                                      "interference")}
                       for run in runs]}
    (scenario / "summary.json").write_text(json.dumps(record, indent=1) + "\n")
    return record, lines


def side_by_side(name: str, runs: list[dict]) -> str:
    """The 10-of-10 rule's two rates, beside CW's null_fail_rate of every
    probe and label of the same runs (median and range)."""
    def rate(kind: str) -> str:
        mine = [run for run in runs if run["kind"] == kind]
        scored_runs = [run for run in mine if run["status"] in SCORED]
        passed = sum(1 for run in scored_runs if run["status"] == "pass")
        rest = len(mine) - len(scored_runs)
        return f"{kind} {passed} of {len(scored_runs)}" + (f" (+{rest} not scored)" if rest else "")
    rates = sorted(result["null_fail_rate"] for run in runs
                   for per in (run.get("labels") or {}).values() for result in per.values()
                   if isinstance(result.get("null_fail_rate"), (int, float)))
    cw = "no null_fail_rate" if not rates else (
        f"CW null_fail_rate median {rates[len(rates) // 2]:g} ({rates[0]:g} to {rates[-1]:g}, "
        f"{len(rates)} probe-labels)")
    return f"{name}: {rate('change')} | {rate('null')} | {cw}"


def run_lines(run: dict) -> list[str]:
    share = (run.get("interference") or {}).get("outside_share_pct")
    lines = [f"  {pathlib.Path(run['directory']).name}: {run['status']}"
             f" (command exit {run.get('command_exit')}, outside share "
             f"{'-' if share is None else f'{share:.2f}%'})"]
    lines += [f"    {reason}" for reason in run.get("reasons") or []]
    lines += [f"    {hint}" for hint in run.get("hints") or []]

    def ms(value) -> str:
        return "-" if value is None else f"{value:g}"
    for label, per in (run.get("labels") or {}).items():
        for key, result in per.items():
            verdict = {True: "pass", False: "FAIL", None: "unscored"}[result["pass"]]
            if "error" in result:
                verdict = "FAIL"
            if result.get("invalid"):
                verdict = f"probe invalid ({'; '.join(result['invalid'])}), CW said {verdict}"
            lines.append(f"    {label} {key}: {verdict} missing {ms(result['missing'])} "
                         f"gap {ms(result['gap_ms'])}/{ms(result['gap_bar_ms'])} ms "
                         f"p99 {ms(result['p99_ms'])}/{ms(result['p99_bar_ms'])} ms "
                         f"null fail rate {ms(result['null_fail_rate'])}"
                         + (f" ({result.get('note') or result.get('error')})"
                            if result.get("note") or result.get("error") else ""))
    return lines


# ------------------------------------------------------------------ stopping cleanly

def install_stops() -> dict:
    """SIGINT, SIGTERM and SIGHUP unwind the run (KeyboardInterrupt or
    Stopped), once: a second one while it cleans up is only logged, so the
    clean-up finishes (SIGKILL still stops the runner at once). Returns the
    handlers to put back; none off the main thread, where Python takes no
    handler."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, *STOP_SIGNALS)}

    def later(signum, _frame) -> None:
        log(f"{signal.Signals(signum).name}: the run is cleaning up (SIGKILL stops it now and "
            "leaves the lab, its shaping and its senders as they are)")

    def first(signum, _frame) -> None:
        for item in previous:
            signal.signal(item, later)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise Stopped(signum)

    for signum in previous:
        signal.signal(signum, first)
    return previous


def restore_stops(previous: dict) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def attempt(failures: list[str], what: str, action, *args) -> object:
    """One step of a clean-up: a failure is recorded and the next step runs."""
    try:
        return action(*args)
    except Exception as error:  # noqa: BLE001 - every step runs whatever the last did
        failures.append(f"{what}: {error}")
        log(f"clean-up: {what} failed: {error}")
        return None


# ------------------------------------------------------------------ a run

def cpu_roles() -> dict[str, list[int]]:
    """Where this runner's own processes run (F3 of local proof 2: the
    tools, the sender and the probes shared the lab's "_rest" and the
    tools' polling moved the probes): edge_lab.rest_split of the lab's
    layout, {} without one."""
    return lab.rest_split(lab.load("cpus.json"))


def pinned(role: str) -> list[str]:
    cpus = cpu_roles().get(role)
    return ["taskset", "-c", lab.cpu_list(cpus)] if cpus else []


def popen(host: str | None, command: list[str], stdout, stderr,
          source: bool = False, role: str | None = None) -> subprocess.Popen:
    """`command` in the namespace of `host` under the lab's prefix, and on
    the CPUs of `role` (probes, tools, sender) when the layout has them."""
    prefix = lab.enter(host, source=True) if source else lab.enter(host)
    prefix += pinned(role) if role else []
    return subprocess.Popen(prefix + command, stdout=stdout, stderr=stderr,
                            stdin=subprocess.DEVNULL, start_new_session=True)


def command_process(command: list[str], nice: int, stdout, stderr) -> subprocess.Popen:
    """The change's command: on the tools' CPUs (and every lab command it
    starts there too, edge_lab.TOOL_CPUS_ENV), at nice `nice`."""
    tools = cpu_roles().get("tools")
    env = dict(os.environ)
    prefix = []
    if tools:
        env[lab.TOOL_CPUS_ENV] = lab.cpu_list(tools)
        prefix += ["taskset", "-c", lab.cpu_list(tools)]
    if nice > 0:
        prefix += ["nice", "-n", str(nice)]
    return subprocess.Popen(prefix + command, stdout=stdout, stderr=stderr,
                            stdin=subprocess.DEVNULL, start_new_session=True, env=env)


def stop_command(process: subprocess.Popen) -> int | None:
    """A stopped run lets the command close what it opened (the engine
    resumes a drained node and prints its end), then kills it. Its exit
    status, or None if it could not be reaped."""
    if process.poll() is not None:
        return process.returncode
    try:
        os.killpg(process.pid, signal.SIGINT)
        return process.wait(timeout=COMMAND_STOP_S)
    except (OSError, subprocess.TimeoutExpired):
        bench.os_kill_group(process.pid)
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return None


def write_marks(directory: pathlib.Path, started_ns: int, reaped_ns: int,
                label: str) -> tuple[list, list]:
    """marks.txt from the command's output, its open labels closed at the
    time it was reaped."""
    marks, events = close_marks(
        marks_of((directory / "command.out").read_text(errors="replace")),
        started_ns, reaped_ns, label)
    (directory / "marks.txt").write_text(marks_text(marks))
    return marks, events


def run_once(args, plan: dict, directory: pathlib.Path) -> dict:
    """One pass of the scenario: probes, the change, ingest and score. An
    interrupt stops the command, the probes and the listeners, keeps the
    marks and leaves the run `interrupted`."""
    directory.mkdir(parents=True)
    probes = probe_items(args, args.name, directory)
    readers = reader_items(args, args.name, directory)
    crowds = crowd_items(args, args.name, directory)
    run = {**plan, "directory": str(directory), "layout": layout(), "probes": probes,
           "readers": readers, "crowds": crowds, "run_reasons": [], "events": [],
           "cpu_roles": cpu_roles(), "nice": getattr(args, "nice", 0)}
    (directory / "run.json").write_text(json.dumps(run, indent=1) + "\n")
    (directory / "nodes-before.json").write_text(json.dumps(nodes_status(), indent=1) + "\n")
    handles, processes = [], []
    command = {"process": None, "started_ns": None, "marks": None}
    interference = Interference(lab.lab_cpus(lab.load("cpus.json"))).start()
    try:
        try:
            for item in crowds:
                pathlib.Path(item["out"]).mkdir(parents=True, exist_ok=True)
                handle = open(pathlib.Path(item["out"]) / "load.log", "ab")
                handles.append(handle)
                processes.append(popen(item["host"], item["command"], handle, handle,
                                       role="tools"))
            for item in readers:
                handle = open(item["log"], "ab")
                handles.append(handle)
                processes.append(popen(item["host"], item["command"], handle, handle,
                                       role="tools"))
            # Not before every probed stream has been live 10 s: a listener
            # that joins at a session's start can get stuck (F2).
            live = plan.get("live_unix")
            if live is not None and now() < live + PROBE_AFTER_LIVE_S:
                sleep(live + PROBE_AFTER_LIVE_S - now())
            started = now()
            for item in probes:
                processes.append(popen(item["host"], item["command"], subprocess.DEVNULL,
                                       subprocess.DEVNULL, role="probes"))
            log(f"{args.name}: {len(probes)} probes for {args.seconds} s; the change at "
                f"{args.at:g} s")
            sleep(max(0.0, started + args.at - now()))
            out_handle = open(directory / "command.out", "wb")
            err_handle = open(directory / "command.err", "wb")
            handles += [out_handle, err_handle]
            log(f"{args.name}: {shlex.join(plan['command'])}")
            command["started_ns"] = now_ns()
            command["process"] = command_process(plan["command"], getattr(args, "nice", 0),
                                                 out_handle, err_handle)
            try:
                run["command_exit"] = command["process"].wait(
                    timeout=max(0.0, started + args.seconds - now()))
            except subprocess.TimeoutExpired:
                run["command_exit"] = stop_command(command["process"])
                run["run_reasons"].append("the command was still running when the probes "
                                          "ended: it was stopped (SIGINT, then killed)")
            command["marks"], events = write_marks(directory, command["started_ns"], now_ns(),
                                                   plan.get("label") or args.name)
            run["events"] += events
            for process in processes:
                try:
                    process.wait(timeout=max(1.0, started + args.seconds + PROBE_GRACE_S - now()))
                except subprocess.TimeoutExpired:
                    bench.os_kill_group(process.pid)
                    run["run_reasons"].append(f"a probe or listener (pid {process.pid}) outlived "
                                              "its duration and was killed")
        finally:
            failures: list[str] = []
            if command["process"] is not None and command["marks"] is None:
                run["command_exit"] = attempt(failures, "stop the command", stop_command,
                                              command["process"])
                marked = attempt(failures, "close the marks", write_marks, directory,
                                 command["started_ns"], now_ns(), args.name)
                if marked:
                    command["marks"] = marked[0]
                    run["events"] += marked[1]
            for process in processes:
                if process.poll() is None:
                    attempt(failures, f"kill pid {process.pid}", bench.os_kill_group, process.pid)
            for handle in handles:
                attempt(failures, f"close {handle.name}", handle.close)
            run["interference"] = attempt(failures, "read the outside share",
                                          interference.stop) or {}
            run["run_reasons"] += [f"clean-up: {item}" for item in failures]
    except BaseException as error:
        run.update({"status": "interrupted",
                    "reasons": [f"interrupted ({type(error).__name__}"
                                + (f": {error}" if str(error) else "") + ")"]})
        (directory / "run.json").write_text(json.dumps(run, indent=1) + "\n")
        raise
    launches = [float(pathlib.Path(item["files"]["launch"]).read_text())
                for item in probes if pathlib.Path(item["files"]["launch"]).is_file()]
    if launches:
        short = probe_too_short(command["marks"], int((min(launches) + args.seconds) * 1e9))
        if short:
            run["run_reasons"].append(short)
    (directory / "nodes-after.json").write_text(json.dumps(nodes_status(), indent=1) + "\n")
    run["crowd_report"] = bench.load_report(crowds)
    run["complete"] = True
    (directory / "run.json").write_text(json.dumps(run, indent=1) + "\n")
    return score(directory)


def start_senders(args, lanes: list[str], directory: pathlib.Path) -> tuple[list, int]:
    """The senders of the lanes, from the contributor's host, recorded in the
    lab's sources so that its `down` stops them, and bounded in time."""
    start = bench.loop_start(now())
    sites = {lab.node(host)["site"] for host in hosts_of(args)}
    senders = []
    sources = lab.load("sources.json")
    host, targets = sender_place(getattr(args, "sender_host", "auto"))
    log(f"senders on {host} to {', '.join(targets)}")
    for lane, command in zip(lanes, sender_commands(lanes, start,
                                                    sender_seconds(args, len(sites), start),
                                                    targets)):
        handle = open(directory / f"sender-{lane}.log", "ab")
        handle.write((shlex.join([f"# on {host}:", *command]) + "\n").encode())
        handle.flush()
        process = popen(host, command, handle, handle, source=True, role="sender")
        handle.close()
        senders.append(process)
        sources[f"change-run-{lane}"] = process.pid
    lab.save("sources.json", sources)
    return senders, start


def stop_senders(lanes: list[str], senders: list) -> None:
    for process in senders:
        bench.stop_sender(process)
    sources = lab.load("sources.json")
    for lane in lanes:
        sources.pop(f"change-run-{lane}", None)
    lab.save("sources.json", sources)


def run(args) -> int:
    check(args)
    for tool in (ntcmp(), change_window(), bench.UDP_CLIENT):
        if not pathlib.Path(tool).is_file():
            raise SystemExit(f"{tool} is missing")
    if args.load_listeners and not bench.LOAD_BIN.is_file():
        raise SystemExit(f"{bench.LOAD_BIN} is missing: build av-mesh's needletail-load or give "
                         "--load-listeners 0")
    lanes = lanes_of(args.lanes)
    kind = kind_of(args)
    plan = {"name": args.name, "kind": kind, "scenario": scenario_of(args),
            "command": change_command(args), "expect_exit": expected_exit(args),
            "base_command": None if is_sleep(command_of(args)) else command_of(args),
            "lab_id": lab.LAB_ID, "lab_up": None if args.no_up else lab_overrides(args)}
    scenario = out_root() / args.name
    check_scenario(scenario, plan)
    scenario.mkdir(parents=True, exist_ok=True)
    if args.no_up and not lab.load("services.json"):
        raise SystemExit("--no-up: the lab is not running")
    # C 2.12 (CL2): the binaries a run takes are frozen, and the run holds
    # its lab (a second run on it waits). With --no-up, what the running
    # lab runs is checked once this run holds it.
    guard.require_frozen(run_binaries(args))
    with guard.hold(f"change-run.py run --name {args.name}"):
        if args.no_up:
            guard.require_frozen({}, running=guard.running_binaries())
        with ChangeRecord(args, plan, scenario) as record:
            return run_held(args, plan, lanes, kind, scenario, record)


class ChangeRecord(archived_run.Run):
    """An invocation of `run` in the run archive (see the module's text)."""

    def __init__(self, args, plan: dict, scenario: pathlib.Path):
        topology, regions = archived_run.local_topology(lab.NODES)
        super().__init__(args.run_id or archived_run.default_run_id("change-run"), "local-lab",
                         "change-run",
                         code=archived_run.code_paths(lab.MESH_ROOT, lab.CONTRIB_ROOT),
                         settings={"plan": plan, "arguments": {key: value for key, value in
                                                              vars(args).items()
                                                              if key != "run_id"}},
                         topology=topology, regions=regions)
        self.name = args.name
        self.scenario = scenario
        self.began_s = time.time()
        self.logs = archived_run.LogSlices(lab.WORK / "logs")
        self.logs.mark()
        self.senders = archived_run.LogSlices(scenario, "sender-*.log")
        self.senders.mark()
        self.listeners = {"probes": len(hosts_of(args)) * len(lanes_of(args.lanes)) * args.clients,
                          "crowd": args.load_listeners, "readers": args.readers}
        self.at_close(self.close_copies)

    def run_directory(self, directory: pathlib.Path) -> None:
        """One run's directory as it ends, with what the nodes' logs
        gained in it, and its probes as a listener step."""
        self.logs.take(self, f"{self.name}/{directory.name}/node-logs")
        self.add_tree(directory, f"{self.name}/{directory.name}", "raw",
                      f"change-run {self.name} {directory.name}: run.json, the probes' CSV and "
                      "JSON, launch times, marks.txt, command.out/err, verdict.json (the "
                      "referee's change-window.py), cw.log, the crowd and readers, the nodes "
                      "before and after", {"latency_us": "us", "complete_unix_us": "us"})
        self.record_step(f"{self.name}/{directory.name}", "probes",
                         self.listeners["probes"], {}, None, crowd_per_edge=self.listeners["crowd"],
                         readers=self.listeners["readers"])

    def node_logs_end(self) -> None:
        self.logs.take(self, f"{self.name}/node-logs-end")

    def close_copies(self) -> None:
        self.senders.take(self, f"{self.name}/senders")
        summary_file = self.scenario / "summary.json"
        if summary_file.is_file() and summary_file.stat().st_mtime >= self.began_s:
            self.add_file(summary_file, f"{self.name}/summary.json", "result",
                          "each change beside its null in each layout: passes of the scored runs")


def run_binaries(args) -> dict:
    """The binaries a run takes from the environment: the lab's when it
    brings the lab up, the UDP client, and the load tool for a crowd."""
    out = guard.lab_binaries() if not args.no_up else {}
    out["needletail-udp-client"] = pathlib.Path(bench.UDP_CLIENT)
    if args.load_listeners:
        out["needletail-load"] = pathlib.Path(bench.LOAD_BIN)
    return out


def run_held(args, plan: dict, lanes: list[str], kind: str, scenario: pathlib.Path,
             record: ChangeRecord) -> int:
    results, senders, shaped, per_packet = [], [], [], False
    failures: list[str] = []
    previous = install_stops()
    try:
        try:
            if args.no_up:
                lab.join_tools()
            else:
                log(f"lab up: {' '.join(plan['lab_up'])}")
                lab.up(plan["lab_up"])
            senders, start = start_senders(args, lanes, scenario)
            plan["loop_start_unix"] = start
            log(f"senders start at {start} ({start - now():.1f} s)")
            sleep(max(0.0, start - now()))
            for lane in lanes:
                for site in sorted({lab.node(host)["site"] for host in hosts_of(args)}):
                    lab.wait_live(LANES[lane]["stream"], site, timeout=LIVE_TIMEOUT_S)
            plan["live_unix"] = now()
            if args.client_loss:
                per_packet = True
                lab.set_tcp_per_packet(True)
                for host in hosts_of(args):
                    shaped.append(("host", host))
                    lab.set_net(host, "pub", "both", bench.client_loss_spec(args.client_loss))
            for site, spec in wan_losses(args.wan_loss).items():
                shaped.append(("wan", site))
                lab.set_wan(site, "both", spec)
            for _ in range(repeats(args)):
                for each in plans_of(args, plan):
                    each["live_unix"] = plan["live_unix"]
                    directory = next_directory(scenario, each["kind"])
                    try:
                        result = run_once(args, each, directory)
                    finally:
                        record.run_directory(directory)
                    results.append(result)
                    for line in run_lines(result):
                        print(line, flush=True)
        finally:
            for what, name in shaped:
                if what == "host":
                    attempt(failures, f"take the impairment off {name}", lab.set_net, name,
                            "pub", "both", None)
                else:
                    attempt(failures, f"take the impairment off {name}'s wide-area link",
                            lab.set_wan, name, "both", None)
            if per_packet:
                attempt(failures, "TCP per packet off", lab.set_tcp_per_packet, False)
            attempt(failures, "stop the senders", stop_senders, lanes, senders)
            record.node_logs_end()
            if not args.keep_up and not args.no_up:
                attempt(failures, "take the lab down", lambda: lab.down(quiet=True))
    finally:
        restore_stops(previous)
    counts = {each: dict.fromkeys(STATUSES, 0) for each in ("change", "null")}
    for result in results:
        counts[result["kind"]][result["status"]] += 1
    print(f"{args.name}: " + "; ".join(count_text(each, counts[each])
                                       for each in ("change", "null")
                                       if any(counts[each].values())), flush=True)
    if alternates(args):
        print(side_by_side(args.name, results), flush=True)
    _record, lines = summary(scenario)
    print("\n".join(lines), flush=True)
    for item in failures:
        print(f"clean-up failed: {item}: check the lab (edge_lab.py status, down)", flush=True)
    passed = sum(counts[each]["pass"] for each in counts)
    return 0 if passed == len(results) and not failures else 1


def rescore(directory: pathlib.Path) -> int:
    if (directory / "run.json").is_file():
        runs, scenario = [score(directory)], directory.parent
    else:
        runs = [score(path.parent) for path in sorted(directory.glob("*/run.json"))]
        scenario = directory
    if not runs:
        raise SystemExit(f"{directory} holds no run")
    _record, lines = summary(scenario)
    print("\n".join([line for run in runs for line in run_lines(run)] + lines), flush=True)
    return 0 if all(run["status"] == "pass" for run in runs) else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verb == "score":
        return rescore(pathlib.Path(args.directory))
    try:
        return run(args)
    except Stopped as stop:
        log(f"{stop}: the run was stopped and its lab cleaned up")
        return 128 + stop.signum
    except KeyboardInterrupt:
        log("SIGINT: the run was stopped and its lab cleaned up")
        return 128 + signal.SIGINT


if __name__ == "__main__":
    sys.exit(main())
