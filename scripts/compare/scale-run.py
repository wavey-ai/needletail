#!/usr/bin/env python3
"""Ramp simulated audio listeners over the large deployed mesh and say which
tier gave out first at each step.

The lab is deploy/low-latency/topology-scale.json (deploy/low-latency/
SCALE.md has the plan and the cost), deployed by
scripts/linode-small-mesh-deploy.py with the settings that `settings`
prints. One audio stream at a time goes from the AEP1 sender on lon-src
through the contributor:

  pcm-1      1 ms 24-bit stereo PCM (48 samples), stream 4001
  opus-1     1 ms Opus Custom (48 samples, 320 kbit/s), stream 3001
  opus-2.5   standard 2.5 ms Opus (120 samples), stream 3001

Every step:

- splits the step's listeners over the regions in proportion to their
  edges (standby edges left out), and in a region over its listener hosts
  (*-load-N) in proportion to their vCPUs; half of them (--hls-share) are
  LL-HLS readers (ntcmp hls --count, requests in flight), pinned evenly to
  the region's edges (`cache=`) or left to its steering (--hls-steering),
  the other half UDP subscriptions (needletail-load: real FEC/compact
  decode, NACKs and loss accounting, sampled, not logged) spread over the
  region's edges. Every listener reads over the region's VLAN: Linode does
  not bill it, and public egress at this scale would be terabytes an hour;
- samples every node of every tier once a second (CPU of the host and of
  its busiest core, steal, bytes and packets in and out per interface and
  which one is public, UDP buffer drops, the CPU of each service);
- watches the listener hosts while the step runs and stops the step when
  one saturates (CPU or UDP receive-buffer drops held for 3 s, or
  needletail-load's own verdict), so that a load host that gave out is
  never reported as a mesh that did;
- collects the load tool's interval and summary files, the readers'
  reports and the edges' client UDP counters, and writes per region the p50
  and p99 of each lane, the units missing, the time to first part, the
  listeners of each edge, the CPU and bytes out of every tier, and which
  tier gave out first (edge, forwarder, contributor or load host) and when.

The ramp ends at the first step where something gave out (--keep-going
runs on). Results: target/compare/scale-NAME/ (plan.json, events.jsonl,
steps/STEP/HOST/..., scale.json, summary.md).

  scale-run.py settings [--second-curve] [--edges-per-region N]
                                           the deploy --set arguments
  scale-run.py plan [--topology-only] [--steps ...] [--cost FILE]
                                           allocation, capacity, duration and
                                           cost of each step
  scale-run.py prepare --audio-file PATH   the loop and the readers' indexes
  scale-run.py run --config pcm-1 [--name NAME] [--steps 100,1000,5000,10000]
  scale-run.py analyse NAME|DIR            aggregate a run's files again
  scale-run.py stop                        stop every source, listener, sampler

The UDP listeners are av-mesh's needletail-load (branch scale-load):
--edges --stream --listeners --ramp --duration --sample --out DIR, which
writes DIR/intervals.jsonl (needletail.load.interval.v1: a line a second,
"unix_ms", "total", "edges" {edge: {...}}, "load_host",
"load_host_saturated"),
DIR/summary.json (needletail.load.summary.v1: "total", "per_edge", "host",
latency and time-to-first-part histograms whose buckets merge across
hosts) and DIR/arrivals/listener-<i>.csv for the --sample listeners. The
readers below also take the field names of its earlier drafts.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import shlex
import ssl
import sys
import threading
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import small_mesh_common as common  # noqa: E402

ROOT = common.ROOT
OUT = ROOT / "target" / "compare"
REMOTE = "/var/lib/needletail-runs/scale"
REMOTE_MEDIA = "/var/lib/compare"
CA = "/usr/local/share/ca-certificates/needletail-qualification.crt"
SERVER_NAME = common.TLS_SERVER_NAME
HTTP_PORT = 19444
CLIENT_UDP_PORT = 19446
CONTRIB_INGEST_PORT = 27100
SAMPLE_RATE = 48_000
TOPOLOGY = ROOT / "deploy" / "low-latency" / "topology-scale.json"
TIERS = {"contributor": "contributor", "distributor": "forwarder", "edge": "edge",
         "livekit": "livekit"}

# The three audio lanes (owner, 22 Nov: "lets stick to audio for now"). The
# AEP1 sender on lon-src feeds the contributor; part N is epoch N of the
# 64 s loop. `index`: the ntcmp audio index of the LL-HLS readers, as
# scripts/compare/compare_lab.py makes them (the loop's fingerprint in
# blocks of one epoch for PCM; the part number for Opus, offset 0 as the
# referee's nt-popus-120s). `udp_mbps`: one UDP listener on the wire before
# wave 7 (the 684-byte envelope, 1,000 datagrams a second at 1 ms);
# `hls_mbps`: an LL-HLS reader with its request and HTTP/3 framing per part.
# Planning figures: the run measures them.
CONFIGS = {
    "pcm-1": {"payload": "pcm", "epoch_samples": 48, "stream": 4001,
              "index": ("nt-pcm-48s", ["@audio", "1.0"]), "udp_mbps": 5.6, "hls_mbps": 4.0},
    "opus-1": {"payload": "opus", "epoch_samples": 48, "stream": 3001,
               "index": ("nt-popus-48s", ["numbered:nt-popus-48s", "1.0", "0"]),
               "udp_mbps": 1.2, "hls_mbps": 2.0},
    "opus-2.5": {"payload": "opus", "epoch_samples": 120, "stream": 3001,
                 "index": ("nt-popus-120s", ["numbered:nt-popus-120s", "2.5", "0"]),
                 "udp_mbps": 0.6, "hls_mbps": 1.0},
}
OPUS_BITRATE = 320_000
DEFAULT_STEPS = "100,1000,5000,10000,20000"
# What one listener costs a listener host, in vCPUs, for a stream of 1,000
# parts a second (scaled by the stream's parts a second; --cost FILE with
# {"udp_vcpu": ..., "hls_vcpu": ...} replaces them):
# - UDP: measured by needletail-load's in-process bench (part L, 22 Nov, AMD
#   EPYC 7713, 1 ms compact PCM, nothing missing): 3.96 µs of worker CPU a
#   datagram, kernel included, plus about 1.5 µs of NIC receive softirq
#   that loopback does not charge: 5.5 µs, 0.0055 vCPU per listener (about
#   600 per g6-dedicated-4 at 80%). The cost is per datagram: one socket per
#   listener, so 5,000 per g6-dedicated-4 at 1 ms is out of reach;
# - LL-HLS: not measured yet. An ntcmp reader of 1 ms parts with 40
#   requests in flight asks for 1,000 parts a second, and Go's HTTP client
#   spends about a vCPU on 25,000-40,000 requests a second (0.03 vCPU each).
COSTS = {"udp_vcpu": 0.0055, "hls_vcpu": 0.03}
# The share of a listener host that the plan fills at most.
PLAN_HEADROOM = 0.70
# Inbound that a listener host is planned to take, Gbit/s (Linode publishes
# the outbound cap, network_out: 4/5/6 Gbit/s for g6-dedicated-2/4/8; the
# inbound one is an assumption the first steps check).
LOAD_HOST_INBOUND_GBPS = 10.0
# Outbound cap of a plan in Mbit/s (GET /linode/types network_out, 22 Nov)
# and its price ($/h): a node gave out when it sends GIVE_OUT_NIC_SHARE of it.
NIC_MBPS = {"g6-dedicated-2": 4000, "g6-dedicated-4": 5000, "g6-dedicated-8": 6000,
            "g6-dedicated-16": 7000}
HOURLY = {"g6-dedicated-2": 0.054, "g6-dedicated-4": 0.108, "g6-dedicated-8": 0.216,
          "g6-dedicated-16": 0.432}
TRANSFER_USD_PER_GB = 0.005
GIVE_OUT_NIC_SHARE = 0.90
# A node is saturated when its CPU stays at or over this share for
# SATURATION_HOLD seconds, or (a listener host) the kernel drops more than
# RCVBUF_DROPS_PER_S UDP datagrams a second for want of buffer that long.
CPU_SATURATED = 0.90
SATURATION_HOLD = 3
RCVBUF_DROPS_PER_S = 50
# An edge gave out when its UDP listeners miss more than this share of the
# parts of an interval after the ramp.
MISSING_GAVE_OUT = 0.01
# Seconds a step spends outside its ramp and hold: start, the monitor's
# margin, the tools' drain, collection and the settle before the next step.
STEP_OVERHEAD_S = 75
LOAD_PROCESS = "needletail-load"
# Where needletail-load --out writes, under a step's directory on the host.
LOAD_DIR = "udp"
RAMP_STAGES = 10


# ---------------------------------------------------------------- the plan

def parse_steps(text: str) -> list[int]:
    """"100,1000,5k,10k" -> [100, 1000, 5000, 10000]; positive and rising."""
    steps = []
    for item in str(text).split(","):
        item = item.strip().lower().replace("_", "")
        if not item:
            continue
        scale = 1
        if item.endswith("k"):
            scale, item = 1000, item[:-1]
        try:
            value = int(round(float(item) * scale))
        except ValueError:
            raise ValueError(f"step {item!r} is not a number of listeners") from None
        if value <= 0:
            raise ValueError(f"step {value} is not positive")
        steps.append(value)
    if not steps:
        raise ValueError("no steps")
    if steps != sorted(steps) or len(set(steps)) != len(steps):
        raise ValueError(f"steps {steps} do not rise")
    return steps


def vcpus(node: dict) -> int:
    match = re.match(r"^g[0-9]+-[a-z]+-([0-9]+)$", str(node.get("type") or ""))
    return int(match.group(1)) if match else 1


def parts_per_s(config: str) -> float:
    return SAMPLE_RATE / CONFIGS[config]["epoch_samples"]


def natural(name: str) -> list:
    """A sort key that puts lon-edge-2 before lon-edge-10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"([0-9]+)", name)]


def regions_of(state: dict, standby: list[str] = ()) -> dict:
    """{site: {"edges", "distributors", "listeners", "view"}} of the lab, by
    name; standby edges are left out. A site without an edge or a listener
    host takes no listeners."""
    out: dict[str, dict] = {}
    for node in sorted(state["nodes"].values(), key=lambda item: natural(item["name"])):
        site = out.setdefault(node["site"], {"edges": [], "distributors": [], "listeners": [],
                                             "view": []})
        if node["role"] == "edge" and node["name"] not in standby:
            site["edges"].append(node)
        elif node["role"] == "distributor":
            site["distributors"].append(node)
        elif node["role"] == "load":
            purpose = common.load_purpose(node)
            if purpose in ("listeners", "view"):
                site[purpose].append(node)
    return {site: item for site, item in out.items() if item["edges"] and item["listeners"]}


def split(total: int, weights: list[float]) -> list[int]:
    """`total` in whole parts proportional to `weights` (largest remainder;
    ties to the first)."""
    if total <= 0 or not weights:
        return [0] * len(weights)
    whole = sum(weights)
    if whole <= 0:
        raise ValueError("weights sum to zero")
    raw = [total * weight / whole for weight in weights]
    parts = [math.floor(value) for value in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in order[:total - sum(parts)]:
        parts[i] += 1
    return parts


def allocate(total: int, regions: dict, hls_share: float = 0.5,
             hls_cap_per_host: int | None = None) -> dict:
    """{site: {host: {"udp": n, "hls": n, "hls_by_edge": {edge: n}}}} for a
    step of `total` listeners: over the regions in proportion to their
    edges, in a region over its listener hosts in proportion to their vCPUs,
    `hls_share` of a region's listeners LL-HLS readers, and a host's readers
    evenly over the region's edges (each host starting at the next edge, so
    that the region's readers spread evenly). With `hls_cap_per_host` a host
    runs at most that many readers and the rest of its share is UDP."""
    if not 0.0 <= hls_share <= 1.0:
        raise ValueError("hls_share is a fraction")
    sites = sorted(regions)
    per_site = split(total, [len(regions[site]["edges"]) for site in sites])
    out: dict[str, dict] = {}
    for site, count in zip(sites, per_site):
        hosts = regions[site]["listeners"]
        edges = [edge["name"] for edge in regions[site]["edges"]]
        weights = [vcpus(host) for host in hosts]
        hls_total = int(round(count * hls_share))
        udp_by_host = split(count - hls_total, weights)
        hls_by_host = split(hls_total, weights)
        out[site] = {}
        offset = 0
        for host, udp, hls in zip(hosts, udp_by_host, hls_by_host):
            if hls_cap_per_host is not None and hls > hls_cap_per_host:
                udp, hls = udp + hls - hls_cap_per_host, hls_cap_per_host
            rotated = edges[offset % len(edges):] + edges[:offset % len(edges)]
            by_edge = dict(zip(rotated, split(hls, [1.0] * len(rotated))))
            offset += hls
            out[site][host["name"]] = {"udp": udp, "hls": hls,
                                       "hls_by_edge": {edge: n for edge, n in by_edge.items()
                                                       if n}}
    return out


def load_costs(path: str | None) -> dict:
    """COSTS, with what a cost file ({"udp_vcpu": ..., "hls_vcpu": ...},
    from needletail-load's in-process bench) gives."""
    if not path:
        return dict(COSTS)
    given = json.loads(pathlib.Path(path).read_text())
    return {**COSTS, **{key: float(given[key]) for key in COSTS if key in given}}


def host_capacity(node: dict, config: str, costs: dict | None = None,
                  udp_mbps: float | None = None,
                  inbound_gbps: float = LOAD_HOST_INBOUND_GBPS,
                  headroom: float = PLAN_HEADROOM) -> dict:
    """What a listener host drives within the headroom: UDP listeners by CPU
    and by inbound bandwidth (the smaller), LL-HLS readers by CPU."""
    costs = {**COSTS, **(costs or {})}
    scale = parts_per_s(config) / 1000
    cores = vcpus(node)
    mbps = udp_mbps if udp_mbps is not None else CONFIGS[config]["udp_mbps"]
    by_cpu = int(cores * headroom / (costs["udp_vcpu"] * scale))
    by_net = int(inbound_gbps * 1000 * headroom / mbps) if mbps > 0 else by_cpu
    return {"vcpus": cores, "udp": min(by_cpu, by_net), "udp_by_cpu": by_cpu,
            "udp_by_network": by_net, "hls": int(cores * headroom / (costs["hls_vcpu"] * scale))}


def host_load(alloc: dict, capacity: dict) -> float:
    """The share of a host's planned capacity that an allocation uses (1.0
    is the headroom): UDP and LL-HLS add."""
    used = 0.0
    if alloc["udp"]:
        used += alloc["udp"] / max(1, capacity["udp"])
    if alloc["hls"]:
        used += alloc["hls"] / max(1, capacity["hls"])
    return used


def step_seconds(listeners_per_host: int, ramp: float | None = None,
                 hold: float = 60.0) -> tuple[float, float]:
    """(ramp, hold) of a step: listeners join over the ramp (about 100 a
    second on one host, 10 s to 60 s) and are held for `hold` seconds."""
    if ramp is None:
        ramp = min(60.0, max(10.0, listeners_per_host / 100.0))
    return float(ramp), float(hold)


def plan_steps(state: dict, steps: list[int], config: str, hls_share: float = 0.5,
               hls_cap_per_host: int | None = None, standby: list[str] = (),
               hold: float = 60.0, ramp: float | None = None, costs: dict | None = None,
               udp_mbps: float | None = None,
               inbound_gbps: float = LOAD_HOST_INBOUND_GBPS) -> dict:
    """The allocation, the capacity check, the edges' planned egress and the
    duration of every step."""
    regions = regions_of(state, standby)
    if not regions:
        raise ValueError("the lab has no region with an edge and a listener host")
    costs = {**COSTS, **(costs or {})}
    hosts = {node["name"]: node for site in regions.values() for node in site["listeners"]}
    cfg = CONFIGS[config]
    out = {"config": config, "hls_share": hls_share, "hls_cap_per_host": hls_cap_per_host,
           "costs": costs, "standby": list(standby),
           "regions": {site: {"edges": [node["name"] for node in item["edges"]],
                              "listeners": [node["name"] for node in item["listeners"]]}
                       for site, item in regions.items()},
           "capacity": {name: host_capacity(node, config, costs, udp_mbps, inbound_gbps)
                        for name, node in hosts.items()},
           "steps": []}
    total_s = 0.0
    for total in steps:
        alloc = allocate(total, regions, hls_share, hls_cap_per_host)
        uncapped = allocate(total, regions, hls_share)
        loads = {name: round(host_load(item, out["capacity"][name]), 3)
                 for by_host in alloc.values() for name, item in by_host.items()}
        biggest = max((item["udp"] + item["hls"] for by_host in alloc.values()
                       for item in by_host.values()), default=0)
        step_ramp, step_hold = step_seconds(biggest, ramp, hold)
        udp = sum(item["udp"] for by_host in alloc.values() for item in by_host.values())
        hls = sum(item["hls"] for by_host in alloc.values() for item in by_host.values())
        sites = {}
        for site in regions:
            edges = regions[site]["edges"]
            site_udp = sum(item["udp"] for item in alloc[site].values())
            site_hls = sum(item["hls"] for item in alloc[site].values())
            egress = ((site_udp * (udp_mbps or cfg["udp_mbps"]) + site_hls * cfg["hls_mbps"])
                      / len(edges))
            sites[site] = {"udp": site_udp, "hls": site_hls,
                           "listeners_per_edge": round((site_udp + site_hls) / len(edges), 1),
                           "edge_egress_mbps": round(egress, 1),
                           "edge_nic_share": round(egress / NIC_MBPS.get(edges[0]["type"], 4000),
                                                   3)}
        seconds = step_ramp + step_hold + STEP_OVERHEAD_S
        total_s += seconds
        out["steps"].append({
            "listeners": total, "udp": udp, "hls": hls,
            "hls_clipped": hls < sum(item["hls"] for by_host in uncapped.values()
                                     for item in by_host.values()),
            "allocation": alloc, "host_load": loads, "sites": sites,
            "drivable": all(value <= 1.0 for value in loads.values()),
            "ramp_s": step_ramp, "hold_s": step_hold, "seconds": seconds})
    out["seconds"] = total_s
    return out


def cost_estimate(state: dict, plan: dict, bring_up_hours: float = 1.0) -> dict:
    """Hours and dollars of one ramp: instance time (every instance billed
    a whole hour at least) and, were it public, the listeners' traffic at
    the transfer price (on the VLAN it is not billed)."""
    gigabytes = 0.0
    cfg = CONFIGS[plan["config"]]
    for step in plan["steps"]:
        mbps = step["udp"] * cfg["udp_mbps"] + step["hls"] * cfg["hls_mbps"]
        gigabytes += mbps / 8 / 1000 * (step["ramp_s"] / 2 + step["hold_s"])
    hourly = sum(HOURLY.get(node.get("type"), 0.0) for node in state["nodes"].values())
    hours = bring_up_hours + plan["seconds"] / 3600
    return {"ramp_minutes": round(plan["seconds"] / 60, 1), "hours": round(hours, 2),
            "hourly_usd": round(hourly, 3),
            "instances_usd": round(hourly * max(1.0, hours), 2),
            "listener_traffic_gb": round(gigabytes, 1),
            "listener_traffic_usd_if_public": round(gigabytes * TRANSFER_USD_PER_GB, 2)}


# ------------------------------------------------------------ the commands

def epoch_ns(samples: int) -> int:
    return samples * 1_000_000_000 // SAMPLE_RATE


def sender_command(config: str, target: str, seconds: int, audio: str,
                   opus_bitrate: int = OPUS_BITRATE) -> str:
    """The AEP1 sender on lon-src, as compare-audio.py starts it: epoch k
    leaves when its last sample has been spoken, the session id the loop
    START (a multiple of 64 s, at least 8 s away) plus one epoch."""
    cfg = CONFIGS[config]
    samples = cfg["epoch_samples"]
    opus = f"--opus-bitrate {opus_bitrate} " if cfg["payload"] == "opus" else ""
    return ("NOW=$(date +%s); START=$(( (NOW / 64 + 1) * 64 )); "
            "[ $(( START - NOW )) -lt 8 ] && START=$(( START + 64 )); "
            f"echo $START >{REMOTE}/source.start; "
            f"nohup /usr/local/bin/aep1-48k-probe send --target {target}:{CONTRIB_INGEST_PORT} "
            f"--session-id ${{START}}{epoch_ns(samples):09d} --group-id 0 "
            f"--duration-seconds {seconds} --payload {cfg['payload']} --epoch-samples {samples} "
            f"{opus}--channels 2 --group-channels 8 --pcm-s24le-file {shlex.quote(audio)} "
            f"--repair-percent 12 --min-repair-symbols 1 "
            f">{REMOTE}/source.json 2>{REMOTE}/source.log </dev/null &")


def ramp_spec(listeners: int, ramp_s: float, stages: int = RAMP_STAGES) -> str:
    """--ramp of one step: needletail-load holds each COUNT@SECOND from that
    second on, so the listeners come in `stages` equal steps over the ramp,
    all of them from its last stage."""
    stages = max(1, min(stages, listeners))
    return ",".join(f"{listeners * stage // stages}@{ramp_s * (stage - 1) / stages:g}"
                    for stage in range(1, stages + 1))


def load_command(alloc: dict, edges: list[str], config: str, ramp_s: float,
                 duration_s: float, sample: int, remote_dir: str, label: str = "scale",
                 bind: str | None = None, extra: str = "", threads: int | None = None) -> str:
    """needletail-load of one listener host for one step (empty when it runs
    no UDP listener). `edges`: the region's edges' VLAN host:port (a
    listener starts at edge i mod n and follows MOVE as the real client
    does); `bind`: the host's VLAN address for the sockets; `threads`: its
    workers, one per vCPU."""
    if not alloc["udp"]:
        return ""
    return (f"ulimit -n 1048576; nohup {LOAD_PROCESS} "
            f"--edges {','.join(edges)} --stream {CONFIGS[config]['stream']} "
            f"--listeners {alloc['udp']} --ramp {ramp_spec(alloc['udp'], ramp_s)} "
            f"--duration {int(round(duration_s))} --sample {min(sample, alloc['udp'])} "
            + (f"--bind {bind} " if bind else "")
            + (f"--threads {threads} " if threads else "")
            + f"--out {remote_dir}/{LOAD_DIR} --label {shlex.quote(label)} --quiet "
            f"{extra} >{remote_dir}/udp.out 2>{remote_dir}/udp.err </dev/null &")


def hls_commands(alloc: dict, entry: str, config: str, ramp_s: float, duration_s: float,
                 remote_dir: str, label: str, window: int = 40, http3: bool = True,
                 steering: str = "spread") -> list[str]:
    """ntcmp LL-HLS crowds of one host for one step: `alloc["hls"]` readers,
    each its own session at the region's entry (a distributor's VLAN
    address), joining over the ramp, `window` part requests in flight; with
    "spread" one crowd per edge pinned to it (`cache=<edge>`, as many as
    `alloc["hls_by_edge"]` gives it), with "steered" one crowd that the
    region's steering assigns. One report per reader, condensed after the
    step."""
    if not alloc["hls"]:
        return []
    cfg = CONFIGS[config]
    index = f"{REMOTE_MEDIA}/index/{cfg['index'][0]}.index.json"
    groups = (alloc.get("hls_by_edge") or {} if steering == "spread"
              else {"steered": alloc["hls"]})
    out = []
    for group, count in sorted(groups.items()):
        if count <= 0:
            continue
        spread = int(round(ramp_s * 1000)) if count > 1 else 0
        pin = f"&cache={group}" if steering == "spread" else ""
        out.append(
            f"ulimit -n 1048576; mkdir -p {remote_dir}/hls; nohup ntcmp hls --lane scale-hls "
            f"--mode chase --window {window} {'--http3 ' if http3 else ''}"
            f"--url 'https://{entry}:{HTTP_PORT}/live/{cfg['stream']}/master.m3u8"
            f"?sid={label}-{group}-{{i}}{pin}' --ca {CA} --server-name {SERVER_NAME} "
            f"--audio-index {index} --duration {int(round(duration_s))} "
            f"--skip-seconds {int(ramp_s) + 2} --label {label}-{group} "
            f"--report {remote_dir}/hls/reader-{group}.json --count {count} "
            + (f"--spread-ms {spread} " if spread else "")
            + f">{remote_dir}/hls-{group}.out 2>{remote_dir}/hls-{group}.err </dev/null &")
    return out


# The sampler of every node: one JSON line a second. CPU busy share of the
# whole host and of its busiest core, steal, bytes and packets a second in
# and out of each interface (and which one carries the public address:
# Linode bills what leaves it), UDP drops for want of buffer, and the CPU
# (in cores) of each service process.
SAMPLER = r"""
import json, os, socket, subprocess, sys, time
out, seconds, public = sys.argv[1], float(sys.argv[2]), sys.argv[3]
WATCH = ("av-mesh", "av-contrib", "needletail-load", "ntcmp", "aep1-48k-probe",
         "livekit-server")
HZ = os.sysconf("SC_CLK_TCK")
def ifaces():
    names = {}
    for line in subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                               text=True).stdout.splitlines():
        parts = line.split()
        names[parts[1]] = parts[3].split("/")[0]
    return names
def cpu():
    rows = {}
    for line in open("/proc/stat"):
        if line.startswith("cpu"):
            name, *values = line.split()
            values = [int(v) for v in values[:8]]
            rows[name] = (sum(values), values[3] + values[4], values[7])
    return rows
def net():
    rows = {}
    for line in open("/proc/net/dev").readlines()[2:]:
        name, data = line.split(":", 1)
        values = data.split()
        rows[name.strip()] = (int(values[0]), int(values[8]), int(values[1]), int(values[9]))
    return rows
def udp():
    lines = [line.split() for line in open("/proc/net/snmp") if line.startswith("Udp:")]
    table = dict(zip(lines[0][1:], (int(v) for v in lines[1][1:])))
    return {key: table.get(key, 0) for key in ("RcvbufErrors", "SndbufErrors", "InErrors")}
PAGE = os.sysconf("SC_PAGE_SIZE")
def procs():
    rows, rss = {}, {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
            if comm not in WATCH:
                continue
            fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            resident = int(open(f"/proc/{pid}/statm").read().split()[1]) * PAGE
            rows[comm] = rows.get(comm, 0) + int(fields[11]) + int(fields[12])
            rss[comm] = rss.get(comm, 0) + resident
        except (OSError, IndexError, ValueError):
            continue
    RSS.clear()
    RSS.update(rss)
    return rows
RSS = {}
addresses = ifaces()
public_iface = next((name for name, address in addresses.items() if address == public), "eth0")
handle = open(out, "w")
handle.write(json.dumps({"host": socket.gethostname(), "public_iface": public_iface,
                         "addresses": addresses, "cpus": os.cpu_count()}) + "\n")
last = (time.time(), cpu(), net(), udp(), procs())
deadline = time.time() + seconds
while time.time() < deadline:
    time.sleep(1.0 - (time.time() % 1.0))
    now = (time.time(), cpu(), net(), udp(), procs())
    dt = now[0] - last[0]
    def busy(name):
        total = now[1][name][0] - last[1][name][0]
        idle = now[1][name][1] - last[1][name][1]
        return round(1 - idle / total, 4) if total > 0 else 0.0
    cores = [name for name in now[1] if name != "cpu"]
    steal_total = now[1]["cpu"][0] - last[1]["cpu"][0]
    def rate(index):
        return {name: int((now[2][name][index] - last[2][name][index]) / dt) for name in now[2]
                if name in last[2] and name != "lo"}
    row = {"t": round(now[0], 3), "cpu": busy("cpu"),
           "core_max": max((busy(name) for name in cores), default=0.0),
           "steal": round((now[1]["cpu"][2] - last[1]["cpu"][2]) / steal_total, 4)
                    if steal_total > 0 else 0.0,
           "rx": rate(0), "tx": rate(1), "rx_pps": rate(2), "tx_pps": rate(3),
           "udp": {key: now[3][key] - last[3][key] for key in now[3]},
           "procs": {name: round((now[4][name] - last[4].get(name, now[4][name])) / HZ / dt, 3)
                     for name in now[4]},
           "rss_mb": {name: round(value / 1e6, 1) for name, value in RSS.items()}}
    handle.write(json.dumps(row) + "\n")
    handle.flush()
    last = now
"""


def sampler_command(node: dict, remote_dir: str, seconds: float) -> str:
    return (f"mkdir -p {remote_dir}; cat >{REMOTE}/sampler.py <<'PY'\n{SAMPLER}\nPY\n"
            f"nohup python3 {REMOTE}/sampler.py {remote_dir}/sampler.jsonl {int(seconds)} "
            f"{node['public_ipv4']} >/dev/null 2>&1 </dev/null &")


# One file of what the step's readers saw: every report of the crowds,
# reduced to the fields that the analysis reads.
HLS_CONDENSE = r"""
import glob, json, sys
rows = []
for path in sorted(glob.glob(sys.argv[1] + "/hls/reader*.json")):
    try:
        report = json.load(open(path))
    except (OSError, ValueError):
        rows.append({"file": path, "error": "unreadable"})
        continue
    detail = report.get("detail") or {}
    row = {key: report.get(key) for key in (
        "label", "audio_packets", "audio_sequence_gaps", "audio_duplicates",
        "audio_late_packets", "audio_latency_ms", "first_audio_ms", "first_packet_ms",
        "duration_seconds", "error")}
    row["published_to_complete"] = (detail.get("server_timing_ms") or {}).get(
        "published_to_complete")
    row["refused"] = sum(int(value) for key, value in (detail.get("counts") or {}).items()
                         if key.endswith(("_429", "_503")))
    rows.append(row)
json.dump(rows, open(sys.argv[1] + "/hls.condensed.json", "w"))
print(len(rows))
"""


# ------------------------------------------------------- reading the files

def read_jsonl(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def sampler_rows(text: str) -> tuple[dict, list[dict]]:
    """(header, one row a second) of a sampler file."""
    rows = read_jsonl(text)
    header = rows[0] if rows and "t" not in rows[0] else {}
    return header, [row for row in rows if "t" in row]


def first_held(rows: list[dict], test, hold: int = SATURATION_HOLD) -> float | None:
    """The time of the first row that starts `hold` rows in a row for which
    test(row) holds."""
    run = 0
    for index, row in enumerate(rows):
        run = run + 1 if test(row) else 0
        if run >= hold:
            return rows[index - hold + 1]["t"]
    return None


def summarize_samples(header: dict, rows: list[dict], since: float | None = None,
                      until: float | None = None, node_type: str | None = None,
                      cpu_saturated: float = CPU_SATURATED,
                      hold: int = SATURATION_HOLD) -> dict:
    """CPU, bytes and drops of one node over a window of its sampler rows,
    and when it saturated: CPU held at or over `cpu_saturated`, UDP
    receive-buffer drops held over RCVBUF_DROPS_PER_S, or bytes out held at
    GIVE_OUT_NIC_SHARE of its plan's outbound cap."""
    rows = [row for row in rows if (since is None or row["t"] >= since)
            and (until is None or row["t"] <= until)]
    if not rows:
        return {"samples": 0}
    public = header.get("public_iface", "eth0")
    cpu = sorted(row["cpu"] for row in rows)

    def mean(values):
        values = list(values)
        return round(sum(values) / len(values), 4) if values else 0.0

    tx_public = [row["tx"].get(public, 0) for row in rows]
    tx_all = [sum(row["tx"].values()) for row in rows]
    rx_all = [sum(row["rx"].values()) for row in rows]
    procs: dict[str, list[float]] = {}
    for row in rows:
        for name, value in row.get("procs", {}).items():
            procs.setdefault(name, []).append(value)
    nic_bytes = NIC_MBPS.get(node_type or "", 0) * 1e6 / 8
    tests = [("cpu", lambda row: row["cpu"] >= cpu_saturated),
             ("udp_rcvbuf_drops", lambda row: row["udp"].get("RcvbufErrors", 0)
              > RCVBUF_DROPS_PER_S)]
    if nic_bytes:
        tests.append(("nic_out", lambda row: sum(row["tx"].values())
                      >= GIVE_OUT_NIC_SHARE * nic_bytes))
    saturated = [(at, why) for why, test in tests
                 if (at := first_held(rows, test, hold)) is not None]
    first = min(saturated) if saturated else None
    return {
        "samples": len(rows), "cpus": header.get("cpus"),
        "cpu_mean": mean(cpu), "cpu_p95": cpu[min(len(cpu) - 1, int(0.95 * len(cpu)))],
        "cpu_max": cpu[-1], "core_max": max(row.get("core_max", 0.0) for row in rows),
        "steal_mean": mean(row.get("steal", 0.0) for row in rows),
        "tx_bps_mean": int(mean(tx_all) * 8), "rx_bps_mean": int(mean(rx_all) * 8),
        "tx_pps_mean": int(mean(sum((row.get("tx_pps") or {}).values()) for row in rows)),
        "tx_public_bps_mean": int(mean(tx_public) * 8),
        "tx_public_bytes": int(sum(tx_public)),
        "udp_rcvbuf_drops": sum(row["udp"].get("RcvbufErrors", 0) for row in rows),
        "udp_sndbuf_drops": sum(row["udp"].get("SndbufErrors", 0) for row in rows),
        "procs_cores_mean": {name: mean(values) for name, values in sorted(procs.items())},
        "procs_rss_mb_max": {name: max(row.get("rss_mb", {}).get(name, 0.0) for row in rows)
                             for name in sorted({name for row in rows
                                                 for name in row.get("rss_mb", {})})},
        "saturated_at": first[0] if first else None,
        "saturated_by": first[1] if first else None,
    }


def pick(item: dict, *names, default=None):
    """The first of `names` that `item` has (the load tool's field names and
    their aliases)."""
    for name in names:
        if isinstance(item, dict) and name in item and item[name] is not None:
            return item[name]
    return default


COUNT_FIELDS = {
    "listeners": ("up", "listeners_up", "listeners", "active"),
    "parts": ("parts", "parts_received", "received"),
    "missing": ("missing", "missing_parts", "lost"),
    "recovered": ("recovered", "recovered_parts"),
    "duplicates": ("duplicates", "duplicate_parts"),
    "nacks": ("nack_parts", "nacks", "nacks_sent"),
    "bytes": ("bytes_in", "bytes"),
}


def interval_rows(rows: list[dict]) -> list[dict]:
    """One row per (interval, edge) of the load tool's intervals file, its
    counters under the names of COUNT_FIELDS ("edge" None for the host's
    total). Takes "edges" as a list of {"edge", ...} or a dict by edge, and
    a line per interval and edge with "edge"."""
    out = []
    for row in rows:
        t = pick(row, "unix_ms", "t", "unix_s", "time", "t_unix")
        if t is None:
            continue
        t = float(t) / (1e6 if float(t) > 1e14 else 1e3 if float(t) > 1e11 else 1)
        groups = []
        if isinstance(row.get("edges"), dict):
            groups += [(edge, item) for edge, item in row["edges"].items()]
        elif isinstance(row.get("edges"), list):
            groups += [(pick(item, "edge", "addr"), item) for item in row["edges"]
                       if isinstance(item, dict)]
        else:
            groups.append((pick(row, "edge"), row))
        if isinstance(row.get("total"), dict):
            groups.append((None, row["total"]))
        host = next((row[key] for key in ("load_host", "host")
                     if isinstance(row.get(key), dict)), row)
        for edge, item in groups:
            out.append({"t": t, "edge": edge,
                        **{key: pick(item, *names, default=0) or 0
                           for key, names in COUNT_FIELDS.items()},
                        "phase": row.get("phase"),
                        "host_cpu": pick(host, "host_cpu_busy", "cpu_host_busy_percent",
                                         "host_cpu", "cpu"),
                        "rcvbuf_drops": pick(host, "udp_rcvbuf_errors", "rcvbuf_errors"),
                        "saturated": bool(pick(row, "load_host_saturated", "saturated",
                                               default=False)),
                        "reasons": pick(row, "load_host_reasons",
                                        default=pick(host, "saturation", default=[]))})
    return out


QUANTILES = (("p50", 0.50), ("p95", 0.95), ("p99", 0.99), ("p999", 0.999))


def histogram_of(item) -> dict[float, int] | None:
    """{bucket value in µs: count} of a histogram's buckets in the shapes
    [[value, count], ...], [{"value_us"|"le_us"|..., "count"}], {value:
    count}, or a block {"buckets": ...}; None when absent."""
    if item is None:
        return None
    if isinstance(item, dict) and "buckets" in item:
        item = item["buckets"]
    out: dict[float, int] = {}
    if isinstance(item, dict):
        for key, value in item.items():
            try:
                out[float(key)] = out.get(float(key), 0) + int(value)
            except (TypeError, ValueError):
                return None
    elif isinstance(item, list):
        for entry in item:
            if isinstance(entry, dict):
                value = pick(entry, "value_us", "le_us", "upper_us", "value", "le", "low")
                count = pick(entry, "count", "n")
            else:
                value, count = entry[0], entry[1]
            out[float(value)] = out.get(float(value), 0) + int(count)
    else:
        return None
    return out or None


def merge_histograms(histograms: list[dict[float, int]]) -> dict[float, int]:
    merged: dict[float, int] = {}
    for histogram in histograms:
        for value, count in histogram.items():
            merged[value] = merged.get(value, 0) + count
    return merged


def histogram_quantiles(histogram: dict[float, int]) -> dict:
    """Quantiles (in ms: the value of the bucket that holds rank
    ceil(q x count), as needletail-load reports its own) and the count of
    a histogram in µs."""
    total = sum(histogram.values())
    if not total:
        return {"count": 0}
    out: dict = {"count": total}
    ordered = sorted(histogram.items())
    for name, q in QUANTILES:
        rank, seen = max(1, math.ceil(q * total)), 0
        for value, count in ordered:
            seen += count
            if seen >= rank:
                out[name] = round(value / 1000, 3)
                break
    out["max"] = round(ordered[-1][0] / 1000, 3)
    return out


def stats_ms(item, unit: str = "ms") -> dict | None:
    """p50/p95/p99/p99.9/max in ms of a percentile block: plain keys in
    `unit` ("ms" or "us"), or keys that name theirs (p50_us, p50_ms)."""
    if not isinstance(item, dict):
        return None
    out = {}
    plain = 1e-3 if unit == "us" else 1.0
    for name in ("p50", "p95", "p99", "p999", "max", "count"):
        for key, scale in ((f"{name}_us", 1e-3), (f"{name}_ms", 1.0), (name, plain)):
            if key in item and item[key] is not None:
                value = float(item[key])
                out[name] = int(value) if name == "count" else round(value * scale, 3)
                break
    return out or None


# needletail-load's histogram: values below 2 x 64 exactly, then 64 buckets
# for each power of two; "buckets_us" gives each bucket's lowest value.
LOAD_SUB_BITS = 6


def load_bucket_middle(low: float) -> float:
    """The value that needletail-load reports for the bucket whose lowest
    value is `low` (its middle, as av_mesh::load::Histogram::value_of)."""
    low = int(low)
    if low < 2 << LOAD_SUB_BITS:
        return float(low)
    shift = low.bit_length() - 1 - LOAD_SUB_BITS
    return float(low + ((1 << shift) - 1) // 2)


def timing(block: dict, *names: str) -> tuple[dict | None, dict | None]:
    """(percentiles in ms, histogram in µs) of the first of `names` (each
    tried as NAME_ms, NAME_us and NAME) that a block has. needletail-load's
    summary blocks are NAME_ms with "buckets_us" (each bucket's lowest
    value, read as its middle); a plain NAME block that carries "buckets"
    is in µs."""
    for name in names:
        for key, unit in ((f"{name}_ms", "ms"), (f"{name}_us", "us"), (name, None)):
            item = block.get(key) if isinstance(block, dict) else None
            if not isinstance(item, dict):
                continue
            if unit is None:
                unit = "us" if "buckets" in item else "ms"
            histogram = histogram_of(item.get("buckets"))
            lows = histogram_of(item.get("buckets_us"))
            if lows:
                histogram = merge_histograms([{load_bucket_middle(low): count}
                                              for low, count in lows.items()])
            return stats_ms(item, unit), histogram
    return None, None


def summary_blocks(summary: dict) -> tuple[dict, dict]:
    """(total, {edge: block}) of the load tool's summary JSON."""
    total = dict(pick(summary, "total", "all", default=summary) or {})
    peak = pick(summary, "listeners_peak", "peak_subscribed")
    if peak is not None and not any(key in total for key in ("listeners", "up")):
        total["listeners"] = peak
    edges = pick(summary, "per_edge", "edges", default={}) or {}
    if isinstance(edges, list):
        edges = {pick(item, "edge", "addr"): item for item in edges if isinstance(item, dict)}
    return total, edges


def block_view(block: dict) -> dict:
    """What the report shows of one block of the load tool's summary:
    latency (publication to arrival), missing share, time to first part,
    listeners, and the histograms when the tool gives them."""
    latency, histogram = timing(block, "latency", "publish_to_arrival")
    ttfp, ttfp_histogram = timing(block, "ttfp", "time_to_first_part")
    parts = pick(block, "parts", "parts_received", "received", default=0) or 0
    missing = pick(block, "missing", "missing_parts", default=0) or 0
    missing_pct = pick(block, "missing_percent", "missing_pct")
    if missing_pct is None and parts + missing:
        missing_pct = 100.0 * missing / (parts + missing)
    return {"listeners": pick(block, "listeners", "up", "listeners_up", "listeners_peak",
                              "listeners_max", "active", "listeners_started"),
            "latency_ms": latency, "histogram": histogram,
            "ttfp_ms": ttfp, "ttfp_histogram": ttfp_histogram,
            "parts": parts, "missing": missing,
            "missing_percent": None if missing_pct is None else round(float(missing_pct), 4),
            "recovered": pick(block, "recovered", "recovered_parts", default=0),
            "nacks": pick(block, "nack_parts", "nacks", default=0),
            "moves": pick(block, "moves", default=0)}


def merge_views(views: list[dict]) -> dict:
    """One group's UDP view (a region, an edge) from its hosts' blocks:
    counts add; latency and time to first part merge exactly when every
    block has a histogram ("merge": "histogram"), else p50 is the
    listener-weighted mean of the blocks' and the upper quantiles the worst
    block's ("merge": "worst-host")."""
    views = [view for view in views if view]
    if not views:
        return {}
    parts = sum(view["parts"] or 0 for view in views)
    missing = sum(view["missing"] or 0 for view in views)
    out = {"hosts": len(views), "listeners": sum(view["listeners"] or 0 for view in views),
           "parts": parts, "missing": missing,
           "missing_percent": round(100.0 * missing / (parts + missing), 4)
           if parts + missing else None,
           "recovered": sum(view["recovered"] or 0 for view in views),
           "nacks": sum(view["nacks"] or 0 for view in views),
           "moves": sum(view.get("moves") or 0 for view in views)}
    for key, hist in (("latency_ms", "histogram"), ("ttfp_ms", "ttfp_histogram")):
        if all(view.get(hist) for view in views):
            out[key] = histogram_quantiles(merge_histograms([view[hist] for view in views]))
            out[key]["merge"] = "histogram"
            continue
        blocks = [(view[key], view["listeners"] or 1) for view in views if view.get(key)]
        if not blocks:
            continue
        weight = sum(count for _block, count in blocks)
        merged: dict = {"merge": "worst-host"}
        if all("p50" in block for block, _count in blocks):
            merged["p50"] = round(sum(block["p50"] * count for block, count in blocks) / weight, 3)
        for name in ("p95", "p99", "p999", "max"):
            values = [block[name] for block, _count in blocks if name in block]
            if values:
                merged[name] = max(values)
        out[key] = merged
    return out


def weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float | None:
    pairs = sorted((value, weight) for value, weight in pairs if weight > 0)
    total = sum(weight for _, weight in pairs)
    if not total:
        return None
    seen = 0.0
    for value, weight in pairs:
        seen += weight
        if seen >= q * total:
            return value
    return pairs[-1][0]


def hls_region(rows: list[dict]) -> dict:
    """One region's LL-HLS view from its readers' condensed reports. ntcmp
    keeps each reader's distribution but no histogram, so the region's
    figures are of the readers': p50 the units-weighted median of their
    medians, p99 the units-weighted p99 of their p99s (an upper bound of
    the region's p99). Mouth to ear (the source's spoken time, from the
    audio index) and publication to arrival (the server timing headers);
    time to first audio over the readers; refused requests (429, 503)."""
    readers = [row for row in rows if isinstance(row, dict)]

    def spans(getter) -> dict | None:
        values = [getter(row) for row in readers]
        values = [value for value in values if isinstance(value, dict) and value.get("count")]
        if not values:
            return None
        return {"readers": len(values),
                "p50": weighted_quantile([(v["p50"], v["count"]) for v in values], 0.5),
                "p99": weighted_quantile([(v["p99"], v["count"]) for v in values], 0.99),
                "max": max(v["max"] for v in values), "merge": "readers"}

    parts = sum(int(row.get("audio_packets") or 0) for row in readers)
    missing = sum(max(0, int(row.get("audio_sequence_gaps") or 0)) for row in readers)
    firsts = sorted(float(value) for value in (
        row.get("first_audio_ms") if (row.get("first_audio_ms") or -1) >= 0
        else row.get("first_packet_ms") for row in readers) if value is not None and value >= 0)
    return {"readers": len(readers),
            "errors": sum(1 for row in readers if row.get("error")),
            "refused_requests": sum(int(row.get("refused") or 0) for row in readers),
            "parts": parts, "missing": missing,
            "missing_percent": round(100.0 * missing / (parts + missing), 4)
            if parts + missing else None,
            "latency_ms": spans(lambda row: row.get("audio_latency_ms")),
            "publish_to_arrival_ms": spans(lambda row: row.get("published_to_complete")),
            "ttfa_ms": {"p50": firsts[len(firsts) // 2],
                        "p99": firsts[min(len(firsts) - 1, math.ceil(0.99 * len(firsts)) - 1)]}
            if firsts else None}


def tier_of(node: dict) -> str:
    if node["role"] == "load":
        return {"listeners": "load", "view": "view", "source": "source"}[
            common.load_purpose(node)]
    return TIERS.get(node["role"], node["role"])


def gave_out_first(state: dict, nodes: dict[str, dict], edges_missing: dict[str, float | None],
                   load_events: dict[str, float | None]) -> dict | None:
    """The first tier that gave out in a step: the earliest of a mesh node's
    or a listener host's saturation (`nodes`: summarize_samples of each), an
    edge whose UDP listeners missed more than MISSING_GAVE_OUT of the parts
    of an interval (`edges_missing`: its first such time), and a listener
    host that needletail-load or the monitor saw saturate (`load_events`).
    None when nothing gave out."""
    events = []
    for name, summary in nodes.items():
        node = state["nodes"].get(name)
        if node is None or summary.get("saturated_at") is None:
            continue
        tier = tier_of(node)
        if tier in ("contributor", "forwarder", "edge", "load"):
            events.append((summary["saturated_at"], tier, name, summary["saturated_by"]))
    for name, at in edges_missing.items():
        if at is not None:
            events.append((at, "edge", name, "listeners_missing_parts"))
    for name, at in load_events.items():
        if at is not None:
            events.append((at, "load", name, "load_tool_saturated"))
    if not events:
        return None
    at, tier, name, reason = min(events)
    return {"tier": tier, "node": name, "t": at, "reason": reason,
            "others": [{"tier": t, "node": n, "t": a, "reason": r}
                       for a, t, n, r in sorted(events)[1:8]]}


def load_saturated(rows: list[dict], intervals: list[dict],
                   cpu_saturated: float = CPU_SATURATED, hold: int = SATURATION_HOLD) -> str | None:
    """Why a listener host is saturated now, from the newest rows of its
    sampler and its load tool's newest intervals; None when it is not."""
    tail = rows[-hold:]
    if len(tail) >= hold and all(row["cpu"] >= cpu_saturated for row in tail):
        return f"cpu {min(row['cpu'] for row in tail):.2f} for {hold} s"
    if len(tail) >= hold and all(row["udp"].get("RcvbufErrors", 0) > RCVBUF_DROPS_PER_S
                                 for row in tail):
        return (f"{sum(row['udp'].get('RcvbufErrors', 0) for row in tail)} UDP receive-buffer "
                f"drops in {hold} s")
    lines = interval_lines(intervals)[-hold:]
    if len(lines) >= hold and all(line["saturated"] for line in lines):
        reasons = lines[-1].get("reasons") or []
        return (f"needletail-load: {', '.join(map(str, reasons)) or 'saturated'} "
                f"for {hold} intervals")
    return None


def interval_lines(rows: list[dict], since: float | None = None,
                   until: float | None = None) -> list[dict]:
    """One row per interval of the load tool (its rows share the interval's
    verdict), in time order, the final line after the drain left out."""
    by_t: dict[float, dict] = {}
    for row in rows:
        if row.get("phase") == "final":
            continue
        if (since is not None and row["t"] < since) or (until is not None and row["t"] > until):
            continue
        by_t.setdefault(row["t"], row)
    return [by_t[t] for t in sorted(by_t)]


def load_saturated_at(rows: list[dict], since: float | None = None, until: float | None = None,
                      hold: int = SATURATION_HOLD) -> float | None:
    """When needletail-load first said its host was saturated for `hold`
    intervals in a row within the step's window: one interval's verdict (a
    few drops, a late timer while listeners join) is not the host giving
    out."""
    return first_held(interval_lines(rows, since, until), lambda line: line["saturated"], hold)


def edge_name_of(address: str | None, state: dict) -> str | None:
    """The node of a load tool's edge address (VLAN or public host:port)."""
    if not address:
        return None
    host = str(address).rsplit(":", 1)[0]
    for node in state["nodes"].values():
        if host in (node.get("vlan_ipv4"), node.get("public_ipv4"), node["name"]):
            return node["name"]
    return None


# Receive softirq of the NIC that a process's own CPU time does not hold,
# vCPU per listener of a 1,000-part stream (part L: about 1.5 µs a datagram).
SOFTIRQ_VCPU = 0.0015


def median(values: list[float]) -> float | None:
    values = sorted(values)
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def measured_costs(nodes: dict[str, dict], allocation: dict, config: str) -> dict:
    """What a listener cost its listener host in this step, from the
    sampler: the CPU (in vCPUs, per listener of a 1,000-part stream) and the
    peak memory (MB per listener) of needletail-load and of ntcmp, the
    median over the hosts that ran them. `udp_vcpu` and `hls_vcpu` add
    SOFTIRQ_VCPU and are what `--cost FILE` takes."""
    scale = parts_per_s(config) / 1000
    out: dict = {"hosts": {}}
    gathered: dict[str, list[float]] = {}
    for by_host in allocation.values():
        for host, alloc in by_host.items():
            summary = nodes.get(host) or {}
            cores = summary.get("procs_cores_mean") or {}
            rss = summary.get("procs_rss_mb_max") or {}
            row = {}
            for lane, process in (("udp", LOAD_PROCESS), ("hls", "ntcmp")):
                count = alloc.get(lane) or 0
                if count and process in cores:
                    row[f"{lane}_process_vcpu"] = round(cores[process] / count / scale, 6)
                    gathered.setdefault(f"{lane}_process_vcpu", []).append(
                        row[f"{lane}_process_vcpu"])
                if count and process in rss:
                    row[f"{lane}_mb"] = round(rss[process] / count, 3)
                    gathered.setdefault(f"{lane}_mb", []).append(row[f"{lane}_mb"])
            if row:
                out["hosts"][host] = row
    for key, values in gathered.items():
        out[key] = round(median(values), 6)
    for lane in ("udp", "hls"):
        if f"{lane}_process_vcpu" in out:
            out[f"{lane}_vcpu"] = round(out[f"{lane}_process_vcpu"] + SOFTIRQ_VCPU, 6)
    return out


def analyse_step(state: dict, step_dir: pathlib.Path, plan_step: dict, window: tuple,
                 config: str = "pcm-1") -> dict:
    """Aggregate one step's files: steps/STEP/HOST/{sampler.jsonl,
    udp/intervals.jsonl, udp/summary.json, hls.condensed.json} and
    counters.json. `window`: (end of the ramp, end of the step), unix s."""
    since, until = window
    nodes = {}
    for host_dir in sorted(path for path in step_dir.iterdir() if path.is_dir()):
        sampler = host_dir / "sampler.jsonl"
        if sampler.is_file():
            header, rows = sampler_rows(sampler.read_text())
            node = state["nodes"].get(host_dir.name) or {}
            nodes[host_dir.name] = summarize_samples(header, rows, since, until, node.get("type"))
    regions: dict[str, dict] = {}
    edges_missing: dict[str, float | None] = {}
    load_events: dict[str, float | None] = {}
    by_edge_rows: dict[str, list[dict]] = {}
    for site, by_host in plan_step["allocation"].items():
        udp_views, hls_rows, edge_views = [], [], {}
        for host in by_host:
            host_dir = step_dir / host
            summary_path = host_dir / LOAD_DIR / "summary.json"
            if summary_path.is_file():
                try:
                    total, edges = summary_blocks(json.loads(summary_path.read_text()))
                except ValueError:
                    total, edges = {}, {}
                udp_views.append(block_view(total))
                for address, block in edges.items():
                    name = edge_name_of(address, state) or str(address)
                    edge_views.setdefault(name, []).append(block_view(block))
            intervals_path = host_dir / LOAD_DIR / "intervals.jsonl"
            if intervals_path.is_file():
                rows = interval_rows(read_jsonl(intervals_path.read_text()))
                load_events[host] = load_saturated_at(rows, since, until)
                for row in rows:
                    if row["edge"] is not None:
                        name = edge_name_of(row["edge"], state) or str(row["edge"])
                        by_edge_rows.setdefault(name, []).append(row)
            condensed = host_dir / "hls.condensed.json"
            if condensed.is_file():
                try:
                    hls_rows += json.loads(condensed.read_text())
                except ValueError:
                    pass
        regions[site] = {"udp": merge_views(udp_views), "hls": hls_region(hls_rows),
                         "edges": {name: merge_views(views)
                                   for name, views in sorted(edge_views.items())}}
    edge_listeners: dict[str, int] = {}
    for name, rows in by_edge_rows.items():
        by_t: dict[float, list[dict]] = {}
        for row in rows:
            by_t.setdefault(round(row["t"]), []).append(row)
        listeners, first_bad = 0, None
        for t in sorted(by_t):
            group = by_t[t]
            listeners = max(listeners, sum(row["listeners"] for row in group))
            # The parts that a saturated load host missed are its own, not
            # the edge's: only the hosts that kept up judge the edge.
            judged = [row for row in group if not row["saturated"]]
            parts = sum(row["parts"] for row in judged)
            missing = sum(row["missing"] for row in judged)
            if (first_bad is None and parts + missing and since <= t <= until
                    and not any(row["phase"] == "final" for row in group)
                    and missing / (parts + missing) > MISSING_GAVE_OUT):
                first_bad = float(t)
        edge_listeners[name] = listeners
        edges_missing[name] = first_bad
    tiers: dict[str, dict] = {}
    for name, summary in nodes.items():
        node = state["nodes"].get(name)
        if node is None or not summary.get("samples"):
            continue
        tier = tiers.setdefault(tier_of(node), {"nodes": 0, "cpu_mean": [], "cpu_max": 0.0,
                                                "tx_bps": 0, "tx_public_bytes": 0,
                                                "udp_rcvbuf_drops": 0})
        tier["nodes"] += 1
        tier["cpu_mean"].append(summary["cpu_mean"])
        tier["cpu_max"] = max(tier["cpu_max"], summary["cpu_max"])
        tier["tx_bps"] += summary["tx_bps_mean"]
        tier["tx_public_bytes"] += summary["tx_public_bytes"]
        tier["udp_rcvbuf_drops"] += summary["udp_rcvbuf_drops"]
    for tier in tiers.values():
        values = tier.pop("cpu_mean")
        tier["cpu_mean"] = round(sum(values) / len(values), 4) if values else None
        tier["tx_bps_per_node"] = int(tier["tx_bps"] / max(1, tier["nodes"]))
    counters_path = step_dir / "counters.json"
    counters = json.loads(counters_path.read_text()) if counters_path.is_file() else {}
    return {"listeners": plan_step["listeners"], "udp": plan_step["udp"],
            "hls": plan_step["hls"], "window": [since, until], "regions": regions,
            "edge_listeners": edge_listeners, "nodes": nodes, "tiers": tiers,
            "edge_counters": counters,
            "public_transfer_gb": round(sum(summary.get("tx_public_bytes", 0)
                                            for summary in nodes.values()) / 1e9, 3),
            "measured_costs": measured_costs(nodes, plan_step["allocation"], config),
            "gave_out_first": gave_out_first(state, nodes, edges_missing, load_events)}


def format_summary(result: dict) -> str:
    """summary.md of a run: per step and region the lanes' figures, per
    step the tiers, then the edges' listeners and what gave out first."""
    lines = [f"# Scale run {result['run']} ({result['config']})", "",
             f"Listeners: {result['hls_share'] * 100:.0f}% LL-HLS (ntcmp: mouth to ear), the rest "
             "UDP (needletail-load: publication to arrival). Latency in ms; LL-HLS p99 is the "
             "readers' p99s' p99 (an upper bound).", "",
             "| step | region | UDP p50 | UDP p99 | UDP missing % | UDP ttfp p50/p99 | "
             "HLS p50 | HLS p99 | HLS missing % | HLS ttfa p50 |",
             "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]

    def get(block, *path):
        for key in path:
            if not isinstance(block, dict):
                return None
            block = block.get(key)
        return block

    def cell(value):
        return "-" if value is None else (f"{value:g}" if isinstance(value, float) else str(value))

    for step in result["steps"]:
        for site, region in sorted(step.get("regions", {}).items()):
            udp, hls = region.get("udp") or {}, region.get("hls") or {}
            lines.append(
                f"| {step['listeners']} | {site} | {cell(get(udp, 'latency_ms', 'p50'))} | "
                f"{cell(get(udp, 'latency_ms', 'p99'))} | {cell(udp.get('missing_percent'))} | "
                f"{cell(get(udp, 'ttfp_ms', 'p50'))}/{cell(get(udp, 'ttfp_ms', 'p99'))} | "
                f"{cell(get(hls, 'latency_ms', 'p50'))} | {cell(get(hls, 'latency_ms', 'p99'))} | "
                f"{cell(hls.get('missing_percent'))} | {cell(get(hls, 'ttfa_ms', 'p50'))} |")
    lines += ["", "| step | tier | nodes | CPU mean | CPU max | Mbit/s out per node | "
              "UDP rcvbuf drops |", "| ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
    for step in result["steps"]:
        for tier, item in sorted(step.get("tiers", {}).items()):
            lines.append(f"| {step['listeners']} | {tier} | {item['nodes']} | "
                         f"{cell(item['cpu_mean'])} | {cell(item['cpu_max'])} | "
                         f"{item['tx_bps_per_node'] / 1e6:.1f} | {item['udp_rcvbuf_drops']} |")
    lines += ["", "| step | UDP listeners per edge | public transfer GB | gave out first | "
              "stopped |", "| ---: | --- | ---: | --- | --- |"]
    for step in result["steps"]:
        first = step.get("gave_out_first")
        listeners = ", ".join(f"{name} {count}" for name, count
                              in sorted(step.get("edge_listeners", {}).items()))
        lines.append(f"| {step['listeners']} | {listeners or '-'} | "
                     f"{step.get('public_transfer_gb', 0)} | "
                     + (f"{first['tier']} ({first['node']}, {first['reason']})" if first
                        else "nothing")
                     + f" | {step.get('stopped') or '-'} |")
    lines += ["", "What a listener cost its listener host (median over hosts; vCPU per listener "
              "of a 1,000-part stream, process CPU plus NIC softirq; peak MB per listener):", "",
              "| step | UDP vCPU | UDP MB | LL-HLS vCPU | LL-HLS MB |",
              "| ---: | ---: | ---: | ---: | ---: |"]
    for step in result["steps"]:
        costs = step.get("measured_costs") or {}
        lines.append(f"| {step['listeners']} | {cell(costs.get('udp_vcpu'))} | "
                     f"{cell(costs.get('udp_mb'))} | {cell(costs.get('hls_vcpu'))} | "
                     f"{cell(costs.get('hls_mb'))} |")
    return "\n".join(lines) + "\n"


def cost_file(result: dict) -> dict | None:
    """The `--cost FILE` of a run: the measured costs of its largest step
    that measured both lanes, else of its largest that measured one."""
    best: dict = {}
    for step in result["steps"]:
        costs = step.get("measured_costs") or {}
        for key in ("udp_vcpu", "hls_vcpu"):
            if costs.get(key) is not None:
                best[key] = costs[key]
                best[f"{key}_step"] = step["listeners"]
    return best or None


# --------------------------------------------------------------- the lab

def state() -> dict:
    return common.load_state()


def deploy_settings() -> dict:
    path = common.ARTIFACT_DIR / "settings.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def scale_settings(standby: list[str]) -> dict:
    """The deploy settings of the scale run: readers on the VLAN (Linode does
    not bill it), PCM in the contributor's LL-HLS formats, the edges' UDP
    subscription cap and egress capacity above what a g6-dedicated-2 can send
    (4 Gbit/s), so that what gives out is the edge and not a setting, and
    the London edges of the second curve on standby (none with
    --second-curve)."""
    return {"playback_address": "vlan", "daw_hls_formats": "flac,opus,pcm",
            "client_udp_max_subscribers": 65536, "edge_egress_capacity_bps": 4_000_000_000,
            "edge_session_limit": 0, "standby_edges": standby}


def topology_state(path: pathlib.Path, without: list[str] = ()) -> dict:
    """A lab state for a topology file (made-up public addresses), for
    `plan --topology-only` and the tests; sites in `without` left out."""
    topology = json.loads(path.read_text())
    nodes = {}
    for index, node in enumerate(topology["nodes"], 1):
        if node["site"] in without:
            continue
        nodes[node["name"]] = {**{key: node.get(key) for key in (
            "name", "role", "site", "region", "type", "purpose")},
            "public_ipv4": f"192.0.2.{index}",
            "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
    return {"nodes": nodes, "standby_edges": topology.get("standby_edges", [])}


def check_vlan(state: dict, regions: dict, allow_public: bool) -> list[str]:
    """Every listener host and edge has a VLAN address (the listeners read
    there); the problems, and exit unless `allow_public`."""
    problems = [f"{node['name']} ({site}) has no VLAN address"
                for site, item in regions.items() for node in item["listeners"] + item["edges"]
                if not node.get("vlan_ipv4")]
    if problems and not allow_public:
        sys.exit("listeners would read over public addresses (billed transfer, about 12 TB an "
                 "hour at 5,000 UDP listeners of 1 ms PCM): " + "; ".join(problems)
                 + ". Give --allow-public-transfer to run anyway")
    return problems


def ssh(node: dict, command: str, timeout: int = 120, check: bool = False) -> str:
    return common.ssh_text(node, command, check=check, timeout=timeout)


def http_get(node: dict, path: str, timeout: float = 5.0):
    """GET https://<public ip>:19444<path> of a mesh node (the operator's
    address is open in the lab firewall), certificate not checked: JSON for
    /api/, text otherwise, {"error"} on failure."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(f"https://{node['public_ipv4']}:{HTTP_PORT}{path}",
                                    timeout=timeout, context=context) as response:
            body = response.read()
        return json.loads(body) if path.startswith("/api/") else body.decode(errors="replace")
    except Exception as error:  # noqa: BLE001 - one node's failure is recorded
        return {"error": str(error)}


def edge_counters(lab: dict, stream: int) -> dict:
    """The client UDP counters (GET /api/assignment) of every edge and
    distributor."""
    nodes = [node for node in lab["nodes"].values() if node["role"] in ("edge", "distributor")]
    results = {}

    def one(node):
        found = http_get(node, f"/api/assignment?stream={stream}")
        results[node["name"]] = found.get("client_udp") if isinstance(found, dict) else None
    threads = [threading.Thread(target=one, args=(node,)) for node in nodes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def counter_delta(before: dict, after: dict) -> dict:
    out = {}
    for name, now in after.items():
        then = before.get(name) or {}
        if not isinstance(now, dict):
            out[name] = None
            continue
        out[name] = {key: value - then.get(key, 0) for key, value in now.items()
                     if isinstance(value, (int, float)) and not isinstance(value, bool)
                     and isinstance(then.get(key, 0), (int, float))}
    return out


def wait_live(lab: dict, stream: int, timeout: float = 120) -> bool:
    dist = common.nodes_by_role(lab, "distributor")[0]
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = http_get(dist, f"/live/{stream}/stream.m3u8")
        if isinstance(body, str) and "#EXT-X-PART:" in body:
            return True
        time.sleep(2)
    return False


def standby_for(nodes: list[dict], standby: list[str], edges_per_region: int | None) -> list[str]:
    """`standby` and, with `edges_per_region`, every edge of a region past
    its first N in service (by name): fewer edges take more listeners each,
    so that an edge gives out before the listener hosts do."""
    out = list(standby)
    if not edges_per_region:
        return out
    by_site: dict[str, list[str]] = {}
    for node in nodes:
        if node["role"] == "edge" and node["name"] not in standby:
            by_site.setdefault(node["site"], []).append(node["name"])
    for names in by_site.values():
        names.sort(key=natural)
        out += names[edges_per_region:]
    return out


def command_settings(args: argparse.Namespace) -> None:
    topology = json.loads(pathlib.Path(args.topology).read_text())
    standby = standby_for(topology["nodes"],
                          [] if args.second_curve else topology.get("standby_edges", []),
                          args.edges_per_region)
    print(" ".join(f"--set {key}={json.dumps(value, separators=(',', ':'))}"
                   for key, value in scale_settings(standby).items()))


def build_plan(args: argparse.Namespace, lab: dict, standby: list[str]) -> dict:
    return plan_steps(lab, parse_steps(args.steps), args.config, args.hls_share,
                      args.hls_cap_per_host, standby, args.hold, args.ramp,
                      load_costs(args.cost), args.udp_mbps, args.inbound_gbps)


def command_plan(args: argparse.Namespace) -> dict:
    if args.topology_only:
        lab = topology_state(pathlib.Path(args.topology),
                             [site for site in args.without_sites.split(",") if site])
        standby = standby_for(list(lab["nodes"].values()),
                              [] if args.second_curve else lab["standby_edges"],
                              args.edges_per_region)
    else:
        lab = state()
        standby = list(deploy_settings().get("standby_edges") or [])
    plan = build_plan(args, lab, standby)
    plan["cost"] = cost_estimate(lab, plan)
    if args.json:
        print(json.dumps(plan, indent=1))
        return plan
    regions = ", ".join(f"{site} {len(item['edges'])} edges / {len(item['listeners'])} "
                        "listener hosts" for site, item in plan["regions"].items())
    costs = plan["costs"]
    print(f"{args.config}: {regions}; {costs['udp_vcpu']} vCPU per UDP listener, "
          f"{costs['hls_vcpu']} per LL-HLS reader at 1,000 parts/s, hosts filled to "
          f"{PLAN_HEADROOM:.0%}")
    for name, cap in sorted(plan["capacity"].items()):
        print(f"  {name:<12} {cap['vcpus']} vCPU: UDP {cap['udp']} (CPU {cap['udp_by_cpu']}, "
              f"network {cap['udp_by_network']}) or LL-HLS {cap['hls']}")
    for step in plan["steps"]:
        worst = max(step["host_load"].values()) if step["host_load"] else 0
        edges = "; ".join(f"{site[:3]} {item['listeners_per_edge']:g}/edge "
                          f"{item['edge_egress_mbps']:.0f} Mbit/s"
                          for site, item in sorted(step["sites"].items()))
        print(f"step {step['listeners']:>6}: UDP {step['udp']:>6}, LL-HLS {step['hls']:>5}"
              f"{' (clipped)' if step['hls_clipped'] else ''}; {edges}; busiest load host "
              f"{worst * 100:.0f}%{'' if step['drivable'] else ' <- OVER THE LOAD HOSTS'}; "
              f"{step['seconds'] / 60:.1f} min")
    cost = plan["cost"]
    print(f"ramp {cost['ramp_minutes']} min; lab ${cost['hourly_usd']}/h for {cost['hours']} h "
          f"with an hour of bring-up: ${cost['instances_usd']}; listener traffic "
          f"{cost['listener_traffic_gb']} GB (on the VLAN, not billed; "
          f"${cost['listener_traffic_usd_if_public']} were it public)")
    return plan


def remote_step_dir(run: str, step: int) -> str:
    return f"{REMOTE}/{run}/{step}"


def command_prepare(args: argparse.Namespace) -> None:
    """The audio loop on lon-src and on every listener host, and the
    readers' indexes made there by ntcmp. The listener hosts' kernel
    settings (ports, buffers, file limits) and tools are the deploy's
    (push: LISTENER_SYSCTLS, needletail-load, ntcmp)."""
    lab = state()
    source = common.nodes_for(lab, "source")[0]
    listeners = common.nodes_for(lab, "listeners")
    audio = pathlib.Path(args.audio_file)
    if not audio.is_file():
        sys.exit(f"missing: {audio}")
    remote_audio = f"{REMOTE_MEDIA}/audio/audio.s24le"

    def one(node: dict) -> str:
        ssh(node, f"mkdir -p {REMOTE} {REMOTE_MEDIA}/audio {REMOTE_MEDIA}/index", check=True)
        common.rsync_to(node, [str(audio)], remote_audio, timeout=1800)
        if node["name"] == source["name"]:
            return "audio"
        made = []
        for config in CONFIGS.values():
            name, spec = config["index"]
            spec = [remote_audio if item == "@audio" else item for item in spec]
            made.append(f"[ -f {REMOTE_MEDIA}/index/{name}.index.json ] || ntcmp audio-index "
                        f"{shlex.quote(spec[0])} {REMOTE_MEDIA}/index/{name}.index.json "
                        + " ".join(shlex.quote(item) for item in spec[1:]))
        ssh(node, " && ".join(made), timeout=600, check=True)
        return ssh(node, f"{LOAD_PROCESS} --help 2>&1 | head -1; ntcmp 2>&1 | head -1; "
                         f"ls {REMOTE_MEDIA}/index | tr '\\n' ' '")

    for name, out in sorted(common.parallel([source] + listeners, one).items()):
        print(f"{name:<12} {out}")


def stop_listeners(lab: dict) -> None:
    hosts = common.nodes_for(lab, "listeners")
    if hosts:
        common.parallel(hosts, lambda node: ssh(
            node, f"pkill -INT -x {LOAD_PROCESS}; pkill -INT -x ntcmp; sleep 3; "
                  f"pkill -KILL -x {LOAD_PROCESS}; pkill -KILL -x ntcmp; true"))


def command_stop(_args: argparse.Namespace) -> None:
    lab = state()
    stop_listeners(lab)
    for node in common.nodes_for(lab, "source"):
        ssh(node, "pkill -x aep1-48k-probe; true")
    common.parallel(list(lab["nodes"].values()),
                    lambda node: ssh(node, f"pkill -f {REMOTE}/sampler.py; true"))
    print("stopped the sources, the listeners and the samplers")


def monitor(hosts: list[dict], remote_dir: str, until: float, poll: float,
            log) -> tuple[str | None, str | None]:
    """Watch the listener hosts until `until`; (host, why) of the first that
    saturates, or (None, None)."""
    while time.time() < until:
        time.sleep(min(poll, max(0.0, until - time.time())))
        tails = common.parallel(hosts, lambda node: ssh(
            node, f"tail -n {SATURATION_HOLD + 2} {remote_dir}/sampler.jsonl 2>/dev/null; "
                  f"echo ==; tail -n {SATURATION_HOLD} {remote_dir}/{LOAD_DIR}/intervals.jsonl "
                  "2>/dev/null", timeout=30))
        for name, text in sorted(tails.items()):
            sampled, _, intervals = text.partition("==")
            why = load_saturated(sampler_rows(sampled)[1], interval_rows(read_jsonl(intervals)))
            if why:
                return name, why
        log({"monitor": round(time.time()), "load_cpu": {
            name: (sampler_rows(text.partition("==")[0])[1] or [{"cpu": None}])[-1]["cpu"]
            for name, text in sorted(tails.items())}})
    return None, None


def step_commands(lab: dict, plan: dict, step: dict, run: str, args) -> dict[str, str]:
    """The shell command of each listener host for one step."""
    regions = regions_of(lab, plan.get("standby") or [])
    remote_dir = remote_step_dir(run, step["listeners"])
    duration = step["ramp_s"] + step["hold_s"]
    commands = {}
    for site, by_host in step["allocation"].items():
        edges = [f"{node['vlan_ipv4']}:{CLIENT_UDP_PORT}" for node in regions[site]["edges"]]
        entry = regions[site]["distributors"][0]["vlan_ipv4"]
        for name, alloc in by_host.items():
            label = f"{run}-{step['listeners']}-{name}"
            # One line each: every tool ends in `&`, and `&;` is a syntax
            # error of the shell.
            commands[name] = "\n".join(part for part in (
                f"mkdir -p {remote_dir}",
                load_command(alloc, edges, args.config, step["ramp_s"], duration, args.sample,
                             remote_dir, label, lab["nodes"][name].get("vlan_ipv4"),
                             args.load_args, vcpus(lab["nodes"][name])),
                *hls_commands(alloc, entry, args.config, step["ramp_s"], duration, remote_dir,
                              label, args.hls_window, not args.hls_http2, args.hls_steering))
                if part)
    return commands


def run_step(lab: dict, run: str, plan: dict, step: dict, args: argparse.Namespace,
             directory: pathlib.Path, log) -> dict:
    remote_dir = remote_step_dir(run, step["listeners"])
    every = list(lab["nodes"].values())
    duration = step["ramp_s"] + step["hold_s"]
    common.parallel(every, lambda node: ssh(
        node, sampler_command(node, remote_dir, duration + 60), check=True))
    before = edge_counters(lab, CONFIGS[args.config]["stream"])
    commands = step_commands(lab, plan, step, run, args)
    step_dir = directory / "steps" / str(step["listeners"])
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "commands.json").write_text(json.dumps(commands, indent=1) + "\n")
    hosts = [lab["nodes"][name] for name in commands]
    started = time.time()
    common.parallel(hosts, lambda node: ssh(node, commands[node["name"]], check=True))
    log({"step": step["listeners"], "started": round(started), "udp": step["udp"],
         "hls": step["hls"], "seconds": duration})
    saturated, why = monitor(hosts, remote_dir, started + duration + 5, args.poll, log)
    stopped = None
    if saturated:
        stopped = f"{saturated}: {why}"
        log({"step": step["listeners"], "stopped": stopped})
    else:
        # The tools end at their duration and drain; give them time to write.
        deadline = time.time() + 60
        while time.time() < deadline:
            busy = common.parallel(hosts, lambda node: ssh(
                node, f"pgrep -c -x {LOAD_PROCESS}; pgrep -c -x ntcmp", timeout=30))
            if not any(int(line) for text in busy.values() for line in text.split()
                       if line.strip().isdigit()):
                break
            time.sleep(3)
    stop_listeners(lab)
    ended = time.time()
    after = edge_counters(lab, CONFIGS[args.config]["stream"])
    common.parallel(hosts, lambda node: ssh(
        node, f"[ -d {remote_dir}/hls ] && python3 - {remote_dir} <<'PY'\n{HLS_CONDENSE}\nPY\n"
              "true", timeout=300))

    def fetch(node: dict) -> None:
        local = step_dir / node["name"]
        local.mkdir(exist_ok=True)
        files = ["sampler.jsonl"]
        if node["name"] in commands:
            # needletail-load's directory (intervals, summary, the sampled
            # listeners' arrivals), the readers' condensed reports, errors.
            files += [LOAD_DIR, "udp.err", "hls.condensed.json"]
        for name in files:
            try:
                common.rsync_from(node, f"{remote_dir}/{name}", str(local) + "/", timeout=600)
            except RuntimeError:
                pass

    common.parallel(every, fetch)
    (step_dir / "counters.json").write_text(json.dumps(counter_delta(before, after), indent=1)
                                            + "\n")
    result = analyse_step(lab, step_dir, step,
                          (started + step["ramp_s"], min(ended, started + duration)), args.config)
    result["stopped"] = stopped
    result["ramp_s"], result["hold_s"] = step["ramp_s"], step["hold_s"]
    (step_dir / "step.json").write_text(json.dumps(result, indent=1) + "\n")
    return result


def command_run(args: argparse.Namespace) -> None:
    lab = state()
    settings = deploy_settings()
    standby = list(settings.get("standby_edges") or [])
    plan = build_plan(args, lab, standby)
    regions = regions_of(lab, standby)
    check_vlan(lab, regions, args.allow_public_transfer)
    if settings.get("playback_address") != "vlan" and not args.allow_public_transfer:
        sys.exit("the deploy's playback_address is not vlan: LL-HLS readers would be steered to "
                 "public addresses (billed). Deploy with `scale-run.py settings`")
    undrivable = [step["listeners"] for step in plan["steps"] if not step["drivable"]]
    if undrivable and not args.force:
        sys.exit(f"steps {undrivable} need more than the listener hosts can drive "
                 "(scale-run.py plan): drop them, give --hls-cap-per-host or --cost, or --force")
    run = args.name or time.strftime("%m%d%H%M", time.gmtime()) + f"-{args.config}"
    directory = OUT / f"scale-{run}"
    directory.mkdir(parents=True, exist_ok=True)
    plan["cost"] = cost_estimate(lab, plan)
    (directory / "plan.json").write_text(json.dumps(plan, indent=1) + "\n")
    events = open(directory / "events.jsonl", "a")

    def log(item: dict) -> None:
        events.write(json.dumps(item) + "\n")
        events.flush()
        print(json.dumps(item), flush=True)

    source = common.nodes_for(lab, "source")[0]
    contributor = common.nodes_by_role(lab, "contributor")[0]
    ssh(source, f"mkdir -p {REMOTE}; pkill -x aep1-48k-probe; "
                + sender_command(args.config, contributor.get("vlan_ipv4")
                                 or contributor["public_ipv4"], int(plan["seconds"] + 900),
                                 f"{REMOTE_MEDIA}/audio/audio.s24le"), check=True)
    stream = CONFIGS[args.config]["stream"]
    # The loop starts at the next 64 s boundary at least 8 s away.
    start = ssh(source, f"cat {REMOTE}/source.start").strip()
    time.sleep(max(0.0, (int(start) if start.isdigit() else time.time()) - time.time() + 3))
    if not wait_live(lab, stream):
        sys.exit(f"stream {stream} is not live at "
                 f"{common.nodes_by_role(lab, 'distributor')[0]['name']}")
    result = {"run": run, "config": args.config, "hls_share": args.hls_share,
              "standby_edges": standby, "steps": [],
              "lab": {name: {key: node.get(key) for key in ("role", "site", "type", "purpose")}
                      for name, node in lab["nodes"].items()}}
    try:
        for step in plan["steps"]:
            outcome = run_step(lab, run, plan, step, args, directory, log)
            result["steps"].append(outcome)
            write_results(directory, result)
            first = outcome.get("gave_out_first")
            log({"step": step["listeners"], "gave_out_first": first,
                 "stopped": outcome["stopped"]})
            if outcome["stopped"]:
                log({"ramp": "ends: a listener host saturated"})
                break
            if first and not args.keep_going:
                log({"ramp": f"ends: {first['tier']} {first['node']} gave out "
                             f"({first['reason']})"})
                break
            time.sleep(args.settle)
    finally:
        stop_listeners(lab)
        ssh(source, "pkill -x aep1-48k-probe; true")
        events.close()
    print(directory / "summary.md")


def write_results(directory: pathlib.Path, result: dict) -> None:
    """scale.json, summary.md and cost.json (the measured costs, for the
    next run's --cost) of a run."""
    (directory / "scale.json").write_text(json.dumps(result, indent=1) + "\n")
    (directory / "summary.md").write_text(format_summary(result))
    costs = cost_file(result)
    if costs:
        (directory / "cost.json").write_text(json.dumps(costs, indent=1) + "\n")


def run_directory(name: str) -> pathlib.Path:
    path = pathlib.Path(name)
    return path if path.is_dir() else OUT / f"scale-{name}"


def command_analyse(args: argparse.Namespace) -> None:
    directory = run_directory(args.run)
    plan = json.loads((directory / "plan.json").read_text())
    previous = json.loads((directory / "scale.json").read_text())
    lab = (topology_state(pathlib.Path(args.topology)) if args.topology_only else state())
    steps = []
    for step, old in zip(plan["steps"], previous["steps"]):
        outcome = analyse_step(lab, directory / "steps" / str(step["listeners"]), step,
                               tuple(old["window"]), plan["config"])
        outcome["stopped"] = old.get("stopped")
        outcome["ramp_s"], outcome["hold_s"] = step["ramp_s"], step["hold_s"]
        steps.append(outcome)
    previous["steps"] = steps
    write_results(directory, previous)
    print((directory / "summary.md").read_text())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def planning(item):
        item.add_argument("--config", choices=sorted(CONFIGS), default="pcm-1")
        item.add_argument("--steps", default=DEFAULT_STEPS)
        item.add_argument("--hls-share", type=float, default=0.5,
                          help="share of each step's listeners that are LL-HLS readers")
        item.add_argument("--hls-cap-per-host", type=int, default=None,
                          help="LL-HLS readers on one listener host at most; the rest UDP")
        item.add_argument("--hold", type=float, default=60.0, help="seconds each step holds")
        item.add_argument("--ramp", type=float, default=None,
                          help="seconds over which a step's listeners join (default by count)")
        item.add_argument("--cost", default=None,
                          help='JSON {"udp_vcpu": ..., "hls_vcpu": ...}: vCPU per listener at '
                               "1,000 parts/s (needletail-load's bench)")
        item.add_argument("--udp-mbps", type=float, default=None,
                          help="wire rate of one UDP listener (default per config)")
        item.add_argument("--inbound-gbps", type=float, default=LOAD_HOST_INBOUND_GBPS)
        item.add_argument("--topology", default=str(TOPOLOGY))

    settings = sub.add_parser("settings", help="the deploy's --set arguments")
    settings.add_argument("--topology", default=str(TOPOLOGY))
    settings.add_argument("--second-curve", action="store_true",
                          help="no standby edge: London's extra edges in")
    settings.add_argument("--edges-per-region", type=int, default=None,
                          help="the first N edges of each region in service, the rest standby")
    settings.set_defaults(handler=command_settings)
    plan = sub.add_parser("plan", help="allocation, capacity, duration and cost of the steps")
    planning(plan)
    plan.add_argument("--topology-only", action="store_true",
                      help="plan from the topology file, without a lab")
    plan.add_argument("--without-sites", default="", help="with --topology-only")
    plan.add_argument("--second-curve", action="store_true", help="with --topology-only")
    plan.add_argument("--edges-per-region", type=int, default=None,
                      help="with --topology-only: the first N edges of each region")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(handler=command_plan)
    prepare = sub.add_parser("prepare", help="the loop and the indexes on the hosts")
    prepare.add_argument("--audio-file", required=True,
                         help="the 64 s loop, s24le stereo 48 kHz (compare-media/audio/audio.s24le)")
    prepare.set_defaults(handler=command_prepare)
    run = sub.add_parser("run", help="the ramp")
    planning(run)
    run.add_argument("--name", default=None, help="the run's name (default: time and config)")
    run.add_argument("--sample", type=int, default=20,
                     help="listeners per host whose every part arrival is kept")
    run.add_argument("--load-args", default="", help="extra arguments of needletail-load")
    run.add_argument("--hls-window", type=int, default=40)
    run.add_argument("--hls-http2", action="store_true", help="LL-HLS over HTTP/2, not HTTP/3")
    run.add_argument("--hls-steering", choices=("spread", "steered"), default="spread",
                     help="readers pinned evenly to the edges (cache=), or left to steering")
    run.add_argument("--poll", type=float, default=5.0)
    run.add_argument("--settle", type=float, default=15.0)
    run.add_argument("--keep-going", action="store_true",
                     help="run the next step after a tier gave out")
    run.add_argument("--force", action="store_true",
                     help="run steps that the plan says the listener hosts cannot drive")
    run.add_argument("--allow-public-transfer", action="store_true")
    run.set_defaults(handler=command_run)
    analyse = sub.add_parser("analyse", help="aggregate a run's files again")
    analyse.add_argument("run", help="the run's name or directory")
    analyse.add_argument("--topology", default=str(TOPOLOGY))
    analyse.add_argument("--topology-only", action="store_true")
    analyse.set_defaults(handler=command_analyse)
    sub.add_parser("stop", help="stop sources, listeners, samplers").set_defaults(
        handler=command_stop)
    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
