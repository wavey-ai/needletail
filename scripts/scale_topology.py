#!/usr/bin/env python3
"""The topology of the large audio mesh (deploy/low-latency/topology-scale.json)
and its variants, generated so that every file of the plan has the same shape.

The merged topology (relay and distributor are one forwarder), audio only, in
London, New York, Tokyo and Sydney:

  per region   two forwarders (`<site>-dist-a`, `-b`, g6-dedicated-2), N edges
               (`<site>-edge-1`..N, g6-dedicated-2), M listener load hosts
               (`<site>-load-1`..M: needletail-load and ntcmp, no VLAN; the
               listeners come over the public network as real ones do), one
               view host for the full probes (`<site>-view`, g6-dedicated-4,
               as the comparison's) and one self-hosted LiveKit SFU
               (`<site>-lk`, g6-dedicated-2)
  London       the contributor alone on a g6-dedicated-4 (`contrib-lon`), the
               source host (`lon-src`, g6-dedicated-4) and the Redis of the
               LiveKit multi-region set (`lk-redis`, g6-dedicated-2), which the
               comparison's LiveKit installer needs

Sydney's nodes come first: the comparison's lab tool creates the first
region of a topology before any other and stops at the first refusal, and
Sydney is the region that is short of dedicated capacity. Without it, `up
--skip-sites sydney` (or a file made with --sites london,newyork,tokyo).

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
# Listener load hosts per region: two g6-dedicated-8 (see SCALE.md for the
# sizing from needletail-load's cost per listener). Sydney had no
# g6-dedicated-8 capacity on 22 November (lab `check`), so it gets the same
# 16 vCPUs as four g6-dedicated-4.
LOAD_TYPE = "g6-dedicated-8"
LOAD_HOSTS = 2
LOAD_BY_SITE = {"sydney": (4, "g6-dedicated-4")}
EDGES = 4
# VLAN host numbers: forwarders .11/.12, edges .21 up (at most 40 edges),
# contributor .5, source .30 (as in topology-merged.json; the edges stop
# short of it below 9 edges and skip it above).
MAX_EDGES = 40


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
                   view_hosts: bool = True, livekit: bool = True) -> dict:
    """The topology document for `sites` (London must be one: the contributor
    is there), `edges` per site (default EDGES), `load_hosts` listener hosts
    of `load_type` per site, or (count, type) of a site in `load_by_site`
    (default LOAD_BY_SITE)."""
    if "london" not in sites:
        raise ValueError("the contributor's site, london, must be in the topology")
    unknown = [site for site in sites if site not in SITES]
    if unknown:
        raise ValueError(f"unknown sites {unknown}; known: {', '.join(SITES)}")
    edges = dict(edges or {})
    for site, count in edges.items():
        if site not in sites:
            raise ValueError(f"edges given for {site}, which is not in the topology")
        if not 1 <= count <= MAX_EDGES:
            raise ValueError(f"{site}: {count} edges; 1 to {MAX_EDGES}")
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
                purpose: str | None = None) -> None:
            node = {"name": name, "role": role, "site": site, "region": region, "type": kind}
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
        count, kind = load[site]
        for index in range(1, count + 1):
            add(f"{short}-load-{index}", "load", kind, purpose="listeners")
        if view_hosts:
            add(f"{short}-view", "load", VIEW_TYPE, purpose="view")
        if livekit:
            add(f"{short}-lk", "livekit", LIVEKIT_TYPE)
            if site == "london":
                add("lk-redis", "livekit", LIVEKIT_TYPE)
    counts = {site: edges.get(site, EDGES) for site in ordered}
    description = (
        "The large audio mesh of the scale run (owner, 22 November 2026: \"test on a larger "
        "deployed mesh\", \"with simulated users too\", audio only), merged topology: in each "
        f"region two forwarders and {', '.join(f'{n} edges in {s}' for s, n in counts.items())}, "
        "all g6-dedicated-2; listener load hosts "
        + ", ".join(f"{load[s][0]} x {load[s][1]} in {s}" for s in ordered) + " "
        "(needletail-load for the UDP listeners, ntcmp for the LL-HLS ones; no VLAN: listeners "
        "come over the public network), a g6-dedicated-4 view host for the full probes and a "
        "self-hosted LiveKit SFU (g6-dedicated-2) per region; the contributor alone on a "
        "g6-dedicated-4 in London with its source host lon-src. Dedicated plans only. Sydney "
        "first: the region short of capacity is created first; the run works without it "
        "(lab up --optional-sites sydney, or --without-sites sydney). Generated by scripts/scale_topology.py; the plan, the cost and "
        "the steps are in deploy/low-latency/SCALE.md.")
    return {
        "description": description,
        "image": "linode/debian13",
        "scale": {"sites": ordered, "edges": counts,
                  "load_hosts": {site: {"count": load[site][0], "type": load[site][1]}
                                 for site in ordered}},
        "nodes": nodes,
    }


# The committed files: (file name, arguments of scale_topology).
VARIANTS = {
    "topology-scale.json": {},
    # The second curve: twice the edges in London (`lab grow`, then push).
    "topology-scale-lon8.json": {"edges": {"london": 8}},
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
        document = scale_topology(tuple(args.sites.split(",")), parse_edges(args.edges_in),
                                  args.load_hosts, args.load_type)
    except ValueError as error:
        sys.exit(str(error))
    sys.stdout.write(render(document))


if __name__ == "__main__":
    main()
