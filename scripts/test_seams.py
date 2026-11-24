#!/usr/bin/env python3
"""Tests of the seams between the tools that integration 2 joined, each of
which fails when one side changes without the other. Offline: a fake Linode
API, a fake user systemd, ssh faked by a script on PATH that runs the
deploy tool's own node script here with journalctl and curl stubbed, and a
temporary archive root. No token, no lab, no service.

(a) The watchdog's collect (lab tool) is the deploy tool's real `collect`:
    its flags, the --timeout-s it is given, its exit codes, and what
    "complete" means; scale-run's end collect is the same command.
(b) Every firewall rules PUT in the tree is put_firewall_rules's.
(c) scale-run's contributor and ingress changes (`kill`, `return`) are the
    deploy tool's real commands, parsed by its own parser, with the
    timeouts their bound is made of, and their marks are read back.
(e) One lab, one run: up, scale-run, collect and down land in one
    ~/needletail-data/<run id>/ with one manifest.
python3 -m unittest scripts/test_seams.py"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_lab_slots as tls  # noqa: E402

lab_tool = tls.lab_tool
run_archive = tls.run_archive


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("deploy_seams", HERE / "linode-small-mesh-deploy.py")
scale = load("scale_run_seams", HERE / "compare" / "scale-run.py")

# ssh as the deploy tool calls it (`ssh <options> root@ADDRESS "bash -c
# '<script>'"`): an address in FAKE_SSH_DOWN does not answer; any other
# runs the script here, with journalctl and curl stubbed.
FAKE_SSH = """#!/bin/sh
host=
for argument; do
    last=$argument
    case $argument in root@*) host=${argument#root@} ;; esac
done
case " $FAKE_SSH_DOWN " in *" $host "*) echo "ssh: connect to host $host: no route" >&2; exit 255 ;; esac
PATH="$FAKE_STUBS:$PATH" exec sh -c "$last"
"""
JOURNALCTL = "#!/bin/sh\necho \"journal $*\"\n"
CURL = ("#!/bin/sh\nout=''\nwhile [ $# -gt 0 ]; do\n  case \"$1\" in -o) out=$2; shift;; esac\n"
        "  last=$1; shift\ndone\necho \"{\\\"url\\\": \\\"$last\\\"}\" >\"$out\"\nprintf 200\n")
SMALL = {"description": "four nodes in London", "image": "linode/debian13", "nodes": [
    {"name": "contrib-lon", "role": "contributor", "site": "london", "region": "gb-lon",
     "type": "g6-dedicated-4", "vlan_ipv4": "10.80.1.5/24"},
    {"name": "lon-dist-a", "role": "distributor", "site": "london", "region": "gb-lon",
     "type": "g6-dedicated-2", "vlan_ipv4": "10.80.1.10/24"},
    {"name": "lon-edge-1", "role": "edge", "site": "london", "region": "gb-lon",
     "type": "g6-dedicated-2", "vlan_ipv4": "10.80.1.20/24"},
    {"name": "lon-load-1", "role": "load", "site": "london", "region": "gb-lon",
     "type": "g6-dedicated-4", "vlan_ipv4": "10.80.1.30/24", "purpose": "listeners"}]}


class Seams(tls.Base):
    """The lab tool's test base (fake API and systemd, the archive in the
    test's directory), with ssh faked on PATH for the real deploy tool."""

    def setUp(self):
        super().setUp()
        self.systemd.active.clear()       # no lab's watchdog runs yet
        bin_dir = self.tmp / "bin"
        stubs = self.tmp / "stubs"
        bin_dir.mkdir()
        stubs.mkdir()
        for folder, name, text in ((bin_dir, "ssh", FAKE_SSH), (stubs, "journalctl", JOURNALCTL),
                                   (stubs, "curl", CURL)):
            (folder / name).write_text(text)
            (folder / name).chmod(0o755)
        # The deploy tool's own files for a lab live in the checkout's
        # target/; a lab of another run id there is not this lab's.
        patcher = mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_STUBS": str(stubs),
            "FAKE_SSH_DOWN": "", "NEEDLETAIL_LINODE_LAB_STATE": self.args.state,
            "NEEDLETAIL_DATA_ROOT": str(self.tmp / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The watchdog's collect runs the real collector in these tests.
        patcher = mock.patch.object(lab_tool, "run_collect", tls.RUN_COLLECT)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.topology = self.tmp / "topology-small.json"
        self.topology.write_text(json.dumps(SMALL))

    def up_small(self, api) -> dict:
        with mock.patch.object(lab_tool, "MAX_NODES", 10), \
                mock.patch.object(lab_tool, "command_status", lambda args: None):
            state = self.up(api, topology=str(self.topology), spent_before_usd=0.0,
                            watchdog_hours=2.0)
        # The deploy tool's addresses: one a node, distinct.
        for index, name in enumerate(sorted(state["nodes"])):
            state["nodes"][name]["public_ipv4"] = f"203.0.113.{20 + index}"
        # As the deploy tool's push leaves it (collect counts a lab without
        # its deployed record and settings as incomplete).
        state["deployed"] = {"settings": {"part_us": 1000}, "tiers": {}, "nodes": {}}
        pathlib.Path(self.args.state).write_text(json.dumps(state))
        return state

    def manifest(self, run_id: str = "t1") -> dict:
        return json.loads((self.tmp / "data" / run_id / "manifest.json").read_text())

    def down(self, api) -> str | None:
        args = argparse.Namespace(state=self.args.state, uncollected=False)
        with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                mock.patch.object(lab_tool, "delete_vlans", lambda api_, run_id: []), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                lab_tool.command_down_operator(args)
            except SystemExit as stop:
                return str(stop.code)
        return None


class WatchdogCollect(Seams):
    """(a) The command `up` records, run by the watchdog as it runs it,
    is the deploy tool's collect: it parses, it takes the --timeout-s the
    watchdog adds, its exit 0 is the archive's `collected.complete`, and its
    exit 1 (a node did not answer) is a failed run with the node named."""

    def watchdog(self, state: dict, bound_s: float = 60.0) -> str:
        until = lab_tool.time.time() + bound_s
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            lab_tool.watchdog_collect(pathlib.Path(self.args.state), state, until)
        return out.getvalue() + err.getvalue()

    def test_the_watchdog_s_collect_is_the_deploy_tool_s_and_completes_the_run(self):
        api = tls.FakeApi()
        state = self.up_small(api)
        self.assertEqual(state["collect"]["command"][1:],
                         [str(lab_tool.DEPLOY_TOOL), "collect", "--run-id", "t1"])
        said = self.watchdog(state)
        timeout = float(said.split("--timeout-s ")[1].split()[0])
        self.assertLess(timeout, 60.0)
        self.assertEqual(timeout, round(lab_tool.collect_pull_s(60.0), 1))
        self.assertIn("watchdog: run t1 is collected", said)
        manifest = self.manifest()
        self.assertTrue(manifest["collected"]["complete"], manifest["collected"])
        self.assertEqual(manifest["status"], "running")
        files = {entry["path"] for entry in manifest["files"]}
        for name in state["nodes"]:
            self.assertTrue(any(path.startswith(f"nodes/{name}/journal.") for path in files),
                            name)
        self.assertIsNone(run_archive.require_collected("t1"))
        # The deploy tool's parser takes exactly what the watchdog gives it.
        recorded = deploy.build_parser().parse_args(
            [*state["collect"]["command"][2:], "--timeout-s", f"{timeout:.1f}"])
        self.assertIs(recorded.handler, deploy.command_collect)
        self.assertEqual(recorded.timeout_s, timeout)

    def test_a_node_that_does_not_answer_fails_the_run_and_is_named(self):
        api = tls.FakeApi()
        state = self.up_small(api)
        os.environ["FAKE_SSH_DOWN"] = state["nodes"]["lon-edge-1"]["public_ipv4"]
        said = self.watchdog(state)
        self.assertIn("it exited with status 1", said)
        manifest = self.manifest()
        self.assertFalse(manifest["collected"]["complete"])
        self.assertIn("lon-edge-1", manifest["collected"]["missing"])
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("the watchdog's collect before its delete failed",
                      manifest["status_reason"])

    def test_scale_run_s_end_collect_is_the_watchdog_s_command(self):
        api = tls.FakeApi()
        state = self.up_small(api)
        self.assertEqual(scale.collect_command(state, 120.0),
                         [*state["collect"]["command"], "--timeout-s", "120"])
        # A lab made before `up` recorded its collect: the same command.
        older = {key: value for key, value in state.items() if key != "collect"}
        self.assertEqual(scale.collect_command(older, 120.0)[1:],
                         [str(scale.DEPLOY), "collect", "--run-id", "t1", "--timeout-s", "120"])
        self.assertEqual(scale.DEPLOY.resolve(), lab_tool.DEPLOY_TOOL.resolve())
        logged = []
        code = scale.collect_lab(state, argparse.Namespace(no_collect=False,
                                                           collect_timeout_s=60.0),
                                 logged.append)
        self.assertEqual(code, 0, logged)
        self.assertEqual([item["event"] for item in logged], ["collect_start", "collect_exit"])
        self.assertTrue(self.manifest()["collected"]["complete"])
        # Both run parsers take the options.
        parsed = scale.build_parser().parse_args(["party", "--no-collect"]) \
            if hasattr(scale, "build_parser") else None
        if parsed is not None:
            self.assertTrue(parsed.no_collect)


class OneRun(Seams):
    """(e) A lab's tools write one run: `up` opens it before anything is
    billed, scale-run and collect append under their own directories, the
    operator's `down` is refused until it is collected, then records the
    deletion and the bill and ends it."""

    def test_up_scale_run_collect_and_down_land_in_one_run(self):
        api = tls.FakeApi()
        state = self.up_small(api)
        manifest = self.manifest()
        self.assertEqual((manifest["kind"], manifest["tool"], manifest["status"]),
                         ("linode-lab", lab_tool.ARCHIVE_TOOL, "running"))
        self.assertEqual(manifest["regions"], ["gb-lon"])
        self.assertEqual(manifest["lab"]["run_tag"], "needletail-small-mesh-t1")
        self.assertEqual(len(manifest["lab"]["instances"]), 4)
        self.assertTrue(any(path.startswith("topology/") for path in
                            (entry["path"] for entry in manifest["files"])))
        # Down before the collect: refused, nothing deleted.
        refusal = self.down(api)
        self.assertIn("is not collected", str(refusal))
        self.assertTrue(pathlib.Path(self.args.state).is_file())
        # scale-run on the lab: its files under scale-run/<run>/.
        directory = self.tmp / "scale-r1"
        directory.mkdir()
        (directory / "plan.json").write_text("{}\n")
        logged = []
        with mock.patch.object(scale, "deploy_settings", return_value={}):
            archive = scale.open_archive(state, "r1", logged.append)
        scale.archive_step(archive, "r1", directory, {"listeners": 100, "sites": {
            "london": {"udp": 100, "hls": 0}}}, "opus-1", 2, logged.append)
        scale.archive_run(archive, "r1", directory, logged.append)
        self.assertEqual(scale.collect_lab(state, argparse.Namespace(
            no_collect=False, collect_timeout_s=60.0), logged.append), 0, logged)
        self.assertIsNone(self.down(api))
        self.assertEqual(sorted(path.name for path in (self.tmp / "data").iterdir()), ["t1"])
        manifest = self.manifest()
        self.assertEqual([item["tool"] for item in manifest["invocations"]],
                         [lab_tool.ARCHIVE_TOOL, scale.ARCHIVE_TOOL, deploy.COLLECT_TOOL])
        self.assertEqual(manifest["status"], "complete", manifest["status_reason"])
        self.assertTrue(manifest["collected"]["complete"])
        self.assertEqual(manifest["regions"], ["gb-lon"])
        self.assertIsNotNone(manifest["lab"]["deleted_at_utc"])
        removed = json.loads((self.tmp / "small-mesh-lab-t1.removed.json").read_text())
        self.assertEqual(manifest["lab"]["cost_usd"], removed["billed_usd"])
        self.assertTrue(all(item["hours_billed"] == 1 for item in manifest["lab"]["instances"]))
        files = {entry["path"] for entry in manifest["files"]}
        self.assertIn("scale-run/r1/plan.json", files)
        self.assertTrue(any(path.startswith("nodes/lon-dist-a/journal.") for path in files))
        self.assertTrue(any(path.startswith("lab/state.") for path in files))
        self.assertEqual(manifest["listener_steps"][0]["listeners_by_region"], {"london": 100})
        self.assertEqual(run_archive.RunArchive.open("t1", record=False).verify(), [])

    def test_a_run_id_that_has_ended_is_refused_before_anything_is_billed(self):
        run_archive.RunArchive.create("t1", "linode-lab", tool="other").finish("failed", "old")
        api = tls.FakeApi()
        with self.assertRaises(SystemExit) as caught, \
                mock.patch.object(lab_tool, "MAX_NODES", 10), \
                mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            lab_tool.command_up(self.up_args(topology=str(self.topology), spent_before_usd=0.0,
                                             watchdog_hours=2.0))
        self.assertIn("give another --run-id", str(caught.exception))
        self.assertFalse([call for call in api.calls if call[0] == "POST"])
        self.assertFalse(pathlib.Path(self.args.state).exists())

    def test_a_run_the_hunt_made_for_the_lab_is_appended_to_and_left_for_it_to_end(self):
        run_archive.RunArchive.create("t1", "linode-lab", tool="linode-stall-hunt.py")
        api = tls.FakeApi()
        self.up_small(api)
        manifest = self.manifest()
        self.assertEqual([item["tool"] for item in manifest["invocations"]],
                         ["linode-stall-hunt.py", lab_tool.ARCHIVE_TOOL])
        run_archive.RunArchive.open("t1", record=False).mark_collected(True)
        self.assertIsNone(self.down(api))
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "running", "the hunt ends its own run")
        self.assertIsNotNone(manifest["lab"]["deleted_at_utc"])


class FirewallWriters(unittest.TestCase):
    """(b) put_firewall_rules is the one way to put a lab's rules: no other
    function of any tool in scripts/ PUTs to /networking/firewalls/.../rules
    or creates a firewall but the lab tool's `up`."""

    @staticmethod
    def strings(node: ast.AST) -> str:
        return " ".join(part.value for part in ast.walk(node)
                        if isinstance(part, ast.Constant) and isinstance(part.value, str))

    def test_only_put_firewall_rules_puts_the_rules(self):
        found = []
        for path in sorted(HERE.rglob("*.py")):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(), str(path))
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for call in ast.walk(function):
                    if not isinstance(call, ast.Call) or len(call.args) < 2:
                        continue
                    method, target = call.args[0], call.args[1]
                    text = self.strings(target)
                    if not (isinstance(method, ast.Constant) and "/networking/firewalls" in text):
                        continue
                    if method.value == "PUT" or (method.value == "POST" and "/devices" not in text):
                        found.append((path.name, function.name, method.value))
        self.assertEqual(sorted(set(found)), [
            ("linode-small-mesh-lab.py", "create_lab", "POST"),
            ("linode-small-mesh-lab.py", "put_firewall_rules", "PUT")])

    def test_every_writer_of_the_rules_calls_it(self):
        """The writers lab2 names (create_lab's second put, add_nodes,
        remove_nodes, command_allow) and the autoscaler's adopt reach the
        rules through put_firewall_rules."""
        lab_tree = ast.parse((HERE / "linode-small-mesh-lab.py").read_text())
        autoscaler = ast.parse((HERE / "linode-autoscale.py").read_text())

        def callers(tree) -> set[str]:
            out = set()
            for function in ast.walk(tree):
                if isinstance(function, ast.FunctionDef):
                    for call in ast.walk(function):
                        if isinstance(call, ast.Call) and (
                                getattr(call.func, "id", None) == "put_firewall_rules"
                                or getattr(call.func, "attr", None) == "put_firewall_rules"):
                            out.add(function.name)
            return out

        self.assertTrue({"create_lab", "add_nodes", "remove_nodes", "command_allow"}
                        <= callers(lab_tree), callers(lab_tree))
        self.assertTrue(callers(autoscaler), "the autoscaler's adopt")


class ContributorChanges(unittest.TestCase):
    """(c) scale-run's `change` kind (C2's kill of contrib-lon and its owed
    return; an ingress the same) is the deploy tool's `kill` / `return`,
    parsed by the deploy tool's own parser into command_change with the
    timeouts that scale-run's bound for it is made of; the marks
    contributor_changes prints are the ones scale-run reads back."""

    def plan_changes(self) -> list[dict]:
        out = []
        for name in ("phases-proving.json", "phases-party.json"):
            spec = json.loads((HERE.parent / "deploy" / "low-latency" / name).read_text())
            defaults = spec.get("defaults") or {}
            scenarios = spec.get("scenarios") or (spec.get("faults") or {}).get("scenarios", [])
            for scenario in scenarios:
                for action in scenario["actions"] + scenario.get("restore", []):
                    if action["kind"] == "change":
                        out.append({**{key: value for key, value in defaults.items()
                                       if key.endswith("_timeout_s")}, **action})
                        restore = scale.restore_of(action)
                        if restore and not scenario.get("restore"):
                            out.append({**restore, **{key: value for key, value in
                                                      defaults.items()
                                                      if key.endswith("_timeout_s")}})
        return out

    def test_each_change_is_the_deploy_tool_s_command_with_its_bound_s_timeouts(self):
        changes = self.plan_changes()
        self.assertTrue({item["change"] for item in changes} >= {"kill", "return"}, changes)
        for action in changes + [{"kind": "change", "change": "kill", "targets": ["lon-dist-a"]},
                                 {"kind": "change", "change": "return",
                                  "targets": ["lon-dist-a"], "ready_timeout_s": 45}]:
            command, _env = scale.action_command(action, argparse.Namespace(), pathlib.Path("."),
                                                 None)
            self.assertEqual(command[1], str(scale.DEPLOY))
            args = deploy.build_parser().parse_args(command[2:])
            self.assertIs(args.handler, deploy.command_change)
            self.assertEqual((args.command, args.node), (action["change"], action["targets"][0]))
            timeouts = scale.engine_timeouts(action)
            bound = scale.action_bound_s(action, {"nodes": {}})
            if action["change"] == "kill":
                self.assertEqual(args.gate_timeout_s, timeouts["gate_timeout_s"])
                self.assertEqual(bound, args.gate_timeout_s + scale.CONTRIB_KILL_END_S)
            else:
                self.assertEqual(args.ready_timeout_s, timeouts["ready_timeout_s"])
                self.assertEqual(bound, args.ready_timeout_s + scale.ENGINE_RESTART_S)
        import contributor_changes  # noqa: PLC0415
        self.assertEqual(scale.CONTRIB_KILL_END_S, contributor_changes.END_S)

    def test_the_marks_of_a_kill_and_its_return_are_read_back(self):
        import test_contributor_changes as tcc  # noqa: PLC0415
        import contributor_changes as cc  # noqa: PLC0415
        case = tcc.Base("setUp")
        case.setUp()
        try:
            for verb in ("kill", "return"):
                command, _env = scale.action_command(
                    {"kind": "change", "change": verb, "targets": ["contrib-lon-b"]},
                    argparse.Namespace(), pathlib.Path("."), None)
                args = deploy.build_parser().parse_args(command[2:])
                code = cc.run(case.sim, verb, args, out=case.out, err=case.err,
                              clock=case.clock, sleep=case.clock.sleep)
                self.assertEqual(code, 0, case.err.getvalue())
                case.clock.sleep(5)
            marks = scale.change_run().marks_of(case.out.getvalue())
            self.assertEqual([(kind, label) for kind, _ns, label in marks],
                             [("start", "kill.contrib-lon-b"), ("end", "kill.contrib-lon-b"),
                              ("start", "return.contrib-lon-b"),
                              ("end", "return.contrib-lon-b")])
        finally:
            for cleanup in reversed(case._cleanups):
                function, arguments, keywords = cleanup
                function(*arguments, **keywords)


if __name__ == "__main__":
    unittest.main()
