#!/usr/bin/env python3
"""The first-session stall hunt's paid lab: bring it up, run its timeline,
collect its records, take it down.

    linode-stall-hunt.py plan
    linode-stall-hunt.py run --budget-usd B [--spent-before-usd S] [--run-id ID] [--dry]
    linode-stall-hunt.py collect [--dry]
    linode-stall-hunt.py down [--dry]

Design: docs/plans/resilient-mesh/D-hunt.md, section 7 (as revised after the
wave 9 run). The lab is deploy/low-latency/topology-hunt.json: 20
dedicated-CPU instances, $1.296 an hour, two billed hours ($2.59; $2.70 with
two replacement instances). Its questions, in order: on a fresh deploy of
the build with the fan-out fix, does any edge still stop for 100 ms or more
in the referee's first 1 ms Opus session, with UDP clients arriving at every
edge; for each stop that remains, was the audio class thread runnable and
not run (the hypervisor or the guest kernel) or blocked in our code (and on
which wait); and do the arena warm-up, first touch of the guest's memory,
the codec of the first session and listener churn change it.
scripts/compare/hunt-report.py reads what `collect` leaves in
target/stall-hunt/<run id>/ and applies the decision rules, which are fixed
before the run.

This tool creates and deletes nothing by itself. Every instance is created
and deleted by scripts/linode-small-mesh-lab.py, called by command line:
`up` with a watchdog of WATCHDOG_HOURS and the budget given here (the lab
tool refuses a plan that is not dedicated, tags every instance and arms the
watchdog before the first create), `remove` and `grow` for the replacement,
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
request, that the lab tool has the watchdog and the budget on `up`, that
the binaries carry what the hunt needs (the stall journal, the arena
warm-up's switch, needletail-nodecheck touch --hold, the sender's lateness
journal) and that no other lab's deploy settings are in this checkout; it
refuses to start otherwise, before anything is billed.

Timeline, minutes after `up` returns:
   0  `lab hosts`; on every node, first command: needletail-nodecheck watch;
      push (the fresh deploy); on every mesh node the 1 s sampler
      (scripts/hunt-sampler.sh); the route survey; media to the hosts
  15  the touched edges (half of them) touch all their free memory and hold it
  18  the referee's 20 s burn on the mesh nodes
  19.5 the touched edges give their memory back
  20  S1 Opus Custom 1 ms, 70 s: the first session, UDP clients arriving at
      every edge; then the one /api/hops pull (London's four large rings)
  24  S2 PCM 1 ms, 70 s
  28  S3 listener churn on every edge: 3 rounds of 50 s, 12 s apart
  35  the edge that was not run most is replaced (remove, grow, hosts,
      watch, push --nodes, sampler)
  44  every forwarder and edge restarted at once (deploy restart --roles)
  46  S4 PCM 1 ms, 70 s (the repeat, PCM first)   50  S5 Opus Custom 1 ms, 70 s
  54  S6 Opus Custom 1 ms, 420 s   63  S7 PCM 1 ms, 420 s   72  S8 Opus 2.5 ms, 300 s
  82  collect (samplers and watches stopped first); `lab hosts` again
  88  down (inside the second billed hour; the watchdog is at 1.9 h)
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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
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
MAX_HOURLY_USD = 1.50
MAX_NODES = 24
WATCHDOG_HOURS = 1.9
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
# runs it; listener churn; the repeat after every forwarder and edge was
# restarted, PCM first; the steady probes of 3.1 (420 s, the referee's
# change probes are 360 s or more) and the 2.5 ms lane.
SESSIONS = (
    {"name": "S1", "minute": 20, "config": "opus-1", "seconds": 70, "role": "first"},
    {"name": "S2", "minute": 24, "config": "pcm-1", "seconds": 70, "role": "second"},
    {"name": "S3", "minute": 28, "config": "opus-1", "seconds": 50, "role": "churn"},
    {"name": "S4", "minute": 46, "config": "pcm-1", "seconds": 70, "role": "repeat-first"},
    {"name": "S5", "minute": 50, "config": "opus-1", "seconds": 70, "role": "repeat-second"},
    {"name": "S6", "minute": 54, "config": "opus-1", "seconds": 420, "role": "steady"},
    {"name": "S7", "minute": 63, "config": "pcm-1", "seconds": 420, "role": "steady"},
    {"name": "S8", "minute": 72, "config": "opus-2.5", "seconds": 300, "role": "steady"},
)
# Listener churn (the churn session): every listener of every view host
# joins at once, for a round, then leaves; the next round 12 s later.
CHURN_ROUNDS = 3
CHURN_GAP_S = 12
TOUCH_MINUTE = 15
BURN_MINUTE = 18
RELEASE_MINUTE = 19.5
REPLACE_MINUTE = 35
RESTART_MINUTE = 44
COLLECT_MINUTE = 82
DOWN_MINUTE = 88
BURN_SECONDS = 20
# Listeners start this long after the sender's START, as the referee's do.
LISTEN_AFTER_START_S = 12
# The touched edges touch MemAvailable less this, at this rate, and hold it.
TOUCH_LEAVE_MB = 384
TOUCH_RATE_MB_S = 1024
STALL_JOURNAL_CAPACITY = 65_536
HOP_JOURNAL_LARGE = 2_097_152
# An edge's "not run" milliseconds count the sampler's seconds in which its
# audio class thread waited runnable this long or longer.
NOT_RUN_SECOND_MS = 50.0
# The sampler (one a mesh node) and how it finds the service and its class
# thread: {role: (process, class thread's name, HTTP port, slow path)}.
SAMPLER = SCRIPTS / "hunt-sampler.sh"
SAMPLED = {"contributor": ("av-contrib", "audio-hot-0", 19443, "/api/pacing"),
           "distributor": ("av-mesh", "needletail-fast", HTTP_PORT, ""),
           "edge": ("av-mesh", "needletail-fast", HTTP_PORT, "")}
# av-mesh's switch of the audio class thread's arena warm-up (MB; 0: none):
# the no_arena_warm arm runs without it.
ARENA_FLAG = "--relay-class-arena-warm-mb"
# What a real run needs of the tools and binaries it calls (preflight).
LAB_UP_FLAGS = ("--watchdog-hours", "--budget-usd", "--optional-sites", "--run-id")
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
    "echo meminfo: $(grep -E 'MemTotal|MemAvailable|AnonHugePages' /proc/meminfo | tr '\\n' ' '); "
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


def listening_seconds(session: dict) -> int:
    """How long a session's listeners run: one round, or the churn's
    rounds and the gaps between them."""
    if session["role"] == "churn":
        return CHURN_ROUNDS * session["seconds"] + (CHURN_ROUNDS - 1) * CHURN_GAP_S
    return session["seconds"]


def session_seconds(session: dict) -> float:
    """The longest a session takes: the contributor's restart, the wait for
    the 64 s boundary (8 to 72 s), the 12 s before the listeners, their
    length, and the journal pull."""
    return 5 + 72 + LISTEN_AFTER_START_S + listening_seconds(session) + 30


def sessions_end_minute() -> float:
    """When the last session has ended at worst, each starting at its
    minute or when the one before it ended, whichever is later."""
    end = 0.0
    for session in SESSIONS:
        end = max(end, session["minute"]) + session_seconds(session) / 60
    return round(end, 2)


def timeline(hunt: "Hunt") -> list[dict]:
    """The steps of a run in order: {"minute", "name", "run"}; a step never
    starts before its minute (counted from the return of `up`). Nothing
    but the deploy, the watch and the sampler comes before the first
    session's listeners except what the referee's lab did (its idle and its
    burn) and the touched arm's touch."""
    steps = [
        {"minute": 0, "name": "hosts", "run": lambda: hunt.hosts("first")},
        {"minute": 0, "name": "watch", "run": hunt.watch},
        {"minute": 0, "name": "push", "run": hunt.push},
        {"minute": 0, "name": "sampler", "run": hunt.sample},
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
    steps.append({"minute": RESTART_MINUTE, "name": "restart every forwarder and edge",
                  "run": hunt.restart_mesh})
    steps += [step for step in sessions if step["minute"] > RESTART_MINUTE]
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


def udp_probe_command(name: str, entry: str, config: str, seconds: int, tag: str) -> str:
    """One full UDP probe, the referee's: needletail-udp-client with its
    arrivals and parts, then `ntcmp udp-ingest` for the per-unit records."""
    cfg = CONFIGS[config]
    return (f"( date +%s.%N >{name}.launch; /usr/local/bin/needletail-udp-client "
            f"--entry https://{entry}:{HTTP_PORT} --stream {cfg['stream']} "
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


def load_command(name: str, listeners: int, sample: int, entries: list[str], edges: list[str],
                 config: str, seconds: int, tag: str) -> str:
    """The crowd: needletail-load over every edge of the site, each
    listener assigned as a client is, so that UDP clients arrive at each."""
    return ("( ulimit -n 1048576; needletail-load "
            f"--edges {','.join(f'{edge}:{CLIENT_UDP_PORT}' for edge in edges)} "
            f"--entry {','.join(f'https://{entry}:{HTTP_PORT}' for entry in entries)} "
            f"--tls-ca {CA} --server-name {SERVER_NAME} --stream {CONFIGS[config]['stream']} "
            f"--listeners {listeners} --ramp {listeners}@0 --duration {seconds} "
            f"--sample {min(sample, listeners)} --out {name} --label {tag} --quiet "
            f">{name}.out 2>{name}.err )")


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
    """The touched arm: MemAvailable less TOUCH_LEAVE_MB, touched page by
    page by needletail-nodecheck touch and held (--hold) until
    release_command, so that the guest's free memory has been written once
    when the first session begins, and is not given back early. Its mark
    and its pid are kept beside it."""
    return (f"mkdir -p {REMOTE} && cd {REMOTE} && "
            "MB=$(( $(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)"
            f" - {TOUCH_LEAVE_MB} )) && echo \"touch $(date +%s.%N) $MB\" >>touch-marks.txt && "
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


def sampler_command(role: str, address: str) -> str:
    """The node's 1 s sampler (scripts/hunt-sampler.sh, sent beside it),
    started detached; an earlier one of this node is stopped first. It
    needs curl and jq (the deploy's bootstrap installs jq; curl is in the
    image, else installed here)."""
    process, comm, port, slow = SAMPLED[role]
    return (f"mkdir -p {REMOTE} && cd {REMOTE} && "
            "{ command -v curl >/dev/null || DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y -q curl >/dev/null; } && "
            "{ [ -f sampler.pid ] && kill $(cat sampler.pid) 2>/dev/null; true; } && "
            f"(setsid nohup sh {REMOTE}/sampler.sh {REMOTE} {process} {comm} "
            f"https://{address}:{port} {slow or chr(39) * 2} >sampler.log 2>&1 </dev/null & "
            "echo $! >sampler.pid)")


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
                              "--code", str(ROOT), "--code", str(mesh_root()),
                              "--code", str(contrib_root()),
                              "--topology", relative(self.topology_path))
        if status != 0:
            raise RuntimeError("the run archive refused the run: nothing was created")

    # ---- the steps


    # ---- the steps

    def up(self) -> None:
        args = ["up", "--topology", relative(self.topology_path), "--run-id", self.run_id,
                "--optional-sites", "sydney", "--watchdog-hours", f"{WATCHDOG_HOURS:g}",
                "--budget-usd", f"{self.budget_usd:g}" if self.budget_usd is not None
                else "<budget>"]
        if self.spent_before_usd is not None:
            args += ["--spent-before-usd", f"{self.spent_before_usd:g}"]
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

    def sample(self, names: list[str] | None = None) -> None:
        """The 1 s sampler on every mesh node (or on `names`): the class
        thread's schedstat and switches, every thread's policy and CPU,
        vmstat, MemFree and the fast plane's counters each second, the
        stall journal every ten (D-hunt.md 7)."""
        names = self.mesh if names is None else names

        def start(name: str) -> None:
            self.runner.send_file(name, SAMPLER, f"{REMOTE}/sampler.sh", mode="644")
            self.runner.ssh(name, sampler_command(self.nodes[name]["role"],
                                                  self.nodes[name]["public_ipv4"]))

        self.runner.each(names, start)

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

        def source(name: str) -> None:
            self.runner.ssh(name, f"mkdir -p {REMOTE} {REMOTE_MEDIA}/audio")
            self.runner.rsync_to(name, [media / "audio.s24le"], f"{REMOTE_MEDIA}/audio/")

        def view(name: str) -> None:
            self.runner.ssh(name, f"mkdir -p {REMOTE} {REMOTE_MEDIA}/index")
            self.runner.rsync_to(name, indexes, f"{REMOTE_MEDIA}/index/")
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
                  prefix: str) -> list[str]:
        """A view host's listeners of one round: the UDP probes through the
        site's entry, one LL-HLS probe at every edge of the site, and the
        crowd over every edge of the site."""
        site_entry = self.entry(host)
        edges = self.site_edges(host)
        commands = [udp_probe_command(f"{prefix}uc-{number}", site_entry, config, seconds, tag)
                    for number in range(1, plan.get("udp", 0) + 1)]
        for edge in edges:
            for number in range(1, plan.get("hls_each_edge", 0) + 1):
                commands.append(hls_probe_command(f"{prefix}hls-{edge}-{number}",
                                                  self.nodes[edge]["public_ipv4"], config,
                                                  seconds, tag))
        if plan.get("load"):
            entries = [found for found in (site_entry, self.entry(host, "b")) if found]
            commands.append(load_command(f"{prefix}load", plan["load"], plan.get("sample", 8),
                                         entries, [self.nodes[edge]["public_ipv4"]
                                                   for edge in edges], config, seconds, tag))
        return commands

    def session(self, session: dict) -> None:
        name, config, seconds = session["name"], session["config"], session["seconds"]
        directory = f"{REMOTE}/{name}"
        tag = f"hunt-{self.run_id}-{name}"
        contributor = self.names("contributor")[0]
        source = self.purpose("source")[0]
        rounds = CHURN_ROUNDS if session["role"] == "churn" else 1
        entry = {"name": name, "role": session["role"], "config": config,
                 "stream": CONFIGS[config]["stream"], "seconds": seconds, "rounds": rounds,
                 "hosts": {}}
        # As the referee's Audio.start: the contributor restarted, then the
        # sender at the next 64 s boundary.
        self.runner.ssh(contributor, "systemctl restart needletail-contrib.service; sleep 3; "
                                     "systemctl is-active needletail-contrib.service")
        printed = self.runner.ssh(source, sender_command(
            config, self.nodes[contributor]["vlan_ipv4"],
            listening_seconds(session) + LISTEN_AFTER_START_S + 10, directory))
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
                               f"r{index}-" if rounds > 1 else "")
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
            # Once in the run, after the first session has ended, never
            # inside one (a pull allocates 50 to 80 MB on the node); only
            # the session's own stream since its start.
            entry["hops_pull"] = self.pull_hops(name, CONFIGS[config]["stream"],
                                                entry.get("start_unix"))
        self.record["sessions"].append(entry)
        self.period(name, "session", entry.get("probes_from_unix", launched),
                    entry.get("probes_to_unix", launched + listening_seconds(session)),
                    config=config, role=session["role"])

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

    def not_run_so_far(self) -> dict[str, float]:
        """Each edge's "not run" milliseconds so far: its watch's stopped
        ticks, and the sampler's seconds in which the audio class thread
        waited runnable NOT_RUN_SECOND_MS or more."""
        out = {}
        for name in self.names("edge"):
            try:
                watch = self.runner.ssh(name, f"cat {REMOTE}/watch.ndjson", timeout=120)
                rows = self.runner.ssh(name, (
                    f"jq -c '[.t_us, .pid, .tid, (.schedstat // [])[1]]' {REMOTE}/sampler.ndjson"),
                    timeout=120)
            except Exception:  # noqa: BLE001 - a node that does not answer has no figure
                continue
            events = stall_records.dedupe(stall_records.parse_ndjson(watch))
            delays = [json.loads(line) for line in rows.splitlines() if line.strip()]
            out[name] = round(stall_records.stopped_ms(events)
                              + stall_records.run_delay_ms(delays, NOT_RUN_SECOND_MS), 3)
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
        instance keeps), and its sampler. The lab's nodes are read again
        whatever happened."""
        try:
            self.replace_instance(name, not_run_ms)
        finally:
            if not self.runner.dry and STATE.exists():
                self.use_lab_nodes()

    def replace_instance(self, name: str, not_run_ms: float | None) -> None:
        dry = self.runner.dry
        before = None if dry else self.nodes[name].get("host_uuid")
        address = None if dry else self.nodes[name].get("public_ipv4")
        # The instance's own records (its watch and sampler above all) go
        # with it: they are fetched first, beside the new instance's.
        try:
            self.stop_recorders([name])
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
                failures.append(f"{name}: no sampler: {error}")
        elif not dry:
            failures.append(f"push --nodes {name} failed: the new instance runs the watch only")
        if not dry:
            self.save()
        if failures:
            raise RuntimeError("; ".join(failures))

    def restart_mesh(self) -> None:
        """The repeat's start: every forwarder and edge restarted at once
        (new processes on guests that have run a session; the replaced
        edge on a new guest); its first session is PCM."""
        started = self.clock()
        status = self.deploy("restart", "--roles", "distributor,edge")
        self.period("restart", "restart", started, self.clock(), ok=status == 0)
        if status != 0:
            raise RuntimeError("deploy restart --roles distributor,edge failed")

    # ---- the end

    def stop_recorders(self, names: list[str]) -> None:
        """The sampler and the watch of `names` stopped (SIGTERM: the watch
        writes what it holds), so that their files are whole when fetched."""
        self.runner.each(names, lambda name: self.runner.ssh(name, (
            f"cd {REMOTE} 2>/dev/null && for f in sampler.pid watch.pid; do "
            "[ -f $f ] && kill $(cat $f) 2>/dev/null; done; sleep 1; true"),
            check=False, what="stop the sampler and the watch"))

    def collect(self) -> None:
        """Everything the report reads, into target/stall-hunt/<run id>/."""
        self.survey_collect()
        nodes_dir = self.out() / "nodes"

        def records(name: str) -> None:
            self.runner.ssh(name, f"mkdir -p {REMOTE} && ( {SYSTEM_FACTS} ) "
                                  f">{REMOTE}/system.txt 2>&1", check=False,
                            what="the node's system facts >system.txt")
            self.runner.fetch_directory(name, REMOTE, nodes_dir / name,
                                        exclude=tuple(session["name"] for session in SESSIONS))

        def sessions(name: str) -> None:
            for session in SESSIONS:
                try:
                    self.runner.rsync_from(name, f"{REMOTE}/{session['name']}/",
                                           self.out() / "sessions" / session["name"] / name)
                except RuntimeError:
                    pass  # a session that did not run left no directory

        failures = []
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
        # not collected is a failed run).
        missing = sorted({line.split(":", 1)[0] for line in failures})
        self.archive("add-tree", self.run_id, relative(self.out()), "--kind", "raw",
                     "--description", "the stall hunt: hunt.json, every node's sampler, stall "
                     "and watch records, the sessions' probes, the route survey")
        self.archive("collected", self.run_id,
                     *(["--complete"] if not missing else
                       ["--incomplete", *[item for name in missing for item in ("--missing", name)]]))

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
                            "watchdog and a budget")
    deploy_source = DEPLOY_TOOL.read_text()
    for setting in DEPLOY_SETTINGS:
        if f'"{setting}"' not in deploy_source:
            problems.append(f"the deploy tool has no setting {setting} (D-hunt.md 2.9)")
    files = [mesh_binary("needletail-nodecheck"), mesh_binary("needletail-load"),
             mesh_binary("av-mesh"), contrib_binary("aep1-48k-probe"),
             compare_root() / "target" / "ntcmp",
             compare_root() / "target" / "compare-media" / "audio" / "audio.s24le"]
    files += [compare_root() / "target" / "compare-media" / "audio" / f"{cfg['index']}.index.json"
              for cfg in CONFIGS.values()]
    for path in files:
        if not path.is_file():
            problems.append(f"missing: {path}")
    if mesh_binary("av-mesh").is_file():
        text = help_text([str(mesh_binary("av-mesh")), "--help"])
        problems += [f"av-mesh has no {flag}: {MESH_FLAG_NEEDS[flag]}"
                     for flag in MESH_FLAGS if flag not in text]
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
    print(f"the sessions end by minute {sessions_end_minute():.1f} at worst; collect at "
          f"{COLLECT_MINUTE}, down at {DOWN_MINUTE}, the watchdog at minute "
          f"{WATCHDOG_HOURS * 60:.0f}")


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
