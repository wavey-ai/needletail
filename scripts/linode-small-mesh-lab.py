#!/usr/bin/env python3
"""Short-lived small-instance Linode lab for Needletail mesh qualification.

The lab is described by a topology JSON file (see
deploy/linode-small-mesh/topology.json). Every node is created with the
smallest plan that the topology names, a lab-only Cloud Firewall, and an
optional regional private VLAN interface for distributor-to-edge replication.

Cost controls are part of the tool, not an operator convention:

* `up` refuses a topology above the node and hourly-price ceilings, and
  records the ceilings in the lab state: `grow`, `add` and `replace` keep
  to them from any shell;
* `up` refuses without `--watchdog-hours` and `--budget-usd`. The budget
  holds the whole work: this lab with every slot of its topology for
  ceil(hours) billed hours each, what was spent before (`--spent-before-usd`,
  by default the sum of the ledger) and the other live labs to their own
  deadlines. `grow`, `add`, `replace --mode new` and a later `watchdog
  --hours` are held to the budget that `up` recorded;
* state records exact instance and firewall identifiers, and `down` also
  sweeps by the unique run tag, so only this lab is ever deleted;
* the watchdog is a systemd user unit (needletail-watchdog-<run>.service,
  Restart=always, enabled: with lingering it returns after a reboot) that
  deletes the lab by a deadline even when the operator session has ended.
  `up` arms it before the first instance is created, counted from the
  lab's creation: it starts `down` WATCHDOG_LEAD_S before the deadline and
  deletes in parallel, so no instance lives past the deadline and none
  bills an hour that the deadline does not. Nothing is created while the
  unit is not active (watchdog_problems), every command on a live lab
  starts a dead one again at the recorded deadline (watchdog_ensure), and
  `down` removes it. A unit whose lab state file is lost (cargo clean, a
  worktree removed) or unreadable does not stop: it asks the account for
  the run's tag, and deletes what carries it at its own deadline;
* every instance carries the tag needletail-deadline:<unix>, so another
  lab's budget prices this one to its deadline, not to now;
* the ledger (NEEDLETAIL_LINODE_LEDGER, one file for every worktree, beside
  the token file) holds what each finished lab cost: `down` appends its
  line, and `up` adds a line for a lab that another tool or checkout made
  and removed, from the account's events;
* every change of the state file is one read-change-write under flock of
  `<state>.lock`, so two commands never write each other's changes away;
* `down` deletes the run's VLANs too, which Linode keeps once empty, and
  takes its nodes' addresses out of the worktree's known_hosts (Linode
  gives a released address to a later instance);
* autoscale slots (topology nodes marked "slot") are never created by `up`
  or `grow`: `add <slot>` creates one under the same checks (dedicated plan,
  node and hourly ceilings, an armed and active watchdog at most
  MAX_LIFE_HOURS into the lab's life and at least ADD_CUTOFF_S ahead, the
  budget), and `remove <name>` deletes one by its recorded id, only when the
  live instance still carries the run's tag and label;
* `replace <name>` rebuilds an instance in place: the same instance id,
  public and VLAN address, tags and billing (no new billed hour), a new
  disk. `replace --mode new` deletes and creates it again on another host
  at the same VLAN address (a new public address and a new billed hour);
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
  transient errors are told apart from the API's message (refusal_kind);
* the run archive (scripts/run_archive.py, ~/needletail-data/<run id>/):
  when a run of the lab's id is in the archive, the operator's `down`
  refuses until that run is marked collected; `down --uncollected` deletes
  all the same and records the run as failed. The watchdog's own delete is
  never refused for data: at its deadline it first runs the collect
  command that `up` recorded in the state (the deploy tool's `collect
  --run-id <id>`) under a hard time bound (`--collect-timeout-s`, 4 min by
  default, ending when `down` starts), then deletes whatever happened; a
  collect that failed, timed out or left the run incomplete records the run
  as failed. `up` refuses a watchdog too short to hold that bound.

The Linode token is read from LINODE_TOKEN, LINODE_PAT, or the token file. It
is never written to state, logs, or generated output. The watchdog unit
names the token file, never the token: a lab is not created when that file
is missing, since a token in the environment alone would leave the unit
unable to delete anything.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_archive  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_BASE = "https://api.linode.com/v4"
DEFAULT_STATE = ROOT / "target" / "linode-qualification" / "small-mesh-lab.json"
DEFAULT_TOPOLOGY = ROOT / "deploy" / "linode-small-mesh" / "topology.json"
DEFAULT_TOKEN_FILE = ROOT.parent / "LINODE_PAT"
TOKEN_FILE = pathlib.Path(os.environ.get("NEEDLETAIL_LINODE_TOKEN_FILE",
                                         str(DEFAULT_TOKEN_FILE)))
# What every finished lab of the work cost, one JSON object a line, beside
# the token file (NEEDLETAIL_LINODE_TOKEN_FILE when it is set, so that a
# worktree at another depth finds the same file): outside git, one for
# every worktree.
LEDGER = pathlib.Path(os.environ.get(
    "NEEDLETAIL_LINODE_LEDGER",
    str(TOKEN_FILE.with_name("needletail-linode-ledger.jsonl"))))
# The worktree's ssh known_hosts (small_mesh_common.KNOWN_HOSTS).
KNOWN_HOSTS = DEFAULT_STATE.with_name("small-mesh-known_hosts")
WATCHDOG_UNIT_DIR = pathlib.Path("~/.config/systemd/user").expanduser()
# The ceilings of a new lab. `up` records the two in force in the lab
# state, and every later command keeps to those (lab_ceilings).
MAX_NODES = int(os.environ.get("NEEDLETAIL_LINODE_MAX_NODES", "40"))
MAX_HOURLY_USD = float(os.environ.get("NEEDLETAIL_LINODE_MAX_HOURLY_USD", "1.00"))
# A run id is part of every label and tag, of the watchdog unit's name and
# of the archive's file name.
RUN_ID = re.compile(r"[a-z0-9]{1,24}")
# Every instance carries its lab's deadline: another lab's budget prices
# this one to it (account_billed).
DEADLINE_TAG = "needletail-deadline:"
# The owner, 21 November 2026: "do not use shared instances". Every node of a
# lab is a dedicated-CPU plan. A shared plan (nanode, standard) is refused.
ALLOWED_CLASSES = {"dedicated"}
# The watchdog's deadline is when the lab must be gone, not when deleting
# starts: Linode bills every started hour, so the watchdog starts `down`
# this long before it (deleting in parallel), polling this often.
WATCHDOG_LEAD_S = 300
WATCHDOG_POLL_S = 5
# Before that `down`, the watchdog runs the lab's collect command (recorded
# in the state by `up`: the deploy tool's `collect --run-id <id>`) for at
# most this long (`up --collect-timeout-s`), started this long before
# `down` starts: it is killed when its time is up, and never delays the
# delete.
COLLECT_TIMEOUT_S = 240
DEPLOY_TOOL = ROOT / "scripts" / "linode-small-mesh-deploy.py"
# A watchdog whose lab state is gone asks the account for the run's
# instances this often until its deadline nears (watch_until_down).
WATCHDOG_ORPHAN_POLL_S = 60
# `add` creates nothing this close to the deadline: a create that retries
# for minutes must never land after `down` has listed the lab.
ADD_CUTOFF_S = 900
# `up` refuses a longer watchdog, and nothing is created in a lab whose
# watchdog is armed for more than this many hours of its life (a deadline
# moved later mid-run would bill more hours).
MAX_LIFE_HOURS = float(os.environ.get("NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS", "6.0"))
DELETE_WORKERS = 8
CREATE_WORKERS = 4
# A rebuild shuts the instance down before it rebuilds it: an instance that
# reads `offline` without having shown `rebuilding` is given this long to
# show it before the rebuild is taken as done.
REBUILD_SETTLE_S = 60.0
REBUILD_TIMEOUT_S = 900.0
EVENTS_PAGE = 200
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
    if not TOKEN_FILE.is_file():
        sys.exit("Linode token file is missing")
    value = TOKEN_FILE.read_text().strip()
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


class LabGone(SystemExit):
    """The lab on disk is no longer the one a command read: its state is
    gone (`down` finished), is another run's, or, for a change that records
    a new instance, `down` has begun."""


@contextlib.contextmanager
def state_lock(path: pathlib.Path):
    """flock on `<state>.lock`, held for one read-change-write of the state
    file and never across a Linode call: `down` must always get it soon.
    Never taken twice by one process (a second flock of the file by its
    holder would wait on itself)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def gone_reason(current: dict | None, run_id: str, creating: bool = False) -> str | None:
    """Why the state on disk (`current`) is no longer run `run_id`'s lab to
    change: gone, another run's, or (`creating`: the change records a new
    instance) `down` has begun. None when it may be changed."""
    if current is None:
        return "the lab state is gone (down has run)"
    if current.get("run_id") != run_id:
        return f"the lab state is now run {current.get('run_id')}'s"
    if creating and current.get("down_started_unix"):
        return "down has begun"
    return None


def change_state(path: pathlib.Path, state: dict, change, creating: bool = False) -> None:
    """One read-change-write of the lab state under its lock: read what is
    on disk now, apply `change(current)` to it, write it, and bring the
    caller's copy `state` up to it (the same dict, so every holder of it
    sees the lab as it is). A command so never writes back the copy it
    read minutes ago over what another command (the deploy's record, the
    autoscaler's slot, `down`'s mark) wrote meanwhile. Raises LabGone, and
    writes nothing, when the lab on disk is no longer this run's
    (gone_reason): a state file that `down` removed is never made again."""
    with state_lock(path):
        current = read_state(path)
        why = gone_reason(current, state.get("run_id"), creating)
        if why:
            raise LabGone(why)
        change(current)
        write_state(path, current)
    state.clear()
    state.update(current)


def lab_hourly(state: dict) -> float:
    return round(sum(node.get("hourly_usd", 0.0) for node in state["nodes"].values()), 4)


def lab_ceilings(state: dict | None = None) -> tuple[int, float]:
    """The node and hourly-price ceilings in force: those that `up`
    recorded in the lab state, whatever this shell's environment says (a
    `replace` from another shell is not refused at the default $1.00 an
    hour); for a new lab, the environment's (MAX_NODES, MAX_HOURLY_USD)."""
    state = state or {}
    return (int(state.get("max_nodes", MAX_NODES)),
            float(state.get("max_hourly_usd", MAX_HOURLY_USD)))


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


def validate_topology(api: Api, topology: dict,
                      ceilings: tuple[int, float] | None = None) -> tuple[dict, float]:
    """Exit unless every node of the topology (its autoscale slots too) is a
    dedicated plan in a region that is up, and the nodes that `up` creates
    (the slots left out) are within the node and hourly ceilings: the
    environment's for a new lab, `ceilings` (lab_ceilings: what `up`
    recorded) for a lab that exists. Returns the topology and the hourly
    price of what `up` creates."""
    max_nodes, max_hourly = ceilings or lab_ceilings()
    names = [node["name"] for node in topology["nodes"]]
    if len(set(names)) != len(names):
        sys.exit("topology node names must be unique")
    created = created_part(topology)
    nodes = created["nodes"]
    if not nodes or len(nodes) > max_nodes:
        sys.exit(f"topology has {len(nodes)} nodes to create; the ceiling is {max_nodes} "
                 "(NEEDLETAIL_LINODE_MAX_NODES)")
    types = {item["id"]: item for item in api.list_all("/linode/types")}
    regions = {item["id"]: item for item in api.list_all("/regions")}
    _all, problems = topology_cost(topology, types, regions)
    if problems:
        sys.exit("\n".join(problems))
    hourly = sum(node["_hourly"] for node in nodes)
    if hourly > max_hourly:
        sys.exit(
            f"topology costs ${hourly:.4f}/hour; the ceiling is ${max_hourly:.2f} "
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
    "error"}], "skipped": [{"name", "region", "type", "after"}]}.

    `down` racing the creates (the watchdog's, or an operator's): once it
    has begun or finished, no create starts, every instance that lands is
    deleted at once instead of recorded, and the command exits."""
    stop: list[dict] = []
    blocked: dict[tuple[str, str], dict] = {}
    lock = threading.Lock()
    out: dict = {"created": [], "refused": [], "skipped": []}
    gone: str | None = None
    orphans: list[dict] = []
    # Read once, before the workers start: record_node brings `state` up
    # to the file on disk (clear, then update) while they run.
    run_id, tag, image = state["run_id"], state["tag"], state["image"]
    stamped = {node["name"]: under_watchdog(state, node) for node in nodes}

    def one(node: dict) -> dict | None:
        with lock:
            cause = stop[0] if stop else blocked.get((node["region"], node["type"]))
            if cause:
                out["skipped"].append({"name": node["name"], "site": node["site"],
                                       "region": node["region"], "type": node["type"],
                                       "after": f"{cause['kind']} refusal of {cause['name']}"})
                return None
        try:
            return create_node(api, run_id, tag, image, key, stamped[node["name"]])
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
            if not gone:
                try:
                    record_node(state_path, state, created)
                except LabGone as error:
                    gone = str(error)
                    with lock:
                        stop.insert(0, {"kind": "down", "name": created["name"]})
            if gone:
                api.call("DELETE", f"/linode/instances/{created['id']}", allow_404=True)
                orphans.append(created)
                continue
            out["created"].append(created["name"])
            print(f"  created {created['name']:<18} {created['region']:<13} "
                  f"{created['type']:<14} {created['public_ipv4']}")
    for refusal in out["refused"]:
        print(f"  refused {refusal['name']:<18} {refusal['region']:<13} {refusal['type']:<14} "
              f"{refusal['kind']}: {refusal['error'][-200:]}", file=sys.stderr)
    if gone:
        sys.exit(f"up: {gone}; deleted the {len(orphans)} instance(s) that landed after it "
                 f"({', '.join(item['label'] for item in orphans)})")
    return out


def drop_site(api: "Api", state: dict, state_path: pathlib.Path, site: str, why: str) -> None:
    """Delete every node of an optional site that `up` created, by its
    recorded id, and go on without the site. Billing keeps each (a whole
    hour at least: state["removed"])."""
    dropped = [name for name, node in state["nodes"].items() if node["site"] == site]
    records = []
    for name in dropped:
        node = state["nodes"][name]
        api.call("DELETE", f"/linode/instances/{node['id']}", allow_404=True)
        records.append(removed_record(state, node, time.time()))

    def change(current: dict) -> None:
        for name in dropped:
            current["nodes"].pop(name, None)
        current.setdefault("removed", []).extend(records)
        if site not in current.setdefault("sites_left_out", []):
            current["sites_left_out"].append(site)
        current["hourly_usd"] = lab_hourly(current)

    change_state(state_path, state, change)
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


def node_interfaces(run_id: str, node: dict) -> list[dict]:
    """A node's two interfaces, as `up` creates them and a rebuild keeps
    them: public first, then the regional VLAN at the node's address."""
    interfaces = [{"purpose": "public"}]
    if node.get("vlan_ipv4"):
        interfaces.append({
            "purpose": "vlan",
            "label": f"ntsm-{run_id}-{node['region']}"[:64],
            "ipam_address": node["vlan_ipv4"],
        })
    return interfaces


def under_watchdog(state: dict, node: dict) -> dict:
    """`node` as create_node takes it: with the lab's deadline, which
    becomes the instance's needletail-deadline tag."""
    return {**node, "deadline_unix": state.get("watchdog_deadline_unix")}


def record_node(state_path: pathlib.Path, state: dict, created: dict) -> None:
    """Record a created instance in the lab state. LabGone when `down` has
    begun or run: the caller deletes the instance."""
    def change(current: dict) -> None:
        current["nodes"][created["name"]] = created

    change_state(state_path, state, change, creating=True)


def create_node(api: Api, run_id: str, tag: str, image: str, key: str,
                node: dict) -> dict:
    if not node.get("deadline_unix"):
        # No instance exists outside a watchdog: its deadline is a tag of
        # the instance from the request that creates it.
        raise RuntimeError(f"{node['name']}: the lab has no watchdog deadline; "
                           "no instance is created outside the watchdog")
    label = f"ntsm-{run_id}-{node['name']}{node.get('name_suffix', '')}"
    body = {
        "region": node["region"],
        "type": node["type"],
        "image": node.get("image", image),
        "label": label,
        "booted": True,
        "authorized_keys": [key],
        "interfaces": node_interfaces(run_id, node),
        "tags": [
            "needletail-qualification", tag,
            f"needletail-role:{node['role']}",
            f"needletail-site:{node['site']}",
            f"{DEADLINE_TAG}{int(node['deadline_unix'])}",
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
        # When this tool saw it created: billing counts from the API's
        # `created` and falls back to this.
        "created_unix": int(time.time()),
        **({"slot": True} if node.get("slot") else {}),
        **({"standby": True} if node.get("standby") else {}),
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
       hosts that exist. An optional site whose topology gives it listener
       hosts and that is left with none of them is deleted (its edges would
       carry no listener); a site planned without listener hosts is kept.
    A transient refusal (create_node's and Api.call's retries spent) stops
    nothing: the stage tries its node once more at its end (attempt), and
    only a second refusal counts (in the core, it deletes the lab). Every
    refusal is kept in `refusals`."""
    batches = getattr(args, "listener_batches", None) or LISTENER_BATCHES
    first, rest = split_optional(nodes, optional)
    core = [node for node in rest if not is_listener_host(node)]
    listeners = [node for node in nodes if is_listener_host(node)]

    def keep_refusals(items: list[dict]) -> None:
        change_state(state_path, state,
                     lambda current: current.setdefault("refusals", []).extend(items))

    def note(result: dict, stage: str) -> None:
        keep_refusals([{**item, "stage": stage} for item in result["refused"]])

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
        keep_refusals([{**item, "stage": stage, "retried": True} for item in transient
                       if item["name"] in {node["name"] for node in again}])
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
    for site, site_nodes in first.items():
        if site in state.get("sites_left_out", []):
            continue
        # The rule holds only where the topology gives the site listener
        # hosts: a site planned with none (a comparison lab's Sydney) has
        # none to wait for, and what was created for it is kept.
        if not any(is_listener_host(node) for node in site_nodes):
            continue
        if not any(node["site"] == site and is_listener_host(node)
                   for node in state["nodes"].values()):
            drop_site(api, state, state_path, site, "none of its listener hosts was created")
            missing = [item for item in missing if item["site"] != site]

    def staged(current: dict) -> None:
        current.setdefault("refusals", [])
        current["listeners_missing"] = missing
        current["hourly_usd"] = lab_hourly(current)

    change_state(state_path, state, staged)
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


@contextlib.contextmanager
def one_up_at_a_time():
    """flock on `<ledger>.up.lock` (one for every worktree, as the ledger),
    held for the whole of an `up`, or `up` is refused. Another lab's
    budget sees this one only once its instances exist: two `up`s at once
    would each count the other at nothing and could pass the budget
    together."""
    path = LEDGER.with_name(LEDGER.name + ".up.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"up refused: another up is creating a lab ({path} is held): its "
                     "instances are not yet there for this budget to count; run again once "
                     "it is done")
        yield


def command_up(args: argparse.Namespace) -> None:
    """Create a lab. Refused, before anything exists, without a watchdog
    and a budget: `--watchdog-hours` (above 0, at most MAX_LIFE_HOURS) and
    `--budget-usd`, which must hold every node created and every slot of
    the topology for ceil(hours) billed hours each, what the work spent
    before (`--spent-before-usd`, by default the ledger's sum: spent_before)
    and the other live labs to their own deadlines (account_billed). The
    budget, the spent figure and the two ceilings in force are recorded in
    the state for every later command. A run id whose tag a live instance
    already carries, or whose watchdog unit exists, is refused: two labs
    under one tag would be one lab to `down` and to every budget, and the
    second would take the first one's unit. One `up` runs at a time
    (one_up_at_a_time)."""
    with one_up_at_a_time():
        create_lab(args)


def create_lab(args: argparse.Namespace) -> None:
    """command_up, under its lock."""
    state_path = pathlib.Path(args.state)
    if state_path.exists():
        sys.exit("lab state already exists; run status or down first")
    hours = getattr(args, "watchdog_hours", None)
    budget = getattr(args, "budget_usd", None)
    if hours is None or budget is None:
        sys.exit("up refused: --watchdog-hours and --budget-usd are required: no lab is "
                 "created without a watchdog and a budget")
    if not 0 < hours <= MAX_LIFE_HOURS:
        sys.exit(f"up refused: --watchdog-hours {hours:g} must be above 0 and at most "
                 f"{MAX_LIFE_HOURS:g} (NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS)")
    collect_s = getattr(args, "collect_timeout_s", COLLECT_TIMEOUT_S)
    if not collect_s > 0:
        sys.exit(f"up refused: --collect-timeout-s {collect_s:g} must be above 0")
    if hours * 3600 <= WATCHDOG_LEAD_S + collect_s:
        sys.exit(f"up refused: --watchdog-hours {hours:g} cannot hold the collect: the watchdog "
                 f"collects for up to {collect_s:g} s (--collect-timeout-s) before it starts "
                 f"deleting {WATCHDOG_LEAD_S} s before the deadline, so the lab must live more "
                 f"than {(WATCHDOG_LEAD_S + collect_s) / 3600:.3f} h")
    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime("%m%d%H%M")
    if not RUN_ID.fullmatch(run_id):
        sys.exit(f"up refused: run id {run_id!r}: 1 to 24 of a-z and 0-9 (it names the "
                 "labels, the tags, the watchdog unit and the archive)")
    problems = watchdog_host_problems()
    if problems:
        sys.exit("up refused: " + "; ".join(problems))
    unit = watchdog_unit(run_id)
    if (WATCHDOG_UNIT_DIR / unit).exists() or watchdog_active(run_id):
        sys.exit(f"up refused: run id {run_id}: the watchdog unit {unit} exists or runs (another "
                 "lab's, in another worktree); give another --run-id")
    key_path = pathlib.Path(args.ssh_public_key).expanduser()
    if not key_path.is_file():
        sys.exit("SSH public key is missing")
    api = Api()
    topology = json.loads(pathlib.Path(args.topology).read_text())
    topology = without_sites(topology, (args.without_sites or "").split(","))
    topology, hourly = validate_topology(api, topology)
    optional = [site for site in (args.optional_sites or "").split(",") if site]
    tag = f"needletail-small-mesh-{run_id}"
    carried = [item for item in api.list_all("/linode/instances", tag)
               if tag in item.get("tags", [])]
    if carried:
        sys.exit(f"up refused: run id {run_id}: {len(carried)} live instance(s) already carry "
                 f"the tag {tag} ("
                 + ", ".join(str(item.get("label")) for item in carried[:4])
                 + ("..." if len(carried) > 4 else "") + "); give another --run-id")
    spent = spent_before(api, getattr(args, "spent_before_usd", None))
    whole_hours = math.ceil(hours - 1e-9)
    other = account_billed(api, tag)
    figures = {"lab_usd": round(sum(node["_hourly"] for node in topology["nodes"])
                                * whole_hours, 4),
               "billed_hours_each": whole_hours, "spent_before_usd": spent,
               "other_labs_usd": other["usd"], "budget_usd": budget}
    figures["total_usd"] = round(figures["lab_usd"] + spent + other["usd"], 4)
    if figures["total_usd"] > budget + 1e-9:
        sys.exit("up refused: over the budget (the lab is every node and every slot of the "
                 f"topology, {whole_hours} billed hour(s) each): {json.dumps(figures)}")
    image = topology.get("image", "linode/debian13")
    state = {
        "provider": "linode", "schema": "needletail.linode-small-mesh-lab.v1",
        "run_id": run_id, "tag": tag, "image": image,
        "topology_file": str(pathlib.Path(args.topology).resolve()),
        "created_unix": int(time.time()), "hourly_usd": round(hourly, 4),
        # What `up` was allowed: every later command (grow, add, replace,
        # watchdog --hours, the autoscaler) keeps to these from any shell.
        "budget_usd": budget, "spent_before_usd": spent,
        "max_nodes": MAX_NODES, "max_hourly_usd": MAX_HOURLY_USD,
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
        # What the watchdog runs before it deletes the lab (watchdog_collect).
        "collect": {"command": collect_command(run_id), "timeout_s": collect_s},
    }
    with state_lock(state_path):
        if state_path.exists():
            sys.exit("lab state already exists; run status or down first")
        write_state(state_path, state)
    try:
        # Armed before the first instance exists: a creation that hangs, or
        # an operator session that ends during it, still ends in `down`.
        # Counted from the lab's creation, so the lab's life is the hours
        # given, never a little more.
        watchdog_arm(state_path, state, int(state["created_unix"] + hours * 3600))
    except SystemExit:
        # Nothing exists yet: no state is left for a lab that never was.
        watchdog_disarm(run_id)
        state_path.unlink(missing_ok=True)
        raise
    topology = created_part(topology)
    print(f"Creating {len(topology['nodes'])} Linodes at ${hourly:.4f}/hour "
          f"(run {run_id}); budget ${budget:.2f}: this lab at most ${figures['lab_usd']:.2f} "
          f"({whole_hours} billed hour(s) each, slots included), ${spent:.2f} spent before, "
          f"${other['usd']:.2f} for the other live labs to their deadlines")
    if other["without_deadline"]:
        print(f"{other['without_deadline']} instance(s) of other labs carry no "
              f"{DEADLINE_TAG}<unix> tag (another tool made them): each priced to "
              f"{MAX_LIFE_HOURS:g} h from its creation", file=sys.stderr)
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
    change_state(state_path, state, lambda current: current.update(
        firewall_id=firewall["id"], operator_ipv4=operator))
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
    """Create nodes, then refresh the lab firewall so the new addresses are
    peers and attach it to them.

    Two things never leave a node behind. `down` racing the create: each
    new node is recorded by a read-change-write of the state on disk
    (record_node), and if `down` has begun (or finished) nothing is written
    and every node this call made is deleted at once. A firewall that
    cannot be set: the new nodes are deleted (never pushed or served with
    every port open)."""
    state_path = pathlib.Path(args.state)
    key = pathlib.Path(args.ssh_public_key).expanduser().read_text().strip()
    made: list[dict] = []
    gone: str | None = None
    failures: list[str] = []

    def record(created: dict | None = None) -> bool:
        """Record a new node (or, with none, the lab's hourly price) unless
        `down` has begun or finished (never recreate a state file that
        `down` removed, never erase its mark)."""
        nonlocal gone
        if gone:
            return False
        try:
            if created:
                record_node(state_path, state, created)
            else:
                change_state(state_path, state, lambda current: current.update(
                    hourly_usd=lab_hourly(current)), creating=True)
        except LabGone as error:
            gone = str(error)
            return False
        return True

    def give_up() -> None:
        for created in made:
            api.call("DELETE", f"/linode/instances/{created['id']}", allow_404=True)
        sys.exit(f"add: {gone}; deleted the {len(made)} instance(s) this call created "
                 f"({', '.join(item['label'] for item in made) or 'none'})")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(create_node, api, state["run_id"], state["tag"],
                        state["image"], key, under_watchdog(state, node)): node
            for node in nodes
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                created = future.result()
            except Exception as error:  # noqa: BLE001 - the others are still recorded
                failures.append(f"{futures[future]['name']}: {error}")
                continue
            made.append(created)
            if record(created):
                print(f"  created {created['name']:<18} {created['region']:<13} "
                      f"{created['type']:<14} {created['public_ipv4']}")
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
        gone = gone_reason(read_state(state_path), state["run_id"], creating=True)
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
    """Create every topology node that the running lab does not have yet,
    under the checks of `add`: the ceilings that `up` recorded, an armed
    and active watchdog at least ADD_CUTOFF_S ahead and within the lab's
    life (watchdog_problems), and the lab's budget (held_to_budget).
    Refused before anything is created or recorded."""
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    watchdog_ensure(state, state_path)
    api = Api()
    topology = json.loads(pathlib.Path(args.topology).read_text())
    # A site that the lab does without stays out: growing London's edges
    # must not create the Sydney that `up` left out.
    left_out = sorted(set(state.get("sites_left_out", []))
                      | {site for site in (args.without_sites or "").split(",") if site})
    known = {node["site"] for node in topology["nodes"]}
    topology = without_sites(topology, [site for site in left_out if site in known])
    topology, hourly = validate_topology(api, topology, lab_ceilings(state))
    slots = slot_specs(topology)
    # Autoscale slots are never grown: only `add` creates one.
    missing = [node for node in created_part(topology)["nodes"]
               if node["name"] not in state["nodes"]]
    if not missing:
        change_state(state_path, state, lambda current: current.update(slots=slots))
        print("lab already has every topology node (autoscale slots are created by add)")
        return
    problems = watchdog_problems(state, time.time(), MAX_LIFE_HOURS)
    if problems:
        sys.exit("grow refused: " + "; ".join(problems))
    held_to_budget("grow", api, state,
                   [{"name": node["name"], "hourly_usd": node["_hourly"]} for node in missing],
                   getattr(args, "budget_usd", None), getattr(args, "spent_before_usd", None))
    change_state(state_path, state, lambda current: current.update(
        slots=slots, topology_file=str(pathlib.Path(args.topology).resolve())))
    print(f"Adding {len(missing)} Linodes; the lab will cost ${hourly:.4f}/hour")
    try:
        add_nodes(args, api, state, missing)
    except RuntimeError as error:
        # Some were refused: those created are recorded (and behind the
        # firewall); the listener hosts still missing stay recorded.
        refresh_listeners_missing(state_path, str(error))
        sys.exit(f"grow: {error}")
    refresh_listeners_missing(state_path)
    command_status(args)


def refresh_listeners_missing(state_path: pathlib.Path, error: str = "") -> None:
    """After `grow`: the listener hosts that `up` recorded as missing and
    that the lab still does not have (a refusal of this grow, `error`,
    named on each that it did not create)."""
    state = read_state(state_path)
    if state is None or not state.get("listeners_missing"):
        return

    def change(current: dict) -> None:
        current["listeners_missing"] = [
            item if not error else {**item, "why": f"grow: {error}"[-300:]}
            for item in current.get("listeners_missing") or []
            if item["name"] not in current["nodes"]]

    change_state(state_path, state, change)


def owned_instance(api: Api, state: dict, name: str, verb: str) -> dict:
    """The live instance of lab node `name` ({} when it is gone already),
    after checking that it still carries the run's tag and the label this
    tool gave it: an id that now names anything else is never deleted or
    rebuilt. Exits (`verb` refused) otherwise."""
    node = state["nodes"][name]
    if not node.get("id"):
        sys.exit(f"{verb} refused: {name} has no recorded instance id")
    live = api.call("GET", f"/linode/instances/{node['id']}", allow_404=True)
    if live:
        if state["tag"] not in live.get("tags", []):
            sys.exit(f"{verb} refused: instance {node['id']} ({live.get('label')}) does not "
                     f"carry the run's tag {state['tag']}")
        if node.get("label") and live.get("label") != node["label"]:
            sys.exit(f"{verb} refused: instance {node['id']} is labelled "
                     f"{live.get('label')}, not {node['label']}")
    return live


def wait_rebuilt(api: Api, ident: int, name: str, started: dict) -> bool:
    """Wait for a rebuild with `booted: false` to end: the instance shows
    `rebuilding`, then `offline`. A rebuild shuts the instance down first,
    so `offline` alone is not the end: an instance that reads `offline`
    without having shown `rebuilding` (in the rebuild's own answer,
    `started`, or since) is given REBUILD_SETTLE_S to show it. An instance
    that shows `booting` or `running` after `rebuilding` was booted after
    all (the API ignored `booted: false`): that too is the end. Returns
    whether it booted."""
    seen = started.get("status") == "rebuilding"
    until = time.time() + REBUILD_TIMEOUT_S
    quiet_since: float | None = None
    while time.time() < until:
        status = api.call("GET", f"/linode/instances/{ident}").get("status")
        if status == "rebuilding":
            seen = True
        elif status in ("booting", "running") and seen:
            return True
        elif status == "offline":
            if seen:
                return False
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= REBUILD_SETTLE_S:
                return False
        if status != "offline":
            quiet_since = None
        time.sleep(5)
    sys.exit(f"replace: the rebuild of {name} (instance {ident}) did not end in "
             f"{REBUILD_TIMEOUT_S / 60:.0f} min; it is still the lab's, under its watchdog")


def shut_down(api: Api, state: dict, name: str) -> None:
    """Shut a running lab instance down and wait for `offline`."""
    ident = state["nodes"][name]["id"]
    wait_running(api, state, [name])
    api.call("POST", f"/linode/instances/{ident}/shutdown")
    until = time.time() + REBUILD_TIMEOUT_S
    while api.call("GET", f"/linode/instances/{ident}").get("status") != "offline":
        if time.time() >= until:
            sys.exit(f"replace: {name} (instance {ident}) did not shut down in "
                     f"{REBUILD_TIMEOUT_S / 60:.0f} min")
        time.sleep(5)


def rebuild_node(api: Api, state_path: pathlib.Path, state: dict, spec: dict,
                 key: str) -> None:
    """Rebuild a lab node's instance in place (POST .../rebuild): a new
    disk from the node's image, the same instance id, public address, tags
    and billing (no new billed hour), on the same physical host. The root
    password that the API asks for is random, used once in that request
    and never stored or printed: access is by the key. The rebuild is asked
    not to boot. Once it is done the config profile is read, and both
    interfaces (public, then the regional VLAN at the node's address) are
    put in place if the rebuild did not keep them; the public address and
    the lab firewall are checked; then the instance is booted, once. An
    instance that booted after its rebuild all the same is shut down
    before its interfaces are put in place, and booted once after; when
    its config holds both, it is left running."""
    name = spec["name"]
    node = state["nodes"][name]
    ident = node["id"]
    started = api.call("POST", f"/linode/instances/{ident}/rebuild", {
        "image": spec.get("image", state["image"]),
        "authorized_keys": [key],
        "root_pass": secrets.token_urlsafe(48),
        "booted": False,
    })
    booted = wait_rebuilt(api, ident, name, started)
    configs = api.list_all(f"/linode/instances/{ident}/configs")
    if not configs:
        sys.exit(f"replace: {name} (instance {ident}) has no config profile after its rebuild")
    config = configs[0]
    wanted = node_interfaces(state["run_id"], spec)

    def shape(interfaces: list[dict]) -> list[tuple]:
        return [(item.get("purpose"), item.get("label") or None,
                 item.get("ipam_address") or None) for item in interfaces]

    if shape(config.get("interfaces") or []) != shape(wanted):
        if booted:
            shut_down(api, state, name)
            booted = False
        api.call("PUT", f"/linode/instances/{ident}/configs/{config['id']}",
                 {"interfaces": wanted})
        print(f"  {name}: the rebuilt config did not hold both interfaces; put in place")
    live = api.call("GET", f"/linode/instances/{ident}")
    public = (live.get("ipv4") or [None])[0]
    if public != node["public_ipv4"]:
        sys.exit(f"replace: {name} (instance {ident}) now has the public address {public}, "
                 f"not the recorded {node['public_ipv4']}"
                 + ("" if booted else "; not booted"))
    if not firewall_attached(api, state, name):
        api.call("POST", f"/networking/firewalls/{state['firewall_id']}/devices",
                 {"id": ident, "type": "linode"})
        print(f"  {name}: the lab firewall was not attached; attached before the boot")
    if not booted:
        api.call("POST", f"/linode/instances/{ident}/boot", {"config_id": config["id"]})
    wait_running(api, state, [name])
    change_state(state_path, state, lambda current: current.setdefault("replaced", []).append(
        {"name": name, "id": ident, "unix": int(time.time()), "mode": "rebuild"}))
    print(f"  rebuilt {name} in place: instance {ident}, {node['public_ipv4']}, "
          f"VLAN {node.get('vlan_ipv4') or '-'}")


def command_replace(args: argparse.Namespace) -> None:
    """Give named nodes a new disk (`--mode rebuild`, the default: the
    instance, its id, both addresses, its tags and its billing continue;
    rebuild_node) or a new instance (`--mode new`: deleted by recorded id
    and created again at the same VLAN address with label suffix r<n>, on
    another physical host, with a new public address and a new billed
    hour: the cure for two forwarders that `hosts` found on one host).

    Before anything is touched: the topology is taken without the sites
    the lab does without and only the named nodes are priced (dedicated
    plans, regions up); each live instance must carry the run's tag and
    its label (owned_instance); the watchdog must be armed, active, at
    least ADD_CUTOFF_S ahead and within the lab's life; and `new` must
    keep the lab within the hourly ceiling that `up` recorded and the work
    within the budget with its new instances."""
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    key = pathlib.Path(args.ssh_public_key).expanduser().read_text().strip()
    watchdog_ensure(state, state_path)
    api = Api()
    topology = json.loads(pathlib.Path(state["topology_file"]).read_text())
    known = {node["site"] for node in topology["nodes"]}
    topology = without_sites(topology, [site for site in state.get("sites_left_out", [])
                                        if site in known])
    by_name = {node["name"]: node for node in topology["nodes"]}
    if len(set(args.names)) != len(args.names):
        sys.exit("replace refused: a node is named twice")
    specs = []
    for name in args.names:
        if name not in state["nodes"] or name not in by_name:
            sys.exit(f"replace refused: {name} is not a lab node")
        spec = dict(by_name[name])
        if spec.get("vlan_ipv4") and "/" not in spec["vlan_ipv4"]:
            spec["vlan_ipv4"] += "/24"
        recorded = state["nodes"][name].get("vlan_ipv4")
        if (spec.get("vlan_ipv4") or "").split("/")[0] != (recorded or ""):
            sys.exit(f"replace refused: the topology gives {name} the VLAN address "
                     f"{spec.get('vlan_ipv4')}, the lab made it at {recorded}")
        specs.append(spec)
    types = {item["id"]: item for item in api.list_all("/linode/types")}
    regions = {item["id"]: item for item in api.list_all("/regions")}
    _hourly, problems = topology_cost({"nodes": specs}, types, regions)
    if problems:
        sys.exit("replace refused: " + "; ".join(problems))
    live = {name: owned_instance(api, state, name, "replace") for name in args.names}
    problems = watchdog_problems(state, time.time(), MAX_LIFE_HOURS)
    if problems:
        sys.exit("replace refused: " + "; ".join(problems))
    if args.mode == "rebuild":
        absent = [name for name in args.names if not live[name]]
        if absent:
            sys.exit(f"replace refused: {', '.join(absent)}: the instance is gone, there is "
                     "nothing to rebuild (--mode new creates it again)")
        for spec in specs:
            rebuild_node(api, state_path, state, spec, key)
        return
    # The same nodes again: only the hourly ceiling can be passed (a plan
    # changed in the topology).
    _max_nodes, max_hourly = lab_ceilings(state)
    hourly = lab_hourly(state) + sum(spec["_hourly"] - state["nodes"][spec["name"]].get(
        "hourly_usd", 0.0) for spec in specs)
    if hourly > max_hourly + 1e-9:
        sys.exit(f"replace refused: the lab would cost ${hourly:.4f}/hour; the ceiling that up "
                 f"recorded is ${max_hourly:.2f}")
    # Priced as it will be: the named instances deleted now (billed to now,
    # not to the deadline) and their new ones kept to the deadline.
    now = time.time()
    after = {**state,
             "nodes": {name: node for name, node in state["nodes"].items()
                       if name not in args.names},
             "removed": list(state.get("removed", []))
             + [removed_record(state, state["nodes"][name], now) for name in args.names]}
    held_to_budget("replace", api, after,
                   [{"name": spec["name"], "hourly_usd": spec["_hourly"]} for spec in specs],
                   getattr(args, "budget_usd", None), getattr(args, "spent_before_usd", None))
    for name in args.names:
        old = state["nodes"][name]
        if live[name]:
            api.call("DELETE", f"/linode/instances/{old['id']}", allow_404=True)
        record = removed_record(state, old, time.time())

        def change(current: dict, name=name, old=old, record=record) -> None:
            current["nodes"].pop(name, None)
            current.setdefault("replaced", []).append(
                {"name": name, "id": old["id"], "unix": int(time.time()), "mode": "new"})
            current.setdefault("removed", []).append(record)
            current["hourly_usd"] = lab_hourly(current)

        change_state(state_path, state, change)
        print(f"  deleted {name} ({old['public_ipv4']})")
    # A deleted label can stay reserved briefly; make the new label unique.
    for spec in specs:
        spec["name_suffix"] = f"r{len(state['replaced'])}"
    add_nodes(args, api, state, specs)


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
    """What billing keeps of an instance deleted before `down` (and its
    address, which `down` takes out of known_hosts)."""
    return {"name": node["name"], "id": node["id"], "label": node.get("label"),
            "role": node.get("role"), "site": node.get("site"), "type": node.get("type"),
            "public_ipv4": node.get("public_ipv4"),
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


def tagged_deadline(tags: list[str]) -> int | None:
    """The lab deadline that an instance carries (needletail-deadline:<unix>)."""
    for tag in tags:
        if tag.startswith(DEADLINE_TAG) and tag[len(DEADLINE_TAG):].isdigit():
            return int(tag[len(DEADLINE_TAG):])
    return None


def plan_prices(api: "Api") -> dict:
    """{(plan, region): hourly price} from the API's plans, region prices
    included; (plan, None) is the plan's base price."""
    prices = {}
    for item in api.list_all("/linode/types"):
        prices[(item["id"], None)] = item["price"]["hourly"]
        for override in item.get("region_prices", []):
            prices[(item["id"], override["id"])] = override["hourly"]
    return prices


def instance_price(prices: dict, instance: dict) -> float:
    """The hourly price of a live instance: its plan's price in its region."""
    plan = instance.get("type")
    return prices.get((plan, instance.get("region")), prices.get((plan, None), 0.0))


def account_billed(api: "Api", run_tag: str | None, now: float | None = None) -> dict:
    """What the account's other live Needletail labs will have cost when
    their own watchdogs delete them: every live instance tagged
    needletail-qualification but not `run_tag`, ceil(hours) x its plan's
    price in its region from its creation to the deadline its
    needletail-deadline tag carries (to now once that has passed). An
    instance without the tag (made by another tool, the referee's among
    them, whose watchdog this tool cannot read) is priced to MAX_LIFE_HOURS
    from its creation, the longest life any watchdog here allows and the
    referee's default, never to now: a lab created half an hour ago bills
    the hours its watchdog gives it, not one (`without_deadline` counts
    them). The dev box has no tag and is never counted, never touched.
    Labs already deleted are not listed by the API: the ledger holds what
    they cost."""
    now = time.time() if now is None else now
    prices = plan_prices(api)
    rows = []
    for item in api.list_all("/linode/instances", "needletail-qualification"):
        tags = item.get("tags", [])
        if run_tag and run_tag in tags:
            continue
        start = created_unix({"created": item.get("created")}, now)
        deadline = tagged_deadline(tags)
        until = deadline if deadline is not None else start + MAX_LIFE_HOURS * 3600
        hours = billed_hours(start, max(now, until))
        rows.append({"label": item.get("label"), "type": item.get("type"), "hours": hours,
                     "usd": hours * instance_price(prices, item),
                     "deadline_unix": deadline})
    return {"usd": round(sum(row["usd"] for row in rows), 4), "instances": rows,
            "without_deadline": sum(1 for row in rows if row["deadline_unix"] is None)}


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


def budget_in_force(state: dict, budget_usd: float | None = None,
                    spent_before_usd: float | None = None) -> tuple[float | None, float]:
    """(budget, spent before) for a command on a live lab: what the command
    was given, else what `up` recorded in the state. Unless given, the
    spent figure is never less than the ledger's sum now: a lab that went
    down since `up` has left the other labs' live instances for the
    ledger."""
    budget = state.get("budget_usd") if budget_usd is None else budget_usd
    if spent_before_usd is None:
        spent_before_usd = max(float(state.get("spent_before_usd") or 0.0),
                               ledger_sum(read_ledger()))
    return budget, spent_before_usd


def held_to_budget(verb: str, api: "Api", state: dict, new: list[dict],
                   budget_usd: float | None = None,
                   spent_before_usd: float | None = None) -> None:
    """Exit (`verb` refused) unless this lab kept to its watchdog's
    deadline, with the instances `new` created now, stays within the
    budget in force (budget_in_force) together with what was spent before
    and the other live labs to their deadlines."""
    budget, spent = budget_in_force(state, budget_usd, spent_before_usd)
    if budget is None:
        sys.exit(f"{verb} refused: the lab state records no budget; give --budget-usd")
    ok, figures = budget_check(state, new, budget, spent,
                               account_billed(api, state["tag"])["usd"])
    if not ok:
        sys.exit(f"{verb} refused: over the budget: {json.dumps(figures)}")


# ------------------------------------------------------------------- ledger
#
# One JSON object a line: {"run_id", "tag", "worktree", "topology",
# "created_unix", "removed_unix", "billed_usd", "instance_hours", "source"}.
# The first line is what the work had cost before the ledger began, and
# until when: {"run_id": "before-23-nov", "billed_usd": 38.0, "removed_unix":
# N}, written by hand or by the first `up --spent-before-usd`. `down`
# appends its lab ("source": "down"); `up` adds the labs that the account's
# events show made and removed by another tool or checkout ("events").

def ledger_lines(text: str) -> list[dict]:
    lines = []
    for number, row in enumerate(text.splitlines(), 1):
        if not row.strip():
            continue
        try:
            line = json.loads(row)
        except ValueError:
            line = None
        if not isinstance(line, dict):
            sys.exit(f"ledger {LEDGER}: line {number} is not a JSON object")
        lines.append(line)
    return lines


def read_ledger() -> list[dict]:
    return ledger_lines(LEDGER.read_text()) if LEDGER.is_file() else []


def ledger_sum(lines: list[dict]) -> float:
    return round(sum(float(line.get("billed_usd") or 0.0) for line in lines), 4)


def ledger_append(lines: list[dict], unless=None) -> list[dict]:
    """Append lines to the ledger under flock (every worktree writes the
    one file), leaving out those that `unless(known lines, line)` finds
    there already. Returns the lines written."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        known = ledger_lines(handle.read())
        written = [line for line in lines if not (unless and unless(known, line))]
        for line in written:
            handle.write(json.dumps(line) + "\n")
    return written


def ledger_holds(known: list[dict], lab: dict) -> bool:
    """Whether the ledger has this lab already: a line of the same run id
    over the same time (a run id is free again once its lab is gone). The
    slack is for the clocks: a `down` line is stamped here, an events line
    by Linode."""
    slack = 600
    for line in known:
        if line.get("run_id") != lab["run_id"] or line.get("created_unix") is None:
            continue
        end = line.get("removed_unix") or line["created_unix"]
        if line["created_unix"] - slack <= lab["removed_unix"] \
                and lab["created_unix"] <= end + slack:
            return True
    return False


def ledger_has(known: list[dict], lab: dict) -> bool:
    """Whether the ledger has this very lab already: a line of its run id
    and its creation time (a lab state written back after `down` removed
    it is the same lab, and is not billed twice)."""
    return any(line.get("run_id") == lab["run_id"]
               and line.get("created_unix") == lab["created_unix"] for line in known)


def account_events(api: "Api", since: float) -> list[dict]:
    """The account's creates and deletes of lab instances (`ntsm-*` labels)
    after `since`, from GET /account/events: [{"action", "id", "label",
    "unix"}]. The API lists the newest first; when a page shows that order
    and is wholly older than `since` the paging stops, and when it does not
    every page is read."""
    found = []
    page = 1
    while True:
        response = api.call("GET", f"/account/events?page={page}&page_size={EVENTS_PAGE}")
        times = []
        for row in response.get("data", []):
            when = created_unix({"created": row.get("created")})
            if when is None:
                continue
            times.append(when)
            entity = row.get("entity") or {}
            if row.get("action") in ("linode_create", "linode_delete") \
                    and row.get("status") != "failed" and entity.get("type") == "linode" \
                    and entity.get("id") is not None \
                    and str(entity.get("label") or "").startswith("ntsm-") and when > since:
                found.append({"action": row["action"], "id": entity.get("id"),
                              "label": entity["label"], "unix": when})
        if page >= response.get("pages", 1):
            break
        if times and times[0] > times[-1] and times[0] <= since:
            break
        page += 1
    return found


def node_plans() -> dict[str, set[str]]:
    """{node name: every plan that a topology file of this checkout gives
    it}. An event names a deleted instance's label, not its plan: this is
    what it may have been."""
    plans: dict[str, set[str]] = {}
    for path in sorted((ROOT / "deploy").glob("*/topology*.json")):
        try:
            nodes = json.loads(path.read_text())["nodes"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for node in nodes:
            if isinstance(node, dict) and node.get("name") and node.get("type"):
                plans.setdefault(node["name"], set()).add(node["type"])
    return plans


def labs_from_events(events: list[dict], live_ids: set, since: float,
                     prices: dict[str, float]) -> list[dict]:
    """Ledger lines ("source": "events") for the labs that the account's
    events show made and removed: the instances of one run id (the label
    is ntsm-<run id>-<node>) that lived at overlapping times, none of them
    live now (a live lab is counted by account_billed, not the ledger).
    Each instance bills ceil(hours) from its create to its delete, a whole
    hour at least, at the dearest plan that any topology file of this
    checkout gives a node of its name (node_plans; the dearest plan of all
    for a name that none has): the figure is an upper estimate, since the
    events do not name the plan. An instance made before `since` is
    counted from `since`."""
    instances: dict = {}
    for event in sorted(events, key=lambda item: item["unix"]):
        item = instances.setdefault(event["id"], {"id": event["id"], "label": event["label"],
                                                  "created": None, "deleted": None})
        if event["action"] == "linode_delete":
            item["deleted"] = event["unix"]
        elif item["created"] is None:
            item["created"] = event["unix"]
    by_run: dict[str, list[dict]] = {}
    for item in instances.values():
        parts = item["label"].split("-", 2)
        if len(parts) < 3 or not parts[1]:
            continue
        item["node"] = parts[2]
        if item["created"] is None:
            item["created"] = since
        by_run.setdefault(parts[1], []).append(item)
    plans = node_plans()
    dearest = max((prices.get(plan, 0.0) for named in plans.values() for plan in named),
                  default=0.0)

    def price(node: str) -> float:
        named = plans.get(node) or plans.get(re.sub(r"[ar][0-9]+$", "", node)) or ()
        return max((prices.get(plan, 0.0) for plan in named), default=0.0) or dearest

    lines = []
    for run_id, items in sorted(by_run.items()):
        items.sort(key=lambda item: item["created"])
        labs: list[list[dict]] = []
        alive_until = float("-inf")
        for item in items:
            if item["created"] > alive_until:
                # Every earlier instance of the run id was gone: another lab.
                labs.append([])
            labs[-1].append(item)
            alive_until = max(alive_until, float("inf") if item["deleted"] is None
                              else item["deleted"])
        for lab in labs:
            if any(item["id"] in live_ids for item in lab):
                continue
            first = min(item["created"] for item in lab)
            last = max([item["deleted"] for item in lab if item["deleted"] is not None]
                       + [item["created"] for item in lab])
            hours = [billed_hours(item["created"], last if item["deleted"] is None
                                  else item["deleted"]) for item in lab]
            usd = sum(count * price(item["node"]) for count, item in zip(hours, lab))
            lines.append({"run_id": run_id, "tag": f"needletail-small-mesh-{run_id}",
                          "worktree": None, "topology": None, "created_unix": int(first),
                          "removed_unix": int(last), "billed_usd": round(usd, 4),
                          "instance_hours": sum(hours), "source": "events"})
    return lines


def reconcile_ledger(api: "Api") -> list[dict]:
    """Add to the ledger the labs that another tool or checkout made and
    removed since its first line (labs_from_events), so that what they cost
    counts against every later budget. Returns the lines added. Raises
    RuntimeError when the ledger has no first line with a time, or the
    events cannot be read."""
    lines = read_ledger()
    since = lines[0].get("removed_unix") if lines else None
    if since is None:
        raise RuntimeError(f"the first line of {LEDGER} has no removed_unix (until when its "
                           "figure counts): the account's events cannot be told from it")
    events = account_events(api, float(since))
    live_ids = {item.get("id") for item in api.list_all("/linode/instances")}
    prices = {item["id"]: item["price"]["hourly"] for item in api.list_all("/linode/types")}
    added = ledger_append(labs_from_events(events, live_ids, float(since), prices),
                          unless=ledger_holds)
    for line in added:
        print(f"ledger: lab {line['run_id']} was made and removed by another tool or "
              f"checkout: {line['instance_hours']} instance-hours, at most "
              f"${line['billed_usd']:.2f} by its node names (from the account's events)")
    return added


def spent_before(api: "Api", given: float | None) -> float:
    """What the work had cost before the lab that `up` is about to create:
    `given` (--spent-before-usd), else the sum of the ledger once it is
    reconciled with the account's events. With no ledger yet, `given`
    becomes its first line; without either, or when the ledger cannot be
    reconciled and no figure was given, `up` is refused: a budget is not
    checked against a guess of nothing spent."""
    if not read_ledger():
        if given is None:
            sys.exit(f"up refused: no ledger at {LEDGER} and no --spent-before-usd: say what "
                     "the work has cost so far (it becomes the ledger's first line)")
        now = time.time()
        ledger_append([{"run_id": time.strftime("before-%d-%b", time.gmtime(now)).lower(),
                        "billed_usd": given, "removed_unix": int(now), "source": "given"}],
                      unless=lambda known, line: bool(known))
        return given
    try:
        reconcile_ledger(api)
    except RuntimeError as error:
        if given is None:
            sys.exit(f"up refused: the ledger could not be reconciled ({error}); correct it, "
                     "or say what the work has cost so far with --spent-before-usd")
        print(f"ledger not reconciled ({error}); --spent-before-usd {given:g} is used as given",
              file=sys.stderr)
    return ledger_sum(read_ledger()) if given is None else given


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


def run_user(command: list[str]) -> subprocess.CompletedProcess:
    """One call of the user's systemd (systemctl --user, loginctl), from a
    login shell, tmux or a unit alike."""
    env = {**os.environ}
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, 127, "", str(error))


def watchdog_unit(run_id: str) -> str:
    return f"needletail-watchdog-{run_id}.service"


def watchdog_unit_text(state_path: pathlib.Path, run_id: str, deadline: int) -> str:
    """The watchdog's user unit: this tool's `watchdog --foreground` for the
    lab's state file, started again whenever it ends (Restart=always) and
    at boot (enabled; the user lingers). It names the token file, the
    ledger and the run archive's root that the arming shell used, never the
    token, and passes on the shell's ssh agent and lab key (the collect
    reaches the nodes by ssh)."""
    def word(text) -> str:
        # One quoted word of a unit file, with systemd's specifier
        # character doubled.
        text = str(text).replace("\\", "\\\\").replace('"', '\\"')
        return '"' + text.replace("%", "%%") + '"'

    command = [sys.executable, pathlib.Path(__file__).resolve(), "--state",
               state_path.resolve(), "watchdog", "--foreground", "--run-id", run_id,
               "--deadline-unix", int(deadline)]
    return "\n".join([
        "[Unit]",
        f"Description=Needletail lab {run_id}: delete it by its deadline",
        "",
        "[Service]",
        # A command line also expands $NAME: doubled, it is a plain dollar.
        "ExecStart=" + " ".join(word(item).replace("$", "$$") for item in command),
        "Environment=" + word(f"NEEDLETAIL_LINODE_TOKEN_FILE={TOKEN_FILE.resolve()}"),
        "Environment=" + word(f"NEEDLETAIL_LINODE_LEDGER={LEDGER.resolve()}"),
        "Environment=" + word(f"NEEDLETAIL_DATA_ROOT={run_archive.data_root()}"),
        *("Environment=" + word(f"{name}={os.environ[name]}")
          for name in ("SSH_AUTH_SOCK", "NEEDLETAIL_LINODE_SSH_KEY") if os.environ.get(name)),
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def watchdog_host_problems() -> list[str]:
    """Why this host cannot keep a watchdog unit: the unit reads the token
    from the token file (a token in this shell's environment never reaches
    it, and is never written into it), and it returns after a reboot and
    outlives the operator's session only when the user lingers."""
    problems = []
    if not TOKEN_FILE.is_file():
        problems.append(f"the watchdog unit reads the Linode token from {TOKEN_FILE} "
                        "(NEEDLETAIL_LINODE_TOKEN_FILE), which is missing: it could delete "
                        "nothing")
    linger = run_user(["loginctl", "show-user", str(os.getuid()), "--property=Linger",
                       "--value"])
    if linger.returncode != 0 or linger.stdout.strip() != "yes":
        problems.append("the user does not linger (loginctl enable-linger): the watchdog unit "
                        "would end with the session and not return after a reboot")
    return problems


def watchdog_active(run_id: str) -> bool:
    """Whether the lab's watchdog unit is running now."""
    result = run_user(["systemctl", "--user", "is-active", watchdog_unit(run_id)])
    return result.stdout.strip() == "active"


def watchdog_arm(state_path: pathlib.Path, state: dict, deadline: int) -> None:
    """Set the lab's deadline and start its watchdog unit at it (again, if
    it runs: the unit file carries the deadline). Exits when the host
    cannot keep the unit or it does not come up; the deadline is in the
    state by then, so nothing is created meanwhile (watchdog_problems)."""
    run_id = state["run_id"]
    unit = watchdog_unit(run_id)
    problems = watchdog_host_problems()
    if problems:
        sys.exit("watchdog: " + "; ".join(problems))
    change_state(state_path, state,
                 lambda current: current.update(watchdog_deadline_unix=int(deadline)))
    WATCHDOG_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (WATCHDOG_UNIT_DIR / unit).write_text(watchdog_unit_text(state_path, run_id, int(deadline)))
    for verb in (["daemon-reload"], ["enable", unit], ["restart", unit]):
        result = run_user(["systemctl", "--user", *verb])
        if result.returncode != 0:
            sys.exit(f"watchdog: systemctl --user {' '.join(verb)} failed: "
                     f"{(result.stderr or result.stdout).strip()[-300:]}")
    if not watchdog_active(run_id):
        sys.exit(f"watchdog: {unit} did not stay active (journalctl --user -u {unit})")


def watchdog_disarm(run_id: str) -> None:
    """Remove a lab's watchdog unit: disabled, its file gone, and stopped
    last, because the caller may be the unit's own process (the watchdog's
    `down`), which the stop ends."""
    unit = watchdog_unit(run_id)
    run_user(["systemctl", "--user", "disable", unit])
    (WATCHDOG_UNIT_DIR / unit).unlink(missing_ok=True)
    run_user(["systemctl", "--user", "daemon-reload"])
    run_user(["systemctl", "--user", "stop", unit])


def watchdog_ensure(state: dict, state_path: pathlib.Path) -> list[str]:
    """Start a dead watchdog again at the recorded deadline, and say so.
    Called by every lab command on a live lab, and by the roll, the
    replace, scale-run's poll and every controller tick: a lab whose
    watchdog was stopped or lost bills until someone looks. Returns what is
    still wrong (empty when the watchdog is armed and its unit active). A
    deadline that has passed is armed all the same: the unit runs `down`
    at once."""
    deadline = state.get("watchdog_deadline_unix")
    if not deadline:
        return ["the lab state has no watchdog deadline: arm it with `watchdog --hours`"]
    run_id = state["run_id"]
    if watchdog_active(run_id):
        return []
    print(f"watchdog: {watchdog_unit(run_id)} was not active; starting it again at the "
          f"recorded deadline ({(deadline - time.time()) / 60:.0f} min from now)",
          file=sys.stderr, flush=True)
    try:
        watchdog_arm(state_path, state, int(deadline))
    except SystemExit as error:
        return [str(error)]
    return []


def tag_deadline(api: "Api", state: dict, deadline: int) -> None:
    """Give every instance of the lab the tag of a moved deadline, in place
    of the one it carries: the other labs' budgets price this lab to it.
    An instance that no longer carries the run's tag is left alone."""
    def one(node: dict) -> None:
        live = api.call("GET", f"/linode/instances/{node['id']}", allow_404=True)
        if not live or state["tag"] not in live.get("tags", []):
            return
        tags = [tag for tag in live["tags"] if not tag.startswith(DEADLINE_TAG)]
        api.call("PUT", f"/linode/instances/{node['id']}",
                 {"tags": tags + [f"{DEADLINE_TAG}{int(deadline)}"]})

    with concurrent.futures.ThreadPoolExecutor(max_workers=DELETE_WORKERS) as pool:
        for _done in pool.map(one, list(state["nodes"].values())):
            pass


def watchdog_problems(state: dict, now: float, max_life_hours: float | None = None) -> list[str]:
    """Why no instance may be created in the lab now, from its watchdog:
    not armed; its unit not active (nothing would delete the lab at the
    deadline); its deadline less than ADD_CUTOFF_S ahead (a create that
    retries must never land after `down` has listed the lab); or armed for
    more than `max_life_hours` of the lab's life (a deadline moved later,
    by `watchdog --hours` mid-run, would bill hours that no budget
    counted). Empty when a create may go ahead."""
    deadline = state.get("watchdog_deadline_unix")
    if not deadline or deadline <= now:
        return ["the lab's watchdog is not armed (or its deadline has passed): "
                "no instance is created outside it"]
    problems = []
    if not watchdog_active(state["run_id"]):
        problems.append(f"the watchdog unit {watchdog_unit(state['run_id'])} is not active: "
                        "nothing would delete the lab at its deadline (`status` starts it "
                        "again)")
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
              budget_usd: float | None = None, spent_before_usd: float | None = None,
              other_labs_usd: float = 0.0,
              max_life_hours: float | None = None) -> list[str]:
    """Create autoscale slots of the lab, with every check that `up` makes
    and more: each name is a slot of this lab that it does not have, at a
    site that it does not leave out; every plan is dedicated and its region
    up (from the API); the lab stays within the node and hourly ceilings
    that `up` recorded (lab_ceilings); the watchdog is armed, its unit
    active, its deadline at least ADD_CUTOFF_S ahead and at most
    `max_life_hours` (default MAX_LIFE_HOURS) into the lab's life
    (watchdog_problems); and the projection to that deadline (budget_check)
    stays within the budget: `budget_usd` and `spent_before_usd`, or what
    `up` recorded (budget_in_force). A lab without a budget gets no slot.
    Exits on any refusal before anything is created. Returns the names
    created."""
    max_life_hours = MAX_LIFE_HOURS if max_life_hours is None else max_life_hours
    max_nodes, max_hourly = lab_ceilings(state)
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
    if count > max_nodes:
        sys.exit(f"add refused: the lab would have {count} nodes; the ceiling is {max_nodes} "
                 "(NEEDLETAIL_LINODE_MAX_NODES)")
    total = sum(node.get("hourly_usd", 0.0) for node in state["nodes"].values()) + hourly
    if total > max_hourly + 1e-9:
        sys.exit(f"add refused: the lab would cost ${total:.4f}/hour; the ceiling is "
                 f"${max_hourly:.2f} (NEEDLETAIL_LINODE_MAX_HOURLY_USD)")
    budget_usd, spent_before_usd = budget_in_force(state, budget_usd, spent_before_usd)
    if budget_usd is None:
        sys.exit("add refused: the lab state records no budget; give --budget-usd")
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
        plan.append((name, node, bool(owned_instance(api, state, name, "remove"))))
    for name, node, exists in plan:
        if exists:
            api.call("DELETE", f"/linode/instances/{node['id']}", allow_404=True)
        record = removed_record(state, node, time.time())

        def change(current: dict, name=name, record=record) -> None:
            current["nodes"].pop(name, None)
            current.setdefault("removed", []).append(record)
            current["hourly_usd"] = lab_hourly(current)

        change_state(state_path, state, change)
        print(f"  deleted {name} ({node.get('public_ipv4')})")
    if refresh_firewall and state.get("firewall_id") and state.get("operator_ipv4"):
        api.call("PUT", f"/networking/firewalls/{state['firewall_id']}/rules",
                 firewall_rules(state))
    return [name for name, _node, _exists in plan]


def command_add(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    watchdog_ensure(state, state_path)
    api = Api()
    add_slots(args, api, state, args.names, args.budget_usd, args.spent_before_usd,
              account_billed(api, state["tag"])["usd"], args.max_watchdog_hours)
    command_status(args)


def command_remove(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    watchdog_ensure(state, state_path)
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


def host_check(nodes: dict[str, dict]) -> dict:
    """The failure domains of the lab's nodes by physical host (`host_uuid`;
    D 2.10). Nodes that back each other up protect each other only on
    different hosts: one stall of a shared host stops both at once. A
    conflict: two distributors or backbones of a region on one host, two
    contributors on one host, or a region's edges (two or more) all on one
    host; and a node of such a pair whose host is not known (its pair
    cannot be judged). Edges that share a host while their region has
    another are listed apart ("shared_edges"); every node with no known
    host is listed ("unknown")."""
    regions: dict[str, dict[str, list[str]]] = {}
    roles: dict[str, dict[str, dict[str, list[str]]]] = {}
    members: dict[tuple[str, str], list[str]] = {}
    unknown = []
    for name in sorted(nodes):
        node = nodes[name]
        kind = {"backbone": "relay", "distributor": "relay"}.get(node["role"], node["role"])
        members.setdefault((kind, node["region"]), []).append(name)
        host = node.get("host_uuid")
        if not host:
            unknown.append(name)
            continue
        regions.setdefault(node["region"], {}).setdefault(host, []).append(name)
        roles.setdefault(kind, {}).setdefault(node["region"], {}).setdefault(host, []).append(name)
    conflicts, shared_edges = [], []
    for (kind, region), names in sorted(members.items()):
        blind = [name for name in names if name in unknown]
        if kind in ("relay", "contributor", "edge") and len(names) > 1 and blind:
            conflicts.append(f"HOST UNKNOWN in {region}: {', '.join(blind)}; their pair "
                             "cannot be judged")
    for kind, what in (("relay", "distributors and backbones"), ("contributor", "contributors")):
        for region, hosts in sorted(roles.get(kind, {}).items()):
            for host, names in sorted(hosts.items()):
                if len(names) > 1:
                    conflicts.append(f"SHARED HOST in {region}: {what} {', '.join(names)} on "
                                     f"{host[:12]}; replace all but one "
                                     "(`replace NAME --mode new`)")
    edge_regions = {}
    for name, node in nodes.items():
        if node["role"] == "edge":
            edge_regions.setdefault(node["region"], []).append(name)
    for region, hosts in sorted(roles.get("edge", {}).items()):
        edges = sorted(edge_regions[region])
        if len(edges) > 1 and len(hosts) < 2 and not set(edges) & set(unknown):
            conflicts.append(f"ONE HOST for the edges of {region}: {', '.join(edges)}; "
                             "replace all but one (`replace NAME --mode new`)")
            continue
        for host, names in sorted(hosts.items()):
            if len(names) > 1:
                shared_edges.append(f"SHARED HOST edges in {region}: {', '.join(names)} on "
                                    f"{host[:12]}")
    return {"regions": regions, "conflicts": conflicts, "shared_edges": shared_edges,
            "unknown": unknown}


def record_hosts(current: dict, found: dict[str, tuple[int, str | None]]) -> None:
    """Set `host_uuid` from `found` ({name: (instance id, host_uuid)}) on
    the nodes of `current` that are still the instance that was read; a
    node removed or replaced meanwhile is left as it is."""
    for name, (instance, host) in found.items():
        node = current["nodes"].get(name)
        if node is not None and node.get("id") == instance:
            node["host_uuid"] = host


def command_hosts(args: argparse.Namespace) -> None:
    """Record the physical host of every node (`host_uuid`, which the deploy
    renders as each av-mesh's --host-tag) and check the failure domains
    (host_check). Exits with status 1 on a conflict. Run after `up`, `add`,
    `grow` and `replace`, and before `push`.

    The GETs can take minutes, so the hosts are recorded on the state as it
    is on disk afterwards (record_hosts, through change_state under the
    state lock), never by writing back the copy read before them: what a
    push, a grow or the autoscaler wrote meanwhile is kept, and a lab that
    `down` removed is not made again."""
    path = pathlib.Path(args.state)
    state = read_state(path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    api = Api()
    found = {}
    for name in sorted(state["nodes"]):
        node = state["nodes"][name]
        live = api.call("GET", f"/linode/instances/{node['id']}", allow_404=True)
        found[name] = (node["id"], live.get("host_uuid"))
    try:
        # Under the state lock (C CT3), on the state as it is now.
        change_state(path, state, lambda current: record_hosts(current, found))
    except LabGone as gone:
        sys.exit(f"the lab was taken down while its hosts were read ({gone}); nothing recorded")
    check = host_check(state["nodes"])
    if args.json:
        print(json.dumps({**check, "nodes": {
            name: {"region": node["region"], "role": node["role"],
                   "host_uuid": node.get("host_uuid")}
            for name, node in sorted(state["nodes"].items())}}, indent=2, sort_keys=True))
    else:
        for region, hosts in sorted(check["regions"].items()):
            for host, names in sorted(hosts.items()):
                print(f"{region:<13} {host[:12]:<12} {', '.join(names)}")
        if check["unknown"]:
            print(f"host unknown: {', '.join(check['unknown'])}")
        for line in check["shared_edges"] + check["conflicts"]:
            print(line)
        if not check["conflicts"] and not check["unknown"]:
            print("every pair that backs each other up is on two hosts")
    if check["conflicts"]:
        sys.exit(1)


def command_status(args: argparse.Namespace) -> None:
    state_path = pathlib.Path(args.state)
    state = read_state(state_path)
    if state is None:
        print("No active Needletail small-mesh lab")
        return
    unarmed = watchdog_ensure(state, state_path)
    api = Api()
    age_hours = (time.time() - state["created_unix"]) / 3600
    print(f"run {state['run_id']}  nodes {len(state['nodes'])}  "
          f"${state['hourly_usd']:.4f}/hour  age {age_hours:.2f} h  "
          f"billed ${billed(state)['usd']:.2f} (whole hours, removed nodes included)")
    deadline = state.get("watchdog_deadline_unix")
    if deadline and not unarmed:
        print(f"watchdog: the lab is gone in {(deadline - time.time()) / 60:.0f} min "
              f"(down starts {WATCHDOG_LEAD_S // 60} min before); "
              f"{watchdog_unit(state['run_id'])} is active")
        command, bound = lab_collect(state)
        print(f"watchdog: collects for at most {bound:.0f} s before down: {' '.join(command)}")
    for problem in unarmed:
        print(f"NO WATCHDOG: {problem}")
    if state.get("budget_usd") is not None and deadline:
        print(f"budget ${state['budget_usd']:.2f}: ${state.get('spent_before_usd', 0.0):.2f} "
              f"spent before this lab, ${billed(state, until=deadline)['usd']:.2f} for it if "
              "kept to the watchdog")
    if state.get("listeners_missing"):
        print("listener hosts missing (refused at up; scale-run.py fits its steps to the rest): "
              + ", ".join(f"{item['name']} ({item['kind']})"
                          for item in state["listeners_missing"]))
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
    # Marked first: a create that lands from here on is deleted by the
    # command that made it rather than recorded (record_node).
    change_state(state_path, state,
                 lambda current: current.setdefault("down_started_unix", int(time.time())))
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
    with state_lock(state_path):
        # What other commands recorded while the lab went (a slot that the
        # autoscaler removed) is in the archive and the bill.
        current = read_state(state_path)
        if current is not None and current.get("run_id") == state["run_id"]:
            state = current
        state["removed_unix"] = int(time.time())
        total = billed(state)
        state["billed_usd"] = total["usd"]
        write_state(archive, state)
        state_path.unlink(missing_ok=True)
    ledger_append([{"run_id": state["run_id"], "tag": tag, "worktree": str(ROOT),
                    "topology": state.get("topology_file"),
                    "created_unix": state["created_unix"],
                    "removed_unix": state["removed_unix"], "billed_usd": total["usd"],
                    "instance_hours": total["instance_hours"], "source": "down"}],
                  unless=ledger_has)
    forget_known_hosts({item.get("public_ipv4") for item in
                        list(state["nodes"].values()) + state.get("removed", [])} - {None})
    print(f"Removed {len(instance_ids)} Linodes and {len(firewall_ids)} firewalls "
          f"after {age_hours:.2f} h (${total['usd']:.2f} of instance time billed in whole "
          f"hours, {total['instance_hours']} instance-hours; in the ledger {LEDGER})", flush=True)
    # Last: when the watchdog itself ran this `down`, stopping the unit ends
    # this process.
    watchdog_disarm(state["run_id"])


ARCHIVE_TOOL = "linode-small-mesh-lab.py"


def archived_run(run_id: str):
    """The run of the lab's id in the run archive (read only), or None when
    the archive has no such run."""
    try:
        return run_archive.RunArchive.open(run_id, tool=ARCHIVE_TOOL, record=False)
    except run_archive.ArchiveError:
        return None


def record_uncollected(run_id: str, why: str, create: bool = False) -> None:
    """Record the archive's run of this id as failed (its lab is deleted
    with its data not collected: notes-stage2, "keep all the data"). Nothing
    when it is collected or failed already, nor when the archive has no
    such run, unless `create` (the watchdog's collect failed): then the run
    is made, a linode-lab run of this tool, to say so. Never raises: no
    delete waits on the archive."""
    try:
        archive = archived_run(run_id)
        if archive is None and create:
            archive = run_archive.RunArchive.create(run_id, "linode-lab", tool=ARCHIVE_TOOL,
                                                    argv=[ARCHIVE_TOOL, "watchdog"])
        if archive is None:
            return
        manifest = archive.manifest
        if (manifest.get("collected") or {}).get("complete") or manifest.get("status") == "failed":
            return
        archive.finish("failed", why)
        print(f"run archive: run {run_id} recorded as failed ({why})", file=sys.stderr,
              flush=True)
    except Exception as error:  # noqa: BLE001 - the delete goes on whatever happens here
        print(f"run archive: run {run_id} could not be recorded as failed ({error!r})",
              file=sys.stderr, flush=True)


def command_down_operator(args: argparse.Namespace) -> None:
    """The `down` command: when the run archive holds a run of the lab's id,
    refused until that run is collected (run_archive.require_collected);
    `--uncollected` deletes all the same and records the run as failed. Only
    this command refuses: up's own clean-up of a refused core and the
    watchdog call command_down, never held for data."""
    state = read_state(pathlib.Path(args.state))
    if state is not None and archived_run(state["run_id"]) is not None:
        refusal = run_archive.require_collected(state["run_id"])
        if refusal:
            if not getattr(args, "uncollected", False):
                sys.exit(f"down {refusal} (`down --uncollected` deletes the lab all the same "
                         "and records the run as failed)")
            record_uncollected(state["run_id"], "the lab was deleted before its data was "
                                                "collected (down --uncollected)")
    command_down(args)


def watchdog_down(args: argparse.Namespace) -> None:
    """The watchdog's `down` at its deadline: never refused for data. A run
    of the lab's id in the archive that is not collected is recorded as
    failed first (the delete ends with the unit, and so with this
    process)."""
    state = read_state(pathlib.Path(args.state))
    if isinstance(state, dict) and state.get("run_id"):
        record_uncollected(state["run_id"], "the watchdog deleted the lab at its deadline "
                                            "before its data was collected")
    command_down(args)


def collect_command(run_id: str) -> list[str]:
    """The collect that `up` records for the watchdog: the deploy tool's
    `collect --run-id <id>` of this checkout."""
    return [sys.executable, str(DEPLOY_TOOL), "collect", "--run-id", run_id]


def lab_collect(state: dict) -> tuple[list[str], float]:
    """The lab's collect command and its time bound, as `up` recorded them
    (a state without them, or with a malformed one: the defaults). Never
    raises: the watchdog's loop reads it at every poll."""
    collect = state.get("collect") if isinstance(state.get("collect"), dict) else {}
    command = collect.get("command")
    if not (isinstance(command, list) and command
            and all(isinstance(item, str) for item in command)):
        command = collect_command(str(state.get("run_id")))
    try:
        bound = float(collect.get("timeout_s", COLLECT_TIMEOUT_S))
    except (TypeError, ValueError):
        bound = COLLECT_TIMEOUT_S
    return list(command), (bound if bound > 0 else float(COLLECT_TIMEOUT_S))


def run_collect(command: list[str], state_path: pathlib.Path, timeout_s: float) -> str | None:
    """Run the lab's collect command, the one call of the collector, by its
    command line: in a session of its own, for the lab state `state_path`
    (NEEDLETAIL_LINODE_LAB_STATE), without the Linode token in its
    environment, its output to the watchdog's. Killed, with every process it
    started, after `timeout_s`. None when it exited 0; else what went wrong
    (a collector that is missing, or a deploy tool without `collect`, is a
    failed collect, never an error of the watchdog)."""
    env = {name: value for name, value in os.environ.items()
           if name not in ("LINODE_TOKEN", "LINODE_PAT")}
    env["NEEDLETAIL_LINODE_LAB_STATE"] = str(state_path.resolve())
    try:
        process = subprocess.Popen(command, env=env, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
    except OSError as error:
        return f"it could not be started ({error})"
    try:
        code = process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return f"it did not end within {timeout_s:.0f} s and was killed"
    if code:
        return f"it exited with status {code}"
    return None


def watchdog_collect(state_path: pathlib.Path, state: dict, until: float, clock=None) -> None:
    """The watchdog's collect before its `down`: the lab's collect command
    (lab_collect), given what is left of its bound before `until` (when
    `down` starts) and killed then. A run that the archive holds collected
    already is not collected again. A collect that could not run in time,
    failed, or ended without the archive's run marked complete records the
    run as failed (made if the archive has none). Never raises and never
    runs past `until`: the delete goes ahead whatever happens here."""
    clock = clock or time.time
    run_id = state["run_id"]
    try:
        command, bound = lab_collect(state)
        archive = archived_run(run_id)
        if archive is not None and (archive.manifest.get("collected") or {}).get("complete"):
            print(f"watchdog: run {run_id} is collected already", flush=True)
            return
        left = min(bound, until - clock())
        if left <= 0:
            why = "no time was left for it before the delete"
        else:
            print(f"watchdog: collecting run {run_id} for at most {left:.0f} s before the "
                  f"delete: {' '.join(command)}", flush=True)
            why = run_collect(command, state_path, left)
        if why is None:
            archive = archived_run(run_id)
            collected = (archive.manifest.get("collected") or {}) if archive else {}
            if not collected.get("complete"):
                missing = collected.get("missing") or []
                why = ("it ended without marking the run collected"
                       + (f" (missing: {', '.join(map(str, missing))})" if missing else ""))
        if why is None:
            print(f"watchdog: run {run_id} is collected", flush=True)
            return
        print(f"watchdog: the collect of run {run_id} failed: {why}; deleting all the same",
              file=sys.stderr, flush=True)
        record_uncollected(run_id, f"the watchdog's collect before its delete failed: {why}",
                           create=True)
    except Exception as error:  # noqa: BLE001 - the delete goes on whatever happens here
        print(f"watchdog: the collect of run {run_id} failed ({error!r}); deleting all the same",
              file=sys.stderr, flush=True)
        record_uncollected(run_id, f"the watchdog's collect before its delete failed: {error!r}",
                           create=True)


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


def forget_known_hosts(addresses: set[str]) -> int:
    """Take a deleted lab's addresses out of the worktree's known_hosts
    (plain and hashed entries). ssh there accepts a new host's key once and
    refuses a changed one, and Linode gives a released address to a later
    instance, whose key is another. Returns the number of entries removed."""
    if not addresses or not KNOWN_HOSTS.is_file():
        return 0

    def names(field: str) -> bool:
        if field.startswith("|1|"):
            try:
                _, _, salt, digest = field.split("|")
                key, wanted = base64.b64decode(salt), base64.b64decode(digest)
            except ValueError:
                return False
            return any(hmac.compare_digest(
                hmac.new(key, address.encode(), hashlib.sha1).digest(), wanted)
                for address in addresses)
        return any(host in addresses or host.startswith("[")
                   and host[1:].split("]")[0] in addresses for host in field.split(","))

    kept = []
    removed = 0
    for line in KNOWN_HOSTS.read_text().splitlines():
        fields = line.split()
        if fields and fields[0].startswith("@"):
            fields = fields[1:]
        if fields and not fields[0].startswith("#") and names(fields[0]):
            removed += 1
        else:
            kept.append(line)
    if removed:
        temporary = KNOWN_HOSTS.with_name(KNOWN_HOSTS.name + ".next")
        temporary.write_text("".join(line + "\n" for line in kept))
        temporary.chmod(0o600)
        temporary.replace(KNOWN_HOSTS)
    return removed


def command_watchdog(args: argparse.Namespace) -> None:
    """`watchdog --hours H`: the lab must be gone H hours from now. The
    deadline is moved in the state, the unit is started again at it, and
    every instance gets its tag. A later deadline is refused when the lab
    kept to it passes the budget (--budget-usd and --spent-before-usd, or
    what `up` recorded).

    `watchdog --foreground --run-id R --deadline-unix D` is the unit's own
    form: it watches until no instance carries run R's tag
    (watch_until_down), then removes the unit (Restart=always would start
    it again to watch nothing)."""
    state_path = pathlib.Path(args.state)
    if args.foreground:
        if not args.run_id or not args.deadline_unix:
            sys.exit("watchdog --foreground is the unit's own form: it needs --run-id and "
                     "--deadline-unix")
        watch_until_down(args, state_path, args.run_id, int(args.deadline_unix))
        watchdog_disarm(args.run_id)
        return
    state = read_state(state_path)
    if state is None:
        sys.exit("No active Needletail small-mesh lab")
    if args.hours is None or args.hours <= 0:
        sys.exit("watchdog: give --hours (above 0): when, from now, the lab must be gone")
    deadline = int(time.time() + args.hours * 3600)
    api = Api()
    if deadline > (state.get("watchdog_deadline_unix") or 0):
        held_to_budget("watchdog", api, {**state, "watchdog_deadline_unix": deadline}, [],
                       args.budget_usd, args.spent_before_usd)
    watchdog_arm(state_path, state, deadline)
    try:
        tag_deadline(api, state, deadline)
    except RuntimeError as error:
        sys.exit("watchdog: the deadline is moved and the unit runs at it, but not every "
                 f"instance carries its tag ({error}): run the command again, or another "
                 "lab's budget prices this one to the old deadline")
    print(f"watchdog: the lab is gone by {args.hours:g} h from now (down starts "
          f"{WATCHDOG_LEAD_S // 60} min before); {watchdog_unit(state['run_id'])} is active "
          f"and every instance carries {DEADLINE_TAG}{deadline}")


def watch_until_down(args: argparse.Namespace, state_path: pathlib.Path, run_id: str,
                     deadline: int, clock=None, sleep=None, down=None, collect=None) -> None:
    """The watchdog unit's loop. Returns once no instance carries the run's
    tag; it never returns while one does.

    While the state file is the run's: every WATCHDOG_POLL_S, read the
    deadline from it (a later `watchdog --hours` moves it); from the lab's
    collect bound before `down` is due, run the collect once for that
    deadline (watchdog_collect, which ends by the time `down` is due, or at
    once with the run recorded failed when no time is left); from
    WATCHDOG_LEAD_S before the deadline run `down`; a `down` that fails is
    run again at the next poll. It writes nothing to the state, so a unit
    started again after a reboot keeps a moved deadline.

    When the state file is gone, unreadable or another run's (`cargo
    clean`, `rm -rf target`, a worktree removed, or `down` finished), that
    is not proof that the lab is gone: the account is asked for the
    instances with the run's tag. None: the loop returns. Some: they are
    watched to the unit's own deadline (`deadline`: watchdog_arm writes
    every moved deadline into the unit) and deleted by the tag from
    WATCHDOG_LEAD_S before it (down_by_tag, priced from the last state the
    loop read)."""
    clock = clock or time.time
    sleep = sleep or time.sleep
    down = down or watchdog_down
    collect = collect or watchdog_collect
    tag = f"needletail-small-mesh-{run_id}"
    last: dict | None = None
    collected_for: float | None = None
    api = None
    said = False
    while True:
        try:
            current = read_state(state_path)
            if current is not None and not isinstance(current, dict):
                raise ValueError("not a JSON object")
            why = None if current is None else f"another run's ({current.get('run_id')})"
        except (OSError, ValueError) as error:
            current, why = None, f"unreadable ({error})"
        if isinstance(current, dict) and current.get("run_id") == run_id:
            last = current
            due = current.get("watchdog_deadline_unix", deadline) - WATCHDOG_LEAD_S
            if collected_for != due and clock() >= due - lab_collect(current)[1]:
                collected_for = due
                collect(state_path, current, due, clock)
            if clock() >= due:
                try:
                    down(args)
                except (Exception, SystemExit) as error:  # noqa: BLE001 - try again
                    print(f"watchdog: down failed ({error}); trying again", file=sys.stderr,
                          flush=True)
            sleep(WATCHDOG_POLL_S)
            continue
        due = deadline - WATCHDOG_LEAD_S
        try:
            api = api or Api()
            if not api.list_all("/linode/instances", tag):
                return
            if not said:
                print(f"watchdog: the lab state {state_path} is {why or 'gone'}, and instances "
                      f"still carry {tag}: deleting them by the tag "
                      f"{max(0.0, due - clock()) / 60:.0f} min from now", file=sys.stderr,
                      flush=True)
                said = True
            if clock() >= due:
                down_by_tag(api, run_id, last)
        except (Exception, SystemExit) as error:  # noqa: BLE001 - try again
            print(f"watchdog: by the tag {tag}: {error}; trying again", file=sys.stderr,
                  flush=True)
        sleep(max(WATCHDOG_POLL_S, min(WATCHDOG_ORPHAN_POLL_S, due - clock())))


def down_by_tag(api: "Api", run_id: str, last: dict | None) -> None:
    """The watchdog's `down` for a lab whose state file it has lost: the
    lab is what carries the run's tag. Deletes every instance and firewall
    with the tag and the run's VLANs, sweeps the tag again, writes the
    ledger line ("source": "watchdog": from `last`, the last state the
    watchdog read, with any tagged instance it does not name; else from the
    instances found) and takes their addresses out of known_hosts. Raises
    when tagged instances remain."""
    tag = f"needletail-small-mesh-{run_id}"
    record_uncollected(run_id, "the watchdog deleted the lab by its tag at its deadline "
                               "(its state was lost) before its data was collected")
    prices = plan_prices(api)
    found = {item["id"]: item for item in api.list_all("/linode/instances", tag)}
    delete_instances(api, set(found))
    firewalls = api.list_all("/networking/firewalls", tag)
    for firewall in firewalls:
        api.call("DELETE", f"/networking/firewalls/{firewall['id']}", allow_404=True)
    if api.list_all("/linode/instances", tag):
        raise RuntimeError("tagged Linodes remain after their delete")
    left = delete_vlans(api, run_id)
    if left:
        print(f"VLANs still attached, not deleted: {', '.join(left)}", file=sys.stderr)
    late = {item["id"]: item for item in api.list_all("/linode/instances", tag)}
    if late:
        delete_instances(api, set(late))
        found.update(late)
        if api.list_all("/linode/instances", tag):
            raise RuntimeError("tagged Linodes remain after their delete")
    end = time.time()
    state = dict(last or {"nodes": {}, "removed": []})
    named = {node.get("id") for node in state.get("nodes", {}).values()}
    state["removed"] = list(state.get("removed", [])) + [
        {"name": item.get("label"), "hourly_usd": instance_price(prices, item),
         "created_unix": created_unix(item, end), "deleted_unix": end}
        for ident, item in sorted(found.items()) if ident not in named]
    total = billed(state, end)
    first = state.get("created_unix") or min(
        [created_unix(item, end) for item in found.values()] or [end])
    ledger_append([{"run_id": run_id, "tag": tag, "worktree": str(ROOT),
                    "topology": state.get("topology_file"), "created_unix": int(first),
                    "removed_unix": int(end), "billed_usd": total["usd"],
                    "instance_hours": total["instance_hours"], "source": "watchdog"}],
                  unless=ledger_has)
    addresses = {(item.get("ipv4") or [None])[0] for item in found.values()}
    addresses |= {item.get("public_ipv4") for item in
                  list(state.get("nodes", {}).values()) + state["removed"]}
    forget_known_hosts(addresses - {None})
    print(f"watchdog: deleted {len(found)} Linodes and {len(firewalls)} firewalls by the tag "
          f"{tag} (${total['usd']:.2f} billed, in the ledger {LEDGER})", flush=True)


def build_parser() -> argparse.ArgumentParser:
    """The lab tool's command line. Other tools that run it (the deploy's
    replace, add and remove; the autoscaler's rebuild) are tested against
    this parser, so a caller's arguments and the lab tool's cannot drift."""
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
    up.add_argument("--watchdog-hours", type=float, required=True,
                    help="the lab's life: the watchdog unit, armed before the first instance "
                         "is created, deletes the lab this many hours after its creation (at "
                         "most NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS or 6.0)")
    up.add_argument("--budget-usd", type=float, required=True,
                    help="refuse when every node and slot of the topology for the watchdog's "
                         "whole hours, --spent-before-usd and the other live labs to their "
                         "deadlines pass it; recorded for grow, add, replace and watchdog")
    up.add_argument("--collect-timeout-s", type=float, default=COLLECT_TIMEOUT_S,
                    help="at its deadline the watchdog runs the deploy tool's collect for at "
                         "most this long, ending when it starts deleting (5 min before the "
                         f"deadline); recorded in the state (default {COLLECT_TIMEOUT_S})")
    up.add_argument("--spent-before-usd", type=float, default=None,
                    help="what the work cost before this lab (default: the sum of the ledger, "
                         "NEEDLETAIL_LINODE_LEDGER, reconciled with the account's events); "
                         "with no ledger yet it becomes the ledger's first line")
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
    replace = sub.add_parser("replace", help="rebuild lab nodes in place, or make them anew")
    replace.add_argument("names", nargs="+")
    replace.add_argument("--mode", choices=("rebuild", "new"), default="rebuild",
                         help="rebuild: a new disk on the same instance (id, both addresses, "
                              "tags and billing kept, same physical host). new: delete and "
                              "create again at the same VLAN address (another host, a new "
                              "public address, a new billed hour)")
    replace.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    add = sub.add_parser("add", help="create autoscale slots of the lab (never anything else)")
    add.add_argument("names", nargs="+")
    add.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    for creating, what in ((grow, "the new nodes"), (replace, "the new instances (--mode new)"),
                           (add, "the new slots")):
        creating.add_argument("--budget-usd", type=float, default=None,
                              help="refuse when this lab to its watchdog's deadline, "
                                   f"{what}, the other live labs to their deadlines and "
                                   "--spent-before-usd pass it (default: the budget that up "
                                   "recorded)")
        creating.add_argument("--spent-before-usd", type=float, default=None,
                              help="what the work cost before this lab (default: what up "
                                   "recorded, or the ledger's sum now if that is more)")
    grow.set_defaults(handler=command_grow)
    replace.set_defaults(handler=command_replace)
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
    hosts = sub.add_parser("hosts", help="record each node's physical host; exit 1 when nodes "
                                         "that back each other up share one")
    hosts.add_argument("--json", action="store_true")
    hosts.set_defaults(handler=command_hosts)
    down = sub.add_parser("down", help="delete the lab; refused while the run archive holds "
                                       "this run uncollected")
    down.add_argument("--uncollected", action="store_true",
                      help="delete a lab whose run in the archive is not collected, and record "
                           "that run as failed")
    down.set_defaults(handler=command_down_operator)
    watchdog = sub.add_parser("watchdog", help="move the lab's deadline")
    watchdog.add_argument("--hours", type=float, default=None,
                          help="the lab must be gone this many hours from now")
    watchdog.add_argument("--budget-usd", type=float, default=None,
                          help="refuse a later deadline when the lab kept to it passes this "
                               "(default: the budget that up recorded)")
    watchdog.add_argument("--spent-before-usd", type=float, default=None,
                          help="what the work cost before this lab (default: what up recorded, "
                               "or the ledger's sum now if that is more)")
    watchdog.add_argument("--foreground", action="store_true",
                          help="the watchdog unit's own form, with --run-id and "
                               "--deadline-unix")
    watchdog.add_argument("--run-id", default=None)
    watchdog.add_argument("--deadline-unix", type=int, default=None)
    watchdog.set_defaults(handler=command_watchdog)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
