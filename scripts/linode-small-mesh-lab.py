#!/usr/bin/env python3
"""Short-lived small-instance Linode lab for Needletail mesh qualification.

The lab is described by a topology JSON file (see
deploy/linode-small-mesh/topology.json). Every node is created with the
smallest plan that the topology names, a lab-only Cloud Firewall, and an
optional regional private VLAN interface for distributor-to-edge replication.

Cost controls are part of the tool, not an operator convention:

* `up` refuses a topology above the node and hourly-price ceilings;
* state records exact instance and firewall identifiers, and `down` also
  sweeps by the unique run tag, so only this lab is ever deleted;
* `watchdog` starts a detached process that deletes the lab at a deadline
  even when the operator session has ended.

The Linode token is read from LINODE_TOKEN, LINODE_PAT, or the token file. It
is never written to state, logs, or generated output.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_BASE = "https://api.linode.com/v4"
DEFAULT_STATE = ROOT / "target" / "linode-qualification" / "small-mesh-lab.json"
DEFAULT_TOPOLOGY = ROOT / "deploy" / "linode-small-mesh" / "topology.json"
DEFAULT_TOKEN_FILE = ROOT.parent / "LINODE_PAT"
MAX_NODES = int(os.environ.get("NEEDLETAIL_LINODE_MAX_NODES", "40"))
MAX_HOURLY_USD = float(os.environ.get("NEEDLETAIL_LINODE_MAX_HOURLY_USD", "1.00"))
# The owner, 21 November 2026: "do not use shared instances". Every node of a
# lab is a dedicated-CPU plan. A shared plan (nanode, standard) is refused.
ALLOWED_CLASSES = {"dedicated"}


def load_token() -> str:
    for name in ("LINODE_TOKEN", "LINODE_PAT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    token_file = pathlib.Path(
        os.environ.get("NEEDLETAIL_LINODE_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))
    )
    if not token_file.is_file():
        sys.exit("Linode token file is missing")
    value = token_file.read_text().strip()
    for prefix in ("export ", "LINODE_TOKEN=", "LINODE_PAT="):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.strip("\"'")
    if not value:
        sys.exit("Linode token is empty")
    return value


class Api:
    def __init__(self, public: bool = False) -> None:
        # `public`: only the endpoints that need no token (plans, regions,
        # availability), so a check never reads the token.
        self.token = None if public else load_token()

    def call(self, method: str, path: str, body: dict | None = None,
             allow_404: bool = False) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(8):
            request = urllib.request.Request(
                API_BASE + path, data=data, method=method,
                headers={
                    **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
                    "Content-Type": "application/json",
                    "User-Agent": "needletail-small-mesh-lab",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")
                if error.code == 404 and allow_404:
                    return {}
                if error.code in (429, 500, 502, 503, 504) or (
                    error.code == 400 and "busy" in detail.lower()
                ):
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise RuntimeError(
                    f"{method} {path} failed with HTTP {error.code}: {detail}"
                ) from None
            except (urllib.error.URLError, TimeoutError):
                time.sleep(min(2 ** attempt, 20))
        raise RuntimeError(f"{method} {path} did not succeed after retries")

    def list_all(self, path: str, tag: str | None = None) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            response = self.call("GET", f"{path}{separator}page={page}&page_size=200")
            results.extend(response.get("data", []))
            if page >= response.get("pages", 1):
                break
            page += 1
        if tag is not None:
            results = [item for item in results if tag in item.get("tags", [])]
        return results


def read_state(path: pathlib.Path) -> dict | None:
    return json.loads(path.read_text()) if path.is_file() else None


def write_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".next")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(path)


def operator_ipv4() -> str:
    with urllib.request.urlopen("https://api.ipify.org", timeout=20) as response:
        address = response.read().decode().strip()
    parts = address.split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        sys.exit("could not determine operator IPv4")
    return address


def without_sites(topology: dict, sites: list[str]) -> dict:
    """The topology without the nodes of `sites` (a region that has no
    dedicated capacity today: Sydney in the scale plan). Refuses a site that
    holds the contributor or names no node."""
    sites = [site for site in sites if site]
    if not sites:
        return topology
    known = {node["site"] for node in topology["nodes"]}
    for site in sites:
        if site not in known:
            sys.exit(f"no node of the topology is at site {site}")
    for node in topology["nodes"]:
        if node["site"] in sites and node["role"] == "contributor":
            sys.exit(f"site {node['site']} holds the contributor; it cannot be left out")
    return {**topology, "nodes": [node for node in topology["nodes"] if node["site"] not in sites]}


def topology_cost(topology: dict, types: dict, regions: dict,
                  availability: dict | None = None) -> tuple[float, list[str]]:
    """The hourly price of a topology from the API's plans (region prices
    included; each node gets `_hourly`), and what the API says against it:
    a plan that is not dedicated, a region that is down or has no VLANs for
    a node that needs one, a duplicate VLAN address, and (with
    `availability`, {(region, plan): bool}) a plan that the region has no
    capacity for today."""
    problems = []
    hourly = 0.0
    addresses: set[tuple[str, str]] = set()
    for node in topology["nodes"]:
        plan = types.get(node["type"])
        if plan is None or plan["class"] not in ALLOWED_CLASSES:
            problems.append(f"{node['name']}: {node['type']} is not a dedicated-CPU plan; "
                            "shared instances are not used")
            continue
        region = regions.get(node["region"])
        if region is None or region["status"] != "ok":
            problems.append(f"{node['name']}: region {node['region']} is unavailable")
            continue
        if node.get("vlan_ipv4") and "Vlans" not in region["capabilities"]:
            problems.append(f"{node['name']}: region {node['region']} has no VLAN support")
        if node.get("vlan_ipv4"):
            key = (node["region"], node["vlan_ipv4"])
            if key in addresses:
                problems.append(f"{node['name']}: duplicate VLAN address {key}")
            addresses.add(key)
        if availability is not None and availability.get((node["region"], node["type"])) is False:
            problems.append(f"{node['name']}: {node['region']} has no {node['type']} "
                            "capacity today (/regions/<id>/availability)")
        price = plan["price"]["hourly"]
        for override in plan.get("region_prices", []):
            if override["id"] == node["region"]:
                price = override["hourly"]
        node["_hourly"] = price
        hourly += price
    return hourly, problems


def region_availability(api: "Api", regions: set[str]) -> dict:
    """{(region, plan): available} from GET /regions/<id>/availability."""
    out = {}
    for region in sorted(regions):
        response = api.call("GET", f"/regions/{region}/availability")
        rows = response if isinstance(response, list) else response.get("data", [])
        for row in rows:
            out[(row["region"], row["plan"])] = bool(row["available"])
    return out


def validate_topology(api: Api, topology: dict) -> tuple[dict, float]:
    nodes = topology["nodes"]
    if not nodes or len(nodes) > MAX_NODES:
        sys.exit(f"topology has {len(nodes)} nodes; the ceiling is {MAX_NODES} "
                 "(NEEDLETAIL_LINODE_MAX_NODES)")
    names = [node["name"] for node in nodes]
    if len(set(names)) != len(names):
        sys.exit("topology node names must be unique")
    types = {item["id"]: item for item in api.list_all("/linode/types")}
    regions = {item["id"]: item for item in api.list_all("/regions")}
    hourly, problems = topology_cost(topology, types, regions)
    if problems:
        sys.exit("\n".join(problems))
    if hourly > MAX_HOURLY_USD:
        sys.exit(
            f"topology costs ${hourly:.4f}/hour; the ceiling is ${MAX_HOURLY_USD:.2f} "
            "(NEEDLETAIL_LINODE_MAX_HOURLY_USD)"
        )
    return topology, hourly


def split_optional(nodes: list[dict], sites: list[str]) -> tuple[dict, list[dict]]:
    """({optional site: its nodes}, every other node). A site that holds the
    contributor is never optional."""
    first: dict[str, list[dict]] = {}
    rest = []
    for node in nodes:
        if node["site"] in sites:
            if node["role"] == "contributor":
                sys.exit(f"site {node['site']} holds the contributor; it cannot be optional")
            first.setdefault(node["site"], []).append(node)
        else:
            rest.append(node)
    return first, rest


def command_check(args: argparse.Namespace) -> None:
    """Price a topology and check it against today's plans, regions and
    capacity, from the API's public endpoints (no token, nothing created).
    Exits 1 when `up` would refuse it or a region has no capacity for one of
    its plans."""
    api = Api(public=True)
    topology = json.loads(pathlib.Path(args.topology).read_text())
    topology = without_sites(topology, (args.without_sites or "").split(","))
    types = {item["id"]: item for item in api.list_all("/linode/types")}
    regions = {item["id"]: item for item in api.list_all("/regions")}
    availability = region_availability(api, {node["region"] for node in topology["nodes"]})
    hourly, problems = topology_cost(topology, types, regions, availability)
    by_type: dict[tuple[str, str], list[dict]] = {}
    for node in topology["nodes"]:
        by_type.setdefault((node["region"], node["type"]), []).append(node)
    print(f"{'REGION':<14} {'PLAN':<16} {'COUNT':>5} {'$/H EACH':>9} {'$/H':>8}  AVAILABLE")
    for (region, plan), nodes in sorted(by_type.items()):
        each = nodes[0].get("_hourly", 0.0)
        print(f"{region:<14} {plan:<16} {len(nodes):>5} {each:>9.3f} {each * len(nodes):>8.3f}  "
              f"{availability.get((region, plan), 'unknown')}")
    print(f"{len(topology['nodes'])} nodes, ${hourly:.3f}/hour; ceilings {MAX_NODES} nodes, "
          f"${MAX_HOURLY_USD:.2f}/hour (NEEDLETAIL_LINODE_MAX_NODES, "
          "NEEDLETAIL_LINODE_MAX_HOURLY_USD)")
    if len(topology["nodes"]) > MAX_NODES:
        problems.append(f"{len(topology['nodes'])} nodes is above the ceiling of {MAX_NODES}")
    if hourly > MAX_HOURLY_USD:
        problems.append(f"${hourly:.3f}/hour is above the ceiling of ${MAX_HOURLY_USD:.2f}")
    if problems:
        print("\n".join(problems))
        sys.exit(1)


def create_node(api: Api, run_id: str, tag: str, image: str, key: str,
                node: dict) -> dict:
    label = f"ntsm-{run_id}-{node['name']}{node.get('name_suffix', '')}"
    interfaces = [{"purpose": "public"}]
    if node.get("vlan_ipv4"):
        interfaces.append({
            "purpose": "vlan",
            "label": f"ntsm-{run_id}-{node['region']}"[:64],
            "ipam_address": node["vlan_ipv4"],
        })
    body = {
        "region": node["region"],
        "type": node["type"],
        "image": node.get("image", image),
        "label": label,
        "booted": True,
        "authorized_keys": [key],
        "interfaces": interfaces,
        "tags": [
            "needletail-qualification", tag,
            f"needletail-role:{node['role']}",
            f"needletail-site:{node['site']}",
        ],
    }
    response = None
    for attempt in range(6):
        try:
            response = api.call("POST", "/linode/instances", body)
            break
        except RuntimeError as error:
            # A timed-out create may still have succeeded: adopt it by label
            # rather than creating a duplicate or abandoning the lab.
            time.sleep(10 + 5 * attempt)
            existing = [item for item in api.list_all("/linode/instances", tag)
                        if item["label"] == label]
            if existing:
                response = existing[0]
                break
            transient = any(marker in str(error) for marker in (
                "HTTP 408", "try again", "did not succeed after retries"))
            if not transient or attempt == 5:
                raise
    return {
        "id": response["id"],
        "name": node["name"],
        "label": label,
        "role": node["role"],
        "site": node["site"],
        "region": node["region"],
        "type": node["type"],
        "hourly_usd": node["_hourly"],
        **({"purpose": node["purpose"]} if node.get("purpose") else {}),
        "public_ipv4": response["ipv4"][0],
        "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None,
        "created": response["created"],
    }


def command_up(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    if state_path.exists():
        sys.exit("lab state already exists; run status or down first")
    key_path = pathlib.Path(args.ssh_public_key).expanduser()
    if not key_path.is_file():
        sys.exit("SSH public key is missing")
    api = Api()
    topology = json.loads(pathlib.Path(args.topology).read_text())
    topology = without_sites(topology, (args.without_sites or "").split(","))
    topology, hourly = validate_topology(api, topology)
    optional = [site for site in (args.optional_sites or "").split(",") if site]
    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime("%m%d%H%M")
    tag = f"needletail-small-mesh-{run_id}"
    image = topology.get("image", "linode/debian13")
    state = {
        "provider": "linode", "schema": "needletail.linode-small-mesh-lab.v1",
        "run_id": run_id, "tag": tag, "image": image,
        "topology_file": str(pathlib.Path(args.topology).resolve()),
        "created_unix": int(time.time()), "hourly_usd": round(hourly, 4),
        "nodes": {}, "firewall_id": None,
        # Sites that this lab does without (--without-sites, and an optional
        # site that could not be created): `grow` leaves them out too.
        "sites_left_out": [site for site in (args.without_sites or "").split(",") if site],
    }
    write_state(state_path, state)
    print(f"Creating {len(topology['nodes'])} Linodes at ${hourly:.4f}/hour "
          f"(run {run_id})")
    key = key_path.read_text().strip()

    def create(nodes: list[dict]) -> list[str]:
        failures: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(create_node, api, run_id, tag, image, key, node): node
                for node in nodes
            }
            for future in concurrent.futures.as_completed(futures):
                node = futures[future]
                try:
                    created = future.result()
                except RuntimeError as error:
                    failures.append(f"{node['name']}: {error}")
                    continue
                state["nodes"][created["name"]] = created
                write_state(state_path, state)
                print(f"  created {created['name']:<18} {created['region']:<13} "
                      f"{created['type']:<14} {created['public_ipv4']}")
        return failures

    # The nodes of an optional site first (a region whose dedicated capacity
    # runs out, such as Sydney): when one of them cannot be created, the
    # site's nodes are deleted and the lab goes on without it.
    first, rest = split_optional(topology["nodes"], optional)
    for site, nodes in first.items():
        failures = create(nodes)
        if failures:
            print("\n".join(failures), file=sys.stderr)
            dropped = [state["nodes"].pop(node["name"]) for node in nodes
                       if node["name"] in state["nodes"]]
            for item in dropped:
                api.call("DELETE", f"/linode/instances/{item['id']}", allow_404=True)
            state.setdefault("sites_left_out", []).append(site)
            state["hourly_usd"] = round(state["hourly_usd"] - sum(
                node.get("_hourly", 0.0) for node in nodes), 4)
            write_state(state_path, state)
            print(f"site {site} left out: {len(failures)} of its {len(nodes)} nodes could not "
                  f"be created; deleted the {len(dropped)} that were", file=sys.stderr)
    failures = create(rest)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print("creation failed; removing the partial lab", file=sys.stderr)
        command_down(args)
        sys.exit(1)

    operator = operator_ipv4()
    node_addresses = [f"{node['public_ipv4']}/32" for node in state["nodes"].values()]
    firewall = api.call("POST", "/networking/firewalls", {
        "label": f"ntsm-{run_id}",
        "tags": ["needletail-qualification", tag],
        "rules": {
            "inbound_policy": "DROP", "outbound_policy": "ACCEPT", "outbound": [],
            "inbound": [
                {"label": "operator-all", "action": "ACCEPT", "protocol": "TCP",
                 "ports": "1-65535", "addresses": {"ipv4": [f"{operator}/32"]}},
                {"label": "operator-udp", "action": "ACCEPT", "protocol": "UDP",
                 "ports": "1-65535", "addresses": {"ipv4": [f"{operator}/32"]}},
                {"label": "operator-icmp", "action": "ACCEPT", "protocol": "ICMP",
                 "addresses": {"ipv4": [f"{operator}/32"]}},
                {"label": "lab-tcp", "action": "ACCEPT", "protocol": "TCP",
                 "ports": "1-65535", "addresses": {"ipv4": node_addresses}},
                {"label": "lab-udp", "action": "ACCEPT", "protocol": "UDP",
                 "ports": "1-65535", "addresses": {"ipv4": node_addresses}},
                {"label": "lab-icmp", "action": "ACCEPT", "protocol": "ICMP",
                 "addresses": {"ipv4": node_addresses}},
            ],
        },
    })
    state["firewall_id"] = firewall["id"]
    state["operator_ipv4"] = operator
    write_state(state_path, state)
    for node in state["nodes"].values():
        api.call("POST", f"/networking/firewalls/{firewall['id']}/devices",
                 {"id": node["id"], "type": "linode"})
    print("Attached one lab-only Cloud Firewall to every lab Linode")

    deadline = time.time() + 600
    pending = set(state["nodes"])
    while pending and time.time() < deadline:
        for name in sorted(pending):
            status = api.call(
                "GET", f"/linode/instances/{state['nodes'][name]['id']}"
            ).get("status")
            if status == "running":
                pending.discard(name)
        if pending:
            time.sleep(5)
    if pending:
        sys.exit(f"Linodes did not become ready: {sorted(pending)}")
    command_status(args)


def firewall_rules(state: dict) -> dict:
    operator = state["operator_ipv4"]
    node_addresses = [f"{node['public_ipv4']}/32" for node in state["nodes"].values()]
    return {
        "inbound_policy": "DROP", "outbound_policy": "ACCEPT", "outbound": [],
        "inbound": [
            {"label": "operator-all", "action": "ACCEPT", "protocol": "TCP",
             "ports": "1-65535", "addresses": {"ipv4": [f"{operator}/32"]}},
            {"label": "operator-udp", "action": "ACCEPT", "protocol": "UDP",
             "ports": "1-65535", "addresses": {"ipv4": [f"{operator}/32"]}},
            {"label": "operator-icmp", "action": "ACCEPT", "protocol": "ICMP",
             "addresses": {"ipv4": [f"{operator}/32"]}},
            {"label": "lab-tcp", "action": "ACCEPT", "protocol": "TCP",
             "ports": "1-65535", "addresses": {"ipv4": node_addresses}},
            {"label": "lab-udp", "action": "ACCEPT", "protocol": "UDP",
             "ports": "1-65535", "addresses": {"ipv4": node_addresses}},
            {"label": "lab-icmp", "action": "ACCEPT", "protocol": "ICMP",
             "addresses": {"ipv4": node_addresses}},
        ],
    }


def wait_running(api: Api, state: dict, names: list[str]) -> None:
    deadline = time.time() + 600
    pending = set(names)
    while pending and time.time() < deadline:
        for name in sorted(pending):
            status = api.call(
                "GET", f"/linode/instances/{state['nodes'][name]['id']}"
            ).get("status")
            if status == "running":
                pending.discard(name)
        if pending:
            time.sleep(5)
    if pending:
        sys.exit(f"Linodes did not become ready: {sorted(pending)}")


def add_nodes(args: argparse.Namespace, api: Api, state: dict,
              nodes: list[dict]) -> None:
    """Create nodes, then refresh the lab firewall so the new addresses are peers."""
    state_path = pathlib.Path(args.state)
    key = pathlib.Path(args.ssh_public_key).expanduser().read_text().strip()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(create_node, api, state["run_id"], state["tag"],
                        state["image"], key, node): node for node in nodes
        }
        for future in concurrent.futures.as_completed(futures):
            created = future.result()
            state["nodes"][created["name"]] = created
            write_state(state_path, state)
            print(f"  created {created['name']:<18} {created['region']:<13} "
                  f"{created['type']:<14} {created['public_ipv4']}")
    state["hourly_usd"] = round(
        sum(node["hourly_usd"] for node in state["nodes"].values()), 4)
    write_state(state_path, state)
    api.call("PUT", f"/networking/firewalls/{state['firewall_id']}/rules",
             firewall_rules(state))
    for node in nodes:
        api.call("POST", f"/networking/firewalls/{state['firewall_id']}/devices",
                 {"id": state["nodes"][node["name"]]["id"], "type": "linode"})
    wait_running(api, state, [node["name"] for node in nodes])


def command_grow(args: argparse.Namespace) -> None:
    """Create every topology node that the running lab does not have yet."""
    state = read_state(pathlib.Path(args.state))
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    api = Api()
    topology = json.loads(pathlib.Path(args.topology).read_text())
    # A site that the lab does without stays out: growing London's edges
    # must not create the Sydney that `up` left out.
    left_out = sorted(set(state.get("sites_left_out", []))
                      | {site for site in (args.without_sites or "").split(",") if site})
    known = {node["site"] for node in topology["nodes"]}
    topology = without_sites(topology, [site for site in left_out if site in known])
    topology, hourly = validate_topology(api, topology)
    missing = [node for node in topology["nodes"] if node["name"] not in state["nodes"]]
    if not missing:
        print("lab already has every topology node")
        return
    print(f"Adding {len(missing)} Linodes; the lab will cost ${hourly:.4f}/hour")
    state["topology_file"] = str(pathlib.Path(args.topology).resolve())
    add_nodes(args, api, state, missing)
    command_status(args)


def command_replace(args: argparse.Namespace) -> None:
    """Delete and recreate named nodes (for example a slow shared-CPU host)."""
    state = read_state(pathlib.Path(args.state))
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    api = Api()
    topology = json.loads(pathlib.Path(state["topology_file"]).read_text())
    topology, _ = validate_topology(api, topology)
    by_name = {node["name"]: node for node in topology["nodes"]}
    replacements = []
    for name in args.names:
        if name not in state["nodes"] or name not in by_name:
            sys.exit(f"{name} is not a lab node")
        old = state["nodes"].pop(name)
        api.call("DELETE", f"/linode/instances/{old['id']}", allow_404=True)
        state.setdefault("replaced", []).append(
            {"name": name, "id": old["id"], "unix": int(time.time())})
        replacements.append(by_name[name])
        print(f"  deleted {name} ({old['public_ipv4']})")
    write_state(pathlib.Path(args.state), state)
    # A deleted label can stay reserved briefly; make the new label unique.
    for node in replacements:
        node["name_suffix"] = f"r{len(state['replaced'])}"
    add_nodes(args, api, state, replacements)


def command_hosts(args: argparse.Namespace) -> None:
    """Show the physical host of every node and each shared failure domain.

    Two distributors of one region protect each other only when they are on
    different hosts: one stall of a shared host stops both at the same time.
    The command exits with status 1 when relay nodes that back each other up
    share a host.
    """
    state = read_state(pathlib.Path(args.state))
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    api = Api()
    by_host: dict[tuple[str, str], list[dict]] = {}
    for name in sorted(state["nodes"]):
        node = state["nodes"][name]
        live = api.call("GET", f"/linode/instances/{node['id']}", allow_404=True)
        node["host_uuid"] = live.get("host_uuid")
        by_host.setdefault((node["region"], node["host_uuid"] or "unknown"), []).append(node)
    write_state(pathlib.Path(args.state), state)
    conflicts = []
    for (region, host), nodes in sorted(by_host.items()):
        names = ", ".join(node["name"] for node in nodes)
        print(f"{region:<13} {host[:12]:<12} {names}")
        relays = [node["name"] for node in nodes
                  if node["role"] in ("distributor", "backbone")]
        if len(relays) > 1:
            conflicts.append((region, relays))
    for region, relays in conflicts:
        print(f"SHARED HOST in {region}: {', '.join(relays)}; replace all but one")
    if conflicts:
        sys.exit(1)
    print("every distributor and backbone relay has its own host")


def command_status(args: argparse.Namespace) -> None:
    state = read_state(pathlib.Path(args.state))
    if state is None:
        print("No active Needletail small-mesh lab")
        return
    api = Api()
    age_hours = (time.time() - state["created_unix"]) / 3600
    print(f"run {state['run_id']}  nodes {len(state['nodes'])}  "
          f"${state['hourly_usd']:.4f}/hour  age {age_hours:.2f} h  "
          f"accrued about ${state['hourly_usd'] * max(age_hours, 1):.2f}")
    deadline = state.get("watchdog_deadline_unix")
    if deadline:
        print(f"watchdog deletes the lab in {(deadline - time.time()) / 60:.0f} min")
    print(f"{'NAME':<18} {'ROLE':<12} {'REGION':<13} {'TYPE':<14} "
          f"{'PUBLIC':<16} {'VLAN':<13} STATUS")
    for name in sorted(state["nodes"]):
        node = state["nodes"][name]
        live = api.call("GET", f"/linode/instances/{node['id']}", allow_404=True)
        print(f"{name:<18} {node['role']:<12} {node['region']:<13} "
              f"{node['type']:<14} {node['public_ipv4']:<16} "
              f"{node['vlan_ipv4'] or '-':<13} {live.get('status', 'absent')}")


def command_down(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        print("No active Needletail small-mesh lab")
        return
    api = Api()
    tag = state["tag"]
    instance_ids = {node["id"] for node in state["nodes"].values()}
    instance_ids |= {item["id"] for item in api.list_all("/linode/instances", tag)}
    for instance_id in sorted(instance_ids):
        api.call("DELETE", f"/linode/instances/{instance_id}", allow_404=True)
    firewall_ids = {item["id"] for item in api.list_all("/networking/firewalls", tag)}
    if state.get("firewall_id"):
        firewall_ids.add(state["firewall_id"])
    for firewall_id in sorted(firewall_ids):
        api.call("DELETE", f"/networking/firewalls/{firewall_id}", allow_404=True)
    remaining = api.list_all("/linode/instances", tag)
    if remaining:
        sys.exit(f"{len(remaining)} tagged Linodes remain; rerun down")
    age_hours = (time.time() - state["created_unix"]) / 3600
    archive = state_path.with_name(f"small-mesh-lab-{state['run_id']}.removed.json")
    state["removed_unix"] = int(time.time())
    write_state(archive, state)
    state_path.unlink()
    print(f"Removed {len(instance_ids)} Linodes and {len(firewall_ids)} firewalls "
          f"after {age_hours:.2f} h (about "
          f"${state['hourly_usd'] * max(age_hours, 1):.2f} of instance time)")


def command_watchdog(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    deadline = int(time.time() + args.hours * 3600)
    state["watchdog_deadline_unix"] = deadline
    write_state(state_path, state)
    if args.foreground:
        while True:
            current = read_state(state_path)
            if current is None or current.get("run_id") != state["run_id"]:
                return
            if time.time() >= current.get("watchdog_deadline_unix", deadline):
                command_down(args)
                return
            time.sleep(30)
    log = state_path.with_name(f"small-mesh-watchdog-{state['run_id']}.log")
    marker = state_path.with_name(f"small-mesh-watchdog-{state['run_id']}.pid")
    if marker.is_file():
        try:
            os.kill(int(marker.read_text()), 0)
            print(f"watchdog already running; deadline moved to {args.hours} h from now")
            return
        except (OSError, ValueError):
            pass
    with open(log, "ab") as handle:
        process = subprocess.Popen(
            [sys.executable, __file__, "--state", str(state_path), "watchdog",
             "--hours", str(args.hours), "--foreground"],
            stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            start_new_session=True,
        )
    marker.write_text(str(process.pid))
    print(f"watchdog pid {process.pid} deletes the lab {args.hours} h from now")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", default=os.environ.get(
        "NEEDLETAIL_LINODE_LAB_STATE", str(DEFAULT_STATE)))
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--topology", default=str(DEFAULT_TOPOLOGY))
    up.add_argument("--run-id", default=None)
    up.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    up.add_argument("--without-sites", default="",
                    help="comma-separated sites whose nodes are not created")
    up.add_argument("--optional-sites", default="",
                    help="comma-separated sites created first and left out when one of "
                         "their nodes cannot be created (no capacity)")
    up.set_defaults(handler=command_up)
    check = sub.add_parser("check", help="price a topology against today's plans and "
                                         "capacity; creates nothing, needs no token")
    check.add_argument("--topology", default=str(DEFAULT_TOPOLOGY))
    check.add_argument("--without-sites", default="")
    check.set_defaults(handler=command_check)
    grow = sub.add_parser("grow")
    grow.add_argument("--topology", required=True)
    grow.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    grow.add_argument("--without-sites", default="",
                      help="sites not to create, besides those the lab already does without")
    grow.set_defaults(handler=command_grow)
    replace = sub.add_parser("replace")
    replace.add_argument("names", nargs="+")
    replace.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    replace.set_defaults(handler=command_replace)
    sub.add_parser("status").set_defaults(handler=command_status)
    sub.add_parser("hosts").set_defaults(handler=command_hosts)
    sub.add_parser("down").set_defaults(handler=command_down)
    watchdog = sub.add_parser("watchdog")
    watchdog.add_argument("--hours", type=float, default=6.0)
    watchdog.add_argument("--foreground", action="store_true")
    watchdog.set_defaults(handler=command_watchdog)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
