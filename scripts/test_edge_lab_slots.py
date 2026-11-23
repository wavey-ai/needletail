#!/usr/bin/env python3
"""Tests of the local lab's slot nodes and the scale-ops scenarios, offline
(no namespace, no service started): lon-dist-c is a slot always and
lon-edge-4 with `up slots=lon-edge-4`; a slot out of service is left out of
edges(), distributors() and what restore starts; the deploy renders
lon-dist-c as a lease child of lon-dist-a and lon-dist-b and never as a
compiled relay child; `drain` names its node; the campaign's needletail-load
command, UDP report and move spread; the scenarios are registered.
python3 -m unittest scripts/test_edge_lab_slots.py"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402

_spec = importlib.util.spec_from_file_location("edge_campaign_slots", HERE / "edge-campaign.py")
campaign = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(campaign)
COMPILER = HERE.parent / "target" / "release" / "needletail-compile"


class Base(unittest.TestCase):
    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        patcher = mock.patch.object(lab, "WORK", self.work)
        patcher.start()
        self.addCleanup(patcher.stop)


class Slots(Base):
    def test_lon_dist_c_is_a_slot_and_out_of_service_until_taken_in(self):
        self.assertEqual(lab.slot_nodes(), ["lon-dist-c"])
        self.assertNotIn("lon-dist-c", lab.distributors("london"))
        self.assertIn("lon-edge-4", lab.edges("london"))
        lab.save("rendered.json", {name: {"args": [], "rust_log": ""} for name in lab.names()
                                   if lab.node(name)["role"] != "backbone"})
        self.assertNotIn("lon-dist-c", lab.standing_services())
        lab.set_slot_in_service("lon-dist-c", True)
        self.assertIn("lon-dist-c", lab.distributors("london"))
        self.assertIn("lon-dist-c", lab.standing_services())
        lab.set_slot_in_service("lon-dist-c", False)
        self.assertNotIn("lon-dist-c", lab.distributors("london"))
        with self.assertRaises(lab.LabError):
            lab.set_slot_in_service("lon-edge-1", True)

    def test_up_slots_holds_lon_edge_4_out_too(self):
        lab.save("slots.json", ["lon-edge-4", "lon-dist-c"])
        self.assertEqual(lab.slot_nodes(), ["lon-dist-c", "lon-edge-4"])
        self.assertEqual(lab.edges("london"), ["lon-edge-1", "lon-edge-2", "lon-edge-3"])
        lab.set_slot_in_service("lon-edge-4", True)
        self.assertIn("lon-edge-4", lab.edges("london"))

    def test_the_lab_state_names_its_slots_for_the_deploy(self):
        state = lab.lab_state()
        self.assertEqual(sorted(state["slots"]), ["lon-dist-c", "lon-edge-4"])
        self.assertEqual(state["slots"]["lon-dist-c"]["vlan_ipv4"], "10.77.1.13")
        self.assertIn("lon-dist-c", state["nodes"])

    def test_drain_names_its_node(self):
        calls = []
        with mock.patch.object(lab, "http_json", lambda name, path, method="GET", body=None:
                               calls.append((name, path, method, body)) or (200, {})), \
                mock.patch.object(sys, "argv", ["edge_lab.py", "drain", "lon-edge-2"]), \
                redirect_stdout(io.StringIO()):
            lab.main()
        self.assertEqual(calls, [("lon-edge-2", "/api/control/drain-node", "POST",
                                  {"node_id": "lon-edge-2"})])


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Render(Base):
    def test_lon_dist_c_is_a_lease_child_never_a_relay_child(self):
        import small_mesh_common
        # deploy_module points the shared artifact directory at the lab's;
        # put it back for the other tests.
        patcher = mock.patch.object(small_mesh_common, "ARTIFACT_DIR",
                                    small_mesh_common.ARTIFACT_DIR)
        patcher.start()
        self.addCleanup(patcher.stop)
        deploy = lab.deploy_module()
        with redirect_stderr(io.StringIO()):
            settings = deploy.load_settings([f"{key}={json.dumps(value)}"
                                             for key, value in lab.DEFAULT_OVERRIDES.items()])
        routes = {"best_direct_rtt_us": 72000, "primary_rtt_us": 72000,
                  "primary_jitter_us": 1000, "secondary_rtt_us": 78000,
                  "secondary_jitter_us": 1000}
        state = lab.lab_state()
        plan = deploy.compile_plan(deploy.build_program(state, settings, routes))
        self.assertNotIn("lon-dist-c", {item["node_id"] for item in plan["services"]})
        rendered = deploy.render(state, settings, plan)
        args = rendered["lon-dist-c"]["args"]
        value = lambda flag: args[args.index(flag) + 1] if flag in args else None  # noqa: E731
        self.assertEqual(value("--cache-mesh-role"), "distributor")
        self.assertEqual(value("--relay-primary-id"), "lon-dist-a")
        self.assertEqual(value("--relay-secondary-id"), "lon-dist-b")
        # Wave 8 stage 5 (AM 12): it is a lease parent of its site's edges
        # (their ranks decide which streams), subscribes at its forwarders
        # for its children's leases, and it and they list each other.
        self.assertEqual(value("--relay-lease-children"), "true")
        self.assertEqual(value("--relay-lease-bind"), "10.77.1.13:22901")
        self.assertNotIn("--relay-lease-primary-children", args)
        self.assertEqual(value("--cache-mesh-upstream-demand"), "true")
        peers = [args[index + 1] for index, arg in enumerate(args) if arg == "--peer"]
        self.assertIn("10.77.1.11:29101", peers)
        self.assertIn("10.77.1.12:29101", peers)
        self.assertIn("10.77.1.13:29101",
                      [arg for index, arg in enumerate(rendered["lon-dist-a"]["args"])
                       if rendered["lon-dist-a"]["args"][index - 1] == "--peer"])
        self.assertNotIn("10.77.1.12:29101",
                         [arg for index, arg in enumerate(rendered["lon-dist-a"]["args"])
                          if rendered["lon-dist-a"]["args"][index - 1] == "--peer"])
        self.assertNotIn("--relay-forward", args)
        # forwarder_slot_feeds_edges=false: as before, no lease child.
        with redirect_stderr(io.StringIO()):
            off = deploy.load_settings([f"{key}={json.dumps(value)}"
                                        for key, value in lab.DEFAULT_OVERRIDES.items()]
                                       + ["forwarder_slot_feeds_edges=false"])
        old = deploy.render(state, off, plan)["lon-dist-c"]["args"]
        self.assertEqual(old[old.index("--relay-lease-children") + 1], "false")
        self.assertNotIn("--cache-mesh-upstream-demand", old)
        # Without lon-dist-c the other nodes' compiled relay ports are the
        # same: it never took a relay child's index.
        bare = {**state, "nodes": {name: node for name, node in state["nodes"].items()
                                   if name != "lon-dist-c"}}
        plain = deploy.compile_plan(deploy.build_program(bare, settings, routes))
        ports = lambda services: sorted(  # noqa: E731
            (item["node_id"], json.dumps(item.get("primary_parent"), sort_keys=True))
            for item in services)
        self.assertEqual(ports(plan["services"]), ports(plain["services"]))


class Campaign(Base):
    def test_the_scale_ops_scenarios_are_registered(self):
        names = {item.name for item in campaign.select(["scale-ops"], True)}
        self.assertEqual(names, {"udp-edge-drain", "udp-edge-kill", "slot-edge-join",
                                 "autoscale-edge-lab"})
        join = next(item for item in campaign.select(["slot-edge-join"], True))
        self.assertEqual(join.needs, ["slots=lon-edge-4"])

    def test_slot_edge_join_fails_when_the_joined_edge_serves_no_one(self):
        """The pass text says the new edge serves readers that arrive
        after it: checked, not only measured."""

        class Job:
            readers: list = []

            def wait(self):
                pass

        class Ctx:
            def __init__(self, finals):
                self.finals = finals
                self.checks = {}
                self.measurements = {}

            def defer(self, function):
                pass

            def check(self, what, ok, value=None):
                self.checks[what] = ok

            def measure(self, key, value):
                self.measurements[key] = value

            def at(self, seconds):
                pass

            def event(self, text):
                pass

            def readers(self, *args, **kwargs):
                job = Job()
                job.readers = [{"final_cache": final} for final in self.finals]
                return job

            def wait_readers(self):
                pass

            def expect_served(self, job):
                pass

            def note(self, text):
                pass

        fresh = {"caches": [{"node_id": "lon-edge-4", "fresh": True}]}
        results = {}
        for finals in (["lon-edge-1", "lon-edge-2"], ["lon-edge-1", "lon-edge-4"]):
            ctx = Ctx(finals)
            with mock.patch.object(lab, "service_pid", lambda name: None), \
                    mock.patch.object(lab, "set_slot_in_service", lambda name, on: None), \
                    mock.patch.object(lab, "start_service", lambda name: None), \
                    mock.patch.object(lab, "http_json", lambda name, path: (200, fresh)), \
                    mock.patch.object(lab, "wait_for", lambda what, test, *a: test()), \
                    mock.patch.object(campaign, "udp_listeners", lambda *a, **k: Job()), \
                    mock.patch.object(campaign, "play", lambda *a, **k: []), \
                    mock.patch.object(campaign, "service_pids", lambda: {"lon-edge-1": 7}), \
                    mock.patch.object(campaign, "udp_report",
                                      lambda ctx, job: {"moves_by_edge": {}}):
                campaign.slot_edge_join(ctx)
            results[tuple(finals)] = ctx.checks["the joined edge serves readers that arrive "
                                                "after it"]
        self.assertEqual(list(results.values()), [False, True])

    def test_the_udp_command_reads_the_edges_public_addresses(self):
        with mock.patch.object(lab, "enter", lambda host: ["nsenter", host]), \
                mock.patch.object(lab, "binary", lambda label: f"/bin/{label}"):
            command = campaign.udp_command("lon-readers", campaign.udp_edges(), 2001, 200, 60,
                                           pathlib.Path("/out"), "x")
        self.assertEqual(command[:3], ["nsenter", "lon-readers", "/bin/needletail-load"])
        edges = command[command.index("--edges") + 1].split(",")
        self.assertEqual(edges, ["10.90.0.21:19446", "10.90.0.22:19446", "10.90.0.23:19446",
                                 "10.90.0.24:19446"])
        self.assertEqual(command[command.index("--ramp") + 1], "200@0")

    def test_the_udp_report_and_the_spread_of_moves(self):
        out = self.work / "udp"
        out.mkdir()
        (out / "summary.json").write_text(json.dumps({
            "total": {"parts": 1000, "missing": 3, "never_first": 0},
            "per_edge": {"10.90.0.22:19446": {"moves": 48}, "10.90.0.23:19446": {"moves": 2}}}))
        (out / "intervals.jsonl").write_text("\n".join(json.dumps(row) for row in (
            {"unix_ms": 1, "total": {"active": 200, "up": 198}},
            {"unix_ms": 2, "total": {"active": 200, "up": 200}},
            {"unix_ms": 3, "phase": "final", "total": {"active": 0, "up": 0}})))
        job = campaign.UdpJob("udp", "lon-readers", out, None, 0, 0)

        class Ctx:
            measurements = {}

            def measure(self, key, value):
                self.measurements[key] = value

        report = campaign.udp_report(Ctx(), job)
        self.assertEqual(report["moves_by_edge"], {"lon-edge-2": 48, "lon-edge-3": 2})
        self.assertEqual(report["stranded_at_end"], 0)
        self.assertEqual(report["missing"], 3)
        self.assertEqual(campaign.move_spread(report["moves_by_edge"],
                                              ["lon-edge-2", "lon-edge-3", "lon-edge-4"]), 2.88)
        self.assertIsNone(campaign.move_spread({}, ["lon-edge-2"]))


if __name__ == "__main__":
    unittest.main()
