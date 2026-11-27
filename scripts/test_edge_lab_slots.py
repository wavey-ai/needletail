#!/usr/bin/env python3
"""Tests of the local lab's slot nodes, build sets and verbs and of the
scale-ops scenarios, offline (no namespace, no service started): lon-dist-c
and new-dist-c are slots always, lon-edge-4 and new-edge-3 with `up slots=`;
a slot out of service is left out of edges(), distributors() and what
restore starts; the deploy renders a dist-c as a lease child of its site's
dist-a and dist-b and never as a compiled relay child; a second build set is
kept beside the first, rendered with its own binaries' flags and settings,
and a node starts on the set it is given; `drain` names its node; `crash`,
`burn` and `status`; the campaign's needletail-load command, UDP report and
move spread; the scenarios are registered; the change group runs each
change's null and the change through change-run.py and takes the referee's
verdicts and the nodes' boot ids as its checks.
python3 -m unittest scripts/test_edge_lab_slots.py"""

from __future__ import annotations

import importlib.util
import inspect
import io
import json
import math
import pathlib
import shutil
import subprocess
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
        # The lab's state and the host's registry of labs: the test's own.
        for name, value in (("WORK", self.work / "lab"), ("REGISTRY", self.work / "labs")):
            patcher = mock.patch.object(lab, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.work = self.work / "lab"
        self.work.mkdir()


class Slots(Base):
    def test_lon_dist_c_is_a_slot_and_out_of_service_until_taken_in(self):
        self.assertEqual(lab.slot_nodes(), ["lon-dist-c", "new-dist-c"])
        self.assertNotIn("lon-dist-c", lab.distributors("london"))
        self.assertIn("lon-edge-4", lab.edges("london"))
        # The far region has the same pair: a forwarder slot that is held
        # out and an edge that is in service unless `up slots=` names it.
        self.assertEqual(lab.distributors("newyork"), ["new-dist-a", "new-dist-b"])
        self.assertEqual(lab.edges("newyork"), ["new-edge-1", "new-edge-2", "new-edge-3"])
        self.assertEqual(lab.node("new-dist-c")["vlan_ipv4"], "10.77.2.13")
        self.assertEqual(lab.node("new-edge-3")["public_ipv4"], "10.90.2.23")
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
        self.assertEqual(lab.slot_nodes(), ["lon-dist-c", "lon-edge-4", "new-dist-c"])
        self.assertEqual(lab.edges("london"), ["lon-edge-1", "lon-edge-2", "lon-edge-3"])
        lab.set_slot_in_service("lon-edge-4", True)
        self.assertIn("lon-edge-4", lab.edges("london"))

    def test_the_far_region_has_slots_too(self):
        lab.save("slots.json", ["new-edge-3"])
        self.assertEqual(lab.slot_nodes(), ["lon-dist-c", "new-dist-c", "new-edge-3"])
        self.assertEqual(lab.edges("newyork"), ["new-edge-1", "new-edge-2"])
        for slot, listed in (("new-edge-3", lab.edges), ("new-dist-c", lab.distributors)):
            lab.set_slot_in_service(slot, True)
            self.assertIn(slot, listed("newyork"))
            lab.set_slot_in_service(slot, False)
            self.assertNotIn(slot, listed("newyork"))

    def test_the_lab_state_names_its_slots_for_the_deploy(self):
        state = lab.lab_state()
        self.assertEqual(sorted(state["slots"]),
                         ["lon-dist-c", "lon-edge-4", "new-dist-c", "new-edge-3"])
        self.assertEqual(state["slots"]["lon-dist-c"]["vlan_ipv4"], "10.77.1.13")
        self.assertEqual(state["slots"]["new-dist-c"]["vlan_ipv4"], "10.77.2.13")
        self.assertIn("lon-dist-c", state["nodes"])
        self.assertIn("new-edge-3", state["nodes"])

    def test_drain_names_its_node(self):
        calls = []
        with mock.patch.object(lab, "http_json", lambda name, path, method="GET", body=None:
                               calls.append((name, path, method, body)) or (200, {})), \
                mock.patch.object(sys, "argv", ["edge_lab.py", "drain", "lon-edge-2",
                                                "--no-room-check"]), \
                redirect_stdout(io.StringIO()):
            lab.main()
        self.assertEqual(calls, [("lon-edge-2", "/api/control/drain-node", "POST",
                                  {"node_id": "lon-edge-2"})])

    def test_drain_checks_the_room_through_the_engine(self):
        """tools6: `drain NODE` goes through mesh_ops.drain (room first),
        `--surge` passed on; its exit status is the engine's."""
        seen = []

        def engine_drain(ops, name, surge=False, options=None, **kwargs):
            seen.append((name, surge, options.gate_timeout_s))
            return 2

        for argv, surge in ((["drain", "lon-edge-2"], False),
                            (["drain", "lon-edge-2", "--surge"], True)):
            with mock.patch.object(lab.mesh_ops, "drain", engine_drain), \
                    mock.patch.object(lab, "EdgeLabOps", lambda: object()), \
                    mock.patch.object(sys, "argv", ["edge_lab.py", *argv]), \
                    self.assertRaises(SystemExit) as caught:
                lab.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(seen[-1], ("lon-edge-2", surge, 30.0))

    def test_drain_answered_202_is_a_success(self):
        # av-mesh queues a control command and answers 202 Accepted.
        with mock.patch.object(lab, "http_json", lambda *a, **k: (202, {"status": "queued"})), \
                mock.patch.object(sys, "argv", ["edge_lab.py", "drain", "lon-edge-2",
                                                "--no-room-check"]), \
                redirect_stdout(io.StringIO()):
            lab.main()  # no SystemExit
        with mock.patch.object(lab, "http_json", lambda *a, **k: (503, {})), \
                mock.patch.object(sys, "argv", ["edge_lab.py", "resume", "lon-edge-2"]), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            lab.main()

    def test_the_newest_build_of_a_binary_is_taken(self):
        import os
        native, cross = self.work / "native", self.work / "cross"
        self.assertEqual(lab.newest_build(native, cross), native)  # neither built
        cross.write_text("old")
        self.assertEqual(lab.newest_build(native, cross), cross)
        native.write_text("new")
        os.utime(cross, (1, 1))
        self.assertEqual(lab.newest_build(native, cross), native)

    def test_the_status_command_runs(self):
        # main() once bound a local `status` in its drain branch, so the
        # status command failed with UnboundLocalError.
        out = io.StringIO()
        with mock.patch.object(lab, "service_pid", lambda name: None), \
                mock.patch.object(sys, "argv", ["edge_lab.py", "status"]), redirect_stdout(out):
            lab.main()
        self.assertIn("lon-dist-c", out.getvalue())

    def test_status_shows_the_contributors_own_node_status(self):
        # av-contrib answers /api/node with A 2.6's build block (build.id),
        # av-mesh with C 2.3's (build.git); status reads both through
        # mesh_ops.status_of and says why a node is not ready.
        documents = {
            "contrib-lon": {"role": "contributor", "build": {"id": "703fe825245f", "unix": 1},
                            "state": "standing_by", "not_ready": ["no source heard"]},
            "lon-edge-1": {"role": "edge", "build": {"git": "d718b6da3eb1", "built_unix": 1},
                           "state": "ready", "not_ready": [], "drain_reasons": []},
        }
        out = io.StringIO()
        with mock.patch.object(lab, "service_pid",
                               lambda name: 7 if name in documents else None), \
                mock.patch.object(lab, "cgroup_dir", lambda name: None), \
                mock.patch.object(lab, "node_status", lambda name: documents.get(name, {})), \
                mock.patch.object(lab, "edge_view", mock.Mock(side_effect=lab.LabError("x"))), \
                mock.patch.object(sys, "argv", ["edge_lab.py", "status"]), redirect_stdout(out):
            lab.main()
        lines = {line.split()[0]: line for line in out.getvalue().splitlines() if line.strip()}
        self.assertIn("build 703fe825245f state standing_by", lines["contrib-lon"])
        self.assertIn("not ready: no source heard", lines["contrib-lon"])
        self.assertIn("build d718b6da3eb1 state ready drain none", lines["lon-edge-1"])
        self.assertNotIn("not ready", lines["lon-edge-1"])


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
        # The far region's slot is the same thing at its own site.
        far = rendered["new-dist-c"]["args"]
        value = lambda flag: far[far.index(flag) + 1] if flag in far else None  # noqa: E731
        self.assertNotIn("new-dist-c", {item["node_id"] for item in plan["services"]})
        self.assertEqual(value("--relay-primary-id"), "new-dist-a")
        self.assertEqual(value("--relay-secondary-id"), "new-dist-b")
        self.assertEqual(value("--relay-lease-bind"), "10.77.2.13:22901")
        self.assertEqual(value("--cache-mesh-upstream-demand"), "true")
        self.assertNotIn("--relay-forward", far)
        edge = rendered["new-edge-3"]["args"]
        peers = [edge[index + 1] for index, arg in enumerate(edge) if arg == "--peer"]
        for parent in ("10.77.2.11:29101", "10.77.2.12:29101", "10.77.2.13:29101"):
            self.assertIn(parent, peers)

    def test_set_b_is_rendered_with_its_own_binary_s_flags(self):
        import small_mesh_common
        patcher = mock.patch.object(small_mesh_common, "ARTIFACT_DIR",
                                    small_mesh_common.ARTIFACT_DIR)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Two builds that differ in what they take: set b's av-mesh reads no
        # short compact header and its av-contrib has no epoch lane switch,
        # so the deploy tool's render must tell set b's contributor so.
        built = fake_builds(self.work.parent, {
            "a-mesh": "--peer <ADDR>\n  --client-udp-compact-short-header <BOOL>",
            "a-contrib": "--relay-compact-audio <BOOL>\n  --audio-epoch-mesh-lane <MODE>",
            "b-mesh": "--peer <ADDR>",
            "b-contrib": "--relay-compact-audio <BOOL>",
            "tool": ""})
        with mock.patch.multiple(lab, AV_MESH=built["a-mesh"], AV_CONTRIB=built["a-contrib"],
                                 READER=built["tool"], PROBE=built["tool"],
                                 LOAD_BIN=self.work / "none", AV_MESH_NEXT=built["b-mesh"],
                                 AV_CONTRIB_NEXT=built["b-contrib"]), \
                redirect_stderr(io.StringIO()):
            lab.snapshot_binaries()
            lab.render(["rust_log_mesh=debug", "relay_lease_children=false"], build="b")
            lab.render([])
        one, two = lab.load("rendered.json"), lab.load("rendered-b.json")
        self.assertEqual(sorted(one), sorted(two))
        contributor = lab.names("contributor")[0]
        self.assertNotIn("--relay-compact-audio", one[contributor]["args"])
        self.assertNotIn("--audio-epoch-ingress-target", one[contributor]["args"])
        args = two[contributor]["args"]
        self.assertEqual(args[args.index("--relay-compact-audio") + 1], "false")
        self.assertIn("--audio-epoch-ingress-target", args)
        # Each set runs its own copies.
        for rendered, build in ((one, "a"), (two, "b")):
            self.assertEqual(rendered[contributor]["args"][0],
                             str(self.work / "bin" / build / "av-contrib"))
            self.assertEqual(rendered["lon-edge-1"]["args"][0],
                             str(self.work / "bin" / build / "av-mesh"))
        # A setting of set b alone, the deploy tool's and the lab's own.
        self.assertEqual(lab.load("settings.json")["rust_log_mesh"],
                         "av_mesh=info,av_web_service=warn")
        self.assertEqual(lab.load("settings-b.json")["rust_log_mesh"], "debug")
        self.assertEqual(two["lon-edge-1"]["rust_log"], "debug")
        lease = lambda rendered, name: rendered[name]["args"][  # noqa: E731
            rendered[name]["args"].index("--relay-lease-children") + 1]
        for name in lab.names("distributor"):
            self.assertEqual(lease(two, name), "false", name)
        self.assertEqual(lease(one, "lon-dist-a"), "true")
        self.assertEqual(lease(one, "new-dist-b"), "true")
        with self.assertRaises(lab.LabError):
            lab.render(["relay_lease_children=maybe"])


class LabRender(Base):
    """What the lab changes in the deploy tool's render, with a deploy tool
    of the test's own."""

    def render(self, args: dict, overrides=()) -> dict:
        lab.save("binaries.json", {"a": {"av-mesh": "/a/av-mesh", "av-contrib": "/a/av-contrib"}})
        deploy = mock.Mock(RENDER_BINARIES={})
        deploy.load_settings.side_effect = lambda items: {}
        deploy.node_sysctls.return_value = {}
        deploy.render.return_value = {name: {"service": "needletail-mesh", "rust_log": "info",
                                             "args": ["av-mesh", *flags]}
                                      for name, flags in args.items()}
        with mock.patch.object(lab, "deploy_module", lambda: deploy):
            return lab.render(list(overrides))

    def test_a_nodes_drain_is_kept_in_the_labs_own_state(self):
        rendered = self.render({
            "lon-edge-1": ["--drain-state-file", "/var/lib/needletail/needletail-mesh.drained",
                           "--peer", "10.77.1.11:29101"],
            "lon-dist-a": ["--relay-lease-children", "true"]})
        self.assertEqual(rendered["lon-edge-1"]["args"], [
            "/a/av-mesh", "--drain-state-file", str(self.work / "state" / "lon-edge-1.drained"),
            "--peer", "10.77.1.11:29101", "--fec-bind", "0.0.0.0:12001"])
        self.assertTrue((self.work / "state").is_dir())

    def test_a_lab_setting_that_would_change_nothing_is_refused(self):
        with self.assertRaises(lab.LabError) as caught:
            self.render({"lon-dist-a": ["--peer", "x"]}, ["relay_lease_children=false"])
        self.assertIn("--relay-lease-children", str(caught.exception))
        rendered = self.render({"lon-dist-a": ["--relay-lease-children", "true"]},
                               ["relay_lease_children=false"])
        self.assertEqual(rendered["lon-dist-a"]["args"][1:], ["--relay-lease-children", "false"])


def fake_builds(directory: pathlib.Path, helps: dict) -> dict:
    """Executables that answer --help with the given flags, as a build of
    av-mesh or av-contrib does."""
    out = {}
    for name, text in helps.items():
        path = directory / name
        path.write_text(f"#!/bin/sh\nprintf '%b\\n' '  {text}'\n")
        path.chmod(0o755)
        out[name] = path
    return out


class BuildSets(Base):
    """Two builds side by side in one lab."""

    def builds(self, next_mesh: bool = True) -> dict:
        built = fake_builds(self.work.parent, {name: f"--{name}" for name in (
            "a-mesh", "a-contrib", "b-mesh", "reader", "probe")})
        patcher = mock.patch.multiple(
            lab, AV_MESH=built["a-mesh"], AV_CONTRIB=built["a-contrib"], READER=built["reader"],
            PROBE=built["probe"], LOAD_BIN=self.work / "none",
            AV_MESH_NEXT=built["b-mesh"] if next_mesh else None, AV_CONTRIB_NEXT=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        return built

    def test_set_b_is_kept_beside_set_a(self):
        built = self.builds()
        # Before the snapshot: the builds themselves.
        self.assertEqual(lab.binary("av-mesh"), str(built["a-mesh"]))
        self.assertEqual(lab.binary("av-mesh", "b"), str(built["b-mesh"]))
        found = lab.snapshot_binaries()
        directory = self.work / "bin"
        self.assertEqual(found, {
            "a": {label: str(directory / "a" / label) for label in (
                "av-mesh", "needletail-reader", "av-contrib", "aep1-48k-probe")},
            "b": {label: str(directory / "b" / label) for label in ("av-mesh", "av-contrib")}})
        self.assertEqual(lab.load("binaries.json"), found)
        self.assertEqual(lab.build_sets(), ["a", "b"])
        same = lambda copy, source: (directory / copy).read_text() == source.read_text()  # noqa: E731
        self.assertTrue(same("a/av-mesh", built["a-mesh"]))
        self.assertTrue(same("b/av-mesh", built["b-mesh"]))
        # No other av-contrib was given: set b runs set a's, its own copy.
        self.assertTrue(same("b/av-contrib", built["a-contrib"]))
        self.assertEqual(lab.binary("av-mesh"), str(directory / "a" / "av-mesh"))
        self.assertEqual(lab.binary("av-mesh", "b"), str(directory / "b" / "av-mesh"))
        # The tools are set a's.
        self.assertEqual(lab.binary("needletail-reader"),
                         str(directory / "a" / "needletail-reader"))

    def test_a_lab_of_one_build_has_no_set_b(self):
        self.builds(next_mesh=False)
        self.assertEqual(sorted(lab.snapshot_binaries()), ["a"])
        self.assertEqual(lab.build_sets(), ["a"])
        with self.assertRaises(lab.LabError):
            lab.binary("av-mesh", "b")
        lab.save("rendered.json", {"lon-edge-1": {"args": ["/a/av-mesh"], "rust_log": ""}})
        with mock.patch.object(lab.subprocess, "Popen") as popen, \
                self.assertRaises(lab.LabError):
            lab.start_service("lon-edge-1", build="b")
        popen.assert_not_called()
        self.assertEqual(lab.load("node-build.json"), {})

    def test_a_node_starts_on_the_build_set_it_was_given(self):
        lab.save("binaries.json", {"a": {}, "b": {}})
        lab.save("rendered.json", {"lon-edge-1": {"args": ["/a/av-mesh", "--old"],
                                                  "rust_log": "info"}})
        lab.save("rendered-b.json", {"lon-edge-1": {"args": ["/b/av-mesh", "--new"],
                                                    "rust_log": "debug"}})
        lab.save("settings.json", {"tokio_worker_threads": None})
        lab.save("settings-b.json", {"tokio_worker_threads": 1, "node_env": {"X": 1}})
        lab.save("namespaces.json", {"lab": 100, "lon-edge-1": 101})
        started = []

        def start(build=None, **kwargs):
            with mock.patch.object(lab.subprocess, "run"), \
                    mock.patch.object(lab.subprocess, "Popen") as popen:
                popen.return_value.pid = 4000 + len(started)
                lab.start_service("lon-edge-1", build=build, **kwargs)
            command, env = popen.call_args.args[0], popen.call_args.kwargs["env"]
            started.append((command[command.index("101") + 1:], env["RUST_LOG"],
                            env["TOKIO_WORKER_THREADS"], env.get("X")))
            return command

        command = start()
        self.assertEqual(started[-1], (["/a/av-mesh", "--old"], "info", "2", None))
        # Its own cgroup, which a process may be put into (burn).
        self.assertEqual(command[command.index("--unit") + 1], lab.unit_name("lon-edge-1"))
        self.assertIn("Delegate=yes", command)
        self.assertEqual(lab.load("services.json")["lon-edge-1"]["build"], "a")
        start(build="b", extra_args=["--more"])
        self.assertEqual(started[-1], (["/b/av-mesh", "--new", "--more"], "debug", "1", "1"))
        self.assertEqual(lab.load("node-build.json"), {"lon-edge-1": "b"})
        self.assertEqual(lab.load("services.json")["lon-edge-1"]["build"], "b")
        self.assertEqual(lab.load("services.json")["lon-edge-1"]["starts"], 2)
        # A start without a set (a restart, the campaign's restore) keeps it.
        start()
        self.assertEqual(started[-1][0], ["/b/av-mesh", "--new"])
        self.assertEqual(lab.service_args("lon-edge-1"), ["/b/av-mesh", "--new"])
        self.assertEqual(lab.service_args("lon-edge-1", "a"), ["/a/av-mesh", "--old"])
        # Installed, not started: its next start is on set a again.
        lab.set_node_build("lon-edge-1", "a")
        self.assertEqual(lab.node_build("lon-edge-1"), "a")
        self.assertEqual(len(started), 3)
        start()
        self.assertEqual(started[-1][0], ["/a/av-mesh", "--old"])
        for name, build in (("lon-edge-1", "c"), ("lon-edge-2", "b")):
            with self.assertRaises(lab.LabError):
                lab.set_node_build(name, build)


class Verbs(Base):
    def main(self, *arguments: str) -> str:
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["edge_lab.py", *arguments]), redirect_stdout(out):
            lab.main()
        return out.getvalue()

    def test_crash_kills_a_node_and_starts_it_after_the_units_restart_time(self):
        # It was started with flags and an environment of a campaign's own:
        # it comes back with them.
        lab.save("services.json", {"new-dist-a": {
            "pid": 10, "build": "a", "limits": {"cpu_percent": 150},
            "env": {"X": "1"}, "extra_args": ["--more"]}})
        for arguments, restart_s in ((["new-dist-a"], 1.0),
                                     (["new-dist-a", "--restart-s", "0.25"], 0.25)):
            calls = []
            running = {"new-dist-a": 10}

            def start(name, **kwargs):
                calls.append(("start", name, kwargs))
                running[name] = 11
                return 11

            with mock.patch.object(lab, "service_pid", lambda name: running.get(name)), \
                    mock.patch.object(lab, "kill_service", lambda name: (
                        calls.append(("kill", name)), running.pop(name))), \
                    mock.patch.object(lab.time, "sleep",
                                      lambda seconds: calls.append(("sleep", seconds))), \
                    mock.patch.object(lab, "start_service", start), \
                    mock.patch.object(lab, "wait_back", lambda name, timeout: calls.append(
                        ("back", name, timeout))):
                out = self.main("crash", *arguments)
            self.assertEqual(calls, [
                ("kill", "new-dist-a"), ("sleep", restart_s),
                ("start", "new-dist-a", {"limits": {"cpu_percent": 150}, "env": {"X": "1"},
                                         "extra_args": ["--more"]}),
                # The command lasts until the node is back, so that the marks
                # that a wrapper writes around it hold the whole change.
                ("back", "new-dist-a", 60.0)])
            self.assertIn("pid 11", out)
        with mock.patch.object(lab, "service_pid", lambda name: None), \
                self.assertRaises(SystemExit) as stopped:
            self.main("crash", "new-dist-a")
        self.assertEqual(str(stopped.exception), "new-dist-a is not running")
        with self.assertRaises(SystemExit):
            self.main("crash", "new-dist-a", "--hold-s", "1")

    def test_a_node_is_back_when_it_says_ready_or_an_old_build_answers_up(self):
        clock = [0.0]

        def answers(table):
            asked = []

            def http_json(name, path, method="GET", body=None, timeout=5.0):
                asked.append(path)
                return table[path].pop(0) if len(table[path]) > 1 else table[path][0]
            return asked, http_json

        def advance(seconds):
            clock[0] += seconds

        common = (mock.patch.object(lab.time, "time", lambda: clock[0]),
                  mock.patch.object(lab.time, "sleep", advance))
        # A build with /api/node: back when its state is ready, not when it
        # first answers.
        asked, http_json = answers({"/api/node": [
            (0, None), (200, {"state": "starting"}), (200, {"state": "ready"})]})
        with common[0], common[1], mock.patch.object(lab, "http_json", http_json), \
                mock.patch.object(lab, "service_pid", lambda name: 11):
            self.assertEqual(lab.wait_back("lon-edge-1"), "ready")
        self.assertEqual(asked, ["/api/node"] * 3)
        # A build without it (404): back when /up answers.
        asked, http_json = answers({"/api/node": [(0, None), (404, None)],
                                    "/up": [(200, "OK")]})
        with common[0], common[1], mock.patch.object(lab, "http_json", http_json), \
                mock.patch.object(lab, "service_pid", lambda name: 11):
            self.assertEqual(lab.wait_back("contrib-lon"), "up")
        self.assertEqual(asked, ["/api/node", "/api/node", "/up"])
        # Never ready within the bound: an error that says what it saw.
        asked, http_json = answers({"/api/node": [(200, {"state": "degraded"})]})
        with common[0], common[1], mock.patch.object(lab, "http_json", http_json), \
                mock.patch.object(lab, "service_pid", lambda name: 11), \
                self.assertRaises(lab.LabError) as caught:
            lab.wait_back("lon-edge-1", timeout=5)
        self.assertIn("state degraded", str(caught.exception))
        # The service stopped again: an error at once, with its log.
        with mock.patch.object(lab, "service_pid", lambda name: None), \
                self.assertRaises(lab.LabError) as caught:
            lab.wait_back("lon-edge-1")
        self.assertIn("stopped again", str(caught.exception))

    def test_a_node_whose_scope_still_runs_is_not_started_again(self):
        lab.save("rendered.json", {"lon-edge-1": {"args": ["/a/av-mesh"], "rust_log": ""}})
        lab.save("namespaces.json", {"lab": 100, "lon-edge-1": 101})
        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds

        with mock.patch.object(lab, "unit_active", lambda unit: True), \
                mock.patch.object(lab.time, "time", lambda: clock[0]), \
                mock.patch.object(lab.time, "sleep", advance), \
                mock.patch.object(lab.subprocess, "Popen") as popen, \
                self.assertRaises(lab.LabError) as caught:
            lab.start_service("lon-edge-1")
        popen.assert_not_called()
        self.assertIn(lab.unit_name("lon-edge-1"), str(caught.exception))
        # One that ends within the bound is waited for.
        ending = [True, True, False]
        with mock.patch.object(lab, "unit_active", lambda unit: ending.pop(0)), \
                mock.patch.object(lab.time, "sleep"), \
                mock.patch.object(lab.subprocess, "run"), \
                mock.patch.object(lab.subprocess, "Popen") as popen:
            popen.return_value.pid = 12
            self.assertEqual(lab.start_service("lon-edge-1"), 12)
        self.assertEqual(ending, [])

    def test_a_burn_ends_with_its_nodes_service(self):
        # The loop itself, for real: its node's service is gone (a pid that
        # has ended), so it stops at once and leaves the scope free.
        gone = subprocess.Popen(["true"])
        gone.wait()
        lab.save("services.json", {"lon-edge-1": {"pid": 9}})
        popen = subprocess.Popen
        started = []

        def loop(command, **kwargs):
            command = [*command[:-1], str(gone.pid)]
            started.append(command)
            return popen(command, **kwargs)

        with mock.patch.object(lab, "service_pid", lambda name: 9), \
                mock.patch.object(lab, "service_limits", lambda name: {"cpu_percent": 100}), \
                mock.patch.object(lab.subprocess, "Popen", loop), \
                mock.patch.object(lab, "attach", lambda unit, pid: None):
            burned = lab.burn("lon-edge-1", 30)
        self.assertEqual(len(started), 1)
        self.assertLess(burned, 10)

    def test_burn_is_in_the_nodes_cgroup_on_its_cpus(self):
        lab.save("cpus.json", {"lon-edge-1": [10, 11], "_rest": [13, 14, 15]})
        events = []

        class Loop:
            def __init__(self, command, **kwargs):
                self.pid = 500 + len([item for item in events if item[0] == "loop"])
                self.stdin = mock.Mock()
                self.stdin.write = lambda data: events.append(("go", self.pid))
                events.append(("loop", command, kwargs.get("start_new_session")))

            def wait(self):
                events.append(("wait", self.pid))

            def poll(self):
                return 0

        with mock.patch.object(lab, "service_pid", lambda name: 9), \
                mock.patch.object(lab.subprocess, "Popen", Loop), \
                mock.patch.object(lab, "attach",
                                  lambda unit, pid: events.append(("attach", unit, pid))):
            self.main("burn", "lon-edge-1", "20")
        loops = [item for item in events if item[0] == "loop"]
        # One for each CPU of the node's limit, each pinned to the node's.
        self.assertEqual(len(loops), 2)
        for _kind, command, new_session in loops:
            self.assertEqual(command[:4], ["taskset", "-c", "10,11", sys.executable])
            # Its seconds, and the service it ends with.
            self.assertEqual(command[-2:], ["20.0", "9"])
            self.assertTrue(new_session)
        # Each is in the node's cgroup before it is told to begin.
        unit = lab.unit_name("lon-edge-1")
        self.assertEqual(events[2:], [("attach", unit, 500), ("attach", unit, 501),
                                      ("go", 500), ("go", 501), ("wait", 500), ("wait", 501)])
        with mock.patch.object(lab, "service_pid", lambda name: None), \
                self.assertRaises(SystemExit):
            self.main("burn", "lon-edge-1", "20")

    def test_stop_waits_as_long_as_the_unit_does(self):
        self.assertEqual(inspect.signature(lab.terminate_service).parameters["wait"].default,
                         15.0)

    def test_status_names_the_labs_of_the_host_and_each_nodes_build(self):
        mine = {"id": "b", "owner": "jo", "pid": lab.os.getpid(), "worktree": str(lab.ROOT),
                "work": str(self.work.resolve()), "cpus": list(range(16, 30)), "since_unix": 1.0}
        other = {**mine, "id": "a", "owner": "sam", "worktree": "/trees/one",
                 "work": "/trees/one/target/elab-a", "cpus": list(range(2, 16))}
        lab.REGISTRY.mkdir()
        for entry in (mine, other):
            (lab.REGISTRY / f"{entry['id']}.json").write_text(json.dumps(entry))
        lab.save("services.json", {"lon-edge-1": {"pid": 7, "build": "b"},
                                   "lon-edge-2": {"pid": 8, "build": "a"}})
        own = {"build": {"git": "3f2a9c1d77e0"}, "state": "draining",
               "drain_reasons": ["operator"]}

        def http_json(name, path, method="GET", body=None, timeout=5.0):
            # lon-edge-2 runs a build that has no /api/node.
            return (200, own) if (name, path) == ("lon-edge-1", "/api/node") else (404, None)

        def view(name):
            raise lab.LabError("no view")

        with mock.patch.object(lab, "service_pid", lambda name: {
                "lon-edge-1": 7, "lon-edge-2": 8}.get(name)), \
                mock.patch.object(lab, "cgroup_dir", lambda name: None), \
                mock.patch.object(lab, "http_json", http_json), \
                mock.patch.object(lab, "edge_view", view):
            lines = self.main("status").splitlines()
        self.assertRegex(lines[0], r"^lab a +owner sam +CPUs 2-15 +/trees/one/target/elab-a$")
        self.assertRegex(lines[1], r"^lab b +owner jo +CPUs 16-29 .*\(this lab\)$")
        first = next(line for line in lines if line.startswith("lon-edge-1"))
        self.assertIn(" set b ", first)
        self.assertIn("build 3f2a9c1d77e0 state draining drain operator", first)
        second = next(line for line in lines if line.startswith("lon-edge-2"))
        self.assertIn(" set a ", second)
        self.assertNotIn("state", second)


class Campaign(Base):
    def test_the_scale_ops_scenarios_are_registered(self):
        names = {item.name for item in campaign.select(["scale-ops"], True)}
        self.assertEqual(names, {"udp-edge-drain", "udp-edge-kill", "udp-two-edges-drain",
                                 "slot-edge-join", "dist-c-join-kill", "autoscale-edge-lab"})
        scale = next(item for item in campaign.select(["autoscale-edge-lab"], True))
        self.assertIn("client_udp_datagram_capacity=40000", scale.needs)
        join = next(item for item in campaign.select(["slot-edge-join"], True))
        self.assertEqual(join.needs, campaign.SCALE_NEEDS)
        self.assertIn("cache_mesh_warm_ms=4500", join.needs)

    def test_slot_edge_join_fails_when_the_joined_edge_serves_no_one(self):
        """The pass text says the new edge serves readers that arrive
        after it: checked, not only measured."""

        class Job:
            readers: list = []

            def wait(self):
                pass

        class Ctx:
            t0 = 0.0

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

        class FakeOps:
            def __init__(self, ctx):
                pass

            def start(self):
                return self

            def stop(self):
                return []

        report = {"moves_by_edge": {}, "missing": 0, "stranded_max": 0, "stranded_at_end": 0,
                  "never_first": 0, "never_resumed": 0, "unaffected_missing": 0,
                  "fault_lost": 0, "left_lost_by_edge": {}}
        fresh = {"caches": [{"node_id": "lon-edge-4", "fresh": True}]}
        results = {}
        for finals in (["lon-edge-1", "lon-edge-2"], ["lon-edge-1", "10.90.0.24:19444"]):
            ctx = Ctx(finals)
            with mock.patch.object(lab, "service_pid", lambda name: None), \
                    mock.patch.object(lab, "set_slot_in_service", lambda name, on: None), \
                    mock.patch.object(lab, "start_service", lambda name: None), \
                    mock.patch.object(lab, "edges", lambda site="london": ["lon-edge-1"]), \
                    mock.patch.object(lab, "http_json", lambda name, path, **k: (200, fresh)), \
                    mock.patch.object(lab, "wait_for", lambda what, test, *a: test()), \
                    mock.patch.object(campaign, "udp_listeners", lambda *a, **k: Job()), \
                    mock.patch.object(campaign, "play", lambda *a, **k: []), \
                    mock.patch.object(campaign, "service_pids", lambda: {"lon-edge-1": 7}), \
                    mock.patch.object(campaign, "OpsSampler", FakeOps), \
                    mock.patch.object(campaign, "udp_report", lambda ctx, job, **k: report), \
                    mock.patch.object(campaign, "edge_p99", lambda *a: 3.0), \
                    mock.patch.object(campaign, "first_parts", lambda *a: {"lon-edge-4": 9}), \
                    mock.patch.object(campaign, "survivor_checks", lambda *a, **k: {}):
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

    def test_the_udp_command_asks_the_assignment_at_the_entries(self):
        with mock.patch.object(lab, "enter", lambda host: ["nsenter", host]), \
                mock.patch.object(lab, "binary", lambda label: f"/bin/{label}"), \
                mock.patch.object(lab, "ca_file", lambda: "/ca.crt"):
            command = campaign.udp_command("lon-readers", campaign.udp_edges(), 3001, 160, 70,
                                           pathlib.Path("/out"), "x",
                                           entries=campaign.udp_entries())
        self.assertEqual(command[command.index("--entry") + 1],
                         "https://10.90.0.11:19444,https://10.90.0.12:19444")
        self.assertEqual(command[command.index("--tls-ca") + 1], "/ca.crt")
        self.assertEqual(command[command.index("--server-name") + 1], lab.SERVER_NAME)
        # needletail-load refuses --edges with --entry.
        self.assertNotIn("--edges", command)

    def test_the_udp_report_books_a_fault_on_the_edge_left(self):
        out = self.work / "udp"
        out.mkdir()
        (out / "summary.json").write_text(json.dumps({
            "listeners_without_parts": 0, "never_resumed": 0,
            "total": {"parts": 1000, "missing": 7, "never_resumed": 0,
                      "moves_by_reason": {"draining": 40},
                      "time_to_move_ms": {"count": 40, "p50": 10.0, "p99": 16.5, "max": 17.0}},
            "per_edge": {"unassigned": {"moves": 0},
                         "10.90.0.21:19446": {"moves_out": 40, "left_lost": 5, "missing": 0},
                         "10.90.0.22:19446": {"moves": 22, "missing": 4},
                         "10.90.0.23:19446": {"moves": 18, "missing": 3}}}))
        (out / "intervals.jsonl").write_text("\n".join(json.dumps(row) for row in (
            {"unix_ms": 1, "total": {"active": 160, "up": 150, "stranded": 2}},
            {"unix_ms": 2, "total": {"active": 160, "up": 160, "stranded": 0}})))
        job = campaign.UdpJob("udp", "lon-readers", out, None, 0, 0)

        class Ctx:
            measurements = {}

            def measure(self, key, value):
                self.measurements[key] = value

        report = campaign.udp_report(Ctx(), job, faulted=["lon-edge-1"])
        self.assertEqual(report["moves_by_edge"],
                         {"lon-edge-1": 0, "lon-edge-2": 22, "lon-edge-3": 18})
        self.assertEqual(report["moves_out_by_edge"], {"lon-edge-1": 40})
        self.assertEqual(report["fault_lost"], 5)
        self.assertEqual(report["unaffected_missing"], 2)
        self.assertEqual(report["stranded_max"], 2)
        self.assertEqual(report["stranded_at_end"], 0)
        self.assertEqual(report["time_to_move_ms"]["p99"], 16.5)
        self.assertEqual(campaign.move_spread(report["moves_by_edge"],
                                              ["lon-edge-2", "lon-edge-3"]), 1.1)

    def test_the_join_warm_up_ends_at_its_first_zero(self):
        pending = [(20.5, 9941), (21.7, 2487), (23.0, 0), (24.2, 0), (35.1, 1016), (36.4, 0)]
        self.assertEqual(campaign.join_warm_up_s(pending, 20.0), 3.0)
        self.assertEqual(campaign.join_warm_up_s([(21.0, 0), (22.0, 0)], 20.0), 0.0)
        self.assertEqual(campaign.join_warm_up_s([(21.0, 5), (22.0, 7)], 20.0), 2.0)

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

    def test_the_change_scenarios_are_registered(self):
        """C CL5: the scored changes of C 6.3, the three rolls and the
        forwarder crash again in the three more cells, all in the `change`
        group, slow, on change-run.py's lab settings for their lanes."""
        names = {item.name for item in campaign.select(["change"], True)}
        scored = {"roll-edges", "roll-forwarders", "upgrade-all", "upgrade-back",
                  "forwarder-kill-return", "replace-edge", "replace-forwarder", "scale-up",
                  "scale-down", "autoscale-edges-up", "autoscale-edges-down",
                  "autoscale-forwarders"}
        cells = {f"{name}-{cell}" for name in ("roll-edges", "roll-forwarders", "upgrade-all",
                                               "forwarder-kill-return")
                 for cell in ("opus25", "client-loss", "wan-loss")}
        self.assertEqual(names, scored | cells)
        items = {item.name: item for item in campaign.select(["change"], True)}
        self.assertTrue(all(item.slow and item.group == "change" for item in items.values()))
        self.assertEqual(campaign.select(["change"], False), [])
        self.assertEqual(items["roll-edges"].needs, [
            "daw_hls_formats=opus,pcm", "opus_epoch_samples=48", "pcm_epoch_samples=48",
            "slots=lon-edge-4"])
        self.assertEqual(items["roll-forwarders-opus25"].needs,
                         ["daw_hls_formats=opus", "opus_epoch_samples=120"])
        for name in ("scale-up", "scale-down", "autoscale-edges-up"):
            self.assertTrue(set(campaign.SCALE_NEEDS) <= set(items[name].needs), name)

    def change_runs(self, ctx_name, command_exit=0, cw_pass=True, before=None, after=None,
                    null_windows=300):
        """The scenario of `ctx_name` run with change-run.py faked: each call
        writes one run of its kind as change-run.py does (run.json with the
        referee's verdict, nodes-before.json and nodes-after.json)."""
        root = pathlib.Path(tempfile.mkdtemp(dir=self.work)) / "change-run"
        calls = []
        module = campaign.change_run()
        before = before or {"lon-edge-1": 1, "lon-edge-2": 2, "lon-dist-a": 3}
        after = after or {"lon-edge-1": 11, "lon-edge-2": 12, "lon-dist-a": 3}

        def node_docs(boots):
            return {name: {"node_id": name, "process": {"boot_id": boot}}
                    for name, boot in boots.items()}

        def run(argv, **kwargs):
            calls.append(argv)
            kind = "null" if "--dry" in argv or argv[argv.index("--") + 1] == "sleep" \
                else "change"
            scenario = root / argv[argv.index("--name") + 1]
            directory = scenario / f"{kind}-{len(list(scenario.glob(kind + '-*'))) + 1:02d}"
            directory.mkdir(parents=True)
            passed = cw_pass if kind == "change" else True
            result = {"pass": passed, "missing": 0 if passed else 3, "gap_ms": 2.1,
                      "gap_bar_ms": 2.4, "p99_ms": 1.5, "p99_bar_ms": 1.6,
                      "null_fail_rate": 0.07, "null_windows": null_windows, "window_s": 40.0}
            (directory / "run.json").write_text(json.dumps({
                "directory": str(directory), "kind": kind, "layout": "cpus=2-15",
                "status": "pass" if passed and (kind == "null" or command_exit == 0) else "fail",
                "command_exit": command_exit if kind == "change" else 0,
                "reasons": [], "labels": {"roll.edges": {
                    "lon-readers-opus-1-1": result, "new-readers-pcm-1-1": result}}}))
            (directory / "nodes-before.json").write_text(json.dumps(node_docs(before)))
            (directory / "nodes-after.json").write_text(json.dumps(
                node_docs(after if kind == "change" else before)))
            return subprocess.CompletedProcess(argv, 0 if passed else 1)

        class Ctx:
            def __init__(self, directory):
                self.scenario = campaign.core.REGISTRY[ctx_name]
                self.directory = directory
                self.checks = []
                self.measurements = {}
                self.deferred = []

            def event(self, text):
                pass

            def check(self, what, ok, detail=""):
                self.checks.append((what, bool(ok)))
                return bool(ok)

            def measure(self, key, value):
                self.measurements[key] = value

            def defer(self, function):
                self.deferred.append(function)

        ctx = Ctx(root.parent / "evidence")
        ctx.directory.mkdir()
        with mock.patch.object(module, "out_root", lambda: root), \
                mock.patch.object(campaign.subprocess, "run", run), \
                mock.patch.object(campaign.lab, "stop_sources", lambda: None), \
                mock.patch.object(campaign.lab, "edges", lambda site="london":
                                  ["lon-edge-1", "lon-edge-2"]), \
                mock.patch.object(campaign.lab, "held_slots", lambda: set()):
            campaign.core.REGISTRY[ctx_name].function(ctx)
        return ctx, calls

    def test_a_change_scenario_runs_its_null_then_its_change_through_change_run(self):
        ctx, calls = self.change_runs("roll-edges")
        self.assertEqual(len(calls), 2)
        null, change = calls
        for argv in calls:
            self.assertEqual(argv[1], str(campaign.CHANGE_RUN))
            self.assertEqual(argv[2:6], ["run", "--name", "roll-edges", "--no-up"])
            self.assertIn("--lanes", argv)
            self.assertEqual(argv[argv.index("--lanes") + 1], "opus-1,pcm-1")
        # The engine's null: the same command with --dry (change-run.py
        # appends --dry-marks); then the change itself.
        self.assertEqual(null[null.index("--") - 1], "--dry")
        self.assertEqual(null[null.index("--") + 1:], campaign.ROLL_EDGES)
        self.assertNotIn("--dry", change)
        self.assertEqual(change[change.index("--") + 1:], campaign.ROLL_EDGES)
        self.assertEqual(ctx.checks and all(ok for _what, ok in ctx.checks), True, ctx.checks)
        names = [what for what, _ok in ctx.checks]
        self.assertIn("null-01: pass by the referee's rule", names)
        self.assertIn("change-01: roll.edges on lon-readers-opus-1-1", names)
        self.assertIn("change-01: lon-edge-1, lon-edge-2 restarted (new boot id)", names)
        self.assertEqual(ctx.measurements["cw_change-01"]["roll.edges new-readers-pcm-1-1"][
            "null_fail_rate"], 0.07)
        # Where CW could not score each null window against 120 others (a
        # 40 s window needs 203 of them), its rate is not measured, never 0.
        ctx, _calls = self.change_runs("roll-edges", null_windows=202)
        figure = ctx.measurements["cw_change-01"]["roll.edges new-readers-pcm-1-1"]
        self.assertIsNone(figure["null_fail_rate"])
        self.assertIn("not measured", figure["null_fail_rate_unmeasured"])
        # The campaign's own sources come back after the runs.
        self.assertEqual(len(ctx.deferred), 1)
        # A plain kind's null is `sleep` under the scenario's name.
        _ctx, calls = self.change_runs("forwarder-kill-return", after={
            "lon-edge-1": 1, "lon-edge-2": 2, "lon-dist-a": 3, "new-dist-a": 9},
            before={"lon-edge-1": 1, "lon-edge-2": 2, "lon-dist-a": 3, "new-dist-a": 4})
        self.assertEqual(calls[0][calls[0].index("--") + 1:], campaign.CRASH_NULL)
        self.assertEqual(calls[1][calls[1].index("--") + 1:], campaign.FORWARDER_CRASH)
        _ctx, calls = self.change_runs("roll-forwarders-client-loss")
        self.assertEqual(calls[1][calls[1].index("--client-loss") + 1], "loss 1%")

    def test_each_change_scenario_s_probes_hold_its_command_s_bound(self):
        """Review of CL5: change-run.py's default 360 s leaves the referee
        under its 120 no-change windows for any change over about 90 s, so
        every scenario's --seconds comes from its command's bound (the
        engine's timeouts it is given), and the null runs as long."""
        def null_windows(seconds, bound):
            # change-window.py's windows after a change at --at of `bound`
            # seconds: its length + 7 s, clear of it and its 1 s tail.
            return math.floor(seconds - campaign.CHANGE_AT_S - bound - 5 - 1 - (bound + 7) - 1)

        roles = {"lon-edge-1": "edge", "lon-edge-2": "edge", "lon-dist-a": "distributor",
                 "contrib-lon": "contributor"}
        for name in ("roll-edges", "upgrade-all", "replace-forwarder", "scale-up",
                     "autoscale-edges-down", "forwarder-kill-return-wan-loss"):
            with mock.patch.object(campaign.lab, "build_sets", lambda: {"a": {}, "b": {}}), \
                    mock.patch.object(campaign.lab, "standing_services", lambda: list(roles)), \
                    mock.patch.object(campaign.lab, "node", lambda node: {"role": roles[node]}), \
                    mock.patch.object(campaign.lab, "terminate_service", lambda node: None), \
                    mock.patch.object(campaign.lab, "set_slot_in_service", lambda node, on: None), \
                    mock.patch.object(campaign.lab, "service_pid", lambda node: 1):
                ctx, calls = self.change_runs(name)
            bound = ctx.measurements["change_bound_s"]
            for argv in calls:
                seconds = int(argv[argv.index("--seconds") + 1])
                self.assertEqual(argv[argv.index("--at") + 1], str(campaign.CHANGE_AT_S))
                self.assertEqual(seconds, ctx.measurements["probe_seconds"])
                self.assertGreaterEqual(seconds, 360, name)
                self.assertGreaterEqual(null_windows(seconds, bound), 120, name)
        # The bound of a roll: 70 s a node with the timeouts it is given,
        # and the surge edge a held edge slot stands for.
        ctx, calls = self.change_runs("roll-edges")
        self.assertEqual(ctx.measurements["change_bound_s"], 2 * campaign.CHANGE_NODE_S)
        self.assertEqual(calls[1][-6:], campaign.CHANGE_TIMEOUTS)
        self.assertEqual(campaign.CHANGE_NODE_S, 70.0)
        with mock.patch.object(campaign.lab, "held_slots", lambda: {"lon-edge-4", "lon-dist-c"}), \
                mock.patch.object(campaign.lab, "node", lambda name: {
                    "lon-edge-4": {"site": "london", "role": "edge"},
                    "lon-dist-c": {"site": "london", "role": "distributor"}}[name]):
            self.assertEqual(campaign.engine_bound_s(["lon-edge-1", "lon-edge-2"], ["london"]),
                             3 * campaign.CHANGE_NODE_S)
            self.assertEqual(campaign.engine_bound_s(["lon-dist-a"], []), campaign.CHANGE_NODE_S)
        # Four edges' roll: 280 s, far past what 360 s of probe can score.
        self.assertGreaterEqual(null_windows(campaign.probe_seconds(280), 280), 120)
        self.assertLess(null_windows(360, 280), 120)

    def test_a_change_scenario_fails_on_the_referee_s_verdict_and_on_restarts(self):
        ctx, _calls = self.change_runs("roll-edges", cw_pass=False)
        failed = [what for what, ok in ctx.checks if not ok]
        self.assertIn("change-01: pass by the referee's rule", failed)
        self.assertIn("change-01: roll.edges on lon-readers-opus-1-1", failed)
        self.assertNotIn("null-01: pass by the referee's rule", failed)
        # The roll restarted a node outside London's edges: a failure.
        ctx, _calls = self.change_runs("roll-edges", after={
            "lon-edge-1": 11, "lon-edge-2": 12, "lon-dist-a": 13})
        self.assertIn("change-01: no other node restarted",
                      [what for what, ok in ctx.checks if not ok])
        # An edge that kept its boot id was not rolled.
        ctx, _calls = self.change_runs("roll-edges", after={
            "lon-edge-1": 11, "lon-edge-2": 2, "lon-dist-a": 3})
        self.assertIn("change-01: lon-edge-1, lon-edge-2 restarted (new boot id)",
                      [what for what, ok in ctx.checks if not ok])
        # A scale-up restarts nothing; the slot that joined is not compared.
        with mock.patch.object(campaign.lab, "terminate_service", lambda name: None), \
                mock.patch.object(campaign.lab, "set_slot_in_service", lambda name, on: None):
            ctx, _calls = self.change_runs("scale-up", after={
                "lon-edge-1": 1, "lon-edge-2": 2, "lon-dist-a": 3, "lon-edge-4": 7})
        self.assertTrue(all(ok for _what, ok in ctx.checks), ctx.checks)
        self.assertEqual(len(ctx.deferred), 2)


if __name__ == "__main__":
    unittest.main()
