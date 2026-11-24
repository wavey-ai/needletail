#!/usr/bin/env python3
"""The first-session stall hunt's paid lab: bring it up, run its timeline,
collect its records, take it down.

    linode-stall-hunt.py plan
    linode-stall-hunt.py run --budget-usd B [--spent-before-usd S] [--run-id ID] [--dry]
    linode-stall-hunt.py collect [--dry]
    linode-stall-hunt.py down [--dry]

Design: docs/plans/resilient-mesh/D-hunt.md, section 7. The lab is
deploy/low-latency/topology-hunt.json: 21 dedicated-CPU instances, $1.350 an
hour, two billed hours ($2.70; $2.81 with two replacement instances; one
repeat at most, $5.62 in all). The question it asks: which instances stop,
whether they share a physical host, whether a check at boot finds them,
whether a replacement is clean, and whether first touch of memory or
transparent huge pages has a part in it. scripts/compare/hunt-report.py
reads what `collect` leaves in target/stall-hunt/<run id>/ and applies the
decision rules, which are fixed before the run (7.5).

This tool creates and deletes nothing by itself. Every instance is created
and deleted by scripts/linode-small-mesh-lab.py, called by command line:
`up` with a watchdog of WATCHDOG_HOURS and the budget given here (the lab
tool refuses a plan that is not dedicated, tags every instance and arms the
watchdog before the first create), `remove` and `grow` for the one
replacement, `down` at the end, whatever happened before. The lab has its
own state file (target/linode-qualification/hunt-lab.json) and its own
ceilings (NEEDLETAIL_LINODE_MAX_HOURLY_USD=1.50, NEEDLETAIL_LINODE_MAX_NODES=24),
set for every call. The Linode token is the lab tool's to read; this tool
never reads, prints or stores it. Services are installed by
scripts/linode-small-mesh-deploy.py `push`, the survey is
scripts/linode-route-survey.py; both by command line too.

`plan` prints the cost, the timeline and every command from the topology
alone: no lab, no token, no request. `run --dry` prints the same commands
through the code of a real run. A real `run` first checks, without a
request, that the lab tool has the watchdog and the budget on `up`, that
the binaries carry the instruments (the stall journal, needletail-nodecheck,
the sender's lateness journal) and that no other lab's deploy settings are
in this checkout; it refuses to start otherwise, before anything is billed.

Timeline, minutes after `up` returns (7.3):
   0  `lab hosts`; on every node, first command: needletail-nodecheck watch;
      warm on the warmed arm; push; the survey starts; media to the hosts
   7, 12, 17  soak rounds: echo pairs inside a site, 60 s at 2,000 a second
  18  the referee's 20 s burn on the mesh nodes
  20  the bare nodes touch memory (fresh, 25: without huge pages, 30: again)
  20  S1 Opus Custom 1 ms, 70 s    23  S2 PCM 1 ms    26  S3 Opus 2.5 ms
  33  warm on every node not yet warmed
  35  the edge or bare node that was stopped most is replaced (remove,
      grow, hosts, push --nodes); if the worst of all is a distributor it
      is replaced at 76 instead and only soaked
  40  S4 Opus Custom 1 ms, 70 s    45  S5 the same, steady, 600 s
  57  S6 PCM 1 ms, 600 s           70  S7 Opus 2.5 ms, 300 s
  82  collect (the deploy tool's collect too: every node's journal since
      boot, its API and samplers into the run archive); `lab hosts` again
  88  down (inside the second billed hour; the watchdog is at 1.9 h)
A session is the referee's: the contributor restarted, the AEP1 sender at
the next 64 s boundary (with --lateness-journal), listeners from 12 s after
it; then one /api/hops pull (since the session's start) from the nodes with
an enlarged ring. A step that fails is recorded and the run goes on;
`collect` and `down` always run, also after an interrupt, SIGTERM or
SIGHUP (after a hangup the output goes to after-hangup.log in the run's
directory). Run it in tmux all the same: the lab tool's watchdog deletes
the lab at its deadline whatever happens here, but an uncollected run is a
failed run.
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
MAX_HOURLY_USD = 1.50
MAX_NODES = 24
WATCHDOG_HOURS = 1.9
BILLED_HOURS = 2
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
ECHO_PORT = 27900
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
SESSIONS = (
    {"name": "S1", "minute": 20, "config": "opus-1", "seconds": 70},
    {"name": "S2", "minute": 23, "config": "pcm-1", "seconds": 70},
    {"name": "S3", "minute": 26, "config": "opus-2.5", "seconds": 70},
    {"name": "S4", "minute": 40, "config": "opus-1", "seconds": 70},
    {"name": "S5", "minute": 45, "config": "opus-1", "seconds": 600},
    {"name": "S6", "minute": 57, "config": "pcm-1", "seconds": 600},
    {"name": "S7", "minute": 70, "config": "opus-2.5", "seconds": 300},
)
SOAK_MINUTES = (7, 12, 17)
COLLECT_MINUTE = 82
DOWN_MINUTE = 88
SOAK_SECONDS = 60
ECHO_PPS = 2000
BURN_SECONDS = 20
# Listeners start this long after the sender's START, as the referee's do.
LISTEN_AFTER_START_S = 12
TOUCH_MB = 400
TOUCH_RATE_MB_S = 2
STALL_JOURNAL_CAPACITY = 65_536
HOP_JOURNAL_LARGE = 2_097_152
POLL_S = 5.0
# What is kept of /api/mesh/local at every poll.
MESH_LOCAL_KEYS = ("stall", "memory", "node_warm", "relay_flows", "fast_plane", "build")
# What a real run needs of the tools and binaries it calls (preflight).
LAB_UP_FLAGS = ("--watchdog-hours", "--budget-usd", "--optional-sites", "--run-id")
DEPLOY_SETTINGS = ("node_overrides", "stall_journal_capacity", "mlock")
MESH_FLAGS = ("--stall-journal-capacity", "--mlock")
SENDER_FLAGS = ("--lateness-journal",)
# The memory programme holds each region until it is ended (7.3: the first
# 400 MB is held from minute 20 to 30, so that the second is fresh memory).
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


def session_seconds(session: dict) -> float:
    """The longest a session takes: the contributor's restart, the wait for
    the 64 s boundary (8 to 72 s), the 12 s before the listeners, their
    length, and the journal pull."""
    return 5 + 72 + LISTEN_AFTER_START_S + session["seconds"] + 30


def sessions_end_minute() -> float:
    """When the last session has ended at worst, each starting at its
    minute or when the one before it ended, whichever is later."""
    end = 0.0
    for session in SESSIONS:
        end = max(end, session["minute"]) + session_seconds(session) / 60
    return round(end, 2)


def timeline(hunt: "Hunt") -> list[dict]:
    """The steps of a run in order: {"minute", "name", "run"}; a step never
    starts before its minute (counted from the return of `up`)."""
    steps = [
        {"minute": 0, "name": "hosts", "run": lambda: hunt.hosts("first")},
        {"minute": 0, "name": "watch", "run": hunt.watch},
        {"minute": 0, "name": "warm the warmed arm",
         "run": lambda: hunt.warm(hunt.warmed, "boot")},
        {"minute": 0, "name": "push", "run": hunt.push},
        {"minute": 0, "name": "survey", "run": hunt.survey_start},
        {"minute": 0, "name": "media", "run": hunt.media},
    ]
    for index, minute in enumerate(SOAK_MINUTES, 1):
        steps.append({"minute": minute, "name": f"soak-{index}",
                      "run": lambda index=index: hunt.soak(f"soak-{index}", hunt.soak_pairs)})
    steps.append({"minute": 18, "name": "burn", "run": hunt.burn})
    steps.append({"minute": 20, "name": "touch", "run": hunt.touch})
    for session in SESSIONS[:3]:
        steps.append({"minute": session["minute"], "name": session["name"],
                      "run": lambda session=session: hunt.session(session)})
    steps.append({"minute": 33, "name": "warm the rest",
                  "run": lambda: hunt.warm(hunt.not_warmed, "late")})
    steps.append({"minute": 35, "name": "replace the worst edge or bare node",
                  "run": hunt.replace_worst})
    for session in SESSIONS[3:]:
        steps.append({"minute": session["minute"], "name": session["name"],
                      "run": lambda session=session: hunt.session(session)})
    steps.append({"minute": 76, "name": "replace the worst distributor",
                  "run": hunt.replace_distributor})
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


def udp_probe_command(number: int, entry: str, config: str, seconds: int, tag: str) -> str:
    """One full UDP probe, the referee's: needletail-udp-client with its
    arrivals and parts, then `ntcmp udp-ingest` for the per-unit records."""
    cfg = CONFIGS[config]
    name = f"uc-{number}"
    return (f"( date +%s.%N >{name}.launch; /usr/local/bin/needletail-udp-client "
            f"--entry https://{entry}:{HTTP_PORT} --stream {cfg['stream']} "
            f"--tls-ca {CA} --server-name {SERVER_NAME} --duration-seconds {seconds} "
            f"--session {tag}-uc-{number} --arrivals {name}.arrivals.csv "
            f"--write-parts {name}.parts.bin >{name}.uc.json 2>{name}.uc.err; "
            f"ntcmp udp-ingest --arrivals {name}.arrivals.csv --parts {name}.parts.bin "
            f"--started $(cat {name}.launch) "
            f"--audio-index {REMOTE_MEDIA}/index/{cfg['index']}.index.json --skip-seconds 5 "
            f"--lane nt-uc --label {tag} --client {name}.uc.json --report {name}.probe.json "
            f"--frames {name}.probe.csv >>{name}.ingest.log 2>&1; "
            f"rm -f {name}.parts.bin {name}.launch )")


def hls_probe_command(number: int, entry: str, config: str, seconds: int, tag: str) -> str:
    """One LL-HLS probe, the referee's HTTP/3 reader with 40 requests in
    flight."""
    cfg = CONFIGS[config]
    name = f"hls-{number}"
    return (f"ntcmp hls --lane nt-h3 --mode chase --window 40 --http3 "
            f"--url 'https://{entry}:{HTTP_PORT}/live/{cfg['stream']}/master.m3u8"
            f"?sid={tag}-hls-{number}' --ca {CA} --server-name {SERVER_NAME} "
            f"--audio-index {REMOTE_MEDIA}/index/{cfg['index']}.index.json "
            f"--duration {seconds} --skip-seconds 5 --report {name}.probe.json --label {tag} "
            f"--count 1 --frames {name}.probe.csv >{name}.log 2>&1")


def load_command(listeners: int, sample: int, entries: list[str], edges: list[str],
                 config: str, seconds: int, tag: str) -> str:
    """The crowd: needletail-load, each listener assigned as a client is."""
    return ("( ulimit -n 1048576; needletail-load "
            f"--edges {','.join(f'{edge}:{CLIENT_UDP_PORT}' for edge in edges)} "
            f"--entry {','.join(f'https://{entry}:{HTTP_PORT}' for entry in entries)} "
            f"--tls-ca {CA} --server-name {SERVER_NAME} --stream {CONFIGS[config]['stream']} "
            f"--listeners {listeners} --ramp {listeners}@0 --duration {seconds} "
            f"--sample {min(sample, listeners)} --out load --label {tag} --quiet "
            ">load.out 2>load.err )")


def listener_script(directory: str, commands: list[str]) -> str:
    """A host's probes of one session: all at once, in the background; the
    file `done` appears when the last has ended."""
    body = " &\n".join(commands)
    return (f"mkdir -p {directory} && cd {directory} && rm -f done && "
            f"cat >run.sh <<'RUN'\n{body} &\nwait\ntouch done\nRUN\n"
            "(setsid nohup sh run.sh >run.log 2>&1 </dev/null &)")


def touch_body() -> str:
    """The memory programme of a bare node (7.3), minutes 20 to 33: 400 MB
    of fresh memory touched at 2 MB a second, then held; five minutes later
    a fresh 400 MB without huge pages (fresh because the first is still
    held); five minutes later the first is freed (its process ends) and
    400 MB is taken again, which the guest serves from the pages just
    freed. `touch --hold` keeps its mapping after the last page until it
    is sent SIGTERM. The marks give the report its periods."""
    touch = f"{NODECHECK} touch --mb {TOUCH_MB} --rate-mb-s {TOUCH_RATE_MB_S} --hold"
    return (
        "mark() { echo \"$1 $(date +%s.%N)\" >>touch-marks.txt; }\n"
        f"mark fresh; {touch} --thp always --out touch-fresh.ndjson & A=$!\n"
        "sleep 300\n"
        f"mark thp-never; {touch} --thp never --out touch-thp-never.ndjson & B=$!\n"
        "sleep 300\n"
        "kill $A 2>/dev/null; wait $A 2>/dev/null\n"
        f"mark again; {touch} --thp always --out touch-again.ndjson & C=$!\n"
        "sleep 180\n"
        "mark end; kill $B $C 2>/dev/null; wait\n")


def touch_script() -> str:
    """The memory programme, written to the node and started detached."""
    return (f"mkdir -p {REMOTE} && cd {REMOTE} && cat >touch.sh <<'RUN'\n{touch_body()}RUN\n"
            "(setsid nohup sh touch.sh >touch.log 2>&1 </dev/null &)")


def node_overrides(topology: dict) -> dict:
    """The per-node deploy settings of 7.2: the wave 7 control, and the
    enlarged hop journal on half the mesh nodes."""
    hunt = topology["hunt"]
    roles = {node["name"]: node["role"] for node in topology["nodes"]}
    overrides: dict[str, dict] = {name: dict(settings)
                                  for name, settings in hunt.get("control", {}).items()}
    for name in hunt.get("hop_journal_large", []):
        key = "extra_edge_args" if roles[name] == "edge" else "extra_distributor_args"
        overrides.setdefault(name, {})[key] = ["--hop-journal-capacity", str(HOP_JOURNAL_LARGE)]
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
        self.stop_polling = threading.Event()
        self.poller: threading.Thread | None = None
        # The polls' place in each node's journal, kept across restarts of
        # the poller, and their failures per node (into hunt.json).
        self.poll_since: dict[str, int] = {}
        self.poll_failures: dict[str, dict] = {}
        self.poll_lock = threading.Lock()
        self.survey_dirs: list[pathlib.Path] = []
        self.interrupted = False
        self.record: dict = {
            "schema": "needletail.stall-hunt.v1", "run_id": run_id,
            "topology_file": relative(topology_path),
            "arms": {"warmed": list(topology["hunt"]["warmed"]),
                     "control": topology["hunt"].get("control", {}),
                     "hop_journal_large": list(topology["hunt"].get("hop_journal_large", []))},
            "periods": [], "sessions": [], "replaced": [], "steps": [], "warm": [], "burn": {},
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
    def warmed(self) -> list[str]:
        return self.present(self.topology["hunt"]["warmed"])

    @property
    def not_warmed(self) -> list[str]:
        warmed = set(self.topology["hunt"]["warmed"])
        return sorted(name for name in self.nodes if name not in warmed)

    @property
    def soak_pairs(self) -> list[list[str]]:
        return [pair for pair in self.topology["hunt"]["soak_pairs"]
                if all(name in self.nodes for name in pair)]

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

    def out(self) -> pathlib.Path:
        return self.runner.out

    def save(self) -> None:
        if self.runner.dry:
            return
        self.out().mkdir(parents=True, exist_ok=True)
        with self.poll_lock:
            self.record["poll_failures"] = json.loads(json.dumps(self.poll_failures))
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
                f"{REMOTE}/watch.ndjson >{REMOTE}/watch.log 2>&1 </dev/null &)")))
        silent = [name for name in names if name not in ready]
        if silent:
            raise RuntimeError(f"ssh did not answer within {SSH_WAIT_S} s: no watch on "
                               + ", ".join(silent))

    def warm(self, names: list[str], label: str) -> None:
        started = self.clock()
        self.runner.each(names, lambda name: self.runner.ssh(name, (
            f"mkdir -p {REMOTE} && echo \"{label} $(date +%s.%N)\" >>{REMOTE}/warm-marks.txt && "
            f"{NODECHECK} warm >{REMOTE}/warm-{label}.json 2>{REMOTE}/warm-{label}.log"),
            timeout=180))
        self.record["warm"].append({"label": label, "nodes": list(names),
                                    "from_unix": round(started, 3),
                                    "to_unix": round(self.clock(), 3)})
        self.save()

    def push(self) -> None:
        args = ["push", "--remeasure"]
        for setting in push_settings(self.topology):
            args += ["--set", setting]
        if self.deploy(*args) != 0:
            raise RuntimeError("deploy push failed")
        self.start_polling()

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

    def soak(self, name: str, pairs: list[list[str]]) -> None:
        """One soak round: in each pair the first node sends numbered
        datagrams to the second, which echoes them with its kernel receive
        stamp. No memory is touched."""
        started = self.clock()

        def serve(pair: list[str]) -> None:
            sender, server = pair
            bind = f"{self.address(server, sender)}:{ECHO_PORT}"
            self.runner.ssh(server, (
                f"mkdir -p {REMOTE} && (setsid nohup timeout {SOAK_SECONDS + 15} {NODECHECK} "
                f"echo-serve --bind {bind} >{REMOTE}/echo-serve.log 2>&1 </dev/null &)"))

        def send(pair: list[str]) -> None:
            sender, server = pair
            target = f"{self.address(server, sender)}:{ECHO_PORT}"
            self.runner.ssh(sender, (
                f"mkdir -p {REMOTE} && (setsid nohup {NODECHECK} echo-send --target {target} "
                f"--pps {ECHO_PPS} --bytes 150 --seconds {SOAK_SECONDS} "
                f"--out {REMOTE}/echo-{name}-{server}.ndjson "
                f">{REMOTE}/echo-{name}.log 2>&1 </dev/null &)"))

        for pair in pairs:
            serve(pair)
        if not self.runner.dry:
            self.sleep(1.0)
        for pair in pairs:
            send(pair)
        self.period(name, "soak", started + 1, started + 1 + SOAK_SECONDS,
                    pairs=[list(pair) for pair in pairs])

    def burn(self) -> None:
        """The referee's steal check: 20 s of one busy loop on every mesh
        node, with the steal it showed."""
        started = self.clock()
        results = self.runner.each(self.mesh, lambda name: self.runner.ssh(
            name, BURN, check=False, timeout=BURN_SECONDS + 60))
        self.record["burn"] = {name: value for name, value in results.items()}
        self.period("burn", "burn", started, self.clock())

    def touch(self) -> None:
        names = self.present(self.topology["hunt"]["touch"])
        started = self.clock()
        self.runner.each(names, lambda name: self.runner.ssh(name, touch_script()))
        self.period("touch", "touch", started, started + 780, nodes=names)

    def entry(self, host: str, letter: str = "a") -> str | None:
        """The public address of a view host's site forwarder (the referee's
        entry is <site>-dist-a)."""
        name = f"{host.split('-')[0]}-dist-{letter}"
        return self.nodes[name]["public_ipv4"] if name in self.nodes else None

    def session(self, session: dict) -> None:
        name, config, seconds = session["name"], session["config"], session["seconds"]
        directory = f"{REMOTE}/{name}"
        tag = f"hunt-{self.run_id}-{name}"
        contributor = self.names("contributor")[0]
        source = self.purpose("source")[0]
        entry = {"name": name, "config": config, "stream": CONFIGS[config]["stream"],
                 "seconds": seconds, "hosts": {}}
        # As the referee's Audio.start: the contributor restarted, then the
        # sender at the next 64 s boundary.
        self.runner.ssh(contributor, "systemctl restart needletail-contrib.service; sleep 3; "
                                     "systemctl is-active needletail-contrib.service")
        printed = self.runner.ssh(source, sender_command(
            config, self.nodes[contributor]["vlan_ipv4"],
            seconds + LISTEN_AFTER_START_S + 10, directory))
        if not self.runner.dry:
            start = int(printed.split()[-1])
            wait = int(self.runner.ssh(source, f"echo $(( {start} - $(date +%s) ))") or 0)
            entry["start_unix"] = start
            self.sleep(max(0, wait) + LISTEN_AFTER_START_S)
        listeners = self.topology["hunt"]["listeners"]
        launched = self.clock()
        for host in self.present(sorted(listeners)):
            plan = listeners[host]
            site_entry = self.entry(host)
            if site_entry is None:
                continue
            host_tag = f"{tag}-{host}"
            commands = [udp_probe_command(number, site_entry, config, seconds, host_tag)
                        for number in range(1, plan.get("udp", 0) + 1)]
            commands += [hls_probe_command(number, site_entry, config, seconds, host_tag)
                         for number in range(1, plan.get("hls", 0) + 1)]
            if plan.get("load"):
                prefix = host.split("-")[0]
                edges = [self.nodes[edge]["public_ipv4"] for edge in self.names("edge")
                         if edge.startswith(prefix + "-")]
                entries = [found for found in (site_entry, self.entry(host, "b")) if found]
                commands.append(load_command(plan["load"], plan.get("sample", 8), entries,
                                             edges, config, seconds, host_tag))
            self.runner.ssh(host, listener_script(directory, commands))
            entry["hosts"][host] = dict(plan)
        if not self.runner.dry:
            entry["probes_from_unix"] = round(launched, 3)
            self.sleep(seconds + 5)
            self.wait_for_probes(sorted(entry["hosts"]), directory, 90)
            entry["probes_to_unix"] = round(launched + seconds, 3)
        self.runner.ssh(source, "pkill -x aep1-48k-probe; true", check=False)
        # Once, after the session has ended, never during one (2.5); only
        # the session's own events are sent.
        entry["hops_pull"] = self.pull_hops(name, CONFIGS[config]["stream"],
                                            entry.get("start_unix"))
        self.record["sessions"].append(entry)
        self.period(name, "session", entry.get("probes_from_unix", launched),
                    entry.get("probes_to_unix", launched + seconds), config=config)

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
        """/api/hops of the session's stream since its sender's START (the
        whole enlarged ring is hundreds of MB from the far sites)."""
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

    # ---- the replacement

    def stopped_so_far(self) -> dict[str, float]:
        """Each node's stopped milliseconds so far, from its watch record."""
        out = {}
        for name in sorted(self.nodes):
            try:
                text = self.runner.ssh(name, f"cat {REMOTE}/watch.ndjson", timeout=120)
            except Exception:  # noqa: BLE001 - a node that does not answer has no figure
                continue
            events = stall_records.dedupe(stall_records.parse_ndjson(text))
            out[name] = round(stall_records.stopped_ms(events), 3)
        return out

    def worst(self) -> tuple[str | None, dict[str, float]]:
        """The node that was stopped most among the edges, the bare nodes
        and the distributors; None when none was stopped at all."""
        figures = self.stopped_so_far()
        candidates = {name: value for name, value in figures.items()
                      if self.nodes[name]["role"] in ("edge", "bare", "distributor")}
        if not candidates or max(candidates.values()) <= 0:
            return None, figures
        return max(sorted(candidates), key=candidates.get), figures

    def replace_worst(self) -> None:
        if self.runner.dry:
            self.replace("<the edge or bare node stopped most>", push=True)
            return
        name, figures = self.worst()
        self.record["stopped_ms_at_35"] = figures
        if name is None:
            self.runner.say("no edge, bare node or distributor was stopped: nothing is replaced")
        elif self.nodes[name]["role"] == "distributor":
            self.record["distributor_to_replace"] = name
            self.runner.say(f"the worst instance is {name}, a distributor: replaced after the "
                            "last session, and only soaked")
        else:
            self.replace(name, push=True, stopped_ms=figures.get(name))
        self.save()

    def replace_distributor(self) -> None:
        if self.runner.dry:
            self.replace("<the distributor stopped most, if it is the worst of all>", push=False)
            return
        name = self.record.get("distributor_to_replace")
        if not name:
            return
        self.replace(name, push=False, stopped_ms=self.record["stopped_ms_at_35"].get(name))
        partner = next((other for pair in self.soak_pairs for other in pair
                        if name in pair and other != name), None)
        if partner:
            for index in (1, 2):
                self.soak(f"soak-new-{index}", [[name, partner]])
                self.sleep(SOAK_SECONDS + 10)

    def replace(self, name: str, push: bool, stopped_ms: float | None = None) -> None:
        """A new instance in a node's place: remove, grow, hosts, the watch
        as its first command, and for an edge or a bare node `push --nodes`
        (an edge is named by its peers by VLAN address only, which the new
        instance keeps). A distributor is not pushed: its public address is
        compiled into other nodes' arguments, and nothing may depend on
        rendering them again. The polls and, for a survey end, the route
        survey stop for it and start again whatever happened in between."""
        surveyed = self.runner.dry or name in self.survey_ends()
        if surveyed:
            # Its links end with its address: their record is closed and a
            # new one begins with the new instance.
            self.survey_collect()
        self.stop_polling_now()
        failure = None
        try:
            self.replace_instance(name, push, stopped_ms)
        except Exception as error:  # noqa: BLE001 - raised again once the polls run again
            failure = error
        problems = self.resume(surveyed)
        if failure is not None:
            raise failure
        if problems:
            raise RuntimeError("; ".join(problems))

    def replace_instance(self, name: str, push: bool, stopped_ms: float | None) -> None:
        dry = self.runner.dry
        before = None if dry else self.nodes[name].get("host_uuid")
        # The instance's own records (its watch above all) go with it:
        # they are fetched first, beside the new instance's.
        try:
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
                 "watched": False, "stopped_ms_before": stopped_ms,
                 "host_uuid_before": before,
                 "host_uuid_after": None if dry else self.nodes[name].get("host_uuid")}
        if not dry:
            self.record["replaced"].append(entry)
            self.save()
        watch_error = None
        try:
            self.watch([name])
            entry["watched"] = True
        except Exception as error:  # noqa: BLE001 - the push is tried all the same
            watch_error = error
        if push:
            entry["pushed"] = self.deploy("push", "--nodes", name) == 0
            if not dry and not entry["pushed"]:
                self.runner.say(f"push --nodes {name} failed: the new instance runs the watch only")
        if not dry:
            self.save()
        if watch_error is not None:
            raise watch_error

    def resume(self, surveyed: bool) -> list[str]:
        """After a replacement, whatever happened: the lab as it is now, the
        polls, and the survey on the ends the lab has. Returns what did not
        start again."""
        problems = []
        if not self.runner.dry and STATE.exists():
            self.use_lab_nodes()
        self.start_polling()
        if surveyed:
            try:
                self.survey_start()
            except Exception as error:  # noqa: BLE001 - named in the step's failure
                problems.append(f"the route survey did not start again: {error}")
        return problems

    def survey_ends(self) -> set[str]:
        survey = self.topology["hunt"].get("survey", {})
        return set(survey.get("senders", [])) | set(survey.get("receivers", []))

    # ---- the polls

    def start_polling(self) -> None:
        """Every POLL_S from every mesh node: /api/stalls?since_us= and
        /api/mesh/local; from the contributor its stall journal and its
        pacing record (7.4). Never /api/hops (2.5). One poller at a time,
        each with its own stop event."""
        if self.runner.dry:
            self.runner.say(f"every {POLL_S:g} s: GET /api/stalls?since_us=T and /api/mesh/local "
                            f"from {', '.join(self.names('distributor', 'edge'))}; /api/stalls "
                            f"and /api/pacing from {', '.join(self.names('contributor'))}")
            return
        if self.poller and self.poller.is_alive():
            return
        stop = threading.Event()
        self.stop_polling = stop
        self.poller = threading.Thread(target=self.poll_loop, args=(stop,), daemon=True)
        self.poller.start()

    def stop_polling_now(self) -> None:
        """The poller ends after the node it is polling: no second one can
        start while it runs."""
        self.stop_polling.set()
        if self.poller:
            self.poller.join()
            self.poller = None

    def poll_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            started = time.time()
            for name in self.mesh:
                if stop.is_set():
                    break
                self.poll_once(name)
            stop.wait(max(0.5, POLL_S - (time.time() - started)))

    def poll_once(self, name: str) -> bool:
        """One poll of a node; a failure is counted (hunt.json
        `poll_failures`) and the node is polled again next time."""
        try:
            self.poll(name, self.poll_since)
            return True
        except Exception as error:  # noqa: BLE001 - a node that does not answer is polled again
            with self.poll_lock:
                slot = self.poll_failures.setdefault(name, {"n": 0})
                slot["n"] += 1
                slot["last"] = str(error)[-200:]
                slot["last_unix"] = round(time.time(), 3)
            return False

    def last_poll(self) -> None:
        """One more poll of every mesh node once the poller has stopped: the
        journals' last events, which the 5 s cadence may not have reached."""
        if self.runner.dry:
            self.runner.say("once more: GET /api/stalls?since_us=T, /api/mesh/local and "
                            "/api/pacing from every mesh node")
            return
        failed = [name for name in self.mesh if not self.poll_once(name)]
        if failed:
            self.runner.say(f"the last poll failed on {', '.join(failed)}")

    def poll(self, name: str, since: dict[str, int]) -> None:
        node = self.nodes[name]
        contributor = node["role"] == "contributor"
        port = stall_records.CONTRIB_HTTP_PORT if contributor else HTTP_PORT
        target = self.out() / "nodes" / name
        target.mkdir(parents=True, exist_ok=True)
        events = stall_records.fetch_stalls(node["public_ipv4"], since.get(name, 0), port, 4.0)
        if events:
            with open(target / "stalls.ndjson", "a") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")
            since[name] = max(stall_records.at_us(event) or 0 for event in events) + 1
        path, file = ("/api/pacing", "pacing.ndjson") if contributor \
            else ("/api/mesh/local", "mesh-local.ndjson")
        body = json.loads(stall_records.fetch(node["public_ipv4"], port, path, 4.0))
        if not contributor and isinstance(body, dict):
            body = {key: body[key] for key in MESH_LOCAL_KEYS if key in body}
        with open(target / file, "a") as handle:
            handle.write(json.dumps({"unix": round(time.time(), 3), "body": body}) + "\n")

    # ---- the end

    def collect(self) -> None:
        """Everything the report reads, into target/stall-hunt/<run id>/."""
        self.stop_polling_now()
        self.last_poll()
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
        if self.archive("add-tree", self.run_id, relative(self.out()), "--kind", "raw",
                        "--description", "the stall hunt: hunt.json, every node's stall and "
                        "watch records, the sessions' probes, the route survey") != 0:
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
        self.stop_polling_now()
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
        problems += [f"av-mesh has no {flag}: the build is without the stall instruments"
                     for flag in MESH_FLAGS if flag not in text]
    if mesh_binary("needletail-nodecheck").is_file():
        text = help_text([str(mesh_binary("needletail-nodecheck")), "touch", "--help"])
        problems += [f"needletail-nodecheck touch has no {flag}: the first 400 MB would be "
                     "freed before the second is taken, and that is not fresh memory (7.3)"
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
