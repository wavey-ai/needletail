#!/usr/bin/env python3
"""Tests of the stall hunt's lab driver (scripts/linode-stall-hunt.py,
D-hunt.md 7.1 to 7.3), offline: no lab, no token, no request, no ssh. The
lab's cost is under the hunt's ceilings and every plan is dedicated; the
timeline is the one of 7.3; a dry run prints every command and runs none;
a real run refuses before anything is billed when the lab tool has no
watchdog and budget on `up`; the node replaced at minute 35 is the one
stopped most.
python3 -m unittest scripts/test_stall_hunt.py"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hunt_tool = load("stall_hunt", "linode-stall-hunt.py")
lab_tool = load("stall_hunt_lab", "linode-small-mesh-lab.py")

TOPOLOGY = hunt_tool.TOPOLOGY
TYPES = {plan: {"id": plan, "class": "dedicated", "price": {"hourly": price},
                "region_prices": []}
         for plan, price in (("g6-dedicated-2", 0.054), ("g6-dedicated-4", 0.108))}
TYPES["g6-standard-2"] = {"id": "g6-standard-2", "class": "standard",
                          "price": {"hourly": 0.036}, "region_prices": []}
REGIONS = {region: {"id": region, "status": "ok", "capabilities": ["Vlans"]}
           for region in ("gb-lon", "us-east", "ap-northeast", "ap-southeast", "nl-ams")}


def dry_run(topology: dict | None = None) -> tuple[str, pathlib.Path]:
    """A whole dry run of the hunt: (what it printed, its output directory).
    Any subprocess or ssh would raise."""
    topology = topology or hunt_tool.load_topology()
    out = pathlib.Path(tempfile.mkdtemp()) / "run"
    printed = io.StringIO()

    def refused(*_args, **_kwargs):
        raise AssertionError("a dry run ran something")

    with mock.patch.object(hunt_tool.subprocess, "run", refused), \
            mock.patch.object(hunt_tool.common, "ssh", refused), \
            mock.patch.object(hunt_tool.common, "rsync_to", refused), \
            mock.patch.object(hunt_tool.common, "rsync_from", refused), \
            mock.patch.object(hunt_tool.stall_records, "fetch", refused), \
            contextlib.redirect_stdout(printed):
        hunt = hunt_tool.Hunt(hunt_tool.Runner(True, out), topology, TOPOLOGY, "hunttest",
                              budget_usd=300)
        status = hunt.run()
    assert status == 0, printed.getvalue()[-2000:]
    return printed.getvalue(), out


class Cost(unittest.TestCase):
    def test_cost_under_the_ceiling(self):
        topology = hunt_tool.load_topology()
        figures = hunt_tool.cost(topology)
        self.assertEqual(figures["instances"], 21)
        self.assertAlmostEqual(figures["hourly_usd"], 1.35)
        self.assertLessEqual(figures["hourly_usd"], hunt_tool.MAX_HOURLY_USD)
        self.assertLessEqual(figures["instances"], hunt_tool.MAX_NODES)
        self.assertAlmostEqual(figures["planned_usd"], 2.70)
        self.assertAlmostEqual(figures["run_usd"], 2.808)
        self.assertAlmostEqual(figures["with_one_repeat_usd"], 5.616)
        # The lab tool prices it the same from the API's plans and finds
        # nothing against it: every plan dedicated, every VLAN address once.
        priced = json.loads(json.dumps(topology))
        hourly, problems = lab_tool.topology_cost(priced, TYPES, REGIONS)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(hourly, figures["hourly_usd"])
        self.assertTrue(all(node["type"].startswith("g6-dedicated-")
                            for node in topology["nodes"]))

    def test_a_shared_plan_is_refused(self):
        topology = hunt_tool.load_topology()
        topology["nodes"][0]["type"] = "g6-standard-2"
        with self.assertRaises(SystemExit):
            hunt_tool.hourly_usd(topology)
        _hourly, problems = lab_tool.topology_cost(topology, TYPES, REGIONS)
        self.assertTrue(any("not a dedicated-CPU plan" in line for line in problems))

    def test_every_tool_gets_the_hunt_s_own_state_and_ceilings_and_no_token(self):
        environment = hunt_tool.tool_environment()
        self.assertEqual(environment["NEEDLETAIL_LINODE_MAX_HOURLY_USD"], "1.50")
        self.assertEqual(environment["NEEDLETAIL_LINODE_MAX_NODES"], "24")
        self.assertTrue(environment["NEEDLETAIL_LINODE_LAB_STATE"].endswith("hunt-lab.json"))
        self.assertFalse(any("TOKEN" in key or "PAT" in key for key in environment))


class Timeline(unittest.TestCase):
    def test_the_timeline_s_order(self):
        topology = hunt_tool.load_topology()
        hunt = hunt_tool.Hunt(hunt_tool.Runner(True, pathlib.Path(tempfile.mkdtemp())),
                              topology, TOPOLOGY, "hunttest")
        steps = hunt_tool.timeline(hunt)
        names = [step["name"] for step in steps]
        self.assertEqual(names, [
            "hosts", "watch", "warm the warmed arm", "push", "survey", "media",
            "soak-1", "soak-2", "soak-3", "burn", "touch", "S1", "S2", "S3",
            "warm the rest", "replace the worst edge or bare node", "S4", "S5", "S6", "S7",
            "replace the worst distributor", "collect", "down"])
        minutes = [step["minute"] for step in steps]
        self.assertEqual(minutes, sorted(minutes))
        at = dict(zip(names, minutes))
        self.assertEqual([at[f"S{index}"] for index in range(1, 8)], [20, 23, 26, 40, 45, 57, 70])
        self.assertEqual((at["soak-1"], at["soak-2"], at["soak-3"], at["burn"]), (7, 12, 17, 18))
        self.assertEqual((at["warm the rest"], at["replace the worst edge or bare node"]), (33, 35))
        self.assertEqual((at["collect"], at["down"]), (82, 88))
        seconds = {session["name"]: session["seconds"] for session in hunt_tool.SESSIONS}
        self.assertEqual(seconds, {"S1": 70, "S2": 70, "S3": 70, "S4": 70, "S5": 600,
                                   "S6": 600, "S7": 300})
        # At worst the sessions end before collect, and down comes well
        # inside the second billed hour and before the watchdog.
        self.assertLess(hunt_tool.sessions_end_minute(), at["collect"])
        self.assertLess(at["down"], 120)
        self.assertLess(at["down"] * 60, hunt_tool.WATCHDOG_HOURS * 3600
                        - hunt_tool.LATEST_START_BEFORE_DEADLINE_S)

    def test_the_arms_and_the_per_node_settings(self):
        topology = hunt_tool.load_topology()
        overrides = hunt_tool.node_overrides(topology)
        self.assertEqual(overrides["lon-edge-2"], {"malloc_tune": False, "prefault": False,
                                                   "mlock": "off"})
        large = ["--hop-journal-capacity", str(hunt_tool.HOP_JOURNAL_LARGE)]
        for name in ("lon-edge-1", "lon-edge-3", "tok-edge-1"):
            self.assertEqual(overrides[name]["extra_edge_args"], large)
        for name in ("lon-dist-a", "tok-dist-a"):
            self.assertEqual(overrides[name]["extra_distributor_args"], large)
        self.assertEqual(sorted(topology["hunt"]["warmed"]),
                         ["lon-bare-2", "lon-bare-4", "lon-dist-a", "lon-edge-3", "tok-dist-b"])
        settings = hunt_tool.push_settings(topology)
        self.assertIn("stall_journal_capacity=65536", settings)


class DryRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.printed, cls.out = dry_run()
        cls.lines = cls.printed.splitlines()

    def commands(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle in line]

    def test_a_dry_run_prints_every_command(self):
        up = self.commands("linode-small-mesh-lab.py up ")
        self.assertEqual(len(up), 1)
        for flag in ("--watchdog-hours 1.9", "--optional-sites sydney", "--budget-usd 300",
                     "--run-id hunttest", "NEEDLETAIL_LINODE_MAX_HOURLY_USD=1.50",
                     "NEEDLETAIL_LINODE_MAX_NODES=24"):
            self.assertIn(flag, up[0])
        # The lab is created, changed and deleted by the lab tool alone.
        # first, after each of the two replacements, last
        self.assertEqual(len(self.commands("linode-small-mesh-lab.py hosts")), 4)
        self.assertEqual(len(self.commands("linode-small-mesh-lab.py remove ")), 2)
        self.assertEqual(len(self.commands("linode-small-mesh-lab.py grow ")), 2)
        self.assertEqual(len(self.commands("linode-small-mesh-lab.py down")), 1)
        names = [node["name"] for node in hunt_tool.load_topology()["nodes"]]
        watch = self.commands("needletail-nodecheck watch --out")
        for name in names:
            self.assertTrue(any(line.startswith(f"ssh {name} ") for line in watch), name)
        warm = self.commands("needletail-nodecheck warm")
        self.assertEqual(len(warm), len(names))
        push = self.commands("linode-small-mesh-deploy.py push --remeasure")
        self.assertEqual(len(push), 1)
        self.assertIn("node_overrides=", push[0])
        self.assertEqual(len(self.commands("linode-small-mesh-deploy.py push --nodes")), 1)
        # at minute 0, and again after each replacement (dry: both are shown
        # as survey ends)
        self.assertEqual(len(self.commands("linode-route-survey.py start")), 3)
        self.assertEqual(len(self.commands("echo-send")), 3 * 6)
        self.assertEqual(len(self.commands("touch.sh <<")), 3)
        self.assertEqual(len(self.commands("systemctl restart needletail-contrib")), 7)
        senders = self.commands("aep1-48k-probe send")
        self.assertEqual(len(senders), 7)
        self.assertTrue(all("--lateness-journal" in line for line in senders))
        self.assertEqual(len([line for line in senders if "--epoch-samples 120" in line]), 2)
        self.assertEqual(len(self.commands("cat >run.sh")), 7 * 2)
        self.assertEqual(len(self.commands("/api/hops")), 7 * 5)
        for word in ("create hunttest", "add-tree hunttest", "collected hunttest",
                     "finish hunttest"):
            self.assertEqual(len(self.commands(f"run_archive.py --tool stall-hunt {word}")), 1,
                             word)
        self.assertTrue(any("tar -C /var/lib/needletail-runs/hunt" in line
                            for line in self.lines))

    def test_the_sessions_are_the_referee_s(self):
        text = self.printed
        self.assertIn("--stream 3001", text)
        self.assertIn("--stream 4001", text)
        self.assertIn("ntcmp udp-ingest", text)
        self.assertIn("nt-popus-48s-la.index.json", text)
        self.assertIn("nt-pcm-48s.index.json", text)
        self.assertIn("nt-popus-120s-la.index.json", text)
        self.assertIn("needletail-load --edges", text)
        self.assertIn("--listeners 40", text)

    def test_nothing_is_written_and_no_token_is_named(self):
        self.assertFalse(self.out.exists())
        for word in ("LINODE_TOKEN", "LINODE_PAT", "api.linode.com", "linode-cli"):
            self.assertNotIn(word, self.printed)

    def test_collect_and_down_come_last(self):
        marks = [line for line in self.lines if line.startswith("# minute")]
        self.assertTrue(marks[-2].endswith("collect"))
        self.assertTrue(marks[-1].endswith("down"))


class Preflight(unittest.TestCase):
    def test_a_real_run_refuses_without_the_lab_tool_s_watchdog_and_budget(self):
        topology = hunt_tool.load_topology()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(hunt_tool, "STATE", pathlib.Path(directory) / "hunt-lab.json"), \
                mock.patch.object(hunt_tool, "help_text", lambda _command: "usage: up --run-id"):
            problems = hunt_tool.preflight(topology)
        text = "\n".join(problems)
        self.assertIn("--watchdog-hours", text)
        self.assertIn("--budget-usd", text)

    def test_the_run_stops_before_anything_is_billed(self):
        args = mock.Mock(topology=str(TOPOLOGY), dry=False, run_id="hunttest", budget_usd=300,
                         spent_before_usd=None)
        with mock.patch.object(hunt_tool, "preflight", lambda _topology: ["a problem"]), \
                mock.patch.object(hunt_tool.Runner, "tool",
                                  side_effect=AssertionError("a tool was called")):
            with self.assertRaises(SystemExit) as stopped:
                hunt_tool.command_run(args)
        self.assertIn("nothing was created", str(stopped.exception))


class Replacement(unittest.TestCase):
    def test_the_node_stopped_most_is_chosen(self):
        topology = hunt_tool.load_topology()
        hunt = hunt_tool.Hunt(hunt_tool.Runner(False, pathlib.Path(tempfile.mkdtemp())),
                              topology, TOPOLOGY, "hunttest")

        def watch(stops_ms: list[float]) -> str:
            lines = [{"kind": "sec", "cpu": cpu, "t_us": 1_000_000, "span_us": 1_000_000}
                     for cpu in (0, 1)]
            lines += [{"kind": "tick", "thread": "tick-0", "cpu": 0, "t_us": 2_000_000 + i,
                       "dur_us": int(ms * 1000), "span_us": int(ms * 1000), "n": 1,
                       "run_delay_us": 10} for i, ms in enumerate(stops_ms)]
            return "\n".join(json.dumps(line) for line in lines)

        records = {name: watch([]) for name in hunt.nodes}
        records["lon-edge-1"] = watch([30.0])
        records["lon-bare-3"] = watch([50.0, 40.0])
        records["contrib-lon"] = watch([900.0])     # not a candidate
        with mock.patch.object(hunt.runner, "ssh",
                               lambda name, _command, **_kwargs: records[name]):
            worst, figures = hunt.worst()
        self.assertEqual(worst, "lon-bare-3")
        self.assertAlmostEqual(figures["lon-bare-3"], 45.0)
        quiet = {name: watch([]) for name in hunt.nodes}
        with mock.patch.object(hunt.runner, "ssh",
                               lambda name, _command, **_kwargs: quiet[name]):
            self.assertIsNone(hunt.worst()[0])


if __name__ == "__main__":
    unittest.main()
