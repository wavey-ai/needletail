#!/usr/bin/env python3
"""The first-session stall hunt's paid lab: bring it up, run its timeline,
collect its records, take it down.

    linode-stall-hunt.py plan
    linode-stall-hunt.py run --budget-usd B [--spent-before-usd S] [--run-id ID] [--dry]
    linode-stall-hunt.py collect [--dry]
    linode-stall-hunt.py down [--dry]

Design: docs/plans/resilient-mesh/D-hunt.md, section 7 (as revised after the
wave 9 run and the wave 9 4K run). The lab is
deploy/low-latency/topology-hunt.json: 24 dedicated-CPU instances, $1.512
an hour, two billed hours ($3.02; $3.13 with two replacement instances).
Its questions, in order: on a fresh deploy of the build with the fan-out
fix, does any edge still stop for 100 ms or more in the referee's first
1 ms Opus session, with UDP clients arriving at every edge; for each stop
that remains, was the vCPU not run, was an av-mesh thread in the kernel
with interrupts off, or was it blocked in our code (and on which wait); do
the arena warm-up, first touch of the guest's memory and listener churn
change it; and (the wave 9 4K run: whole-node stops of 1.6 to 8.5 s on both
New York forwarders, none with GSO off) do the senders' UDP GSO or the
receivers' UDP GRO make them, for 4K at 25 and 80 Mbit/s and for the first
audio session, in London and New York.

Every node is sampled by the referee's own samplers (needletail-compare
scripts/compare/compare-run.py `start_samplers` and `collect_samplers`,
loaded by path from NEEDLETAIL_COMPARE_ROOT, never copied): NODE.sys.T.log
at 1 s (per-CPU /proc/stat, interrupts, softirqs, softnet_stat, each
av-mesh and av-contrib thread's raw stat), NODE.heartbeat.T.log (a
SCHED_FIFO 99 heartbeat pinned to each vCPU, its late wakes), NODE.nic.T.txt
(ethtool), NODE.meshlocal.T.log (/api/mesh/local each second) and the rest
of the referee's files, in the run's samplers/. Beside them the hunt's
extra sampler (EXTRA_SAMPLER) takes what they do not: tc -s qdisc and the
Udp counters each second, and the kernel stacks of av-mesh's and
av-contrib's threads when a heartbeat is late, with
kernel.watchdog_thresh=1 and journalctl -k at collect.
scripts/compare/hunt-report.py reads what `collect` leaves in
target/stall-hunt/<run id>/ and applies the decision rules, which are fixed
before the run.

This tool creates and deletes nothing by itself. Every instance is created
and deleted by scripts/linode-small-mesh-lab.py, called by command line:
`up` with a watchdog of WATCHDOG_HOURS, its bounded collect
(COLLECT_TIMEOUT_S: the deploy tool's `collect --run-id` before the
watchdog deletes) and the budget given here (the lab tool refuses a plan
that is not dedicated, tags every instance and arms the watchdog before the
first create), `remove` and `grow` for the replacement,
`down` at the end, whatever happened before. The lab has its own state file
(target/linode-qualification/hunt-lab.json) and its own ceilings
(NEEDLETAIL_LINODE_MAX_HOURLY_USD=1.50, NEEDLETAIL_LINODE_MAX_NODES=24), set
for every call. The Linode token is the lab tool's to read; this tool never
reads, prints or stores it. Services are installed by
scripts/linode-small-mesh-deploy.py (`push`, `restart --roles`), the survey
is scripts/linode-route-survey.py; both by command line too.

`plan` prints the cost, the timeline and every command from the topology
alone: no lab, no token, no request. `run --dry` prints the same commands
through the code of a real run. A real `run` first checks, without a
request, that the lab tool has the watchdog, its collect bound and the
budget on `up` and the deploy tool a `collect --run-id`, that
the binaries carry what the hunt needs (the stall journal, the arena
warm-up's switch, needletail-nodecheck touch --hold, the sender's lateness
journal) and that no other lab's deploy settings are in this checkout; it
refuses to start otherwise, before anything is billed.

Timeline, minutes after `up` returns:
   0  `lab hosts`; on every node, first command: needletail-nodecheck watch;
      push (the fresh deploy); the referee's samplers on every mesh node,
      source and view host; the route survey; media to the hosts (the 4K
      clips and ffmpeg on the source host)
  15  the touched edges (half of them) touch all their free memory and hold it
  18  the referee's 20 s burn on the mesh nodes
  19.5 the touched edges give their memory back
  20  S1 Opus Custom 1 ms, 70 s: the first session, UDP clients arriving at
      every edge; then the one /api/hops pull (London's four large rings)
  24  S2 PCM 1 ms, 70 s
  28  S3 listener churn on every edge: 3 rounds of 50 s, 12 s apart
  35  the edge that was not run most is replaced (remove, grow, hosts,
      watch, push --nodes, the referee's samplers)
  44  M1, then M2, M3, M4 each as the one before ends: the GSO/GRO
      matrix, the defaults first (M1 sender GSO on, receiver GRO on; M2
      GSO off; M3 GRO off; M4 both off). Each: `deploy push` with
      relay_video_gso and client_udp_gso (the senders), relay_video_gro
      (the receivers) and relay_class_realtime set, which restarts every
      mesh node (each process's age is recorded: `fresh`); the first
      audio session of those processes (Opus Custom 1 ms, 120 s, the UDP
      probes with needletail-udp-client --gro as the combination says);
      then 4K at 25 and at 80 Mbit/s, 120 s each (the referee's clip over
      RTMP to the contributor, one HTTP/3 LL-HLS probe a view host through
      its site's entry). At worst M4 ends at 94.5.
      M5, if it starts by 96.8 minutes after `up` began: the defaults with
      relay_class_realtime false, 4K only (8.2 min at worst)
  96  collect (the referee's samplers collected, the watches stopped; the
      deploy tool's collect too: every node's journal since boot, its API
      and samplers into the run archive); `lab hosts` again
 101  down (inside the second billed hour; the watchdog is at 1.95 h)
A session is the referee's: the contributor restarted, the AEP1 sender at
the next 64 s boundary (with --lateness-journal), listeners from 12 s after
it. /api/hops is never read inside a session. A step that fails is
recorded and the run goes on; `collect` and `down` always run, also after
an interrupt, SIGTERM or SIGHUP (after a hangup the output goes to
after-hangup.log in the run's directory). Run it in tmux all the same: the
lab tool's watchdog deletes the lab at its deadline whatever happens here,
but an uncollected run is a failed run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import archived_run  # noqa: E402
import run_archive  # noqa: E402
import small_mesh_common as common  # noqa: E402
import stall_records  # noqa: E402

ROOT = common.ROOT
SCRIPTS = ROOT / "scripts"
LAB_TOOL = SCRIPTS / "linode-small-mesh-lab.py"
DEPLOY_TOOL = SCRIPTS / "linode-small-mesh-deploy.py"
SURVEY_TOOL = SCRIPTS / "linode-route-survey.py"
ARCHIVE_TOOL = SCRIPTS / "run_archive.py"
TOPOLOGY = ROOT / "deploy" / "low-latency" / "topology-hunt.json"
STATE = common.QUALIFICATION_ROOT / "hunt-lab.json"
OUT = ROOT / "target" / "stall-hunt"
# The lab's own ceilings and watchdog (7.1).
MAX_HOURLY_USD = 1.60
MAX_NODES = 26
WATCHDOG_HOURS = 1.95
BILLED_HOURS = 2
# Instances made anew in a run, each billed an hour: the edge replaced at
# REPLACE_MINUTE, and one grow tried again after a refusal part-way.
REPLACEMENTS = 2
# Plan prices, for `plan`'s arithmetic and the offline tests only: the lab
# tool prices and checks every plan from the API before it creates anything.
HOURLY = {"g6-dedicated-2": 0.054, "g6-dedicated-4": 0.108}
# No step starts this close to the watchdog's deadline (it starts `down`
# five minutes before it): the run goes to collect and down instead.
LATEST_START_BEFORE_DEADLINE_S = 15 * 60
# A new instance is `running` to the API before its sshd answers: the first
# command on it waits this long at most.
SSH_WAIT_S = 180
# `grow` after `remove`: the label of an instance just deleted can stay
# taken a while (the lab tool's own `replace` and `add` give a new label
# instead; `grow` cannot), so it is tried again this many times.
GROW_ATTEMPTS = 4
GROW_RETRY_S = 30.0
REMOTE = "/var/lib/needletail-runs/hunt"
REMOTE_MEDIA = "/var/lib/compare"
NODECHECK = "/usr/local/bin/needletail-nodecheck"
CA = "/usr/local/share/ca-certificates/needletail-qualification.crt"
SERVER_NAME = common.TLS_SERVER_NAME
HTTP_PORT = 19444
CLIENT_UDP_PORT = 19446
CONTRIB_INGEST_PORT = 27100
SAMPLE_RATE = 48_000
MESH_ROLES = ("contributor", "distributor", "edge")
# The three lanes: the AEP1 sender's payload and epoch, the stream, and the
# referee's ntcmp index of the lane.
CONFIGS = {
    "opus-1": {"payload": "opus", "epoch_samples": 48, "stream": 3001, "index": "nt-popus-48s-la"},
    "pcm-1": {"payload": "pcm", "epoch_samples": 48, "stream": 4001, "index": "nt-pcm-48s"},
    "opus-2.5": {"payload": "opus", "epoch_samples": 120, "stream": 3001,
                 "index": "nt-popus-120s-la"},
}
# The sessions, each the referee's (a role says what it is for): the first
# session on the fresh deploy; the PCM session after it, as the referee
# runs it; listener churn. The repeat after a restart is the GSO/GRO
# matrix's (MATRIX): each combination's first audio session is the first
# of new processes. (The steady 420 s sessions and the 2.5 ms lane of the
# plan before the 4K run were left out for the matrix within two billed
# hours; 3.1's acceptance is the proving run's.)
SESSIONS = (
    {"name": "S1", "minute": 20, "config": "opus-1", "seconds": 70, "role": "first"},
    {"name": "S2", "minute": 24, "config": "pcm-1", "seconds": 70, "role": "second"},
    {"name": "S3", "minute": 28, "config": "opus-1", "seconds": 50, "role": "churn"},
)
# The wave 9 4K run (notes-stage2, "Wave 9 4K on Linode"): whole-node stops
# on both New York forwarders at 25 to 120 Mbit/s, none with GSO off. Each
# combination of the senders' UDP GSO (relay_video_gso on the contributor
# and the relays, client_udp_gso on the edges' client fan-out) and the
# receivers' UDP GRO (relay_video_gro on the relays' and edges' video
# sockets, needletail-udp-client --gro at the listeners), the defaults
# first, then each one off, then both: a push that restarts every mesh
# node, the first audio session of those processes, then 4K at each rung.
#
# Each combination starts at minute 44 or as the one before it ends: the
# worst case is as with fixed slots (13 min apart), and the usual case
# leaves room for M5. Each push restarts every mesh node (the deploy's
# activate ends in `systemctl restart`), so every combination runs on
# freshly restarted processes (reports/4k.md: the cmpd6 GSO-off step was
# the only one on restarted nodes); `fresh` records each mesh process's age
# after the push.
#
# M5 (reports/4k.md, "what only Linode can settle"): the defaults with
# relay_class_realtime false (no SCHED_FIFO class thread pinned to a vCPU:
# whether the stops need a guest with no spare vCPU), 4K only. It runs only
# when it, collect and down end by OPTIONAL_END_MINUTE after `up` began
# (inside the second billed hour and before the watchdog's bounded
# collect); otherwise it is recorded as left out. It costs nothing more.
MATRIX = (
    {"name": "M1", "minute": 44, "gso": True, "gro": True},
    {"name": "M2", "minute": 44, "gso": False, "gro": True},
    {"name": "M3", "minute": 44, "gso": True, "gro": False},
    {"name": "M4", "minute": 44, "gso": False, "gro": False},
    {"name": "M5", "minute": 44, "gso": True, "gro": True, "realtime": False, "audio": False,
     "optional": True},
)
# M5's limit, in minutes from the start of `up` (the first create; the
# watchdog is armed then): 120 billed less 8, and under the watchdog's
# 117 less its 4-minute bounded collect.
OPTIONAL_END_MINUTE = 112
# What collect and down take at most after the last combination
# (COLLECT_MINUTE to DOWN_MINUTE, and down itself).
COLLECT_AND_DOWN_S = (5 + 2) * 60
MATRIX_SECONDS = 120
MATRIX_AUDIO = "opus-1"
VIDEO_RUNGS = ("2160p25", "2160p80")
# The referee's 4K lane: stream 1, RTMP into the contributor, its clips
# (a rung's directory) and indexes; COMPARE_CLIPS as the referee names it.
VIDEO_STREAM = 1
VIDEO_RTMP_PORT = 19350
VIDEO_WARM_S = 12
CLIPS = os.environ.get("NEEDLETAIL_HUNT_CLIPS", "clips6-full")
# A combination's push (skip bootstrap and binaries; every mesh node
# staged and activated, parents first), at worst.
APPLY_S = 180
# Listener churn (the churn session): every listener of every view host
# joins at once, for a round, then leaves; the next round 12 s later.
CHURN_ROUNDS = 3
CHURN_GAP_S = 12
# A round ends when its last probe has ended: ntcmp udp-ingest after the
# UDP client, the crowd's drain. The sender is given this much more a round.
ROUND_OVERHEAD_S = 15
TOUCH_MINUTE = 15
BURN_MINUTE = 18
RELEASE_MINUTE = 19.5
REPLACE_MINUTE = 35
COLLECT_MINUTE = 96
DOWN_MINUTE = 101
BURN_SECONDS = 20
# Listeners start this long after the sender's START, as the referee's do.
LISTEN_AFTER_START_S = 12
# The touched edges touch MemFree less a margin, at this rate, and hold it.
# The margin is this, or twice the zones' high watermarks when that is
# more, so that the kernel neither reclaims page cache nor swaps for it.
TOUCH_LEAVE_MB = 384
TOUCH_RATE_MB_S = 1024
STALL_JOURNAL_CAPACITY = 65_536
HOP_JOURNAL_LARGE = 2_097_152
# An edge's "not run" milliseconds count its heartbeats' late wakes of this
# long or longer (the referee's FIFO 99 heartbeat on each vCPU).
NOT_RUN_SECOND_MS = 50.0
# The referee's samplers (needletail-compare scripts/compare/compare-run.py,
# compare-cloudflare 472fb47 or later): loaded by path, never copied.
REFEREE_RUN = pathlib.Path("scripts") / "compare" / "compare-run.py"
REFEREE_SAMPLED_ROLES = ("contributor", "backbone", "distributor", "edge", "livekit", "load")
# av-mesh's switch of the audio class thread's arena warm-up (MB; 0: none):
# the no_arena_warm arm runs without it.
ARENA_FLAG = "--relay-class-arena-warm-mb"
# The watchdog's bounded collect at its deadline (the lab tool's `up
# --collect-timeout-s`, running the deploy tool's `collect --run-id`).
COLLECT_TIMEOUT_S = 240
# What a real run needs of the tools and binaries it calls (preflight).
LAB_UP_FLAGS = ("--watchdog-hours", "--budget-usd", "--collect-timeout-s", "--optional-sites",
                "--run-id")
DEPLOY_COLLECT_FLAGS = ("--run-id",)
DEPLOY_SETTINGS = ("node_overrides", "stall_journal_capacity", "mlock")
MESH_FLAG_NEEDS = {
    "--stall-journal-capacity": "the build is without the stall instruments (D1)",
    "--hop-journal-capacity": "the London rings cannot be enlarged",
    ARENA_FLAG: "the no_arena_warm arm cannot run (av-mesh must take the audio class thread's "
                "arena warm-up in MB, default 32, 0 for none)"}
MESH_FLAGS = tuple(MESH_FLAG_NEEDS)
SENDER_FLAGS = ("--lateness-journal",)
# The touched edges hold their memory until it is given back just before
# the first session.
TOUCH_FLAGS = ("--hold",)

# What the referee's samplers do not take and the 4K run asks of Linode
# (reports/4k.md, "what only Linode can settle"), sampled by the hunt itself
# beside them on every node they take, into REMOTE/extra.log (fetched with
# the node's records): once a second `tc -s qdisc show` and the Udp lines of
# /proc/net/snmp (SndbufErrors, RcvbufErrors, InErrors); and when one of the
# referee's heartbeats (/var/tmp/compare-heartbeat.CPU.log) is
# EXTRA_LATE_MS or more late, every av-mesh and av-contrib thread's kernel
# stack (/proc/PID/task/*/stack), state and wchan, at most one dump each
# EXTRA_DUMP_GAP_S and EXTRA_DUMPS in all. Before it starts it sets
# kernel.watchdog_thresh=1 (a soft lockup over 2 s goes to the kernel log
# with the stuck CPU's stack; the value before is kept in
# watchdog_thresh.before); collect saves `journalctl -k` as kernel.log.
EXTRA_LATE_MS = 50
EXTRA_DUMP_GAP_S = 0.5
EXTRA_DUMPS = 600
EXTRA_PY = r'''
import glob, os, subprocess, sys, time
out = open(sys.argv[1], "a", buffering=1)
late_ms, gap_s, limit = float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
def write(kind, text):
    out.write(f"{time.time():.6f} X {kind} {text}\n")
def ours():
    found = []
    for path in glob.glob("/proc/[0-9]*/comm"):
        try:
            if open(path).read().strip() in ("av-mesh", "av-contrib"):
                found.append(path.split("/")[2])
        except OSError:
            pass
    return found
def dump(reason):
    for pid in ours():
        for task in glob.glob(f"/proc/{pid}/task/*"):
            try:
                stat = open(f"{task}/stat").read()
                wchan = open(f"{task}/wchan").read().strip() or "0"
                frames = [line.split()[-1] for line in open(f"{task}/stack").read().splitlines() if line.split()]
            except OSError:
                continue
            comm = stat[stat.index("(") + 1:stat.rindex(")")].replace(" ", "_")
            state = stat[stat.rindex(")") + 2]
            write("stack", f"{reason} {pid} {task.rsplit('/', 1)[1]} {comm} {state} {wchan} {';'.join(frames) or '-'}")
offsets, first, dumps, last_dump, next_second = {}, True, 0, 0.0, 0.0
while True:
    now = time.time()
    if now >= next_second:
        next_second = now + 1
        try:
            for line in open("/proc/net/snmp").read().splitlines():
                if line.startswith(("Udp:", "UdpLite:")):
                    write("snmp", line)
        except OSError:
            pass
        qdisc = subprocess.run(["tc", "-s", "qdisc", "show"], capture_output=True, text=True)
        write("qdisc", qdisc.stdout.strip().replace("\n", " | "))
    for path in glob.glob("/var/tmp/compare-heartbeat.*.log"):
        try:
            size = os.path.getsize(path)
            at = offsets.get(path, size if first else 0)
            if size < at:
                at = 0
            with open(path) as beats:
                beats.seek(at)
                text = beats.read()
        except OSError:
            offsets.pop(path, None)
            continue
        whole = text.rfind("\n") + 1
        offsets[path] = at + whole
        for line in text[:whole].splitlines():
            parts = line.split()
            if len(parts) < 6 or parts[0] != "HB" or not parts[5].lstrip("-").isdigit():
                continue
            late = int(parts[5]) / 1e6
            if late >= late_ms:
                write("late", f"cpu {parts[1]} late_ms {late:.1f} realtime_ns {parts[3]}")
                if dumps < limit and time.time() - last_dump >= gap_s:
                    dumps += 1
                    last_dump = time.time()
                    dump(f"hb{parts[1]}:{late:.0f}ms")
    first = False
    time.sleep(0.1)
'''
EXTRA_SAMPLER = (
    f"mkdir -p {REMOTE}; [ -f {REMOTE}/extra.pid ] && kill $(cat {REMOTE}/extra.pid) 2>/dev/null; "
    f"sysctl -n kernel.watchdog_thresh >{REMOTE}/watchdog_thresh.before 2>&1; "
    "sysctl -qw kernel.watchdog_thresh=1; "
    "cat >/var/tmp/hunt-extra.py <<'EOS'\n" + EXTRA_PY + "EOS\n"
    f"(setsid nohup python3 /var/tmp/hunt-extra.py {REMOTE}/extra.log {EXTRA_LATE_MS} "
    f"{EXTRA_DUMP_GAP_S} {EXTRA_DUMPS} >{REMOTE}/extra.err 2>&1 </dev/null & "
    f"echo $! >{REMOTE}/extra.pid)")
KERNEL_LOG = f"journalctl -k -o short-iso-precise --no-pager >{REMOTE}/kernel.log 2>&1"

BURN = (
    "a=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $9}' /proc/stat); "
    f"timeout {BURN_SECONDS} sh -c 'while :; do :; done'; "
    "b=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $9}' /proc/stat); "
    "echo \"$a $b\" | awk '{printf \"%.1f\", 100*($4-$2)/($3-$1)}'")

SYSTEM_FACTS = (
    "echo thp: $(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null); "
    "echo balloon: $(ls /sys/bus/virtio/drivers/virtio_balloon 2>/dev/null | tr '\\n' ' '); "
    "for f in /sys/bus/virtio/drivers/virtio_balloon/virtio*/features; do "
    "[ -f \"$f\" ] && echo balloon-features: $(cat \"$f\"); done; "
    "echo clocksource: $(cat /sys/devices/system/clocksource/clocksource0/current_clocksource); "
    "echo meminfo: $(grep -E 'MemTotal|MemAvailable|AnonHugePages|SwapTotal|SwapFree' "
    "/proc/meminfo | tr '\\n' ' '); "
    "echo swaps: $(tail -n +2 /proc/swaps | tr '\\n' ' '); "
    "chronyc tracking 2>/dev/null || timedatectl timesync-status 2>/dev/null; true")


# ----------------------------------------------------------------- the plan

def load_topology(path: pathlib.Path | str | None = None) -> dict:
    topology = json.loads(pathlib.Path(path or TOPOLOGY).read_text())
    if "hunt" not in topology:
        sys.exit(f"{path or TOPOLOGY}: no `hunt` block; not the hunt's topology")
    return topology


def hourly_usd(topology: dict) -> float:
    """The lab's hourly price at the plan prices. A plan that is not a
    dedicated one of HOURLY has no price here and is refused."""
    total = 0.0
    for node in topology["nodes"]:
        if node["type"] not in HOURLY:
            sys.exit(f"{node['name']}: {node['type']} is not a dedicated plan this lab uses")
        total += HOURLY[node["type"]]
    return round(total, 4)


def cost(topology: dict) -> dict:
    """What a run costs (7.1): every instance for BILLED_HOURS, and
    REPLACEMENTS replacement instances of the smallest plan for an hour."""
    hourly = hourly_usd(topology)
    planned = round(hourly * BILLED_HOURS, 4)
    replacements = round(REPLACEMENTS * min(HOURLY.values()), 4)
    return {"instances": len(topology["nodes"]), "hourly_usd": hourly,
            "billed_hours": BILLED_HOURS, "planned_usd": planned,
            "replacements_usd": replacements, "run_usd": round(planned + replacements, 4),
            "with_one_repeat_usd": round(2 * (planned + replacements), 4),
            "max_hourly_usd": MAX_HOURLY_USD, "max_nodes": MAX_NODES,
            "watchdog_hours": WATCHDOG_HOURS}


def rounds_of(session: dict) -> int:
    return CHURN_ROUNDS if session["role"] == "churn" else 1


def listening_seconds(session: dict) -> int:
    """How long a session's listeners run: one round, or the churn's
    rounds and the gaps between them."""
    rounds = rounds_of(session)
    return rounds * session["seconds"] + (rounds - 1) * CHURN_GAP_S


def sender_seconds(session: dict) -> int:
    """How long the sender runs: from its START, the 12 s before the
    listeners, their rounds, each round's own end (its probes' processing,
    the crowd's drain) and a margin; it is stopped when the probes are
    done."""
    return (LISTEN_AFTER_START_S + listening_seconds(session)
            + rounds_of(session) * ROUND_OVERHEAD_S + 10)


def session_seconds(session: dict) -> float:
    """The longest a session takes: the contributor's restart, the wait for
    the 64 s boundary (8 to 72 s), the sender's run, and the journal
    pull."""
    return 5 + 72 + sender_seconds(session) + 30


def sessions_end_minute() -> float:
    """When the last session has ended at worst, each starting at its
    minute or when the one before it ended, whichever is later."""
    end = 0.0
    for session in SESSIONS:
        end = max(end, session["minute"]) + session_seconds(session) / 60
    return round(end, 2)


def matrix_audio(combination: dict) -> dict:
    """A combination's first audio session, as a session of SESSIONS."""
    return {"name": f"{combination['name']}-A", "minute": combination["minute"],
            "config": MATRIX_AUDIO, "seconds": MATRIX_SECONDS, "role": "matrix-first",
            "gro": combination["gro"], "combination": combination_of(combination)}


def combination_of(combination: dict) -> dict:
    return {"name": combination["name"], "gso": combination["gso"], "gro": combination["gro"],
            **({"realtime": False} if combination.get("realtime") is False else {})}


def video_seconds() -> float:
    """One rung, at worst: the contributor's restart, the feed's warm-up,
    the probes and their end."""
    return 5 + VIDEO_WARM_S + MATRIX_SECONDS + ROUND_OVERHEAD_S + 5


def combination_seconds(combination: dict) -> float:
    audio = session_seconds(matrix_audio(combination)) if combination.get("audio", True) else 0
    return APPLY_S + audio + len(VIDEO_RUNGS) * video_seconds()


def matrix_end_minute() -> float:
    """When the last combination that always runs has ended at worst (each
    at its minute or when the one before it ended)."""
    end = sessions_end_minute()
    for combination in MATRIX:
        if combination.get("optional"):
            continue
        end = max(end, combination["minute"]) + combination_seconds(combination) / 60
    return round(end, 2)


def optional_fits(combination: dict, elapsed_s: float) -> bool:
    """Whether an optional combination, started `elapsed_s` after `up`
    began, ends with collect and down by OPTIONAL_END_MINUTE."""
    return (elapsed_s + combination_seconds(combination) + COLLECT_AND_DOWN_S
            <= OPTIONAL_END_MINUTE * 60)


def optional_latest_start_minute(combination: dict) -> float:
    """The latest start, in minutes after `up` began, at which it runs."""
    latest = OPTIONAL_END_MINUTE - (combination_seconds(combination) + COLLECT_AND_DOWN_S) / 60
    return int(latest * 100) / 100


def combination_settings(combination: dict) -> list[str]:
    """The deploy settings of a combination (`--set` values): the senders'
    GSO, video (contributor, relays, the edges' fast-lane video) and the
    edges' audio fan-out to clients; the receivers' video GRO."""
    on = {True: "true", False: "false"}
    return [f"relay_video_gso={on[combination['gso']]}",
            f"client_udp_gso={on[combination['gso']]}",
            f"relay_video_gro={on[combination['gro']]}",
            f"relay_class_realtime={on[combination.get('realtime', True)]}"]


def describe(combination: dict) -> str:
    if combination.get("realtime") is False:
        return (f"{combination['name']}: the defaults with the class threads not real-time"
                + (", 4K only" if combination.get("audio") is False else "")
                + (", if it fits" if combination.get("optional") else ""))
    return (f"{combination['name']}: sender GSO {'on' if combination['gso'] else 'off'}, "
            f"receiver GRO {'on' if combination['gro'] else 'off'}"
            + (" (the defaults)" if combination["gso"] and combination["gro"] else ""))


def timeline(hunt: "Hunt") -> list[dict]:
    """The steps of a run in order: {"minute", "name", "run"}; a step never
    starts before its minute (counted from the return of `up`). Nothing
    but the deploy, the watch and the sampler comes before the first
    session's listeners except what the referee's lab did (its idle and its
    burn) and the touched arm's touch. The GSO/GRO matrix follows the
    replacement."""
    steps = [
        {"minute": 0, "name": "hosts", "run": lambda: hunt.hosts("first")},
        {"minute": 0, "name": "watch", "run": hunt.watch},
        {"minute": 0, "name": "push", "run": hunt.push},
        {"minute": 0, "name": "the referee's samplers", "run": hunt.sample},
        {"minute": 0, "name": "survey", "run": hunt.survey_start},
        {"minute": 0, "name": "media", "run": hunt.media},
        {"minute": TOUCH_MINUTE, "name": "touch the touched edges", "run": hunt.touch},
        {"minute": BURN_MINUTE, "name": "burn", "run": hunt.burn},
        {"minute": RELEASE_MINUTE, "name": "give the touched memory back", "run": hunt.release},
    ]
    sessions = [{"minute": session["minute"], "name": session["name"],
                 "run": lambda session=session: hunt.session(session)} for session in SESSIONS]
    steps += [step for step in sessions if step["minute"] < REPLACE_MINUTE]
    steps.append({"minute": REPLACE_MINUTE, "name": "replace the edge not run most",
                  "run": hunt.replace_worst})
    steps += [{"minute": combination["minute"], "name": describe(combination),
               "run": lambda combination=combination: hunt.combination(combination)}
              for combination in MATRIX]
    steps.append({"minute": COLLECT_MINUTE, "name": "collect", "run": hunt.collect})
    steps.append({"minute": DOWN_MINUTE, "name": "down", "run": hunt.down})
    return steps


# ------------------------------------------------------------- the commands

def epoch_ns(samples: int) -> int:
    return round(samples * 1_000_000_000 / SAMPLE_RATE)


def sender_command(config: str, target: str, seconds: int, directory: str) -> str:
    """The AEP1 sender on the source host, as the referee starts it: epoch k
    leaves when its last sample has been spoken, the session id the loop
    START (a multiple of 64 s, at least 8 s away) plus one epoch; with the
    lateness journal (D-hunt.md 2.7). Prints START."""
    cfg = CONFIGS[config]
    return (f"mkdir -p {directory}; pkill -x aep1-48k-probe; "
            "NOW=$(date +%s); START=$(( (NOW / 64 + 1) * 64 )); "
            "[ $(( START - NOW )) -lt 8 ] && START=$(( START + 64 )); "
            f"echo $START >{directory}/source.start; "
            f"nohup /usr/local/bin/aep1-48k-probe send --target {target}:{CONTRIB_INGEST_PORT} "
            f"--session-id ${{START}}{epoch_ns(cfg['epoch_samples']):09d} --group-id 0 "
            f"--duration-seconds {seconds} --payload {cfg['payload']} "
            f"--epoch-samples {cfg['epoch_samples']} --channels 2 --group-channels 8 "
            f"--pcm-s24le-file {REMOTE_MEDIA}/audio/audio.s24le "
            "--repair-percent 12 --min-repair-symbols 1 "
            f"--lateness-journal {directory}/sender.ndjson "
            f">{directory}/sender.json 2>{directory}/sender.log </dev/null & echo $START")


def udp_probe_command(name: str, entry: str, config: str, seconds: int, tag: str,
                      gro: bool = True) -> str:
    """One full UDP probe, the referee's: needletail-udp-client with its
    arrivals and parts, then `ntcmp udp-ingest` for the per-unit records.
    `gro` False: the client's socket without UDP GRO (--gro false; the
    matrix's receivers)."""
    cfg = CONFIGS[config]
    return (f"( date +%s.%N >{name}.launch; /usr/local/bin/needletail-udp-client "
            f"--entry https://{entry}:{HTTP_PORT} --stream {cfg['stream']} "
            + ("" if gro else "--gro false ") +
            f"--tls-ca {CA} --server-name {SERVER_NAME} --duration-seconds {seconds} "
            f"--session {tag}-{name} --arrivals {name}.arrivals.csv "
            f"--write-parts {name}.parts.bin >{name}.uc.json 2>{name}.uc.err; "
            f"ntcmp udp-ingest --arrivals {name}.arrivals.csv --parts {name}.parts.bin "
            f"--started $(cat {name}.launch) "
            f"--audio-index {REMOTE_MEDIA}/index/{cfg['index']}.index.json --skip-seconds 5 "
            f"--lane nt-uc --label {tag} --client {name}.uc.json --report {name}.probe.json "
            f"--frames {name}.probe.csv >>{name}.ingest.log 2>&1; "
            f"rm -f {name}.parts.bin {name}.launch )")


def hls_probe_command(name: str, edge: str, config: str, seconds: int, tag: str) -> str:
    """One LL-HLS probe at one edge, the referee's HTTP/3 reader with 40
    requests in flight: its rows carry that edge's commit stamp of every
    part, which is how the report finds the edge's stops. It reads the
    edge's media playlist itself: the master playlist would steer it to
    the first pathway, whichever edge that is."""
    cfg = CONFIGS[config]
    return (f"ntcmp hls --lane nt-h3 --mode chase --window 40 --http3 "
            f"--url 'https://{edge}:{HTTP_PORT}/live/{cfg['stream']}/stream.m3u8"
            f"?sid={tag}-{name}' --ca {CA} --server-name {SERVER_NAME} "
            f"--audio-index {REMOTE_MEDIA}/index/{cfg['index']}.index.json "
            f"--duration {seconds} --skip-seconds 5 --report {name}.probe.json --label {tag} "
            f"--count 1 --frames {name}.probe.csv >{name}.log 2>&1")


def load_command(name: str, listeners: int, sample: int, edges: list[str],
                 config: str, seconds: int, tag: str) -> str:
    """The crowd: needletail-load over every edge of the site with no entry
    point, so that listener i starts at edge i mod n and UDP clients arrive
    at every edge at once. Through an entry point each listener would go
    where /api/assignment sends it, which puts new readers on the edges
    that already hold the playlist (wave 9: every lon-view lane on
    lon-edge-1). The report checks the arrival per edge (intervals.jsonl)."""
    return ("( ulimit -n 1048576; needletail-load "
            f"--edges {','.join(f'{edge}:{CLIENT_UDP_PORT}' for edge in edges)} "
            f"--stream {CONFIGS[config]['stream']} "
            f"--listeners {listeners} --ramp {listeners}@0 --duration {seconds} "
            f"--sample {min(sample, listeners)} --out {name} --label {tag} --quiet "
            f">{name}.out 2>{name}.err )")


def video_sender_command(rung: str, target: str, seconds: int, directory: str) -> str:
    """The referee's 4K lane (compare-run.py Lane.start, "nt"): its clip of
    `rung` as FLV, without audio, over RTMP into the contributor, for
    `seconds`, detached; an earlier feed is stopped first."""
    feed = (f"timeout {seconds} ntcmp flv --clip {REMOTE_MEDIA}/clips/{rung} --no-audio | "
            "ffmpeg -hide_banner -loglevel warning -nostdin -f flv -i pipe:0 -c copy -f flv "
            f"rtmp://{target}:{VIDEO_RTMP_PORT}/live/needletail-lab")
    # The source host runs no other ntcmp (its probes are on the view
    # hosts): `pkill -x` by name, never -f, which would match this shell.
    return (f"mkdir -p {directory} && cd {directory} && {{ pkill -x ffmpeg; pkill -x ntcmp; true; }} && "
            f"(setsid nohup sh -c {shlex.quote(feed)} >video-{rung}.log 2>&1 </dev/null &)")


def video_probe_command(name: str, entry: str, rung: str, seconds: int, tag: str) -> str:
    """The referee's 4K probe (compare-run.py Lane.probe, "nt", "h3"): an
    HTTP/3 LL-HLS reader of stream 1 through the site's entry, eight part
    requests in flight, measured by the rung's index."""
    return (f"ntcmp hls --lane nt-h3 --mode chase --window 8 --http3 "
            f"--url 'https://{entry}:{HTTP_PORT}/live/{VIDEO_STREAM}/master.m3u8"
            f"?sid={tag}-{name}-{{i}}' --ca {CA} --server-name {SERVER_NAME} "
            f"--index {REMOTE_MEDIA}/index/{rung}.index.json --duration {seconds} "
            f"--report {name}.json --label {tag} --count 1 --frames {name}.csv "
            f">{name}.log 2>&1")


def listener_script(directory: str, rounds: list[list[str]], gap_s: int) -> str:
    """A host's probes of one session, round by round: each round's all at
    once, in the background, its start written to rounds.txt; the next
    round `gap_s` after the last of the round before has ended; the file
    `done` appears when the last round has ended."""
    body = ""
    for index, commands in enumerate(rounds, 1):
        if index > 1:
            body += f"sleep {gap_s}\n"
        body += f"echo \"{index} $(date +%s.%N)\" >>rounds.txt\n"
        body += " &\n".join(commands) + " &\nwait\n"
    return (f"mkdir -p {directory} && cd {directory} && rm -f done rounds.txt && "
            f"cat >run.sh <<'RUN'\n{body}touch done\nRUN\n"
            "(setsid nohup sh run.sh >run.log 2>&1 </dev/null &)")


def touch_command() -> str:
    """The touched arm: the guest's free memory (MemFree, not MemAvailable:
    that counts the page cache, which holds av-mesh's own text) less a
    margin, touched page by page by needletail-nodecheck touch and held
    (--hold) until release_command, so that it has been written once when
    the first session begins, and is not given back early. The margin is
    TOUCH_LEAVE_MB, or twice the zones' high watermarks when that is more:
    the kernel then has no reason to reclaim or swap for the touch. The
    mark (`touch <unix> <MB> <MemFree MB> <margin MB>`) and the pid are
    kept beside it, and /proc/vmstat as the touch starts
    (vmstat-touch.txt: the referee's samplers take no swap counters)."""
    return (f"mkdir -p {REMOTE} && cd {REMOTE} && cat /proc/vmstat >vmstat-touch.txt && "
            "set -- $(awk -v least=" + str(TOUCH_LEAVE_MB) + " '"
            "FNR == NR && /^MemFree:/ { free = int($2 / 1024) } "
            "FNR != NR && $1 == \"high\" { high += $2 } "
            "END { leave = int(2 * high * 4 / 1024); if (leave < least) leave = least; "
            "print free - leave, free, leave }' /proc/meminfo /proc/zoneinfo) && MB=$1 && "
            "echo \"touch $(date +%s.%N) $1 $2 $3\" >>touch-marks.txt && "
            f"(setsid nohup {NODECHECK} touch --mb $MB --rate-mb-s {TOUCH_RATE_MB_S} --hold "
            "--out touch-guest.ndjson >touch.log 2>&1 </dev/null & echo $! >touch.pid)")


def release_command() -> str:
    """The touched memory given back (SIGTERM to the holding touch, waited
    for up to 30 s); the mark says whether the touch had finished."""
    return (f"cd {REMOTE} && P=$(cat touch.pid) && "
            "if grep -q touch_done touch-guest.ndjson 2>/dev/null; then F=finished; "
            "else F=unfinished; fi && kill $P 2>/dev/null; n=0; "
            "while kill -0 $P 2>/dev/null && [ $n -lt 300 ]; do sleep 0.1; n=$((n + 1)); done; "
            "echo \"released $(date +%s.%N) $F\" >>touch-marks.txt")


def referee_samplers(root: pathlib.Path | None = None):
    """The referee's compare-run.py (needletail-compare, NEEDLETAIL_COMPARE_ROOT),
    loaded by path as a module of its own: its `start_samplers` and
    `collect_samplers` are called as they are. It imports small_mesh_common,
    which is this checkout's (already loaded here) and compare_lab.py from
    its own directory; sys.path is restored after."""
    path = (root or compare_root()) / REFEREE_RUN
    if not path.is_file():
        raise RuntimeError(f"{path} is missing: the referee's samplers")
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("referee_compare_run", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    for name in ("start_samplers", "collect_samplers"):
        if not callable(getattr(module, name, None)):
            raise RuntimeError(f"{path} has no {name}")
    return module


def node_overrides(topology: dict) -> dict:
    """The per-node deploy settings: the large hop journal on both London
    forwarders and the first pair of London twins, and the no_arena_warm
    arm (av-mesh's audio class thread starts without its arena warm-up)."""
    hunt = topology["hunt"]
    roles = {node["name"]: node["role"] for node in topology["nodes"]}
    overrides: dict[str, dict] = {}

    def add(name: str, args: list[str]) -> None:
        key = "extra_edge_args" if roles[name] == "edge" else "extra_distributor_args"
        overrides.setdefault(name, {}).setdefault(key, []).extend(args)

    for name in hunt.get("hop_journal_large", []):
        add(name, ["--hop-journal-capacity", str(HOP_JOURNAL_LARGE)])
    for name in hunt.get("no_arena_warm", []):
        add(name, [ARENA_FLAG, "0"])
    return overrides


def push_settings(topology: dict) -> list[str]:
    """The deploy settings of the hunt, as `--set` values: the referee's
    (the PCM rendition), the stall journal's size on every node, and the
    per-node overrides."""
    return ["daw_hls_formats=flac,opus,pcm",
            f"stall_journal_capacity={STALL_JOURNAL_CAPACITY}",
            "node_overrides=" + json.dumps(node_overrides(topology), sort_keys=True,
                                           separators=(",", ":"))]


def tool_environment() -> dict:
    """The hunt's own lab state and ceilings, for every tool it calls."""
    return {"NEEDLETAIL_LINODE_LAB_STATE": str(STATE),
            "NEEDLETAIL_LINODE_MAX_HOURLY_USD": f"{MAX_HOURLY_USD:.2f}",
            "NEEDLETAIL_LINODE_MAX_NODES": str(MAX_NODES)}


def relative(path) -> str:
    try:
        return str(pathlib.Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


# --------------------------------------------------------------- the runner

class Runner:
    """Every command of a run goes through here: carried out and written to
    the run's journal, or, dry, printed and not carried out."""

    def __init__(self, dry: bool, out: pathlib.Path) -> None:
        self.dry = dry
        self.out = out
        self.nodes: dict[str, dict] = {}
        self.lock = threading.Lock()

    def journal(self, kind: str, **fields) -> None:
        if self.dry:
            return
        self.out.mkdir(parents=True, exist_ok=True)
        with self.lock, open(self.out / "journal.ndjson", "a") as handle:
            handle.write(json.dumps({"unix": round(time.time(), 3), "kind": kind, **fields}) + "\n")

    def say(self, text: str) -> None:
        print(text, flush=True)

    def tool(self, script: pathlib.Path, *args: str, capture: bool = False,
             timeout: float | None = None) -> tuple[int, str]:
        """A tool of this checkout by command line, from the checkout's root
        (paths are given relative to it), under the hunt's lab state and
        ceilings. Returns (exit status, its output when captured)."""
        environment = tool_environment()
        shown = " ".join(f"{key}={relative(value) if key.endswith('STATE') else value}"
                         for key, value in environment.items())
        line = f"{shown} {relative(script)} " + " ".join(shlex.quote(arg) for arg in args)
        if self.dry:
            self.say(f"$ {line}")
            return 0, ""
        self.say(f"$ {relative(script)} " + " ".join(shlex.quote(arg) for arg in args))
        result = subprocess.run([sys.executable, str(script), *args], cwd=str(ROOT),
                                env={**os.environ, **environment}, timeout=timeout,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.STDOUT if capture else None)
        output = result.stdout.decode(errors="replace") if capture else ""
        if capture and output:
            self.say(output.rstrip())
        self.journal("tool", tool=script.name, args=list(args), rc=result.returncode)
        return result.returncode, output

    def ssh(self, name: str, command: str, check: bool = True, timeout: int = 600,
            stdin: bytes | None = None, what: str | None = None) -> str:
        """A command on a lab node. `what` stands for the command in the
        journal and the dry print when it carries a file."""
        if self.dry:
            self.say(f"ssh {name} {shlex.quote(what or command)}")
            return ""
        result = common.ssh(self.nodes[name], command, check=False, timeout=timeout, stdin=stdin)
        self.journal("ssh", node=name, command=what or command, rc=result.returncode)
        if check and result.returncode != 0:
            raise RuntimeError(f"{name}: remote command failed ({result.returncode}): "
                               f"{result.stderr.decode(errors='replace')[-800:]}")
        return result.stdout.decode(errors="replace").strip()

    def answers(self, name: str) -> bool:
        """Whether a node takes an ssh command now."""
        try:
            return common.ssh(self.nodes[name], "true", check=False, timeout=30).returncode == 0
        except Exception:  # noqa: BLE001 - a timeout or a refused connection: not yet
            return False

    def send_file(self, name: str, source: pathlib.Path, destination: str,
                  mode: str = "755") -> None:
        """A local file to a node through ssh alone (a new instance has no
        rsync before the deploy's bootstrap)."""
        if self.dry:
            self.say(f"ssh {name} 'cat >{destination}; chmod {mode} {destination}' "
                     f"<{relative(source)}")
            return
        self.ssh(name, f"mkdir -p {shlex.quote(str(pathlib.PurePosixPath(destination).parent))} "
                       f"&& cat >{destination}.next && chmod {mode} {destination}.next "
                       f"&& mv {destination}.next {destination}",
                 stdin=source.read_bytes(), timeout=900, what=f"cat >{destination} <{relative(source)}")

    def rsync_to(self, name: str, sources: list[pathlib.Path], destination: str) -> None:
        if self.dry:
            self.say(f"rsync {' '.join(relative(source) for source in sources)} "
                     f"{name}:{destination}")
            return
        common.rsync_to(self.nodes[name], [str(source) for source in sources], destination)
        self.journal("rsync-to", node=name, destination=destination)

    def rsync_from(self, name: str, source: str, destination: pathlib.Path) -> None:
        if self.dry:
            self.say(f"rsync {name}:{source} {relative(destination)}/")
            return
        destination.mkdir(parents=True, exist_ok=True)
        common.rsync_from(self.nodes[name], source, str(destination) + "/", timeout=1800)
        self.journal("rsync-from", node=name, source=source)

    def fetch_directory(self, name: str, source: str, destination: pathlib.Path,
                        exclude: tuple[str, ...] = ()) -> None:
        """A node's record directory, as a tar stream over ssh."""
        excluded = "".join(f" --exclude=./{item}" for item in exclude)
        if self.dry:
            self.say(f"ssh {name} 'tar -C {source}{excluded} -czf - .' | tar -C "
                     f"{relative(destination)} -xzf -")
            return
        result = common.ssh(self.nodes[name], f"tar -C {source}{excluded} -czf - .", check=False,
                            timeout=900)
        self.journal("fetch", node=name, source=source, rc=result.returncode)
        if result.returncode != 0 or not result.stdout:
            raise RuntimeError(f"{name}: {source} could not be read: "
                               f"{result.stderr.decode(errors='replace')[-300:]}")
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as archive:
            archive.extractall(destination, filter="data")

    def each(self, names: list[str], function) -> dict:
        """function(name) on every node at once (dry: one after another, so
        the print is in order). {name: result}; a failure is raised after
        every node was tried."""
        if self.dry:
            return {name: function(name) for name in names}
        return common.parallel([self.nodes[name] for name in names],
                               lambda node: function(node["name"]))


# ----------------------------------------------------------------- the hunt

class Hunt:
    def __init__(self, runner: Runner, topology: dict, topology_path: pathlib.Path,
                 run_id: str, budget_usd: float | None = None,
                 spent_before_usd: float | None = None, clock=time.time,
                 sleep=time.sleep) -> None:
        self.runner = runner
        self.topology = topology
        self.topology_path = topology_path
        self.run_id = run_id
        self.budget_usd = budget_usd
        self.spent_before_usd = spent_before_usd
        self.clock, self.sleep = clock, sleep
        self.t0: float | None = None
        self.up_started: float | None = None
        self.survey_dirs: list[pathlib.Path] = []
        self.interrupted = False
        hunt = topology["hunt"]
        self.record: dict = {
            "schema": "needletail.stall-hunt.v2", "run_id": run_id,
            "topology_file": relative(topology_path),
            "arms": {"touched": list(hunt.get("touched", [])),
                     "no_arena_warm": list(hunt.get("no_arena_warm", [])),
                     "hop_journal_large": list(hunt.get("hop_journal_large", []))},
            "periods": [], "sessions": [], "replaced": [], "steps": [], "burn": {},
            "hosts": {}, "nodes": {}, "sites_left_out": [], "survey": [],
        }
        self.use_topology_nodes()

    # ---- the nodes

    def use_topology_nodes(self) -> None:
        """Before the lab exists (and in a dry run): the topology's nodes,
        with a name in place of each public address."""
        self.runner.nodes = {
            node["name"]: {**node, "public_ipv4": f"<public:{node['name']}>",
                           "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
            for node in self.topology["nodes"]}


    def use_lab_nodes(self) -> None:
        """The lab as it is: its state file's nodes (a site left out is not
        among them; a replaced node has its new address)."""
        state = json.loads(STATE.read_text())
        self.runner.nodes = state["nodes"]
        self.record["nodes"] = {name: {key: node.get(key) for key in (
            "role", "site", "region", "type", "public_ipv4", "vlan_ipv4", "host_uuid")}
            for name, node in state["nodes"].items()}
        self.record["sites_left_out"] = state.get("sites_left_out", [])
        self.record["created_unix"] = state.get("created_unix")
        self.record["watchdog_deadline_unix"] = state.get("watchdog_deadline_unix")

    @property
    def nodes(self) -> dict[str, dict]:
        return self.runner.nodes

    def names(self, *roles: str) -> list[str]:
        return sorted(name for name, node in self.nodes.items() if node["role"] in roles)


    def present(self, names) -> list[str]:
        return [name for name in names if name in self.nodes]

    @property
    def mesh(self) -> list[str]:
        return self.names(*MESH_ROLES)

    @property
    def touched(self) -> list[str]:
        return self.present(self.topology["hunt"].get("touched", []))

    def purpose(self, purpose: str) -> list[str]:
        return sorted(name for name, node in self.nodes.items()
                      if common.load_purpose(node) == purpose)


    def address(self, name: str, peer: str | None = None) -> str:
        """A node's address for a peer: the VLAN's when both are on the
        site's VLAN, else the public one."""
        node = self.nodes[name]
        other = self.nodes.get(peer) if peer else None
        if other and node.get("vlan_ipv4") and other.get("vlan_ipv4") \
                and node["site"] == other["site"]:
            return node["vlan_ipv4"]
        return node["public_ipv4"]

    # ---- the record


    # ---- the record

    def out(self) -> pathlib.Path:
        return self.runner.out


    def save(self) -> None:
        if self.runner.dry:
            return
        self.out().mkdir(parents=True, exist_ok=True)
        path = self.out() / "hunt.json"
        temporary = path.with_suffix(".json.next")
        temporary.write_text(json.dumps(self.record, indent=1) + "\n")
        temporary.replace(path)

    def period(self, name: str, kind: str, start: float, end: float, **more) -> None:
        self.record["periods"].append({"name": name, "kind": kind, "from_unix": round(start, 3),
                                       "to_unix": round(end, 3), **more})
        self.save()


    def lab(self, *args: str, capture: bool = False) -> tuple[int, str]:
        return self.runner.tool(LAB_TOOL, *args, capture=capture)


    def deploy(self, *args: str) -> int:
        return self.runner.tool(DEPLOY_TOOL, *args)[0]


    def archive(self, *args: str) -> int:
        """The run archive (~/needletail-data/<run id>/, scripts/run_archive.py)
        by command line: the run's raw data is kept there, outside target/."""
        return self.runner.tool(ARCHIVE_TOOL, "--tool", "stall-hunt", *args)[0]


    def archive_create(self) -> None:
        """The run's entry in the archive, before anything is billed."""
        status = self.archive("create", self.run_id, "--kind", "linode-lab",
                              *[item for path in archived_run.code_paths(mesh_root(), contrib_root())
                                for item in ("--code", str(path))],
                              "--topology", relative(self.topology_path))
        if status != 0:
            raise RuntimeError("the run archive refused the run: nothing was created")

    # ---- the steps


    # ---- the steps

    def up(self) -> None:
        args = ["up", "--topology", relative(self.topology_path), "--run-id", self.run_id,
                "--optional-sites", "sydney", "--watchdog-hours", f"{WATCHDOG_HOURS:g}",
                "--collect-timeout-s", f"{COLLECT_TIMEOUT_S:g}",
                "--budget-usd",f"{self.budget_usd:g}" if self.budget_usd is not None
                else "<budget>"]
        if self.spent_before_usd is not None:
            args += ["--spent-before-usd", f"{self.spent_before_usd:g}"]
        self.up_started = self.clock()
        self.record["up_started_unix"] = round(self.up_started, 3)
        status, _ = self.lab(*args)
        if self.runner.dry:
            return
        if status != 0:
            raise RuntimeError(f"lab up failed with status {status}")
        self.use_lab_nodes()
        self.t0 = self.clock()
        self.record["t0_unix"] = round(self.t0, 3)
        self.save()


    def hosts(self, label: str) -> None:
        """`lab hosts` writes each node's physical host into the lab state.
        Its status 1 (nodes that back each other up share a host, or a
        region's edges sit on one) is a finding here, not a stop."""
        status, output = self.lab("hosts", capture=True)
        if self.runner.dry:
            return
        (self.out() / f"hosts-{label}.txt").write_text(output)
        self.use_lab_nodes()
        self.record["hosts"][label] = {
            "unix": round(self.clock(), 3), "status": status,
            "host_uuid": {name: node.get("host_uuid") for name, node in self.nodes.items()}}
        self.save()


    def install_nodecheck(self, names: list[str]) -> None:
        source = mesh_binary("needletail-nodecheck")
        self.runner.each(names, lambda name: self.runner.send_file(name, source, NODECHECK))


    def wait_ssh(self, names: list[str], patience_s: float = SSH_WAIT_S) -> list[str]:
        """The nodes of `names` that take an ssh command within `patience_s`:
        a new instance is `running` to the API before its sshd answers."""
        if self.runner.dry:
            self.runner.say(f"# until ssh answers on {', '.join(names)} (at most {patience_s:g} s)")
            return list(names)
        deadline = self.clock() + patience_s
        pending = list(names)
        while True:
            answered = self.runner.each(pending, self.runner.answers)
            pending = [name for name in pending if not answered.get(name)]
            if not pending or self.clock() >= deadline:
                break
            self.sleep(5.0)
        return [name for name in names if name not in pending]


    def watch(self, names: list[str] | None = None) -> None:
        """On every node, the first command: needletail-nodecheck watch,
        which runs to the end and allocates nothing. A node whose ssh does
        not answer in time is named in the step's failure; the others are
        watched all the same."""
        names = sorted(self.nodes) if names is None else names
        ready = self.wait_ssh(names)
        if ready:
            self.install_nodecheck(ready)
            self.runner.each(ready, lambda name: self.runner.ssh(name, (
                f"mkdir -p {REMOTE} && (setsid nohup {NODECHECK} watch --out "
                f"{REMOTE}/watch.ndjson >{REMOTE}/watch.log 2>&1 </dev/null & "
                f"echo $! >{REMOTE}/watch.pid)")))
        silent = [name for name in names if name not in ready]
        if silent:
            raise RuntimeError(f"ssh did not answer within {SSH_WAIT_S} s: no watch on "
                               + ", ".join(silent))

    def push(self) -> None:
        """The fresh deploy of the build under test."""
        args = ["push", "--remeasure"]
        for setting in push_settings(self.topology):
            args += ["--set", setting]
        if self.deploy(*args) != 0:
            raise RuntimeError("deploy push failed")

    def sampled(self, names: list[str] | None = None) -> list[str]:
        """The nodes the referee's samplers take: every node of its roles
        (the mesh, the source and view hosts; no bare node)."""
        return [name for name in (sorted(self.nodes) if names is None else names)
                if self.nodes[name]["role"] in REFEREE_SAMPLED_ROLES]

    def referee_session(self, names: list[str]):
        """What the referee's start_samplers and collect_samplers take of a
        session: its nodes and its directory (they write samplers/ there)."""
        return types.SimpleNamespace(nodes={name: self.nodes[name] for name in names},
                                     dir=self.out(), name=self.run_id)

    def sample(self, names: list[str] | None = None) -> None:
        """The referee's samplers (compare-run.py start_samplers) on every
        node it samples (or on `names`): NODE.sys.T.log at 1 s (per-CPU
        /proc/stat, interrupts, softirqs, softnet_stat, every av-mesh and
        av-contrib thread's raw stat), a SCHED_FIFO 99 heartbeat pinned to
        each vCPU (NODE.heartbeat.T.log), /api/stats and /api/mesh/local
        each second, and NODE.nic.T.txt (ethtool) now. They run until
        collect_samplers."""
        names = self.sampled(names)
        if self.runner.dry:
            self.runner.say(f"# the referee's samplers: {relative(compare_root() / REFEREE_RUN)}"
                            f" start_samplers on {', '.join(names)} (sys, heartbeat, stats, "
                            "meshlocal; nic now) into samplers/")
        else:
            module = referee_samplers()
            sampled = module.start_samplers(self.referee_session(names))
            self.runner.journal("referee-samplers", action="start",
                                nodes=[item["name"] for item in sampled])
        # Beside them, what they do not take (EXTRA_SAMPLER).
        self.runner.each(names, lambda name: self.runner.ssh(
            name, EXTRA_SAMPLER, what=(
                "kernel.watchdog_thresh=1; the extra sampler of the hunt: tc -s qdisc and Udp "
                "SndbufErrors/RcvbufErrors at 1 s, kernel stacks of the av-mesh and av-contrib "
                f"threads on a heartbeat {EXTRA_LATE_MS} ms late >extra.log")))

    def collect_samplers(self, names: list[str] | None = None) -> None:
        """The referee's collect_samplers: each node's samplers stopped and
        their logs fetched into samplers/ (a file a node and kind for each
        call), and the lab's hosts beside them."""
        names = self.sampled(names)
        if self.runner.dry:
            self.runner.say(f"# the referee's samplers: collect_samplers on {', '.join(names)} "
                            "(NODE.sys, NODE.heartbeat, NODE.stats, NODE.meshlocal, NODE.stalls "
                            "into samplers/, hosts.T.txt)")
            return
        module = referee_samplers()
        module.collect_samplers(self.referee_session(names),
                                [self.nodes[name] for name in names])
        self.runner.journal("referee-samplers", action="collect", nodes=names)

    def survey_start(self) -> None:
        directory = self.out() / ("survey" if not self.survey_dirs
                                  else f"survey-{len(self.survey_dirs) + 1}")
        status, _ = self.runner.tool(SURVEY_TOOL, "start", "--out", relative(directory),
                                     *(["--dry"] if self.runner.dry else []))
        if status != 0:
            raise RuntimeError("the route survey did not start")
        self.survey_dirs.append(directory)
        self.record["survey"].append(relative(directory))
        self.save()


    def survey_collect(self) -> None:
        if not self.survey_dirs:
            return
        dry = ["--dry"] if self.runner.dry else []
        self.runner.tool(SURVEY_TOOL, "stop", *dry)
        self.runner.tool(SURVEY_TOOL, "collect", "--out", relative(self.survey_dirs[-1]), *dry)


    def media(self) -> None:
        """What the sessions need on the hosts: the referee's loop on the
        source host; on the view hosts the referee's ntcmp (the build with
        udp-ingest), its indexes and needletail-load."""
        media = compare_root() / "target" / "compare-media" / "audio"
        indexes = [media / f"{cfg['index']}.index.json" for cfg in CONFIGS.values()]
        clips = clips_root()

        def source(name: str) -> None:
            self.runner.ssh(name, f"mkdir -p {REMOTE} {REMOTE_MEDIA}/audio {REMOTE_MEDIA}/clips && "
                                  "{ command -v ffmpeg >/dev/null || { export "
                                  "DEBIAN_FRONTEND=noninteractive; apt-get update -q >/dev/null; "
                                  "apt-get install -y -q ffmpeg >/dev/null; }; }", timeout=900)
            self.runner.rsync_to(name, [media / "audio.s24le"], f"{REMOTE_MEDIA}/audio/")
            # The 4K lane's clips (each rung a directory) and ntcmp, which
            # makes their FLV.
            self.runner.rsync_to(name, [clips / rung for rung in VIDEO_RUNGS],
                                 f"{REMOTE_MEDIA}/clips/")
            self.runner.rsync_to(name, [compare_root() / "target" / "ntcmp"], "/usr/local/bin/")

        def view(name: str) -> None:
            self.runner.ssh(name, f"mkdir -p {REMOTE} {REMOTE_MEDIA}/index")
            self.runner.rsync_to(name, indexes + [clips / f"{rung}.index.json"
                                                  for rung in VIDEO_RUNGS],
                                 f"{REMOTE_MEDIA}/index/")
            self.runner.rsync_to(name, [compare_root() / "target" / "ntcmp",
                                        mesh_binary("needletail-load")], "/usr/local/bin/")

        self.runner.each(self.purpose("source"), source)
        self.runner.each(self.purpose("view"), view)


    def burn(self) -> None:
        """The referee's steal check: 20 s of one busy loop on every mesh
        node, with the steal it showed."""
        started = self.clock()
        results = self.runner.each(self.mesh, lambda name: self.runner.ssh(
            name, BURN, check=False, timeout=BURN_SECONDS + 60))
        self.record["burn"] = {name: value for name, value in results.items()}
        self.period("burn", "burn", started, self.clock())


    def touch(self) -> None:
        """The touched arm: the guest's free memory written once and held."""
        names = self.touched
        started = self.clock()
        self.runner.each(names, lambda name: self.runner.ssh(name, touch_command()))
        self.record["touch"] = {"nodes": names, "from_unix": round(started, 3)}
        self.save()

    def release(self) -> None:
        """Just before the first session the touched memory is given back."""
        names = self.touched
        self.runner.each(names, lambda name: self.runner.ssh(name, release_command(),
                                                             timeout=120))
        touch = self.record.setdefault("touch", {"nodes": names})
        touch["released_unix"] = round(self.clock(), 3)
        self.period("touch", "touch", touch.get("from_unix", self.clock()),
                    touch["released_unix"], nodes=names)

    def entry(self, host: str, letter: str = "a") -> str | None:
        """The public address of a view host's site forwarder (the referee's
        entry is <site>-dist-a)."""
        name = f"{host.split('-')[0]}-dist-{letter}"
        return self.nodes[name]["public_ipv4"] if name in self.nodes else None


    def site_edges(self, host: str) -> list[str]:
        """The edges of a view host's site, by name."""
        site = self.nodes[host]["site"]
        return [name for name in self.names("edge") if self.nodes[name]["site"] == site]

    def listeners(self, host: str, plan: dict, config: str, seconds: int, tag: str,
                  prefix: str, gro: bool = True) -> list[str]:
        """A view host's listeners of one round: the UDP probes through the
        site's entry (`gro` False: without UDP GRO), one LL-HLS probe at
        every edge of the site, and the crowd over every edge of the site."""
        site_entry = self.entry(host)
        edges = self.site_edges(host)
        commands = [udp_probe_command(f"{prefix}uc-{number}", site_entry, config, seconds, tag,
                                      gro)
                    for number in range(1, plan.get("udp", 0) + 1)]
        for edge in edges:
            for number in range(1, plan.get("hls_each_edge", 0) + 1):
                commands.append(hls_probe_command(f"{prefix}hls-{edge}-{number}",
                                                  self.nodes[edge]["public_ipv4"], config,
                                                  seconds, tag))
        if plan.get("load"):
            commands.append(load_command(f"{prefix}load", plan["load"], plan.get("sample", 8),
                                         [self.nodes[edge]["public_ipv4"] for edge in edges],
                                         config, seconds, tag))
        return commands

    def session(self, session: dict) -> None:
        name, config, seconds = session["name"], session["config"], session["seconds"]
        directory = f"{REMOTE}/{name}"
        tag = f"hunt-{self.run_id}-{name}"
        contributor = self.names("contributor")[0]
        source = self.purpose("source")[0]
        rounds = rounds_of(session)
        entry = {"name": name, "role": session["role"], "config": config,
                 "stream": CONFIGS[config]["stream"], "seconds": seconds, "rounds": rounds,
                 "hosts": {}, **({"combination": session["combination"]}
                                 if session.get("combination") else {})}
        # As the referee's Audio.start: the contributor restarted, then the
        # sender at the next 64 s boundary.
        self.runner.ssh(contributor, "systemctl restart needletail-contrib.service; sleep 3; "
                                     "systemctl is-active needletail-contrib.service")
        printed = self.runner.ssh(source, sender_command(
            config, self.nodes[contributor]["vlan_ipv4"], sender_seconds(session), directory))
        if not self.runner.dry:
            start = int(printed.split()[-1])
            wait = int(self.runner.ssh(source, f"echo $(( {start} - $(date +%s) ))") or 0)
            entry["start_unix"] = start
            self.sleep(max(0, wait) + LISTEN_AFTER_START_S)
        plans = self.topology["hunt"]["listeners"]
        launched = self.clock()
        for host in self.present(sorted(plans)):
            if self.entry(host) is None:
                continue
            host_tag = f"{tag}-{host}"
            script = listener_script(directory, [
                self.listeners(host, plans[host], config, seconds, host_tag,
                               f"r{index}-" if rounds > 1 else "", session.get("gro", True))
                for index in range(1, rounds + 1)], CHURN_GAP_S)
            self.runner.ssh(host, script)
            entry["hosts"][host] = {**plans[host], "edges": self.site_edges(host)}
        if not self.runner.dry:
            entry["probes_from_unix"] = round(launched, 3)
            self.sleep(listening_seconds(session) + 5)
            self.wait_for_probes(sorted(entry["hosts"]), directory, 90)
            entry["probes_to_unix"] = round(launched + listening_seconds(session), 3)
        self.runner.ssh(source, "pkill -x aep1-48k-probe; true", check=False)
        if session["role"] == "first":
            # The touched edges' /proc/vmstat at the end of the first
            # session, against vmstat-touch.txt (H4: swap and refaults).
            self.runner.each(self.touched, lambda name: self.runner.ssh(
                name, f"cat /proc/vmstat >{REMOTE}/vmstat-after-first.txt", check=False))
            # Once in the run, after the first session has ended, never
            # inside one (a pull allocates 50 to 80 MB on the node); only
            # the session's own stream since its start.
            entry["hops_pull"] = self.pull_hops(name, CONFIGS[config]["stream"],
                                                entry.get("start_unix"))
        self.record["sessions"].append(entry)
        self.period(name, "session", entry.get("probes_from_unix", launched),
                    entry.get("probes_to_unix", launched + listening_seconds(session)),
                    config=config, role=session["role"],
                    **({"combination": session["combination"]}
                       if session.get("combination") else {}))

    def wait_for_probes(self, hosts: list[str], directory: str, patience_s: float) -> None:
        deadline = self.clock() + patience_s
        pending = set(hosts)
        while pending and self.clock() < deadline:
            for host in sorted(pending):
                if self.runner.ssh(host, f"test -f {directory}/done && echo done",
                                   check=False) == "done":
                    pending.discard(host)
            if pending:
                self.sleep(3.0)
        if pending:
            self.runner.say(f"probes still running on {', '.join(sorted(pending))}: "
                            "their records are taken as they are at collect")


    def pull_hops(self, session: str, stream: int, start_unix: int | None) -> dict:
        """/api/hops of the session's stream since its sender's START from
        the nodes with the large ring (D-hunt.md 2.5: never polled)."""
        names = self.present(self.topology["hunt"].get("hop_journal_large", []))
        started = self.clock()
        since = None if start_unix is None else int(start_unix) * 1_000_000
        query = f"/api/hops?since_us={'<session start>' if since is None else since}&stream={stream}"
        if self.runner.dry:
            for name in names:
                self.runner.say(f"GET https://{self.nodes[name]['public_ipv4']}:{HTTP_PORT}"
                                f"{query} >nodes/{name}/hops-{session}.ndjson")
            return {"nodes": names}

        def one(name: str) -> int:
            body = stall_records.fetch(self.nodes[name]["public_ipv4"], HTTP_PORT, query,
                                       timeout=120)
            target = self.out() / "nodes" / name
            target.mkdir(parents=True, exist_ok=True)
            (target / f"hops-{session}.ndjson").write_bytes(body)
            return len(body)

        sizes, failed = {}, {}
        for name in names:
            try:
                sizes[name] = one(name)
            except Exception as error:  # noqa: BLE001 - one node's journal is not the run
                failed[name] = str(error)[-200:]
        return {"nodes": names, "since_us": since, "stream": stream,
                "from_unix": round(started, 3), "to_unix": round(self.clock(), 3),
                "bytes": sizes, "failed": failed}

    # ---- the replacement and the restart

    def session_spans(self) -> list[tuple[int, int]]:
        """The sessions run so far, (from_us, to_us)."""
        return [(int(item["from_unix"] * 1e6), int(item["to_unix"] * 1e6))
                for item in self.record["periods"] if item.get("kind") == "session"]

    def not_run_so_far(self) -> dict[str, float]:
        """Each edge's "not run" milliseconds in the sessions so far (not in
        the deploy, the burn or the touched arm's touch, which would choose
        a touched edge for its arm): its watch's stopped ticks that ended
        in a session, and its vCPUs' heartbeats (the referee's, read where
        they run) late by NOT_RUN_SECOND_MS or more in a session, the most
        of any vCPU."""
        spans = self.session_spans()
        out = {}
        for name in self.names("edge"):
            try:
                watch = self.runner.ssh(name, f"cat {REMOTE}/watch.ndjson", timeout=120)
                beats = self.runner.ssh(name, "cat /var/tmp/compare-heartbeat.*.log 2>/dev/null; "
                                              "true", timeout=120)
            except Exception:  # noqa: BLE001 - a node that does not answer has no figure
                continue
            events = stall_records.dedupe(stall_records.parse_ndjson(watch))
            cpus = len(stall_records.cpus_of(events)) or 1
            out[name] = round(sum(stall_records.stopped_ms(events, start, end, cpus)
                                  for start, end in spans)
                              + stall_records.heartbeat_late_ms(
                                  stall_records.parse_heartbeat(beats), NOT_RUN_SECOND_MS, spans),
                              3)
        return out

    def worst(self) -> tuple[str | None, dict[str, float]]:
        """The edge that was not run most; None when none was."""
        figures = self.not_run_so_far()
        if not figures or max(figures.values()) <= 0:
            return None, figures
        return max(sorted(figures), key=figures.get), figures

    def replace_worst(self) -> None:
        """The "not run" rule's step: the edge not run most is replaced once
        and is in every session after it (the repeat included)."""
        if self.runner.dry:
            # Which edge it is is known only in the run: the commands are
            # printed for a stand-in, which is no node of any session.
            stand_in = "<the edge not run most>"
            self.nodes[stand_in] = {"name": stand_in, "role": "edge",
                                    "public_ipv4": f"<public:{stand_in}>"}
            try:
                self.replace(stand_in)
            finally:
                self.nodes.pop(stand_in)
            return
        name, figures = self.worst()
        self.record["not_run_ms_at_replace"] = figures
        self.save()
        if name is None:
            self.runner.say("no edge was not run: nothing is replaced")
            return
        self.replace(name, not_run_ms=figures.get(name))

    def replace(self, name: str, not_run_ms: float | None = None) -> None:
        """A new instance in an edge's place: its records fetched, remove,
        grow, hosts, the watch as its first command, `push --nodes` (an
        edge is named by its peers by VLAN address only, which the new
        instance keeps), and the referee's samplers. The lab's nodes are
        read again whatever happened."""
        try:
            self.replace_instance(name, not_run_ms)
        finally:
            if not self.runner.dry and STATE.exists():
                self.use_lab_nodes()

    def replace_instance(self, name: str, not_run_ms: float | None) -> None:
        dry = self.runner.dry
        before = None if dry else self.nodes[name].get("host_uuid")
        address = None if dry else self.nodes[name].get("public_ipv4")
        # The instance's own records (its watch and the referee's samplers
        # above all) go with it: they are fetched first, beside the new
        # instance's.
        try:
            self.collect_samplers([name])
            self.stop_recorders([name])
            self.runner.ssh(name, KERNEL_LOG, check=False, what="journalctl -k >kernel.log")
            self.runner.fetch_directory(name, REMOTE, self.out() / "nodes" / name / "before-replace")
        except Exception as error:  # noqa: BLE001 - a timeout too; the replace goes on
            self.runner.say(f"{name}: its records could not be fetched before the replace: {error}")
        status, _ = self.lab("remove", name, "--any-lab-node")
        if not dry and status != 0:
            raise RuntimeError(f"lab remove {name} failed")
        for attempt in range(GROW_ATTEMPTS):
            if attempt:
                self.sleep(GROW_RETRY_S)
            status, _ = self.lab("grow", "--topology", relative(self.topology_path))
            if dry or status == 0:
                break
        if not dry and status != 0:
            raise RuntimeError(f"lab grow did not recreate {name} in {GROW_ATTEMPTS} tries: "
                               "the run goes on without it")
        self.hosts(f"replaced-{name}" if not dry else "replaced")
        entry = {"name": name, "unix": round(self.clock(), 3), "pushed": False,
                 "watched": False, "sampled": False, "not_run_ms_before": not_run_ms,
                 "host_uuid_before": before, "public_ipv4_before": address,
                 "host_uuid_after": None if dry else self.nodes[name].get("host_uuid")}
        if not dry:
            self.record["replaced"].append(entry)
            self.save()
        failures = []
        try:
            self.watch([name])
            entry["watched"] = True
        except Exception as error:  # noqa: BLE001 - the push is tried all the same
            failures.append(str(error))
        entry["pushed"] = self.deploy("push", "--nodes", name) == 0
        if entry["pushed"]:
            try:
                self.sample([name])
                entry["sampled"] = True
            except Exception as error:  # noqa: BLE001 - named in the step's failure
                failures.append(f"{name}: no samplers: {error}")
        elif not dry:
            failures.append(f"push --nodes {name} failed: the new instance runs the watch only")
        if not dry:
            self.save()
        if failures:
            raise RuntimeError("; ".join(failures))

    # ---- the GSO/GRO matrix

    def combination(self, combination: dict) -> None:
        """One combination of the senders' GSO and the receivers' GRO: the
        push that sets them (every mesh node staged and activated: new
        processes), the first audio session of those processes, then 4K at
        each rung. A part that fails is recorded and the next goes on. An
        optional one (M5) runs only if it fits (OPTIONAL_END_MINUTE)."""
        if combination.get("optional"):
            latest = optional_latest_start_minute(combination)
            if self.runner.dry:
                self.runner.say(f"# {combination['name']} runs only if it starts by minute "
                                f"{latest:g} after `up` began (then it, collect and down end "
                                f"by {OPTIONAL_END_MINUTE}); else it is left out")
            else:
                elapsed = self.clock() - (self.up_started or self.t0 or self.clock())
                if not optional_fits(combination, elapsed):
                    self.record.setdefault("left_out", []).append(
                        {"name": combination["name"], "elapsed_min": round(elapsed / 60, 2),
                         "latest_start_min": latest})
                    self.save()
                    self.runner.say(f"{combination['name']} left out: {elapsed / 60:.1f} min "
                                    f"after up began, later than {latest:g}")
                    return
        started = self.clock()
        status = self.deploy("push", "--skip-bootstrap", "--skip-binaries",
                             *[item for setting in combination_settings(combination)
                               for item in ("--set", setting)])
        self.period(f"{combination['name']}-apply", "apply", started, self.clock(),
                    ok=status == 0, combination=combination_of(combination))
        if status != 0:
            raise RuntimeError(f"{combination['name']}: deploy push failed: the combination "
                               "is not measured")
        self.fresh(combination, started)
        failures = []
        audio = ([("audio", lambda: self.session(matrix_audio(combination)))]
                 if combination.get("audio", True) else [])
        for part, run in audio + [
                (rung, lambda rung=rung: self.video(combination, rung)) for rung in VIDEO_RUNGS]:
            try:
                run()
            except Exception as error:  # noqa: BLE001 - the other parts go on
                failures.append(f"{part}: {str(error)[-300:]}")
        if failures:
            raise RuntimeError(f"{combination['name']}: " + "; ".join(failures))

    def fresh(self, combination: dict, pushed_from: float) -> None:
        """Each mesh process's age after a combination's push (`ps -o
        etimes`): a process older than the push did not restart."""
        command = ("for p in $(pgrep -x av-mesh) $(pgrep -x av-contrib); do "
                   "echo $(cat /proc/$p/comm) $(ps -o etimes= -p $p); done")
        if self.runner.dry:
            self.runner.say(f"# {combination['name']}: the age of each mesh process "
                            f"(every one restarted by the push): ssh <mesh> {shlex.quote(command)}")
            return
        ages = {}
        try:
            for name, text in self.runner.each(self.present(self.mesh()), lambda name: self.runner.ssh(
                    name, command, check=False, timeout=60)).items():
                ages[name] = [int(line.split()[1]) for line in str(text).splitlines()
                              if len(line.split()) == 2 and line.split()[1].isdigit()]
        except Exception as error:  # noqa: BLE001 - a record, not a step
            self.runner.say(f"{combination['name']}: process ages not read: {error}")
        since = self.clock() - pushed_from
        stale = sorted(name for name, values in ages.items()
                       if not values or max(values) > since + 5)
        self.record.setdefault("fresh", []).append(
            {"name": combination["name"], "ages_s": ages, "push_s_ago": round(since, 1),
             "not_restarted": stale})
        self.save()
        if stale:
            self.runner.say(f"{combination['name']}: not restarted by the push: {', '.join(stale)}")

    def video(self, combination: dict, rung: str) -> None:
        """4K at one rung for MATRIX_SECONDS: the contributor restarted (as
        the referee's Lane.start), the clip fed over RTMP, VIDEO_WARM_S to
        settle, then one HTTP/3 probe a view host through its site's entry."""
        name = f"{combination['name']}-V{rung.removeprefix('2160p')}"
        directory = f"{REMOTE}/{name}"
        tag = f"hunt-{self.run_id}-{name}"
        contributor = self.names("contributor")[0]
        source = self.purpose("source")[0]
        self.runner.ssh(contributor, "systemctl restart needletail-contrib.service; sleep 3; "
                                     "systemctl is-active needletail-contrib.service")
        self.runner.ssh(source, video_sender_command(
            rung, self.nodes[contributor].get("vlan_ipv4") or self.nodes[contributor]["public_ipv4"],
            int(VIDEO_WARM_S + MATRIX_SECONDS + ROUND_OVERHEAD_S + 5), directory))
        if not self.runner.dry:
            self.sleep(VIDEO_WARM_S)
        launched = self.clock()
        hosts = []
        for host in self.present(sorted(self.topology["hunt"]["listeners"])):
            entry = self.entry(host)
            if entry is None:
                continue
            self.runner.ssh(host, listener_script(directory, [[video_probe_command(
                "v", entry, rung, MATRIX_SECONDS, f"{tag}-{host}")]], 0))
            hosts.append(host)
        if not self.runner.dry:
            self.sleep(MATRIX_SECONDS + 5)
            self.wait_for_probes(hosts, directory, 60)
        self.runner.ssh(source, "pkill -x ffmpeg; pkill -x ntcmp; true", check=False)
        self.record.setdefault("video", []).append(
            {"name": name, "rung": rung, "combination": combination_of(combination),
             "hosts": hosts, "from_unix": round(launched, 3)})
        self.period(name, "video", launched, launched + MATRIX_SECONDS, rung=rung,
                    combination=combination_of(combination))

    def session_names(self) -> list[str]:
        """Every directory the sessions and the matrix leave on the hosts."""
        return ([session["name"] for session in SESSIONS]
                + [name for combination in MATRIX for name in (
                    ([matrix_audio(combination)["name"]] if combination.get("audio", True)
                     else [])
                    + [f"{combination['name']}-V{rung.removeprefix('2160p')}"
                       for rung in VIDEO_RUNGS])])

    # ---- the end

    def stop_recorders(self, names: list[str]) -> None:
        """The watch of `names` stopped (SIGTERM: it writes what it holds),
        waited for up to 15 s, so that its file is whole when fetched (the
        referee's samplers are stopped by collect_samplers)."""
        self.runner.each(names, lambda name: self.runner.ssh(name, (
            f"cd {REMOTE} 2>/dev/null && P=$(cat watch.pid extra.pid 2>/dev/null); "
            "kill $P 2>/dev/null; n=0; while [ $n -lt 150 ]; do a=; for p in $P; do "
            "kill -0 $p 2>/dev/null && a=1; done; [ -z \"$a\" ] && break; sleep 0.1; "
            "n=$((n + 1)); done; true"),
            check=False, what="stop the watch"))

    def collect(self) -> None:
        """Everything the report reads, into target/stall-hunt/<run id>/."""
        self.survey_collect()
        nodes_dir = self.out() / "nodes"

        def records(name: str) -> None:
            self.runner.ssh(name, f"mkdir -p {REMOTE} && ( {SYSTEM_FACTS} ) "
                                  f">{REMOTE}/system.txt 2>&1; {KERNEL_LOG}", check=False,
                            what="the node's system facts >system.txt; journalctl -k "
                                 ">kernel.log")
            self.runner.fetch_directory(name, REMOTE, nodes_dir / name,
                                        exclude=tuple(self.session_names()))

        def sessions(name: str) -> None:
            for session in self.session_names():
                try:
                    self.runner.rsync_from(name, f"{REMOTE}/{session}/",
                                           self.out() / "sessions" / session / name)
                except RuntimeError:
                    pass  # a session that did not run left no directory

        failures = []
        try:
            self.collect_samplers()
        except Exception as error:  # noqa: BLE001 - the rest is collected all the same
            failures.append(f"samplers: {str(error)[-300:]}")
        try:
            self.stop_recorders(sorted(self.nodes))
        except RuntimeError as error:
            failures += str(error).splitlines()
        for names, function in ((sorted(self.nodes), records),
                                (self.purpose("source") + self.purpose("view"), sessions)):
            try:
                self.runner.each(names, function)
            except RuntimeError as error:
                failures += str(error).splitlines()
        self.hosts("last")
        if not self.runner.dry:
            artifacts = self.out() / "deploy"
            artifacts.mkdir(parents=True, exist_ok=True)
            for file in ("settings.json", "routes.json"):
                if (common.ARTIFACT_DIR / file).is_file():
                    shutil.copy2(common.ARTIFACT_DIR / file, artifacts / file)
            self.record["collected_unix"] = round(self.clock(), 3)
            self.record["collect_failures"] = failures
            self.save()
        if failures:
            self.runner.say("collect: " + "; ".join(failures))
        # Into the archive before the lab is deleted (a lab whose data was
        # not collected is a failed run): every node's journal since boot,
        # its API at the end and its samplers' files, and the lab's files,
        # by the deploy tool's collect; then this hunt's own records. The
        # run is collected when both are.
        began_utc = run_archive.utc_now()
        collected = self.deploy("collect", "--run-id", self.run_id)
        missing = {line.split(":", 1)[0] for line in failures}
        if self.archive("add-tree", self.run_id, relative(self.out()), "--as", "hunt",
                        "--kind", "raw",
                        "--description", "the stall hunt: hunt.json, every node's sampler, stall "
                        "and watch records, the sessions' probes, the route survey") != 0:
            missing.add(f"{relative(self.out())} (add-tree)")
        missing = sorted(missing | set(self.deploy_missing(collected, began_utc)))
        self.archive("collected", self.run_id,
                     *(["--complete"] if not missing else
                       ["--incomplete", *[item for name in missing for item in ("--missing", name)]]))

    def deploy_missing(self, status: int, began_utc: str) -> list[str]:
        """What the deploy tool's collect could not have, as it recorded it
        in the run (exit 0 or 1) after it began (`began_utc`), or the
        collect itself: refused, failed, or ended without marking the run
        (a Python crash and a refusal by message also exit 1, so the exit
        alone is not trusted)."""
        if self.runner.dry:
            return []
        if status not in (0, 1):
            return [f"deploy collect (exit {status})"]
        try:
            manifest = run_archive.RunArchive.open(self.run_id, record=False).manifest
        except run_archive.ArchiveError as error:
            return [f"deploy collect ({error})"]
        collected = manifest.get("collected") or {}
        if not collected.get("at_utc") or collected["at_utc"] < began_utc:
            return [f"deploy collect (exit {status}, the run not marked by it)"]
        return list(collected.get("missing") or [])

    def down(self) -> None:
        """The lab tool's `down`, then this checkout's deploy settings of
        the run are moved into the run's directory (a later lab must not
        render with them)."""
        status, output = self.lab("down", capture=True)
        if self.runner.dry:
            self.runner.say(f"mv {relative(common.ARTIFACT_DIR)}/{{settings,routes}}.json "
                            f"{relative(self.out())}/deploy/")
            return
        if status != 0 or STATE.exists():
            raise RuntimeError("lab down failed: run `linode-stall-hunt.py down` again (the "
                               "watchdog deletes the lab at its deadline). The lab tool "
                               "refuses while this run is not marked collected in the "
                               "archive: collect again, or `linode-small-mesh-lab.py down "
                               "--uncollected`, which records the run as failed")
        artifacts = self.out() / "deploy"
        artifacts.mkdir(parents=True, exist_ok=True)
        for file in ("settings.json", "routes.json"):
            if (common.ARTIFACT_DIR / file).is_file():
                shutil.move(str(common.ARTIFACT_DIR / file), str(artifacts / file))
        self.record["down_unix"] = round(self.clock(), 3)
        self.record["down_output"] = output.strip().splitlines()[-1:] if output else []
        self.save()

    # ---- the run

    def wait_until(self, minute: float, name: str) -> bool:
        """Wait for a step's minute. False when it is too close to the
        watchdog's deadline to start."""
        if self.runner.dry:
            self.runner.say(f"# minute {minute:g}: {name}")
            return True
        due = self.t0 + minute * 60
        deadline = self.record.get("watchdog_deadline_unix")
        while self.clock() < due:
            self.sleep(min(5.0, due - self.clock()))
        if deadline and name not in ("collect", "down") \
                and self.clock() > deadline - LATEST_START_BEFORE_DEADLINE_S:
            return False
        return True

    def run(self) -> int:
        """`up`, then the timeline. Whatever happens, collect and down."""
        failed = 0
        archived = False
        steps = timeline(self)
        try:
            if not self.runner.dry:
                self.runner.say(f"run {self.run_id}: records in {relative(self.out())}")
            self.archive_create()
            archived = True
            self.runner.say("# up") if self.runner.dry else None
            self.up()
            for step in steps[:-2]:
                failed += 0 if self.step(step) else 1
        except KeyboardInterrupt:
            self.interrupted = True
            self.runner.say("interrupted: collecting, then taking the lab down")
        except Exception as error:  # noqa: BLE001 - the lab is taken down whatever failed
            failed += 1
            self.runner.say(f"the run stopped: {error}")
        finally:
            if self.runner.dry or STATE.exists():
                for step in steps[-2:]:
                    try:
                        if self.interrupted or failed:
                            self.runner.say(f"# {step['name']}")
                            step["run"]()
                        elif not self.step(step):
                            failed += 1
                    except KeyboardInterrupt:
                        self.runner.say("interrupted again: going on to down")
                    except Exception as error:  # noqa: BLE001 - down must still run
                        failed += 1
                        self.runner.say(f"{step['name']} failed: {error}")
            if archived:
                self.archive("finish", self.run_id, "--status",
                             "failed" if failed or self.interrupted else "complete",
                             *(["--reason", "interrupted" if self.interrupted else
                                f"{failed} step(s) failed"] if failed or self.interrupted else []))
        return 1 if failed or self.interrupted else 0

    def step(self, step: dict) -> bool:
        if not self.wait_until(step["minute"], step["name"]):
            self.runner.say(f"{step['name']}: too close to the watchdog's deadline; left out")
            self.record["steps"].append({"name": step["name"], "minute": step["minute"],
                                         "ok": False, "error": "left out: watchdog deadline"})
            self.save()
            return False
        started = self.clock()
        error = None
        try:
            step["run"]()
        except KeyboardInterrupt:
            raise
        except Exception as caught:  # noqa: BLE001 - a failed step is recorded, the run goes on
            error = str(caught)[-500:]
            self.runner.say(f"{step['name']} failed: {error}")
        self.record["steps"].append({"name": step["name"], "minute": step["minute"],
                                     "from_unix": round(started, 3),
                                     "to_unix": round(self.clock(), 3), "ok": error is None,
                                     **({"error": error} if error else {})})
        self.save()
        return error is None


# ------------------------------------------------------------- the preflight

def mesh_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_AV_MESH_ROOT", str(common.WORKSPACE / "av-mesh")))


def contrib_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_AV_CONTRIB_ROOT",
                                       str(common.WORKSPACE / "av-contrib")))


def compare_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT",
                                       str(common.WORKSPACE / "needletail-compare")))


def clips_root() -> pathlib.Path:
    """The referee's 4K clips (a directory a rung) and their indexes."""
    return compare_root() / "target" / "compare-media" / CLIPS


def mesh_binary(name: str) -> pathlib.Path:
    return mesh_root() / "target" / "x86_64-unknown-linux-gnu" / "release" / name


def contrib_binary(name: str) -> pathlib.Path:
    return contrib_root() / "target" / "x86_64-unknown-linux-gnu" / "release" / name


def help_text(command: list[str]) -> str:
    """A command's --help, run here: no request, no token."""
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout + result.stderr).decode(errors="replace")


def preflight(topology: dict) -> list[str]:
    """Why a real run must not start, each found without a request to
    Linode: the paid step comes only after every one of these is clear."""
    problems = []
    if STATE.exists():
        problems.append(f"{relative(STATE)} exists: a hunt lab is up (collect and down first)")
    if common.DEFAULT_STATE.exists():
        problems.append(f"{relative(common.DEFAULT_STATE)} exists: another lab is deployed "
                        "from this checkout, and the deploy tool's settings are per checkout")
    for file in ("settings.json", "routes.json"):
        if (common.ARTIFACT_DIR / file).is_file():
            problems.append(f"{relative(common.ARTIFACT_DIR / file)} is left from another lab: "
                            "move it away (the hunt must not render with its settings)")
    if not ARCHIVE_TOOL.is_file():
        problems.append(f"{relative(ARCHIVE_TOOL)} is missing: the run's data must be kept in "
                        "the run archive")
    up_help = help_text([sys.executable, str(LAB_TOOL), "up", "--help"])
    for flag in LAB_UP_FLAGS:
        if flag not in up_help:
            problems.append(f"the lab tool's `up` has no {flag}: no lab is created without a "
                            "watchdog that collects before it deletes, and a budget")
    collect_help = help_text([sys.executable, str(DEPLOY_TOOL), "collect", "--help"])
    for flag in DEPLOY_COLLECT_FLAGS:
        if flag not in collect_help:
            problems.append(f"the deploy tool has no `collect {flag}`: the watchdog could not "
                            "collect the run before it deletes the lab")
    deploy_source = DEPLOY_TOOL.read_text()
    for setting in ("relay_video_gso", "relay_video_gro", "client_udp_gso"):
        if f'"{setting}"' not in deploy_source:
            problems.append(f"the deploy tool has no setting {setting}: the GSO/GRO matrix "
                            "cannot set it")
    for setting in DEPLOY_SETTINGS:
        if f'"{setting}"' not in deploy_source:
            problems.append(f"the deploy tool has no setting {setting} (D-hunt.md 2.9)")
    files = [mesh_binary("needletail-nodecheck"), mesh_binary("needletail-load"),
             mesh_binary("av-mesh"), contrib_binary("aep1-48k-probe"),
             compare_root() / "target" / "ntcmp",
             compare_root() / "target" / "compare-media" / "audio" / "audio.s24le"]
    files += [compare_root() / "target" / "compare-media" / "audio" / f"{cfg['index']}.index.json"
              for cfg in CONFIGS.values()]
    files += [clips_root() / f"{rung}.index.json" for rung in VIDEO_RUNGS]
    files += [clips_root() / rung / "video.h264" for rung in VIDEO_RUNGS]
    try:
        referee_samplers()
    except Exception as error:  # noqa: BLE001 - any failure to load them refuses the run
        problems.append(f"the referee's samplers do not load ({error}): the hunt samples "
                        "with them (NEEDLETAIL_COMPARE_ROOT, compare-cloudflare 472fb47 or later)")
    for path in files:
        if not path.is_file():
            problems.append(f"missing: {path}")
    if mesh_binary("av-mesh").is_file():
        text = help_text([str(mesh_binary("av-mesh")), "--help"])
        problems += [f"av-mesh has no {flag}: {MESH_FLAG_NEEDS[flag]}"
                     for flag in MESH_FLAGS if flag not in text]
    if mesh_binary("needletail-udp-client").is_file():
        text = help_text([str(mesh_binary("needletail-udp-client")), "--help"])
        if "--gro" not in text:
            problems.append("needletail-udp-client has no --gro: the matrix's listeners cannot "
                            "turn GRO off")
    if mesh_binary("needletail-nodecheck").is_file():
        text = help_text([str(mesh_binary("needletail-nodecheck")), "touch", "--help"])
        problems += [f"needletail-nodecheck touch has no {flag}: the touched edges' memory "
                     "would be given back as soon as it is written, minutes before the first "
                     "session, not just before it"
                     for flag in TOUCH_FLAGS if flag not in text]
    if contrib_binary("aep1-48k-probe").is_file():
        text = help_text([str(contrib_binary("aep1-48k-probe")), "send", "--help"])
        problems += [f"aep1-48k-probe send has no {flag}: the sender is without its journal"
                     for flag in SENDER_FLAGS if flag not in text]
    if hourly_usd(topology) > MAX_HOURLY_USD or len(topology["nodes"]) > MAX_NODES:
        problems.append("the topology is over the hunt's ceilings")
    return problems


# ----------------------------------------------------------------- commands

def default_run_id() -> str:
    """hunt<month><day><hour><minute> (UTC): R6's repeat on the same day
    gets a run id, an archive entry and a run directory of its own."""
    return "hunt" + dt.datetime.now(dt.timezone.utc).strftime("%m%d%H%M")


def print_plan(topology: dict) -> None:
    figures = cost(topology)
    by_plan: dict[str, int] = {}
    for node in topology["nodes"]:
        by_plan[node["type"]] = by_plan.get(node["type"], 0) + 1
    print(f"{figures['instances']} instances ("
          + ", ".join(f"{count} {plan}" for plan, count in sorted(by_plan.items()))
          + f"): ${figures['hourly_usd']:.3f} an hour; ceilings ${MAX_HOURLY_USD:.2f} an hour "
          f"and {MAX_NODES} nodes; watchdog {WATCHDOG_HOURS:g} h")
    print(f"{BILLED_HOURS} billed hours ${figures['planned_usd']:.2f}; with {REPLACEMENTS} "
          f"replacement instances ${figures['run_usd']:.2f}; one repeat at most: "
          f"${figures['with_one_repeat_usd']:.2f}")
    print(f"the sessions end by minute {sessions_end_minute():.1f} at worst, the GSO/GRO "
          f"matrix by minute {matrix_end_minute():.1f}; collect at {COLLECT_MINUTE}, down at "
          f"{DOWN_MINUTE}, the watchdog at minute {WATCHDOG_HOURS * 60:.0f}")
    regions = sorted({node["site"] for node in topology["nodes"]
                      if node["role"] in ("distributor", "edge")})
    print(f"the GSO/GRO matrix, the defaults first, on the mesh in {', '.join(regions)}, from "
          f"minute {MATRIX[0]['minute']}, each as the one before ends: "
          + "; ".join(describe(combination) for combination in MATRIX))
    print(f"  each: deploy push --set {' --set '.join(combination_settings(MATRIX[0]))} (as "
          f"set; it restarts every mesh node), the first audio session ({MATRIX_AUDIO}, "
          f"{MATRIX_SECONDS} s, needletail-udp-client --gro as set), 4K "
          + " and ".join(f"{rung.removeprefix('2160p')} Mbit/s" for rung in VIDEO_RUNGS)
          + f" ({MATRIX_SECONDS} s each), the referee's samplers running; at worst "
          f"{combination_seconds(MATRIX[0]) / 60:.1f} min")
    for combination in MATRIX:
        if combination.get("optional"):
            print(f"  {combination['name']}: 4K only, at worst "
                  f"{combination_seconds(combination) / 60:.1f} min; it runs only if it starts "
                  f"by minute {optional_latest_start_minute(combination):g} after `up` began "
                  f"(it, collect and down by {OPTIONAL_END_MINUTE}); no extra cost")
    print(f"  beside the referee's samplers: kernel.watchdog_thresh=1, tc -s qdisc and Udp "
          f"Snd/RcvbufErrors at 1 s, kernel stacks on a heartbeat {EXTRA_LATE_MS} ms late "
          "(extra.log); journalctl -k at collect (kernel.log)")


def command_plan(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.topology)
    topology = load_topology(path)
    print_plan(topology)
    hunt = Hunt(Runner(True, OUT / args.run_id), topology, path, args.run_id)
    hunt.run()


def command_run(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.topology)
    topology = load_topology(path)
    problems = preflight(topology)
    if args.dry:
        print_plan(topology)
        for problem in problems:
            print(f"# a real run would refuse: {problem}")
    elif problems:
        sys.exit("the hunt does not start (nothing was created):\n  " + "\n  ".join(problems))
    hunt = Hunt(Runner(args.dry, OUT / args.run_id), topology, path, args.run_id,
                args.budget_usd, args.spent_before_usd)
    if not args.dry:
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGHUP, lambda *_: hung_up(hunt.out()))
    sys.exit(hunt.run())


def interrupt(*_args) -> None:
    raise KeyboardInterrupt()


def hung_up(out: pathlib.Path) -> None:
    """The terminal is gone: this process's output and its tools' (they
    share its descriptors) go to a file of the run, so that collect and
    down do not fail on a write; then as SIGTERM."""
    out.mkdir(parents=True, exist_ok=True)
    log = os.open(out / "after-hangup.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(log)
    interrupt()


def existing_hunt(args: argparse.Namespace) -> Hunt:
    """The hunt of the lab that is up (or, dry, of the topology)."""
    if not STATE.exists():
        if not args.dry:
            sys.exit(f"{relative(STATE)} is missing: no hunt lab is up")
        path = TOPOLOGY
        topology = load_topology(path)
        return Hunt(Runner(True, OUT / default_run_id()), topology, path, default_run_id())
    state = json.loads(STATE.read_text())
    path = pathlib.Path(state["topology_file"])
    topology = load_topology(path)
    hunt = Hunt(Runner(args.dry, OUT / state["run_id"]), topology, path, state["run_id"])
    saved = hunt.out() / "hunt.json"
    if saved.is_file():
        hunt.record = json.loads(saved.read_text())
        hunt.survey_dirs = [ROOT / item for item in hunt.record.get("survey", [])]
    hunt.use_lab_nodes()
    return hunt


def command_collect(args: argparse.Namespace) -> None:
    existing_hunt(args).collect()


def command_down(args: argparse.Namespace) -> None:
    existing_hunt(args).down()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="the cost, the timeline and every command; no lab, "
                                       "no token, no request")
    plan.add_argument("--topology", default=str(TOPOLOGY))
    plan.add_argument("--run-id", default=default_run_id())
    plan.set_defaults(handler=command_plan)
    run = sub.add_parser("run", help="bring the lab up, run the timeline, collect, take it down")
    run.add_argument("--topology", default=str(TOPOLOGY))
    run.add_argument("--run-id", default=default_run_id(),
                     help="the lab's run id and tag (default hunt<month><day><hour><minute>, UTC)")
    run.add_argument("--budget-usd", type=float, default=None,
                     help="the budget of the work, passed to the lab tool's `up`: it refuses "
                          "when this lab to its watchdog's deadline, what was spent before and "
                          "the other live labs pass it")
    run.add_argument("--spent-before-usd", type=float, default=None,
                     help="what earlier labs of the work cost (default: the lab tool's ledger)")
    run.add_argument("--dry", action="store_true",
                     help="print every command and run none")
    run.set_defaults(handler=command_run)
    for name, handler, text in (
            ("collect", command_collect, "fetch the records of the lab that is up"),
            ("down", command_down, "take the lab down through the lab tool")):
        item = sub.add_parser(name, help=text)
        item.add_argument("--dry", action="store_true")
        item.set_defaults(handler=handler)
    args = parser.parse_args()
    if args.command == "run" and not args.dry and args.budget_usd is None:
        parser.error("run needs --budget-usd (or --dry)")
    os.environ.update(tool_environment())
    args.handler(args)


if __name__ == "__main__":
    main()
