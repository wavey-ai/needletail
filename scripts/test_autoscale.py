#!/usr/bin/env python3
"""Tests of the autoscaler (scripts/linode-autoscale.py), offline: a
simulated backend that records every call, a fake clock, no Linode and no
lab. Hysteresis and the trigger; nodes still joining counted; the cooldown
and the no-flap rule; standby edges first, then the warm pool, then a slot
to create; the budget refusal; drain before delete, and a drain that times
out resumed and never deleted; the warm pool released at the end of a paid
hour; a restart from the controller's state (a create adopted, a drain past
its deadline resumed); the safety layer (only slots are created or deleted,
never the dev box, the first distributors or a node without proof that it
holds no listener); scale-down only with live gauges; a dry run that calls
nothing; events with Unix-µs timestamps; the linode backend's roster and
budget from a lab state; the edge_lab backend from a fake lab.
python3 -m unittest scripts/test_autoscale.py"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("autoscale", HERE / "linode-autoscale.py")
auto = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = auto  # its dataclass looks itself up there
_spec.loader.exec_module(auto)


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
    lon-dist-c. Every call is recorded; a node that is started advertises
    on the next observation."""

    name = "sim"

    def __init__(self, clock: Clock):
        self.clock = clock
        self.calls: list[tuple[str, str]] = []
        self.nodes: dict[str, dict] = {}
        for index in range(1, 13):
            name = f"lon-edge-{index}"
            slot = index > 10
            self.nodes[name] = {"role": "edge", "slot": slot, "created": not slot,
                                "running": index <= 5, "load": 500, "udp": 100, "hls": 0,
                                "refusals": 0, "draining": False,
                                "created_unix": clock() - 1800}
        for name, slot in (("lon-dist-a", False), ("lon-dist-b", False), ("lon-dist-c", True)):
            self.nodes[name] = {"role": "distributor", "slot": slot, "created": not slot,
                                "running": not slot, "load": 0, "udp": 0, "hls": 0,
                                "refusals": 0, "draining": False,
                                "created_unix": clock() - 1800}
        self.budget_ok = True
        self.fail_create = False

    def set_load(self, permille: int, names=None) -> None:
        for name, node in self.nodes.items():
            if node["role"] == "edge" and (names is None or name in names):
                node["load"] = permille

    def roster(self):
        out = []
        for name, node in self.nodes.items():
            out.append({"name": name, "role": node["role"], "site": "london",
                        "created": node["created"], "held": node["created"] and not node["running"],
                        "slot": node["slot"], "first": name in ("lon-dist-a", "lon-dist-b"),
                        "created_unix": node["created_unix"] if node["created"] else None,
                        "hourly_usd": 0.054})
        return {"london": out}

    def observe(self, roster):
        out = {}
        for name, node in self.nodes.items():
            serving = node["created"] and node["running"]
            out[name] = {"advertised": serving and node["role"] == "edge",
                         "draining": node["draining"],
                         "load_permille": node["load"] if serving else None,
                         "udp_subscribers": node["udp"] if serving else None,
                         "hls_sessions": node["hls"] if serving else None,
                         "refusals": node["refusals"],
                         "lease_children": node.get("lease") if serving else None,
                         "cache_children": node.get("cache") if serving else None}
        return out

    def record(self, verb, name):
        self.calls.append((verb, name))

    def create(self, name):
        self.record("create", name)
        if self.fail_create:
            raise RuntimeError("API timeout")
        self.nodes[name]["created"] = True
        self.nodes[name]["created_unix"] = self.clock()

    def push(self, name):
        self.record("push", name)
        self.nodes[name]["running"] = True

    def start(self, name):
        self.record("start", name)
        self.nodes[name]["running"] = True

    def stop(self, name):
        self.record("stop", name)
        self.nodes[name]["running"] = False
        self.nodes[name]["draining"] = False

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

    def budget(self, new, offline=False):
        return self.budget_ok, {"budget_usd": 100.0, "new": new}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        self.sim = Sim(self.clock)
        self.config = auto.Config()
        self.state_path = self.tmp / "controller.json"
        self.events_path = self.tmp / "events.jsonl"

    def controller(self, dry_run: bool = False, config: auto.Config | None = None):
        backend = auto.Guarded(self.sim, config or self.config, dry_run)
        events = auto.Events(str(self.events_path), echo=False, clock=self.clock)
        return auto.Controller(backend, config or self.config, self.state_path, events,
                               clock=self.clock)

    def ticks(self, controller, count: int, step: float = 2.0) -> list[list[dict]]:
        out = []
        for _ in range(count):
            out.append(controller.tick())
            self.clock.advance(step)
        return out

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def mutating(self) -> list[tuple[str, str]]:
        return list(self.sim.calls)


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
        self.assertEqual(self.mutating(), [("create", "lon-edge-11"), ("push", "lon-edge-11"),
                                           ("create", "lon-edge-12"), ("push", "lon-edge-12")])
        self.ticks(controller, 1)
        kinds = [item["kind"] for item in self.events()]
        self.assertIn("created", kinds)
        self.assertEqual(kinds.count("serving"), 2)
        serving = [item for item in self.events() if item["kind"] == "serving"]
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
        self.ticks(controller, 3)
        self.assertEqual(self.mutating(), [])
        failures = [item for item in self.events() if item["kind"] == "failure"]
        self.assertEqual({item["what"] for item in failures}, {"budget"})

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

    def test_scale_down_needs_live_gauges(self):
        controller = self.controller()
        self.sim.set_load(100)
        original = self.sim.observe

        def unknown(roster):
            out = original(roster)
            for item in out.values():
                item["udp_subscribers"] = None
            return out

        self.sim.observe = unknown
        decisions = self.ticks(controller, 200)
        self.assertEqual(self.mutating(), [])
        self.assertIn("Phase AM item 8", decisions[-1][0]["reason"])

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
        failures = [item for item in self.events() if item["kind"] == "failure"]
        self.assertEqual(failures[0]["what"], "drain-timeout")
        self.assertTrue(self.sim.nodes["lon-edge-11"]["created"])

    def test_a_drained_slot_is_stopped_kept_warm_then_released_before_its_next_hour(self):
        self.sim.nodes["lon-edge-11"].update(created=True, running=True,
                                             created_unix=self.clock() - 600)
        controller = self.controller()
        self.drain_one("lon-edge-11", controller)
        self.sim.set_load(500)
        self.ticks(controller, 2)
        self.sim.nodes["lon-edge-11"].update(udp=0, hls=0)
        self.ticks(controller, 2)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-11"), ("stop", "lon-edge-11")])
        self.assertIn("lon-edge-11", controller.state["warm"])
        kinds = [item["kind"] for item in self.events()]
        self.assertLess(kinds.index("drain_start"), kinds.index("drain_end"))
        # 600 s old: its paid hour ends 3,000 s from now; released 300 s
        # before.
        self.clock.advance(2600)
        self.ticks(controller, 1)
        self.assertNotIn(("delete", "lon-edge-11"), self.mutating())
        self.clock.advance(120)
        self.ticks(controller, 1)
        self.assertEqual(self.mutating()[-1], ("delete", "lon-edge-11"))

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
        self.assertIn("warm_left", [item["kind"] for item in self.events()])

    def test_a_create_that_fails_after_the_instance_exists_is_parked_warm(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        controller = self.controller(config=auto.Config(max_per_action=1))
        original = self.sim.push

        def failing_push(name):
            self.sim.record("push", name)
            raise auto.Refused("bootstrap failed")

        self.sim.push = failing_push
        self.sim.set_load(950)
        self.ticks(controller, 3)
        self.sim.push = original
        self.assertEqual(self.mutating(), [("create", "lon-edge-11"), ("push", "lon-edge-11"),
                                           ("stop", "lon-edge-11")])
        self.assertEqual(controller.state["warm"]["lon-edge-11"]["proof"], "never-served")
        self.assertIn("parked", [item["kind"] for item in self.events()])

    def test_the_warm_pool_is_taken_back_before_a_new_create(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.nodes["lon-edge-12"].update(created=True, running=False)
        controller = self.controller()
        controller.state["warm"]["lon-edge-12"] = {"since_unix": self.clock(),
                                                   "proof": "drained-to-zero"}
        self.sim.set_load(950)
        self.ticks(controller, 3)
        self.assertEqual(self.mutating()[:1], [("start", "lon-edge-12")])
        self.assertEqual(self.mutating()[1:], [("create", "lon-edge-11"), ("push", "lon-edge-11")])
        self.assertNotIn("lon-edge-12", controller.state["warm"])

    def test_a_drained_standby_edge_is_stopped_not_deleted(self):
        self.sim.nodes["lon-edge-6"]["running"] = True
        controller = self.controller()
        self.drain_one("lon-edge-6", controller)
        self.sim.nodes["lon-edge-6"].update(udp=0, hls=0)
        self.ticks(controller, 3)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-6"), ("stop", "lon-edge-6")])
        self.assertNotIn("lon-edge-6", controller.state["warm"])

    def test_without_the_warm_pool_a_drained_slot_is_deleted_after_it_stops(self):
        self.sim.nodes["lon-edge-11"].update(created=True, running=True)
        config = auto.Config(warm_pool=False)
        controller = self.controller(config=config)
        self.drain_one("lon-edge-11", controller)
        self.sim.nodes["lon-edge-11"].update(udp=0, hls=0)
        self.ticks(controller, 3)
        self.assertEqual(self.mutating(), [("drain", "lon-edge-11"), ("stop", "lon-edge-11"),
                                           ("delete", "lon-edge-11")])


class Forwarders(Base):
    def drain_forwarder(self, controller):
        self.sim.nodes["lon-dist-c"].update(created=True, running=True, udp=0, hls=0)
        controller.state["actions"]["lon-dist-c"] = {
            "kind": "drain", "site": "london", "phase": "drain", "drain_sent": False,
            "role": "distributor", "started_unix": self.clock(),
            "deadline_unix": self.clock() + 60, "reason": "test"}

    def test_a_forwarder_that_still_feeds_edges_is_never_stopped_or_deleted(self):
        controller = self.controller(config=auto.Config(warm_pool=False))
        self.drain_forwarder(controller)
        # No listener of its own (0 and 0, a forwarder's normal state), and
        # its children unknown (no gauge until AM item 12): not drained.
        self.ticks(controller, 35)
        self.assertEqual(self.mutating(), [("drain", "lon-dist-c"), ("resume", "lon-dist-c")])
        failures = [item for item in self.events() if item["kind"] == "failure"]
        self.assertIn("lease children None", failures[0]["detail"])
        # Children known but one still fed: the same.
        self.sim.calls.clear()
        self.sim.nodes["lon-dist-c"].update(lease=0, cache=3)
        self.drain_forwarder(controller)
        self.ticks(controller, 35)
        self.assertNotIn("stop", [verb for verb, _ in self.mutating()])
        self.assertNotIn("delete", [verb for verb, _ in self.mutating()])
        # Both 0: stopped, then deleted.
        self.sim.calls.clear()
        self.sim.nodes["lon-dist-c"].update(lease=0, cache=0)
        self.drain_forwarder(controller)
        self.ticks(controller, 3)
        self.assertEqual(self.mutating(), [("drain", "lon-dist-c"), ("stop", "lon-dist-c"),
                                           ("delete", "lon-dist-c")])


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
        kinds = [item["kind"] for item in self.events()]
        self.assertIn("wound_down", kinds)
        self.assertEqual(kinds[-1], "controller_stop")

    def test_one_controller_per_state_file(self):
        first = auto.hold_lock(self.state_path)
        self.addCleanup(first.close)
        with self.assertRaises(auto.Refused):
            auto.hold_lock(self.state_path)
        first.close()
        auto.hold_lock(self.state_path).close()


class Restart(Base):
    def test_a_create_that_was_under_way_is_adopted_not_repeated(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        self.sim.fail_create = True
        controller = self.controller()
        self.sim.set_load(950)
        self.ticks(controller, 3)
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["actions"]["lon-edge-11"]["phase"], "create")
        # The instance came up after all (the lab tool adopts it by label);
        # a new controller reads the state and carries on from there.
        self.sim.fail_create = False
        self.sim.nodes["lon-edge-11"]["created"] = True
        self.sim.calls.clear()
        again = self.controller()
        self.ticks(again, 1)
        self.assertNotIn(("create", "lon-edge-11"), self.mutating())
        self.assertIn(("push", "lon-edge-11"), self.mutating())

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
                guard.create(name)
            with self.assertRaises(auto.Refused, msg=name):
                guard.delete(name, proof="drained-to-zero")
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
        self.assertEqual(self.mutating(), [("delete", "lon-edge-11")])

    def test_a_dry_run_calls_nothing_and_writes_no_state(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True
        controller = self.controller(dry_run=True)
        self.sim.set_load(950)
        with redirect_stdout(io.StringIO()):
            self.ticks(controller, 3)
        self.assertEqual(self.sim.calls, [])
        self.assertEqual(controller.backend.calls, [("create", "lon-edge-11"),
                                                    ("push", "lon-edge-11"),
                                                    ("create", "lon-edge-12"),
                                                    ("push", "lon-edge-12")])
        self.assertFalse(self.state_path.exists())
        decisions = [item for item in self.events() if item["kind"] == "decision"]
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
        join = [item for item in self.events() if item["kind"] == "decision"
                and item["action"] == "join"][0]
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

    def test_nothing_is_pushed_to_a_slot_without_the_lab_firewall(self):
        deployed = []
        self.backend.deploy = lambda verb, name: deployed.append((verb, name))
        with mock.patch.object(self.backend, "api", lambda: object()), \
                mock.patch.object(self.backend.lab, "firewall_attached",
                                  lambda api, state, name: False):
            with self.assertRaises(auto.Refused):
                self.backend.push("lon-edge-1")
        self.assertEqual(deployed, [])
        with mock.patch.object(self.backend, "api", lambda: object()), \
                mock.patch.object(self.backend.lab, "firewall_attached",
                                  lambda api, state, name: True):
            self.backend.push("lon-edge-1")
        self.assertEqual(deployed, [("push", "lon-edge-1")])

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

    def test_observation_parses_the_assignment_and_the_mesh_api(self):
        assignment = {"caches": [
            {"node_id": "lon-edge-1", "fresh": True, "draining": False, "load_permille": 640},
            {"node_id": "lon-edge-2", "fresh": False, "draining": False, "load_permille": 0},
            {"node_id": "lon-edge-3", "fresh": True, "draining": True, "load_permille": 300}],
            "client_udp": {"subscriptions": 4000, "refusals": 2}}
        local = {"edge_services": [{"node_id": "lon-edge-1", "active_playback_sessions": 12,
                                    "egress_rejected_requests": 1}]}

        def fetch(address, path, timeout=2.0):
            return assignment if path.startswith("/api/assignment") else local

        with mock.patch.object(auto, "http_json", fetch):
            obs = self.backend.observe({"london": [node for node in self.backend.roster()[
                "london"] if node["name"] in ("lon-edge-1", "lon-edge-2", "lon-edge-3",
                                              "lon-edge-11")]})
        self.assertTrue(obs["lon-edge-1"]["advertised"])
        self.assertEqual(obs["lon-edge-1"]["load_permille"], 640)
        self.assertEqual(obs["lon-edge-1"]["hls_sessions"], 12)
        self.assertEqual(obs["lon-edge-1"]["refusals"], 3)
        # No live gauge of UDP subscribers until Phase AM item 8: unknown,
        # never 0 (the cumulative subscriptions are not one).
        self.assertIsNone(obs["lon-edge-1"]["udp_subscribers"])
        self.assertFalse(obs["lon-edge-2"]["advertised"])
        self.assertTrue(obs["lon-edge-3"]["draining"])
        self.assertFalse(obs["lon-edge-11"]["advertised"])  # not created


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
        import tempfile
        import textwrap
        import time
        work = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, work, True)
        (work / "edge_lab.py").write_text(textwrap.dedent("""
            import json, pathlib
            HERE = pathlib.Path(__file__).parent
            NODES = [("lon-dist-a", "distributor", "london"), ("lon-dist-b", "distributor", "london"),
                     ("lon-edge-1", "edge", "london"), ("lon-edge-2", "edge", "london"),
                     ("lon-edge-3", "edge", "london")]
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
                                            for n in ("lon-edge-1", "lon-edge-2", "lon-edge-3")],
                                 "client_udp": {"subscribers": 5}}
                return 200, {"edge_services": [{"node_id": name, "active_playback_sessions": 0}]}
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
                                   preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
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


class EdgeLabBackendTests(unittest.TestCase):
    def test_slots_are_created_by_a_marker_and_started_in_their_namespace(self):
        saved = {}
        running = {"lon-edge-1", "lon-edge-2", "lon-edge-3", "lon-dist-a", "lon-dist-b"}
        started = []

        fake = argparse.Namespace(
            NODES=[("lon-dist-a", "distributor", "london"), ("lon-dist-b", "distributor", "london"),
                   ("lon-dist-c", "distributor", "london"), ("lon-edge-1", "edge", "london"),
                   ("lon-edge-2", "edge", "london"), ("lon-edge-3", "edge", "london"),
                   ("lon-edge-4", "edge", "london")],
            SLOT_NODES=("lon-edge-4", "lon-dist-c"),
            load=lambda name, default=None: saved.get(name, default),
            save=lambda name, value: saved.__setitem__(name, value),
            service_pid=lambda name: 1 if name in running else None,
            start_service=lambda name: (started.append(name), running.add(name)),
            terminate_service=lambda name: running.discard(name),
            http_json=lambda name, path, method="GET", body=None: (200, {
                "caches": [{"node_id": item, "fresh": True, "load_permille": 500}
                           for item in sorted(running)]} if "assignment" in path
                else {"edge_services": []}))
        backend = auto.EdgeLabBackend(2001, lab=fake)
        roster = backend.roster()["london"]
        by_name = {node["name"]: node for node in roster}
        self.assertFalse(by_name["lon-edge-4"]["created"])
        self.assertTrue(by_name["lon-edge-4"]["slot"])
        self.assertTrue(by_name["lon-dist-a"]["first"])
        guard = auto.Guarded(backend, auto.Config())
        guard.roster()
        guard.create("lon-edge-4")
        guard.roster()
        guard.push("lon-edge-4")
        self.assertEqual(started, ["lon-edge-4"])
        obs = backend.observe(backend.roster())
        self.assertTrue(obs["lon-edge-4"]["advertised"])
        guard.roster()
        guard.delete("lon-edge-4", proof="drained-to-zero")
        self.assertNotIn("lon-edge-4", running)
        self.assertEqual(saved["slots-created.json"], [])

    def test_a_control_command_answered_202_is_accepted(self):
        # av-mesh queues drain-node and resume-node and answers 202 Accepted:
        # the edge_lab backend must not take that as a failure (it did, so
        # the controller could never drain an edge in the local lab).
        calls = []

        def http_json(name, path, method="GET", body=None):
            calls.append((name, path, method, body))
            return (202 if "control" in path else 500), {}

        fake = argparse.Namespace(NODES=[], SLOT_NODES=(), load=lambda *a, **k: [],
                                  save=lambda *a: None, http_json=http_json)
        backend = auto.EdgeLabBackend(2001, lab=fake)
        backend.drain("lon-edge-4")
        backend.resume("lon-edge-4")
        self.assertEqual([call[1] for call in calls],
                         ["/api/control/drain-node", "/api/control/resume-node"])
        self.assertEqual(calls[0][3], {"node_id": "lon-edge-4"})
        failing = argparse.Namespace(NODES=[], SLOT_NODES=(), load=lambda *a, **k: [],
                                     save=lambda *a: None,
                                     http_json=lambda *a, **k: (503, {}))
        with self.assertRaises(RuntimeError):
            auto.EdgeLabBackend(2001, lab=failing).drain("lon-edge-4")
        self.assertTrue(auto.control_accepted(200))
        self.assertFalse(auto.control_accepted(None))


if __name__ == "__main__":
    unittest.main()
