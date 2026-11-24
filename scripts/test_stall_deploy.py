#!/usr/bin/env python3
"""Tests of the deploy settings for the stall instruments (D 2.9, D4): a
finite LimitMEMLOCK on both services; --mlock in place of --mlock-all; the
check at boot (its unit, the services' Wants=, --node-warm-file) only
when set; per-node overrides that reach their node only; --host-tag from
the lab state's host_uuid, only to a binary that has it; needletail-nodecheck
on every role; and the lab tool's `hosts`, which flags contributors on one
host and a region whose edges sit on one host. Offline: no Linode call, no
token, nothing started.
python3 -m unittest scripts/test_stall_deploy.py"""

from __future__ import annotations

import argparse
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


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_module("deploy_stall", "linode-small-mesh-deploy.py")
lab_tool = load_module("lab_tool_stall", "linode-small-mesh-lab.py")
COMPILER = deploy.common.ROOT / "target" / "release" / "needletail-compile"
ROUTES = {"best_direct_rtt_us": 74000, "primary_rtt_us": 74000, "primary_jitter_us": 1000,
          "secondary_rtt_us": 80000, "secondary_jitter_us": 1000}
CHECK = "needletail-node-check.service"


def value_of(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for target, name, value in ((deploy, "SETTINGS_FILE", self.dir / "settings.json"),
                                    (deploy, "HUNT_FILE", self.dir / "ecmp-ports.json"),
                                    (deploy.common, "ARTIFACT_DIR", self.dir)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        deploy.RENDER_BINARIES.clear()
        self.addCleanup(deploy.RENDER_BINARIES.clear)

    def settings(self, overrides=()) -> dict:
        (self.dir / "settings.json").unlink(missing_ok=True)
        with redirect_stderr(io.StringIO()):
            return deploy.load_settings(list(overrides))

    def render(self, overrides=(), state: dict | None = None) -> dict[str, list[str]]:
        if not COMPILER.is_file():
            self.skipTest("needs target/release/needletail-compile")
        settings = self.settings(overrides)
        state = state or lab.lab_state()
        plan = deploy.compile_plan(deploy.build_program(state, settings, ROUTES))
        return {name: item["args"] for name, item in
                deploy.render(state, settings, plan).items()}

    def fake_binary(self, name: str, flags: str) -> pathlib.Path:
        path = self.dir / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!/bin/sh\necho \"{flags}\"\n")
        path.chmod(0o755)
        return path


class Units(Base):
    def test_unit_has_a_finite_memlock(self):
        """D 2.3: LimitMEMLOCK=512M on both services, always: --mlock rings
        locks its rings under it, and nothing locks the whole process."""
        for overrides in ((), ("mlock=off",), ("mlock=rings",)):
            settings = self.settings(overrides)
            for service in ("needletail-mesh", "needletail-contrib"):
                unit = deploy.unit_text(service, "info", settings)
                self.assertIn("\nLimitMEMLOCK=512M\n", unit, (service, overrides))
                self.assertNotIn("infinity", unit)
        self.assertNotIn("mlock_all", deploy.DEFAULT_SETTINGS)
        self.assertIn("mlock", deploy.ROLLABLE_SETTINGS)
        self.assertNotIn("mlock_all", deploy.ROLLABLE_SETTINGS)
        with self.assertRaises(SystemExit):
            self.settings(["mlock=all"])

    def test_check_unit_only_when_set(self):
        """D 2.4: no check unit, no Wants= and no --node-warm-file by
        default (no placebo); node_check_seconds or node_warm render all
        three; a watch longer than push waits for is refused. A check that
        fails never keeps the service down: the service Wants= it (not
        Requires=), and any exit of its ExecStart (`-`) counts as done."""
        settings = self.settings()
        self.assertIsNone(deploy.check_unit_text(settings))
        self.assertNotIn(CHECK, deploy.unit_text("needletail-mesh", "info", settings))
        self.assertNotIn("check", deploy.node_files(["x"], "unit", settings))
        args = self.render()
        for name, node_args in args.items():
            self.assertNotIn("--node-warm-file", node_args, name)

        settings = self.settings(["node_check_seconds=30"])
        for service in ("needletail-mesh", "needletail-contrib"):
            unit = deploy.unit_text(service, "info", settings)
            self.assertIn(f"\nWants={CHECK}\nAfter={CHECK}\n", unit, service)
            self.assertNotIn("Requires=", unit, service)
        check = deploy.check_unit_text(settings)
        self.assertIn("Type=oneshot\nRemainAfterExit=yes\n", check)
        self.assertIn("RuntimeDirectory=needletail\nRuntimeDirectoryPreserve=yes\n", check)
        self.assertIn("TimeoutStartSec=100\n", check)
        self.assertIn("\nExecStart=-/usr/local/bin/needletail-nodecheck boot ", check)
        self.assertIn("needletail-nodecheck boot --out /run/needletail/node-warm.json \\\n"
                      "  --check-seconds 30 --max-stop-ms 20 --max-steal-permille 10\n", check)
        self.assertNotIn("--warm", check)
        self.assertEqual(deploy.node_files(["x"], "unit", settings)["check"], check)
        args = self.render(["node_check_seconds=30"])
        for name, node_args in args.items():
            if name.startswith("contrib"):
                self.assertNotIn("--node-warm-file", node_args, name)
            else:
                self.assertEqual(value_of(node_args, "--node-warm-file"),
                                 "/run/needletail/node-warm.json", name)

        check = deploy.check_unit_text(self.settings(["node_warm=true"]))
        self.assertIn("node-warm.json --warm \\\n  --check-seconds 0 ", check)
        self.assertIn("TimeoutStartSec=70\n", check)
        for bad in ("node_check_seconds=61", "node_check_seconds=-1",
                    "node_check_max_stop_ms=x", "node_warm=False", "node_warm=1",
                    "node_check_seconds=true", "stall_tick_us=false"):
            with self.subTest(bad), self.assertRaises(SystemExit):
                self.settings([bad])

    def test_nodecheck_is_installed_on_every_role(self):
        nodes = lab.lab_state()["nodes"].values()
        for role in sorted({node["role"] for node in nodes} | {"load"}):
            names = [path.name for path in deploy.binaries_for(role)]
            self.assertIn("needletail-nodecheck", names, role)
            self.assertNotEqual(names[0], "needletail-nodecheck", role)


class Render(Base):
    def test_mlock_replaces_mlock_all(self):
        args = self.render()
        for name, node_args in args.items():
            self.assertNotIn("--mlock", node_args, name)
            self.assertNotIn("--mlock-all", node_args, name)
        args = self.render(["mlock=off", "stall_tick_us=0", "stall_journal_capacity=65536"])
        for name, node_args in args.items():
            self.assertEqual(value_of(node_args, "--mlock"), "off", name)
            if name.startswith("contrib"):
                self.assertNotIn("--stall-tick-us", node_args)
                continue
            self.assertEqual(value_of(node_args, "--stall-tick-us"), "0", name)
            self.assertEqual(value_of(node_args, "--stall-journal-capacity"), "65536", name)
        # An extra argument wins over the rendered value; --mlock-all is a
        # flag that av-mesh no longer has.
        args = self.render(["mlock=off", 'extra_edge_args=["--mlock","rings"]'])
        self.assertEqual(args["lon-edge-1"].count("--mlock"), 1)
        self.assertEqual(value_of(args["lon-edge-1"], "--mlock"), "rings")
        with self.assertRaises(ValueError) as caught:
            self.render(['extra_edge_args=["--mlock-all","true"]'])
        self.assertIn("--mlock-all", str(caught.exception))

    def test_a_setting_given_to_a_binary_without_its_flag_is_refused(self):
        deploy.RENDER_BINARIES.update({
            "av-mesh": self.fake_binary("av-mesh", "--prefault --malloc-tune"),
            "av-contrib": self.fake_binary("av-contrib", "--mlock")})
        self.render()
        with self.assertRaises(ValueError) as caught:
            self.render(["mlock=rings"])
        self.assertIn("needs av-mesh --mlock", str(caught.exception))
        with self.assertRaises(ValueError):
            self.render(["stall_journal_capacity=65536"])
        # The check's record is rendered only to a binary that reads it.
        args = self.render(["node_check_seconds=10"])
        for name, node_args in args.items():
            self.assertNotIn("--node-warm-file", node_args, name)

    def test_node_override_reaches_only_its_node(self):
        overrides = {"lon-edge-1": {"mlock": "off", "stall_journal_capacity": 65536,
                                    "node_check_seconds": 20, "prefault": False},
                     "contrib-lon": {"mlock": "off"}}
        base = self.render()
        args = self.render([f"node_overrides={json.dumps(overrides)}"])
        edge = args["lon-edge-1"]
        self.assertEqual(value_of(edge, "--mlock"), "off")
        self.assertEqual(value_of(edge, "--stall-journal-capacity"), "65536")
        self.assertEqual(value_of(edge, "--prefault"), "false")
        self.assertEqual(value_of(edge, "--node-warm-file"), "/run/needletail/node-warm.json")
        self.assertEqual(value_of(args["contrib-lon"], "--mlock"), "off")
        for name in args:
            if name not in overrides:
                self.assertEqual(args[name], base[name], name)
        settings = self.settings([f"node_overrides={json.dumps(overrides)}"])
        own = deploy.node_own_settings(settings, "lon-edge-1")
        self.assertIn(f"Wants={CHECK}", deploy.unit_text("needletail-mesh", "info", own))
        self.assertIn("check", deploy.node_files(["x"], "unit", own))
        other = deploy.node_own_settings(settings, "lon-edge-2")
        self.assertNotIn(CHECK, deploy.unit_text("needletail-mesh", "info", other))
        self.assertNotIn("check", deploy.node_files(["x"], "unit", other))
        # Only a setting of one node, and only a node of the lab.
        for bad in ({"lon-edge-1": {"class_port_offset": 2000}},
                    {"lon-edge-1": {"mlock": "all"}},
                    {"lon-edge-1": {"node_check_seconds": 600}},
                    {"lon-edge-1": {"node_warm": "yes"}},
                    {"lon-edge-1": "mlock"}):
            with self.subTest(bad), self.assertRaises(SystemExit):
                self.settings([f"node_overrides={json.dumps(bad)}"])
        with self.assertRaises(ValueError) as caught:
            self.render(['node_overrides={"lon-edge-99": {"mlock": "off"}}'])
        self.assertIn("lon-edge-99", str(caught.exception))

    def test_host_tag_from_host_uuid(self):
        """B's seam (D 5): an av-mesh node whose host the lab state knows
        is told it, the first 12 hex digits of host_uuid; none else."""
        state = lab.lab_state()
        state["nodes"]["lon-edge-1"]["host_uuid"] = "3A3DDD59-D9A7-8BB8-DE04-1391075DF44D"
        state["nodes"]["lon-dist-a"]["host_uuid"] = "0123456789abcdef0123"
        state["nodes"]["contrib-lon"]["host_uuid"] = "fedcba9876543210"
        args = self.render(state=state)
        self.assertEqual(value_of(args["lon-edge-1"], "--host-tag"), "3a3ddd59d9a7")
        self.assertEqual(value_of(args["lon-dist-a"], "--host-tag"), "0123456789ab")
        self.assertNotIn("--host-tag", args["contrib-lon"])
        self.assertNotIn("--host-tag", args["lon-edge-2"])
        self.assertIsNone(deploy.host_tag({"host_uuid": None}))
        # An av-mesh without the flag is not given it.
        deploy.RENDER_BINARIES["av-mesh"] = self.fake_binary("av-mesh", "--prefault")
        args = self.render(state=state)
        self.assertNotIn("--host-tag", args["lon-edge-1"])


class Hosts(unittest.TestCase):
    """The lab tool's `hosts` (D 2.10) against a fake API: no token, no
    request leaves the process."""

    NODES = {
        "contrib-lon": ("contributor", "gb-lon", "h1"),
        "contrib-lon-b": ("contributor", "gb-lon", "h1"),
        "lon-dist-a": ("distributor", "gb-lon", "h2"),
        "lon-dist-b": ("distributor", "gb-lon", "h3"),
        "lon-edge-1": ("edge", "gb-lon", "h4"),
        "lon-edge-2": ("edge", "gb-lon", "h4"),
        "lon-edge-3": ("edge", "gb-lon", "h5"),
        "tok-dist-a": ("distributor", "ap-northeast", "t1"),
        "tok-dist-b": ("distributor", "ap-northeast", "t2"),
        "tok-edge-1": ("edge", "ap-northeast", "t3"),
        "tok-edge-2": ("edge", "ap-northeast", "t3"),
        "syd-edge-1": ("edge", "ap-southeast", "s1"),
        "lon-load-1": ("load", "gb-lon", "h1"),
    }

    def nodes(self, hosts: dict | None = None) -> dict:
        out = {}
        for index, (name, (role, region, host)) in enumerate(sorted(self.NODES.items())):
            out[name] = {"name": name, "id": 100 + index, "role": role, "region": region,
                         "host_uuid": (hosts or {}).get(name, host)}
        return out

    def test_hosts_flags_contributors_and_a_region_with_one_edge_host(self):
        check = lab_tool.host_check(self.nodes())
        self.assertEqual(len(check["conflicts"]), 2, check["conflicts"])
        self.assertIn("contributors contrib-lon, contrib-lon-b", check["conflicts"][0])
        self.assertIn("ONE HOST for the edges of ap-northeast: tok-edge-1, tok-edge-2",
                      check["conflicts"][1])
        # London's edges are on two hosts: the pair that shares one is
        # listed, not a conflict. Sydney's one edge backs up nothing.
        self.assertEqual(check["shared_edges"],
                         ["SHARED HOST edges in gb-lon: lon-edge-1, lon-edge-2 on h4"])
        self.assertEqual(check["unknown"], [])
        # The distributors' rule holds as before.
        check = lab_tool.host_check(self.nodes({"lon-dist-b": "h2", "contrib-lon-b": "h9",
                                                "tok-edge-2": "t4"}))
        self.assertEqual(len(check["conflicts"]), 1)
        self.assertIn("distributors and backbones lon-dist-a, lon-dist-b", check["conflicts"][0])
        # A node with no known host is listed and judged by nothing.
        check = lab_tool.host_check(self.nodes({"contrib-lon-b": None, "tok-edge-2": None}))
        self.assertEqual((check["conflicts"], check["unknown"]),
                         ([], ["contrib-lon-b", "tok-edge-2"]))

    def command(self, hosts: dict, json_out: bool) -> tuple[int, str, dict]:
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "lab.json"
        nodes = self.nodes()
        for node in nodes.values():
            node.pop("host_uuid")
        path.write_text(json.dumps({"run_id": "t1", "nodes": nodes}))
        live = {node["id"]: hosts.get(name, self.NODES[name][2])
                for name, node in nodes.items()}

        class FakeApi:
            calls = []

            def call(self, method, api_path, body=None, allow_404=False):
                self.calls.append((method, api_path))
                assert method == "GET", method
                return {"host_uuid": live[int(api_path.rsplit("/", 1)[1])]}

        out = io.StringIO()
        code = 0
        with mock.patch.object(lab_tool, "Api", FakeApi), redirect_stdout(out):
            try:
                lab_tool.command_hosts(argparse.Namespace(state=str(path), json=json_out))
            except SystemExit as exit_:
                code = exit_.code
        self.assertTrue(all(method == "GET" for method, _path in FakeApi.calls))
        return code, out.getvalue(), json.loads(path.read_text())

    def test_hosts_records_host_uuid_and_exits_1_on_a_conflict(self):
        code, text, state = self.command({}, json_out=False)
        self.assertEqual(code, 1)
        self.assertIn("SHARED HOST in gb-lon: contributors", text)
        self.assertIn("SHARED HOST edges in gb-lon", text)
        self.assertEqual(state["nodes"]["lon-edge-1"]["host_uuid"], "h4")
        code, text, _state = self.command({"contrib-lon-b": "h9", "tok-edge-2": "t4"},
                                          json_out=True)
        self.assertEqual(code, 0)
        document = json.loads(text)
        self.assertEqual(document["conflicts"], [])
        self.assertEqual(document["nodes"]["tok-edge-2"],
                         {"region": "ap-northeast", "role": "edge", "host_uuid": "t4"})
        self.assertEqual(document["regions"]["ap-northeast"]["t4"], ["tok-edge-2"])


if __name__ == "__main__":
    unittest.main()
