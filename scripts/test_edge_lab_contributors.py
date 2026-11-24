#!/usr/bin/env python3
"""Tests of the local lab's second contributor and source host (A NT4),
offline (no namespace, no service started): the rows contrib-lon-b (the
standby) and lon-src (a load host of purpose source, on the London VLAN);
the deploy renders the standby beside the origin and nothing on lon-src;
the sources run in lon-src, on its own CPUs, and send every AEP1 epoch to
both contributors; the verbs kill, return and freeze run workstream A's
changes (scripts/contributor_changes.py) on EdgeLabOps, whose backend
methods use the lab's own primitives.
python3 -m unittest scripts/test_edge_lab_contributors.py"""

from __future__ import annotations

import io
import json
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import contributor_changes  # noqa: E402
import edge_lab as lab  # noqa: E402
import small_mesh_common  # noqa: E402
from test_contributor_changes import SAME, ContribSim  # noqa: E402
from test_mesh_ops import MARK, Clock  # noqa: E402

COMPILER = HERE.parent / "target" / "release" / "needletail-compile"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, value in (("WORK", self.tmp / "lab"), ("REGISTRY", self.tmp / "labs")):
            patcher = mock.patch.object(lab, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.tmp / "lab").mkdir()


class Rows(Base):
    def test_the_standby_and_the_source_host(self):
        standby = lab.node("contrib-lon-b")
        self.assertEqual((standby["role"], standby["site"], standby["public_ipv4"],
                          standby["vlan_ipv4"], standby.get("standby")),
                         ("contributor", "london", "10.90.0.7", "10.77.1.7", True))
        self.assertNotIn("standby", lab.node("contrib-lon"))
        source = lab.node("lon-src")
        self.assertEqual((source["role"], source["public_ipv4"], source["vlan_ipv4"]),
                         ("load", "10.90.0.4", "10.77.1.4"))
        self.assertEqual(small_mesh_common.load_purpose(source), "source")
        state = lab.lab_state()
        self.assertEqual([node["name"] for node in small_mesh_common.contributors(state)],
                         ["contrib-lon", "contrib-lon-b"])
        self.assertEqual(lab.contributor_targets(), ["10.77.1.5:27100", "10.77.1.7:27100"])
        self.assertEqual(lab.names("contributor"), ["contrib-lon", "contrib-lon-b"])
        # Every address of the lab is its own.
        addresses = [address for _name, _role, _site, _region, public, vlan in lab.NODES
                     for address in (public, vlan) if address]
        self.assertEqual(len(addresses), len(set(addresses)))

    def test_the_range_layout_gives_both_rows_a_window(self):
        standing = [name for name, role, *_ in lab.NODES
                    if role != "backbone" and name not in lab.DEFAULT_SLOTS]
        sets = lab.node_cpu_sets(standing, cpus=(2, 15))
        self.assertEqual(len(sets["contrib-lon-b"]), 4)
        self.assertEqual(len(sets["lon-src"]), 2)
        self.assertFalse(set(sets["lon-src"]) & set(sets["_rest"]),
                         "the source runs on node CPUs, not the readers'")

    def test_pinned_limits(self):
        sets = {"_first": 0, "contrib-lon": [0, 1], "contrib-lon-b": [2, 3]}
        limits = lab.pinned_limits(sets, {"contrib-lon-b": {"cpu_percent": 100}})
        self.assertEqual(limits, {"contrib-lon": {"cpu_percent": 200},
                                  "contrib-lon-b": {"cpu_percent": 100}})
        self.assertEqual(lab.pinned_limits({"_range": [2, 15]}, {}), {})


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Render(Base):
    def test_the_standby_is_rendered_beside_the_origin_and_lon_src_runs_nothing(self):
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
        with redirect_stderr(io.StringIO()):
            rendered = deploy.render(state, settings, plan)
        self.assertNotIn("lon-src", rendered)
        self.assertEqual(rendered["contrib-lon-b"]["service"], "needletail-contrib")
        args = rendered["contrib-lon-b"]["args"]
        value = lambda flag, items=args: items[items.index(flag) + 1]  # noqa: E731
        self.assertEqual(value("--relay-local-id"), "contrib-b")
        for forwarder in ("lon-dist-a", "lon-dist-b"):
            self.assertEqual(value("--relay-secondary-id", rendered[forwarder]["args"]),
                             "contrib-b", forwarder)


class Sources(Base):
    def run_sources(self, audio):
        lab.save("settings.json", {"pcm_epoch_samples": 48, "opus_epoch_samples": 48})
        lab.save("cpus.json", {"_first": 0, "lon-src": [26, 27], "_rest": [28, 29]})
        started = []

        def popen(command, **kwargs):
            started.append(command)
            return types.SimpleNamespace(pid=1000 + len(started))

        with mock.patch.object(lab, "ns_pid", lambda name: {"lon-src": 4}[name]), \
                mock.patch.object(lab, "join_tools", lambda: None), \
                mock.patch.object(lab, "binary", lambda label, build="a": f"/bin/{label}"), \
                mock.patch.object(lab, "daw_tracks", lambda: pathlib.Path("/tracks")), \
                mock.patch.object(lab.subprocess, "Popen", popen):
            lab.start_sources(60, video=True, audio=audio)
        return started

    def test_the_sources_run_in_lon_src_to_both_contributors(self):
        video, probe = self.run_sources("probe")
        for command in (video, probe):
            self.assertEqual(command[:9], ["nsenter", "--preserve-credentials", "-U", "-n",
                                           "-t", "4", "taskset", "-c", "26,27"])
        self.assertIn("srt://10.77.1.5:27001?mode=caller&transtype=live&latency=80", video)
        targets = [probe[index + 1] for index, item in enumerate(probe) if item == "--target"]
        self.assertEqual(targets, ["10.77.1.5:27100", "10.77.1.7:27100"])
        self.assertEqual(lab.load("sources.json"), {"video": 1001, "audio": 1002})

    def test_the_daw_source_sends_to_both(self):
        _video, daw = self.run_sources("daw")
        self.assertIn("10.77.1.5:27100,10.77.1.7:27100", daw)


class Verbs(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        self.sim = ContribSim(self.clock, self.tmp)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def verb(self, *arguments) -> int:
        return lab.change_verb(arguments[0], list(arguments[1:]), ops=self.sim, out=self.out,
                               err=self.err, clock=self.clock, sleep=self.clock.sleep)

    def test_kill_return_and_freeze_take_the_node_first(self):
        self.assertEqual(self.verb("kill", "contrib-lon"), 0, self.err.getvalue())
        self.clock.sleep(5)
        self.assertEqual(self.verb("return", "contrib-lon"), 0, self.err.getvalue())
        self.assertEqual(self.verb("freeze", "contrib-lon-b", "--ms", "5000"), 0)
        lines = self.out.getvalue().splitlines()
        for line in lines:
            self.assertRegex(line, MARK)
        self.assertEqual([line.split()[2] for line in lines[::2]],
                         ["kill.contrib-lon", "return.contrib-lon", "freeze.contrib-lon-b"])

    def test_a_kill_with_one_contributor_publishing_is_refused(self):
        self.sim.hears["contrib-lon-b"] = False
        self.assertEqual(self.verb("kill", "contrib-lon", "--gate-timeout-s", "1"), 2)
        self.assertEqual(self.out.getvalue(), "")

    def test_the_verbs_are_reached_from_main(self):
        seen = []
        with mock.patch.object(lab, "change_verb",
                               lambda command, arguments: seen.append((command, arguments)) or 0), \
                mock.patch.object(sys, "argv", ["edge_lab.py", "kill", "contrib-lon",
                                                "--unprotected"]), \
                self.assertRaises(SystemExit) as code:
            lab.main()
        self.assertEqual(code.exception.code, 0)
        self.assertEqual(seen, [("kill", ["contrib-lon", "--unprotected"])])


class Backend(unittest.TestCase):
    """EdgeLabOps's methods for workstream A, on the lab's primitives."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []
        self.running = {"contrib-lon": None}
        fake = types.SimpleNamespace(
            WORK=self.tmp, LabError=lab.LabError,
            kill_service=lambda name: self.calls.append(("kill_service", name)),
            freeze_service=lambda name, frozen: self.calls.append(("freeze", name, frozen)),
            service_pid=lambda name: self.running.get(name),
            start_service=lambda name, **given: self.calls.append(("start", name, given)),
            load=lambda name, default=None: {"contrib-lon": {"limits": {"cpu_percent": 200},
                                                             "env": None,
                                                             "extra_args": ["--x"]}}
            if name == "services.json" else (default or {}),
            binary=lambda label, build="a": f"/lab/bin/{build}/{label}",
            service_args=lambda name, build: [f"/lab/bin/{build}/av-contrib", "--node-id", name])
        self.ops = lab.EdgeLabOps(lab=fake)

    def test_kill_release_and_freeze(self):
        self.ops.hold_down("contrib-lon")
        self.ops.kill("contrib-lon")
        self.ops.release("contrib-lon")
        self.running["contrib-lon"] = 77
        self.ops.release("contrib-lon")
        self.ops.freeze("contrib-lon", True)
        self.assertEqual(self.calls, [
            ("kill_service", "contrib-lon"),
            ("start", "contrib-lon", {"limits": {"cpu_percent": 200}, "env": None,
                                      "extra_args": ["--x"]}),
            ("freeze", "contrib-lon", True)])

    def test_the_staged_identity_is_the_staged_set_s(self):
        self.assertIsNone(self.ops.staged_identity("contrib-lon"))
        self.ops.staged["contrib-lon"] = "b"
        seen = []
        with mock.patch.object(contributor_changes, "identity_of",
                               lambda binary, args: seen.append((binary, args)) or SAME):
            self.assertEqual(self.ops.staged_identity("contrib-lon"), SAME)
        self.assertEqual(seen, [("/lab/bin/b/av-contrib",
                                 ["/lab/bin/b/av-contrib", "--node-id", "contrib-lon", "--x"])])


if __name__ == "__main__":
    unittest.main()
