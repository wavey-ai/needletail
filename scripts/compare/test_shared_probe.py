#!/usr/bin/env python3
"""Shared probes (the proving run, 24 Nov): several changes one after
another in one probe, each its own label, the probe run on until every
label's length has its no-change windows outside every label; the runner's
put-backs (a start after a kill, a resume after a drain) with their label
holds; a command's marks under the action's own label. And the proving
plan itself: phases-proving.json in one lab, its capacity curve."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("scale_run_shared", HERE / "scale-run.py")
scale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scale)

LOW_LATENCY = scale.ROOT / "deploy" / "low-latency"
PROVING = LOW_LATENCY / "phases-proving.json"
TOPOLOGY = LOW_LATENCY / "topology-proving.json"


def lab(without=()) -> dict:
    state = scale.topology_state(TOPOLOGY, list(without))
    return {**state, "standby_edges": state.get("standby_edges") or []}


class FakeProcess:
    def __init__(self, command, code):
        self.command, self.returncode = command, code
        self.pid = 4242

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class ThePlan(unittest.TestCase):
    def test_two_runs_in_two_groups_every_scenario_one_shared_probe(self):
        groups = {group: [item["name"] for item in scale.load_phase_plan(PROVING, group)[
            "scenarios"]] for group in ("C1", "C2")}
        self.assertEqual(groups, {"C1": ["F1", "R1"], "C2": ["F2", "R2"]})
        spec = json.loads(PROVING.read_text())
        state = lab()
        for scenario in spec["scenarios"]:
            self.assertEqual(scenario["probe"], "shared", scenario["name"])
            for action in scenario["actions"]:
                for target in action.get("targets", []):
                    self.assertIn(target, state["nodes"], scenario["name"])
        kinds = {action["kind"] for scenario in spec["scenarios"] for action in scenario["actions"]}
        self.assertEqual(kinds, {"drain", "resume", "kill", "start", "change", "scale", "upgrade"})
        # Every change of the brief, twice: drain, edge kill, forwarder kill,
        # ingress and contributor kill and return, rolls of each tier, the
        # autoscaler up and down; the one real create in F1, the slot edge
        # joining in F2.
        labels = {scenario["name"]: [action.get("label") or f"{action['kind']}" for action in
                                     scenario["actions"]] for scenario in spec["scenarios"]}
        self.assertIn("scale.up.london.create", labels["F1"])
        self.assertIn("scale.up.london.slot", labels["F2"])
        for name in ("R1", "R2"):
            self.assertEqual([label for label in labels[name]],
                             [f"upgrade.{tier}.{site}" for tier in ("edges", "forwarders")
                              for site in ("london", "newyork", "tokyo", "sydney")]
                             + ["upgrade.contributors"])
        builds = {scenario["name"]: {action.get("build") for action in scenario["actions"]
                                     if action["kind"] == "upgrade"}
                  for scenario in spec["scenarios"]}
        self.assertEqual((builds["R1"], builds["R2"]), ({"next"}, {"first"}))
        for name in ("F1", "F2"):
            ingress = [action["targets"][0] for action in next(
                item for item in spec["scenarios"] if item["name"] == name)["actions"]
                if action["kind"] == "change"]
            self.assertEqual(ingress[2:], ["contrib-lon", "contrib-lon"])
            self.assertIn(ingress[0], ("lon-dist-a", "lon-dist-b"))

    def test_every_label_has_its_null_outside_every_label(self):
        """For every scenario of the plan on the proving lab (and without
        Sydney): labels at least SHARED_GAP_S apart, and every label's length
        has MIN_NULL_WINDOWS + SHARED_NULL_MARGIN no-change windows in its
        probe, counted as change-window.py with the shared-probe patch counts
        them; the probes 360 s at least."""
        for without in ((), ("sydney",)):
            plan = scale.load_phase_plan(PROVING)
            timeline = scale.phase_timeline(plan, lab(without))
            for row in timeline["scenarios"]:
                windows = scale.shared_label_windows(row["actions"])
                self.assertEqual(row["labels"], len(windows))
                for (_a, end), (start, _b) in zip(windows, windows[1:]):
                    self.assertGreaterEqual(start - end, scale.SHARED_GAP_S, row["name"])
                probe_end = row["probe_start"] + row["probe_s"]
                self.assertGreaterEqual(row["probe_s"], scale.PROBE_MIN_S)
                for start, end in windows:
                    self.assertGreaterEqual(start, row["probe_start"] + scale.CW_SKIP_S)
                    self.assertGreaterEqual(
                        scale.shared_null_windows(windows, row["probe_start"], probe_end,
                                                  end - start),
                        scale.MIN_NULL_WINDOWS + scale.SHARED_NULL_MARGIN,
                        (row["name"], start))
            names = [row["name"] for row in timeline["scenarios"]]
            self.assertEqual(names, ["F1", "R1", "F2", "R2"])
        # Without Sydney its rolls are left out of R1 and R2.
        rows = {row["name"]: row for row in scale.phase_timeline(
            scale.load_phase_plan(PROVING), lab(("sydney",)))["scenarios"]}
        self.assertEqual(rows["R1"]["labels"], 7)
        rows = {row["name"]: row for row in scale.phase_timeline(
            scale.load_phase_plan(PROVING), lab())["scenarios"]}
        self.assertEqual(rows["R1"]["labels"], 9)
        self.assertEqual(rows["F1"]["labels"], 12)
        self.assertEqual(rows["F2"]["labels"], 12)

    def test_the_minutes_of_the_schedule(self):
        """The scenarios' lengths that SCALE.md's table gives (probe and the
        scenario's baseline and settle; scale-run plan adds the ramp)."""
        rows = {row["name"]: row for row in scale.phase_timeline(
            scale.load_phase_plan(PROVING), lab())["scenarios"]}
        minutes = {name: round((row["end"] - row["base_start"]) / 60, 1)
                   for name, row in rows.items()}
        self.assertEqual(minutes, {"F1": 37.8, "R1": 25.8, "F2": 24.8, "R2": 25.8})
        text = (LOW_LATENCY / "SCALE.md").read_text()
        for name, value in minutes.items():
            self.assertIn(f"{value:.1f}", text, name)

    def test_the_capacity_curve(self):
        """party-capacity.json: London only, two paths, rising steps, no
        churn, the autoscaler dry, no faults; it loads as a party schedule."""
        spec = scale.load_party(LOW_LATENCY / "party-capacity.json")
        self.assertEqual(spec["shares"], {"london": 1.0})
        self.assertEqual(spec["paths"], 2)
        self.assertEqual(spec["steps"], [150, 300, 450, 600, 750, 900])
        self.assertEqual(spec["churn"]["percent_per_min"], 0)
        self.assertEqual(spec["autoscale"]["controller_args"], ["--dry-run"])
        self.assertNotIn("faults", spec)


class Checks(unittest.TestCase):
    def scenario(self, actions, **extra):
        return {"schema": scale.PHASE_SCHEMA, "defaults": {},
                "scenarios": [{"name": "X", "group": "G", "probe": "shared",
                               "actions": actions, **extra}]}

    def load(self, plan):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "plan.json"
        path.write_text(json.dumps(plan))
        return scale.load_phase_plan(path)

    def test_a_fault_is_put_back_inside_its_probe_and_states_its_span(self):
        kill = {"kind": "kill", "targets": ["lon-edge-2"], "span_s": 60, "at_s": 0}
        with self.assertRaisesRegex(ValueError, "put back by a later start"):
            self.load(self.scenario([kill]))
        with self.assertRaisesRegex(ValueError, "span_s"):
            self.load(self.scenario([{"kind": "drain", "targets": ["lon-edge-1"]},
                                     {"kind": "resume", "targets": ["lon-edge-1"],
                                      "span_s": 5, "at_s": 40}]))
        with self.assertRaisesRegex(ValueError, "at their at_s"):
            self.load(self.scenario([{"kind": "autoscale", "until": "join", "span_s": 10,
                                      "after": {"event": "x", "timeout_s": 5}}]))
        plan = self.load(self.scenario([kill, {"kind": "start", "targets": ["lon-edge-2"],
                                               "span_s": 0, "at_s": 20}]))
        row = scale.phase_timeline(plan, lab())["scenarios"][0]
        self.assertEqual(row["restores"], [])
        self.assertEqual(row["labels"], 1)

    def test_labels_too_close_are_refused(self):
        plan = self.load(self.scenario([
            {"kind": "drain", "targets": ["lon-edge-1"], "span_s": 20, "at_s": 0},
            {"kind": "resume", "targets": ["lon-edge-1"], "span_s": 30, "at_s": 40}]))
        with self.assertRaisesRegex(ValueError, "confound"):
            scale.phase_timeline(plan, lab())
        plan["scenarios"][0]["actions"][1]["at_s"] = 60
        scale.phase_timeline(plan, lab())

    def test_the_null_counts_as_the_patched_scorer(self):
        windows = [(100.0, 130.0), (200.0, 260.0)]
        # A 30 s window fits from 5 s after the start (skip) to 1 s before
        # the first label (tail): a from 5 to 68.
        self.assertEqual(scale.shared_null_windows(windows, 0.0, 99.0, 30.0), 63)
        self.assertEqual(scale.shared_null_windows([], 0.0, 100.0, 30.0), 64)
        tail = scale.shared_probe_tail(windows, 0.0)
        for start, end in windows:
            self.assertGreaterEqual(scale.shared_null_windows(windows, 0.0, 260.0 + tail,
                                                              end - start), 150)


class Runner(unittest.TestCase):
    def setUp(self):
        self.directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.logged = []
        self.args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=None,
                                       next_av_mesh_root=None, next_av_contrib_root=None)

    def runner(self, actions):
        plan = {"schema": scale.PHASE_SCHEMA, "defaults": {},
                "scenarios": [{"name": "S", "group": "G", "probe": "shared",
                               "actions": actions}]}
        timeline = scale.phase_timeline(plan, lab())
        row = timeline["scenarios"][0]
        runner = scale.PhaseRunner(lab(), timeline, time.time() - row["fault"],
                                   self.logged.append, self.args, directory=self.directory)
        return runner, row

    def marks(self):
        return scale.change_run().marks_of(
            (self.directory / "scenarios" / "S" / "marks.txt").read_text())

    def join(self, runner):
        for _ in range(3):
            for thread in list(runner.threads):
                thread.join(timeout=5)

    def test_a_start_puts_a_kill_back_and_its_label_holds(self):
        runner, row = self.runner([
            {"kind": "kill", "targets": ["lon-edge-2"], "span_s": 40, "label_hold_s": 0.3,
             "at_s": 0},
            {"kind": "start", "targets": ["lon-edge-2"], "span_s": 0, "at_s": 20}])
        kill, start = row["actions"]
        sent = []
        with mock.patch.object(scale, "ssh", lambda node, command, **k: sent.append(command)), \
                mock.patch.object(scale, "http_get", lambda node, path, **k: {"ready": True}):
            runner.owed.append(kill)
            runner.act(kill)
            self.assertEqual(list(runner.open_marks["S"]), ["S.kill.lon-edge-2"])
            began = time.time()
            with runner.owed_lock:
                pass
            runner.act(start)
            self.join(runner)
        marks = self.marks()
        self.assertEqual([(kind, label) for kind, _t, label in marks],
                         [("start", "S.kill.lon-edge-2"), ("end", "S.kill.lon-edge-2")])
        self.assertGreaterEqual(marks[1][1] / 1e9 - began, 0.3)    # the hold
        self.assertEqual(runner.open_marks["S"], {})
        self.assertIn("systemctl start needletail-mesh.service", sent[-1])
        held = [item for item in self.logged if item.get("event") == "end"]
        self.assertIn("held 0.3 s", held[-1]["why"])

    def test_the_put_back_clears_what_the_fault_owes(self):
        runner, row = self.runner([
            {"kind": "drain", "targets": ["lon-edge-1"], "span_s": 20, "at_s": 0},
            {"kind": "resume", "targets": ["lon-edge-1"], "span_s": 30, "label_hold_s": 0.2,
             "at_s": 60}])
        drain, resume = row["actions"]
        posted = []
        schedule = runner.schedule()
        self.assertEqual([item["kind"] for item in schedule],
                         ["_probes", "drain", "resume", "_score"])
        with mock.patch.object(scale, "http_post",
                               lambda node, path, body, **k: posted.append(path) or 200), \
                mock.patch.object(runner, "watch_drain",
                                  lambda target, item: runner.mark_end("S", item["labels"][0],
                                                                       "drain_end")):
            runner.owed.append(drain)
            runner.act(drain)
            # As run() does for an action: a put-back of an owed fault.
            with runner.owed_lock:
                runner.owed = [owed for owed in runner.owed if not (
                    (scale.restore_of(owed) or {}).get("kind") == resume["kind"]
                    and owed.get("targets") == resume.get("targets"))]
            began = time.time()
            runner.act(resume)
            self.join(runner)
        self.assertEqual(posted, ["/api/control/drain-node", "/api/control/resume-node"])
        labels = [(kind, label) for kind, _t, label in self.marks()]
        self.assertEqual(labels, [("start", "S.drain.lon-edge-1"), ("end", "S.drain.lon-edge-1"),
                                  ("start", "S.resume.lon-edge-1"), ("end", "S.resume.lon-edge-1")])
        self.assertGreaterEqual(self.marks()[-1][1] / 1e9 - began, 0.2)
        self.assertEqual(runner.owed, [])

    def test_run_drops_the_owed_restore_at_the_put_back(self):
        """run(): the drain is owed when it is carried out, and its own
        resume (an action, not a restore) takes it off what is owed."""
        runner, row = self.runner([
            {"kind": "drain", "targets": ["lon-edge-1"], "span_s": 20, "at_s": 0},
            {"kind": "resume", "targets": ["lon-edge-1"], "span_s": 30, "at_s": 60}])
        runner.start = time.time() - 10_000          # everything is due
        seen = []
        with mock.patch.object(runner, "act", lambda item: seen.append(
                (item["kind"], [owed["kind"] for owed in runner.owed]))), \
                mock.patch.object(runner, "wait_paths", lambda name: 0.0), \
                mock.patch.object(runner, "log_boot_ids", lambda *a: None), \
                mock.patch.object(runner, "watch", lambda *a: None):
            runner.run()
        self.assertEqual(seen, [("drain", ["drain"]), ("resume", [])])

    def test_a_command_s_marks_take_the_action_s_label_and_hold(self):
        runner, row = self.runner([
            {"kind": "upgrade", "tier": "edges", "build": "next", "sites": ["london"],
             "span_s": 100, "label": "upgrade.edges.london", "label_hold_s": 0.2, "at_s": 0,
             "gate_timeout_s": 10, "drain_timeout_s": 15, "ready_timeout_s": 30}])
        self.args.next_av_mesh_root = "/w/next"

        def popen(command, **kwargs):
            kwargs["stdout"].write(b"start 1790000000.000000001 roll.edges\n"
                                   b"end 1790000090.000000001 roll.edges\n")
            return FakeProcess(command, 0)

        with mock.patch.object(scale.subprocess, "Popen", popen), \
                mock.patch.dict(scale.os.environ, {"NEEDLETAIL_AV_MESH_ROOT": "/w/a",
                                                   "NEEDLETAIL_AV_CONTRIB_ROOT": "/w/c"}):
            began = time.time()
            runner.act(row["actions"][0])
        marks = self.marks()
        self.assertEqual([(kind, label) for kind, _t, label in marks],
                         [("start", "S.upgrade.edges.london"), ("end", "S.upgrade.edges.london")])
        self.assertEqual(marks[0][1], 1_790_000_000_000_000_001)
        self.assertGreaterEqual(marks[1][1] / 1e9, began + 0.2)


if __name__ == "__main__":
    unittest.main()
