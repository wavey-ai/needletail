#!/usr/bin/env python3
"""Autoscale the edges (and, with --forwarders, the forwarder slots) of the
large mesh from the mesh's own advertised load, and recover a lost edge or
slot on evidence from inside the mesh.

Where it runs (low-latency ops D1): a control-plane process on the operator
host, not in the av-mesh binary. It needs the Linode token, which never goes
onto a mesh node. It acts with no human in the loop, from what the mesh
advertises: one entry point's `/api/assignment` per region (each cache's
freshness, draining and load_permille) and every node's own `GET /api/node`
(its state, client UDP and LL-HLS gauges, lease and cache-mesh children, CPU,
and whom it hears). A record of a run says plainly that the controller ran
on the operator host. The in-mesh edge_lifecycle stays off (it can close an
edge that has UDP listeners).

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
  touched: not started, stopped, drained, restarted, booted or rebuilt.
* Before every create, a budget guard: this lab projected to its watchdog's
  deadline in whole billed hours, the new slots included, plus the other
  labs' live needletail instances and what earlier labs spent
  (--spent-before-usd), within --budget-usd. Either defaults to what the
  lab tool holds (budget_in_force: the budget `up` recorded, and up's
  spent figure or the ledger's sum now if that is more); a lab with no
  budget anywhere gets no create. It runs on the worker. It refuses when the
  watchdog is not armed, is less than 15 min ahead, or is set past
  --max-watchdog-hours (6.0) of the lab's life (the lab tool's
  watchdog_problems; `add` applies the same rule). A slot whose create
  failed is not asked again for 10 minutes (an attempt that reaches Linode
  bills an hour).
* The tick never waits on slow work. One worker thread takes the creates,
  pushes, starts, stops, deletes and recovery steps from a queue (the
  creates of a tick in one `add_slots` call, the pushes in one `push --nodes
  a,b`); the tick saves each phase before it hands the job over and reads
  only the job's result, so every region stays observed while a create
  takes minutes. A controller started again adopts an instance that a dead
  worker created and the lab state does not know (by its run tag and its
  slot's label), and names any other.
* Drain before delete: drain-node, wait until the edge's four live gauges
  (UDP subscribers, LL-HLS sessions, moved sessions still fetching, reads in
  flight) are 0 on observations a second apart (an unknown gauge is not 0),
  and for a forwarder slot its lease and cache-mesh children, then for a
  forwarder the forwarder gate (mesh_ops.forwarder_gate: every child's other
  parent delivers, whole and on time), stop its service, then delete. A
  drain that does not reach 0 within --drain-timeout (60 s), or a gate that
  refuses, is resumed (resume-node), recorded as a failure, and nothing is
  deleted.
* A node is lost only on evidence from inside the mesh, in each of
  --lost-samples (5) ticks in a row: its /api/node did not answer, its
  site's entry answered and does not list it fresh, and at least one other
  node of its site answered and shows it silent (not heard for 2 s; for a
  forwarder also a child fed by another parent and not by it) while none
  hears it. A site whose entry does not answer, or a tick that would lose
  more than one node of a site or two of the mesh, is `observer_blind`: no
  action. A lost serving edge gets one spare at once (`replace_join`); a
  lost edge or slot is recovered by the worker (restart a stopped unit,
  boot an offline instance, rebuild one that runs and has not answered ssh
  for 60 s), never while a peer hears it, at most once per --recover-gap-s
  (600 s), and after two recoveries it is quarantined (held out, named). A
  slot that cannot be recovered is deleted by its recorded id (tag and
  label checked). A lost first forwarder is never touched: with
  --forwarders its region's slot joins until it is back.
* However it stops (SIGINT or SIGTERM, its --duration, its change, a
  failure), it hands the worker nothing more, waits for the worker's job in
  hand (up to 120 s, then ends that job's tool and names it), and resumes
  every drain in flight before it exits. One change command or controller
  per lab: the ops lock (`<lab state>.ops.lock`) is held for the
  controller's life and by the `scale` verbs, and let go only after the
  worker's job.
* Nothing is pushed to a slot that the lab's Cloud Firewall does not
  cover (the lab tool deletes a slot whose firewall cannot be set).
* Hysteresis (scale up at a mean of 700 permille for 3 samples, one edge
  at 850 or more with the mean at 600 or more for 3 samples when its UDP
  listeners account for its load over the target (wave 9 review), or
  refusals in 2 samples; down only when the load with one edge fewer stays
  at or under 450 permille for 300 s and more than --min-edges (3: a
  listener holds two edges and needs a third to move to) serve), a cooldown
  per region (60 s), no add and remove within 5 min of each other in a
  region, at most 2 nodes per action, nothing else while a drain is in
  flight, no drain while edges join, and nodes still joining counted
  against the target. A join is serving when the entry lists it fresh and
  not draining and its /api/node says `ready`.
* Warm pool (referee R2): a drained slot is kept stopped while its billed
  hour is paid and taken back first when load rises; it is deleted
  --warm-release seconds before its next billed hour would start.
* --dry-run observes and prints every decision with its inputs, and makes
  no call that changes anything (and writes no state).

One command per change, each printing its own two marks on stdout (`start
<unix.ns> <label>`, `end ...`; the referee's change-window.py reads them)
and nothing else there (every other line of the process goes to stderr),
after this host's clock was compared with one node of each region (over
50 ms: refused): `scale up|down --site S` takes the candidate or the
victim the controller would take and ends when the new node serves and the
region's rebalance has been still for 5 s (at most 60 s after it serves), or
when the drained node is stopped; `run --until join|stopped` lets the
controller decide alone and returns when its first action of that kind is
complete. Labels `autoscale.up.<site>`, `autoscale.down.<site>`.

Backends: `linode` (the lab tool, the deploy tool's push/start/stop
--nodes, the lab tool's `replace --mode rebuild`, HTTPS to the nodes'
public addresses over kept-open connections) and `edge_lab` (the local
namespace lab, scripts/edge_lab.py: its slots in London and New York), for
proof without a Linode.

Every decision, with its inputs, and every action, goes to --events (JSON
lines, `unix_us`: Unix time in microseconds), the same file as the scale
run's phases (referee R3), and to stderr.

  linode-autoscale.py run --backend linode --budget-usd 100 \\
      --spent-before-usd 31.4 --events target/compare/scale-a1/events.jsonl
  linode-autoscale.py run --backend edge_lab --stream 2001 --dry-run --once
  linode-autoscale.py run --backend edge_lab --until join --marks marks.txt
  linode-autoscale.py scale up --backend edge_lab --site london
  linode-autoscale.py status
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import math
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from mesh_ops import (CLOCK_LIMIT_S, EMPTY_HOLD_S, HTTP_PORT, LIVE_MS, Http,  # noqa: E402
                      Marks, Ops, OpsLock, Refused, address_of, control_accepted,
                      control_body, forwarder_gate, gauge, http_post, signals_held,
                      status_of)
SCHEMA = "needletail.autoscale.v1"
# Never touched, whatever a state file says.
DENY = {"arch-us-east"}
# The plans a slot may have, and their hourly price (GET /linode/types, 22
# Nov; the lab tool checks the class from the API again before it creates).
DEDICATED_PRICES = {"g6-dedicated-2": 0.054, "g6-dedicated-4": 0.108,
                    "g6-dedicated-8": 0.216}
# av-mesh --rebalance-target-permille's default: an edge over it sends UDP
# listeners to an edge with room (wave 9 C1).
REBALANCE_TARGET_PERMILLE = 800
# The mesh service of an edge or a distributor (the deploy's unit).
MESH_SERVICE = "needletail-mesh.service"
# A peer not heard for this long is silent (3.8).
SILENT_MS = 2000
# A slot whose create failed is not asked again for this long.
CREATE_BACKOFF_S = 600.0
# A running instance whose ssh has failed this long, still lost, is rebuilt.
SSH_DOWN_S = 60.0
# How often a recovery probes a node that it waits on.
PROBE_EVERY_S = 10.0
# After a recovery step, how long the node has to answer `ready`.
RECOVER_SETTLE_S = 120.0
# The longest one recovery may take, a rebuild and a push included.
RECOVER_TIMEOUT_S = 1800.0
# A scale-up ends when the region's rebalance has been still this long...
REBALANCE_STILL_S = 5.0
# ... or this long after the new node serves.
REBALANCE_BOUND_S = 60.0
# A controller that stops waits this long for the worker's job in flight
# (a create, a push, a rebuild) before it ends that job's tool and lets the
# ops lock go: scale-run sends SIGINT, then SIGTERM 90 s later, then
# SIGKILL 90 s after that.
WIND_DOWN_WAIT_S = 120.0
# Exit status of the change verbs (as mesh_ops): done, refused before
# anything changed, the change failed, interrupted.
EXIT_DONE, EXIT_REFUSED, EXIT_FAILED, EXIT_INTERRUPTED = 0, 2, 3, 130


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
    # B's seam: a listener holds two edges and needs a third to move to.
    min_edges: int = 3
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
    # What earlier labs of the work spent; required with the linode backend
    # for any create (unknown is not 0).
    spent_before_usd: float | None = None
    max_watchdog_hours: float = 6.0
    # Seconds between two "hold" decisions of a region written to the
    # events (every other decision is written when it is taken).
    hold_log_s: float = 30.0
    # A node is lost after this many ticks in a row of in-mesh evidence.
    lost_samples: int = 5
    # A lost serving edge gets a spare at once.
    replace_lost: bool = True
    # At most one recovery of a node in this long; after two, quarantined.
    recover_gap_s: float = 600.0
    # The forwarder gate before a forwarder slot's stop.
    gate_timeout_s: float = 30.0


# The Config fields a `scale` verb uses (the others are the controller's).
SCALE_FIELDS = ("poll_s", "min_edges", "drain_timeout_s", "join_timeout_s",
                "create_timeout_s", "warm_pool", "budget_usd", "spent_before_usd",
                "max_watchdog_hours", "gate_timeout_s")


# ----------------------------------------------------------------- events

class Events:
    """JSON lines with `unix_us` (Unix time in µs) and `source`, appended
    and flushed one by one, so that they line up with the per-second
    samplers and the listeners' arrivals (referee R3). Also shown on stderr
    (stdout carries a change's marks and nothing else)."""

    def __init__(self, path: str | None, echo: bool = True, clock=time.time):
        self.path = pathlib.Path(path) if path else None
        self.echo = echo
        self.clock = clock
        self.lines: list[dict] = []
        self.lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, kind: str, **fields) -> dict:
        item = {"unix_us": int(self.clock() * 1_000_000), "source": "autoscaler", "kind": kind,
                **fields}
        text = json.dumps(item, sort_keys=True)
        with self.lock:
            self.lines.append(item)
            if self.path:
                with open(self.path, "a") as handle:
                    handle.write(text + "\n")
            if self.echo:
                print(text, file=sys.stderr, flush=True)
        return item


# ------------------------------------------------------------------ backends

class Backend:
    """What a backend gives the controller. `roster()`: {site: [node]} of
    every edge and distributor that the lab may serve with, each {"name",
    "role", "site", "created" (an instance exists), "held" (created and held
    out of service), "slot" (an autoscale slot: may be created and deleted),
    "first" (a site's first two distributors: never touched),
    "created_unix", "vlan", "public" (its addresses, or None)}.
    `observe(roster)`: merge_views of the regions' entries and the nodes'
    /api/node. Changing calls raise on failure; `Refused` means nothing
    was done. `create` and `push` take a batch of names."""

    name = "backend"

    def identity(self) -> dict:
        return {}

    def lock_path(self) -> pathlib.Path:
        raise NotImplementedError

    def roster(self) -> dict[str, list[dict]]:
        raise NotImplementedError

    def observe(self, roster: dict[str, list[dict]]) -> dict[str, dict]:
        raise NotImplementedError

    def documents(self, names: list[str]) -> dict[str, dict | None]:
        """{name: its /api/node JSON, or None when it did not answer}."""
        raise NotImplementedError

    def get(self, name: str, path: str) -> tuple:
        """(HTTP status or None, JSON or None) of a GET on the node."""
        raise NotImplementedError

    def adopt(self, record: bool = True) -> list[dict]:
        """Instances of the lab's run tag that the lab state does not know:
        a slot's is recorded (`record`) and its item says "adopted"; any
        other is named and left to `down`."""
        return []

    def create(self, names: list[str]) -> None:
        raise NotImplementedError

    def push(self, names: list[str]) -> None:
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

    def probe(self, name: str) -> dict:
        """What the recovery ladder reads of a node, read-only: {"ssh":
        answered (None: not asked), "unit_active": the mesh unit is active
        (None: unknown), "instance": the instance's status ("running",
        "offline", ..., "gone")}."""
        raise NotImplementedError

    def restart(self, name: str) -> None:
        raise NotImplementedError

    def boot(self, name: str) -> None:
        raise NotImplementedError

    def rebuild(self, name: str) -> None:
        raise NotImplementedError

    def forget_host(self, name: str) -> None:
        """Forget the node's ssh host key and close its kept connection
        (a rebuilt host has a new key). Idempotent."""
        raise NotImplementedError

    def clock_offset(self, name: str) -> float | None:
        """The node's clock minus this host's, in seconds; None when it
        cannot be read."""
        raise NotImplementedError

    def terminate_children(self) -> None:
        """End the tools this backend started that still run (the
        controller stops and will not wait for them)."""

    def budget(self, new: list[str], offline: bool = False) -> tuple[bool, dict]:
        return True, {}


class Guarded(Backend):
    """Every call of the controller goes through here. It checks the safety
    rules against the backend's own roster before it lets a call through,
    and in a dry run lets no changing call through at all (it records it)."""

    PROOFS = ("drained-to-zero", "warm", "never-served", "lost")
    STEPS = ("restart", "boot", "rebuild")

    def __init__(self, inner: Backend, config: Config, dry_run: bool = False):
        self.inner = inner
        self.config = config
        self.dry_run = dry_run
        self.name = inner.name
        self.calls: list[tuple[str, str]] = []
        self._roster: dict[str, dict] = {}

    def identity(self) -> dict:
        return self.inner.identity()

    def lock_path(self) -> pathlib.Path:
        return self.inner.lock_path()

    def roster(self) -> dict[str, list[dict]]:
        roster = self.inner.roster()
        self._roster = {node["name"]: node for nodes in roster.values() for node in nodes}
        return roster

    def observe(self, roster):
        return self.inner.observe(roster)

    def documents(self, names):
        return self.inner.documents(names)

    def get(self, name, path):
        return self.inner.get(name, path)

    def probe(self, name):
        return self.inner.probe(name)

    def clock_offset(self, name):
        return self.inner.clock_offset(name)

    def terminate_children(self) -> None:
        self.inner.terminate_children()

    def adopt(self, record: bool = True) -> list[dict]:
        return self.inner.adopt(record and not self.dry_run)

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

    def call(self, verb: str, target: str, *args) -> None:
        self.calls.append((verb, target))
        if self.dry_run:
            return
        getattr(self.inner, verb)(*args)

    def create(self, names: list[str]) -> None:
        """Create slots, after the budget guard (on the worker: it reads
        the account, which can take a minute of retries)."""
        for name in names:
            node = self.node(name)
            if not node.get("slot"):
                raise Refused(f"create {name}: not an autoscale slot of the lab")
            if node.get("created"):
                raise Refused(f"create {name}: it exists already")
        ok, figures = self.budget(list(names))
        if not ok:
            raise Refused(f"budget: {json.dumps(figures)}")
        self.call("create", ",".join(names), list(names))

    def push(self, names: list[str]) -> None:
        for name in names:
            node = self.node(name)
            if not node.get("created") and not self.dry_run:
                raise Refused(f"push {name}: not created")
        self.call("push", ",".join(names), list(names))

    def start(self, name: str) -> None:
        node = self.node(name)
        if not node.get("created"):
            raise Refused(f"start {name}: not created")
        self.call("start", name, name)

    def stop(self, name: str) -> None:
        self.node(name)
        self.call("stop", name, name)

    def drain(self, name: str) -> None:
        self.node(name)
        self.call("drain", name, name)

    def resume(self, name: str) -> None:
        self.node(name)
        self.call("resume", name, name)

    def recover(self, name: str, step: str, proof: str | None = None) -> None:
        """One step of the recovery ladder of a lost node: edges and slots
        only (node()), and only with the proof "lost"."""
        self.node(name)
        if proof != "lost":
            raise Refused(f"{step} {name}: no proof that it is lost")
        if step not in self.STEPS:
            raise Refused(f"{step} is not a step of the recovery ladder")
        self.call(step, name, name)

    def forget_host(self, name: str) -> None:
        self.node(name)
        self.call("forget_host", name, name)

    def delete(self, name: str, proof: str | None = None) -> None:
        """Delete a slot. `proof` is why it may go: "drained-to-zero" (its
        gauges were seen at 0 after a drain and it was stopped), "warm" (a
        drained slot whose paid hour runs out), "never-served" (a join that
        never advertised) or "lost" (lost on in-mesh evidence and not
        recovered). Anything else is refused."""
        node = self.node(name)
        if not node.get("slot"):
            raise Refused(f"delete {name}: not an autoscale slot (standby edges are only "
                          "stopped)")
        if proof not in self.PROOFS:
            raise Refused(f"delete {name}: no proof that it holds no listener")
        if not node.get("created"):
            return
        self.call("delete", name, name)

    def budget(self, new):
        if self.dry_run:
            # A dry run reads no account: the projection of the lab alone.
            return self.inner.budget(new, offline=True)
        return self.inner.budget(new)


class GateOps(Ops):
    """The engine's backend over the controller's, for the forwarder gate
    before a forwarder slot stops: the roster's nodes and addresses, their
    /api/node and any GET. Read-only."""

    def __init__(self, backend: Backend):
        self.backend = backend

    def nodes(self) -> list[dict]:
        out = []
        for nodes in self.backend.roster().values():
            for node in nodes:
                out.append({"name": node["name"], "role": node["role"], "site": node["site"],
                            "vlan": node.get("vlan"), "public": node.get("public"),
                            "serving": bool(node.get("created") and not node.get("held")),
                            "slot": bool(node.get("slot")),
                            "standby": bool(node.get("held") and not node.get("slot")),
                            "created": bool(node.get("created")),
                            "primary": None, "secondary": None})
        return out

    def statuses(self, nodes) -> dict:
        return self.backend.documents(list(nodes))

    def status(self, node: str):
        return self.statuses([node]).get(node)

    def get(self, node: str, path: str) -> tuple:
        return self.backend.get(node, path)


# ------------------------------------------------------------- observation

def node_view(name: str, document: dict | None, by_address: dict | None = None) -> dict:
    """A node's own counters from its `GET /api/node` (C 2.3): its state,
    the client UDP block of an edge (`subscribers`, `hls_sessions`,
    `hls_moved_active`, `hls_requests_in_flight`, `refusals`,
    `rebalance_moves`), its lease and cache-mesh children, its CPU, and the
    nodes it hears: `heard` {peer: heard_age_ms} from cache_mesh.peers and
    `fed` {parent: the youngest source_age_ms of its rows} from parents[],
    each named through `by_address` (an address no node of the roster has
    keeps its address). Unknown is None, never 0."""
    status = status_of(document)
    if status is None:
        return {"answered": False, "state": None, "ready": None, "hello_draining": None,
                "operator_drained": None, "udp_subscribers": None, "hls_sessions": None,
                "hls_moved_active": None, "hls_requests_in_flight": None, "refusals": None,
                "rebalance_moves": None, "lease_children": None, "cache_children": None,
                "cpu": None, "heard": {}, "fed": {}}
    by_address = by_address or {}
    udp = status.client_udp or {}
    cpu = gauge(document, "process.cpu_permille")
    heard: dict[str, float] = {}
    peers = (document.get("cache_mesh") or {}).get("peers") \
        if isinstance(document.get("cache_mesh"), dict) else None
    for peer in peers or []:
        age = gauge(peer, "heard_age_ms") if isinstance(peer, dict) else None
        if age is None or not isinstance(peer.get("peer"), str):
            continue
        key = by_address.get(address_of(peer["peer"]), address_of(peer["peer"]))
        heard[key] = min(age, heard.get(key, math.inf))
    fed: dict[str, float] = {}
    for parent in status.parents or []:
        ages = [row["source_age_ms"] for row in parent.rows.values()
                if gauge(row, "source_age_ms") is not None]
        if ages:
            key = by_address.get(parent.address, parent.address)
            fed[key] = min(min(ages), fed.get(key, math.inf))
    return {"answered": True, "state": status.state, "ready": status.ready,
            "hello_draining": status.hello_draining,
            "operator_drained": None if status.drain_reasons is None
            else bool({"operator", "stopping"} & set(status.drain_reasons)),
            "udp_subscribers": gauge(udp, "subscribers"),
            "hls_sessions": gauge(udp, "hls_sessions"),
            "hls_moved_active": gauge(udp, "hls_moved_active"),
            "hls_requests_in_flight": gauge(udp, "hls_requests_in_flight"),
            "refusals": gauge(udp, "refusals"),
            "rebalance_moves": gauge(udp, "rebalance_moves"),
            "lease_children": gauge(document, "children.lease.children"),
            "cache_children": gauge(document, "cache_mesh.children"),
            "cpu": None if cpu is None else cpu / 1000,
            "heard": heard, "fed": fed}


def merge_views(roster: dict[str, list[dict]], entries: dict[str, dict | None],
                documents: dict[str, dict | None]) -> dict[str, dict]:
    """{name: observation}: each region's advertisement (the "caches" of
    the entry point that answered `/api/assignment`), whether it answered
    (`entry_answered`), and each node's own view (node_view). An edge is
    `advertised` when the entry lists it fresh; a forwarder, which no entry
    lists, when its own status says ready and its hellos do not say
    draining. `listed_fresh` is the entry's word alone."""
    by_address = {}
    for nodes in roster.values():
        for node in nodes:
            for key in ("vlan", "public"):
                if node.get(key):
                    by_address[node[key]] = node["name"]
    out = {}
    for site, nodes in roster.items():
        entry = entries.get(site)
        caches = {item.get("node_id"): item for item in ((entry or {}).get("caches") or [])
                  if isinstance(item, dict)}
        for node in nodes:
            cache = caches.get(node["name"]) or {}
            view = node_view(node["name"], documents.get(node["name"]), by_address)
            listed = bool(cache.get("fresh")) and node.get("created", True)
            if node["role"] == "edge":
                advertised, draining = listed, bool(cache.get("draining"))
            else:
                advertised = view["ready"] is True and view["hello_draining"] is False
                draining = bool(view["operator_drained"])
            out[node["name"]] = {**view, "advertised": advertised, "draining": draining,
                                 "listed_fresh": listed,
                                 "overloaded": bool(cache.get("overloaded")),
                                 "load_permille": cache.get("load_permille"),
                                 "entry_answered": entry is not None}
    return out


def first_entry(nodes: list[dict], fetch) -> dict | None:
    """The site's entry: the first created node in service, in roster
    order, whose `/api/assignment` answers with caches. The first is asked
    alone; when it does not answer, the rest are asked at once, so a dead
    entry costs one timeout, not one per node before the one that
    answers."""
    candidates = [node for node in nodes if node.get("created") and not node.get("held")]

    def answer(reply: tuple) -> dict | None:
        status, found = reply
        if status == 200 and isinstance(found, dict) and isinstance(found.get("caches"), list):
            return found
        return None

    if not candidates:
        return None
    found = answer(fetch(candidates[0]))
    if found is not None or len(candidates) == 1:
        return found
    with ThreadPoolExecutor(max_workers=len(candidates) - 1) as pool:
        replies = list(pool.map(fetch, candidates[1:]))
    return next((item for item in map(answer, replies) if item is not None), None)


class LinodeBackend(Backend):
    """The Linode lab: the lab tool's state and functions (in-process), the
    deploy tool's push/start/stop --nodes and the lab tool's `replace --mode
    rebuild` (a subprocess each), ssh to a node for the recovery ladder,
    HTTPS to the nodes' public addresses over kept-open connections (the
    operator's address is open in the lab's firewall). The token is read by
    the lab tool's Api, here, on the operator host; it never reaches a node
    and is never printed."""

    name = "linode"

    def __init__(self, state_path: str, stream: int, config: Config, sites: list[str] = (),
                 ssh_public_key: str = "~/.ssh/id_ed25519.pub", http: Http | None = None):
        self.lab = load_module("lab_for_autoscale", "linode-small-mesh-lab.py")
        self.state_path = pathlib.Path(state_path)
        self.stream = stream
        self.config = config
        self.sites = list(sites)
        self.ssh_public_key = ssh_public_key
        self.http = http or Http()
        self.lock_env: dict = {}
        self._api = None
        self._children: set = set()
        self._children_lock = threading.Lock()

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

    def lock_path(self) -> pathlib.Path:
        return self.state_path.with_name(self.state_path.name + ".ops.lock")

    def held_out(self) -> set[str]:
        import small_mesh_common as common  # noqa: PLC0415
        path = common.ARTIFACT_DIR / "settings.json"
        settings = json.loads(path.read_text()) if path.is_file() else {}
        return set(settings.get("standby_edges") or [])

    def roster(self) -> dict[str, list[dict]]:
        state = self.state()
        # Every tick: a dead watchdog is started again at the recorded
        # deadline (the lab tool's watchdog_ensure, C CT3), or the tick
        # fails (a run ends with exit 3, its drains resumed).
        for problem in self.lab.watchdog_ensure(state, self.state_path):
            raise Refused(f"the lab's watchdog: {problem}")
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
                "vlan": node.get("vlan_ipv4"),
                "public": node.get("public_ipv4") if created else None})
        return out

    def documents(self, names: list[str]) -> dict[str, dict | None]:
        nodes = self.state()["nodes"]
        targets = {name: (nodes[name]["public_ipv4"], HTTP_PORT, "/api/node") for name in names
                   if (nodes.get(name) or {}).get("public_ipv4")}
        answers = self.http.poll(targets)
        return {name: (answers[name][1] if name in answers and answers[name][0] == 200
                       and isinstance(answers[name][1], dict) else None) for name in names}

    def get(self, name: str, path: str) -> tuple:
        node = self.state()["nodes"].get(name) or {}
        if not node.get("public_ipv4"):
            return None, None
        return self.http.get(node["public_ipv4"], HTTP_PORT, path)

    def observe(self, roster):
        """The regions' entries and every node's /api/node at once: a dead
        node costs the tick one timeout, not one for its entry and another
        for its status."""
        path = f"/api/assignment?stream={self.stream}"

        def fetch(node: dict) -> tuple:
            return self.http.get(node["public"], HTTP_PORT, path) if node.get("public") \
                else (None, None)

        sites = list(roster)
        names = [node["name"] for nodes in roster.values() for node in nodes
                 if node.get("created")]
        with ThreadPoolExecutor(max_workers=len(sites) + 1) as pool:
            documents = pool.submit(self.documents, names)
            entries = dict(zip(sites, pool.map(lambda site: first_entry(roster[site], fetch),
                                               sites)))
            return merge_views(roster, entries, documents.result())

    def subprocess_env(self) -> dict:
        return {**os.environ, "NEEDLETAIL_LINODE_LAB_STATE": str(self.state_path),
                **self.lock_env}

    def run_child(self, command: list[str], timeout: float) -> subprocess.CompletedProcess:
        """A tool's process (deploy, lab replace), in its own process group
        so that a controller that stops and will not wait for it can end it
        (terminate_children) rather than leave it running under a lock that
        is let go."""
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=self.subprocess_env(),
                              start_new_session=True) as child:
            with self._children_lock:
                self._children.add(child)
            try:
                out, err = child.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                child.communicate()
                raise RuntimeError(f"{' '.join(command[1:4])}: no result in {timeout:.0f} s; "
                                   "ended") from None
            finally:
                with self._children_lock:
                    self._children.discard(child)
        return subprocess.CompletedProcess(command, child.returncode, out, err)

    def terminate_children(self) -> None:
        with self._children_lock:
            children = list(self._children)
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def deploy(self, verb: str, names: str) -> None:
        command = [sys.executable, str(HERE / "linode-small-mesh-deploy.py"), verb,
                   "--nodes", names]
        result = self.run_child(command, 1800)
        if result.returncode != 0:
            raise RuntimeError(f"deploy {verb} --nodes {names}: {result.stderr[-1500:]}")

    def public(self, name: str) -> str:
        node = self.state()["nodes"].get(name)
        if not node or not node.get("public_ipv4"):
            raise Refused(f"{name} has no address in the lab state")
        return node["public_ipv4"]

    def adopt(self, record: bool = True) -> list[dict]:
        """Every instance of the run tag that the lab state does not know.
        One whose label is exactly the label `add_slots` gives a slot the
        state does not have (a worker that died between the create and the
        write) is recorded in the lab state, as create_node records it, and
        given the lab firewall; any other is named and left to `down`."""
        state = self.state()
        known = {node.get("id") for node in state["nodes"].values()}
        slots = self.lab.lab_slots(state)
        labels = {}
        for name in slots:
            again = sum(1 for item in state.get("removed", []) if item["name"] == name)
            labels[f"ntsm-{state['run_id']}-{name}{f'a{again}' if again else ''}"] = name
        out = []
        for item in self.api().list_all("/linode/instances", state["tag"]):
            if item.get("id") in known or state["tag"] not in item.get("tags", []):
                continue
            name = labels.get(item.get("label"))
            if name is None or name in state["nodes"]:
                out.append({"id": item.get("id"), "label": item.get("label"), "adopted": False,
                            "why": "not a missing slot of the lab: left to down"})
                continue
            if not record:
                out.append({"id": item["id"], "label": item["label"], "name": name,
                            "adopted": False, "why": "dry run"})
                continue
            why = self.record_adopted(name, item, slots[name])
            out.append({"id": item["id"], "label": item["label"], "name": name,
                        "adopted": why is None, **({"why": why} if why else {})})
        return out

    def record_adopted(self, name: str, item: dict, slot: dict) -> str | None:
        """Record the instance as the slot, as create_node records it, then
        give it the lab firewall; one whose firewall cannot be set is
        deleted at once, as add_nodes does (never pushed or served with
        every port open, never left recorded without its firewall). None
        when adopted, else why not."""
        state = self.state()
        kind = item.get("type") or slot.get("type")
        record = {
            "id": item["id"], "name": name, "label": item["label"], "role": slot["role"],
            "site": slot["site"], "region": slot.get("region") or item.get("region"),
            "type": kind, "hourly_usd": DEDICATED_PRICES.get(kind, 0.054),
            "public_ipv4": (item.get("ipv4") or [None])[0],
            "vlan_ipv4": slot.get("vlan_ipv4"), "created": item.get("created"),
            "created_unix": int(time.time()), "slot": True}

        def adopt(current: dict) -> None:
            current["nodes"][name] = record
            current["hourly_usd"] = self.lab.lab_hourly(current)

        # Under the lab tool's state lock (C CT3): what another command
        # wrote meanwhile is kept, and a lab that is gone or going down is
        # never written (LabGone).
        try:
            self.lab.change_state(self.state_path, state, adopt, creating=True)
        except self.lab.LabGone as gone:
            raise Refused(f"adopt {name}: {gone}") from None
        api = self.api()
        try:
            self.lab.put_firewall_rules(api, self.state_path, state)
            api.call("POST", f"/networking/firewalls/{state['firewall_id']}/devices",
                     {"id": item["id"], "type": "linode"})
        except Exception as error:  # noqa: BLE001 - no node serves without the firewall
            try:
                self.lab.remove_nodes(api, self.state_path, state, [name], slots_only=True,
                                      refresh_firewall=False)
            except (Exception, SystemExit) as again:  # noqa: BLE001
                return (f"the lab firewall could not be set ({error}) and the instance could "
                        f"not be deleted ({again}): run down")
            return f"the lab firewall could not be set ({error}): deleted"
        return None

    def create(self, names: list[str]) -> None:
        """add_slots, after Guarded.create's budget guard; add_slots applies
        the watchdog, the ceilings, the dedicated plans and the budget
        again."""
        state = self.state()
        args = argparse.Namespace(state=str(self.state_path),
                                  ssh_public_key=self.ssh_public_key)
        other = self.lab.account_billed(self.api(), state["tag"])["usd"]
        try:
            self.lab.add_slots(args, self.api(), state, list(names), self.config.budget_usd,
                               self.config.spent_before_usd, other,
                               self.config.max_watchdog_hours)
        except SystemExit as error:
            raise Refused(str(error)) from None

    def push(self, names: list[str]) -> None:
        # Never push (and so start) a node that the lab's Cloud Firewall
        # does not cover: it would serve with every port open.
        for name in names:
            if not self.lab.firewall_attached(self.api(), self.state(), name):
                raise Refused(f"{name} is not attached to the lab's Cloud Firewall: not pushed")
        self.deploy("push", ",".join(names))

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

    def instance(self, name: str) -> dict:
        """The live instance of a lab node ({} when it is gone), after the
        check that it still carries the run's tag and its label."""
        state = self.state()
        node = state["nodes"].get(name)
        if not node or not node.get("id"):
            raise Refused(f"{name} has no recorded instance")
        live = self.api().call("GET", f"/linode/instances/{node['id']}", allow_404=True)
        if live:
            if state["tag"] not in live.get("tags", []):
                raise Refused(f"instance {node['id']} does not carry the run's tag {state['tag']}")
            if node.get("label") and live.get("label") != node["label"]:
                raise Refused(f"instance {node['id']} is labelled {live.get('label')}, not "
                              f"{node['label']}")
        return live or {}

    def node(self, name: str) -> dict:
        node = self.state()["nodes"].get(name)
        if not node:
            raise Refused(f"{name} is not a node of the lab state")
        return node

    def probe(self, name: str) -> dict:
        import small_mesh_common as common  # noqa: PLC0415
        live = self.instance(name)
        status = live.get("status") if live else "gone"
        ssh = active = None
        if status == "running":
            try:
                result = common.ssh(self.node(name), f"systemctl is-active {MESH_SERVICE}",
                                    check=False, timeout=30)
            except subprocess.TimeoutExpired:
                # A host that takes TCP and never finishes the handshake, or
                # a stale kept connection: ssh does not answer.
                ssh = False
            else:
                # ssh exits 255 when it could not reach the node or log in.
                ssh = result.returncode != 255
                if ssh:
                    active = result.stdout.decode(errors="replace").strip() == "active"
        return {"ssh": ssh, "unit_active": active, "instance": status}

    def clock_offset(self, name: str) -> float | None:
        """The node's clock minus this host's, from the quickest of three
        reads over the kept ssh connection (the midpoint of each), as the
        deploy tool's LinodeOps reads it."""
        import small_mesh_common as common  # noqa: PLC0415
        node = self.node(name)
        best = None
        for _ in range(3):
            began = time.time()
            try:
                remote = float(common.ssh_text(node, "date +%s.%N", timeout=30))
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                continue
            ended = time.time()
            if best is None or ended - began < best[0]:
                best = (ended - began, remote - (began + ended) / 2)
        return None if best is None else best[1]

    def restart(self, name: str) -> None:
        import small_mesh_common as common  # noqa: PLC0415
        common.ssh(self.node(name), f"systemctl restart {MESH_SERVICE}", timeout=60)

    def boot(self, name: str) -> None:
        live = self.instance(name)
        if live.get("status") != "offline":
            raise Refused(f"boot {name}: the instance is {live.get('status') or 'gone'}")
        self.api().call("POST", f"/linode/instances/{live['id']}/boot")

    def rebuild(self, name: str) -> None:
        """The lab tool's `replace NAME --mode rebuild` (its watchdog checks
        included; the instance, its id, both addresses and its billing
        continue), then the host's old key is forgotten: the rebuilt host
        has a new one."""
        command = [sys.executable, str(HERE / "linode-small-mesh-lab.py"), *self.lab_argv(
            "replace", name, "--mode", "rebuild", "--ssh-public-key", self.ssh_public_key)]
        result = self.run_child(command, 3600)
        if result.returncode != 0:
            raise RuntimeError(f"lab replace {name} --mode rebuild: "
                               f"{(result.stderr or result.stdout)[-1500:]}")
        self.forget_host(name)

    def lab_argv(self, verb: str, *rest: str) -> list[str]:
        """The lab tool's arguments for `verb` on this backend's lab: its
        state given as `--state` before the verb (the lab tool's top-level
        option), so that the command acts on the lab this controller
        watches, whatever NEEDLETAIL_LINODE_LAB_STATE says."""
        return ["--state", str(self.state_path), verb, *rest]

    def forget_host(self, name: str) -> None:
        """The host's key out of the worktree's known_hosts and its kept
        ssh connection closed (ssh refuses a host whose key changed): the
        deploy tool's own common.forget_host, which the deploy's replace
        uses too."""
        import small_mesh_common as common  # noqa: PLC0415
        common.forget_host(self.public(name))

    def budget(self, new: list[str], offline: bool = False) -> tuple[bool, dict]:
        """The lab tool's budget_check, with the watchdog rule of D7: the
        watchdog must be armed, and at most max_watchdog_hours after the
        lab was created. `offline` (a dry run): the other labs' live
        instances are not read from the account."""
        state = self.state()
        # The lab tool's figures in force (C CT3): what the controller was
        # given, else the budget `up` recorded and what earlier labs spent
        # (up's figure, or the ledger's sum now if that is more). Unknown is
        # never 0: no budget anywhere refuses.
        budget, spent = self.lab.budget_in_force(state, self.config.budget_usd,
                                                 self.config.spent_before_usd)
        if budget is None:
            return False, {"why": "no --budget-usd, and the lab state records none"}
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
        ok, figures = self.lab.budget_check(state, extra, budget, spent, other)
        if offline:
            figures["other_labs"] = "not read (dry run)"
        return ok, figures


class EdgeLabBackend(Backend):
    """The local namespace lab (scripts/edge_lab.py) as a backend, every
    region of it: its slot nodes (edge_lab.slot_nodes(): lon-dist-c and
    new-dist-c, and lon-edge-4 and new-edge-3 with `up slots=...`) stand for
    slots that are not created until the controller "creates" one (in the
    lab's slots-created.json: nothing to buy), `push` and `start` start the
    node's service in its namespace, `stop` ends it (a slot leaves
    slots-created.json, an edge of the lab is held out in
    autoscale-held.json), `delete` ends it and clears the marker. The
    recovery ladder has one step here, `restart` (start the service again);
    there is no instance to boot or rebuild. Observation is the same HTTP
    API, from the lab's namespace. Billing is nothing."""

    name = "edge_lab"
    HELD = "autoscale-held.json"

    def __init__(self, stream: int, sites: list[str] = (), lab=None):
        if lab is None:
            import edge_lab as lab  # noqa: PLC0415
        self.edge_lab = lab
        self.stream = stream
        self.sites = list(sites)

    def lock_path(self) -> pathlib.Path:
        return pathlib.Path(self.edge_lab.WORK) / "ops.lock"

    def created(self) -> set[str]:
        return set(self.edge_lab.load("slots-created.json", []) or [])

    def held(self) -> set[str]:
        return set(self.edge_lab.load(self.HELD, []) or [])

    def roster(self):
        lab = self.edge_lab
        slots = set(lab.slot_nodes())
        made, held = self.created(), self.held()
        out: dict[str, list[dict]] = {}
        firsts: dict[str, list[str]] = {}
        for name, role, site, *_ in lab.NODES:
            if role == "distributor" and name not in slots:
                firsts.setdefault(site, []).append(name)
        for name, role, site, _region, public, vlan in lab.NODES:
            if role not in ("edge", "distributor") or (self.sites and site not in self.sites):
                continue
            created = name not in slots or name in made
            out.setdefault(site, []).append({
                "name": name, "role": role, "site": site, "created": created,
                "held": created and name in held, "slot": name in slots,
                "first": name in firsts.get(site, [])[:2], "created_unix": None,
                "hourly_usd": 0.0, "vlan": vlan, "public": public})
        return out

    def get(self, name: str, path: str) -> tuple:
        return self.edge_lab.http_json(name, path)

    def documents(self, names: list[str]) -> dict[str, dict | None]:
        def one(name: str):
            status, body = self.edge_lab.http_json(name, "/api/node")
            return body if status == 200 and isinstance(body, dict) else None

        if not names:
            return {}
        with ThreadPoolExecutor(max_workers=min(16, len(names))) as pool:
            return dict(zip(names, pool.map(one, names)))

    def observe(self, roster):
        path = f"/api/assignment?stream={self.stream}"
        entries = {site: first_entry(nodes, lambda node: self.edge_lab.http_json(node["name"],
                                                                                 path))
                   for site, nodes in roster.items()}
        names = [node["name"] for nodes in roster.values() for node in nodes
                 if node.get("created")]
        return merge_views(roster, entries, self.documents(names))

    def set_marker(self, file: str, name: str, present: bool) -> None:
        names = set(self.edge_lab.load(file, []) or [])
        names = names | {name} if present else names - {name}
        self.edge_lab.save(file, sorted(names))

    def create(self, names):
        for name in names:
            self.set_marker("slots-created.json", name, True)

    def push(self, names):
        for name in names:
            self.start(name)

    def start(self, name):
        if self.edge_lab.service_pid(name) is None:
            self.edge_lab.start_service(name)
        self.set_marker(self.HELD, name, False)

    def stop(self, name):
        self.edge_lab.terminate_service(name)
        if name in set(self.edge_lab.slot_nodes()):
            self.set_marker("slots-created.json", name, False)
        else:
            self.set_marker(self.HELD, name, True)

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
        self.set_marker("slots-created.json", name, False)

    def probe(self, name):
        return {"ssh": True, "unit_active": self.edge_lab.service_pid(name) is not None,
                "instance": "running"}

    def restart(self, name):
        if self.edge_lab.service_pid(name) is not None:
            self.edge_lab.terminate_service(name)
        self.edge_lab.start_service(name)

    def boot(self, name):
        raise Refused(f"boot {name}: the local lab has no instance to boot")

    def rebuild(self, name):
        raise Refused(f"rebuild {name}: the local lab has no instance to rebuild")

    def forget_host(self, name):
        """Nothing to forget: the local lab is not reached over ssh."""

    def clock_offset(self, name):
        """The namespaces share this host's clock."""
        return 0.0


# ------------------------------------------------------------------ worker

class Worker:
    """One thread that does the slow work, so that a tick never waits on a
    create, a push or a recovery step. The tick hands over each tick's jobs
    at once (`submit`); the worker takes everything queued, runs the creates
    as one call and the pushes as one call (the lab state and the deploy's
    record are read, changed and written by each call: one worker), the
    other jobs one by one in order, and keeps each job's result for the tick
    (`take_all`). A job is {"id", "verb", "names", ...}: create, push,
    start, stop (with "gate": the forwarder gate first), delete (with its
    "proof"), recover (with its "step": probe, restart, boot or rebuild),
    forget_host. Once closed it starts nothing more: a job handed over and
    not started gets a result that says it was dropped."""

    def __init__(self, backend: Backend, config: Config, clock=time.time):
        self.backend = backend
        self.config = config
        self.clock = clock
        self.cond = threading.Condition()
        self.queue: list[dict] = []
        self.results: dict[str, dict] = {}
        self.ids: set[str] = set()
        self.busy = False
        self.closed = False
        self.thread: threading.Thread | None = None

    def submit(self, jobs: list[dict]) -> None:
        if not jobs:
            return
        with self.cond:
            self.queue.extend(jobs)
            self.ids.update(job["id"] for job in jobs)
            if self.thread is None:
                self.thread = threading.Thread(target=self.loop, name="autoscale-worker",
                                               daemon=True)
                self.thread.start()
            self.cond.notify_all()

    def knows(self, job_id: str) -> bool:
        with self.cond:
            return job_id in self.ids

    def take_all(self) -> dict[str, dict]:
        with self.cond:
            out, self.results = self.results, {}
            return out

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Until every job handed over has its result (tests; a verb's
        last step)."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.queue or self.busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
        return True

    @staticmethod
    def dropped(job: dict) -> dict:
        return {"ok": False, "refused": False, "dropped": True, "verb": job["verb"],
                "names": job["names"], "error": "dropped: the controller stopped before it ran"}

    def close(self, wait_s: float = 0.0) -> bool:
        """Start nothing more (what is queued is dropped) and wait up to
        `wait_s` for the job in hand. Whether the worker is idle."""
        deadline = time.monotonic() + wait_s
        with self.cond:
            self.closed = True
            for job in self.queue:
                self.results[job["id"]] = self.dropped(job)
            self.queue = []
            self.cond.notify_all()
            while self.busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
        return True

    def loop(self) -> None:
        while True:
            with self.cond:
                while not self.queue and not self.closed:
                    self.cond.wait()
                if self.closed:
                    return
                batch, self.queue = self.queue, []
                self.busy = True
            try:
                for verb, jobs in self.groups(batch):
                    with self.cond:
                        if self.closed:
                            for job in jobs:
                                self.results[job["id"]] = self.dropped(job)
                            continue
                    self.run(verb, jobs)
            finally:
                with self.cond:
                    self.busy = False
                    self.cond.notify_all()

    @staticmethod
    def groups(batch: list[dict]) -> list[tuple[str, list[dict]]]:
        """The creates in one group and the pushes in one, each where its
        first job stood; every other job alone, in order."""
        out: list[tuple[str, list[dict]]] = []
        merged: dict[str, list[dict]] = {}
        for job in batch:
            if job["verb"] in ("create", "push"):
                if job["verb"] not in merged:
                    merged[job["verb"]] = []
                    out.append((job["verb"], merged[job["verb"]]))
                merged[job["verb"]].append(job)
            else:
                out.append((job["verb"], [job]))
        return out

    def run(self, verb: str, jobs: list[dict]) -> None:
        names = [name for job in jobs for name in job["names"]]
        value = None
        try:
            if verb in ("create", "push"):
                getattr(self.backend, verb)(names)
            elif verb == "stop" and jobs[0].get("gate"):
                gate = forwarder_gate(GateOps(self.backend), names[0],
                                      timeout_s=self.config.gate_timeout_s)
                value = {"gate": gate.evidence, "polls": gate.polls}
                if not gate.ok:
                    raise Refused("the forwarder gate refused: " + "; ".join(gate.problems[:6]))
                self.backend.stop(names[0])
            elif verb == "delete":
                self.backend.delete(names[0], proof=jobs[0]["proof"])
            elif verb == "recover":
                step = jobs[0]["step"]
                if step == "probe":
                    value = self.backend.probe(names[0])
                else:
                    self.backend.recover(names[0], step, proof="lost")
            else:
                getattr(self.backend, verb)(names[0])
            result = {"ok": True, "value": value}
        except Refused as error:
            result = {"ok": False, "refused": True, "error": str(error)[-1500:], "value": value}
        except Exception as error:  # noqa: BLE001 - the result says what failed
            result = {"ok": False, "refused": False, "error": str(error)[-1500:],
                      "value": value}
        result["unix"] = self.clock()
        with self.cond:
            for job in jobs:
                self.results[job["id"]] = {**result, "verb": verb, "names": job["names"]}


# ---------------------------------------------------------------- deciding

def edge_candidates(edges: list[dict], in_flight, serving, obs: dict, warm: dict,
                    blocked) -> list[tuple[str, str]]:
    """The edges a region may add, in order: standby edges (paid until
    down), then drained slots in their paid hour (the warm pool), then slots
    to create; never a node in flight, serving, first, or `blocked` (a
    create in back-off, a quarantined node)."""
    candidates = []
    for node in sorted(edges, key=lambda item: natural(item["name"])):
        name = node["name"]
        if name in in_flight or name in serving or node.get("first") or name in blocked:
            continue
        if obs.get(name, {}).get("advertised") and not node.get("held"):
            continue  # serving but draining: not a candidate
        if node.get("created") and node.get("held") and name not in warm \
                and not node.get("slot"):
            candidates.append((0, name, "start"))
        elif node.get("created") and (name in warm or node.get("held")):
            candidates.append((1, name, "start"))
        elif node.get("slot") and not node.get("created"):
            candidates.append((2, name, "create"))
    candidates.sort(key=lambda item: (item[0], natural(item[1])))
    return [(name, how) for _rank, name, how in candidates]


def edge_victims(serving: list[dict], obs: dict, config: Config, now: float) -> list[tuple]:
    """The serving edges a region may drain, best first: fewest listeners
    (UDP and LL-HLS, live gauges only), slots before standby-able edges,
    the highest name; never a first node or one younger than min_age_s.
    Each item ends with its listeners and its name."""
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
    victims.sort()
    return victims


def decide_region(site: str, nodes: list[dict], obs: dict[str, dict], actions: dict,
                  region: dict, warm: dict, config: Config, now: float,
                  blocked=frozenset()) -> dict:
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
    hot_name = max(loads, key=lambda name: loads[name]) if loads else None
    # Wave 9 review: the hot edge's load must be load a new edge can take,
    # its UDP listeners at what one costs (the median of load over
    # subscribers among the edges with >= 4, as av-mesh reckons it) enough
    # to bring it to the rebalance target (REBALANCE_TARGET_PERMILLE, above
    # which an edge sends listeners to one with room); an edge hot from its
    # LL-HLS readers' CPU, which no rebalance moves, is not cooled by a new
    # edge.
    costs = sorted(loads[name] / obs[name]["udp_subscribers"] for name in loads
                   if loads[name] > 0
                   and isinstance(obs.get(name, {}).get("udp_subscribers"), int)
                   and obs[name]["udp_subscribers"] >= 4)
    cost = costs[len(costs) // 2] if costs else None
    hot_subscribers = obs.get(hot_name, {}).get("udp_subscribers") if hot_name else None
    movable = (None if cost is None or not isinstance(hot_subscribers, int)
               else round(hot_subscribers * cost))
    hot = (hottest is not None and config.up_max_permille > 0
           and hottest >= config.up_max_permille
           and mean is not None and mean >= config.target_permille
           and (movable is None or movable >= hottest - REBALANCE_TARGET_PERMILLE))
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
              "max_permille": hottest, "hot_movable_permille": movable,
              "refusal_streak": region["refusal_streak"],
              "low_for_s": round(now - region["low_since_unix"], 1) if low else 0.0}
    if blocked:
        inputs["blocked"] = sorted(blocked, key=natural)

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
        candidates = edge_candidates(edges, in_flight, loads, obs, warm, blocked)
        if want <= 0:
            return result("hold", f"{count} serving and {len(joining)} joining meet the "
                                  f"target of {desired}")
        if not candidates:
            return result("hold", "no edge left to add: every edge of the region's roster "
                                  "serves (the slots included) or is held back")
        if since_action < config.cooldown_s:
            return result("hold", f"cooldown: {since_action:.0f} s since the last action")
        if now - region.get("last_remove_unix", -math.inf) < config.no_flap_s and not refused:
            return result("hold", "a node left this region less than "
                                  f"{config.no_flap_s:.0f} s ago")
        chosen = candidates[:want]
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
        victims = edge_victims(serving, obs, config, now)
        if not victims:
            return result("hold", "scale-down needs live gauges of UDP subscribers and HLS "
                                  "sessions on the edges (/api/node client_udp); an edge whose "
                                  "listeners are unknown is never drained to be removed")
        victim = victims[0][-1]
        inputs["victim_listeners"] = victims[0][0]
        return result("drain", f"{projected:.0f} <= {config.down_permille} with one edge "
                               f"fewer for {now - region['low_since_unix']:.0f} s", [(victim,
                                                                                    "drain")])
    return result("hold", "within the band" if count else "no serving edge advertised")


def forwarder_slot_candidate(forwarders: list[dict], obs: dict, actions: dict,
                             blocked) -> tuple[str, str] | None:
    """The region's forwarder slot to add: one that is not in service, not
    in flight and not held back; started when it exists, else created."""
    for node in sorted(forwarders, key=lambda item: natural(item["name"])):
        if not node.get("slot") or node["name"] in actions or node["name"] in blocked:
            continue
        if not obs.get(node["name"], {}).get("advertised"):
            return node["name"], "start" if node.get("created") else "create"
    return None


def decide_forwarders(site: str, nodes: list[dict], obs: dict, actions: dict, region: dict,
                      config: Config, now: float, blocked=frozenset()) -> dict | None:
    """The forwarder tier (with --forwarders): add the site's forwarder
    slot when a forwarder's lease children pass K or its CPU passes
    forwarder_cpu for 3 samples; drain it when every forwarder's children
    stay at K/2 or under for down_hold_s, unless it stands in for a lost
    forwarder (region["covering"])."""
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
        target = forwarder_slot_candidate(forwarders, obs, actions, blocked)
        if target:
            return {"site": site, "action": "join", "targets": [target],
                    "reason": "forwarder lease children or CPU over the limit",
                    "inputs": inputs, "tier": "forwarder"}
    if cold and now - region["fwd_cold_since_unix"] >= config.down_hold_s \
            and not region.get("covering"):
        for node in slots:
            view = obs.get(node["name"], {})
            if view.get("advertised") and view.get("lease_children") is not None \
                    and view.get("cache_children") is not None:
                return {"site": site, "action": "drain", "targets": [(node["name"], "drain")],
                        "reason": "forwarder lease children at K/2 or under", "inputs": inputs,
                        "tier": "forwarder"}
    return None


def drained(role: str, view: dict) -> tuple[bool, dict]:
    """Whether a drained node holds nobody: an edge's four live gauges at
    0 (UDP subscribers, LL-HLS sessions, moved sessions still fetching,
    reads in flight); a forwarder's lease and cache-mesh children at 0.
    Unknown is not 0."""
    if role == "distributor":
        figures = {key: view.get(key) for key in ("lease_children", "cache_children")}
    else:
        figures = {key: view.get(key) for key in ("udp_subscribers", "hls_sessions",
                                                  "hls_moved_active", "hls_requests_in_flight")}
    return all(value == 0 for value in figures.values()), figures


def silence(name: str, node: dict, site_nodes: list[dict], obs: dict) -> tuple[bool, dict]:
    """The three parts of `lost` for one tick (3.8), with the evidence:
    the node's /api/node did not answer; its site's entry answered and does
    not list it fresh; at least one other node of the site answered and
    shows it silent (not heard for SILENT_MS in its cache-mesh peers), and
    no node that answered hears it (a peer entry or a parent row of it
    younger than SILENT_MS). For a forwarder also a child fed by another
    parent (a row of it no older than LIVE_MS) whose row of the node is
    older than SILENT_MS. An answering node with no entry for the node says
    nothing either way."""
    view = obs.get(name) or {}
    if view.get("answered"):
        return False, {"answered": True}
    if not view.get("entry_answered"):
        return False, {"entry": "did not answer"}
    if view.get("listed_fresh"):
        return False, {"entry": "lists it fresh"}
    others = [item["name"] for item in site_nodes
              if item["name"] != name and (obs.get(item["name"]) or {}).get("answered")]
    if not others:
        return False, {"peers": "no other node of the site answered"}
    silent_by, starved = {}, {}
    for other in others:
        seen = obs[other]
        heard, fed = (seen.get("heard") or {}).get(name), (seen.get("fed") or {}).get(name)
        if (heard is not None and heard <= SILENT_MS) or (fed is not None and fed <= SILENT_MS):
            return False, {"heard_by": other, "heard_age_ms": heard, "fed_age_ms": fed}
        if heard is not None:
            silent_by[other] = heard
        if fed is not None:
            others_fed = [age for parent, age in (seen.get("fed") or {}).items()
                          if parent != name and age <= LIVE_MS]
            if others_fed:
                starved[other] = fed
    evidence = {"silent_to": silent_by, "children_fed_elsewhere": starved}
    if not silent_by:
        return False, {**evidence, "peers": "no node that answered shows it silent"}
    if node["role"] == "distributor" and not starved:
        return False, {**evidence, "peers": "no child fed by another parent shows it silent"}
    return True, evidence


# --------------------------------------------------------------- the loop

class Controller:
    def __init__(self, backend: Guarded, config: Config, state_path: pathlib.Path | None,
                 events: Events, clock=time.time, worker: Worker | None = None):
        self.backend = backend
        self.config = config
        self.state_path = state_path
        self.events = events
        self.clock = clock
        self.state = self.load()
        self.worker = worker or Worker(backend, config, clock)
        self.pending: list[dict] = []
        self.finished: dict[str, dict] = {}
        # name -> (outcome, unix): "serving", "stopped" or "failed", for
        # the change a verb or `run --until` follows.
        self.outcomes: dict[str, tuple[str, float]] = {}
        self.change: dict | None = None
        self.marks: Marks | None = None
        self.silent: dict[str, dict] = {}
        self.blind: set[str] = set()
        # Whether this tick judged the lost rule (a verb's tick does not).
        self.watched = False
        self.obs: dict[str, dict] = {}
        # `run --until`: "join" or "drain", the kind of the first action
        # to follow with marks.
        self.until: str | None = None
        # How long a controller that stops waits for the worker's job.
        self.stop_wait_s = WIND_DOWN_WAIT_S
        self.closed = False

    # -- state
    def load(self) -> dict:
        identity = self.backend.identity()
        fresh = {"schema": SCHEMA, "backend": self.backend.name, **identity,
                 "regions": {}, "actions": {}, "warm": {}, "failures": [], "backoff": {},
                 "lost": {}, "recovers": {}, "quarantined": {}, "replaced": {}, "covers": {}}
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

    def close(self) -> None:
        """Stop the worker: nothing queued is started, the job in hand is
        waited for up to stop_wait_s, and if it is still running then, the
        tool it runs is ended and named. The caller lets the ops lock go
        only after this."""
        if self.closed:
            return
        self.closed = True
        if not self.worker.close(self.stop_wait_s):
            self.events("worker_abandoned", wait_s=self.stop_wait_s,
                        why="the worker's job was still running when the controller stopped: "
                            "its tool was ended; a controller started again adopts or repeats "
                            "it")
            self.backend.terminate_children()

    # -- jobs
    def queue(self, holder: dict, verb: str, names: list[str], **extra) -> None:
        """Hand a job to the worker at the end of this tick; `holder` (an
        action, a warm entry) keeps its id and reads its result."""
        job = {"id": uuid.uuid4().hex, "verb": verb, "names": list(names), **extra}
        self.pending.append(job)
        holder["job"] = job["id"]

    def take(self, holder: dict) -> dict | None:
        """The result of `holder`'s job: None while it runs; {"orphaned":
        True} for a job of a controller that is gone (this worker never had
        it)."""
        job = holder.get("job")
        if job is None:
            return None
        if job in self.finished:
            holder.pop("job")
            return self.finished.pop(job)
        if any(item["id"] == job for item in self.pending) or self.worker.knows(job):
            return None
        holder.pop("job")
        return {"ok": False, "orphaned": True, "refused": False, "error": "orphaned"}

    def flush(self) -> None:
        jobs, self.pending = self.pending, []
        self.worker.submit(jobs)

    def blocked(self, now: float) -> set[str]:
        backoff = {name for name, until in self.state["backoff"].items() if until > now}
        return backoff | set(self.state["quarantined"])

    # -- one pass
    def begin(self, command: str = "run") -> None:
        """At start: say so, and adopt what a dead worker created."""
        self.events("controller_start", command=command, backend=self.backend.name,
                    dry_run=self.backend.dry_run, config=dataclasses.asdict(self.config),
                    resumed_actions=sorted(self.state["actions"]),
                    host="operator host (control plane, not in the mesh)")
        self.backend.roster()
        for item in self.backend.adopt():
            self.events("adopted", **item)

    def tick(self, decide: bool = True) -> list[dict]:
        """One pass. `decide` False (a `scale` verb): only the actions in
        flight are carried on."""
        now = self.clock()
        # The worker's results before the roster: every result this tick
        # handles is older than the roster it reads (a create that finished
        # during the observation is the next tick's).
        self.finished.update(self.worker.take_all())
        roster = self.backend.roster()
        obs = self.backend.observe(roster)
        by_name = {node["name"]: node for nodes in roster.values() for node in nodes}
        self.obs = obs
        self.watched = decide
        if decide:
            self.watch_lost(now, roster, obs, by_name)
        self.progress(now, obs, by_name, set(roster))
        decisions = []
        if decide:
            self.release_warm(now, by_name, obs)
            blocked = self.blocked(now)
            for site in sorted(roster):
                region = self.region(site)
                decision = decide_region(site, roster[site], obs, self.state["actions"], region,
                                         self.state["warm"], self.config, now, blocked)
                if self.config.forwarders and decision["action"] in ("hold",):
                    tier = decide_forwarders(site, roster[site], obs, self.state["actions"],
                                             region, self.config, now, blocked)
                    if tier is not None:
                        decision = tier
                decisions.append(decision)
                self.log_decision(decision, now)
                if decision["action"] == "join":
                    decision["started"] = self.join(site, decision, now, by_name)
                elif decision["action"] == "drain":
                    decision["started"] = self.start_drain(site, decision, now, by_name)
                if self.until == decision["action"] and self.change is None \
                        and decision.get("started"):
                    # Marked before its first job is handed over (below).
                    direction = "up" if self.until == "join" else "down"
                    self.open_change(f"autoscale.{direction}.{site}", self.until,
                                     decision["started"], site)
        self.follow_change(now, obs, by_name)
        for job, result in sorted(self.finished.items()):
            # Jobs that no action waits on (a parked node's stop).
            self.events("job_done", job=job, verb=result.get("verb"), names=result.get("names"),
                        ok=result.get("ok"), error=result.get("error"))
        self.finished.clear()
        self.save()
        self.flush()
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

    def join(self, site: str, decision: dict, now: float, by_name: dict) -> list[str]:
        """Start the joins of a decision; the names whose join started."""
        region = self.region(site)
        region["last_action_unix"] = now
        region["last_add_unix"] = now
        started = []
        for name, how in decision["targets"]:
            # A create's budget guard runs on the worker (Guarded.create).
            timeout = (self.config.create_timeout_s if how == "create"
                       else self.config.join_timeout_s)
            self.state["warm"].pop(name, None)
            self.state["actions"][name] = {
                "kind": "join", "site": site, "how": how, "phase": how,
                "trigger_unix": now, "started_unix": now, "deadline_unix": now + timeout,
                "reason": decision["reason"]}
            self.save()
            self.events("join", node=name, region=site, how=how, reason=decision["reason"])
            started.append(name)
            self.advance(name, now, {}, by_name)
        return started

    def start_drain(self, site: str, decision: dict, now: float,
                    by_name: dict | None = None) -> list[str]:
        region = self.region(site)
        region["last_action_unix"] = now
        started = []
        for name, _how in decision["targets"]:
            role = (by_name or {}).get(name, {}).get("role") or (
                "distributor" if decision.get("tier") == "forwarder" else "edge")
            self.state["actions"][name] = {
                "kind": "drain", "site": site, "phase": "drain", "drain_sent": False,
                "role": role,
                "started_unix": now, "deadline_unix": now + self.config.drain_timeout_s,
                "reason": decision["reason"]}
            self.save()
            started.append(name)
            self.advance(name, now, {}, by_name or {})
        return started

    def progress(self, now: float, obs: dict, by_name: dict, sites) -> None:
        """Carry on every action of the sites observed (a controller or a
        verb given --sites leaves the others' actions as they are)."""
        for name in sorted(self.state["actions"]):
            if self.state["actions"][name]["site"] in sites:
                self.advance(name, now, obs, by_name)

    def done(self, name: str, outcome: str) -> None:
        self.state["actions"].pop(name, None)
        self.outcomes[name] = (outcome, self.clock())

    def advance(self, name: str, now: float, obs: dict, by_name: dict) -> None:
        """Carry an action one or more phases on. Each phase is written to
        the state before the job that it names is handed over, so that a
        restart repeats at most an idempotent call."""
        action = self.state["actions"].get(name)
        if action is None:
            return
        view = obs.get(name, {})
        node = by_name.get(name, {})
        try:
            if action["kind"] == "join":
                self.advance_join(name, action, now, view, node)
            elif action["kind"] == "drain":
                self.advance_drain(name, action, now, view, node)
            else:
                self.advance_recover(name, action, now, view, node)
        except Refused as error:
            self.fail(name, f"{action['kind']} {action['phase']} refused", str(error))
            self.done(name, "failed")
            if action["kind"] == "join":
                self.park(name, node)
        except Exception as error:  # noqa: BLE001 - one action's failure; the next tick retries
            action["errors"] = action.get("errors", 0) + 1
            self.events("error", node=name, phase=action["phase"], detail=str(error)[-500:])
            if action["errors"] >= 5:
                self.fail(name, f"{action['kind']} {action['phase']}", str(error))
                if action["kind"] == "drain":
                    self.resume_after_failure(name)
                self.done(name, "failed")
                if action["kind"] == "join":
                    self.park(name, node)
        self.save()

    def advance_join(self, name: str, action: dict, now: float, view: dict, node: dict) -> None:
        """create -> creating -> push -> pushing -> joining, or start ->
        starting -> joining; serving when the entry lists it fresh and not
        draining and its /api/node says ready."""
        if action["phase"] == "create":
            if node.get("created"):
                action["phase"] = "push"  # adopted, or the lab has it already
            else:
                self.queue(action, "create", [name])
                action["phase"] = "creating"
                return
        if action["phase"] == "creating":
            result = self.take(action)
            if result is None:
                return
            if result.get("orphaned") and not node.get("created"):
                # The controller that asked for it is gone and nothing was
                # adopted: asked again.
                self.events("requeued", node=name, phase="create")
                self.queue(action, "create", [name])
                return
            if not result.get("orphaned") and not result["ok"] and not node.get("created"):
                until = now + CREATE_BACKOFF_S
                self.state["backoff"][name] = until
                self.fail(name, "create", result["error"])
                self.events("backoff", node=name, until_unix=int(until),
                            why="its create failed; not asked again for "
                                f"{CREATE_BACKOFF_S:.0f} s")
                self.done(name, "failed")
                return
            action["phase"] = "push"
            action["created_unix"] = self.clock()
            self.events("created", node=name, region=action["site"],
                        seconds=round(self.clock() - action["trigger_unix"], 3))
        if action["phase"] == "push":
            self.queue(action, "push", [name])
            action["phase"] = "pushing"
            return
        if action["phase"] == "start":
            self.queue(action, "start", [name])
            action["phase"] = "starting"
            return
        if action["phase"] in ("pushing", "starting"):
            result = self.take(action)
            if result is None:
                return
            if result.get("orphaned"):
                self.queue(action, {"pushing": "push", "starting": "start"}[action["phase"]],
                           [name])
                return
            if not result["ok"]:
                raise Refused(result["error"]) if result.get("refused") \
                    else RuntimeError(result["error"])
            action["phase"] = "joining"
        if action["phase"] == "joining":
            if view.get("advertised") and not view.get("draining") and view.get("ready") is True:
                self.events("serving", node=name, region=action["site"], how=action["how"],
                            trigger_to_fresh_s=round(now - action["trigger_unix"], 3))
                self.done(name, "serving")
                # Back in service after it was held out for its spare: a
                # later loss of it brings a spare again.
                self.state["replaced"].pop(name, None)
            elif now > action["deadline_unix"]:
                self.fail(name, "join", "not serving (fresh, not draining, ready) before the "
                                        "deadline")
                self.done(name, "failed")
                self.park(name, node, proof="never-served")

    def advance_drain(self, name: str, action: dict, now: float, view: dict, node: dict) -> None:
        if action["phase"] == "drain":
            if not action.get("drain_sent"):
                self.events("drain_start", node=name, region=action["site"],
                            reason=action["reason"])
                self.backend.drain(name)
                action["drain_sent"] = True
                self.save()
                return
            role = node.get("role") or action.get("role")
            empty, figures = drained(role, view)
            if empty:
                zero = action.setdefault("zero_unix", now)
                if now - zero >= EMPTY_HOLD_S:
                    action["phase"] = "stop"
                    self.events("drain_end", node=name, region=action["site"],
                                seconds=round(now - action["started_unix"], 3), gauges=figures)
                    self.save()
                else:
                    return
            elif now > action["deadline_unix"]:
                detail = (", ".join(f"{key} {value}" for key, value in figures.items())
                          + f" after {self.config.drain_timeout_s:.0f} s: resumed, not deleted")
                self.fail(name, "drain-timeout", detail)
                self.resume_after_failure(name)
                self.done(name, "failed")
                return
            else:
                action.pop("zero_unix", None)
                return
        if action["phase"] == "stop":
            # A forwarder stops only after the forwarder gate passed.
            self.queue(action, "stop", [name],
                       gate=(node.get("role") or action.get("role")) == "distributor")
            action["phase"] = "stopping"
            return
        if action["phase"] == "stopping":
            result = self.take(action)
            if result is None:
                return
            if result.get("orphaned"):
                action["phase"] = "stop"
                return
            if result.get("value", {}) and result["value"].get("gate") is not None:
                self.events("gate", node=name, ok=result["ok"], polls=result["value"]["polls"],
                            evidence=result["value"]["gate"])
            if not result["ok"]:
                self.fail(name, "stop", result["error"])
                self.resume_after_failure(name)
                self.done(name, "failed")
                return
            self.region(action["site"])["last_remove_unix"] = now
            self.events("stopped", node=name, region=action["site"])
            self.outcomes[name] = ("stopped", self.clock())
            if node.get("slot"):
                if self.config.warm_pool:
                    self.state["warm"][name] = {"since_unix": now, "proof": "drained-to-zero"}
                    self.state["actions"].pop(name, None)
                    return
                action["phase"] = "delete"
            else:
                self.state["actions"].pop(name, None)
                return
        if action["phase"] == "delete":
            self.queue(action, "delete", [name], proof="drained-to-zero")
            action["phase"] = "deleting"
            return
        if action["phase"] == "deleting":
            result = self.take(action)
            if result is None:
                return
            if result.get("orphaned"):
                action["phase"] = "delete"
                return
            if not result["ok"]:
                if result.get("refused"):
                    raise Refused(result["error"])
                action["phase"] = "delete"
                raise RuntimeError(result["error"])
            self.events("deleted", node=name, region=action["site"])
            self.state["actions"].pop(name, None)

    def park(self, name: str, node: dict, proof: str = "never-served") -> None:
        """A join that failed: its node is stopped (a job), and a slot that
        exists is held out in the warm pool as never served, so that it is
        reused or deleted at the end of its paid hour rather than left to
        bill until `down`. The warm entry follows its stop (release_warm):
        until the stop is done the slot is not yet held out."""
        if not node.get("created"):
            return
        if node.get("slot"):
            item = {"since_unix": self.clock(), "proof": proof, "stopping": True}
            self.state["warm"][name] = item
            self.queue(item, "stop", [name])
            self.events("parked", node=name, why="its join failed; held out in the warm pool")
        else:
            self.queue({}, "stop", [name])

    def wind_down(self) -> None:
        """The controller stops (stopped from outside, its --duration or
        its change over, or a failure): no new work is handed over (this
        tick's jobs and the worker's queue are dropped; their phases are
        saved, so a controller started again repeats or adopts them), the
        worker's job in hand is waited for (close), and every drain it has
        sent and not finished with a stop is resumed, so no node stays
        draining with nobody to finish it. A join or a recovery is left in
        the state as it is (a controller started again adopts it or waits
        for it; `down` deletes the lab's slots in any case), and named in
        the event. Signals wait until it is done."""
        with signals_held():
            for job in self.pending:
                self.finished[job["id"]] = Worker.dropped(job)
            dropped = [f"{job['verb']} {','.join(job['names'])}" for job in self.pending]
            self.pending = []
            self.close()
            self.finished.update(self.worker.take_all())
            for name in sorted(self.state["actions"]):
                action = self.state["actions"][name]
                if action["kind"] == "recover" and action["phase"] == "rebuilding" \
                        and (self.finished.get(action.get("job")) or {}).get("dropped"):
                    # Never started: nothing was done to the instance.
                    action.pop("job")
                    action.update(phase="probe", step=None)
                if action["kind"] != "drain":
                    continue
                phase = action["phase"]
                if phase in ("stopping", "deleting"):
                    result = self.take(action)
                    if result is None:
                        continue  # still running: a controller started again finds it
                    if phase == "stopping":
                        phase = "stopped" if result["ok"] else "stop"
                    else:
                        phase = "deleted" if result["ok"] else "delete"
                if phase in ("drain", "stop"):
                    if action.get("drain_sent"):
                        self.fail(name, "drain interrupted", "the controller stopped: resumed")
                        self.resume_after_failure(name)
                elif phase in ("stopped", "delete") \
                        and self.backend._roster.get(name, {}).get("slot"):
                    # Stopped: kept stopped, released by the warm pool.
                    self.state["warm"][name] = {"since_unix": self.clock(),
                                                "proof": "drained-to-zero"}
                self.state["actions"].pop(name)
            left = sorted(self.state["actions"])
            self.events("wound_down", left=left, warm=sorted(self.state["warm"]),
                        dropped=dropped, why="the controller stops; drains in flight resumed")
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
            if item.get("job"):
                result = self.take(item)
                if result is None:
                    continue
                if item.pop("stopping", False):
                    self.parked_stop(name, item, result)
                    continue
                if result["ok"]:
                    self.events("deleted", node=name, why="warm slot at the end of its paid hour",
                                billed_hours=item.get("hours"))
                    self.state["warm"].pop(name)
                elif result.get("refused"):
                    self.fail(name, "warm release refused", result["error"])
                    self.state["warm"].pop(name)
                else:
                    self.events("error", node=name, phase="warm release",
                                detail=result["error"][-500:])
                continue
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
            item["hours"] = hours
            self.queue(item, "delete", [name], proof=item.get("proof", "warm"))

    def parked_stop(self, name: str, item: dict, result: dict) -> None:
        """The stop of a parked slot: done, it is held out and released at
        the end of its paid hour; not run (its controller stopped), asked
        again; failed five times, the slot is deleted with its proof."""
        if result["ok"]:
            return
        if not (result.get("orphaned") or result.get("dropped")):
            item["stop_errors"] = item.get("stop_errors", 0) + 1
            self.events("error", node=name, phase="park stop", detail=result["error"][-500:])
        if item.get("stop_errors", 0) >= 5:
            self.fail(name, "park stop", "not stopped after five attempts: deleted")
            self.queue(item, "delete", [name], proof=item.get("proof", "never-served"))
            return
        item["stopping"] = True
        self.queue(item, "stop", [name])

    # -- lost nodes
    def lost_candidate(self, node: dict) -> bool:
        name = node["name"]
        action = self.state["actions"].get(name)
        return (node.get("created") and not node.get("held") and name not in DENY
                and name not in self.state["quarantined"] and name not in self.state["warm"]
                and (action is None or action["kind"] == "recover"))

    def watch_lost(self, now: float, roster: dict, obs: dict, by_name: dict) -> None:
        """The lost rule (3.8), each tick: the three parts (silence), the
        blind cases, the streak of lost_samples ticks, and what follows a
        verdict and a return."""
        self.silent, self.blind = {}, set()
        fresh: dict[str, list[str]] = {}
        for site, nodes in roster.items():
            candidates = [node for node in nodes if self.lost_candidate(node)]
            entry = any((obs.get(node["name"]) or {}).get("entry_answered") for node in nodes)
            unanswered = [node["name"] for node in candidates
                          if not (obs.get(node["name"]) or {}).get("answered")]
            if not entry and unanswered:
                self.blind.add(site)
                continue
            for node in candidates:
                silent, evidence = silence(node["name"], node, nodes, obs)
                if silent:
                    self.silent[node["name"]] = evidence
                    if not self.state["lost"].get(node["name"], {}).get("verdict"):
                        fresh.setdefault(site, []).append(node["name"])
        crowded = {site for site, names in fresh.items() if len(names) > 1}
        if sum(len(names) for names in fresh.values()) > 2:
            crowded = set(fresh)
        self.blind |= crowded
        for site in sorted(roster):
            region = self.region(site)
            if site in self.blind:
                if not region.get("blind"):
                    why = ("its entry did not answer and a node did not answer"
                           if site not in crowded else
                           f"{', '.join(fresh.get(site, []))} would be lost in one tick")
                    self.events("observer_blind", region=site, why=why,
                                unanswered=sorted(name for name in by_name
                                                  if by_name[name]["site"] == site
                                                  and not (obs.get(name) or {}).get("answered")))
                region["blind"] = True
            else:
                region.pop("blind", None)
        for name in sorted(self.state["lost"]):
            node = by_name.get(name)
            if node is None or node["site"] in self.blind:
                if node is None or not self.state["lost"][name].get("verdict"):
                    self.state["lost"].pop(name)  # a blind tick breaks the row
                continue
            if name not in self.silent:
                self.returned(name, now, obs)
        for name in sorted(self.silent):
            node = by_name[name]
            if node["site"] in self.blind:
                continue
            record = self.state["lost"].setdefault(name, {"streak": 0, "first_unix": now})
            record["streak"] += 1
            if not record.get("verdict") and record["streak"] >= self.config.lost_samples:
                record["verdict"] = True
                record["lost_unix"] = now
                self.events("lost", node=name, region=node["site"], role=node["role"],
                            slot=bool(node.get("slot")), samples=record["streak"],
                            evidence=self.silent[name])
                self.on_lost(name, node, now, obs, by_name)
            elif record.get("verdict") and name not in self.state["actions"]:
                self.consider_recover(name, node, now)
        self.cover_returns(now, obs, by_name)

    def returned(self, name: str, now: float, obs: dict) -> None:
        """A node no longer silent: its row ends; one that was lost and is
        not in a recovery came back by itself."""
        record = self.state["lost"].pop(name)
        action = self.state["actions"].get(name)
        if record.get("verdict") and (action is None or action["kind"] != "recover") \
                and (obs.get(name) or {}).get("answered"):
            self.events("recovered", node=name, how="by itself",
                        lost_for_s=round(now - record.get("lost_unix", now), 3))

    def on_lost(self, name: str, node: dict, now: float, obs: dict, by_name: dict) -> None:
        site = node["site"]
        if node["role"] == "distributor" and not node.get("slot"):
            # A compiled forwarder: never restarted or rebuilt by the
            # controller; with --forwarders the region's slot stands in.
            if self.config.forwarders and name not in self.state["covers"]:
                forwarders = [item for item in by_name.values()
                              if item["site"] == site and item["role"] == "distributor"]
                target = forwarder_slot_candidate(forwarders, obs, self.state["actions"],
                                                  self.blocked(now))
                if target is None:
                    self.events("replace_join", node=name, spare=None,
                                why="no forwarder slot of the region is free")
                else:
                    self.state["covers"][name] = target[0]
                    self.region(site)["covering"] = name
                    self.events("replace_join", node=name, spare=target[0], how=target[1])
                    self.join(site, {"targets": [target], "tier": "forwarder",
                                     "reason": f"stands in for the lost forwarder {name}"},
                              now, by_name)
            return
        if node["role"] == "edge" and self.config.replace_lost \
                and name not in self.state["replaced"]:
            edges = [item for item in by_name.values()
                     if item["site"] == site and item["role"] == "edge"]
            in_flight = {key for key, action in self.state["actions"].items()
                         if action["site"] == site}
            serving = {item["name"] for item in edges if item["name"] not in self.silent
                       and not item.get("held") and item.get("created")}
            candidates = edge_candidates(edges, in_flight, serving, obs, self.state["warm"],
                                         self.blocked(now) | {name})
            spare = candidates[0] if candidates else None
            self.state["replaced"][name] = spare[0] if spare else None
            self.events("replace_join", node=name, spare=spare[0] if spare else None,
                        how=spare[1] if spare else None,
                        why=None if spare else "no standby edge or slot left")
            if spare:
                self.join(site, {"targets": [spare], "reason": f"replaces the lost edge {name}"},
                          now, by_name)
        self.consider_recover(name, node, now)

    def consider_recover(self, name: str, node: dict, now: float) -> None:
        """A lost edge or slot (never a compiled forwarder): recovered at
        most once per recover_gap_s; after two recoveries, quarantined."""
        if node["role"] == "distributor" and not node.get("slot"):
            return
        history = self.state["recovers"].setdefault(name, [])
        if len(history) >= 2:
            self.state["quarantined"][name] = {
                "since_unix": now, "recovers": list(history),
                "why": "lost again after two recoveries: held out until an operator looks"}
            self.state["lost"].pop(name, None)
            self.events("quarantined", node=name, region=node["site"], recovers=len(history))
            return
        if history and now - history[-1] < self.config.recover_gap_s:
            record = self.state["lost"].get(name, {})
            if not record.get("waiting"):
                record["waiting"] = True
                self.events("recover_wait", node=name,
                            next_unix=int(history[-1] + self.config.recover_gap_s),
                            why=f"one recovery in {self.config.recover_gap_s:.0f} s")
            return
        history.append(now)
        self.state["lost"].get(name, {}).pop("waiting", None)
        self.state["actions"][name] = {
            "kind": "recover", "site": node["site"], "role": node["role"],
            "slot": bool(node.get("slot")), "phase": "probe", "step": None,
            "started_unix": now, "deadline_unix": now + RECOVER_TIMEOUT_S}
        self.events("recover_start", node=name, region=node["site"], attempt=len(history))
        self.save()

    def cover_returns(self, now: float, obs: dict, by_name: dict) -> None:
        """A lost compiled forwarder that is ready again: the slot that
        stood in for it is drained and stopped."""
        for name in sorted(self.state["covers"]):
            view = obs.get(name) or {}
            if not (view.get("answered") and view.get("ready") is True):
                continue
            slot = self.state["covers"].pop(name)
            node = by_name.get(slot) or {}
            site = (by_name.get(name) or node).get("site")
            if site:
                self.region(site).pop("covering", None)
            if slot in self.state["actions"] or not (obs.get(slot) or {}).get("advertised"):
                self.events("cover_end", node=name, slot=slot, drained=False,
                            why="the slot is not in service")
                continue
            self.events("cover_end", node=name, slot=slot, drained=True)
            self.start_drain(site, {"targets": [(slot, "drain")], "tier": "forwarder",
                                    "reason": f"the lost forwarder {name} is ready again"},
                             now, by_name)

    def advance_recover(self, name: str, action: dict, now: float, view: dict,
                        node: dict) -> None:
        """The ladder (3.8), one step per probe: probe -> restart | boot |
        rebuilding -> pushing -> settle. A step is taken only while the node
        is silent this tick (no peer hears it, its site is not blind); a
        node that answers ready is recovered. A rebuild whose controller
        died goes forget -> forgetting -> awaiting (probes until the
        instance runs and ssh answers) -> pushing: never a second rebuild."""
        if view.get("answered") and view.get("ready") is True:
            self.events("recovered", node=name, how=action.get("step") or "by itself",
                        seconds=round(now - action["started_unix"], 3))
            self.state["lost"].pop(name, None)
            self.done(name, "recovered")
            self.after_recovered(name, node, now)
            return
        heard = self.watched and name not in self.silent and node.get("site") not in self.blind
        settled = action["phase"] == "settle" and now > action["settle_deadline_unix"]
        if heard and (action["phase"] in ("probe", "probing") or settled):
            # No longer lost (a peer hears it, or the entry lists it): the
            # ladder stops with nothing more done to it.
            self.events("recover_stopped", node=name, step=action.get("step"),
                        why="no longer lost: heard inside the mesh, not answering the operator")
            self.state["actions"].pop(name, None)
            return
        if now > action["deadline_unix"]:
            self.recover_failed(name, action, node, "not recovered within "
                                                    f"{RECOVER_TIMEOUT_S:.0f} s")
            return
        phase = action["phase"]
        if phase == "probe":
            if now >= action.get("next_probe_unix", 0):
                self.queue(action, "recover", [name], step="probe")
                action["phase"] = "probing"
            return
        if phase == "settle":
            if now > action["settle_deadline_unix"]:
                self.recover_failed(name, action, node, f"{action['step']} done, not ready "
                                                        f"within {RECOVER_SETTLE_S:.0f} s")
            return
        if phase == "forget":
            if now >= action.get("next_probe_unix", 0):
                self.queue(action, "forget_host", [name])
                action["phase"] = "forgetting"
            return
        result = self.take(action)
        if phase == "forgetting" and result is not None and not result.get("orphaned"):
            if result["ok"]:
                action["phase"] = "awaiting"
            else:
                self.events("error", node=name, phase="forget host",
                            detail=result["error"][-500:])
                action.update(phase="forget", next_probe_unix=now + PROBE_EVERY_S)
            return
        if phase == "awaiting":
            # The rebuild of a controller that is gone: wait for the
            # instance, never rebuild again.
            if result is None and action.get("job"):
                return
            if result is not None and result["ok"] and (result["value"] or {}).get(
                    "instance") == "running" and (result["value"] or {}).get("ssh"):
                self.queue(action, "push", [name])
                action["phase"] = "pushing"
                return
            if now >= action.get("next_probe_unix", 0):
                action["next_probe_unix"] = now + PROBE_EVERY_S
                self.queue(action, "recover", [name], step="probe")
            return
        if result is None:
            return
        if phase == "probing":
            if not result["ok"]:
                self.events("error", node=name, phase="probe", detail=result["error"][-500:])
                action.update(phase="probe", next_probe_unix=now + PROBE_EVERY_S)
                return
            self.choose_step(name, action, now, node, result["value"] or {})
            return
        if result.get("orphaned"):
            if phase == "rebuilding":
                self.events("recover_wait", node=name,
                            why="a rebuild was in flight when the controller stopped: waiting "
                                "for the instance, not rebuilding again")
                # The rebuilt host has a new key, which that controller did
                # not live to forget: forgotten first, or ssh refuses it.
                action["phase"] = "forget"
            elif phase == "forgetting":
                action["phase"] = "forget"
            elif phase == "pushing":
                self.queue(action, "push", [name])
            else:
                action.update(phase="settle", settle_deadline_unix=now + RECOVER_SETTLE_S)
            return
        if not result["ok"]:
            self.recover_failed(name, action, node, f"{action['step']}: {result['error']}")
            return
        if phase == "rebuilding":
            self.queue(action, "push", [name])
            action["phase"] = "pushing"
            return
        action.update(phase="settle", settle_deadline_unix=now + RECOVER_SETTLE_S)

    def choose_step(self, name: str, action: dict, now: float, node: dict,
                    probe: dict) -> None:
        action["probe"] = probe
        step = None
        if name not in self.silent or node.get("site") in self.blind:
            why = "not silent this tick (a peer hears it, or the site is blind)"
        elif probe.get("instance") == "gone":
            self.recover_failed(name, action, node, "its instance is gone")
            return
        elif probe.get("ssh") and probe.get("unit_active") is False:
            step, why = "restart", "ssh answers and the unit is not active"
        elif probe.get("instance") == "offline":
            step, why = "boot", "the instance is offline"
        elif probe.get("instance") == "running" and probe.get("ssh") is False:
            since = action.setdefault("ssh_down_unix", now)
            if now - since >= SSH_DOWN_S:
                step, why = "rebuild", f"running, ssh down for {now - since:.0f} s"
            else:
                why = f"running, ssh down for {now - since:.0f} s of {SSH_DOWN_S:.0f}"
        else:
            why = "nothing of the ladder applies (the unit runs, or the instance is changing)"
        if probe.get("ssh"):
            action.pop("ssh_down_unix", None)
        if step is None:
            action.update(phase="probe", next_probe_unix=now + PROBE_EVERY_S, waiting=why)
            return
        action.pop("waiting", None)
        action["step"] = step
        self.events("recover_step", node=name, step=step, why=why, probe=probe)
        self.queue(action, "recover", [name], step=step)
        action["phase"] = {"restart": "restart", "boot": "boot", "rebuild": "rebuilding"}[step]

    def recover_failed(self, name: str, action: dict, node: dict, why: str) -> None:
        self.events("recover_failed", node=name, region=action["site"], why=why)
        self.fail(name, "recover", why)
        self.done(name, "failed")
        if action.get("slot") and self.watched and name in self.silent:
            # A slot that cannot be recovered and is still silent goes, by
            # its recorded id (the lab tool checks its tag and label).
            self.queue({}, "delete", [name], proof="lost")

    def after_recovered(self, name: str, node: dict, now: float) -> None:
        """A base edge whose place a spare took is held out as the standby
        (drained and stopped)."""
        spare = self.state["replaced"].get(name)
        serving = (self.obs.get(spare) or {}).get("advertised") if spare else False
        if serving and node.get("role") == "edge" and not node.get("slot"):
            self.events("held_out", node=name, spare=spare,
                        why="a spare took its place: it becomes the standby")
            self.start_drain(node["site"], {"targets": [(name, "drain")],
                                            "reason": f"{spare} took its place"}, now)

    # -- a change with marks
    def clock_check(self, roster: dict, obs: dict | None = None) -> None:
        """Before the first mark (2.7): this host's clock against one node
        of each region of the roster (a node that answered first; the next
        when one cannot be read), refused above CLOCK_LIMIT_S. Marks are
        stamped here and judged against probes stamped on the view hosts."""
        offsets = {}
        for site in sorted(roster):
            nodes = sorted((node for node in roster[site]
                            if node.get("created") and not node.get("held")),
                           key=lambda node: not ((obs or {}).get(node["name"]) or {}).get(
                               "answered"))
            if not nodes:
                continue
            for node in nodes[:2]:
                offset = self.backend.clock_offset(node["name"])
                if offset is not None:
                    break
            if offset is None:
                raise Refused(f"the clock of no node of {site} could be read "
                              f"({', '.join(node['name'] for node in nodes[:2])})")
            offsets[site] = {"node": node["name"], "offset_ms": round(offset * 1000, 3)}
            if abs(offset) > CLOCK_LIMIT_S:
                raise Refused(f"the clock of {node['name']} is {offset * 1000:.0f} ms from this "
                              f"host's (at most {CLOCK_LIMIT_S * 1000:.0f} ms)")
        self.events("clock_check", offsets=offsets)

    def open_change(self, label: str, kind: str, names: list[str], site: str) -> None:
        for name in names:
            self.outcomes.pop(name, None)
        self.change = {"label": label, "kind": kind, "names": list(names), "site": site,
                       "serving_unix": None, "moves": None, "moved_unix": None, "result": None}
        self.marks.start(label)

    def follow_change(self, now: float, obs: dict, by_name: dict) -> None:
        """A join is complete when its nodes serve and the region's summed
        rebalance_moves has not risen for REBALANCE_STILL_S (at most
        REBALANCE_BOUND_S after they serve); a drain when its node is
        stopped. A failure ends it too."""
        change = self.change
        if change is None or change["result"] is not None:
            return
        outcomes = [self.outcomes.get(name) for name in change["names"]]
        result = None
        if any(item and item[0] == "failed" for item in outcomes):
            result = "failed"
        elif change["kind"] == "drain":
            if all(item and item[0] == "stopped" for item in outcomes):
                result = "done"
        elif all(item and item[0] == "serving" for item in outcomes):
            change["serving_unix"] = change["serving_unix"] or max(item[1] for item in outcomes)
            moves = [obs[node["name"]]["rebalance_moves"] for node in by_name.values()
                     if node["site"] == change["site"] and node["role"] == "edge"
                     and isinstance((obs.get(node["name"]) or {}).get("rebalance_moves"),
                                    (int, float))]
            total = sum(moves) if moves else None
            if total is not None and (change["moves"] is None or total > change["moves"]):
                change["moved_unix"] = now if change["moves"] is not None \
                    else change["serving_unix"]
            if total is not None:
                change["moves"] = total
            quiet = change["moved_unix"] is not None \
                and now - max(change["serving_unix"], change["moved_unix"]) >= REBALANCE_STILL_S
            if quiet or now - change["serving_unix"] >= REBALANCE_BOUND_S:
                result = "done"
        if result:
            change["result"] = result
            self.marks.end(change["label"])
            self.events("change_end", label=change["label"], result=result,
                        rebalance_moves=change.get("moves"))

    def finish_change(self) -> None:
        """After the change's mark: what its nodes still have to do (a
        drained slot's delete without the warm pool) is carried out, not
        left to a process that is about to exit."""
        while any(name in self.state["actions"] for name in self.change["names"]):
            self.worker.wait_idle()
            self.tick(decide=False)

    def run(self, duration: float | None = None, once: bool = False, sleep=time.sleep,
            until: str | None = None, marks: Marks | None = None) -> int:
        """Observe, decide and act until stopped, or for `duration`.
        `until` ("join" or "stopped"): print the marks of the controller's
        first action of that kind and return when it is complete (0), or
        failed or not taken within `duration` (3)."""
        self.marks = marks
        self.until = {"join": "join", "stopped": "drain", None: None}[until]
        self.begin()
        if until:
            self.clock_check(self.backend.roster())
        end = None if duration is None else self.clock() + duration
        code = None
        try:
            while True:
                began = self.clock()
                self.tick()
                if until and self.change and self.change["result"]:
                    self.finish_change()
                    code = EXIT_DONE if self.change["result"] == "done" else EXIT_FAILED
                    break
                if once or (end is not None and self.clock() >= end):
                    break
                sleep(max(0.0, self.config.poll_s - (self.clock() - began)))
        except Exception as error:  # noqa: BLE001 - named, and nothing left draining
            # SIGINT or SIGTERM (main turns it into KeyboardInterrupt) is
            # not caught here: stopped from outside, it winds down below.
            self.fail("controller", "tick", f"{type(error).__name__}: {error}")
            self.events("error", node=None, phase="tick", detail=traceback.format_exc()[-1500:])
            code = EXIT_FAILED
        finally:
            # Whatever ends it (its duration, its change, a failure, an
            # interrupt): no node left draining, the worker's job waited
            # for, the marks closed.
            with signals_held():
                self.wind_down()
                if self.marks is not None:
                    self.marks.close()
                self.events("controller_stop", actions=sorted(self.state["actions"]),
                            warm=sorted(self.state["warm"]), dry_run=self.backend.dry_run)
        if code is not None:
            return code
        if until:
            self.events("change_end", label=None, result="failed",
                        why=f"no {until} of the controller within the duration")
            return EXIT_FAILED
        return EXIT_DONE


def command_scale(controller: Controller, direction: str, site: str, tier: str,
                  marks: Marks, sleep=time.sleep) -> int:
    """`scale up|down`: one change, as the controller would make it, with
    its marks. Up: the candidate the controller would take (a standby edge,
    then a warm slot, then a slot to create; for forwarders the site's
    slot), through join and advance_join with no trigger; `end` when it
    serves and the region's rebalance is still. Down: the victim it would
    pick (fewest listeners, slots first; never below min_edges; for
    forwarders the site's slot in service), through start_drain and
    advance_drain; `end` at `stopped` (a delete is logged, not marked).
    Refused (exit 2) before any mark when there is no such node."""
    controller.marks = marks
    controller.begin(f"scale {direction}")
    now = controller.clock()
    roster = controller.backend.roster()
    if site not in roster:
        raise Refused(f"no site {site!r} in the lab's roster")
    obs = controller.backend.observe(roster)
    by_name = {node["name"]: node for nodes in roster.values() for node in nodes}
    actions = controller.state["actions"]
    busy = sorted(name for name, action in actions.items() if action["site"] == site)
    if busy:
        raise Refused(f"actions of the controller are in flight in {site}: {', '.join(busy)}")
    nodes = roster[site]
    blocked = controller.blocked(now)
    role = "edge" if tier == "edges" else "distributor"
    members = [node for node in nodes if node["role"] == role]
    label = f"autoscale.{direction}.{site}"
    if direction == "up":
        if tier == "edges":
            serving = {node["name"] for node in members
                       if (obs.get(node["name"]) or {}).get("advertised")
                       and not node.get("held")}
            found = edge_candidates(members, {}, serving, obs, controller.state["warm"],
                                    blocked)[:1]
        else:
            found = [item for item in [forwarder_slot_candidate(members, obs, actions,
                                                                blocked)] if item]
        if not found:
            raise Refused(f"no {role} of {site} left to add")
        name, how = found[0]
        if how == "create":
            ok, figures = controller.backend.budget([name])
            if not ok:
                raise Refused(f"budget: {json.dumps(figures)}")
        targets, kind = [(name, how)], "join"
    else:
        serving = [node for node in members if (obs.get(node["name"]) or {}).get("advertised")
                   and not (obs.get(node["name"]) or {}).get("draining")
                   and not node.get("held")]
        if tier == "edges":
            if len(serving) <= controller.config.min_edges:
                raise Refused(f"{site} serves {len(serving)} edges; never fewer than "
                              f"{controller.config.min_edges}")
            victims = edge_victims(serving, obs, controller.config, now)
            found = [victims[0][-1]] if victims else []
        else:
            found = [node["name"] for node in serving if node.get("slot")
                     and (obs.get(node["name"]) or {}).get("lease_children") is not None
                     and (obs.get(node["name"]) or {}).get("cache_children") is not None][:1]
        if not found:
            raise Refused(f"no {role} of {site} that may be drained (live gauges, not first)")
        targets, kind = [(found[0], "drain")], "drain"
    controller.clock_check(roster, obs)
    decision = {"targets": targets, "reason": f"scale {direction}",
                "tier": "forwarder" if tier != "edges" else "edge"}
    controller.open_change(label, kind, [targets[0][0]], site)
    # From the start mark on, whatever goes wrong is a failed change (exit
    # 3), never a refusal: the end is printed and no node is left draining.
    try:
        if kind == "join":
            controller.join(site, decision, now, by_name)
        else:
            controller.start_drain(site, decision, now, by_name)
        controller.save()
        controller.flush()
        while controller.change["result"] is None:
            sleep(controller.config.poll_s)
            controller.tick(decide=False)
        # A slot drained by `scale down` without the warm pool is deleted
        # after its stop: billing, carried on without a mark.
        controller.finish_change()
    except Exception as error:  # noqa: BLE001 - named; the change failed
        controller.fail(targets[0][0], f"scale {direction}", f"{type(error).__name__}: {error}")
        if controller.change["result"] is None:
            controller.change["result"] = "failed"
    finally:
        with signals_held():
            controller.wind_down()
            marks.close()
            controller.events("controller_stop", actions=sorted(controller.state["actions"]),
                              warm=sorted(controller.state["warm"]), dry_run=False)
    return EXIT_DONE if controller.change["result"] == "done" else EXIT_FAILED


# --------------------------------------------------------------------- CLI

def default_state_path(backend: str) -> pathlib.Path:
    return ROOT / "target" / "linode-qualification" / f"autoscale-{backend}.json"


def config_from(args: argparse.Namespace) -> Config:
    fields = {field.name for field in dataclasses.fields(Config)}
    return Config(**{key: value for key, value in vars(args).items()
                     if key in fields and value is not None})


def add_config_flags(parser: argparse.ArgumentParser, names=None) -> None:
    for field in dataclasses.fields(Config):
        if names is not None and field.name not in names:
            continue
        flag = "--" + field.name.replace("_", "-")
        if field.type in ("bool",) or isinstance(field.default, bool):
            parser.add_argument(flag, dest=field.name, action=argparse.BooleanOptionalAction,
                                default=None)
        else:
            parser.add_argument(flag, dest=field.name, type=float if "float" in str(field.type)
                                else int, default=None)


def add_backend_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", choices=("linode", "edge_lab"), required=True)
    parser.add_argument("--state", default=os.environ.get("NEEDLETAIL_LINODE_LAB_STATE"),
                        help="the lab state (linode backend)")
    parser.add_argument("--controller-state", default=None,
                        help="the controller's own state (default target/linode-qualification/"
                             "autoscale-<backend>.json)")
    parser.add_argument("--events", default=None, help="append the decisions here (JSON lines)")
    parser.add_argument("--stream", type=int, default=4001)
    parser.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    parser.add_argument("--marks", default=None,
                        help="also append the change's two marks to this file")


def make_backend(args: argparse.Namespace, config: Config, sites: list[str]) -> Backend:
    if args.backend == "linode":
        # The controller may create at any time; a `scale` verb that would
        # create is refused at its budget check, before any mark.
        if not args.state:
            import small_mesh_common as common  # noqa: PLC0415
            args.state = str(common.state_path())
        backend = LinodeBackend(args.state, args.stream, config, sites, args.ssh_public_key)
        if args.command == "run" and not args.dry_run:
            # --budget-usd and --spent-before-usd default to what the lab
            # tool holds (budget_in_force: up's budget, the ledger's sum);
            # a lab with no budget anywhere is not watched.
            state = backend.lab.read_state(backend.state_path) or {}
            if backend.lab.budget_in_force(state, config.budget_usd)[0] is None:
                raise Refused("the linode backend needs a budget: --budget-usd, or the one "
                              "the lab tool's up recorded in the lab state")
        return backend
    return EdgeLabBackend(args.stream, sites)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="observe, decide and act until stopped")
    add_backend_flags(run)
    run.add_argument("--sites", default="", help="comma-separated sites (default every one)")
    run.add_argument("--dry-run", action="store_true",
                     help="print the decisions; change nothing, write no state")
    run.add_argument("--once", action="store_true")
    run.add_argument("--duration", type=float, default=None, help="seconds, then stop")
    run.add_argument("--until", choices=("join", "stopped"), default=None,
                     help="return when the controller's first join (or drain to stopped) is "
                          "complete, printing its marks")
    add_config_flags(run)
    scale = sub.add_parser("scale", help="one scale-up or scale-down, with its marks")
    scale.add_argument("direction", choices=("up", "down"))
    add_backend_flags(scale)
    scale.add_argument("--site", required=True)
    scale.add_argument("--tier", choices=("edges", "forwarders"), default="edges")
    add_config_flags(scale, SCALE_FIELDS)
    status = sub.add_parser("status", help="the controller's state")
    status.add_argument("--controller-state", default=None)
    status.add_argument("--backend", choices=("linode", "edge_lab"), default="linode")
    args = parser.parse_args(argv)
    if args.command == "status":
        path = pathlib.Path(args.controller_state or default_state_path(args.backend))
        print(path.read_text() if path.is_file() else f"no controller state at {path}")
        return
    config = config_from(args)
    if args.command == "scale":
        sites = [args.site]
        dry_run = False
    else:
        sites = [site for site in args.sites.split(",") if site]
        dry_run = args.dry_run
    install_stop_signals()
    lock = controller = None
    # stdout is the change's two marks and nothing else (2.7): the marks
    # keep the real stdout, and every other line printed in this process
    # (the lab tool's own, from the worker thread) goes to stderr, so no
    # other line can reach stdout or split a mark.
    stdout = sys.stdout
    marks = Marks(out=stdout, marks_file=args.marks)
    sys.stdout = sys.stderr
    try:
        inner = make_backend(args, config, sites)
        backend = Guarded(inner, config, dry_run)
        if not dry_run:
            # One change command or controller per lab (2.6); a command it
            # starts (deploy push, lab replace) carries its token.
            lock = OpsLock(backend.lock_path()).acquire()
            inner.lock_env = lock.env()
        events = Events(args.events)
        state_path = pathlib.Path(args.controller_state or default_state_path(args.backend))
        controller = Controller(backend, config, state_path, events)
        if args.command == "scale":
            code = command_scale(controller, args.direction, args.site, args.tier, marks)
        else:
            code = controller.run(args.duration, args.once, until=args.until, marks=marks)
    except Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        code = EXIT_REFUSED
    except KeyboardInterrupt:
        code = EXIT_INTERRUPTED
    except Exception:  # noqa: BLE001 - said on stderr; the marks are closed below
        traceback.print_exc(file=sys.stderr)
        code = EXIT_FAILED
    finally:
        with signals_held():
            marks.close()
            if controller is not None:
                controller.close()
            if lock is not None:
                lock.release()
            sys.stdout = stdout
    sys.exit(code)


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


if __name__ == "__main__":
    main()
