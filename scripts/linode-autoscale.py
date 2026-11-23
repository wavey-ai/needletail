#!/usr/bin/env python3
"""Autoscale the edges (and, with --forwarders, the forwarder slots) of the
large mesh from the mesh's own advertised load.

Where it runs (low-latency ops D1): a control-plane process on the operator
host, not in the av-mesh binary. It needs the Linode token, which never goes
onto a mesh node. It acts with no human in the loop, from what the mesh
advertises (`/api/assignment`: each cache's freshness, draining and
load_permille; each node's client UDP counters; `/api/mesh/local`: HLS
sessions). A record of a run says plainly that the controller ran on the
operator host. The in-mesh edge_lifecycle stays off (it can close an edge
that has UDP listeners).

Safety, before anything else:

* It only ever creates or deletes **autoscale slots of the recorded lab**
  (topology nodes marked "slot", recorded by `lab up`), and only through the
  lab tool's own functions (`add_slots`, `remove_nodes`): dedicated plans
  only, the run tag, the firewall, NEEDLETAIL_LINODE_MAX_NODES and
  _MAX_HOURLY_USD, the armed watchdog and `down` all apply. A delete goes by
  the recorded instance id and only when the live instance still carries
  the run's tag and label. The dev box (arch-us-east) and anything untagged
  are never touched: they are not slots of the lab, and a hard deny list
  names the dev box as well.
* Standby edges (created by `up`) are only started and stopped, never
  deleted. The contributor and a site's first two distributors are never
  touched.
* Before every create, a budget guard: this lab projected to its watchdog's
  deadline in whole billed hours, the new slots included, plus the other
  labs' live needletail instances and what earlier labs spent, within
  --budget-usd (required with the linode backend). It refuses when the
  watchdog is not armed, is less than 15 min ahead, or is set past
  --max-watchdog-hours (6.0) of the lab's life (the lab tool's
  watchdog_problems; `add` applies the same rule).
* Drain before delete: drain-node, wait until the node's UDP subscribers
  and HLS sessions are 0 (live gauges; an unknown gauge is not 0), and for
  a forwarder its lease children and cache-mesh children too (Phase AM
  item 12), stop its service, then delete. A drain that does not reach 0
  within --drain-timeout (60 s) is resumed (resume-node), recorded as a
  failure, and nothing is deleted.
* Stopped from outside (SIGINT or SIGTERM), it resumes every drain in
  flight before it exits. One controller per state file (a lock beside
  it); a second is refused.
* Nothing is pushed to a slot that the lab's Cloud Firewall does not
  cover (the lab tool deletes a slot whose firewall cannot be set).
* Hysteresis (scale up at a mean of 700 permille for 3 samples, one edge
  at 850 or more with the mean at 600 or more for 3 samples, or
  refusals in 2 samples; down only when the load with one edge fewer stays
  at or under 450 permille for 300 s), a cooldown per region (60 s), no
  add and remove within 5 min of each other in a region, at most 2 nodes
  per action, nothing else while a drain is in flight, no drain while
  edges join, and nodes still joining counted against the target.
* Every intent is written to the controller's state file before it is
  carried out, so a controller started again picks up where it stopped:
  a create is adopted (the lab tool adopts an instance by its label), a
  drain keeps its first deadline (past it: resume, never delete).
* Warm pool (referee R2): a drained slot is kept stopped while its billed
  hour is paid and taken back first when load rises; it is deleted
  --warm-release seconds before its next billed hour would start.
* --dry-run observes and prints every decision with its inputs, and makes
  no call that changes anything (and writes no state).

Backends: `linode` (the lab tool, the deploy tool's push/start/stop
--nodes, HTTPS to the nodes' public addresses) and `edge_lab` (the local
namespace lab, scripts/edge_lab.py: its slot nodes lon-edge-4 and
lon-dist-c), for proof without a Linode.

Every decision, with its inputs, and every action, goes to --events (JSON
lines, `unix_us`: Unix time in microseconds), the same file as the scale
run's phases (referee R3).

  linode-autoscale.py run --backend linode --budget-usd 100 \\
      --spent-before-usd 31.4 --events target/compare/scale-a1/events.jsonl
  linode-autoscale.py run --backend edge_lab --stream 2001 --dry-run --once
  linode-autoscale.py status
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import importlib.util
import json
import math
import os
import pathlib
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA = "needletail.autoscale.v1"
# Never touched, whatever a state file says.
DENY = {"arch-us-east"}
# The plans a slot may have, and their hourly price (GET /linode/types, 22
# Nov; the lab tool checks the class from the API again before it creates).
DEDICATED_PRICES = {"g6-dedicated-2": 0.054, "g6-dedicated-4": 0.108,
                    "g6-dedicated-8": 0.216}
HTTP_PORT = 19444


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def natural(name: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"([0-9]+)", name)]


@dataclasses.dataclass
class Config:
    poll_s: float = 2.0
    up_permille: int = 700
    # Wave 9 C3: one edge at or over this for up_samples with the mean at
    # or over target_permille also scales up (a ramp fills the edges
    # unevenly; at the mean's 700 one may be past 900 already).
    up_max_permille: int = 850
    up_samples: int = 3
    refusal_samples: int = 2
    target_permille: int = 600
    down_permille: int = 450
    down_hold_s: float = 300.0
    min_edges: int = 2
    max_per_action: int = 2
    cooldown_s: float = 60.0
    no_flap_s: float = 300.0
    drain_timeout_s: float = 60.0
    join_timeout_s: float = 120.0
    # A real create: create, wait for running, VLAN bootstrap (up to 900 s
    # in the deploy), binaries (critique 28: more than 5 minutes).
    create_timeout_s: float = 1800.0
    # Production: prefer a node near the end of its billed hour (the
    # victim's age). The test runs 0: each instance bills an hour anyway.
    min_age_s: float = 0.0
    warm_pool: bool = True
    warm_release_s: float = 300.0
    forwarders: bool = False
    lease_k: int = 12
    forwarder_cpu: float = 0.60
    budget_usd: float | None = None
    spent_before_usd: float = 0.0
    max_watchdog_hours: float = 6.0
    # Seconds between two "hold" decisions of a region written to the
    # events (every other decision is written when it is taken).
    hold_log_s: float = 30.0


class Refused(Exception):
    """A safety rule refused an action; nothing was done."""


# ----------------------------------------------------------------- events

class Events:
    """JSON lines with `unix_us` (Unix time in µs) and `source`, appended
    and flushed one by one, so that they line up with the per-second
    samplers and the listeners' arrivals (referee R3). Also printed."""

    def __init__(self, path: str | None, echo: bool = True, clock=time.time):
        self.path = pathlib.Path(path) if path else None
        self.echo = echo
        self.clock = clock
        self.lines: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind: str, **fields) -> dict:
        item = {"unix_us": int(self.clock() * 1_000_000), "source": "autoscaler", "kind": kind,
                **fields}
        self.lines.append(item)
        text = json.dumps(item, sort_keys=True)
        if self.path:
            with open(self.path, "a") as handle:
                handle.write(text + "\n")
        if self.echo:
            print(text, flush=True)
        return item


# ------------------------------------------------------------------ backends

class Backend:
    """What a backend gives the controller. `roster()`: {site: [node]} of
    every edge and distributor that the lab may serve with, each {"name",
    "role", "site", "created" (an instance exists), "held" (created and held
    out of service), "slot" (an autoscale slot: may be created and deleted),
    "first" (a site's first two distributors: never touched),
    "created_unix"}. `observe(roster)`: {name: {"advertised" (fresh),
    "draining", "load_permille", "udp_subscribers", "hls_sessions"
    (None: unknown), "refusals" (cumulative, None unknown),
    "lease_children", "cpu"}} of the nodes it reached."""

    name = "backend"

    def identity(self) -> dict:
        return {}

    def roster(self) -> dict[str, list[dict]]:
        raise NotImplementedError

    def observe(self, roster: dict[str, list[dict]]) -> dict[str, dict]:
        raise NotImplementedError

    def create(self, name: str) -> None:
        raise NotImplementedError

    def push(self, name: str) -> None:
        raise NotImplementedError

    def start(self, name: str) -> None:
        raise NotImplementedError

    def stop(self, name: str) -> None:
        raise NotImplementedError

    def drain(self, name: str) -> None:
        raise NotImplementedError

    def resume(self, name: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError

    def budget(self, new: list[str], offline: bool = False) -> tuple[bool, dict]:
        return True, {}


class Guarded(Backend):
    """Every call of the controller goes through here. It checks the safety
    rules against the backend's own roster before it lets a call through,
    and in a dry run lets no changing call through at all (it records it)."""

    def __init__(self, inner: Backend, config: Config, dry_run: bool = False):
        self.inner = inner
        self.config = config
        self.dry_run = dry_run
        self.name = inner.name
        self.calls: list[tuple[str, str]] = []
        self._roster: dict[str, dict] = {}

    def identity(self) -> dict:
        return self.inner.identity()

    def roster(self) -> dict[str, list[dict]]:
        roster = self.inner.roster()
        self._roster = {node["name"]: node for nodes in roster.values() for node in nodes}
        return roster

    def observe(self, roster):
        return self.inner.observe(roster)

    def node(self, name: str) -> dict:
        if name in DENY:
            raise Refused(f"{name} is never touched")
        node = self._roster.get(name)
        if node is None:
            raise Refused(f"{name} is not an edge or distributor of the lab's roster")
        if node.get("first") or node["role"] not in ("edge", "distributor"):
            raise Refused(f"{name} is a {node['role']} that the controller never touches")
        if node["role"] == "distributor" and not node.get("slot"):
            raise Refused(f"{name}: only a forwarder slot is ever started or stopped")
        return node

    def call(self, verb: str, name: str) -> None:
        self.calls.append((verb, name))
        if self.dry_run:
            return
        getattr(self.inner, verb)(name)

    def create(self, name: str) -> None:
        node = self.node(name)
        if not node.get("slot"):
            raise Refused(f"create {name}: not an autoscale slot of the lab")
        if node.get("created"):
            raise Refused(f"create {name}: it exists already")
        self.call("create", name)

    def push(self, name: str) -> None:
        node = self.node(name)
        if not node.get("created") and not self.dry_run:
            raise Refused(f"push {name}: not created")
        self.call("push", name)

    def start(self, name: str) -> None:
        node = self.node(name)
        if not node.get("created"):
            raise Refused(f"start {name}: not created")
        self.call("start", name)

    def stop(self, name: str) -> None:
        self.node(name)
        self.call("stop", name)

    def drain(self, name: str) -> None:
        self.node(name)
        self.call("drain", name)

    def resume(self, name: str) -> None:
        self.node(name)
        self.call("resume", name)

    def delete(self, name: str, proof: str | None = None) -> None:
        """Delete a slot. `proof` is why it may go: "drained-to-zero" (its
        gauges were seen at 0 after a drain and it was stopped), "warm" (a
        drained slot whose paid hour runs out) or "never-served" (a join
        that never advertised). Anything else is refused."""
        node = self.node(name)
        if not node.get("slot"):
            raise Refused(f"delete {name}: not an autoscale slot (standby edges are only "
                          "stopped)")
        if proof not in ("drained-to-zero", "warm", "never-served"):
            raise Refused(f"delete {name}: no proof that it holds no listener")
        if not node.get("created"):
            return
        self.call("delete", name)

    def budget(self, new):
        if self.dry_run:
            # A dry run reads no account: the projection of the lab alone.
            return self.inner.budget(new, offline=True)
        return self.inner.budget(new)


def http_json(address: str, path: str, timeout: float = 2.0):
    """GET https://<address>:19444<path>, certificate not checked (the lab's
    own CA); the JSON, or None."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(f"https://{address}:{HTTP_PORT}{path}", timeout=timeout,
                                    context=context) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001 - a node that does not answer is not advertised
        return None


def control_body(node_id: str) -> dict:
    """The body of a drain-node or resume-node request. It always names its
    node: av-mesh takes a request with no node_id and no region as global
    (control_targets_local), so an empty body could drain every node that
    hears it."""
    if not node_id:
        raise Refused("a control request must name its node")
    return {"node_id": node_id}


def control_accepted(status) -> bool:
    """av-mesh answers a control command (drain-node, resume-node) with 202
    Accepted: it is queued, not done. Any 2xx is taken; 200 as well, for a
    node of before."""
    return isinstance(status, int) and 200 <= status < 300


def http_post(address: str, path: str, body: dict, timeout: float = 5.0) -> int:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(f"https://{address}:{HTTP_PORT}{path}",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.status


def gauge(item, *names):
    """The first of `names` in a dict (nested "a.b" allowed) that is a
    number, else None."""
    for name in names:
        value = item
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def node_view(name: str, assignment: dict | None, local: dict | None) -> dict:
    """A node's own counters: from its /api/assignment, the client UDP
    block (`subscribers` is the live gauge of Phase AM item 8; the
    cumulative `subscriptions` is not one), and from its /api/mesh/local
    its edge service's HLS sessions and refused requests."""
    udp = (assignment or {}).get("client_udp") or {}
    edge = next((item for item in ((local or {}).get("edge_services") or [])
                 if item.get("node_id") == name), {})
    hls = gauge(edge, "active_playback_sessions", "sessions") if local else None
    refusals = None
    if assignment is not None or local is not None:
        refusals = (gauge(udp, "refusals") or 0) + (gauge(edge, "egress_rejected_requests") or 0)
    return {"udp_subscribers": gauge(udp, "subscribers", "active_subscribers"),
            "hls_sessions": hls, "refusals": refusals,
            "lease_children": gauge(local or {}, "fast_plane.lease_children", "lease_children",
                                    "relay.lease_children"),
            # The edges that fetch from this node over the cache mesh (a
            # forwarder's children): a live gauge once Phase AM item 12
            # lands; None (unknown, never 0) until then.
            "cache_children": gauge(local or {}, "cache_mesh.children", "cache_children",
                                    "relay.cache_children"),
            "answered": assignment is not None or local is not None}


def merge_views(roster: dict[str, list[dict]], entries: dict[str, dict | None],
                own: dict[str, dict]) -> dict[str, dict]:
    """{name: observation}: each region's advertisement (the "caches" of
    the first entry point that answered) and each node's own counters."""
    out = {}
    for site, nodes in roster.items():
        caches = {item.get("node_id"): item for item in
                  ((entries.get(site) or {}).get("caches") or [])}
        for node in nodes:
            cache = caches.get(node["name"]) or {}
            view = own.get(node["name"]) or {}
            out[node["name"]] = {
                "advertised": bool(cache.get("fresh")) and node.get("created", True),
                "draining": bool(cache.get("draining")),
                "overloaded": bool(cache.get("overloaded")),
                "load_permille": cache.get("load_permille"),
                "udp_subscribers": view.get("udp_subscribers"),
                "hls_sessions": view.get("hls_sessions"),
                "refusals": view.get("refusals"),
                "lease_children": view.get("lease_children"),
                "cache_children": view.get("cache_children"),
                "answered": view.get("answered", False)}
    return out


class LinodeBackend(Backend):
    """The Linode lab: the lab tool's state and functions (in-process), the
    deploy tool's push/start/stop --nodes (a subprocess each), HTTPS to the
    nodes' public addresses (the operator's address is open in the lab's
    firewall). The token is read by the lab tool's Api, here, on the
    operator host; it never reaches a node."""

    name = "linode"

    def __init__(self, state_path: str, stream: int, config: Config, sites: list[str] = (),
                 ssh_public_key: str = "~/.ssh/id_ed25519.pub"):
        self.lab = load_module("lab_for_autoscale", "linode-small-mesh-lab.py")
        self.state_path = pathlib.Path(state_path)
        self.stream = stream
        self.config = config
        self.sites = list(sites)
        self.ssh_public_key = ssh_public_key
        self._api = None

    def api(self):
        if self._api is None:
            self._api = self.lab.Api()
        return self._api

    def state(self) -> dict:
        state = self.lab.read_state(self.state_path)
        if state is None:
            raise Refused("no lab state")
        if not str(state.get("tag", "")).startswith("needletail-small-mesh-"):
            raise Refused(f"the lab state's tag {state.get('tag')!r} is not a lab's run tag")
        return state

    def identity(self) -> dict:
        state = self.state()
        return {"run_id": state["run_id"], "tag": state["tag"]}

    def held_out(self) -> set[str]:
        sys.path.insert(0, str(HERE))
        import small_mesh_common as common
        path = common.ARTIFACT_DIR / "settings.json"
        settings = json.loads(path.read_text()) if path.is_file() else {}
        return set(settings.get("standby_edges") or [])

    def roster(self) -> dict[str, list[dict]]:
        state = self.state()
        slots = self.lab.lab_slots(state)
        held = self.held_out()
        out: dict[str, list[dict]] = {}
        names = sorted(set(state["nodes"]) | set(slots), key=natural)
        firsts = {}
        for name in names:
            node = state["nodes"].get(name) or slots[name]
            if node["role"] == "distributor" and name not in slots:
                firsts.setdefault(node["site"], []).append(name)
        for name in names:
            node = state["nodes"].get(name) or slots[name]
            if node["role"] not in ("edge", "distributor") or name in DENY:
                continue
            if self.sites and node["site"] not in self.sites:
                continue
            created = name in state["nodes"]
            out.setdefault(node["site"], []).append({
                "name": name, "role": node["role"], "site": node["site"],
                "created": created, "held": created and name in held,
                "slot": name in slots,
                "first": name in firsts.get(node["site"], [])[:2],
                "created_unix": self.lab.created_unix(node, state.get("created_unix"))
                if created else None,
                "hourly_usd": node.get("hourly_usd", 0.054),
                "public_ipv4": node.get("public_ipv4")})
        return out

    def observe(self, roster):
        entries: dict[str, dict | None] = {}
        own: dict[str, dict] = {}
        lock = threading.Lock()

        def entry(site: str, nodes: list[dict]) -> None:
            for node in nodes:
                if node.get("created") and not node.get("held") and node.get("public_ipv4"):
                    found = http_json(node["public_ipv4"], f"/api/assignment?stream={self.stream}")
                    if isinstance(found, dict) and found.get("caches") is not None:
                        with lock:
                            entries[site] = found
                        return

        def one(node: dict) -> None:
            if not node.get("created") or not node.get("public_ipv4"):
                return
            assignment = http_json(node["public_ipv4"], f"/api/assignment?stream={self.stream}")
            local = http_json(node["public_ipv4"], "/api/mesh/local")
            with lock:
                own[node["name"]] = node_view(node["name"], assignment if isinstance(
                    assignment, dict) else None, local if isinstance(local, dict) else None)

        threads = [threading.Thread(target=entry, args=(site, nodes))
                   for site, nodes in roster.items()]
        threads += [threading.Thread(target=one, args=(node,))
                    for nodes in roster.values() for node in nodes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return merge_views(roster, entries, own)

    def deploy(self, verb: str, name: str) -> None:
        command = [sys.executable, str(HERE / "linode-small-mesh-deploy.py"), verb,
                   "--nodes", name]
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800,
                                env={**os.environ,
                                     "NEEDLETAIL_LINODE_LAB_STATE": str(self.state_path)})
        if result.returncode != 0:
            raise RuntimeError(f"deploy {verb} --nodes {name}: {result.stderr[-1500:]}")

    def public(self, name: str) -> str:
        node = self.state()["nodes"].get(name)
        if not node or not node.get("public_ipv4"):
            raise Refused(f"{name} has no address in the lab state")
        return node["public_ipv4"]

    def create(self, name: str) -> None:
        state = self.state()
        ok, figures = self.budget([name])
        if not ok:
            raise Refused(f"budget: {json.dumps(figures)}")
        args = argparse.Namespace(state=str(self.state_path),
                                  ssh_public_key=self.ssh_public_key)
        other = self.lab.account_billed(self.api(), state["tag"])["usd"]
        try:
            self.lab.add_slots(args, self.api(), state, [name], self.config.budget_usd,
                               self.config.spent_before_usd, other,
                               self.config.max_watchdog_hours)
        except SystemExit as error:
            raise Refused(str(error)) from None

    def push(self, name: str) -> None:
        # Never push (and so start) a node that the lab's Cloud Firewall
        # does not cover: it would serve with every port open.
        if not self.lab.firewall_attached(self.api(), self.state(), name):
            raise Refused(f"{name} is not attached to the lab's Cloud Firewall: not pushed")
        self.deploy("push", name)

    def start(self, name: str) -> None:
        self.deploy("start", name)

    def stop(self, name: str) -> None:
        self.deploy("stop", name)

    def drain(self, name: str) -> None:
        http_post(self.public(name), "/api/control/drain-node", control_body(name))

    def resume(self, name: str) -> None:
        http_post(self.public(name), "/api/control/resume-node", control_body(name))

    def delete(self, name: str) -> None:
        state = self.state()
        try:
            self.lab.remove_nodes(self.api(), self.state_path, state, [name], slots_only=True)
        except SystemExit as error:
            raise Refused(str(error)) from None

    def budget(self, new: list[str], offline: bool = False) -> tuple[bool, dict]:
        """The lab tool's budget_check, with the watchdog rule of D7: the
        watchdog must be armed, and at most max_watchdog_hours after the
        lab was created. `offline` (a dry run): the other labs' live
        instances are not read from the account."""
        state = self.state()
        if self.config.budget_usd is None:
            return False, {"why": "no --budget-usd"}
        # The lab tool's own rule (add_slots applies it again): armed, at
        # least 15 min ahead, at most max_watchdog_hours into the lab's life.
        problems = self.lab.watchdog_problems(state, time.time(), self.config.max_watchdog_hours)
        if problems:
            return False, {"why": "; ".join(problems)}
        slots = self.lab.lab_slots(state)
        extra = []
        for name in new:
            kind = slots.get(name, {}).get("type")
            if kind not in DEDICATED_PRICES:
                return False, {"why": f"{name}: {kind} is not a dedicated plan the controller "
                                      "creates"}
            extra.append({"name": name, "hourly_usd": DEDICATED_PRICES[kind]})
        other = 0.0 if offline else self.lab.account_billed(self.api(), state["tag"])["usd"]
        ok, figures = self.lab.budget_check(state, extra, self.config.budget_usd,
                                            self.config.spent_before_usd, other)
        if offline:
            figures["other_labs"] = "not read (dry run)"
        return ok, figures


class EdgeLabBackend(Backend):
    """The local namespace lab (scripts/edge_lab.py) as a backend: its slot
    nodes (edge_lab.slot_nodes(): lon-dist-c, and lon-edge-4 with `up
    slots=lon-edge-4`) stand for slots
    that are not created until the controller "creates" one (a marker in the
    lab's state directory; nothing to buy), `push` and `start` start the
    node's service in its namespace, `stop` ends it, `delete` ends it and
    clears the marker. Observation is the same HTTP API, from the lab's
    namespace. Billing is nothing."""

    name = "edge_lab"

    def __init__(self, stream: int, sites: list[str] = (), lab=None):
        if lab is None:
            sys.path.insert(0, str(HERE))
            import edge_lab as lab  # noqa: PLC0415
        self.edge_lab = lab
        self.stream = stream
        self.sites = list(sites)

    def created(self) -> set[str]:
        return set(self.edge_lab.load("slots-created.json", []) or [])

    def roster(self):
        lab = self.edge_lab
        slots = set(lab.slot_nodes() if hasattr(lab, "slot_nodes")
                    else getattr(lab, "SLOT_NODES", ()))
        made = self.created()
        out: dict[str, list[dict]] = {}
        firsts: dict[str, list[str]] = {}
        for name, role, site, *_ in lab.NODES:
            if role == "distributor" and name not in slots:
                firsts.setdefault(site, []).append(name)
        for name, role, site, *_ in lab.NODES:
            if role not in ("edge", "distributor") or (self.sites and site not in self.sites):
                continue
            created = name not in slots or name in made
            running = lab.service_pid(name) is not None
            out.setdefault(site, []).append({
                "name": name, "role": role, "site": site, "created": created,
                "held": created and not running, "slot": name in slots,
                "first": name in firsts.get(site, [])[:2], "created_unix": None,
                "hourly_usd": 0.0})
        return out

    def observe(self, roster):
        lab = self.edge_lab
        entries, own = {}, {}
        for site, nodes in roster.items():
            for node in nodes:
                if lab.service_pid(node["name"]) is None:
                    continue
                status, found = lab.http_json(node["name"], f"/api/assignment?stream={self.stream}")
                if site not in entries and status == 200 and isinstance(found, dict) \
                        and found.get("caches") is not None:
                    entries[site] = found
                local_status, local = lab.http_json(node["name"], "/api/mesh/local")
                own[node["name"]] = node_view(
                    node["name"], found if status == 200 and isinstance(found, dict) else None,
                    local if local_status == 200 and isinstance(local, dict) else None)
        return merge_views(roster, entries, own)

    def create(self, name):
        made = self.created() | {name}
        self.edge_lab.save("slots-created.json", sorted(made))

    def push(self, name):
        self.start(name)

    def start(self, name):
        if self.edge_lab.service_pid(name) is None:
            self.edge_lab.start_service(name)

    def stop(self, name):
        self.edge_lab.terminate_service(name)

    def drain(self, name):
        status, _ = self.edge_lab.http_json(name, "/api/control/drain-node", method="POST",
                                            body=control_body(name))
        if not control_accepted(status):
            raise RuntimeError(f"drain {name}: HTTP {status}")

    def resume(self, name):
        status, _ = self.edge_lab.http_json(name, "/api/control/resume-node", method="POST",
                                            body=control_body(name))
        if not control_accepted(status):
            raise RuntimeError(f"resume {name}: HTTP {status}")

    def delete(self, name):
        self.edge_lab.terminate_service(name)
        self.edge_lab.save("slots-created.json", sorted(self.created() - {name}))


# ---------------------------------------------------------------- deciding

def decide_region(site: str, nodes: list[dict], obs: dict[str, dict], actions: dict,
                  region: dict, warm: dict, config: Config, now: float) -> dict:
    """One region's decision from its observation: {"action": "join" |
    "drain" | "hold" | "wait", "targets": [(name, how)], "reason",
    "inputs"}. Updates the region's streaks in `region`. Pure apart from
    that: no backend call."""
    edges = [node for node in nodes if node["role"] == "edge"]
    in_flight = {name: action for name, action in actions.items() if action["site"] == site}
    joining = sorted(name for name, action in in_flight.items() if action["kind"] == "join")
    draining = sorted(name for name, action in in_flight.items() if action["kind"] == "drain")
    serving = [node for node in edges
               if obs.get(node["name"], {}).get("advertised")
               and not obs[node["name"]].get("draining")
               and node["name"] not in in_flight and not node.get("held")]
    loads = {node["name"]: int(obs[node["name"]].get("load_permille") or 0) for node in serving}
    count = len(serving)
    total = sum(loads.values())
    mean = total / count if count else None
    last = region.setdefault("last_refusals", {})
    refused = {}
    for node in serving:
        now_value = obs[node["name"]].get("refusals")
        if now_value is None:
            continue
        before = last.get(node["name"])
        if before is not None and now_value > before:
            refused[node["name"]] = now_value - before
        last[node["name"]] = now_value
    high = mean is not None and mean >= config.up_permille
    region["high_streak"] = region.get("high_streak", 0) + 1 if high else 0
    hottest = max(loads.values(), default=None)
    hot = (hottest is not None and config.up_max_permille > 0
           and hottest >= config.up_max_permille
           and mean is not None and mean >= config.target_permille)
    region["hot_streak"] = region.get("hot_streak", 0) + 1 if hot else 0
    region["refusal_streak"] = region.get("refusal_streak", 0) + 1 if refused else 0
    projected = total / (count - 1) if count > 1 else None
    low = (count > config.min_edges and projected is not None
           and projected <= config.down_permille and not refused)
    if low:
        region.setdefault("low_since_unix", now)
    else:
        region.pop("low_since_unix", None)
    inputs = {"serving": sorted(loads, key=natural), "load_permille": loads,
              "sum_permille": total, "mean_permille": None if mean is None else round(mean, 1),
              "projected_one_fewer": None if projected is None else round(projected, 1),
              "refusals": refused, "joining": joining, "draining": draining,
              "high_streak": region["high_streak"], "hot_streak": region["hot_streak"],
              "max_permille": hottest, "refusal_streak": region["refusal_streak"],
              "low_for_s": round(now - region["low_since_unix"], 1) if low else 0.0}

    def result(action: str, reason: str, targets=()) -> dict:
        return {"site": site, "action": action, "reason": reason, "targets": list(targets),
                "inputs": inputs}

    if draining:
        return result("wait", "a drain of this region is in flight")
    since_action = now - region.get("last_action_unix", -math.inf)
    trigger_up = (region["high_streak"] >= config.up_samples
                  or region["hot_streak"] >= config.up_samples
                  or region["refusal_streak"] >= config.refusal_samples)
    if trigger_up:
        desired = math.ceil(total / config.target_permille) if count else config.min_edges
        if (refused or region["hot_streak"] >= config.up_samples) and desired <= count:
            desired = count + 1
        inputs["desired"] = desired
        want = min(config.max_per_action, desired - (count + len(joining)))
        # Candidates: standby edges (paid until down), then drained slots
        # in their paid hour (the warm pool), then slots to create.
        candidates = []
        for node in sorted(edges, key=lambda item: natural(item["name"])):
            if node["name"] in in_flight or node["name"] in loads or node.get("first"):
                continue
            if obs.get(node["name"], {}).get("advertised") and not node.get("held"):
                continue  # serving but draining: not a candidate
            if node.get("created") and node.get("held") and node["name"] not in warm \
                    and not node.get("slot"):
                candidates.append((0, node["name"], "start"))
            elif node.get("created") and (node["name"] in warm or node.get("held")):
                candidates.append((1, node["name"], "start"))
            elif node.get("slot") and not node.get("created"):
                candidates.append((2, node["name"], "create"))
        candidates.sort(key=lambda item: (item[0], natural(item[1])))
        if want <= 0:
            return result("hold", f"{count} serving and {len(joining)} joining meet the "
                                  f"target of {desired}")
        if not candidates:
            return result("hold", "no edge left to add: every edge of the region's roster "
                                  "serves (the slots included)")
        if since_action < config.cooldown_s:
            return result("hold", f"cooldown: {since_action:.0f} s since the last action")
        if now - region.get("last_remove_unix", -math.inf) < config.no_flap_s and not refused:
            return result("hold", "a node left this region less than "
                                  f"{config.no_flap_s:.0f} s ago")
        chosen = [(name, how) for _rank, name, how in candidates[:want]]
        why = (f"mean {mean:.0f} >= {config.up_permille} for {region['high_streak']} samples"
               if region["high_streak"] >= config.up_samples
               else f"an edge at {hottest} >= {config.up_max_permille} with the mean "
                    f"{mean:.0f} >= {config.target_permille} for {region['hot_streak']} samples"
               if region["hot_streak"] >= config.up_samples
               else f"refusals in {region['refusal_streak']} samples")
        return result("join", f"{why}: {count} serving, target {desired}", chosen)
    if joining:
        return result("wait", "edges of this region are joining")
    if low and now - region["low_since_unix"] >= config.down_hold_s:
        if since_action < config.cooldown_s:
            return result("hold", f"cooldown: {since_action:.0f} s since the last action")
        if now - region.get("last_add_unix", -math.inf) < config.no_flap_s:
            return result("hold", "a node joined this region less than "
                                  f"{config.no_flap_s:.0f} s ago")
        victims = []
        for node in serving:
            view = obs[node["name"]]
            udp, hls = view.get("udp_subscribers"), view.get("hls_sessions")
            age = now - (node.get("created_unix") or 0)
            if node.get("first") or age < config.min_age_s:
                continue
            if udp is None or hls is None:
                continue
            victims.append((udp + hls, 0 if node.get("slot") else 1,
                            [-part if isinstance(part, int) else part
                             for part in natural(node["name"])], node["name"]))
        if not victims:
            return result("hold", "scale-down needs live gauges of UDP subscribers and HLS "
                                  "sessions on the edges (Phase AM item 8); an edge whose "
                                  "listeners are unknown is never drained to be removed")
        victims.sort()
        victim = victims[0][-1]
        inputs["victim_listeners"] = victims[0][0]
        return result("drain", f"{projected:.0f} <= {config.down_permille} with one edge "
                               f"fewer for {now - region['low_since_unix']:.0f} s", [(victim,
                                                                                    "drain")])
    return result("hold", "within the band" if count else "no serving edge advertised")


def decide_forwarders(site: str, nodes: list[dict], obs: dict, actions: dict, region: dict,
                      config: Config, now: float) -> dict | None:
    """The forwarder tier (with --forwarders): add the site's forwarder
    slot when a forwarder's lease children pass K or its CPU passes
    forwarder_cpu for 3 samples; drain it when every forwarder's children
    stay at K/2 or under for down_hold_s. Lease children per forwarder are
    advertised only once Phase AM item 12 lands (None until then: no
    decision)."""
    forwarders = [node for node in nodes if node["role"] == "distributor"]
    slots = [node for node in forwarders if node.get("slot")]
    if not slots or any(action["site"] == site for action in actions.values()):
        return None
    serving = [node for node in forwarders if obs.get(node["name"], {}).get("advertised")
               or (node.get("created") and not node.get("held") and not node.get("slot"))]
    children = [obs.get(node["name"], {}).get("lease_children") for node in serving]
    cpus = [obs.get(node["name"], {}).get("cpu") for node in serving]
    known = [value for value in children if value is not None]
    hot = (known and max(known) > config.lease_k) or any(
        value is not None and value > config.forwarder_cpu for value in cpus)
    region["fwd_hot_streak"] = region.get("fwd_hot_streak", 0) + 1 if hot else 0
    cold = bool(known) and max(known) <= config.lease_k / 2
    if cold:
        region.setdefault("fwd_cold_since_unix", now)
    else:
        region.pop("fwd_cold_since_unix", None)
    inputs = {"lease_children": dict(zip([node["name"] for node in serving], children)),
              "cpu": dict(zip([node["name"] for node in serving], cpus)),
              "hot_streak": region["fwd_hot_streak"]}
    if not known and not any(value is not None for value in cpus):
        return None
    if now - region.get("last_action_unix", -math.inf) < config.cooldown_s:
        return None
    if region["fwd_hot_streak"] >= 3:
        for node in sorted(slots, key=lambda item: natural(item["name"])):
            if not obs.get(node["name"], {}).get("advertised"):
                how = "start" if node.get("created") else "create"
                return {"site": site, "action": "join", "targets": [(node["name"], how)],
                        "reason": "forwarder lease children or CPU over the limit",
                        "inputs": inputs, "tier": "forwarder"}
    if cold and now - region["fwd_cold_since_unix"] >= config.down_hold_s:
        for node in slots:
            view = obs.get(node["name"], {})
            if view.get("advertised") and view.get("udp_subscribers") is not None \
                    and view.get("hls_sessions") is not None:
                return {"site": site, "action": "drain", "targets": [(node["name"], "drain")],
                        "reason": "forwarder lease children at K/2 or under", "inputs": inputs,
                        "tier": "forwarder"}
    return None


# --------------------------------------------------------------- the loop

class Controller:
    def __init__(self, backend: Guarded, config: Config, state_path: pathlib.Path | None,
                 events: Events, clock=time.time):
        self.backend = backend
        self.config = config
        self.state_path = state_path
        self.events = events
        self.clock = clock
        self.state = self.load()

    # -- state
    def load(self) -> dict:
        identity = self.backend.identity()
        fresh = {"schema": SCHEMA, "backend": self.backend.name, **identity,
                 "regions": {}, "actions": {}, "warm": {}, "failures": []}
        if self.state_path is None or not self.state_path.is_file():
            return fresh
        saved = json.loads(self.state_path.read_text())
        for key, value in identity.items():
            if saved.get(key) != value:
                raise Refused(f"the controller state {self.state_path} is for {key} "
                              f"{saved.get(key)!r}, not this lab's {value!r}: move it away")
        if saved.get("backend") != self.backend.name:
            raise Refused(f"the controller state is the {saved.get('backend')} backend's")
        return {**fresh, **saved}

    def save(self) -> None:
        if self.state_path is None or self.backend.dry_run:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".next")
        temporary.write_text(json.dumps(self.state, indent=1, sort_keys=True) + "\n")
        temporary.replace(self.state_path)

    def region(self, site: str) -> dict:
        return self.state["regions"].setdefault(site, {})

    def fail(self, name: str, what: str, detail: str) -> None:
        self.state["failures"].append({"unix": int(self.clock()), "node": name, "what": what,
                                       "detail": detail[-500:]})
        self.events("failure", node=name, what=what, detail=detail[-500:])

    # -- one pass
    def tick(self) -> list[dict]:
        now = self.clock()
        roster = self.backend.roster()
        obs = self.backend.observe(roster)
        by_name = {node["name"]: node for nodes in roster.values() for node in nodes}
        self.progress(now, obs, by_name)
        self.release_warm(now, by_name, obs)
        decisions = []
        for site in sorted(roster):
            region = self.region(site)
            decision = decide_region(site, roster[site], obs, self.state["actions"], region,
                                     self.state["warm"], self.config, now)
            if self.config.forwarders and decision["action"] in ("hold",):
                tier = decide_forwarders(site, roster[site], obs, self.state["actions"],
                                         region, self.config, now)
                if tier is not None:
                    decision = tier
            decisions.append(decision)
            self.log_decision(decision, now)
            if decision["action"] == "join":
                self.join(site, decision, now, by_name)
            elif decision["action"] == "drain":
                self.start_drain(site, decision, now, by_name)
        self.save()
        return decisions

    def log_decision(self, decision: dict, now: float) -> None:
        region = self.region(decision["site"])
        quiet = decision["action"] in ("hold", "wait")
        changed = decision["reason"] != region.get("last_reason")
        if quiet and not changed and now - region.get("last_hold_log", -math.inf) \
                < self.config.hold_log_s:
            return
        region["last_reason"] = decision["reason"]
        region["last_hold_log"] = now
        self.events("decision", region=decision["site"], action=decision["action"],
                    reason=decision["reason"], targets=[list(item) for item in
                                                        decision["targets"]],
                    inputs=decision["inputs"], dry_run=self.backend.dry_run,
                    tier=decision.get("tier", "edge"))

    def join(self, site: str, decision: dict, now: float, by_name: dict) -> None:
        region = self.region(site)
        region["last_action_unix"] = now
        region["last_add_unix"] = now
        for name, how in decision["targets"]:
            if how == "create":
                ok, figures = self.backend.budget([name])
                if not ok:
                    self.fail(name, "budget", json.dumps(figures))
                    continue
            timeout = (self.config.create_timeout_s if how == "create"
                       else self.config.join_timeout_s)
            self.state["warm"].pop(name, None)
            self.state["actions"][name] = {
                "kind": "join", "site": site, "how": how, "phase": how,
                "trigger_unix": now, "started_unix": now, "deadline_unix": now + timeout,
                "reason": decision["reason"]}
            self.save()
            self.events("join", node=name, region=site, how=how, reason=decision["reason"])
            self.advance(name, now, {}, by_name)

    def start_drain(self, site: str, decision: dict, now: float,
                    by_name: dict | None = None) -> None:
        region = self.region(site)
        region["last_action_unix"] = now
        for name, _how in decision["targets"]:
            role = (by_name or {}).get(name, {}).get("role") or (
                "distributor" if decision.get("tier") == "forwarder" else "edge")
            self.state["actions"][name] = {
                "kind": "drain", "site": site, "phase": "drain", "drain_sent": False,
                "role": role,
                "started_unix": now, "deadline_unix": now + self.config.drain_timeout_s,
                "reason": decision["reason"]}
            self.save()
            self.advance(name, now, {}, {})

    def progress(self, now: float, obs: dict, by_name: dict) -> None:
        for name in sorted(self.state["actions"]):
            self.advance(name, now, obs, by_name)

    def advance(self, name: str, now: float, obs: dict, by_name: dict) -> None:
        """Carry an action one or more phases on. Each phase is written to
        the state before the call that it names, so that a restart repeats
        at most an idempotent call."""
        action = self.state["actions"].get(name)
        if action is None:
            return
        view = obs.get(name, {})
        node = by_name.get(name, {})
        try:
            if action["kind"] == "join":
                self.advance_join(name, action, now, view, node)
            else:
                self.advance_drain(name, action, now, view, node)
        except Refused as error:
            self.fail(name, f"{action['kind']} {action['phase']} refused", str(error))
            self.state["actions"].pop(name, None)
            if action["kind"] == "join":
                self.park(name)
        except Exception as error:  # noqa: BLE001 - one action's failure; the next tick retries
            action["errors"] = action.get("errors", 0) + 1
            self.events("error", node=name, phase=action["phase"], detail=str(error)[-500:])
            if action["errors"] >= 5:
                self.fail(name, f"{action['kind']} {action['phase']}", str(error))
                if action["kind"] == "drain":
                    self.resume_after_failure(name)
                self.state["actions"].pop(name, None)
                if action["kind"] == "join":
                    self.park(name)
        self.save()

    def advance_join(self, name: str, action: dict, now: float, view: dict, node: dict) -> None:
        if action["phase"] == "create":
            if node.get("created"):
                action["phase"] = "push"  # a restart: the lab has it already
            elif now > action["deadline_unix"]:
                raise Refused("the create did not finish in time")
            else:
                self.backend.create(name)
                action["phase"] = "push"
                action["created_unix"] = self.clock()
                node["created"] = True
                self.events("created", node=name, region=action["site"],
                            seconds=round(self.clock() - action["trigger_unix"], 3))
            self.save()
        if action["phase"] == "push":
            self.backend.push(name)
            action["phase"] = "joining"
            self.save()
        if action["phase"] == "start":
            self.backend.start(name)
            action["phase"] = "joining"
            self.save()
        if action["phase"] == "joining":
            if view.get("advertised") and not view.get("draining"):
                self.events("serving", node=name, region=action["site"], how=action["how"],
                            trigger_to_fresh_s=round(now - action["trigger_unix"], 3))
                self.state["actions"].pop(name, None)
            elif now > action["deadline_unix"]:
                self.fail(name, "join", "no fresh advertisement before the deadline")
                self.backend.stop(name)
                if node.get("slot"):
                    # Never served: kept stopped, the warm pool deletes it.
                    self.state["warm"][name] = {"since_unix": now, "proof": "never-served"}
                self.state["actions"].pop(name, None)

    def advance_drain(self, name: str, action: dict, now: float, view: dict, node: dict) -> None:
        if action["phase"] == "drain":
            if not action.get("drain_sent"):
                self.events("drain_start", node=name, region=action["site"],
                            reason=action["reason"])
                self.backend.drain(name)
                action["drain_sent"] = True
                self.save()
                return
            udp, hls = view.get("udp_subscribers"), view.get("hls_sessions")
            role = node.get("role") or action.get("role")
            # A forwarder feeds edges, not listeners: drained only when it
            # also has no lease child and no cache-mesh child left (live
            # gauges, Phase AM item 12; unknown is not 0).
            children = {"lease_children": view.get("lease_children"),
                        "cache_children": view.get("cache_children")}
            fed = role == "distributor" and any(value != 0 for value in children.values())
            if udp == 0 and hls == 0 and not fed:
                action["phase"] = "stop"
                action["zero_unix"] = now
                self.events("drain_end", node=name, region=action["site"],
                            seconds=round(now - action["started_unix"], 3))
                self.save()
            elif now > action["deadline_unix"]:
                detail = (f"UDP subscribers {udp}, HLS sessions {hls}"
                          + (f", lease children {children['lease_children']}, cache-mesh "
                             f"children {children['cache_children']}"
                             if role == "distributor" else "")
                          + f" after {self.config.drain_timeout_s:.0f} s: resumed, not deleted")
                self.fail(name, "drain-timeout", detail)
                self.resume_after_failure(name)
                self.state["actions"].pop(name, None)
                return
            else:
                return
        if action["phase"] == "stop":
            self.backend.stop(name)
            self.region(action["site"])["last_remove_unix"] = now
            self.events("stopped", node=name, region=action["site"])
            if node.get("slot"):
                if self.config.warm_pool:
                    self.state["warm"][name] = {"since_unix": now, "proof": "drained-to-zero"}
                    self.state["actions"].pop(name, None)
                    return
                action["phase"] = "delete"
                self.save()
            else:
                self.state["actions"].pop(name, None)
                return
        if action["phase"] == "delete":
            self.backend.delete(name, proof="drained-to-zero")
            self.events("deleted", node=name, region=action["site"])
            self.state["actions"].pop(name, None)

    def park(self, name: str) -> None:
        """A join that failed: if its slot exists now (the create went
        through, the push or the start did not), hold it out and put it in
        the warm pool as never served, so that it is reused or deleted at
        the end of its paid hour rather than left to bill until `down`."""
        try:
            roster = self.backend.roster()
        except Exception as error:  # noqa: BLE001
            self.fail(name, "park", str(error))
            return
        node = next((item for nodes in roster.values() for item in nodes
                     if item["name"] == name), None)
        if node is None or not node.get("slot") or not node.get("created"):
            return
        try:
            self.backend.stop(name)
        except Exception as error:  # noqa: BLE001 - it is parked all the same
            self.fail(name, "park stop", str(error))
        self.state["warm"][name] = {"since_unix": self.clock(), "proof": "never-served"}
        self.events("parked", node=name, why="its join failed; held out in the warm pool")

    def wind_down(self) -> None:
        """The controller is being stopped (a scale run's step ends early):
        every drain it has sent and not finished with a stop is resumed, so
        no edge stays draining with nobody to finish it. A join is left in
        the state as it is (a controller started again adopts it; `down`
        deletes the lab's slots in any case), and named in the event."""
        for name in sorted(self.state["actions"]):
            action = self.state["actions"][name]
            if action["kind"] != "drain":
                continue
            if action.get("drain_sent") and action["phase"] in ("drain", "stop"):
                self.fail(name, "drain interrupted", "the controller was stopped: resumed")
                self.resume_after_failure(name)
            elif action["phase"] == "delete":
                # Stopped already: kept stopped, released by the warm pool.
                self.state["warm"][name] = {"since_unix": self.clock(),
                                            "proof": "drained-to-zero"}
            self.state["actions"].pop(name, None)
        joins = sorted(self.state["actions"])
        self.events("wound_down", joins_left=joins, warm=sorted(self.state["warm"]),
                    why="stopped from outside; drains in flight resumed")
        self.save()

    def resume_after_failure(self, name: str) -> None:
        try:
            self.backend.resume(name)
            self.events("resumed", node=name)
        except Exception as error:  # noqa: BLE001
            self.fail(name, "resume", str(error))

    def release_warm(self, now: float, by_name: dict, obs: dict | None = None) -> None:
        """Delete a warm slot before its next billed hour starts (it bills
        the started hour whatever happens), and forget one that is gone. A
        warm slot that is serving again (someone took it back into service,
        or it advertises) leaves the pool and is never deleted from it."""
        for name in sorted(self.state["warm"]):
            item = self.state["warm"][name]
            node = by_name.get(name)
            if node is None or not node.get("created"):
                self.state["warm"].pop(name)
                continue
            if item.get("since_unix", 0) >= now:
                continue  # parked in this pass: the observation is older than that
            view = (obs or {}).get(name) or {}
            if not node.get("held") or view.get("advertised"):
                self.events("warm_left", node=name,
                            why="in service again: not deleted from the warm pool")
                self.state["warm"].pop(name)
                continue
            created = node.get("created_unix")
            if created is None:
                continue
            hours = max(1, math.ceil((now - created) / 3600 - 1e-9))
            boundary = created + hours * 3600
            if boundary - now > self.config.warm_release_s:
                continue
            try:
                self.backend.delete(name, proof=item.get("proof", "warm"))
                self.events("deleted", node=name, why="warm slot at the end of its paid hour",
                            billed_hours=hours)
                self.state["warm"].pop(name)
            except Refused as error:
                self.fail(name, "warm release refused", str(error))
                self.state["warm"].pop(name)
            except Exception as error:  # noqa: BLE001 - tried again next tick
                self.events("error", node=name, phase="warm release", detail=str(error)[-500:])

    def run(self, duration: float | None = None, once: bool = False, sleep=time.sleep) -> None:
        self.events("controller_start", backend=self.backend.name, dry_run=self.backend.dry_run,
                    config=dataclasses.asdict(self.config),
                    resumed_actions=sorted(self.state["actions"]),
                    host="operator host (control plane, not in the mesh)")
        end = None if duration is None else self.clock() + duration
        try:
            while True:
                began = self.clock()
                self.tick()
                if once or (end is not None and self.clock() >= end):
                    break
                sleep(max(0.0, self.config.poll_s - (self.clock() - began)))
        except KeyboardInterrupt:
            # SIGINT, or SIGTERM (main turns it into this): stopped from
            # outside, perhaps mid-action. Leave no node draining.
            self.wind_down()
            raise
        finally:
            self.events("controller_stop", actions=sorted(self.state["actions"]),
                        warm=sorted(self.state["warm"]), dry_run=self.backend.dry_run)


# --------------------------------------------------------------------- CLI

def default_state_path(backend: str) -> pathlib.Path:
    return ROOT / "target" / "linode-qualification" / f"autoscale-{backend}.json"


def config_from(args: argparse.Namespace) -> Config:
    fields = {field.name for field in dataclasses.fields(Config)}
    return Config(**{key: value for key, value in vars(args).items()
                     if key in fields and value is not None})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="observe, decide and act until stopped")
    run.add_argument("--backend", choices=("linode", "edge_lab"), required=True)
    run.add_argument("--state", default=os.environ.get("NEEDLETAIL_LINODE_LAB_STATE"),
                     help="the lab state (linode backend)")
    run.add_argument("--controller-state", default=None,
                     help="the controller's own state (default target/linode-qualification/"
                          "autoscale-<backend>.json)")
    run.add_argument("--events", default=None, help="append the decisions here (JSON lines)")
    run.add_argument("--stream", type=int, default=4001)
    run.add_argument("--sites", default="", help="comma-separated sites (default every one)")
    run.add_argument("--dry-run", action="store_true",
                     help="print the decisions; change nothing, write no state")
    run.add_argument("--once", action="store_true")
    run.add_argument("--duration", type=float, default=None, help="seconds, then stop")
    run.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    for field in dataclasses.fields(Config):
        flag = "--" + field.name.replace("_", "-")
        if field.type in ("bool",) or isinstance(field.default, bool):
            run.add_argument(flag, dest=field.name, action=argparse.BooleanOptionalAction,
                             default=None)
        else:
            run.add_argument(flag, dest=field.name, type=float if "float" in str(field.type)
                             else int, default=None)
    status = sub.add_parser("status", help="the controller's state")
    status.add_argument("--controller-state", default=None)
    status.add_argument("--backend", choices=("linode", "edge_lab"), default="linode")
    args = parser.parse_args(argv)
    if args.command == "status":
        path = pathlib.Path(args.controller_state or default_state_path(args.backend))
        print(path.read_text() if path.is_file() else f"no controller state at {path}")
        return
    config = config_from(args)
    sites = [site for site in args.sites.split(",") if site]
    if args.backend == "linode":
        if not args.dry_run and config.budget_usd is None:
            sys.exit("the linode backend needs --budget-usd (and --spent-before-usd for what "
                     "earlier labs of the work cost)")
        if not args.state:
            sys.path.insert(0, str(HERE))
            import small_mesh_common as common  # noqa: PLC0415
            args.state = str(common.state_path())
        inner: Backend = LinodeBackend(args.state, args.stream, config, sites,
                                       args.ssh_public_key)
    else:
        inner = EdgeLabBackend(args.stream, sites)
    backend = Guarded(inner, config, args.dry_run)
    events = Events(args.events)
    state_path = pathlib.Path(args.controller_state or default_state_path(args.backend))
    install_stop_signals()
    lock = None
    try:
        # One controller per state file: two would each act on and save it.
        lock = None if args.dry_run else hold_lock(state_path)
        controller = Controller(backend, config, state_path, events)
        controller.run(args.duration, args.once)
    except Refused as error:
        sys.exit(f"refused: {error}")
    except KeyboardInterrupt:
        sys.exit(130)
    finally:
        if lock is not None:
            lock.close()


def interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def install_stop_signals() -> None:
    """SIGINT and SIGTERM stop the controller: drains in flight resumed.
    SIGINT is set here too, not left to Python's default: a process started
    in the background by a non-interactive shell (`controller &` in a
    script) or by a parent that ignores SIGINT inherits SIGINT ignored, and
    Python then installs no KeyboardInterrupt for it, so `kill -INT` did
    nothing and the controller went on acting until its --duration."""
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)


def hold_lock(state_path: pathlib.Path):
    """An exclusive lock beside the controller's state file, held for the
    controller's life (the open file is returned; closing it releases the
    lock). Refused when another controller holds it."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(state_path.with_name(state_path.name + ".lock"), "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise Refused(f"another controller holds {state_path}: one controller per state "
                      "file") from None
    return handle


if __name__ == "__main__":
    main()
