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
  topology-proving-syd2.json  the same with Sydney's listener host as two
                              g6-dedicated-2 and its view host a
                              g6-dedicated-2 (no larger plan had capacity
                              there on 22 November). Without Sydney at all:
                              `lab up --without-sites sydney` on either

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

Flags over the variant (each SITE=... flag may be given for several sites):
--sites, --edges-in SITE=N, --standby SITE=N (how many of the site's last
edges are on standby), --slots SITE=edges:N,forwarders:N, --load-hosts N and
--load-type TYPE (every site), --load SITE=COUNT:TYPE (one site), --view
SITE=TYPE, --contributors 1|2, --no-livekit."""

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


def edge_host(index: int) -> int:
    """The VLAN host number of edge `index` (1-based): .21 up, skipping .30
    (the source host's address in London) and .5."""
    host = 20 + index
    if host >= 30:
        host += 1
    return host


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
    slots = site_slots(sites, slots)
    for site, value in slots.items():
        if edges.get(site, EDGES) + value["edges"] > MAX_EDGES:
            raise ValueError(f"{site}: {edges.get(site, EDGES)} edges and "
                             f"{value['edges']} edge slots; at most {MAX_EDGES}")
        if value["forwarders"] > len(FORWARDER_LETTERS):
            raise ValueError(f"{site}: at most {len(FORWARDER_LETTERS)} forwarder slots")
    load = {site: (load_hosts, load_type) for site in sites}
    load.update({site: value for site, value in (LOAD_BY_SITE if load_by_site is None
                                                  else load_by_site).items() if site in sites})
    view = {site: VIEW_TYPE for site in sites}
    for site, kind in (view_by_site or {}).items():
        if site not in sites:
            raise ValueError(f"a view host given for {site}, which is not in the topology")
        view[site] = kind
    for site, (count, kind) in load.items():
        if not kind.startswith("g6-dedicated-"):
            raise ValueError(f"{site}: {kind}: dedicated plans only")
        if count < 0:
            raise ValueError(f"{site}: {count} load hosts")
    for site, kind in view.items():
        if not kind.startswith("g6-dedicated-"):
            raise ValueError(f"{site}: view host {kind}: dedicated plans only")
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
        + (f"Standby edges ({', '.join(held)}): created, held out of the first curve by the "
           "deploy setting standby_edges, started in (deploy start --nodes) for the second. "
           if held else "")
        + (f"Autoscale slots ({', '.join(slot_names)}): in every peer list from the first "
           "push, created only by the autoscaler (lab add), never by up or grow. "
           if slot_names else "")
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
# view host a region, the self-hosted LiveKit nodes.
PROVING = {"edges": {site: 4 for site in DEFAULT_SITES},
           "standby": {site: 1 for site in DEFAULT_SITES},
           "load_hosts": 1, "load_type": "g6-dedicated-8", "load_by_site": {},
           "contributors": 2, "about": PROVING_ABOUT}
# The committed files: (file name, arguments of scale_topology).
VARIANTS = {
    "topology-scale.json": {"slots": {"london": {"edges": 2}}},
    "topology-proving.json": PROVING,
    "topology-proving-syd2.json": {**PROVING, "load_by_site": {"sydney": (2, "g6-dedicated-2")},
                                   "view_by_site": {"sydney": "g6-dedicated-2"}},
}


def render(document: dict) -> str:
    return json.dumps(document, indent=2) + "\n"


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
    over them (a SITE=... flag changes that site only)."""
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
        if given:
            kwargs[key] = {**(kwargs.get(key) or {}), **given}
        # A site that --sites leaves out takes its variant's values with it.
        if kwargs.get(key) is not None:
            kwargs[key] = {site: value for site, value in kwargs[key].items() if site in sites}
    if args.load_hosts is not None:
        kwargs["load_hosts"] = args.load_hosts
    if args.load_type is not None:
        kwargs["load_type"] = args.load_type
    if args.contributors is not None:
        kwargs["contributors"] = args.contributors
    if args.no_livekit:
        kwargs["livekit"] = False
    return kwargs


def main() -> None:
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
                        help=f"rewrite the committed files ({', '.join(VARIANTS)})")
    args = parser.parse_args()
    if args.write:
        for name, kwargs in VARIANTS.items():
            (OUT / name).write_text(render(scale_topology(**kwargs)))
            print(f"wrote {OUT / name}")
        return
    try:
        document = scale_topology(**variant_arguments(args))
    except ValueError as error:
        sys.exit(str(error))
    sys.stdout.write(render(document))


if __name__ == "__main__":
    main()
