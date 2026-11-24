#!/usr/bin/env python3
"""Tests of the warm pass at boot by default (integration 4, after the stall
lab hunt11241537): the waits that follow a boot have the warm allowance on
top (push's activation, the engine's readiness after roll and replace, the
autoscaler's join and recovery); push warms the standby edges, the load
hosts and the self-hosted LiveKit hosts; a slow host is replaced once when it
is an edge and only reported otherwise (push and the autoscaler); status
reads the warm record; the local labs run no warm pass. Offline: no Linode
call, no token, nothing started (FakeHosts, the engine's Sim, the
autoscaler's Sim).
python3 -m unittest scripts/test_warm_boot.py"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab  # noqa: E402
import mesh_ops  # noqa: E402
import test_autoscale as autoscale_tests  # noqa: E402
import test_mesh_ops as engine_tests  # noqa: E402
import test_scale_topology as scale  # noqa: E402

deploy = scale.deploy
auto = autoscale_tests.auto

WARM_FILE = "/run/needletail/node-warm.json"
CHECK = "needletail-node-check.service"


def record(slow: bool = False, verdict: str = "ok", reasons=("page-stall",),
           unix_us: int | None = None) -> dict:
    """A needletail.node-warm.v2 record as needletail-nodecheck boot writes
    it (this boot's: written after the push began)."""
    return {"schema": "needletail.node-warm.v2", "complete": verdict == "ok", "verdict": verdict,
            "unix_us": unix_us if unix_us is not None else int((time.time() + 3600) * 1e6),
            "slow_host": slow, "slow_reasons": list(reasons) if slow else [],
            "warm": {"ran": True, "finished": True, "gib": 3.1, "seconds": 71.4,
                     "worst_page_us": 6_100_000 if slow else 9_000, "touched_mb": 3200},
            "check": {}}


class Arithmetic(unittest.TestCase):
    def test_the_bound_is_300_s_for_every_4_gb_and_the_waits_longer(self):
        for kind, seconds in (("g6-dedicated-2", 300), ("g6-dedicated-4", 600),
                              ("g6-dedicated-8", 1200), ("g6-dedicated-12", 1800),
                              ("g6-nanode-1", 300)):
            self.assertEqual(mesh_ops.warm_seconds(kind), seconds, kind)
            self.assertEqual(mesh_ops.warm_allowance_seconds(kind), seconds + 70, kind)
        self.assertEqual(mesh_ops.warm_seconds("g6-dedicated-4", 150), 300)
        self.assertIsNone(mesh_ops.warm_allowance_seconds(None))
        for kind in (None, "", "local", "x1-gpu-2"):
            self.assertIsNone(mesh_ops.warm_seconds(kind), kind)

    def test_activation_wait_has_the_check_timeout_on_top(self):
        """push waits 150 s for an activation (the 120 s deadline, the
        stop's 20 s, 10 s); the first one after the check unit is new also
        waits for the check: its TimeoutStartSec on top (a g6-dedicated-4:
        600 + 70)."""
        seen = []

        def ssh_text(node, script, **kwargs):
            seen.append(kwargs.get("timeout"))
            return '{"ok": true, "why": ""}'

        with mock.patch.object(deploy.common, "ssh_text", ssh_text):
            deadline = int((time.time() + 120) * 1000)
            deploy.activate_node({"name": "n"}, "needletail-mesh", deadline)
            settings = dict(deploy.DEFAULT_SETTINGS)
            check = deploy.check_timeout_s(settings, {"type": "g6-dedicated-4"})
            self.assertEqual(check, 670)
            deploy.activate_node({"name": "n"}, "needletail-mesh", deadline, check_s=check)
        self.assertAlmostEqual(seen[1] - seen[0], 670, delta=2)
        self.assertAlmostEqual(seen[0], 120 + 20 + 10 + 60, delta=2)


class Preflight(unittest.TestCase):
    def test_push_refuses_a_nodecheck_without_the_warm_flags(self):
        """An old needletail-nodecheck refuses the rendered flags: its
        ExecStart fails, no record is written, and no node would ever be
        ready; push says so before it installs anything."""
        import tempfile
        folder = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, folder, True)

        def tool(name: str, text: str) -> pathlib.Path:
            path = folder / name
            path.write_text(f"#!/bin/sh\n[ \"$1 $2\" = \"boot --help\" ] && echo \"{text}\"\n"
                            "exit 0\n")
            path.chmod(0o755)
            return path

        old = tool("old", "--out <OUT> --warm --check-seconds --max-stop-ms")
        new = tool("new", "--out --warm --warm-leave-mb --warm-max-seconds --slow-write-ms "
                          "--slow-seconds-per-4gb --check-seconds")
        silent = tool("silent", "")
        problem = deploy.nodecheck_problem(old)
        self.assertIn("--warm-leave-mb, --warm-max-seconds, --slow-write-ms, "
                      "--slow-seconds-per-4gb", problem)
        self.assertIn("node_warm=false", problem)
        self.assertIsNone(deploy.nodecheck_problem(new))
        self.assertIsNone(deploy.nodecheck_problem(silent))           # cannot tell
        self.assertIsNone(deploy.nodecheck_problem(folder / "none"))  # not built here


class EngineWaits(engine_tests.Base):
    """The engine's readiness wait after an activation, a replace's push and
    a slot's create is --ready-timeout-s plus the backend's warm allowance."""

    def waits(self) -> list[tuple[str, float]]:
        seen = []
        original = mesh_ops.wait_ready

        def wait_ready(ops, name, **kwargs):
            seen.append((name, kwargs["timeout_s"]))
            return original(ops, name, **kwargs)

        patcher = mock.patch.object(mesh_ops, "wait_ready", wait_ready)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_roll_waits_ready_timeout_plus_the_warm_allowance(self):
        seen = self.waits()
        self.sim.warm_allowance_s = lambda name: 240.0 if name.startswith("lon-") else 0.0
        code = self.roll("edges", options=self.options(ready_timeout_s=30.0))
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertTrue(seen)
        for name, timeout in seen:
            self.assertEqual(timeout, 270.0 if name.startswith("lon-") else 30.0, name)

    def test_replace_and_its_slot_wait_with_the_allowance(self):
        seen = self.waits()
        self.sim.warm_allowance_s = lambda name: 120.0
        code = mesh_ops.replace(self.sim, "lon-dist-b", options=self.options(ready_timeout_s=60.0),
                                out=self.out, err=self.err, clock=self.clock,
                                sleep=self.clock.sleep)
        self.assertEqual(code, 0, self.err.getvalue())
        names = {name for name, _timeout in seen}
        self.assertIn("lon-dist-b", names)
        self.assertIn("lon-dist-c", names)   # the slot's create
        self.assertTrue(all(timeout == 180.0 for _name, timeout in seen), seen)

    def test_a_backend_without_an_allowance_waits_as_before(self):
        self.assertEqual(mesh_ops.warm_allowance(object(), "x"), 0.0)
        self.assertEqual(mesh_ops.Ops().warm_allowance_s("x"), 0.0)
        engine = mesh_ops.Engine(self.sim, "roll", self.options(ready_timeout_s=45.0),
                                 self.out, self.err, self.clock, self.clock.sleep)
        self.assertEqual(engine.ready_timeout_s("lon-edge-1"), 45.0)


class Push(scale.FakeLab):
    """push on FakeHosts: what each host is given for its warm pass, and
    what a slow host costs."""

    def scripts_of(self, name: str) -> str:
        return "\n".join(self.hosts.scripts.get(name, []))

    def timeouts(self) -> dict:
        seen: dict[str, list] = {}
        original = self.hosts.ssh_text

        def ssh_text(node, script, **kwargs):
            seen.setdefault(node["name"], []).append((script, kwargs.get("timeout")))
            return original(node, script, **kwargs)

        patcher = mock.patch.object(deploy.common, "ssh_text", ssh_text)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_every_host_push_manages_is_warmed(self):
        seen = self.timeouts()
        self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True))
        state = self.state()
        kinds = {node["role"] for node in state["nodes"].values()}
        self.assertTrue({"edge", "distributor", "contributor", "load", "livekit"} <= kinds, kinds)
        standby = set(self.saved_standby())
        for name, node in state["nodes"].items():
            text = self.scripts_of(name)
            plan = deploy.mesh_ops.warm_seconds(node.get("type")) or 300
            with self.subTest(name):
                if node["role"] in ("load", "livekit"):
                    # Its own unit, enabled at boot, started now, waited for.
                    self.assertIn(f"systemctl enable --quiet {CHECK}", text)
                    self.assertIn(f"systemctl start {CHECK}", text)
                    self.assertIn(f"--warm-max-seconds {plan} ", text)
                    self.assertIn("WantedBy=multi-user.target", text)
                    timeout = [t for script, t in seen[name] if CHECK in script][0]
                    self.assertEqual(timeout, plan + 70 + 60)
                    continue
                service = "needletail-contrib" if node["role"] == "contributor" \
                    else "needletail-mesh"
                staged = self.hosts.fs(name).get(f"/etc/needletail/{service}.check.next")
                if staged is None:
                    continue  # a backbone the topology does not use
                self.assertIn(f"--warm-max-seconds {plan} ".encode(), staged)
                if name in standby:
                    # Warmed held out: taking it in waits for no pass.
                    self.assertIn(f"systemctl start {CHECK}", text)
                    self.assertNotIn("systemd-run", text)
                else:
                    activation = [t for script, t in seen[name] if "systemd-run" in script][0]
                    self.assertGreaterEqual(activation, 120 + 20 + 10 + plan + 70 + 60 - 2)
                    self.assertIn(f"cat {WARM_FILE}", text)

    def test_without_node_warm_no_load_or_livekit_host_is_warmed(self):
        self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True,
                                                         set=["node_warm=false"]))
        state = self.state()
        for name, node in state["nodes"].items():
            text = self.scripts_of(name)
            if node["role"] == "livekit":
                self.assertEqual(text, "", name)   # never touched
            elif node["role"] == "load":
                self.assertNotIn(f"systemctl start {CHECK}", text, name)
                self.assertIn(f"systemctl disable --quiet {CHECK}", text, name)
            else:
                self.assertNotIn(f"systemctl start {CHECK}", text, name)

    def seed(self, records: dict) -> None:
        for name, item in records.items():
            self.hosts.fs(name)[WARM_FILE] = json.dumps(item).encode()

    def slow_push(self, *extra):
        replaced = []
        patches = (mock.patch.object(deploy.LinodeOps, "replace_slow_host",
                                     lambda ops, name: replaced.append(name)),
                   mock.patch.object(deploy.LinodeOps, "address_dependents",
                                     lambda ops, name: []))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True, **dict(extra)))
        return replaced

    def test_a_slow_edge_is_replaced_once_and_other_roles_only_reported(self):
        self.seed({"lon-edge-1": record(slow=True), "lon-edge-6": record(slow=True),
                   "lon-dist-a": record(slow=True, reasons=("slow-pass",)),
                   "contrib-lon": record(slow=True), "lon-load-1": record(slow=True),
                   "lon-edge-2": record(slow=False),
                   # Written before this push (an earlier boot): not judged now.
                   "lon-edge-3": record(slow=True, unix_us=1_000_000)})
        replaced = self.slow_push()
        self.assertEqual(replaced, ["lon-edge-1", "lon-edge-6"])   # a standby edge too
        self.assertIn("slow host: lon-dist-a (distributor", self.stderr)
        self.assertIn("a forwarder is never replaced for slow_host: report only", self.stderr)
        self.assertIn("slow host: contrib-lon (contributor", self.stderr)
        self.assertNotIn("slow host: lon-edge-3", self.stderr)
        ledger = json.loads((self.dir / deploy.SLOW_HOSTS_FILE).read_text())
        self.assertEqual(sorted(ledger["replaced"]), ["lon-edge-1", "lon-edge-6"])
        self.assertEqual(ledger["replaced"]["lon-edge-1"]["reasons"], ["page-stall"])
        # The new instance is slow again: reported, never replaced twice.
        replaced = self.slow_push()
        self.assertEqual(replaced, [])
        self.assertIn("replaced once for slow_host already: report only", self.stderr)

    def test_a_touched_edge_of_the_hunt_is_not_a_slow_host(self):
        """The hunt's three warmed edges (hunt11241537): single page writes
        of up to 1.93 s in a pass of 43 to 79 s, then an hour clean. Their
        nodecheck (the 1 s rule) said slow_host; the deploy's rule does
        not, and nothing is replaced."""
        hunt = record(slow=True)
        hunt["warm"].update({"worst_page_us": 1_930_000, "seconds": 79.0, "stall_ms": 100,
                             "stalls": [{"at_unix": 1.0, "ms": 1930.0, "after_mb": 10},
                                        {"at_unix": 2.0, "ms": 640.0, "after_mb": 900}],
                             "stalls_dropped": 0, "mem_total_mb": 3910})
        self.seed({"lon-edge-1": hunt})
        replaced = self.slow_push()
        self.assertEqual(replaced, [])
        self.assertNotIn("slow host: lon-edge-1", self.stderr)
        warm = self.state()["deployed"]["warm"]["lon-edge-1"]
        self.assertIs(warm["slow_host"], False)
        self.assertEqual(warm["slow_reasons"], [])

    def test_the_autoscaler_s_push_only_reports(self):
        self.seed({"lon-edge-1": record(slow=True)})
        replaced = self.slow_push(("slow_host", "report"))
        self.assertEqual(replaced, [])
        self.assertIn("--slow-host report", self.stderr)

    def test_an_edge_whose_address_others_name_is_only_reported(self):
        self.seed({"lon-edge-1": record(slow=True)})
        with mock.patch.object(deploy.LinodeOps, "address_dependents",
                               lambda ops, name: ["lon-dist-a"]), \
                mock.patch.object(deploy.LinodeOps, "replace_slow_host",
                                  lambda ops, name: self.fail("replaced")):
            self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True))
        self.assertIn("in the arguments of lon-dist-a: report only", self.stderr)

    def test_the_deployed_record_keeps_each_host_s_warm_pass(self):
        """Every host whose warm pass push ran or read: its verdict,
        duration and worst page write (its worst stall) in deployed
        ["warm"], load hosts included, so that a report can show them."""
        slow = record(slow=True)
        self.seed({"lon-edge-1": slow, "lon-load-1": record(), "lon-edge-6": record()})
        self.slow_push()
        warm = self.state()["deployed"]["warm"]
        self.assertEqual(warm["lon-edge-1"]["seconds"], 71.4)
        self.assertEqual(warm["lon-edge-1"]["worst_page_us"], 6_100_000)
        self.assertIs(warm["lon-edge-1"]["slow_host"], True)
        self.assertEqual(warm["lon-edge-1"]["slow_reasons"], ["page-stall"])
        self.assertEqual(warm["lon-load-1"]["verdict"], "ok")
        self.assertEqual(warm["lon-edge-6"]["verdict"], "ok")      # a standby edge
        self.assertEqual(warm["lon-edge-2"], {"verdict": "missing", "complete": False})
        self.assertNotIn("contrib-lon", {name for name, item in warm.items()
                                         if item["verdict"] == "ok"})

    def test_collect_takes_every_node_s_warm_record(self):
        for name in ("lon-edge-1", "lon-load-1", "lon-lk"):
            node = self.state()["nodes"][name]
            script = deploy.collect_script(node, "T1")
            self.assertIn(f'[ -f {WARM_FILE} ] && cp {WARM_FILE} "$D/node-warm.$T.json"', script)
        kind, text, units = deploy.collect_describe("node-warm.T1.json")
        self.assertEqual(kind, "node-api")
        self.assertIn("worst page write", text)

    def test_the_decision(self):
        decide = deploy.slow_host_decision
        self.assertEqual(decide({"role": "edge"}, False)[0], "replace")
        self.assertEqual(decide({"role": "edge", "slot": True}, False)[0], "replace")
        self.assertEqual(decide({"role": "edge"}, True)[0], "report")
        self.assertEqual(decide({"role": "edge"}, False, "report")[0], "report")
        for role in ("distributor", "backbone", "contributor", "load", "livekit"):
            self.assertEqual(decide({"role": role}, False)[0], "report", role)


class SlowHostRule(unittest.TestCase):
    """The deploy's slow-host rule (lead, 24 Nov): a pass over 180 s for
    every 4 GB, one write of 5 s or more, or over a quarter of the pass's
    thread time in writes of 100 ms or more; nodecheck's own judgement kept
    beside it."""

    SETTINGS = deploy.DEFAULT_SETTINGS
    EDGE = {"name": "lon-edge-1", "role": "edge", "type": "g6-dedicated-2"}

    def judged(self, settings=None, node=None, **warm):
        base = {"ran": True, "finished": True, "seconds": 70.0, "worst_page_us": 9_000,
                "stall_ms": 100, "stalls": [], "stalls_dropped": 0, "mem_total_mb": 3910}
        item = record(slow=True, reasons=("page-stall",))
        item["warm"] = {**base, **warm}
        return deploy.judge_warm(item, settings or self.SETTINGS, node or self.EDGE)

    def test_the_defaults(self):
        self.assertEqual(self.SETTINGS["node_warm_slow_write_ms"], 5000)
        self.assertEqual(self.SETTINGS["node_warm_slow_stall_share_permille"], 250)
        self.assertEqual(self.SETTINGS["node_warm_slow_seconds_per_4gb"], 180)

    def test_the_hunt_s_touched_edge_is_not_slow(self):
        out = self.judged(seconds=79.0, worst_page_us=1_930_000,
                          stalls=[{"ms": 1930.0}, {"ms": 640.0}, {"ms": 120.0}])
        self.assertIs(out["slow_host"], False)
        self.assertEqual(out["slow_reasons"], [])
        self.assertIs(out["nodecheck_slow_host"], True)
        self.assertEqual(out["nodecheck_slow_reasons"], ["page-stall"])
        self.assertEqual(out["stall_share_permille"], 17)    # 2.69 s of 158 thread-s

    def test_one_write_of_five_seconds(self):
        self.assertEqual(self.judged(worst_page_us=4_999_000)["slow_reasons"], [])
        self.assertEqual(self.judged(worst_page_us=5_000_000)["slow_reasons"], ["page-stall"])

    def test_a_pass_over_180_s_for_every_4_gb(self):
        self.assertEqual(self.judged(seconds=180.0)["slow_reasons"], [])
        self.assertEqual(self.judged(seconds=180.5)["slow_reasons"], ["slow-pass"])
        # 16 GB (g6-dedicated-8 by MemTotal): 720 s.
        self.assertEqual(self.judged(seconds=700.0, mem_total_mb=16_000)["slow_reasons"], [])
        self.assertEqual(self.judged(seconds=730.0, mem_total_mb=16_384)["slow_reasons"],
                         ["slow-pass"])
        # No MemTotal in the record: the plan's memory.
        node = {"name": "lon-load-1", "role": "load", "type": "g6-dedicated-8"}
        self.assertEqual(self.judged(node=node, seconds=700.0, mem_total_mb=None)["slow_reasons"],
                         [])

    def test_a_quarter_of_the_pass_in_stalls(self):
        # 70 s on two threads: 140 thread-s; a quarter is 35 s.
        many = [{"ms": 300.0}] * 110                       # 33 s
        self.assertEqual(self.judged(stalls=many)["slow_reasons"], [])
        self.assertEqual(self.judged(stalls=many + [{"ms": 3000.0}])["slow_reasons"],
                         ["stall-share"])
        # Dropped stalls count at 100 ms each (a lower bound).
        self.assertEqual(self.judged(stalls=many, stalls_dropped=21)["slow_reasons"],
                         ["stall-share"])
        # nodecheck's own total and thread count win when it gives them.
        self.assertEqual(self.judged(stalled_us=34_000_000, threads=1)["slow_reasons"],
                         ["stall-share"])
        self.assertEqual(self.judged(stalls=many, stalled_us=1_000_000)["slow_reasons"], [])

    def test_a_timeout_and_the_settings(self):
        self.assertIn("warm-timeout", self.judged(finished=False, seconds=300.0)["slow_reasons"])
        strict = {**self.SETTINGS, "node_warm_slow_write_ms": 1000,
                  "node_warm_slow_stall_share_permille": 10}
        self.assertEqual(self.judged(strict, worst_page_us=1_930_000,
                                     stalls=[{"ms": 1930.0}])["slow_reasons"],
                         ["page-stall", "stall-share"])
        self.assertIsNone(deploy.judge_warm(None, self.SETTINGS, self.EDGE))
        bare = {"verdict": "ok", "slow_host": False}
        self.assertEqual(deploy.judge_warm(bare, self.SETTINGS, self.EDGE), bare)

    def test_the_settings_are_checked_and_rendered(self):
        for bad in ({"node_warm_slow_write_ms": 0}, {"node_warm_slow_stall_share_permille": 1001},
                    {"node_warm_slow_stall_share_permille": True}):
            with self.subTest(bad), self.assertRaises(ValueError):
                deploy.check_stall_settings({**self.SETTINGS, **bad})
        text = deploy.check_unit_text({**self.SETTINGS, "node_warm_slow_write_ms": 2500},
                                      self.EDGE)
        self.assertIn("--slow-write-ms 2500 ", text)
        self.assertIn("node_warm_slow_write_ms", deploy.NODE_SETTINGS)
        self.assertIn("node_warm_slow_stall_share_permille", deploy.NODE_SETTINGS)

    def test_status_judges_by_the_deployed_settings(self):
        line = "needletail-mesh active restarts=0\nWARM " + json.dumps(
            {**record(slow=True), "warm": {"ran": True, "finished": True, "seconds": 60.0,
                                           "worst_page_us": 1_500_000, "stalls": []}})
        _main, (_word, judged) = deploy.split_status_probe(line, self.SETTINGS, self.EDGE)
        self.assertIs(judged["slow_host"], False)
        _main, (_word, raw) = deploy.split_status_probe(line)
        self.assertIs(raw["slow_host"], True)


class Status(unittest.TestCase):
    def test_the_probe_line_and_the_summary(self):
        text = ("needletail-mesh active restarts=0 mem_mb=40\n"
                "THP always [madvise] never\n"
                f"WARM {json.dumps(record(slow=True))}\n")
        line, (word, found) = deploy.split_status_probe(text)
        self.assertEqual(line, "needletail-mesh active restarts=0 mem_mb=40")
        self.assertIn("thp=madvise", word)
        self.assertIn("warm ok 71.4s worst 6100ms SLOW HOST page-stall", word)
        self.assertTrue(found["slow_host"])
        _line, (word, found) = deploy.split_status_probe("x\nTHP \nWARM \n")
        self.assertIsNone(found)
        self.assertIn("warm missing", word)
        state = {"run_id": "r", "nodes": {"e": {"role": "edge"}, "d": {"role": "distributor"},
                                          "l": {"role": "load"}},
                 "deployed": {"settings": {"transparent_hugepage": "never"}}}
        with mock.patch.object(deploy.common, "ARTIFACT_DIR", pathlib.Path("/nonexistent")):
            lines = deploy.warm_status(state, {"e": record(slow=True), "d": record(slow=True),
                                               "l": None})
        self.assertIn("warm pass: not complete on l", lines)
        self.assertTrue(any(line.startswith("slow host: e (edge) page-stall: to replace once")
                            for line in lines), lines)
        self.assertTrue(any("slow host: d (distributor)" in line and "report only" in line
                            for line in lines), lines)
        self.assertEqual(lines[-1], "transparent_hugepage: deployed never")

    def test_node_summary_shows_node_warm_from_the_api(self):
        document = {"schema": "needletail.node.v1", "node_id": "e", "role": "edge",
                    "build": {"git": "abc", "built_unix": 1, "dirty": False},
                    "process": {"boot_id": 1, "args_sha256": "x"}, "state": "starting",
                    "ready": False, "node_warm": {"configured": True, "complete": False,
                                                  "verdict": "warm-timeout", "slow_host": True}}
        self.assertIn("node_warm=warm-timeout slow_host", deploy.node_summary(document, None))


class ShellWarm(unittest.TestCase):
    def test_the_dependency_free_pass_holds_nothing_and_says_what_it_did(self):
        """scripts/warm-host.sh, for a host without needletail-nodecheck:
        dd's buffer is anonymous memory, gone when dd exits (no file: safe
        to kill, nothing held); one JSON line. Run here on 32 MiB only."""
        import subprocess
        result = subprocess.run(["sh", str(HERE / "warm-host.sh"), "--mb", "32"],
                                capture_output=True, text=True, timeout=60, check=True)
        line = json.loads(result.stdout)
        self.assertEqual((line["method"], line["ok"], line["touched_mb"]), ("dd", True, 32))
        self.assertGreater(line["mem_total_mb"], 0)
        text = (HERE / "warm-host.sh").read_text()
        self.assertNotIn("/dev/shm", text)
        self.assertIn("of=/dev/null", text)
        refused = subprocess.run(["sh", str(HERE / "warm-host.sh"), "--nope"],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(refused.returncode, 2)


class LocalLabs(unittest.TestCase):
    def test_the_local_labs_run_no_warm_pass(self):
        """The namespace labs share the dev box's one kernel and memory and
        run no check unit: node_warm is off there (a record that never
        comes would keep every node from being ready)."""
        self.assertIs(edge_lab.DEFAULT_OVERRIDES["node_warm"], False)
        spec = importlib.util.spec_from_file_location("local_region_lab_warm",
                                                      HERE / "local-region-lab.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.DEFAULT_OVERRIDES["node_warm"], False)
        self.assertIs(deploy.DEFAULT_SETTINGS["node_warm"], True)


class WarmSim(autoscale_tests.Sim):
    """The autoscaler's Sim with Linode plans and each node's node_warm."""

    def __init__(self, clock, newyork=False):
        super().__init__(clock, newyork)
        self.slow: set[str] = set()
        self.creates: dict[str, int] = {}

    def roster(self):
        out = super().roster()
        for nodes in out.values():
            for node in nodes:
                node["type"] = "g6-dedicated-2" if node["role"] == "edge" else "g6-dedicated-4"
        return out

    def document(self, name):
        document = super().document(name)
        if document is not None:
            slow = name in self.slow
            document["node_warm"] = {"configured": True, "complete": True, "verdict": "ok",
                                     "slow_host": slow, "warm": {}, "check": {}}
        return document

    def create(self, names):
        super().create(names)
        for name in names:
            self.creates[name] = self.creates.get(name, 0) + 1


class Autoscaler(autoscale_tests.Base):
    def setUp(self):
        super().setUp()
        self.sim = WarmSim(self.clock)
        self.addCleanup(__import__("shutil").rmtree, self.sim.lock, True)

    def fill_standby(self):
        for index in range(6, 11):
            self.sim.nodes[f"lon-edge-{index}"]["running"] = True

    def test_a_join_s_deadline_has_the_warm_allowance(self):
        controller = self.controller()
        self.sim.set_load(950)
        self.ticks(controller, 3)
        joins = [action for action in controller.state["actions"].values()
                 if action["kind"] == "join"]
        self.assertTrue(joins)
        for action in joins:
            base = (self.config.create_timeout_s if action["how"] == "create"
                    else self.config.join_timeout_s)
            self.assertAlmostEqual(action["deadline_unix"] - action["trigger_unix"], base + 370)
        self.assertEqual(auto.Controller.warm_s({"type": "g6-dedicated-8"}), 1270.0)
        self.assertEqual(auto.Controller.warm_s({}), 0.0)

    def test_recovery_after_a_boot_or_rebuild_waits_for_the_warm_pass(self):
        controller = self.controller()
        node = {"type": "g6-dedicated-2"}
        self.assertEqual(controller.settle_s({"step": "restart"}, node), auto.RECOVER_SETTLE_S)
        for step in ("boot", "rebuild"):
            self.assertEqual(controller.settle_s({"step": step}, node),
                             auto.RECOVER_SETTLE_S + 370)

    def test_a_slow_new_slot_edge_is_deleted_and_created_again_once(self):
        self.fill_standby()
        self.sim.slow = {"lon-edge-11", "lon-edge-12"}
        for name in ("lon-edge-11", "lon-edge-12"):
            self.sim.nodes[name].update(udp=0)
        controller = self.controller()
        self.sim.set_load(950)
        self.ticks(controller, 12)
        events = self.of("slow_host")
        replaced = [item["node"] for item in events if item["action"] == "replace"]
        self.assertEqual(sorted(replaced), ["lon-edge-11", "lon-edge-12"])
        self.assertIn(("delete", "lon-edge-11"), self.mutating())
        self.assertEqual(self.sim.creates.get("lon-edge-11"), 2)
        # The second instance is slow too: reported, and it serves.
        reports = [item for item in events if item["action"] == "report"]
        self.assertTrue(any("replaced once for slow_host already" in item["why"]
                            for item in reports), events)
        self.assertEqual(sorted(item["node"] for item in self.of("serving")
                                if item["node"].startswith("lon-edge-1")
                                and item["node"] in ("lon-edge-11", "lon-edge-12")),
                         ["lon-edge-11", "lon-edge-12"])
        self.assertEqual(self.mutating().count(("delete", "lon-edge-11")), 1)
        self.assertIn("lon-edge-11", controller.state["slow_replaced"])

    def test_a_standby_edge_on_a_slow_host_is_only_reported(self):
        self.sim.slow = {f"lon-edge-{index}" for index in range(6, 11)}
        controller = self.controller()
        self.sim.set_load(950)
        self.ticks(controller, 6)
        events = self.of("slow_host")
        self.assertTrue(events)
        self.assertTrue(all(item["action"] == "report" for item in events), events)
        self.assertTrue(all("standby edge" in item["why"] for item in events), events)
        self.assertFalse([call for call in self.mutating() if call[0] == "delete"])

    def test_a_slow_slot_that_holds_listeners_is_only_reported(self):
        self.fill_standby()
        self.sim.slow = {"lon-edge-11", "lon-edge-12"}
        controller = self.controller()
        self.sim.set_load(950)
        self.ticks(controller, 8)
        events = self.of("slow_host")
        self.assertTrue(events)
        self.assertTrue(all(item["action"] == "report" for item in events), events)
        self.assertTrue(all("holds listeners" in item["why"] for item in events), events)

    def test_the_guard_takes_the_slow_host_proof_for_slots_only(self):
        backend = auto.Guarded(self.sim, self.config)
        backend.roster()
        with self.assertRaises(auto.Refused):
            backend.delete("lon-edge-6", proof="slow-host")   # a standby edge
        with self.assertRaises(auto.Refused):
            backend.delete("lon-dist-a", proof="slow-host")   # a first forwarder

    def test_node_view_reads_node_warm(self):
        view = auto.node_view("x", {"schema": "needletail.node.v1", "node_id": "x",
                                    "role": "edge", "state": "ready", "ready": True,
                                    "node_warm": {"configured": True, "complete": True,
                                                  "verdict": "ok", "slow_host": True}})
        self.assertIs(view["slow_host"], True)
        self.assertEqual(view["warm_verdict"], "ok")
        view = auto.node_view("x", {"schema": "needletail.node.v1", "node_id": "x",
                                    "role": "edge", "state": "ready", "ready": True})
        self.assertIsNone(view["slow_host"])
        self.assertIsNone(auto.node_view("x", None)["slow_host"])


if __name__ == "__main__":
    unittest.main()
