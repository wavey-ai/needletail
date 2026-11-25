#!/usr/bin/env python3
"""Tests of the heavy egress plan (tools6, item 1): each mesh node's
planned egress at the heaviest stream against its plan's limit, the same
children as the deploy tool feeds, `up`, `grow` and the deploy's `plan`
refusing a topology over a limit unless --allow-egress-over, the 4K
topologies passing at 247 Mbit/s, and status's measured egress.
python3 -m unittest scripts/test_egress_plan.py"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import io
import json
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import egress_plan  # noqa: E402
import test_lab_slots  # noqa: E402


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_module("deploy_egress", "linode-small-mesh-deploy.py")
lab_tool = test_lab_slots.lab_tool
ROOT = HERE.parent
COMPARE_4K = ROOT / "deploy" / "compare-cloudflare" / "topology-4k-lk.json"
PROVING = ROOT / "deploy" / "low-latency" / "topology-proving.json"
SCALE = ROOT / "deploy" / "low-latency" / "topology-scale.json"
PARTY = ROOT / "deploy" / "low-latency" / "topology-party.json"
MERGED = ROOT / "deploy" / "low-latency" / "topology-merged.json"
LONDON_FIRST = ("lon-dist-a", "lon-dist-b")


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def as_cmpd8(topology: dict) -> dict:
    """The topology with London's first hop on g6-dedicated-2, as cmpd8 ran
    it (needletail-compare's topology-4k-lk.json)."""
    changed = copy.deepcopy(topology)
    for node in changed["nodes"]:
        if node["name"] in LONDON_FIRST:
            node["type"] = "g6-dedicated-2"
    return changed


def topology_state(topology: dict) -> dict:
    """A lab state of the topology as `up` leaves it (slots recorded, not
    created)."""
    slots = lab_tool.slot_specs(topology)
    nodes = {}
    for index, node in enumerate(topology["nodes"], 1):
        if node["name"] in slots:
            continue
        nodes[node["name"]] = {
            "name": node["name"], "role": node["role"], "site": node["site"],
            "region": node["region"], "type": node["type"],
            **({"standby": True} if node.get("standby") else {}),
            **({"purpose": node["purpose"]} if node.get("purpose") else {}),
            "public_ipv4": f"198.51.{index // 250}.{index % 250 + 1}",
            "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
    return {"run_id": "t1", "nodes": nodes, "slots": slots}


class Plan(unittest.TestCase):
    def test_cmpd8_s_first_hop_is_what_was_measured_and_is_refused(self):
        """cmpd8's lon-dist-b: three regions in full and three as repair, one
        London edge in full and one as repair: 4.8 x 247 = 1,186 Mbit/s
        (measured about 1.15 Gbit/s), over a g6-dedicated-2's 600."""
        planned = egress_plan.topology_plan(as_cmpd8(load(COMPARE_4K)))
        item = planned["nodes"]["lon-dist-b"]
        self.assertEqual(sorted(item["full"]), ["lon-edge-2", "new-dist-b", "syd-dist-b",
                                                "tok-dist-b"])
        self.assertEqual(sorted(item["repair"]), ["lon-edge-1", "new-dist-a", "syd-dist-a",
                                                  "tok-dist-a"])
        self.assertAlmostEqual(item["mbit"], 1185.6)
        self.assertEqual(item["limit"], 600)
        self.assertEqual(sorted(planned["over"]), list(LONDON_FIRST))
        refused = egress_plan.refusal(planned)
        self.assertIn("lon-dist-b (g6-dedicated-2: 1186 > 600 Mbit/s", refused)
        self.assertIn("--allow-egress-over", refused)
        # The regional forwarders and the edges (two heavy viewers) pass.
        self.assertAlmostEqual(planned["nodes"]["new-dist-a"]["mbit"], 296.4)
        self.assertAlmostEqual(planned["nodes"]["new-edge-1"]["mbit"], 494.0)
        self.assertAlmostEqual(planned["nodes"]["contrib-lon"]["mbit"], 543.4)

    def test_the_4k_topologies_pass_at_247(self):
        """The compare topology (London's first hop on g6-dedicated-4) and the
        proving topology (on g6-dedicated-8) pass; every mesh node is a
        dedicated plan."""
        for path, kind in ((COMPARE_4K, "g6-dedicated-4"), (PROVING, "g6-dedicated-8")):
            topology = load(path)
            self.assertEqual(topology["plan_stream_mbit"], 247, path.name)
            planned = egress_plan.topology_plan(topology)
            self.assertEqual(planned["over"], [], path.name)
            for name in LONDON_FIRST:
                self.assertEqual(planned["nodes"][name]["type"], kind)
            self.assertTrue(all(node["type"].startswith("g6-dedicated-")
                                for node in topology["nodes"]), path.name)
        proving = egress_plan.topology_plan(load(PROVING))
        self.assertAlmostEqual(proving["nodes"]["lon-dist-a"]["mbit"], 1482.0)
        self.assertAlmostEqual(proving["nodes"]["new-dist-a"]["mbit"], 592.8)
        # On g6-dedicated-4 the proving first hop would not pass.
        smaller = copy.deepcopy(load(PROVING))
        for node in smaller["nodes"]:
            if node["name"] in LONDON_FIRST:
                node["type"] = "g6-dedicated-4"
        self.assertEqual(sorted(egress_plan.topology_plan(smaller)["over"]), list(LONDON_FIRST))

    def test_audio_labs_declare_their_rate(self):
        for path in (SCALE, PARTY):
            topology = load(path)
            self.assertEqual(topology["plan_stream_mbit"], 2.6, path.name)
            self.assertEqual(egress_plan.topology_plan(topology)["over"], [], path.name)
        # Planned at 247 instead, the scale lab's London first hop would not.
        self.assertIn("lon-dist-a", egress_plan.topology_plan(load(SCALE), 247)["over"])

    def test_settings_and_limits(self):
        settings = egress_plan.settings_from(
            {"plan_stream_mbit": 100, "max_heavy_egress_mbit": {"g6-dedicated-2": 500}},
            {"plan_stream_mbit": 320, "max_heavy_egress_mbit": "g6-dedicated-4=1000"})
        self.assertEqual(settings["plan_stream_mbit"], 320)
        self.assertEqual(settings["max_heavy_egress_mbit"]["g6-dedicated-2"], 500)
        self.assertEqual(settings["max_heavy_egress_mbit"]["g6-dedicated-4"], 1000)
        self.assertEqual(settings["max_heavy_egress_mbit"]["g6-dedicated-8"], 2400)
        self.assertEqual(egress_plan.settings_from()["plan_stream_mbit"], 247)
        with self.assertRaises(ValueError):
            egress_plan.parse_limits("g6-dedicated-2")
        with self.assertRaises(ValueError):
            egress_plan.settings_from({"plan_repair_share": -1})

    def test_slots_are_judged_only_when_fed(self):
        topology = load(PROVING)
        without = egress_plan.topology_plan(topology)
        fed = egress_plan.topology_plan(topology, include_slots=True)
        self.assertNotIn("lon-edge-5", without["nodes"])
        self.assertIn("lon-edge-5", fed["nodes"])
        self.assertIn("new-dist-a", fed["over"])
        self.assertIn("lon-dist-c", fed["nodes"]["lon-dist-a"]["full"])


class SameAsDeploy(unittest.TestCase):
    """egress_plan.feeds names the children the deploy renders: relay_tree
    for the forwarders' relay children, fast_plane_feed for the edges."""

    def deploy_feeds(self, topology: dict, settings: dict) -> dict:
        state = deploy.active_state(topology_state(topology), settings)
        out: dict[str, dict] = {}

        def add(parent, child, kind):
            out.setdefault(parent, {"full": [], "repair": []})[kind].append(child)

        tree = deploy.relay_tree(state, settings)
        for child in tree["children"]:
            add(child["primary"]["name"], child["node"]["name"], "full")
            add(child["secondary"]["name"], child["node"]["name"], "repair")
        for contributor in (tree["contrib"], tree["standby"]):
            if contributor is None:
                continue
            add(contributor["name"], tree["first"][0]["name"], "full")
            add(contributor["name"], tree["first"][1]["name"], "full")
            add(contributor["name"], tree["first"][1]["name"], "repair")
        feed = deploy.fast_plane_feed(state, deploy.class_plan(settings))
        for kind in ("edges", "slot_forwarders"):
            for name, item in feed[kind].items():
                if name not in state["nodes"]:
                    continue
                add(item["primary"]["name"], name, "full")
                if item["secondary"] is not None:
                    add(item["secondary"]["name"], name, "repair")
        return {name: {kind: sorted(items) for kind, items in value.items()}
                for name, value in out.items()}

    def test_feeds_match(self):
        settings = dict(deploy.DEFAULT_SETTINGS)
        for path in (COMPARE_4K, PROVING, SCALE, MERGED):
            topology = load(path)
            nodes, slots = egress_plan.topology_nodes(topology)
            planned = egress_plan.plan(nodes, egress_plan.settings_from(), slots=slots)
            mine = {name: {"full": sorted(item["full"]), "repair": sorted(item["repair"])}
                    for name, item in planned["nodes"].items() if item["full"] or item["repair"]}
            self.assertEqual(mine, self.deploy_feeds(topology, settings), path.name)


class LabTool(test_lab_slots.Base):

    def test_up_refuses_a_topology_over_its_limits_before_anything_is_created(self):
        path = self.tmp / "cmpd8.json"
        path.write_text(json.dumps(as_cmpd8(load(COMPARE_4K))))
        api = test_lab_slots.FakeApi()
        with mock.patch.object(lab_tool, "MAX_NODES", 40), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 3.0):
            refused = self.refused(lambda: self.up(api, topology=str(path), run_id="t2",
                                                   plan_stream_mbit=None,
                                                   allow_egress_over=False,
                                                   spent_before_usd=0.0))
        self.assertIn("up refused: planned heavy egress over the plan's limit", refused)
        self.assertIn("lon-dist-a", refused)
        self.assertFalse([call for call in api.calls if call == ("POST", "/linode/instances")])
        self.assertFalse(pathlib.Path(self.args.state).exists())

    def test_egress_checked_prints_refuses_and_allows(self):
        topology = as_cmpd8(load(COMPARE_4K))
        refused = self.refused(lab_tool.egress_checked, topology, self.args_ns(), "grow")
        self.assertTrue(refused.startswith("grow refused: planned heavy egress"))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            planned = lab_tool.egress_checked(topology, self.args_ns(allow_egress_over=True),
                                              "up")
        self.assertEqual(sorted(planned["over"]), list(LONDON_FIRST))
        self.assertIn("lon-dist-b", out.getvalue())
        self.assertIn("<-- OVER", out.getvalue())
        self.assertIn("--allow-egress-over: planned heavy egress", err.getvalue())
        # A lower declared rate passes.
        with redirect_stdout(io.StringIO()):
            planned = lab_tool.egress_checked(topology, self.args_ns(plan_stream_mbit=120), "up")
        self.assertEqual(planned["over"], [])
        # The 4K files pass as they are.
        with redirect_stdout(io.StringIO()):
            self.assertEqual(lab_tool.egress_checked(load(PROVING), self.args_ns(), "up")["over"],
                             [])

    def args_ns(self, **kwargs) -> argparse.Namespace:
        return argparse.Namespace(**{"plan_stream_mbit": None, "allow_egress_over": False,
                                     **kwargs})

    def test_the_parser_takes_the_flags(self):
        parser = lab_tool.build_parser()
        for verb in ("up", "check", "grow"):
            extra = {"up": ["--watchdog-hours", "1", "--budget-usd", "5"],
                     "grow": ["--topology", "x"], "check": []}[verb]
            parsed = parser.parse_args([verb, *extra, "--plan-stream-mbit", "320",
                                        "--allow-egress-over"])
            self.assertEqual(parsed.plan_stream_mbit, 320)
            self.assertTrue(parsed.allow_egress_over)


class DeployPlan(unittest.TestCase):
    def state(self, topology: dict, path: pathlib.Path | None = None) -> dict:
        state = topology_state(topology)
        if path is not None:
            state["topology_file"] = str(path)
        return state

    def test_plan_refuses_and_push_only_prints(self):
        settings = dict(deploy.DEFAULT_SETTINGS)
        state = self.state(as_cmpd8(load(COMPARE_4K)))
        with self.assertRaises(SystemExit) as caught, redirect_stdout(io.StringIO()):
            deploy.egress_report(state, settings, refuse=True)
        self.assertIn("plan refused: planned heavy egress", str(caught.exception))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            deploy.egress_report(state, settings, refuse=True, allow=True)
            deploy.egress_report(state, settings, refuse=False)
        self.assertIn("planned  1185.6 /   600", out.getvalue())
        self.assertIn("egress: planned heavy egress", err.getvalue())

    def test_the_topology_file_s_rate_and_a_setting_over_it(self):
        state = self.state(load(PROVING), PROVING)
        settings = dict(deploy.DEFAULT_SETTINGS)
        planned = deploy.lab_egress_plan(state, settings)
        self.assertEqual(planned["settings"]["plan_stream_mbit"], 247)
        self.assertEqual(planned["over"], [])
        # Standby edges are planned (they can be started); slots are not.
        self.assertIn("lon-edge-4", planned["nodes"])
        self.assertNotIn("lon-edge-5", planned["nodes"])
        settings["plan_stream_mbit"] = 333
        over = deploy.lab_egress_plan(state, settings)["over"]
        self.assertIn("lon-edge-1", over)
        self.assertNotIn("lon-dist-a", over)
        settings["plan_stream_mbit"] = 420
        self.assertIn("lon-dist-a", deploy.lab_egress_plan(state, settings)["over"])
        self.assertIn("plan_stream_mbit", deploy.DEFAULT_SETTINGS)
        self.assertEqual(deploy.parse_overrides(["plan_stream_mbit=320"]),
                         {"plan_stream_mbit": 320})

    def test_status_measures_egress_beside_the_plan(self):
        text = ("needletail-mesh active\nNET 100.0 1000 2000 101.0 126000 150002000\n"
                "THP [madvise]")
        rest, measured = deploy.split_net_probe(text)
        self.assertEqual(rest, "needletail-mesh active\nTHP [madvise]")
        self.assertEqual(measured, (1200.0, 1.0))
        self.assertEqual(deploy.split_net_probe("x\nNET garbage")[1], None)
        line = deploy.egress_summary(measured, {"mbit": 1185.6, "limit": 600.0})
        self.assertEqual(line, " net out=1200.0 in=1.0Mbit/s plan=1186/600 <-- EGRESS OVER LIMIT")
        self.assertEqual(deploy.egress_summary(None, None), " net out=- in=-")
        self.assertIn("/proc/net/dev", deploy.STATUS_NET_PROBE)
        parser = deploy.build_parser()
        self.assertTrue(parser.parse_args(["plan", "--allow-egress-over"]).allow_egress_over)


if __name__ == "__main__":
    unittest.main()
