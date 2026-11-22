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
  scale-run.py run --phase-plan deploy/low-latency/phases-scale.json --group F|R|A
                                           failover, redistribution, autoscale:
                                           one held step, the scenarios' faults
                                           at their times, each metric before,
                                           during and after (phases.json,
                                           events.jsonl with unix_us, the
                                           "Phases" table of summary.md)

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
import signal
import ssl
import subprocess
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
# The warm-up of a copy that a listener moves onto (deploy cache_mesh_warm_ms;
# low-latency ops D6): a moved listener resumes from the part after its last,
# which is the client's silence detection (1 s, client_session.rs) plus the
# move behind the head; the failover criteria bound a move at 3 s (F2's
# max), and half a second of margin. 4,500 parts of the shortest (1 ms)
# part, every codec at least that long; Opus of 2.5 ms gets 11.25 s.
SILENCE_MS = 1000
MOVE_BOUND_MS = 3000
WARM_MARGIN_MS = 500
WARM_MS = SILENCE_MS + MOVE_BOUND_MS + WARM_MARGIN_MS
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
             hls_cap_per_host: int | None = None, caps: dict[str, int] | None = None) -> dict:
    """{site: {host: {"udp": n, "hls": n, "hls_by_edge": {edge: n}}}} for a
    step of `total` listeners: over the regions in proportion to their
    edges, in a region over its listener hosts in proportion to their vCPUs,
    `hls_share` of a region's listeners LL-HLS readers, and a host's readers
    evenly over the region's edges (each host starting at the next edge, so
    that the region's readers spread evenly). With `hls_cap_per_host` a host
    runs at most that many readers and the rest of its share is UDP. With
    `caps` ({site: n}) a region takes at most n listeners (what its listener
    hosts drive, fit_caps) and the others keep their share."""
    if not 0.0 <= hls_share <= 1.0:
        raise ValueError("hls_share is a fraction")
    sites = sorted(regions)
    per_site = split(total, [len(regions[site]["edges"]) for site in sites])
    if caps:
        per_site = [min(count, caps.get(site, count)) for site, count in zip(sites, per_site)]
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


def drivable(alloc: dict, capacity: dict) -> bool:
    return all(host_load(item, capacity[name]) <= 1.0
               for by_host in alloc.values() for name, item in by_host.items())


def fit_caps(regions: dict, capacity: dict, hls_share: float,
             hls_cap_per_host: int | None = None, allocate_with=None) -> dict[str, int]:
    """{site: the most listeners that the region's listener hosts drive}
    within the plan's headroom (each host's load at most 1.0), for the
    step's share of LL-HLS: the largest count whose allocation over the
    region alone is drivable (a binary search; the load rises with the
    count)."""
    allocate_with = allocate_with or (lambda total, one: allocate(total, one, hls_share,
                                                                  hls_cap_per_host))
    caps = {}
    for site, region in regions.items():
        one = {site: region}
        low, high = 0, 1
        while drivable(allocate_with(high, one), capacity) and high < 10_000_000:
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if drivable(allocate_with(middle, one), capacity):
                low = middle
            else:
                high = middle
        caps[site] = low
    return caps


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
               inbound_gbps: float = LOAD_HOST_INBOUND_GBPS, fit: bool = False) -> dict:
    """The allocation, the capacity check, the edges' planned egress and the
    duration of every step. With `fit` (a lab whose account refused some
    listener hosts: `listeners_missing` in its state, or --fit-hosts), each
    region takes at most what its listener hosts drive (fit_caps), a step
    is named by the listeners it runs ("requested": what was asked), and a
    step that would run no more than the one before is dropped."""
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
    caps = None
    if fit:
        caps = fit_caps(regions, out["capacity"], hls_share, hls_cap_per_host)
        out["fit"] = {"caps": caps, "dropped": [],
                      "listeners_missing": [item.get("name") for item in
                                            state.get("listeners_missing") or []]}
    total_s = 0.0
    previous = 0
    for requested in steps:
        alloc = allocate(requested, regions, hls_share, hls_cap_per_host, caps)
        uncapped = allocate(requested, regions, hls_share, None, caps)
        total = sum(item["udp"] + item["hls"] for by_host in alloc.values()
                    for item in by_host.values())
        if fit and total <= previous:
            out["fit"]["dropped"].append(requested)
            continue
        previous = total
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
            "listeners": total, "requested": requested, "udp": udp, "hls": hls,
            **({"clipped_regions": {site: caps[site] for site, count in zip(
                sorted(regions), split(requested, [len(regions[site]["edges"])
                                                   for site in sorted(regions)]))
                if count > caps[site]}} if fit else {}),
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
                 bind: str | None = None, extra: str = "", threads: int | None = None,
                 ramp: str | None = None) -> str:
    """needletail-load of one listener host for one step (empty when it runs
    no UDP listener). `edges`: the region's edges' VLAN host:port (a
    listener starts at edge i mod n and follows MOVE as the real client
    does); `bind`: the host's VLAN address for the sockets; `threads`: its
    workers, one per vCPU."""
    if not alloc["udp"]:
        return ""
    return (f"ulimit -n 1048576; nohup {LOAD_PROCESS} "
            f"--edges {','.join(edges)} --stream {CONFIGS[config]['stream']} "
            f"--listeners {alloc['udp']} --ramp {ramp or ramp_spec(alloc['udp'], ramp_s)} "
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
    "moves": ("moves",),
    # Listeners that exist (subscribed or not) and needletail-load's own
    # gauge of listeners with no part for 5 s (Phase AM item 10), when the
    # tool gives them.
    "active": ("active", "listeners_active"),
    "stranded": ("stranded",),
    "never_resumed": ("never_resumed",),
}


# Fields that are None, not 0, when the load tool does not give them.
OPTIONAL_FIELDS = ("active", "stranded", "never_resumed")


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
                        **{key: (pick(item, *names) if key in OPTIONAL_FIELDS
                                 else pick(item, *names, default=0) or 0)
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


def excluded(exclude: dict | None, name: str, at: float | None) -> bool:
    """Whether an event of `name` at `at` falls under a fault (`exclude`:
    {"windows": [(start, end)] of the faults and their observation,
    "node_windows": {name: its own faults' windows}}): any node while a
    fault is being observed, or a faulted node within its own fault, did
    not give out by load. A faulted node outside its fault is judged like
    any other; an event with no time is left out only for a faulted node."""
    if not exclude:
        return False
    own = (exclude.get("node_windows") or {}).get(name) or []
    if at is None:
        return bool(own)
    return any(start <= at <= end for start, end in list(exclude.get("windows") or ()) + own)


def gave_out_first(state: dict, nodes: dict[str, dict], edges_missing: dict[str, float | None],
                   load_events: dict[str, float | None],
                   exclude: dict | None = None) -> dict | None:
    """The first tier that gave out in a step: the earliest of a mesh node's
    or a listener host's saturation (`nodes`: summarize_samples of each), an
    edge whose UDP listeners missed more than MISSING_GAVE_OUT of the parts
    of an interval (`edges_missing`: its first such time), and a listener
    host that needletail-load or the monitor saw saturate (`load_events`).
    Events under a fault (`exclude`, see excluded) are left out: a phase
    run's kill is not a tier giving out. None when nothing gave out."""
    events = []
    for name, summary in nodes.items():
        node = state["nodes"].get(name)
        if node is None or summary.get("saturated_at") is None:
            continue
        tier = tier_of(node)
        if tier in ("contributor", "forwarder", "edge", "load") and not (
                tier != "load" and excluded(exclude, name, summary["saturated_at"])):
            events.append((summary["saturated_at"], tier, name, summary["saturated_by"]))
    for name, at in edges_missing.items():
        if at is not None and not excluded(exclude, name, at):
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
                 config: str = "pcm-1", exclude: dict | None = None) -> dict:
    """Aggregate one step's files: steps/STEP/HOST/{sampler.jsonl,
    udp/intervals.jsonl, udp/summary.json, hls.condensed.json} and
    counters.json. `window`: (end of the ramp, end of the step), unix s.
    `exclude`: the faults of a phase run (see excluded), which do not
    count as a tier giving out."""
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
                    and missing / (parts + missing) > MISSING_GAVE_OUT
                    and not excluded(exclude, name, float(t))):
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
            "gave_out_first": gave_out_first(state, nodes, edges_missing, load_events, exclude)}


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


# ------------------------------------------------------------------ phases
#
# Failover, redistribution and autoscale phases (low-latency ops, items 4
# and 5): one held step of listeners, and within it a timed list of
# scenarios, each a baseline, a fault (or a join, or the controller left to
# act), an observation and a settle. deploy/low-latency/phases-scale.json
# is the plan. Every action and what follows it goes to events.jsonl with
# Unix-µs timestamps (referee R3); the analysis splits every metric into
# before, during and after each fault.

PHASE_SCHEMA = "needletail.scale.phases.v1"
PHASES = ROOT / "deploy" / "low-latency" / "phases-scale.json"
ACTION_KINDS = ("kill", "crash", "drain", "resume", "stop", "start", "join", "leave", "add",
                "remove", "autoscale")
# What puts a fault back after its observation, when the scenario names no
# restore of its own. A crash restarts by itself (Restart=always, 2 s). An
# added slot is only stopped (leave), never deleted while listeners may
# still be on it: `down` deletes it, or a scenario's own drain then remove.
RESTORE_OF = {"kill": "start", "drain": "resume", "stop": "start", "join": "leave",
              "add": "leave"}
# How long a scale run waits for the previous scenario's controller to exit
# before it starts the next (SIGINT then, and it resumes its drains), and
# for a controller stopped with SIGINT before SIGTERM, then SIGKILL.
CONTROLLER_WAIT_S = 300
CONTROLLER_STOP_S = 90
# The service commands of a node: a kill is SIGKILL and then the unit
# stopped, or Restart=always would bring the node back in 2 s (D4); a
# crash is the SIGKILL alone, the 2 s restart.
NODE_COMMANDS = {
    "kill": "systemctl kill -s KILL needletail-mesh.service; "
            "systemctl stop needletail-mesh.service; true",
    "crash": "systemctl kill -s KILL needletail-mesh.service; true",
    "stop": "systemctl stop needletail-mesh.service",
    "start": "systemctl start needletail-mesh.service",
}
# A listener with no part for this long, from this long after the fault,
# is stranded (ops design 2).
STRANDED_GAP_S = 5
STRANDED_AFTER_S = 10


def unix_us() -> int:
    return time.time_ns() // 1000


def load_phase_plan(path: str | pathlib.Path, group: str | None = None,
                    names: list[str] | None = None) -> dict:
    """The phase plan, checked, with the scenarios of `group` (F, R, A or
    None for every one) or of `names`, in their order."""
    plan = json.loads(pathlib.Path(path).read_text())
    if plan.get("schema") != PHASE_SCHEMA:
        raise ValueError(f"{path}: schema is not {PHASE_SCHEMA}")
    seen = set()
    for scenario in plan["scenarios"]:
        name = scenario["name"]
        if name in seen:
            raise ValueError(f"scenario {name} twice")
        seen.add(name)
        if not scenario.get("actions"):
            raise ValueError(f"{name}: no action")
        for action in scenario["actions"] + scenario.get("restore", []):
            if action.get("kind") not in ACTION_KINDS:
                raise ValueError(f"{name}: action {action.get('kind')!r} is not one of "
                                 f"{', '.join(ACTION_KINDS)}")
            if action["kind"] != "autoscale" and not (
                    isinstance(action.get("targets"), list) and action["targets"]
                    and all(isinstance(item, str) and item for item in action["targets"])):
                raise ValueError(f"{name}: {action['kind']} needs a list of targets")
            if float(action.get("at_s", 0)) < 0:
                raise ValueError(f"{name}: at_s is negative")
        # A slot is deleted only after a drain of it earlier in the same
        # scenario (and the run checks at run time that the drain ended).
        for action in scenario["actions"] + scenario.get("restore", []):
            if action["kind"] != "remove":
                continue
            at = float(action.get("at_s", 0)) if action in scenario["actions"] else math.inf
            drained = {target for other in scenario["actions"] if other["kind"] == "drain"
                       and float(other.get("at_s", 0)) < at for target in other["targets"]}
            if set(action["targets"]) - drained:
                raise ValueError(f"{name}: remove of {sorted(set(action['targets']) - drained)} "
                                 "with no drain of it earlier in the scenario")
        for point in scenario.get("profile") or []:
            if len(point) != 2 or float(point[0]) < 0 or not 0 <= float(point[1]) <= 1:
                raise ValueError(f"{name}: a profile point is [seconds, share of the step]")
    # A scenario that is not enabled (it waits for Phase AM work) runs only
    # when it is named.
    chosen = [scenario for scenario in plan["scenarios"]
              if (scenario["name"] in (names or ()))
              or (not names and scenario.get("enabled", True)
                  and (group is None or scenario.get("group") == group))]
    unknown = sorted(set(names or ()) - seen)
    if unknown:
        raise ValueError(f"no scenario {unknown} in {path}")
    if not chosen:
        raise ValueError(f"no scenario of group {group!r} in {path}")
    return {**plan, "scenarios": chosen}


def phase_timeline(plan: dict) -> dict:
    """When each scenario's parts happen, in seconds from the start of the
    hold (after the listeners' ramp): "base_start", "fault" (the first
    action), "observe_end" (the restore), "end" (after the settle), the
    actions and restores with their "t", and the scenario's fault
    targets. "hold_s": the whole hold."""
    defaults = plan.get("defaults") or {}
    t = 0.0
    rows = []
    for scenario in plan["scenarios"]:
        def value(key, fallback):
            return float(scenario.get(key, defaults.get(key, fallback)))

        baseline, observe, settle = (value("baseline_s", 60), value("observe_s", 90),
                                     value("settle_s", 30))
        fault = t + baseline
        actions = [{**action, "t": fault + float(action.get("at_s", 0)),
                    "scenario": scenario["name"]} for action in scenario["actions"]]
        last = max(action["t"] for action in actions)
        observe_end = max(fault + observe, last + float(scenario.get("after_last_s", 0)))
        restore = scenario.get("restore")
        if restore is None:
            restore = [{"kind": RESTORE_OF[action["kind"]], "targets": action["targets"]}
                       for action in scenario["actions"] if action["kind"] in RESTORE_OF]
        restores = [{**item, "t": observe_end, "scenario": scenario["name"], "restore": True}
                    for item in restore]
        end = observe_end + settle
        targets = sorted({target for action in scenario["actions"]
                          if action["kind"] in ("kill", "crash", "drain", "stop", "leave",
                                                "remove")
                          for target in action.get("targets", [])})
        profile = [[fault + float(at), float(share)] for at, share in scenario.get("profile") or []]
        rows.append({"name": scenario["name"], "group": scenario.get("group"),
                     "profile": profile,
                     "base_start": t, "fault": fault, "observe_end": observe_end, "end": end,
                     "actions": actions, "restores": restores, "targets": targets,
                     "joins": sorted({target for action in scenario["actions"]
                                      if action["kind"] in ("join", "add", "start")
                                      for target in action.get("targets", [])})})
        t = end
    return {"scenarios": rows, "hold_s": t}


def profile_ramp(listeners: int, ramp_s: float, timeline: dict | None,
                 step: float = 30.0) -> str:
    """--ramp of needletail-load for a phase run: up to the first profile's
    share over the step's ramp, then each scenario's profile (shares of
    `listeners`, linear between its points, a point every `step` seconds),
    each held until the next. Without a profile, ramp_spec."""
    points = [point for row in (timeline or {}).get("scenarios", [])
              for point in row.get("profile") or []]
    if not points:
        return ramp_spec(listeners, ramp_s)
    points.sort()
    first = points[0][1]
    spec = [(ramp_s * stage / RAMP_STAGES, int(listeners * first * (stage + 1) / RAMP_STAGES))
            for stage in range(RAMP_STAGES)]
    for (t0, a), (t1, b) in zip(points, points[1:]):
        count = max(1, int((t1 - t0) // step))
        for index in range(count):
            at = t0 + (t1 - t0) * index / count
            spec.append((ramp_s + at, int(round(listeners * (a + (b - a) * index / count)))))
    spec.append((ramp_s + points[-1][0], int(round(listeners * points[-1][1]))))
    out, last_t = [], -1.0
    for at, count in sorted(spec):
        if at <= last_t:
            out[-1] = (out[-1][0], count)
            continue
        out.append((at, count))
        last_t = at
    return ",".join(f"{count}@{at:g}" for at, count in out)


def fault_nodes(timeline: dict) -> set[str]:
    return {name for row in timeline["scenarios"] for name in row["targets"]}


def exclusion(timeline: dict, start: float) -> dict:
    """The faults of a phase run in Unix time, for gave_out_first: each
    scenario that faults a node (a kill, crash, drain, stop, leave or
    remove), from its fault to the end of its settle, for every node; and
    each faulted node within its own scenario's window only. A scenario
    that faults nothing (an autoscale ramp, a join) excludes nothing: an
    edge that saturates as the load ramps is exactly what it must report."""
    windows, by_node = [], {}
    for row in timeline["scenarios"]:
        if not row["targets"]:
            continue
        window = (start + row["fault"], start + row["end"])
        windows.append(window)
        for name in row["targets"]:
            by_node.setdefault(name, []).append(window)
    return {"windows": windows, "node_windows": {name: by_node[name] for name in sorted(by_node)}}


def entry_nodes(region: dict, avoid: set[str]) -> list[dict]:
    """The entry points of a region that no phase faults: its distributors,
    then its edges. LL-HLS readers enter there, and the stream's liveness is
    read there (critique 9: not the lab's first distributor whatever)."""
    nodes = [node for node in region["distributors"] + region["edges"]
             if node["name"] not in avoid and node.get("vlan_ipv4")]
    if not nodes:
        raise ValueError("every distributor and edge of a region is under a fault: "
                         "no entry point is left")
    return nodes


def reader_commands(alloc: dict, entries: list[str], config: str, duration_s: float,
                    remote_dir: str, label: str) -> list[str]:
    """LL-HLS readers that follow steering and move when their cache fails
    or drains (item 5): needletail-reader with the region's entry points
    that no phase faults (`entries`: VLAN addresses), all of them starting
    at once (their moves' `at_ms` are from the launch, which the run logs).
    Its report: per reader the moves, stalls, parts missing."""
    if not alloc["hls"]:
        return []
    stream = CONFIGS[config]["stream"]
    entry_args = " ".join(f"--entry https://{entry}:{HTTP_PORT}" for entry in entries)
    return [f"mkdir -p {remote_dir}/hls; nohup needletail-reader {entry_args} "
            f"--stream {stream} --session {shlex.quote(label)} --readers {alloc['hls']} "
            f"--duration-seconds {int(round(duration_s))} --tls-ca {CA} "
            f"--server-name {SERVER_NAME} --steering --report {remote_dir}/hls/steered.json "
            f">{remote_dir}/hls-steered.out 2>{remote_dir}/hls-steered.err </dev/null &"]


# The mesh sampler of every edge and distributor in a phase run: once a
# second, its own /api/mesh/local, /api/assignment and /metrics, condensed:
# the client UDP counters (and the live gauges of Phase AM item 8 when the
# node gives them), the cache replication counters, the warm-up backlog of
# the copies it serves, hole repair and gap skips, its own advertisement,
# and the av-mesh counters of /metrics that the analysis reads.
MESH_SAMPLER = r"""
import json, re, ssl, sys, time, urllib.request
out, seconds, stream, name = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
context = ssl.create_default_context(); context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
KEEP = re.compile(r"^av_mesh_(edge_hole_repair|canonical|fec_rx_(presumed_lost|expired)|"
                  r"replica_queue|peer_limit_drops|rx_evicted|warm_|hole_parts|backfill|"
                  r"client_udp)")
def get(path):
    try:
        with urllib.request.urlopen("https://127.0.0.1:19444" + path, timeout=0.8,
                                    context=context) as response:
            return response.read()
    except Exception:
        return None
def numbers(item):
    return {key: value for key, value in (item or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}
handle = open(out, "w")
deadline = time.time() + seconds
while time.time() < deadline:
    time.sleep(1.0 - (time.time() % 1.0))
    row = {"t": round(time.time(), 3), "node": name}
    body = get("/api/mesh/local")
    if body:
        try:
            local = json.loads(body)
        except ValueError:
            local = {}
        replication = local.get("cache_replication") or {}
        row["cache_replication"] = numbers(replication)
        served = replication.get("served_subscriptions") or []
        pending = [item.get("warm_parts_pending") for item in served if isinstance(item, dict)]
        row["served_subscriptions"] = len(served)
        row["warm_parts_pending"] = (sum(value for value in pending if isinstance(value, int))
                                     if any(isinstance(value, int) for value in pending)
                                     else None)
        for key in ("canonical_gap_skip", "edge_hole_repair_requests", "backfill_parts_pending"):
            if isinstance(local.get(key), (int, float)):
                row[key] = local[key]
        edge = next((item for item in local.get("edge_services") or []
                     if item.get("node_id") == name), {})
        row["sessions"] = edge.get("active_playback_sessions")
        row["load_permille"] = edge.get("load_permille")
        row["draining"] = edge.get("draining")
        row["rejected"] = edge.get("egress_rejected_requests")
    body = get(f"/api/assignment?stream={stream}")
    if body:
        try:
            assignment = json.loads(body)
        except ValueError:
            assignment = {}
        row["client_udp"] = numbers(assignment.get("client_udp"))
        row["caches_fresh"] = sorted(item.get("node_id") for item in
                                     assignment.get("caches") or [] if item.get("fresh"))
    body = get("/metrics")
    if body:
        metrics = {}
        for line in body.decode(errors="replace").splitlines():
            if not line or line.startswith("#"):
                continue
            key, _, value = line.rpartition(" ")
            metric = key.split("{", 1)[0]
            if KEEP.match(metric):
                try:
                    metrics[metric] = metrics.get(metric, 0) + float(value)
                except ValueError:
                    pass
        row["metrics"] = metrics
    handle.write(json.dumps(row) + "\n")
    handle.flush()
"""


def mesh_sampler_command(node: dict, remote_dir: str, seconds: float, stream: int) -> str:
    return (f"mkdir -p {remote_dir}; cat >{REMOTE}/mesh-sampler.py <<'PY'\n{MESH_SAMPLER}\nPY\n"
            f"nohup python3 {REMOTE}/mesh-sampler.py {remote_dir}/mesh.jsonl {int(seconds)} "
            f"{stream} {node['name']} >/dev/null 2>&1 </dev/null &")


# Counters of the mesh sampler whose rise during and after a fault the
# survivors must not show (ops design 2: no rise in drops, would-block or
# send-queue drops; no refusals unless the scenario expects them).
SURVIVOR_COUNTERS = ("client_udp.would_block", "client_udp.send_queue_drops",
                     "client_udp.refusals", "client_udp.send_errors")
MESH_COUNTERS = SURVIVOR_COUNTERS + (
    "client_udp.moves", "client_udp.playout_skipped_parts", "client_udp.region_silence_held",
    "client_udp.moves_held_down", "client_udp.parts", "canonical_gap_skip",
    "edge_hole_repair_requests", "cache_replication.subscriptions_opened",
    "cache_replication.subscriptions_released", "cache_replication.subscriptions_refused",
    "rejected")
MESH_GAUGES = ("client_udp.subscribers", "sessions", "warm_parts_pending",
               "backfill_parts_pending", "load_permille", "served_subscriptions")


def dotted(row: dict, key: str):
    value = row
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def window_rows(rows: list[dict], start: float, end: float) -> list[dict]:
    return [row for row in rows if start <= row["t"] < end]


def mesh_window(rows: list[dict], start: float, end: float) -> dict:
    """One node's mesh sampler over a window: each counter's rise (None
    when the node does not give it), each gauge's maximum, and its last
    value of the advertised load and draining."""
    rows = window_rows(rows, start, end)
    out: dict = {"samples": len(rows)}
    for key in MESH_COUNTERS:
        values = [value for value in (dotted(row, key) for row in rows) if value is not None]
        out[key] = values[-1] - values[0] if len(values) >= 2 else (0 if values else None)
    for key in MESH_GAUGES:
        values = [value for value in (dotted(row, key) for row in rows) if value is not None]
        out[f"{key}.max"] = max(values) if values else None
    metrics: dict[str, list[float]] = {}
    for row in rows:
        for key, value in (row.get("metrics") or {}).items():
            metrics.setdefault(key, []).append(value)
    out["metrics"] = {key: values[-1] - values[0] for key, values in sorted(metrics.items())
                      if len(values) >= 2}
    return out


def gauges_zero_at(rows: list[dict], after: float) -> float | None:
    """When a drained node's live gauges (UDP subscribers and HLS
    sessions) first both read 0 after `after`; None when they never do or
    the node does not give them (Phase AM item 8)."""
    for row in rows:
        if row["t"] < after:
            continue
        udp, hls = dotted(row, "client_udp.subscribers"), dotted(row, "sessions")
        if udp == 0 and hls == 0:
            return row["t"]
    return None


def stranded_listeners(rows: list[dict], fault: float, end: float) -> dict:
    """Stranded listeners of one listener host's intervals (its "total"
    rows, edge None): needletail-load's own gauge when it gives one (Phase
    AM item 10), else listeners that exist but are not subscribed
    (active - up) for STRANDED_GAP_S intervals in a row from
    STRANDED_AFTER_S after the fault, and at the end. None when neither
    figure is in the file."""
    totals = sorted((row for row in rows if row["edge"] is None
                     and fault + STRANDED_AFTER_S <= row["t"] <= end and row["phase"] != "final"),
                    key=lambda row: row["t"])
    if not totals:
        return {"stranded": None, "at_end": None, "source": None}
    if any(row.get("stranded") is not None for row in totals):
        values = [row.get("stranded") or 0 for row in totals]
        return {"stranded": max(values), "at_end": values[-1], "source": "needletail-load"}
    if not any(row.get("active") is not None for row in totals):
        return {"stranded": None, "at_end": None, "source": None}
    gaps = [max(0, (row.get("active") or 0) - (row["listeners"] or 0)) for row in totals]
    held = 0
    for index in range(len(gaps)):
        window = gaps[index:index + STRANDED_GAP_S]
        if len(window) == STRANDED_GAP_S:
            held = max(held, min(window))
    return {"stranded": held, "at_end": gaps[-1], "source": "active - up"}


def udp_window(rows: list[dict], start: float, end: float, edges: set[str] | None,
               state: dict) -> dict:
    """UDP listeners' parts, missing parts and moves over a window, for the
    edges in `edges` (None: all), from needletail-load's per-edge interval
    rows; moves by the edge that they were counted on (needletail-load
    counts a move on the edge the listener moved to)."""
    parts = missing = moves = 0
    listeners: dict[float, int] = {}
    by_edge: dict[str, int] = {}
    for row in rows:
        if row["edge"] is None or not start <= row["t"] < end or row["phase"] == "final":
            continue
        name = edge_name_of(row["edge"], state) or str(row["edge"])
        if edges is not None and name not in edges:
            continue
        parts += row["parts"]
        missing += row["missing"]
        moves += row["moves"]
        by_edge[name] = by_edge.get(name, 0) + row["moves"]
        listeners[round(row["t"])] = listeners.get(round(row["t"]), 0) + row["listeners"]
    return {"parts": parts, "missing": missing, "moves": moves, "moves_by_edge": by_edge,
            "missing_percent": round(100.0 * missing / (parts + missing), 4)
            if parts + missing else None,
            "listeners_max": max(listeners.values()) if listeners else 0}


def spread(by_edge: dict[str, int], edges: list[str]) -> float | None:
    """max/mean of the moves over the edges that took them (F1: at most
    1.3)."""
    counts = [by_edge.get(edge, 0) for edge in edges]
    if not counts or not sum(counts):
        return None
    return round(max(counts) / (sum(counts) / len(counts)), 3)


def reader_moves(report: dict, launched: float) -> list[dict]:
    """needletail-reader's moves in Unix time: `at_ms` counts from the
    reader's start, which is the launch the run logged (the readers start
    together)."""
    out = []
    for reader in report.get("readers") or []:
        for move in reader.get("moves") or []:
            out.append({"t": launched + move.get("at_ms", 0) / 1000.0, "from": move.get("from"),
                        "to": move.get("to"), "reason": move.get("reason"),
                        "session": reader.get("session")})
    return sorted(out, key=lambda item: item["t"])


def check(name: str, ok, value=None, needs: str | None = None) -> dict:
    """One pass criterion: ok True or False, or None when it cannot be
    measured (`needs`: what it waits for)."""
    return {"criterion": name, "ok": ok, "value": value,
            **({"needs": needs} if needs else {})}


def survivors_window(nodes_summary: dict, state: dict, avoid: set[str]) -> dict:
    """The busiest survivor (a forwarder or an edge not under fault) of a
    window: CPU, busiest core, share of its outbound cap, UDP drops."""
    out = {"cpu_max": 0.0, "core_max": 0.0, "nic_share_max": 0.0, "udp_rcvbuf_drops": 0,
           "nodes": 0}
    for name, summary in nodes_summary.items():
        node = state["nodes"].get(name)
        if node is None or node["role"] not in ("edge", "distributor") or name in avoid \
                or not summary.get("samples"):
            continue
        out["nodes"] += 1
        out["cpu_max"] = max(out["cpu_max"], summary["cpu_p95"])
        out["core_max"] = max(out["core_max"], summary["core_max"])
        cap = NIC_MBPS.get(node.get("type") or "", 0) * 1e6
        if cap:
            out["nic_share_max"] = max(out["nic_share_max"], round(summary["tx_bps_mean"] / cap, 4))
        out["udp_rcvbuf_drops"] += summary["udp_rcvbuf_drops"]
    return out


def analyse_phases(state: dict, step_dir: pathlib.Path, timeline: dict, start: float,
                   plan: dict, events: list[dict] | None = None,
                   readers_launched: float | None = None) -> list[dict]:
    """Each scenario of a phase run from the step's files: every metric
    before (the baseline), during (the fault and its observation) and after
    (the settle); the listeners stranded; the moves and their spread; the
    survivors' load; the mesh counters of the survivors and of the nodes
    under fault; the readers' moves; the events; and the pass criteria,
    each True, False or None (cannot be measured yet, and why)."""
    samplers, meshes, intervals_by_host, reports = {}, {}, {}, []
    for host_dir in sorted(path for path in step_dir.iterdir() if path.is_dir()):
        sampler = host_dir / "sampler.jsonl"
        if sampler.is_file():
            samplers[host_dir.name] = sampler_rows(sampler.read_text())
        mesh = host_dir / "mesh.jsonl"
        if mesh.is_file():
            meshes[host_dir.name] = sorted(read_jsonl(mesh.read_text()), key=lambda row: row["t"])
        intervals = host_dir / LOAD_DIR / "intervals.jsonl"
        if intervals.is_file():
            intervals_by_host[host_dir.name] = interval_rows(read_jsonl(intervals.read_text()))
        report = host_dir / "hls" / "steered.json"
        if report.is_file():
            try:
                reports.append(json.loads(report.read_text()))
            except ValueError:
                pass
    every_row = [row for rows in intervals_by_host.values() for row in rows]
    moves = [move for report in reports
             for move in reader_moves(report, readers_launched or start)]
    criteria_of = {scenario["name"]: scenario for scenario in plan["scenarios"]}
    edges_all = sorted((name for name, node in state["nodes"].items() if node["role"] == "edge"),
                       key=natural)
    out = []
    for row in timeline["scenarios"]:
        spec = criteria_of.get(row["name"], {})
        windows = {"before": (start + row["base_start"], start + row["fault"]),
                   "during": (start + row["fault"], start + row["observe_end"]),
                   "after": (start + row["observe_end"], start + row["end"])}
        targets = set(row["targets"])
        unaffected = {name for name in edges_all if name not in targets}
        result: dict = {"name": row["name"], "group": row["group"],
                        "title": spec.get("title"), "targets": sorted(targets),
                        "joins": row["joins"],
                        "windows": {key: [round(a, 3), round(b, 3)] for key, (a, b)
                                    in windows.items()},
                        "needs_am": spec.get("needs_am") or [], "udp": {}, "survivors": {},
                        "mesh": {}}
        for key, (a, b) in windows.items():
            result["udp"][key] = {"unaffected": udp_window(every_row, a, b, unaffected, state),
                                  "faulted": udp_window(every_row, a, b, targets, state)}
            summaries = {name: summarize_samples(header, rows, a, b,
                                                 (state["nodes"].get(name) or {}).get("type"))
                         for name, (header, rows) in samplers.items()}
            result["survivors"][key] = survivors_window(summaries, state, targets)
            per_node = {name: mesh_window(rows, a, b) for name, rows in meshes.items()}
            survivors = {name: item for name, item in per_node.items() if name not in targets}
            result["mesh"][key] = {
                "survivors": {counter: (None if all(item.get(counter) is None
                                                    for item in survivors.values())
                                        else sum(item.get(counter) or 0
                                                 for item in survivors.values()))
                              for counter in SURVIVOR_COUNTERS + (
                                  "client_udp.moves", "edge_hole_repair_requests",
                                  "canonical_gap_skip",
                                  "cache_replication.subscriptions_released")},
                "warm_parts_pending_max": max((item.get("warm_parts_pending.max") or 0
                                               for item in per_node.values()), default=None)
                if any(item.get("warm_parts_pending.max") is not None
                       for item in per_node.values()) else None,
                "nodes": {name: per_node[name] for name in sorted(targets | set(row["joins"]))
                          if name in per_node}}
        fault_t, end_t = windows["during"][0], windows["after"][1]
        stranded_by_host = {host: stranded_listeners(rows, fault_t, end_t)
                            for host, rows in intervals_by_host.items()}
        known = [item for item in stranded_by_host.values() if item["stranded"] is not None]
        result["stranded"] = {"max": sum(item["stranded"] for item in known) if known else None,
                              "at_end": sum(item["at_end"] for item in known) if known else None,
                              "source": known[0]["source"] if known else None,
                              "hosts": len(known)}
        during = result["udp"]["during"]
        receiving = [edge for edge in unaffected
                     if any(state["nodes"].get(edge, {}).get("site")
                            == state["nodes"].get(target, {}).get("site") for target in targets)]
        result["moves"] = {"udp": during["unaffected"]["moves"] + during["faulted"]["moves"],
                           "udp_by_edge": {**during["unaffected"]["moves_by_edge"],
                                           **during["faulted"]["moves_by_edge"]},
                           "udp_spread_max_over_mean": spread(
                               during["unaffected"]["moves_by_edge"], receiving),
                           "hls": [move for move in moves if fault_t <= move["t"] < end_t]}
        result["moves"]["hls_by_reason"] = {}
        for move in result["moves"]["hls"]:
            reason = move["reason"] or "?"
            result["moves"]["hls_by_reason"][reason] = \
                result["moves"]["hls_by_reason"].get(reason, 0) + 1
        result["moves"]["hls"] = result["moves"]["hls"][:50]
        result["events"] = [item for item in (events or [])
                            if windows["before"][0] * 1e6 <= item.get("unix_us", 0)
                            <= windows["after"][1] * 1e6]
        result["criteria"] = phase_criteria(result, spec)
        out.append(result)
    return out


def phase_criteria(result: dict, spec: dict) -> list[dict]:
    """The pass criteria of a scenario that the run can judge now, and the
    ones it cannot, with what they wait for (ops design 2, as the critique
    corrects it)."""
    needs = spec.get("needs_am") or []
    expect = spec.get("expect") or {}
    out = []
    stranded = result["stranded"]["max"]
    out.append(check("stranded = 0", None if stranded is None else stranded == 0, stranded,
                     None if stranded is not None else
                     "needletail-load's active/stranded gauges (Phase AM item 10)"))
    # needletail-load books a listener's missing parts on its current edge,
    # and a move switches the edge before the parts lost around it settle:
    # the edges not faulted carry the moved listeners' losses too. So the
    # judged criterion is every listener's; the unaffected listeners' own
    # figure waits for per-listener moves (Phase AM item 10).
    on_edges_not_faulted = sum(result["udp"][key]["unaffected"]["missing"]
                               for key in ("during", "after"))
    every = on_edges_not_faulted + sum(result["udp"][key]["faulted"]["missing"]
                                       for key in ("during", "after"))
    out.append(check("all listeners (moved ones included): 0 missing", every == 0, every))
    out.append(check("unaffected listeners alone: 0 missing", None, on_edges_not_faulted,
                     "per-listener moves from needletail-load (Phase AM item 10): until then "
                     "a moved listener's losses are booked on the edge it moved to"))
    before = result["survivors"]["before"]
    worst = {key: max(result["survivors"][window][key] for window in ("during", "after"))
             for key in ("cpu_max", "core_max", "nic_share_max")}
    if before["nodes"]:
        out.append(check("survivors: busiest core < 90%", worst["core_max"] < 0.90,
                         worst["core_max"]))
        out.append(check("survivors: CPU < 80%", worst["cpu_max"] < 0.80, worst["cpu_max"]))
        out.append(check("survivors: tx < 80% of the cap", worst["nic_share_max"] < 0.80,
                         worst["nic_share_max"]))
    rise = {}
    for counter in SURVIVOR_COUNTERS:
        values = {key: result["mesh"][key]["survivors"].get(counter)
                  for key in ("before", "during", "after")}
        if any(value is None for value in values.values()):
            continue
        rate = {key: values[key] / max(1e-9, result["windows"][key][1] - result["windows"][key][0])
                for key in values}
        rise[counter] = max(rate["during"], rate["after"]) - rate["before"]
    for counter, value in sorted(rise.items()):
        if counter == "client_udp.refusals" and expect.get("refusals"):
            continue
        out.append(check(f"survivors: no rise in {counter}", value <= 0, round(value, 3)))
    if "moves" in expect:
        moves = result["moves"]["udp"]
        out.append(check(f"UDP moves = {expect['moves']}", moves == expect["moves"], moves))
    if "missing" in expect:
        missing = sum(result["udp"][key][side]["missing"] for key in ("during", "after")
                      for side in ("unaffected", "faulted"))
        out.append(check(f"missing = {expect['missing']}", missing == expect["missing"], missing))
    if "spread_max_over_mean" in expect:
        value = result["moves"]["udp_spread_max_over_mean"]
        out.append(check(f"moves spread max/mean <= {expect['spread_max_over_mean']}",
                         None if value is None else value <= expect["spread_max_over_mean"],
                         value, None if value is not None else "moves to spread"))
    for name in ("time_to_move_p99_ms", "time_to_move_max_ms", "warm_parts_pending_zero_s",
                 "first_part_s", "largest_burst_per_tick", "watermark_stall_ms",
                 "parent_change_share", "trigger_to_fresh_s"):
        if name in expect:
            out.append(check(f"{name} <= {expect[name]}", None, None,
                             spec.get("needs_for", {}).get(name) or
                             f"a counter of Phase AM ({', '.join(needs) or 'see SCALE.md'})"))
    if needs:
        out.append(check("Phase AM items this scenario needs before it can pass", None,
                         needs, ", ".join(f"AM item {item}" for item in needs)))
    return out


def format_phases(phases: list[dict]) -> str:
    lines = ["", "## Phases", "",
             "Missing on edges not faulted includes the losses of listeners that moved "
             "there (needletail-load books them on the edge they moved to; the "
             "per-listener split waits for Phase AM item 10).", "",
             "| scenario | targets | stranded | missing on edges not faulted (during/after) | "
             "UDP moves | HLS moves | survivors core max | criteria passed/failed/open |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    for item in phases:
        crit = item["criteria"]
        passed = sum(1 for row in crit if row["ok"] is True)
        failed = sum(1 for row in crit if row["ok"] is False)
        open_ = sum(1 for row in crit if row["ok"] is None)
        worst = max(item["survivors"][key]["core_max"] for key in ("during", "after"))
        lines.append(
            f"| {item['name']} | {', '.join(item['targets'] or item['joins']) or '-'} | "
            f"{item['stranded']['max'] if item['stranded']['max'] is not None else 'n/a'} | "
            f"{item['udp']['during']['unaffected']['missing']}/"
            f"{item['udp']['after']['unaffected']['missing']} | {item['moves']['udp']} | "
            f"{sum(item['moves']['hls_by_reason'].values())} | {worst:.2f} | "
            f"{passed}/{failed}/{open_} |")
    lines.append("")
    for item in phases:
        failed = [row for row in item["criteria"] if row["ok"] is False]
        opened = [row for row in item["criteria"] if row["ok"] is None]
        if failed or opened:
            lines.append(f"- {item['name']}: "
                         + "; ".join(f"FAIL {row['criterion']} ({row['value']})"
                                     for row in failed)
                         + ("; " if failed and opened else "")
                         + "; ".join(f"open {row['criterion']}: {row.get('needs')}"
                                     for row in opened))
    return "\n".join(lines) + "\n"


class PhaseRunner:
    """Carries out a phase timeline against the lab, in a thread: each
    action at its time (the targets of one action at once), the watchers
    that log when a drain ends and when a joining node first serves, and on
    stop every restore that a fault still owes (a step stopped early leaves
    no node killed: a fault is owed before it is carried out, no new one
    starts once stopped, and finish() waits for the one under way before it
    restores). At most one autoscale controller runs at a time."""

    def __init__(self, lab: dict, timeline: dict, start: float, log, args,
                 events_path: pathlib.Path | None = None,
                 directory: pathlib.Path | None = None):
        self.lab = lab
        self.timeline = timeline
        self.start = start
        self.log = log
        self.args = args
        self.events_path = events_path
        # Where the controllers' output and their state go (the step's
        # directory): a controller state of another run or lab is never read.
        self.directory = directory or (events_path.parent if events_path else None)
        self.stop = threading.Event()
        self.owed: list[dict] = []
        self.owed_lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.processes: list[dict] = []
        self.drained: set[str] = set()
        self.thread: threading.Thread | None = None

    def schedule(self) -> list[dict]:
        items = []
        for row in self.timeline["scenarios"]:
            items += row["actions"] + row["restores"]
        return sorted(items, key=lambda item: (item["t"], bool(item.get("restore"))))

    def begin(self) -> None:
        """run() in a thread of its own; finish() waits for it."""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        for item in self.schedule():
            delay = self.start + item["t"] - time.time()
            if (delay > 0 and self.stop.wait(delay)) or self.stop.is_set():
                break
            with self.owed_lock:
                if item.get("restore"):
                    self.owed = [owed for owed in self.owed
                                 if owed["scenario"] != item["scenario"]]
                elif item["kind"] in RESTORE_OF:
                    # Owed before it is carried out: a step stopped while
                    # it runs still puts it back.
                    self.owed.append(item)
            self.act(item)
        for entry in list(self.processes):
            try:
                entry["process"].wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    def node(self, name: str) -> dict:
        node = self.lab["nodes"].get(name)
        if node is None:
            raise ValueError(f"{name} is not a node of the lab")
        if node["role"] not in ("edge", "distributor"):
            raise ValueError(f"{name}: phases act on edges and distributors only")
        return node

    def act(self, item: dict) -> None:
        kind = item["kind"]
        base = {"source": "scale-run", "scenario": item["scenario"], "kind": kind,
                "restore": bool(item.get("restore"))}
        if kind == "autoscale":
            self.autoscale(item, base)
            return
        targets = list(item["targets"])
        if len(targets) == 1:
            self.act_one(item, base, targets[0])
            return
        # The targets of one action at once (F7, F8 kill several "at
        # once"): one thread each, and the spread of their starts logged.
        started: dict[str, int] = {}
        threads = [threading.Thread(target=self.act_one, args=(item, base, target, started))
                   for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if started:
            self.log({**base, "event": "spread", "targets": targets, "unix_us": unix_us(),
                      "spread_us": max(started.values()) - min(started.values())})

    def act_one(self, item: dict, base: dict, target: str,
                started: dict | None = None) -> None:
        kind = item["kind"]
        if self.stop.is_set() and not item.get("restore"):
            # Stopped early: no new fault (the restore of this one is owed
            # all the same, and is harmless on a node it never reached).
            self.log({**base, "event": "skipped", "node": target, "unix_us": unix_us(),
                      "why": "the step was stopped"})
            return
        at = unix_us()
        if started is not None:
            started[target] = at
        self.log({**base, "event": "action", "node": target, "unix_us": at})
        try:
            if kind in NODE_COMMANDS:
                ssh(self.node(target), NODE_COMMANDS[kind], timeout=60, check=True)
            elif kind in ("drain", "resume"):
                # Always naming the node: av-mesh takes a request with no
                # node_id and no region as global.
                http_post(self.node(target), f"/api/control/{kind}-node",
                          {"node_id": target})
            elif kind in ("join", "leave"):
                deploy_nodes("start" if kind == "join" else "stop", target)
            elif kind == "add":
                lab_tool_slot("add", target, self.args)
                deploy_nodes("push", target)
                self.lab = state()
            elif kind == "remove":
                # Only a slot whose drain ended (its live gauges read 0) is
                # deleted; otherwise it is left for `down`.
                if target not in self.drained:
                    raise RuntimeError(f"{target}: its drain did not end (no drain_end): "
                                       "not deleted; `down` deletes it")
                deploy_nodes("stop", target)
                lab_tool_slot("remove", target, self.args)
            self.log({**base, "event": "done", "node": target, "unix_us": unix_us()})
        except Exception as error:  # noqa: BLE001 - the run records it and goes on
            self.log({**base, "event": "failed", "node": target, "unix_us": unix_us(),
                      "error": str(error)[-400:]})
            return
        if kind == "drain" and not item.get("restore"):
            self.watch(self.watch_drain, target, item)
        if kind in ("join", "add", "start"):
            self.watch(self.watch_serving, target, item)

    def watch(self, function, target: str, item: dict) -> None:
        thread = threading.Thread(target=function, args=(target, item), daemon=True)
        thread.start()
        self.threads.append(thread)

    def scenario_end(self, item: dict) -> float:
        row = next(row for row in self.timeline["scenarios"] if row["name"] == item["scenario"])
        return self.start + row["observe_end"]

    def watch_drain(self, target: str, item: dict) -> None:
        """Log drain_end when the drained node's live gauges read 0, or
        drain_unfinished with the last ones at the end of the observation
        (None: the node gives no live gauge, Phase AM item 8)."""
        end = self.scenario_end(item)
        node = self.lab["nodes"].get(target)
        last = (None, None)
        while time.time() < end and not self.stop.is_set():
            assignment = http_get(node, f"/api/assignment?stream={self.args.stream}")
            local = http_get(node, "/api/mesh/local")
            udp = dotted(assignment.get("client_udp") or {}, "subscribers") \
                if isinstance(assignment, dict) else None
            edge = next((entry for entry in (local.get("edge_services") or [])
                         if entry.get("node_id") == target), {}) if isinstance(local, dict) else {}
            hls = edge.get("active_playback_sessions")
            last = (udp, hls)
            if udp == 0 and hls == 0:
                self.drained.add(target)
                self.log({"source": "scale-run", "scenario": item["scenario"],
                          "event": "drain_end", "node": target, "unix_us": unix_us()})
                return
            self.stop.wait(0.5)
        self.log({"source": "scale-run", "scenario": item["scenario"], "event": "drain_unfinished",
                  "node": target, "unix_us": unix_us(), "udp_subscribers": last[0],
                  "hls_sessions": last[1]})

    def watch_serving(self, target: str, item: dict) -> None:
        """Log first_advertised when the region's entry sees the joining
        node fresh, and first_serving when the node first counts a part sent
        or a session."""
        end = self.scenario_end(item)
        node = self.lab["nodes"].get(target) or state()["nodes"].get(target)
        if node is None:
            return
        entries = [other for other in self.lab["nodes"].values()
                   if other["site"] == node["site"] and other["role"] == "distributor"
                   and other["name"] != target]
        advertised = served = False
        baseline = None
        while time.time() < end and not self.stop.is_set() and not (advertised and served):
            if not advertised:
                for entry in entries:
                    found = http_get(entry, f"/api/assignment?stream={self.args.stream}")
                    caches = found.get("caches") if isinstance(found, dict) else None
                    if caches and any(cache.get("node_id") == target and cache.get("fresh")
                                      for cache in caches):
                        advertised = True
                        self.log({"source": "scale-run", "scenario": item["scenario"],
                                  "event": "first_advertised", "node": target,
                                  "unix_us": unix_us()})
                        break
            if not served:
                own = http_get(node, f"/api/assignment?stream={self.args.stream}")
                local = http_get(node, "/api/mesh/local")
                parts = dotted((own.get("client_udp") or {}) if isinstance(own, dict) else {},
                               "parts")
                edge = next((entry for entry in (local.get("edge_services") or [])
                             if entry.get("node_id") == target), {}) \
                    if isinstance(local, dict) else {}
                count = (parts or 0) + (edge.get("active_playback_sessions") or 0)
                if baseline is None and (parts is not None or edge):
                    baseline = parts or 0
                elif baseline is not None and count > baseline:
                    served = True
                    self.log({"source": "scale-run", "scenario": item["scenario"],
                              "event": "first_serving", "node": target, "unix_us": unix_us()})
            self.stop.wait(0.5)

    def autoscale(self, item: dict, base: dict) -> None:
        """The controller, set loose for the scenario: linode-autoscale.py
        with the run's events file, until the scenario's observation ends.
        Every decision it takes is its own. One controller at a time: the
        previous scenario's is waited for (its last tick can run long: a
        create and a push), and stopped with SIGINT if it has not exited
        after CONTROLLER_WAIT_S. Its state lives in the step's directory
        (A1 hands its warm pool to A2; no other run's state is read), its
        output goes to controller-<scenario>.log there, and its exit code
        is logged."""
        for entry in list(self.processes):
            if entry["process"].poll() is None:
                self.log({**base, "event": "controller_wait", "unix_us": unix_us(),
                          "previous": entry["scenario"]})
                deadline = time.time() + CONTROLLER_WAIT_S
                while entry["process"].poll() is None and time.time() < deadline \
                        and not self.stop.is_set():
                    self.stop.wait(1.0)
                self.stop_controller(entry)
        if self.stop.is_set():
            return
        seconds = max(1.0, self.scenario_end(item) - time.time())
        command = [sys.executable, str(HERE.parent / "linode-autoscale.py"), "run",
                   "--backend", "linode", "--stream", str(self.args.stream),
                   "--duration", str(int(seconds))]
        if self.events_path:
            command += ["--events", str(self.events_path)]
        output = subprocess.DEVNULL
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            command += ["--controller-state", str(self.directory / "autoscale-state.json")]
            output = open(self.directory / f"controller-{item['scenario']}.log", "ab")
        if getattr(self.args, "budget_usd", None) is not None:
            command += ["--budget-usd", str(self.args.budget_usd),
                        "--spent-before-usd", str(self.args.spent_before_usd)]
        command += [str(value) for value in item.get("controller_args", [])]
        self.log({**base, "event": "controller_start", "unix_us": unix_us(),
                  "command": command[2:]})
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT)
        finally:
            if output is not subprocess.DEVNULL:
                output.close()
        entry = {"process": process, "scenario": item["scenario"]}
        self.processes.append(entry)
        self.watch(self.watch_controller, entry, item)

    def watch_controller(self, entry: dict, item: dict) -> None:
        """Log a controller's exit and its code (a refusal or a crash is
        never mistaken for "nothing scaled"), with the end of its output."""
        code = entry["process"].wait()
        event = {"source": "scale-run", "scenario": entry["scenario"],
                 "event": "controller_exit", "unix_us": unix_us(), "returncode": code}
        if code != 0 and self.directory is not None:
            path = self.directory / f"controller-{entry['scenario']}.log"
            if path.is_file():
                event["output_tail"] = path.read_text(errors="replace")[-800:]
        self.log(event)

    def stop_controller(self, entry: dict) -> None:
        """SIGINT (the controller resumes its drains in flight and exits),
        then SIGTERM, then SIGKILL."""
        process = entry["process"]
        steps = (("SIGINT", lambda: process.send_signal(signal.SIGINT)),
                 ("SIGTERM", process.terminate), ("SIGKILL", process.kill))
        for name, send in steps:
            if process.poll() is not None:
                return
            send()
            self.log({"source": "scale-run", "scenario": entry["scenario"],
                      "event": "controller_stop", "unix_us": unix_us(), "signal": name})
            try:
                process.wait(timeout=CONTROLLER_STOP_S)
            except subprocess.TimeoutExpired:
                continue

    def finish(self, join_s: float = 120.0) -> None:
        """Stop: let the action under way end (run() starts no other), then
        put back every fault still owed its restore, then stop a controller
        still running (SIGINT: it resumes its drains in flight)."""
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=join_s)
            if self.thread.is_alive():
                self.log({"source": "scale-run", "event": "runner_still_acting",
                          "unix_us": unix_us(), "waited_s": join_s})
        with self.owed_lock:
            owed, self.owed = list(self.owed), []
        for item in owed:
            restore = {"kind": RESTORE_OF[item["kind"]], "targets": item["targets"],
                       "scenario": item["scenario"], "restore": True, "t": 0}
            self.act(restore)
        for entry in list(self.processes):
            self.stop_controller(entry)


def http_post(node: dict, path: str, body: dict, timeout: float = 5.0) -> int:
    if path.startswith("/api/control/") and not (body or {}).get("node_id"):
        raise ValueError("a control request must name its node (av-mesh takes one without "
                         "node_id or region as global)")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(f"https://{node['public_ipv4']}:{HTTP_PORT}{path}",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.status


def deploy_nodes(verb: str, name: str) -> None:
    """The deploy tool's push/start/stop --nodes for one node."""
    result = subprocess.run([sys.executable, str(HERE.parent / "linode-small-mesh-deploy.py"),
                             verb, "--nodes", name], capture_output=True, text=True,
                            timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"deploy {verb} --nodes {name}: {result.stderr[-800:]}")


def lab_tool_slot(verb: str, name: str, args) -> None:
    """The lab tool's add (with the run's budget) or remove of one slot."""
    command = [sys.executable, str(HERE.parent / "linode-small-mesh-lab.py"), verb, name]
    if verb == "add":
        if getattr(args, "budget_usd", None) is None:
            raise RuntimeError("an add needs --budget-usd")
        command += ["--budget-usd", str(args.budget_usd),
                    "--spent-before-usd", str(args.spent_before_usd)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"lab {verb} {name}: {result.stderr[-800:]}")


def phase_cost(timeline: dict, ramp_s: float, hourly: float,
               overhead_s: float = STEP_OVERHEAD_S) -> dict:
    """Minutes and pro-rata dollars of a phase group (one held step), and of
    each scenario; under hourly rounding a scenario costs nothing unless
    the run's end passes an hour boundary, and then the whole lab's hour."""
    seconds = ramp_s + timeline["hold_s"] + overhead_s
    return {"minutes": round(seconds / 60, 1), "pro_rata_usd": round(seconds / 3600 * hourly, 2),
            "scenarios": {row["name"]: {"minutes": round((row["end"] - row["base_start"]) / 60, 1),
                                        "pro_rata_usd": round((row["end"] - row["base_start"])
                                                              / 3600 * hourly, 2)}
                          for row in timeline["scenarios"]}}


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
    --second-curve), and a new copy's warm-up sized in time (WARM_MS)."""
    return {"playback_address": "vlan", "daw_hls_formats": "flac,opus,pcm",
            "client_udp_max_subscribers": 65536, "edge_egress_capacity_bps": 4_000_000_000,
            "edge_session_limit": 0, "cache_mesh_warm_ms": WARM_MS, "standby_edges": standby}


def topology_state(path: pathlib.Path, without: list[str] = ()) -> dict:
    """A lab state for a topology file (made-up public addresses), for
    `plan --topology-only` and the tests; sites in `without` left out, and
    the autoscale slots (`"slot": true`), which `lab up` does not create."""
    topology = json.loads(path.read_text())
    nodes = {}
    slots = set(topology.get("autoscale_slots") or [])
    for index, node in enumerate(topology["nodes"], 1):
        if node["site"] in without or node.get("slot") or node["name"] in slots:
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


def ssh_quiet(node: dict, command: str, timeout: int = 30) -> str:
    """ssh for the watching during a step: an unanswered or failed probe of
    one host (a saturated host's sshd is slow) reads as no output rather
    than ending the whole run."""
    try:
        return ssh(node, command, timeout=timeout)
    except Exception:  # noqa: BLE001 - one host's probe; the next poll tries again
        return ""


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


def live_node(lab: dict, avoid: set[str] = frozenset()) -> dict:
    """The distributor whose playlist says the stream is live: the first
    that no phase faults (critique 9)."""
    for dist in common.nodes_by_role(lab, "distributor"):
        if dist["name"] not in avoid:
            return dist
    raise ValueError("every distributor is under a fault")


def wait_live(lab: dict, stream: int, timeout: float = 120,
              avoid: set[str] = frozenset()) -> bool:
    dist = live_node(lab, avoid)
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
    slots = set(topology.get("autoscale_slots") or [])
    created = [node for node in topology["nodes"]
               if not node.get("slot") and node["name"] not in slots]
    standby = standby_for(created,
                          [] if args.second_curve else topology.get("standby_edges", []),
                          args.edges_per_region)
    print(" ".join(f"--set {key}={json.dumps(value, separators=(',', ':'))}"
                   for key, value in scale_settings(standby).items()))


def fit_wanted(args: argparse.Namespace, lab: dict) -> bool:
    """Fit the steps to the listener hosts that exist: with --fit-hosts, or
    when the lab tool recorded listener hosts that the account refused
    (`listeners_missing`), unless --force (run the steps as given; the
    monitor still stops a step whose host saturates)."""
    if getattr(args, "fit_hosts", False):
        return True
    return bool(lab.get("listeners_missing")) and not getattr(args, "force", False)


def build_plan(args: argparse.Namespace, lab: dict, standby: list[str]) -> dict:
    return plan_steps(lab, parse_steps(args.steps), args.config, args.hls_share,
                      args.hls_cap_per_host, standby, args.hold, args.ramp,
                      load_costs(args.cost), args.udp_mbps, args.inbound_gbps,
                      fit=fit_wanted(args, lab))


def fit_line(plan: dict) -> str | None:
    """What fitting the steps to the listener hosts changed, for the
    operator and the events (None when the plan was not fitted)."""
    fit = plan.get("fit")
    if not fit:
        return None
    clipped = sorted({site for step in plan["steps"] for site in step.get("clipped_regions", {})})
    return (f"steps fitted to the listener hosts that exist (missing: "
            f"{', '.join(fit['listeners_missing']) or 'none'}): at most "
            + ", ".join(f"{site} {cap}" for site, cap in sorted(fit["caps"].items()))
            + f" listeners a region; clipped in {', '.join(clipped) or 'no region'}; steps "
            f"run {[step['listeners'] for step in plan['steps']]}"
            + (f", dropped {fit['dropped']} (no more listeners than the step before)"
               if fit["dropped"] else ""))


def command_plan(args: argparse.Namespace) -> dict:
    phases = phase_setup(args)
    resolve_defaults(args)
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
    if phases:
        plan["phases"] = {"timeline": phases["timeline"],
                          "cost": phase_cost(phases["timeline"], plan["steps"][0]["ramp_s"],
                                             plan["cost"]["hourly_usd"])}
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
    if fit_line(plan):
        print(fit_line(plan))
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
    if phases:
        priced = plan["phases"]["cost"]
        print(f"phases: one held step of {plan['steps'][0]['listeners']} listeners, "
              f"{priced['minutes']} min with its ramp and overhead, ${priced['pro_rata_usd']} "
              "pro rata (under hourly rounding a phase costs nothing unless the run's end "
              "passes an hour boundary, and then the whole lab's hour)")
        for row in phases["timeline"]["scenarios"]:
            item = priced["scenarios"][row["name"]]
            print(f"  {row['name']:<4} {item['minutes']:>5} min  fault at "
                  f"{row['fault']:>6.0f} s  {', '.join(row['targets'] or row['joins']) or '-'}")
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
        tails = common.parallel(hosts, lambda node: ssh_quiet(
            node, f"tail -n {SATURATION_HOLD + 2} {remote_dir}/sampler.jsonl 2>/dev/null; "
                  f"echo ==; tail -n {SATURATION_HOLD} {remote_dir}/{LOAD_DIR}/intervals.jsonl "
                  "2>/dev/null"), workers=64)
        for name, text in sorted(tails.items()):
            sampled, _, intervals = text.partition("==")
            why = load_saturated(sampler_rows(sampled)[1], interval_rows(read_jsonl(intervals)))
            if why:
                return name, why
        log({"monitor": round(time.time()), "load_cpu": {
            name: (sampler_rows(text.partition("==")[0])[1] or [{"cpu": None}])[-1]["cpu"]
            for name, text in sorted(tails.items())}})
    return None, None


def step_commands(lab: dict, plan: dict, step: dict, run: str, args,
                  avoid: set[str] = frozenset(), readers: bool = False,
                  timeline: dict | None = None) -> dict[str, str]:
    """The shell command of each listener host for one step. The LL-HLS
    entry is the region's first node that no phase faults (`avoid`);
    `readers`: the LL-HLS listeners are needletail-reader crowds that
    follow steering and move (a phase run), not ntcmp."""
    regions = regions_of(lab, plan.get("standby") or [])
    remote_dir = remote_step_dir(run, step["listeners"])
    duration = step["ramp_s"] + step["hold_s"]
    commands = {}
    for site, by_host in step["allocation"].items():
        edges = [f"{node['vlan_ipv4']}:{CLIENT_UDP_PORT}" for node in regions[site]["edges"]]
        entries = [node["vlan_ipv4"] for node in entry_nodes(regions[site], set(avoid))]
        entry = entries[0]
        for name, alloc in by_host.items():
            label = f"{run}-{step['listeners']}-{name}"
            # One line each: every tool ends in `&`, and `&;` is a syntax
            # error of the shell.
            commands[name] = "\n".join(part for part in (
                f"mkdir -p {remote_dir}",
                load_command(alloc, edges, args.config, step["ramp_s"], duration, args.sample,
                             remote_dir, label, lab["nodes"][name].get("vlan_ipv4"),
                             args.load_args, vcpus(lab["nodes"][name]),
                             profile_ramp(alloc["udp"], step["ramp_s"], timeline)
                             if timeline else None),
                *(reader_commands(alloc, entries[:4], args.config, duration, remote_dir, label)
                  if readers else
                  hls_commands(alloc, entry, args.config, step["ramp_s"], duration, remote_dir,
                               label, args.hls_window, not args.hls_http2, args.hls_steering)))
                if part)
    return commands


def run_step(lab: dict, run: str, plan: dict, step: dict, args: argparse.Namespace,
             directory: pathlib.Path, log, phases: dict | None = None) -> dict:
    """One step. With `phases` ({"timeline", "plan", "events"}): the
    listeners are held for the timeline's scenarios, a PhaseRunner carries
    out their actions from the end of the ramp, every edge and distributor
    runs the mesh sampler, the LL-HLS readers are needletail-reader crowds
    entering at nodes that no phase faults, and the faults are left out of
    what gave out first."""
    remote_dir = remote_step_dir(run, step["listeners"])
    every = list(lab["nodes"].values())
    duration = step["ramp_s"] + step["hold_s"]
    stream = CONFIGS[args.config]["stream"]
    common.parallel(every, lambda node: ssh(
        node, sampler_command(node, remote_dir, duration + 60), check=True))
    mesh_nodes = [node for node in every if node["role"] in ("edge", "distributor")]
    if phases:
        common.parallel(mesh_nodes, lambda node: ssh(
            node, mesh_sampler_command(node, remote_dir, duration + 60, stream), check=True))
    before = edge_counters(lab, stream)
    avoid = fault_nodes(phases["timeline"]) if phases else set()
    commands = step_commands(lab, plan, step, run, args, avoid, readers=bool(phases),
                             timeline=phases["timeline"] if phases else None)
    step_dir = directory / "steps" / str(step["listeners"])
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "commands.json").write_text(json.dumps(commands, indent=1) + "\n")
    hosts = [lab["nodes"][name] for name in commands]
    started = time.time()
    common.parallel(hosts, lambda node: ssh(node, commands[node["name"]], check=True))
    log({"step": step["listeners"], "started": round(started), "udp": step["udp"],
         "hls": step["hls"], "seconds": duration,
         **({"readers_launched_us": int(started * 1e6)} if phases else {})})
    runner = None
    if phases:
        args.stream = stream
        runner = PhaseRunner(lab, phases["timeline"], started + step["ramp_s"], log, args,
                             phases.get("events"), directory=step_dir)
        runner.begin()
    try:
        saturated, why = monitor(hosts, remote_dir, started + duration + 5, args.poll, log)
    finally:
        if runner is not None:
            runner.finish()
    stopped = None
    if saturated:
        stopped = f"{saturated}: {why}"
        log({"step": step["listeners"], "stopped": stopped})
    else:
        # The tools end at their duration and drain; give them time to write.
        deadline = time.time() + 60
        while time.time() < deadline:
            busy = common.parallel(hosts, lambda node: ssh_quiet(
                node, f"pgrep -c -x {LOAD_PROCESS}; pgrep -c -x ntcmp"), workers=64)
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
        if phases and node["role"] in ("edge", "distributor"):
            files.append("mesh.jsonl")
        if node["name"] in commands:
            # needletail-load's directory (intervals, summary, the sampled
            # listeners' arrivals), the readers' condensed reports, errors.
            files += [LOAD_DIR, "udp.err", "hls.condensed.json"]
            if phases:
                files += ["hls", "hls-steered.err"]
        for name in files:
            try:
                common.rsync_from(node, f"{remote_dir}/{name}", str(local) + "/", timeout=600)
            except RuntimeError:
                pass

    common.parallel(every, fetch)
    (step_dir / "counters.json").write_text(json.dumps(counter_delta(before, after), indent=1)
                                            + "\n")
    exclude = exclusion(phases["timeline"], started + step["ramp_s"]) if phases else None
    result = analyse_step(lab, step_dir, step,
                          (started + step["ramp_s"], min(ended, started + duration)), args.config,
                          exclude)
    result["stopped"] = stopped
    result["ramp_s"], result["hold_s"] = step["ramp_s"], step["hold_s"]
    if phases:
        result["phase_start"] = started + step["ramp_s"]
        result["readers_launched"] = started
        result["exclude"] = exclude
        events = read_jsonl(phases["events"].read_text()) if phases.get("events") \
            and phases["events"].is_file() else []
        result["phases"] = analyse_phases(lab, step_dir, phases["timeline"],
                                          started + step["ramp_s"], phases["plan"], events,
                                          started)
    (step_dir / "step.json").write_text(json.dumps(result, indent=1) + "\n")
    return result


def phase_setup(args: argparse.Namespace) -> dict | None:
    """With --phase-plan: the chosen scenarios, their timeline, and the
    step's listeners, share and hold from the plan's defaults where the
    command line gives none. None without --phase-plan."""
    if not getattr(args, "phase_plan", None):
        return None
    names = [name for name in (args.scenarios or "").split(",") if name]
    plan = load_phase_plan(args.phase_plan, args.group, names or None)
    timeline = phase_timeline(plan)
    defaults = {**(plan.get("defaults") or {}),
                **((plan.get("groups") or {}).get(args.group or "") or {})}
    if args.steps is None:
        args.steps = str(defaults.get("listeners", 8000))
    if len(parse_steps(args.steps)) != 1:
        raise ValueError("a phase run holds one step of listeners: give one --steps value")
    if args.hls_share is None:
        args.hls_share = float(defaults.get("hls_share", 0.05))
    if getattr(args, "config", None) is None:
        args.config = defaults.get("config", "pcm-1")
    args.hold = timeline["hold_s"]
    return {"plan": plan, "timeline": timeline}


def resolve_defaults(args: argparse.Namespace) -> None:
    if args.steps is None:
        args.steps = DEFAULT_STEPS
    if args.hls_share is None:
        args.hls_share = 0.5
    if getattr(args, "config", None) is None:
        args.config = "pcm-1"


def command_run(args: argparse.Namespace) -> None:
    lab = state()
    settings = deploy_settings()
    standby = list(settings.get("standby_edges") or [])
    phases = phase_setup(args)
    resolve_defaults(args)
    plan = build_plan(args, lab, standby)
    if not plan["steps"]:
        sys.exit("no step is left once fitted to the listener hosts that exist "
                 f"({fit_line(plan)})")
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
    events_path = directory / "events.jsonl"
    events = open(events_path, "a")
    lock = threading.Lock()

    def log(item: dict) -> None:
        # Every line with its Unix time in µs (referee R3), to line up with
        # the samplers, the listeners' arrivals and the controller's lines.
        item = {"unix_us": unix_us(), "source": "scale-run", **item}
        with lock:
            events.write(json.dumps(item) + "\n")
            events.flush()
        print(json.dumps(item), flush=True)

    if plan.get("fit"):
        log({"fit": plan["fit"], "steps": [step["listeners"] for step in plan["steps"]],
             "note": fit_line(plan)})
    if phases:
        phases["events"] = events_path
        (directory / "phases.json").write_text(json.dumps(
            {"plan": phases["plan"], "timeline": phases["timeline"],
             "cost": phase_cost(phases["timeline"], plan["steps"][0]["ramp_s"],
                                plan["cost"]["hourly_usd"])}, indent=1) + "\n")

    source = common.nodes_for(lab, "source")[0]
    contributor = common.nodes_by_role(lab, "contributor")[0]
    ssh(source, f"mkdir -p {REMOTE}; pkill -x aep1-48k-probe; "
                + sender_command(args.config, contributor.get("vlan_ipv4")
                                 or contributor["public_ipv4"], int(plan["seconds"] + 900),
                                 f"{REMOTE_MEDIA}/audio/audio.s24le"), check=True)
    stream = CONFIGS[args.config]["stream"]
    avoid = fault_nodes(phases["timeline"]) if phases else set()
    # The loop starts at the next 64 s boundary at least 8 s away.
    start = ssh(source, f"cat {REMOTE}/source.start").strip()
    time.sleep(max(0.0, (int(start) if start.isdigit() else time.time()) - time.time() + 3))
    if not wait_live(lab, stream, avoid=avoid):
        sys.exit(f"stream {stream} is not live at {live_node(lab, avoid)['name']}")
    result = {"run": run, "config": args.config, "hls_share": args.hls_share,
              "standby_edges": standby, "steps": [],
              "lab": {name: {key: node.get(key) for key in ("role", "site", "type", "purpose")}
                      for name, node in lab["nodes"].items()}}
    if phases:
        result["phase_plan"] = str(args.phase_plan)
        result["phase_scenarios"] = [row["name"] for row in phases["timeline"]["scenarios"]]
    try:
        for step in plan["steps"]:
            outcome = run_step(lab, run, plan, step, args, directory, log, phases)
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
    text = format_summary(result)
    for step in result["steps"]:
        if step.get("phases"):
            text += format_phases(step["phases"])
    (directory / "summary.md").write_text(text)
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
    phases_file = directory / "phases.json"
    phases = json.loads(phases_file.read_text()) if phases_file.is_file() else None
    events_file = directory / "events.jsonl"
    events = read_jsonl(events_file.read_text()) if events_file.is_file() else []
    steps = []
    for step, old in zip(plan["steps"], previous["steps"]):
        step_dir = directory / "steps" / str(step["listeners"])
        exclude = old.get("exclude")
        if exclude:
            exclude = {"nodes": exclude["nodes"],
                       "windows": [tuple(window) for window in exclude["windows"]]}
        outcome = analyse_step(lab, step_dir, step, tuple(old["window"]), plan["config"], exclude)
        outcome["stopped"] = old.get("stopped")
        outcome["ramp_s"], outcome["hold_s"] = step["ramp_s"], step["hold_s"]
        if phases and old.get("phase_start") is not None:
            for key in ("phase_start", "readers_launched", "exclude"):
                outcome[key] = old.get(key)
            outcome["phases"] = analyse_phases(lab, step_dir, phases["timeline"],
                                               old["phase_start"], phases["plan"], events,
                                               old.get("readers_launched"))
        steps.append(outcome)
    previous["steps"] = steps
    write_results(directory, previous)
    print((directory / "summary.md").read_text())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def planning(item):
        item.add_argument("--config", choices=sorted(CONFIGS), default=None,
                          help="the audio lane (default pcm-1, or the phase plan's)")
        item.add_argument("--steps", default=None, help=f"default {DEFAULT_STEPS}, or the phase "
                                                        "plan's listeners")
        item.add_argument("--hls-share", type=float, default=None,
                          help="share of each step's listeners that are LL-HLS readers "
                               "(default 0.5, or the phase plan's)")
        item.add_argument("--phase-plan", default=None,
                          help=f"failover, redistribution and autoscale phases in one held "
                               f"step ({PHASES.relative_to(ROOT)})")
        item.add_argument("--group", choices=("F", "R", "A"), default=None,
                          help="the phase plan's scenarios of one group")
        item.add_argument("--scenarios", default="", help="comma-separated scenario names")
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
        item.add_argument("--fit-hosts", action="store_true",
                          help="each region takes at most what its listener hosts drive (the "
                               "default when the lab misses listener hosts that the account "
                               "refused, unless --force)")

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
                     help="run steps that the plan says the listener hosts cannot drive (on a "
                          "lab missing listener hosts: the steps as given, not fitted)")
    run.add_argument("--allow-public-transfer", action="store_true")
    run.add_argument("--budget-usd", type=float, default=None,
                     help="the shared budget, for a phase's add or the autoscaler's creates")
    run.add_argument("--spent-before-usd", type=float, default=0.0,
                     help="what earlier labs of the work cost")
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
