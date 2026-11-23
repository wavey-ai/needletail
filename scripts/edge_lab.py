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
  edge_lab.py status                    one line for each node
  edge_lab.py sh <node> <command...>    run a command in the namespace of a node
  edge_lab.py net <node> <pub|vlan> <out|in|both> <netem spec | clear>
  edge_lab.py wan <site> <out|in|both> <netem spec | clear>
  edge_lab.py sources <seconds>         start the 4K video and the audio sources
  edge_lab.py start <node>              start a node's service (a slot: take it into
                                        service)
  edge_lab.py stop <node>               stop a node's service (SIGTERM; a slot: out of
                                        service again)
  edge_lab.py drain <node>              POST /api/control/drain-node for that node
  edge_lab.py resume <node>             POST /api/control/resume-node for that node

Slot nodes (low-latency ops item 7): nodes that `up` renders, with every
peer list naming them, but does not start, as the large mesh's autoscale
slots: lon-dist-c always (a third London forwarder, a fast-plane lease child
of lon-dist-a and lon-dist-b and a cache-mesh parent of the edges, never a
compiled relay child), and lon-edge-4 with `up slots=lon-edge-4`. A slot is
in service once `start` (or the autoscaler's edge_lab backend) takes it in;
until then edges() and distributors() leave it out, and restore() does not
start it. Starting one restarts no other service: their arguments already
name it.

The fault campaign (`edge-campaign.py`) drives this module.

Sibling checkouts are found under the workspace root. Override them with
NEEDLETAIL_AV_MESH_ROOT, NEEDLETAIL_AV_CONTRIB_ROOT, NEEDLETAIL_PROBE_BIN and
NEEDLETAIL_EDGE_LAB_WORK (the state directory, for a second lab on one host).
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent
WORK = pathlib.Path(os.environ.get(
    "NEEDLETAIL_EDGE_LAB_WORK", str(ROOT / "target" / "edge-lab")))
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
]
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


def node(name: str) -> dict:
    for item, role, site, region, public, vlan in NODES:
        if item == name:
            return {"name": item, "role": role, "site": site, "region": region,
                    "public_ipv4": public, "vlan_ipv4": vlan}
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
# names more: lon-dist-c is new and has no service unless taken in.
SLOT_CANDIDATES = ("lon-edge-4", "lon-dist-c")
DEFAULT_SLOTS = ("lon-dist-c",)


def slot_nodes() -> list[str]:
    """The slots of this lab (`up slots=`), lon-dist-c always."""
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


def enter(name: str | None) -> list[str]:
    """Command prefix that runs a command in the namespace of a node, or in
    the lab namespace (which holds the bridges) for `None`."""
    pid = lab_pid() if name is None else ns_pid(name)
    return ["nsenter", "--preserve-credentials", "-U", "-n", "-t", str(pid)]


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
    distributor slot is a lease child, never a compiled relay child."""
    return {"run_id": "edge-lab", "hourly_usd": 0.0,
            "nodes": {name: {**node(name), "type": "local"} for name, *_ in NODES},
            "slots": {name: {**node(name), "type": "local", "slot": True}
                      for name in SLOT_CANDIDATES}}


def snapshot_binaries() -> dict:
    """Copy the binaries into the state directory and run them from there,
    so that a build during a long run does not change the lab under it."""
    import shutil
    directory = WORK / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for label, source in (("av-mesh", AV_MESH), ("needletail-reader", READER),
                          ("av-contrib", AV_CONTRIB), ("aep1-48k-probe", PROBE),
                          ("needletail-load", LOAD_BIN)):
        if label == "needletail-load" and not source.is_file():
            continue
        target = directory / label
        temporary = directory / (label + ".new")
        shutil.copy2(source, temporary)
        temporary.replace(target)
        out[label] = str(target)
    save("binaries.json", out)
    return out


def binary(label: str) -> str:
    """Path of a binary of the running lab."""
    paths = load("binaries.json")
    if label in paths:
        return paths[label]
    return str({"av-mesh": AV_MESH, "needletail-reader": READER, "av-contrib": AV_CONTRIB,
                "aep1-48k-probe": PROBE, "needletail-load": LOAD_BIN}[label])


def render(overrides: list[str]) -> dict:
    """Service arguments of every node, as the Linode deploy tool renders
    them. Only the paths of the binaries and of the TLS files differ."""
    deploy = deploy_module()
    deploy.ensure_tls()
    (WORK / "artifacts" / "settings.json").unlink(missing_ok=True)
    merged = [f"{key}={json.dumps(value)}" for key, value in DEFAULT_OVERRIDES.items()]
    settings = deploy.load_settings(merged + overrides)
    lon_ny_us = int(2000 * (SITE_DELAY_MS["london"] + SITE_DELAY_MS["newyork"]))
    routes = {"best_direct_rtt_us": lon_ny_us, "primary_rtt_us": lon_ny_us,
              "primary_jitter_us": 1000, "secondary_rtt_us": lon_ny_us + 6000,
              "secondary_jitter_us": 1000}
    state = lab_state()
    plan = deploy.compile_plan(deploy.build_program(state, settings, routes))
    # The render looks at the flags of the binaries that the lab runs.
    deploy.RENDER_BINARIES.clear()
    deploy.RENDER_BINARIES.update({"av-mesh": AV_MESH, "av-contrib": AV_CONTRIB})
    rendered = deploy.render(state, settings, plan)
    save("sysctls.json", deploy.node_sysctls(settings))
    save("settings.json", settings)
    tls = WORK / "artifacts" / "tls"
    out = {}
    for name, item in rendered.items():
        args = list(item["args"])
        contributor = item["service"] == "needletail-contrib"
        args[0] = binary("av-contrib" if contributor else "av-mesh")
        fixed = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("--cert", "--key"):
                fixed += [arg, str(tls / pathlib.Path(args[index + 1]).name)]
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
        out[name] = {"args": fixed, "rust_log": item["rust_log"]}
    save("rendered.json", out)
    return out


def unit_name(name: str) -> str:
    return f"needletail-lab-{WORK.name}-{name}.scope"


def start_service(name: str, limits: dict | None = None, env: dict | None = None,
                  extra_args: list[str] | None = None) -> int:
    """Start the service of a node in its namespace and in its own cgroup."""
    item = load("rendered.json").get(name)
    if item is None:
        raise LabError(f"{name} has no service in this lab's topology"
                       + (" (merged: no backbone relay; up topology=legacy)"
                          if node(name)["role"] == "backbone" else ""))
    limits = {**default_limits(name), **(limits or load("limits.json").get(name, {}))}
    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    log = open(WORK / "logs" / f"{name}.log", "ab")
    subprocess.run(["systemctl", "--user", "reset-failed", unit_name(name)],
                   capture_output=True)
    command = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
               "--unit", unit_name(name),
               "-p", f"CPUQuota={limits['cpu_percent']}%",
               "-p", f"MemoryMax={limits['memory_mb']}M",
               "-p", "MemorySwapMax=0",
               "--", *enter(name), *item["args"], *(extra_args or [])]
    # A service on a Nanode sees one CPU and starts one worker thread. Here it
    # would see every CPU of the host, so it is told the number that its
    # limit stands for.
    threads = load("settings.json").get("tokio_worker_threads") or max(
        1, limits["cpu_percent"] // 100)
    shim = ({"LD_PRELOAD": str(clock_shim()),
             "NEEDLETAIL_CLOCK_OFFSET_FILE": str(WORK / "clock" / name)}
            if clock_shim().is_file() else {})
    process = subprocess.Popen(
        command, env={**os.environ, **shim, "RUST_LOG": item["rust_log"],
                      "TOKIO_WORKER_THREADS": str(threads),
                      **{key: str(value) for key, value in
                         (load("settings.json").get("node_env") or {}).items()},
                      **(env or {})},
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    services = load("services.json")
    services[name] = {"pid": process.pid, "started_unix": time.time(),
                      "starts": services.get(name, {}).get("starts", 0) + 1}
    save("services.json", services)
    return process.pid


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
    if pid is None:
        return None
    try:
        path = pathlib.Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
    except (OSError, IndexError):
        return None
    return pathlib.Path("/sys/fs/cgroup" + path)


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


def terminate_service(name: str, wait: float = 10.0) -> None:
    """Ask a service to stop, as `systemctl stop` does."""
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


def up(overrides: list[str], start: bool = True) -> dict:
    for binary in (AV_MESH, READER, AV_CONTRIB, PROBE):
        if not binary.is_file():
            raise LabError(f"binary is missing: {binary}")
    down(quiet=True)
    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    for log in (WORK / "logs").glob("*"):
        if log.is_file():
            log.unlink()
    limits = {}
    plain = []
    slots = set(DEFAULT_SLOTS)
    for item in overrides:
        key, _, value = item.partition("=")
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
        else:
            plain.append(item)
    save("limits.json", limits)
    save("slots.json", sorted(slots))
    save("slots-created.json", [])
    build_networks()
    snapshot_binaries()
    build_clock_shim()
    rendered = render(plain)
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
    return rendered


def down(quiet: bool = False) -> None:
    for name, item in list(load("services.json").items()) + list(load("sources.json").items()):
        pid = item["pid"] if isinstance(item, dict) else item
        for target in (pid,):
            try:
                os.killpg(target, signal.SIGKILL)
            except OSError:
                pass
    for _name, pid in load("namespaces.json").items():
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    for path in ("services.json", "namespaces.json", "sources.json", "links.json",
                 "shaped.json", "qdisc.json"):
        (WORK / path).unlink(missing_ok=True)
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


def start_sources(seconds: int, video: bool = True, audio: str | None = "probe",
                  clip: pathlib.Path | None = None) -> None:
    """Start the sources next to the contributor, as an encoder on its LAN.
    `audio` is `probe` (lossless FLAC), `daw` (FLAC and HQ SoundKit Opus) or
    None."""
    sources = load("sources.json")
    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    if video and "video" not in sources and load("settings.json").get("hls_renditions"):
        # An encoder that sends a ladder: one process reads every rendition
        # at its own rate and publishes each under the key of its stream.
        log = open(WORK / "logs" / "source-video.log", "ab")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        for _stream, path, *_ in LADDER:
            command += ["-re", "-stream_loop", "-1", "-i", str(path)]
        for index, (stream, *_rest) in enumerate(LADDER):
            command += ["-map", f"{index}:v:0", "-map", f"{index}:a:0", "-c", "copy",
                        "-t", str(seconds), "-f", "flv",
                        f"rtmp://127.0.0.1:{RTMP_PORT}/live/{stream}"]
        process = subprocess.Popen(enter("contrib-lon") + command, stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        sources["video"] = process.pid
    if video and "video" not in sources:
        log = open(WORK / "logs" / "source-video.log", "ab")
        process = subprocess.Popen(
            enter("contrib-lon") + [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
                # A 4K clip of 120 Mbit/s or more holds its first audio
                # packet past the default 5 MB probe: without a longer one
                # ffmpeg finds no sample rate and sends nothing.
                "-probesize", "64M", "-analyzeduration", "10000000",
                "-stream_loop", "-1", "-i", str(clip or VIDEO_CLIP), "-t", str(seconds),
                "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-muxdelay", "0",
                "-muxpreload", "0", "-f", "mpegts",
                "srt://127.0.0.1:27001?mode=caller&transtype=live&latency=80"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["video"] = process.pid
    if audio == "daw" and "audio" not in sources:
        log = open(WORK / "logs" / "source-audio.log", "ab")
        process = subprocess.Popen(
            enter("contrib-lon") + [
                str(DAW_SOURCE), "--direct-contributor", "--loop-prepared-pcm",
                # The DAW lane's Opus frame and rate: 1 ms Opus Custom at
                # 320 kb/s by default.
                "--opus-frame-samples", str(probe_epoch_samples("opus")),
                "--opus-bitrate", str(opus_bitrate()),
                # The lab's lossless lane is FLAC (the contributor's
                # daw_hls_formats "flac,opus"); daw-nexus now sends PCM
                # unless told, and the contributor then made no stream 2001.
                "--lossless-payload", "flac",
                "127.0.0.1:27100", str(seconds), str(daw_tracks())],
            env={**os.environ, "RUST_LOG": "info",
                 "DAW_TEST_SOURCE_START_UNIX_NS": str(time.time_ns() + 3_000_000_000),
                 "DAW_TEST_SOURCE_CAPACITY_OUTPUT": str(WORK / "logs" / "daw-capacity.ndjson")},
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["audio"] = process.pid
    elif audio == "probe" and "audio" not in sources:
        log = open(WORK / "logs" / "source-audio.log", "ab")
        process = subprocess.Popen(
            enter("contrib-lon") + [
                binary("aep1-48k-probe"), "send", "--target", "127.0.0.1:27100",
                "--session-id", str(time.time_ns() + 2_000_000_000), "--group-id", "0",
                "--duration-seconds", str(seconds), "--payload", "flac",
                "--epoch-samples", str(probe_epoch_samples("flac")),
                "--channels", "2", "--group-channels", "8",
                "--pcm-s24le-file", str(AUDIO_PCM),
                "--repair-percent", "12", "--min-repair-symbols", "1"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["audio"] = process.pid
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

def status() -> None:
    held = held_slots()
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
                f"mem {stats.get('memory_bytes', 0) / 1e6:6.0f} MB "
                f"throttled {stats.get('throttled_usec', 0) / 1e6:6.1f} s")
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
            # Always naming the node: av-mesh takes a request without one
            # as global.
            # Not `status`: that name is the status command's function, and
            # binding it here made `edge_lab.py status` fail (UnboundLocalError).
            code, body = http_json(name, f"/api/control/{command}-node", "POST",
                                   {"node_id": name})
            print(f"{command} {name}: HTTP {code} {json.dumps(body)[:200]}")
            # av-mesh answers a control command with 202 Accepted (queued).
            if not 200 <= code < 300:
                sys.exit(1)
        else:
            sys.exit(__doc__)
    except LabError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
