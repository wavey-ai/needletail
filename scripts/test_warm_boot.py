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

import contextlib
import importlib.util
import io
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
                          "--slow-seconds-per-4gb --slow-stall-share-permille --check-seconds")
        silent = tool("silent", "")
        problem = deploy.nodecheck_problem(old)
        self.assertIn("--warm-leave-mb, --warm-max-seconds, --slow-write-ms, "
                      "--slow-seconds-per-4gb, --slow-stall-share-permille", problem)
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

    def test_push_waits_for_every_node_s_ssh_and_pushes_nothing_without_it(self):
        # cmpd8: the first push after `up` was refused by a host a minute old.
        state = self.state()
        late = sorted(state["nodes"])[0]
        asked = []

        def wait_ssh(nodes, **_kwargs):
            asked.append(sorted(node["name"] for node in nodes))
            return [node["name"] for node in nodes if node["name"] != late]

        before = {name: self.scripts_of(name) for name in state["nodes"]}
        with mock.patch.object(deploy.common, "wait_ssh", wait_ssh):
            with self.assertRaises(SystemExit) as refused:
                self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True))
        self.assertIn(late, str(refused.exception))
        self.assertIn("nothing was pushed", str(refused.exception))
        self.assertEqual(asked, [sorted(state["nodes"])])
        self.assertEqual({name: self.scripts_of(name) for name in state["nodes"]}, before)

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
                # Never changed: push only asks whether its sshd answers.
                self.assertEqual(text, "true", name)
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
        replaced = self.slow_push(("slow_host", "replace"))
        self.assertEqual(replaced, ["lon-edge-1", "lon-edge-6"])   # a standby edge too
        self.assertIn("slow host: lon-dist-a (distributor", self.stderr)
        self.assertIn("a forwarder is never replaced for slow_host: report only", self.stderr)
        self.assertIn("slow host: contrib-lon (contributor", self.stderr)
        self.assertNotIn("slow host: lon-edge-3", self.stderr)
        ledger = json.loads((self.dir / deploy.SLOW_HOSTS_FILE).read_text())
        self.assertEqual(sorted(ledger["replaced"]), ["lon-edge-1", "lon-edge-6"])
        self.assertEqual(ledger["replaced"]["lon-edge-1"]["reasons"], ["page-stall"])
        # The new instance is slow again: reported, never replaced twice.
        replaced = self.slow_push(("slow_host", "replace"))
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

    def test_a_replacement_that_never_answers_fails_that_node_only(self):
        """Lead, 24 Nov (the referee's lab): a push that replaced four edges
        timed out on ssh to instances still booting. A replacement that does
        not answer fails for that node alone: the others are replaced, the
        push completes and names it."""
        self.seed({"lon-edge-1": record(slow=True), "lon-edge-6": record(slow=True)})
        replaced = []

        def replace(ops, name):
            if name == "lon-edge-1":
                raise RuntimeError(f"{name}: the new instance's ssh did not answer within 300 s")
            replaced.append(name)

        with mock.patch.object(deploy.LinodeOps, "replace_slow_host", replace), \
                mock.patch.object(deploy.LinodeOps, "address_dependents", lambda ops, name: []):
            with self.assertRaises(SystemExit) as caught:
                self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True,
                                                                 slow_host="replace"))
        self.assertEqual(replaced, ["lon-edge-6"])
        self.assertIn("lon-edge-1: the replace for slow_host failed", str(caught.exception))
        self.assertNotIn("lon-edge-6", str(caught.exception))
        self.assertIn("warm pass:", self.stderr)          # the push's summary still printed

    def test_every_pass_is_recorded_whether_slow_or_not(self):
        """Every host's warm figures in the deployed record and its history
        (a replaced instance's pass kept with its host), so that the rule
        can be tuned from the data."""
        fine = record()
        fine["warm"].update({"stalls": [{"ms": 400.0}, {"ms": 1930.0}], "stalls_dropped": 0,
                             "pages_over_100ms": 2, "worst_page_us": 1_930_000})
        self.seed({"lon-edge-1": fine, "lon-load-1": record()})
        self.slow_push()
        deployed = self.state()["deployed"]
        entry = deployed["warm"]["lon-edge-1"]
        self.assertIs(entry["slow_host"], False)
        self.assertEqual((entry["seconds"], entry["worst_page_us"], entry["stalls"],
                          entry["stalls_ms"]), (71.4, 1_930_000, 2, 2330.0))
        self.assertEqual(entry["nodecheck_slow_reasons"], [])
        self.assertIn("stall_share_permille", entry)
        history = [item for item in deployed["warm_history"] if item["node"] == "lon-edge-1"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["role"], "edge")
        self.assertIn("host_uuid", history[0])
        # The same record read again is not a second pass; a new boot's is.
        self.slow_push()
        again = record(unix_us=int((time.time() + 7200) * 1e6))
        self.seed({"lon-edge-1": again})
        self.slow_push()
        history = [item for item in self.state()["deployed"]["warm_history"]
                   if item["node"] == "lon-edge-1"]
        self.assertEqual(len(history), 2)

    def test_the_autoscaler_s_push_only_reports(self):
        self.seed({"lon-edge-1": record(slow=True)})
        replaced = self.slow_push(("slow_host", "report"))
        self.assertEqual(replaced, [])
        self.assertIn("reported only (the default", self.stderr)

    def test_push_only_reports_by_default(self):
        """Lead, 24 Nov: the referee's lab replaced four edges under the old
        rule and a replacement then warmed in 0.4 s. Push's default is to
        report; `--slow-host replace` asks for the replace."""
        self.seed({"lon-edge-1": record(slow=True)})
        self.assertEqual(deploy.build_parser().parse_args(["push"]).slow_host, "report")
        replaced = self.slow_push()
        self.assertEqual(replaced, [])
        self.assertIn("slow host: lon-edge-1 (edge", self.stderr)
        self.assertIn("reported only", self.stderr)
        self.assertIs(self.state()["deployed"]["warm"]["lon-edge-1"]["slow_host"], True)

    def test_an_edge_whose_address_others_name_is_only_reported(self):
        self.seed({"lon-edge-1": record(slow=True)})
        with mock.patch.object(deploy.LinodeOps, "address_dependents",
                               lambda ops, name: ["lon-dist-a"]), \
                mock.patch.object(deploy.LinodeOps, "replace_slow_host",
                                  lambda ops, name: self.fail("replaced")):
            self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True,
                                                             slow_host="replace"))
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
        # Not seeded: the check unit ran at this push and wrote its record,
        # the contributor's too (cmpd8: it had none).
        self.assertEqual((warm["lon-edge-2"]["verdict"], warm["lon-edge-2"]["complete"]),
                         ("ok", True))
        self.assertEqual((warm["contrib-lon"]["verdict"], warm["contrib-lon"]["complete"]),
                         ("ok", True))

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
        self.assertEqual(decide({"role": "edge"}, False)[0], "report")      # the default
        self.assertEqual(decide({"role": "edge"}, False, "replace")[0], "replace")
        self.assertEqual(decide({"role": "edge", "slot": True}, False, "replace")[0], "replace")
        self.assertEqual(decide({"role": "edge"}, True, "replace")[0], "report")
        self.assertEqual(decide({"role": "edge"}, False, "report")[0], "report")
        for role in ("distributor", "backbone", "contributor", "load", "livekit"):
            self.assertEqual(decide({"role": role}, False)[0], "report", role)


class ContributorUnit(unittest.TestCase):
    """cmpd8 (24 Nov): the contributor ended a whole push without a warm
    record. Its unit, its check unit and its build's nodecheck, as push
    renders them (no fake: the real binaries_for)."""

    def test_the_contributor_s_unit_wants_the_check_and_starts_after_it(self):
        settings = dict(deploy.DEFAULT_SETTINGS)
        unit = deploy.unit_text("needletail-contrib", "info", settings)
        self.assertIn(f"Wants={CHECK}\nAfter={CHECK}\n", unit)
        node = {"name": "contrib-lon", "role": "contributor", "type": "g6-dedicated-4"}
        files = deploy.node_files(["/opt/needletail/builds/k-1/av-contrib"], unit, settings, node)
        self.assertIn("needletail-nodecheck boot --out /run/needletail/node-warm.json --warm",
                      files["check"])
        self.assertIn("--warm-max-seconds 600 ", files["check"])   # 8 GB: 2 x 300 s
        names = [path.name for path in deploy.binaries_for("contributor")]
        self.assertEqual(names[0], "av-contrib")
        self.assertIn("needletail-nodecheck", names)


class ContributorWarm(scale.FakeLab):
    """push, start and replace on FakeHosts, whose check unit writes a warm
    record only with needletail-nodecheck in /usr/local/bin (FakeHosts.
    run_check): every managed host ends with a complete record, the
    contributor included; a host without one makes the command exit
    EXIT_UNWARMED naming it, after everything else, unless
    --allow-unwarmed."""

    def push(self, **kwargs):
        return self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True, **kwargs))

    def wanted(self) -> set:
        state = self.state()
        return deploy.status_warm_wanted(state)

    def test_a_push_ends_with_a_complete_record_on_every_role(self):
        self.push()
        state = self.state()
        warm = state["deployed"]["warm"]
        wanted = self.wanted()
        roles = {state["nodes"][name]["role"] for name in wanted}
        self.assertTrue({"edge", "distributor", "contributor", "load", "livekit"} <= roles, roles)
        for name in wanted:
            with self.subTest(name):
                self.assertTrue(warm[name]["complete"], warm[name])
                self.assertTrue(self.hosts.warm_record_complete(name))
        # The contributor's build carries nodecheck, linked on the node.
        self.assertIn("/usr/local/bin/needletail-nodecheck", self.hosts.fs("contrib-lon"))
        self.assertIn("warm pass: ", self.stderr)
        self.assertNotIn("UNWARMED", self.stderr)

    def cache_old_contributor_build(self) -> pathlib.Path:
        """The contributor's build as an older deploy tool cached it: its
        av-contrib, the probe and the source, no needletail-nodecheck (cmpd8:
        703fe825245f cached before 68eaaaf gave the contributor one)."""
        build = deploy.build_of(self.roots / "contrib" / "av-contrib")
        target = deploy.builds_dir() / build["key"]
        target.mkdir(parents=True)
        for path in (self.roots / "contrib" / "av-contrib", self.roots / "tools" / "aep1-48k-probe",
                     self.roots / "tools" / "daw-test-source"):
            (target / path.name).write_bytes(path.read_bytes())
            (target / path.name).chmod(0o755)
        return target

    def test_cmpd8_a_contributor_build_cached_without_nodecheck_is_completed(self):
        target = self.cache_old_contributor_build()
        self.push()
        self.assertTrue((target / "needletail-nodecheck").is_file())
        self.assertTrue(self.state()["deployed"]["warm"]["contrib-lon"]["complete"])
        self.assertIn("/usr/local/bin/needletail-nodecheck", self.hosts.fs("contrib-lon"))

    def test_a_build_without_nodecheck_and_none_to_add_is_refused_at_its_stage(self):
        """No nodecheck in the roots either: the stage of a node with the
        check unit is refused, rather than leave the node unwarmed."""
        target = self.cache_old_contributor_build()
        with mock.patch.object(deploy, "binaries_for", lambda role, node=None: []):
            with self.assertRaises(deploy.mesh_ops.Refused) as caught:
                deploy.ensure_nodecheck(target.name)
        self.assertIn(f"build {target.name} has no needletail-nodecheck", str(caught.exception))
        deploy.ensure_nodecheck(target.name)           # the roots have one: added
        self.assertTrue((target / "needletail-nodecheck").is_file())

    def test_a_running_contributor_without_a_record_is_warmed_by_the_next_push(self):
        """cmpd8's lab after the fix: the contributor runs, its check ran
        with no nodecheck and left no record. The next push's activation
        runs it again (stopping the service first, ACTIVATE_SCRIPT)."""
        self.hosts.no_warm_record.add("contrib-lon")
        with self.assertRaises(SystemExit):
            self.push()
        self.assertTrue(self.hosts.running["contrib-lon"])
        self.hosts.no_warm_record.clear()
        self.push(nodes="contrib-lon")
        self.assertTrue(self.state()["deployed"]["warm"]["contrib-lon"]["complete"])

    def test_a_push_that_leaves_one_host_unwarmed_exits_naming_it(self):
        self.hosts.no_warm_record.add("contrib-lon")
        self.hosts.warm_incomplete.add("lon-edge-2")
        with self.assertRaises(SystemExit) as caught:
            self.push()
        self.assertEqual(caught.exception.code, deploy.EXIT_UNWARMED)
        self.assertIn("push: UNWARMED: contrib-lon (warm missing), lon-edge-2 (warm "
                      "warm-timeout", self.stderr)
        self.assertNotIn("lon-edge-1 (", self.stderr.split("UNWARMED")[-1])
        # Everything else was done: every node is recorded, its warm pass too.
        state = self.state()
        self.assertIn("contrib-lon", state["deployed"]["nodes"])
        self.assertEqual(state["deployed"]["warm"]["contrib-lon"]["verdict"], "missing")
        self.assertTrue(state["deployed"]["warm"]["lon-load-1"]["complete"])
        # status marks them plainly.
        records = {name: deploy.judge_warm(deploy.parse_warm_record(
            self.hosts.fs(name).get(WARM_FILE, b"").decode()), deploy.DEFAULT_SETTINGS, None)
            for name in state["nodes"]}
        with mock.patch.object(deploy.common, "ARTIFACT_DIR", pathlib.Path("/nonexistent")):
            lines = deploy.warm_status(state, records, self.wanted())
        self.assertTrue(lines[0].startswith("warm pass: UNWARMED contrib-lon, lon-edge-2: "),
                        lines)

    def test_allow_unwarmed_goes_on_and_names_the_hosts(self):
        self.hosts.no_warm_record.add("contrib-lon")
        self.push(allow_unwarmed=True)
        self.assertIn("UNWARMED hosts, allowed by --allow-unwarmed", self.stderr)
        self.assertIn("contrib-lon (warm missing)", self.stderr)
        self.assertFalse(deploy.build_parser().parse_args(["push"]).allow_unwarmed)
        for verb in (["push", "--allow-unwarmed"], ["start", "--nodes", "x", "--allow-unwarmed"],
                     ["replace", "--nodes", "x", "--budget-usd", "1", "--allow-unwarmed"]):
            self.assertTrue(deploy.build_parser().parse_args(verb).allow_unwarmed, verb)

    def test_without_node_warm_nothing_is_asked(self):
        self.hosts.no_warm_record.update(self.state()["nodes"])
        self.push(set=["node_warm=false"])
        self.assertNotIn("UNWARMED", self.stderr)

    def test_start_of_a_standby_edge_without_a_record_exits_naming_it(self):
        standby = sorted(self.saved_standby())[0]
        self.hosts.no_warm_record.add(standby)
        self.push(allow_unwarmed=True)
        start = lambda **kwargs: self.quietly(deploy.command_start,
                                               self.namespace(nodes=standby, **kwargs))
        with self.assertRaises(SystemExit) as caught:
            start()
        self.assertEqual(caught.exception.code, deploy.EXIT_UNWARMED)
        self.assertIn(f"start: UNWARMED: {standby} (warm missing)", self.stderr)
        self.assertTrue(self.hosts.running[standby])           # started all the same
        self.assertNotIn(standby, self.saved_standby())
        # Its record written later (the check run again): a start is clean.
        self.hosts.no_warm_record.clear()
        self.quietly(deploy.command_stop, self.namespace(nodes=standby))
        self.hosts.fs(standby).pop(WARM_FILE, None)
        start()
        self.assertNotIn("UNWARMED", self.stderr)
        self.assertTrue(self.state()["deployed"]["warm"][standby]["complete"])

    def test_replace_ends_strict_on_the_hosts_it_pushed(self):
        self.push()

        def replace(ops, name, **kwargs):
            ops.warm_wanted.add(name)
            ops.warm_records[name] = None
            return deploy.mesh_ops.EXIT_DONE

        args = self.namespace(nodes="contrib-lon", mode="rebuild", no_slot=False, keep_slot=False,
                              budget_usd=1.0, spent_before_usd=None)
        with mock.patch.object(deploy, "REPLACE_LAB_CALLS", ()), \
                mock.patch.object(deploy.mesh_ops, "replace", replace):
            with self.assertRaises(SystemExit) as caught:
                self.quietly(deploy.command_replace, args)
            self.assertEqual(caught.exception.code, deploy.EXIT_UNWARMED)
            self.assertIn("replace: UNWARMED: contrib-lon (warm missing)", self.stderr)
            args.allow_unwarmed = True
            with self.assertRaises(SystemExit) as caught:
                self.quietly(deploy.command_replace, args)
            self.assertEqual(caught.exception.code, 0)


class EngineWarm(scale.FakeLab):
    """roll and restart end as push does: the records of the nodes their
    activations ran on are read, and a change that was done exits
    EXIT_UNWARMED naming a node without a complete one, unless
    --allow-unwarmed. The engine runs on a fake clock (the real
    mesh_ops.roll and restart_nodes, given it)."""

    def engine(self):
        clock = scale.types.SimpleNamespace(now=max(time.time(), getattr(self, "last_now", 0.0)))
        clock_now = lambda: clock.now  # noqa: E731

        def sleep(seconds):
            clock.now += max(seconds, 0.001)
            self.last_now = clock.now

        self.hosts.clock = clock_now
        return {"out": io.StringIO(), "err": io.StringIO(), "clock": clock_now, "sleep": sleep}

    def command(self, handler, verb, **kwargs):
        real = getattr(deploy.mesh_ops, verb)

        def on_fake_clock(*args, **given):
            return real(*args, **given, **self.engine())

        with mock.patch.object(deploy.mesh_ops, verb, on_fake_clock):
            with self.assertRaises(SystemExit) as caught:
                self.quietly(handler, self.namespace(**kwargs))
        return caught.exception.code

    def cold(self, name: str) -> None:
        """The host's check writes no record from now on, and this boot's
        is gone."""
        self.hosts.no_warm_record.add(name)
        self.hosts.fs(name).pop(WARM_FILE, None)

    def test_a_roll_names_a_node_it_left_unwarmed(self):
        ContributorWarm.push(self)
        self.set_root("av-mesh", scale.NEW)
        self.cold("lon-edge-1")
        code = self.command(deploy.command_roll, "roll", tier="edges", nodes="lon-edge-1")
        self.assertEqual(code, deploy.EXIT_UNWARMED, self.stderr)
        self.assertIn("roll: UNWARMED: lon-edge-1 (warm missing)", self.stderr)
        # Everything else was done: it runs the new build, recorded.
        self.assertEqual(self.state()["deployed"]["nodes"]["lon-edge-1"]["build"],
                         scale.key(scale.NEW))
        self.assertEqual(self.state()["deployed"]["warm"]["lon-edge-1"]["verdict"], "missing")
        # Its check was run again by the activation (the record was gone).
        self.assertGreaterEqual(self.hosts.warm_runs.get("lon-edge-1", 0), 2)

    def test_a_restart_names_a_node_it_left_unwarmed_and_allow_goes_on(self):
        ContributorWarm.push(self)
        self.cold("lon-edge-2")
        code = self.command(deploy.command_restart, "restart_nodes", nodes="lon-edge-2")
        self.assertEqual(code, deploy.EXIT_UNWARMED, self.stderr)
        self.assertIn("restart: UNWARMED: lon-edge-2 (warm missing)", self.stderr)
        code = self.command(deploy.command_restart, "restart_nodes", nodes="lon-edge-2",
                            allow_unwarmed=True)
        self.assertEqual(code, 0, self.stderr)
        self.assertIn("restart: UNWARMED hosts, allowed by --allow-unwarmed", self.stderr)
        # Warm again: a clean restart, and the record says so.
        self.hosts.no_warm_record.clear()
        code = self.command(deploy.command_restart, "restart_nodes", nodes="lon-edge-2")
        self.assertEqual(code, 0, self.stderr)
        self.assertNotIn("UNWARMED", self.stderr)
        self.assertTrue(self.state()["deployed"]["warm"]["lon-edge-2"]["complete"])

    def test_a_restart_of_a_role_names_a_node_it_left_unwarmed(self):
        ContributorWarm.push(self)
        self.cold("contrib-lon")
        with self.assertRaises(SystemExit) as caught:
            self.quietly(deploy.command_restart, self.namespace(roles="contributor"))
        self.assertEqual(caught.exception.code, deploy.EXIT_UNWARMED, self.stderr)
        self.assertIn("restart: UNWARMED: contrib-lon (warm missing)", self.stderr)
        self.hosts.no_warm_record.clear()
        self.quietly(deploy.command_restart, self.namespace(roles="contributor"))
        self.assertNotIn("UNWARMED", self.stderr)

    def test_a_failed_change_keeps_its_status_and_names_the_hosts(self):
        ops = mock.Mock()
        with mock.patch.object(deploy, "unwarmed_hosts", lambda ops: {"lon-edge-1": "missing"}):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code = deploy.engine_warm_exit(ops, "roll", deploy.mesh_ops.EXIT_STOPPED,
                                               self.namespace())
        self.assertEqual(code, deploy.mesh_ops.EXIT_STOPPED)
        self.assertIn("roll: UNWARMED: lon-edge-1 (missing)", err.getvalue())

    def test_roll_and_restart_take_the_flag(self):
        parser = deploy.build_parser()
        for verb in (["roll", "edges"], ["restart", "--nodes", "x"]):
            self.assertFalse(parser.parse_args(verb).allow_unwarmed, verb)
            self.assertTrue(parser.parse_args(verb + ["--allow-unwarmed"]).allow_unwarmed, verb)


class DualContributorWarm(scale.FakeLab):
    """The proving lab's two contributors (contrib-lon and its standby
    contrib-lon-b): both warmed, judged and named."""

    push = ContributorWarm.push

    def setUp(self):
        super().setUp()
        topology = json.loads((scale.FILES / "topology-proving.json").read_text())
        full = scale.topology_state(topology)
        self.state_file.write_text(json.dumps(full))
        self.hosts.by_address = {node["public_ipv4"]: node for node in full["nodes"].values()}
        self.hosts.by_address_name = dict(full["nodes"])
        (self.dir / "settings.json").unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            deploy.load_settings([f"standby_edges={json.dumps(topology['standby_edges'])}"],
                                 lab_run_id="test")

    def test_both_contributors_are_warmed_and_judged(self):
        self.push()
        warm = self.state()["deployed"]["warm"]
        for name in ("contrib-lon", "contrib-lon-b"):
            self.assertTrue(warm[name]["complete"], name)
        self.hosts.no_warm_record.add("contrib-lon-b")
        for name in ("contrib-lon", "contrib-lon-b"):
            self.hosts.fs(name).pop(WARM_FILE)
        with self.assertRaises(SystemExit) as caught:
            self.push()
        self.assertEqual(caught.exception.code, deploy.EXIT_UNWARMED)
        self.assertIn("UNWARMED: contrib-lon-b (warm missing)", self.stderr)
        self.assertTrue(self.state()["deployed"]["warm"]["contrib-lon"]["complete"])


class TouchProof(unittest.TestCase):
    """A pass's touch proved by what it wrote, mapped and held (peak RSS):
    a 0.4 s pass over memory the host had backed is told from one that
    touched nothing (the referee's lab, 24 Nov: seven hosts at 0.4 s)."""

    def record(self, **warm):
        item = record()
        item["warm"].update({"seconds": 0.394, "worst_page_us": 1843,
                             "mem_available_before_mb": 3557, "leave_mb": 300,
                             "mapped_mb": 3264, "pages_written": 3257 * 256, "vm_hwm_mb": 3270,
                             **warm})
        return item

    def test_a_fast_pass_that_held_its_memory_is_proved(self):
        item = self.record()
        self.assertEqual(deploy.touch_proof(item), "proved")
        word = deploy.warm_word(item)
        self.assertIn("wrote 3257MB of 3557MB available, mapped 3264MB, peak RSS 3270MB", word)
        self.assertNotIn("TOUCH", word)
        self.assertEqual(deploy.warm_entry(item)["touch"], "proved")
        self.assertEqual(deploy.warm_entry(item)["vm_hwm_mb"], 3270)

    def test_a_pass_that_touched_nothing_is_named(self):
        nothing = self.record(pages_written=10, vm_hwm_mb=40, mapped_mb=3264)
        proof = deploy.touch_proof(nothing)
        self.assertTrue(proof.startswith("NOT PROVED"), proof)
        self.assertIn("wrote 0 of 3257 MB", proof)
        self.assertIn("TOUCH NOT PROVED", deploy.warm_word(nothing))
        unheld = self.record(vm_hwm_mb=200)
        self.assertEqual(deploy.touch_proof(unheld), "NOT PROVED (peak RSS 200 MB)")
        lines = deploy.warm_status({"run_id": "r", "nodes": {"e": {"role": "edge"},
                                                             "f": {"role": "edge"}}},
                                   {"e": unheld, "f": record()})
        self.assertTrue(any(line.startswith("warm touch NOT proved on e (peak RSS 200 MB)")
                            for line in lines), lines)
        self.assertTrue(any(line.startswith("warm touch unknown") and line.endswith(" f")
                            for line in lines), lines)
        self.assertIsNone(deploy.touch_proof(record()))     # an older nodecheck


class ThereferesLab(unittest.TestCase):
    """The new rule on the referee's lab of 24 Nov (31 hosts, the default
    warm pass; per-node seconds, worst write and writes of 100 ms or more
    from its deployed record): the pass and the 5 s write flag tok-edge-1
    (5.30 s write) and tok-lk (212.9 s and a 7.48 s write) and nothing
    else; the quarter-of-the-pass clause, with each write of 100 ms or more
    counted at 100 ms (a lower bound), flags ten more."""

    TABLE = {  # node: (plan, seconds, worst ms, writes of 100 ms or more, MemTotal MB)
        "contrib-lon": ("g6-dedicated-4", 40.45, 1174, 505, 7947),
        "lon-dist-a": ("g6-dedicated-2", 20.53, 506, 142, 3921),
        "lon-edge-2": ("g6-dedicated-2", 0.394, 1, 0, 3921),
        "new-edge-2": ("g6-dedicated-2", 21.964, 204, 28, 3921),
        "tok-edge-1": ("g6-dedicated-2", 97.437, 5301, 144, 3921),
        "tok-lk": ("g6-dedicated-2", 212.881, 7479, 151, 3921),
        "tok-nano": ("g6-dedicated-2", 83.615, 4847, 157, 3921),
        "tok-view": ("g6-dedicated-4", 49.948, 1199, 511, 7947),
    }

    def test_what_the_rule_flags(self):
        flagged = {}
        for name, (kind, seconds, worst, over, total) in self.TABLE.items():
            item = record()
            item["warm"] = {"ran": True, "finished": True, "seconds": seconds,
                            "worst_page_us": worst * 1000, "mem_total_mb": total,
                            "stall_ms": 100, "stalls": [{"ms": 100.0}] * over,
                            "stalls_dropped": 0}
            judged = deploy.judge_warm(item, deploy.DEFAULT_SETTINGS,
                                       {"name": name, "type": kind})
            flagged[name] = judged["slow_reasons"]
        self.assertEqual(flagged["tok-edge-1"], ["page-stall"])
        self.assertEqual(flagged["tok-lk"], ["slow-pass", "page-stall"])
        self.assertEqual(flagged["tok-nano"], [])           # 4.85 s: under the 5 s write
        self.assertEqual(flagged["lon-edge-2"], [])         # 0.4 s, backed already
        self.assertEqual(flagged["new-edge-2"], [])
        # The stall-share clause is off (tools6): none of the table's hosts.
        # At a quarter it flagged ten fast first-touch hosts (the fastest
        # spent most of their pass in long writes).
        self.assertEqual(flagged["lon-dist-a"], [])
        self.assertEqual(flagged["contrib-lon"], [])
        self.assertEqual(flagged["tok-view"], [])
        self.assertEqual(sorted(name for name, reasons in flagged.items() if reasons),
                         ["tok-edge-1", "tok-lk"])
        quarter = {**deploy.DEFAULT_SETTINGS, "node_warm_slow_stall_share_permille": 250}
        item = record()
        kind, seconds, worst, over, total = self.TABLE["lon-dist-a"]
        item["warm"] = {"ran": True, "finished": True, "seconds": seconds,
                        "worst_page_us": worst * 1000, "mem_total_mb": total,
                        "stall_ms": 100, "stalls": [{"ms": 100.0}] * over, "stalls_dropped": 0}
        self.assertEqual(deploy.judge_warm(item, quarter, {"name": "lon-dist-a", "type": kind})
                         ["slow_reasons"], ["stall-share"])


class Boot(unittest.TestCase):
    """A new or rebuilt instance is waited for until its sshd answers."""

    def test_wait_ssh_is_bounded_and_names_who_answered(self):
        clock = [0.0]
        tries = {"a": 0, "b": 0}

        def probe(node):
            tries[node["name"]] += 1
            return node["name"] == "a" and tries["a"] >= 3

        def sleep(seconds):
            clock[0] += seconds

        answered = deploy.common.wait_ssh([{"name": "a"}, {"name": "b"}], patience_s=60,
                                          poll_s=5, clock=lambda: clock[0], sleep=sleep,
                                          probe=probe)
        self.assertEqual(answered, ["a"])
        self.assertEqual(tries["a"], 3)
        self.assertGreaterEqual(clock[0], 60)
        self.assertEqual(deploy.common.wait_ssh([], probe=probe), [])

    def test_a_replace_waits_for_ssh_before_its_push(self):
        import types  # noqa: PLC0415
        calls = []
        ops = types.SimpleNamespace(full={"nodes": {"lon-edge-1": {"name": "lon-edge-1",
                                                                   "public_ipv4": "192.0.2.9"}}},
                                    reload=lambda: None, install=lambda names, build: calls.append(
                                        ("install", names)))
        ops.wait_booted = lambda node: deploy.LinodeOps.wait_booted(ops, node)
        result = types.SimpleNamespace(returncode=0)
        with mock.patch.object(deploy, "run_lab", lambda *a: result), \
                mock.patch.object(deploy.common, "forget_host", lambda *a: None), \
                mock.patch.object(deploy, "bootstrap", lambda nodes: calls.append("bootstrap")), \
                mock.patch.object(deploy.common, "wait_ssh", lambda nodes, **k: []):
            with self.assertRaisesRegex(RuntimeError, "lon-edge-1: the new instance's ssh did "
                                                      "not answer"):
                deploy.LinodeOps.replace_slow_host(ops, "lon-edge-1")
        self.assertEqual(calls, [])                      # nothing pushed to it
        with mock.patch.object(deploy, "run_lab", lambda *a: result), \
                mock.patch.object(deploy.common, "forget_host", lambda *a: None), \
                mock.patch.object(deploy, "bootstrap", lambda nodes: calls.append("bootstrap")), \
                mock.patch.object(deploy.common, "wait_ssh",
                                  lambda nodes, **k: [node["name"] for node in nodes]):
            deploy.LinodeOps.replace_slow_host(ops, "lon-edge-1")
        self.assertEqual(calls, ["bootstrap", ("install", ["lon-edge-1"])])
        # The engine's replace (rebuild or new) waits the same way.
        ops.budget_args = []
        with mock.patch.object(deploy, "run_lab", lambda *a: result), \
                mock.patch.object(deploy.common, "forget_host", lambda *a: None), \
                mock.patch.object(deploy.common, "wait_ssh", lambda nodes, **k: []):
            with self.assertRaisesRegex(RuntimeError, "did not answer"):
                deploy.LinodeOps.rebuild(ops, "lon-edge-1", "rebuild")

    def test_collect_writes_the_warm_table(self):
        written = {}

        class Run:
            def write_json(self, path, document, *rest):
                written[path] = document

            def guarded(self, *a, **k):
                return None

        state = {"run_id": "r", "nodes": {"e": {"role": "edge"}},
                 "deployed": {"warm": {"e": {"seconds": 70.0, "slow_host": False}},
                              "warm_history": [{"node": "e", "seconds": 70.0,
                                                "host_uuid": "h1"}]}}
        with mock.patch.object(deploy, "topology_path", lambda state: None), \
                mock.patch.object(deploy, "deployed_builds", lambda state: {}), \
                mock.patch.object(deploy, "scrubbed_record", lambda record: record):
            try:
                deploy.collect_lab(Run(), state, "s")
            except Exception:  # noqa: BLE001 - only the warm table is looked at
                pass
        self.assertEqual(written["lab/warm.s.json"]["history"][0]["host_uuid"], "h1")
        self.assertIs(written["lab/warm.s.json"]["current"]["e"]["slow_host"], False)


class SlowHostRule(unittest.TestCase):
    """The deploy's slow-host rule (lead, 24 Nov): a pass over 180 s for
    every 4 GB, one write of 5 s or more, or over half the pass's
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
        # tools6: the stall-share clause is off (cmpd8's table: no value
        # separates the hosts that stopped later); at a quarter it flagged
        # ten fast first-touch hosts, at half 22 of cmpd8's 30.
        self.assertIsNone(self.SETTINGS["node_warm_slow_stall_share_permille"])
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

    def test_half_of_the_pass_in_stalls(self):
        # Off by default: the share is recorded, never a reason.
        out = self.judged(stalled_us=139_000_000, threads=2)
        self.assertEqual(out["slow_reasons"], [])
        self.assertEqual(out["stall_share_permille"], 993)
        half = {**self.SETTINGS, "node_warm_slow_stall_share_permille": 500}
        # 70 s on two threads: 140 thread-s; half is 70 s.
        many = [{"ms": 300.0}] * 230                       # 69 s
        self.assertEqual(self.judged(half, stalls=many)["slow_reasons"], [])
        self.assertEqual(self.judged(half, stalls=many + [{"ms": 3000.0}])["slow_reasons"],
                         ["stall-share"])
        # Dropped stalls count at 100 ms each (a lower bound).
        self.assertEqual(self.judged(half, stalls=many, stalls_dropped=11)["slow_reasons"],
                         ["stall-share"])
        # nodecheck's own total and thread count win when it gives them.
        self.assertEqual(self.judged(half, stalled_us=36_000_000, threads=1)["slow_reasons"],
                         ["stall-share"])
        self.assertEqual(self.judged(half, stalls=many, stalled_us=1_000_000)["slow_reasons"],
                         [])

    # cmpd8 (25 Nov): each host's warm pass at push (lab state's
    # deployed.warm: stall_share_permille, seconds) and its worst heartbeat
    # stop in any step afterwards (needletail-compare evidence
    # 20261125-4k-wave11-host-stops.json, every session; 0: none of 20 ms).
    CMPD8 = {
        "lon-dist-a": (0, 25), "syd-dist-b": (0, 0), "syd-edge-2": (0, 0), "new-view": (0, 492),
        "syd-nano": (0, 1122), "syd-view": (0, 3291), "new-lk": (58, 0), "new-edge-2": (189, 27),
        "new-nano": (354, 1020), "new-dist-b": (540, 47), "new-dist-a": (549, 20),
        "lon-lk": (638, 0), "tok-edge-2": (649, 0), "syd-dist-a": (656, 20),
        "tok-dist-b": (661, 0), "new-edge-1": (661, 70), "tok-view": (680, 2870),
        "lon-edge-1": (717, 63), "lon-src": (748, 0), "tok-edge-1": (759, 0),
        "lon-edge-2": (811, 546), "tok-lk": (845, 0), "tok-dist-a": (849, 42),
        "syd-lk": (869, 0), "lon-nano": (880, 1640), "lon-view": (884, 2955),
        "tok-nano": (884, 1811), "syd-edge-1": (891, 231), "lon-dist-b": (918, 506),
        "lk-redis": (924, 0)}

    def test_no_share_separates_cmpd8_s_hosts_that_stopped(self):
        """Why the clause is off: for every threshold of the share, hosts
        that later stopped for 100 ms or more fall on both sides, or hosts
        that never did are flagged; the best threshold still misjudges 7 of
        30."""
        bad = {name for name, (_share, stop) in self.CMPD8.items() if stop >= 100}
        best = len(self.CMPD8)
        for threshold in range(0, 1001):
            flagged = {name for name, (share, _stop) in self.CMPD8.items() if share > threshold}
            best = min(best, len(flagged ^ bad))
        self.assertEqual(best, 7)
        self.assertEqual(sorted(name for name in bad if self.CMPD8[name][0] == 0),
                         ["new-view", "syd-nano", "syd-view"])

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
    def test_a_bare_warm_word_is_not_part_of_the_service_line(self):
        # cmpd8: a host with no warm record ended its probe with "WARM " and the
        # output came back stripped, so the line read "... cpu_mb_s=1474 WARM"
        # and status died parsing the speed.
        line, (word, found) = deploy.split_status_probe(
            "svc active restarts=0 cpu_mb_s=1474\nTHP always [madvise] never\nWARM")
        self.assertEqual(line, "svc active restarts=0 cpu_mb_s=1474")
        self.assertIsNone(found)
        self.assertIn("thp=madvise", word)
        line, _ = deploy.split_status_probe("svc cpu_mb_s=1474\nTHP\nWARM")
        self.assertEqual(line, "svc cpu_mb_s=1474")

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
        self.assertTrue(any(line.startswith("slow host: e (edge) page-stall: reported only")
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
        controller = self.controller(config=auto.Config(slow_host_replace=True))
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
        controller = self.controller(config=auto.Config(slow_host_replace=True))
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
        controller = self.controller(config=auto.Config(slow_host_replace=True))
        self.sim.set_load(950)
        self.ticks(controller, 8)
        events = self.of("slow_host")
        self.assertTrue(events)
        self.assertTrue(all(item["action"] == "report" for item in events), events)
        self.assertTrue(all("holds listeners" in item["why"] for item in events), events)

    def test_the_controller_only_reports_a_slow_slot_by_default(self):
        self.fill_standby()
        self.sim.slow = {"lon-edge-11", "lon-edge-12"}
        for name in ("lon-edge-11", "lon-edge-12"):
            self.sim.nodes[name].update(udp=0)
        controller = self.controller()
        self.assertIs(self.config.slow_host_replace, False)
        self.sim.set_load(950)
        self.ticks(controller, 12)
        events = self.of("slow_host")
        self.assertTrue(events)
        self.assertTrue(all(item["action"] == "report" and "reported only by default"
                            in item["why"] for item in events), events)
        self.assertFalse([call for call in self.mutating() if call[0] == "delete"])
        self.assertEqual(controller.state["slow_replaced"], {})

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
