"""The heavy egress each mesh node is planned to send (tools6, item 1).

cmpd8 (25 Nov, four regions at 240 Mbit/s): the London first-hop forwarder
lon-dist-b (g6-dedicated-2) sent about 1.15 Gbit/s to its children (880
long-haul, 270 to its edges) and its send path stalled for seconds
(fast-plane WouldBlock); edges with two 4K viewers stalled at 470-560
Mbit/s. So a topology is planned by the heaviest stream it carries: each
node's planned heavy egress is

    rate x (children fed in full + repair share x children fed repair only
            + expected heavy viewers or readers, on an edge)

and it must stay within its plan's limit (`max_heavy_egress_mbit`: 600 for
a g6-dedicated-2, 1200 for -4, 2400 for -8; cmpd8-edge-analysis: "keep a
guest's heavy egress <= ~600 Mb/s until the NIC stalls are explained").

Who feeds whom follows the deploy tool exactly (linode-small-mesh-deploy.py
relay_tree and fast_plane_feed; test_egress_plan checks the two agree):

- merged (the default): the contributor feeds the first two distributors of
  its site (by name) on its two lanes, the source lane and the
  source-and-repair lane; every other distributor (autoscale slots aside)
  takes its sources from one of those two and repair from the other,
  alternating by rank within its site;
- legacy: the contributor feeds the *-primary and *-secondary backbones; each
  distributor takes its sources from the primary and repair from the
  secondary;
- the fast plane (edge_fast_plane): each edge of a site, by name, takes its
  video in full from one of the site's first two distributors and repair
  from the other, alternating; a distributor slot is a lease child of the
  first. Audio comes in full from both parents, but it is not heavy and is
  not counted here.

The audio lanes are not changed by any of this: it only measures.
"""

from __future__ import annotations

DEFAULT_STREAM_MBIT = 247.0
# The repair lane's share of the stream: cmpd8 lon-dist-b sent 880 Mbit/s
# long-haul to three source children (741 at 247) and three repair children
# (139: 0.19 each), and 270 to its two edges (one full, one repair: 0.09).
DEFAULT_REPAIR_SHARE = 0.2
# Heavy viewers or readers an edge is planned for (a UDP 4K viewer or an
# LL-HLS 4K reader each send the stream once).
DEFAULT_EDGE_VIEWERS = 2
DEFAULT_LIMITS_MBIT = {
    "g6-dedicated-2": 600.0,
    "g6-dedicated-4": 1200.0,
    "g6-dedicated-8": 2400.0,
    "g6-dedicated-16": 4800.0,
    "g6-dedicated-32": 9600.0,
}
MESH_ROLES = ("contributor", "backbone", "distributor", "edge")


def _number(value, name: str, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is not a number: {value!r}") from None
    if number < minimum:
        raise ValueError(f"{name} must be at least {minimum:g}: {value!r}")
    return number


def parse_limits(value, defaults: bool = True) -> dict[str, float]:
    """`max_heavy_egress_mbit`: a {plan: Mbit/s} map, or "plan=mbit,..."
    text; merged over the defaults (with `defaults`), else alone."""
    limits = dict(DEFAULT_LIMITS_MBIT) if defaults else {}
    if value in (None, "", {}):
        return limits
    if isinstance(value, str):
        pairs = {}
        for piece in value.split(","):
            if not piece.strip():
                continue
            if "=" not in piece:
                raise ValueError(f"max_heavy_egress_mbit: {piece!r} is not plan=mbit")
            plan, mbit = piece.split("=", 1)
            pairs[plan.strip()] = mbit
        value = pairs
    if not isinstance(value, dict):
        raise ValueError("max_heavy_egress_mbit is not a map of plan to Mbit/s")
    for plan, mbit in value.items():
        limits[str(plan)] = _number(mbit, f"max_heavy_egress_mbit[{plan}]", 1.0)
    return limits


def settings_from(topology: dict | None = None, settings: dict | None = None) -> dict:
    """The plan's settings: a deploy setting given (not None) first, then the
    topology file's key of the same name, then the default."""
    topology = topology or {}
    settings = settings or {}

    def pick(key, default):
        if settings.get(key) is not None:
            return settings[key]
        if topology.get(key) is not None:
            return topology[key]
        return default

    return {
        "plan_stream_mbit": _number(pick("plan_stream_mbit", DEFAULT_STREAM_MBIT),
                                    "plan_stream_mbit"),
        "plan_repair_share": _number(pick("plan_repair_share", DEFAULT_REPAIR_SHARE),
                                     "plan_repair_share"),
        "plan_edge_viewers": _number(pick("plan_edge_viewers", DEFAULT_EDGE_VIEWERS),
                                     "plan_edge_viewers"),
        "max_heavy_egress_mbit": {
            **parse_limits(topology.get("max_heavy_egress_mbit")),
            **parse_limits(settings.get("max_heavy_egress_mbit"), defaults=False)},
    }


def _by_role(nodes: list[dict], role: str) -> list[dict]:
    return sorted((node for node in nodes if node.get("role") == role),
                  key=lambda node: node["name"])


def feeds(nodes: list[dict], mode: str = "merged", fast_plane: bool = True,
          slots: set[str] | frozenset = frozenset()) -> dict[str, dict]:
    """{parent name: {"full": [child names], "repair": [child names]}} of the
    heavy stream, by the deploy tool's rules (module doc). `nodes`: every
    node of the lab's roster with name, role and site (an edge or a
    distributor needs `vlan_ipv4` to be on the fast plane), the standby
    edges and the autoscale slots (`slots`) included: as in the deploy, a
    slot keeps its place (and so every other edge's) whether it exists or
    not. The caller leaves out the children it does not count."""
    out: dict[str, dict] = {}

    def add(parent: dict | None, child: dict, kind: str) -> None:
        if parent is None:
            return
        entry = out.setdefault(parent["name"], {"full": [], "repair": []})
        entry[kind].append(child["name"])

    contributors = _by_role(nodes, "contributor")
    distributors = [node for node in _by_role(nodes, "distributor")
                    if node["name"] not in slots]
    origins = [node for node in contributors if not node.get("standby")]
    if mode == "legacy":
        relays = _by_role(nodes, "backbone")
        primary = next((node for node in relays if node["name"].endswith("primary")), None)
        secondary = next((node for node in relays if node["name"].endswith("secondary")), None)
        first = [node for node in (primary, secondary) if node is not None]
        for node in distributors:
            add(primary, node, "full")
            add(secondary, node, "repair")
    elif origins:
        local = [node for node in distributors if node["site"] == origins[0]["site"]]
        first = local[:2]
        others = [node for node in distributors if node not in first]
        by_site: dict[str, int] = {}
        if len(first) == 2:
            for node in sorted(others, key=lambda item: (item["site"], item["name"])):
                rank = by_site.get(node["site"], 0)
                by_site[node["site"]] = rank + 1
                primary, secondary = first if rank % 2 == 0 else first[::-1]
                add(primary, node, "full")
                add(secondary, node, "repair")
    else:
        first = []
    # The contributors' two lanes: sources to the first, sources and repair
    # to the second (the standby the same, from its own address).
    for contributor in contributors:
        for index, node in enumerate(first[:2]):
            add(contributor, node, "full")
            if index == 1:
                add(contributor, node, "repair")
    if fast_plane:
        edges = _by_role(nodes, "edge")
        for site in sorted({node["site"] for node in edges}):
            parents = [node for node in distributors
                       if node["site"] == site and node.get("vlan_ipv4")][:2]
            if not parents:
                continue
            children = [(edge, True) for edge in edges
                        if edge["site"] == site and edge.get("vlan_ipv4")]
            children += [(node, False) for node in _by_role(nodes, "distributor")
                         if node["site"] == site and node.get("vlan_ipv4")
                         and node["name"] in slots]
            for rank, (child, is_edge) in enumerate(children):
                turn = rank if is_edge else 0
                add(parents[turn % len(parents)], child, "full")
                if len(parents) > 1:
                    add(parents[(turn + 1) % len(parents)], child, "repair")
    return out


def plan(nodes: list[dict], settings: dict, mode: str = "merged", fast_plane: bool = True,
         slots: set[str] | frozenset = frozenset(), include_slots: bool = False) -> dict:
    """Each mesh node's planned heavy egress and its plan's limit:
    {"nodes": {name: {role, type, full, repair, viewers, units, mbit,
    limit, over}}, "over": [names over their limit], "settings": settings}.
    The autoscale slots are fed in the plan of their parents only with
    `include_slots` (`up` does not create them), and are judged then."""
    present = [node for node in nodes if include_slots or node["name"] not in slots]
    counted = {node["name"] for node in present}
    fed = {name: {kind: [child for child in children if child in counted]
                  for kind, children in entry.items()}
           for name, entry in feeds(nodes, mode, fast_plane, slots).items()}
    rate = settings["plan_stream_mbit"]
    share = settings["plan_repair_share"]
    limits = settings["max_heavy_egress_mbit"]
    out: dict[str, dict] = {}
    for node in sorted(present, key=lambda item: item["name"]):
        if node.get("role") not in MESH_ROLES:
            continue
        entry = fed.get(node["name"], {"full": [], "repair": []})
        viewers = settings["plan_edge_viewers"] if node["role"] == "edge" else 0
        units = len(entry["full"]) + share * len(entry["repair"]) + viewers
        mbit = round(rate * units, 1)
        limit = limits.get(node.get("type") or "")
        out[node["name"]] = {
            "role": node["role"], "type": node.get("type"), "site": node.get("site"),
            "full": entry["full"], "repair": entry["repair"], "viewers": viewers,
            "units": round(units, 3), "mbit": mbit, "limit": limit,
            "over": limit is not None and mbit > limit + 1e-9}
    return {"nodes": out, "over": [name for name, item in out.items() if item["over"]],
            "settings": {**settings, "mode": mode, "fast_plane": fast_plane,
                         "include_slots": include_slots}}


def topology_nodes(topology: dict) -> tuple[list[dict], set[str]]:
    """A topology file's nodes (VLAN address without its prefix) and its
    autoscale slots (marked "slot", or named in `autoscale_slots`)."""
    named = set(topology.get("autoscale_slots") or [])
    nodes, slots = [], set()
    for node in topology.get("nodes") or []:
        item = {**node, "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
        nodes.append(item)
        if node.get("slot") or node["name"] in named:
            slots.add(node["name"])
    return nodes, slots


def topology_plan(topology: dict, stream_mbit: float | None = None,
                  include_slots: bool = False) -> dict:
    """plan() of a topology file: its own settings (plan_stream_mbit and the
    others as top-level keys; `relay_topology`: "merged" or "legacy"),
    `stream_mbit` over its rate when given."""
    settings = settings_from(topology, {"plan_stream_mbit": stream_mbit})
    nodes, slots = topology_nodes(topology)
    mode = topology.get("relay_topology") or "merged"
    return plan(nodes, settings, mode=mode, fast_plane=topology.get("edge_fast_plane", True),
                slots=slots, include_slots=include_slots)


def lines(planned: dict, measured: dict | None = None) -> list[str]:
    """The plan as a table: one line a mesh node (its children in full and
    repair-only, its viewers, planned Mbit/s and limit; with `measured`,
    {name: (egress Mbit/s, ingress Mbit/s)}, what it sent beside it)."""
    settings = planned["settings"]
    out = [f"heavy egress plan at {settings['plan_stream_mbit']:g} Mbit/s "
           f"(repair share {settings['plan_repair_share']:g}, "
           f"{settings['plan_edge_viewers']:g} heavy viewer(s) an edge"
           + (", slots fed" if settings.get("include_slots") else "") + "):"]
    for name, item in planned["nodes"].items():
        limit = "-" if item["limit"] is None else f"{item['limit']:.0f}"
        line = (f"  {name:<18} {item['role']:<12} {item.get('type') or '-':<16} "
                f"full {len(item['full']):>2} repair {len(item['repair']):>2} "
                f"viewers {item['viewers']:g}  planned {item['mbit']:>7.1f} / {limit:>5} Mbit/s")
        if measured is not None:
            sent = measured.get(name)
            line += ("  measured -" if not sent
                     else f"  measured out {sent[0]:.1f} in {sent[1]:.1f}")
        if item["over"]:
            line += "  <-- OVER"
        out.append(line)
    return out


def refusal(planned: dict, flag: str = "--allow-egress-over") -> str | None:
    """The refusal text when a node passes its limit, else None."""
    if not planned["over"]:
        return None
    items = planned["nodes"]
    return ("planned heavy egress over the plan's limit on "
            + ", ".join(f"{name} ({items[name]['type']}: {items[name]['mbit']:.0f} > "
                        f"{items[name]['limit']:.0f} Mbit/s; {len(items[name]['full'])} full, "
                        f"{len(items[name]['repair'])} repair, {items[name]['viewers']:g} viewers)"
                        for name in planned["over"])
            + f"; fewer children per forwarder or a larger plan, or {flag}")
