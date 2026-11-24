#!/usr/bin/env python3
"""The 4K isolation lab: one path in one region, a ramp of the referee's 4K
clips, and A/B cells of one change each, to find the exact limit of a
forwarder and an edge and its cause.

    linode-4k-isolate.py plan [--run-id ID]
    linode-4k-isolate.py run --budget-usd B [--spent-before-usd S] [--run-id ID] [--dry]
    linode-4k-isolate.py collect [--dry]
    linode-4k-isolate.py down [--dry]

The owner's question (24 Nov 2026, night): "we can keep sweeping but it
sounds like this is an issue we could test much more specifically". The
lab is deploy/low-latency/topology-4k-iso.json: London only, 7 dedicated
instances, $0.648 an hour, two billed hours. Its `iso` block names three
shapes (the topology's description says why the node-size pair is the
second ingress, not a third forwarder):
  one   contributor -> lon-dist-a -> lon-edge-1; lon-dist-c4 gets no video
        (the contributor's video class ports, 23000-23999, are dropped at
        its input by iptables, so the contributor sends exactly as in the
        normal shape) and lon-edge-c4 is held out (standby_edges)
  two   the normal shape: lon-edge-1 fed in full by lon-dist-a, repair by
        lon-dist-c4
  d4    contributor -> lon-dist-c4 -> lon-edge-c4, all g6-dedicated-4;
        lon-dist-a gets no video and lon-edge-1 is held out
Every cell restarts the forwarders, the edges and the contributor (`deploy
restart --roles`, with every A/B knob given explicitly, so no cell inherits
another's setting): av-mesh's RSS grows by about the video rate x 50-60 s
and is never returned (cmpd7), so no cell starts on a node over
RSS_START_LIMIT_MB, and a step is cut (its source stopped) when a mesh node
passes RSS_GUARD_MB.

A cell: host settings (RPS/RFS, busy polling) on the mesh nodes, the
restart, the iptables drop of its blind forwarder, the RSS check, the 1 ms
PCM audio lane (the referee's AEP1 sender on lon-src, at the next 64 s
boundary) and a check that the edge has its stream, the 4K clip over RTMP
into the contributor 9 s before the audio START, then on lon-view from
START + 3 s: one UDP 4K viewer (needletail-udp-client), one LL-HLS 4K
viewer (HTTP/3, eight parts in flight) and one UDP audio listener of the
PCM lane, all the referee's own commands (compare-run.py Lane.probe and
audio_uc, loaded by path); held SECONDS, the source stopped, then each
mesh node's /api/node, /api/mesh/local and /api/hops of the video stream
pulled, a snapshot of every node's kernel counters (nstat, snmp,
softnet_stat, ss -uamn, tc -s qdisc, ethtool -S, iptables) at both ends,
and the new lines of every sampler fetched into the step's directory, which
goes into the run archive (~/needletail-data/<run id>/steps/) before the
next cell's probes start. The viewer's records are fetched and scored
(scripts/compare/iso-report.py, the referee's rule) while the next cell
restarts.

Samplers, on every node from the start to collect: the referee's
(compare-run.py start_samplers by path: per-CPU /proc/stat, interrupts,
softirqs, softnet_stat, every av-mesh and av-contrib thread's stat with its
last CPU, a SCHED_FIFO 99 heartbeat per vCPU, VmRSS at 1 s, nstat Udp
counters, per-socket drops from ss -uamn, /api/stats, /api/mesh/local and
/api/node at 1 s, ethtool at start) and the stall hunt's extra sampler (tc
-s qdisc and the Udp lines of /proc/net/snmp at 1 s, kernel stacks on a
late heartbeat, kernel.watchdog_thresh=1).

The plan, in priority order, each cell only when it still ends inside the
plan's minutes (PLAN_END_MINUTE after `up` began):
  W0     a 20 s throwaway pass of 4K at 40 Mbit/s with audio (first video
         of fresh guests; not scored)
  BASE   shape one: 40, 80, 120, 164, 247, then 333 (the clip of the 300
         rung) if 247 was whole
  TWO    shape two: 164, 247, then 333 if 247 was whole there
  AB     at the highest BASE rung that was whole (H) and the first that
         was not (F), one change each, shape one, everything else at the
         defaults: video socket buffers fixed at 16 MiB and at 128 MiB
         (the default grows to 2 s of the rate); GSO off; GRO off; RPS/RFS
         on; the video class thread unpinned; busy polling 50 us; then the
         node size (shape d4) at 247 and 333
  SOAK   3 minutes at 247 on shape two, no restart inside it, stopped when
         a mesh node passes SOAK_GUARD_MB (the memory fix's before picture)
Repair sized to measured loss and the re-request fallback are another
engineer's and not in this build: the slot for them is ARMS's `repair`
entry, empty until they land (a cell runs only arms that set something).

This tool creates and deletes nothing itself: the lab tool does (`up` with
its watchdog, budget and bounded collect, `down`), with this lab's state
(target/linode-qualification/iso-lab.json) and ceilings
(NEEDLETAIL_LINODE_MAX_HOURLY_USD=0.70, NEEDLETAIL_LINODE_MAX_NODES=7). It
never reads the token. `plan` and `run --dry` print the cost, the schedule
and every command; a real `run` refuses before anything is billed when a
preflight finds a problem.
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
ARCHIVE_TOOL = SCRIPTS / "run_archive.py"
HUNT_TOOL = SCRIPTS / "linode-stall-hunt.py"
REPORT_TOOL = SCRIPTS / "compare" / "iso-report.py"
TOPOLOGY = ROOT / "deploy" / "low-latency" / "topology-4k-iso.json"
STATE = common.QUALIFICATION_ROOT / "iso-lab.json"
OUT = ROOT / "target" / "4k-iso"
# The lab's ceilings (the lead: one lab, at most $6, two billed hours an
# instance, dedicated plans only).
MAX_HOURLY_USD = 0.70
MAX_NODES = 7
WATCHDOG_HOURS = 1.9
BILLED_HOURS = 2
LAB_CEILING_USD = 6.0
HOURLY = {"g6-dedicated-2": 0.054, "g6-dedicated-4": 0.108}
MEMORY_MB = {"g6-dedicated-2": 4096, "g6-dedicated-4": 8192}
COLLECT_TIMEOUT_S = 240
# No cell starts unless it ends (at worst) by this minute after `up`
# began; collect and down come after (COLLECT_AND_DOWN_S), well inside the
# second billed hour and the watchdog's deadline (114 min, its own collect
# from about minute 105).
PLAN_END_MINUTE = 93
COLLECT_AND_DOWN_S = 10 * 60
# A cell runs when its expected length plus this ends by PLAN_END_MINUTE.
FIT_MARGIN_S = 90
REMOTE = "/var/lib/needletail-runs/iso"
# The referee's layout on the hosts (compare-run.py REMOTE): clips in
# clips/<rung>/, the loop's PCM in clips/audio.s24le, indexes in index/.
REMOTE_MEDIA = "/var/lib/compare"
CA = "/usr/local/share/ca-certificates/needletail-qualification.crt"
SERVER_NAME = common.TLS_SERVER_NAME
HTTP_PORT = 19444
CONTRIB_HTTP_PORT = 19443
CONTRIB_INGEST_PORT = 27100
RTMP_PORT = 19350
VIDEO_STREAM = 1
AUDIO_STREAM = 4001
AUDIO_INDEX = "nt-pcm-48s.index.json"
CLIPS = os.environ.get("NEEDLETAIL_HUNT_CLIPS", "clips6-full")
# The rungs: label, the referee's clip, its measured media rate (Mbit/s,
# clips6-full media.json `kbps`). "333" is the clip of the brief's 300 rung.
RUNGS = {"40": ("2160p40", 40.2), "80": ("2160p80", 80.6), "120": ("2160p120", 122.4),
         "164": ("2160p160", 163.8), "247": ("2160p240", 247.4), "333": ("2160p320", 333.0)}
BASE_RUNGS = ("40", "80", "120", "164", "247")
TWO_RUNGS = ("164", "247")
TOP_RUNG = "333"
D4_RUNGS = ("247", "333")
# Probe seconds: 45 below 164, 40 from 164 up (the memory: a 40 s step at
# 247 took a fresh node from 0.45 to 2.0-2.5 GB).
LONG_FROM = 164.0
SECONDS_LOW, SECONDS_HIGH = 45, 40
WARMUP_SECONDS = 20
SOAK_SECONDS = 180
# The source starts this long before the audio START; probes at START +
# PROBE_AFTER_START_S (the referee's 12 s of warm-up for the video).
VIDEO_LEAD_S = 9
PROBE_AFTER_START_S = 3
# Memory (cmpd7): no cell starts with a mesh node over this; a step is cut
# when one passes the guard; the soak's own guard.
RSS_START_LIMIT_MB = 2500
RSS_GUARD_MB = 3300
SOAK_GUARD_MB = 3200
GUARD_EVERY_S = 3.0
# A new instance's sshd answers some time after the API says running.
SSH_WAIT_S = 300
MESH_ROLES = ("contributor", "distributor", "edge")
RESTART_ROLES = "distributor,edge,contributor"
# Expected and worst seconds of a cell's parts, for the schedule: the
# restart (re-render, stage, activate every mesh node and the contributor),
# the 64 s boundary (8 to 72 s; the restart's own time is inside it after
# the sender starts), the pulls and snapshots, the slack.
RESTART_S = (45, 150)
BOUNDARY_S = (25, 72)
PULL_S = (15, 60)
SLACK_S = (10, 30)
# Up, push (the warm pass on the d4 hosts), media, samplers, the warm-up.
BRINGUP_S = (16 * 60, 26 * 60)

# The A/B arms (one change each from the defaults; every arm gives every
# knob, so nothing carries over). `set`: deploy settings; `extra`: av-mesh
# arguments of the forwarders and edges; `host`: kernel settings of every
# mesh node (applied before the restart: busy_read is taken when a socket
# is made); `shape`: another path.
KNOBS = {"relay_video_gso": True, "client_udp_gso": None, "relay_video_gro": None,
         "relay_class_cpus": "auto"}
ARMS = {
    "default": {"what": "the defaults (video sockets grow to 2 s of the rate, GSO and GRO "
                        "on, the class threads pinned 0,1, no RPS, no busy polling)"},
    "sockbuf16": {"what": "video socket buffers fixed at 16 MiB",
                  "extra": ["--relay-video-socket-buffer-ms", "0",
                            "--udp-socket-buffer-bytes", str(16 << 20)]},
    "sockbuf128": {"what": "video socket buffers fixed at 128 MiB",
                   "extra": ["--relay-video-socket-buffer-ms", "0",
                             "--udp-socket-buffer-bytes", str(128 << 20)]},
    "gso-off": {"what": "UDP GSO off (contributor, relays, the edge's client sends)",
                "set": {"relay_video_gso": False, "client_udp_gso": False}},
    "gro-off": {"what": "UDP GRO off on the video sockets of the forwarders and edges",
                "set": {"relay_video_gro": False}},
    "rps-on": {"what": "RPS/RFS on every interface of the mesh nodes (rps_cpus all CPUs, "
                       "rps_sock_flow_entries 32768)", "host": {"rps": True}},
    "unpinned": {"what": "the video class thread unpinned (audio still on CPU 0)",
                 "set": {"relay_class_cpus": ""}, "extra": ["--relay-class-cpus", "0,"]},
    "busy50": {"what": "net.core.busy_poll and busy_read 50 us",
               "host": {"busy_poll": 50}},
    "d4": {"what": "forwarder and edge on g6-dedicated-4 (contributor -> lon-dist-c4 -> "
                   "lon-edge-c4)", "shape": "d4"},
    # The slot for repair sized to measured loss and the re-request
    # fallback (another engineer's; not in tonight's build): an arm that
    # sets nothing is never run.
    "repair": {"what": "repair sized to measured loss, re-request fallback (not in this "
                       "build)", "pending": True},
}
AB_ORDER = ("sockbuf16", "sockbuf128", "gso-off", "gro-off", "rps-on", "unpinned", "busy50")
# The stall hunt's extra sampler (tc -s qdisc and Udp snmp at 1 s, kernel
# stacks on a late heartbeat), written into this lab's directory.
EXTRA_LATE_MS = 50

# A snapshot of a node's kernel counters at a step's ends (deltas in the
# report): every section starts with a line "== name".
SNAPSHOT = (
    "echo \"== unix $(date +%s.%N)\"; "
    "echo '== nstat'; nstat -az 2>/dev/null | grep -E '^(Udp|Ip|TcpExt(Udp|Rcv))' ; "
    "echo '== snmp'; grep -E '^(Udp|UdpLite|Ip):' /proc/net/snmp; "
    "echo '== softnet'; cat /proc/net/softnet_stat; "
    "echo '== sockets'; ss -uamn 2>/dev/null; "
    "echo '== qdisc'; tc -s qdisc show 2>/dev/null; "
    "for i in $(ls /sys/class/net | grep -v '^lo$'); do echo \"== ethtool $i\"; "
    "ethtool -S $i 2>/dev/null; echo \"== link $i\"; ip -s link show $i; "
    "echo \"== rps $i\"; for q in /sys/class/net/$i/queues/rx-*; do "
    "echo \"$q $(cat $q/rps_cpus) $(cat $q/rps_flow_cnt)\"; done; done; "
    "echo '== sysctl'; sysctl net.core.busy_poll net.core.busy_read net.core.rps_sock_flow_entries "
    "net.core.rmem_max net.core.netdev_max_backlog net.core.netdev_budget 2>/dev/null; "
    "echo '== iptables'; iptables -L NTISO -v -n -x 2>/dev/null; "
    "echo '== rss'; for p in $(pgrep -x av-mesh) $(pgrep -x av-contrib); do "
    "echo \"$(cat /proc/$p/comm) $p $(awk '/VmRSS/{print $2}' /proc/$p/status) "
    "$(ps -o etimes= -p $p)\"; done; "
    "echo '== meminfo'; grep -E 'MemTotal|MemFree|MemAvailable|AnonPages|AnonHugePages' "
    "/proc/meminfo; true")

# The new bytes of every sampler file on a node since the last call, as a
# tar.gz on stdout (the offsets are kept on the node): a step's slice of the
# referee's samplers and the extra sampler, fetched as the run goes.
SAMPLER_FILES = ("/var/tmp/compare-sampler.log", "/var/tmp/compare-sys.log",
                 "/var/tmp/compare-stats.log", "/var/tmp/compare-meshlocal.log",
                 "/var/tmp/compare-apinode.log", "/var/tmp/compare-heartbeat.*.log",
                 f"{REMOTE}/extra.log")
INCREMENT = (
    f"mkdir -p {REMOTE}/inc/out && rm -f {REMOTE}/inc/out/*; "
    f"for f in {' '.join(SAMPLER_FILES)}; do [ -f \"$f\" ] || continue; "
    "b=$(basename $f); o=$(cat " + REMOTE + "/inc/$b.off 2>/dev/null || echo 0); "
    "s=$(stat -c %s $f); [ $s -lt $o ] && o=0; "
    "tail -c +$((o + 1)) $f | head -c $((s - o)) >" + REMOTE + "/inc/out/$b; "
    "echo $s >" + REMOTE + "/inc/$b.off; done; "
    f"tar -C {REMOTE}/inc/out -czf - .")


# ----------------------------------------------------------------- the plan

def load_topology(path: pathlib.Path | str | None = None) -> dict:
    topology = json.loads(pathlib.Path(path or TOPOLOGY).read_text())
    if "iso" not in topology:
        sys.exit(f"{path or TOPOLOGY}: no `iso` block; not the isolation lab's topology")
    return topology


def hourly_usd(topology: dict) -> float:
    total = 0.0
    for node in topology["nodes"]:
        if node["type"] not in HOURLY:
            sys.exit(f"{node['name']}: {node['type']} is not a dedicated plan this lab uses")
        total += HOURLY[node["type"]]
    return round(total, 4)


def cost(topology: dict) -> dict:
    hourly = hourly_usd(topology)
    return {"instances": len(topology["nodes"]), "hourly_usd": hourly,
            "billed_hours": BILLED_HOURS, "planned_usd": round(hourly * BILLED_HOURS, 4),
            "ceiling_usd": LAB_CEILING_USD, "max_hourly_usd": MAX_HOURLY_USD,
            "max_nodes": MAX_NODES, "watchdog_hours": WATCHDOG_HOURS}


def rung_seconds(rung: str) -> int:
    return SECONDS_HIGH if RUNGS[rung][1] >= LONG_FROM else SECONDS_LOW


def cell(name: str, group: str, shape: str, rung: str, arm: str = "default",
         seconds: int | None = None, scored: bool = True, guard_mb: int = RSS_GUARD_MB,
         restart: bool = True) -> dict:
    spec = ARMS[arm]
    return {"name": name, "group": group, "shape": spec.get("shape", shape), "rung": rung,
            "clip": RUNGS[rung][0], "mbit": RUNGS[rung][1], "arm": arm,
            "seconds": seconds or rung_seconds(rung), "scored": scored, "guard_mb": guard_mb,
            "restart": restart}


def ran_name(ran: list[str], rung: str) -> str | None:
    """The cell among `ran` (B247, T247) that carried `rung`."""
    return next((name for name in ran if name[1:] == rung), None)


def warmup_cell() -> dict:
    return cell("W0", "warmup", "one", "40", seconds=WARMUP_SECONDS, scored=False)


def base_cells(base: dict[str, bool] | None = None) -> list[dict]:
    """BASE: the ramp on one forwarder; 333 only after a whole 247 (known
    only in the run: `base` is {rung: whole} so far, None in the plan)."""
    cells = [cell(f"B{rung}", "base", "one", rung) for rung in BASE_RUNGS]
    if base is None or base.get("247"):
        cells.append(cell(f"B{TOP_RUNG}", "base", "one", TOP_RUNG))
    return cells


def two_cells(two: dict[str, bool] | None = None) -> list[dict]:
    cells = [cell(f"T{rung}", "two", "two", rung) for rung in TWO_RUNGS]
    if two is None or two.get("247"):
        cells.append(cell(f"T{TOP_RUNG}", "two", "two", TOP_RUNG))
    return cells


def ab_rungs(base: dict[str, bool]) -> tuple[str | None, str | None]:
    """(H, F): the highest BASE rung that was whole and the first (lowest)
    that was not. A rung that did not run is neither."""
    order = [rung for rung in RUNGS if rung in base]
    whole = [rung for rung in order if base[rung] is True]
    broken = [rung for rung in order if base[rung] is False]
    return (whole[-1] if whole else None), (broken[0] if broken else None)


def ab_cells(base: dict[str, bool] | None) -> list[dict]:
    """One change at a time at H and at F (the plan, before any rung ran,
    shows them as 247 and 333), then the node size at 247 and 333."""
    high, first = ab_rungs(base) if base is not None else ("247", "333")
    rungs = [rung for rung in (high, first) if rung is not None]
    cells = [cell(f"A-{arm}-{rung}", "ab", "one", rung, arm)
             for arm in AB_ORDER if not ARMS[arm].get("pending") for rung in rungs]
    cells += [cell(f"A-d4-{rung}", "ab", "d4", rung, "d4") for rung in D4_RUNGS]
    return cells


def soak_cell() -> dict:
    return cell("S247", "soak", "two", "247", seconds=SOAK_SECONDS, guard_mb=SOAK_GUARD_MB)


def cell_seconds(item: dict, worst: bool = False) -> float:
    """How long a cell takes: its restart, the boundary wait, the video's
    lead, its probes, the pulls and some slack."""
    pick = 1 if worst else 0
    return (RESTART_S[pick] + BOUNDARY_S[pick] + PROBE_AFTER_START_S + item["seconds"] + 5
            + PULL_S[pick] + SLACK_S[pick])


def schedule(base: dict | None = None, two: dict | None = None) -> list[dict]:
    """Every cell of the plan in order (in the run the plan is made again as
    the verdicts come in)."""
    return ([warmup_cell()] + base_cells(base) + two_cells(two) + ab_cells(base)
            + [soak_cell()])


def arm_settings(arm: str, held_out: list[str]) -> list[str]:
    """The `--set` values of a cell: every knob (the arm's value or the
    default), the extra arguments of forwarders and edges, the held-out
    edges."""
    spec = ARMS[arm]
    values = {**KNOBS, **spec.get("set", {})}
    out = [f"{key}={'' if value == '' else json.dumps(value)}" for key, value in values.items()]
    extra = json.dumps(spec.get("extra", []), separators=(",", ":"))
    out += [f"extra_distributor_args={extra}", f"extra_edge_args={extra}",
            f"standby_edges={json.dumps(sorted(held_out), separators=(',', ':'))}"]
    return out


def host_command(arm: str) -> str:
    """The kernel settings of an arm on a mesh node, every one given (a
    later cell puts the defaults back): RPS/RFS and busy polling."""
    host = ARMS[arm].get("host", {})
    busy = int(host.get("busy_poll", 0))
    rps = bool(host.get("rps"))
    flows = 32768 if rps else 0
    return ("n=$(nproc); mask=$(printf '%x' $(( (1 << n) - 1 ))); "
            f"sysctl -qw net.core.busy_poll={busy} net.core.busy_read={busy} "
            f"net.core.rps_sock_flow_entries={flows}; "
            "for i in $(ls /sys/class/net | grep -v '^lo$'); do "
            "qs=$(ls -d /sys/class/net/$i/queues/rx-* 2>/dev/null | wc -l); "
            "for q in /sys/class/net/$i/queues/rx-*; do "
            + (f"echo $mask >$q/rps_cpus; echo $(( {flows} / (qs > 0 ? qs : 1) )) >$q/rps_flow_cnt; "
               if rps else "echo 0 >$q/rps_cpus; echo 0 >$q/rps_flow_cnt; ")
            + "done; done; true")


def block_command(contributor_vlan: str | None, ports: str) -> str:
    """A forwarder that carries no video in its cell: the contributor's
    datagrams to its video class ports dropped at its input (chain NTISO);
    None: the chain emptied."""
    low, high = ports.split("-")
    base = ("iptables -N NTISO 2>/dev/null; iptables -F NTISO; "
            "iptables -C INPUT -j NTISO 2>/dev/null || iptables -I INPUT 1 -j NTISO; ")
    if contributor_vlan is None:
        return base + "true"
    return base + (f"iptables -A NTISO -s {contributor_vlan} -p udp --dport {low}:{high} "
                   "-j DROP; iptables -L NTISO -n | grep -c DROP")


def audio_sender_command(target: str, directory: str) -> str:
    """The referee's 1 ms PCM lane (compare-run.py Lane.start, audio "pcm"):
    at the next 64 s boundary at least 8 s away, session START + 1 ms, for
    an hour; prints START."""
    return (f"mkdir -p {directory} && cd {directory} && {{ pkill -x aep1-48k-probe; true; }} && "
            "NOW=$(date +%s); START=$(( (NOW / 64 + 1) * 64 )); "
            "[ $(( START - NOW )) -lt 8 ] && START=$(( START + 64 )); echo $START >nt-audio.start; "
            "(setsid nohup /usr/local/bin/aep1-48k-probe send "
            f"--target {target}:{CONTRIB_INGEST_PORT} --session-id ${{START}}001000000 "
            "--group-id 0 --duration-seconds 3600 --payload pcm --epoch-samples 48 --channels 2 "
            f"--group-channels 8 --pcm-s24le-file {REMOTE_MEDIA}/clips/audio.s24le "
            "--repair-percent 12 --min-repair-symbols 1 >nt-audio.log 2>&1 </dev/null &); "
            "echo $START")


def video_sender_command(clip: str, target: str, directory: str) -> str:
    """The referee's 4K lane (compare-run.py Lane.start "nt"): the clip as
    FLV without audio, over RTMP into the contributor; detached."""
    feed = (f"ntcmp flv --clip {REMOTE_MEDIA}/clips/{clip} --no-audio | ffmpeg -hide_banner "
            "-loglevel warning -nostdin -f flv -i pipe:0 -c copy -f flv "
            f"rtmp://{target}:{RTMP_PORT}/live/needletail-lab")
    return (f"mkdir -p {directory} && cd {directory} && {{ pkill -x ffmpeg; pkill -x ntcmp; true; }} "
            f"&& (setsid nohup sh -c {shlex.quote(feed)} >nt.log 2>&1 </dev/null &)")


STOP_SOURCES = "pkill -x ffmpeg; pkill -x ntcmp; pkill -x aep1-48k-probe; true"


def listener_script(directory: str, commands: list[str]) -> str:
    """The viewer host's probes of a cell, all at once, in the background;
    the file `done` when the last has ended (and its ingest)."""
    body = "".join(f"( {command} ) >/dev/null 2>>errors.log &\n" for command in commands)
    return (f"rm -rf {directory} && mkdir -p {directory} && cd {directory} && "
            f"cat >run.sh <<'RUN'\necho \"start $(date +%s.%N)\" >times.txt\n{body}wait\n"
            "echo \"end $(date +%s.%N)\" >>times.txt\ntouch done\nRUN\n"
            "(setsid nohup bash run.sh >run.log 2>&1 </dev/null &)")


def referee_lane(entry_ip, module=None):
    """The referee's Lane "nt" (compare-run.py), made without its Session:
    only its probe and audio_uc, whose entry is lon-dist-a's address."""
    if module is None:
        return None
    lane = module.Lane.__new__(module.Lane)
    lane.name = "nt"
    lane.session = types.SimpleNamespace(ip=lambda name: entry_ip)
    return lane


def probe_commands(lane, clip: str, seconds: int, tag: str) -> list[str]:
    """The three listeners of a cell, the referee's commands: the UDP 4K
    viewer, the HTTP/3 LL-HLS 4K viewer, the UDP listener of the PCM lane."""
    return [lane.probe("lon", clip, seconds, "probe.uc", 1, True, tag, "uc"),
            lane.probe("lon", clip, seconds, "probe.h3", 1, True, tag, "h3"),
            lane.audio_uc("lon", seconds, tag)]


def tool_environment() -> dict:
    return {"NEEDLETAIL_LINODE_LAB_STATE": str(STATE),
            "NEEDLETAIL_LINODE_MAX_HOURLY_USD": f"{MAX_HOURLY_USD:.2f}",
            "NEEDLETAIL_LINODE_MAX_NODES": str(MAX_NODES)}


def relative(path) -> str:
    try:
        return str(pathlib.Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def load_by_path(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


def hunt_module():
    """The stall hunt's driver, by path: its runner, its extra sampler and
    the referee's samplers' loader."""
    return load_by_path("iso_stall_hunt", HUNT_TOOL)


def report_module():
    return load_by_path("iso_report", REPORT_TOOL)


def mesh_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_AV_MESH_ROOT", str(common.WORKSPACE / "av-mesh")))


def contrib_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_AV_CONTRIB_ROOT",
                                       str(common.WORKSPACE / "av-contrib")))


def compare_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT",
                                       str(common.WORKSPACE / "needletail-compare")))


def clips_root() -> pathlib.Path:
    return compare_root() / "target" / "compare-media" / CLIPS


def mesh_binary(name: str) -> pathlib.Path:
    return mesh_root() / "target" / "x86_64-unknown-linux-gnu" / "release" / name


# --------------------------------------------------------------- the runner

class Runner:
    """Every command goes through here: run and journalled, or (dry)
    printed."""

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
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
        print(f"[{stamp}] {text}" if not self.dry else text, flush=True)

    def tool(self, script: pathlib.Path, *args: str, capture: bool = False,
             timeout: float | None = None) -> tuple[int, str]:
        environment = tool_environment()
        line = f"{relative(script)} " + " ".join(shlex.quote(arg) for arg in args)
        if self.dry:
            shown = " ".join(f"{key}={relative(value) if key.endswith('STATE') else value}"
                             for key, value in environment.items())
            self.say(f"$ {shown} {line}")
            return 0, ""
        self.say(f"$ {line}")
        started = time.time()
        try:
            result = subprocess.run([sys.executable, str(script), *args], cwd=str(ROOT),
                                    env={**os.environ, **environment}, timeout=timeout,
                                    stdout=subprocess.PIPE if capture else None,
                                    stderr=subprocess.STDOUT if capture else None)
        except subprocess.TimeoutExpired:
            self.journal("tool", tool=script.name, args=list(args), rc="timeout")
            return 124, ""
        output = result.stdout.decode(errors="replace") if capture else ""
        if capture and output:
            self.say(output.rstrip())
        self.journal("tool", tool=script.name, args=list(args), rc=result.returncode,
                     seconds=round(time.time() - started, 1))
        return result.returncode, output

    def ssh(self, name: str, command: str, check: bool = True, timeout: int = 600,
            stdin: bytes | None = None, what: str | None = None) -> str:
        if self.dry:
            self.say(f"ssh {name} {shlex.quote(what or command)}")
            return ""
        result = common.ssh(self.nodes[name], command, check=False, timeout=timeout, stdin=stdin)
        self.journal("ssh", node=name, command=(what or command)[:400], rc=result.returncode)
        if check and result.returncode != 0:
            raise RuntimeError(f"{name}: remote command failed ({result.returncode}): "
                               f"{result.stderr.decode(errors='replace')[-600:]}")
        return result.stdout.decode(errors="replace").strip()

    def ssh_bytes(self, name: str, command: str, timeout: int = 300, what: str = "") -> bytes:
        if self.dry:
            self.say(f"ssh {name} {shlex.quote(what or command)}")
            return b""
        result = common.ssh(self.nodes[name], command, check=False, timeout=timeout)
        self.journal("ssh", node=name, command=(what or command)[:200], rc=result.returncode)
        return result.stdout if result.returncode == 0 else b""

    def rsync_to(self, name: str, sources: list[pathlib.Path], destination: str,
                 timeout: int = 1800) -> None:
        if self.dry:
            self.say(f"rsync {' '.join(relative(source) for source in sources)} "
                     f"{name}:{destination}")
            return
        common.rsync_to(self.nodes[name], [str(source) for source in sources], destination,
                        timeout=timeout)
        self.journal("rsync-to", node=name, destination=destination)

    def rsync_from(self, name: str, source: str, destination: pathlib.Path) -> None:
        if self.dry:
            self.say(f"rsync {name}:{source} {relative(destination)}/")
            return
        destination.mkdir(parents=True, exist_ok=True)
        common.rsync_from(self.nodes[name], source, str(destination) + "/", timeout=900)
        self.journal("rsync-from", node=name, source=source)

    def each(self, names: list[str], function) -> dict:
        if self.dry:
            return {name: function(name) for name in names}
        return common.parallel([self.nodes[name] for name in names],
                               lambda node: function(node["name"]))

    def fetch(self, name: str, port: int, path: str, timeout: float = 30.0) -> bytes:
        if self.dry:
            self.say(f"GET https://<{name}>:{port}{path}")
            return b""
        return stall_records.fetch(self.nodes[name]["public_ipv4"], port, path, timeout=timeout)


# ---------------------------------------------------------------- the lab

class Iso:
    def __init__(self, runner: Runner, topology: dict, topology_path: pathlib.Path, run_id: str,
                 budget_usd: float | None = None, spent_before_usd: float | None = None,
                 clock=time.time, sleep=time.sleep) -> None:
        self.runner = runner
        self.topology = topology
        self.topology_path = topology_path
        self.run_id = run_id
        self.budget_usd = budget_usd
        self.spent_before_usd = spent_before_usd
        self.clock, self.sleep = clock, sleep
        self.resume = False
        self.up_started: float | None = None
        self.interrupted = False
        self.iso = topology["iso"]
        self.shape: str | None = None
        self.pending: threading.Thread | None = None
        self.referee = None
        self.lane = None
        self.record: dict = {"schema": "needletail.4k-iso.v1", "run_id": run_id,
                             "topology_file": relative(topology_path), "steps": [],
                             "cells": [], "left_out": [], "warm": {}, "verdicts": {},
                             "failures": []}
        self.runner.nodes = {
            node["name"]: {**node, "public_ipv4": f"<public:{node['name']}>",
                           "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
            for node in topology["nodes"]}

    # ---- nodes and records

    @property
    def nodes(self) -> dict[str, dict]:
        return self.runner.nodes

    def names(self, *roles: str) -> list[str]:
        return sorted(name for name, node in self.nodes.items() if node["role"] in roles)

    @property
    def mesh(self) -> list[str]:
        return self.names(*MESH_ROLES)

    def purpose(self, purpose: str) -> str:
        return next(name for name, node in self.nodes.items()
                    if common.load_purpose(node) == purpose)

    def out(self) -> pathlib.Path:
        return self.runner.out

    def save(self) -> None:
        if self.runner.dry:
            return
        self.out().mkdir(parents=True, exist_ok=True)
        path = self.out() / "iso.json"
        path.with_suffix(".json.next").write_text(json.dumps(self.record, indent=1) + "\n")
        path.with_suffix(".json.next").replace(path)

    def use_lab_nodes(self) -> None:
        state = json.loads(STATE.read_text())
        self.runner.nodes = state["nodes"]
        self.record["nodes"] = {name: {key: node.get(key) for key in (
            "role", "type", "public_ipv4", "vlan_ipv4", "host_uuid")}
            for name, node in state["nodes"].items()}
        self.record["created_unix"] = state.get("created_unix")
        self.record["watchdog_deadline_unix"] = state.get("watchdog_deadline_unix")

    def elapsed(self) -> float:
        return self.clock() - (self.up_started or self.clock())

    def lab(self, *args: str, capture: bool = False) -> tuple[int, str]:
        return self.runner.tool(LAB_TOOL, *args, capture=capture)

    def deploy(self, *args: str, capture: bool = False, timeout: float | None = None) -> int:
        return self.runner.tool(DEPLOY_TOOL, *args, capture=capture, timeout=timeout)[0]

    def archive(self, *args: str) -> int:
        return self.runner.tool(ARCHIVE_TOOL, "--tool", "4k-isolate", *args)[0]

    # ---- bring-up

    def archive_create(self) -> None:
        status = self.archive("create", self.run_id, "--kind", "linode-lab",
                              *[item for path in archived_run.code_paths(mesh_root(), contrib_root())
                                for item in ("--code", str(path))],
                              "--topology", relative(self.topology_path))
        if status != 0:
            raise RuntimeError("the run archive refused the run: nothing was created")

    def up(self) -> None:
        args = ["up", "--topology", relative(self.topology_path), "--run-id", self.run_id,
                "--watchdog-hours", f"{WATCHDOG_HOURS:g}",
                "--collect-timeout-s", f"{COLLECT_TIMEOUT_S:g}",
                "--budget-usd", f"{self.budget_usd:g}" if self.budget_usd is not None
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
        self.record["up_ended_unix"] = round(self.clock(), 3)
        self.save()

    def hosts(self, label: str) -> None:
        status, output = self.lab("hosts", capture=True)
        if not self.runner.dry:
            (self.out() / f"hosts-{label}.txt").write_text(output)
            self.use_lab_nodes()

    def wait_ssh(self, patience_s: float = SSH_WAIT_S) -> None:
        """Every node takes an ssh command: a new instance is `running` to
        the API before its sshd answers (iso11242128: push's bootstrap met
        two that did not, 30 s after up)."""
        if self.runner.dry:
            self.runner.say(f"# until ssh answers on every node (at most {patience_s:g} s)")
            return
        deadline = self.clock() + patience_s
        pending = sorted(self.nodes)
        while pending and self.clock() < deadline:
            answered = self.runner.each(pending, lambda name: common.ssh(
                self.nodes[name], "true", check=False, timeout=30).returncode == 0)
            pending = [name for name in pending if not answered.get(name)]
            if pending:
                self.sleep(5.0)
        if pending:
            raise RuntimeError(f"ssh did not answer within {patience_s:g} s: {', '.join(pending)}")
        self.runner.say("ssh answers on every node")

    def push(self) -> None:
        """The fresh deploy: every node, the warm pass on every host (the
        source and viewer hosts too: role load), slow hosts reported only,
        the PCM rendition for the audio lane, the edge of shape one in
        service."""
        held = self.iso["shapes"]["one"]["held_out"]
        status = self.deploy("push", "--remeasure", "--slow-host", "report",
                             "--set", "daw_hls_formats=flac,opus,pcm",
                             *[item for setting in arm_settings("default", held)
                               for item in ("--set", setting)])
        if status != 0 and not self.runner.dry:
            # Once more: a node's first minutes (apt, sshd) fail a push that
            # the next one passes.
            self.runner.say("deploy push failed: once more")
            self.wait_ssh()
            status = self.deploy("push", "--remeasure", "--slow-host", "report",
                                 "--set", "daw_hls_formats=flac,opus,pcm",
                                 *[item for setting in arm_settings("default", held)
                                   for item in ("--set", setting)])
        if status != 0:
            raise RuntimeError("deploy push failed")
        self.shape = "one"

    def warm_table(self) -> None:
        """Each host's warm pass as the deploy recorded it: seconds and the
        worst single page write."""
        if self.runner.dry:
            self.runner.say("# the warm table: lab state deployed.warm (seconds, worst page "
                            "write, slow_host) of every host; `deploy status`")
            self.deploy("status")
            return
        state = json.loads(STATE.read_text())
        warm = (state.get("deployed") or {}).get("warm") or {}
        self.record["warm"] = warm
        for name in sorted(self.nodes):
            entry = warm.get(name) or {}
            worst = entry.get("worst_page_us")
            self.runner.say(f"warm {name}: {entry.get('verdict', 'missing')} "
                            f"{entry.get('seconds', '?')} s, worst write "
                            f"{(worst / 1000.0) if worst is not None else '?'} ms"
                            + (f", SLOW {entry.get('slow_reasons')}" if entry.get("slow_host")
                               else ""))
        self.save()
        self.deploy("status")
        missing = [name for name in self.nodes if not (warm.get(name) or {}).get("complete")]
        if missing:
            self.runner.say(f"warm pass not complete on {', '.join(missing)}: warming them now")
            self.runner.each(missing, lambda name: self.runner.ssh(
                name, "needletail-nodecheck warm --leave-mb 300 2>&1 | tail -1 || "
                      "/bin/true", check=False, timeout=900))

    def media(self) -> None:
        """What the cells need on the hosts: the referee's clips, the loop's
        PCM, ntcmp and ffmpeg on the source host; ntcmp and the indexes on
        the viewer host."""
        source, view = self.purpose("source"), self.purpose("view")
        clips = clips_root()
        ntcmp = compare_root() / "target" / "ntcmp"
        self.runner.ssh(source, f"mkdir -p {REMOTE} {REMOTE_MEDIA}/clips && "
                                "{ command -v ffmpeg >/dev/null || { export "
                                "DEBIAN_FRONTEND=noninteractive; apt-get update -q >/dev/null; "
                                "apt-get install -y -q ffmpeg >/dev/null; }; }", timeout=900)
        self.runner.ssh(view, f"mkdir -p {REMOTE} {REMOTE_MEDIA}/index")
        jobs = [(source, [ntcmp], "/usr/local/bin/"), (view, [ntcmp], "/usr/local/bin/"),
                (source, [clips / "audio.s24le"], f"{REMOTE_MEDIA}/clips/"),
                (view, [clips / f"{clip}.index.json" for clip, _ in RUNGS.values()]
                 + [compare_root() / "target" / "compare-media" / "audio" / AUDIO_INDEX],
                 f"{REMOTE_MEDIA}/index/")]
        jobs += [(source, [clips / clip], f"{REMOTE_MEDIA}/clips/") for clip, _ in RUNGS.values()]
        if self.runner.dry:
            for name, sources, destination in jobs:
                self.runner.rsync_to(name, sources, destination)
            return
        errors = []

        def one(job) -> None:
            try:
                self.runner.rsync_to(*job)
            except Exception as error:  # noqa: BLE001 - named below
                errors.append(f"{job[0]} {job[2]}: {error}")

        threads = [threading.Thread(target=one, args=(job,)) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise RuntimeError("media: " + "; ".join(errors))

    def start_samplers(self) -> None:
        """The referee's samplers on every node, and the hunt's extra
        sampler beside them, from now to collect."""
        names = sorted(self.nodes)
        hunt = hunt_module() if not self.runner.dry else None
        if self.runner.dry:
            self.runner.say(f"# the referee's samplers: {relative(compare_root())}/scripts/compare/"
                            f"compare-run.py start_samplers on {', '.join(names)} (sys, heartbeat, "
                            "stats with nstat and ss -uamn, meshlocal, apinode, RSS; nic now)")
            self.runner.say(f"# the stall hunt's extra sampler on {', '.join(names)}: tc -s qdisc "
                            f"and Udp snmp at 1 s, stacks on a heartbeat {EXTRA_LATE_MS} ms late "
                            f">{REMOTE}/extra.log")
            return
        self.referee = hunt.referee_samplers(compare_root())
        sampled = self.referee.start_samplers(self.referee_session(names))
        self.runner.journal("referee-samplers", action="start",
                            nodes=[item["name"] for item in sampled])
        extra = hunt.EXTRA_SAMPLER.replace(hunt.REMOTE, REMOTE)
        self.runner.each(names, lambda name: self.runner.ssh(name, extra, what="the extra sampler"))

    def referee_session(self, names: list[str]):
        return types.SimpleNamespace(nodes={name: self.nodes[name] for name in names},
                                     dir=self.out(), name=self.run_id)

    # ---- a cell

    def shape_nodes(self, shape: str) -> dict:
        return self.iso["shapes"][shape]

    def restart(self, item: dict) -> float:
        """Host settings, the restart of every mesh node with the cell's
        settings, the blind forwarder's drop; returns the restart's
        seconds."""
        shape = self.shape_nodes(item["shape"])
        forwarders_edges = self.names("distributor", "edge")
        command = host_command(item["arm"])
        self.runner.each(forwarders_edges, lambda name: self.runner.ssh(
            name, command, what=f"host settings of arm {item['arm']}: {command}"))
        started = self.clock()
        status = self.deploy("restart", "--roles", RESTART_ROLES,
                             *[part for setting in arm_settings(item["arm"], shape["held_out"])
                               for part in ("--set", setting)], timeout=600)
        took = self.clock() - started
        if status != 0:
            raise RuntimeError(f"{item['name']}: restart --roles failed ({status})")
        self.shape = item["shape"]
        contributor = self.nodes[self.names("contributor")[0]]
        vlan = (contributor.get("vlan_ipv4") or "").split("/")[0] or "<contrib-vlan>"
        for name in self.names("distributor"):
            blind = name in shape["blind"]
            self.runner.ssh(name, block_command(vlan if blind else None, self.iso["video_ports"]),
                            what=("drop the contributor's video ports "
                                  f"{self.iso['video_ports']}" if blind else "no drop"))
        return took

    def extra_memory_mb(self, name: str) -> float:
        """Memory a node has beyond a g6-dedicated-2's 4 GB: the limits are
        a 4 GB node's, moved up by it."""
        return MEMORY_MB.get(self.nodes[name].get("type"), 4096) - 4096

    def restart_left(self, item: dict, entry: dict) -> None:
        """A mesh process the restart did not restart (older than it):
        restarted by systemctl, then waited for until ready."""
        for name in entry.get("not_restarted") or []:
            service = ("needletail-contrib" if self.nodes[name]["role"] == "contributor"
                       else "needletail-mesh")
            if name in self.shape_nodes(item["shape"])["held_out"]:
                continue
            self.runner.say(f"{item['name']}: {name} was not restarted: systemctl restart {service}")
            self.runner.ssh(name, f"systemctl restart {service}.service", check=False, timeout=120)
            port = CONTRIB_HTTP_PORT if service == "needletail-contrib" else HTTP_PORT
            deadline = self.clock() + 90
            while self.clock() < deadline:
                try:
                    body = json.loads(self.runner.fetch(name, port, "/api/node", timeout=5))
                    if body.get("ready") or service == "needletail-contrib":
                        break
                except Exception:  # noqa: BLE001 - not yet
                    pass
                self.sleep(2.0)
        if entry.get("not_restarted"):
            entry["ages_after_fix_s"] = self.ages()

    def rss_mb(self) -> dict[str, float]:
        """Each mesh node's largest av-mesh / av-contrib RSS, MB."""
        if self.runner.dry:
            return {}
        command = "ps -o rss= -C av-mesh,av-contrib 2>/dev/null | sort -n | tail -1"
        values = self.runner.each(self.mesh, lambda name: self.runner.ssh(
            name, command, check=False, timeout=30, what="rss"))
        return {name: round(int(text or 0) / 1024.0, 1) for name, text in values.items()
                if str(text).strip().isdigit()}

    def ages(self) -> dict[str, list[int]]:
        command = ("for p in $(pgrep -x av-mesh) $(pgrep -x av-contrib); do "
                   "ps -o etimes= -p $p; done")
        if self.runner.dry:
            self.runner.say(f"# each mesh process's age: ssh <mesh> {shlex.quote(command)}")
            return {}
        values = self.runner.each(self.mesh, lambda name: self.runner.ssh(
            name, command, check=False, timeout=30, what="process ages"))
        return {name: [int(word) for word in str(text).split() if word.isdigit()]
                for name, text in values.items()}

    def snapshot(self, directory: pathlib.Path, label: str) -> None:
        def one(name: str) -> None:
            text = self.runner.ssh(name, SNAPSHOT, check=False, timeout=60, what=f"snapshot {label}")
            if not self.runner.dry:
                target = directory / "nodes" / name
                target.mkdir(parents=True, exist_ok=True)
                (target / f"snapshot-{label}.txt").write_text(text + "\n")
        self.runner.each(sorted(self.nodes), one)

    def stream_flows(self, edge: str, stream: int) -> bool:
        """Whether the edge has the stream now (its /api/node streams)."""
        if self.runner.dry:
            self.runner.fetch(edge, HTTP_PORT, "/api/node")
            return True
        try:
            body = json.loads(self.runner.fetch(edge, HTTP_PORT, "/api/node", timeout=5))
        except Exception:  # noqa: BLE001 - not yet
            return False
        for item in body.get("streams") or []:
            if item.get("stream_id") == stream and (item.get("ingest_age_ms") or 10**9) < 1000:
                return True
        return False

    def guard(self, item: dict, until: float, stats: dict) -> None:
        """Until `until`: each mesh node's RSS every GUARD_EVERY_S; the
        source stopped when one passes the cell's guard."""
        source = self.purpose("source")
        peak: dict[str, float] = {}
        while self.clock() < until:
            try:
                for name, value in self.rss_mb().items():
                    peak[name] = max(peak.get(name, 0.0), value)
                    if value > item["guard_mb"] + self.extra_memory_mb(name) and not stats.get("cut"):
                        self.runner.ssh(source, STOP_SOURCES, check=False)
                        stats["cut"] = {"node": name, "rss_mb": value,
                                        "after_s": round(self.clock() - stats["from"], 1)}
                        self.runner.say(f"{item['name']}: {name} RSS {value:.0f} MB > "
                                        f"{item['guard_mb']} MB: the source is stopped")
            except Exception as error:  # noqa: BLE001 - a guard read that failed
                self.runner.say(f"rss guard: {error}")
            self.sleep(min(GUARD_EVERY_S, max(0.0, until - self.clock())))
        stats["rss_peak_mb"] = peak

    def pulls(self, item: dict, directory: pathlib.Path, since_us: int) -> None:
        """Each node's own figures at the step's end: /api/node,
        /api/mesh/local and the hop journal of the video stream since the
        step began (the contributor's too, on its port)."""
        def one(name: str) -> None:
            port = CONTRIB_HTTP_PORT if self.nodes[name]["role"] == "contributor" else HTTP_PORT
            target = directory / "nodes" / name
            for path, file in (("/api/node", "api-node.json"),
                               ("/api/mesh/local", "api-mesh-local.json"),
                               (f"/api/hops?since_us={since_us}&stream={VIDEO_STREAM}",
                                "hops-video.ndjson")):
                try:
                    body = self.runner.fetch(name, port, path, timeout=60)
                except Exception as error:  # noqa: BLE001 - a node without it
                    body = json.dumps({"error": str(error)[:300]}).encode()
                if not self.runner.dry:
                    target.mkdir(parents=True, exist_ok=True)
                    (target / file).write_bytes(body)
        self.runner.each(self.mesh, one)

    def increments(self, directory: pathlib.Path) -> None:
        """The samplers' lines since the last step, every node."""
        def one(name: str) -> None:
            data = self.runner.ssh_bytes(name, INCREMENT, what="the samplers' new lines")
            if self.runner.dry or not data:
                return
            target = directory / "samplers" / name
            target.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                archive.extractall(target, filter="data")
        self.runner.each(sorted(self.nodes), one)

    def run_cell(self, item: dict) -> dict:
        """One cell; returns its entry (also in iso.json)."""
        name = item["name"]
        directory = self.out() / "steps" / name
        remote = f"{REMOTE}/{name}"
        entry = {**item, "arm_what": ARMS[item["arm"]]["what"], "started_unix": round(self.clock(), 3)}
        self.runner.say(f"cell {name}: {item['group']} shape {item['shape']} {item['clip']} "
                        f"({item['mbit']} Mbit/s) arm {item['arm']}, {item['seconds']} s")
        source, view = self.purpose("source"), self.purpose("view")
        contributor = self.names("contributor")[0]
        target = (self.nodes[contributor].get("vlan_ipv4") or "<contrib-vlan>").split("/")[0]
        shape = self.shape_nodes(item["shape"])
        self.runner.ssh(source, STOP_SOURCES, check=False)
        if item["restart"]:
            entry["restart_s"] = round(self.restart(item), 1)
        entry["ages_s"] = self.ages()
        if item["restart"] and entry["ages_s"]:
            entry["not_restarted"] = sorted(
                node for node, values in entry["ages_s"].items()
                if (not values or max(values) > entry["restart_s"] + 30)
                and node not in self.shape_nodes(item["shape"])["held_out"])
            self.restart_left(item, entry)
        rss = self.rss_mb()
        entry["rss_start_mb"] = rss
        over = {node: value for node, value in rss.items()
                if value > RSS_START_LIMIT_MB + self.extra_memory_mb(node)}
        if over:
            raise RuntimeError(f"{name}: mesh node(s) over {RSS_START_LIMIT_MB} MB at the start: "
                               f"{over}")
        # The previous cell's viewer records first (its ingest ran during
        # the restart): the viewer host must be idle before this cell's
        # probes, and the 64 s boundary is chosen after the wait.
        self.join_pending()
        printed = self.runner.ssh(source, audio_sender_command(target, f"{REMOTE}/src"))
        start = int(printed.split()[-1]) if not self.runner.dry else int(self.clock()) + 64
        entry["audio_start_unix"] = start
        if not self.runner.dry:
            directory.mkdir(parents=True, exist_ok=True)
        self.snapshot(directory, "start")
        self.wait_until(start - VIDEO_LEAD_S)
        self.runner.ssh(source, video_sender_command(item["clip"], target, f"{REMOTE}/src"))
        entry["video_started_unix"] = round(self.clock(), 3)
        self.wait_until(start + PROBE_AFTER_START_S)
        flows = self.stream_flows(shape["edge"], AUDIO_STREAM)
        if not flows and not self.runner.dry:
            self.sleep(3.0)
            flows = self.stream_flows(shape["edge"], AUDIO_STREAM)
        entry["audio_flowing_at_probes"] = flows
        if not flows:
            self.runner.say(f"{name}: the PCM lane is not at {shape['edge']} yet; probes go on")
        tag = f"iso-{self.run_id}-{name}"
        lane = self.lane or referee_lane("<entry>", None)
        commands = (probe_commands(lane, item["clip"], item["seconds"], tag) if lane is not None
                    else [f"<the referee's probe.uc, probe.h3 and nt-uc of {item['clip']}, "
                          f"{item['seconds']} s>"])
        self.runner.ssh(view, listener_script(remote, commands), what=(
            f"probes on {view} in {remote}: " + " ; ".join(commands)))
        probes_from = self.clock()
        entry["probes_from_unix"] = round(probes_from, 3)
        stats = {"from": probes_from}
        if self.runner.dry:
            self.runner.say(f"# guard: every {GUARD_EVERY_S:g} s each mesh node's RSS; the "
                            f"source stopped above {item['guard_mb']} MB")
        else:
            self.guard(item, probes_from + item["seconds"] + 2, stats)
        self.runner.ssh(source, STOP_SOURCES, check=False)
        entry["probes_to_unix"] = round(probes_from + item["seconds"], 3)
        entry["source_stopped_unix"] = round(self.clock(), 3)
        entry["rss_peak_mb"] = stats.get("rss_peak_mb")
        entry["cut"] = stats.get("cut")
        since_us = int((entry["video_started_unix"]) * 1_000_000)
        self.parallel_parts(item, directory, since_us)
        entry["ended_unix"] = round(self.clock(), 3)
        self.record["cells"].append(entry)
        self.save()
        if not self.runner.dry:
            (directory / "cell.json").write_text(json.dumps(entry, indent=1) + "\n")
        self.pending = threading.Thread(target=self.finish_cell, args=(entry, directory, remote),
                                        daemon=True)
        if self.runner.dry:
            self.finish_cell(entry, directory, remote)
            self.pending = None
        else:
            self.pending.start()
        return entry

    def parallel_parts(self, item: dict, directory: pathlib.Path, since_us: int) -> None:
        """The pulls, the end snapshot and the samplers' new lines, at once
        (dry: one after another, so the print is in order)."""
        parts = (lambda: self.pulls(item, directory, since_us),
                 lambda: self.snapshot(directory, "end"),
                 lambda: self.increments(directory))
        if self.runner.dry:
            for part in parts:
                part()
            return
        threads = [threading.Thread(target=function) for function in parts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def finish_cell(self, entry: dict, directory: pathlib.Path, remote: str) -> None:
        """After a cell: the viewer's records (once its probes and their
        ingest are done), the verdict by the referee's rule, the step into
        the archive."""
        view = self.purpose("view")
        try:
            if not self.runner.dry:
                deadline = self.clock() + 150
                while self.clock() < deadline:
                    if self.runner.ssh(view, f"test -f {remote}/done && echo done",
                                       check=False, timeout=30) == "done":
                        break
                    self.sleep(3.0)
            self.runner.rsync_from(view, f"{remote}/", directory / "viewer")
            if not self.runner.dry and entry.get("scored"):
                row = report_module().step_row(directory)
                entry["whole"] = row.get("whole")
                entry["summary"] = {key: row.get(key) for key in (
                    "on_time_percent", "longest_stall_ms", "latency_p50_ms", "latency_p99_ms",
                    "hls_on_time_percent", "audio_missing", "audio_p99_ms")}
                self.record["verdicts"][entry["name"]] = row.get("whole")
                (directory / "cell.json").write_text(json.dumps(entry, indent=1) + "\n")
                self.runner.say(f"{entry['name']}: {'WHOLE' if row.get('whole') else 'NOT whole'}"
                                f" {json.dumps(entry['summary'])}")
        except Exception as error:  # noqa: BLE001 - recorded; the run goes on
            self.record["failures"].append(f"{entry['name']} finish: {str(error)[-300:]}")
            self.runner.say(f"{entry['name']}: its viewer records: {error}")
        finally:
            self.save()
            self.archive("add-tree", self.run_id, relative(directory),
                         "--as", f"steps/{entry['name']}", "--kind", "raw",
                         "--description", f"cell {entry['name']}: {entry['group']}, shape "
                         f"{entry['shape']}, {entry['clip']}, arm {entry['arm']}")

    def join_pending(self) -> None:
        if self.pending is not None:
            self.pending.join()
            self.pending = None

    def wait_until(self, when: float) -> None:
        if self.runner.dry:
            return
        while self.clock() < when:
            self.sleep(min(1.0, when - self.clock()))

    # ---- the plan

    def fits(self, item: dict) -> bool:
        """Whether a cell, as long as expected plus FIT_MARGIN_S, ends by
        PLAN_END_MINUTE after up began (the worst case of every cell would
        leave out half the plan; a cell that overruns eats the margin
        before collect, never the watchdog's)."""
        if self.runner.dry:
            return True
        return self.elapsed() + cell_seconds(item) + FIT_MARGIN_S <= PLAN_END_MINUTE * 60

    def verdict(self, name: str) -> bool | None:
        """A cell's verdict, once its viewer records are in."""
        if self.runner.dry:
            return True
        self.join_pending()
        return self.record["verdicts"].get(name)

    def try_cell(self, item: dict) -> bool:
        """A cell if it fits; whether it ran without failing."""
        if not self.fits(item):
            self.record["left_out"].append({"name": item["name"],
                                            "elapsed_min": round(self.elapsed() / 60, 2)})
            self.runner.say(f"{item['name']} left out: {self.elapsed() / 60:.1f} min after up "
                            f"began; it would end after minute {PLAN_END_MINUTE}")
            self.save()
            return False
        started = self.clock()
        error = None
        try:
            self.run_cell(item)
        except KeyboardInterrupt:
            raise
        except Exception as caught:  # noqa: BLE001 - recorded; the next cell goes on
            error = str(caught)[-500:]
            self.runner.say(f"{item['name']} failed: {error}")
            self.record["failures"].append(f"{item['name']}: {error}")
            self.runner.ssh(self.purpose("source"), STOP_SOURCES, check=False)
        self.record["steps"].append({"name": item["name"], "from_unix": round(started, 3),
                                     "to_unix": round(self.clock(), 3), "ok": error is None,
                                     **({"error": error} if error else {})})
        self.save()
        return error is None

    def ramp(self, cells: list[dict], label: str) -> dict[str, bool]:
        """A ramp's cells; 333 only after a whole 247 (the only verdict the
        ramp waits for; the others are read when the ramp ends)."""
        ran = []
        for item in cells:
            if item["rung"] == TOP_RUNG and not self.verdict(ran_name(ran, "247")):
                self.runner.say(f"{item['name']} not run: 247 was not whole on {label}")
                continue
            if self.try_cell(item):
                ran.append(item["name"])
        out = {}
        for name in ran:
            value = self.verdict(name)
            if value is not None:
                out[name.lstrip("BT")] = value
        return out

    def plan_run(self) -> None:
        """The cells, in priority order, the plan made again as verdicts
        come in."""
        self.try_cell(warmup_cell())
        base = self.ramp(base_cells(), "one forwarder")
        self.record["base"] = base
        two = self.ramp(two_cells(), "two forwarders")
        self.record["two"] = two
        high, first = ab_rungs(base) if not self.runner.dry else ("247", "333")
        self.record["ab_rungs"] = {"highest_whole": high, "first_not_whole": first}
        self.runner.say(f"A/B at the highest whole rung {high} and the first not whole {first}")
        self.save()
        for item in ab_cells(base if not self.runner.dry else None):
            self.try_cell(item)
        self.try_cell(soak_cell())
        self.join_pending()

    # ---- the end

    def collect(self) -> None:
        """Everything into target/4k-iso/<run id>/ and the run archive
        before the lab is deleted."""
        self.join_pending()
        failures = []
        names = sorted(self.nodes)
        try:
            if self.runner.dry:
                self.runner.say(f"# the referee's collect_samplers on {', '.join(names)} "
                                "(whole logs into samplers/)")
            else:
                module = self.referee or hunt_module().referee_samplers(compare_root())
                module.collect_samplers(self.referee_session(names),
                                        [self.nodes[name] for name in names])
        except Exception as error:  # noqa: BLE001 - the rest is collected all the same
            failures.append(f"samplers: {str(error)[-300:]}")
        facts = ("mkdir -p {r}; cat {r}/extra.pid 2>/dev/null | xargs -r kill 2>/dev/null; "
                 "uname -a >{r}/system.txt; cat /sys/kernel/mm/transparent_hugepage/enabled "
                 ">>{r}/system.txt; nproc >>{r}/system.txt; "
                 "journalctl -k -o short-iso-precise --no-pager >{r}/kernel.log 2>&1; true"
                 ).format(r=REMOTE)

        def records(name: str) -> None:
            self.runner.ssh(name, facts, check=False, what="stop the extra sampler; system "
                                                            "facts, kernel log")
            data = self.runner.ssh_bytes(name, f"tar -C {REMOTE} --exclude=./inc -czf - .",
                                         timeout=600, what=f"tar {REMOTE}")
            if data:
                target = self.out() / "nodes" / name
                target.mkdir(parents=True, exist_ok=True)
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                    archive.extractall(target, filter="data")
        try:
            self.runner.each(names, records)
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
        began_utc = run_archive.utc_now()
        collected = self.deploy("collect", "--run-id", self.run_id)
        missing = {line.split(":", 1)[0] for line in failures}
        if self.archive("add-tree", self.run_id, relative(self.out()), "--as", "iso",
                        "--kind", "raw", "--exclude", "steps",
                        "--description", "the isolation lab: iso.json, the journal, every "
                        "node's whole sampler logs, extra sampler, kernel log") != 0:
            missing.add(f"{relative(self.out())} (add-tree)")
        missing = sorted(missing | set(self.deploy_missing(collected, began_utc)))
        self.archive("collected", self.run_id,
                     *(["--complete"] if not missing else
                       ["--incomplete", *[item for name in missing for item in ("--missing", name)]]))

    def deploy_missing(self, status: int, began_utc: str) -> list[str]:
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
        status, output = self.lab("down", capture=True)
        if self.runner.dry:
            self.runner.say(f"mv {relative(common.ARTIFACT_DIR)}/{{settings,routes}}.json "
                            f"{relative(self.out())}/deploy/")
            return
        if status != 0 or STATE.exists():
            raise RuntimeError("lab down failed: run `linode-4k-isolate.py down` again; the "
                               "watchdog deletes the lab at its deadline")
        artifacts = self.out() / "deploy"
        artifacts.mkdir(parents=True, exist_ok=True)
        for file in ("settings.json", "routes.json", "ecmp-ports.json", "slow-hosts.json"):
            if (common.ARTIFACT_DIR / file).is_file():
                shutil.move(str(common.ARTIFACT_DIR / file), str(artifacts / file))
        self.record["down_unix"] = round(self.clock(), 3)
        self.record["down_output"] = output.strip().splitlines()[-3:] if output else []
        self.save()

    # ---- the run

    def bring_up(self) -> None:
        if self.resume:
            # The lab that `up` made earlier (a run cut before its push):
            # its nodes and its creation, the plan's clock from there.
            self.use_lab_nodes()
            self.up_started = float(self.record.get("created_unix") or self.clock())
            self.record["up_started_unix"] = self.up_started
            self.record["resumed_unix"] = round(self.clock(), 3)
            self.runner.say(f"resumed on the lab up since {self.elapsed() / 60:.1f} min")
        else:
            self.up()
        self.hosts("first")
        self.wait_ssh()
        failures = []
        threads = []
        if not self.runner.dry:
            # The clips (about 10 GB to lon-src) go while push bootstraps
            # and warms; push writes nothing to /var/lib/compare.
            media_error: list[str] = []

            def media() -> None:
                try:
                    self.media()
                except Exception as error:  # noqa: BLE001 - named below
                    media_error.append(str(error))
            threads.append(threading.Thread(target=media))
            threads[0].start()
        else:
            self.media()
        self.push()
        for thread in threads:
            thread.join()
        if not self.runner.dry and media_error:
            failures.append("media: " + "; ".join(media_error))
            self.media()
        self.warm_table()
        self.start_samplers()
        module = self.referee or load_by_path(
            "iso_referee_run", compare_root() / "scripts" / "compare" / "compare-run.py")
        self.lane = referee_lane(self.nodes["lon-dist-a"]["public_ipv4"], module)

    def run(self) -> int:
        failed = 0
        archived = False
        try:
            if not self.runner.dry:
                self.runner.say(f"run {self.run_id}: records in {relative(self.out())}")
            self.archive_create()
            archived = True
            self.bring_up()
            self.plan_run()
        except KeyboardInterrupt:
            self.interrupted = True
            self.runner.say("interrupted: collecting, then taking the lab down")
        except Exception as error:  # noqa: BLE001 - the lab is taken down whatever failed
            failed += 1
            self.record["failures"].append(f"run: {str(error)[-500:]}")
            self.runner.say(f"the run stopped: {error}")
        finally:
            if self.runner.dry or STATE.exists():
                for name, step in (("collect", self.collect), ("down", self.down)):
                    try:
                        self.runner.say(f"# {name}")
                        step()
                    except KeyboardInterrupt:
                        self.runner.say("interrupted again: going on to down")
                    except Exception as error:  # noqa: BLE001 - down must still run
                        failed += 1
                        self.runner.say(f"{name} failed: {error}")
            if archived:
                self.archive("finish", self.run_id, "--status",
                             "failed" if failed or self.interrupted else "complete",
                             *(["--reason", "interrupted" if self.interrupted else
                                f"{failed} step(s) failed"] if failed or self.interrupted else []))
        return 1 if failed or self.interrupted else 0


# ------------------------------------------------------------- the preflight

def help_text(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout + result.stderr).decode(errors="replace")


def preflight(topology: dict) -> list[str]:
    """Why a real run must not start, each found without a request."""
    problems = []
    if STATE.exists():
        problems.append(f"{relative(STATE)} exists: an isolation lab is up (collect and down first)")
    if common.DEFAULT_STATE.exists():
        problems.append(f"{relative(common.DEFAULT_STATE)} exists: another lab is deployed from "
                        "this checkout")
    for file in ("settings.json", "routes.json"):
        if (common.ARTIFACT_DIR / file).is_file():
            problems.append(f"{relative(common.ARTIFACT_DIR / file)} is left from another lab")
    if hourly_usd(topology) > MAX_HOURLY_USD or len(topology["nodes"]) > MAX_NODES:
        problems.append("the topology is over the lab's ceilings")
    if hourly_usd(topology) * BILLED_HOURS > LAB_CEILING_USD:
        problems.append(f"two billed hours cost more than ${LAB_CEILING_USD:g}")
    up_help = help_text([sys.executable, str(LAB_TOOL), "up", "--help"])
    for flag in ("--watchdog-hours", "--budget-usd", "--collect-timeout-s", "--run-id"):
        if flag not in up_help:
            problems.append(f"the lab tool's `up` has no {flag}")
    push_help = help_text([sys.executable, str(DEPLOY_TOOL), "push", "--help"])
    if "--slow-host" not in push_help:
        problems.append("the deploy tool's push has no --slow-host (it must be report only)")
    restart_help = help_text([sys.executable, str(DEPLOY_TOOL), "restart", "--help"])
    if "--roles" not in restart_help:
        problems.append("the deploy tool's restart has no --roles")
    compiler = ROOT / "target" / "release" / "needletail-compile"
    files = [compiler, mesh_binary("av-mesh"), mesh_binary("needletail-udp-client"),
             mesh_binary("needletail-nodecheck"),
             contrib_root() / "target" / "x86_64-unknown-linux-gnu" / "release" / "av-contrib",
             contrib_root() / "target" / "x86_64-unknown-linux-gnu" / "release" / "aep1-48k-probe",
             compare_root() / "target" / "ntcmp", clips_root() / "audio.s24le",
             compare_root() / "target" / "compare-media" / "audio" / AUDIO_INDEX,
             ROOT / "deploy" / "linode-small-mesh" / "soundkit-part-check" / "target"
             / "x86_64-unknown-linux-gnu" / "release" / "soundkit-part-check", REPORT_TOOL]
    files += [clips_root() / f"{clip}.index.json" for clip, _ in RUNGS.values()]
    files += [clips_root() / clip / "video.h264" for clip, _ in RUNGS.values()]
    for path in files:
        if not path.is_file():
            problems.append(f"missing: {path}")
    if mesh_binary("av-mesh").is_file():
        text = help_text([str(mesh_binary("av-mesh")), "--help"])
        for flag in ("--relay-video-socket-buffer-ms", "--udp-socket-buffer-bytes",
                     "--relay-video-gro", "--relay-video-gso", "--relay-class-cpus"):
            if flag not in text:
                problems.append(f"av-mesh has no {flag}: an A/B arm cannot be set")
    try:
        hunt = hunt_module()
        hunt.referee_samplers(compare_root())
        module = load_by_path("iso_referee_check", compare_root() / "scripts" / "compare"
                              / "compare-run.py")
        lane = referee_lane("192.0.2.1", module)
        probe_commands(lane, "2160p40", 10, "check")
    except Exception as error:  # noqa: BLE001 - any failure refuses the run
        problems.append(f"the referee's samplers or probes do not load ({error})")
    try:
        report = report_module()
        for name in ("step_row", "rows", "limits"):
            if not callable(getattr(report, name, None)):
                problems.append(f"{relative(REPORT_TOOL)} has no {name}")
    except Exception as error:  # noqa: BLE001
        problems.append(f"{relative(REPORT_TOOL)} does not load ({error})")
    return problems


# ----------------------------------------------------------------- commands

def default_run_id() -> str:
    return "iso" + dt.datetime.now(dt.timezone.utc).strftime("%m%d%H%M")


def print_plan(topology: dict) -> None:
    figures = cost(topology)
    by_plan: dict[str, int] = {}
    for node in topology["nodes"]:
        by_plan[node["type"]] = by_plan.get(node["type"], 0) + 1
    print(f"{figures['instances']} instances ("
          + ", ".join(f"{count} {plan}" for plan, count in sorted(by_plan.items()))
          + f"): ${figures['hourly_usd']:.3f} an hour; {BILLED_HOURS} billed hours "
          f"${figures['planned_usd']:.2f} (the lead's ceiling ${LAB_CEILING_USD:g}); lab ceilings "
          f"${MAX_HOURLY_USD:.2f} an hour and {MAX_NODES} nodes; watchdog {WATCHDOG_HOURS:g} h "
          f"({WATCHDOG_HOURS * 60:.0f} min, its collect from about minute "
          f"{WATCHDOG_HOURS * 60 - 5 - COLLECT_TIMEOUT_S / 60:.0f})")
    for name, shape in topology["iso"]["shapes"].items():
        print(f"shape {name}: edge {shape['edge']} (held out: {', '.join(shape['held_out'])}), "
              f"forwarder {shape['forwarder']}, no video at: "
              f"{', '.join(shape['blind']) or 'none (both forwarders feed the edge)'}")
    expected = BRINGUP_S[0]
    worst = BRINGUP_S[1]
    print(f"schedule (minutes after up began; expected / worst; a cell starts only when its "
          f"expected length plus {FIT_MARGIN_S / 60:g} min ends by minute {PLAN_END_MINUTE}, then "
          f"collect and down, {COLLECT_AND_DOWN_S / 60:.0f} min):")
    print(f"  {expected / 60:5.1f} / {worst / 60:5.1f}  up, hosts, push --slow-host report (the "
          "warm pass on every host), media, samplers")
    for item in schedule():
        expected += cell_seconds(item)
        worst += cell_seconds(item, worst=True)
        print(f"  {expected / 60:5.1f} / {worst / 60:5.1f}  {item['name']:16} {item['group']:6} "
              f"shape {item['shape']:3} {item['clip']:9} {item['mbit']:5.1f} Mbit/s "
              f"{item['seconds']:3d} s  {ARMS[item['arm']]['what']}"
              + ("" if item["scored"] else " (not scored)"))
    print(f"  the plan is made again as it runs: 333 only after a whole 247; the A/B rungs are "
          "the highest BASE rung that was whole and the first that was not (shown here as 247 "
          "and 333); cells that would end after the limit are left out from the end")
    pending = [name for name, spec in ARMS.items() if spec.get("pending")]
    print(f"  slot for later arms (not in this build): {', '.join(pending)}")


def command_plan(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.topology)
    topology = load_topology(path)
    print_plan(topology)
    Iso(Runner(True, OUT / args.run_id), topology, path, args.run_id).run()


def command_run(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.topology)
    topology = load_topology(path)
    problems = preflight(topology)
    if args.resume:
        # The lab up is this run's own (checked below).
        problems = [problem for problem in problems if not problem.startswith(relative(STATE))]
    if args.dry:
        print_plan(topology)
        for problem in problems:
            print(f"# a real run would refuse: {problem}")
    elif problems:
        sys.exit("the isolation lab does not start (nothing was created):\n  "
                 + "\n  ".join(problems))
    if args.resume:
        if not STATE.exists() and not args.dry:
            sys.exit("--resume: no isolation lab is up")
        if STATE.exists() and json.loads(STATE.read_text()).get("run_id") != args.run_id:
            sys.exit("--resume: the lab that is up is not run " + args.run_id)
    iso = Iso(Runner(args.dry, OUT / args.run_id), topology, path, args.run_id,
              args.budget_usd, args.spent_before_usd)
    iso.resume = args.resume
    if not args.dry:
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGHUP, lambda *_: hung_up(iso.out()))
    sys.exit(iso.run())


def interrupt(*_args) -> None:
    raise KeyboardInterrupt()


def hung_up(out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    log = os.open(out / "after-hangup.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(log)
    interrupt()


def existing(args: argparse.Namespace) -> Iso:
    if not STATE.exists():
        if not args.dry:
            sys.exit(f"{relative(STATE)} is missing: no isolation lab is up")
        return Iso(Runner(True, OUT / default_run_id()), load_topology(), TOPOLOGY,
                   default_run_id())
    state = json.loads(STATE.read_text())
    path = pathlib.Path(state["topology_file"])
    iso = Iso(Runner(args.dry, OUT / state["run_id"]), load_topology(path), path, state["run_id"])
    saved = iso.out() / "iso.json"
    if saved.is_file():
        iso.record = json.loads(saved.read_text())
    iso.use_lab_nodes()
    return iso


def command_cells(args: argparse.Namespace) -> None:
    """Named cells of the plan on the lab that is up, again, each under
    its name plus `--suffix` (a cell whose run went wrong keeps its record):
    no up, no push, no collect, no down. The samplers that `run` started
    are still running."""
    iso = existing(args)
    iso.up_started = float(iso.record.get("up_started_unix") or iso.record.get("created_unix")
                           or time.time())
    if not args.dry:
        iso.referee = hunt_module().referee_samplers(compare_root())
    module = iso.referee or load_by_path(
        "iso_referee_run", compare_root() / "scripts" / "compare" / "compare-run.py")
    iso.lane = referee_lane(iso.nodes["lon-dist-a"]["public_ipv4"], module)
    by_name = {item["name"]: item for item in schedule()}
    for name in args.names.split(","):
        if name not in by_name:
            sys.exit(f"no cell {name} in the plan: " + ", ".join(by_name))
        item = {**by_name[name], "name": name + (f"-{args.suffix}" if args.suffix else "")}
        iso.try_cell(item)
    iso.join_pending()
    iso.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="the cost, the schedule and every command; no request")
    plan.add_argument("--topology", default=str(TOPOLOGY))
    plan.add_argument("--run-id", default=default_run_id())
    plan.set_defaults(handler=command_plan)
    run = sub.add_parser("run", help="up, the cells, collect, down")
    run.add_argument("--topology", default=str(TOPOLOGY))
    run.add_argument("--run-id", default=default_run_id())
    run.add_argument("--budget-usd", type=float, default=None)
    run.add_argument("--spent-before-usd", type=float, default=None)
    run.add_argument("--dry", action="store_true", help="print every command and run none")
    run.add_argument("--resume", action="store_true",
                     help="go on with the lab of this run id that is up (no `up`): a run cut "
                          "before its cells")
    run.set_defaults(handler=command_run)
    cells = sub.add_parser("cells", help="named cells again on the lab that is up (no up, push, "
                                         "collect or down)")
    cells.add_argument("--names", required=True, help="cells of the plan, comma-separated")
    cells.add_argument("--suffix", default="", help="appended to each cell's name")
    cells.add_argument("--dry", action="store_true")
    cells.set_defaults(handler=command_cells)
    for name, handler, text in (("collect", lambda args: existing(args).collect(),
                                 "fetch the records of the lab that is up"),
                                ("down", lambda args: existing(args).down(),
                                 "take the lab down through the lab tool")):
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
