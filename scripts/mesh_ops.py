#!/usr/bin/env python3
"""The engine of the mesh's change commands (C-operations 2.13, 3.3 to 3.7).

One engine drives every planned change of a running mesh, on Linode
(`LinodeOps`, in linode-small-mesh-deploy.py) and in the local lab
(`EdgeLabOps`, in edge_lab.py), through one backend interface, `Ops`. It is a
module with no command line: the deploy tool's `roll`, `restart --nodes` and
`replace`, the local lab's verbs and the autoscaler call it.

What it guarantees:

* stdout carries the change's two marks and nothing else (`start <unix.ns>
  <label>`, `end <unix.ns> <label>`, the referee's change-window.py reads
  them). Per-node marks go to their own file and to stderr. The `end` of a
  change is printed on every exit path, an interrupt included.
* One node at a time. A node is touched only after its gate passed on two
  polls in a row; every figure a gate reads must be present, a missing one
  fails the gate ("parity unknown"). An activation carries a deadline (the
  last passing poll + 1.5 s), so no restart lands long after the gate that
  allowed it.
* A node that fails its readiness for the build's reasons (R1, R2, a
  forwarder's R4, a contributor's readiness) is rolled back, through the same
  gate; for any other reason it is left as it is and named. Nothing is
  restarted twice on a guess: after an activation error the boot id decides.
* A journal beside the evidence says which node is in hand and in what
  phase; SIGINT, SIGTERM and SIGHUP resume a drained node that was not
  activated, and `resume` finishes or undoes the node in hand of a roll that
  was killed outright.
* One ops lock for every change command (`<lab state>.ops.lock`); a command
  started by the holder passes its token in NEEDLETAIL_OPS_LOCK_TOKEN.
* A dry mode (`--dry-marks`) runs every precondition and gate for real and
  read-only, sleeps in place of each drain, activation and resume, and prints
  the same marks: the null of a roll.

Exit status: 0 done; 2 refused before anything changed; 3 stopped at a gate,
a drain, or a condition that is not the build's; 4 a node failed its
readiness and was rolled back; 5 a node answers on neither build (or is left
down); 130 interrupted and cleaned up.

The contributor tier's gate and readiness are workstream A's
(scripts/contributor_changes.py, `contrib_gate` and `contrib_ready`); they
are reached through `contributor_rules` alone.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import http.client
import json
import os
import pathlib
import re
import signal
import ssl
import statistics
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

HTTP_PORT = 19444
# av-mesh's --rebalance-target-permille default: an edge over it sends UDP
# legs to an edge with room (the capacity gate's ceiling).
REBALANCE_TARGET_PERMILLE = 800
# Polls are this far apart, or one round trip of the farthest node.
POLL_S = 0.2
# The operator's clock against one node of each region (2.7).
CLOCK_LIMIT_S = 0.05
# An activation must start within this of the last passing poll (3.5).
NOT_AFTER_S = 1.5
# "Live" on the fast plane: an arrival within 100 ms.
LIVE_MS = 100
# Seconds of record a parent's row must hold for the completeness check.
WINDOW_S = 10
# Drained means the four gauges at 0 on polls spanning this long.
EMPTY_HOLD_S = 1.0
DRAIN_GAUGES = ("subscribers", "hls_sessions", "hls_moved_active", "hls_requests_in_flight")
# The mesh unit's TimeoutStopSec (B 3.12: --stop-drain-ms 14000 on an edge,
# TimeoutStopSec=20): after an activation error, an activation that started
# before its deadline has finished its restart by the deadline plus this.
UNIT_TIMEOUT_STOP_S = 20.0
# B's edge-roll rule: before the next drain of a region, its sum of
# single_legs is back at its value before the previous step, or this long.
SINGLE_LEGS_WAIT_S = 20.0
# R7: the region's entry lists a resumed edge fresh within this.
SERVING_TIMEOUT_S = 15.0
# On an interrupt after an activation: how long to wait for R1 and warm.
INTERRUPT_WAIT_S = 15.0
# A capacity cost is taken from edges carrying at least this many legs.
COST_MIN_LEGS = 10
# The dry mode's sleeps when no real roll left evidence (3.5).
DRY_TYPICAL_S = {"edges": {"drain": 4.0, "activate": 2.0, "ready": 5.0},
                 "forwarders": {"activate": 2.0, "ready": 6.0},
                 "contributors": {"activate": 8.0}}

EXIT_DONE, EXIT_REFUSED, EXIT_STOPPED, EXIT_ROLLED_BACK, EXIT_NO_ANSWER = 0, 2, 3, 4, 5
EXIT_INTERRUPTED = 130

TIERS = ("edges", "forwarders", "contributors")
ROLE_TIER = {"edge": "edges", "distributor": "forwarders", "backbone": "forwarders",
             "contributor": "contributors"}
# The level conditions (C 3.1) as the node names them in `not_ready`.
LEVEL_REASONS = {"edges": ("class_threads", "no parent heard", "parents lost"),
                 "forwarders": ("class_threads", "peers not admitted")}
OPERATOR_DRAIN = "draining: operator"
# The phases of a node in hand (journalled before each step). Only these
# leave nothing to undo or to finish: before any change, after the node was
# given back, and the dry mode's (nothing is changed).
CLEAN_PHASES = ("gate", "resumed", "dry-drain", "dry-activate")
# Drained by the command and not activated: resume-node undoes it.
DRAINED_PHASES = ("drain", "drained")
# The node may run what was put in force (an activation, a surge edge's
# start, an edge being resumed): its readiness is what is left to see.
STARTED_PHASES = ("activating", "activated", "rolled-back", "surge-start", "resume")


class Refused(Exception):
    """A rule refused; nothing was done (exit 2)."""


class Stop(Exception):
    """The change stops here with an exit status and the reason."""

    def __init__(self, code: int, why: str):
        super().__init__(why)
        self.code = code
        self.why = why


class Interrupted(BaseException):
    """SIGINT, SIGTERM or SIGHUP reached the engine. A BaseException so that
    no backend's `except Exception` swallows it."""


def natural(name: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"([0-9]+)", name)]


# ------------------------------------------------------- HTTP (moved from AUT)

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


class Http:
    """One kept-open HTTPS connection per (address, port), 2 s timeout, one
    reconnect after a failure; certificates not checked (the lab's own CA).
    `poll` asks every target at once and takes the time of the slowest. A
    backend's `status`, `statuses`, `get` and `post` go through one of these."""

    def __init__(self, timeout: float = 2.0, connect=None):
        self.timeout = timeout
        self._connect = connect or self._https
        self._connections: dict[tuple, object] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()

    @staticmethod
    def _https(host: str, port: int, timeout: float):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)

    def request(self, host: str, port: int, method: str, path: str, body: dict | None = None):
        """(HTTP status, JSON or None); (None, None) when the node did not
        answer on a kept connection nor on a new one."""
        key = (host, port)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        payload = None if body is None else json.dumps(body).encode()
        headers = {"content-type": "application/json"} if payload is not None else {}
        with lock:
            for _attempt in range(2):
                connection = self._connections.get(key)
                if connection is None:
                    connection = self._connect(host, port, self.timeout)
                    self._connections[key] = connection
                try:
                    connection.request(method, path, body=payload, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                except (OSError, http.client.HTTPException):
                    connection.close()
                    self._connections.pop(key, None)
                    continue
                try:
                    document = json.loads(data) if data else None
                except ValueError:
                    document = None
                return response.status, document
        return None, None

    def get(self, host: str, port: int, path: str):
        return self.request(host, port, "GET", path)

    def post(self, host: str, port: int, path: str, body: dict):
        return self.request(host, port, "POST", path, body)

    def poll(self, targets: dict[str, tuple[str, int, str]]) -> dict[str, tuple]:
        """{name: (host, port, path)} -> {name: (status, JSON)}, all at once."""
        if not targets:
            return {}
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = {name: pool.submit(self.get, *target) for name, target in targets.items()}
            return {name: future.result() for name, future in futures.items()}

    def close(self) -> None:
        with self._guard:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()


# --------------------------------------------------------------- node status

def _num(item, path: str):
    value = item
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _bool(item, path: str):
    value = item
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value if isinstance(value, bool) else None


def _str(item, path: str):
    value = item
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value if isinstance(value, str) else None


def _strings(item, path: str):
    value = item
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        return list(value)
    return None


def address_of(text: str) -> str:
    """The host of "ip" or "ip:port" (IPv4)."""
    return text.rsplit(":", 1)[0] if text.count(":") == 1 else text


@dataclasses.dataclass
class Parent:
    """One row of a node's `parents[]`: what one sender address delivered,
    per (stream, track); each row is the JSON object as the node sent it."""
    address: str
    id: str | None
    path: str | None
    rows: dict[tuple[int, str], dict]


@dataclasses.dataclass
class Status:
    """A node's `GET /api/node` (`needletail.node.v1`), in one form for
    av-mesh (C 2.3) and av-contrib (A 2.6). Every field is None when the node
    did not send it: a gate never reads a missing figure as 0."""
    raw: dict
    node_id: str | None
    role: str | None
    build_git: str | None
    build_unix: int | None
    dirty: bool | None
    boot_id: int | None
    args_sha256: str | None
    state: str | None
    ready: bool | None
    warm: bool | None
    ready_forced: bool | None
    not_ready: list[str] | None
    drain_reasons: list[str] | None
    hello_draining: bool | None
    threads: int | None
    threads_expected: int | None
    received_1s: int | None
    admitted_1s: int | None
    audio_deadline_drops: int | None
    parents: list[Parent] | None
    streams: dict[int, dict] | None
    compiled_targets: list[str] | None
    lease_pairs: list[tuple[str, int]] | None
    lease_pairs_total: int | None
    lease_fed_age_ms: dict[int, float] | None
    warm_parts_pending: int | None
    client_udp: dict | None
    caches_fresh: int | None

    def udp(self, name: str):
        return _num(self.client_udp, name) if isinstance(self.client_udp, dict) else None

    def stream_class(self, stream_id: int) -> str | None:
        entry = (self.streams or {}).get(stream_id) or {}
        value = entry.get("class")
        return value if isinstance(value, str) else None


def status_of(document) -> Status | None:
    """The one parser of a node's status JSON. av-mesh: `build.git` (12 hex,
    `-dirty` when dirty) and `build.built_unix` (C 2.3). av-contrib, role
    `contributor`: `build.id` and `build.unix` (A 2.6). None for no answer."""
    if not isinstance(document, dict):
        return None
    role = _str(document, "role")
    if role == "contributor":
        build_git, build_unix = _str(document, "build.id"), _num(document, "build.unix")
    else:
        build_git, build_unix = _str(document, "build.git"), _num(document, "build.built_unix")
    parents = None
    if isinstance(document.get("parents"), list):
        parents = []
        for entry in document["parents"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("address"), str):
                continue
            rows = {}
            for row in entry.get("streams") or []:
                stream_id, track = _num(row, "stream_id"), _str(row, "track")
                if stream_id is not None and track is not None:
                    rows[(int(stream_id), track)] = row
            parents.append(Parent(address_of(entry["address"]), _str(entry, "id"),
                                  _str(entry, "path"), rows))
    streams = None
    if isinstance(document.get("streams"), list):
        streams = {int(row["stream_id"]): row for row in document["streams"]
                   if _num(row, "stream_id") is not None}
    compiled = None
    compiled_list = (document.get("children") or {}).get("compiled") \
        if isinstance(document.get("children"), dict) else None
    if isinstance(compiled_list, list):
        compiled = [row["target"] for row in compiled_list
                    if isinstance(row, dict) and isinstance(row.get("target"), str)]
    lease = (document.get("children") or {}).get("lease") \
        if isinstance(document.get("children"), dict) else None
    pairs = fed = None
    if isinstance(lease, dict):
        if isinstance(lease.get("pairs_list"), list):
            pairs = [(address_of(row["child"]), int(row["stream_id"]))
                     for row in lease["pairs_list"]
                     if isinstance(row, dict) and isinstance(row.get("child"), str)
                     and _num(row, "stream_id") is not None]
        if isinstance(lease.get("streams"), list):
            fed = {int(row["stream_id"]): row["fed_age_ms"] for row in lease["streams"]
                   if _num(row, "stream_id") is not None and _num(row, "fed_age_ms") is not None}
    udp = document.get("client_udp")
    return Status(
        raw=document, node_id=_str(document, "node_id"), role=role,
        build_git=build_git, build_unix=build_unix, dirty=_bool(document, "build.dirty"),
        boot_id=_num(document, "process.boot_id"),
        args_sha256=_str(document, "process.args_sha256"),
        state=_str(document, "state"), ready=_bool(document, "ready"),
        warm=_bool(document, "warm"), ready_forced=_bool(document, "ready_forced"),
        not_ready=_strings(document, "not_ready"),
        drain_reasons=_strings(document, "drain_reasons"),
        hello_draining=_bool(document, "hello_draining"),
        threads=_num(document, "fast_plane.threads"),
        threads_expected=_num(document, "fast_plane.threads_expected"),
        received_1s=_num(document, "fast_plane.received_1s"),
        admitted_1s=_num(document, "fast_plane.admitted_1s"),
        audio_deadline_drops=_num(document, "ingest.audio_deadline_drops"),
        parents=parents, streams=streams, compiled_targets=compiled,
        lease_pairs=pairs, lease_pairs_total=_num(lease or {}, "pairs"),
        lease_fed_age_ms=fed,
        warm_parts_pending=_num(document, "cache_mesh.warm_parts_pending"),
        client_udp=udp if isinstance(udp, dict) else None,
        caches_fresh=_num(document, "entry.caches_fresh"))


def idle_contributor(status: Status) -> bool:
    """A contributor that hears no source (A 2.6: `standing_by`): not
    ready, and not failing; workstream A's gate decides whether it may be
    changed."""
    return status.role == "contributor" and status.state == "standing_by"


def parent_row(status: Status | None, addresses, stream_id: int, track: str = "audio"):
    """`status`'s row for the parent at one of `addresses`, for a stream."""
    if status is None or status.parents is None:
        return None
    for parent in status.parents:
        if parent.address in addresses:
            return parent.rows.get((stream_id, track))
    return None


# ------------------------------------------------------------------- backend

class Ops:
    """The backend: everything the engine asks of a lab. `LinodeOps` (the
    deploy tool), `EdgeLabOps` (the local lab) and the tests' fake implement
    it. Changing calls raise on failure; `Refused` means nothing changed.

    `nodes()` -> [record], one per node of the lab: {"name", "role" (edge,
    distributor, backbone, contributor), "site", "vlan", "public" (addresses
    or None), "serving" (in service now: an edge not held out, a slot that
    exists and runs, every other node), "slot" (an autoscale slot),
    "standby" (an edge held out that may be started), "created" (a slot
    whose instance exists, running or stopped), "primary", "secondary" (the
    names of a level-2 forwarder's compiled parents, the secondary being
    its repair lane; None elsewhere)}."""

    #: <lab state>.ops.lock
    lock_path: pathlib.Path
    #: where evidence and journals go: small-mesh-artifacts/rolls/
    evidence_dir: pathlib.Path

    def nodes(self) -> list[dict]:
        raise NotImplementedError

    def status(self, node: str):
        """The node's `/api/node` JSON, or None when it does not answer."""
        raise NotImplementedError

    def statuses(self, nodes) -> dict:
        """{node: JSON or None}, asked of every node at once."""
        return {node: self.status(node) for node in nodes}

    def get(self, node: str, path: str) -> tuple:
        """(HTTP status or None, JSON or None) of any GET on the node."""
        raise NotImplementedError

    def post(self, node: str, path: str, body: dict) -> int | None:
        """The HTTP status of a POST to the node itself."""
        raise NotImplementedError

    def check(self, command: str, nodes: list[str]) -> None:
        """The backend's preconditions (watchdog, budget, the reproduce check
        and the --set rules, the binaries): raise Refused."""
        raise NotImplementedError

    def target(self, tier: str) -> dict:
        """The build a roll of `tier` installs: {"git", "unix", "dirty"}."""
        raise NotImplementedError

    def expected(self, node: str, rollback: bool = False) -> dict:
        """What the node reports after its activation: {"git", "unix",
        "args_sha256"} of the staged arguments (of `.args.prev` with
        rollback)."""
        raise NotImplementedError

    def stage(self, nodes: list[str], build: dict | None) -> None:
        """Files only, in parallel, each file's sha256 checked; build None:
        the tier's recorded build (restart --nodes)."""
        raise NotImplementedError

    def activate(self, node: str, not_after_ms: int, rollback: bool = False) -> dict:
        """Run the node's activation script; {"ok": bool, "why": str}, "why"
        "late" for the script's deadline refusal. It returns only once the
        script's result file is written (the restart has run), so the
        engine's NRestarts read after it is the one to compare with."""
        raise NotImplementedError

    def boot_changed(self, node: str, since_ms: int) -> bool | None:
        """Whether the node's service started after `since_ms` (from the
        node's service manager, for a node whose HTTP does not answer); None
        when it cannot be told."""
        raise NotImplementedError

    def unit_restarts(self, node: str) -> int | None:
        """The unit's NRestarts, an integer (`systemctl show -p NRestarts`;
        the local lab gives its own count). None only when it cannot be
        read, which fails a readiness wait as not the build's."""
        raise NotImplementedError

    def clock_offset(self, node: str) -> float | None:
        """The node's clock minus the operator's, in seconds."""
        raise NotImplementedError

    def surge_candidates(self, site: str) -> list[str]:
        """Standby edges and created slots of the site that can start on the
        target build."""
        raise NotImplementedError

    def start_node(self, node: str) -> None:
        """Start a standby edge or a stopped slot and count it in service.
        It runs its staged arguments (`.args.next` put in force as the
        activation script does) when something is staged, else those in
        force. Refused: nothing was started."""
        raise NotImplementedError

    def stop_node(self, node: str, not_after_ms: int | None = None) -> None:
        """A graceful stop; an edge goes back to the standby edges. With
        `not_after_ms` the stop is bound as an activation is: when the
        node's clock is past it, Refused and nothing stopped."""
        raise NotImplementedError

    def rebuild(self, node: str, mode: str) -> None:
        """The lab tool's `replace --mode rebuild|new`, then forget the
        host's key."""
        raise NotImplementedError

    def push(self, node: str) -> None:
        """Bootstrap, binaries of the tier's recorded build, arguments, start."""
        raise NotImplementedError

    def add_slot(self, node: str) -> None:
        """Create (the lab tool's `add`, with the budget) and push a slot.
        Refused: nothing was created; any other error: it may exist."""
        raise NotImplementedError

    def remove_slot(self, node: str) -> None:
        """Delete a slot by its recorded id, whatever state it is in (one
        created part-way included); nothing to delete is not an error."""
        raise NotImplementedError


class Lab:
    """The node records of `Ops.nodes()`, with the relations the gates need."""

    def __init__(self, records: list[dict]):
        self.records = {record["name"]: dict(record) for record in records}
        self.order = [record["name"] for record in records]
        self.readdress([])

    def readdress(self, records: list[dict]) -> None:
        """Take the addresses of `records` (`replace --mode new` gives a
        node new ones)."""
        for record in records:
            if record["name"] in self.records:
                for key in ("vlan", "public"):
                    self.records[record["name"]][key] = record.get(key)
        self.by_address = {}
        for name, record in self.records.items():
            for key in ("vlan", "public"):
                if record.get(key):
                    self.by_address[record[key]] = name

    def tier(self, name: str) -> str:
        return ROLE_TIER[self.records[name]["role"]]

    def site(self, name: str) -> str:
        return self.records[name]["site"]

    def addresses(self, name: str) -> set[str]:
        record = self.records.get(name) or {}
        return {record[key] for key in ("vlan", "public") if record.get(key)}

    def name_of(self, address: str) -> str | None:
        return self.by_address.get(address_of(address))

    def sites(self) -> list[str]:
        out = []
        for name in self.order:
            if self.records[name]["site"] not in out:
                out.append(self.records[name]["site"])
        return out

    def serving(self, site: str | None = None, roles=None) -> list[str]:
        return sorted((name for name, record in self.records.items()
                       if record.get("serving") and (site is None or record["site"] == site)
                       and (roles is None or record["role"] in roles)), key=natural)

    def edges(self, site: str) -> list[str]:
        return self.serving(site, ("edge",))

    def distributors(self, site: str) -> list[str]:
        return self.serving(site, ("distributor", "backbone"))

    def first_distributors(self, site: str) -> list[str]:
        """The site's compiled distributors (not slots), by name."""
        return sorted((name for name, record in self.records.items()
                       if record["site"] == site and record["role"] == "distributor"
                       and not record.get("slot")), key=natural)

    def contributor_site(self) -> str | None:
        return next((record["site"] for record in self.records.values()
                     if record["role"] == "contributor"), None)

    def compiled_children(self, name: str) -> list[str]:
        return sorted((child for child, record in self.records.items()
                       if name in (record.get("primary"), record.get("secondary"))), key=natural)

    def other_parent(self, child: str, name: str) -> str | None:
        record = self.records[child]
        pair = (record.get("primary"), record.get("secondary"))
        return pair[1] if pair[0] == name else pair[0] if pair[1] == name else None


def plan_order(lab: Lab, tier: str, nodes=None, sites=None) -> list[str]:
    """The order of a roll (3.5). Edges region by region. Forwarders so that
    siblings are apart: every remote region's other forwarders (a slot in
    service before its region's dist-b), then every remote region's first
    by name, then the contributor's site from its last to its first (its
    ingresses and the lab's entry last). Contributors from the last by name
    (the standby `contrib-lon-b` before `contrib-lon`). `all` is edges,
    forwarders, contributors: receivers before senders."""
    if tier == "all":
        order = [name for each in TIERS for name in plan_order(lab, each, None, sites)]
        unknown = [name for name in nodes or () if name not in order]
        if unknown:
            raise Refused(f"not a serving node of the lab: {', '.join(unknown)}")
        return [name for name in order if not nodes or name in nodes]
    if tier not in TIERS:
        raise Refused(f"no tier {tier!r}: edges, forwarders, contributors or all")
    if tier == "edges":
        order = [name for site in lab.sites() for name in lab.edges(site)]
    elif tier == "forwarders":
        home = lab.contributor_site()
        remote = [site for site in lab.sites() if site != home and lab.distributors(site)]
        first_wave, second_wave = [], []
        for site in remote:
            names = lab.distributors(site)
            first = names[0]
            first_wave += [name for name in reversed(names) if name != first]
            second_wave.append(first)
        order = first_wave + second_wave + list(reversed(lab.distributors(home) if home else []))
    else:
        order = list(reversed(lab.serving(None, ("contributor",))))
    if sites:
        order = [name for name in order if lab.site(name) in sites]
    if nodes:
        unknown = [name for name in nodes if name not in order]
        if unknown:
            raise Refused(f"not a serving node of the {tier} tier: {', '.join(unknown)}")
        order = [name for name in order if name in nodes]
    return order


# ------------------------------------------------------------ marks, journal

@contextlib.contextmanager
def signals_held():
    """SIGINT, SIGTERM and SIGHUP wait (blocked in the kernel) while a mark is
    written and its label recorded as open or closed, so an interrupt never
    falls between the two: a label is open exactly when its start was
    written, and `close` writes every end once."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK,
                                      {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class Marks:
    """`start <unix seconds, nine decimals> <label>` and `end ...`: the
    change's two on stdout (flushed) and in `--marks FILE`; per-node marks in
    `--node-marks FILE` and on stderr as `mark start ...`."""

    def __init__(self, out=None, err=None, marks_file=None, node_marks_file=None,
                 clock_ns=time.time_ns):
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.marks_file = pathlib.Path(marks_file) if marks_file else None
        self.node_marks_file = pathlib.Path(node_marks_file) if node_marks_file else None
        self.clock_ns = clock_ns
        self.open: list[str] = []
        self.node_open: list[str] = []

    @staticmethod
    def line(kind: str, ns: int, label: str) -> str:
        if not label or any(char.isspace() for char in label):
            raise ValueError(f"a mark's label has no spaces: {label!r}")
        return f"{kind} {ns // 1_000_000_000}.{ns % 1_000_000_000:09d} {label}"

    @staticmethod
    def _append(path, text: str) -> None:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as handle:
                handle.write(text + "\n")

    def _change(self, kind: str, label: str) -> None:
        text = self.line(kind, self.clock_ns(), label)
        self.out.write(text + "\n")
        self.out.flush()
        self._append(self.marks_file, text)

    def _node(self, kind: str, label: str) -> None:
        text = self.line(kind, self.clock_ns(), label)
        self.err.write(f"mark {text}\n")
        self.err.flush()
        self._append(self.node_marks_file, text)

    def start(self, label: str) -> None:
        with signals_held():
            self._change("start", label)
            self.open.append(label)

    def end(self, label: str) -> None:
        with signals_held():
            self._change("end", label)
            self.open.remove(label)

    def node_start(self, label: str) -> None:
        with signals_held():
            self._node("start", label)
            self.node_open.append(label)

    def node_end(self, label: str) -> None:
        with signals_held():
            self._node("end", label)
            self.node_open.remove(label)

    def close(self) -> None:
        """Every label still open gets its end: per-node ones, then the
        change's."""
        for label in reversed(list(self.node_open)):
            self.node_end(label)
        for label in reversed(list(self.open)):
            self.end(label)


class Journal:
    """`rolls/<command>-<unix>.state.json`: the plan, the nodes done (with
    what they ran before), the node in hand and its phase (`in_hand`), the
    other nodes the command left in a phase that is not clean (`left`: a
    node that failed before a rollback-all, a replace's slot), the surge
    edges, the open label, and whether the command finished: `finished` is
    true only when no node is left. Written whole and renamed into place on
    every change."""

    def __init__(self, path: pathlib.Path, data: dict):
        self.path = pathlib.Path(path)
        self.data = data

    @classmethod
    def create(cls, directory, command: str, unix: int, **fields) -> "Journal":
        directory = pathlib.Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{command}-{unix}.state.json"
        suffix = 1
        while path.exists():
            path = directory / f"{command}-{unix}-{suffix}.state.json"
            suffix += 1
        journal = cls(path, {"command": command, "unix": unix, "finished": False, "done": [],
                             "before": {}, "in_hand": None, "left": {}, "surge": {},
                             **fields})
        journal.write()
        return journal

    def update(self, **fields) -> None:
        self.data.update(fields)
        self.write()

    def write(self) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(temporary, self.path)

    @staticmethod
    def unfinished(directory, command: str | None = None) -> list["Journal"]:
        """Journals of commands that did not finish, newest first: a node a
        dead roll left in hand is named here (the deploy's `status`)."""
        directory = pathlib.Path(directory)
        if not directory.is_dir():
            return []
        out = []
        for path in directory.glob("*.state.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if data.get("finished") is False and command in (None, data.get("command")):
                out.append(Journal(path, data))
        return sorted(out, key=lambda journal: (journal.data.get("unix", 0), journal.path.name),
                      reverse=True)

    @staticmethod
    def hands(data: dict) -> list[dict]:
        """Every node a journal names: the node in hand, then those left."""
        return ([data["in_hand"]] if data.get("in_hand") else []) + \
            list((data.get("left") or {}).values())


class OpsLock:
    """`<lab state>.ops.lock`: one change command or controller at a time.
    The holder writes a token into the file; a command it starts carries the
    token in NEEDLETAIL_OPS_LOCK_TOKEN and proceeds under its lock."""

    ENV = "NEEDLETAIL_OPS_LOCK_TOKEN"

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.handle = None
        self.token: str | None = None

    def acquire(self) -> "OpsLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            held = handle.read().strip()
            handle.close()
            given = os.environ.get(self.ENV)
            if given and given == held:
                self.token = given
                return self
            raise Refused(f"another change command or controller holds {self.path}") from None
        self.token = uuid.uuid4().hex
        handle.seek(0)
        handle.truncate()
        handle.write(self.token)
        handle.flush()
        self.handle = handle
        return self

    def env(self) -> dict:
        """The environment a command started by the holder needs."""
        return {self.ENV: self.token}

    def release(self) -> None:
        if self.handle is not None:
            self.handle.seek(0)
            self.handle.truncate()
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


# ------------------------------------------------------------------- the seam

def contributor_rules():
    """Workstream A's contributor gate and readiness (A 2.10), the one place
    the engine reaches them: `contrib_gate(ops, node, *, timeout_s, poll_s,
    clock, sleep, video_gap)` and `contrib_ready(ops, node, *, timeout_s,
    poll_s, clock, sleep)`, each returning (ok, why, evidence) after its own
    polling. Imported here, not at the top: contributor_changes imports this
    module."""
    import contributor_changes
    return contributor_changes.contrib_gate, contributor_changes.contrib_ready


# ---------------------------------------------------------------- the checks

PARENT_FIELDS = ("source_age_ms", "window_s", "skipped_10s", "resets_10s", "objects_10s",
                 "slack_mean_us_10s", "slack_min_us_10s")
LEAVING_FIELDS = ("objects_1s", "objects_10s", "slack_mean_us_10s", "slack_min_us_10s")


def feed_parity(child: Status | None, stream_id: int, leaving, staying, parity_ms: float = 1.0,
                repair_lane: bool = False) -> list[str]:
    """P1 to P4 of 3.3 on the child's own record: may the parent at
    `leaving` (addresses) go, with the parent at `staying` left to feed
    `stream_id` to this child? [] passes; every problem is named, a missing
    figure as "parity unknown". `repair_lane`: the staying parent feeds the
    child on a repair lane (a compiled child's secondary)."""
    if child is None:
        return ["parity unknown: the child does not answer"]
    x = parent_row(child, set(leaving), stream_id)
    p = parent_row(child, set(staying), stream_id)
    if x is None:
        return [f"parity unknown: no row for the leaving parent, stream {stream_id}"]
    if p is None:
        return [f"parity unknown: no row for the other parent, stream {stream_id}"]
    missing = [f"other.{name}" for name in PARENT_FIELDS if _num(p, name) is None]
    missing += [f"leaving.{name}" for name in LEAVING_FIELDS if _num(x, name) is None]
    if repair_lane and _num(x, "multi_symbol_10s") is None:
        missing.append("leaving.multi_symbol_10s")
    if missing:
        return [f"parity unknown: {', '.join(missing)} (stream {stream_id})"]
    problems = []
    if p["source_age_ms"] > LIVE_MS:
        problems.append(f"P1 live: the other parent's last source is {p['source_age_ms']} ms old")
    if p["window_s"] < WINDOW_S:
        problems.append(f"P2 complete: {p['window_s']} s of record, {WINDOW_S} wanted")
    if p["skipped_10s"] != 0:
        problems.append(f"P2 complete: the other parent skipped {p['skipped_10s']} objects in 10 s")
    if p["resets_10s"] != 0:
        problems.append(f"P2 complete: the other parent's feed reset {p['resets_10s']} times")
    behind = x["objects_10s"] - x["objects_1s"] / 10
    if p["objects_10s"] < behind:
        problems.append(f"P2 complete: the other parent delivered {p['objects_10s']} objects in "
                        f"10 s, the leaving one {x['objects_10s']}")
    parity_us = parity_ms * 1000
    if p["slack_mean_us_10s"] < x["slack_mean_us_10s"] - parity_us:
        problems.append(f"P3 on time: mean slack {p['slack_mean_us_10s']} us against "
                        f"{x['slack_mean_us_10s']} us of the leaving parent")
    if p["slack_min_us_10s"] < x["slack_min_us_10s"] - parity_us:
        problems.append(f"P3 on time: worst slack {p['slack_min_us_10s']} us against "
                        f"{x['slack_min_us_10s']} us of the leaving parent")
    if repair_lane and x["multi_symbol_10s"] != 0:
        problems.append(f"P4 whole objects: {x['multi_symbol_10s']} audio objects of more than one "
                        "symbol, which a repair lane does not carry")
    return problems


def _idle(status: Status) -> bool:
    """No parent of the node delivered in the last 10 s (every figure there)."""
    rows = [row for parent in status.parents or [] for row in parent.rows.values()]
    return all(_num(row, "objects_10s") == 0 for row in rows)


def _fresh_caches(document) -> int | None:
    if not isinstance(document, dict) or not isinstance(document.get("caches"), list):
        return None
    return sum(1 for cache in document["caches"] if isinstance(cache, dict) and cache.get("fresh"))


def forwarder_scope(lab: Lab, x: str) -> list[str]:
    """Every node the forwarder gate reads: X, its site's serving nodes (the
    lease children and their other parents), its compiled children and
    their other parents."""
    names = {x} | set(lab.serving(lab.site(x), ("edge", "distributor", "backbone")))
    for child in lab.compiled_children(x):
        names.add(child)
        other = lab.other_parent(child, x)
        if other:
            names.add(other)
    return sorted(names, key=natural)


def forwarder_check(lab: Lab, x: str, statuses: dict, previous: dict | None,
                    assignments: dict | None = None, parity_ms: float = 1.0):
    """One poll of the forwarder gate (3.3): (problems, evidence, pairs).
    `statuses` {name: Status or None} of `forwarder_scope`; `previous` the
    poll before (for the deadline-drop checks LG4, CG4); `assignments`
    {name: (status, JSON)} of `/api/assignment` on the site's two first
    distributors when X is the first of them. `pairs` are the audio lease
    pairs (child, address, stream, fed) seen at X, kept for R4; `fed`: X
    feeds the child that stream (X's row at the child has objects_1s > 0).

    A lease pair that X does not feed (no row for X at the child, or
    objects_1s 0: a playlist held with no source, or a build that keeps its
    leases and forwards nothing) cannot be harmed by X's stop: it is judged
    as an idle mesh is (LG1 and some other parent `ready`) and listed in
    the evidence as not fed. A row for X without objects_1s is "parity
    unknown"."""
    problems: list[str] = []
    evidence: dict = {"lease": [], "compiled": [], "video": [], "not_fed": []}
    pairs: list = []
    sx = statuses.get(x)
    if sx is None:
        return [f"{x} does not answer /api/node"], evidence, pairs
    if sx.parents is None:
        problems.append(f"parity unknown: {x}'s parents[]")
        idle = False
    else:
        idle = _idle(sx)
    evidence["idle"] = idle
    x_addresses = lab.addresses(x)

    def drops_held(name: str, status: Status, label: str) -> None:
        if status.audio_deadline_drops is None:
            problems.append(f"{label}: parity unknown: {name}'s audio_deadline_drops")
            return
        before = (previous or {}).get(name)
        if before is not None and before.audio_deadline_drops != status.audio_deadline_drops:
            problems.append(f"{label}: {name} dropped "
                            f"{status.audio_deadline_drops - (before.audio_deadline_drops or 0)} "
                            "audio objects past their deadline during the gate")

    # Lease children.
    if sx.lease_pairs is None:
        problems.append(f"parity unknown: {x}'s children.lease.pairs_list")
    for child_address, stream_id in sx.lease_pairs or []:
        child = lab.name_of(child_address)
        if child is None:
            problems.append(f"LG1: lease child {child_address} is not a node of the lab")
            continue
        sc = statuses.get(child)
        if sc is None:
            problems.append(f"LG1: {child} does not answer /api/node")
            continue
        if sc.state == "stopping":
            problems.append(f"LG1: {child} is stopping")
            continue
        if sc.parents is None:
            problems.append(f"parity unknown: {child}'s parents[]")
            continue
        own_row = parent_row(sc, x_addresses, stream_id)
        if own_row is not None and _num(own_row, "objects_1s") is None:
            problems.append(f"parity unknown: {child}'s row for {x} has no objects_1s "
                            f"(stream {stream_id})")
            continue
        fed = own_row is not None and own_row["objects_1s"] > 0
        kind = sx.stream_class(stream_id) or (own_row or {}).get("class")
        if fed and kind is None:
            problems.append(f"parity unknown: the class of stream {stream_id} at {child}")
            continue
        if kind is not None and kind != "audio":
            evidence["video"].append({"child": child, "stream_id": stream_id})
            continue
        if not fed and not idle:
            evidence["not_fed"].append({"child": child, "stream_id": stream_id})
        pairs.append((child, child_address, stream_id, fed))
        # Judged as an idle mesh is when X does not feed this pair.
        light = idle or not fed
        others = [parent for parent in sc.parents if parent.address not in x_addresses]
        candidates = [lab.name_of(parent.address) for parent in others]
        if light and not others:
            candidates = [name for name in lab.distributors(lab.site(x)) if name != x]
        found, reasons = False, []
        for other in candidates:
            if other is None:
                reasons.append(f"LG3: a parent of {child} is not a node of the lab")
                continue
            sp = statuses.get(other)
            failed = []
            if not light:
                failed += [f"LG2 {child} <- {other} {stream_id}: {item}" for item in
                           feed_parity(sc, stream_id, x_addresses, lab.addresses(other), parity_ms)]
            if sp is None:
                failed.append(f"LG3: {other} does not answer /api/node")
            elif sp.ready is not True:
                failed.append(f"LG3: {other} is not ready ({sp.state})")
            elif not light:
                if sp.hello_draining is not False:
                    failed.append(f"LG3: {other}'s hellos say draining (or say nothing)")
                if sp.lease_pairs is None or (child_address, stream_id) not in sp.lease_pairs:
                    failed.append(f"LG3: {other} does not hold the lease of {child} for "
                                  f"{stream_id}")
                fed = (sp.lease_fed_age_ms or {}).get(stream_id)
                if fed is None or fed > LIVE_MS:
                    failed.append(f"LG3: {other} fed {stream_id} {fed} ms ago")
                fed_rows = [parent.rows.get((stream_id, "audio")) for parent in sp.parents or []]
                fed_rows = [row for row in fed_rows if row is not None]
                if not any(_num(row, "source_age_ms") is not None
                           and row["source_age_ms"] <= LIVE_MS
                           and _num(row, "skipped_10s") == 0 for row in fed_rows):
                    failed.append(f"LG4: {other} is not itself fed {stream_id} live and complete")
                if sp.audio_deadline_drops is None:
                    failed.append(f"LG4: parity unknown: {other}'s audio_deadline_drops")
                elif (previous or {}).get(other) is not None and \
                        previous[other].audio_deadline_drops != sp.audio_deadline_drops:
                    failed.append(f"LG4: {other} dropped audio past its deadline during the gate")
            if failed:
                reasons += failed
            else:
                found = True
                break
        if not candidates:
            reasons.append(f"LG2: {child} has no other parent of {stream_id}")
        evidence["lease"].append({"child": child, "stream_id": stream_id, "passed": found,
                                  "reasons": [] if found else reasons})
        if not found:
            problems += reasons

    # Compiled long-haul children.
    children = lab.compiled_children(x)
    for child in children:
        other = lab.other_parent(child, x)
        sd = statuses.get(child)
        if sd is None:
            problems.append(f"CG1: {child} does not answer /api/node")
            continue
        if sd.ready is not True:
            problems.append(f"CG1: {child} is not ready ({sd.state})")
        entry = {"child": child, "other": other, "streams": []}
        if not idle:
            x_rows = next((parent.rows for parent in sd.parents or []
                           if parent.address in x_addresses), None)
            if x_rows is None:
                problems.append(f"parity unknown: {child} has no row for {x}")
            for (stream_id, track), row in sorted((x_rows or {}).items()):
                if _num(row, "objects_1s") is None:
                    problems.append(f"parity unknown: {child}'s row for {x} has no objects_1s")
                    continue
                if row["objects_1s"] <= 0:
                    continue
                if track != "audio":
                    evidence["video"].append({"child": child, "stream_id": stream_id})
                    continue
                repair = lab.records[child].get("secondary") == other
                staying = lab.addresses(other) if other else set()
                failed = feed_parity(sd, stream_id, x_addresses, staying, parity_ms,
                                     repair_lane=repair)
                entry["streams"].append({"stream_id": stream_id, "problems": failed})
                problems += [f"CG2 {child} <- {other} {stream_id}: {item}" for item in failed]
        sy = statuses.get(other) if other else None
        if idle:
            pass
        elif sy is None:
            problems.append(f"CG4: {other} does not answer /api/node")
        else:
            if sy.ready is not True:
                problems.append(f"CG4: {other} is not ready ({sy.state})")
            if sy.threads is None or sy.threads != sy.threads_expected:
                problems.append(f"CG4: {other} runs {sy.threads}/{sy.threads_expected} "
                                "class threads")
            drops_held(other, sy, "CG4")
        evidence["compiled"].append(entry)
    if sx.compiled_targets is None:
        problems.append(f"parity unknown: {x}'s children.compiled[]")
    else:
        running = {lab.name_of(target) or target for target in sx.compiled_targets}
        if running != set(children):
            problems.append(f"CG3: {x} forwards to {sorted(running)}, the tree names {children}")

    # The site's entry.
    firsts = lab.first_distributors(lab.site(x))
    if firsts and firsts[0] == x and len(firsts) > 1:
        sibling = firsts[1]
        code, document = (assignments or {}).get(sibling, (None, None))
        mine = _fresh_caches((assignments or {}).get(x, (None, None))[1])
        theirs = _fresh_caches(document)
        if code != 200 or theirs is None:
            problems.append(f"entry: {sibling}'s /api/assignment answered {code}")
        elif mine is None:
            problems.append(f"entry: parity unknown: {x}'s /api/assignment")
        elif theirs < mine:
            problems.append(f"entry: {sibling} lists {theirs} fresh caches, {x} {mine}")
    evidence["video_children"] = len(evidence["video"])
    return problems, evidence, pairs


@dataclasses.dataclass
class Gate:
    ok: bool
    problems: list
    evidence: dict
    pairs: list
    last_poll: float
    polls: int


def _poll_statuses(ops: Ops, names) -> dict:
    return {name: status_of(document) for name, document in ops.statuses(list(names)).items()}


def forwarder_gate(ops: Ops, x: str, *, lab: Lab | None = None, timeout_s: float = 30.0,
                   parity_ms: float = 1.0, poll_s: float = POLL_S, clock=time.time,
                   sleep=time.sleep) -> Gate:
    """May forwarder X stop now (3.3): passes on two polls in a row that both
    pass, fails after `timeout_s` with the last poll's problems."""
    lab = lab or Lab(ops.nodes())
    scope = forwarder_scope(lab, x)
    firsts = lab.first_distributors(lab.site(x))
    entry = firsts[:2] if firsts and firsts[0] == x else []
    deadline = clock() + timeout_s
    previous, previous_ok, polls = None, False, 0
    while True:
        began = clock()
        statuses = _poll_statuses(ops, scope)
        assignments = {name: ops.get(name, "/api/assignment") for name in entry}
        polls += 1
        problems, evidence, pairs = forwarder_check(lab, x, statuses, previous, assignments,
                                                    parity_ms)
        now = clock()
        if not problems and previous_ok:
            return Gate(True, [], evidence, pairs, now, polls)
        previous_ok, previous = not problems, statuses
        if now >= deadline:
            return Gate(False, problems or ["the gate did not pass twice in a row"], evidence,
                        pairs, now, polls)
        sleep(max(0.0, poll_s - (now - began)))


def leg_cost(lab: Lab, site: str, statuses: dict) -> float | None:
    """Permille per leg: the median over the site's serving edges with at
    least 10 legs of load_permille / subscribers. None when no edge carries
    10 legs (a region that light has room for every leg anywhere)."""
    ratios = []
    for name in lab.edges(site):
        status = statuses.get(name)
        legs = status.udp("subscribers") if status else None
        load = status.udp("load_permille") if status else None
        if legs is not None and load is not None and legs >= COST_MIN_LEGS:
            ratios.append(load / legs)
    return statistics.median(ratios) if ratios else None


def capacity_check(lab: Lab, x: str, statuses: dict, cost: float | None,
                   one_path: bool = False):
    """One poll of the edge capacity gate (3.5 with B's seam): every other
    serving edge is ready; the others together have room for X's legs; and
    for every other edge E, the edges other than X and E have room for the
    legs of X whose listener's other leg is on E (X's `legs_by_other[E]`:
    those legs can go to neither). `one_path` (`--surge never`): a leg whose
    listener has its other leg elsewhere is left to that one path, and only
    X's single legs (`legs_by_other[""]`) need room. (problems, evidence)."""
    site = lab.site(x)
    others = [name for name in lab.edges(site) if name != x]
    problems = []
    sx = statuses.get(x)
    if sx is None:
        return [f"{x} does not answer /api/node"], {}
    legs = sx.udp("subscribers")
    by_other = (sx.client_udp or {}).get("legs_by_other")
    if legs is None:
        problems.append(f"capacity unknown: {x}'s client_udp.subscribers")
    if not isinstance(by_other, dict):
        problems.append(f"capacity unknown: {x}'s client_udp.legs_by_other")
        by_other = {}
    room, single = {}, 0
    single_known = sx.udp("single_legs") is not None
    single += sx.udp("single_legs") or 0
    for name in others:
        status = statuses.get(name)
        if status is None:
            problems.append(f"{name} does not answer /api/node")
            continue
        if status.ready is not True:
            problems.append(f"{name} is not ready ({status.state})")
        held = status.udp("subscribers")
        if held is None:
            problems.append(f"capacity unknown: {name}'s client_udp.subscribers")
            continue
        if status.udp("single_legs") is None:
            single_known = False
        single += status.udp("single_legs") or 0
        room[name] = None if cost is None else (REBALANCE_TARGET_PERMILLE - held * cost) / cost
    if not single_known:
        problems.append(f"capacity unknown: client_udp.single_legs in {site}")
    evidence = {"legs": legs, "legs_by_other": by_other, "cost": cost, "room": room,
                "single_legs": single if single_known else None}
    if one_path:
        legs = by_other.get("", 0) if isinstance(by_other.get("", 0), int) else None
        if legs is None:
            problems.append(f"capacity unknown: {x}'s legs_by_other[\"\"]")
        by_other = {}
    if problems or not legs:
        return problems, evidence
    if cost is None:
        if legs >= COST_MIN_LEGS:
            problems.append(f"capacity unknown: {x} holds {legs} legs and no edge gives a cost")
        return problems, evidence
    if sum(room.values()) < legs:
        problems.append(f"capacity: the other edges have room for {sum(room.values()):.0f} legs, "
                        f"{x} holds {legs}")
    for name in others:
        shared = by_other.get(name, 0)
        rest = sum(value for other, value in room.items() if other != name)
        if shared and rest < shared:
            problems.append(f"capacity: {shared} legs of {x} have their other leg on {name}; "
                            f"the rest of {site} has room for {rest:.0f}")
    return problems, evidence


def edge_gate(ops: Ops, x: str, *, lab: Lab | None = None, cost: float | None = None,
              one_path: bool = False, timeout_s: float = 30.0, poll_s: float = POLL_S,
              clock=time.time, sleep=time.sleep) -> Gate:
    """May edge X be drained now: `capacity_check` on two polls in a row."""
    lab = lab or Lab(ops.nodes())
    scope = lab.edges(lab.site(x))
    if x not in scope:
        scope = scope + [x]
    deadline = clock() + timeout_s
    previous_ok, polls = False, 0
    while True:
        began = clock()
        statuses = _poll_statuses(ops, scope)
        polls += 1
        problems, evidence = capacity_check(lab, x, statuses, cost, one_path)
        now = clock()
        if not problems and previous_ok:
            return Gate(True, [], evidence, [], now, polls)
        previous_ok = not problems
        if now >= deadline:
            return Gate(False, problems or ["the gate did not pass twice in a row"], evidence,
                        [], now, polls)
        sleep(max(0.0, poll_s - (now - began)))


@dataclasses.dataclass
class Ready:
    ok: bool
    cause: str | None          # "build" (R1, R2, R4) or "other"
    problems: list
    answered: bool
    status: Status | None


def readiness_check(lab: Lab, name: str, statuses: dict, *, expected: dict, before_boot: int,
                    activation_us: int, before: dict | None = None, gate_pairs=(),
                    accept_operator_drain: bool = True) -> dict:
    """{check: [problems]} of R1 to R6 (3.4) on one poll; an empty list
    passes. R7 (an edge resumed and listed) is the engine's, after its
    resume."""
    tier = lab.tier(name)
    status = statuses.get(name)
    out = {"R1": [], "R2": []}
    if status is None:
        out["R1"].append(f"{name} does not answer /api/node")
        return out
    floor_us = activation_us - CLOCK_LIMIT_S * 1_000_000
    if status.boot_id is None or status.boot_id <= before_boot or status.boot_id < floor_us:
        out["R1"].append(f"boot id {status.boot_id} is not later than the activation")
    if (status.build_git, status.build_unix) != (expected.get("git"), expected.get("unix")):
        out["R1"].append(f"build {status.build_git} {status.build_unix}, expected "
                         f"{expected.get('git')} {expected.get('unix')}")
    if status.args_sha256 != expected.get("args_sha256"):
        out["R1"].append("args_sha256 differs from the rendered arguments")
    if tier == "contributors":
        # A 2.10: a contributor that hears no source is standing_by, never
        # ready; contrib_ready then decides whether that is allowed.
        if status.state != "ready" and not idle_contributor(status):
            out["R2"].append(f"state {status.state}")
        return out
    if status.threads is None or status.threads_expected is None:
        out["R2"].append("parity unknown: fast_plane.threads")
    elif status.threads != status.threads_expected:
        out["R2"].append(f"class_threads {status.threads}/{status.threads_expected}")
    if status.not_ready is None:
        out["R2"].append("parity unknown: not_ready")
    else:
        out["R2"] += [reason for reason in status.not_ready
                      if reason.startswith(LEVEL_REASONS[tier])]
    if tier == "forwarders" and (status.received_1s or 0) > 0 and status.admitted_1s == 0:
        out["R2"].append("peers not admitted")
    allowed = {OPERATOR_DRAIN} if accept_operator_drain else set()
    if tier == "edges":
        out["R6"] = []
        if status.warm is not True:
            out["R6"].append("not warm")
        if status.ready_forced is not False:
            out["R6"].append(f"ready_forced {status.ready_forced}")
        if status.not_ready is not None and not set(status.not_ready) <= allowed:
            out["R6"].append(f"not ready: {status.not_ready}")
        return out
    out.update({"R3": [], "R4": [], "R5": []})
    if status.warm is not True or status.state != "ready":
        out["R3"].append(f"warm {status.warm}, state {status.state}")
    addresses = lab.addresses(name)
    for child, child_address, stream_id, fed in gate_pairs:
        held_elsewhere = any(
            other != name and statuses.get(other) is not None
            and (child_address, stream_id) in (statuses[other].lease_pairs or [])
            for other in lab.distributors(lab.site(name)))
        if not held_elsewhere:
            continue
        if (child_address, stream_id) not in (status.lease_pairs or []):
            out["R4"].append(f"{child}'s lease of {stream_id} is not back")
            continue
        if not fed:
            # The node fed this pair nothing before its restart either.
            continue
        row = parent_row(statuses.get(child), addresses, stream_id)
        if row is None or _num(row, "source_age_ms") is None or row["source_age_ms"] > LIVE_MS:
            out["R4"].append(f"{child} is not fed {stream_id} by {name}")
    for child in lab.compiled_children(name):
        other = lab.other_parent(child, name)
        sd = statuses.get(child)
        if sd is None:
            out["R4"].append(f"{child} does not answer /api/node")
            continue
        other_rows = next((parent.rows for parent in sd.parents or []
                           if other and parent.address in lab.addresses(other)), {})
        for (stream_id, track), row in other_rows.items():
            if track != "audio" or _num(row, "source_age_ms") is None or \
                    row["source_age_ms"] > LIVE_MS:
                continue
            mine = parent_row(sd, addresses, stream_id)
            if mine is None or _num(mine, "source_age_ms") is None or \
                    mine["source_age_ms"] > LIVE_MS:
                out["R4"].append(f"{child} is not fed {stream_id} by {name}")
    if status.warm_parts_pending != 0:
        out["R5"].append(f"warm_parts_pending {status.warm_parts_pending}")
    fresh_before = (before or {}).get("caches_fresh")
    if fresh_before is not None and (status.caches_fresh is None
                                     or status.caches_fresh < fresh_before):
        out["R5"].append(f"entry lists {status.caches_fresh} fresh caches, {fresh_before} before")
    return out


BUILD_CHECKS = ("R1", "R2", "R4")


def wait_ready(ops: Ops, name: str, *, expected: dict, before_boot: int, activation_us: int,
               lab: Lab | None = None, before: dict | None = None, gate_pairs=(),
               restarts_before: int | None = None, timeout_s: float = 60.0,
               poll_s: float = POLL_S, clock=time.time, sleep=time.sleep) -> Ready:
    """Poll until every check of 3.4 for the node's tier holds at once, then
    the unit's NRestarts must not have risen; else, at `timeout_s`, the
    cause: "build" when R1, R2 or R4 fails, "other" otherwise. An NRestarts
    the backend cannot read is no evidence against the build: it fails the
    wait as "unit", cause "other"."""
    lab = lab or Lab(ops.nodes())
    scope = forwarder_scope(lab, name) if lab.tier(name) == "forwarders" else [name]
    deadline = clock() + timeout_s
    answered, last = False, {}
    while True:
        began = clock()
        statuses = _poll_statuses(ops, scope)
        answered = answered or statuses.get(name) is not None
        last = readiness_check(lab, name, statuses, expected=expected, before_boot=before_boot,
                               activation_us=activation_us, before=before, gate_pairs=gate_pairs)
        if not any(last.values()):
            restarts = ops.unit_restarts(name)
            if restarts is None or restarts_before is None:
                last["unit"] = ["parity unknown: the unit's NRestarts"]
            elif restarts > restarts_before:
                last["R1"].append(f"the unit restarted {restarts - restarts_before} times")
            else:
                return Ready(True, None, [], True, statuses.get(name))
        now = clock()
        if now >= deadline:
            failing = [f"{check}: {item}" for check, items in last.items() for item in items]
            cause = "build" if any(last.get(check) for check in BUILD_CHECKS) else "other"
            return Ready(False, cause, failing, answered, statuses.get(name))
        sleep(max(0.0, poll_s - (now - began)))


# ------------------------------------------------------------------- engine

@dataclasses.dataclass
class Options:
    force: bool = False
    dry: bool = False
    allow_dirty: bool = False
    gate_timeout_s: float = 30.0
    drain_timeout_s: float = 30.0
    ready_timeout_s: float = 60.0
    on_failure: str = "stop"           # stop | rollback-all
    surge: str = "auto"                # auto | never
    parity_ms: float = 1.0
    video_gap: str = "refuse"          # refuse | accept
    hold_s: float = 0.0
    rollback: bool = False             # restart --rollback: .args.prev in force
    poll_s: float = POLL_S
    marks_file: str | None = None
    node_marks_file: str | None = None


def typical_times(directory, tier: str) -> dict:
    """The dry mode's sleeps: the medians of each phase in the newest real
    command's evidence that has nodes of this tier, else DRY_TYPICAL_S."""
    directory = pathlib.Path(directory)
    files = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True) \
        if directory.is_dir() else []
    for path in files:
        if path.name.endswith(".state.json"):
            continue
        try:
            evidence = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if evidence.get("dry") is not False:
            continue
        times: dict[str, list] = {}
        for node in (evidence.get("nodes") or {}).values():
            if node.get("tier") == tier and node.get("result") == "done":
                for phase, seconds in (node.get("phases") or {}).items():
                    times.setdefault(phase, []).append(seconds)
        if times and set(DRY_TYPICAL_S[tier]) <= set(times):
            return {phase: statistics.median(times[phase]) for phase in DRY_TYPICAL_S[tier]}
    return dict(DRY_TYPICAL_S[tier])


class Engine:
    """One change command: `roll`, `restart_nodes` or `replace`. The
    module's functions of those names make one and run it.

    Bookkeeping: `hand` is the node in hand and its phase, journalled
    before each step. Taking another node in hand moves a hand that is not
    in a clean phase to `left`; the command is finished (its journal
    `finished`) only when no node is in hand or left outside a clean phase.
    Every node left is named on stderr at the end, with the command that
    finishes it (`resume_command`)."""

    def __init__(self, ops: Ops, command: str, options: Options, out=None, err=None,
                 clock=time.time, sleep=time.sleep):
        self.ops = ops
        self.command = command
        self.options = options
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.clock = clock
        self.sleep = sleep
        clock_ns = time.time_ns if clock is time.time else (lambda: int(round(clock() * 1e9)))
        self.marks = Marks(self.out, self.err, options.marks_file, options.node_marks_file,
                           clock_ns)
        self.lab: Lab | None = None
        self.journal: Journal | None = None
        self.evidence: dict = {"command": command, "dry": options.dry, "nodes": {}}
        self.rules = None
        self.costs: dict[str, float | None] = {}
        self.single_baseline: dict[str, int] = {}
        self.surge: dict[str, str] = {}
        self.surge_staged: dict[str, str] = {}
        self.hand: dict | None = None
        self.left: dict[str, dict] = {}
        self.before: dict[str, dict] = {}
        self.done: list[str] = []
        self.seen_ready: set[str] = set()
        self.cleaning = False
        self.hurry = False
        self.resume_command = command
        # Nodes a resumed command finishes first, or a restart --rollback
        # names: not held to the readiness precondition.
        self.excluded: set[str] = set()

    # ---------------------------------------------------------- plumbing
    def say(self, text: str) -> None:
        self.err.write(text + "\n")
        self.err.flush()

    def now_us(self) -> int:
        return int(self.clock() * 1_000_000)

    def poll(self, names) -> dict:
        return _poll_statuses(self.ops, names)

    def status(self, name: str) -> Status | None:
        return status_of(self.ops.status(name))

    def record(self, name: str) -> dict:
        """The node's entry in the evidence."""
        return self.evidence["nodes"].setdefault(name, {"tier": self.lab.tier(name), "phases": {},
                                                        "events": []})

    @staticmethod
    def unclean(hand: dict | None) -> bool:
        return hand is not None and hand.get("phase") not in CLEAN_PHASES

    def is_clean(self) -> bool:
        return not self.unclean(self.hand) and not self.left

    def write_journal(self) -> None:
        if self.journal:
            self.journal.update(in_hand=self.hand, left=self.left, done=self.done,
                                before=self.before, surge=self.surge)

    def phase(self, name: str, phase: str, **fields) -> None:
        """The node in hand and its phase, in the journal before the step. A
        node that was in hand in a phase that is not clean is kept in
        `left`."""
        current = self.hand
        if current is not None and current.get("node") == name:
            hand = dict(current)
        else:
            if self.unclean(current):
                self.left[current["node"]] = current
            hand = dict(self.left.pop(name, None) or {})
        hand.update(fields, node=name, tier=self.lab.tier(name), phase=phase)
        self.hand = hand
        self.record(name)["events"].append([round(self.clock(), 3), phase])
        self.write_journal()

    def hold(self, name: str, phase: str, **fields) -> None:
        """A node the command leaves in a state while it works on another
        (a replace's slot): named in `left` until released."""
        entry = dict(self.left.get(name) or {})
        entry.update(fields, node=name, tier=self.lab.tier(name), phase=phase)
        self.left[name] = entry
        self.record(name)["events"].append([round(self.clock(), 3), phase])
        self.write_journal()

    def timed(self, name: str, phase: str, began: float) -> None:
        phases = self.record(name)["phases"]
        phases[phase] = round(phases.get(phase, 0.0) + self.clock() - began, 3)

    def release_hand(self, name: str, result: str) -> None:
        self.record(name)["result"] = result
        if self.hand is not None and self.hand.get("node") == name:
            self.hand = None
        self.left.pop(name, None)
        self.write_journal()

    def snapshot(self, status: Status) -> dict:
        return {"boot_id": status.boot_id, "git": status.build_git, "unix": status.build_unix,
                "args_sha256": status.args_sha256, "caches_fresh": status.caches_fresh}

    @contextlib.contextmanager
    def signals(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        def handler(signum, _frame):
            if self.cleaning:
                self.hurry = True
                return
            raise Interrupted(signal.Signals(signum).name)

        previous = {sig: signal.signal(sig, handler)
                    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            yield
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)

    def write_evidence(self) -> None:
        if not self.journal:
            return
        path = self.journal.path.with_name(self.journal.path.name.replace(".state.json", ".json"))
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(self.evidence, indent=1, sort_keys=True, default=str))
        os.replace(temporary, path)

    # ---------------------------------------------------------- preparation
    def clock_check(self) -> None:
        for site in self.lab.sites():
            names = self.lab.serving(site)
            if not names:
                continue
            offset = self.ops.clock_offset(names[0])
            if offset is None:
                raise Refused(f"the clock of {names[0]} cannot be read")
            if abs(offset) > CLOCK_LIMIT_S:
                raise Refused(f"the clock of {names[0]} is {offset * 1000:.0f} ms from this host's "
                              f"(at most {CLOCK_LIMIT_S * 1000:.0f} ms)")

    def all_ready(self, names) -> dict:
        statuses = self.poll(names)
        # A 2.10: an idle pair (every contributor standing_by) may be
        # changed; one standing_by beside a ready one is "1 of 2".
        idle_pair = all(status is not None and idle_contributor(status)
                        for name, status in statuses.items()
                        if self.lab.tier(name) == "contributors")
        bad = [f"{name} ({'no answer' if status is None else status.state})"
               for name, status in statuses.items() if status is None or (
                   status.ready is not True and not (idle_pair and idle_contributor(status)))]
        if bad:
            raise Refused(f"not every node is ready: {', '.join(bad)}")
        self.seen_ready.update(statuses)
        return statuses

    def video_rule(self, statuses: dict, scope) -> None:
        if self.options.video_gap == "accept":
            return
        for name in scope:
            status = statuses.get(name)
            if status is None or self.lab.tier(name) != "contributors":
                continue
            video = [stream_id for stream_id, entry in (status.streams or {}).items()
                     if entry.get("class") == "video" and entry.get("publishing") is not False]
            if video:
                raise Refused(f"{name} publishes video stream(s) {video}: its restart is a 4K gap "
                              "(one contributor carries video); --video-gap accept goes on")

    def prepare(self, scope: list[str], builds: dict[str, dict | None]) -> dict[str, dict]:
        """Every check before the first node (3.5): exit 2 on any failure.
        Returns {node: expected after activation}."""
        self.ops.check(self.command, scope)
        if any(self.lab.tier(name) == "contributors" for name in scope):
            self.rules = contributor_rules()
        self.clock_check()
        tiers = {self.lab.tier(name) for name in scope}
        tier_nodes = [name for site in self.lab.sites() for name in self.lab.serving(site)
                      if self.lab.tier(name) in tiers and name not in self.excluded]
        statuses = self.all_ready(tier_nodes)
        for tier, build in builds.items():
            if build is not None and build.get("dirty") and not self.options.allow_dirty:
                raise Refused(f"the {tier} build {build.get('git')} is dirty (--allow-dirty)")
        self.video_rule(statuses, scope)
        for name in scope:
            if name not in self.excluded:
                self.before.setdefault(name, self.snapshot(statuses[name]))
        for site in self.lab.sites():
            self.costs[site] = leg_cost(self.lab, site, statuses)
        if not self.options.dry and not self.options.rollback:
            for tier in TIERS:
                names = [name for name in scope if self.lab.tier(name) == tier]
                if names:
                    self.stage(names, builds.get(tier))
        if self.options.dry:
            # Nothing is staged: a node is on the target when it runs the
            # target build with the arguments it runs.
            return {name: {"git": (builds.get(self.lab.tier(name)) or {}).get("git"),
                           "unix": (builds.get(self.lab.tier(name)) or {}).get("unix"),
                           "args_sha256": (self.before.get(name) or {}).get("args_sha256")}
                    for name in scope}
        return {name: self.ops.expected(name, rollback=self.options.rollback) for name in scope}

    def stage(self, names: list[str], build: dict | None) -> None:
        try:
            self.ops.stage(names, build)
        except Refused:
            raise
        except Exception as error:  # noqa: BLE001 - staging writes files only
            raise Refused(f"staging {', '.join(names)} failed: {error}") from None

    def on_target(self, name: str, expected: dict) -> bool:
        before = self.before.get(name) or {}
        return (before.get("git"), before.get("unix"), before.get("args_sha256")) == \
            (expected.get("git"), expected.get("unix"), expected.get("args_sha256"))

    def print_plan(self, order: list[str], skipped: list[str]) -> None:
        total = 0.0
        for name in order:
            if name in skipped:
                self.say(f"plan: {name} skipped (on the target build)")
                continue
            seconds = sum(typical_times(self.ops.evidence_dir, self.lab.tier(name)).values())
            total += seconds
            self.say(f"plan: {name} ({self.lab.tier(name)}, about {seconds:.0f} s)")
        for site, name in sorted(self.surge_staged.items()):
            self.say(f"plan: {name} joins {site} as its surge edge and leaves after its last edge")
        self.say(f"plan: {len(order) - len(skipped)} node(s), about {total:.0f} s"
                 + (" (dry: gates only, nothing restarted)" if self.options.dry else ""))

    # ---------------------------------------------------------- primitives
    def control(self, name: str, kind: str) -> None:
        code = self.ops.post(name, f"/api/control/{kind}", control_body(name))
        if not control_accepted(code):
            raise Stop(EXIT_STOPPED, f"{name} answered {code} to {kind}")

    def resume_node(self, name: str) -> bool:
        """resume-node, reporting rather than raising: used on every way out."""
        try:
            code = self.ops.post(name, "/api/control/resume-node", control_body(name))
        except Exception as error:  # noqa: BLE001
            self.say(f"{name}: resume-node failed: {error}")
            return False
        if not control_accepted(code):
            self.say(f"{name}: resume-node answered {code}")
            return False
        return True

    def give_back(self, name: str) -> bool:
        """resume-node for a node this command drained and did not activate;
        its phase becomes `resumed` when the node accepted it."""
        if self.resume_node(name):
            self.phase(name, "resumed")
            return True
        return False

    def wait_empty(self, name: str, give_back: bool = True) -> float:
        """The four gauges at 0 on polls spanning a second (3.5 step 3); the
        time of the last poll. On timeout: stop (exit 3), the node given back
        (resume-node), except inside a rollback (`give_back` False), where
        it runs the failed build and stays drained."""
        deadline = self.clock() + self.options.drain_timeout_s
        zero_since = None
        while True:
            began = self.clock()
            status = self.status(name)
            values = {gauge_name: status.udp(gauge_name) if status else None
                      for gauge_name in DRAIN_GAUGES}
            now = self.clock()
            if all(value == 0 for value in values.values()):
                zero_since = zero_since if zero_since is not None else now
                if now - zero_since >= EMPTY_HOLD_S:
                    return now
            else:
                zero_since = None
            if now >= deadline:
                why = f"{name} did not empty in {self.options.drain_timeout_s:.0f} s ({values})"
                if not give_back:
                    raise Stop(EXIT_STOPPED, f"{why}; it stays drained")
                raise Stop(EXIT_STOPPED, f"{why}; " + ("resumed" if self.give_back(name) else
                                                      f"it stays drained (resume-node {name})"))
            self.sleep(max(0.0, self.options.poll_s - (now - began)))

    def boot_changed(self, name: str, before_boot: int | None, since_us: int,
                     settle_until: float) -> bool | None:
        """After an activation error: did the node restart? Polls the node
        until `settle_until` (the deadline plus the unit's stop bound: an
        activation that began in time has restarted by then); then asks the
        service manager. None: it cannot be told."""
        while True:
            status = self.status(name)
            if status is not None and status.boot_id is not None:
                if before_boot is None or status.boot_id != before_boot:
                    return True
            if self.clock() >= settle_until:
                break
            self.sleep(self.options.poll_s)
        if status is not None and status.boot_id is not None:
            return False
        return self.ops.boot_changed(name, since_us // 1000)

    def activate(self, name: str, not_after: float, rollback: bool, regate,
                 expected: dict) -> tuple[int, int | None]:
        """Activation with its deadline (3.5): (activation start in µs, the
        unit's NRestarts after it). A refusal as late gates again and tries
        once more; after any other error the boot id decides: changed, go on;
        unchanged, gate again and try once more; never a second restart on a
        guess. A node that was not activated keeps the phase it had (drained,
        held, rollback-gate), and after the second try it is given back or
        named (`not_activated`)."""
        entry = self.hand["phase"] if self.hand and self.hand.get("node") == name else "gate"
        status = self.status(name)
        before_boot = status.boot_id if status else (self.before.get(name) or {}).get("boot_id")
        for attempt in (1, 2):
            start_us = self.now_us()
            self.phase(name, "activating", activation_us=start_us, rollback=rollback,
                       not_after=not_after, before_boot=before_boot, expected=expected,
                       entry=entry)
            began = self.clock()
            try:
                result = self.ops.activate(name, int(not_after * 1000), rollback)
            except Exception as error:  # noqa: BLE001 - the boot id decides
                result = {"ok": False, "why": f"error: {error}"}
            self.timed(name, "activate", began)
            why = str(result.get("why", ""))
            if result.get("ok"):
                self.phase(name, "activated")
                return start_us, self.ops.unit_restarts(name)
            if why == "late":
                self.say(f"{name}: the activation came after its deadline and was refused")
                changed = False
            else:
                self.say(f"{name}: activation: {why}; reading its boot id")
                changed = self.boot_changed(name, before_boot, start_us,
                                            not_after + UNIT_TIMEOUT_STOP_S)
            if changed is True or changed is None:
                self.phase(name, "activated")
                return start_us, self.ops.unit_restarts(name)
            self.phase(name, entry)
            if attempt == 2:
                raise Stop(EXIT_STOPPED, self.not_activated(name, entry, why))
            not_after = regate() + NOT_AFTER_S
        raise AssertionError("unreachable")

    def not_activated(self, name: str, entry: str, why: str) -> str:
        text = f"{name} was not activated twice ({why})"
        if entry in DRAINED_PHASES:
            return text + ("; it runs as before and was resumed" if self.give_back(name) else
                           f"; it runs as before and stays drained (resume-node {name})")
        if entry in ("held", "rollback-gate"):
            return f"{text}; {self.left_message(self.hand)}"
        return text + "; it runs as before"

    def wait_serving(self, name: str) -> None:
        """R7: the node `ready`, and an entry of its region that is not in
        hand lists it fresh and not draining."""
        deadline = self.clock() + SERVING_TIMEOUT_S
        entries = [entry for entry in self.lab.distributors(self.lab.site(name))
                   if entry != name]
        while True:
            status = self.status(name)
            listed = None
            for entry in entries:
                code, document = self.ops.get(entry, "/api/assignment")
                if code == 200 and isinstance(document, dict):
                    cache = next((item for item in document.get("caches") or []
                                  if isinstance(item, dict) and item.get("node_id") == name), None)
                    listed = bool(cache and cache.get("fresh") and not cache.get("draining"))
                    break
            if status is not None and status.state == "ready" and listed:
                return
            if self.clock() >= deadline:
                raise Stop(EXIT_STOPPED, f"R7: {name} is {status.state if status else 'silent'}, "
                                         f"listed by its entry: {listed}")
            self.sleep(self.options.poll_s)

    def wait_single_legs(self, site: str) -> None:
        """B's rule before the next drain of a region (the surge edge's
        leave included): the region's single_legs back at its value before
        the previous step, or 20 s."""
        baseline = self.single_baseline.get(site)
        if baseline is None:
            return
        deadline = self.clock() + SINGLE_LEGS_WAIT_S
        while True:
            statuses = self.poll(self.lab.edges(site))
            values = [status.udp("single_legs") if status else None for status in statuses.values()]
            if None not in values and sum(values) <= baseline:
                return
            if self.clock() >= deadline:
                self.say(f"{site}: single_legs {values} not back at {baseline} in "
                         f"{SINGLE_LEGS_WAIT_S:.0f} s; going on")
                return
            self.sleep(self.options.poll_s)

    def gate_forwarder(self, name: str) -> Gate:
        gate = forwarder_gate(self.ops, name, lab=self.lab, timeout_s=self.options.gate_timeout_s,
                              parity_ms=self.options.parity_ms, poll_s=self.options.poll_s,
                              clock=self.clock, sleep=self.sleep)
        self.record(name)["gate"] = gate.evidence
        return gate

    def gate_edge(self, name: str) -> Gate:
        gate = edge_gate(self.ops, name, lab=self.lab, cost=self.costs.get(self.lab.site(name)),
                         one_path=self.options.surge == "never",
                         timeout_s=self.options.gate_timeout_s, poll_s=self.options.poll_s,
                         clock=self.clock, sleep=self.sleep)
        self.record(name)["gate"] = gate.evidence
        return gate

    def gate_contributor(self, name: str) -> None:
        gate, _ready = self.rules
        ok, why, evidence = gate(self.ops, name, timeout_s=self.options.gate_timeout_s,
                                 poll_s=self.options.poll_s, clock=self.clock, sleep=self.sleep,
                                 video_gap=self.options.video_gap)
        self.record(name)["gate"] = evidence
        if not ok:
            raise Stop(EXIT_STOPPED, f"{name}: the contributor gate refuses: {why}")

    def ready(self, name: str, expected: dict, before_boot: int, activation_us: int,
              restarts: int | None, gate_pairs=()) -> Ready:
        began = self.clock()
        result = wait_ready(self.ops, name, expected=expected, before_boot=before_boot or 0,
                            activation_us=activation_us, lab=self.lab, before=self.before.get(name),
                            gate_pairs=gate_pairs, restarts_before=restarts,
                            timeout_s=self.options.ready_timeout_s, poll_s=self.options.poll_s,
                            clock=self.clock, sleep=self.sleep)
        if result.ok and self.lab.tier(name) == "contributors":
            _gate, contrib_ready = self.rules
            ok, why, evidence = contrib_ready(self.ops, name,
                                              timeout_s=self.options.ready_timeout_s,
                                              poll_s=self.options.poll_s, clock=self.clock,
                                              sleep=self.sleep)
            self.record(name)["contributor_ready"] = evidence
            if not ok:
                result = Ready(False, "build", [f"contributor readiness: {why}"], True,
                               result.status)
        self.timed(name, "ready", began)
        self.record(name)["ready"] = {"ok": result.ok, "cause": result.cause,
                                      "problems": result.problems}
        return result

    # ---------------------------------------------------------- one node
    def change_node(self, name: str, expected: dict, *, rollback: bool = False,
                    undo: bool = False) -> None:
        """Gate, (drain,) activate, readiness, (resume) for one node of any
        tier. `undo`: this activation is itself the way back (rollback-all,
        `restart --rollback`); a failed readiness is not rolled back again."""
        tier = self.lab.tier(name)
        label = f"{self.command}.{tier}.{name}" if self.command == "roll" else \
            f"{self.command}.{name}"
        self.marks.node_start(label)
        status = self.status(name)
        if status is None:
            raise Stop(EXIT_STOPPED, f"{name} does not answer /api/node; nothing done to it")
        self.before.setdefault(name, self.snapshot(status))
        before_boot = status.boot_id
        self.phase(name, "gate", before=self.before.get(name), undo=undo)
        dry = self.options.dry
        typical = typical_times(self.ops.evidence_dir, tier) if dry else {}
        pairs = []
        if tier == "edges":
            site = self.lab.site(name)
            self.wait_single_legs(site)
            gate = self.gate_edge(name)
            if not gate.ok:
                raise Stop(EXIT_STOPPED, f"{name}: the capacity gate refuses: "
                                         f"{'; '.join(gate.problems)}")
            if gate.evidence.get("single_legs") is not None:
                self.single_baseline[site] = gate.evidence["single_legs"]
            began = self.clock()
            if dry:
                self.phase(name, "dry-drain")
                self.sleep(typical["drain"])
                last = self.clock()
            else:
                self.phase(name, "drain")
                self.control(name, "drain-node")
                self.phase(name, "drained")
                last = self.wait_empty(name)
            self.timed(name, "drain", began)
            regate = lambda: self.wait_empty(name)  # noqa: E731
        elif tier == "forwarders":
            gate = self.gate_forwarder(name)
            if not gate.ok:
                raise Stop(EXIT_STOPPED, f"{name}: the forwarder gate refuses: "
                                         f"{'; '.join(gate.problems)}")
            last, pairs = gate.last_poll, gate.pairs

            def regate():
                again = self.gate_forwarder(name)
                if not again.ok:
                    raise Stop(EXIT_STOPPED, f"{name}: the forwarder gate refuses: "
                                             f"{'; '.join(again.problems)}")
                pairs[:] = again.pairs
                return again.last_poll
        else:
            self.gate_contributor(name)
            last = self.clock()

            def regate():
                self.gate_contributor(name)
                return self.clock()
        if dry:
            self.phase(name, "dry-activate")
            self.sleep(typical["activate"])
            if "ready" in typical:
                self.sleep(typical["ready"])
            self.release_hand(name, "dry")
            self.marks.node_end(label)
            return
        if self.options.hold_s > 0:
            last = self.hold_stopped(name, last, expected, rollback)
            # A stopped node only needs to start: no gate asks it anything.
            regate = self.clock
        activation_us, restarts = self.activate(name, last + NOT_AFTER_S, rollback, regate,
                                                expected)
        result = self.ready(name, expected, before_boot, activation_us, restarts, pairs)
        if not result.ok:
            drained = "; it stays drained" if tier == "edges" else ""
            if undo:
                if not result.answered:
                    raise Stop(EXIT_NO_ANSWER, f"{name} does not answer /api/node after its "
                                               f"rollback{drained}")
                raise Stop(EXIT_ROLLED_BACK, f"{name} is not ready on its previous arguments "
                                             f"either: {'; '.join(result.problems)}{drained}")
            if result.cause == "build":
                self.roll_back(name, result)
            raise Stop(EXIT_STOPPED, f"{name} runs the new build and is not ready, not for the "
                                     f"build's reasons: {'; '.join(result.problems)}{drained}")
        if tier == "edges":
            began = self.clock()
            self.phase(name, "resume")
            if not self.resume_node(name):
                raise Stop(EXIT_STOPPED, f"{name} is ready and stays drained: resume-node failed")
            self.wait_serving(name)
            self.timed(name, "ready", began)
        if name not in self.done:
            self.done.append(name)
        self.release_hand(name, "done")
        self.marks.node_end(label)

    def hold_stopped(self, name: str, last: float, expected: dict, rollback: bool) -> float:
        """`--hold-s`: stop the node within its gate's deadline (as an
        activation is bound) and keep it stopped; the time to activate
        from."""
        entry = self.hand["phase"]
        status = self.status(name)
        self.phase(name, "held", entry=entry, before_boot=status.boot_id if status else None,
                   expected=expected, rollback=rollback)
        try:
            self.ops.stop_node(name, not_after_ms=int((last + NOT_AFTER_S) * 1000))
        except Refused as refused:
            self.phase(name, entry)
            text = f"{name}: the hold's stop was refused ({refused}); it runs as before"
            if entry in DRAINED_PHASES:
                text += "; resumed" if self.give_back(name) else \
                    f"; it stays drained (resume-node {name})"
            raise Stop(EXIT_STOPPED, text) from None
        self.sleep(self.options.hold_s)
        return self.clock()

    def roll_back(self, name: str, failed: Ready) -> None:
        """A node that failed R1, R2 or R4 (3.5): back to `.args.prev`,
        through the gate. Always raises."""
        tier = self.lab.tier(name)
        before = self.before[name]
        what = "; ".join(failed.problems)
        self.phase(name, "rollback-gate", failed=what)
        if tier == "edges":
            # Still drained by the roll (the drain survives the restart) and
            # empty; one that answers and holds anyone is emptied first. A
            # drain that does not empty leaves it drained on the failed
            # build: it is never given back to listeners from here.
            status = self.status(name)
            if status is not None and any(status.udp(item) != 0 for item in DRAIN_GAUGES):
                self.control(name, "drain-node")
                self.wait_empty(name, give_back=False)
            last = self.clock()
            regate = lambda: self.wait_empty(name, give_back=False)  # noqa: E731
        elif tier == "forwarders":
            gate = self.gate_forwarder(name)
            if not gate.ok:
                raise Stop(EXIT_STOPPED, f"{name} failed ({what}); the rollback was not made: the "
                                         f"gate refuses ({'; '.join(gate.problems)}); {name} is "
                                         f"left as it is ({self.resume_command} makes the "
                                         "rollback once the gate passes)")
            last = gate.last_poll

            def regate():
                again = self.gate_forwarder(name)
                if not again.ok:
                    raise Stop(EXIT_STOPPED, f"{name}: the rollback's gate refuses: "
                                             f"{'; '.join(again.problems)}")
                return again.last_poll
        else:
            try:
                self.gate_contributor(name)
            except Stop as stop:
                raise Stop(EXIT_STOPPED, f"{name} failed ({what}); the rollback was not made: "
                                         f"{stop.why}") from None
            last = self.clock()

            def regate():
                self.gate_contributor(name)
                return self.clock()
        status = self.status(name)
        boot = status.boot_id if status and status.boot_id else before["boot_id"]
        previous = {"git": before["git"], "unix": before["unix"],
                    "args_sha256": before["args_sha256"]}
        activation_us, restarts = self.activate(name, last + NOT_AFTER_S, True, regate, previous)
        self.phase(name, "rolled-back")
        result = self.ready(name, previous, boot, activation_us, restarts)
        drained = "; it stays drained" if tier == "edges" else ""
        if not result.ok:
            if not result.answered and not failed.answered:
                raise Stop(EXIT_NO_ANSWER, f"{name} answers /api/node on neither build; "
                                           "its sibling or the other edges carry on")
            raise Stop(EXIT_ROLLED_BACK, f"{name} failed ({what}) and, rolled back to "
                                         f"{before['git']}, fails again "
                                         f"({'; '.join(result.problems)}): the condition is not "
                                         f"the build's{drained}")
        if tier == "edges":
            self.phase(name, "resume")
            if not self.resume_node(name):
                raise Stop(EXIT_ROLLED_BACK, f"{name} failed ({what}), was rolled back to "
                                             f"{before['git']} and stays drained: resume-node "
                                             "failed")
            self.wait_serving(name)
        self.release_hand(name, "rolled-back")
        raise Stop(EXIT_ROLLED_BACK, f"{name} failed its readiness on the new build ({what}) and "
                                     f"was rolled back to {before['git']}")

    # ---------------------------------------------------------- surge
    def surge_needed(self, site: str) -> bool:
        return not self.options.dry and self.options.surge != "never" and \
            site not in self.surge and len(self.lab.edges(site)) < 3

    def surge_prepare(self, scope, build: dict | None) -> None:
        """Before the first node: every region whose edges the command
        drains and that serves fewer than three edges has an edge to start,
        and that edge's files are staged (`build`: the target; None, the
        tier's recorded build), so that it starts on them."""
        for site in sorted({self.lab.site(name) for name in scope
                            if self.lab.tier(name) == "edges"}):
            if not self.surge_needed(site):
                continue
            candidates = self.ops.surge_candidates(site)
            if not candidates:
                raise Refused(f"{site} serves {len(self.lab.edges(site))} edges and has no standby "
                              "edge or slot to start; --surge never rolls with each listener on "
                              "one path while its edge restarts")
            self.stage([candidates[0]], build)
            self.surge_staged[site] = candidates[0]

    def surge_join(self, site: str) -> None:
        """Step 0 (3.5): start the staged surge edge, wait R1, R2, R6, give it
        listeners (resume-node: a standby edge keeps the drain of its last
        leave) and wait R7."""
        if not self.surge_needed(site):
            return
        name = self.surge_staged[site]
        label = f"{self.command}.edges.join.{name}"
        self.marks.node_start(label)
        status = self.status(name)
        before_boot = status.boot_id if status and status.boot_id else 0
        expected = self.ops.expected(name)
        start_us = self.now_us()
        self.surge[site] = name
        self.phase(name, "surge-start", activation_us=start_us, before_boot=before_boot,
                   expected=expected)
        try:
            self.ops.start_node(name)
        except Refused as refused:
            self.surge.pop(site)
            self.release_hand(name, "not started")
            raise Stop(EXIT_STOPPED, f"the surge edge {name} was not started ({refused}); "
                                     f"no edge of {site} was touched") from None
        except Exception as error:  # noqa: BLE001 - it may be running: named
            raise Stop(EXIT_STOPPED, f"the surge edge {name} failed to start ({error}); it may "
                                     "be running") from None
        self.lab.records[name]["serving"] = True
        result = self.ready(name, expected, before_boot, start_us, self.ops.unit_restarts(name))
        if not result.ok:
            raise Stop(EXIT_STOPPED, f"the surge edge {name} did not become ready: "
                                     f"{'; '.join(result.problems)}; it is left as it is")
        self.phase(name, "resume")
        if not self.resume_node(name):
            raise Stop(EXIT_STOPPED, f"the surge edge {name} is ready and stays drained: "
                                     "resume-node failed")
        self.wait_serving(name)
        self.release_hand(name, "surge")
        self.marks.node_end(label)

    def surge_leave(self, site: str) -> None:
        """The surge edge leaves after its region's last edge, as any edge
        is drained: B's single_legs wait, the capacity gate, drain, empty,
        stop. Refused: it is left serving (exit 3)."""
        name = self.surge.get(site)
        if name is None:
            return
        label = f"{self.command}.edges.leave.{name}"
        self.marks.node_start(label)
        self.phase(name, "gate")
        self.wait_single_legs(site)
        gate = self.gate_edge(name)
        if not gate.ok:
            raise Stop(EXIT_STOPPED, f"the surge edge {name} may not leave {site}: the capacity "
                                     f"gate refuses ({'; '.join(gate.problems)}); it is left "
                                     "serving")
        self.phase(name, "drain")
        self.control(name, "drain-node")
        self.phase(name, "drained")
        self.wait_empty(name)
        self.phase(name, "stop")
        self.ops.stop_node(name)
        self.lab.records[name]["serving"] = False
        self.surge.pop(site)
        self.release_hand(name, "left")
        self.marks.node_end(label)

    def surge_finish(self, code: int) -> None:
        """Exit 0: drained and stopped (done per region already). 3 and 130:
        left serving and named. 4 and 5: drained, awaited and stopped."""
        for site in list(self.surge):
            name = self.surge[site]
            if code not in (EXIT_ROLLED_BACK, EXIT_NO_ANSWER):
                self.say(f"the surge edge {name} of {site} is left serving")
                continue
            try:
                self.surge_leave(site)
            except Stop as stop:
                self.say(f"surge edge {name}: {stop.why}")
            except Exception as error:  # noqa: BLE001 - named, never raised past the end
                self.say(f"the surge edge {name} of {site}: {error!r}; it is left as it is")

    # ---------------------------------------------------------- interrupts
    def left_message(self, hand: dict) -> str:
        """What a node left in a phase that is not clean is, and what
        finishes it."""
        name, phase = hand["node"], hand["phase"]
        drained = " and stays drained" if hand.get("tier") == "edges" else ""
        resume = self.resume_command
        if phase in DRAINED_PHASES:
            return f"{name} is drained and was not activated (resume-node {name})"
        if phase == "held":
            return f"{name} is stopped by the hold ({resume} starts it)"
        if phase == "rollback-gate":
            return (f"{name} runs the build that failed{drained}; it was not rolled back "
                    f"({resume} makes the rollback)")
        if phase in STARTED_PHASES:
            return f"{name} is in phase {phase}{drained}; its readiness was not seen ({resume})"
        if phase == "replacing":
            return f"{name} is left stopped or half-installed ({resume} goes on)"
        if phase == "pushed":
            return f"{name} runs its new install; its readiness was not seen ({resume} goes on)"
        if phase == "slot-creating":
            return (f"the slot {name} may exist and bill ({resume} deletes it and goes on; the "
                    "lab's down deletes it)")
        if phase in ("slot-serving", "slot-drain"):
            return f"the slot {name} is serving ({resume} goes on and removes it)"
        return f"{name} was left in phase {phase} ({resume})"

    def interrupted(self) -> None:
        """After a signal (3.5): give back the node in hand where that is
        quick and certain, and say what is left. A drained node that was not
        activated is resumed; a node held stopped is started again; an edge
        that was activated gets 15 s for R1 and warm and is resumed; anything
        else is named and left to its resume."""
        hand = self.hand
        if not self.unclean(hand) or self.options.dry:
            return
        name, phase = hand["node"], hand["phase"]
        if phase in DRAINED_PHASES:
            resumed = self.give_back(name)
            self.say(f"interrupted: {name} was drained and not activated; "
                     + ("resumed" if resumed else f"it stays drained (resume-node {name})"))
            return
        if phase == "slot-drain":
            self.resume_node(name)
            self.phase(name, "slot-serving")
            return
        if phase == "held":
            hand = self.restart_held(name, hand)
            if hand is None:
                return
            phase = hand["phase"]
        if phase in STARTED_PHASES and hand.get("tier") == "edges":
            self.warm_then_resume(name, hand)
            return
        self.say(f"interrupted: {self.left_message(hand)}")

    def restart_held(self, name: str, hand: dict) -> dict | None:
        """An interrupt while a node is held stopped: start it again (its
        activation; starting harms nobody). None when nothing is left to do."""
        if self.status(name) is not None:
            # The hold's stop has not landed: the node runs as before.
            entry = hand.get("entry") or "gate"
            self.phase(name, entry)
            if entry in DRAINED_PHASES:
                self.say(f"interrupted: {name} was not stopped yet; "
                         + ("resumed" if self.give_back(name) else
                            f"it stays drained (resume-node {name})"))
            return None
        start_us = self.now_us()
        try:
            result = self.ops.activate(name, int((self.clock() + NOT_AFTER_S) * 1000),
                                       bool(hand.get("rollback")))
        except Exception as error:  # noqa: BLE001
            result = {"ok": False, "why": f"error: {error}"}
        if not result.get("ok"):
            self.say(f"interrupted: starting {name} failed ({result.get('why')}); "
                     f"{self.left_message(hand)}")
            return None
        self.phase(name, "activated", activation_us=start_us)
        self.say(f"interrupted: {name} was held stopped and is started again")
        return self.hand

    def warm_then_resume(self, name: str, hand: dict) -> None:
        """Up to 15 s for R1 (the build and arguments it was given) and
        `warm`, then resume-node; else it stays drained and is named."""
        expected = hand.get("expected") or {}
        deadline = self.clock() + INTERRUPT_WAIT_S
        while not self.hurry:
            status = self.status(name)
            r1 = readiness_check(self.lab, name, {name: status}, expected=expected,
                                 before_boot=hand.get("before_boot") or 0,
                                 activation_us=hand.get("activation_us") or 0)["R1"]
            if not r1 and status is not None and status.warm is True:
                break
            if self.clock() >= deadline:
                self.say(f"interrupted: {name} is not warm with its new arguments in "
                         f"{INTERRUPT_WAIT_S:.0f} s ({'; '.join(r1) or 'not warm'}); "
                         f"{self.left_message(hand)}")
                return
            self.sleep(self.options.poll_s)
        if self.hurry:
            self.say(f"interrupted twice: {self.left_message(hand)}")
            return
        self.phase(name, "resume")
        if self.resume_node(name):
            self.phase(name, "resumed")
            self.say(f"interrupted: {name} is warm and resumed")
        else:
            self.say(f"interrupted: {self.left_message(self.hand)}")

    # ---------------------------------------------------------- the commands
    def finish(self, code: int, why: str) -> int:
        self.cleaning = True
        self.marks.close()
        clean = self.is_clean()
        if not self.unclean(self.hand):
            self.hand = None
        self.evidence.update(exit=code, why=why, done=self.done, left=list(self.left))
        if self.journal:
            self.journal.update(finished=clean, exit=code, why=why, done=self.done,
                                in_hand=self.hand, left=self.left, surge=self.surge)
            if clean and self.command in ("roll", "restart"):
                supersede_stale(self)
        self.write_evidence()
        if why:
            self.say(why)
        if not clean:
            for hand in Journal.hands({"in_hand": self.hand, "left": self.left}):
                self.say(f"left: {self.left_message(hand)}")
            self.say(f"the journal {self.journal.path} names them")
        return code

    def run(self, label: str, body, open_journal) -> int:
        """The frame of every command: lock, signals, preparation (exit 2),
        the change's marks, the body, and every way out."""
        lock = OpsLock(self.ops.lock_path)
        try:
            lock.acquire()
        except Refused as refused:
            self.say(str(refused))
            return EXIT_REFUSED
        try:
            with self.signals():
                try:
                    body("prepare")
                except Refused as refused:
                    self.say(str(refused))
                    return EXIT_REFUSED
                except Interrupted:
                    self.say("interrupted before the change began; nothing was done")
                    return EXIT_INTERRUPTED
                self.journal = open_journal()
                self.write_journal()
                code, why = EXIT_DONE, ""
                try:
                    self.marks.start(label)
                    try:
                        body("run")
                    except Stop as stop:
                        code, why = stop.code, stop.why
                    except Refused as refused:
                        # A rule refused after the change began: not exit 2.
                        hand = self.hand or {}
                        code, why = EXIT_STOPPED, f"refused after the change began: {refused}" + (
                            f"; {hand['node']} is in phase {hand['phase']}" if hand else "")
                    if code in (EXIT_STOPPED, EXIT_ROLLED_BACK, EXIT_NO_ANSWER) and \
                            self.options.on_failure == "rollback-all":
                        code, why = self.rollback_all(code, why)
                    self.cleaning = True
                    self.surge_finish(code)
                except Interrupted as caught:
                    self.cleaning = True
                    code, why = EXIT_INTERRUPTED, f"interrupted ({caught})"
                    try:
                        self.interrupted()
                    except Exception as error:  # noqa: BLE001 - named below
                        self.say(f"interrupted: the clean-up failed: {error!r}")
                    self.surge_finish(code)
                except Exception as error:  # noqa: BLE001 - a backend failed mid-change
                    self.cleaning = True
                    hand = self.hand or {}
                    code = EXIT_STOPPED
                    why = (f"stopped by an error: {error!r}"
                           + (f"; {hand['node']} is in phase {hand['phase']}" if hand else ""))
                    self.surge_finish(code)
                return self.finish(code, why)
        finally:
            lock.release()

    def rollback_all(self, code: int, why: str) -> tuple[int, str]:
        """--on-failure rollback-all: the nodes already rolled, in reverse
        order, through the same gates. The node that failed stays named in
        the journal (`left`) while it is not clean."""
        for name in reversed(list(self.done)):
            before = self.before[name]
            try:
                self.say(f"rollback-all: {name} back to {before['git']}")
                self.change_node(name, {"git": before["git"], "unix": before["unix"],
                                        "args_sha256": before["args_sha256"]},
                                 rollback=True, undo=True)
                self.done.remove(name)
                self.write_journal()
            except Stop as stop:
                return stop.code, f"{why}; rollback-all stopped at {name}: {stop.why}"
        return code, why


def supersede_stale(engine: Engine) -> None:
    """Older unfinished `roll` and `restart` journals whose every named node
    this command saw `ready` or finished itself are closed (superseded by
    this one) and named on stderr: a stale journal must not be resumed
    later, nor shown as a node in hand."""
    own = engine.journal
    for journal in Journal.unfinished(engine.ops.evidence_dir):
        if journal.path == own.path or journal.data.get("command") not in ("roll", "restart") \
                or journal.data.get("unix", 0) > own.data.get("unix", 0):
            continue
        names = [hand["node"] for hand in Journal.hands(journal.data)
                 if hand.get("phase") not in CLEAN_PHASES]
        if all(name in engine.seen_ready or name in engine.done for name in names):
            journal.update(finished=True, superseded_by=str(own.path))
            engine.say(f"closed {journal.path.name}: "
                       + (f"{', '.join(names)} ready now" if names else "nothing in hand")
                       + "".join(f"; its surge edge {name} of {site} was left serving"
                                 for site, name in sorted((journal.data.get("surge") or {}).items())))


def take_over(engine: Engine, label: str | None = None) -> tuple[Journal, list[dict]]:
    """`--resume`, before the preconditions: the newest unfinished journal
    of the command (with this label, when given). A node it left drained and
    not activated is resumed now; every other node it names is left out of
    the readiness precondition and finished first (`finish_node`)."""
    journals = [journal for journal in Journal.unfinished(engine.ops.evidence_dir, engine.command)
                if label is None or journal.data.get("label") == label]
    if not journals:
        raise Refused(f"no unfinished {engine.command} to resume"
                      + (f" ({label})" if label else ""))
    journal = journals[0]
    for site, name in (journal.data.get("surge") or {}).items():
        if name in engine.lab.records:
            engine.surge[site] = name
            engine.lab.records[name]["serving"] = True
    engine.done = list(journal.data.get("done") or [])
    engine.before.update(journal.data.get("before") or {})
    to_finish = []
    for hand in Journal.hands(journal.data):
        name, phase = hand["node"], hand.get("phase")
        if phase in CLEAN_PHASES:
            continue
        if name not in engine.lab.records:
            raise Refused(f"the journal {journal.path} names {name}, which is not a node of the "
                          "lab any more")
        if phase in DRAINED_PHASES:
            if not engine.resume_node(name):
                raise Refused(f"resume: {name} was drained and not activated, and resume-node "
                              "failed")
            engine.say(f"resume: {name} was drained and not activated; resumed")
            continue
        engine.excluded.add(name)
        to_finish.append(hand)
    return journal, to_finish


def open_change(engine: Engine, label: str, plan: list[str], previous: Journal | None,
                to_finish: list[dict], **fields) -> Journal:
    """The journal of a roll or a restart. A resumed command takes over the
    nodes its predecessor left (they are `left` until finished) and closes
    it; older unfinished journals whose nodes are ready again are closed."""
    journal = Journal.create(engine.ops.evidence_dir, engine.command, int(engine.clock()),
                             label=label, plan=plan, **fields)
    engine.journal = journal
    if previous is not None:
        engine.left.update({hand["node"]: hand for hand in to_finish})
        journal.update(resumed_from=str(previous.path))
        previous.update(finished=True, superseded_by=str(journal.path))
    engine.write_journal()
    supersede_stale(engine)
    return journal


def activation_ran(engine: Engine, name: str, hand: dict) -> bool:
    """For a node left `activating`: whether the activation ran. Unknown is
    taken as ran (its readiness decides), except for a node that was held
    stopped, which is started."""
    status = engine.status(name)
    if status is not None and status.boot_id is not None:
        return hand.get("before_boot") is None or status.boot_id != hand["before_boot"]
    ran = engine.ops.boot_changed(name, (hand.get("activation_us") or 0) // 1000)
    if ran is None:
        return hand.get("entry") != "held"
    return ran


def finish_node(engine: Engine, hand: dict) -> None:
    """`--resume`, after the preconditions: finish one node a cut-off
    command left, by its phase. Held stopped: start it. Activating: if the
    activation did not run, the node is as it was (the command comes to it
    again). Activated, rolled back, a surge edge started or an edge being
    resumed: its readiness, then resume and R7 for an edge. Before its
    rollback: the rollback, through the gate."""
    name, phase = hand["node"], hand["phase"]
    engine.before.setdefault(name, hand.get("before") or {})
    engine.phase(name, phase, **{key: value for key, value in hand.items()
                                 if key not in ("node", "tier", "phase")})
    engine.say(f"resume: finishing {name} (phase {phase})")
    tier = engine.lab.tier(name)
    if phase == "activating":
        if activation_ran(engine, name, hand):
            phase = "rolled-back" if hand.get("entry") == "rollback-gate" else "activated"
        else:
            phase = hand.get("entry") or "gate"
            engine.say(f"resume: {name} was not activated")
        engine.phase(name, phase)
    if phase in DRAINED_PHASES:
        if not engine.give_back(name):
            raise Stop(EXIT_STOPPED, f"resume: {name} stays drained: resume-node failed")
        engine.release_hand(name, "resumed")
        return
    if phase == "gate":
        engine.release_hand(name, "untouched")
        return
    if phase == "held":
        engine.activate(name, engine.clock() + NOT_AFTER_S, bool(hand.get("rollback")),
                        engine.clock, hand.get("expected") or
                        engine.ops.expected(name, rollback=bool(hand.get("rollback"))))
        phase = "activated"
    if phase == "rollback-gate":
        engine.roll_back(name, Ready(False, "build", [hand.get("failed") or
                                                      "its readiness failed"], True, None))
    if phase not in STARTED_PHASES:
        raise Stop(EXIT_STOPPED, f"resume: {name} is in phase {phase}, which a resume does not "
                                 "finish")
    current = engine.hand
    before = engine.before.get(name) or {}
    if phase == "rolled-back":
        expected = {key: before.get(key) for key in ("git", "unix", "args_sha256")}
    else:
        expected = current.get("expected") or engine.ops.expected(
            name, rollback=bool(current.get("rollback")))
    result = engine.ready(name, expected, current.get("before_boot") or 0,
                          current.get("activation_us") or 0, engine.ops.unit_restarts(name))
    if not result.ok:
        if result.cause == "build" and phase == "activated" and not current.get("undo"):
            engine.roll_back(name, result)
        raise Stop(EXIT_ROLLED_BACK if phase == "rolled-back" else EXIT_STOPPED,
                   f"{name} is not ready: {'; '.join(result.problems)}"
                   + ("; it stays drained" if tier == "edges" else ""))
    if tier == "edges":
        engine.phase(name, "resume")
        if not engine.resume_node(name):
            raise Stop(EXIT_STOPPED, f"{name} is ready and stays drained: resume-node failed")
        engine.wait_serving(name)
    if phase == "rolled-back":
        engine.release_hand(name, "rolled-back")
        raise Stop(EXIT_ROLLED_BACK, f"{name} was rolled back by the command that was cut off; "
                                     "it does not go on")
    if phase == "surge-start":
        engine.release_hand(name, "surge")
        return
    if name not in engine.done:
        engine.done.append(name)
    engine.release_hand(name, "done")


def _builds(ops: Ops, lab: Lab, scope: list[str]) -> dict:
    return {tier: ops.target(tier) for tier in TIERS
            if any(lab.tier(name) == tier for name in scope)}


def roll(ops: Ops, tier: str, *, nodes=None, sites=None, options: Options | None = None,
         resume: bool = False, out=None, err=None, clock=time.time, sleep=time.sleep) -> int:
    """`roll edges|forwarders|contributors|all` (3.5): one node at a time,
    in tier order, each gated, activated, made ready; nodes already on the
    target build skipped unless `options.force`. `resume`: first finish or
    undo every node the newest unfinished roll left."""
    options = options or Options()
    engine = Engine(ops, "roll", options, out, err, clock, sleep)
    engine.resume_command = f"roll {tier} --resume"
    label = f"roll.{tier}"
    state: dict = {"previous": None, "finish": []}

    def body(step: str):
        if step == "prepare":
            engine.lab = Lab(ops.nodes())
            if resume:
                state["previous"], state["finish"] = take_over(engine)
            state["order"] = plan_order(engine.lab, tier, nodes, sites)
            if not state["order"]:
                raise Refused(f"no serving node of the {tier} tier in scope")
            builds = _builds(ops, engine.lab, state["order"])
            state["expected"] = engine.prepare(state["order"], builds)
            skipped = [] if options.force else [
                name for name in state["order"] if engine.on_target(name, state["expected"][name])]
            state["skipped"] = skipped
            engine.surge_prepare([name for name in state["order"] if name not in skipped],
                                 builds.get("edges"))
            engine.print_plan(state["order"], skipped)
            return
        for hand in state["finish"]:
            finish_node(engine, hand)
        order = state["order"]
        for index, name in enumerate(order):
            if name in state["skipped"] or name in engine.done:
                continue
            site = engine.lab.site(name)
            if engine.lab.tier(name) == "edges":
                engine.surge_join(site)
            engine.change_node(name, state["expected"][name])
            last_of_site = not any(engine.lab.tier(other) == "edges"
                                   and engine.lab.site(other) == site
                                   and other not in state["skipped"]
                                   for other in order[index + 1:])
            if engine.lab.tier(name) == "edges" and last_of_site:
                engine.surge_leave(site)

    def open_journal():
        return open_change(engine, label, state["order"], state["previous"], state["finish"],
                           tier=tier)

    return engine.run(label, body, open_journal)


def restart_nodes(ops: Ops, names: list[str], *, options: Options | None = None,
                  resume: bool = False, out=None, err=None, clock=time.time,
                  sleep=time.sleep) -> int:
    """`restart --nodes a,b` (2.6): the roll's gates and readiness (the
    surge edge included), one node at a time in the order given, on each
    node's recorded build (`stage` with no build); `options.hold_s` holds
    each node stopped that long; `options.rollback` puts `.args.prev` in
    force, the operator's rollback of a node an earlier command left (that
    node need not be ready, and its rollback is not rolled back again).
    `resume`: finish what the newest unfinished restart of these nodes
    left, then go on."""
    options = options or Options()
    engine = Engine(ops, "restart", options, out, err, clock, sleep)
    label = "restart." + ",".join(names)
    engine.resume_command = f"restart --nodes {','.join(names)} --resume"
    state: dict = {"previous": None, "finish": []}

    def body(step: str):
        if step == "prepare":
            engine.lab = Lab(ops.nodes())
            unknown = [name for name in names if name not in engine.lab.records
                       or not engine.lab.records[name].get("serving")]
            if unknown:
                raise Refused(f"not serving nodes of the lab: {', '.join(unknown)}")
            if resume:
                state["previous"], state["finish"] = take_over(engine, label)
            if options.rollback:
                engine.excluded.update(names)
            state["expected"] = engine.prepare(list(names), {tier: None for tier in TIERS})
            engine.surge_prepare([name for name in names if name not in engine.done], None)
            engine.print_plan(list(names), [name for name in names if name in engine.done])
            return
        for hand in state["finish"]:
            finish_node(engine, hand)
        for index, name in enumerate(names):
            if name in engine.done:
                continue
            site = engine.lab.site(name)
            edge = engine.lab.tier(name) == "edges"
            if edge:
                engine.surge_join(site)
            engine.change_node(name, state["expected"][name], rollback=options.rollback,
                               undo=options.rollback)
            if edge and not any(engine.lab.tier(other) == "edges" and engine.lab.site(other) == site
                                for other in names[index + 1:]):
                engine.surge_leave(site)

    def open_journal():
        return open_change(engine, label, list(names), state["previous"], state["finish"])

    return engine.run(label, body, open_journal)


def replace(ops: Ops, name: str, *, mode: str = "rebuild", no_slot: bool = False,
            keep_slot: bool = False, resume: bool = False, options: Options | None = None,
            out=None, err=None, clock=time.time, sleep=time.sleep) -> int:
    """`replace --nodes X` (3.7): an edge emptied, stopped, rebuilt, pushed
    and resumed; a forwarder through its region's slot; a contributor
    through workstream A's gate and readiness. Each step done is journalled,
    and the slot with its state (creating, serving, kept); a node or slot
    left by a failure or an interrupt is named, and `resume` goes on after
    the last step done of the newest unfinished replace of X (a slot whose
    create failed part-way is deleted first)."""
    options = options or Options()
    engine = Engine(ops, "replace", options, out, err, clock, sleep)
    engine.resume_command = f"replace --nodes {name} --resume"
    label = f"replace.{name}"
    context: dict = {"done_steps": [], "slot": None, "slot_state": None, "slot_created": False,
                     "slot_began_us": None, "push_began_us": None, "pairs": []}
    state: dict = {}

    def save() -> None:
        engine.journal.update(context=context)

    def step_done(step: str) -> None:
        context["done_steps"].append(step)
        save()

    def ready_after_push(node: str, began_us: int, pairs=()):
        expected = ops.expected(node)
        result = engine.ready(node, expected, 0, began_us, ops.unit_restarts(node), pairs)
        if not result.ok:
            raise Stop(EXIT_STOPPED, f"{node} is not ready after its replacement: "
                                     f"{'; '.join(result.problems)}")

    def pairs_zero(node: str) -> None:
        deadline = clock() + options.drain_timeout_s
        while True:
            status = engine.status(node)
            if status is not None and status.lease_pairs_total == 0:
                return
            if clock() >= deadline:
                raise Stop(EXIT_STOPPED, f"{node}'s lease pairs did not reach 0 in "
                                         f"{options.drain_timeout_s:.0f} s")
            sleep(options.poll_s)

    def gate(node: str) -> Gate:
        found = engine.gate_forwarder(node)
        if not found.ok:
            raise Stop(EXIT_STOPPED, f"{node}: the forwarder gate refuses: "
                                     f"{'; '.join(found.problems)}")
        return found

    def rebuild_and_push(node: str) -> int:
        """Stop, rebuild, push, each journalled; the time the push began."""
        steps = (("stop", ops.stop_node), ("rebuild", lambda each: ops.rebuild(each, mode)),
                 ("push", ops.push))
        for step, action in steps:
            if step in context["done_steps"]:
                continue
            if step == "push":
                context["push_began_us"] = engine.now_us()
                save()
            try:
                action(node)
            except Exception as error:  # noqa: BLE001 - the node may be down: named
                raise Stop(EXIT_NO_ANSWER, f"{node}'s {step} failed ({error}): it is left stopped "
                                           f"or half-installed; {engine.resume_command} goes "
                                           "on") from None
            step_done(step)
            if step == "rebuild" and mode == "new":
                engine.lab.readdress(ops.nodes())
        return context["push_began_us"]

    def give_back_x(stop: Stop, slot: str | None) -> Stop:
        resumed = engine.give_back(name)
        return Stop(EXIT_STOPPED, f"{stop.why}; {name} " + ("resumed" if resumed else
                                                            f"stays drained (resume-node {name})")
                    + (f"; the slot {slot} is left serving" if slot else ""))

    def make_slot(slot: str) -> None:
        """Create (or start, when it exists stopped) the region's slot and
        wait it ready; X is untouched whatever happens here."""
        record = engine.lab.records[slot]
        if context["slot_state"] == "creating":
            engine.say(f"the slot {slot} of the replace that was cut off may exist: deleting it")
            ops.remove_slot(slot)
            context["slot_state"] = None
            save()
            engine.release_hand(slot, "removed")
            record.update(serving=False, created=False)
        if not record.get("serving"):
            gate(name)
            context.update(slot_state="creating", slot_began_us=engine.now_us())
            save()
            engine.hold(slot, "slot-creating")
            try:
                if record.get("created"):
                    ops.start_node(slot)
                else:
                    ops.add_slot(slot)
            except Refused as refused:
                context["slot_state"] = None
                save()
                engine.release_hand(slot, "refused")
                raise Stop(EXIT_STOPPED, f"the slot {slot} was refused ({refused}); {name} is "
                                         "untouched") from None
            except Exception as error:  # noqa: BLE001 - it may exist: named
                raise Stop(EXIT_STOPPED, f"the slot {slot} failed part-way ({error}); {name} is "
                                         "untouched") from None
            context.update(slot_state="serving", slot_created=True)
            save()
            engine.hold(slot, "slot-serving")
            record["serving"] = True
            # A kept slot keeps the drain of its last leave; resume-node
            # removes only that (its hellos say draining until it is warm).
            if not engine.resume_node(slot):
                raise Stop(EXIT_STOPPED, f"the slot {slot} stays drained: resume-node failed; "
                                         f"{name} is untouched")
        if context["slot_created"]:
            result = wait_ready(ops, slot, expected=ops.expected(slot), before_boot=0,
                                activation_us=context["slot_began_us"], lab=engine.lab,
                                restarts_before=ops.unit_restarts(slot),
                                timeout_s=options.ready_timeout_s, poll_s=options.poll_s,
                                clock=clock, sleep=sleep)
            if not result.ok:
                raise Stop(EXIT_STOPPED, f"the slot {slot} is not ready "
                                         f"({'; '.join(result.problems)}); {name} is untouched")

    def remove_slot(slot: str) -> None:
        """Drain the slot, its pairs to 0, the gate, stop, delete (or keep
        it stopped). Refused at any step: it is resumed and left serving."""
        if engine.lab.records[slot].get("serving"):
            engine.phase(slot, "slot-drain")
            engine.control(slot, "drain-node")
            try:
                pairs_zero(slot)
                gate(slot)
            except Stop as stop:
                resumed = engine.resume_node(slot)
                engine.phase(slot, "slot-serving")
                raise Stop(EXIT_STOPPED, f"{stop.why}; the slot {slot} "
                                         + ("is resumed" if resumed else "stays drained")
                                         + " and left serving") from None
            engine.phase(slot, "slot-stop")
            ops.stop_node(slot)
            engine.lab.records[slot]["serving"] = False
        if not keep_slot:
            ops.remove_slot(slot)
        context["slot_state"] = "kept" if keep_slot else None
        step_done("slot-gone")
        engine.release_hand(slot, "kept stopped" if keep_slot else "removed")

    def body(step: str):
        if step == "prepare":
            engine.lab = Lab(ops.nodes())
            if name not in engine.lab.records:
                raise Refused(f"{name} is not a node of the lab")
            ops.check("replace", [name])
            tier = engine.lab.tier(name)
            if tier == "contributors":
                engine.rules = contributor_rules()
            engine.clock_check()
            if resume:
                journals = [journal for journal in Journal.unfinished(ops.evidence_dir, "replace")
                            if journal.data.get("label") == label]
                if not journals:
                    raise Refused(f"no unfinished replace of {name} to resume")
                state["previous"] = journals[0]
                context.update(journals[0].data.get("context") or {})
                engine.surge.update(journals[0].data.get("surge") or {})
            if tier == "forwarders" and not no_slot and context["slot"] is None:
                slots = [node for node, record in engine.lab.records.items()
                         if record["site"] == engine.lab.site(name) and record.get("slot")
                         and record["role"] == "distributor"]
                if not slots:
                    raise Refused(f"{engine.lab.site(name)} has no forwarder slot (--no-slot)")
                context["slot"] = sorted(slots, key=natural)[0]
            if tier == "edges" and "empty" not in context["done_steps"]:
                engine.surge_prepare([name], None)
            status = engine.status(name)
            if status is not None:
                engine.before[name] = engine.snapshot(status)
            for site in engine.lab.sites():
                engine.costs[site] = leg_cost(engine.lab, site, engine.poll(engine.lab.edges(site)))
            return
        tier = engine.lab.tier(name)
        site = engine.lab.site(name)
        node_label = f"replace.{tier}.{name}"
        engine.marks.node_start(node_label)
        done = context["done_steps"]
        slot = context["slot"]
        if slot and context["slot_state"] in ("creating", "serving") and "slot-gone" not in done:
            engine.hold(slot, f"slot-{context['slot_state']}")
        # Where a cut-off replace left X.
        if "push" in done:
            first, fields = "pushed", {"activation_us": context["push_began_us"]}
        elif "stop" in done:
            first, fields = "replacing", {}
        elif "empty" in done or (slot and "drained" in done):
            first, fields = "drained", {}
        else:
            first, fields = "gate", {}
        engine.phase(name, first, before=engine.before.get(name), **fields)
        if tier == "edges":
            if "empty" not in done:
                engine.surge_join(site)
                engine.phase(name, "gate")
                found = engine.gate_edge(name)
                if not found.ok:
                    raise Stop(EXIT_STOPPED, f"{name}: the capacity gate refuses: "
                                             f"{'; '.join(found.problems)}")
                engine.phase(name, "drain")
                engine.control(name, "drain-node")
                engine.phase(name, "drained")
                engine.wait_empty(name)
                step_done("empty")
            engine.phase(name, "replacing")
            began_us = rebuild_and_push(name)
            engine.phase(name, "pushed", activation_us=began_us)
            ready_after_push(name, began_us)
            engine.phase(name, "resume")
            if not engine.resume_node(name):
                raise Stop(EXIT_STOPPED, f"{name} is ready and stays drained: resume-node failed")
            engine.wait_serving(name)
            engine.phase(name, "resumed")
            engine.surge_leave(site)
        elif tier == "forwarders":
            if slot and "slot-ready" not in done:
                make_slot(slot)
                step_done("slot-ready")
            if "drained" not in done:
                if slot:
                    engine.phase(name, "drain")
                    engine.control(name, "drain-node")
                    try:
                        pairs_zero(name)
                    except Stop as stop:
                        raise give_back_x(stop, slot) from None
                    engine.phase(name, "drained")
                step_done("drained")
            if "stop" not in done:
                try:
                    context["pairs"] = gate(name).pairs
                except Stop as stop:
                    if slot:
                        raise give_back_x(stop, slot) from None
                    raise
                save()
            engine.phase(name, "replacing")
            began_us = rebuild_and_push(name)
            engine.phase(name, "pushed", activation_us=began_us)
            ready_after_push(name, began_us, [tuple(pair) for pair in context["pairs"]])
            engine.phase(name, "resume")
            if not engine.resume_node(name):
                raise Stop(EXIT_STOPPED, f"{name} is ready and stays drained: resume-node failed")
            engine.phase(name, "resumed")
            if slot and context["slot_created"] and "slot-gone" not in done:
                remove_slot(slot)
        else:
            if "stop" not in done:
                engine.gate_contributor(name)
            engine.phase(name, "replacing")
            began_us = rebuild_and_push(name)
            engine.phase(name, "pushed", activation_us=began_us)
            ready_after_push(name, began_us)
        engine.release_hand(name, "done")
        engine.marks.node_end(node_label)

    def open_journal():
        journal = Journal.create(ops.evidence_dir, "replace", int(clock()), label=label,
                                 plan=[name], context=context)
        engine.journal = journal
        previous = state.get("previous")
        if previous is not None:
            journal.update(resumed_from=str(previous.path))
            previous.update(finished=True, superseded_by=str(journal.path))
        return journal

    return engine.run(label, body, open_journal)
