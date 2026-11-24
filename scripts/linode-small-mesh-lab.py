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
* `watchdog` starts a detached process that deletes the lab by a deadline
  even when the operator session has ended (`up --watchdog-hours` arms it
  before the first instance is created, counted from the lab's creation):
  it starts `down` WATCHDOG_LEAD_S before the deadline and deletes in
  parallel, so no instance lives past the deadline and none bills an hour
  that the deadline does not;
* `down` deletes the run's VLANs too, which Linode keeps once empty;
* autoscale slots (topology nodes marked "slot") are never created by `up`
  or `grow`: `add <slot>` creates one under the same checks (dedicated plan,
  node and hourly ceilings, an armed watchdog at most MAX_LIFE_HOURS into
  the lab's life and at least ADD_CUTOFF_S ahead, an optional budget), and
  `remove <name>` deletes one by its recorded id, only when the live
  instance still carries the run's tag and label;
* `billed` is what the lab has cost: every instance a whole hour at least
  (ceil(hours) x its price), instances removed before `down` included;
* `up` creates in stages: an optional site's core, the core (every node but
  the listener hosts), then the listener hosts in `--listener-batches`
  batches. Every stage stops at the first refusal (the creates already
  under way finish). A refusal in the core deletes the partial lab (and
  says what it billed); an account refusal there first gives up the
  optional sites, whose instances count against the same limit. A refusal
  among the listener hosts keeps what exists and records the hosts that
  are missing (`listeners_missing`), for scale-run.py to fit its ramp to
  the hosts that exist. Account limits (instances, vCPUs), capacity and
  transient errors are told apart from the API's message (refusal_kind).

The Linode token is read from LINODE_TOKEN, LINODE_PAT, or the token file. It
is never written to state, logs, or generated output.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import threading
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
# The watchdog's deadline is when the lab must be gone, not when deleting
# starts: Linode bills every started hour, so the watchdog starts `down`
# this long before it (deleting in parallel), polling this often.
WATCHDOG_LEAD_S = 300
WATCHDOG_POLL_S = 5
# `add` creates nothing this close to the deadline: a create that retries
# for minutes must never land after `down` has listed the lab.
ADD_CUTOFF_S = 900
# `add` refuses a lab whose watchdog is armed for more than this many hours
# of its life (a deadline moved later mid-run would bill more hours).
MAX_LIFE_HOURS = float(os.environ.get("NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS", "6.0"))
DELETE_WORKERS = 8
CREATE_WORKERS = 4
# `up` creates the listener hosts in this many batches after the core.
LISTENER_BATCHES = 2
# What a refused create says, lower case. The account's own limits (how many
# instances, how many dedicated vCPUs it may have: Linode support raises them
# on request) are told apart from a region short of capacity for a plan and
# from errors that pass (Api.call already retries 429 and 5xx, create_node a
# timed-out create). A message that says a region or plan has nothing to
# give is capacity, even when it suggests a support ticket, unless it names
# the account's own limit (ACCOUNT_NAMED). The account kind needs a limit
# marker (Linode's message: "Account Limit reached. Please open a support
# ticket ..."), or a hint (a quota, a ticket, an increase) with "limit" or
# "account" in the same message.
ACCOUNT_MARKERS = ("account limit", "limit reached", "limit exceeded", "exceeds your limit",
                   "maximum number of", "cpu limit", "allowed on your account",
                   "too many linodes")
ACCOUNT_HINTS = ("quota", "support ticket", "request an increase")
ACCOUNT_NAMED = ("account limit", "allowed on your account", "too many linodes",
                 "exceeds your limit")
CAPACITY_MARKERS = ("capacity", "not available in this region", "unavailable in this region",
                    "not available in the selected region", "out of stock", "sold out",
                    "no resources available", "not accepting new")
# A transient refusal (create_node's own retries spent) is tried once more,
# after this pause, at the end of its stage, rather than ending the stage.
TRANSIENT_RETRY_PAUSE_S = 30.0
TRANSIENT_MARKERS = ("http 408", "http 429", "http 500", "http 502", "http 503", "http 504",
                     "try again", "busy", "timed out", "timeout", "did not succeed after retries",
                     "connection reset", "temporarily")


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


def is_slot(node: dict) -> bool:
    """An autoscale slot (`"slot": true`): a node of the topology that `up`
    and `grow` never create. Only `add <slot>` does (the autoscaler,
    scripts/linode-autoscale.py, through it)."""
    return bool(node.get("slot"))


def slot_specs(topology: dict) -> dict[str, dict]:
    """{name: node} of the topology's autoscale slots: the nodes marked
    "slot", and any that its `autoscale_slots` list names. Each must be a
    distributor or an edge with a VLAN address (the deploy renders it into
    the peer lists by that address before it exists)."""
    named = set(topology.get("autoscale_slots") or [])
    out = {}
    for node in topology["nodes"]:
        if is_slot(node) or node["name"] in named:
            if node["role"] not in ("edge", "distributor") or not node.get("vlan_ipv4"):
                sys.exit(f"slot {node['name']}: only an edge or a distributor with a VLAN "
                         "address can be an autoscale slot")
            out[node["name"]] = {**{key: node[key] for key in (
                "name", "role", "site", "region", "type") if key in node},
                "vlan_ipv4": node["vlan_ipv4"].split("/")[0],
                "vlan_cidr": node["vlan_ipv4"] if "/" in node["vlan_ipv4"]
                else node["vlan_ipv4"] + "/24", "slot": True}
    missing = named - set(out) - {node["name"] for node in topology["nodes"]}
    if missing:
        sys.exit(f"autoscale_slots names nodes that are not in the topology: {sorted(missing)}")
    return out


def created_part(topology: dict) -> dict:
    """The topology without its autoscale slots: what `up` and `grow`
    create."""
    slots = slot_specs(topology)
    return {**topology, "nodes": [node for node in topology["nodes"]
                                  if node["name"] not in slots]}


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
    """Exit unless every node of the topology (its autoscale slots too) is a
    dedicated plan in a region that is up, and the nodes that `up` creates
    (the slots left out) are within MAX_NODES and MAX_HOURLY_USD. Returns
    the topology and the hourly price of what `up` creates."""
    names = [node["name"] for node in topology["nodes"]]
    if len(set(names)) != len(names):
        sys.exit("topology node names must be unique")
    created = created_part(topology)
    nodes = created["nodes"]
    if not nodes or len(nodes) > MAX_NODES:
        sys.exit(f"topology has {len(nodes)} nodes to create; the ceiling is {MAX_NODES} "
                 "(NEEDLETAIL_LINODE_MAX_NODES)")
    types = {item["id"]: item for item in api.list_all("/linode/types")}
    regions = {item["id"]: item for item in api.list_all("/regions")}
    _all, problems = topology_cost(topology, types, regions)
    if problems:
        sys.exit("\n".join(problems))
    hourly = sum(node["_hourly"] for node in nodes)
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


def is_listener_host(node: dict) -> bool:
    """A listener host of the scale run (role load, purpose "listeners", or
    named *-load-N in a topology without purposes): the nodes that `up`
    creates last, in batches, and does without when the account refuses
    them. Everything else is the core."""
    if node.get("role") != "load":
        return False
    purpose = node.get("purpose")
    if purpose:
        return purpose == "listeners"
    return bool(re.search(r"-load-[0-9]+$", str(node.get("name") or "")))


def listener_batches(nodes: list[dict], count: int) -> list[list[dict]]:
    """The listener hosts in `count` batches, each with its share of every
    site's hosts (the first hosts of each site in the first batch), so that
    a lab whose later batches are refused still has listeners everywhere."""
    count = max(1, int(count))
    by_site: dict[str, list[dict]] = {}
    for node in nodes:
        by_site.setdefault(node["site"], []).append(node)
    batches: list[list[dict]] = [[] for _ in range(count)]
    for site_nodes in by_site.values():
        size = math.ceil(len(site_nodes) / count)
        for index in range(count):
            batches[index] += site_nodes[index * size:(index + 1) * size]
    return [batch for batch in batches if batch]


def refusal_kind(error) -> str:
    """Why a create was refused, from the API's message: "account" (the
    account's limit of instances or of dedicated vCPUs), "capacity" (the
    region has none of the plan today), "transient" (retried already and
    still failing: a timeout, 429, 5xx, busy) or "other"."""
    text = str(error).lower()
    if any(marker in text for marker in CAPACITY_MARKERS) \
            and not any(marker in text for marker in ACCOUNT_NAMED):
        return "capacity"
    if any(marker in text for marker in ACCOUNT_MARKERS) or (
            any(marker in text for marker in ACCOUNT_HINTS)
            and ("limit" in text or "account" in text)):
        return "account"
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return "transient"
    return "other"


def create_batch(api: "Api", state: dict, state_path: pathlib.Path, nodes: list[dict], key: str,
                 capacity_blocks_plan_only: bool = False,
                 workers: int | None = None) -> dict:
    """Create `nodes` (CREATE_WORKERS at a time), recording each in the
    state as it lands, and stop at the first refusal: no create starts
    after it (the ones under way finish and are recorded). A transient
    refusal stops nothing (the stage retries it: create_staged). With
    `capacity_blocks_plan_only`, a capacity refusal stops only the nodes of
    the same region and plan; any other refusal stops all. Returns
    {"created": [names], "refused": [{"name", "region", "type", "kind",
    "error"}], "skipped": [{"name", "region", "type", "after"}]}."""
    stop: list[dict] = []
    blocked: dict[tuple[str, str], dict] = {}
    lock = threading.Lock()
    out: dict = {"created": [], "refused": [], "skipped": []}

    def one(node: dict) -> dict | None:
        with lock:
            cause = stop[0] if stop else blocked.get((node["region"], node["type"]))
            if cause:
                out["skipped"].append({"name": node["name"], "site": node["site"],
                                       "region": node["region"], "type": node["type"],
                                       "after": f"{cause['kind']} refusal of {cause['name']}"})
                return None
        try:
            return create_node(api, state["run_id"], state["tag"], state["image"], key, node)
        except Exception as error:  # noqa: BLE001 - every refusal is recorded and stops
            refusal = {"name": node["name"], "site": node["site"], "region": node["region"],
                       "type": node["type"], "kind": refusal_kind(error),
                       "error": str(error)[-400:]}
            with lock:
                out["refused"].append(refusal)
                if refusal["kind"] == "transient":
                    pass
                elif refusal["kind"] == "capacity" and capacity_blocks_plan_only:
                    blocked.setdefault((node["region"], node["type"]), refusal)
                elif not stop:
                    stop.append(refusal)
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers or CREATE_WORKERS)) \
            as pool:
        futures = [pool.submit(one, node) for node in nodes]
        for future in concurrent.futures.as_completed(futures):
            created = future.result()
            if not created:
                continue
            state["nodes"][created["name"]] = created
            write_state(state_path, state)
            out["created"].append(created["name"])
            print(f"  created {created['name']:<18} {created['region']:<13} "
                  f"{created['type']:<14} {created['public_ipv4']}")
    for refusal in out["refused"]:
        print(f"  refused {refusal['name']:<18} {refusal['region']:<13} {refusal['type']:<14} "
              f"{refusal['kind']}: {refusal['error'][-200:]}", file=sys.stderr)
    return out


def drop_site(api: "Api", state: dict, state_path: pathlib.Path, site: str, why: str) -> None:
    """Delete every node of an optional site that `up` created, by its
    recorded id, and go on without the site. Billing keeps each (a whole
    hour at least: state["removed"])."""
    dropped = [name for name, node in state["nodes"].items() if node["site"] == site]
    for name in dropped:
        node = state["nodes"].pop(name)
        api.call("DELETE", f"/linode/instances/{node['id']}", allow_404=True)
        state.setdefault("removed", []).append(removed_record(state, node, time.time()))
    if site not in state.setdefault("sites_left_out", []):
        state["sites_left_out"].append(site)
    state["hourly_usd"] = round(sum(node.get("hourly_usd", 0.0)
                                    for node in state["nodes"].values()), 4)
    write_state(state_path, state)
    cost = sum(row["usd"] for row in billed(state)["rows"]
               if not row["live"] and row["name"] in dropped)
    print(f"site {site} left out ({why}); deleted the {len(dropped)} of its nodes that were "
          f"created: ${cost:.2f} billed for them (an hour each at least)", file=sys.stderr)


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
    _all, problems = topology_cost(topology, types, regions, availability)
    slots = slot_specs(topology)
    created = [node for node in topology["nodes"] if node["name"] not in slots]
    hourly = sum(node.get("_hourly", 0.0) for node in created)
    by_type: dict[tuple[str, str], list[dict]] = {}
    for node in created:
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
            # A refusal (no capacity for the plan, a restricted region) is
            # final: raise at once, so that the nodes not yet created are not.
            if any(code in str(error) for code in ("HTTP 403", "HTTP 400")):
                raise
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
        # When this tool saw it created: billing counts from the API's
        # `created` and falls back to this.
        "created_unix": int(time.time()),
        **({"slot": True} if node.get("slot") else {}),
    }


def missing_entry(node: dict, kind: str, why: str) -> dict:
    return {"name": node["name"], "site": node["site"], "region": node["region"],
            "type": node["type"], "kind": kind, "why": why[-300:]}


def create_staged(args: argparse.Namespace, api: "Api", state: dict, state_path: pathlib.Path,
                  nodes: list[dict], optional: list[str], key: str) -> None:
    """`up`'s creation, in stages, each stopping at its first refusal:

    1. the core of each optional site (Sydney: the region short of capacity,
       tried first): a refusal deletes what of the site was created and the
       lab goes on without it;
    2. the core, every other node but the listener hosts (contributor,
       source, forwarders, edges with the standby ones, view hosts, LiveKit
       and its Redis): without it the lab is of no use, so a refusal deletes
       the partial lab (`down`) and says what it billed. An account refusal
       (the limit of instances or vCPUs) first gives up the optional sites,
       whose instances count against the same limit, and tries the rest of
       the core once more;
    3. the listener hosts, in `--listener-batches` batches (LISTENER_BATCHES),
       each with a share of every site's: a refusal keeps what exists. An
       account refusal, or one of no known kind, stops every later create;
       a capacity refusal stops only that region's plan; a transient one
       only that host. The hosts not created are recorded in
       `listeners_missing` with why, and scale-run.py fits its ramp to the
       hosts that exist. An optional site left with no listener host is
       deleted (its edges would carry no listener).
    A transient refusal (create_node's and Api.call's retries spent) stops
    nothing: the stage tries its node once more at its end (attempt), and
    only a second refusal counts (in the core, it deletes the lab). Every
    refusal is kept in `refusals`."""
    batches = getattr(args, "listener_batches", None) or LISTENER_BATCHES
    first, rest = split_optional(nodes, optional)
    core = [node for node in rest if not is_listener_host(node)]
    listeners = [node for node in nodes if is_listener_host(node)]
    state.setdefault("refusals", [])

    def note(result: dict, stage: str) -> None:
        state["refusals"] += [{**item, "stage": stage} for item in result["refused"]]
        write_state(state_path, state)

    def attempt(todo: list[dict], stage: str, plan_only: bool = False) -> dict:
        """create_batch, then once more for the nodes refused by a transient
        error, unless the stage was stopped by another refusal (or, with
        `plan_only`, the node's plan was refused for capacity). The first
        transient refusals are kept in `refusals` ("retried")."""
        result = create_batch(api, state, state_path, todo, key,
                              capacity_blocks_plan_only=plan_only)
        transient = [item for item in result["refused"] if item["kind"] == "transient"]
        others = [item for item in result["refused"] if item["kind"] != "transient"]
        if not transient or any(not (plan_only and item["kind"] == "capacity")
                                for item in others):
            return result
        blocked = {(item["region"], item["type"]) for item in others}
        again = [node for node in todo if node["name"] in {item["name"] for item in transient}
                 and (node["region"], node["type"]) not in blocked]
        state["refusals"] += [{**item, "stage": stage, "retried": True} for item in transient
                              if item["name"] in {node["name"] for node in again}]
        write_state(state_path, state)
        print(f"{stage}: {len(again)} transient refusal(s) ("
              + ", ".join(node["name"] for node in again)
              + f"); trying them once more in {TRANSIENT_RETRY_PAUSE_S:.0f} s", file=sys.stderr)
        time.sleep(TRANSIENT_RETRY_PAUSE_S)
        retried = create_batch(api, state, state_path, again, key,
                               capacity_blocks_plan_only=plan_only)
        names = {node["name"] for node in again}
        return {"created": result["created"] + retried["created"],
                "refused": [item for item in result["refused"] if item["name"] not in names]
                + retried["refused"],
                "skipped": result["skipped"] + retried["skipped"]}

    for site, site_nodes in first.items():
        result = attempt([node for node in site_nodes if not is_listener_host(node)],
                         f"core of optional site {site}")
        if result["refused"]:
            note(result, f"core of optional site {site}")
            refused = result["refused"][0]
            drop_site(api, state, state_path, site,
                      f"{refused['kind']} refusal of {refused['name']}")
    print(f"Core: {len(core)} nodes")
    result = attempt(core, "core")
    kept_optional = [site for site in first if site not in state.get("sites_left_out", [])]
    if result["refused"] and kept_optional and any(
            item["kind"] == "account" for item in result["refused"]):
        note(result, "core")
        for site in kept_optional:
            drop_site(api, state, state_path, site,
                      "the account refused the core: the optional site's instances are given "
                      "back to its limit")
        result = attempt([node for node in core if node["name"] not in state["nodes"]], "core")
    if result["refused"]:
        note(result, "core")
        refused = result["refused"][0]
        cost = billed(state)
        print(f"the core was refused at {refused['name']} ({refused['kind']}: "
              f"{refused['error'][-200:]}); creation stopped there. {cost['live']} instances "
              f"exist and {cost['removed']} were deleted already; deleting the partial lab. It "
              f"bills ${cost['usd']:.2f} ({cost['instance_hours']} instance-hours: every "
              "instance created bills an hour at least)", file=sys.stderr)
        command_down(args)
        sys.exit(1)

    listeners = [node for node in listeners
                 if node["site"] not in state.get("sites_left_out", [])]
    missing: list[dict] = []
    stopped: str | None = None
    blocked: dict[tuple[str, str], str] = {}
    for index, batch in enumerate(listener_batches(listeners, batches), 1):
        todo = []
        for node in batch:
            why = stopped or blocked.get((node["region"], node["type"]))
            if why:
                missing.append(missing_entry(node, "not attempted", why))
            else:
                todo.append(node)
        if not todo:
            continue
        print(f"Listener hosts, batch {index}: {len(todo)} nodes")
        result = attempt(todo, f"listener batch {index}", plan_only=True)
        note(result, f"listener batch {index}")
        for item in result["refused"]:
            missing.append({**{key_: item[key_] for key_ in ("name", "site", "region", "type",
                                                               "kind")},
                            "why": item["error"][-300:]})
            cause = f"{item['kind']} refusal of {item['name']}"
            if item["kind"] == "transient":
                # Refused twice: that host only is missing.
                continue
            if item["kind"] == "capacity":
                blocked.setdefault((item["region"], item["type"]), cause)
            elif not stopped:
                stopped = cause
        for item in result["skipped"]:
            missing.append(missing_entry(item, "not attempted", item["after"]))
    for site in first:
        if site in state.get("sites_left_out", []):
            continue
        # Only a site that was to have listener hosts can be left without them
        # (wave 9: a topology without any deleted Sydney's whole core).
        if not any(node["site"] == site and is_listener_host(node) for node in nodes):
            continue
        if not any(node["site"] == site and is_listener_host(node)
                   for node in state["nodes"].values()):
            drop_site(api, state, state_path, site, "none of its listener hosts was created")
            missing = [item for item in missing if item["site"] != site]
    state["listeners_missing"] = missing
    state["hourly_usd"] = round(sum(node.get("hourly_usd", 0.0)
                                    for node in state["nodes"].values()), 4)
    write_state(state_path, state)
    if missing:
        made = sum(1 for node in state["nodes"].values() if is_listener_host(node))
        print(f"listener hosts: {made} created, {len(missing)} missing ("
              + ", ".join(f"{item['name']}: {item['kind']}" for item in missing)
              + "); the lab is kept. scale-run.py fits its steps to the hosts that exist; "
              "`grow` creates the missing ones once the account allows", file=sys.stderr)
    idle = sorted({item["site"] for item in missing} - {
        node["site"] for node in state["nodes"].values() if is_listener_host(node)})
    if idle:
        print(f"no listener host in {', '.join(idle)}: its edges, SFU and view host are kept "
              "and billed but carry no listener (scale-run.py gives its share to nobody and "
              "runs no joins there). `grow` once the account allows, or `down` and `up` "
              f"--without-sites {','.join(idle)}", file=sys.stderr)


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
        # The autoscale slots: never created by up or grow; `add` creates
        # one, `remove` deletes it, and the deploy renders every one into
        # the peer lists by its VLAN address from the first push.
        "slots": slot_specs(topology),
        # Instances deleted before `down` (remove, replace): billing keeps
        # them, each a whole hour at least.
        "removed": [],
    }
    write_state(state_path, state)
    if getattr(args, "watchdog_hours", None):
        # Armed before the first instance exists: a creation that hangs, or
        # an operator session that ends during it, still ends in `down`.
        # Counted from the lab's creation, so the lab's life is the hours
        # given, never a little more.
        command_watchdog(argparse.Namespace(
            state=str(state_path), hours=args.watchdog_hours, foreground=False,
            deadline_unix=int(state["created_unix"] + args.watchdog_hours * 3600)))
        state["watchdog_deadline_unix"] = read_state(state_path)["watchdog_deadline_unix"]
    topology = created_part(topology)
    print(f"Creating {len(topology['nodes'])} Linodes at ${hourly:.4f}/hour "
          f"(run {run_id})")
    key = key_path.read_text().strip()
    create_staged(args, api, state, state_path, topology["nodes"], optional, key)

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


def lab_going_down(state_path: pathlib.Path, run_id: str) -> str | None:
    """Why the lab on disk is no longer one to add to: its state is gone
    (`down` finished), is another run's, or `down` has begun. None when it
    is still up."""
    current = read_state(state_path)
    if current is None:
        return "the lab state is gone (down has run)"
    if current.get("run_id") != run_id:
        return f"the lab state is now run {current.get('run_id')}'s"
    if current.get("down_started_unix"):
        return "down has begun"
    return None


def add_nodes(args: argparse.Namespace, api: Api, state: dict,
              nodes: list[dict]) -> None:
    """Create nodes, then refresh the lab firewall so the new addresses are
    peers and attach it to them.

    Two things never leave a node behind. `down` racing the create: before
    each new node is recorded the state on disk is read again, and if
    `down` has begun (or finished) nothing is written back and every node
    this call made is deleted at once. A firewall that cannot be set: the
    new nodes are deleted (never pushed or served with every port open)."""
    state_path = pathlib.Path(args.state)
    key = pathlib.Path(args.ssh_public_key).expanduser().read_text().strip()
    made: list[dict] = []
    gone: str | None = None
    failures: list[str] = []

    def record() -> bool:
        """Write the state unless `down` has begun or finished (never
        recreate a state file that `down` removed, never erase its mark)."""
        nonlocal gone
        gone = gone or lab_going_down(state_path, state["run_id"])
        if gone:
            return False
        write_state(state_path, state)
        return True

    def give_up() -> None:
        for created in made:
            api.call("DELETE", f"/linode/instances/{created['id']}", allow_404=True)
        sys.exit(f"add: {gone}; deleted the {len(made)} instance(s) this call created "
                 f"({', '.join(item['label'] for item in made) or 'none'})")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(create_node, api, state["run_id"], state["tag"],
                        state["image"], key, node): node for node in nodes
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                created = future.result()
            except Exception as error:  # noqa: BLE001 - the others are still recorded
                failures.append(f"{futures[future]['name']}: {error}")
                continue
            made.append(created)
            if gone:
                continue
            state["nodes"][created["name"]] = created
            if record():
                print(f"  created {created['name']:<18} {created['region']:<13} "
                      f"{created['type']:<14} {created['public_ipv4']}")
    state["hourly_usd"] = round(
        sum(node["hourly_usd"] for node in state["nodes"].values()), 4)
    if not record():
        give_up()
    # Every node this call made gets the firewall, even when another
    # failed to create (that failure is raised after).
    names = [created["name"] for created in made]
    try:
        api.call("PUT", f"/networking/firewalls/{state['firewall_id']}/rules",
                 firewall_rules(state))
        for name in names:
            api.call("POST", f"/networking/firewalls/{state['firewall_id']}/devices",
                     {"id": state["nodes"][name]["id"], "type": "linode"})
    except Exception as error:  # noqa: BLE001 - no node serves without the firewall
        print(f"the lab firewall could not be set for {', '.join(names)}: {error}; "
              "deleting them", file=sys.stderr)
        gone = lab_going_down(state_path, state["run_id"])
        if gone:
            give_up()
        try:
            remove_nodes(api, state_path, state, names, slots_only=False,
                         refresh_firewall=False)
        except (Exception, SystemExit) as again:  # noqa: BLE001
            sys.exit(f"add: {', '.join(names)} have no lab firewall and could not be "
                     f"deleted ({again}): run down")
        sys.exit(f"add: the lab firewall could not be set for {', '.join(names)}; "
                 "deleted them")
    if failures:
        raise RuntimeError("\n".join(failures))
    wait_running(api, state, names)


def firewall_attached(api: Api, state: dict, name: str) -> bool:
    """Whether the lab's Cloud Firewall is attached to node `name`'s
    instance (checked before a slot is pushed and started)."""
    node = state["nodes"].get(name)
    if not node or not state.get("firewall_id"):
        return False
    devices = api.list_all(f"/networking/firewalls/{state['firewall_id']}/devices")
    return any((item.get("entity") or {}).get("id") == node["id"]
               and (item.get("entity") or {}).get("type", "linode") == "linode"
               for item in devices)


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
    state["slots"] = slot_specs(topology)
    write_state(pathlib.Path(args.state), state)
    # Autoscale slots are never grown: only `add` creates one.
    missing = [node for node in created_part(topology)["nodes"]
               if node["name"] not in state["nodes"]]
    if not missing:
        print("lab already has every topology node (autoscale slots are created by add)")
        return
    print(f"Adding {len(missing)} Linodes; the lab will cost ${hourly:.4f}/hour")
    state["topology_file"] = str(pathlib.Path(args.topology).resolve())
    try:
        add_nodes(args, api, state, missing)
    except RuntimeError as error:
        # Some were refused: those created are recorded (and behind the
        # firewall); the listener hosts still missing stay recorded.
        refresh_listeners_missing(pathlib.Path(args.state), str(error))
        sys.exit(f"grow: {error}")
    refresh_listeners_missing(pathlib.Path(args.state))
    command_status(args)


def refresh_listeners_missing(state_path: pathlib.Path, error: str = "") -> None:
    """After `grow`: the listener hosts that `up` recorded as missing and
    that the lab still does not have (a refusal of this grow, `error`,
    named on each that it did not create)."""
    state = read_state(state_path)
    if state is None or not state.get("listeners_missing"):
        return
    still = [item if not error else {**item, "why": f"grow: {error}"[-300:]}
             for item in state["listeners_missing"] if item["name"] not in state["nodes"]]
    state["listeners_missing"] = still
    write_state(state_path, state)


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
        state.setdefault("removed", []).append(removed_record(state, old, time.time()))
        replacements.append(by_name[name])
        print(f"  deleted {name} ({old['public_ipv4']})")
    write_state(pathlib.Path(args.state), state)
    # A deleted label can stay reserved briefly; make the new label unique.
    for node in replacements:
        node["name_suffix"] = f"r{len(state['replaced'])}"
    add_nodes(args, api, state, replacements)


# ------------------------------------------------------------------ billing

def created_unix(node: dict, fallback: float | None = None) -> float | None:
    """When an instance was created: the API's `created` (UTC, no zone),
    else the time this tool recorded, else `fallback`."""
    text = node.get("created")
    if text:
        try:
            parsed = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    if node.get("created_unix") is not None:
        return float(node["created_unix"])
    return fallback


def billed_hours(start: float, end: float) -> int:
    """Hours that Linode bills an instance that lived from `start` to `end`:
    every started hour, and one at least, however short its life."""
    return max(1, math.ceil(max(0.0, end - start) / 3600 - 1e-9))


def removed_record(state: dict, node: dict, deleted: float) -> dict:
    """What billing keeps of an instance deleted before `down`."""
    return {"name": node["name"], "id": node["id"], "label": node.get("label"),
            "role": node.get("role"), "site": node.get("site"), "type": node.get("type"),
            "hourly_usd": node.get("hourly_usd", 0.0),
            "created_unix": created_unix(node, state.get("created_unix")),
            "deleted_unix": int(deleted)}


def billed(state: dict, now: float | None = None, until: float | None = None,
           extra: list[dict] = ()) -> dict:
    """What the lab has cost by `now` (default: this moment), or would cost
    were every live instance kept to `until` (the watchdog's deadline, for
    a projection) and each of `extra` ({"hourly_usd"}) created now and kept
    as long: every instance ceil(hours) x its price, a whole hour at least,
    the instances that `remove` and `replace` deleted included."""
    now = time.time() if now is None else now
    end = now if until is None else max(now, until)
    rows = []
    for name, node in sorted(state.get("nodes", {}).items()):
        start = created_unix(node, state.get("created_unix"))
        hours = billed_hours(now if start is None else start, end)
        rows.append({"name": name, "hours": hours, "usd": hours * node.get("hourly_usd", 0.0),
                     "live": True})
    for item in state.get("removed", []):
        deleted = item.get("deleted_unix")
        deleted = now if deleted is None else deleted
        start = item.get("created_unix")
        hours = billed_hours(deleted if start is None else start, deleted)
        rows.append({"name": item["name"], "hours": hours,
                     "usd": hours * item.get("hourly_usd", 0.0), "live": False})
    for item in extra:
        hours = billed_hours(now, end)
        rows.append({"name": item.get("name", "new"), "hours": hours,
                     "usd": hours * item.get("hourly_usd", 0.0), "live": True})
    return {"usd": round(sum(row["usd"] for row in rows), 4),
            "instance_hours": sum(row["hours"] for row in rows),
            "live": sum(1 for row in rows if row["live"]),
            "removed": len(state.get("removed", [])),
            "hourly_usd": round(sum(node.get("hourly_usd", 0.0)
                                    for node in state.get("nodes", {}).values()), 4),
            "until_unix": int(end), "rows": rows}


def account_billed(api: "Api", run_tag: str | None, now: float | None = None) -> dict:
    """What the account's other Needletail labs bill now: every live
    instance tagged needletail-qualification but not `run_tag`, ceil(hours)
    x its plan's price (the dev box has no tag and is never counted, never
    touched). Labs already deleted are not listed by the API: give what
    they cost as --spent-before-usd."""
    now = time.time() if now is None else now
    prices = {item["id"]: item["price"]["hourly"] for item in api.list_all("/linode/types")}
    rows = []
    for item in api.list_all("/linode/instances", "needletail-qualification"):
        if run_tag and run_tag in item.get("tags", []):
            continue
        start = created_unix({"created": item.get("created")}, now)
        hours = billed_hours(start, now)
        rows.append({"label": item.get("label"), "type": item.get("type"), "hours": hours,
                     "usd": hours * prices.get(item.get("type"), 0.0)})
    return {"usd": round(sum(row["usd"] for row in rows), 4), "instances": rows}


def budget_check(state: dict, new: list[dict], budget_usd: float, spent_before_usd: float = 0.0,
                 other_labs_usd: float = 0.0, now: float | None = None) -> tuple[bool, dict]:
    """Whether creating `new` keeps the work within `budget_usd`: this lab
    projected to its watchdog's deadline with the new instances (each a
    whole hour at least), plus what other labs bill now and what earlier
    labs spent. (ok, the figures). The deadline is when the lab is gone:
    the watchdog starts `down` WATCHDOG_LEAD_S before it, so no instance
    bills an hour past it."""
    now = time.time() if now is None else now
    deadline = state.get("watchdog_deadline_unix")
    if not deadline:
        return False, {"why": "no watchdog deadline in the lab state"}
    projection = billed(state, now, deadline, new)
    total = projection["usd"] + spent_before_usd + other_labs_usd
    figures = {"lab_projected_usd": projection["usd"], "spent_before_usd": spent_before_usd,
               "other_labs_usd": other_labs_usd, "total_usd": round(total, 4),
               "budget_usd": budget_usd, "until_unix": int(deadline)}
    return total <= budget_usd + 1e-9, figures


# ------------------------------------------------------------- slots

def lab_slots(state: dict) -> dict[str, dict]:
    """The autoscale slots of a lab: those `up` recorded, or the topology
    file's (a state from before slots existed)."""
    if state.get("slots"):
        return state["slots"]
    path = pathlib.Path(state.get("topology_file") or "")
    if path.is_file():
        return slot_specs(json.loads(path.read_text()))
    return {}


def watchdog_problems(state: dict, now: float, max_life_hours: float | None = None) -> list[str]:
    """Why no instance may be created in the lab now, from its watchdog:
    not armed; its deadline less than ADD_CUTOFF_S ahead (a create that
    retries must never land after `down` has listed the lab); or armed for
    more than `max_life_hours` of the lab's life (a deadline moved later,
    by `watchdog --hours` mid-run, would bill hours that no budget
    counted). Empty when a create may go ahead."""
    deadline = state.get("watchdog_deadline_unix")
    if not deadline or deadline <= now:
        return ["the lab's watchdog is not armed (or its deadline has passed): "
                "no instance is created outside it"]
    problems = []
    if deadline - now < ADD_CUTOFF_S:
        problems.append(f"the watchdog deletes the lab in {(deadline - now) / 60:.1f} min: "
                        f"nothing is created within {ADD_CUTOFF_S // 60} min of its deadline")
    if max_life_hours is not None and state.get("created_unix"):
        life = (deadline - state["created_unix"]) / 3600
        if life > max_life_hours + 1e-9:
            problems.append(f"the watchdog is armed for {life:.3f} h of the lab's life; at most "
                            f"{max_life_hours:g} h (NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS)")
    return problems


def add_slots(args: argparse.Namespace, api: "Api", state: dict, names: list[str],
              budget_usd: float | None = None, spent_before_usd: float = 0.0,
              other_labs_usd: float = 0.0,
              max_life_hours: float | None = None) -> list[str]:
    """Create autoscale slots of the lab, with every check that `up` makes
    and more: each name is a slot of this lab that it does not have, at a
    site that it does not leave out; every plan is dedicated and its region
    up (from the API); the lab stays within MAX_NODES and MAX_HOURLY_USD;
    the watchdog is armed, its deadline at least ADD_CUTOFF_S ahead and at
    most `max_life_hours` (default MAX_LIFE_HOURS) into the lab's life
    (watchdog_problems); and with `budget_usd` the projection to that
    deadline (budget_check) stays within it. Exits on any refusal before
    anything is created. Returns the names created."""
    max_life_hours = MAX_LIFE_HOURS if max_life_hours is None else max_life_hours
    slots = lab_slots(state)
    if not names:
        sys.exit("add: name the slots to create")
    problems = []
    for name in names:
        if name not in slots:
            problems.append(f"{name} is not an autoscale slot of this lab "
                            f"(slots: {', '.join(sorted(slots)) or 'none'})")
        elif name in state["nodes"]:
            problems.append(f"{name} exists already")
        elif slots[name]["site"] in state.get("sites_left_out", []):
            problems.append(f"{name}: the lab does without site {slots[name]['site']}")
    if len(set(names)) != len(names):
        problems.append("a slot is named twice")
    problems += watchdog_problems(state, time.time(), max_life_hours)
    if problems:
        sys.exit("add refused: " + "; ".join(problems))
    nodes = [{**slots[name], "vlan_ipv4": slots[name].get("vlan_cidr")
              or slots[name]["vlan_ipv4"] + "/24"} for name in names]
    for node in nodes:
        # A deleted label can stay reserved for a while: a slot made again
        # after `remove` gets a label of its own.
        again = sum(1 for item in state.get("removed", []) if item["name"] == node["name"])
        if again:
            node["name_suffix"] = f"a{again}"
    types = {item["id"]: item for item in api.list_all("/linode/types")}
    regions = {item["id"]: item for item in api.list_all("/regions")}
    hourly, problems = topology_cost({"nodes": nodes}, types, regions)
    if problems:
        sys.exit("add refused: " + "; ".join(problems))
    count = len(state["nodes"]) + len(nodes)
    if count > MAX_NODES:
        sys.exit(f"add refused: the lab would have {count} nodes; the ceiling is {MAX_NODES} "
                 "(NEEDLETAIL_LINODE_MAX_NODES)")
    total = sum(node.get("hourly_usd", 0.0) for node in state["nodes"].values()) + hourly
    if total > MAX_HOURLY_USD + 1e-9:
        sys.exit(f"add refused: the lab would cost ${total:.4f}/hour; the ceiling is "
                 f"${MAX_HOURLY_USD:.2f} (NEEDLETAIL_LINODE_MAX_HOURLY_USD)")
    if budget_usd is not None:
        ok, figures = budget_check(state, [{"name": node["name"], "hourly_usd": node["_hourly"]}
                                           for node in nodes],
                                   budget_usd, spent_before_usd, other_labs_usd)
        if not ok:
            sys.exit(f"add refused: over the budget: {json.dumps(figures)}")
    print(f"Adding {len(nodes)} slot(s) {', '.join(names)}: the lab will cost "
          f"${total:.4f}/hour")
    add_nodes(args, api, state, nodes)
    return names


def remove_nodes(api: "Api", state_path: pathlib.Path, state: dict, names: list[str],
                 slots_only: bool = True, refresh_firewall: bool = True) -> list[str]:
    """Delete lab nodes by their recorded instance ids. Every name must be a
    node of the lab state (an autoscale slot, unless `slots_only` is
    False), and its live instance must still carry the run's tag and the
    label this tool gave it: an id that now names anything else is never
    deleted. Checks every name before it deletes any. Billing keeps each
    one (state["removed"]); the firewall's rules lose its address."""
    slots = lab_slots(state)
    if not names:
        sys.exit("remove: name the nodes to delete")
    plan = []
    for name in names:
        node = state["nodes"].get(name)
        if node is None:
            sys.exit(f"remove refused: {name} is not a node of this lab")
        if slots_only and name not in slots:
            sys.exit(f"remove refused: {name} is not an autoscale slot (--any-lab-node to "
                     "delete another node of this lab)")
        if not node.get("id"):
            sys.exit(f"remove refused: {name} has no recorded instance id")
        live = api.call("GET", f"/linode/instances/{node['id']}", allow_404=True)
        if live:
            if state["tag"] not in live.get("tags", []):
                sys.exit(f"remove refused: instance {node['id']} ({live.get('label')}) does not "
                         f"carry the run's tag {state['tag']}")
            if node.get("label") and live.get("label") != node["label"]:
                sys.exit(f"remove refused: instance {node['id']} is labelled "
                         f"{live.get('label')}, not {node['label']}")
        plan.append((name, node, bool(live)))
    for name, node, exists in plan:
        if exists:
            api.call("DELETE", f"/linode/instances/{node['id']}", allow_404=True)
        state["nodes"].pop(name)
        state.setdefault("removed", []).append(removed_record(state, node, time.time()))
        write_state(state_path, state)
        print(f"  deleted {name} ({node.get('public_ipv4')})")
    state["hourly_usd"] = round(
        sum(node.get("hourly_usd", 0.0) for node in state["nodes"].values()), 4)
    write_state(state_path, state)
    if refresh_firewall and state.get("firewall_id") and state.get("operator_ipv4"):
        api.call("PUT", f"/networking/firewalls/{state['firewall_id']}/rules",
                 firewall_rules(state))
    return [name for name, _node, _exists in plan]


def command_add(args: argparse.Namespace) -> None:
    state = read_state(pathlib.Path(args.state))
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    api = Api()
    other = account_billed(api, state["tag"])["usd"] if args.budget_usd is not None else 0.0
    add_slots(args, api, state, args.names, args.budget_usd, args.spent_before_usd, other,
              args.max_watchdog_hours)
    command_status(args)


def command_remove(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    remove_nodes(Api(), state_path, state, args.names, not args.any_lab_node)
    print_billed(state)


def print_billed(state: dict) -> None:
    now = billed(state)
    line = (f"billed so far ${now['usd']:.2f} ({now['instance_hours']} instance-hours, "
            f"{now['live']} live, {now['removed']} removed); ${now['hourly_usd']:.4f}/hour now")
    deadline = state.get("watchdog_deadline_unix")
    if deadline:
        line += f"; ${billed(state, until=deadline)['usd']:.2f} if kept to the watchdog"
    print(line)


def command_billed(args: argparse.Namespace) -> None:
    state = read_state(pathlib.Path(args.state))
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    if args.json:
        print(json.dumps(billed(state), indent=1))
        return
    print_billed(state)


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
          f"billed ${billed(state)['usd']:.2f} (whole hours, removed nodes included)")
    deadline = state.get("watchdog_deadline_unix")
    if deadline:
        print(f"watchdog: the lab is gone in {(deadline - time.time()) / 60:.0f} min "
              f"(down starts {WATCHDOG_LEAD_S // 60} min before)")
    if state.get("listeners_missing"):
        print("listener hosts missing (refused at up; scale-run.py fits its steps to the rest): "
              + ", ".join(f"{item['name']} ({item['kind']})"
                          for item in state["listeners_missing"]))
    print(f"{'NAME':<18} {'ROLE':<12} {'REGION':<13} {'TYPE':<14} "
          f"{'PUBLIC':<16} {'VLAN':<13} STATUS")
    shared = []
    for name in sorted(state["nodes"]):
        node = state["nodes"][name]
        live = api.call("GET", f"/linode/instances/{node['id']}", allow_404=True)
        live_type = live.get("type", node["type"])
        if not live_type.startswith("g6-dedicated") and live:
            shared.append(f"{name} ({live_type})")
        print(f"{name:<18} {node['role']:<12} {node['region']:<13} "
              f"{live_type:<14} {node['public_ipv4']:<16} "
              f"{node['vlan_ipv4'] or '-':<13} {live.get('status', 'absent')}")
    if shared:
        print("SHARED INSTANCES IN THE LAB: " + ", ".join(shared))
        sys.exit(2)
    print(f"every one of the {len(state['nodes'])} instances is a dedicated-CPU type")


def command_down(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        print("No active Needletail small-mesh lab")
        return
    api = Api()
    tag = state["tag"]
    # Marked first: an `add` whose create lands from here on deletes what
    # it made rather than writing the state back (add_nodes).
    state.setdefault("down_started_unix", int(time.time()))
    write_state(state_path, state)
    instance_ids = {node["id"] for node in state["nodes"].values()}
    instance_ids |= {item["id"] for item in api.list_all("/linode/instances", tag)}
    delete_instances(api, instance_ids)
    firewall_ids = {item["id"] for item in api.list_all("/networking/firewalls", tag)}
    if state.get("firewall_id"):
        firewall_ids.add(state["firewall_id"])
    for firewall_id in sorted(firewall_ids):
        api.call("DELETE", f"/networking/firewalls/{firewall_id}", allow_404=True)
    remaining = api.list_all("/linode/instances", tag)
    if remaining:
        sys.exit(f"{len(remaining)} tagged Linodes remain; rerun down")
    left = delete_vlans(api, state["run_id"])
    if left:
        print(f"VLANs still attached, not deleted (delete them with DELETE "
              f"/networking/vlans/<region>/<label> once empty): {', '.join(left)}",
              file=sys.stderr)
    # A second sweep by the run tag: a create that was in flight when the
    # lab was first listed has landed by now.
    late = {item["id"] for item in api.list_all("/linode/instances", tag)}
    if late:
        print(f"{len(late)} tagged Linodes appeared during down; deleting them",
              file=sys.stderr)
        delete_instances(api, late)
        instance_ids |= late
        remaining = api.list_all("/linode/instances", tag)
        if remaining:
            sys.exit(f"{len(remaining)} tagged Linodes remain; rerun down")
    age_hours = (time.time() - state["created_unix"]) / 3600
    archive = state_path.with_name(f"small-mesh-lab-{state['run_id']}.removed.json")
    state["removed_unix"] = int(time.time())
    total = billed(state)
    state["billed_usd"] = total["usd"]
    write_state(archive, state)
    state_path.unlink()
    print(f"Removed {len(instance_ids)} Linodes and {len(firewall_ids)} firewalls "
          f"after {age_hours:.2f} h (${total['usd']:.2f} of instance time billed in whole "
          f"hours, {total['instance_hours']} instance-hours)")


def delete_instances(api: "Api", instance_ids, workers: int = DELETE_WORKERS) -> None:
    """Delete instances by id, several at a time (the watchdog's `down` must
    be done before its deadline). Raises when any delete failed, after
    trying every one."""
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(api.call, "DELETE", f"/linode/instances/{ident}",
                               None, True): ident for ident in sorted(instance_ids)}
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as error:  # noqa: BLE001 - try every one, then report
                failures.append(f"{futures[future]}: {error}")
    if failures:
        raise RuntimeError("deleting instances failed: " + "; ".join(sorted(failures)))


def delete_vlans(api: "Api", run_id: str, attempts: int = 12, pause: float = 10.0) -> list[str]:
    """Delete the run's VLANs (`ntsm-<run id>-<region>`, made with its
    instances): Linode keeps a VLAN after its last instance is deleted. A
    VLAN that an instance still detaches from refuses the delete for a few
    seconds, so each is tried again. The VLANs of an optional site that was
    left out are found too: they are listed, not taken from the state.
    Returns the labels that could not be deleted."""
    prefix = f"ntsm-{run_id}-"
    pending = {(item["region"], item["label"]) for item in api.list_all("/networking/vlans")
               if str(item.get("label", "")).startswith(prefix)}
    for attempt in range(attempts):
        for region, label in sorted(pending):
            try:
                api.call("DELETE", f"/networking/vlans/{region}/{label}", allow_404=True)
                pending.discard((region, label))
            except RuntimeError:
                pass
        if not pending:
            break
        if attempt + 1 < attempts:
            time.sleep(pause)
    return sorted(label for _region, label in pending)


def command_watchdog(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    # The deadline is when the lab must be gone. Given (by `up`, from the
    # lab's creation; by the detached process, as its parent set it) or
    # `--hours` from now.
    deadline = getattr(args, "deadline_unix", None) or int(time.time() + args.hours * 3600)
    state["watchdog_deadline_unix"] = int(deadline)
    write_state(state_path, state)
    if args.foreground:
        watch_until_down(args, state_path, state["run_id"], int(deadline))
        return
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
             "--hours", str(args.hours), "--deadline-unix", str(int(deadline)),
             "--foreground"],
            stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            start_new_session=True,
        )
    marker.write_text(str(process.pid))
    print(f"watchdog pid {process.pid}: the lab is gone by "
          f"{(deadline - time.time()) / 3600:.2f} h from now (down starts "
          f"{WATCHDOG_LEAD_S // 60} min before)")


def watch_until_down(args: argparse.Namespace, state_path: pathlib.Path, run_id: str,
                     deadline: int, clock=time.time, sleep=time.sleep,
                     down=None) -> None:
    """The detached watchdog's loop: every WATCHDOG_POLL_S, read the
    deadline from the state (a later `watchdog` call moves it), and from
    WATCHDOG_LEAD_S before it run `down` until the state is gone; a `down`
    that fails is run again at the next poll."""
    down = down or command_down
    while True:
        current = read_state(state_path)
        if current is None or current.get("run_id") != run_id:
            return
        due = current.get("watchdog_deadline_unix", deadline) - WATCHDOG_LEAD_S
        if clock() >= due:
            try:
                down(args)
            except (Exception, SystemExit) as error:  # noqa: BLE001 - try again
                print(f"watchdog: down failed ({error}); trying again", file=sys.stderr,
                      flush=True)
        sleep(WATCHDOG_POLL_S)


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
    up.add_argument("--watchdog-hours", type=float, default=None,
                    help="arm the watchdog (delete the lab after this many hours) before the "
                         "first instance is created")
    up.add_argument("--optional-sites", default="",
                    help="comma-separated sites whose core is created first; the site is left "
                         "out when one of its nodes cannot be created (no capacity), when the "
                         "account refuses the core, or when none of its listener hosts is")
    up.add_argument("--listener-batches", type=int, default=LISTENER_BATCHES,
                    help="the listener hosts are created after the core in this many batches; "
                         "a refusal among them keeps the lab and records the hosts missing")
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
    add = sub.add_parser("add", help="create autoscale slots of the lab (never anything else)")
    add.add_argument("names", nargs="+")
    add.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    add.add_argument("--budget-usd", type=float, default=None,
                     help="refuse when this lab to its watchdog's deadline, the new slots, the "
                          "other labs' live instances and --spent-before-usd pass it")
    add.add_argument("--spent-before-usd", type=float, default=0.0,
                     help="what earlier, deleted labs of the work cost")
    add.add_argument("--max-watchdog-hours", type=float, default=None,
                     help="refuse when the watchdog is armed for more of the lab's life than "
                          "this (default NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS or 6.0)")
    add.set_defaults(handler=command_add)
    remove = sub.add_parser("remove", help="delete autoscale slots of the lab by recorded id")
    remove.add_argument("names", nargs="+")
    remove.add_argument("--any-lab-node", action="store_true",
                        help="allow a lab node that is not an autoscale slot")
    remove.set_defaults(handler=command_remove)
    billed_parser = sub.add_parser("billed", help="what the lab has cost, in whole hours")
    billed_parser.add_argument("--json", action="store_true")
    billed_parser.set_defaults(handler=command_billed)
    sub.add_parser("status").set_defaults(handler=command_status)
    sub.add_parser("hosts").set_defaults(handler=command_hosts)
    sub.add_parser("down").set_defaults(handler=command_down)
    watchdog = sub.add_parser("watchdog")
    watchdog.add_argument("--hours", type=float, default=6.0)
    watchdog.add_argument("--foreground", action="store_true")
    watchdog.add_argument("--deadline-unix", type=int, default=None,
                          help="when the lab must be gone (default --hours from now)")
    watchdog.set_defaults(handler=command_watchdog)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
