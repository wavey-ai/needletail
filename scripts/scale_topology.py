#!/usr/bin/env python3
"""The topologies of the deployed audio mesh (deploy/low-latency/topology-*.json),
generated so that every file of the plan has the same shape.

The merged topology (relay and distributor are one forwarder), audio only, in
London, New York, Tokyo and Sydney:

  per region   two forwarders (`<site>-dist-a`, `-b`, g6-dedicated-2), the
               edges (`<site>-edge-1`.., g6-dedicated-2; the last of each
               region on standby), the listener load hosts
               (`<site>-load-1`.., needletail-load and ntcmp), one view host
               for the full probes (`<site>-view`, g6-dedicated-4, as the
               comparison's) and one self-hosted LiveKit SFU (`<site>-lk`,
               g6-dedicated-2). The listener and view hosts are on the
               region's VLAN (.100 up) and read the edges there: Linode does
               not bill VLAN traffic, and 5,000 UDP listeners of 1 ms PCM
               over the public network would be about 28 Gbit/s, 12 TB an
               hour of billed transfer
  London       the contributor alone on a g6-dedicated-4 (`contrib-lon`), with
               --contributors 2 its standby alone on another
               (`contrib-lon-b`, "standby": true: both always publish the
               same parts, lead decision L1), the source host (`lon-src`,
               g6-dedicated-4) and the Redis of the LiveKit multi-region set
               (`lk-redis`, g6-dedicated-2), which the comparison's LiveKit
               installer needs

The files (VARIANTS):

  topology-scale.json         the scale run (SCALE.md): ten edges a region,
                              five on standby for the first curve, eight
                              g6-dedicated-8 listener hosts a region (Sydney
                              sixteen g6-dedicated-4, the same vCPUs: no
                              g6-dedicated-8 capacity there on 22 November)
  topology-proving.json       the proving lab of the resilient mesh
                              (C-operations 2.9 and 7): both contributors,
                              three serving edges and one standby edge a
                              region, one g6-dedicated-8 listener host and
                              one g6-dedicated-4 view host a region
                              (without Sydney: `lab up --without-sites
                              sydney`, or `--optional-sites sydney` to go on
                              without it when its core is refused)
  topology-party.json         the global listening party (owner, 23
                              November): one Opus stream to 20,000
                              listeners of 1 ms Opus Custom with two paths,
                              sized by party_plan from planning figures that
                              are all arguments; PARTY.md prints its plan and
                              those of 5,000, 10,000 and 20,000 listeners at
                              both lanes and both path counts

Autoscale slots (`autoscale_slots`, nodes marked `"slot": true`): nodes of
the topology that `lab up` and `grow` do not create. The deploy renders them
into every peer list from the first push (a slot not created yet is a peer
that is down), so the autoscaler (scripts/linode-autoscale.py) creates one
with `lab add <slot>` and starts it with `deploy start --nodes <slot>`,
and nothing else restarts. Every site has one edge slot past its edges and a
third forwarder (`<short>-dist-c`, VLAN .13: a fast-plane lease child of its
site's dist-a and dist-b and a cache-mesh parent of its edges, never a
compiled relay child, so no other node's relay ports move); London has two
edge slots in topology-scale.json.

Sydney's nodes come first in the file: it is the region short of dedicated
capacity. `lab up --optional-sites sydney` creates its core before the rest (its
listener hosts with the other regions', after the core) and goes on
without Sydney when its core cannot be created; `--without-sites sydney`
leaves it out (or a file made with --sites london,newyork,tokyo).

  scale_topology.py                                 print topology-scale.json
  scale_topology.py --edges-in london=8             eight London edges
  scale_topology.py --variant topology-proving.json --standby london=2
  scale_topology.py --write                         rewrite the committed files
  scale_topology.py plan --listeners 10000 --lane opus-2.5 --paths 1 --hours 3
                                                    a party's plan (party: its topology)

Flags over the variant (each SITE=... flag may be given for several sites):
--sites, --edges-in SITE=N, --standby SITE=N (how many of the site's last
edges are on standby), --slots SITE=edges:N,forwarders:N, --load-hosts N and
--load-type TYPE (every site), --load SITE=COUNT:TYPE (one site), --view
SITE=TYPE, --contributors 1|2, --no-livekit. `party` and `plan` take the
party's flags (scale_topology.py plan --help)."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / "low-latency"
# Region, short name and VLAN subnet (10.80.<n>.0/24) of each site. The order
# is the order of creation: the scarce region first.
SITES = {
    "sydney": ("ap-southeast", "syd", 4),
    "london": ("gb-lon", "lon", 1),
    "newyork": ("us-east", "new", 2),
    "tokyo": ("ap-northeast", "tok", 3),
}
DEFAULT_SITES = ("sydney", "london", "newyork", "tokyo")
VLAN_NET = "10.80"
MESH_TYPE = "g6-dedicated-2"
CONTRIBUTOR_TYPE = "g6-dedicated-4"
SOURCE_TYPE = "g6-dedicated-4"
VIEW_TYPE = "g6-dedicated-4"
LIVEKIT_TYPE = "g6-dedicated-2"
# Listener and view hosts: g6-dedicated-4 or -8 only (owner, 21-22 November).
LOAD_TYPES = ("g6-dedicated-4", "g6-dedicated-8")
# Linode's hourly prices (GET /linode/types, 22 November): every plan a
# generated topology may use.
PRICES = {"g6-dedicated-2": 0.054, "g6-dedicated-4": 0.108, "g6-dedicated-8": 0.216}
# The contributor and, with contributors=2, its standby (workstream A's
# render: role contributor, "standby": true, at the contributor's site on
# its VLAN), each alone on its own g6-dedicated-4.
CONTRIBUTOR = ("contrib-lon", 5)
STANDBY_CONTRIBUTOR = ("contrib-lon-b", 6)
# Listener load hosts per region: eight g6-dedicated-8, about 8,000 UDP
# listeners of a 1 ms stream within 70% busy (SCALE.md: needletail-load's
# cost per listener), above what ten edges send (about 7,000 at 1 ms PCM).
# Sydney had no g6-dedicated-8 capacity on 22 November (lab `check`), so it
# gets the same 64 vCPUs as sixteen g6-dedicated-4.
LOAD_TYPE = "g6-dedicated-8"
LOAD_HOSTS = 8
LOAD_BY_SITE = {"sydney": (16, "g6-dedicated-4")}
EDGES = 10
# Edges of a site other than EDGES, and the edges of each site that the first
# curve holds out (deploy setting standby_edges): the second curve doubles
# every region's edges.
EDGES_BY_SITE: dict[str, int] = {}
STANDBY_BY_SITE = {"sydney": 5, "london": 5, "newyork": 5, "tokyo": 5}
# VLAN host numbers of the view host and the listener hosts (above the edges).
VIEW_HOST = 100
LOAD_HOST_BASE = 101
# VLAN host numbers: forwarders .11/.12, edges .21 up (at most 40 edges),
# contributor .5, its standby .6, source .30 (as in topology-merged.json; the
# edges stop short of it below 9 edges and skip it above).
MAX_EDGES = 40
# Autoscale slots of every site: extra edges past the site's edges (numbered
# on) and extra forwarders (`<short>-dist-c`); `slots` of scale_topology
# changes a site's. VLAN host of a forwarder slot: FORWARDER_SLOT_HOST +
# index (.13 up, after dist-a .11 and dist-b .12).
SITE_SLOTS = {"edges": 1, "forwarders": 1}
FORWARDER_SLOT_HOST = 13
FORWARDER_LETTERS = "cdefgh"
# The opening and the close of the scale run's description.
SCALE_ABOUT = (
    "The large audio mesh of the scale run (owner, 22 November 2026: \"test on a larger "
    "deployed mesh\", \"with simulated users too\", audio only), merged topology: ",
    "Sized for the owner's budget of 22 November (the shared $100 of the Needletail "
    "work; the watchdog caps the created nodes at $72.95 in 7 h, or $62.53 in 6 h): the "
    "referee's plan (needletail-compare target/compare/scale-plan.md) grown to as "
    "many listener hosts as it buys, and enough edges a region that on the second curve "
    "the edges give out before the listener hosts; Sydney's listener hosts are "
    "g6-dedicated-4, for it had no g6-dedicated-8 capacity on 22 November (same vCPUs, "
    "same price). Generated by scripts/scale_topology.py; the plan, the cost and "
    "the steps are in deploy/low-latency/SCALE.md.")
PROVING_ABOUT = (
    "The proving lab of the resilient mesh (C-operations 2.9 and 7: rolls of every tier, "
    "crashes, the contributors' changes, replace, the autoscaler on Linode, scored by the "
    "referee's change-window.py), merged topology: ",
    "Three serving edges and one standby edge a region for the kill and roll rows (agreed "
    "with the referee, 23 November). Generated by scripts/scale_topology.py; run as "
    "C-operations section 7 says, under the lab tool's watchdog and budget.")


def ceiling_usd(hourly: float) -> float:
    """The lab's hourly ceiling for `hourly`: rounded up to five cents."""
    return math.ceil(hourly * 20 - 1e-9) / 20


def edge_host(index: int) -> int:
    """The VLAN host number of edge `index` (1-based): .21 up, skipping .30
    (the source host's address in London) and .5."""
    host = 20 + index
    if host >= 30:
        host += 1
    return host


def default_standby(sites: tuple[str, ...], edges: dict[str, int]) -> dict[str, int]:
    """STANDBY_BY_SITE's counts for the sites of `sites` that keep an edge in
    service with them (`edges`: a site's edges other than EDGES)."""
    return {site: count for site, count in STANDBY_BY_SITE.items()
            if site in sites and edges.get(site, EDGES) > count}


def site_slots(sites: tuple[str, ...], slots: dict[str, dict[str, int]] | None) -> dict:
    """{site: {"edges": n, "forwarders": n}} of every site: SITE_SLOTS, with
    what `slots` gives for a site in place of the default's keys."""
    slots = slots or {}
    for site, value in slots.items():
        if site not in sites:
            raise ValueError(f"slots given for {site}, which is not in the topology")
        if set(value) - {"edges", "forwarders"} or any(count < 0 for count in value.values()):
            raise ValueError(f"{site}: slots {value}; {{'edges': n, 'forwarders': n}}")
    return {site: {**SITE_SLOTS, **slots.get(site, {})} for site in sites}


def scale_topology(sites: tuple[str, ...] = DEFAULT_SITES, edges: dict[str, int] | None = None,
                   load_hosts: int = LOAD_HOSTS, load_type: str = LOAD_TYPE,
                   load_by_site: dict[str, tuple[int, str]] | None = None,
                   view_hosts: bool = True, view_by_site: dict[str, str] | None = None,
                   livekit: bool = True, standby: dict[str, int] | None = None,
                   slots: dict[str, dict[str, int]] | None = None, contributors: int = 1,
                   about: tuple[str, str] = SCALE_ABOUT) -> dict:
    """The topology document for `sites` (London must be one: the contributor
    is there), `edges` per site (default EDGES), `load_hosts` listener hosts
    of `load_type` per site, or (count, type) of a site in `load_by_site`
    (default LOAD_BY_SITE). `edges` None is EDGES_BY_SITE; `standby` (None:
    STANDBY_BY_SITE) is how many of a site's last edges the document names in
    `standby_edges`, held out of the first curve. `slots` changes a site's
    autoscale slots ({"edges": n, "forwarders": n} over SITE_SLOTS): nodes
    marked "slot" that `lab up` does not create. `view_by_site` gives a
    site's view host another plan than VIEW_TYPE. `contributors` 2 adds the
    standby contributor. `about` is the description's opening and close."""
    if "london" not in sites:
        raise ValueError("the contributor's site, london, must be in the topology")
    unknown = [site for site in sites if site not in SITES]
    if unknown:
        raise ValueError(f"unknown sites {unknown}; known: {', '.join(SITES)}")
    if contributors not in (1, 2):
        raise ValueError(f"{contributors} contributors; 1, or 2 with the standby")
    if edges is None:
        edges = {site: count for site, count in EDGES_BY_SITE.items() if site in sites}
    edges = dict(edges)
    if standby is None:
        standby = default_standby(sites, edges)
    for site, count in standby.items():
        if site not in sites:
            raise ValueError(f"standby edges given for {site}, which is not in the topology")
        if not 0 <= count < edges.get(site, EDGES):
            raise ValueError(f"{site}: {count} standby edges; it must keep one in service")
    for site, count in edges.items():
        if site not in sites:
            raise ValueError(f"edges given for {site}, which is not in the topology")
        if not 1 <= count <= MAX_EDGES:
            raise ValueError(f"{site}: {count} edges; 1 to {MAX_EDGES}")
    slots = site_slots(sites, slots)
    for site, value in slots.items():
        if edges.get(site, EDGES) + value["edges"] > MAX_EDGES:
            raise ValueError(f"{site}: {edges.get(site, EDGES)} edges and "
                             f"{value['edges']} edge slots; at most {MAX_EDGES}")
        if value["forwarders"] > len(FORWARDER_LETTERS):
            raise ValueError(f"{site}: at most {len(FORWARDER_LETTERS)} forwarder slots")
    if load_by_site is None:
        load_by_site = {site: value for site, value in LOAD_BY_SITE.items() if site in sites}
    for site in load_by_site:
        if site not in sites:
            raise ValueError(f"load hosts given for {site}, which is not in the topology")
    load = {site: (load_hosts, load_type) for site in sites}
    load.update(load_by_site)
    view = {site: VIEW_TYPE for site in sites}
    for site, kind in (view_by_site or {}).items():
        if site not in sites:
            raise ValueError(f"a view host given for {site}, which is not in the topology")
        view[site] = kind
    for site, (count, kind) in load.items():
        if kind not in LOAD_TYPES:
            raise ValueError(f"{site}: listener hosts {kind}: {' or '.join(LOAD_TYPES)} only")
        if count < 0:
            raise ValueError(f"{site}: {count} load hosts")
    for site, kind in view.items():
        if kind not in LOAD_TYPES:
            raise ValueError(f"{site}: view host {kind}: {' or '.join(LOAD_TYPES)} only")
    ordered = [site for site in SITES if site in sites]
    nodes = []
    for site in ordered:
        region, short, net = SITES[site]
        vlan = f"{VLAN_NET}.{net}"

        def add(name: str, role: str, kind: str, host: int | None = None,
                purpose: str | None = None, slot: bool = False,
                standby_contributor: bool = False) -> None:
            node = {"name": name, "role": role, "site": site, "region": region, "type": kind}
            if slot:
                node["slot"] = True
            if standby_contributor:
                node["standby"] = True
            if purpose:
                # What a load node is for: the source, the full probes
                # (view) or the simulated listeners.
                node["purpose"] = purpose
            if host is not None:
                node["vlan_ipv4"] = f"{vlan}.{host}/24"
            nodes.append(node)

        if site == "london":
            add(CONTRIBUTOR[0], "contributor", CONTRIBUTOR_TYPE, CONTRIBUTOR[1])
            if contributors == 2:
                add(STANDBY_CONTRIBUTOR[0], "contributor", CONTRIBUTOR_TYPE,
                    STANDBY_CONTRIBUTOR[1], standby_contributor=True)
            add("lon-src", "load", SOURCE_TYPE, 30, "source")
        add(f"{short}-dist-a", "distributor", MESH_TYPE, 11)
        add(f"{short}-dist-b", "distributor", MESH_TYPE, 12)
        for index in range(1, edges.get(site, EDGES) + 1):
            add(f"{short}-edge-{index}", "edge", MESH_TYPE, edge_host(index))
        for index in range(slots[site]["forwarders"]):
            add(f"{short}-dist-{FORWARDER_LETTERS[index]}", "distributor", MESH_TYPE,
                FORWARDER_SLOT_HOST + index, slot=True)
        first = edges.get(site, EDGES) + 1
        for index in range(first, first + slots[site]["edges"]):
            add(f"{short}-edge-{index}", "edge", MESH_TYPE, edge_host(index), slot=True)
        count, kind = load[site]
        for index in range(1, count + 1):
            add(f"{short}-load-{index}", "load", kind, LOAD_HOST_BASE + index - 1,
                purpose="listeners")
        if view_hosts:
            add(f"{short}-view", "load", view[site], VIEW_HOST, purpose="view")
        if livekit:
            add(f"{short}-lk", "livekit", LIVEKIT_TYPE)
            if site == "london":
                add("lk-redis", "livekit", LIVEKIT_TYPE)
    counts = {site: edges.get(site, EDGES) for site in ordered}
    held = [f"{SITES[site][1]}-edge-{index}" for site in ordered
            for index in range(counts[site] - standby.get(site, 0) + 1, counts[site] + 1)]
    slot_names = [node["name"] for node in nodes if node.get("slot")]
    created_hourly = sum(PRICES[node["type"]] for node in nodes if not node.get("slot"))
    every_hourly = sum(PRICES[node["type"]] for node in nodes)
    views = sorted({view[site] for site in ordered})
    view_text = (f"a {views[0]} view host" if len(views) == 1 else
                 "a view host (" + ", ".join(f"{view[site]} in {site}" for site in ordered) + ")")
    description = (
        about[0]
        + f"in each region two forwarders and {', '.join(f'{n} edges in {s}' for s, n in counts.items())}, "
        "all g6-dedicated-2; listener load hosts "
        + ", ".join(f"{load[s][0]} x {load[s][1]} in {s}" for s in ordered) + " "
        "(needletail-load for the UDP listeners, ntcmp for the LL-HLS ones)"
        + (f" and {view_text} for the full probes" if view_hosts else "")
        + ", on the region's VLAN (Linode does not bill VLAN traffic; "
        "5,000 UDP listeners of 1 ms PCM over the public network would be about 12 TB an hour "
        "of billed transfer)"
        + (", and a self-hosted LiveKit SFU (g6-dedicated-2) per region" if livekit else "")
        + "; the contributor alone on a g6-dedicated-4 in London"
        + (f", its standby {STANDBY_CONTRIBUTOR[0]} alone on another (both publish the same "
           "parts)" if contributors == 2 else "")
        + " with its source host lon-src. Dedicated plans only. Sydney "
        "first: the core of the region short of capacity is created first; the run works "
        "without it (lab up --optional-sites sydney, or --without-sites sydney). The listener "
        "hosts are created last, in batches: the lab is kept when the account refuses some "
        "(listeners_missing), and scale-run.py fits its steps to the hosts that exist. "
        + (f"Standby edges ({', '.join(held)}): created, held out by the deploy setting "
           "standby_edges until they are started in (deploy start --nodes, or the "
           "autoscaler). " if held else "")
        + (f"Autoscale slots ({', '.join(slot_names)}): in every peer list from the first "
           "push, created only by the autoscaler (lab add), never by up or grow. "
           if slot_names else "")
        + f"Up creates {len(nodes) - len(slot_names)} nodes at ${created_hourly:.3f} an hour; "
        f"with every slot {len(nodes)} nodes at ${every_hourly:.3f}, so the lab's ceilings are "
        f"NEEDLETAIL_LINODE_MAX_NODES={len(nodes)} and NEEDLETAIL_LINODE_MAX_HOURLY_USD="
        f"{ceiling_usd(every_hourly):.2f} or more. "
        + about[1])
    return {
        "description": description,
        "image": "linode/debian13",
        "standby_edges": held,
        "autoscale_slots": slot_names,
        "scale": {"sites": ordered, "edges": counts,
                  "load_hosts": {site: {"count": load[site][0], "type": load[site][1]}
                                 for site in ordered}},
        "nodes": nodes,
    }


# The proving lab (C-operations 2.9): both contributors, three serving edges
# and one standby edge a region, the default slots (`<s>-dist-c`,
# `<s>-edge-5`), one g6-dedicated-8 listener host and one g6-dedicated-4
# view host a region, the self-hosted LiveKit nodes. No `-syd2` file: its
# g6-dedicated-2 listener and view hosts break the owner's rule for load
# hosts; the fallback is the lab without Sydney.
PROVING = {"edges": {site: 4 for site in DEFAULT_SITES},
           "standby": {site: 1 for site in DEFAULT_SITES},
           "load_hosts": 1, "load_type": "g6-dedicated-8", "load_by_site": {},
           "contributors": 2, "about": PROVING_ABOUT}
# The committed files: (file name, arguments of scale_topology).
VARIANTS = {
    "topology-scale.json": {"slots": {"london": {"edges": 2}}},
    "topology-proving.json": PROVING,
}
# The party's committed files (party_plan at PARTY_DEFAULTS).
PARTY_FILE = "topology-party.json"
PARTY_MD = "PARTY.md"


def render(document: dict) -> str:
    return json.dumps(document, indent=2) + "\n"


# The global listening party (owner, 23 November 2026): one Opus stream to
# listeners in every region, ramped to tens of thousands. Every figure below
# is a planning estimate that the proving run replaces, so each is an
# argument of party_plan (and a flag) and the plan prints it.
LANES = {
    # `leg_mbps`: one subscription (a one-path listener) on the wire,
    # scale-run.py CONFIGS' planning figures.
    "opus-1": {"what": "1 ms Opus Custom", "part_ms": 1.0, "leg_mbps": 1.2},
    "opus-2.5": {"what": "standard 2.5 ms Opus", "part_ms": 2.5, "leg_mbps": 0.6},
}
# The transfer that each instance adds to the account's monthly pool
# (Linode's dedicated plans: 4, 5 and 6 TB), accrued by the hour of a month
# of 730 hours.
TRANSFER_GB_A_MONTH = {"g6-dedicated-2": 4000, "g6-dedicated-4": 5000, "g6-dedicated-8": 6000}
HOURS_A_MONTH = 730
TRANSFER_USD_PER_GB = 0.005
# A forwarder of the merged topology feeds every edge of its site; the
# deploy refuses more than its setting max_edges_per_forwarder (default 16,
# linode-small-mesh-deploy.py check_edge_fanout) and passes the larger of the
# two as --relay-max-lease-children.
DEPLOY_EDGES_PER_FORWARDER = 16
# The autoscaler's trigger to add an edge (linode-autoscale.py Config): a
# region's mean edge load at up_permille of client_udp_datagram_capacity, or
# one edge at up_max_permille with the mean at target_permille.
AUTOSCALE_UP_PERMILLE = 700
AUTOSCALE_UP_MAX_PERMILLE = 850
AUTOSCALE_TARGET_PERMILLE = 600
PARTY_DEFAULTS = {
    "sites": DEFAULT_SITES,
    "listeners": {site: 5000 for site in DEFAULT_SITES},
    "lane": "opus-1",
    "paths": 2,
    # About 400,000 client datagrams a second fill a g6-dedicated-2 edge
    # (3.0-3.3 us a datagram, wave 7; scale-run DATAGRAM_CAPACITY); the
    # autoscaler adds an edge at 700 permille of it.
    "edge_datagrams_per_s": 400_000,
    "edge_fill": 0.70,
    # A leg's datagrams per part: about 1,031 a second at 1 ms (C 7).
    "datagrams_per_part": 1.031,
    "leg_mbps": None,
    # needletail-load: 0.0055 vCPU per one-path listener of 1,000 parts a
    # second (3.96 us a datagram plus 1.5 us of NIC softirq), scaled by the
    # lane's parts a second and the paths; a host filled to 70%.
    "load_vcpu_per_listener": 0.0055,
    "load_fill": 0.70,
    "load_type": "g6-dedicated-8",
    # Sydney's listener hosts g6-dedicated-4 (on 22 November it had no
    # g6-dedicated-4 or larger and refused g6-dedicated-2 part way; listener
    # hosts are -4 or -8 only, so the region is optional: up
    # --optional-sites sydney).
    "load_type_by_site": {"sydney": "g6-dedicated-4"},
    "standby_edges": 1,
    "edge_slots": 2,
    "forwarder_slots": 1,
    # The self-hosted LiveKit control: one SFU a region at the count it
    # carries (scale-run LK_PER_SFU), its crowd on the listener hosts
    # (0.02 vCPU a subscriber at 1,000 units a second, 400 a second here)
    # and over the public network (0.42 Mbit/s a subscriber of 2.5 ms Opus).
    "livekit": True,
    "lk_listeners": 450,
    "lk_vcpu_per_listener": 0.008,
    "lk_mbps": 0.42,
    # Copies of the stream into each region outside London: its two
    # forwarders each fed by both London forwarders.
    "long_haul_copies": 4,
    "hours": 4.0,
    # The budget of the whole work (owner, 23 November), what is spent
    # (.resilient/ledger.md: $40.65), what is held for others (the cloud
    # session's $40 and the stall hunt with one repeat, $6.26: D-hunt 7.1),
    # and the proving run before the party (its file, every node and slot
    # for its billed hours: phases-proving.json is 483.6 minutes, with 20
    # minutes of bring-up two labs of 5 hours each under the lab tool's
    # 6-hour watchdog limit, SCALE.md "The proving run").
    "budget_usd": 300.0,
    "spent_usd": 41.0,
    "reserved_usd": 46.26,
    "proving_hours": 10.0,
}
PARTY_PLANS = [(listeners, lane, paths) for listeners in (5000, 10000, 20000)
               for lane in LANES for paths in (2, 1)]


def vcpus(kind: str) -> int:
    return int(kind.rsplit("-", 1)[1])


def whole_hours(hours: float) -> int:
    return max(1, math.ceil(hours - 1e-9))


def proving_cost(hours: float) -> float:
    """The proving run's cost: every node and slot of topology-proving.json
    for `hours`, each billed whole hours."""
    document = scale_topology(**VARIANTS["topology-proving.json"])
    return whole_hours(hours) * sum(PRICES[node["type"]] for node in document["nodes"])


def party_plan(**given) -> dict:
    """The party's plan: PARTY_DEFAULTS with `given` over them. Per region:
    legs (listeners x paths), the legs an edge carries (edge datagrams x
    fill / the lane's datagrams a leg), the serving edges (at least two, and
    at least the paths), the standby edges and slots, the listener hosts
    (Needletail's legs and LiveKit's crowd within the host's fill); then
    the nodes by plan and region, the price, vCPUs, traffic and budget."""
    unknown = set(given) - set(PARTY_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown party arguments {sorted(unknown)}")
    p = {**PARTY_DEFAULTS, **given}
    sites = tuple(site for site in SITES if site in p["sites"])
    if "london" not in sites or len(sites) != len(p["sites"]):
        raise ValueError(f"sites {p['sites']}: known {', '.join(SITES)}, london among them")
    if p["lane"] not in LANES:
        raise ValueError(f"lane {p['lane']}: one of {', '.join(LANES)}")
    if p["paths"] not in (1, 2):
        raise ValueError(f"{p['paths']} paths a listener; 1 or 2")
    for key in ("edge_datagrams_per_s", "edge_fill", "datagrams_per_part",
                "load_vcpu_per_listener", "load_fill", "hours"):
        if not p[key] > 0:
            raise ValueError(f"{key} {p[key]}: must be positive")
    for key in ("standby_edges", "edge_slots", "forwarder_slots", "lk_listeners"):
        if p[key] < 0:
            raise ValueError(f"{key} {p[key]}: must not be negative")
    listeners = {site: int(p["listeners"].get(site, 0)) for site in sites}
    if set(p["listeners"]) - set(sites) or any(count < 0 for count in listeners.values()):
        raise ValueError(f"listeners {p['listeners']}: a count for sites of {', '.join(sites)}")
    if "load_type_by_site" not in given:
        p["load_type_by_site"] = {site: kind for site, kind in p["load_type_by_site"].items()
                                  if site in sites}
    for site in p["load_type_by_site"]:
        if site not in sites:
            raise ValueError(f"listener hosts given for {site}, which is not in the party")
    load_types = {site: p["load_type_by_site"].get(site, p["load_type"]) for site in sites}
    for site, kind in load_types.items():
        if kind not in LOAD_TYPES:
            raise ValueError(f"{site}: load hosts {kind}: {' or '.join(LOAD_TYPES)} only")
    lane = LANES[p["lane"]]
    leg_mbps = lane["leg_mbps"] if p["leg_mbps"] is None else p["leg_mbps"]
    parts_per_s = 1000 / lane["part_ms"]
    leg_datagrams = parts_per_s * p["datagrams_per_part"]
    legs_per_edge = int(p["edge_datagrams_per_s"] * p["edge_fill"] // leg_datagrams)
    if legs_per_edge < 1:
        raise ValueError("an edge carries no leg at these figures")
    leg_vcpu = p["load_vcpu_per_listener"] * parts_per_s / 1000
    regions = {}
    for site in sites:
        legs = listeners[site] * p["paths"]
        serving = max(2, p["paths"], math.ceil(legs / legs_per_edge))
        edges = serving + p["standby_edges"]
        if edges + p["edge_slots"] > MAX_EDGES:
            raise ValueError(f"{site}: {serving} serving edges, {p['standby_edges']} standby "
                             f"and {p['edge_slots']} slots; at most {MAX_EDGES} a region")
        kind = load_types[site]
        # LiveKit's crowd: the count an SFU carries, the region's listeners
        # when fewer.
        lk_listeners = min(p["lk_listeners"], listeners[site]) if p["livekit"] else 0
        load_vcpu = legs * leg_vcpu + lk_listeners * p["lk_vcpu_per_listener"]
        hosts = max(1, math.ceil(load_vcpu / (vcpus(kind) * p["load_fill"]) - 1e-9))
        regions[site] = {
            "listeners": listeners[site], "legs": legs, "serving_edges": serving,
            "standby_edges": p["standby_edges"], "edge_slots": p["edge_slots"],
            "forwarder_slots": p["forwarder_slots"], "load_type": kind, "load_hosts": hosts,
            "load_vcpu": load_vcpu,
            "listeners_per_load_host": int(vcpus(kind) * p["load_fill"] // (p["paths"] * leg_vcpu)),
            "lk_listeners": lk_listeners,
        }
    document = scale_topology(
        sites=sites, edges={site: r["serving_edges"] + r["standby_edges"]
                            for site, r in regions.items()},
        standby={site: r["standby_edges"] for site, r in regions.items()},
        slots={site: {"edges": r["edge_slots"], "forwarders": r["forwarder_slots"]}
               for site, r in regions.items()},
        load_by_site={site: (r["load_hosts"], r["load_type"]) for site, r in regions.items()},
        livekit=p["livekit"], contributors=2, about=party_about(p, sites, listeners))
    created = [node for node in document["nodes"] if not node.get("slot")]
    slots = [node for node in document["nodes"] if node.get("slot")]
    table: dict[str, dict[str, int]] = {}
    for node in created:
        row = table.setdefault(node["site"], {})
        row[node["type"]] = row.get(node["type"], 0) + 1
    hourly = sum(PRICES[node["type"]] for node in created)
    slots_hourly = sum(PRICES[node["type"]] for node in slots)
    billed = whole_hours(p["hours"])
    # Traffic, Mbit/s. VLAN (not billed): the edges to the listener hosts,
    # both forwarders to every edge, and in London the source to both
    # contributors and both contributors to both forwarders.
    vlan = {site: r["legs"] * leg_mbps + 2 * (r["serving_edges"] + r["standby_edges"]) * leg_mbps
            + (6 * leg_mbps if site == "london" else 0) for site, r in regions.items()}
    # Public (outbound billed): the copies into the other regions and the
    # LiveKit crowd, whose SFUs advertise their public address.
    public = {site: (0 if site == "london" else p["long_haul_copies"] * leg_mbps)
              + r["lk_listeners"] * p["lk_mbps"] for site, r in regions.items()}
    gb_per_hour = sum(public.values()) * 3600 / 8 / 1000
    pool_gb = billed * sum(TRANSFER_GB_A_MONTH[node["type"]] for node in created) / HOURS_A_MONTH
    before = proving_cost(p["proving_hours"])
    left = p["budget_usd"] - p["spent_usd"] - p["reserved_usd"] - before
    run_usd = billed * hourly
    overage = max(0.0, gb_per_hour * p["hours"] - pool_gb) * TRANSFER_USD_PER_GB
    worst_usd = billed * (hourly + slots_hourly) + overage
    roster_edges = max(r["serving_edges"] + r["standby_edges"] + r["edge_slots"]
                       for r in regions.values())
    # The serving edges' mean load at the plan's listeners, the gauge the
    # autoscaler reads (client datagrams over the deploy's capacity).
    for r in regions.values():
        r["edge_permille"] = round(1000 * r["legs"] * leg_datagrams
                                   / (r["serving_edges"] * p["edge_datagrams_per_s"]))
    # What the lab is when `up --optional-sites sydney` goes on without
    # Sydney: the same file less Sydney's nodes, and its listeners.
    without_sydney = None
    if "sydney" in regions:
        rest = [node for node in created if node["site"] != "sydney"]
        rest_hourly = sum(PRICES[node["type"]] for node in rest)
        without_sydney = {
            "listeners": sum(r["listeners"] for site, r in regions.items() if site != "sydney"),
            "instances": len(rest), "vcpus": sum(vcpus(node["type"]) for node in rest),
            "hourly_usd": rest_hourly, "run_usd": billed * rest_hourly}
    return {
        "inputs": {**p, "sites": list(sites), "listeners": listeners, "leg_mbps": leg_mbps,
                   "load_type_by_site": dict(p["load_type_by_site"])},
        "lane": lane, "parts_per_s": parts_per_s, "leg_datagrams": leg_datagrams,
        "legs_per_edge": legs_per_edge, "leg_vcpu": leg_vcpu,
        "listeners_per_edge": legs_per_edge // p["paths"],
        "regions": regions, "document": document,
        "table": table, "instances": len(created), "slots": len(slots),
        "vcpus": sum(vcpus(node["type"]) for node in created),
        "slot_vcpus": sum(vcpus(node["type"]) for node in slots),
        "hourly_usd": hourly, "slots_hourly_usd": slots_hourly, "billed_hours": billed,
        "run_usd": run_usd, "worst_usd": worst_usd,
        "vlan_mbps": vlan, "public_mbps": public, "public_gb_per_hour": gb_per_hour,
        "public_gb": gb_per_hour * p["hours"], "pool_gb": pool_gb,
        "overage_usd": overage, "proving_usd": before, "left_usd": left,
        "fits_budget": worst_usd <= left,
        "roster_edges": roster_edges,
        "edge_permille": max(r["edge_permille"] for r in regions.values()),
        "without_sydney": without_sydney,
        "max_edges_per_forwarder": roster_edges if roster_edges > DEPLOY_EDGES_PER_FORWARDER
        else None,
        "max_nodes": len(created) + len(slots),
        "max_hourly_usd": ceiling_usd(hourly + slots_hourly),
    }


def party_about(p: dict, sites: tuple[str, ...], listeners: dict[str, int]) -> tuple[str, str]:
    lane = LANES[p["lane"]]
    total = sum(listeners.values())
    return (
        "The global listening party (owner, 23 November 2026: one Opus stream to a global "
        f"audience, ramped to tens of thousands), sized for {total:,} listeners of "
        f"{lane['what']} ({p['lane']}) with {p['paths']} path{'s' if p['paths'] > 1 else ''} "
        f"each ({', '.join(f'{listeners[s]:,} in {s}' for s in sites)}), merged topology: ",
        "Planning figures (the proving run replaces them): "
        f"{p['edge_datagrams_per_s']:,} client datagrams a second fill an edge, planned to "
        f"{p['edge_fill']:.0%}; {p['load_vcpu_per_listener']} vCPU per one-path listener of "
        f"1,000 parts a second on a listener host, planned to {p['load_fill']:.0%}. "
        "Generated by scripts/scale_topology.py party; the plan, its cost and its assumptions "
        "are in deploy/low-latency/PARTY.md.")


def party_document(plan: dict) -> dict:
    """The party's topology document with its plan's inputs and per-region
    figures under "party" (for scale-run's party schedule)."""
    return {**plan["document"], "party": {
        "inputs": plan["inputs"], "legs_per_edge": plan["legs_per_edge"],
        "regions": plan["regions"], "hourly_usd": round(plan["hourly_usd"], 3),
        "slots_hourly_usd": round(plan["slots_hourly_usd"], 3)}}


def plan_text(plan: dict) -> str:
    """The plan in Markdown: every input, the nodes by plan and region,
    dollars an hour, the run's cost, vCPUs, traffic, budget and fit."""
    p = plan["inputs"]
    lane = plan["lane"]
    total = sum(r["listeners"] for r in plan["regions"].values())
    types = sorted({kind for row in plan["table"].values() for kind in row}, key=vcpus)
    lines = [
        f"### {total:,} listeners, {p['lane']} ({lane['what']}), {p['paths']} "
        f"path{'s' if p['paths'] > 1 else ''}",
        "",
        f"- Legs: listeners x {p['paths']}. A leg is {plan['parts_per_s']:,.0f} parts a second x "
        f"{p['datagrams_per_part']} = {plan['leg_datagrams']:,.1f} datagrams a second and "
        f"{p['leg_mbps']} Mbit/s.",
        f"- An edge: {p['edge_datagrams_per_s']:,} datagrams a second x {p['edge_fill']:.2f} = "
        f"{plan['legs_per_edge']} legs ({plan['listeners_per_edge']} listeners).",
        f"- A listener host: {p['load_vcpu_per_listener']} vCPU per one-path listener of 1,000 "
        f"parts a second, so {plan['leg_vcpu']:.4f} a leg; filled to {p['load_fill']:.2f}"
        + (f"; LiveKit's crowd up to {p['lk_listeners']} a region at "
           f"{p['lk_vcpu_per_listener']} vCPU each" if p["livekit"] else "") + ".",
        "",
        "| Region | Listeners | Legs | Serving edges | Standby | Edge slots | Forwarders "
        "(+ slots) | Listener hosts | Needletail listeners a host |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for site, r in plan["regions"].items():
        lines.append(f"| {site} | {r['listeners']:,} | {r['legs']:,} | {r['serving_edges']} | "
                     f"{r['standby_edges']} | {r['edge_slots']} | 2 (+{r['forwarder_slots']}) | "
                     f"{r['load_hosts']} x {r['load_type']} | {r['listeners_per_load_host']:,} |")
    lines += ["", "| Region | " + " | ".join(types) + " | Instances | vCPUs | $/h |",
              "| --- | " + " | ".join("---:" for _ in types) + " | ---: | ---: | ---: |"]
    for site, row in plan["table"].items():
        lines.append(f"| {site} | " + " | ".join(str(row.get(kind, 0)) for kind in types)
                     + f" | {sum(row.values())} | {sum(vcpus(k) * n for k, n in row.items())} | "
                     f"{sum(PRICES[k] * n for k, n in row.items()):.3f} |")
    lines += [
        f"| **created by up** | " + " | ".join(
            str(sum(row.get(kind, 0) for row in plan["table"].values())) for kind in types)
        + f" | **{plan['instances']}** | **{plan['vcpus']}** | **{plan['hourly_usd']:.3f}** |",
        "| slots, if all exist | " + " | ".join("" for _ in types)
        + f" | {plan['slots']} | {plan['slot_vcpus']} | {plan['slots_hourly_usd']:.3f} |",
        "",
        f"- Cost: ${plan['hourly_usd']:.3f} an hour; a run of {p['hours']:g} h bills "
        f"{plan['billed_hours']} whole hours of every instance: **${plan['run_usd']:.2f}**, "
        f"${plan['worst_usd']:.2f} at its worst (every slot made once and alive the whole "
        "run, and the overage below; a slot deleted and made again bills another whole hour, "
        "which this does not count and the lab tool's budget guard bounds).",
        *([f"- Without Sydney (`up --optional-sites sydney` goes on without it when Sydney "
            "refuses its core; on 22 November it had no g6-dedicated-4 or larger): "
            f"{rest['listeners']:,} listeners, {rest['instances']} instances, "
            f"{rest['vcpus']} vCPUs, ${rest['hourly_usd']:.3f} an hour, "
            f"${rest['run_usd']:.2f} for the run. To carry the whole audience in three "
            "regions, plan with `--sites london,newyork,tokyo`."]
          if (rest := plan["without_sydney"]) else []),
        f"- Instances and vCPUs: {plan['instances']} instances, {plan['vcpus']} vCPUs "
        f"({plan['max_nodes']} and {plan['vcpus'] + plan['slot_vcpus']} with the slots). The "
        "account's instance and vCPU limits are not known: check them before `up`, and stage "
        "it (the core first, the listener hosts in batches, as `up` creates them).",
        "- Traffic: VLAN (not billed) "
        + ", ".join(f"{site} {mbps / 1000:.2f} Gbit/s" for site, mbps in plan["vlan_mbps"].items())
        + f"; public {sum(plan['public_mbps'].values()):.0f} Mbit/s "
        f"({plan['public_gb_per_hour']:.0f} GB an hour, {plan['public_gb']:.0f} GB in the run: "
        f"{p['long_haul_copies']} copies of the stream into each region outside London"
        + (f", LiveKit's crowd of up to {p['lk_listeners']} a region at {p['lk_mbps']} "
           "Mbit/s" if p["livekit"] else "")
        + f"); the run's instances add {plan['pool_gb']:,.0f} GB to the transfer pool, so "
        f"overage ${plan['overage_usd']:.2f} even with nothing else of the pool left.",
        f"- Budget: ${p['budget_usd']:.0f} - ${p['spent_usd']:.2f} spent - "
        f"${p['reserved_usd']:.2f} held - ${plan['proving_usd']:.2f} for the proving run = "
        f"${plan['left_usd']:.2f} left; this run at its worst ${plan['worst_usd']:.2f}: "
        + ("**fits**." if plan["fits_budget"] else "**does not fit**."),
        "- Deploy: "
        + (f"{plan['roster_edges']} edges in a region's peer lists, over the forwarder's "
           f"{DEPLOY_EDGES_PER_FORWARDER}: deploy with `--set max_edges_per_forwarder="
           f"{plan['max_edges_per_forwarder']}` (not yet run on Linode)"
           if plan["max_edges_per_forwarder"] else
           f"{plan['roster_edges']} edges in a region's peer lists, within the forwarder's "
           f"{DEPLOY_EDGES_PER_FORWARDER}")
        + f"; `--set client_udp_datagram_capacity={p['edge_datagrams_per_s']}` (the edge "
        "capacity this plan assumes; the autoscaler's load is client datagrams over it, so "
        "any other value moves its trigger). Lab ceilings: "
        f"`NEEDLETAIL_LINODE_MAX_NODES={plan['max_nodes']} "
        f"NEEDLETAIL_LINODE_MAX_HOURLY_USD={plan['max_hourly_usd']:.2f}`.",
        f"- Autoscaler: at the plan's listeners the serving edges' mean load is up to "
        f"{plan['edge_permille']} permille; the autoscaler adds an edge at a mean of "
        f"{AUTOSCALE_UP_PERMILLE}, or one edge at {AUTOSCALE_UP_MAX_PERMILLE} with the mean at "
        f"{AUTOSCALE_TARGET_PERMILLE}. "
        + ("The top step runs at the trigger: legs spread unevenly there make it take the "
           "region's standby edge and slots, and the edge killed mid-party may find no spare."
           if plan["edge_permille"] >= AUTOSCALE_TARGET_PERMILLE else
           "The top step runs below the trigger."),
        "",
    ]
    return "\n".join(lines)


def even_split(total: int, sites: tuple[str, ...] = DEFAULT_SITES) -> dict[str, int]:
    """`total` listeners over `sites`, as evenly as whole listeners go (the
    first sites take the remainder)."""
    share, rest = divmod(total, len(sites))
    return {site: share + (1 if index < rest else 0) for index, site in enumerate(sites)}


def party_markdown() -> str:
    """deploy/low-latency/PARTY.md: the assumptions, the budget and the
    plans of PARTY_PLANS at the defaults."""
    d = PARTY_DEFAULTS
    plans = [(key, party_plan(listeners=even_split(key[0]), lane=key[1], paths=key[2]))
             for key in PARTY_PLANS]
    file_key = (sum(d["listeners"].values()), d["lane"], d["paths"])
    first = plans[0][1]
    file_plan = dict(plans)[file_key]
    lines = [
        "# The global listening party: topology and cost plans",
        "",
        "The owner, 23 November 2026: \"lets prepare for large listener runs of opus audio - "
        "simulating a global listening party with thousands or tens of thousands of users\". "
        "One stream, 1 ms Opus Custom (`opus-1`) or standard 2.5 ms Opus (`opus-2.5`) over UDP, "
        "listeners in London, New York, Tokyo and Sydney, ramped 1,000, 5,000, 10,000, 20,000 "
        "and on, joining in waves, an edge and a distributor killed mid-party, the autoscaler "
        "adding edges as it grows, self-hosted LiveKit and LiveKit Cloud as controls.",
        "",
        "Generated by `scripts/scale_topology.py --write` (with "
        "`deploy/low-latency/topology-party.json`, the topology of the "
        f"{file_key[0]:,}-listener `{file_key[1]}` plan with {file_key[2]} paths); do not edit "
        "by hand. Any other plan: `scripts/scale_topology.py plan --listeners N --lane L "
        "--paths P --hours H` (every figure below is a flag of it), and its topology: "
        "`scripts/scale_topology.py party` with the same flags.",
        "",
        "## Assumptions",
        "",
        "Planning estimates, not measurements on Linode. The proving run (C-operations 7) "
        "measures them; give the measured figures to `plan` and print again.",
        "",
        f"- **An edge** (g6-dedicated-2) is full at about {d['edge_datagrams_per_s']:,} client "
        "datagrams a second (wave 7: 3.0-3.3 us a datagram; scale-run's DATAGRAM_CAPACITY). "
        f"The plan fills it to {d['edge_fill']:.0%}, the autoscaler's trigger to add an edge "
        "(`--edge-datagrams-per-s`, `--edge-fill`). At that fill p99 may not stay within 1 ms "
        "of idle: wave 7 put that at 200-250 one-path 1 ms listeners an edge.",
        f"- **A leg** (one subscription; a two-path listener is two legs on two edges) is "
        f"{d['datagrams_per_part']} datagrams a part (repair rows; C 7: about 1,031 a second at "
        "1 ms) and, on the wire, "
        + " and ".join(f"{LANES[lane]['leg_mbps']} Mbit/s at `{lane}`" for lane in LANES)
        + " (scale-run's planning figures; `--datagrams-per-part`, `--leg-mbps`).",
        f"- **A listener host** (needletail-load) spends {d['load_vcpu_per_listener']} vCPU per "
        "one-path listener of 1,000 parts a second (in-process bench on the dev box: 3.96 us "
        "a datagram plus about 1.5 us of NIC softirq), scaled by the lane's parts a second and "
        f"the paths, filled to {d['load_fill']:.0%} (`--load-vcpu-per-listener`, "
        f"`--load-fill`). {d['load_type']} hosts, "
        + ", ".join(f"{kind} in {site}" for site, kind in d["load_type_by_site"].items())
        + " (`--load-type`, `--load-type-in SITE=TYPE`).",
        "- **Sydney** had no g6-dedicated-4 or larger capacity on 22 November and refused "
        "g6-dedicated-2 part way on several attempts (AGENTS.md). Listener hosts are "
        "g6-dedicated-4 or -8 only (owner), so the plans keep g6-dedicated-4 there and treat "
        "the region as optional: `up --optional-sites sydney` creates Sydney's core first and "
        "goes on without it when it is refused. Each plan prints the lab without Sydney (its "
        "listeners, instances, vCPUs and cost), and the summary its $/h.",
        f"- **The LiveKit control**: one self-hosted SFU a region carrying {d['lk_listeners']} "
        f"subscribers, its crowd on the listener hosts at {d['lk_vcpu_per_listener']} vCPU and "
        f"{d['lk_mbps']} Mbit/s of public traffic each (`--lk-listeners-per-region`, "
        "`--lk-vcpu-per-listener`, `--lk-mbps`, `--no-livekit`). LiveKit Cloud's egress is "
        "billed by LiveKit, not Linode, and is not in these figures.",
        f"- **Spares**: {d['standby_edges']} standby edge a region (created, held out: the "
        f"autoscaler's first join and the edge killed mid-party is replaced from it), "
        f"{d['edge_slots']} edge slots and {d['forwarder_slots']} forwarder slot (`dist-c`) a "
        "region that only the autoscaler creates (`--standby-edges`, `--edge-slots`, "
        "`--forwarder-slots`). Serving edges are sized for the plan's listeners, at least two "
        "a region.",
        "- **Fixed nodes**: two forwarders a region (g6-dedicated-2), both contributors alone "
        "on a g6-dedicated-4 each, the source host (g6-dedicated-4), a view host a region "
        "(g6-dedicated-4) for the probes, LiveKit's Redis.",
        f"- **Traffic**: listener and view hosts read the edges over the region's VLAN (not "
        f"billed). Public: {d['long_haul_copies']} copies of the stream into each region "
        "outside London (both forwarders fed by both of London's; `--long-haul-copies`) and "
        "LiveKit's crowd. Each instance adds its plan's transfer to the account's monthly "
        "pool (4, 5 and 6 TB for g6-dedicated-2, -4 and -8, by the hour of a 730-hour "
        f"month); past the pool ${TRANSFER_USD_PER_GB} a GB.",
        f"- **Cost**: every instance billed whole hours, at least one (`--hours`, default "
        f"{d['hours']:g}: the watchdog's hours). Prices of 22 November: "
        + ", ".join(f"{kind} ${price}" for kind, price in PRICES.items()) + " an hour.",
        "- **The warm pass at boot** (the deploy's `node_warm`, on since the stall lab of 24 "
        "November): every host the deploy pushes writes its free memory once before its "
        "service starts and is not ready before that; the pass is bounded at 300 s for every "
        "4 GB of the plan (a g6-dedicated-2 took 83 to 95 s on 24 November, one Tokyo edge more "
        "than 240 s). The lab's push takes that much longer (its hosts warm in parallel, the "
        "listener hosts' g6-dedicated-8 the longest); a slot the autoscaler creates serves that "
        "much later after its trigger; the standby edges, warmed at the push, join as fast as "
        "before. Every measured window starts only once every host reports warm "
        "(`linode-small-mesh-deploy.py status`: `warm pass: complete on all`).",
        "- **Not counted**: LL-HLS readers (scale-run sizes them), the view hosts' probes "
        "(a few listeners), more long-haul flows than the copies above (workstream D), "
        "LiveKit Cloud.",
        "",
        "## Budget",
        "",
        f"About ${d['budget_usd']:.0f} for the whole work (owner, 23 November), "
        f"${d['spent_usd']:.2f} spent (`.resilient/ledger.md`: $40.65), "
        f"${d['reserved_usd']:.2f} held for others (the cloud session's $40 and the stall hunt "
        f"with one repeat, $6.26), and the proving run before the party: every node and slot "
        f"of `topology-proving.json` for {d['proving_hours']:g} billed hours (two labs of 5 "
        "hours: `deploy/low-latency/SCALE.md`, \"The proving run\"), "
        f"${first['proving_usd']:.2f}. **${first['left_usd']:.2f} is left for the party** "
        "(`--budget-usd`, `--spent-usd`, `--reserved-usd`, `--proving-hours`). Every plan "
        "below is judged at its worst: every slot made once and alive for the whole run, and "
        "every GB of public traffic past the pool. A slot deleted and made again bills another "
        "whole hour; the worst does not count that, and the lab tool's budget guard bounds it.",
        "",
        "## Summary",
        "",
        "| Listeners | Lane | Paths | Edges a region (serving) | Listener hosts | Instances "
        "| vCPUs | $/h | Run of "
        f"{d['hours']:g} h | Worst | Fits the budget | max_edges_per_forwarder | Without Sydney "
        "(instances, $/h) |",
        "| ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for (listeners, lane, paths), plan in plans:
        serving = sorted({r["serving_edges"] for r in plan["regions"].values()})
        lines.append(
            f"| {listeners:,}{' (topology-party.json)' if (listeners, lane, paths) == file_key else ''}"
            f" | {lane} | {paths} | {', '.join(map(str, serving))} | "
            f"{sum(r['load_hosts'] for r in plan['regions'].values())} | {plan['instances']} | "
            f"{plan['vcpus']} | {plan['hourly_usd']:.3f} | ${plan['run_usd']:.2f} | "
            f"${plan['worst_usd']:.2f} | {'yes' if plan['fits_budget'] else 'no'} | "
            f"{plan['max_edges_per_forwarder'] or 'default (16)'} | "
            f"{plan['without_sydney']['instances']}, "
            f"{plan['without_sydney']['hourly_usd']:.3f} |")
    lines += [
        "",
        "What the summary says: two paths cost about twice the edges and listener hosts of "
        "one; the 1 ms lane about two and a half times the 2.5 ms lane's. A plan whose "
        "region has more than 16 edges in its peer lists needs the deploy setting "
        "`max_edges_per_forwarder` raised: a forwarder then feeds that many edges, which no "
        "run has measured (the forwarders send each edge the whole stream; the proving run "
        "has four a region). The account's instance and vCPU limits are not known: check them "
        "before `up` and stage it.",
        "",
        f"The party is one lab: `topology-party.json` is the {file_key[0]:,}-listener "
        f"`{file_key[1]}` plan with {file_key[2]} paths, the heaviest above, and the other "
        "lanes, path counts and the ramp's lower steps run on it (they need fewer edges and "
        "hosts; the deploy's `standby_edges` holds out what a step does not use). At its worst "
        f"it costs ${file_plan['worst_usd']:.2f} of the ${file_plan['left_usd']:.2f} left, "
        f"which leaves ${file_plan['left_usd'] - file_plan['worst_usd']:.2f} for a repeat or "
        "a longer run. Each instance created is billed an hour at least, even if deleted at "
        "once.",
        "",
        "## Plans",
        "",
    ]
    for _key, plan in plans:
        lines.append(plan_text(plan))
    return "\n".join(lines)


def site_values(values: list[str], flag: str, parse) -> dict:
    """{site: parse(value)} of SITE=VALUE arguments."""
    out = {}
    for value in values:
        site, _, rest = value.partition("=")
        try:
            out[site] = parse(rest)
        except ValueError:
            raise SystemExit(f"{flag} {value!r}: see --help") from None
    return out


def parse_slots(text: str) -> dict[str, int]:
    out = {}
    for part in text.split(","):
        key, _, count = part.partition(":")
        out[key] = int(count)
    return out


def parse_load(text: str) -> tuple[int, str]:
    count, _, kind = text.partition(":")
    if not kind:
        raise ValueError(text)
    return int(count), kind


def variant_arguments(args: argparse.Namespace) -> dict:
    """The arguments of scale_topology: the variant's, with the flags given
    over them. A SITE=... flag changes that site only: it is merged over the
    variant's values, or over scale_topology's per-site defaults where the
    variant has none (and over the site's other slot counts for --slots). A
    site that --sites leaves out takes the variant's values with it, but a
    flag's site is passed on as given, so that scale_topology refuses a site
    that is not in the topology."""
    if args.variant not in VARIANTS:
        raise SystemExit(f"--variant {args.variant}: one of {', '.join(VARIANTS)}")
    kwargs = dict(VARIANTS[args.variant])
    if args.sites is not None:
        kwargs["sites"] = tuple(site for site in args.sites.split(",") if site)
    sites = kwargs.get("sites", DEFAULT_SITES)

    for key, given in (("edges", site_values(args.edges_in, "--edges-in", int)),
                       ("standby", site_values(args.standby, "--standby", int)),
                       ("slots", site_values(args.slots, "--slots", parse_slots)),
                       ("load_by_site", site_values(args.load, "--load", parse_load)),
                       ("view_by_site", site_values(args.view, "--view", str))):
        own = kwargs.get(key)
        if own is not None:
            own = {site: value for site, value in own.items() if site in sites}
        if given and own is None:
            # The per-site defaults scale_topology takes when the key is unset
            # (the edges, merged above, decide the default standby).
            if key == "standby":
                own = default_standby(sites, kwargs.get("edges") or {})
            elif key == "load_by_site":
                own = {site: value for site, value in LOAD_BY_SITE.items() if site in sites}
            else:
                own = {}
        if key == "slots" and own is not None:
            given = {site: {**own.get(site, {}), **value} for site, value in given.items()}
        if own is not None:
            kwargs[key] = {**own, **given}
    if args.load_hosts is not None:
        kwargs["load_hosts"] = args.load_hosts
    if args.load_type is not None:
        kwargs["load_type"] = args.load_type
    if args.contributors is not None:
        kwargs["contributors"] = args.contributors
    if args.no_livekit:
        kwargs["livekit"] = False
    return kwargs


def party_arguments(argv: list[str]) -> dict:
    """party_plan's arguments from the flags of `party` and `plan`: each
    figure of PARTY_DEFAULTS, the default printed in --help."""
    d = PARTY_DEFAULTS
    parser = argparse.ArgumentParser(
        prog="scale_topology.py party|plan",
        description="The listening party's topology (party) or its plan (plan).")
    parser.add_argument("--sites", default=",".join(d["sites"]))
    parser.add_argument("--listeners", type=int,
                        help="listeners in all, split evenly over the sites "
                             f"(default {sum(d['listeners'].values()):,})")
    parser.add_argument("--listeners-per-region", action="append", default=[],
                        metavar="SITE=N", help="a region's listeners, over --listeners' share")
    parser.add_argument("--lane", choices=tuple(LANES), default=d["lane"])
    parser.add_argument("--paths", type=int, choices=(1, 2), default=d["paths"])
    for key, kind in (("edge_datagrams_per_s", int), ("edge_fill", float),
                      ("datagrams_per_part", float), ("leg_mbps", float),
                      ("load_vcpu_per_listener", float), ("load_fill", float),
                      ("standby_edges", int), ("edge_slots", int), ("forwarder_slots", int),
                      ("lk_vcpu_per_listener", float), ("lk_mbps", float),
                      ("long_haul_copies", int), ("hours", float), ("budget_usd", float),
                      ("spent_usd", float), ("reserved_usd", float), ("proving_hours", float)):
        parser.add_argument("--" + key.replace("_", "-"), type=kind, default=d[key], dest=key,
                            help="default " + ("the lane's" if d[key] is None else str(d[key])))
    parser.add_argument("--lk-listeners-per-region", type=int, default=d["lk_listeners"],
                        dest="lk_listeners", help=f"default {d['lk_listeners']}")
    parser.add_argument("--load-type", choices=LOAD_TYPES, default=d["load_type"])
    parser.add_argument("--load-type-in", action="append", default=[], metavar="SITE=TYPE",
                        help="a region's listener host plan (default "
                             + ", ".join(f"{s}={k}" for s, k in d["load_type_by_site"].items())
                             + ")")
    parser.add_argument("--no-livekit", action="store_true")
    args = parser.parse_args(argv)
    sites = tuple(site for site in args.sites.split(",") if site)
    listeners = (even_split(args.listeners, sites) if args.listeners is not None
                 else {site: count for site, count in d["listeners"].items() if site in sites})
    listeners.update(site_values(args.listeners_per_region, "--listeners-per-region", int))
    given = {key: value for key, value in vars(args).items()
             if key in PARTY_DEFAULTS and key not in ("sites", "listeners", "load_type_by_site")}
    return {**given, "sites": sites, "listeners": listeners, "livekit": not args.no_livekit,
            "load_type_by_site": {**{site: kind for site, kind in d["load_type_by_site"].items()
                                     if site in sites},
                                  **site_values(args.load_type_in, "--load-type-in", str)}}


def write_files() -> None:
    for name, kwargs in VARIANTS.items():
        (OUT / name).write_text(render(scale_topology(**kwargs)))
        print(f"wrote {OUT / name}")
    (OUT / PARTY_FILE).write_text(render(party_document(party_plan())))
    print(f"wrote {OUT / PARTY_FILE}")
    (OUT / PARTY_MD).write_text(party_markdown())
    print(f"wrote {OUT / PARTY_MD}")


def main() -> None:
    if sys.argv[1:2] in (["party"], ["plan"]):
        try:
            plan = party_plan(**party_arguments(sys.argv[2:]))
        except ValueError as error:
            sys.exit(str(error))
        sys.stdout.write(render(party_document(plan)) if sys.argv[1] == "party"
                         else plan_text(plan))
        return
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--variant", default="topology-scale.json",
                        help=f"the file whose arguments the flags change ({', '.join(VARIANTS)})")
    parser.add_argument("--sites")
    parser.add_argument("--edges-in", action="append", default=[], metavar="SITE=COUNT")
    parser.add_argument("--standby", action="append", default=[], metavar="SITE=N",
                        help="how many of the site's last edges are on standby")
    parser.add_argument("--slots", action="append", default=[],
                        metavar="SITE=edges:N,forwarders:N")
    parser.add_argument("--load-hosts", type=int, help="listener hosts of every site")
    parser.add_argument("--load-type", help="the listener hosts' plan in every site")
    parser.add_argument("--load", action="append", default=[], metavar="SITE=COUNT:TYPE")
    parser.add_argument("--view", action="append", default=[], metavar="SITE=TYPE")
    parser.add_argument("--contributors", type=int, choices=(1, 2))
    parser.add_argument("--no-livekit", action="store_true")
    parser.add_argument("--write", action="store_true",
                        help=f"rewrite the committed files ({', '.join(VARIANTS)}, "
                             f"{PARTY_FILE}, {PARTY_MD})")
    args = parser.parse_args()
    if args.write:
        write_files()
        return
    try:
        document = scale_topology(**variant_arguments(args))
    except ValueError as error:
        sys.exit(str(error))
    sys.stdout.write(render(document))


if __name__ == "__main__":
    main()
