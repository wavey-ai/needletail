#!/usr/bin/env python3
"""The topology of the large audio mesh (deploy/low-latency/topology-scale.json)
and its variants, generated so that every file of the plan has the same shape.

The merged topology (relay and distributor are one forwarder), audio only, in
London, New York, Tokyo and Sydney:

  per region   two forwarders (`<site>-dist-a`, `-b`, g6-dedicated-2), N edges
               (`<site>-edge-1`..N, g6-dedicated-2; eight in London, four
               elsewhere), M listener load hosts (`<site>-load-1`..M:
               needletail-load and ntcmp), one view host for the full probes
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

This is the referee's plan (needletail-compare target/compare/scale-plan.md)
node for node, but for Sydney's listener hosts: four g6-dedicated-4 in place
of two g6-dedicated-8, which Sydney had no capacity for on 22 November (lab
`check`), the same vCPUs at the same price. London's edges 5 to 8 are the
second curve's: the document names them in `standby_edges`, which the
first curve deploys with (scale-run.py settings), and the second restarts
them in (scale-run.py settings --second-curve).

Sydney's nodes come first in the file: it is the region short of dedicated
capacity. `lab up --optional-sites sydney` creates them before the rest and
goes on without Sydney when one cannot be created; `--without-sites sydney`
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
# Listener load hosts per region: two g6-dedicated-8 (see SCALE.md for the
# sizing from needletail-load's cost per listener). Sydney had no
# g6-dedicated-8 capacity on 22 November (lab `check`), so it gets the same
# 16 vCPUs as four g6-dedicated-4.
LOAD_TYPE = "g6-dedicated-8"
LOAD_HOSTS = 2
LOAD_BY_SITE = {"sydney": (4, "g6-dedicated-4")}
EDGES = 4
# Edges of a site other than EDGES, and the edges of each site that the first
# curve holds out (deploy setting standby_edges): twice the edges in London
# for the second curve.
EDGES_BY_SITE = {"london": 8}
STANDBY_BY_SITE = {"london": 4}
# VLAN host numbers of the view host and the listener hosts (above the edges).
VIEW_HOST = 100
LOAD_HOST_BASE = 101
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
                   view_hosts: bool = True, livekit: bool = True,
                   standby: dict[str, int] | None = None) -> dict:
    """The topology document for `sites` (London must be one: the contributor
    is there), `edges` per site (default EDGES), `load_hosts` listener hosts
    of `load_type` per site, or (count, type) of a site in `load_by_site`
    (default LOAD_BY_SITE). `edges` None is EDGES_BY_SITE; `standby` (None:
    STANDBY_BY_SITE) is how many of a site's last edges the document names in
    `standby_edges`, held out of the first curve."""
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
        "first: the region short of capacity is created first; the run works without it "
        "(lab up --optional-sites sydney, or --without-sites sydney). "
        + (f"Standby edges ({', '.join(held)}): created, held out of the first curve by the "
           "deploy setting standby_edges, restarted in for the second. " if held else "")
        + "The referee's plan (needletail-compare target/compare/scale-plan.md) node for node, "
        "but for Sydney's listener hosts: four g6-dedicated-4 for two g6-dedicated-8, which "
        "Sydney had no capacity for on 22 November; same vCPUs, same price. Generated by "
        "scripts/scale_topology.py; the plan, the cost and "
        "the steps are in deploy/low-latency/SCALE.md.")
    return {
        "description": description,
        "image": "linode/debian13",
        "standby_edges": held,
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
