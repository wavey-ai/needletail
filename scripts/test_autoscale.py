#!/usr/bin/env python3
"""Tests of the autoscaler (scripts/linode-autoscale.py), offline: a
simulated backend that serves every node's /api/node and the regions'
/api/assignment as the mesh would (so every test reads through node_view
and merge_views) and records every call, a fake clock, the real worker
thread, no Linode and no lab. Hysteresis and the trigger; nodes still
joining counted; the cooldown and the no-flap rule; standby edges first,
then the warm pool, then a slot to create; the budget refusal; drain before
delete, and a drain that times out resumed and never deleted; the warm pool
released at the end of a paid hour; the forwarder gate before a forwarder
slot stops; the CPU trigger; the worker (a create never blocks the tick, a
create in flight adopted after a restart, a refused create backed off, a
create that finishes mid-tick or only in part still pushed); the
lost rule (blind is not lost, a node its peers hear is not lost, a lost
edge replaced once and recovered, a first forwarder never touched, a
flapping node quarantined, a rebuild in flight awaited, a lost slot deleted
only with its tag); the change verbs and their marks; stopping (nothing
new handed over, the job in hand waited for under the lock, drains resumed
after any failure); main's stdout and clock check; a restart from the
controller's state; the safety layer; a dry run that calls nothing; events
with Unix-µs timestamps; the linode backend's roster, budget, observation
and adoption from a lab state; the edge_lab backend from a fake lab.
python3 -m unittest scripts/test_autoscale.py"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("autoscale", HERE / "linode-autoscale.py")
auto = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = auto  # its dataclass looks itself up there
_spec.loader.exec_module(auto)

STREAM = 4001


class Clock:
    def __init__(self, now: float = 1_790_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Sim(auto.Backend):
    """London: edges 1-5 serving, 6-10 standby (created, held out), slots
    lon-edge-11 and -12 (not created); lon-dist-a/b (first) and the slot
    lon-dist-c. With `newyork`: new-edge-1..4 serving, new-dist-a/b. Every
    node serves its /api/node, and each site's first node in service its
    /api/assignment, as av-mesh does: a node that runs is heard by its
    peers (40 ms) and feeds its children (1 ms); a crashed one answers
    nothing and its peers' and children's ages grow from its crash; an
    unreachable one runs but does not answer the operator. Every changing
    call is recorded; a node that is started advertises on the next
    observation. As on Linode, `held` is "held out by a stop" (the
    deploy's standby_edges): a slot that was created and never pushed is
    neither running nor held. Each host has an ssh key that ssh learns
    once and a rebuild changes; until it is forgotten, ssh to the rebuilt
    host fails."""

    name = "sim"

    def __init__(self, clock: Clock, newyork: bool = False):
        self.clock = clock
        self.calls: list[tuple[str, str]] = []
        self.nodes: dict[str, dict] = {}
        self.entry_down: set[str] = set()
        self.orphans: list[dict] = []
        self.peer_age: dict[str, float] = {}      # subject -> age every peer reports
        self.hears: dict[tuple, float] = {}        # (observer, subject) -> age
        self.on_observe: list = []
        self.create_gate: threading.Event | None = None
        self.rebuild_gate: threading.Event | None = None
        self.die_in_create = False
        self.refuse_create: set[str] = set()
        self.budget_ok = True
        self.fail_create = False
        # Creates the first slot of a batch and raises for the rest.
        self.partial_create = False
        # Prints as the lab tool does (its "Adding ..." and "created ...").
        self.chatty = False
        self.start_gate: threading.Event | None = None
        # {node: how many more starts or pushes end unwarmed} (the deploy's
        # exit 6: everything else done, the warm record not complete).
        self.unwarmed: dict[str, int] = {}
        self.stop_gate: threading.Event | None = None
        self.known: dict[str, int] = {}            # host -> the key ssh learned
        self.forgotten: list[str] = []
        self.offset = 0.0                          # every node's clock minus ours, s
        self.lock = tempfile.mkdtemp()
        for index in range(1, 13):
            self.add(f"lon-edge-{index}", "london", "edge", slot=index > 10, running=index <= 5)
        for name, slot in (("lon-dist-a", False), ("lon-dist-b", False), ("lon-dist-c", True)):
            self.add(name, "london", "distributor", slot=slot, running=not slot)
        if newyork:
            for index in range(1, 5):
                self.add(f"new-edge-{index}", "newyork", "edge", slot=False, running=True)
            for name in ("new-dist-a", "new-dist-b"):
                self.add(name, "newyork", "distributor", slot=False, running=True)

    def add(self, name, site, role, slot, running):
        index = len(self.nodes) + 1
        self.known[name] = 1  # ssh learned its key at the deploy
        self.nodes[name] = {
            "site": site, "role": role, "slot": slot, "created": not slot, "running": running,
            "load": 500 if role == "edge" else 0, "udp": 100 if role == "edge" else 0,
            "hls": 0, "moved": 0, "reads": 0, "refusals": 0, "draining": False,
            "created_unix": self.clock() - 1800, "lease": None, "cache": None, "cpu": None,
            "moves": 0, "ready": True, "crashed_unix": None, "reachable": True, "stale": False,
            "ssh": True, "instance": "running", "restartable": True, "pairs": [], "rows": None,
            "listed": None, "restart_error": False, "unpushed": False, "host_key": 1,
            "vlan": f"10.0.{0 if site == 'london' else 1}.{index}",
            "public": f"192.0.2.{index}"}

    # -- what the controller sees
    def set_load(self, permille: int, names=None) -> None:
        for name, node in self.nodes.items():
            if node["role"] == "edge" and (names is None or name in names):
                node["load"] = permille

    def alive(self, name: str) -> bool:
        node = self.nodes[name]
        return node["created"] and node["running"] and node["crashed_unix"] is None

    def up(self, name: str) -> bool:
        """Running or crashed (its peers know it), not held out."""
        node = self.nodes[name]
        return node["created"] and node["running"]

    def age(self, name: str) -> float:
        node = self.nodes[name]
        if node["crashed_unix"] is None:
            return 40.0
        return (self.clock() - node["crashed_unix"]) * 1000 + 40.0

    def roster(self):
        out: dict[str, list[dict]] = {}
        for name, node in self.nodes.items():
            out.setdefault(node["site"], []).append({
                "name": name, "role": node["role"], "site": node["site"],
                "created": node["created"],
                "held": node["created"] and not node["running"] and not node["unpushed"],
                "slot": node["slot"],
                "first": node["role"] == "distributor" and not node["slot"],
                "created_unix": node["created_unix"] if node["created"] else None,
                "hourly_usd": 0.054, "vlan": node["vlan"],
                "public": node["public"] if node["created"] else None})
        return out

    def document(self, name: str):
        node = self.nodes[name]
        if not self.alive(name) or not node["reachable"]:
            return None
        site = node["site"]
        peers = []
        for other, item in self.nodes.items():
            if other == name or item["site"] != site or not self.up(other):
                continue
            age = self.hears.get((name, other), self.peer_age.get(other, self.age(other)))
            peers.append({"peer": f"{item['vlan']}:29101", "heard_age_ms": age})
        parents = []
        if node["rows"] is not None:
            parents = node["rows"]
        elif node["role"] == "edge":
            for other, item in self.nodes.items():
                if item["site"] == site and item["role"] == "distributor" and self.up(other):
                    age = 1.0 if item["crashed_unix"] is None else self.age(other)
                    parents.append({"address": item["vlan"], "streams": [
                        {"stream_id": STREAM, "track": "audio", "source_age_ms": age,
                         "objects_1s": 1000 if item["crashed_unix"] is None else 0}]})
        document = {
            "schema": "needletail.node.v1", "node_id": name, "role": node["role"],
            "state": "draining" if node["draining"] else "ready" if node["ready"] else "starting",
            "ready": node["ready"] and not node["draining"],
            "hello_draining": node["draining"] or not node["ready"],
            "drain_reasons": ["operator"] if node["draining"] else [],
            "process": {} if node["cpu"] is None else {"cpu_permille": round(node["cpu"] * 1000)},
            "fast_plane": {"threads": 2, "threads_expected": 2},
            "ingest": {"audio_deadline_drops": 0},
            "parents": parents,
            "children": {"compiled": [], "lease": {
                "children": node["lease"], "pairs": len(node["pairs"]),
                "pairs_list": [{"child": self.nodes[child]["vlan"], "stream_id": stream}
                               for child, stream in node["pairs"]],
                "streams": []}},
            "cache_mesh": {"children": node["cache"], "peers": peers},
            "client_udp": None}
        if node["role"] == "edge":
            document["client_udp"] = {
                "subscribers": node["udp"], "hls_sessions": node["hls"],
                "hls_moved_active": node["moved"], "hls_requests_in_flight": node["reads"],
                "refusals": node["refusals"], "rebalance_moves": node["moves"],
                "load_permille": node["load"], "parents_lost": False,
                # B's gauges, as av-mesh serves them beside these (C 7).
                "leaving": 0, "single_legs": node["udp"], "legs_by_other": {},
                "moves_completed": 0, "moves_expired": 0, "moves_taken_back": 0,
                "redundant_refusals": 0}
        return document

    def assignment(self, site: str):
        if site in self.entry_down:
            return None
        entry = next((name for name, node in self.nodes.items() if node["site"] == site
                      and self.alive(name) and node["reachable"]), None)
        if entry is None:
            return None
        return {"caches": [{"node_id": name, "fresh": node["listed"] if node["listed"] is not None
                            else self.alive(name) and node["ready"] and not node["stale"],
                            "draining": node["draining"],
                            "load_permille": node["load"] if self.alive(name) else 0}
                           for name, node in self.nodes.items()
                           if node["site"] == site and node["role"] == "edge"
                           and node["created"] and node["running"]]}

    def documents(self, names):
        return {name: self.document(name) for name in names}

    def get(self, name, path):
        document = self.assignment(self.nodes[name]["site"])
        return (200, document) if document else (None, None)

    def observe(self, roster):
        for hook in list(self.on_observe):
            hook()
        entries = {site: self.assignment(site) for site in roster}
        documents = self.documents([name for name, node in self.nodes.items()
                                    if node["created"]])
        return auto.merge_views(roster, entries, documents)

    def lock_path(self):
        return pathlib.Path(self.lock) / "ops.lock"

    # -- what the controller does
    def record(self, verb, name):
        self.calls.append((verb, name))

    def create(self, names):
        self.record("create", ",".join(names))
        if self.chatty:
            print(f"Adding {len(names)} slot(s) {', '.join(names)}: the lab will cost more",
                  end="")
            print()
        if self.create_gate is not None:
            self.create_gate.wait(30)
        if self.die_in_create:
            raise RuntimeError("the worker died")
        refused = [name for name in names if name in self.refuse_create]
        if refused:
            raise auto.Refused(f"no capacity for {', '.join(refused)}")
        if self.fail_create:
            raise RuntimeError("API timeout")
        for index, name in enumerate(names):
            if self.partial_create and index > 0:
                raise RuntimeError(f"create {name}: 503 from the API")
            self.nodes[name].update(created=True, created_unix=self.clock(), unpushed=True)
            if self.chatty:
                print(f"  created {name}")

    def push(self, names):
        self.record("push", ",".join(names))
        for name in names:
            self.nodes[name].update(running=True, crashed_unix=None, unpushed=False)
        self.warm_or_raise(names)

    def start(self, name):
        self.record("start", name)
        if self.start_gate is not None:
            self.start_gate.wait(30)
        self.nodes[name].update(running=True, unpushed=False)
        self.warm_or_raise([name])

    def warm_or_raise(self, names):
        cold = [name for name in names if self.unwarmed.get(name, 0) > 0]
        for name in cold:
            self.unwarmed[name] -= 1
            # A node without a complete warm record is never ready (its
            # service waits for the record).
            self.nodes[name].update(ready=False, cold=True)
        for name in names:
            if name not in cold and self.nodes[name].pop("cold", False):
                self.nodes[name]["ready"] = True
        if cold:
            raise auto.Unwarmed(f"UNWARMED: {', '.join(cold)}: no complete warm record")

    def stop(self, name):
        self.record("stop", name)
        if self.stop_gate is not None:
            self.stop_gate.wait(30)
        self.nodes[name].update(running=False, draining=False, unpushed=False)

    def drain(self, name):
        self.record("drain", name)
        self.nodes[name]["draining"] = True

    def resume(self, name):
        self.record("resume", name)
        self.nodes[name]["draining"] = False

    def delete(self, name):
        self.record("delete", name)
        self.nodes[name]["created"] = False
        self.nodes[name]["running"] = False

    def probe(self, name):
        node = self.nodes[name]
        ssh = node["ssh"]
        if ssh and node["instance"] == "running":
            # accept-new: a key is learned once; another key is refused.
            ssh = self.known.setdefault(name, node["host_key"]) == node["host_key"]
        return {"ssh": ssh, "unit_active": (ssh and node["crashed_unix"] is None) if ssh
                else None, "instance": node["instance"]}

    def forget_host(self, name):
        self.forgotten.append(name)
        self.known.pop(name, None)

    def clock_offset(self, name):
        return self.offset

    def restart(self, name):
        self.record("restart", name)
        if self.nodes[name]["restartable"]:
            self.nodes[name]["crashed_unix"] = None
        if self.nodes[name]["restart_error"]:
            raise RuntimeError("ssh: connection reset after the restart was sent")

    def boot(self, name):
        self.record("boot", name)
        self.nodes[name].update(instance="running", crashed_unix=None)

    def rebuild(self, name):
        """A new host key, whatever happens after; forgotten (as the
        linode backend's rebuild does) only when the rebuild returns."""
        self.record("rebuild", name)
        self.nodes[name]["instance"] = "rebuilding"
        self.nodes[name]["host_key"] += 1
        if self.rebuild_gate is not None:
            self.rebuild_gate.wait(30)
            raise RuntimeError("the controller died")
        self.nodes[name].update(instance="running", ssh=True)
        self.forget_host(name)

    def adopt(self, record=True):
        out = []
        for item in self.orphans:
            name = item.get("name")
            if name in self.nodes and self.nodes[name]["slot"] and record:
                self.nodes[name]["created"] = True
                out.append({"name": name, "label": item["label"], "adopted": True})
            else:
                out.append({"label": item["label"], "adopted": False,
                            "why": "not a missing slot of the lab: left to down"})
        self.orphans = []
        return out

    def budget(self, new, offline=False):
        return self.budget_ok, {"budget_usd": 100.0, "new": new}


class Base(unittest.TestCase):
    newyork = False

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        self.sim = Sim(self.clock, newyork=self.newyork)
        self.addCleanup(shutil.rmtree, self.sim.lock, True)
        self.config = auto.Config()
        self.state_path = self.tmp / "controller.json"
        self.events_path = self.tmp / "events.jsonl"

    def controller(self, dry_run: bool = False, config: auto.Config | None = None):
        backend = auto.Guarded(self.sim, config or self.config, dry_run)
        events = auto.Events(str(self.events_path), echo=False, clock=self.clock)
        controller = auto.Controller(backend, config or self.config, self.state_path, events,
                                     clock=self.clock)
        # A test's gated job is not waited for at the end.
        controller.stop_wait_s = 0.2
        self.addCleanup(controller.close)
        return controller

    def ticks(self, controller, count: int, step: float = 2.0,
              wait: bool = True) -> list[list[dict]]:
        out = []
        for _ in range(count):
            out.append(controller.tick())
            if wait:
                self.assertTrue(controller.worker.wait_idle(15), "the worker did not finish")
            self.clock.advance(step)
        return out

    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def kinds(self) -> list[str]:
        return [item["kind"] for item in self.events()]

    def of(self, kind: str) -> list[dict]:
        return [item for item in self.events() if item["kind"] == kind]

    def mutating(self) -> list[tuple[str, str]]:
        return list(self.sim.calls)

    def touching(self, name: str) -> list[tuple[str, str]]:
        return [call for call in self.sim.calls if name in call[1].split(",")]

    def crash(self, name: str, ago: float = 3.0) -> None:
        """The node died `ago` seconds before now: its peers have not
        heard it for longer than SILENT_MS already."""
        self.sim.nodes[name]["crashed_unix"] = self.clock() - ago


class Decide(Base):
    def test_hysteresis_three_samples_over_700_then_join_standby_first(self):
        controller = self.controller()
        self.sim.set_load(650)
        decisions = self.ticks(controller, 5)
        self.assertTrue(all(item[0]["action"] == "hold" for item in decisions))
        self.sim.set_load(760)
        first, second, third = self.ticks(controller, 3)
        self.assertEqual(first[0]["action"], "hold")
        self.assertEqual(second[0]["action"], "hold")
        self.assertEqual(third[0]["action"], "join")
        # 5 x 760 = 3,800 permille / 600 = 7 edges: two more, at most two an
        # action; the standby edges first.
        self.assertEqual(third[0]["inputs"]["desired"], 7)
        self.assertEqual(third[0]["targets"], [("lon-edge-6", "start"), ("lon-edge-7", "start")])
        self.assertEqual(self.mutating(), [("start", "lon-edge-6"), ("start", "lon-edge-7")])

    def test_an_unwarmed_standby_is_not_ready_and_its_check_runs_again(self):
        """The deploy's start exits 6 (the node started, its warm record not
        complete): the join does not count it as ready; a push of it runs
        its check again (its activation stops the service first), and it
        serves once that push ends warm. --allow-unwarmed is never asked."""
        self.sim.unwarmed["lon-edge-6"] = 1
        controller = self.controller()
        self.sim.set_load(760)
        self.ticks(controller, 3)
        self.assertEqual(self.touching("lon-edge-6"), [("start", "lon-edge-6")])
        self.ticks(controller, 1)
        unwarmed = self.of("unwarmed")
        self.assertEqual([(item["node"], item["rerun"]) for item in unwarmed],
                         [("lon-edge-6", 1)])
        # Not serving though its service runs: its check runs again.
        self.assertNotIn("lon-edge-6", [item["node"] for item in self.of("serving")])
        self.assertEqual(self.touching("lon-edge-6"), [("start", "lon-edge-6"),
                                                      ("push", "lon-edge-6")])
        self.ticks(controller, 2)
        self.assertIn("lon-edge-6", [item["node"] for item in self.of("serving")])
        self.assertEqual(len(self.touching("lon-edge-6")), 2)
        self.assertEqual([item for item in self.of("failure") if item["node"] == "lon-edge-6"],
                         [])

    def test_a_node_that_stays_unwarmed_fails_its_join_and_is_parked(self):
        self.sim.unwarmed["lon-edge-6"] = 10
        controller = self.controller()
        self.sim.set_load(760)
        self.ticks(controller, 8)
        self.assertEqual(self.touching("lon-edge-6"),
                         [("start", "lon-edge-6"), ("push", "lon-edge-6"),
                          ("push", "lon-edge-6"), ("stop", "lon-edge-6")])
        self.assertEqual(len(self.of("unwarmed")), auto.WARM_RERUNS)
        failures = [item for item in self.of("failure") if item["node"] == "lon-edge-6"]
        self.assertEqual([item["what"] for item in failures], ["warm"])
        self.assertNotIn("lon-edge-6", [item["node"] for item in self.of("serving")])

    def test_the_band_between_450_and_700_does_nothing(self):
        controller = self.controller()
        for load in (460, 600, 699, 520):
            self.sim.set_load(load)
            self.ticks(controller, 200, step=2.0)  # 400 s at each
        self.assertEqual(self.mutating(), [])

    def test_nodes_still_joining_count_and_the_cooldown_holds(self):
        controller = self.controller()
        self.sim.set_load(900)
        self.ticks(controller, 3)
        self.assertEqual(len(self.mutating()), 2)
        # The two started edges do not advertise yet (a slow boot): the
        # load stays high, but they count, and the cooldown holds.
        original = self.sim.observe

        def slow(roster):
            out = original(roster)
            for name in ("lon-edge-6", "lon-edge-7"):
                out[name]["advertised"] = False
            return out

        self.sim.observe = slow
        decisions = self.ticks(controller, 20)  # 40 s
        self.assertEqual(len(self.mutating()), 2)
        self.assertTrue(any("cooldown" in item[0]["reason"] for item in decisions))
        # Past the cooldown: 5 x 900 / 600 = 8; 5 serving + 2 joining = 7: one more.
        self.clock.advance(30)
        decisions = self.ticks(controller, 1)
        self.assertEqual(decisions[0][0]["targets"], [("lon-edge-8", "start")])
        self.assertEqual(decisions[0][0]["inputs"]["joining"], ["lon-edge-6", "lon-edge-7"])

    def test_a_join_serves_only_when_its_node_says_ready(self):
        controller = self.controller()
        self.sim.set_load(760)
        self.ticks(controller, 3)
        # Started, listed fresh by the entry, but its /api/node says
        # starting: not serving yet.
        self.sim.nodes["lon-edge-6"].update(ready=False, listed=True)
        self.ticks(controller, 3)
        self.assertNotIn("lon-edge-6", [item["node"] for item in self.of("serving")])
        self.sim.nodes["lon-edge-6"].update(ready=True, listed=None)
        self.ticks(controller, 1)
        self.assertIn("lon-edge-6", [item["node"] for item in self.of("serving")])

    def test_one_edge_over_850_with_the_region_over_600_adds_an_edge(self):
        """Wave 9 C3: a ramp fills the edges unevenly; at a mean of 700 one
        may be past 900 already. One edge at 850 or more for 3 samples with
        the mean at 600 or more adds an edge (one, when the mean alone asks
        for none); with the mean under 600 it is the edges' rebalance, not
        the controller's, and nothing is added."""
        controller = self.controller()
        self.sim.set_load(450)
        self.sim.set_load(900, ["lon-edge-1"])
        decisions = self.ticks(controller, 5)
        self.assertTrue(all(item[0]["action"] == "hold" for item in decisions), decisions)
        self.assertEqual(self.mutating(), [])
        self.sim.set_load(620)
        self.sim.set_load(880, ["lon-edge-1"])
        first, second, third = self.ticks(controller, 3)
        self.assertEqual(first[0]["action"], "hold")
        self.assertEqual(second[0]["action"], "hold")
        self.assertEqual(third[0]["action"], "join", third)
        self.assertIn("an edge at 880 >= 850", third[0]["reason"])
        self.assertEqual(third[0]["inputs"]["max_permille"], 880)
        # 4 x 620 + 880 = 3,360 / 600 = 6 edges: one more.
        self.assertEqual(third[0]["targets"], [("lon-edge-6", "start")])

    def test_an_edge_hot_from_load_no_rebalance_moves_adds_no_edge(self):
        """Wave 9 review: an edge at 880 whose load is its LL-HLS readers'
        CPU (3 UDP listeners, at 6.2 permille each 19 of the 80 over the
        rebalance target) is not cooled by a new edge: none is added; its
        UDP listeners' load is reported."""
        controller = self.controller()
        self.sim.set_load(620)
        self.sim.set_load(880, ["lon-edge-1"])
        self.sim.nodes["lon-edge-1"]["udp"] = 3
        self.sim.nodes["lon-edge-1"]["hls"] = 4
        decisions = self.ticks(controller, 4)
        self.assertTrue(all(item[0]["action"] == "hold" for item in decisions), decisions)
        self.assertEqual(self.mutating(), [])
        self.assertEqual(decisions[-1][0]["inputs"]["hot_movable_permille"], 19)
        self.assertEqual(decisions[-1][0]["inputs"]["hot_streak"], 0)

    def test_refusals_in_two_samples_add_one_edge(self):
        controller = self.controller()
        self.sim.set_load(500)
        self.ticks(controller, 1)
        self.sim.nodes["lon-edge-2"]["refusals"] = 3
        self.ticks(controller, 1)
        self.sim.nodes["lon-edge-2"]["refusals"] = 9
        decisions = self.ticks(controller, 1)
        self.assertEqual(decisions[0][0]["action"], "join")
        self.assertEqual(decisions[0][0]["targets"], [("lon-edge-6", "start")])

    def test_after_the_standby_edges_a_slot_is_created_then_pushed(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        controller = self.controller()
        self.sim.set_load(950)
        self.ticks(controller, 3)
        # The creates of one tick in one call; the push once both exist.
        self.assertEqual(self.mutating(), [("create", "lon-edge-11,lon-edge-12")])
        self.ticks(controller, 1)
        self.assertEqual(self.mutating(), [("create", "lon-edge-11,lon-edge-12"),
                                           ("push", "lon-edge-11,lon-edge-12")])
        self.ticks(controller, 1)
        kinds = self.kinds()
        self.assertIn("created", kinds)
        self.assertEqual(kinds.count("serving"), 2)
        serving = self.of("serving")
        self.assertTrue(all(item["trigger_to_fresh_s"] >= 0 for item in serving))
        # Every edge of the roster serves: nothing left to add.
        self.clock.advance(120)
        decisions = self.ticks(controller, 3)
        self.assertIn("no edge left", decisions[-1][0]["reason"])

    def test_a_create_over_the_budget_is_refused_and_nothing_is_created(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.budget_ok = False
        controller = self.controller()
        self.sim.set_load(950)
        self.ticks(controller, 4)
        self.assertEqual(self.mutating(), [])
        # Refused on the worker by the budget guard, before the create.
        failures = self.of("failure")
        self.assertEqual({item["what"] for item in failures}, {"create"})
        self.assertTrue(all("budget" in item["detail"] for item in failures))
        self.assertEqual(len(controller.backend.calls), 0)

    def test_scale_down_waits_300_s_then_drains_the_edge_with_fewest_listeners(self):
        for index in range(6, 9):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        controller = self.controller()
        self.sim.set_load(300)
        self.sim.nodes["lon-edge-3"]["udp"] = 7
        decisions = self.ticks(controller, 149)  # 298 s
        self.assertEqual(self.mutating(), [])
        self.assertTrue(all(item[0]["action"] in ("hold", "wait") for item in decisions))
        decisions = self.ticks(controller, 2)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-3")])
        drained = [item[0] for item in decisions if item[0]["action"] == "drain"]
        self.assertEqual(drained[0]["inputs"]["victim_listeners"], 7)
        self.assertGreaterEqual(drained[0]["inputs"]["low_for_s"], 300)
        self.assertLessEqual(drained[0]["inputs"]["projected_one_fewer"], 450)

    def test_scale_down_never_drains_into_no_room(self):
        """tools6: even when the mean with one edge fewer is under the
        band (here a band of 900 a test sets), the victim is drained only
        when the other serving edges have room for its load under the
        rebalance target (800): seven others at 750 have 350 for its 750."""
        for index in range(6, 9):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        controller = self.controller(config=auto.Config(down_permille=900, up_permille=950,
                                                        up_max_permille=990))
        self.sim.set_load(750)
        self.sim.nodes["lon-edge-3"]["udp"] = 7
        decisions = self.ticks(controller, 200)
        self.assertEqual(self.mutating(), [])
        held = [item[0] for item in decisions if item[0]["reason"].startswith("no room")]
        self.assertTrue(held)
        self.assertIn("no room to drain lon-edge-3: it carries 750 permille, the other serving "
                      "edges have 350 under 800", held[-1]["reason"])
        self.assertEqual(held[-1]["inputs"]["room"]["victim"], "lon-edge-3")
        self.assertEqual(auto.no_room("a", {"a": 300, "b": 500}), None)
        self.assertEqual(auto.no_room("a", {"a": 300, "b": 600})["room_permille"], 200)

    def test_never_fewer_than_three_edges(self):
        """B's seam: a listener holds two edges and needs a third to move
        to, so the controller never drains a region below three."""
        for index in (4, 5):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = False
        controller = self.controller()
        self.sim.set_load(50)
        self.ticks(controller, 400)
        self.assertEqual(self.mutating(), [])

    def test_scale_down_needs_live_gauges(self):
        controller = self.controller()
        self.sim.set_load(100)
        for node in self.sim.nodes.values():
            node["udp"] = None
        decisions = self.ticks(controller, 200)
        self.assertEqual(self.mutating(), [])
        self.assertIn("live gauges", decisions[-1][0]["reason"])

    def test_no_edge_removed_within_five_minutes_of_an_add(self):
        controller = self.controller()
        self.sim.set_load(900)
        self.ticks(controller, 3)
        self.sim.set_load(100)
        self.ticks(controller, 140)  # 280 s after the add
        self.assertNotIn("drain", [verb for verb, _ in self.mutating()])
        self.ticks(controller, 20)
        self.assertIn("drain", [verb for verb, _ in self.mutating()])
        drained = [name for verb, name in self.mutating() if verb == "drain"]
        self.assertEqual(len(drained), 1)


class Observe(unittest.TestCase):
    def test_node_view_reads_the_node_status(self):
        roster = {"london": [
            {"name": "lon-dist-a", "role": "distributor", "site": "london", "created": True,
             "vlan": "10.80.3.11", "public": "192.0.2.11"},
            {"name": "lon-edge-1", "role": "edge", "site": "london", "created": True,
             "vlan": "10.80.3.21", "public": "192.0.2.21"},
            {"name": "lon-edge-2", "role": "edge", "site": "london", "created": True,
             "vlan": "10.80.3.22", "public": "192.0.2.22"}]}
        documents = {
            "lon-dist-a": {"node_id": "lon-dist-a", "role": "distributor", "state": "ready",
                           "ready": True, "hello_draining": False, "drain_reasons": [],
                           "process": {"cpu_permille": 650},
                           "children": {"lease": {"children": 7}},
                           "cache_mesh": {"children": 3, "peers": [
                               {"peer": "10.80.3.21:29101", "heard_age_ms": 40},
                               {"peer": "10.80.3.99:29101", "heard_age_ms": 9}]}},
            "lon-edge-1": {"node_id": "lon-edge-1", "role": "edge", "state": "ready",
                           "ready": True, "drain_reasons": [],
                           "client_udp": {"subscribers": 240, "hls_sessions": 4,
                                          "hls_moved_active": 1, "hls_requests_in_flight": 2,
                                          "refusals": 5, "rebalance_moves": 11},
                           "parents": [{"address": "10.80.3.11", "streams": [
                               {"stream_id": 4001, "track": "audio", "source_age_ms": 3},
                               {"stream_id": 3001, "track": "audio", "source_age_ms": 1}]}]},
            "lon-edge-2": None}
        entries = {"london": {"caches": [{"node_id": "lon-edge-1", "fresh": True,
                                          "load_permille": 610}]}}
        obs = auto.merge_views(roster, entries, documents)
        self.assertEqual(obs["lon-dist-a"]["cpu"], 0.65)
        self.assertEqual(obs["lon-dist-a"]["lease_children"], 7)
        self.assertEqual(obs["lon-dist-a"]["cache_children"], 3)
        self.assertEqual(obs["lon-dist-a"]["heard"], {"lon-edge-1": 40, "10.80.3.99": 9})
        self.assertTrue(obs["lon-dist-a"]["advertised"])  # ready, hellos not draining
        edge = obs["lon-edge-1"]
        self.assertEqual((edge["udp_subscribers"], edge["hls_sessions"], edge["hls_moved_active"],
                          edge["hls_requests_in_flight"], edge["refusals"],
                          edge["rebalance_moves"]), (240, 4, 1, 2, 5, 11))
        self.assertEqual(edge["fed"], {"lon-dist-a": 1})
        self.assertTrue(edge["advertised"] and edge["entry_answered"])
        self.assertEqual(edge["load_permille"], 610)
        self.assertFalse(obs["lon-edge-2"]["answered"])
        self.assertIsNone(obs["lon-edge-2"]["udp_subscribers"])
        self.assertFalse(obs["lon-edge-2"]["advertised"])
        # No entry answered: every node of the site knows it.
        obs = auto.merge_views(roster, {"london": None}, documents)
        self.assertFalse(any(view["entry_answered"] for view in obs.values()))
        self.assertFalse(obs["lon-edge-1"]["advertised"])


class Drain(Base):
    def drain_one(self, name: str, controller):
        controller.state["actions"][name] = {
            "kind": "drain", "site": "london", "phase": "drain", "drain_sent": False,
            "started_unix": self.clock(), "deadline_unix": self.clock() + 60, "reason": "test"}
        controller.state["regions"].setdefault("london", {})["last_action_unix"] = self.clock()

    def test_a_drain_that_does_not_reach_zero_is_resumed_and_never_deleted(self):
        self.sim.nodes["lon-edge-11"].update(created=True, running=True)
        controller = self.controller()
        self.drain_one("lon-edge-11", controller)
        self.sim.set_load(500)
        self.ticks(controller, 35)  # 70 s; the listeners stay
        self.assertEqual(self.mutating(), [("drain", "lon-edge-11"), ("resume", "lon-edge-11")])
        self.assertEqual(self.of("failure")[0]["what"], "drain-timeout")
        self.assertTrue(self.sim.nodes["lon-edge-11"]["created"])

    def test_every_one_of_the_four_gauges_holds_the_stop(self):
        """UDP subscribers and LL-HLS sessions at 0 are not enough: a moved
        session that still fetches, or a read in flight, holds the stop."""
        for gauge in ("moved", "reads"):
            self.sim.calls.clear()
            self.sim.nodes["lon-edge-11"].update(
                {"created": True, "running": True, "udp": 0, "hls": 0, "moved": 0, "reads": 0,
                 gauge: 1})
            controller = self.controller()
            self.drain_one("lon-edge-11", controller)
            self.ticks(controller, 10)
            self.assertNotIn(("stop", "lon-edge-11"), self.mutating(), gauge)
            self.sim.nodes["lon-edge-11"][gauge] = 0
            self.ticks(controller, 3)
            self.assertIn(("stop", "lon-edge-11"), self.mutating(), gauge)

    def test_a_drained_slot_is_stopped_kept_warm_then_released_before_its_next_hour(self):
        self.sim.nodes["lon-edge-11"].update(created=True, running=True,
                                             created_unix=self.clock() - 600)
        controller = self.controller()
        self.drain_one("lon-edge-11", controller)
        self.sim.set_load(500)
        self.ticks(controller, 2)
        self.sim.nodes["lon-edge-11"].update(udp=0, hls=0)
        # Zero on two observations a second or more apart, then the stop.
        self.ticks(controller, 1)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-11")])
        self.ticks(controller, 2)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-11"), ("stop", "lon-edge-11")])
        self.assertIn("lon-edge-11", controller.state["warm"])
        kinds = self.kinds()
        self.assertLess(kinds.index("drain_start"), kinds.index("drain_end"))
        # 600 s old: its paid hour ends 3,000 s from now; released 300 s
        # before.
        self.clock.advance(2600)
        self.ticks(controller, 1)
        self.assertNotIn(("delete", "lon-edge-11"), self.mutating())
        self.clock.advance(120)
        self.ticks(controller, 1)
        self.assertEqual(self.mutating()[-1], ("delete", "lon-edge-11"))
        self.ticks(controller, 1)
        self.assertNotIn("lon-edge-11", controller.state["warm"])

    def test_a_warm_slot_back_in_service_is_never_released(self):
        self.sim.nodes["lon-edge-11"].update(created=True, running=False,
                                             created_unix=self.clock() - 3500)
        controller = self.controller()
        controller.state["warm"]["lon-edge-11"] = {"since_unix": self.clock() - 100,
                                                   "proof": "drained-to-zero"}
        # Someone started it again: it serves, it must not be deleted.
        self.sim.nodes["lon-edge-11"]["running"] = True
        self.sim.set_load(500)
        self.ticks(controller, 1)
        self.assertNotIn(("delete", "lon-edge-11"), self.mutating())
        self.assertNotIn("lon-edge-11", controller.state["warm"])
        self.assertIn("warm_left", self.kinds())

    def test_a_create_that_fails_after_the_instance_exists_is_parked_warm(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        controller = self.controller(config=auto.Config(max_per_action=1))
        original = self.sim.push

        def failing_push(names):
            self.sim.record("push", ",".join(names))
            raise auto.Refused("bootstrap failed")

        self.sim.push = failing_push
        self.sim.set_load(950)
        self.ticks(controller, 5)
        self.sim.push = original
        self.assertEqual(self.mutating(), [("create", "lon-edge-11"), ("push", "lon-edge-11"),
                                           ("stop", "lon-edge-11")])
        self.assertEqual(controller.state["warm"]["lon-edge-11"]["proof"], "never-served")
        self.assertIn("parked", self.kinds())

    def test_the_warm_pool_is_taken_back_before_a_new_create(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.nodes["lon-edge-12"].update(created=True, running=False)
        controller = self.controller()
        controller.state["warm"]["lon-edge-12"] = {"since_unix": self.clock(),
                                                   "proof": "drained-to-zero"}
        self.sim.set_load(950)
        self.ticks(controller, 4)
        self.assertEqual(self.mutating()[:1], [("start", "lon-edge-12")])
        self.assertEqual(self.mutating()[1:], [("create", "lon-edge-11"), ("push", "lon-edge-11")])
        self.assertNotIn("lon-edge-12", controller.state["warm"])

    def test_a_drained_standby_edge_is_stopped_not_deleted(self):
        self.sim.nodes["lon-edge-6"]["running"] = True
        controller = self.controller()
        self.drain_one("lon-edge-6", controller)
        self.sim.nodes["lon-edge-6"].update(udp=0, hls=0)
        self.ticks(controller, 4)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-6"), ("stop", "lon-edge-6")])
        self.assertNotIn("lon-edge-6", controller.state["warm"])
        self.assertEqual(controller.state["actions"], {})

    def test_a_parked_slot_is_kept_while_its_stop_runs(self):
        """A create whose push failed is parked: until its stop is done the
        slot is created, not running and not yet held out (as on Linode,
        where only the stop puts it in standby_edges). It is kept in the
        warm pool all that time, never let go as "in service again" to
        bill until down."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        controller = self.controller(config=auto.Config(max_per_action=1))

        def failing_push(names):
            self.sim.record("push", ",".join(names))
            raise auto.Refused("bootstrap failed")

        self.sim.push = failing_push
        self.sim.set_load(950)
        self.ticks(controller, 4)  # created, its push failed
        self.sim.stop_gate = threading.Event()
        self.addCleanup(self.sim.stop_gate.set)
        self.ticks(controller, 1, wait=False)  # parked: its stop handed over
        deadline = time.monotonic() + 5
        while ("stop", "lon-edge-11") not in self.mutating() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(("stop", "lon-edge-11"), self.mutating())
        self.ticks(controller, 3, wait=False)  # the stop still runs
        self.assertIn("lon-edge-11", controller.state["warm"])
        self.assertNotIn("warm_left", self.kinds())
        self.sim.stop_gate.set()
        self.assertTrue(controller.worker.wait_idle(5))
        self.ticks(controller, 2)
        self.assertIn("lon-edge-11", controller.state["warm"])
        self.assertNotIn("warm_left", self.kinds())
        self.assertNotIn(("delete", "lon-edge-11"), self.mutating())

    def test_without_the_warm_pool_a_drained_slot_is_deleted_after_it_stops(self):
        self.sim.nodes["lon-edge-11"].update(created=True, running=True)
        config = auto.Config(warm_pool=False)
        controller = self.controller(config=config)
        self.drain_one("lon-edge-11", controller)
        self.sim.nodes["lon-edge-11"].update(udp=0, hls=0)
        self.ticks(controller, 4)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-11"), ("stop", "lon-edge-11"),
                                           ("delete", "lon-edge-11")])


class Forwarders(Base):
    def drain_forwarder(self, controller):
        self.sim.nodes["lon-dist-c"].update(created=True, running=True)
        controller.state["actions"]["lon-dist-c"] = {
            "kind": "drain", "site": "london", "phase": "drain", "drain_sent": False,
            "role": "distributor", "started_unix": self.clock(),
            "deadline_unix": self.clock() + 60, "reason": "test"}

    def test_a_forwarder_that_still_feeds_edges_is_never_stopped_or_deleted(self):
        controller = self.controller(config=auto.Config(warm_pool=False))
        self.drain_forwarder(controller)
        # Its children unknown: not drained.
        self.ticks(controller, 35)
        self.assertEqual(self.mutating(), [("drain", "lon-dist-c"), ("resume", "lon-dist-c")])
        self.assertIn("lease_children None", self.of("failure")[0]["detail"])
        # Children known but one still fed: the same.
        self.sim.calls.clear()
        self.sim.nodes["lon-dist-c"].update(lease=0, cache=3)
        self.drain_forwarder(controller)
        self.ticks(controller, 35)
        self.assertNotIn("stop", [verb for verb, _ in self.mutating()])
        self.assertNotIn("delete", [verb for verb, _ in self.mutating()])
        # Both 0: the forwarder gate passes (no lease pair left at it),
        # then stopped, then deleted.
        self.sim.calls.clear()
        self.sim.nodes["lon-dist-c"].update(lease=0, cache=0)
        self.drain_forwarder(controller)
        self.ticks(controller, 4)
        self.assertEqual(self.mutating(), [("drain", "lon-dist-c"), ("stop", "lon-dist-c"),
                                           ("delete", "lon-dist-c")])
        gate = self.of("gate")[-1]
        self.assertTrue(gate["ok"])
        self.assertGreaterEqual(gate["polls"], 2)

    def test_a_forwarder_the_gate_refuses_is_resumed_not_stopped(self):
        """Children at 0 by the gauges, but the gate still sees a lease
        pair at the slot that no other parent of the child carries: the
        gate refuses, the slot is resumed, nothing stopped or deleted."""
        config = auto.Config(warm_pool=False, gate_timeout_s=0.5)
        controller = self.controller(config=config)
        self.drain_forwarder(controller)
        self.sim.nodes["lon-dist-c"].update(lease=0, cache=0, pairs=[("lon-edge-1", STREAM)])
        # Fed itself (not an idle mesh), so the gate judges the pair.
        self.sim.nodes["lon-dist-c"]["rows"] = [
            {"address": self.sim.nodes["lon-dist-a"]["vlan"], "streams": [
                {"stream_id": STREAM, "track": "audio", "source_age_ms": 1, "objects_1s": 1000,
                 "objects_10s": 10000}]}]
        self.sim.nodes["lon-edge-1"]["rows"] = [
            {"address": self.sim.nodes["lon-dist-c"]["vlan"], "streams": [
                {"stream_id": STREAM, "track": "audio", "class": "audio", "source_age_ms": 1,
                 "objects_1s": 1000}]}]
        self.ticks(controller, 4)
        self.assertEqual(self.mutating(), [("drain", "lon-dist-c"), ("resume", "lon-dist-c")])
        failure = self.of("failure")[-1]
        self.assertEqual(failure["what"], "stop")
        self.assertIn("forwarder gate refused", failure["detail"])
        self.assertFalse(self.of("gate")[-1]["ok"])
        self.assertTrue(self.sim.nodes["lon-dist-c"]["created"])

    def test_cpu_alone_adds_the_slot(self):
        """The forwarder CPU from /api/node's process.cpu_permille: over
        0.60 for three samples adds the region's forwarder slot, with no
        lease-children figure at all."""
        controller = self.controller(config=auto.Config(forwarders=True))
        self.sim.nodes["lon-dist-a"]["cpu"] = 0.55
        self.ticks(controller, 6)
        self.assertEqual(self.mutating(), [])
        self.sim.nodes["lon-dist-a"]["cpu"] = 0.65
        first, second, third = self.ticks(controller, 3)
        self.assertEqual([item[0]["action"] for item in (first, second)], ["hold", "hold"])
        self.assertEqual(third[0]["action"], "join")
        self.assertEqual(third[0]["tier"], "forwarder")
        self.assertEqual(third[0]["targets"], [("lon-dist-c", "create")])
        self.assertEqual(third[0]["inputs"]["cpu"]["lon-dist-a"], 0.65)
        self.assertIsNone(third[0]["inputs"]["lease_children"]["lon-dist-a"])
        self.assertEqual(self.mutating(), [("create", "lon-dist-c")])
        self.ticks(controller, 2)
        self.assertEqual(self.mutating(), [("create", "lon-dist-c"), ("push", "lon-dist-c")])
        self.assertIn("lon-dist-c", [item["node"] for item in self.of("serving")])


class Worker(Base):
    newyork = True

    def test_a_create_does_not_block_the_tick(self):
        """A create that takes minutes runs on the worker: the tick goes on
        observing and deciding in every region (a drain in New York goes
        out while London's create is in flight), and the join's phase is
        saved before the job is handed over."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.set_load(950, [name for name in self.sim.nodes if name.startswith("lon-")])
        self.sim.set_load(100, [name for name in self.sim.nodes if name.startswith("new-")])
        self.sim.create_gate = threading.Event()
        self.addCleanup(self.sim.create_gate.set)
        controller = self.controller(config=auto.Config(down_hold_s=10, max_per_action=1))
        self.ticks(controller, 3, wait=False)
        self.assertEqual(self.mutating(), [])  # the create has not been called yet...
        deadline = time.monotonic() + 5
        while not self.mutating() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.mutating(), [("create", "lon-edge-11")])  # ...and now waits
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["actions"]["lon-edge-11"]["phase"], "creating")
        slowest = 0.0
        for _ in range(10):
            began = time.monotonic()
            self.ticks(controller, 1, wait=False)
            slowest = max(slowest, time.monotonic() - began)
        self.assertLess(slowest, 1.0)
        drains = [call for call in self.mutating() if call[0] == "drain"]
        self.assertEqual(len(drains), 1)
        self.assertTrue(drains[0][1].startswith("new-edge-"))
        self.assertEqual(controller.state["actions"]["lon-edge-11"]["phase"], "creating")
        self.sim.create_gate.set()
        self.assertTrue(controller.worker.wait_idle(10))
        self.ticks(controller, 3)
        self.assertIn(("push", "lon-edge-11"), self.mutating())
        self.assertIn("lon-edge-11", [item["node"] for item in self.of("serving")])

    def test_a_restart_adopts_a_create_in_flight(self):
        """The controller dies while its worker creates a slot; the
        instance came up but the lab state never recorded it. A controller
        started again adopts it by its run tag and label (and names an
        instance that is not a missing slot, leaving it to down), finds the
        join in `creating`, and pushes it: the slot is never created
        twice."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.create_gate = threading.Event()
        self.addCleanup(self.sim.create_gate.set)
        self.sim.die_in_create = True
        first = self.controller(config=auto.Config(max_per_action=1))
        self.sim.set_load(950)
        self.ticks(first, 3, wait=False)
        deadline = time.monotonic() + 5
        while not self.mutating() and time.monotonic() < deadline:
            time.sleep(0.01)
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["actions"]["lon-edge-11"]["phase"], "creating")
        first.close()  # dead; its worker never reports
        self.sim.orphans = [{"name": "lon-edge-11", "label": "ntsm-t1-lon-edge-11"},
                            {"name": "lon-load-9", "label": "ntsm-t1-lon-load-9"}]
        again = self.controller(config=auto.Config(max_per_action=1))
        again.begin()
        adopted = self.of("adopted")
        self.assertEqual([(item.get("name"), item["adopted"]) for item in adopted],
                         [("lon-edge-11", True), (None, False)])
        self.ticks(again, 3)
        self.assertEqual([call for call in self.mutating() if call[0] == "create"],
                         [("create", "lon-edge-11")])
        self.assertIn(("push", "lon-edge-11"), self.mutating())
        self.assertIn("lon-edge-11", [item["node"] for item in self.of("serving")])

    def test_a_create_lost_with_its_controller_is_asked_again(self):
        """Nothing to adopt: the instance was never made. The join is
        found in `creating` and its create asked again, once."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.create_gate = threading.Event()
        self.addCleanup(self.sim.create_gate.set)
        self.sim.die_in_create = True
        first = self.controller(config=auto.Config(max_per_action=1))
        self.sim.set_load(950)
        self.ticks(first, 3, wait=False)
        deadline = time.monotonic() + 5
        while not self.mutating() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.mutating(), [("create", "lon-edge-11")])
        first.close()
        self.sim.create_gate = None
        self.sim.die_in_create = False
        again = self.controller(config=auto.Config(max_per_action=1))
        again.begin()
        self.ticks(again, 3)
        self.assertEqual([call for call in self.mutating() if call[0] == "create"],
                         [("create", "lon-edge-11"), ("create", "lon-edge-11")])
        self.assertIn("requeued", self.kinds())
        self.assertIn(("push", "lon-edge-11"), self.mutating())

    def await_call(self, call) -> None:
        deadline = time.monotonic() + 5
        while call not in self.mutating() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(call, self.mutating())

    def finish_create_during_the_observation(self, controller) -> None:
        """The create waits at its gate; it is let go during the next
        tick's observation, and its result is in before that tick would
        take results after observing."""
        def release():
            if self.sim.create_gate is not None and not self.sim.create_gate.is_set():
                self.sim.create_gate.set()
                self.assertTrue(controller.worker.wait_idle(5))

        self.sim.on_observe.append(release)
        self.ticks(controller, 1, wait=False)
        self.assertTrue(controller.worker.wait_idle(5))

    def test_a_create_that_finishes_during_the_observation_is_pushed(self):
        """The result of a create that finished while a tick observed is
        handled with a roster that already has the slot: it is pushed and
        serves (the push is never refused as "not created")."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.set_load(950)
        self.sim.create_gate = threading.Event()
        self.addCleanup(self.sim.create_gate.set)
        controller = self.controller(config=auto.Config(max_per_action=1))
        self.ticks(controller, 3, wait=False)
        self.await_call(("create", "lon-edge-11"))
        self.finish_create_during_the_observation(controller)
        self.ticks(controller, 3)
        self.assertIn(("push", "lon-edge-11"), self.mutating())
        self.assertIn("lon-edge-11", [item["node"] for item in self.of("serving")])
        self.assertEqual(self.of("failure"), [])

    def test_a_partial_create_pushes_the_slot_it_made(self):
        """A batch whose create made one slot and failed for the other,
        finishing while a tick observed: the slot that exists is pushed
        and serves (never left created, unpushed and unheld to bill until
        down); the other backs off."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.set_load(950)
        self.sim.create_gate = threading.Event()
        self.addCleanup(self.sim.create_gate.set)
        self.sim.partial_create = True
        controller = self.controller()
        self.ticks(controller, 3, wait=False)
        self.await_call(("create", "lon-edge-11,lon-edge-12"))
        self.finish_create_during_the_observation(controller)
        self.ticks(controller, 3)
        self.assertIn(("push", "lon-edge-11"), self.mutating())
        self.assertIn("lon-edge-11", [item["node"] for item in self.of("serving")])
        self.assertEqual([item["node"] for item in self.of("backoff")], ["lon-edge-12"])
        self.assertFalse(self.sim.nodes["lon-edge-12"]["created"])

    def test_a_refused_create_backs_off(self):
        """A slot whose create was refused (no capacity) is not asked again
        for 10 minutes: the next slot is asked instead, and the refused one
        only after its back-off."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.refuse_create = {"lon-edge-11", "lon-edge-12"}
        controller = self.controller(config=auto.Config(max_per_action=1))
        self.sim.set_load(950)
        self.ticks(controller, 4)
        self.assertEqual(self.mutating(), [("create", "lon-edge-11")])
        backoff = self.of("backoff")
        self.assertEqual(backoff[0]["node"], "lon-edge-11")
        self.ticks(controller, 60)  # 120 s: past the cooldown, inside the back-off
        creates = [name for verb, name in self.mutating() if verb == "create"]
        self.assertEqual(creates, ["lon-edge-11", "lon-edge-12"])
        self.ticks(controller, 200)  # 520 s: both in back-off, nothing asked
        creates = [name for verb, name in self.mutating() if verb == "create"]
        self.assertEqual(creates, ["lon-edge-11", "lon-edge-12"])
        self.sim.refuse_create = set()
        self.ticks(controller, 50)  # past 600 s since the first refusal
        creates = [name for verb, name in self.mutating() if verb == "create"]
        self.assertEqual(creates[:3], ["lon-edge-11", "lon-edge-12", "lon-edge-11"])
        refusals = [item for item in self.of("failure") if item["what"] == "create"]
        self.assertEqual(len(refusals), 2)


class Lost(Base):
    def lost_at(self, controller, name: str, limit: int = 30) -> int:
        """The tick (1-based) at which `name` was judged lost."""
        for index in range(1, limit + 1):
            self.ticks(controller, 1)
            if any(item["node"] == name for item in self.of("lost")):
                return index
        self.fail(f"{name} was not judged lost in {limit} ticks")

    def test_no_entry_answer_is_blind_not_lost(self):
        """An edge is dead, but its site's entry does not answer: blind,
        not lost. Nothing is done however long it lasts. Once the entry
        answers again, the edge is lost after five ticks in a row, not
        before."""
        controller = self.controller()
        self.ticks(controller, 2)
        self.crash("lon-edge-2")
        self.sim.entry_down = {"london"}
        self.ticks(controller, 30)
        self.assertEqual(self.of("lost"), [])
        self.assertEqual(self.mutating(), [])
        self.assertEqual(len(self.of("observer_blind")), 1)
        self.assertNotIn("lon-edge-2", controller.state["lost"])
        self.sim.entry_down = set()
        self.assertEqual(self.lost_at(controller, "lon-edge-2"), 5)
        self.assertEqual(self.of("lost")[0]["samples"], 5)

    def test_a_blind_tick_breaks_the_row(self):
        controller = self.controller()
        self.crash("lon-edge-2")
        self.ticks(controller, 4)
        self.sim.entry_down = {"london"}
        self.ticks(controller, 1)
        self.sim.entry_down = set()
        self.ticks(controller, 4)
        self.assertEqual(self.of("lost"), [])
        self.ticks(controller, 1)
        self.assertEqual([item["node"] for item in self.of("lost")], ["lon-edge-2"])

    def test_a_whole_site_silent_to_the_operator_changes_nothing(self):
        """The operator's path to every London node is cut: no node and no
        entry answers. Every node is alive and its peers hear it. Blind:
        nothing is judged lost, nothing restarted, drained or added, for as
        long as it lasts; the blind event is written once."""
        controller = self.controller()
        self.sim.set_load(900)  # a load that would add edges if seen
        for node in self.sim.nodes.values():
            node["reachable"] = False
        self.ticks(controller, 60)
        self.assertEqual(self.mutating(), [])
        self.assertEqual(self.of("lost"), [])
        self.assertEqual(len(self.of("observer_blind")), 1)
        self.assertEqual(controller.state["lost"], {})

    def test_two_nodes_of_a_site_at_once_is_blind(self):
        controller = self.controller()
        self.crash("lon-edge-2")
        self.crash("lon-edge-3")
        self.ticks(controller, 20)
        self.assertEqual(self.of("lost"), [])
        self.assertEqual(self.mutating(), [])
        self.assertIn("would be lost in one tick", self.of("observer_blind")[0]["why"])

    def test_a_node_its_peers_hear_is_not_lost(self):
        """No answer to the operator and not listed fresh, yet heard inside
        the mesh: never lost. (a) its peers hear it; (b) it is dead to all
        but one peer that still hears it; (c) a forwarder whose hellos are
        not heard but whose children are still fed by it."""
        controller = self.controller()
        edge = self.sim.nodes["lon-edge-2"]
        edge.update(reachable=False, stale=True)
        self.ticks(controller, 20)
        edge.update(reachable=True, stale=False)
        self.crash("lon-edge-2")
        self.sim.hears[("lon-edge-4", "lon-edge-2")] = 300.0
        self.ticks(controller, 20)
        self.sim.hears.clear()
        self.sim.nodes["lon-edge-2"]["crashed_unix"] = None
        self.sim.nodes["lon-dist-a"]["reachable"] = False
        self.sim.peer_age["lon-dist-a"] = 9000.0
        self.ticks(controller, 20)
        self.assertEqual(self.of("lost"), [])
        self.assertEqual(self.mutating(), [])
        self.assertEqual(controller.state["lost"], {})
        # (f) Dead, its peers report it silent, but no child shows a row
        # of it beside a live one (an idle mesh): not lost.
        self.sim.nodes["lon-dist-a"].update(reachable=True, crashed_unix=self.clock() - 30)
        self.sim.peer_age.clear()
        for index in range(1, 6):
            self.sim.nodes[f"lon-edge-{index}"]["rows"] = []
        self.ticks(controller, 20)
        self.assertEqual(self.of("lost"), [])
        # The children are fed by lon-dist-b and not by it: now it is.
        for index in range(1, 6):
            self.sim.nodes[f"lon-edge-{index}"]["rows"] = None
        self.lost_at(controller, "lon-dist-a")

    def test_each_part_of_the_rule_held_alone_is_not_lost(self):
        """Two of the three parts are not enough. (d) It answers the
        operator, though its peers report it silent and the entry does not
        list it. (e) It does not answer and its peers report it silent, but
        the entry lists it fresh."""
        controller = self.controller()
        self.sim.nodes["lon-edge-3"]["stale"] = True
        self.sim.peer_age["lon-edge-3"] = 9000.0
        self.ticks(controller, 20)
        self.sim.nodes["lon-edge-3"]["stale"] = False
        self.sim.nodes["lon-edge-3"]["reachable"] = False
        self.ticks(controller, 20)
        self.assertEqual(self.of("lost"), [])
        self.assertEqual(self.mutating(), [])
        # All three: lost.
        self.sim.nodes["lon-edge-3"]["stale"] = True
        self.assertEqual(self.lost_at(controller, "lon-edge-3"), 5)

    def test_the_ladder_never_restarts_a_node_a_peer_hears(self):
        """Lost, its probe in flight; then a peer hears it again (it does
        not answer the operator): the recovery stops with nothing done."""
        controller = self.controller(config=auto.Config(replace_lost=False))
        self.crash("lon-edge-2")
        self.lost_at(controller, "lon-edge-2")
        self.assertEqual(len(self.of("recover_start")), 1)
        self.sim.hears[("lon-edge-3", "lon-edge-2")] = 50.0
        self.ticks(controller, 40)
        self.assertEqual(self.mutating(), [])
        self.assertEqual(len(self.of("recover_stopped")), 1)
        self.assertEqual(controller.state["actions"], {})

    def test_a_slot_heard_again_is_never_deleted(self):
        """Its restart reports an error, but the slot came back and its
        peers hear it: the recovery fails, and the slot is not deleted."""
        node = self.sim.nodes["lon-edge-11"]
        node.update(created=True, running=True, restart_error=True)
        controller = self.controller(config=auto.Config(replace_lost=False))
        self.crash("lon-edge-11")
        node["ready"] = False  # back, warming, not yet ready
        self.lost_at(controller, "lon-edge-11")
        self.ticks(controller, 20)
        self.assertEqual(self.touching("lon-edge-11"), [("restart", "lon-edge-11")])
        self.assertIn("lon-edge-11", [item["node"] for item in self.of("recover_failed")])

    def test_no_step_while_the_site_is_blind(self):
        """Lost, its probe in flight; then the site's entry stops answering:
        no step is taken until the in-mesh evidence is back."""
        controller = self.controller(config=auto.Config(replace_lost=False))
        self.crash("lon-edge-2")
        self.lost_at(controller, "lon-edge-2")
        self.sim.entry_down = {"london"}
        self.ticks(controller, 30)
        self.assertEqual(self.mutating(), [])
        self.sim.entry_down = set()
        self.ticks(controller, 10)
        self.assertEqual(self.touching("lon-edge-2"), [("restart", "lon-edge-2")])

    def test_a_base_edge_is_held_out_only_when_its_spare_serves(self):
        controller = self.controller()
        self.sim.nodes["lon-edge-6"]["ready"] = False  # the spare never warms
        self.crash("lon-edge-2")
        self.lost_at(controller, "lon-edge-2")
        self.ticks(controller, 5)
        self.assertEqual(self.of("recovered")[0]["node"], "lon-edge-2")
        self.assertEqual(self.of("held_out"), [])
        self.assertNotIn(("drain", "lon-edge-2"), self.mutating())

    def test_an_offline_instance_is_booted(self):
        node = self.sim.nodes["lon-edge-11"]
        node.update(created=True, running=True)
        controller = self.controller(config=auto.Config(replace_lost=False))
        self.crash("lon-edge-11")
        node.update(instance="offline", ssh=None)
        self.lost_at(controller, "lon-edge-11")
        self.ticks(controller, 3)
        self.assertEqual(self.touching("lon-edge-11"), [("boot", "lon-edge-11")])
        self.assertEqual(self.of("recovered")[0]["how"], "boot")

    def test_a_lost_edge_is_replaced_once_then_recovered(self):
        """A serving edge is killed and its unit stays down: lost after
        five ticks; one spare joins at once (the standby first), once
        however long it lasts; the worker's probe finds ssh up and the unit
        not active and restarts it; it answers ready: recovered. A base
        edge whose place the spare took is then held out as the standby."""
        controller = self.controller()
        self.ticks(controller, 2)
        self.crash("lon-edge-2")
        self.assertEqual(self.lost_at(controller, "lon-edge-2"), 5)
        self.assertEqual(self.of("replace_join")[0]["spare"], "lon-edge-6")
        self.assertEqual(self.mutating(), [("start", "lon-edge-6")])
        self.assertEqual(len(self.of("recover_start")), 1)
        self.ticks(controller, 3)
        self.assertEqual([item["step"] for item in self.of("recover_step")], ["restart"])
        self.assertEqual(self.of("recovered")[0]["node"], "lon-edge-2")
        # Held out at once: drained, and stopped once it holds nobody.
        self.assertEqual(self.touching("lon-edge-2"), [("restart", "lon-edge-2"),
                                                       ("drain", "lon-edge-2")])
        self.assertEqual(self.of("held_out")[0]["spare"], "lon-edge-6")
        self.sim.nodes["lon-edge-2"].update(udp=0, hls=0)
        self.ticks(controller, 50)
        self.assertIn(("stop", "lon-edge-2"), self.mutating())
        self.assertEqual([call for call in self.mutating() if call[0] in ("start", "restart")],
                         [("start", "lon-edge-6"), ("restart", "lon-edge-2")])
        self.assertEqual(len(self.of("replace_join")), 1)

    def test_a_first_forwarder_is_never_recovered(self):
        """lon-dist-a dies: lost from in-mesh evidence (its peers no longer
        hear it and its children are fed by lon-dist-b alone). Nothing is
        ever done to it; with --forwarders the region's slot stands in, and
        is drained and stopped when lon-dist-a is ready again."""
        controller = self.controller(config=auto.Config(forwarders=True, warm_pool=False))
        self.ticks(controller, 2)
        self.crash("lon-dist-a")
        self.lost_at(controller, "lon-dist-a")
        self.assertEqual(self.of("replace_join")[0]["spare"], "lon-dist-c")
        self.ticks(controller, 4)
        self.assertEqual(self.touching("lon-dist-c"), [("create", "lon-dist-c"),
                                                       ("push", "lon-dist-c")])
        self.assertIn("lon-dist-c", [item["node"] for item in self.of("serving")])
        self.ticks(controller, 200)  # 400 s lost: no recovery, no cover drained
        self.assertEqual(self.touching("lon-dist-a"), [])
        self.assertEqual(self.of("recover_start"), [])
        self.assertEqual(len(self.touching("lon-dist-c")), 2)
        guard = controller.backend
        for step in ("restart", "boot", "rebuild"):
            with self.assertRaises(auto.Refused):
                guard.recover("lon-dist-a", step, proof="lost")
        # It comes back (systemd, or the operator's replace): the slot goes.
        self.sim.nodes["lon-dist-a"]["crashed_unix"] = None
        self.sim.nodes["lon-dist-c"].update(lease=0, cache=0)
        self.ticks(controller, 6)
        self.assertEqual(self.touching("lon-dist-c")[2:], [("drain", "lon-dist-c"),
                                                           ("stop", "lon-dist-c"),
                                                           ("delete", "lon-dist-c")])
        self.assertEqual(self.touching("lon-dist-a"), [])
        self.assertEqual(self.of("recovered")[-1]["how"], "by itself")

    def test_a_flapping_node_is_quarantined(self):
        """A slot that dies again and again: one recovery, then none within
        ten minutes of it, a second after them, and when it is lost a third
        time it is quarantined: held out and named, never restarted again.
        The spare that joined for it stays; only one ever joined."""
        self.sim.nodes["lon-edge-11"].update(created=True, running=True)
        controller = self.controller()
        self.ticks(controller, 2)
        self.crash("lon-edge-11")
        self.lost_at(controller, "lon-edge-11")
        self.ticks(controller, 3)
        self.assertEqual(len(self.of("recovered")), 1)
        self.clock.advance(120)
        self.crash("lon-edge-11")
        self.lost_at(controller, "lon-edge-11")
        self.ticks(controller, 100)  # 200 s, still inside ten minutes of the first
        self.assertEqual(self.touching("lon-edge-11"), [("restart", "lon-edge-11")])
        self.assertEqual(len(self.of("recover_wait")), 1)
        self.ticks(controller, 150)  # past ten minutes since the first recovery
        self.assertEqual(self.touching("lon-edge-11"), [("restart", "lon-edge-11")] * 2)
        self.assertEqual(len(self.of("recovered")), 2)
        self.crash("lon-edge-11")
        self.ticks(controller, 400)
        self.assertEqual(self.touching("lon-edge-11"), [("restart", "lon-edge-11")] * 2)
        quarantined = self.of("quarantined")
        self.assertEqual([item["node"] for item in quarantined], ["lon-edge-11"])
        self.assertIn("lon-edge-11", controller.state["quarantined"])
        self.assertEqual(len(self.of("lost")), 3)
        self.assertEqual(len(self.of("replace_join")), 1)
        self.assertEqual([call for call in self.mutating() if call[0] == "start"],
                         [("start", "lon-edge-6")])

    def test_a_rebuild_in_flight_is_awaited_after_a_restart(self):
        """A slot whose instance runs and whose ssh is down for 60 s while
        it is still lost is rebuilt. The controller dies during the
        rebuild (the host has its new key; nobody forgot the old one); the
        one started again finds the phase, forgets the host's key once,
        waits for the instance (never a second rebuild), reaches it over
        ssh, then pushes it: recovered."""
        node = self.sim.nodes["lon-edge-11"]
        node.update(created=True, running=True)
        first = self.controller()
        self.ticks(first, 2)
        self.crash("lon-edge-11")
        node["ssh"] = False
        self.sim.rebuild_gate = threading.Event()
        self.addCleanup(self.sim.rebuild_gate.set)
        self.lost_at(first, "lon-edge-11")
        for _ in range(60):
            self.ticks(first, 1, wait=False)
            time.sleep(0.005)
            if ("rebuild", "lon-edge-11") in self.mutating():
                break
        self.assertIn(("rebuild", "lon-edge-11"), self.mutating())
        step = self.of("recover_step")[0]
        self.assertEqual((step["step"], step["probe"]["instance"], step["probe"]["ssh"]),
                         ("rebuild", "running", False))
        lost = self.of("lost")[0]["unix_us"]
        self.assertGreaterEqual((step["unix_us"] - lost) / 1e6, auto.SSH_DOWN_S)
        self.assertLess((step["unix_us"] - lost) / 1e6, auto.SSH_DOWN_S + 15)
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["actions"]["lon-edge-11"]["phase"], "rebuilding")
        first.close()
        again = self.controller()
        self.ticks(again, 10)  # the instance is still rebuilding
        self.assertEqual(self.touching("lon-edge-11"), [("rebuild", "lon-edge-11")])
        self.assertIn("waiting for the instance", self.of("recover_wait")[0]["why"])
        node.update(instance="running", ssh=True)
        self.ticks(again, 10)
        self.assertEqual(self.touching("lon-edge-11"), [("rebuild", "lon-edge-11"),
                                                        ("push", "lon-edge-11")])
        self.assertEqual(self.sim.forgotten, ["lon-edge-11"])
        self.assertEqual(self.of("recovered")[0]["node"], "lon-edge-11")

    def rebuilt_slot(self, unwarmed: int):
        """A lost slot whose ssh stays down is rebuilt and pushed; that push
        ends unwarmed `unwarmed` times (the deploy's exit 6)."""
        node = self.sim.nodes["lon-edge-11"]
        node.update(created=True, running=True)
        controller = self.controller()
        self.ticks(controller, 2)
        self.crash("lon-edge-11")
        node["ssh"] = False
        self.sim.unwarmed["lon-edge-11"] = unwarmed
        self.lost_at(controller, "lon-edge-11")
        for _ in range(80):
            self.ticks(controller, 1)
            if ("rebuild", "lon-edge-11") in self.mutating():
                break
        self.assertIn(("rebuild", "lon-edge-11"), self.mutating())
        node.update(instance="running", ssh=True)
        return controller

    def test_an_unwarmed_push_after_a_rebuild_runs_the_check_again(self):
        """tools6: the recovery's push after a rebuild exits 6 once: not a
        failed step; its check runs again (another push), then the node is
        recovered."""
        controller = self.rebuilt_slot(unwarmed=1)
        self.ticks(controller, 12)
        self.assertEqual(self.touching("lon-edge-11"), [("rebuild", "lon-edge-11"),
                                                        ("push", "lon-edge-11"),
                                                        ("push", "lon-edge-11")])
        unwarmed = [item for item in self.of("unwarmed") if item["node"] == "lon-edge-11"]
        self.assertEqual([(item["rerun"], item["step"]) for item in unwarmed], [(1, "rebuild")])
        self.assertEqual(self.of("recover_failed"), [])
        self.assertEqual(self.of("recovered")[0]["node"], "lon-edge-11")

    def test_a_rebuilt_node_that_stays_unwarmed_fails_its_recovery_on_warm(self):
        controller = self.rebuilt_slot(unwarmed=10)
        self.ticks(controller, 20)
        pushes = [call for call in self.touching("lon-edge-11") if call[0] == "push"]
        self.assertEqual(len(pushes), 1 + auto.WARM_RERUNS)
        failed = self.of("recover_failed")
        self.assertEqual([item["node"] for item in failed], ["lon-edge-11"])
        self.assertTrue(failed[0]["why"].startswith("warm: no complete warm record after 2"),
                        failed[0]["why"])
        self.assertEqual(self.of("recovered"), [])

    def test_more_than_two_newly_silent_across_the_mesh_is_blind(self):
        """One node silent in each of three sites in one tick: the mesh,
        not three nodes, is in doubt (the operator's own path): blind
        everywhere, nothing judged lost. Two across the mesh, one a site:
        both lost."""
        self.sim = Sim(self.clock, newyork=True)
        self.addCleanup(shutil.rmtree, self.sim.lock, True)
        for index in range(1, 5):
            self.sim.add(f"tok-edge-{index}", "tokyo", "edge", slot=False, running=True)
        for name in ("tok-dist-a", "tok-dist-b"):
            self.sim.add(name, "tokyo", "distributor", slot=False, running=True)
        controller = self.controller(config=auto.Config(replace_lost=False))
        self.ticks(controller, 2)
        for name in ("lon-edge-2", "new-edge-2", "tok-edge-2"):
            self.crash(name)
        self.ticks(controller, 20)
        self.assertEqual(self.of("lost"), [])
        self.assertEqual(self.mutating(), [])
        blind = self.of("observer_blind")
        self.assertEqual(sorted(item["region"] for item in blind),
                         ["london", "newyork", "tokyo"])
        self.assertTrue(all("would be lost in one tick" in item["why"] for item in blind))
        self.sim.nodes["tok-edge-2"]["crashed_unix"] = None
        for _ in range(6):
            self.ticks(controller, 1)
        self.assertEqual(sorted(item["node"] for item in self.of("lost")),
                         ["lon-edge-2", "new-edge-2"])

    def test_a_held_out_edge_back_in_service_gets_a_spare_when_lost_again(self):
        """A base edge lost, replaced, recovered and held out as the
        standby; later a scale-up takes it back into service. Lost again an
        hour later, its region gets a spare again."""
        controller = self.controller(config=auto.Config(max_per_action=1))
        self.ticks(controller, 2)
        self.crash("lon-edge-2")
        self.lost_at(controller, "lon-edge-2")
        self.ticks(controller, 3)
        self.assertEqual(self.of("held_out")[0]["node"], "lon-edge-2")
        self.sim.nodes["lon-edge-2"].update(udp=0, hls=0)
        self.ticks(controller, 5)
        self.assertIn(("stop", "lon-edge-2"), self.mutating())
        self.assertIn("lon-edge-2", controller.state["replaced"])
        self.clock.advance(400)
        self.sim.set_load(950)
        self.ticks(controller, 5)
        self.assertIn(("start", "lon-edge-2"), self.mutating())
        self.assertIn("lon-edge-2", [item["node"] for item in self.of("serving")
                                     if item["how"] == "start"])
        self.assertNotIn("lon-edge-2", controller.state["replaced"])
        self.sim.set_load(500)
        self.clock.advance(3600)
        self.crash("lon-edge-2")
        self.ticks(controller, 6)
        self.assertEqual([item["node"] for item in self.of("lost")], ["lon-edge-2"] * 2)
        self.assertEqual([item["node"] for item in self.of("replace_join")],
                         ["lon-edge-2", "lon-edge-2"])
        self.assertIsNotNone(self.of("replace_join")[1]["spare"])

    def test_a_lost_slot_is_deleted_only_with_its_tag(self):
        """A lost slot that its restart does not bring back is deleted with
        the proof "lost"; a lost base edge is never deleted. The delete
        itself goes by the recorded id through the lab tool, which refuses
        an instance that does not carry the run's tag (LinodeDelete)."""
        self.sim.nodes["lon-edge-11"].update(created=True, running=True, restartable=False)
        self.sim.nodes["lon-edge-3"]["restartable"] = False
        controller = self.controller()
        self.ticks(controller, 2)
        self.crash("lon-edge-11")
        self.lost_at(controller, "lon-edge-11")
        self.ticks(controller, 80)  # past the settle bound
        self.assertEqual(self.touching("lon-edge-11"), [("restart", "lon-edge-11"),
                                                        ("delete", "lon-edge-11")])
        self.assertIn("lon-edge-11", [item["node"] for item in self.of("recover_failed")])
        self.crash("lon-edge-3")
        self.lost_at(controller, "lon-edge-3")
        self.ticks(controller, 80)
        self.assertEqual(self.touching("lon-edge-3"), [("restart", "lon-edge-3")])
        guard = controller.backend
        with self.assertRaises(auto.Refused):
            guard.delete("lon-edge-3", proof="lost")


class LinodeDelete(unittest.TestCase):
    """The linode backend's delete of a lost slot through the lab tool's
    remove_nodes, against a fake API: by the recorded id, and never an
    instance without the run's tag or with another label."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "lab.json"
        self.state = {"run_id": "t1", "tag": "needletail-small-mesh-t1", "created_unix":
                      time.time() - 600, "nodes": {"lon-edge-11": {
                          "id": 4242, "name": "lon-edge-11", "label": "ntsm-t1-lon-edge-11",
                          "role": "edge", "site": "london", "hourly_usd": 0.054,
                          "public_ipv4": "192.0.2.50", "vlan_ipv4": "10.80.3.31",
                          "created_unix": time.time() - 600}},
                      "slots": {"lon-edge-11": {"name": "lon-edge-11", "role": "edge",
                                                "site": "london", "type": "g6-dedicated-2",
                                                "vlan_ipv4": "10.80.3.31"}},
                      "removed": [], "firewall_id": 7, "operator_ipv4": "203.0.113.1"}
        self.path.write_text(json.dumps(self.state))
        self.backend = auto.LinodeBackend(str(self.path), STREAM, auto.Config(), ["london"])

    def api(self, live):
        calls = []

        class Api:
            def call(self, method, path, body=None, allow_404=False):
                calls.append((method, path))
                return live if method == "GET" else {}

        return Api(), calls

    def test_an_instance_without_the_run_tag_is_never_deleted(self):
        for live in ({"id": 4242, "label": "ntsm-t1-lon-edge-11", "tags": ["other"]},
                     {"id": 4242, "label": "someone-else", "tags": ["needletail-small-mesh-t1"]}):
            api, calls = self.api(live)
            self.backend._api = api
            with redirect_stdout(io.StringIO()), self.assertRaises(auto.Refused):
                self.backend.delete("lon-edge-11")
            self.assertNotIn("DELETE", [method for method, _ in calls])
        api, calls = self.api({"id": 4242, "label": "ntsm-t1-lon-edge-11",
                               "tags": ["needletail-small-mesh-t1"]})
        self.backend._api = api
        with redirect_stdout(io.StringIO()):
            self.backend.delete("lon-edge-11")
        self.assertIn(("DELETE", "/linode/instances/4242"), calls)


class LinodeRecover(LinodeDelete):
    """The linode backend's ladder: the probe (the instance's status, then
    ssh), the boot of an offline instance only, the rebuild by the lab
    tool's `replace --mode rebuild` with the lab state and the ops lock's
    token, and the host's old key forgotten."""

    def test_the_probe_reads_the_instance_then_ssh(self):
        import small_mesh_common
        live = {"id": 4242, "label": "ntsm-t1-lon-edge-11", "tags": ["needletail-small-mesh-t1"],
                "status": "running"}
        api, calls = self.api(live)
        self.backend._api = api
        asked = []
        for code, text, expected in ((3, b"inactive\n", {"ssh": True, "unit_active": False}),
                                     (0, b"active\n", {"ssh": True, "unit_active": True}),
                                     (255, b"", {"ssh": False, "unit_active": None})):
            result = types.SimpleNamespace(returncode=code, stdout=text)
            with mock.patch.object(small_mesh_common, "ssh",
                                   lambda node, command, **kw: (asked.append(command), result)[1]):
                found = self.backend.probe("lon-edge-11")
            self.assertEqual(found, {**expected, "instance": "running"})
        self.assertEqual(set(asked), {"systemctl is-active needletail-mesh.service"})
        live["status"] = "offline"
        asked.clear()
        self.assertEqual(self.backend.probe("lon-edge-11"),
                         {"ssh": None, "unit_active": None, "instance": "offline"})
        self.assertEqual(asked, [])

    def test_a_probe_that_times_out_is_ssh_down(self):
        """A host that takes TCP and never finishes the handshake: ssh times
        out, which counts as ssh down (so 60 s of it can bring the
        rebuild), not as a failed probe."""
        import subprocess
        import small_mesh_common
        live = {"id": 4242, "label": "ntsm-t1-lon-edge-11", "tags": ["needletail-small-mesh-t1"],
                "status": "running"}
        api, _calls = self.api(live)
        self.backend._api = api

        def hang(node, command, **kwargs):
            raise subprocess.TimeoutExpired(["ssh"], kwargs.get("timeout", 30))

        with mock.patch.object(small_mesh_common, "ssh", hang):
            self.assertEqual(self.backend.probe("lon-edge-11"),
                             {"ssh": False, "unit_active": None, "instance": "running"})

    def test_only_an_offline_instance_of_the_run_is_booted(self):
        live = {"id": 4242, "label": "ntsm-t1-lon-edge-11", "tags": ["needletail-small-mesh-t1"],
                "status": "running"}
        api, calls = self.api(live)
        self.backend._api = api
        with self.assertRaises(auto.Refused):
            self.backend.boot("lon-edge-11")
        live["tags"] = []
        live["status"] = "offline"
        with self.assertRaises(auto.Refused):
            self.backend.boot("lon-edge-11")
        self.assertNotIn("POST", [method for method, _ in calls])
        live["tags"] = ["needletail-small-mesh-t1"]
        self.backend.boot("lon-edge-11")
        self.assertIn(("POST", "/linode/instances/4242/boot"), calls)

    def test_the_rebuild_is_the_lab_tools_replace_in_place(self):
        """The replace runs in its own process group with the lab state and
        the lock's token; then the host's key is forgotten (the deploy
        tool's common.forget_host). Its command line is the lab tool's own
        (C CT3): parsed by the lab tool's parser, it is `replace` of the
        node in place, on this controller's lab state."""
        import small_mesh_common
        runs, popens = [], []

        class Popen:
            def __init__(self, command, **kwargs):
                popens.append((command, kwargs))
                self.pid, self.returncode = 4321, 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def communicate(self, timeout=None):
                return "", ""

        def run(command, **kwargs):
            runs.append(command)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        self.backend.lock_env = {"NEEDLETAIL_OPS_LOCK_TOKEN": "t0ken"}
        with mock.patch.object(auto.subprocess, "Popen", Popen), \
                mock.patch.object(small_mesh_common, "forget_host",
                                  lambda *addresses: runs.append(addresses)):
            self.backend.rebuild("lon-edge-11")
        command, kwargs = popens[0]
        self.assertEqual(pathlib.Path(command[1]).name, "linode-small-mesh-lab.py")
        parsed = self.backend.lab.build_parser().parse_args(command[2:])
        self.assertIs(parsed.handler, self.backend.lab.command_replace)
        self.assertEqual((parsed.state, parsed.names, parsed.mode),
                         (str(self.path), ["lon-edge-11"], "rebuild"))
        self.assertEqual(kwargs["env"]["NEEDLETAIL_LINODE_LAB_STATE"], str(self.path))
        self.assertEqual(kwargs["env"]["NEEDLETAIL_OPS_LOCK_TOKEN"], "t0ken")
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(runs, [("192.0.2.50",)])

        class Failing(Popen):
            def __init__(self, command, **kwargs):
                super().__init__(command, **kwargs)
                self.returncode = 1

            def communicate(self, timeout=None):
                return "", "watchdog not active"

        runs.clear()
        with mock.patch.object(auto.subprocess, "Popen", Failing), \
                mock.patch.object(small_mesh_common, "forget_host",
                                  lambda *addresses: runs.append(addresses)), \
                self.assertRaises(RuntimeError):
            self.backend.rebuild("lon-edge-11")
        self.assertEqual(runs, [])  # nothing forgotten when the rebuild failed


class Scale(Base):
    def run_scale(self, controller, direction, tier="edges"):
        out = io.StringIO()
        marks = auto.Marks(out=out, err=io.StringIO())

        def sleep(seconds):
            controller.worker.wait_idle(10)
            self.clock.advance(seconds)

        code = auto.command_scale(controller, direction, "london", tier, marks, sleep=sleep)
        return code, out.getvalue().splitlines()

    def test_scale_up_ends_when_the_rebalance_is_still(self):
        """The standby joins; listeners are rebalanced onto it (the
        region's rebalance_moves rises for a while); `end` comes 5 s after
        the last rise. stdout is the two marks and nothing else."""
        controller = self.controller()
        rises = {"left": 0}

        def rebalance():
            if any(item["node"] == "lon-edge-6" for item in self.of("serving")) \
                    and rises["left"] > 0:
                self.sim.nodes["lon-edge-1"]["moves"] += 7
                rises["left"] -= 1

        rises["left"] = 4
        self.sim.on_observe.append(rebalance)
        code, lines = self.run_scale(controller, "up")
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in lines], ["start", "end"])
        self.assertTrue(all(line.split()[2] == "autoscale.up.london" for line in lines))
        self.assertEqual(self.mutating(), [("start", "lon-edge-6")])
        serving = self.of("serving")[0]["unix_us"] / 1e6
        end = self.of("change_end")[0]
        self.assertEqual(end["result"], "done")
        self.assertEqual(end["rebalance_moves"], 28)
        # Four rises, one a tick (2 s) from the serving tick: still from
        # the last at serving + 6 s, so the end at serving + 6 + 5 s at
        # the earliest.
        self.assertGreaterEqual(end["unix_us"] / 1e6 - serving, 11)
        self.assertLess(end["unix_us"] / 1e6 - serving, 15)

    def test_scale_up_ends_at_the_bound_when_the_rebalance_never_stops(self):
        controller = self.controller()
        self.sim.on_observe.append(
            lambda: self.sim.nodes["lon-edge-1"].update(moves=self.sim.nodes["lon-edge-1"]
                                                        ["moves"] + 1))
        code, lines = self.run_scale(controller, "up")
        self.assertEqual(code, 0)
        serving = self.of("serving")[0]["unix_us"] / 1e6
        end = self.of("change_end")[0]["unix_us"] / 1e6
        self.assertGreaterEqual(end - serving, 60)
        self.assertLess(end - serving, 63)

    def test_scale_down_ends_at_stopped(self):
        """The victim the controller would pick (fewest listeners) is
        drained; `end` when it is stopped, after its gauges were at 0."""
        for index in range(6, 8):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.nodes["lon-edge-4"]["udp"] = 3
        self.sim.on_observe.append(
            lambda: self.sim.nodes["lon-edge-4"].update(udp=0) if self.sim.nodes["lon-edge-4"][
                "draining"] else None)
        controller = self.controller()
        code, lines = self.run_scale(controller, "down")
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in lines], ["start", "end"])
        self.assertTrue(all(line.split()[2] == "autoscale.down.london" for line in lines))
        self.assertEqual(self.mutating(), [("drain", "lon-edge-4"), ("stop", "lon-edge-4")])
        kinds = self.kinds()
        self.assertLess(kinds.index("drain_end"), kinds.index("stopped"))
        self.assertLess(kinds.index("stopped"), kinds.index("change_end"))

    def test_the_forwarder_tier_adds_and_removes_the_slot(self):
        """`scale up --tier forwarders`: the site's forwarder slot is
        created and pushed and serves (ready, hellos not draining);
        `scale down --tier forwarders`: drained, gated, stopped."""
        controller = self.controller()
        code, lines = self.run_scale(controller, "up", tier="forwarders")
        self.assertEqual(code, 0)
        self.assertEqual([line.split(maxsplit=2)[::2] for line in lines],
                         [["start", "autoscale.up.london"], ["end", "autoscale.up.london"]])
        self.assertEqual(self.mutating(), [("create", "lon-dist-c"), ("push", "lon-dist-c")])
        self.sim.nodes["lon-dist-c"].update(lease=0, cache=0)
        again = self.controller()
        code, lines = self.run_scale(again, "down", tier="forwarders")
        self.assertEqual(code, 0)
        self.assertEqual([line.split(maxsplit=2)[::2] for line in lines],
                         [["start", "autoscale.down.london"], ["end", "autoscale.down.london"]])
        self.assertEqual(self.touching("lon-dist-c")[2:], [("drain", "lon-dist-c"),
                                                           ("stop", "lon-dist-c")])
        self.assertTrue(self.of("gate")[-1]["ok"])

    def test_scale_down_is_refused_without_room_and_prints_no_mark(self):
        for index in range(6, 8):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.set_load(780)
        self.sim.nodes["lon-edge-4"]["udp"] = 3
        controller = self.controller()
        with self.assertRaises(auto.Refused) as caught:
            self.run_scale(controller, "down")
        self.assertIn("no room to drain lon-edge-4: it carries 780 permille", str(caught.exception))
        self.assertEqual(self.mutating(), [])

    def test_scale_down_never_below_three_edges_and_prints_no_mark(self):
        for index in (4, 5):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = False
        controller = self.controller()
        with self.assertRaises(auto.Refused):
            self.run_scale(controller, "down")
        self.assertEqual(self.mutating(), [])

    def test_a_scale_verb_is_refused_while_a_controller_holds_the_lock(self):
        fake = types.SimpleNamespace(WORK=self.tmp, NODES=[], slot_nodes=lambda: [])
        holder = auto.OpsLock(self.tmp / "ops.lock").acquire()
        self.addCleanup(holder.release)
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):  # main() installs its own
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"edge_lab": fake}), redirect_stderr(err), \
                redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as exit_:
            auto.main(["scale", "up", "--backend", "edge_lab", "--site", "london",
                       "--controller-state", str(self.tmp / "c.json")])
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("another change command or controller holds", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_run_until_returns_after_its_first_action(self):
        """The controller alone: at its first join decision it prints the
        start mark, and it returns once that join serves and the rebalance
        is still, without ticking on. --until stopped the same for its
        first drain."""
        controller = self.controller()
        self.sim.set_load(760)
        out = io.StringIO()
        ticks = []
        original = controller.tick

        def counted(**kwargs):
            ticks.append(self.clock())
            return original(**kwargs)

        controller.tick = counted

        def sleep(seconds):
            controller.worker.wait_idle(10)
            self.clock.advance(seconds)

        code = controller.run(duration=600, sleep=sleep, until="join",
                              marks=auto.Marks(out=out, err=io.StringIO()))
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["start", "end"])
        self.assertEqual(lines[0].split()[2], "autoscale.up.london")
        end = self.of("change_end")[0]["unix_us"] / 1e6
        self.assertLessEqual(ticks[-1], end)
        self.assertLess(len(ticks), 20)
        self.assertEqual(self.kinds()[-1], "controller_stop")
        # --until stopped: a low load, one drain (a slot, the first
        # victim), returns at its stop once the slot is deleted (no warm
        # pool: A2p's slot goes inside its scenario).
        self.sim.nodes["lon-edge-11"].update(created=True, running=True)
        self.sim.set_load(100)
        for index in range(1, 12):
            self.sim.nodes[f"lon-edge-{index}"]["udp"] = 0
        config = auto.Config(down_hold_s=10, no_flap_s=0, cooldown_s=0, warm_pool=False)
        down = self.controller(config=config)
        out = io.StringIO()
        code = down.run(duration=600, sleep=lambda seconds: (down.worker.wait_idle(10),
                                                             self.clock.advance(seconds)),
                        until="stopped", marks=auto.Marks(out=out, err=io.StringIO()))
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual([line.split(maxsplit=2)[::2] for line in lines],
                         [["start", "autoscale.down.london"], ["end", "autoscale.down.london"]])
        self.assertEqual([call for call in self.mutating() if call[0] in ("drain", "stop",
                                                                          "delete")],
                         [("drain", "lon-edge-11"), ("stop", "lon-edge-11"),
                          ("delete", "lon-edge-11")])
        self.assertEqual(down.state["actions"], {})


class Stopping(Base):
    def test_stopped_from_outside_it_resumes_the_drain_in_flight(self):
        self.sim.nodes["lon-edge-11"].update(created=True, running=True)
        controller = self.controller()
        controller.state["actions"]["lon-edge-11"] = {
            "kind": "drain", "site": "london", "phase": "drain", "drain_sent": False,
            "started_unix": self.clock(), "deadline_unix": self.clock() + 60, "reason": "test"}
        ticks = []

        def tick():
            ticks.append(1)
            if len(ticks) == 2:
                raise KeyboardInterrupt  # SIGINT (or SIGTERM, which main makes one)
            return auto.Controller.tick(controller)

        controller.tick = tick
        with self.assertRaises(KeyboardInterrupt):
            controller.run(duration=600, sleep=lambda seconds: self.clock.advance(seconds))
        self.assertEqual(self.mutating(), [("drain", "lon-edge-11"), ("resume", "lon-edge-11")])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["actions"], {})
        kinds = self.kinds()
        self.assertIn("wound_down", kinds)
        self.assertEqual(kinds[-1], "controller_stop")


class Failing(Base):
    def drain_in_flight(self, controller) -> None:
        self.sim.nodes["lon-edge-11"].update(created=True, running=True)
        controller.state["actions"]["lon-edge-11"] = {
            "kind": "drain", "site": "london", "phase": "drain", "drain_sent": False,
            "started_unix": self.clock(), "deadline_unix": self.clock() + 60, "reason": "test"}

    def test_an_interrupt_drops_a_create_not_yet_handed_over(self):
        """Interrupted after a tick queued a create and before it handed it
        over: nothing is created by a controller that is stopping (it
        could die mid-call and leave an instance outside the lab state).
        The join stays `creating` in the state, and a controller started
        again asks for it once."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.set_load(950)
        controller = self.controller(config=auto.Config(max_per_action=1))
        flush = controller.flush

        def interrupted():
            if any(job["verb"] == "create" for job in controller.pending):
                raise KeyboardInterrupt
            flush()

        controller.flush = interrupted
        with self.assertRaises(KeyboardInterrupt):
            controller.run(duration=600, sleep=lambda seconds: (controller.worker.wait_idle(5),
                                                                self.clock.advance(seconds)))
        time.sleep(0.05)
        self.assertEqual(self.mutating(), [])
        self.assertEqual(self.of("wound_down")[0]["dropped"], ["create lon-edge-11"])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["actions"]["lon-edge-11"]["phase"], "creating")
        again = self.controller(config=auto.Config(max_per_action=1))
        again.begin()
        self.ticks(again, 3)
        self.assertEqual([call for call in self.mutating() if call[0] == "create"],
                         [("create", "lon-edge-11")])
        self.assertIn("requeued", self.kinds())

    def test_a_failure_mid_run_leaves_no_node_drained(self):
        """The lab state cannot be read mid-run (or the account's API
        fails): the controller ends with exit 3, names the failure, and
        resumes the drain it had sent."""
        controller = self.controller()
        self.drain_in_flight(controller)
        roster, reads = self.sim.roster, []

        def failing():
            reads.append(1)
            if len(reads) == 3:
                raise RuntimeError("GET /linode/instances: 503 after retries")
            return roster()

        self.sim.roster = failing
        code = controller.run(duration=600, sleep=lambda seconds: self.clock.advance(seconds))
        self.assertEqual(code, auto.EXIT_FAILED)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-11"), ("resume", "lon-edge-11")])
        self.assertIn("RuntimeError", [item for item in self.of("failure")
                                       if item["what"] == "tick"][0]["detail"])
        self.assertEqual(json.loads(self.state_path.read_text())["actions"], {})
        self.assertEqual(self.kinds()[-1], "controller_stop")

    def test_a_scale_that_fails_after_its_start_is_exit_3_and_closed(self):
        """`scale down` whose observation fails mid-drain, or whose lab
        state is refused mid-drain: exit 3 (not 2: something changed), the
        end mark printed, the drain resumed."""
        for error in (RuntimeError("connection reset"), auto.Refused("no lab state")):
            self.sim.calls.clear()
            for index in range(6, 8):
                self.sim.nodes[f"lon-edge-{index}"]["running"] = True
            self.sim.nodes["lon-edge-4"].update(udp=3, draining=False)
            observe, seen = self.sim.observe, []

            def failing(roster, observe=observe, seen=seen, error=error):
                seen.append(1)
                if len(seen) == 3:
                    raise error
                return observe(roster)

            self.sim.observe = failing
            controller = self.controller()
            out = io.StringIO()

            def sleep(seconds, controller=controller):
                controller.worker.wait_idle(10)
                self.clock.advance(seconds)

            code = auto.command_scale(controller, "down", "london", "edges",
                                      auto.Marks(out=out, err=io.StringIO()), sleep=sleep)
            self.sim.observe = observe
            self.assertEqual(code, auto.EXIT_FAILED, error)
            self.assertEqual([line.split()[0] for line in out.getvalue().splitlines()],
                             ["start", "end"])
            self.assertEqual(self.mutating(), [("drain", "lon-edge-4"), ("resume", "lon-edge-4")])


class Main(Base):
    """The command as an operator or scale-run runs it (main), over the
    simulated backend: stdout, the clock check, the ops lock."""

    def main(self, argv: list[str]) -> tuple[int, str, str]:
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):  # main() installs its own
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        out, err = io.StringIO(), io.StringIO()
        # The run goes into a temporary archive, never ~/needletail-data.
        record = lambda args, backend, config, sites: auto.archived_run.Run(  # noqa: E731
            "as-main", "local-lab", "linode-autoscale", argv=["linode-autoscale.py"],
            prefix="autoscale/", root=self.tmp / "data")
        with mock.patch.object(auto, "make_backend", lambda args, config, sites: self.sim), \
                mock.patch.object(auto, "archive_record", record), \
                mock.patch.object(auto, "REBALANCE_STILL_S", 0.2), \
                redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as exit_:
                auto.main(argv + ["--backend", "edge_lab", "--controller-state",
                                  str(self.state_path), "--events", str(self.events_path),
                                  "--poll-s", "0.05"])
            self.assertIs(sys.stdout, out)  # given back
        return exit_.exception.code, out.getvalue(), err.getvalue()

    def test_stdout_is_the_two_marks_even_when_the_lab_tool_prints(self):
        """A scale-up that creates: the lab tool prints its own lines from
        the worker thread while the main thread writes the marks. stdout
        is exactly the two marks; the tool's lines are on stderr."""
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.chatty = True
        code, out, err = self.main(["scale", "up", "--site", "london"])
        self.assertEqual(code, 0, err[-2000:])
        lines = out.splitlines()
        self.assertEqual([line.split(maxsplit=2)[::2] for line in lines],
                         [["start", "autoscale.up.london"], ["end", "autoscale.up.london"]])
        self.assertTrue(all(len(line.split()) == 3 for line in lines))
        self.assertIn("Adding 1 slot(s) lon-edge-11", err)
        self.assertIn("  created lon-edge-11", err)

    def test_the_clock_check_refuses_over_50_ms(self):
        """Before the first mark: a node whose clock is 60 ms from this
        host's (either way) refuses the change, exit 2, nothing on stdout,
        nothing done; at 40 ms it goes ahead."""
        for offset in (0.06, -0.06):
            self.sim.offset = offset
            code, out, err = self.main(["scale", "up", "--site", "london"])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("ms from this host's", err)
        self.assertEqual(self.mutating(), [])
        controller = self.controller()
        marks = io.StringIO()
        with self.assertRaises(auto.Refused):
            controller.run(duration=60, until="join", sleep=self.clock.advance,
                           marks=auto.Marks(out=marks, err=io.StringIO()))
        self.assertEqual(marks.getvalue(), "")
        self.sim.offset = 0.04
        code, out, err = self.main(["scale", "up", "--site", "london"])
        self.assertEqual(code, 0, err[-2000:])
        self.assertEqual(len(out.splitlines()), 2)
        self.assertEqual(self.of("clock_check")[-1]["offsets"]["london"]["offset_ms"], 40.0)

    def test_the_lock_is_held_until_the_job_in_hand_is_done(self):
        """Interrupted while its worker runs a job (the standby's start): the
        command waits for that job before it lets the ops lock go, so no
        other change command can act while it still runs; the end mark is
        printed, exit 130."""
        import os
        import signal
        self.sim.start_gate = threading.Event()
        self.addCleanup(self.sim.start_gate.set)
        seen = {}

        def interrupt():
            self.await_call(("start", "lon-edge-6"))
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.3)
            try:
                auto.OpsLock(self.sim.lock_path()).acquire().release()
                seen["held"] = False
            except auto.Refused:
                seen["held"] = True
            seen["done"] = self.sim.nodes["lon-edge-6"]["running"]
            self.sim.start_gate.set()

        thread = threading.Thread(target=interrupt)
        thread.start()
        code, out, err = self.main(["scale", "up", "--site", "london"])
        thread.join(10)
        self.assertEqual(code, 130, err[-2000:])
        self.assertEqual(seen, {"held": True, "done": False})
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["start", "end"])
        self.assertTrue(self.sim.nodes["lon-edge-6"]["running"])  # the start ran whole
        self.assertNotIn("worker_abandoned", self.kinds())
        auto.OpsLock(self.sim.lock_path()).acquire().release()

    def await_call(self, call) -> None:
        deadline = time.monotonic() + 5
        while call not in self.mutating() and time.monotonic() < deadline:
            time.sleep(0.01)


class Restart(Base):
    def test_a_drain_past_its_deadline_on_restart_is_resumed_not_deleted(self):
        self.sim.nodes["lon-edge-11"].update(created=True, running=True)
        controller = self.controller()
        controller.state["actions"]["lon-edge-11"] = {
            "kind": "drain", "site": "london", "phase": "drain", "drain_sent": True,
            "started_unix": self.clock() - 90, "deadline_unix": self.clock() - 30,
            "reason": "test"}
        controller.save()
        self.sim.calls.clear()
        again = self.controller()
        self.ticks(again, 1)
        self.assertEqual(self.mutating(), [("resume", "lon-edge-11")])
        self.assertEqual(json.loads(self.state_path.read_text())["actions"], {})

    def test_a_state_of_another_lab_is_refused(self):
        self.state_path.write_text(json.dumps({"schema": auto.SCHEMA, "backend": "sim",
                                               "run_id": "old", "regions": {}, "actions": {},
                                               "warm": {}, "failures": []}))
        self.sim.identity = lambda: {"run_id": "new"}
        with self.assertRaises(auto.Refused):
            self.controller()


class Safety(Base):
    def guarded(self, dry_run=False):
        guard = auto.Guarded(self.sim, self.config, dry_run)
        guard.roster()
        return guard

    def test_only_slots_are_created_or_deleted(self):
        guard = self.guarded()
        for name in ("lon-edge-1", "lon-edge-6", "lon-dist-a", "arch-us-east", "nope"):
            with self.assertRaises(auto.Refused, msg=name):
                guard.create([name])
            for proof in ("drained-to-zero", "lost"):
                with self.assertRaises(auto.Refused, msg=name):
                    guard.delete(name, proof=proof)
        # A slot needs a proof that it holds no listener.
        self.sim.nodes["lon-edge-11"]["created"] = True
        guard.roster()
        for proof in (None, "because"):
            with self.assertRaises(auto.Refused):
                guard.delete("lon-edge-11", proof=proof)
        guard.delete("lon-edge-11", proof="drained-to-zero")
        # The first distributors and the dev box are never touched at all.
        for verb in ("start", "stop", "drain", "resume"):
            for name in ("lon-dist-a", "lon-dist-b", "arch-us-east"):
                with self.assertRaises(auto.Refused):
                    getattr(guard, verb)(name)
        for name in ("lon-dist-a", "lon-dist-b", "arch-us-east"):
            with self.assertRaises(auto.Refused):
                guard.recover(name, "restart", proof="lost")
        self.assertEqual(self.mutating(), [("delete", "lon-edge-11")])

    def test_a_recovery_step_needs_the_proof_lost(self):
        guard = self.guarded()
        for proof in (None, "drained-to-zero"):
            with self.assertRaises(auto.Refused):
                guard.recover("lon-edge-2", "restart", proof=proof)
        with self.assertRaises(auto.Refused):
            guard.recover("lon-edge-2", "reformat", proof="lost")
        guard.recover("lon-edge-2", "restart", proof="lost")
        self.assertEqual(self.mutating(), [("restart", "lon-edge-2")])

    def test_a_dry_run_calls_nothing_and_writes_no_state(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        controller = self.controller(dry_run=True)
        self.sim.set_load(950)
        self.ticks(controller, 4)
        self.assertEqual(self.sim.calls, [])
        self.assertEqual(controller.backend.calls, [("create", "lon-edge-11,lon-edge-12"),
                                                    ("push", "lon-edge-11,lon-edge-12")])
        self.assertFalse(self.state_path.exists())
        decisions = self.of("decision")
        self.assertTrue(all(item["dry_run"] for item in decisions))
        joined = [item for item in decisions if item["action"] == "join"]
        self.assertEqual(joined[0]["targets"], [["lon-edge-11", "create"],
                                                ["lon-edge-12", "create"]])

    def test_events_carry_unix_microseconds_and_the_inputs(self):
        controller = self.controller()
        self.sim.set_load(800)
        self.ticks(controller, 3)
        for item in self.events():
            self.assertIsInstance(item["unix_us"], int)
            self.assertAlmostEqual(item["unix_us"] / 1e6, self.clock.now, delta=10)
            self.assertEqual(item["source"], "autoscaler")
        join = [item for item in self.of("decision") if item["action"] == "join"][0]
        self.assertEqual(join["inputs"]["mean_permille"], 800)
        self.assertEqual(join["inputs"]["sum_permille"], 4000)
        self.assertIn("load_permille", join["inputs"])


class ControlBody(unittest.TestCase):
    def test_every_drain_and_resume_names_its_node(self):
        sent = []
        backend = auto.LinodeBackend.__new__(auto.LinodeBackend)
        backend.public = lambda name: "192.0.2.7"
        with mock.patch.object(auto, "http_post", lambda address, path, body, **k:
                               sent.append((address, path, body))):
            backend.drain("lon-edge-3")
            backend.resume("lon-edge-3")
        self.assertEqual(sent, [("192.0.2.7", "/api/control/drain-node", {"node_id": "lon-edge-3"}),
                                ("192.0.2.7", "/api/control/resume-node",
                                 {"node_id": "lon-edge-3"})])
        with self.assertRaises(auto.Refused):
            auto.control_body("")


class LinodeBackendTests(unittest.TestCase):
    """The linode backend's roster and budget from a lab state on disk,
    and its delete through the lab tool: no Linode call is made (the lab
    tool's Api is replaced)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        lab = auto.load_module("lab_for_tests", "linode-small-mesh-lab.py")
        topology = json.loads((HERE.parent / "deploy" / "low-latency"
                               / "topology-scale.json").read_text())
        now = time.time()
        nodes = {}
        for index, node in enumerate(lab.created_part(topology)["nodes"]):
            nodes[node["name"]] = {"id": 1000 + index, "name": node["name"],
                                   "label": f"ntsm-t1-{node['name']}", "role": node["role"],
                                   "site": node["site"], "region": node["region"],
                                   "type": node["type"], "hourly_usd": 0.054,
                                   "public_ipv4": f"192.0.2.{index % 250}",
                                   "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0],
                                   "created_unix": now - 3600}
        self.state = {"run_id": "t1", "tag": "needletail-small-mesh-t1", "created_unix":
                      now - 3600, "watchdog_deadline_unix": now + 2 * 3600, "nodes": nodes,
                      "slots": lab.slot_specs(topology), "removed": [], "hourly_usd": 10.422,
                      "firewall_id": 1, "operator_ipv4": "203.0.113.1"}
        self.path = self.tmp / "lab.json"
        self.path.write_text(json.dumps(self.state))
        artifacts = self.tmp / "artifacts"
        artifacts.mkdir()
        (artifacts / "settings.json").write_text(json.dumps(
            {"standby_edges": ["lon-edge-6", "lon-edge-7"]}))
        sys_path = str(HERE)
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        import small_mesh_common
        patcher = mock.patch.object(small_mesh_common, "ARTIFACT_DIR", artifacts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = auto.Config(budget_usd=100.0, spent_before_usd=20.0)
        self.backend = auto.LinodeBackend(str(self.path), 4001, self.config, ["london"])
        # The lab's watchdog unit is running (no user systemd is asked).
        patcher = mock.patch.object(self.backend.lab, "watchdog_active", lambda run_id: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roster_slots_standby_and_first_distributors(self):
        roster = self.backend.roster()
        self.assertEqual(list(roster), ["london"])
        by_name = {node["name"]: node for node in roster["london"]}
        self.assertTrue(by_name["lon-dist-a"]["first"] and by_name["lon-dist-b"]["first"])
        self.assertFalse(by_name["lon-dist-c"]["first"])
        self.assertTrue(by_name["lon-dist-c"]["slot"])
        self.assertFalse(by_name["lon-dist-c"]["created"])
        self.assertTrue(by_name["lon-edge-11"]["slot"])
        self.assertTrue(by_name["lon-edge-6"]["held"])
        self.assertFalse(by_name["lon-edge-1"]["held"])
        self.assertNotIn("contrib-lon", by_name)
        self.assertNotIn("lon-load-1", by_name)
        # Addresses: the VLAN's for every node (a slot's before it exists),
        # the public one once created.
        self.assertEqual(by_name["lon-edge-1"]["vlan"], self.state["nodes"]["lon-edge-1"][
            "vlan_ipv4"])
        self.assertEqual(by_name["lon-dist-c"]["vlan"],
                         self.state["slots"]["lon-dist-c"]["vlan_ipv4"])
        self.assertIsNone(by_name["lon-dist-c"]["public"])
        self.assertEqual(self.backend.lock_path(), self.tmp / "lab.json.ops.lock")

    def test_budget_projects_to_the_watchdog_and_refuses_past_six_hours(self):
        ok, figures = self.backend.budget(["lon-edge-11"], offline=True)
        # 1 h old, 2 h of watchdog left: 3 h of every node, and the new
        # slot 2 h.
        self.assertAlmostEqual(figures["lab_projected_usd"], 99 * 0.054 * 3 + 0.054 * 2,
                               places=3)
        self.assertTrue(ok)
        # No tolerance past 6.0 h of the lab's life (a seventh billed hour).
        for life in (6.2, 6.0 + 30 / 3600):
            self.state["watchdog_deadline_unix"] = self.state["created_unix"] + life * 3600
            self.path.write_text(json.dumps(self.state))
            ok, figures = self.backend.budget(["lon-edge-11"], offline=True)
            self.assertFalse(ok)
            self.assertIn("at most 6 h", figures["why"])
        # Nothing created within 15 min of the deadline.
        self.state["watchdog_deadline_unix"] = time.time() + 600
        self.path.write_text(json.dumps(self.state))
        ok, figures = self.backend.budget(["lon-edge-11"], offline=True)
        self.assertFalse(ok)
        self.assertIn("within 15 min", figures["why"])
        del self.state["watchdog_deadline_unix"]
        self.path.write_text(json.dumps(self.state))
        self.assertFalse(self.backend.budget(["lon-edge-11"], offline=True)[0])
        self.backend.config.budget_usd = None
        self.assertFalse(self.backend.budget(["lon-edge-11"], offline=True)[0])

    def test_a_create_needs_what_earlier_labs_spent(self):
        """Unknown is not 0 (C CT3's ledger): without --spent-before-usd the
        budget guard takes what earlier labs spent from the lab tool (up's
        recorded figure, or the ledger's sum when that is more), and
        without --budget-usd the budget up recorded; with no budget
        anywhere every create is refused and `run` is refused before it
        starts."""
        ledger = self.tmp / "ledger.jsonl"
        ledger.write_text(json.dumps({"run_id": "before", "billed_usd": 90.0}) + "\n"
                          + json.dumps({"run_id": "a1", "billed_usd": 12.5}) + "\n")
        patcher = mock.patch.object(self.backend.lab, "LEDGER", ledger)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend.config.spent_before_usd = None
        ok, figures = self.backend.budget(["lon-edge-11"], offline=True)
        self.assertEqual(figures["spent_before_usd"], 102.5)
        self.assertFalse(ok)  # 102.5 spent before is past 100 already
        self.backend.config.spent_before_usd = 20.0
        ok, figures = self.backend.budget(["lon-edge-11"], offline=True)
        self.assertEqual(figures["spent_before_usd"], 20.0)
        self.assertTrue(ok, figures)
        # The budget that up recorded is the default of --budget-usd.
        self.backend.config.budget_usd = None
        ok, figures = self.backend.budget(["lon-edge-11"], offline=True)
        self.assertFalse(ok)
        self.assertIn("--budget-usd", figures["why"])
        args = argparse.Namespace(backend="linode", command="run", dry_run=False,
                                  state=str(self.path), stream=4001, ssh_public_key="k")
        with self.assertRaises(auto.Refused):
            auto.make_backend(args, auto.Config(), [])
        guard = auto.Guarded(self.backend, self.backend.config)
        guard.roster()
        with self.assertRaises(auto.Refused):
            guard.create(["lon-edge-11"])
        self.state["budget_usd"] = 100.0
        self.path.write_text(json.dumps(self.state))
        ok, figures = self.backend.budget(["lon-edge-11"], offline=True)
        self.assertTrue(ok, figures)
        self.assertEqual(figures["budget_usd"], 100.0)
        self.assertIsInstance(auto.make_backend(args, auto.Config(), []), auto.LinodeBackend)

    def test_every_roster_starts_a_dead_watchdog_again(self):
        """C CT6 finding 8, at the CT3 merge: each tick's roster calls the
        lab tool's watchdog_ensure, which arms a unit that is not active at
        the recorded deadline; a watchdog that cannot be armed fails the
        tick."""
        armed = []
        with mock.patch.object(self.backend.lab, "watchdog_active", lambda run_id: False), \
                mock.patch.object(self.backend.lab, "watchdog_arm",
                                  lambda path, state, deadline: armed.append(
                                      (pathlib.Path(path), state["run_id"], deadline))), \
                redirect_stderr(io.StringIO()) as err:
            self.backend.roster()
        self.assertEqual(armed, [(self.path, "t1", int(self.state["watchdog_deadline_unix"]))])
        self.assertIn("starting it again", err.getvalue())

        def cannot(path, state, deadline):
            raise SystemExit("linger is off for this user")

        with mock.patch.object(self.backend.lab, "watchdog_active", lambda run_id: False), \
                mock.patch.object(self.backend.lab, "watchdog_arm", cannot), \
                redirect_stderr(io.StringIO()), self.assertRaises(auto.Refused) as caught:
            self.backend.roster()
        self.assertIn("linger is off", str(caught.exception))

    def test_nothing_is_pushed_to_a_slot_without_the_lab_firewall(self):
        deployed = []
        self.backend.deploy = lambda verb, names, *extra: deployed.append((verb, names, *extra))
        with mock.patch.object(self.backend, "api", lambda: object()), \
                mock.patch.object(self.backend.lab, "firewall_attached",
                                  lambda api, state, name: name != "lon-edge-2"):
            with self.assertRaises(auto.Refused):
                self.backend.push(["lon-edge-1", "lon-edge-2"])
        self.assertEqual(deployed, [])
        with mock.patch.object(self.backend, "api", lambda: object()), \
                mock.patch.object(self.backend.lab, "firewall_attached",
                                  lambda api, state, name: True):
            self.backend.push(["lon-edge-1", "lon-edge-2"])
        # One push of the batch; the controller, not the deploy, decides
        # what a slow host costs its slots.
        self.assertEqual(deployed, [("push", "lon-edge-1,lon-edge-2", "--slow-host", "report")])

    def test_the_deploy_s_exit_6_is_unwarmed_never_allowed(self):
        """A start or push that exits 6 (EXIT_UNWARMED) is Unwarmed, not a
        failed step; any other failure stays a RuntimeError, and the
        controller never asks the deploy for --allow-unwarmed."""
        commands = []

        def child(code):
            def run_child(command, timeout):
                commands.append(command)
                return subprocess.CompletedProcess(command, code, "", "UNWARMED: lon-edge-6")
            return run_child

        with mock.patch.object(self.backend, "run_child", child(auto.DEPLOY_EXIT_UNWARMED)):
            with self.assertRaises(auto.Unwarmed):
                self.backend.start("lon-edge-6")
            with self.assertRaises(auto.Unwarmed):
                self.backend.deploy("push", "lon-edge-6", "--slow-host", "report")
            # stop's exit 6 is no warm verdict.
            with self.assertRaises(RuntimeError) as caught:
                self.backend.stop("lon-edge-6")
            self.assertNotIsInstance(caught.exception, auto.Unwarmed)
        with mock.patch.object(self.backend, "run_child", child(1)):
            with self.assertRaises(RuntimeError) as caught:
                self.backend.start("lon-edge-6")
            self.assertNotIsInstance(caught.exception, auto.Unwarmed)
        self.assertTrue(all("--allow-unwarmed" not in command for command in commands))

    def test_a_state_without_a_run_tag_is_refused(self):
        self.state["tag"] = ""
        self.path.write_text(json.dumps(self.state))
        with self.assertRaises(auto.Refused):
            self.backend.roster()

    def test_delete_goes_through_the_lab_tool_slots_only(self):
        called = []

        def remove(api, path, state, names, slots_only=True):
            called.append((names, slots_only))

        with mock.patch.object(self.backend.lab, "remove_nodes", remove), \
                mock.patch.object(self.backend, "api", lambda: object()):
            self.backend.delete("lon-edge-11")
        self.assertEqual(called, [(["lon-edge-11"], True)])

    def test_observation_reads_the_entry_and_each_node_status(self):
        """One /api/assignment per region (the first node in service that
        answers it) and one /api/node per node, over the kept-open
        connections."""
        addresses = {node["public_ipv4"]: name for name, node in self.state["nodes"].items()}
        asked = []

        class FakeHttp:
            def get(self, host, port, path):
                asked.append((addresses[host], path))
                if addresses[host] == "lon-dist-a":
                    return None, None  # the first entry does not answer
                # lon-dist-b's answer is the one taken: the first in roster
                # order that answered.
                load = 640 if addresses[host] == "lon-dist-b" else 999
                return 200, {"caches": [
                    {"node_id": "lon-edge-1", "fresh": True, "draining": False,
                     "load_permille": load},
                    {"node_id": "lon-edge-3", "fresh": True, "draining": True,
                     "load_permille": 300}]}

            def poll(self, targets):
                for name, (host, port, path) in targets.items():
                    asked.append((name, path))
                return {name: (200, {"node_id": name, "role": "edge", "state": "ready",
                                     "ready": True,
                                     "client_udp": {"subscribers": 12, "hls_sessions": 4,
                                                    "hls_moved_active": 0,
                                                    "hls_requests_in_flight": 0}})
                        if name != "lon-edge-2" else (None, None) for name in targets}

        self.backend.http = FakeHttp()
        roster = {"london": [node for node in self.backend.roster()["london"] if node["name"]
                             in ("lon-dist-a", "lon-dist-b", "lon-edge-1", "lon-edge-2",
                                 "lon-edge-3", "lon-edge-11")]}
        obs = self.backend.observe(roster)
        entries = [name for name, path in asked if path.startswith("/api/assignment")]
        # The first alone; it did not answer, so the rest in service at once.
        self.assertEqual(entries[0], "lon-dist-a")
        self.assertEqual(sorted(entries[1:]), ["lon-dist-b", "lon-edge-1", "lon-edge-2",
                                               "lon-edge-3"])
        self.assertEqual(sorted(name for name, path in asked if path == "/api/node"),
                         ["lon-dist-a", "lon-dist-b", "lon-edge-1", "lon-edge-2", "lon-edge-3"])
        self.assertTrue(obs["lon-edge-1"]["advertised"])
        self.assertEqual(obs["lon-edge-1"]["load_permille"], 640)
        self.assertEqual(obs["lon-edge-1"]["hls_sessions"], 4)
        self.assertEqual(obs["lon-edge-1"]["udp_subscribers"], 12)
        self.assertFalse(obs["lon-edge-2"]["answered"])
        self.assertIsNone(obs["lon-edge-2"]["udp_subscribers"])
        self.assertTrue(obs["lon-edge-3"]["draining"])
        self.assertTrue(obs["lon-edge-1"]["entry_answered"])
        self.assertFalse(obs["lon-edge-11"]["advertised"])  # not created

    def test_adoption_records_a_missing_slot_and_names_anything_else(self):
        """An instance of the run tag that the lab state does not know: a
        slot's (by the label add_slots gives it) is recorded and given the
        firewall; any other is named and left to down; a dry run records
        nothing."""
        instances = [
            {"id": 77, "label": "ntsm-t1-lon-edge-11", "tags": ["needletail-small-mesh-t1"],
             "ipv4": ["192.0.2.201"], "type": "g6-dedicated-2", "created": "2026-11-23T10:00:00",
             "region": "gb-lon"},
            {"id": 78, "label": "ntsm-t1-lon-edge-1x", "tags": ["needletail-small-mesh-t1"],
             "ipv4": ["192.0.2.202"]},
            {"id": 1000, "label": "ntsm-t1-known", "tags": ["needletail-small-mesh-t1"]}]
        calls = []

        class Api:
            def list_all(self, path, tag=None):
                calls.append(("LIST", path, tag))
                return instances

            def call(self, method, path, body=None, allow_404=False):
                calls.append((method, path))
                return {}

        self.backend._api = Api()
        dry = self.backend.adopt(record=False)
        self.assertEqual([(item["id"], item["adopted"]) for item in dry],
                         [(77, False), (78, False)])
        self.assertNotIn("lon-edge-11", json.loads(self.path.read_text())["nodes"])
        found = self.backend.adopt()
        self.assertEqual([(item["id"], item["adopted"]) for item in found],
                         [(77, True), (78, False)])
        self.assertEqual(calls[0], ("LIST", "/linode/instances", "needletail-small-mesh-t1"))
        saved = json.loads(self.path.read_text())["nodes"]["lon-edge-11"]
        self.assertEqual((saved["id"], saved["public_ipv4"], saved["slot"], saved["vlan_ipv4"]),
                         (77, "192.0.2.201", True,
                          self.state["slots"]["lon-edge-11"]["vlan_ipv4"]))
        self.assertIn(("POST", "/networking/firewalls/1/devices"), calls)
        self.assertIn(("PUT", "/networking/firewalls/1/rules"), calls)
        # Adopted once: it is the lab's now.
        self.assertEqual([item["id"] for item in self.backend.adopt()], [78])

    def test_no_slot_is_adopted_into_a_lab_going_down(self):
        """The adoption's record goes through the lab tool's change_state
        (creating): once `down` has begun nothing is written and nothing is
        given the firewall; the instance is left to down's sweep by tag."""
        instances = [{"id": 77, "label": "ntsm-t1-lon-edge-11",
                      "tags": ["needletail-small-mesh-t1"], "ipv4": ["192.0.2.201"],
                      "type": "g6-dedicated-2", "region": "gb-lon"}]
        calls = []

        class Api:
            def list_all(self, path, tag=None):
                return instances

            def call(self, method, path, body=None, allow_404=False):
                calls.append((method, path))
                return {}

        self.backend._api = Api()
        self.state["down_started_unix"] = int(time.time())
        self.path.write_text(json.dumps(self.state))
        with self.assertRaises(auto.Refused) as caught:
            self.backend.adopt()
        self.assertIn("down has begun", str(caught.exception))
        self.assertNotIn("lon-edge-11", json.loads(self.path.read_text())["nodes"])
        self.assertEqual(calls, [])

    def test_an_adopted_slot_whose_firewall_fails_is_deleted(self):
        """As add_nodes does: a slot that cannot have the lab firewall is
        deleted at once (by its recorded id, tag and label checked), never
        left recorded where every push refuses it and it bills until
        down."""
        instance = {"id": 77, "label": "ntsm-t1-lon-edge-11", "tags": ["needletail-small-mesh-t1"],
                    "ipv4": ["192.0.2.201"], "type": "g6-dedicated-2", "region": "gb-lon"}
        calls = []

        class Api:
            def list_all(self, path, tag=None):
                return [instance]

            def call(self, method, path, body=None, allow_404=False):
                calls.append((method, path))
                if method == "POST" and path.endswith("/devices"):
                    raise RuntimeError("firewall: 500")
                return instance if method == "GET" else {}

        self.backend._api = Api()
        with redirect_stdout(io.StringIO()):
            found = self.backend.adopt()
        self.assertEqual([(item["id"], item["adopted"]) for item in found], [(77, False)])
        self.assertIn("deleted", found[0]["why"])
        self.assertIn(("DELETE", "/linode/instances/77"), calls)
        saved = json.loads(self.path.read_text())
        self.assertNotIn("lon-edge-11", saved["nodes"])
        self.assertEqual([item["name"] for item in saved["removed"]], ["lon-edge-11"])


class StopSignalTests(unittest.TestCase):
    def test_sigint_stops_the_controller_even_when_inherited_ignored(self):
        import signal
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)  # as `controller &` in a script
            auto.install_stop_signals()
            self.assertIs(signal.getsignal(signal.SIGINT), auto.interrupt)
            self.assertIs(signal.getsignal(signal.SIGTERM), auto.interrupt)
            with self.assertRaises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGINT)
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)

    def test_a_backgrounded_controller_winds_down_on_sigint(self):
        """End to end, a controller process started with SIGINT ignored (as
        a non-interactive shell starts `&` jobs) stops at SIGINT and resumes
        its drain in flight: the edge_lab backend over a fake lab module."""
        import os
        import signal
        import subprocess
        import textwrap
        work = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        (work / "edge_lab.py").write_text(textwrap.dedent("""
            import json, pathlib
            HERE = pathlib.Path(__file__).parent
            WORK = HERE / "work"
            MESH_ROOT = HERE / "av-mesh"
            CONTRIB_ROOT = HERE / "av-contrib"
            NODES = [("lon-dist-a", "distributor", "london", "gb-lon", "10.90.0.11", "10.77.1.11"),
                     ("lon-dist-b", "distributor", "london", "gb-lon", "10.90.0.12", "10.77.1.12"),
                     ("lon-edge-1", "edge", "london", "gb-lon", "10.90.0.21", "10.77.1.21"),
                     ("lon-edge-2", "edge", "london", "gb-lon", "10.90.0.22", "10.77.1.22"),
                     ("lon-edge-3", "edge", "london", "gb-lon", "10.90.0.23", "10.77.1.23")]
            def slot_nodes(): return []
            def load(name, default=None): return default
            def save(name, value): pass
            def service_pid(name): return 1
            def start_service(name): pass
            def terminate_service(name): pass
            def http_json(name, path, method="GET", body=None, timeout=5.0):
                if "control" in path:
                    with open(HERE / "control.log", "a") as handle:
                        handle.write(name + " " + path + "\\n")
                    return 202, {}
                if "assignment" in path:
                    return 200, {"caches": [{"node_id": n, "fresh": True, "load_permille": 10}
                                            for n in ("lon-edge-1", "lon-edge-2", "lon-edge-3")]}
                return 200, {"node_id": name, "state": "ready", "ready": True,
                             "client_udp": {"subscribers": 5, "hls_sessions": 0,
                                            "hls_moved_active": 0, "hls_requests_in_flight": 0}}
        """))
        script = pathlib.Path(auto.__file__)
        command = [sys.executable, "-c", textwrap.dedent(f"""
            import sys, importlib.util
            sys.path.insert(0, {str(work)!r})
            import edge_lab
            spec = importlib.util.spec_from_file_location("auto", {str(script)!r})
            auto = importlib.util.module_from_spec(spec)
            sys.modules["auto"] = auto
            spec.loader.exec_module(auto)
            sys.argv = ["linode-autoscale.py", "run", "--backend", "edge_lab", "--stream", "1",
                        "--sites", "london", "--controller-state", {str(work / "state.json")!r},
                        "--events", {str(work / "events.jsonl")!r}, "--duration", "60",
                        "--poll-s", "0.2", "--down-hold-s", "0", "--cooldown-s", "0",
                        "--no-flap-s", "0", "--min-edges", "2"]
            auto.main()
        """)]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
                                   env={**os.environ, "NEEDLETAIL_DATA_ROOT": str(work / "data")})
        events = work / "events.jsonl"
        deadline = time.time() + 20
        while time.time() < deadline and not (events.is_file()
                                              and '"drain_start"' in events.read_text()):
            time.sleep(0.05)
        os.kill(process.pid, signal.SIGINT)
        try:
            code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            self.fail("the controller ignored SIGINT")
        self.assertTrue(events.is_file())
        kinds = [json.loads(line)["kind"] for line in events.read_text().splitlines()]
        self.assertEqual(code, 130)
        self.assertIn("drain_start", kinds)
        self.assertEqual(kinds[-3:], ["resumed", "wound_down", "controller_stop"])
        log = (work / "control.log").read_text().split()
        self.assertEqual(log[1::2], ["/api/control/drain-node", "/api/control/resume-node"])
        # It held the lab's ops lock, and let it go.
        self.assertTrue((work / "work" / "ops.lock").exists())
        auto.OpsLock(work / "work" / "ops.lock").acquire().release()


class EdgeLabBackendTests(unittest.TestCase):
    NODES = [("lon-dist-a", "distributor", "london", "gb-lon", "10.90.0.11", "10.77.1.11"),
             ("lon-dist-b", "distributor", "london", "gb-lon", "10.90.0.12", "10.77.1.12"),
             ("lon-dist-c", "distributor", "london", "gb-lon", "10.90.0.13", "10.77.1.13"),
             ("lon-edge-1", "edge", "london", "gb-lon", "10.90.0.21", "10.77.1.21"),
             ("lon-edge-2", "edge", "london", "gb-lon", "10.90.0.22", "10.77.1.22"),
             ("lon-edge-3", "edge", "london", "gb-lon", "10.90.0.23", "10.77.1.23"),
             ("lon-edge-4", "edge", "london", "gb-lon", "10.90.0.24", "10.77.1.24"),
             ("new-dist-a", "distributor", "newyork", "us-east", "10.90.2.11", "10.77.2.11"),
             ("new-dist-b", "distributor", "newyork", "us-east", "10.90.2.12", "10.77.2.12"),
             ("new-edge-1", "edge", "newyork", "us-east", "10.90.2.21", "10.77.2.21"),
             ("new-edge-2", "edge", "newyork", "us-east", "10.90.2.22", "10.77.2.22"),
             ("new-edge-3", "edge", "newyork", "us-east", "10.90.2.23", "10.77.2.23"),
             ("new-dist-c", "distributor", "newyork", "us-east", "10.90.2.13", "10.77.2.13")]

    def fake(self):
        saved = {}
        running = {name for name, *_ in self.NODES if name not in (
            "lon-edge-4", "lon-dist-c", "new-edge-3", "new-dist-c")}
        started = []

        def http_json(name, path, method="GET", body=None):
            if name not in running:
                return None, None
            if "assignment" in path:
                site = "london" if name.startswith("lon-") else "newyork"
                return 200, {"caches": [{"node_id": item, "fresh": True, "load_permille": 500}
                                        for item in sorted(running)
                                        if item.startswith(site[:3]) and "edge" in item]}
            return 200, {"node_id": name, "state": "ready", "ready": True,
                         "hello_draining": False, "drain_reasons": []}

        fake = argparse.Namespace(
            NODES=self.NODES, WORK="/nonexistent",
            slot_nodes=lambda: ["lon-dist-c", "lon-edge-4", "new-dist-c", "new-edge-3"],
            load=lambda name, default=None: saved.get(name, default),
            save=lambda name, value: saved.__setitem__(name, value),
            service_pid=lambda name: 1 if name in running else None,
            start_service=lambda name: (started.append(name), running.add(name)),
            terminate_service=lambda name: running.discard(name),
            http_json=http_json)
        return fake, saved, running, started

    def test_slots_are_created_by_a_marker_and_started_in_their_namespace(self):
        fake, saved, running, started = self.fake()
        backend = auto.EdgeLabBackend(2001, lab=fake)
        roster = backend.roster()["london"]
        by_name = {node["name"]: node for node in roster}
        self.assertFalse(by_name["lon-edge-4"]["created"])
        self.assertTrue(by_name["lon-edge-4"]["slot"])
        self.assertTrue(by_name["lon-dist-a"]["first"])
        self.assertEqual(by_name["lon-edge-1"]["vlan"], "10.77.1.21")
        guard = auto.Guarded(backend, auto.Config())
        guard.roster()
        guard.create(["lon-edge-4"])
        guard.roster()
        guard.push(["lon-edge-4"])
        self.assertEqual(started, ["lon-edge-4"])
        obs = backend.observe(backend.roster())
        self.assertTrue(obs["lon-edge-4"]["advertised"])
        guard.roster()
        guard.delete("lon-edge-4", proof="drained-to-zero")
        self.assertNotIn("lon-edge-4", running)
        self.assertEqual(saved["slots-created.json"], [])
        self.assertEqual(backend.lock_path(), pathlib.Path("/nonexistent/ops.lock"))

    def test_the_far_region_has_its_slots_and_its_entry(self):
        """New York: its own first forwarders, its edge and forwarder slots,
        its own entry; an edge of the lab that the controller stops is held
        out (a standby) and started again; a slot it stops leaves
        service."""
        fake, saved, running, started = self.fake()
        backend = auto.EdgeLabBackend(2001, lab=fake)
        roster = backend.roster()
        self.assertEqual(sorted(roster), ["london", "newyork"])
        by_name = {node["name"]: node for node in roster["newyork"]}
        self.assertTrue(by_name["new-dist-a"]["first"] and by_name["new-dist-b"]["first"])
        self.assertTrue(by_name["new-dist-c"]["slot"] and by_name["new-edge-3"]["slot"])
        self.assertFalse(by_name["new-dist-c"]["first"])
        obs = backend.observe(roster)
        self.assertTrue(obs["new-edge-1"]["advertised"] and obs["new-edge-1"]["entry_answered"])
        self.assertTrue(obs["new-dist-a"]["advertised"])  # ready, hellos not draining
        self.assertFalse(obs["new-edge-3"]["advertised"])
        guard = auto.Guarded(backend, auto.Config())
        guard.roster()
        guard.stop("new-edge-2")
        held = {node["name"]: node for node in backend.roster()["newyork"]}["new-edge-2"]
        self.assertTrue(held["created"] and held["held"])
        guard.roster()
        guard.start("new-edge-2")
        self.assertFalse({node["name"]: node for node in backend.roster()["newyork"]}[
            "new-edge-2"]["held"])
        guard.create(["new-dist-c"])
        guard.roster()
        guard.push(["new-dist-c"])
        self.assertIn("new-dist-c", running)
        guard.roster()
        guard.stop("new-dist-c")
        self.assertNotIn("new-dist-c", saved["slots-created.json"])
        self.assertEqual(backend.probe("new-dist-c"),
                         {"ssh": True, "unit_active": False, "instance": "running"})
        guard.roster()
        with self.assertRaises(auto.Refused):
            backend.rebuild("new-edge-1")
        guard.recover("new-edge-1", "restart", proof="lost")
        self.assertIn("new-edge-1", running)

    def test_a_control_command_answered_202_is_accepted(self):
        # av-mesh queues drain-node and resume-node and answers 202 Accepted:
        # the edge_lab backend must not take that as a failure (it did, so
        # the controller could never drain an edge in the local lab).
        calls = []

        def http_json(name, path, method="GET", body=None):
            calls.append((name, path, method, body))
            return (202 if "control" in path else 500), {}

        fake = argparse.Namespace(NODES=[], slot_nodes=lambda: [], load=lambda *a, **k: [],
                                  save=lambda *a: None, http_json=http_json)
        backend = auto.EdgeLabBackend(2001, lab=fake)
        backend.drain("lon-edge-4")
        backend.resume("lon-edge-4")
        self.assertEqual([call[1] for call in calls],
                         ["/api/control/drain-node", "/api/control/resume-node"])
        self.assertEqual(calls[0][3], {"node_id": "lon-edge-4"})
        failing = argparse.Namespace(NODES=[], slot_nodes=lambda: [], load=lambda *a, **k: [],
                                     save=lambda *a: None,
                                     http_json=lambda *a, **k: (503, {}))
        with self.assertRaises(RuntimeError):
            auto.EdgeLabBackend(2001, lab=failing).drain("lon-edge-4")
        self.assertTrue(auto.control_accepted(200))
        self.assertFalse(auto.control_accepted(None))


class Archive(Base):
    """The controller's events and state transitions in the run archive."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "data"
        patcher = mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(auto.archived_run, "code_paths", lambda *_: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def lines(self, run_id: str, name: str) -> list[dict]:
        path = self.root / run_id / "autoscale" / name
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_events_and_each_change_of_state_go_into_the_run(self):
        record = auto.archived_run.Run("as1", "local-lab", "linode-autoscale", argv=["a"],
                                       prefix="autoscale/")
        backend = auto.Guarded(self.sim, self.config, False)
        events = auto.Events(str(self.events_path), echo=False, clock=self.clock, record=record)
        controller = auto.Controller(backend, self.config, self.state_path, events,
                                     clock=self.clock)
        controller.stop_wait_s = 0.2
        self.sim.set_load(760)
        self.ticks(controller, 6)
        controller.close()
        record.close()
        archived = self.lines("as1", "events.ndjson")
        written = [json.loads(line) for line in self.events_path.read_text().splitlines()]
        self.assertEqual(archived, written)
        self.assertTrue(all(isinstance(item["unix_us"], int) for item in archived))
        self.assertIn("join", [item["kind"] for item in archived])
        states = self.lines("as1", "state.ndjson")
        self.assertGreater(len(states), 1)
        self.assertEqual(len({json.dumps(item["state"], sort_keys=True) for item in states}),
                         len(states), "a state is kept only when it changed")
        self.assertEqual(states[-1]["state"], json.loads(self.state_path.read_text()))
        manifest = json.loads((self.root / "as1" / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "complete")
        units = {entry["path"]: entry["units"] for entry in manifest["files"]}
        self.assertEqual(units["autoscale/events.ndjson"]["unix_us"], "us")

    def test_a_dry_run_writes_nothing_to_the_archive(self):
        events = auto.Events(None, echo=False, clock=self.clock)
        backend = auto.Guarded(self.sim, self.config, True)
        controller = auto.Controller(backend, self.config, self.state_path, events,
                                     clock=self.clock)
        controller.stop_wait_s = 0.2
        self.ticks(controller, 2)
        controller.close()
        self.assertFalse(self.root.exists())

    def test_the_linode_backend_appends_to_the_lab_s_run_and_never_ends_it(self):
        backend = auto.LinodeBackend(str(self.tmp / "lab.json"), STREAM, self.config)
        state = {"run_id": "lab7", "tag": "needletail-small-mesh-lab7",
                 "nodes": {"lon-edge-1": {"region": "gb-lon"}, "tok-edge-1": {"region": "ap-northeast"}}}
        args = argparse.Namespace(command="run", backend="linode", stream=STREAM, run_id=None)
        with mock.patch.object(backend, "state", lambda: state):
            record = auto.archive_record(args, backend, self.config, ["london"])
        record.close()
        manifest = json.loads((self.root / "lab7" / "manifest.json").read_text())
        self.assertEqual((manifest["kind"], manifest["status"]), ("linode-lab", "running"))
        self.assertEqual(manifest["regions"], ["ap-northeast", "gb-lon"])
        self.assertEqual(manifest["deploy_settings"]["backend"], "linode")
        outcome = self.lines("lab7", "outcomes.ndjson")
        self.assertEqual(outcome[0]["status"], "complete")

    def test_the_edge_lab_backend_has_a_run_of_its_own_unless_one_is_named(self):
        fake = types.SimpleNamespace(NODES=[("lon-edge-1", "edge", "london", "gb-lon", "a", "b")],
                                     MESH_ROOT=self.tmp / "av-mesh", CONTRIB_ROOT=self.tmp / "c")
        backend = auto.EdgeLabBackend(STREAM, ["london"], lab=fake)
        args = argparse.Namespace(command="scale", backend="edge_lab", stream=STREAM, run_id=None,
                                  direction="up", site="london", tier="edges")
        record = auto.archive_record(args, backend, self.config, ["london"])
        record.close()
        self.assertRegex(record.run_id, r"^linode-autoscale-\d{8}T\d{6}Z$")
        manifest = json.loads((record.path / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["deploy_settings"]["direction"], "up")
        named = auto.archive_record(argparse.Namespace(**{**vars(args), "run_id": record.run_id}),
                                    backend, self.config, ["london"])
        self.assertFalse(named.made)

    def test_the_archive_is_written_off_the_decision_loop(self):
        record = auto.archived_run.Run("as2", "local-lab", "linode-autoscale", argv=["a"],
                                       prefix="autoscale/")
        appended = []
        real = record.append_ndjson
        loop = threading.current_thread()

        def slow(name, records, *args, **kwargs):
            appended.append((threading.current_thread() is loop, name, len(records)))
            time.sleep(0.3)
            return real(name, records, *args, **kwargs)

        record.append_ndjson = slow
        events = auto.Events(None, echo=False, clock=self.clock, record=record)
        began = time.monotonic()
        for index in range(20):
            events("decision", index=index)
        events.state({"phase": "steady"})
        self.assertLess(time.monotonic() - began, 0.25, "an event never waits on the archive")
        record.close()
        self.assertFalse(any(on_loop for on_loop, _name, _count in appended))
        self.assertLess(len(appended), 21, "queued lines go in batches")
        self.assertEqual([item["index"] for item in self.lines("as2", "events.ndjson")],
                         list(range(20)))
        self.assertEqual(self.lines("as2", "state.ndjson")[0]["state"], {"phase": "steady"})
        self.assertEqual(json.loads((self.root / "as2" / "manifest.json").read_text())["status"],
                         "complete")

    def test_a_state_is_kept_as_it_was_when_saved(self):
        record = auto.archived_run.Run("as3", "local-lab", "linode-autoscale", argv=["a"],
                                       prefix="autoscale/")
        events = auto.Events(None, echo=False, clock=self.clock, record=record)
        state = {"phase": "joining"}
        events.state(state)
        state["phase"] = "steady"
        events.state(state)
        record.close()
        self.assertEqual([item["state"]["phase"] for item in self.lines("as3", "state.ndjson")],
                         ["joining", "steady"])

    def test_main_runs_the_controller_when_the_archive_refuses(self):
        for sig in (signal.SIGINT, signal.SIGTERM):  # main() installs its own
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

        def refused(args, backend, config, sites):
            raise auto.archived_run.ra.ArchiveError("run 'x' refused: disk full")

        ran = []
        with mock.patch.object(auto, "make_backend", lambda args, config, sites: self.sim), \
                mock.patch.object(auto, "archive_record", refused), \
                mock.patch.object(auto, "command_scale",
                                  lambda *args, **kwargs: ran.append(args) or auto.EXIT_DONE), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as exit_:
            auto.main(["scale", "up", "--site", "london", "--backend", "edge_lab",
                       "--controller-state", str(self.state_path)])
        self.assertEqual(exit_.exception.code, auto.EXIT_DONE)
        self.assertEqual(len(ran), 1)
        self.assertIn("run archive: not written (ArchiveError", err.getvalue())

    def test_main_ends_the_run_failed_when_the_controller_fails(self):
        for sig in (signal.SIGINT, signal.SIGTERM):  # main() installs its own
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        made = {}

        def record(args, backend, config, sites):
            made["run"] = auto.archived_run.Run("as9", "local-lab", "linode-autoscale",
                                                argv=["a"], prefix="autoscale/")
            return made["run"]

        def failing(*_args, **_kwargs):
            raise RuntimeError("observation failed")

        with mock.patch.object(auto, "make_backend", lambda args, config, sites: self.sim), \
                mock.patch.object(auto, "archive_record", record), \
                mock.patch.object(auto, "command_scale", failing), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as exit_:
            auto.main(["scale", "up", "--site", "london", "--backend", "edge_lab",
                       "--controller-state", str(self.state_path)])
        self.assertEqual(exit_.exception.code, auto.EXIT_FAILED)
        manifest = json.loads((self.root / "as9" / "manifest.json").read_text())
        self.assertEqual((manifest["status"], manifest["status_reason"]), ("failed", "failed"))


if __name__ == "__main__":
    unittest.main()
