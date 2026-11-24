#!/usr/bin/env python3
"""scale-run's warm gate: no measured window until every host of it is warm
(mesh nodes ready with their boot warm pass complete, standby edges by the
deploy's record, the other hosts by the referee's warm result), else a
recorded operator override."""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("scale_run_gate", HERE / "scale-run.py")
scale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scale)

CREATED = "2026-11-25T09:00:00"
CREATED_UNIX = 1_795_597_200.0


def lab() -> dict:
    nodes = {}
    for name, role in (("contrib-lon", "contributor"), ("lon-dist-a", "distributor"),
                       ("lon-edge-1", "edge"), ("lon-edge-4", "edge"), ("lon-src", "load"),
                       ("lon-view", "load"), ("lon-lk", "livekit")):
        nodes[name] = {"name": name, "role": role, "site": "london", "public_ipv4": "10.0.0.1",
                       "created": CREATED}
    return {"nodes": nodes, "deployed": {"warm": {
        "contrib-lon": {"verdict": "ok", "complete": True},
        "lon-edge-4": {"verdict": "ok", "complete": True}}}}


def api(ready=True, complete=True, warm=True) -> dict:
    document = {"schema": "needletail.node.v1", "state": "ready" if ready else "starting",
                "ready": ready}
    if warm:
        document["node_warm"] = {"configured": True, "complete": complete,
                                 "verdict": "ok" if complete else "warm-timeout"}
    return document


class Gate(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "target" / "compare").mkdir(parents=True)
        self.out = self.root / "run"
        self.out.mkdir()
        self.events = []
        patcher = mock.patch.object(scale, "COMPARE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def referee(self, started: float, hosts: dict) -> None:
        path = self.root / "target" / "compare" / f"warm-{int(started)}.json"
        path.write_text(json.dumps({"started_unix": started, "ended_unix": started + 60,
                                    "all": False, "reserve_mb": 300, "hosts": hosts}))

    def warm_hosts(self, started: float = CREATED_UNIX + 600) -> None:
        self.referee(started, {name: {"touched_mb": 3500, "seconds": 41.0, "worst_write_ms": 210,
                                      "stalls": [], "stall_ms": 100}
                               for name in ("lon-src", "lon-view", "lon-lk")})

    def gate(self, answers: dict, override=None, wait_s=0.0):
        args = argparse.Namespace(warm_wait_s=wait_s, warm_override=override)
        poll = lambda node, path: answers[node["name"]]() if callable(answers[node["name"]]) \
            else answers[node["name"]]
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        return scale.warm_gate(lab(), ["lon-edge-4"], args, self.events.append, self.out,
                               poll=poll, now=lambda: clock[0], sleep=sleep)

    def answers(self, **changed):
        base = {"contrib-lon": api(warm=False), "lon-dist-a": api(), "lon-edge-1": api()}
        return {**base, **changed}

    def test_every_host_warm_passes_and_is_recorded(self):
        self.warm_hosts()
        result = self.gate(self.answers())
        self.assertTrue(result["ok"])
        self.assertEqual(result["cold"], [])
        hosts = result["hosts"]
        self.assertEqual(hosts["lon-edge-1"]["source"], "api/node node_warm")
        self.assertEqual(hosts["contrib-lon"]["source"], "deployed record")   # av-contrib
        self.assertEqual(hosts["lon-edge-4"]["source"], "deployed record (standby)")
        self.assertEqual(hosts["lon-view"]["source"], "referee")
        written = json.loads((self.out / scale.WARM_GATE_FILE).read_text())
        self.assertTrue(written["ok"])
        self.assertEqual(self.events[-1]["event"], "warm_gate")
        self.assertTrue(self.events[-1]["ok"])

    def test_a_cold_mesh_node_blocks_the_window(self):
        self.warm_hosts()
        with self.assertRaises(SystemExit) as caught:
            self.gate(self.answers(**{"lon-edge-1": api(complete=False)}))
        self.assertIn("lon-edge-1 (warm pass not complete (warm-timeout))", str(caught.exception))
        self.assertIn("--warm-override", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.gate(self.answers(**{"lon-dist-a": api(ready=False)}))
        self.assertIn("lon-dist-a (not ready)", str(caught.exception))
        with self.assertRaises(SystemExit):
            self.gate(self.answers(**{"lon-dist-a": {"error": "timed out"}}))
        self.assertFalse(json.loads((self.out / scale.WARM_GATE_FILE).read_text())["ok"])

    def test_the_gate_waits_for_a_node_still_warming(self):
        self.warm_hosts()
        states = iter([api(complete=False), api(complete=False), api()])
        result = self.gate(self.answers(**{"lon-edge-1": lambda: next(states)}), wait_s=600)
        self.assertTrue(result["ok"])
        self.assertEqual(result["waited_s"], 20.0)

    def test_other_hosts_need_the_referee_s_warm(self):
        with self.assertRaises(SystemExit) as caught:
            self.gate(self.answers())
        self.assertIn("lon-src (no warm result of the referee (compare_lab.py warm) is present)",
                      str(caught.exception))
        # A result older than the instance is another instance's.
        self.warm_hosts(started=CREATED_UNIX - 3600)
        with self.assertRaises(SystemExit) as caught:
            self.gate(self.answers())
        self.assertIn("older than the instance", str(caught.exception))
        # A host whose warm failed in the newest result.
        self.referee(CREATED_UNIX + 900, {"lon-src": {"error": "ssh: timed out"},
                                         "lon-view": {"touched_mb": 3000},
                                         "lon-lk": {"touched_mb": 1500}})
        with self.assertRaises(SystemExit) as caught:
            self.gate(self.answers())
        self.assertIn("lon-src (the referee's warm failed: ssh: timed out)", str(caught.exception))
        self.assertNotIn("lon-view", str(caught.exception))

    def test_an_operator_override_is_recorded_with_the_hosts_it_let_through(self):
        result = self.gate(self.answers(**{"lon-edge-1": api(complete=False)}),
                           override="the referee warmed lon-src by hand at 09:12")
        self.assertFalse(result["ok"])
        self.assertEqual(result["overridden"]["reason"],
                         "the referee warmed lon-src by hand at 09:12")
        self.assertEqual(result["overridden"]["hosts"],
                         ["lon-edge-1", "lon-lk", "lon-src", "lon-view"])
        self.assertEqual(self.events[-1]["overridden"]["reason"],
                         "the referee warmed lon-src by hand at 09:12")
        written = json.loads((self.out / scale.WARM_GATE_FILE).read_text())
        self.assertIn("overridden", written)

    def test_run_and_party_take_the_options(self):
        parser = scale.build_parser()
        for verb in ("run", "party"):
            args = parser.parse_args([verb, "--warm-override", "x", "--warm-wait-s", "30"])
            self.assertEqual((args.warm_override, args.warm_wait_s), ("x", 30.0))
            args = parser.parse_args([verb])
            self.assertEqual((args.warm_override, args.warm_wait_s), (None, scale.WARM_WAIT_S))

    def test_command_run_gates_before_its_phases(self):
        """command_run calls the gate after it opens the run and before the
        phases or the first step (read from its source: the run itself
        needs a lab)."""
        import inspect
        source = inspect.getsource(scale.command_run)
        self.assertLess(source.index("warm_gate(lab, standby, args, log, directory)"),
                        source.index("begin_phases("))
        self.assertLess(source.index("warm_gate("), source.index("run_step("))
        party = inspect.getsource(scale.command_party)
        self.assertLess(party.index("warm_gate(lab, standby, args, log, directory)"),
                        party.index("begin_phases("))


if __name__ == "__main__":
    unittest.main()
