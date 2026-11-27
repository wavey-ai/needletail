#!/usr/bin/env python3
"""hunt-report.py on the real stall-hunt run hunt11241537 (24 Nov 2026,
9.3 GB in ~/needletail-data/hunt11241537), when it is on this machine: the
fixed report must give the operator's reading (.resilient/reports/hunt-lab.md)
in about a minute.

- S1, the first session on the fresh deploy: the touched edges (lon-edge-1,
  lon-edge-3, tok-edge-1) have 0 ms of their own stops; lon-edge-2 101 ms
  (one vCPU not run), lon-edge-4 1,184 ms (the whole guest not run), the old
  tok-edge-2 18.5 s (seven stops of 100 ms or more, five of 2.65 to 4.79 s),
  new-edge-1 none.
- The source host's 2,251 ms stop at 16:00:46.79 and its 131 ms one at
  16:00:54.1 are shared by the edges (upstream), never an edge's stop.
- The old tok-edge-2's stops happened on host b28cb764, the replacement's
  on 06b7abc1; nine soft lockups on the old tok-edge-2, all on b28cb764.
- H4 (first touch) MATTERS; no stop is called "kernel, interrupts off".

    python3 -m unittest test_hunt_report_real (in scripts/compare; reads the
    run, writes nothing)
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
RUN = pathlib.Path(os.environ.get("NEEDLETAIL_HUNT_RUN",
                                  str(pathlib.Path.home() / "needletail-data" / "hunt11241537"
                                      / "hunt")))
SOURCE_STOP_S = 1795536046.789
LIMIT_S = 120.0


def load_report():
    spec = importlib.util.spec_from_file_location("hunt_report_real", HERE / "hunt-report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((RUN / "hunt.json").is_file(), "the hunt run hunt11241537 is not here")
class RealRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        report = load_report()
        started = time.monotonic()
        cls.report = report.build(report.Run(RUN), null=False,
                                  jobs=min(8, os.cpu_count() or 1))
        cls.text = report.format_report(cls.report)
        cls.seconds = time.monotonic() - started

    def stops(self, session: str, edge: str) -> list[dict]:
        return [stop for stop in self.report["edges"][session]["stops"] if stop["edge"] == edge]

    def test_it_finishes_in_about_a_minute(self):
        self.assertLess(self.seconds, LIMIT_S)

    def test_the_first_session_s_own_stops_per_edge(self):
        for edge in ("lon-edge-1", "lon-edge-3", "tok-edge-1", "new-edge-1"):
            self.assertEqual(self.stops("S1", edge), [], edge)
        self.assertEqual([round(stop["ms"]) for stop in self.stops("S1", "lon-edge-2")], [101])
        self.assertEqual([round(stop["ms"]) for stop in self.stops("S1", "lon-edge-4")], [1184])
        tok = [stop["ms"] for stop in self.stops("S1", "tok-edge-2")]
        self.assertEqual(len(tok), 7)
        self.assertAlmostEqual(sum(tok) / 1000, 18.5, delta=0.1)
        long = sorted(ms for ms in tok if ms >= 1000)
        self.assertEqual(len(long), 5)
        self.assertAlmostEqual(long[0], 2654, delta=5)
        self.assertAlmostEqual(long[-1], 4793, delta=5)

    def test_the_source_s_stops_are_shared_not_the_edges(self):
        shared = self.report["edges"]["S1"]["shared"]
        big = [slot for slot in shared if abs(slot["from_us"] / 1e6 - SOURCE_STOP_S) < 0.1]
        self.assertEqual(len(big), 1)
        self.assertGreaterEqual(big[0]["ms"], 2250)
        self.assertEqual(len(big[0]["edges"]), 7)
        self.assertTrue(any(abs(slot["from_us"] / 1e6 - 1795536054.117) < 0.1
                            and 125 <= slot["ms"] <= 140 for slot in shared))
        for stop in self.report["edges"]["S1"]["stops"]:
            self.assertFalse(SOURCE_STOP_S - 0.1 <= stop["from_us"] / 1e6 <= SOURCE_STOP_S + 2.3,
                             stop)

    def test_each_stop_s_class_and_host(self):
        (edge4,) = self.stops("S1", "lon-edge-4")
        self.assertEqual(edge4["class"], "not run")
        self.assertIn("the whole guest", edge4["where"])
        (edge2,) = self.stops("S1", "lon-edge-2")
        self.assertEqual(edge2["class"], "not run")
        self.assertIn("vCPU 0 was not run", edge2["where"])
        self.assertTrue(all(stop["host"].startswith("b28cb764")
                            for stop in self.stops("S1", "tok-edge-2")))
        after = self.stops("M1-A", "tok-edge-2")
        self.assertTrue(after)
        self.assertTrue(all(stop["host"].startswith("06b7abc1") for stop in after))
        self.assertNotIn("interrupts off", self.text)

    def test_the_soft_lockups_of_the_old_tok_edge_2(self):
        lockups = self.report["lockups"]
        self.assertEqual(sorted(lockups), ["tok-edge-2"])
        self.assertEqual(len(lockups["tok-edge-2"]), 9)
        self.assertTrue(all(item["host"].startswith("b28cb764") for item in lockups["tok-edge-2"]))
        self.assertIn("tok-edge-2 9 on host b28cb764", self.report["rules"]["H2"]["line"])
        self.assertIn("zeroing a fresh huge page on first touch 2",
                      self.report["rules"]["H2"]["line"])

    def test_rules(self):
        rules = self.report["rules"]
        self.assertEqual(rules["H1"]["stopped"], ["lon-edge-2", "lon-edge-4", "tok-edge-2"])
        self.assertTrue(rules["H4"]["fires"])
        self.assertIn("MATTERS", rules["H4"]["line"])
        self.assertEqual(rules["H4"]["with"], 0.0)
        self.assertIn("b28cb764 -> 06b7abc1", rules["H2"]["line"])
        self.assertFalse(rules["H5"]["fires"])


if __name__ == "__main__":
    unittest.main()
