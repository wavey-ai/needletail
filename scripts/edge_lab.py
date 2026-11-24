#!/usr/bin/env python3
"""A two-region Needletail mesh on one host, built to be broken.

Every node is its own network namespace with its own interfaces, as a Linode
is: `eth0` on the public network of its site and `eth1` on the private
network (VLAN) of its region. The links are veth pairs into bridges. The
public networks of the sites meet in a router namespace, the wide-area
network, and each site reaches it over a link with the delay of its distance.
Every link takes `tc netem` in each direction: delay, jitter, random and burst
loss, reordering, duplication and a rate cap. What a node sends also passes
the deploy tool's host qdisc where the kernel allows it (`host_qdisc`: a prio
child of the netem, audio first). Every service runs in its own cgroup with
the limits of a g6-dedicated-2: two CPUs and 4 GB, no swap.

The relay DAG is the deploy tool's `topology` setting. "merged" (the
default, low-latency item 4): the contributor feeds lon-dist-a and
lon-dist-b, which feed new-dist-a and new-dist-b crossed; the two backbone
namespaces stay empty (no service). `up topology=legacy` puts the backbone
relays relay-primary and relay-secondary between them again. Edges are fed
by the fast plane from the two distributors of their site, beside the cache
mesh (the deploy tool's `edge_fast_plane`; `up edge_fast_plane=false` leaves
them to the cache mesh alone, for an A/B).

Nothing needs root. The namespaces belong to a rootless user namespace, and
the cgroups are scopes of the user's systemd manager.

  edge_lab.py up [key=value ...]        build the networks and start every service
  edge_lab.py down                      stop everything and remove the namespaces
  edge_lab.py status                    the labs on this host, and one line for each
                                        node: build set, build, state, drain reasons
  edge_lab.py sh <node> <command...>    run a command in the namespace of a node
  edge_lab.py net <node> <pub|vlan> <out|in|both> <netem spec | clear>
  edge_lab.py wan <site> <out|in|both> <netem spec | clear>
  edge_lab.py sources <seconds>         start the 4K video and the audio sources in
                                        lon-src: every AEP1 epoch to both contributors
  edge_lab.py start <node>              start a node's service (a slot: take it into
                                        service)
  edge_lab.py stop <node>               stop a node's service (SIGTERM; a slot: out of
                                        service again)
  edge_lab.py drain <node>              POST /api/control/drain-node for that node
  edge_lab.py resume <node>             POST /api/control/resume-node for that node
  edge_lab.py crash <node> [--restart-s 1]
                                        SIGKILL a node's service, start it again
                                        after the unit's RestartSec, and return
                                        when it is back (ready, or /up answers)
  edge_lab.py burn <node> <seconds>     busy loops in the node's own cgroup, on its CPUs
  edge_lab.py kill <node> [--unprotected] [--video-gap accept] [--gate-timeout-s 30]
                                        a contributor or a first forwarder killed (SIGKILL)
                                        and held down, behind workstream A's gate; exit 2
                                        and nothing touched when the other contributor (or
                                        forwarder) does not carry every stream
  edge_lab.py return <node> [--ready-timeout-s 60]
                                        the node `kill` held down started and made ready
  edge_lab.py freeze <node> --ms N      a contributor or a first forwarder stopped
                                        (SIGSTOP) for N ms and thawed
                                        (kill, return, freeze: stdout is their two marks,
                                        --marks FILE appends them; scripts/contributor_changes.py)
  edge_lab.py roll <edges|forwarders|contributors|all> [--build a|b] [--nodes a,b]
              [--sites s,..] [--force] [--dry-marks] [--resume] [engine options]
                                        a roll through the change engine (mesh_ops):
                                        one node at a time, gated, made ready, rolled
                                        back on the build's failure; stdout is its two
                                        marks. Without --build: a rolling restart of
                                        each node on its own build set
  edge_lab.py restart <node> [--hold-s 0] [--rollback] [--dry-marks] [--resume]
                                        one node restarted through the engine's gates
  edge_lab.py replace <node> [--gap-s 30] [--no-slot] [--resume]
                                        the engine's replace: an edge drained, stopped,
                                        "rebuilt" (--gap-s down, its drain file gone),
                                        started and resumed; a forwarder through its
                                        site's dist-c slot
  edge_lab.py rt-check [node ...]       is each node's audio class thread SCHED_FIFO
                                        and pinned (rt=1): its fast plane's counters
                                        and the kernel's view of its threads; exit 1
                                        when one is not
  Engine options: --marks FILE --node-marks FILE --gate-timeout-s 30
  --drain-timeout-s 30 --ready-timeout-s 60 --on-failure stop|rollback-all
  --surge auto|never --parity-ms 1.0. Exit status is the engine's (0 done, 2
  refused, 3 stopped, 4 rolled back, 5 no answer, 130 interrupted).

Slot nodes (low-latency ops item 7): nodes that `up` renders, with every
peer list naming them, but does not start, as the large mesh's autoscale
slots: lon-dist-c and new-dist-c always (a third forwarder of its site, a
fast-plane lease child of the site's dist-a and dist-b and a cache-mesh
parent of its edges, never a compiled relay child), and lon-edge-4 and
new-edge-3 with `up slots=lon-edge-4,new-edge-3`. A slot is in service once
`start` (or the autoscaler's edge_lab backend) takes it in; until then
edges() and distributors() leave it out, and restore() does not start it.
Starting one restarts no other service: their arguments already name it.

Several labs on one host. A lab is its state directory. NEEDLETAIL_EDGE_LAB_ID
(one to eight of a-z and 0-9) names it: the state directory is then
target/elab-<id> (NEEDLETAIL_EDGE_LAB_WORK still overrides the directory).
Networks, addresses and ports are inside each lab's own namespaces; the
systemd units carry the state directory's name and a hash of its path. `up`
writes the lab into a registry that every worktree of the host shares
($XDG_RUNTIME_DIR/needletail-labs/<id>.json) and `down` removes it:
- `up` on a running lab of another owner (NEEDLETAIL_EDGE_LAB_OWNER, by
  default the id), or on an id that runs from another state directory, is
  refused unless `up take=1`, which stops that lab first.
- `up cpus=A-B` (or NEEDLETAIL_EDGE_LAB_CPUS=A-B) keeps everything the lab
  runs on host CPUs A to B: the last max(2, N // 4) of them for the reader
  hosts, the load tool, probes, senders and the lab's own commands, the
  others for the nodes, each a window of as many CPUs as its limit, taken
  round robin (nodes share CPUs when the range is short; a held slot takes
  its window when it is taken in). A lab without a range may run on every
  CPU. CPUs that a registered lab has are refused unless `up
  cpus_overlap=1`, and so is any CPU while a lab that the registry does not
  know runs. The standing layout of two labs on the 32 CPU host: lab a on
  2-15, lab b on 16-29.
- `up node_cpus=FIRST` is the layout of one lab alone: each node its own
  CPUs from FIRST on (the contributors 2 each, lon-src 2, lon-edge-4 and
  new-edge-3 included, the two dist-c slots none: they cannot be taken in
  under it), the reader hosts on every other CPU of the host: 4 CPUs on the
  32 CPU host from 0.
- Whatever the lab runs beside a node's service is in one cgroup, the scope
  needletail-lab-...-tools.scope, the process that drives the lab included,
  so the lab's own CPU time can be read (scope_cpu_usec).

Real time as on Linode (`up rt=1`, off by default). A Linode unit gives its
service CAP_SYS_NICE: the audio class thread runs SCHED_FIFO 10 pinned to
the instance's CPU 0, the video one nice -10 on CPU 1. Under rt=1:
- every node has exactly two CPUs (RT_CPUS), the contributor too, and is
  rendered as a g6-dedicated-2, so that the deploy's relay_class_cpus
  "auto" gives it "0,1" as on Linode, which become its own two CPUs; in a
  range the edges have CPUs of their own when they fit and the other nodes
  share the rest of the ring (`up rt=1 slots=lon-edge-4,new-edge-3` fits
  five edges and the rest in 16-31);
- each service's scope first raises its own RLIMIT_RTPRIO (99) and
  RLIMIT_NICE (40) with `sudo -n prlimit --pid $$` and then becomes the
  service as the user (RT_GRANT): a capability would not reach a process
  in the lab's user namespace, these limits do. Without passwordless sudo
  for prlimit, or without a CPU layout, `up rt=1` is refused before
  anything is stopped;
- `up` returns only when every av-mesh node's audio class thread is seen
  SCHED_FIFO 10 on its one CPU, by its fast plane's counters and by ps
  (rt-check.json; `edge_lab.py rt-check` again at any time).
`up rt=pin` is the same layout, render and pinning without the grant (the
control: av-mesh is refused SCHED_FIFO and runs its fallback), checked the
same way for the pinning alone.
The host's own sched_rt_runtime_us is what it is (1000000 here: no
throttling of real-time threads); a Linode guest's kernel has its own.

Two builds in one lab. With NEEDLETAIL_AV_MESH_BIN_NEXT or
NEEDLETAIL_AV_CONTRIB_BIN_NEXT, `up` keeps a second set of binaries (set b,
beside set a) and renders it too, with its own binaries' flags; `up
b.<setting>=<value>` gives set b a setting of its own. Every node starts on
set a; start_service(name, build="b") starts one on set b.

The fault campaign (`edge-campaign.py`) drives this module.

Sibling checkouts are found under the workspace root. Override them with
NEEDLETAIL_AV_MESH_ROOT, NEEDLETAIL_AV_CONTRIB_ROOT and NEEDLETAIL_PROBE_BIN.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import contributor_changes  # noqa: E402
import mesh_ops  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent


def lab_id(environ=os.environ) -> str | None:
    """NEEDLETAIL_EDGE_LAB_ID: the name of this lab among the labs of the
    host, one to eight of a-z and 0-9, or None."""
    value = environ.get("NEEDLETAIL_EDGE_LAB_ID") or None
    if value is not None and not re.fullmatch(r"[a-z0-9]{1,8}", value):
        sys.exit(f"NEEDLETAIL_EDGE_LAB_ID={value!r}: a lab id is one to eight of a-z and 0-9")
    return value


def work_dir(environ=os.environ) -> pathlib.Path:
    """The state directory: NEEDLETAIL_EDGE_LAB_WORK, else target/elab-<id>
    of this checkout for a lab with an id, else target/edge-lab."""
    ident = lab_id(environ)
    given = environ.get("NEEDLETAIL_EDGE_LAB_WORK")
    if given:
        return pathlib.Path(given)
    return ROOT / "target" / (f"elab-{ident}" if ident else "edge-lab")


LAB_ID = lab_id()
WORK = work_dir()
MESH_ROOT = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AV_MESH_ROOT", str(WORKSPACE / "av-mesh-features")))
AV_MESH = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AV_MESH_BIN", str(MESH_ROOT / "target" / "release" / "av-mesh")))
READER = pathlib.Path(os.environ.get(
    "NEEDLETAIL_READER_BIN", str(MESH_ROOT / "target" / "release" / "needletail-reader")))
# The UDP listener crowds of the scale scenarios (av-mesh low-latency, where
# scale-load is merged).
# Optional: a lab without it runs every scenario but those.
LOAD_BIN = pathlib.Path(os.environ.get(
    "NEEDLETAIL_LOAD_BIN", str(MESH_ROOT / "target" / "release" / "needletail-load")))
CONTRIB_ROOT = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AV_CONTRIB_ROOT", str(WORKSPACE / "av-contrib-features")))
AV_CONTRIB = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AV_CONTRIB_BIN", str(CONTRIB_ROOT / "target" / "release" / "av-contrib")))
PROBE = pathlib.Path(os.environ.get(
    "NEEDLETAIL_PROBE_BIN", str(CONTRIB_ROOT / "target" / "release" / "aep1-48k-probe")))
# The second build set (set b) of a lab that runs two builds side by side:
# the av-mesh and the av-contrib that a roll or an upgrade moves nodes to.
AV_MESH_NEXT = (pathlib.Path(os.environ["NEEDLETAIL_AV_MESH_BIN_NEXT"])
                if os.environ.get("NEEDLETAIL_AV_MESH_BIN_NEXT") else None)
AV_CONTRIB_NEXT = (pathlib.Path(os.environ["NEEDLETAIL_AV_CONTRIB_BIN_NEXT"])
                   if os.environ.get("NEEDLETAIL_AV_CONTRIB_BIN_NEXT") else None)
VIDEO_CLIP = pathlib.Path(os.environ.get(
    "NEEDLETAIL_VIDEO_CLIP",
    str(pathlib.Path.home() / "media" / "4k-encodes" / "tos24-16m.ts")))
# A ladder of the clip: the 4K original and three smaller encodes of it with
# the same closed two-second group of pictures (see ENCODES.md). Each entry is
# stream, file, peak bit/s, average bit/s, frame, frame rate in mHz.
LADDER_DIR = pathlib.Path(os.environ.get(
    "NEEDLETAIL_VIDEO_LADDER", str(VIDEO_CLIP.parent / "ladder")))
LADDER = [
    (1, VIDEO_CLIP, 18_000_000, 16_600_000, "3840x2160", 24_000),
    (11, LADDER_DIR / "tos24-1080p-5m.ts", 6_000_000, 5_350_000, "1920x1080", 24_000),
    (12, LADDER_DIR / "tos24-720p-2m.ts", 2_800_000, 2_480_000, "1280x720", 24_000),
    (13, LADDER_DIR / "tos24-360p-700k.ts", 1_100_000, 950_000, "640x360", 24_000),
]
RTMP_PORT = 19350
# ntcmp of the referee's harness (needletail-compare), which sends a clip of
# its prepare-media to a contributor (`ntcmp flv`).
NTCMP = pathlib.Path(os.environ.get(
    "NEEDLETAIL_NTCMP_BIN", str(WORKSPACE / "needletail-compare" / "target" / "ntcmp")))


def referee_clip(path: pathlib.Path) -> dict | None:
    """A clip directory of needletail-compare's prepare-media (DIR/video.h264,
    its media.json beside DIR): {"rung", "fps", "kbps"}, else None."""
    path = pathlib.Path(path)
    if not (path.is_dir() and (path / "video.h264").is_file()):
        return None
    out = {"rung": path.name, "fps": 24, "kbps": None}
    for media in (path.parent / "media.json", path.parent.parent / "clips6" / "media.json"):
        try:
            data = json.loads(media.read_text())
        except (OSError, ValueError):
            continue
        out["fps"] = int(data.get("fps") or 24)
        out["kbps"] = ((data.get("rungs") or {}).get(path.name) or {}).get("kbps")
        break
    return out
# The source host's 4K feed loop (A 3.6): one contributor at a time.
VIDEO_FEED = pathlib.Path(__file__).resolve().parent / "video-feed.sh"


def ladder_renditions() -> list[str]:
    """The `hls_renditions` setting of the ladder."""
    master = LADDER[0][0]
    return [f"{master}:{stream}:{peak}:{average}:{frame}:{rate}"
            for stream, _path, peak, average, frame, rate in LADDER]


AUDIO_PCM = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AUDIO_PCM",
    str(WORKSPACE / "needletail" / "target" / "linode-qualification" / "media"
        / "daw-nexus-album-1-track-pcm-300s" / "source-track-00.s24le")))
def newest_build(*paths: pathlib.Path) -> pathlib.Path:
    """The most recently built of `paths` that exists (else the first): a
    native build and a cross build (zigbuild, for Linode) of one checkout
    sit in different target directories, and the older one can lack the
    options the lab passes (a cross build of 19 Nov has no
    --opus-frame-samples, so the DAW source refused to start)."""
    found = [path for path in paths if path.is_file()]
    return max(found, key=lambda path: path.stat().st_mtime) if found else paths[0]


DAW_SOURCE = pathlib.Path(os.environ.get(
    "NEEDLETAIL_DAW_SOURCE_BIN",
    str(newest_build(
        WORKSPACE / "daw-nexus" / "target" / "release" / "daw-test-source",
        WORKSPACE / "daw-nexus" / "target" / "x86_64-unknown-linux-gnu" / "release"
        / "daw-test-source"))))
DAW_TRACKS = pathlib.Path(os.environ.get(
    "NEEDLETAIL_DAW_TRACKS",
    str(WORKSPACE / "needletail" / "target" / "linode-qualification" / "media"
        / "daw-nexus-album-1-track-pcm-600s")))
SERVER_NAME = "mesh.needletail.test"


def daw_tracks() -> pathlib.Path:
    """The audio album of the lab: the default, or the album with the number
    of tracks that the settings of the lab name."""
    tracks = load("settings.json").get("daw_album_tracks") or 1
    if tracks == 1:
        return DAW_TRACKS
    found = sorted(DAW_TRACKS.parent.glob(f"daw-nexus-album-{tracks}-track-pcm-*"))
    if not found:
        raise LabError(f"no audio album with {tracks} tracks in {DAW_TRACKS.parent}")
    return found[-1]
HTTP_PORT = 19444
CONTRIB_HTTP_PORT = 19443
VIDEO_STREAM = 1
AUDIO_STREAM = 2001
HQ_STREAM = 3001

# name, role, site, region, public address, private (VLAN) address
NODES = [
    ("contrib-lon", "contributor", "london", "gb-lon", "10.90.0.5", "10.77.1.5"),
    ("relay-primary", "backbone", "london", "gb-lon", "10.90.0.6", "10.77.1.6"),
    ("relay-secondary", "backbone", "amsterdam", "nl-ams", "10.90.1.2", None),
    ("lon-dist-a", "distributor", "london", "gb-lon", "10.90.0.11", "10.77.1.11"),
    ("lon-dist-b", "distributor", "london", "gb-lon", "10.90.0.12", "10.77.1.12"),
    ("lon-edge-1", "edge", "london", "gb-lon", "10.90.0.21", "10.77.1.21"),
    ("lon-edge-2", "edge", "london", "gb-lon", "10.90.0.22", "10.77.1.22"),
    ("lon-edge-3", "edge", "london", "gb-lon", "10.90.0.23", "10.77.1.23"),
    ("lon-edge-4", "edge", "london", "gb-lon", "10.90.0.24", "10.77.1.24"),
    # A slot (SLOT_CANDIDATES): rendered, never started by `up`.
    ("lon-dist-c", "distributor", "london", "gb-lon", "10.90.0.13", "10.77.1.13"),
    ("new-dist-a", "distributor", "newyork", "us-east", "10.90.2.11", "10.77.2.11"),
    ("new-dist-b", "distributor", "newyork", "us-east", "10.90.2.12", "10.77.2.12"),
    ("new-edge-1", "edge", "newyork", "us-east", "10.90.2.21", "10.77.2.21"),
    ("new-edge-2", "edge", "newyork", "us-east", "10.90.2.22", "10.77.2.22"),
    # The far region's slot candidates: an edge and a third forwarder, so
    # that the forwarder tier and a second region scale in the lab too.
    ("new-edge-3", "edge", "newyork", "us-east", "10.90.2.23", "10.77.2.23"),
    ("new-dist-c", "distributor", "newyork", "us-east", "10.90.2.13", "10.77.2.13"),
    # Workstream A (A 2.7, NT4): the standby contributor, publishing the
    # same parts beside contrib-lon on lanes of its own to lon-dist-a and
    # lon-dist-b, and the source host, which sends every AEP1 epoch to both
    # over the London VLAN, as lon-src does on Linode.
    ("contrib-lon-b", "contributor", "london", "gb-lon", "10.90.0.7", "10.77.1.7"),
    ("lon-src", "load", "london", "gb-lon", "10.90.0.4", "10.77.1.4"),
]
# The standby contributor (the topology's `"standby": true`) and the source
# host (a load host of purpose "source").
STANDBY_CONTRIBUTORS = ("contrib-lon-b",)
SOURCE_HOST = "lon-src"
# Reader hosts: a namespace on the public network of a site. They are not
# mesh nodes and have no CPU or memory limit.
READER_HOSTS = [
    ("lon-readers", "london", "10.90.0.101"),
    ("lon-readers-2", "london", "10.90.0.102"),
    ("new-readers", "newyork", "10.90.2.101"),
    # The New York load host (latency-bench --load-sites newyork): a crowd
    # off the measuring clients' link, as lon-readers-2 is in London.
    ("new-readers-2", "newyork", "10.90.2.102"),
]
SITE_VLAN = {"london": "10.77.1", "newyork": "10.77.2"}
SITE_PUBLIC = {"london": "10.90.0", "amsterdam": "10.90.1", "newyork": "10.90.2"}
# One-way delay from a site to the middle of the wide-area network. A path
# between two sites has the sum of the two and of the two access links:
# London to New York is 37 ms each way (a 74 ms round trip), London to
# Amsterdam 7 ms. Two nodes of one site are 1 ms apart on the public network.
SITE_DELAY_MS = {"london": 2.0, "amsterdam": 4.0, "newyork": 34.0}


def site_delays(text: str | None, base: dict[str, float]) -> dict[str, float]:
    """`base` with the delays of NEEDLETAIL_EDGE_LAB_SITE_DELAY_MS
    (`site=ms[,site=ms]`) put in. `newyork=101` puts the second region as far
    from London as Tokyo is (104.5 ms one way with the access links), for
    the long-haul hop; the rendered route RTTs follow."""
    out = dict(base)
    for item in (text or "").split(","):
        if not item.strip():
            continue
        site, _, ms = item.partition("=")
        site = site.strip()
        if site not in out:
            raise LabError(f"NEEDLETAIL_EDGE_LAB_SITE_DELAY_MS: unknown site {site!r}")
        out[site] = float(ms)
    return out


ACCESS_DELAY_MS = 0.5
VLAN_DELAY_MS = 0.15
# The limits of a node. The tiny node of the design is the smallest
# dedicated-CPU instance, g6-dedicated-2: 2 vCPU and 4 GB. The contributor runs
# alone on a g6-dedicated-4. NEEDLETAIL_EDGE_LAB_NANODE=1 restores the limits of
# a shared Nanode (1 vCPU, 1 GB) used before 21 November 2026.
if os.environ.get("NEEDLETAIL_EDGE_LAB_NANODE") == "1":
    NANODE = {"cpu_percent": 100, "memory_mb": 1024}
    CONTRIBUTOR_LIMITS = dict(NANODE)
else:
    NANODE = {"cpu_percent": 200, "memory_mb": 4096}
    CONTRIBUTOR_LIMITS = {"cpu_percent": 400, "memory_mb": 8192}


def default_limits(name: str) -> dict:
    """The limits a service starts with, and goes back to after a fault:
    CONTRIBUTOR_LIMITS for the contributor, NANODE for every other node. Read
    at call time, so a campaign that replaces NANODE or CONTRIBUTOR_LIMITS is
    followed."""
    return dict(CONTRIBUTOR_LIMITS if node(name)["role"] == "contributor" else NANODE)


def service_limits(name: str) -> dict:
    """default_limits with the lab's limit.<node>.* overrides of `up`."""
    return {**default_limits(name), **load("limits.json").get(name, {})}

# One node configuration carries the smallest parts of every stream: one AEP1
# epoch per audio part (48 samples, 1 ms, for PCM and FLAC; 48 samples, 1 ms
# Opus Custom, for Opus: `opus_epoch_samples`, which the probe sender and the
# DAW source both take; 120 is 2.5 ms standard Opus) and one frame per video
# part. The caches' nominal part is
# 2.5 ms. Segments are 1 s whatever the part. The window is 12 s of the
# shortest part: av-mesh counts a window in nominal parts and holds no stream
# to more, so the deploy tool renders --window-parts for the shortest part
# (12,000 at 1 ms, 48,000 at 0.25 ms), and a stream of longer parts keeps
# more than 12 s (up to 40 s; see window_parts_for). Campaign verdicts taken
# before 22 November 2026 were at 5 ms audio parts and five-frame video parts
# (`part_ms=5 fmp4_part_ms=5 fmp4_video_part_ms=200` restores them).
DEFAULT_OVERRIDES = {
    "part_us": 2500,
    "segment_ms": 1000,
    "window_ms": 12000,
    "audio_part": "epoch",
    "video_part": "frame",
    "pcm_epoch_samples": 48,
    "opus_epoch_samples": 48,
    # Video segments of one group of pictures.
    "fmp4_video_segment_ms": 2000,
    "mesh_sync_interval_ms": 2,
    "mesh_symbol_size": 900,
    "mesh_max_symbol_size": 16000,
    "telemetry_interval_ms": 500,
    "playback_address": "public",
    "cache_mesh_demand_idle_ms": 8000,
    "cache_mesh_lease_ms": 3000,
    "cache_mesh_warm_kbps": 20000,
    # A small edge: 100 Mbit/s of egress, admission at 85 Mbit/s.
    "edge_egress_capacity_bps": 100_000_000,
    "steering_ttl_seconds": 1,
}


class LabError(RuntimeError):
    pass


SITE_DELAY_MS = site_delays(os.environ.get("NEEDLETAIL_EDGE_LAB_SITE_DELAY_MS"), SITE_DELAY_MS)


def node(name: str) -> dict:
    for item, role, site, region, public, vlan in NODES:
        if item == name:
            extra = ({"standby": True} if item in STANDBY_CONTRIBUTORS else
                     {"purpose": "source"} if item == SOURCE_HOST else {})
            return {"name": item, "role": role, "site": site, "region": region,
                    "public_ipv4": public, "vlan_ipv4": vlan, **extra}
    for item, site, public in READER_HOSTS:
        if item == name:
            return {"name": item, "role": "reader", "site": site, "region": None,
                    "public_ipv4": public, "vlan_ipv4": None}
    raise LabError(f"unknown node: {name}")


def names(role: str | None = None, site: str | None = None) -> list[str]:
    return [name for name, item_role, where, *_ in NODES
            if (role is None or item_role == role) and (site is None or where == site)]


def service_names(role: str | None = None, site: str | None = None) -> list[str]:
    """The nodes of `names` that run a service in this lab's topology, as
    rendered by the last `up`: the merged topology (the default) renders no
    backbone relay, so relay-primary and relay-secondary have nothing to
    start, limit or restore. Empty before the lab is rendered."""
    rendered = load("rendered.json")
    return [name for name in names(role, site) if name in rendered]


# The nodes that can be slots, and those that are slots unless `up slots=`
# names more: a dist-c has no service unless taken in.
SLOT_CANDIDATES = ("lon-edge-4", "lon-dist-c", "new-edge-3", "new-dist-c")
DEFAULT_SLOTS = ("lon-dist-c", "new-dist-c")


def slot_nodes() -> list[str]:
    """The slots of this lab (`up slots=`), the two dist-c always."""
    saved = load("slots.json", list(DEFAULT_SLOTS))
    return sorted(set(saved if isinstance(saved, list) else DEFAULT_SLOTS) | set(DEFAULT_SLOTS))


def slots_in_service() -> set[str]:
    """Slots taken into service (`start`, or the autoscaler's create)."""
    saved = load("slots-created.json", [])
    return set(saved) if isinstance(saved, list) else set()


def held_slots() -> set[str]:
    return set(slot_nodes()) - slots_in_service()


def set_slot_in_service(name: str, in_service: bool) -> None:
    if name not in SLOT_CANDIDATES:
        raise LabError(f"{name} is not a slot of this lab")
    made = slots_in_service()
    made = made | {name} if in_service else made - {name}
    save("slots-created.json", sorted(made))


def edges(site: str = "london") -> list[str]:
    """The edges of a site in service: a slot only once it is taken in."""
    held = held_slots()
    return [name for name in names("edge", site) if name not in held]


def distributors(site: str = "london") -> list[str]:
    held = held_slots()
    return [name for name in names("distributor", site) if name not in held]


def standing_services(role: str | None = None, site: str | None = None) -> list[str]:
    """service_names without the slots that are not in service: what `up`
    starts and restore() brings back."""
    held = held_slots()
    return [name for name in service_names(role, site) if name not in held]


def base_url(name: str) -> str:
    return f"https://{node(name)['public_ipv4']}:{HTTP_PORT}"


# ------------------------------------------------------------------ state

def load(name: str, default=None):
    try:
        return json.loads((WORK / name).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def save(name: str, value) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    temporary = WORK / (name + ".tmp")
    temporary.write_text(json.dumps(value, indent=1))
    temporary.replace(WORK / name)


def lab_pid() -> int:
    try:
        return int(load("namespaces.json")["lab"])
    except (KeyError, TypeError):
        raise LabError("the lab is not up")


def ns_pid(name: str) -> int:
    try:
        return int(load("namespaces.json")[name])
    except KeyError:
        raise LabError(f"no namespace for {name}")


def nsenter(name: str | None) -> list[str]:
    """Command prefix that enters the namespace of a node, or the lab
    namespace (which holds the bridges) for `None`."""
    pid = lab_pid() if name is None else ns_pid(name)
    return ["nsenter", "--preserve-credentials", "-U", "-n", "-t", str(pid)]


def enter(name: str | None) -> list[str]:
    """Command prefix that runs a command of the lab's own (a reader, the
    load tool, a probe, a sender, tc, curl) in the namespace of a node, or
    in the lab namespace for `None`. Such a command is in the lab's tools
    scope with the process that starts it (join_tools) and, with the lab's
    CPU sets (`cpus`, `node_cpus`), on the CPUs that are not the nodes'.
    Only a node's service runs elsewhere: in its own scope, on its own CPUs
    (start_service)."""
    prefix = nsenter(name)
    join_tools()
    rest = load("cpus.json").get("_rest")
    if rest:
        prefix += ["taskset", "-c", cpu_list(rest)]
    return prefix


# ------------------------------------------------------------------ the labs of the host

def work_tag(work: pathlib.Path) -> str:
    """What names a state directory in a unit: its basename and the first
    eight hex digits of the sha1 of its absolute path, so that two
    directories with one basename are two labs."""
    path = pathlib.Path(work).resolve()
    return f"{path.name}-{hashlib.sha1(str(path).encode()).hexdigest()[:8]}"


def unit_name(name: str) -> str:
    return f"needletail-lab-{work_tag(WORK)}-{name}.scope"


def lab_key() -> str:
    """The lab's name in the registry: its id, or without one the tag of
    its state directory (never the shape of an id)."""
    return LAB_ID or work_tag(WORK)


def lab_owner() -> str:
    """Who runs this lab: NEEDLETAIL_EDGE_LAB_OWNER, by default the lab's
    own name."""
    return os.environ.get("NEEDLETAIL_EDGE_LAB_OWNER") or lab_key()


# One file for each running lab of the host, whichever worktree it is run
# from: {"id", "owner", "pid", "worktree", "work", "cpus", "since_unix"}.
REGISTRY = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR")
                        or f"/run/user/{os.getuid()}") / "needletail-labs"


@contextlib.contextmanager
def registry_lock():
    """Every read-change-write of the registry holds this lock."""
    REGISTRY.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def cpu_list(cpus: list[int]) -> str:
    return ",".join(str(cpu) for cpu in cpus)


def cpu_ranges(cpus: list[int]) -> str:
    """CPUs as a person reads them: 2-15, or 0-3,8."""
    out, cpus = [], sorted(cpus)
    for cpu in cpus:
        if out and cpu == out[-1][1] + 1:
            out[-1][1] = cpu
        else:
            out.append([cpu, cpu])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


def process_started_unix(pid: int) -> float | None:
    """When a process started (unix seconds, to the clock tick, and the
    boot time to the second), None when there is no such process."""
    try:
        stat = pathlib.Path(f"/proc/{int(pid)}/stat").read_text()
        boot = next(float(line.split()[1]) for line in
                    pathlib.Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
        # Field 22, counted after the command name, which may hold spaces.
        ticks = int(stat.rpartition(")")[2].split()[19])
    except (OSError, ValueError, IndexError, StopIteration, TypeError):
        return None
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def pid_alive(pid, written_unix: float | None = None) -> bool:
    """Whether a process runs as `pid`. With `written_unix`, when the
    record that names it was written, it must also have started by then:
    a process that started later has only been given the same pid."""
    try:
        if not pathlib.Path(f"/proc/{int(pid)}/cmdline").read_bytes():
            return False
    except (OSError, TypeError, ValueError):
        return False
    if written_unix is None:
        return True
    started = process_started_unix(pid)
    return started is not None and started <= written_unix + 1.0


def active_scopes(pattern: str) -> list[str]:
    """The active scopes of the user's systemd manager whose names match
    `pattern`."""
    result = subprocess.run(
        ["systemctl", "--user", "list-units", "--type=scope", "--state=active", "--no-legend",
         "--plain", pattern], capture_output=True, text=True)
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


def lab_scopes(work: str) -> list[str]:
    """The systemd scopes of the lab in state directory `work` that are
    alive: its nodes' services and its tools."""
    return active_scopes(f"needletail-lab-{work_tag(pathlib.Path(work))}-*.scope")


def live_entries() -> list[dict]:
    """The registry's labs that run, read under registry_lock. An entry is
    stale, and is dropped, when its process is gone (or its pid is another
    process's now) and no scope of its lab is alive."""
    out = []
    for path in sorted(REGISTRY.glob("*.json")):
        try:
            entry = json.loads(path.read_text())
            entry = {"id": str(entry["id"]), "owner": str(entry["owner"]),
                     "pid": int(entry["pid"]), "worktree": str(entry["worktree"]),
                     "work": str(entry["work"]), "cpus": [int(cpu) for cpu in entry["cpus"]],
                     "since_unix": float(entry["since_unix"])}
            alive = (pid_alive(entry["pid"], path.stat().st_mtime)
                     or bool(lab_scopes(entry["work"])))
        except (OSError, ValueError, KeyError, TypeError):
            alive = False
        if alive:
            out.append(entry)
        else:
            path.unlink(missing_ok=True)
    return out


def registered_labs() -> list[dict]:
    """The labs that run on this host, from every worktree."""
    with registry_lock():
        return live_entries()


def write_entry(entry: dict) -> None:
    temporary = REGISTRY / f"{entry['id']}.json.tmp"
    temporary.write_text(json.dumps(entry, indent=1))
    temporary.replace(REGISTRY / f"{entry['id']}.json")


def claim(cpus: list[int], take: bool = False, overlap: bool = False) -> None:
    """`up`, before anything is stopped or started: make this lab the one
    that the registry knows by its name and its state directory, on `cpus`.
    Refused, with every running lab left as it is:
    - when the lab runs for another owner, or under the same id from
      another state directory (another worktree), unless `take`, which
      stops that lab;
    - when another lab has one of `cpus`, unless `overlap`.
    Otherwise the lab that ran in this state directory is stopped, as `up`
    always did, and the entry is written with this process as its pid until
    the lab's namespace holder exists (register_holder)."""
    key, owner, work = lab_key(), lab_owner(), str(WORK.resolve())
    with registry_lock():
        entries = live_entries()
        same = [entry for entry in entries if entry["id"] == key or entry["work"] == work]
        for entry in same:
            where = (f"lab {entry['id']} of {entry['owner']} runs from {entry['work']} "
                     f"(worktree {entry['worktree']}, CPUs {cpu_ranges(entry['cpus'])})")
            if entry["owner"] != owner and not take:
                raise LabError(f"{where}; you are {owner}. `up take=1` stops it and takes "
                               "its place")
            if entry["work"] != work and not take:
                raise LabError(f"{where}, not from {work}. Take another "
                               "NEEDLETAIL_EDGE_LAB_ID, or `up take=1` stops that lab")
        if not overlap:
            for entry in entries:
                shared = sorted(set(entry["cpus"]) & set(cpus))
                if entry not in same and shared:
                    taken = sorted({cpu for item in entries if item not in same
                                    for cpu in item["cpus"]})
                    raise LabError(
                        f"CPUs {cpu_ranges(shared)} are lab {entry['id']}'s ({entry['owner']}, "
                        f"worktree {entry['worktree']}); CPUs {cpu_ranges(taken)} are taken on "
                        "this host. Give `up cpus=A-B` (or NEEDLETAIL_EDGE_LAB_CPUS) a range "
                        "outside them, or `up cpus_overlap=1`")
            # A lab that no entry names (brought up by the code before the
            # registry) may run on any CPU.
            known = [f"needletail-lab-{work_tag(pathlib.Path(item))}-"
                     for item in [work] + [entry["work"] for entry in entries]]
            unknown = [unit for unit in active_scopes("needletail-lab-*.scope")
                       if not unit.startswith(tuple(known))]
            if unknown:
                raise LabError(
                    f"a lab that is not registered runs on this host, on any of its CPUs: "
                    f"{', '.join(unknown[:4])}{' ...' if len(unknown) > 4 else ''}. Stop it "
                    "(`edge_lab.py down` from its worktree, with its NEEDLETAIL_EDGE_LAB_WORK), "
                    "or `up cpus_overlap=1`")
        for entry in same:
            if entry["work"] != work:
                kill_lab(pathlib.Path(entry["work"]))
            (REGISTRY / f"{entry['id']}.json").unlink(missing_ok=True)
        kill_lab(WORK)
        write_entry({"id": key, "owner": owner, "pid": os.getpid(), "worktree": str(ROOT),
                     "work": work, "cpus": sorted(cpus), "since_unix": time.time()})


def register_holder(pid: int) -> None:
    """The lab's entry lives as long as its namespace holder (or one of its
    scopes) does."""
    with registry_lock():
        path = REGISTRY / f"{lab_key()}.json"
        entry = json.loads(path.read_text())
        write_entry({**entry, "pid": pid})


def unregister() -> None:
    """Remove this lab's entry: only the one of this state directory (the
    same id may run from another worktree)."""
    work = str(WORK.resolve())
    with registry_lock():
        for path in REGISTRY.glob("*.json"):
            try:
                if json.loads(path.read_text()).get("work") == work:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, AttributeError):
                pass


# ------------------------------------------------------------------ CPUs

def cpu_range(text: str) -> tuple[int, int]:
    """`cpus=A-B`: host CPUs A to B, at least three (two for the reader
    hosts and the lab's own commands, one for the nodes)."""
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", text)
    total = os.cpu_count() or 1
    if not match:
        raise LabError(f"cpus={text}: give a range of host CPUs, A-B")
    first, last = int(match.group(1)), int(match.group(2))
    if last >= total:
        raise LabError(f"cpus={text}: the host has CPUs 0-{total - 1}")
    if last - first + 1 < 3:
        raise LabError(f"cpus={text}: a lab needs at least three CPUs")
    return first, last


# The layout of one lab alone (`node_cpus=FIRST`) on the 32 CPU host, with
# workstream A's second contributor and source host: 22 CPUs for the
# standing mesh nodes, 2 for each contributor, 2 for lon-src and 4 left for
# the reader hosts, the probes and the lab's own commands. A contributor's 4
# CPUs (its g6-dedicated-4) do not fit beside its standby's, so in this
# layout each has 2 (its CPU limit 200%, unless `up limit.<node>.*` says
# otherwise), and the dist-c slots have none: they cannot be taken in under
# it (a scenario that needs one runs in a range, `cpus=A-B`). The range
# layout is as it was: every node a window of its limit on the ring.
PINNED_CONTRIBUTOR_CPUS = 2


def take_window(sets: dict, name: str) -> list[int]:
    """Give node `name` its CPUs in `sets` (changed in place), after the
    nodes that have theirs. In a range: the next window of as many CPUs as
    its limit on the ring of node CPUs, so nodes share when the ring is
    short (an edge on the edges' own ring when the layout has one). With
    `node_cpus`: the next CPUs of the host, its own, taken from "_rest" (a
    contributor PINNED_CONTRIBUTOR_CPUS of them). With "_per_node" (rt=1):
    exactly that many, whatever the node's limit."""
    count = sets.get("_per_node") or max(1, default_limits(name)["cpu_percent"] // 100)
    if "_range" not in sets and node(name)["role"] == "contributor":
        count = min(count, PINNED_CONTRIBUTOR_CPUS)
    if "_range" in sets:
        edge_ring = "_edge_cpus" in sets and node(name)["role"] == "edge"
        ring = sets["_edge_cpus" if edge_ring else "_node_cpus"]
        at = "_edge_next" if edge_ring else "_next"
        count = min(count, len(ring))
        window = [ring[(sets[at] + index) % len(ring)] for index in range(count)]
        sets[at] = (sets[at] + count) % len(ring)
    else:
        window = list(range(sets["_next"], sets["_next"] + count))
        rest = [cpu for cpu in sets["_rest"] if cpu not in window]
        if window[-1] >= sets["_total"]:
            raise LabError(f"node_cpus={sets['_first']}: the host has {sets['_total']} CPUs, "
                           f"too few for {name} ({count} from CPU {window[0]})")
        if not rest:
            raise LabError(f"node_cpus={sets['_first']}: no CPU is left for the reader hosts")
        sets["_next"] += count
        sets["_rest"] = rest
    sets[name] = window
    return window


def node_cpu_sets(names: list[str], first: int | None = None,
                  cpus: tuple[int, int] | None = None, per_node: int | None = None) -> dict:
    """The CPUs of the nodes `names` (in the order of NODES) and "_rest",
    the CPUs of the reader and load hosts and of the lab's own commands.

    `cpus=(A, B)` (`up cpus=A-B`): the lab stays on host CPUs A to B. The
    last max(2, N // 4) of the N are "_rest"; the others are the node CPUs,
    and each node has a window of them, as many as its limit (an edge or a
    distributor 2, the contributor 4), taken round robin. A pinned class
    thread (--relay-class-cpus) lands in its node's window.

    `first` (`up node_cpus=FIRST`): each node its own CPUs, as a Linode
    instance has, from host CPU FIRST on, and "_rest" is every other CPU of
    the host: the layout of one lab alone (PINNED_CONTRIBUTOR_CPUS: the
    contributors 2 CPUs each, the dist-c slots none).

    In a range, a node that is not in `names` (a slot that is held out, a
    backbone relay) takes its window on the ring when its service starts
    (node_cpus). With `first` every node that is to have CPUs must be in
    `names`: a window taken later could only come out of "_rest", which the
    reader hosts and the lab's own commands already run on. With neither
    layout: {}, and every node shares every CPU of the host under its CPU
    quota.

    `per_node` (rt=1, RT_CPUS): every node exactly that many CPUs, as a
    g6-dedicated-2 has, whatever its limit (the contributor too). In a
    range the edges then come first and have CPUs of their own, as an
    instance has, when they fit with `per_node` CPUs left for the others
    ("_edge_cpus"); the other nodes share what is left of the ring. An
    edge taken in later wraps on the edges' ring."""
    total = os.cpu_count() or 1
    if cpus is not None:
        low, high = cpus
        count = high - low + 1
        rest = max(2, count // 4)
        out = {"_range": [low, high], "_node_cpus": list(range(low, high + 1 - rest)),
               "_rest": list(range(high + 1 - rest, high + 1)), "_next": 0}
        if per_node:
            out["_per_node"] = per_node
            ring = out["_node_cpus"]
            need = per_node * sum(1 for name in names if node(name)["role"] == "edge")
            if need and need + per_node <= len(ring):
                out.update({"_edge_cpus": ring[:need], "_node_cpus": ring[need:],
                            "_edge_next": 0})
    elif first is not None:
        out = {"_first": first, "_total": total, "_rest": list(range(total)), "_next": first}
        if per_node:
            out["_per_node"] = per_node
        names = [name for name in names if name not in DEFAULT_SLOTS]
    else:
        return {}
    for name, *_ in NODES:
        if name in names:
            take_window(out, name)
    return out


def pinned_limits(sets: dict, limits: dict) -> dict:
    """`limits` (up's limit.<node>.*) with, in the `node_cpus` layout, each
    contributor's CPU limit that of its window (PINNED_CONTRIBUTOR_CPUS)
    unless given: its quota and worker threads follow the CPUs it has."""
    out = {name: dict(value) for name, value in limits.items()}
    if "_first" in sets:
        for name in names("contributor"):
            if name in sets:
                out.setdefault(name, {}).setdefault("cpu_percent", 100 * len(sets[name]))
    if sets.get("_per_node"):
        # rt=1: every node's quota is its window, the contributor's too.
        for name, *_ in NODES:
            out.setdefault(name, {}).setdefault("cpu_percent", 100 * sets["_per_node"])
    return out


def node_cpus(name: str) -> list[int] | None:
    """The CPUs of a node's service in this lab's layout, None without a
    layout. In a range, a node that has none yet (a slot taken in, a
    backbone relay of the legacy topology) takes its window on the ring
    now, under a lock, so that two starts at once take two windows. With
    `node_cpus` every node that has CPUs was given them at `up`; a backbone
    relay of the legacy topology runs under its quota alone, as before."""
    sets = load("cpus.json")
    if "_first" in sets and name not in sets and name in DEFAULT_SLOTS:
        raise LabError(f"node_cpus={sets['_first']}: {name} has no CPUs of its own in this "
                       "layout (PINNED_CONTRIBUTOR_CPUS); a lab that takes a dist-c in runs in "
                       "a range, cpus=A-B")
    if name in sets or "_range" not in sets:
        return sets.get(name)
    with open(WORK / "cpus.json.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        sets = load("cpus.json")
        if name not in sets:
            take_window(sets, name)
            save("cpus.json", sets)
        return sets[name]


def lab_cpus(sets: dict) -> list[int]:
    """Every host CPU that a lab with the layout `sets` may use: its range;
    with `node_cpus`, or without a layout, every CPU of the host (a node or
    a command that a later shell starts may run on any of them)."""
    if "_range" in sets:
        return list(range(sets["_range"][0], sets["_range"][1] + 1))
    return list(range(sets.get("_total") or os.cpu_count() or 1))


def node_class_cpus(name: str, value: str) -> str:
    """--relay-class-cpus of a node with a CPU set: the rendered CPUs are
    its own CPUs by index (0,1: the first two of its set), as the deploy's
    "0,1" pins the audio and video threads of an instance."""
    cpus = load("cpus.json").get(name)
    if not cpus:
        return value
    return ",".join(str(cpus[int(item) % len(cpus)]) for item in value.split(",") if item)


# ------------------------------------------------------------------ real time (rt=1)

# `up rt=1`: each node's service may use real-time scheduling as a Linode
# unit's does (AmbientCapabilities=CAP_SYS_NICE): the audio class thread
# SCHED_FIFO 10, the video one nice -10, the fan-out threads nice -5. A
# capability cannot reach the service here: it runs in the lab's rootless
# user namespace, and a capability held there counts for nothing in the
# host's (sched_setscheduler asks the host's). What does reach it are
# RLIMIT_RTPRIO and RLIMIT_NICE, which allow the same calls to a process
# that has no capability. So the node's start, inside its own scope, raises
# both on its own shell (`sudo -n prlimit --pid $$`, root for that alone)
# and then becomes the service, as the user, with the limits inherited.
# Nothing else is run as root and no system file or setting is changed.
RT_PRIO = 99    # what CAP_SYS_NICE allows: any SCHED_FIFO priority
RT_NICE = 40    # RLIMIT_NICE 40: down to nice -20, as CAP_SYS_NICE allows
RT_CPUS = 2     # every node a g6-dedicated-2 (2 vCPU) under rt=1
RT_AUDIO_PRIO = 10   # av-mesh's AUDIO_FIFO_PRIORITY
RT_AUDIO_COMM = "needletail-fast"
RT_GRANT = ('sudo -n prlimit --pid $$ --rtprio={prio}:{prio} --nice={nice}:{nice} '
            '|| {{ echo "rt=1: sudo -n prlimit could not raise the real-time limits of this '
            'service" >&2; exit 126; }}; exec "$@"')


def rt_preflight(run=subprocess.run) -> None:
    """rt=1 needs `sudo -n prlimit`, and refuses clearly without it. `sudo
    -n -l prlimit` only asks whether it may, and runs nothing."""
    try:
        result = run(["sudo", "-n", "-l", "prlimit"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LabError(f"rt=1: sudo cannot be run ({error}); the lab needs `sudo -n prlimit` to "
                       "give its nodes real-time limits. Leave rt=1 out")
    if result.returncode != 0:
        raise LabError("rt=1: `sudo -n prlimit` is not allowed here without a password "
                       f"({(result.stderr or result.stdout).strip()[:200]}). The nodes cannot "
                       "be given SCHED_FIFO without it; leave rt=1 out")


def rt_grant() -> list[str]:
    """The prefix, inside a node's scope, that raises the service's own
    real-time limits before it becomes the service (rt=1); [] without."""
    if not load("rt.json").get("rt"):
        return []
    return ["sh", "-c", RT_GRANT.format(prio=RT_PRIO, nice=RT_NICE), "needletail-rt"]


def ps_threads(pid: int) -> list[dict]:
    """Every thread of a process: tid, name, class (TS, FF, RR...), rtprio,
    the CPU it last ran on and the CPUs it may run on."""
    result = subprocess.run(["ps", "-L", "-o", "tid=,cls=,rtprio=,psr=,comm=", "-p", str(pid)],
                            capture_output=True, text=True)
    out = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5:
            continue
        tid, cls, rtprio, psr, comm = fields
        allowed = None
        try:
            for item in pathlib.Path(f"/proc/{pid}/task/{tid}/status").read_text().splitlines():
                if item.startswith("Cpus_allowed_list:"):
                    allowed = item.split(":", 1)[1].strip()
        except OSError:
            pass
        out.append({"tid": int(tid), "cls": cls, "rtprio": None if rtprio == "-" else int(rtprio),
                    "psr": int(psr), "comm": comm.strip(), "allowed": allowed})
    return out


def expand_cpus(text: str | None) -> list[int]:
    """A CPU list as the kernel prints it (0-3,8) as numbers."""
    out = []
    for item in (text or "").split(","):
        low, _, high = item.strip().partition("-")
        if low.strip().isdigit():
            out += list(range(int(low), int(high or low) + 1))
    return out


def own_counters(snapshot, keys: tuple[str, ...]) -> dict:
    """The largest value of each counter named in `keys` on this node's own
    paths of /api/mesh/local (never under another node's telemetry)."""
    out: dict = {}

    def walk(item, top: str | None) -> None:
        if isinstance(item, dict):
            for key, value in item.items():
                if top is None and key in ("relay_nodes", "nodes", "peers", "edge_services",
                                           "streams", "connections"):
                    continue
                if key in keys and isinstance(value, (int, float)):
                    out[key] = max(out.get(key, value), value)
                walk(value, key if top is None else top)
        elif isinstance(item, list):
            for value in item:
                walk(value, top or "list")

    walk(snapshot, None)
    return out


def rt_check(name: str, snapshot=None, threads=None, limits_text: str | None = None,
             realtime: bool | None = None) -> dict:
    """Is a node's audio class thread real-time and pinned, as on Linode?
    From the fast plane's own counters (/api/mesh/local `realtime_threads`,
    `pinned_threads`) and from the kernel (ps: SCHED_FIFO 10, and its CPU
    list the one CPU that --relay-class-cpus names). A node rendered without
    --relay-class-cpus (the contributor) is only described. Under rt=pin
    (`realtime` False, or rt.json's) the thread must be pinned only."""
    pid = service_pid(name)
    args = service_args(name) if name in load("rendered.json") else []
    class_cpus = (args[args.index("--relay-class-cpus") + 1]
                  if "--relay-class-cpus" in args else "")
    out = {"node": name, "pid": pid, "class_cpus": class_cpus, "node_cpus": node_cpus(name)}
    if pid is None:
        return {**out, "ok": False, "why": "the service does not run"}
    if threads is None:
        threads = ps_threads(pid)
    if limits_text is None:
        try:
            limits_text = pathlib.Path(f"/proc/{pid}/limits").read_text()
        except OSError:
            limits_text = ""
    rtprio_limit = next((line.split()[3] for line in limits_text.splitlines()
                         if line.startswith("Max realtime priority")), None)
    fifo = [item for item in threads if item["cls"] in ("FF", "RR")]
    out.update({"rtprio_limit": rtprio_limit, "threads": len(threads),
                "realtime": [{key: item[key] for key in ("tid", "comm", "cls", "rtprio", "psr",
                                                         "allowed")} for item in fifo]})
    if node(name)["role"] == "contributor" or not class_cpus:
        return {**out, "ok": None, "why": "no class thread to pin (described only)"}
    if snapshot is None:
        try:
            snapshot = mesh_local(name)
        except LabError as error:
            return {**out, "ok": False, "why": str(error)}
    counts = own_counters(snapshot, ("realtime_threads", "pinned_threads", "threads"))
    out["fast_plane"] = counts
    audio_cpu = int(class_cpus.split(",")[0])
    granted = load("rt.json").get("rt", True) if realtime is None else realtime
    problems = []
    if granted:
        audio = [item for item in fifo if item["comm"] == RT_AUDIO_COMM
                 and item["rtprio"] == RT_AUDIO_PRIO
                 and expand_cpus(item["allowed"]) == [audio_cpu]]
        if not counts.get("realtime_threads"):
            problems.append(f"the fast plane counts {counts.get('realtime_threads')} real-time "
                            "threads")
        if not audio:
            problems.append(f"no {RT_AUDIO_COMM} thread at SCHED_FIFO {RT_AUDIO_PRIO} allowed "
                            f"only CPU {audio_cpu}")
    else:
        # rt=pin: pinned as the deploy pins it, at the normal policy.
        audio = [item for item in threads if item["comm"] == RT_AUDIO_COMM
                 and expand_cpus(item["allowed"]) == [audio_cpu]]
        if not audio:
            problems.append(f"no {RT_AUDIO_COMM} thread allowed only CPU {audio_cpu}")
    if not counts.get("pinned_threads"):
        problems.append(f"the fast plane counts {counts.get('pinned_threads')} pinned threads")
    out["audio_thread"] = audio[0] if audio else None
    policy = f"SCHED_FIFO {RT_AUDIO_PRIO}" if granted else f"{audio[0]['cls'] if audio else '?'}"
    return {**out, "ok": not problems, "why": "; ".join(problems) or
            f"audio class thread {audio[0]['tid']} {policy} on CPU {audio_cpu}"}


def rt_verify(timeout: float = 30.0) -> list[dict]:
    """After an rt=1 `up`: every node's check, waited for until each av-mesh
    node with class CPUs passes (its class threads start with the service).
    Kept in rt-check.json; a node that does not pass is an error."""
    deadline = time.time() + timeout
    while True:
        checks = [rt_check(name) for name in standing_services()]
        failed = [item for item in checks if item["ok"] is False]
        if not failed or time.time() > deadline:
            break
        time.sleep(1.0)
    save("rt-check.json", checks)
    if failed:
        raise LabError("rt=1: not real-time and pinned: " + "; ".join(
            f"{item['node']}: {item['why']}" for item in failed))
    return checks


# ------------------------------------------------------------------ the tools scope

def attach(unit: str, pid: int) -> None:
    """Move a process into a scope of the user's systemd manager: what it
    and its children use is counted, and limited, in that scope's cgroup
    from then on. The scope must delegate (Delegate=yes)."""
    result = subprocess.run(
        ["busctl", "--user", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
         "org.freedesktop.systemd1.Manager", "AttachProcessesToUnit", "ssau", unit, "", "1",
         str(pid)], capture_output=True, text=True)
    if result.returncode != 0:
        # The user's manager can move only a process of its own tree: one
        # started from a login session's scope (a plain ssh shell) is not.
        raise LabError(f"could not move process {pid} into {unit}: "
                       f"{(result.stderr or result.stdout).strip()[:300]}. Run the lab's "
                       "commands from a process of the user's systemd manager (a tmux pane, "
                       "or under `systemd-run --user --scope`)")


CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")


def cgroup_of(pid: int) -> pathlib.Path | None:
    try:
        path = pathlib.Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
    except (OSError, IndexError):
        return None
    return CGROUP_ROOT / path.lstrip("/")


def scope_state(unit: str) -> tuple[str, list[int]]:
    """A scope's ActiveState and the processes in its cgroup."""
    result = subprocess.run(["systemctl", "--user", "show", "--property=ActiveState",
                             "--property=ControlGroup", unit], capture_output=True, text=True)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    pids = []
    if fields.get("ControlGroup"):
        try:
            pids = [int(pid) for pid in (CGROUP_ROOT / fields["ControlGroup"].lstrip("/")
                                         / "cgroup.procs").read_text().split()]
        except (OSError, ValueError):
            pass
    return fields.get("ActiveState") or "inactive", pids


def start_tools() -> None:
    """The lab's tools scope: one cgroup for everything the lab runs that is
    not a node's service. A sleeping holder keeps it (and its CPU counter)
    for the life of the lab; the processes that drive the lab join it."""
    unit = unit_name("tools")
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
             "stderr": subprocess.DEVNULL, "start_new_session": True}
    # The holder of the lab that ran here was killed a moment ago: wait
    # until its scope has ended, or is held by a process that is still in
    # it (this one, after a `down`), before taking it again or joining it.
    deadline = time.time() + 5
    state, pids = scope_state(unit)
    while not (state in ("inactive", "failed") or state == "active" and pids):
        if time.time() > deadline:
            raise LabError(f"the tools scope {unit} is {state} with no process in it")
        time.sleep(0.05)
        state, pids = scope_state(unit)
    joining = state == "active"
    if joining:
        holder = subprocess.Popen(["sleep", "infinity"], **quiet)
    else:
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
        holder = subprocess.Popen(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", unit,
             "-p", "Delegate=yes", "--", "sleep", "infinity"], **quiet)
    try:
        if joining:
            attach(unit, holder.pid)
        deadline = time.time() + 5
        while not str(cgroup_of(holder.pid) or "").endswith("/" + unit):
            if time.time() > deadline or holder.poll() is not None:
                raise LabError(f"the tools scope {unit} did not start")
            time.sleep(0.05)
    except BaseException:
        # A holder that is left would keep the scope, and the lab's entry,
        # alive with nothing to stop it.
        holder.kill()
        raise
    save("tools.json", {"pid": holder.pid, "unit": unit})


_joined = threading.Lock(), set()


def join_tools() -> None:
    """Put this process into the lab's tools scope and, with the lab's CPU
    sets, on the CPUs that are not the nodes': once for each lab instance
    that this process drives. Its children are then there too: every reader,
    probe, sender and command that it starts."""
    lock, done = _joined
    with lock:
        tools = load("tools.json")
        if not tools:
            raise LabError("the lab has no tools scope: it is not up")
        if tools["pid"] in done:
            return
        attach(tools["unit"], os.getpid())
        rest = load("cpus.json").get("_rest")
        for task in os.listdir("/proc/self/task") if rest else []:
            try:
                os.sched_setaffinity(int(task), rest)
            except ProcessLookupError:
                pass  # a thread that ended meanwhile
            except OSError as error:
                raise LabError(f"this process may not run on the lab's CPUs "
                               f"{cpu_ranges(rest)}: {error}")
        done.add(tools["pid"])


def scope_cpu_usec() -> dict:
    """CPU time, in microseconds, that each scope of the lab has used since
    the scope began: {"tools": N, node: N for each running service}. A
    restarted service's count begins again."""
    out = {}
    tools = load("tools.json").get("pid")
    for name, directory in [("tools", cgroup_of(tools) if tools else None)] + [
            (name, cgroup_dir(name)) for name, *_ in NODES]:
        usage = read_cgroup(directory).get("usage_usec") if directory else None
        if usage is not None:
            out[name] = usage
    return out


def run_in(name: str | None, command: list[str], check: bool = True,
           timeout: float = 60, text_input: str | None = None) -> str:
    result = subprocess.run(enter(name) + command, capture_output=True,
                            timeout=timeout, input=text_input.encode() if text_input else None)
    if check and result.returncode != 0:
        raise LabError(f"{name or 'lab'}: {' '.join(command)[:160]} failed: "
                       f"{result.stderr.decode(errors='replace')[-600:]}")
    return result.stdout.decode(errors="replace")


def sh_in(name: str | None, script: str, check: bool = True, timeout: float = 60) -> str:
    return run_in(name, ["bash", "-c", script], check=check, timeout=timeout)


# ------------------------------------------------------------------ networks

def link_names(name: str) -> dict:
    """Interfaces of a node: inside the node, and the bridge end in the lab
    namespace."""
    index = load("links.json")[name]
    return {"pub": ("eth0", f"p{index}"), "vlan": ("eth1", f"v{index}")}


def netem_spec(delay_ms: float = 0.0, jitter_ms: float = 0.0, loss: float = 0.0,
               burst: tuple | None = None, reorder: float = 0.0,
               duplicate: float = 0.0, rate_kbit: int = 0, corrupt: float = 0.0) -> str:
    """A `tc netem` argument string. `burst` is a Gilbert-Elliott model:
    (p, r, loss in the bad state, loss in the good state), all in percent.

    The spec of a link with jitter starts with the word `ordered`, and
    `set_net` gives the netem a FIFO child. On older kernels that kept the
    order of packets. On the kernel of this lab it does not: netem sorts by
    the time at which each packet is due, so jitter reorders packets that
    are closer together than the jitter. A link with jitter is therefore
    also a link that reorders.

    A link with a rate has a queue of a quarter of a second, as an access
    link has. With the 50,000 packets of the other links it held more than a
    minute of traffic, and a player on it saw delay grow without loss: the
    stalls of the first ladder runs on capped links were those of the queue."""
    limit = 50_000
    if rate_kbit:
        limit = max(20, int(rate_kbit * 1000 / 8 * 0.25 / 1500))
    parts = ["limit", str(limit)]
    if jitter_ms and not reorder:
        parts.insert(0, "ordered")
    if delay_ms or jitter_ms or reorder:
        parts += ["delay", f"{max(delay_ms, 0.01):.2f}ms"]
        if jitter_ms:
            parts += [f"{jitter_ms:.2f}ms", "distribution", "normal"]
    if burst:
        p, r, bad, good = burst
        parts += ["loss", "gemodel", f"{p}%", f"{r}%", f"{100 - bad}%", f"{good}%"]
    elif loss:
        parts += ["loss", "random", f"{loss}%"]
    if reorder:
        parts += ["reorder", f"{reorder}%", "50%"]
    if duplicate:
        parts += ["duplicate", f"{duplicate}%"]
    if corrupt:
        parts += ["corrupt", f"{corrupt}%"]
    if rate_kbit:
        parts += ["rate", f"{rate_kbit}kbit"]
    return " ".join(parts)


def base_spec(name: str, network: str) -> str:
    """The netem of a healthy link of this node."""
    if network == "vlan":
        return netem_spec(delay_ms=VLAN_DELAY_MS)
    return netem_spec(delay_ms=ACCESS_DELAY_MS)


def host_qdisc() -> str:
    """The host qdisc of the running lab (the deploy setting `host_qdisc`)."""
    return load("settings.json").get("host_qdisc") or "none"


# The deploy tool's host qdisc inside a node, where the root of each of its
# interfaces is the netem of the link: a prio child of the netem, with the
# deploy's priomap, so that what leaves the netem at one time leaves audio
# (SO_PRIORITY 6, band 0) first. Band 1 is a pfifo, not the deploy's fq: fq
# under netem stalls a paced flow on this kernel (the packets it holds back
# are never sent), and the lab does not pace.
LAB_HOST_QDISC = [
    "qdisc add dev {device} parent 1:1 handle 10: prio bands 3 priomap {priomap}",
    "qdisc add dev {device} parent 10:1 handle 11: pfifo limit 10000",
    "qdisc add dev {device} parent 10:2 handle 12: pfifo limit 50000",
]
PRIOMAP = "1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"


def apply_netem(namespace: str | None, device: str, spec: str) -> None:
    words = spec.split()
    ordered = "ordered" in words
    words = [word for word in words if word != "ordered"]
    # `tc qdisc replace` of a netem qdisc changes the options that it is given
    # and keeps the others: a rate cap or a reorder setting of an earlier
    # scenario stayed on the link after it was "cleared". Delete and add in
    # one batch, so that the link carries exactly this specification.
    batch = [f"qdisc del dev {device} root",
             f"qdisc add dev {device} root handle 1: netem {' '.join(words)}"]
    # The host qdisc goes on what a node sends; a reader host is no node.
    host = namespace in names() and host_qdisc() == "prio"
    if host:
        # Its bands are FIFOs, so the child keeps the order that `ordered`
        # asks for as the pfifo did.
        batch += [line.format(device=device, priomap=PRIOMAP) for line in LAB_HOST_QDISC]
    elif ordered:
        batch.append(f"qdisc add dev {device} parent 1:1 pfifo limit 50000")
    run_in(namespace, ["tc", "-force", "-batch", "-"], check=False,
           text_input="\n".join(batch) + "\n")
    shown = run_in(namespace, ["tc", "qdisc", "show", "dev", device])
    if "netem" not in shown:
        raise LabError(f"{namespace or 'lab'}: netem was not applied to {device}: {shown[:200]}")
    for word in ("rate", "reorder", "loss", "duplicate", "corrupt"):
        if (word in shown.split()) != (word in words):
            raise LabError(f"{namespace or 'lab'}: {device} carries '{shown.strip()[:200]}', "
                           f"asked for '{' '.join(words)}'")
    if host:
        # Where possible: a node whose kernel refuses the child keeps the
        # netem alone, and the lab records it.
        record = load("qdisc.json")
        record[f"{namespace}/{device}"] = ("prio" if "qdisc prio 10: parent 1:1" in shown
                                           else f"refused: {shown.strip()[:200]}")
        save("qdisc.json", record)


def set_net(name: str, network: str, direction: str, spec: str | None) -> None:
    """Shape one link of a node. `direction` is `out` (node to network), `in`
    (network to node) or `both`. `spec` of None restores the healthy link. A
    spec is added to the base delay of the link unless it names a delay."""
    inside, outside = link_names(name)[network]
    if network == "vlan" and node(name)["vlan_ipv4"] is None:
        raise LabError(f"{name} has no private network")
    spec = base_spec(name, network) if spec in (None, "clear") else spec
    if "delay" not in spec:
        base = base_spec(name, network).replace("limit 50000 ", "")
        spec = spec.replace("limit 50000", f"limit 50000 {base}")
    if direction in ("out", "both"):
        apply_netem(name, inside, spec)
    if direction in ("in", "both"):
        apply_netem(None, outside, spec)
    shaped = load("shaped.json")
    shaped[f"{name}/{network}/{direction}"] = spec
    save("shaped.json", shaped)


def set_tcp_per_packet(per_packet: bool, site: str | None = None) -> None:
    """Make the caches of a site send one TCP segment in each packet, or let
    them hand the kernel large segments again.

    A sender on a veth link gives the kernel segments of up to 64 KB, and
    netem then loses or reorders 64 KB at a time. A scenario that puts loss,
    reordering or a rate cap on the path of TCP turns this on before its
    readers connect, so that netem acts on packets as a real link does. It
    costs the sender CPU for each packet, so it is off otherwise: the CPU of
    a node is then what it is with the segmentation offload of a real NIC.
    """
    segments = "1" if per_packet else "65535"
    for name in names():
        if site is not None and node(name)["site"] != site:
            continue
        sh_in(name, f"ip link set eth0 gso_max_segs {segments}", check=False)
    for name, *_ in READER_HOSTS:
        sh_in(name, f"ip link set eth0 gso_max_segs {segments}", check=False)
    state = load("shaped.json")
    if per_packet:
        state["tcp_per_packet"] = site or "all"
    else:
        state.pop("tcp_per_packet", None)
    save("shaped.json", state)


def set_wan(site: str, direction: str, spec: str | None) -> None:
    """Shape the link of a site to the wide-area network. `out` is the
    direction from the site."""
    base = netem_spec(delay_ms=SITE_DELAY_MS[site])
    spec = base if spec in (None, "clear") else spec
    if "delay" not in spec:
        spec = spec.replace("limit 50000", "limit 50000 " + base.replace("limit 50000 ", ""))
    short = site[:6]
    if direction in ("out", "both"):
        apply_netem(None, f"c-{short}", spec)
    if direction in ("in", "both"):
        apply_netem("core", f"w-{short}", spec)
    shaped = load("shaped.json")
    shaped[f"wan/{site}/{direction}"] = spec
    save("shaped.json", shaped)


def set_wan_link(site: str, up: bool) -> None:
    """Cut a site off the wide-area network, or join it again. The nodes of
    the site still reach each other."""
    run_in(None, ["ip", "link", "set", f"c-{site[:6]}", "up" if up else "down"])


def set_link(name: str, network: str, up: bool) -> None:
    """Cut a link, or join it again. The interface of the node stays up, as
    it does when a switch port or a VLAN fails."""
    _inside, outside = link_names(name)[network]
    run_in(None, ["ip", "link", "set", outside, "up" if up else "down"])


def join_load(host: str, entry: str, stream: int, rate: int, seconds: int,
              label: str) -> dict:
    """New playback sessions at an entry point at a fixed rate, from a reader
    host. Returns the report of `join-load.py`."""
    out = run_in(host, [
        "python3", str(ROOT / "scripts" / "join-load.py"),
        "--url", f"{base_url(entry)}/live/{stream}/master.m3u8",
        "--rate", str(rate), "--seconds", str(seconds), "--ca", str(ca_file()),
        "--server-name", SERVER_NAME, "--label", label], timeout=seconds + 120)
    return json.loads(out.strip().splitlines()[-1])


def node_log(name: str) -> str:
    """The log of the service of one node."""
    try:
        return (WORK / "logs" / f"{name}.log").read_text(errors="replace")
    except OSError:
        return ""


CLOCK_SHIM_SOURCE = pathlib.Path(__file__).resolve().parent / "clock_shift.c"


def clock_shim() -> pathlib.Path:
    return WORK / "bin" / "clock_shift.so"


def build_clock_shim() -> None:
    """Build the library that lets one service of the lab have its own wall
    clock. Without a C compiler the lab runs as before, and the clock
    scenarios need real instances."""
    import shutil
    compiler = shutil.which("cc") or shutil.which("gcc")
    clock_shim().unlink(missing_ok=True)
    if not compiler or not CLOCK_SHIM_SOURCE.is_file():
        return
    (WORK / "bin").mkdir(parents=True, exist_ok=True)
    subprocess.run([compiler, "-O2", "-fPIC", "-shared", "-o", str(clock_shim()),
                    str(CLOCK_SHIM_SOURCE), "-ldl"], capture_output=True)


def set_clock(name: str, offset_seconds: float | None) -> None:
    """Move the wall clock of the service of one node, or give it back.

    Namespaces share the clock of the host, so the service is started with
    `clock_shift.so` preloaded, which adds the seconds in a file of the node
    to CLOCK_REALTIME. The step reaches the running service within 50 ms.
    The monotonic clock does not move, as on an instance whose clock is
    stepped."""
    if not clock_shim().is_file():
        raise LabError("the clock of one node cannot be moved in this lab: no clock shim "
                       "(a C compiler is needed when the lab starts)")
    node(name)
    directory = WORK / "clock"
    directory.mkdir(parents=True, exist_ok=True)
    if offset_seconds is None:
        (directory / name).unlink(missing_ok=True)
    else:
        (directory / name).write_text(f"{float(offset_seconds)}\n")
    time.sleep(0.12)


def fill_disk(name: str, full: bool) -> int:
    """Fill the disk of one node. The nodes of the namespace lab share a
    disk, so this needs real instances."""
    raise LabError("the disk of one node cannot be filled in the namespace lab")


def heal_all() -> None:
    """Put every link back to its healthy state."""
    links = load("links.json")
    for name in links:
        item = node(name)
        for network in ("pub", "vlan"):
            if network == "vlan" and item["vlan_ipv4"] is None:
                continue
            set_link(name, network, True)
            set_net(name, network, "both", None)
    for site in SITE_PUBLIC:
        set_wan_link(site, True)
        set_wan(site, "both", None)
    set_tcp_per_packet(False)
    save("shaped.json", {})
    for file in (WORK / "clock").glob("*"):
        file.unlink(missing_ok=True)


def build_networks() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        ["unshare", "-Urn", "sleep", "infinity"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    namespaces = {"lab": holder.pid}
    save("namespaces.json", namespaces)
    time.sleep(0.3)
    everyone = [name for name, *_ in NODES] + [name for name, *_ in READER_HOSTS]
    for name in ["core"] + everyone:
        child = subprocess.Popen(
            enter(None) + ["unshare", "-n", "sleep", "infinity"], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        namespaces[name] = child.pid
    save("namespaces.json", namespaces)
    save("links.json", {name: index for index, name in enumerate(everyone)})
    time.sleep(0.3)
    # The lab namespace holds the bridges: the public network of each site
    # and the private network of each region. It has an address on each, so
    # that the campaign reaches every node without a wide-area link.
    script = ["set -e", "ip link set lo up"]
    for site, prefix in SITE_PUBLIC.items():
        short = site[:6]
        script += [f"ip link add bp-{short} type bridge", f"ip link set bp-{short} up",
                   f"ip addr add {prefix}.254/24 dev bp-{short}",
                   # The link of the site to the wide-area router.
                   f"ip link add c-{short} type veth peer name w-{short} "
                   f"netns {namespaces['core']}",
                   f"ip link set c-{short} master bp-{short}", f"ip link set c-{short} up"]
    for site, prefix in SITE_VLAN.items():
        short = site[:6]
        script += [f"ip link add bv-{short} type bridge", f"ip link set bv-{short} up",
                   f"ip addr add {prefix}.254/24 dev bv-{short}"]
    for index, name in enumerate(everyone):
        item = node(name)
        pid = namespaces[name]
        short = item["site"][:6]
        script += [f"ip link add p{index} type veth peer name eth0 netns {pid}",
                   f"ip link set p{index} master bp-{short}", f"ip link set p{index} up"]
        if item["vlan_ipv4"]:
            script += [f"ip link add v{index} type veth peer name eth1 netns {pid}",
                       f"ip link set v{index} master bv-{short}",
                       f"ip link set v{index} up"]
    sh_in(None, "\n".join(script))
    script = ["set -e", "ip link set lo up", "sysctl -qw net.ipv4.ip_forward=1"]
    for site, prefix in SITE_PUBLIC.items():
        script += [f"ip addr add {prefix}.1/24 dev w-{site[:6]}",
                   f"ip link set w-{site[:6]} up"]
    sh_in("core", "\n".join(script))
    for name in everyone:
        item = node(name)
        script = ["set -e", "ip link set lo up",
                  f"ip addr add {item['public_ipv4']}/24 dev eth0",
                  "ip link set eth0 up",
                  f"ip route add 10.90.0.0/16 via {SITE_PUBLIC[item['site']]}.1"]
        if item["vlan_ipv4"]:
            script += [f"ip addr add {item['vlan_ipv4']}/24 dev eth1",
                       "ip link set eth1 up"]
        sh_in(name, "\n".join(script))
    heal_all()


# ------------------------------------------------------------------ services

def deploy_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    import small_mesh_common as common
    spec = importlib.util.spec_from_file_location(
        "deploy", ROOT / "scripts" / "linode-small-mesh-deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    artifacts = WORK / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    common.ARTIFACT_DIR = artifacts
    deploy.SETTINGS_FILE = artifacts / "settings.json"
    deploy.HUNT_FILE = artifacts / "ecmp-ports.json"
    return deploy


def lab_state() -> dict:
    """The lab as the deploy tool's state. Its "slots" are the slot
    candidates: the deploy renders them into every peer list, and a
    distributor slot is a lease child, never a compiled relay child.
    Under rt=1 every node is the Linode type of its RT_CPUS vCPUs, so that
    the deploy's "auto" settings render what they render on Linode for it
    (relay_class_cpus: the audio class thread on CPU 0, the video one on 1;
    service_args puts them on the node's own CPUs)."""
    kind = f"g6-dedicated-{RT_CPUS}" if load("rt.json").get("layout") else "local"
    return {"run_id": "edge-lab", "hourly_usd": 0.0,
            "nodes": {name: {**node(name), "type": kind} for name, *_ in NODES},
            "slots": {name: {**node(name), "type": kind, "slot": True}
                      for name in SLOT_CANDIDATES}}


def build_sources() -> dict:
    """The binaries of each build set, where they are built: set a always;
    set b (the build that a roll or an upgrade moves nodes to) when
    NEEDLETAIL_AV_MESH_BIN_NEXT or NEEDLETAIL_AV_CONTRIB_BIN_NEXT is given,
    with set a's binary for the service that has no other."""
    out = {"a": {"av-mesh": AV_MESH, "needletail-reader": READER, "av-contrib": AV_CONTRIB,
                 "aep1-48k-probe": PROBE, "needletail-load": LOAD_BIN}}
    if AV_MESH_NEXT or AV_CONTRIB_NEXT:
        out["b"] = {"av-mesh": AV_MESH_NEXT or AV_MESH,
                    "av-contrib": AV_CONTRIB_NEXT or AV_CONTRIB}
    return out


def snapshot_binaries() -> dict:
    """Copy the binaries into the state directory, a directory for each
    build set (bin/a, and bin/b for a second build), and run them from
    there, so that a build during a long run does not change the lab under
    it. `binaries.json` is {set: {label: path}}."""
    import shutil
    out = {}
    for build, sources in build_sources().items():
        directory = WORK / "bin" / build
        directory.mkdir(parents=True, exist_ok=True)
        out[build] = {}
        for label, source in sources.items():
            if label == "needletail-load" and not source.is_file():
                continue
            target = directory / label
            temporary = directory / (label + ".new")
            shutil.copy2(source, temporary)
            temporary.replace(target)
            out[build][label] = str(target)
    save("binaries.json", out)
    return out


def build_sets() -> list[str]:
    """The build sets of the running lab: ["a"], or ["a", "b"]."""
    return sorted(load("binaries.json")) or ["a"]


def binary(label: str, build: str = "a") -> str:
    """Path of a binary of the running lab: the tools and, without `build`,
    the services of set a."""
    paths = load("binaries.json").get(build, {})
    if label in paths:
        return paths[label]
    sources = build_sources()
    if build not in sources:
        raise LabError(f"this lab has no build set {build}: `up` it with "
                       "NEEDLETAIL_AV_MESH_BIN_NEXT or NEEDLETAIL_AV_CONTRIB_BIN_NEXT")
    return str(sources[build][label])


def set_file(stem: str, build: str) -> str:
    """The state file of a build set: `rendered.json` and `settings.json`
    are set a's (the shape every reader of them knows), `rendered-b.json`
    and `settings-b.json` set b's."""
    return f"{stem}.json" if build == "a" else f"{stem}-{build}.json"


# Settings of the lab's render that the deploy tool does not have, each for
# a proof that needs a build to misbehave. `relay_lease_children=false`: the
# distributors feed no edge on the fast plane (--relay-lease-children false).
LAB_RENDER_SETTINGS = ("relay_lease_children",)


def render(overrides: list[str], build: str = "a") -> dict:
    """Service arguments of every node on a build set, as the Linode deploy
    tool renders them for that set's binaries (their flags). Only the paths
    of the binaries and of the TLS files differ."""
    lab_only, given = {}, []
    for item in overrides:
        key, _, value = item.partition("=")
        if key not in LAB_RENDER_SETTINGS:
            given.append(item)
        elif value in ("true", "false"):
            lab_only[key] = value == "true"
        else:
            raise LabError(f"{key}={value}: true or false")
    deploy = deploy_module()
    deploy.ensure_tls()
    (WORK / "artifacts" / "settings.json").unlink(missing_ok=True)
    merged = [f"{key}={json.dumps(value)}" for key, value in DEFAULT_OVERRIDES.items()]
    settings = deploy.load_settings(merged + given)
    lon_ny_us = int(2000 * (SITE_DELAY_MS["london"] + SITE_DELAY_MS["newyork"]))
    routes = {"best_direct_rtt_us": lon_ny_us, "primary_rtt_us": lon_ny_us,
              "primary_jitter_us": 1000, "secondary_rtt_us": lon_ny_us + 6000,
              "secondary_jitter_us": 1000}
    state = lab_state()
    plan = deploy.compile_plan(deploy.build_program(state, settings, routes))
    # The render looks at the flags of the binaries that this set runs.
    deploy.RENDER_BINARIES.clear()
    deploy.RENDER_BINARIES.update({"av-mesh": binary("av-mesh", build),
                                   "av-contrib": binary("av-contrib", build)})
    rendered = deploy.render(state, settings, plan)
    if build == "a":
        save("sysctls.json", deploy.node_sysctls(settings))
    save(set_file("settings", build), settings)
    tls = WORK / "artifacts" / "tls"
    out = {}
    for name, item in rendered.items():
        args = list(item["args"])
        contributor = item["service"] == "needletail-contrib"
        args[0] = binary("av-contrib" if contributor else "av-mesh", build)
        fixed = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("--cert", "--key"):
                fixed += [arg, str(tls / pathlib.Path(args[index + 1]).name)]
                index += 2
            elif arg == "--drain-state-file":
                # The deploy's /var/lib/needletail/<service>.drained: a
                # node's own file in this lab's state directory.
                fixed += [arg, str(WORK / "state" / f"{name}.drained")]
                index += 2
            else:
                fixed.append(arg)
                index += 1
        if contributor:
            fixed += ["--srt-bind", "0.0.0.0:27001", "--srt-stream-id", str(VIDEO_STREAM)]
        elif node(name)["role"] == "edge" and "--fec-bind" not in fixed:
            # An edge on the cache mesh alone; one fed by the fast plane
            # (edge_fast_plane) has its relay parents' --fec-bind already.
            fixed += ["--fec-bind", "0.0.0.0:12001"]
        if (lab_only.get("relay_lease_children") is False
                and node(name)["role"] == "distributor" and "--relay-lease-children" in fixed):
            fixed[fixed.index("--relay-lease-children") + 1] = "false"
        out[name] = {"args": fixed, "rust_log": item["rust_log"]}
    if lab_only.get("relay_lease_children") is False and not any(
            "--relay-lease-children" in out[name]["args"] for name in out
            if node(name)["role"] == "distributor"):
        raise LabError(f"relay_lease_children=false: no distributor of build set {build} is "
                       "rendered with --relay-lease-children, so the setting would change "
                       "nothing")
    (WORK / "state").mkdir(parents=True, exist_ok=True)
    save(set_file("rendered", build), out)
    return out


def node_build(name: str) -> str:
    """The build set that a node's service runs, and starts on again."""
    return load("node-build.json").get(name, "a")


def set_node_build(name: str, build: str) -> None:
    """Install a build on one node: its service runs set `build` from its
    next start (`node-build.json`). Nothing is stopped or started."""
    if build not in build_sets():
        raise LabError(f"this lab has no build set {build} (it has {', '.join(build_sets())})")
    if name not in load(set_file("rendered", build)):
        raise LabError(f"{name} has no service in build set {build}")
    builds = load("node-build.json")
    builds[name] = build
    save("node-build.json", builds)


def rendered_service(name: str, build: str) -> dict:
    """What a build set's render holds for a node: {"args", "rust_log"}."""
    item = load(set_file("rendered", build)).get(name)
    if item is None:
        raise LabError(f"{name} has no service in this lab's topology"
                       + (" (merged: no backbone relay; up topology=legacy)"
                          if node(name)["role"] == "backbone" else ""))
    return item


def service_args(name: str, build: str | None = None) -> list[str]:
    """The command line of a node's service on a build set (without one,
    the node's own): the set's render, with --relay-class-cpus in the
    node's own CPUs once it has them (start_service gives a slot its CPUs
    before it asks)."""
    args = list(rendered_service(name, build or node_build(name))["args"])
    if "--relay-class-cpus" in args:
        at = args.index("--relay-class-cpus") + 1
        args[at] = node_class_cpus(name, args[at])
    return args


def start_service(name: str, limits: dict | None = None, env: dict | None = None,
                  extra_args: list[str] | None = None, build: str | None = None) -> int:
    """Start the service of a node in its namespace and in its own cgroup,
    on its build set, or on `build`, which is then the node's set."""
    if build is not None:
        set_node_build(name, build)
    build = node_build(name)
    rust_log = rendered_service(name, build)["rust_log"]
    # The node's own CPUs (cpus, node_cpus), as an instance has.
    cpus = node_cpus(name)
    args = service_args(name, build)
    settings = load(set_file("settings", build))
    given = {"limits": limits, "env": env, "extra_args": extra_args}
    limits = {**default_limits(name), **(limits or load("limits.json").get(name, {}))}
    # The scope of the node's last start ends when its last process has
    # gone (the service, or a burn's loops, which follow it within a few
    # milliseconds); a scope that still runs cannot be started again.
    deadline = time.time() + 5
    while unit_active(unit_name(name)):
        if time.time() > deadline:
            raise LabError(f"{unit_name(name)} still runs: {name} cannot be started again "
                           "until it has stopped")
        time.sleep(0.1)
    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    log = open(WORK / "logs" / f"{name}.log", "ab")
    subprocess.run(["systemctl", "--user", "reset-failed", unit_name(name)],
                   capture_output=True)
    pin = ["taskset", "-c", cpu_list(cpus)] if cpus else []
    # rt=1: the scope's first process raises its own real-time limits and
    # becomes the service (RT_GRANT); systemd-run must leave its $$ alone.
    grant = rt_grant()
    command = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
               *(["--expand-environment=no"] if grant else []),
               "--unit", unit_name(name),
               "-p", f"CPUQuota={limits['cpu_percent']}%",
               "-p", f"MemoryMax={limits['memory_mb']}M",
               "-p", "MemorySwapMax=0",
               # A process may be put into the node's cgroup (burn).
               "-p", "Delegate=yes",
               "--", *grant, *nsenter(name), *pin, *args, *(extra_args or [])]
    # A service on a Nanode sees one CPU and starts one worker thread. Here it
    # would see every CPU of the host, so it is told the number that its
    # limit stands for.
    threads = settings.get("tokio_worker_threads") or max(
        1, limits["cpu_percent"] // 100)
    shim = ({"LD_PRELOAD": str(clock_shim()),
             "NEEDLETAIL_CLOCK_OFFSET_FILE": str(WORK / "clock" / name)}
            if clock_shim().is_file() else {})
    process = subprocess.Popen(
        command, env={**os.environ, **shim, "RUST_LOG": rust_log,
                      "TOKIO_WORKER_THREADS": str(threads),
                      **{key: str(value) for key, value in
                         (settings.get("node_env") or {}).items()},
                      **(env or {})},
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    log.close()
    services = load("services.json")
    # What it was started with, so that a crash brings it back the same.
    services[name] = {"pid": process.pid, "started_unix": time.time(), "build": build,
                      "starts": services.get(name, {}).get("starts", 0) + 1,
                      "crashes": services.get(name, {}).get("crashes", 0), **given}
    save("services.json", services)
    return process.pid


def unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                          capture_output=True).returncode == 0


def service_pid(name: str) -> int | None:
    pid = load("services.json").get(name, {}).get("pid")
    if pid is None:
        return None
    try:
        command = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return pid if command else None


def cgroup_dir(name: str) -> pathlib.Path | None:
    pid = service_pid(name)
    return None if pid is None else cgroup_of(pid)


def kill_service(name: str) -> None:
    """Stop a service at once, as a crash or a lost host does."""
    pid = service_pid(name)
    if pid:
        os.kill(pid, signal.SIGKILL)


def freeze_service(name: str, frozen: bool) -> None:
    """Freeze a service, or let it run again. A frozen service keeps its
    sockets open and answers nothing, as a stalled host does."""
    pid = service_pid(name)
    if pid:
        os.kill(pid, signal.SIGSTOP if frozen else signal.SIGCONT)


def terminate_service(name: str, wait: float = 15.0) -> None:
    """Ask a service to stop, as `systemctl stop` does: SIGTERM, and SIGKILL
    after the unit's TimeoutStopSec (15 s) if it is still there."""
    pid = service_pid(name)
    if not pid:
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + wait
    while time.time() < deadline and service_pid(name):
        time.sleep(0.1)
    kill_service(name)


def restart_service(name: str, **kwargs) -> int:
    kill_service(name)
    deadline = time.time() + 5
    while time.time() < deadline and service_pid(name):
        time.sleep(0.05)
    return start_service(name, **kwargs)


def wait_back(name: str, timeout: float = 60.0) -> str:
    """Wait until a node that was started again is back: its `/api/node`
    says `ready`, or, from a build without `/api/node` (it answers 404),
    `/up` answers. Returns what was seen; LabError when the service stops
    again or `timeout` (the tool's readiness bound) passes first."""
    deadline = time.time() + timeout
    seen = "no answer"
    while True:
        if service_pid(name) is None:
            raise LabError(f"{name} stopped again after its start:\n{node_log(name)[-1200:]}")
        code, body = http_json(name, "/api/node", timeout=2.0)
        if code == 200 and isinstance(body, dict):
            seen = f"state {body.get('state')}"
            if body.get("state") == "ready":
                return "ready"
        elif code == 404:
            up_code, _body = http_json(name, "/up", timeout=2.0)
            seen = f"no /api/node, /up {up_code}"
            if up_code == 200:
                return "up"
        if time.time() > deadline:
            raise LabError(f"{name} was not back within {timeout:g} s ({seen})")
        time.sleep(0.25)


def crash_service(name: str, restart_s: float = 1.0, ready_timeout_s: float = 60.0) -> int:
    """A crash of a node's service and its return: SIGKILL, and a start
    `restart_s` after the process is gone, as systemd starts a unit again
    after its RestartSec (1 s), with what it was started with (build set,
    limits, environment, extra arguments). Returns, with the new pid, only
    when the node is back (wait_back), so that a wrapper's marks around the
    command hold the whole change."""
    if service_pid(name) is None:
        raise LabError(f"{name} is not running")
    given = load("services.json").get(name, {})
    kill_service(name)
    deadline = time.time() + 5
    while time.time() < deadline and service_pid(name):
        time.sleep(0.02)
    time.sleep(restart_s)
    pid = start_service(name, limits=given.get("limits"), env=given.get("env"),
                        extra_args=given.get("extra_args"))
    # What systemd counts as NRestarts: a start after a crash (EdgeLabOps).
    services = load("services.json")
    services[name]["crashes"] = services[name].get("crashes", 0) + 1
    save("services.json", services)
    wait_back(name, ready_timeout_s)
    return pid


def burn(name: str, seconds: float) -> float:
    """Busy loops in the cgroup of a node's service for `seconds`, one for
    each CPU of its limit, on its CPUs: they take the node's own CPU time
    under its quota, as a noisy neighbour inside an instance does. A loop
    ends with the service, so that it does not keep the node's scope (and
    the node's next start) after a kill, a crash or a `down`. Returns the
    seconds they ran."""
    pid = service_pid(name)
    if pid is None:
        raise LabError(f"{name} is not running")
    cpus = load("cpus.json").get(name)
    pin = ["taskset", "-c", cpu_list(cpus)] if cpus else []
    # Each loop waits for a line, so that it is in the node's cgroup before
    # it uses any CPU, and looks every few milliseconds whether the service
    # still runs (a pid whose command line is gone has ended).
    script = ("import sys, time\nsys.stdin.readline()\n"
              "end = time.monotonic() + float(sys.argv[1])\n"
              "service = '/proc/' + sys.argv[2] + '/cmdline'\n"
              "turn = 0\n"
              "while time.monotonic() < end:\n"
              "    turn += 1\n"
              "    if turn % 20000 == 0:\n"
              "        try:\n"
              "            if not open(service, 'rb').read():\n"
              "                break\n"
              "        except OSError:\n"
              "            break\n")
    loops = [subprocess.Popen([*pin, sys.executable, "-c", script, str(seconds), str(pid)],
                              stdin=subprocess.PIPE, start_new_session=True)
             for _ in range(max(1, service_limits(name)["cpu_percent"] // 100))]
    started = time.monotonic()
    try:
        for loop in loops:
            attach(unit_name(name), loop.pid)
        for loop in loops:
            loop.stdin.write(b"\n")
            loop.stdin.close()
        for loop in loops:
            loop.wait()
    finally:
        for loop in loops:
            if loop.poll() is None:
                loop.kill()
    return time.monotonic() - started


def set_limits(name: str, cpu_percent: int | None = None,
               memory_mb: int | None = None) -> None:
    """Change the limits of a running service."""
    properties = []
    if cpu_percent is not None:
        properties.append(f"CPUQuota={cpu_percent}%")
    if memory_mb is not None:
        properties.append(f"MemoryMax={memory_mb}M")
    result = subprocess.run(["systemctl", "--user", "set-property", "--runtime",
                             unit_name(name), *properties], capture_output=True, text=True)
    if result.returncode != 0:
        raise LabError(f"set-property {name}: {result.stderr.strip()}")


def switch(key: str, value: str) -> bool:
    """A lab setting that is on or off: `take=1`, `cpus_overlap=1`."""
    if value not in ("0", "1"):
        raise LabError(f"{key}={value}: 1 or 0")
    return value == "1"


def up(overrides: list[str], start: bool = True) -> dict:
    sources = build_sources()
    for binary in (AV_MESH, READER, AV_CONTRIB, PROBE, *sources.get("b", {}).values()):
        if not binary.is_file():
            raise LabError(f"binary is missing: {binary}")
    limits = {}
    plain = []
    # Settings that set b alone renders with (`b.<setting>=<value>`).
    next_only = []
    slots = set(DEFAULT_SLOTS)
    first_cpu = None
    cpus = os.environ.get("NEEDLETAIL_EDGE_LAB_CPUS") or None
    take = overlap = realtime = layout = False
    for item in overrides:
        key, _, value = item.partition("=")
        if key == "rt":
            # A lab setting: the nodes may use real-time scheduling
            # (RT_GRANT), on two CPUs each with their class threads pinned;
            # rt=pin: the same layout and pinning without the grant.
            if value not in ("0", "1", "pin"):
                raise LabError(f"rt={value}: 1, pin or 0")
            realtime, layout = value == "1", value in ("1", "pin")
            continue
        if key == "node_cpus":
            # A lab setting: each node its own host CPUs (node_cpu_sets).
            value = value.strip().strip('"')
            first_cpu = int(value) if value not in ("", "null", "none") else None
            continue
        if key == "cpus":
            # A lab setting: the host CPUs that the lab stays on.
            cpus = value.strip().strip('"') or None
            continue
        if key == "take":
            take = switch(key, value)
            continue
        if key == "cpus_overlap":
            overlap = switch(key, value)
            continue
        if key == "slots":
            given = {name for name in value.split(",") if name}
            unknown = sorted(given - set(SLOT_CANDIDATES))
            if unknown:
                raise LabError(f"slots: {unknown} are not slot candidates "
                               f"({', '.join(SLOT_CANDIDATES)})")
            slots |= given
            continue
        if key.startswith("limit."):
            # limit.<node>.cpu_percent=50, limit.<node>.memory_mb=512
            _, name, field = key.split(".", 2)
            limits.setdefault(name, {})[field] = int(value)
        elif key.startswith("b."):
            if "b" not in sources:
                raise LabError(f"{item}: this lab has one build set; set b needs "
                               "NEEDLETAIL_AV_MESH_BIN_NEXT or NEEDLETAIL_AV_CONTRIB_BIN_NEXT")
            if key[2:] in ("node_cpus", "cpus", "take", "cpus_overlap", "slots", "rt") \
                    or key[2:].startswith(("limit.", "b.")):
                raise LabError(f"{item}: {key[2:].split('.')[0]} is the lab's, not a build "
                               "set's")
            next_only.append(item[2:])
        else:
            plain.append(item)
    if first_cpu is not None and cpus is not None:
        raise LabError(f"node_cpus={first_cpu} is the layout of one lab on the whole host and "
                       f"cpus={cpus} that of a lab in a range: give one")
    if layout:
        if first_cpu is None and cpus is None:
            raise LabError("rt=1 pins each node's class threads to CPUs of its own: give the "
                           "lab a layout, cpus=A-B (or NEEDLETAIL_EDGE_LAB_CPUS) or node_cpus=FIRST")
    if realtime:
        rt_preflight()
    # In a range, a slot that is held out takes its window when it is taken
    # in; with node_cpus every slot has its own CPUs from the start, as the
    # reader hosts are pinned to what is left (node_cpu_sets).
    sets = node_cpu_sets([name for name, role, *_ in NODES
                          if role != "backbone" and (cpus is None or name not in slots)],
                         first=first_cpu, cpus=None if cpus is None else cpu_range(cpus),
                         per_node=RT_CPUS if layout else None)
    # Refused here, with nothing stopped yet, when the lab is another
    # owner's or its CPUs are another lab's; else the lab that ran in this
    # state directory is stopped.
    claim(lab_cpus(sets), take=take, overlap=overlap)
    limits = pinned_limits(sets, limits)
    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    for log in (WORK / "logs").glob("*"):
        if log.is_file():
            log.unlink()
    # New nodes: no operator's drain of the lab before is kept.
    for kept in (WORK / "state").glob("*.drained"):
        kept.unlink()
    save("limits.json", limits)
    save("cpus.json", sets)
    save("rt.json", {"rt": realtime, "layout": layout, "cpus_per_node": RT_CPUS,
                     **({"rtprio": RT_PRIO, "nice": RT_NICE} if realtime else {})}
         if layout else {"rt": False, "layout": False})
    (WORK / "rt-check.json").unlink(missing_ok=True)
    save("slots.json", sorted(slots))
    save("slots-created.json", [])
    start_tools()
    join_tools()
    build_networks()
    register_holder(lab_pid())
    snapshot_binaries()
    build_clock_shim()
    # Set a last: the deploy tool's files in the state directory are then
    # those of the set that every node starts on.
    for stem in ("rendered", "settings"):
        (WORK / set_file(stem, "b")).unlink(missing_ok=True)
    if "b" in sources:
        render(plain + next_only, build="b")
    rendered = render(plain)
    save("node-build.json", {name: "a" for name in rendered})
    # The kernel settings that the deploy tool gives a node.
    sysctls = load("sysctls.json")
    if sysctls:
        script = "; ".join(f"sysctl -qw {key}={value}" for key, value in sysctls.items())
        for name in rendered:
            sh_in(name, script, check=False)
    # The host qdisc of this run's settings (the links were built before
    # they were rendered).
    save("qdisc.json", {})
    for name in rendered:
        for network in ("pub", "vlan"):
            if network == "vlan" and node(name)["vlan_ipv4"] is None:
                continue
            apply_netem(name, link_names(name)[network][0], base_spec(name, network))
    if not start:
        return rendered
    for role in ("backbone", "distributor", "edge", "contributor"):
        # The merged topology renders no backbone relay; a slot stays down.
        for name in standing_services(role):
            start_service(name)
        time.sleep(1.0)
    time.sleep(1.0)
    held = held_slots()
    dead = [name for name in rendered if service_pid(name) is None and name not in held]
    if dead:
        tails = "\n".join(f"--- {name}\n" + (WORK / "logs" / f"{name}.log").read_text()[-1200:]
                          for name in dead)
        raise LabError(f"services did not start: {dead}\n{tails}")
    if layout:
        rt_verify()
    return rendered


def kill_lab(work: pathlib.Path) -> None:
    """Stop the lab whose state directory is `work`: every service, source
    and namespace and the holder of its tools scope, and forget what ran."""
    def saved(name: str) -> dict:
        try:
            found = json.loads((work / name).read_text())
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    pids = [item["pid"] if isinstance(item, dict) else item
            for item in list(saved("services.json").values())
            + list(saved("sources.json").values())]
    pids += list(saved("namespaces.json").values())
    pids += [pid for pid in [saved("tools.json").get("pid")] if pid]
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    for path in ("services.json", "namespaces.json", "sources.json", "links.json",
                 "shaped.json", "qdisc.json", "cpus.json", "tools.json"):
        (work / path).unlink(missing_ok=True)


def down(quiet: bool = False) -> None:
    kill_lab(WORK)
    unregister()
    if not quiet:
        print("down")


# ------------------------------------------------------------------ sources

def probe_epoch_samples(payload: str) -> int:
    """Samples of one AEP1 epoch of the lab's sender for a payload, from the
    settings of the running lab (`pcm_epoch_samples`, `opus_epoch_samples`).
    FLAC frames are at least 32 samples."""
    settings = load("settings.json")
    if payload == "opus":
        return int(settings.get("opus_epoch_samples") or 48)
    samples = int(settings.get("pcm_epoch_samples") or 48)
    return max(32, samples) if payload == "flac" else samples


def opus_bitrate() -> int:
    """Opus bitrate (CBR) of the lab's Opus sources, from the settings of the
    running lab (`opus_bitrate`): the lane's 320 kb/s by default."""
    return int(load("settings.json").get("opus_bitrate") or 320000)


def contributor_targets() -> list[str]:
    """`address:27100` of every contributor on the London VLAN, the origin
    first: where a source sends each AEP1 epoch (the deploy's
    contributor_targets of this lab)."""
    import small_mesh_common
    return small_mesh_common.contributor_targets(lab_state())


def source_prefix() -> list[str]:
    """Command prefix of a source: in lon-src's namespace and the lab's tools
    scope, on lon-src's own CPUs (a starved sender looks like a mesh stall),
    or on the readers' CPUs in a lab without them."""
    prefix = nsenter(SOURCE_HOST)
    join_tools()
    cpus = node_cpus(SOURCE_HOST) or load("cpus.json").get("_rest")
    if cpus:
        prefix += ["taskset", "-c", cpu_list(cpus)]
    return prefix


def start_sources(seconds: int, video: bool = True, audio: str | None = "probe",
                  clip: pathlib.Path | None = None) -> None:
    """Start the sources in lon-src, an encoder on the contributors' VLAN as
    on Linode: every AEP1 epoch to each contributor (contributor_targets),
    the video to one contributor at a time through the feed loop
    (scripts/video-feed.sh: contrib-lon first, the other when it fails).
    `audio` is `probe` (lossless FLAC), `daw` (FLAC and HQ SoundKit Opus) or
    None."""
    sources = load("sources.json")
    targets = contributor_targets()
    feed = [str(VIDEO_FEED), "--seconds", str(seconds),
            *[target.rsplit(":", 1)[0] for target in targets], "--"]
    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    if video and "video" not in sources and load("settings.json").get("hls_renditions"):
        # An encoder that sends a ladder: one process reads every rendition
        # at its own rate and publishes each under the key of its stream.
        log = open(WORK / "logs" / "source-video.log", "ab")
        command = ["-hide_banner", "-loglevel", "error", "-nostdin"]
        for _stream, path, *_ in LADDER:
            command += ["-re", "-stream_loop", "-1", "-i", str(path)]
        for index, (stream, *_rest) in enumerate(LADDER):
            command += ["-map", f"{index}:v:0", "-map", f"{index}:a:0", "-c", "copy",
                        "-f", "flv", f"rtmp://{{host}}:{RTMP_PORT}/live/{stream}"]
        process = subprocess.Popen(source_prefix() + feed + command, stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        sources["video"] = process.pid
        log.close()
    if video and "video" not in sources and clip is not None and referee_clip(clip):
        # A clip of the referee's (needletail-compare prepare-media: DIR/video.h264,
        # Annex B, constant bit rate with filler): sent as the referee sends it to
        # a Needletail contributor, `ntcmp flv` (frames paced at the clip's rate,
        # filler dropped, no audio) into ffmpeg and RTMP.
        log = open(WORK / "logs" / "source-video.log", "ab")
        command = (f"{shlex.quote(str(NTCMP))} flv --clip {shlex.quote(str(clip))} --no-audio "
                   f"--fps {referee_clip(clip)['fps']} | "
                   + shlex.join(feed + ["-hide_banner", "-loglevel", "error", "-f", "flv",
                                        "-i", "pipe:0", "-c", "copy", "-f", "flv",
                                        f"rtmp://{{host}}:{RTMP_PORT}/live/needletail-lab"]))
        process = subprocess.Popen(source_prefix() + ["sh", "-c", command], stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        sources["video"] = process.pid
        log.close()
    if video and "video" not in sources:
        log = open(WORK / "logs" / "source-video.log", "ab")
        process = subprocess.Popen(
            source_prefix() + feed + [
                "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
                # A 4K clip of 120 Mbit/s or more holds its first audio
                # packet past the default 5 MB probe: without a longer one
                # ffmpeg finds no sample rate and sends nothing.
                "-probesize", "64M", "-analyzeduration", "10000000",
                "-stream_loop", "-1", "-i", str(clip or VIDEO_CLIP),
                "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-muxdelay", "0",
                "-muxpreload", "0", "-f", "mpegts",
                "srt://{host}:27001?mode=caller&transtype=live&latency=80"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["video"] = process.pid
        log.close()
    if audio == "daw" and "audio" not in sources:
        log = open(WORK / "logs" / "source-audio.log", "ab")
        process = subprocess.Popen(
            source_prefix() + [
                str(DAW_SOURCE), "--direct-contributor", "--loop-prepared-pcm",
                # The DAW lane's Opus frame and rate: 1 ms Opus Custom at
                # 320 kb/s by default.
                "--opus-frame-samples", str(probe_epoch_samples("opus")),
                "--opus-bitrate", str(opus_bitrate()),
                # The lab's lossless lane is FLAC (the contributor's
                # daw_hls_formats "flac,opus"); daw-nexus now sends PCM
                # unless told, and the contributor then made no stream 2001.
                "--lossless-payload", "flac",
                # Each contributor an upstream: daw-nexus sends every epoch
                # to the first and then to each extra one (A DN1).
                ",".join(targets), str(seconds), str(daw_tracks())],
            env={**os.environ, "RUST_LOG": "info",
                 "DAW_TEST_SOURCE_START_UNIX_NS": str(time.time_ns() + 3_000_000_000),
                 "DAW_TEST_SOURCE_CAPACITY_OUTPUT": str(WORK / "logs" / "daw-capacity.ndjson")},
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["audio"] = process.pid
        log.close()
    elif audio == "probe" and "audio" not in sources:
        log = open(WORK / "logs" / "source-audio.log", "ab")
        process = subprocess.Popen(
            source_prefix() + [
                binary("aep1-48k-probe"), "send",
                *[item for target in targets for item in ("--target", target)],
                "--session-id", str(time.time_ns() + 2_000_000_000), "--group-id", "0",
                "--duration-seconds", str(seconds), "--payload", "flac",
                "--epoch-samples", str(probe_epoch_samples("flac")),
                "--channels", "2", "--group-channels", "8",
                "--pcm-s24le-file", str(AUDIO_PCM),
                "--repair-percent", "12", "--min-repair-symbols", "1"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["audio"] = process.pid
        log.close()
    save("sources.json", sources)


def stop_sources() -> None:
    for _name, pid in load("sources.json").items():
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    (WORK / "sources.json").unlink(missing_ok=True)


# ------------------------------------------------------------------ HTTP

def ca_file() -> str:
    return str(WORK / "artifacts" / "tls" / "ca.crt")


def curl(name: str, path: str, method: str = "GET", body: dict | None = None,
         timeout: float = 5.0, headers: dict | None = None, via: str | None = None,
         http3: bool = False) -> tuple[int, str, str]:
    """Call the HTTP service of a node from the lab namespace, or from the
    namespace `via`. Returns (status, response headers, body)."""
    port = CONTRIB_HTTP_PORT if node(name)["role"] == "contributor" else HTTP_PORT
    command = ["curl", "-sS", "--cacert", ca_file(), "--resolve",
               f"{SERVER_NAME}:{port}:{node(name)['public_ipv4']}", "-X", method,
               "-m", str(timeout), "-D", "-", "-o", "-",
               f"https://{SERVER_NAME}:{port}{path}"]
    if http3:
        command.insert(1, "--http3-only")
    for key, value in (headers or {}).items():
        command += ["-H", f"{key}: {value}"]
    if body is not None:
        command += ["-H", "content-type: application/json", "-d", json.dumps(body)]
    try:
        output = run_in(via, command, check=False, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return 0, "", ""
    head, _, text = output.partition("\r\n\r\n")
    while text.startswith("HTTP/"):
        head, _, text = text.partition("\r\n\r\n")
    fields = head.split()
    status = int(fields[1]) if head.startswith("HTTP/") and len(fields) > 1 else 0
    return status, head, text


def http_json(name: str, path: str, method: str = "GET", body: dict | None = None,
              timeout: float = 5.0):
    status, _head, text = curl(name, path, method, body, timeout)
    try:
        return status, (json.loads(text) if text.strip() else None)
    except json.JSONDecodeError:
        return status, text


def mesh_local(name: str) -> dict:
    status, snapshot = http_json(name, "/api/mesh/local")
    if status != 200 or not isinstance(snapshot, dict):
        raise LabError(f"{name}: /api/mesh/local answered {status}")
    return snapshot


def edge_view(name: str) -> dict:
    """What one cache holds and serves, from its own advertisement."""
    snapshot = mesh_local(name)
    edge = next((item for item in snapshot["edge_services"]
                 if item["node_id"] == name), {})
    replication = snapshot["cache_replication"]
    return {
        "node": name,
        "playlists": {str(item["stream_id"]): {
            "head_part": item["head_part"], "sessions": item["sessions"],
            "bitrate_mbps": round((item.get("bitrate_bps") or 0) / 1e6, 2)}
            for item in edge.get("playlists", [])},
        "subscribed": replication["subscribed_playlists"],
        "served_subscriptions": len(replication["served_subscriptions"]),
        "sessions": edge.get("active_playback_sessions", 0),
        "load_permille": edge.get("load_permille", 0),
        "committed_mbps": round(edge.get("committed_bps", 0) / 1e6, 1),
        "observed_mbps": round(edge.get("egress_observed_bps", 0) / 1e6, 1),
        "draining": edge.get("draining", False),
        "rejected": edge.get("egress_rejected_requests", 0),
    }


def wait_for(description: str, predicate, timeout: float = 30.0, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (LabError, KeyError, StopIteration, subprocess.TimeoutExpired) as error:
            last = error
        time.sleep(interval)
    raise LabError(f"timed out waiting for {description}; last: {last}")


def head_part(name: str, stream: int) -> int | None:
    snapshot = mesh_local(name)
    edge = next((item for item in snapshot["edge_services"]
                 if item["node_id"] == name), {})
    for item in edge.get("playlists", []):
        if item["stream_id"] == stream:
            return item["head_part"]
    return None


def wait_live(stream: int, site: str = "london", timeout: float = 40.0) -> None:
    """Wait until both distributors of a site hold a moving playlist."""
    def moving():
        heads = [head_part(name, stream) for name in distributors(site)]
        if any(head is None for head in heads):
            return False
        time.sleep(0.5)
        later = [head_part(name, stream) for name in distributors(site)]
        return all(b is not None and b > a for a, b in zip(heads, later))
    wait_for(f"stream {stream} live in {site}", moving, timeout)


# ------------------------------------------------------------------ metrics

def read_cgroup(directory: pathlib.Path) -> dict:
    out = {}
    try:
        for line in (directory / "cpu.stat").read_text().splitlines():
            key, value = line.split()
            if key in ("usage_usec", "nr_throttled", "throttled_usec"):
                out[key] = int(value)
        out["memory_bytes"] = int((directory / "memory.current").read_text())
        out["memory_peak_bytes"] = int((directory / "memory.peak").read_text())
        for line in (directory / "memory.events").read_text().splitlines():
            key, value = line.split()
            if key in ("oom_kill", "max"):
                out[f"memory_{key}"] = int(value)
        for line in (directory / "memory.stat").read_text().splitlines():
            key, value = line.split()
            if key in ("anon", "file", "sock", "kernel"):
                out[f"memory_{key}_bytes"] = int(value)
        out["tasks"] = int((directory / "pids.current").read_text())
    except (OSError, ValueError):
        pass
    return out


def read_process(pid: int) -> dict:
    out = {}
    try:
        descriptors = os.listdir(f"/proc/{pid}/fd")
        out["fds"] = len(descriptors)
        sockets = 0
        for descriptor in descriptors:
            try:
                if os.readlink(f"/proc/{pid}/fd/{descriptor}").startswith("socket:"):
                    sockets += 1
            except OSError:
                pass
        out["sockets"] = sockets
        for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                out["rss_bytes"] = int(line.split()[1]) * 1024
            elif line.startswith("Threads:"):
                out["threads"] = int(line.split()[1])
    except OSError:
        pass
    return out


NETDEV_INTERFACES = ("eth0", "eth1")


def read_netdev(pid: int) -> dict:
    """Packets and bytes that each interface of the namespace of `pid` sent
    and received (/proc/PID/net/dev: bytes include the 14-byte Ethernet
    header, as the referee's samplers count them): `eth0_tx_packets`,
    `eth0_tx_bytes`, `eth0_rx_packets`, `eth0_rx_bytes`, the same for eth1."""
    out = {}
    try:
        text = pathlib.Path(f"/proc/{pid}/net/dev").read_text()
    except OSError:
        return out
    for line in text.splitlines()[2:]:
        device, _, counters = line.partition(":")
        device = device.strip()
        if device not in NETDEV_INTERFACES:
            continue
        fields = counters.split()
        if len(fields) < 10:
            continue
        out[f"{device}_rx_bytes"] = int(fields[0])
        out[f"{device}_rx_packets"] = int(fields[1])
        out[f"{device}_tx_bytes"] = int(fields[8])
        out[f"{device}_tx_packets"] = int(fields[9])
    return out


def read_udp_sockets(pid: int) -> dict:
    """Per-socket UDP receive drops and receive-queue bytes of the namespace
    of `pid` (/proc/PID/net/udp and udp6: the kernel's `drops` column, a
    socket whose receive queue was full, as `ss -uamn` shows it in skmem
    `d`), keyed by local port (sockets of one port summed; the queue is the
    largest): `udp_socket_drops`, `udp_socket_rx_queue`. The node-wide
    RcvbufErrors cannot tell which socket overflowed (wave 8 stage 3: the
    referee's 4K losses were at one socket of each node)."""
    drops: dict[str, int] = {}
    queue: dict[str, int] = {}
    for table in ("udp", "udp6"):
        try:
            text = pathlib.Path(f"/proc/{pid}/net/{table}").read_text()
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 13:
                continue
            try:
                port = str(int(fields[1].rsplit(":", 1)[1], 16))
                rx_queue = int(fields[4].split(":")[1], 16)
                dropped = int(fields[-1])
            except (ValueError, IndexError):
                continue
            drops[port] = drops.get(port, 0) + dropped
            queue[port] = max(queue.get(port, 0), rx_queue)
    if not drops:
        return {}
    return {"udp_socket_drops": drops, "udp_socket_rx_queue": queue}


def socket_drop_deltas(running: list[dict]) -> dict:
    """What each port's sockets dropped over the samples (ports with a drop
    only), and each port's largest receive queue seen (bytes)."""
    have = [item["udp_socket_drops"] for item in running if "udp_socket_drops" in item]
    if len(have) < 2:
        return {}
    first, last = have[0], have[-1]
    drops = {port: last[port] - first.get(port, 0) for port in last
             if last[port] - first.get(port, 0) > 0}
    queues: dict[str, int] = {}
    for item in running:
        for port, size in item.get("udp_socket_rx_queue", {}).items():
            queues[port] = max(queues.get(port, 0), size)
    return {"udp_socket_drops": dict(sorted(drops.items(), key=lambda kv: -kv[1])),
            "udp_socket_rx_queue_max": {port: size for port, size in queues.items() if size > 0}}


def netdev_rates(running: list[dict]) -> dict:
    """Per-second packets and Mbit/s of each interface over the samples
    that have the counters: `eth0_tx_pps`, `eth0_tx_mbps`, `eth0_rx_pps`,
    ... (None where a node has no such interface)."""
    have = [item for item in running if "eth0_tx_packets" in item]
    if len(have) < 2:
        return {}
    first, last = have[0], have[-1]
    elapsed = last["at"] - first["at"]
    if elapsed <= 0:
        return {}
    out = {"netdev_seconds": round(elapsed, 2)}
    for device in NETDEV_INTERFACES:
        for direction in ("tx", "rx"):
            packets = f"{device}_{direction}_packets"
            octets = f"{device}_{direction}_bytes"
            if packets not in first or packets not in last:
                continue
            out[f"{device}_{direction}_pps"] = round((last[packets] - first[packets]) / elapsed, 1)
            out[f"{device}_{direction}_mbps"] = round(
                (last[octets] - first[octets]) * 8 / elapsed / 1e6, 3)
    return out


def read_netstat(name: str) -> dict:
    """UDP and TCP counters of the namespace of a node."""
    out = {}
    try:
        text = run_in(name, ["cat", "/proc/net/snmp", "/proc/net/sockstat"], check=False,
                      timeout=5)
    except (LabError, subprocess.TimeoutExpired):
        return out
    lines = text.splitlines()
    for header, values in zip(lines, lines[1:]):
        if header.startswith("Udp:") and values.startswith("Udp:") and "InDatagrams" in header:
            fields = dict(zip(header.split()[1:], values.split()[1:]))
            out["udp_in"] = int(fields["InDatagrams"])
            out["udp_out"] = int(fields["OutDatagrams"])
            out["udp_rcvbuf_errors"] = int(fields["RcvbufErrors"])
            out["udp_in_errors"] = int(fields["InErrors"])
        if header.startswith("Tcp:") and values.startswith("Tcp:") and "RetransSegs" in header:
            fields = dict(zip(header.split()[1:], values.split()[1:]))
            out["tcp_retrans"] = int(fields["RetransSegs"])
            out["tcp_out_segs"] = int(fields["OutSegs"])
            out["tcp_curr_estab"] = int(fields["CurrEstab"])
    for line in lines:
        if line.startswith("TCP: inuse"):
            fields = line.split()
            out["tcp_sockets"] = int(fields[2])
            out["tcp_mem_pages"] = int(fields[-1])
        elif line.startswith("UDP: inuse"):
            fields = line.split()
            out["udp_sockets"] = int(fields[2])
            out["udp_mem_pages"] = int(fields[-1])
    return out


class Sampler:
    """Samples every node once a second into an NDJSON file, and keeps a
    summary of each node: CPU, throttling, memory, descriptors, drops."""

    def __init__(self, path: pathlib.Path, interval: float = 1.0, app: bool = True):
        self.path = path
        self.interval = interval
        self.app = app
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.samples: dict[str, list[dict]] = {}

    def start(self) -> "Sampler":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        return self

    def stop(self) -> dict:
        self.stop_event.set()
        self.thread.join(timeout=15)
        return self.summary()

    def sample(self, tick: int) -> dict:
        now = time.time()
        out = {}
        for name, *_ in NODES:
            pid = service_pid(name)
            if pid is None:
                out[name] = {"at": now, "running": False}
                continue
            directory = cgroup_dir(name)
            item = {"at": now, "running": True, **(read_cgroup(directory) if directory else {}),
                    **read_process(pid), **read_netdev(pid), **read_udp_sockets(pid)}
            if tick % 5 == 0:
                item.update(read_netstat(name))
            out[name] = item
        return out

    def loop(self) -> None:
        tick = 0
        with open(self.path, "a") as handle:
            while not self.stop_event.is_set():
                started = time.time()
                try:
                    sample = self.sample(tick)
                except Exception as error:  # noqa: BLE001 - a sampler must not die
                    sample = {"error": {"at": started, "message": str(error)}}
                for name, item in sample.items():
                    self.samples.setdefault(name, []).append(item)
                handle.write(json.dumps(sample) + "\n")
                handle.flush()
                tick += 1
                self.stop_event.wait(max(0.05, self.interval - (time.time() - started)))

    def summary(self) -> dict:
        out = {}
        for name, samples in self.samples.items():
            running = [item for item in samples if item.get("running") and "usage_usec" in item]
            if len(running) < 2:
                out[name] = {"samples": len(samples), "running_samples": len(running)}
                continue
            cpu = []
            for before, after in zip(running, running[1:]):
                elapsed = after["at"] - before["at"]
                used = after["usage_usec"] - before["usage_usec"]
                if elapsed > 0 and used >= 0:
                    cpu.append(used / 1e4 / elapsed)
            cpu.sort()
            first, last = running[0], running[-1]

            def delta(key):
                values = [item[key] for item in running if key in item]
                return values[-1] - values[0] if len(values) >= 2 else None

            out[name] = {
                "samples": len(samples),
                "not_running_samples": len(samples) - len([s for s in samples if s.get("running")]),
                "cpu_percent_mean": round(sum(cpu) / len(cpu), 1) if cpu else None,
                "cpu_percent_p95": round(cpu[int(len(cpu) * 0.95) - 1], 1) if cpu else None,
                "cpu_percent_max": round(cpu[-1], 1) if cpu else None,
                "throttled_ms": round((last.get("throttled_usec", 0)
                                       - first.get("throttled_usec", 0)) / 1000),
                "memory_mb_first": round(first.get("memory_bytes", 0) / 1e6, 1),
                "memory_mb_last": round(last.get("memory_bytes", 0) / 1e6, 1),
                "memory_mb_max": round(max(item.get("memory_bytes", 0) for item in running) / 1e6, 1),
                "rss_mb_last": round(last.get("rss_bytes", 0) / 1e6, 1),
                "oom_kills": last.get("memory_oom_kill", 0),
                "fds_first": first.get("fds"), "fds_last": last.get("fds"),
                "fds_max": max(item.get("fds", 0) for item in running),
                "sockets_max": max(item.get("sockets", 0) for item in running),
                "threads_max": max(item.get("threads", 0) for item in running),
                "udp_rcvbuf_errors": delta("udp_rcvbuf_errors"),
                "tcp_retrans": delta("tcp_retrans"),
                "tcp_out_segs": delta("tcp_out_segs"),
                "udp_out": delta("udp_out"),
                "udp_in": delta("udp_in"),
                **netdev_rates(running),
                **socket_drop_deltas(running),
            }
        return out


# ------------------------------------------------------------------ CLI

def control(name: str, action: str) -> tuple[int, object]:
    """POST /api/control/<action>-node (`drain`, `resume`, `close`) to the
    node itself, naming it: av-mesh takes a request without a node as
    global. Returns the HTTP status (202: accepted and queued) and the
    answer."""
    return http_json(name, f"/api/control/{action}-node", "POST", {"node_id": name})


def node_status(name: str) -> dict:
    """What a running mesh node says of itself (GET /api/node): its build,
    state and drain reasons; {} from a node that does not answer it."""
    code, body = http_json(name, "/api/node", timeout=2.0)
    return body if code == 200 and isinstance(body, dict) else {}


# ------------------------------------------------------------------ the engine's backend
#
# C 2.12 and 2.13 (Unit L-E, CL1): the verbs roll, restart and replace run
# through the change engine (scripts/mesh_ops.py), as the deploy tool's do on
# Linode, with this lab as the backend: EdgeLabOps.

# Edges held out of service by a command (the autoscaler's edge_lab backend,
# linode-autoscale.py EdgeLabBackend.HELD, and EdgeLabOps): standby edges.
HELD_EDGES = "autoscale-held.json"
# {node: build set} that each node ran before its last activation: what a
# rollback puts back (the activation script's .args.prev).
PREV_BUILDS = "node-build-prev.json"
VERSION_BUILD = re.compile(r"\(([0-9a-f]{7,40}(?:-dirty)?) ([0-9]+)\)")
_BUILD_IDS: dict[tuple, dict | None] = {}


def build_id(label: str, build: str = "a") -> dict | None:
    """{"git", "unix", "dirty"} of a build set's binary, from its --version
    (C 2.2: `av-mesh 0.1.0 (3f2a9c1d77e0 1763900000)`); None for a binary
    that prints no build id (one from before the build id)."""
    path = binary(label, build)
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return None
    if key not in _BUILD_IDS:
        try:
            text = subprocess.run([path, "--version"], capture_output=True, text=True,
                                  timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
        match = VERSION_BUILD.search(text)
        _BUILD_IDS[key] = None if match is None else {
            "git": match.group(1), "unix": int(match.group(2)),
            "dirty": match.group(1).endswith("-dirty")}
    return _BUILD_IDS[key]


def relay_parents() -> dict[str, tuple[str | None, str | None]]:
    """{level-2 forwarder: (primary, secondary)}: its compiled parents in
    the relay DAG this lab was rendered with (the deploy tool's
    relay_tree)."""
    deploy = deploy_module()
    tree = deploy.relay_tree(lab_state(), load("settings.json"))
    return {child["node"]["name"]: (child["primary"]["name"], child["secondary"]["name"])
            for child in tree["children"]}


class EdgeLabOps(mesh_ops.Ops):
    """This lab as the change engine's backend (mesh_ops.Ops). A node is
    its service in its namespace; "staging" chooses the build set a node
    starts on (`build`: the set a roll installs, None: each node's own),
    the activation stops the service as the unit does (SIGTERM, 15 s), puts
    the set in force and starts it with what it was started with, and keeps
    the set it ran for a rollback (PREV_BUILDS, the script's .args.prev). A
    standby edge is a slot edge out of service or an edge held out
    (HELD_EDGES); a dist-c out of service is a slot not created: add_slot
    takes it in and starts it. `rebuild` is the instance's absence: the node
    stays down `gap_s` and loses its drain file (a new disk). The
    namespaces share this host's clock. The lock is the autoscaler's
    (WORK/ops.lock), so a roll and a controller of one lab exclude each
    other. `lab` is this module (a fake in the tests); `clock` and `sleep`
    the engine's."""

    def __init__(self, build: str | None = None, gap_s: float = 30.0, lab=None,
                 clock=time.time, sleep=time.sleep):
        self.lab = lab if lab is not None else sys.modules[__name__]
        self.build = build
        self.gap_s = gap_s
        self.clock = clock
        self.sleep = sleep
        self.lock_path = pathlib.Path(self.lab.WORK) / "ops.lock"
        self.evidence_dir = pathlib.Path(self.lab.WORK) / "rolls"
        self.staged: dict[str, str] = {}
        self._parents: dict | None = None

    # ---------------------------------------------------------- the lab
    def role(self, name: str) -> str:
        for item, role, *_ in self.lab.NODES:
            if item == name:
                return role
        raise mesh_ops.Refused(f"{name} is not a node of the lab")

    def label(self, name: str) -> str:
        return "av-contrib" if self.role(name) == "contributor" else "av-mesh"

    def held(self) -> set[str]:
        return set(self.lab.load(HELD_EDGES, []) or [])

    def set_held(self, name: str, held: bool) -> None:
        names = self.held()
        names = names | {name} if held else names - {name}
        self.lab.save(HELD_EDGES, sorted(names))

    def rendered(self, name: str, build: str | None = None) -> bool:
        try:
            self.lab.rendered_service(name, build or self.lab.node_build(name))
            return True
        except self.lab.LabError:
            return False

    def in_service(self, name: str) -> bool:
        slots = set(self.lab.slot_nodes())
        return (name not in slots or name in self.lab.slots_in_service()) \
            and name not in self.held()

    def services(self) -> dict:
        return self.lab.load("services.json")

    def prev_builds(self) -> dict:
        return dict(self.lab.load(PREV_BUILDS))

    def nodes(self) -> list[dict]:
        if self._parents is None:
            self._parents = self.lab.relay_parents()
        slots = set(self.lab.slot_nodes())
        made = self.lab.slots_in_service()
        out = []
        for name, role, site, _region, public, vlan in self.lab.NODES:
            if role not in mesh_ops.ROLE_TIER or not self.rendered(name):
                continue  # a backbone the topology does not use
            slot = name in slots
            taken = self.in_service(name)
            primary, secondary = self._parents.get(name, (None, None))
            out.append({"name": name, "role": role, "site": site, "vlan": vlan,
                        "public": public, "serving": taken, "slot": slot,
                        "standby": role == "edge" and not taken,
                        "created": role == "edge" or not slot or name in made,
                        "primary": primary, "secondary": secondary})
        return out

    # ---------------------------------------------------------- HTTP
    def status(self, node: str):
        code, body = self.lab.http_json(node, "/api/node", timeout=2.0)
        return body if code == 200 and isinstance(body, dict) else None

    def statuses(self, nodes) -> dict:
        nodes = list(nodes)
        if not nodes:
            return {}
        with ThreadPoolExecutor(max_workers=min(16, len(nodes))) as pool:
            return dict(zip(nodes, pool.map(self.status, nodes)))

    def get(self, node: str, path: str) -> tuple:
        code, body = self.lab.http_json(node, path, timeout=2.0)
        return (code or None), body

    def post(self, node: str, path: str, body: dict) -> int | None:
        code, _answer = self.lab.http_json(node, path, "POST", body)
        return code or None

    # ---------------------------------------------------------- builds
    def build_of(self, name: str, build: str) -> dict:
        found = self.lab.build_id(self.label(name), build)
        if found is None:
            raise mesh_ops.Refused(f"the {self.label(name)} of build set {build} prints no build id "
                                   "(--version): the engine needs a build with one (C 2.2)")
        return found

    def check(self, command: str, nodes: list[str]) -> None:
        if not (self.lab.load("namespaces.json") or {}).get("lab"):
            raise mesh_ops.Refused("the lab is not up")
        sets = self.lab.build_sets()
        if self.build is not None and self.build not in sets:
            raise mesh_ops.Refused(f"this lab has no build set {self.build} (it has "
                                   f"{', '.join(sets)}): up it with NEEDLETAIL_AV_MESH_BIN_NEXT")
        for name in nodes:
            build = self.build or self.lab.node_build(name)
            if not self.rendered(name, build):
                raise mesh_ops.Refused(f"{name} has no service in build set {build}")
            self.build_of(name, build)

    def target(self, tier: str) -> dict | None:
        """The build a roll installs: build set `build`'s; None without one,
        a rolling restart of each node on its own set (stage(None))."""
        if self.build is None:
            return None
        found = self.lab.build_id("av-contrib" if tier == "contributors" else "av-mesh",
                                  self.build)
        if found is None:
            raise mesh_ops.Refused(f"build set {self.build} prints no build id (--version)")
        return dict(found)

    def set_for(self, name: str, build: dict) -> str:
        """The build set whose binary for `name` is `build`."""
        for candidate in self.lab.build_sets():
            found = self.lab.build_id(self.label(name), candidate)
            if found and (found["git"], found["unix"]) == (build.get("git"), build.get("unix")):
                return candidate
        raise mesh_ops.Refused(f"no build set of this lab has {self.label(name)} "
                               f"{build.get('git')} {build.get('unix')}")

    def args_of(self, name: str, build: str) -> list[str]:
        """The argv the node's process gets on build set `build`: its
        render's, then the extra arguments it was started with."""
        extra = (self.services().get(name) or {}).get("extra_args") or []
        return list(self.lab.service_args(name, build)) + [str(arg) for arg in extra]

    def expected(self, node: str, rollback: bool = False) -> dict:
        if rollback:
            build = self.prev_builds().get(node)
            if build is None:
                raise mesh_ops.Refused(f"{node} has no previous build set to roll back to")
        else:
            build = self.staged.get(node) or self.lab.node_build(node)
        found = self.build_of(node, build)
        args = self.args_of(node, build)
        return {"git": found["git"], "unix": found["unix"],
                "args_sha256": hashlib.sha256("\n".join(args).encode()).hexdigest()}

    def stage(self, nodes: list[str], build: dict | None) -> None:
        for name in nodes:
            target = self.set_for(name, build) if build else self.lab.node_build(name)
            if not self.rendered(name, target):
                raise mesh_ops.Refused(f"{name} has no service in build set {target}")
            self.staged[name] = target

    # ---------------------------------------------------------- the service
    def restart(self, name: str, build: str) -> None:
        """Stop as the unit does, put `build` in force, start with what the
        node was started with (limits, environment, extra arguments)."""
        given = self.services().get(name) or {}
        self.lab.terminate_service(name)
        self.lab.set_node_build(name, build)
        self.lab.start_service(name, limits=given.get("limits"), env=given.get("env"),
                               extra_args=given.get("extra_args"))

    def activate(self, node: str, not_after_ms: int, rollback: bool = False) -> dict:
        if self.clock() * 1000 > not_after_ms:
            return {"ok": False, "why": "late"}
        current = self.lab.node_build(node)
        prev = self.prev_builds()
        if rollback:
            target = prev.get(node)
            if target is None:
                return {"ok": False, "why": "no previous build set to put back"}
        else:
            target = self.staged.get(node, current)
            prev[node] = current
            self.lab.save(PREV_BUILDS, prev)
        try:
            self.restart(node, target)
        except self.lab.LabError as error:
            return {"ok": False, "why": str(error)}
        return {"ok": True, "why": ""}

    def boot_changed(self, node: str, since_ms: int) -> bool | None:
        started = (self.services().get(node) or {}).get("started_unix")
        return None if started is None else started * 1000 > since_ms

    def unit_restarts(self, node: str) -> int | None:
        """The lab's own count: starts after a crash (`crash`), as systemd's
        NRestarts counts the unit's restarts after a failure."""
        return int((self.services().get(node) or {}).get("crashes", 0))

    def clock_offset(self, node: str) -> float | None:
        return 0.0

    def surge_candidates(self, site: str) -> list[str]:
        slots = set(self.lab.slot_nodes())
        return sorted((name for name, role, where, *_ in self.lab.NODES
                       if role == "edge" and where == site and not self.in_service(name)
                       and self.rendered(name)),
                      key=lambda name: (name in slots, mesh_ops.natural(name)))

    def start_node(self, node: str) -> None:
        if not self.rendered(node):
            raise mesh_ops.Refused(f"{node} has no service in this lab")
        slot = node in self.lab.slot_nodes()
        target = self.staged.get(node)
        try:
            if slot:
                self.lab.set_slot_in_service(node, True)
            current = self.lab.node_build(node)
            if target is not None and target != current:
                prev = self.prev_builds()
                prev[node] = current
                self.lab.save(PREV_BUILDS, prev)
            if self.lab.service_pid(node) is not None:
                if target is not None and target != current:
                    self.restart(node, target)
            else:
                given = self.services().get(node) or {}
                if target is not None:
                    self.lab.set_node_build(node, target)
                self.lab.start_service(node, limits=given.get("limits"), env=given.get("env"),
                                       extra_args=given.get("extra_args"))
        except self.lab.LabError as error:
            if slot and self.lab.service_pid(node) is None:
                self.lab.set_slot_in_service(node, False)
            raise mesh_ops.Refused(f"{node}: {error}") from None
        self.set_held(node, False)

    def stop_node(self, node: str, not_after_ms: int | None = None) -> None:
        if not_after_ms is not None:
            if self.clock() * 1000 > not_after_ms:
                raise mesh_ops.Refused(f"{node}: the stop came after its deadline")
            self.lab.terminate_service(node)
            return
        self.lab.terminate_service(node)
        if node in self.lab.slot_nodes():
            self.lab.set_slot_in_service(node, False)
        elif self.role(node) == "edge":
            self.set_held(node, True)

    # ---------------------------------------------------------- replace
    def rebuild(self, node: str, mode: str) -> None:
        if mode != "rebuild":
            raise mesh_ops.Refused("the local lab has no instance to make anew; replace "
                                   "rebuilds in place")
        self.sleep(self.gap_s)
        (pathlib.Path(self.lab.WORK) / "state" / f"{node}.drained").unlink(missing_ok=True)

    def push(self, node: str) -> None:
        if self.lab.service_pid(node) is None:
            given = self.services().get(node) or {}
            self.lab.start_service(node, limits=given.get("limits"), env=given.get("env"),
                                   extra_args=given.get("extra_args"))
        if node in self.lab.slot_nodes():
            self.lab.set_slot_in_service(node, True)
        self.set_held(node, False)
        self.staged.pop(node, None)

    def add_slot(self, node: str) -> None:
        if node not in self.lab.slot_nodes():
            raise mesh_ops.Refused(f"{node} is not a slot of this lab")
        self.lab.set_slot_in_service(node, True)
        try:
            self.lab.start_service(node)
        except self.lab.LabError as error:
            if self.lab.service_pid(node) is None:
                self.lab.set_slot_in_service(node, False)
                raise mesh_ops.Refused(f"{node}: {error}") from None
            raise

    def remove_slot(self, node: str) -> None:
        self.lab.terminate_service(node)
        if node in self.lab.SLOT_CANDIDATES:
            self.lab.set_slot_in_service(node, False)

    # ---------------------------------------------------------- workstream A's changes
    def hold_down(self, node: str) -> None:
        """Nothing to hold: the lab starts no service again by itself."""

    def kill(self, node: str) -> None:
        self.lab.kill_service(node)

    def release(self, node: str) -> None:
        """Start the node with what it was started with, unless it runs."""
        if self.lab.service_pid(node) is None:
            given = self.services().get(node) or {}
            self.lab.start_service(node, limits=given.get("limits"), env=given.get("env"),
                                   extra_args=given.get("extra_args"))

    def freeze(self, node: str, frozen: bool) -> None:
        self.lab.freeze_service(node, frozen)

    def staged_identity(self, node: str) -> dict | None:
        """`--print-fingerprint` of the build set staged for contributor
        `node`, with the arguments it gets on that set; None when nothing
        is staged."""
        build = self.staged.get(node)
        if build is None:
            return None
        return contributor_changes.identity_of(self.lab.binary("av-contrib", build),
                                               self.args_of(node, build))


ENGINE_VERBS = ("roll", "restart", "replace")


def engine_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"edge_lab.py {command}")
    if command == "roll":
        parser.add_argument("tier", choices=("edges", "forwarders", "contributors", "all"))
        parser.add_argument("--build", default=None,
                            help="the build set to install; without it, every node in scope "
                                 "is restarted on its own set (a rolling restart)")
        parser.add_argument("--nodes", default=None)
        parser.add_argument("--sites", default=None)
        parser.add_argument("--force", action="store_true")
    else:
        parser.add_argument("node")
    if command == "restart":
        parser.add_argument("--hold-s", type=float, default=0.0)
        parser.add_argument("--rollback", action="store_true")
    if command == "replace":
        parser.add_argument("--gap-s", type=float, default=30.0)
        parser.add_argument("--no-slot", action="store_true")
    else:
        parser.add_argument("--dry-marks", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--marks", default=None)
    parser.add_argument("--node-marks", default=None)
    parser.add_argument("--gate-timeout-s", type=float, default=30.0)
    parser.add_argument("--drain-timeout-s", type=float, default=30.0)
    parser.add_argument("--ready-timeout-s", type=float, default=60.0)
    parser.add_argument("--on-failure", choices=("stop", "rollback-all"), default="stop")
    parser.add_argument("--surge", choices=("auto", "never"), default="auto")
    parser.add_argument("--parity-ms", type=float, default=1.0)
    return parser


def names_of(text: str | None) -> list[str] | None:
    return [item for item in (text or "").split(",") if item] or None


def engine_verb(command: str, arguments: list[str], ops: EdgeLabOps | None = None,
                **engine) -> int:
    """`roll`, `restart` and `replace` through the change engine on this
    lab (C 2.12). stdout carries the change's two marks only. Returns the
    engine's exit status."""
    args = engine_parser(command).parse_args(arguments)
    options = mesh_ops.Options(
        # Without --build a roll restarts every node in scope on its own
        # set: each is on its target already, so it is forced.
        force=getattr(args, "force", False) or (command == "roll" and args.build is None),
        dry=getattr(args, "dry_marks", False), gate_timeout_s=args.gate_timeout_s,
        drain_timeout_s=args.drain_timeout_s, ready_timeout_s=args.ready_timeout_s,
        on_failure=args.on_failure, surge=args.surge, parity_ms=args.parity_ms,
        hold_s=getattr(args, "hold_s", 0.0), rollback=getattr(args, "rollback", False),
        marks_file=args.marks, node_marks_file=args.node_marks)
    if ops is None:
        ops = EdgeLabOps(build=getattr(args, "build", None), gap_s=getattr(args, "gap_s", 30.0))
    if command == "roll":
        return mesh_ops.roll(ops, args.tier, nodes=names_of(args.nodes),
                             sites=names_of(args.sites), options=options, resume=args.resume,
                             **engine)
    if command == "restart":
        return mesh_ops.restart_nodes(ops, [args.node], options=options, resume=args.resume,
                                      **engine)
    return mesh_ops.replace(ops, args.node, mode="rebuild", no_slot=args.no_slot,
                            resume=args.resume, options=options, **engine)


def change_verb(command: str, arguments: list[str], ops: EdgeLabOps | None = None,
                **change) -> int:
    """`kill`, `return` and `freeze` on this lab (workstream A, A 2.10):
    contributor_changes on an EdgeLabOps backend. stdout carries the
    change's two marks only. Returns the change's exit status."""
    parser = argparse.ArgumentParser(prog=f"edge_lab.py {command}")
    contributor_changes.add_options(parser, command, positional=True)
    args = parser.parse_args(arguments)
    return contributor_changes.run(ops or EdgeLabOps(), command, args, **change)


def status() -> None:
    work = str(WORK.resolve())
    for entry in registered_labs():
        print(f"lab {entry['id']:<10} owner {entry['owner']:<10} "
              f"CPUs {cpu_ranges(entry['cpus']):<8} {entry['work']}"
              + (" (this lab)" if entry["work"] == work else ""))
    held = held_slots()
    services = load("services.json")
    for name, role, site, *_ in NODES:
        pid = service_pid(name)
        if pid is None:
            unused = role == "backbone" and name not in load("rendered.json")
            print(f"{name:16} {role:12} " + ("not used by the topology" if unused
                                             else "slot, not in service" if name in held
                                             else "not running"))
            continue
        directory = cgroup_dir(name)
        stats = read_cgroup(directory) if directory else {}
        line = (f"{name:16} {role:12} pid {pid:<8} "
                f"set {services.get(name, {}).get('build', 'a')} "
                f"mem {stats.get('memory_bytes', 0) / 1e6:6.0f} MB "
                f"throttled {stats.get('throttled_usec', 0) / 1e6:6.1f} s")
        if role in ("edge", "distributor", "contributor"):
            # One parser for both shapes of the build block (av-mesh's and
            # av-contrib's, mesh_ops.status_of).
            own = mesh_ops.status_of(node_status(name) or None)
            if own:
                reasons = ",".join(own.drain_reasons or []) or "none"
                line += f" build {own.build_git} state {own.state} drain {reasons}"
                if own.not_ready:
                    line += f" not ready: {'; '.join(own.not_ready)}"
        if role in ("edge", "distributor"):
            try:
                view = edge_view(name)
                line += (f" holds {sorted(view['playlists'])} sessions {view['sessions']} "
                         f"load {view['load_permille']}")
            except LabError as error:
                line += f" ({error})"
        print(line)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command, arguments = sys.argv[1], sys.argv[2:]
    try:
        if command == "up":
            rendered = up(arguments)
            print(f"started {len(rendered)} services, each in its own namespace and cgroup")
            print(f"lab {lab_key()} of {lab_owner()}, "
                  f"CPUs {cpu_ranges(lab_cpus(load('cpus.json')))}, "
                  f"build sets {', '.join(build_sets())}, state in {WORK}")
        elif command == "down":
            down()
        elif command == "status":
            status()
        elif command == "sh":
            target = None if arguments[0] == "lab" else arguments[0]
            sys.exit(subprocess.run(enter(target) + arguments[1:]).returncode)
        elif command == "net":
            name, network, direction = arguments[:3]
            set_net(name, network, direction, " ".join(arguments[3:]) or None)
        elif command == "wan":
            set_wan(arguments[0], arguments[1], " ".join(arguments[2:]) or None)
        elif command == "sources":
            start_sources(int(arguments[0]) if arguments else 600)
        elif command == "start":
            name = arguments[0]
            if name in SLOT_CANDIDATES:
                set_slot_in_service(name, True)
            pid = service_pid(name) or start_service(name)
            print(f"{name} runs (pid {pid}); no other service was restarted")
        elif command == "stop":
            name = arguments[0]
            terminate_service(name)
            if name in slot_nodes():
                set_slot_in_service(name, False)
            print(f"{name} stopped")
        elif command in ("drain", "resume"):
            name = arguments[0]
            # Not `status`: that name is the status command's function, and
            # binding it here made `edge_lab.py status` fail (UnboundLocalError).
            code, body = control(name, command)
            print(f"{command} {name}: HTTP {code} {json.dumps(body)[:200]}")
            # av-mesh answers a control command with 202 Accepted (queued).
            if not 200 <= code < 300:
                sys.exit(1)
        elif command == "crash":
            name = arguments[0]
            restart_s = 1.0
            if arguments[1:]:
                if len(arguments) != 3 or arguments[1] != "--restart-s":
                    sys.exit("usage: edge_lab.py crash <node> [--restart-s SECONDS]")
                restart_s = float(arguments[2])
            pid = crash_service(name, restart_s)
            print(f"{name} was killed and is back (pid {pid})")
        elif command in ENGINE_VERBS:
            sys.exit(engine_verb(command, arguments))
        elif command in contributor_changes.VERBS:
            sys.exit(change_verb(command, arguments))
        elif command == "burn":
            if len(arguments) != 2:
                sys.exit("usage: edge_lab.py burn <node> <seconds>")
            burned = burn(arguments[0], float(arguments[1]))
            print(f"{arguments[0]} burned for {burned:.1f} s"
                  + ("" if burned >= float(arguments[1]) else ": its service stopped"))
        elif command == "rt-check":
            checks = [rt_check(name) for name in (arguments or standing_services())]
            setting = load("rt.json")
            print(f"rt={'1' if setting.get('rt') else 'pin' if setting.get('layout') else '0'}")
            for item in checks:
                verdict = {True: "ok", False: "NOT", None: "-"}[item["ok"]]
                realtime = ", ".join(f"{thread['comm']}/{thread['tid']} {thread['cls']} "
                                     f"{thread['rtprio']} cpus {thread['allowed']}"
                                     for thread in item.get("realtime", []))
                print(f"{item['node']:<14} {verdict:<3} cpus {cpu_ranges(item['node_cpus'] or [])}"
                      f" class {item['class_cpus'] or '-'} rtprio-limit "
                      f"{item.get('rtprio_limit')} fast-plane {item.get('fast_plane', {})}: "
                      f"{item['why']}" + (f"; real-time threads: {realtime}" if realtime else ""))
            if any(item["ok"] is False for item in checks):
                sys.exit(1)
        else:
            sys.exit(__doc__)
    except LabError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
