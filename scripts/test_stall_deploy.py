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
import hashlib
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
import test_scale_topology as scale  # noqa: E402


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

    def test_mesh_nodes_allow_receive_buffers_of_128_mib(self):
        """fix4-video and integration 4: av-mesh grows its video and
        cache-mesh sockets' receive buffers to two seconds of their busiest
        second, up to 128 MiB, so every mesh node renders
        net.core.rmem_max=134217728 in its kernel settings (above the
        bootstrap's 64 MiB); a settings file may still override it."""
        settings = self.settings()
        self.assertEqual(deploy.node_sysctls(settings)["net.core.rmem_max"], "134217728")
        sysctl = deploy.node_files(["x"], "unit", settings)["sysctl"]
        self.assertIn("net.core.rmem_max = 134217728\n", sysctl)
        self.assertEqual(deploy.node_sysctls({"node_sysctls": {"net.core.rmem_max": "268435456"}})
                         ["net.core.rmem_max"], "268435456")
        self.assertNotIn("net.core.rmem_max",
                         deploy.node_sysctls({"node_sysctls": {"net.core.rmem_max": ""}}))

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
        # The longest watch: the stop (20 s) and the check at its timeout
        # (70 + 50 s) end 10 s inside push's 150 s wait for an activation.
        check = deploy.check_unit_text(self.settings(["node_check_seconds=50"]))
        self.assertIn("TimeoutStartSec=120\n", check)
        for bad in ("node_check_seconds=51", "node_check_seconds=-1",
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
        # The time of each render (the contributors' path observation) aside.
        for name in args:
            if name not in overrides:
                self.assertEqual(deploy.render_sha256(args[name]),
                                 deploy.render_sha256(base[name]), name)
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


class OverridesShipped(scale.FakeLab):
    """Per-node overrides reach the files that push and a roll ship
    (LinodeOps.install, stage), not only render: the overridden node's unit
    and check unit are staged, the record holds that unit, and no other
    node gets them. On FakeHosts: nothing leaves the process."""

    OVERRIDES = {"lon-edge-1": {"node_check_seconds": 20,
                                "node_env": {"NEEDLETAIL_HOP_JOURNAL_CAPACITY": "2097152"}}}

    def files(self, name: str) -> dict:
        return self.hosts.fs(name)

    def assert_shipped(self, staged: bool) -> None:
        etc, unit = "/etc/needletail/needletail-mesh", "/etc/systemd/system/needletail-mesh.service"
        own = self.files("lon-edge-1")
        self.assertIn(f"{etc}.check.next", own)
        self.assertIn(b"--check-seconds 20 ", own[f"{etc}.check.next"])
        text = own[f"{etc}.unit.next" if staged else unit].decode()
        self.assertIn(f"\nWants={CHECK}\nAfter={CHECK}\n", text)
        self.assertIn("\nEnvironment=NEEDLETAIL_HOP_JOURNAL_CAPACITY=2097152\n", text)
        other = self.files("lon-edge-2")
        self.assertNotIn(f"{etc}.check.next", other)
        text = other[f"{etc}.unit.next" if staged else unit].decode()
        self.assertNotIn(CHECK, text)
        self.assertNotIn("NEEDLETAIL_HOP_JOURNAL_CAPACITY", text)

    def test_push_and_roll_ship_a_node_s_overrides_to_that_node_only(self):
        overrides = f"node_overrides={json.dumps(self.OVERRIDES)}"
        self.quietly(scale.deploy.command_push,
                     self.namespace(skip_bootstrap=True, set=[overrides]))
        self.assert_shipped(staged=False)
        # The record is of the unit that node runs, so the reproduce check
        # of a later roll holds.
        recorded = self.state()["deployed"]["nodes"]
        for name in ("lon-edge-1", "lon-edge-2"):
            unit = self.files(name)["/etc/systemd/system/needletail-mesh.service"]
            self.assertEqual(recorded[name]["unit_sha256"], hashlib.sha256(unit).hexdigest(), name)
        self.assertNotEqual(recorded["lon-edge-1"]["unit_sha256"],
                            recorded["lon-edge-2"]["unit_sha256"])
        # A roll stages the same files.
        for name in ("lon-edge-1", "lon-edge-2"):
            self.files(name).pop("/etc/needletail/needletail-mesh.check.next", None)
        self.set_root("av-mesh", scale.NEW)
        ops = self.ops()
        ops.stage(["lon-edge-1", "lon-edge-2"], ops.target("edges"))
        self.assert_shipped(staged=True)


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
        # A node with no known host is listed; one of a pair is a conflict,
        # since its pair cannot be judged; a lone one (Sydney's one edge, a
        # load host) is not.
        fine = {"contrib-lon-b": "h9", "tok-edge-2": "t4"}
        check = lab_tool.host_check(self.nodes({**fine, "syd-edge-1": None,
                                                "lon-load-1": None}))
        self.assertEqual((check["conflicts"], check["unknown"]),
                         ([], ["lon-load-1", "syd-edge-1"]))
        check = lab_tool.host_check(self.nodes({**fine, "lon-dist-b": None,
                                                "tok-edge-2": None}))
        self.assertEqual(check["unknown"], ["lon-dist-b", "tok-edge-2"])
        self.assertEqual(sorted(check["conflicts"]), [
            "HOST UNKNOWN in ap-northeast: tok-edge-2; their pair cannot be judged",
            "HOST UNKNOWN in gb-lon: lon-dist-b; their pair cannot be judged"])

    def command(self, hosts: dict, json_out: bool, meanwhile=None) -> tuple[int, str, dict]:
        """`hosts` on a fake API; `meanwhile(path)` runs after the first GET,
        as another command that writes the state during the GETs would."""
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
                if meanwhile is not None and len(self.calls) == 1:
                    meanwhile(path)
                return {"host_uuid": live[int(api_path.rsplit("/", 1)[1])]}

        out = io.StringIO()
        code = 0
        with mock.patch.object(lab_tool, "Api", FakeApi), redirect_stdout(out):
            try:
                lab_tool.command_hosts(argparse.Namespace(state=str(path), json=json_out))
            except SystemExit as exit_:
                code = exit_.code
        self.assertTrue(all(method == "GET" for method, _path in FakeApi.calls))
        return code, out.getvalue(), json.loads(path.read_text()) if path.is_file() else None

    def test_hosts_keeps_what_another_command_wrote_during_its_reads(self):
        """A push's record, a grow and a replace written while `hosts` reads
        the API are kept; a node that went is not brought back, and a node
        rebuilt as another instance is not given the old one's host."""
        def meanwhile(path):
            state = json.loads(path.read_text())
            state["deployed"] = {"nodes": {"lon-edge-1": {"build": "b"}}}
            state["nodes"]["lon-edge-9"] = {"name": "lon-edge-9", "id": 999, "role": "edge",
                                            "region": "gb-lon"}
            del state["nodes"]["lon-load-1"]
            state["nodes"]["lon-edge-3"]["id"] = 555
            path.write_text(json.dumps(state))

        fine = {"contrib-lon-b": "h9", "tok-edge-2": "t4"}
        _code, _text, state = self.command(fine, json_out=True, meanwhile=meanwhile)
        self.assertEqual(state["deployed"], {"nodes": {"lon-edge-1": {"build": "b"}}})
        self.assertNotIn("lon-load-1", state["nodes"])
        self.assertNotIn("host_uuid", state["nodes"]["lon-edge-9"])
        self.assertNotIn("host_uuid", state["nodes"]["lon-edge-3"])
        self.assertEqual(state["nodes"]["lon-edge-1"]["host_uuid"], "h4")

    def test_hosts_writes_under_the_lab_tool_s_state_lock(self):
        """At the CT3 merge (D4 fix report): the one write goes through
        change_state, which holds `<state>.lock` for its read-change-write;
        a lab whose state became another run's during the reads is not
        written."""
        real = lab_tool.change_state
        locked = []

        def change_state(path, state, change, creating=False):
            locked.append(pathlib.Path(path).name)
            return real(path, state, change, creating)

        with mock.patch.object(lab_tool, "change_state", change_state):
            _code, _text, state = self.command({}, json_out=True)
            self.assertEqual(locked, ["lab.json"])
            self.assertTrue(all(node.get("host_uuid") for node in state["nodes"].values()))

            def another_run(path):
                document = json.loads(path.read_text())
                document["run_id"] = "t2"
                path.write_text(json.dumps(document))

            code, _text, state = self.command({}, json_out=True, meanwhile=another_run)
        self.assertIn("taken down", str(code))
        self.assertFalse(any(node.get("host_uuid") for node in state["nodes"].values()))

    def test_hosts_does_not_make_again_a_lab_taken_down_during_its_reads(self):
        code, text, state = self.command({}, json_out=False,
                                         meanwhile=lambda path: path.unlink())
        self.assertIn("taken down", str(code))
        self.assertIsNone(state)
        self.assertEqual(text, "")

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
