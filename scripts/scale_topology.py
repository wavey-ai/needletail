#!/usr/bin/env python3
"""The topology of the large audio mesh (deploy/low-latency/topology-scale.json)
and its variants, generated so that every file of the plan has the same shape.

The merged topology (relay and distributor are one forwarder), audio only, in
London, New York, Tokyo and Sydney:

  per region   two forwarders (`<site>-dist-a`, `-b`, g6-dedicated-2), ten
               edges (`<site>-edge-1`..10, g6-dedicated-2; the last five of
               each region on standby for the first curve), eight listener
               load hosts (`<site>-load-1`..8, g6-dedicated-8: needletail-load
               and ntcmp; Sydney sixteen g6-dedicated-4, the same vCPUs, for
               it has no g6-dedicated-8 capacity), one view host for the full probes
               (`<site>-view`, g6-dedicated-4, as the comparison's) and one
               self-hosted LiveKit SFU (`<site>-lk`, g6-dedicated-2). The
               listener and view hosts are on the region's VLAN (.100 up)
               and read the edges there: Linode does not bill VLAN traffic,
               and 5,000 UDP listeners of 1 ms PCM over the public network
               would be about 28 Gbit/s, 12 TB an hour of billed transfer
  London       the contributor alone on a g6-dedicated-4 (`contrib-lon`), the
               source host (`lon-src`, g6-dedicated-4) and the Redis of the
               LiveKit multi-region set (`lk-redis`, g6-dedicated-2), which the
               comparison's LiveKit installer needs

Sized for the owner's budget of 22 November (first about $60 to $70, then
the shared $100 of the Needletail work, the failover, redistribution and
autoscale phases included; the watchdog caps the lab at $72.95 in 7 h, the
schedule with the joins phase in every step, or $62.53 in 6 h, plus
whatever slots the autoscaler creates): as many simulated listeners as it
buys, and in
each region enough edges that on the second curve the edges, not the
listener hosts, give out first (deploy/low-latency/SCALE.md has the
arithmetic). Edges 6 to 10 of each region are the second curve's: the
document names them in `standby_edges`, which the first curve deploys with
(scale-run.py settings), and the second starts them in (deploy start
--nodes, which restarts nothing else: every peer list names them from the
first push).

Autoscale slots (`autoscale_slots`, nodes marked `"slot": true`): nodes of
the topology that `lab up` and `grow` do not create. The deploy renders them
into every peer list from the first push (a slot not created yet is a peer
that is down), so the autoscaler (scripts/linode-autoscale.py) creates one
with `lab add <slot>` and starts it with `deploy start --nodes <slot>`,
and nothing else restarts. London has two edge slots past its ten edges
(`lon-edge-11`, `-12`), London and New York a third forwarder each
(`lon-dist-c`, `new-dist-c`: a fast-plane lease child of its site's dist-a
and dist-b and a cache-mesh parent of its edges, never a compiled relay
child, so no other node's relay ports move).

Sydney's nodes come first in the file: it is the region short of dedicated
capacity. `lab up --optional-sites sydney` creates its core before the rest (its
listener hosts with the other regions', after the core) and goes on
without Sydney when its core cannot be created; `--without-sites sydney`
leaves it out (or a file made with --sites london,newyork,tokyo).

  scale_topology.py                       print topology-scale.json
  scale_topology.py --edges-in london=8   the second curve: eight London edges
  scale_topology.py --write               rewrite the committed files
"""

from __future__ import annotations

import argparse
import json
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
# contributor .5, source .30 (as in topology-merged.json; the edges stop
# short of it below 9 edges and skip it above).
MAX_EDGES = 40
# Autoscale slots per site: extra edges past the site's edges (numbered on)
# and extra forwarders (`<short>-dist-c`). VLAN host of a forwarder slot:
# FORWARDER_SLOT_HOST + index (.13 up, after dist-a .11 and dist-b .12).
SLOTS_BY_SITE = {"london": {"edges": 2, "forwarders": 1}, "newyork": {"forwarders": 1}}
FORWARDER_SLOT_HOST = 13
FORWARDER_LETTERS = "cdefgh"


def edge_host(index: int) -> int:
    """The VLAN host number of edge `index` (1-based): .21 up, skipping .30
    (the source host's address in London) and .5."""
    host = 20 + index
    if host >= 30:
        host += 1
    return host


def scale_topology(sites: tuple[str, ...] = DEFAULT_SITES, edges: dict[str, int] | None = None,
                   load_hosts: int = LOAD_HOSTS, load_type: str = LOAD_TYPE,
                   load_by_site: dict[str, tuple[int, str]] | None = None,
                   view_hosts: bool = True, livekit: bool = True,
                   standby: dict[str, int] | None = None,
                   slots: dict[str, dict[str, int]] | None = None) -> dict:
    """The topology document for `sites` (London must be one: the contributor
    is there), `edges` per site (default EDGES), `load_hosts` listener hosts
    of `load_type` per site, or (count, type) of a site in `load_by_site`
    (default LOAD_BY_SITE). `edges` None is EDGES_BY_SITE; `standby` (None:
    STANDBY_BY_SITE) is how many of a site's last edges the document names in
    `standby_edges`, held out of the first curve. `slots` (None:
    SLOTS_BY_SITE) is the autoscale slots of a site, {"edges": n,
    "forwarders": n}: nodes marked "slot" that `lab up` does not create."""
    if "london" not in sites:
        raise ValueError("the contributor's site, london, must be in the topology")
    unknown = [site for site in sites if site not in SITES]
    if unknown:
        raise ValueError(f"unknown sites {unknown}; known: {', '.join(SITES)}")
    if edges is None:
        edges = {site: count for site, count in EDGES_BY_SITE.items() if site in sites}
    edges = dict(edges)
    if standby is None:
        standby = {site: count for site, count in STANDBY_BY_SITE.items()
                   if site in sites and edges.get(site, EDGES) > count}
    for site, count in standby.items():
        if site not in sites or not 0 <= count < edges.get(site, EDGES):
            raise ValueError(f"{site}: {count} standby edges; it must keep one in service")
    for site, count in edges.items():
        if site not in sites:
            raise ValueError(f"edges given for {site}, which is not in the topology")
        if not 1 <= count <= MAX_EDGES:
            raise ValueError(f"{site}: {count} edges; 1 to {MAX_EDGES}")
    if slots is None:
        slots = {site: dict(value) for site, value in SLOTS_BY_SITE.items() if site in sites}
    for site, value in slots.items():
        if site not in sites:
            raise ValueError(f"slots given for {site}, which is not in the topology")
        if set(value) - {"edges", "forwarders"} or any(count < 0 for count in value.values()):
            raise ValueError(f"{site}: slots {value}; {{'edges': n, 'forwarders': n}}")
        if edges.get(site, EDGES) + value.get("edges", 0) > MAX_EDGES:
            raise ValueError(f"{site}: {edges.get(site, EDGES)} edges and "
                             f"{value.get('edges', 0)} edge slots; at most {MAX_EDGES}")
        if value.get("forwarders", 0) > len(FORWARDER_LETTERS):
            raise ValueError(f"{site}: at most {len(FORWARDER_LETTERS)} forwarder slots")
    load = {site: (load_hosts, load_type) for site in sites}
    load.update({site: value for site, value in (LOAD_BY_SITE if load_by_site is None
                                                  else load_by_site).items() if site in sites})
    for site, (count, kind) in load.items():
        if not kind.startswith("g6-dedicated-"):
            raise ValueError(f"{site}: {kind}: dedicated plans only")
        if count < 0:
            raise ValueError(f"{site}: {count} load hosts")
    ordered = [site for site in SITES if site in sites]
    nodes = []
    for site in ordered:
        region, short, net = SITES[site]
        vlan = f"{VLAN_NET}.{net}"

        def add(name: str, role: str, kind: str, host: int | None = None,
                purpose: str | None = None, slot: bool = False) -> None:
            node = {"name": name, "role": role, "site": site, "region": region, "type": kind}
            if slot:
                node["slot"] = True
            if purpose:
                # What a load node is for: the source, the full probes
                # (view) or the simulated listeners.
                node["purpose"] = purpose
            if host is not None:
                node["vlan_ipv4"] = f"{vlan}.{host}/24"
            nodes.append(node)

        if site == "london":
            add("contrib-lon", "contributor", CONTRIBUTOR_TYPE, 5)
            add("lon-src", "load", SOURCE_TYPE, 30, "source")
        add(f"{short}-dist-a", "distributor", MESH_TYPE, 11)
        add(f"{short}-dist-b", "distributor", MESH_TYPE, 12)
        for index in range(1, edges.get(site, EDGES) + 1):
            add(f"{short}-edge-{index}", "edge", MESH_TYPE, edge_host(index))
        site_slots = slots.get(site, {})
        for index in range(site_slots.get("forwarders", 0)):
            add(f"{short}-dist-{FORWARDER_LETTERS[index]}", "distributor", MESH_TYPE,
                FORWARDER_SLOT_HOST + index, slot=True)
        first = edges.get(site, EDGES) + 1
        for index in range(first, first + site_slots.get("edges", 0)):
            add(f"{short}-edge-{index}", "edge", MESH_TYPE, edge_host(index), slot=True)
        count, kind = load[site]
        for index in range(1, count + 1):
            add(f"{short}-load-{index}", "load", kind, LOAD_HOST_BASE + index - 1,
                purpose="listeners")
        if view_hosts:
            add(f"{short}-view", "load", VIEW_TYPE, VIEW_HOST, purpose="view")
        if livekit:
            add(f"{short}-lk", "livekit", LIVEKIT_TYPE)
            if site == "london":
                add("lk-redis", "livekit", LIVEKIT_TYPE)
    counts = {site: edges.get(site, EDGES) for site in ordered}
    held = [f"{SITES[site][1]}-edge-{index}" for site in ordered
            for index in range(counts[site] - standby.get(site, 0) + 1, counts[site] + 1)]
    slot_names = [node["name"] for node in nodes if node.get("slot")]
    description = (
        "The large audio mesh of the scale run (owner, 22 November 2026: \"test on a larger "
        "deployed mesh\", \"with simulated users too\", audio only), merged topology: in each "
        f"region two forwarders and {', '.join(f'{n} edges in {s}' for s, n in counts.items())}, "
        "all g6-dedicated-2; listener load hosts "
        + ", ".join(f"{load[s][0]} x {load[s][1]} in {s}" for s in ordered) + " "
        "(needletail-load for the UDP listeners, ntcmp for the LL-HLS ones) and a g6-dedicated-4 "
        "view host for the full probes, on the region's VLAN (Linode does not bill VLAN traffic; "
        "5,000 UDP listeners of 1 ms PCM over the public network would be about 12 TB an hour "
        "of billed transfer), and a self-hosted LiveKit SFU (g6-dedicated-2) per region; the "
        "contributor alone on a "
        "g6-dedicated-4 in London with its source host lon-src. Dedicated plans only. Sydney "
        "first: the core of the region short of capacity is created first; the run works "
        "without it (lab up --optional-sites sydney, or --without-sites sydney). The listener "
        "hosts are created last, in batches: the lab is kept when the account refuses some "
        "(listeners_missing), and scale-run.py fits its steps to the hosts that exist. "
        + (f"Standby edges ({', '.join(held)}): created, held out of the first curve by the "
           "deploy setting standby_edges, started in (deploy start --nodes) for the second. "
           if held else "")
        + (f"Autoscale slots ({', '.join(slot_names)}): in every peer list from the first "
           "push, created only by the autoscaler (lab add), never by up or grow. "
           if slot_names else "")
        + "Sized for the owner's budget of 22 November (the shared $100 of the Needletail "
        "work; the watchdog caps the created nodes at $72.95 in 7 h, or $62.53 in 6 h): the "
        "referee's plan (needletail-compare target/compare/scale-plan.md) grown to as "
        "many listener hosts as it buys, and enough edges a region that on the second curve "
        "the edges give out before the listener hosts; Sydney's listener hosts are "
        "g6-dedicated-4, for it had no g6-dedicated-8 capacity on 22 November (same vCPUs, "
        "same price). Generated by scripts/scale_topology.py; the plan, the cost and "
        "the steps are in deploy/low-latency/SCALE.md.")
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


# The committed files: (file name, arguments of scale_topology).
VARIANTS = {
    "topology-scale.json": {},
}


def render(document: dict) -> str:
    return json.dumps(document, indent=2) + "\n"


def parse_edges(values: list[str]) -> dict[str, int]:
    out = {}
    for value in values:
        site, _, count = value.partition("=")
        try:
            out[site] = int(count)
        except ValueError:
            raise SystemExit(f"--edges-in {value!r}: SITE=COUNT") from None
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sites", default=",".join(DEFAULT_SITES))
    parser.add_argument("--edges-in", action="append", default=[], metavar="SITE=COUNT")
    parser.add_argument("--load-hosts", type=int, default=LOAD_HOSTS)
    parser.add_argument("--load-type", default=LOAD_TYPE)
    parser.add_argument("--write", action="store_true",
                        help=f"rewrite the committed files ({', '.join(VARIANTS)})")
    args = parser.parse_args()
    if args.write:
        for name, kwargs in VARIANTS.items():
            (OUT / name).write_text(render(scale_topology(**kwargs)))
            print(f"wrote {OUT / name}")
        return
    try:
        document = scale_topology(tuple(args.sites.split(",")), parse_edges(args.edges_in) or None,
                                  args.load_hosts, args.load_type)
    except ValueError as error:
        sys.exit(str(error))
    sys.stdout.write(render(document))


if __name__ == "__main__":
    main()
