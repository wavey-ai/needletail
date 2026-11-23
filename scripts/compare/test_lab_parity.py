#!/usr/bin/env python3
"""Tests of lab-parity.py: its plan and the comparison's arithmetic; nothing
is started. python3 -m unittest scripts/compare/test_lab_parity.py"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("lab_parity", HERE / "lab-parity.py")
parity = importlib.util.module_from_spec(_spec)
sys.modules["lab_parity"] = parity
_spec.loader.exec_module(parity)

KEYS = ("opus-1@lon-readers", "pcm-1@new-readers")


def clean(p99s: list[float], gap: float = 3.0, missing: int = 0) -> list[dict]:
    return [{"p99_ms": p99, "max_ms": p99 + 2, "gap_ms": gap, "missing": missing}
            for p99 in p99s]


def run(status: str = "pass", passed=True, missing: int = 0, share: float = 1.0) -> dict:
    return {"status": status, "directory": "d", "interference": {"outside_share_pct": share},
            "labels": {"roll.edges": {key: {"pass": passed, "missing": missing}
                                      for key in KEYS}}}


def layout(p99s=(7.0, 7.05, 7.1), gap=3.0, share=1.0) -> dict:
    return {"clean": {key: clean(list(p99s), gap) for key in KEYS},
            "changes": {scenario: {"change": [run(share=share) for _ in range(3)],
                                   "null": [run(share=share) for _ in range(3)]}
                        for scenario in parity.CHANGES}}


def agreeing() -> dict:
    return {"Q0": layout(), "Q1": layout(share=2.5), "Q2": layout(p99s=(7.1, 7.15, 7.2)),
            "Q3": layout(gap=4.0)}


class Comparison(unittest.TestCase):
    def test_layouts_that_agree_pass_and_q1_gives_the_floor(self):
        verdict = parity.compare(agreeing())
        self.assertEqual(verdict["failures"], [])
        self.assertTrue(verdict["pass"])
        self.assertTrue(verdict["two_labs_for_gated_proofs"])
        self.assertEqual(verdict["floor_pct"], 2.5)
        row = verdict["figures"]["opus-1@lon-readers"]["Q2"]
        self.assertEqual(row, {"p99_median_ms": 7.15, "p99_spread_ms": 0.1, "max_ms": 9.2,
                               "gap_ms": 3.0, "repeats": 3})
        self.assertEqual(verdict["verdicts"]["Q1"]["roll-edges/null/pcm-1@new-readers"], [3, 3])

    def test_the_median_p99_is_held_to_0_15_ms(self):
        results = agreeing()
        # 7.05 + 0.15 is within the bound; 0.16 is not.
        results["Q2"]["clean"]["opus-1@lon-readers"] = clean([7.2, 7.2, 1.0])
        self.assertTrue(parity.compare(results)["pass"])
        results["Q3"]["clean"]["opus-1@lon-readers"] = clean([7.21, 7.21, 7.0])
        failures = parity.compare(results)["failures"]
        self.assertEqual(len(failures), 1)
        self.assertIn("median p99 7.21 ms in Q3 against 7.05 ms in Q1", failures[0])
        results = agreeing()
        results["Q0"]["clean"]["pcm-1@new-readers"] = clean([6.8, 6.8, 6.8])
        self.assertIn("in Q1 against 6.8 ms in Q0", parity.compare(results)["failures"][0])

    def test_the_steady_gap_may_grow_by_one_unit(self):
        results = agreeing()
        results["Q2"]["clean"]["pcm-1@new-readers"] = clean([7.1, 7.1, 7.1], gap=4.0)
        self.assertTrue(parity.compare(results)["pass"])
        results["Q2"]["clean"]["pcm-1@new-readers"] = clean([7.1, 7.1, 7.1], gap=4.001)
        failures = parity.compare(results)["failures"]
        self.assertEqual(len(failures), 1)
        self.assertIn("steady longest gap 4.001 ms in Q2 against 3 ms in Q1", failures[0])
        self.assertEqual(parity.unit_ms("opus-1@x"), 1.0)
        self.assertEqual(parity.unit_ms("opus-2.5@x"), 2.5)

    def test_missing_anywhere_or_a_failed_null_fails(self):
        results = agreeing()
        results["Q0"]["clean"]["opus-1@lon-readers"][1]["missing"] = 2
        results["Q3"]["changes"]["roll-edges"]["change"][0] = run(missing=1)
        failures = parity.compare(results)["failures"]
        self.assertTrue(any("Q0 opus-1@lon-readers: 2 missing" in item for item in failures))
        self.assertTrue(any("Q3 roll-edges change roll.edges" in item for item in failures))
        results = agreeing()
        results["Q0"]["changes"]["roll-forwarders"]["null"][2] = run("fail", passed=False)
        failures = parity.compare(results)["failures"]
        self.assertTrue(any("Q0 roll-forwarders: a null run was fail" in item for item in failures))

    def test_the_verdicts_must_be_the_same_in_q1_q2_and_q3(self):
        results = agreeing()
        # Q0 is not held to the verdicts of the shared layouts.
        results["Q0"]["changes"]["roll-edges"]["change"][0] = run("fail", passed=False)
        self.assertTrue(parity.compare(results)["pass"])
        results["Q3"]["changes"]["roll-edges"]["change"][0] = run("fail", passed=False)
        failures = parity.compare(results)["failures"]
        self.assertEqual(len(failures), 2)
        self.assertIn("roll-edges/change/opus-1@lon-readers: change-window.py passed [2, 3] in "
                      "Q3 against [3, 3] in Q1", failures)
        results["Q2"]["changes"]["roll-edges"]["change"].pop()
        self.assertTrue(any("in Q2" in item for item in parity.compare(results)["failures"]))

    def test_a_spread_q1_sends_gated_proofs_to_one_lab(self):
        results = agreeing()
        results["Q1"]["clean"]["opus-1@lon-readers"] = clean([7.0, 7.05, 7.16])
        verdict = parity.compare(results)
        self.assertEqual(verdict["q1_spreads"], ["opus-1@lon-readers"])
        self.assertFalse(verdict["two_labs_for_gated_proofs"])
        self.assertTrue(any("one lab on cpus=2-29" in line for line in parity.report_lines(verdict)))

    def test_a_layout_not_run_is_not_compared(self):
        results = {"Q1": layout(), "Q2": layout()}
        verdict = parity.compare(results)
        self.assertTrue(verdict["pass"])
        self.assertIsNotNone(verdict["floor_pct"])
        self.assertIsNone(parity.compare({"Q0": layout()})["floor_pct"])


class Figures(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def test_the_figures_of_a_clean_pass(self):
        hosts = {"lon-readers": {"unit_latency_ms": {"p50": 6.0, "p99": 7.1, "max": 9.0},
                                 "missing": 0}}
        (self.dir / "hops.json").write_text(json.dumps({"udp_clients": {"hosts": hosts}}))
        base = 1_763_900_000_000_000

        def arrivals(gaps: dict[int, int]) -> list[int]:
            """10 s of a part a ms, with a silence of gaps[ms] us at ms."""
            out, t = [], base
            for n in range(10_000):
                t += gaps.get(n, 1_000)
                out.append(t)
            return out
        # A 40 ms silence in the first 5 s is the start-up, not the steady
        # state; the steady longest gaps are 3 ms and 4 ms, the host's 4 ms.
        for index, gaps in enumerate(({1000: 40_000, 7000: 3_000}, {8000: 4_000})):
            lines = ["part,arrival_unix_us,bytes"] + [f"{n},{t},288" for n, t in
                                                      enumerate(arrivals(gaps))]
            (self.dir / f"lon-readers.uc{index}.arrivals.csv").write_text("\n".join(lines) + "\n")
        figures = parity.clean_figures(self.dir, "opus-1")
        self.assertEqual(figures, {"opus-1@lon-readers": {"p99_ms": 7.1, "max_ms": 9.0,
                                                          "gap_ms": 4.0, "missing": 0}})
        self.assertEqual(parity.clean_figures(self.dir / "nothing", "opus-1"), {})
        self.assertIsNone(parity.steady_gap_ms(self.dir / "nothing.csv"))

    def test_the_runs_of_a_scenario_are_read_by_kind(self):
        scenario = self.dir / "parity-q1-roll-edges"
        for name, kind in (("change-01", "change"), ("null-01", "null"), ("change-02", "change")):
            (scenario / name).mkdir(parents=True)
            (scenario / name / "run.json").write_text(json.dumps(
                {"kind": kind, "status": "pass", "labels": {}, "directory": name}))
        (scenario / "change-03").mkdir()
        (scenario / "change-03" / "run.json").write_text(json.dumps({"kind": "change"}))
        figures = parity.change_figures(scenario)
        self.assertEqual([item["directory"] for item in figures["change"]],
                         ["change-01", "change-02"])
        self.assertEqual(len(figures["null"]), 1)


class Plan(unittest.TestCase):
    def test_k_in_every_layout(self):
        steps = parity.plan(list(parity.LAYOUTS), 3)
        self.assertEqual(sorted(steps["Q2"]["labs"]), ["a", "b"])
        self.assertEqual(sorted(steps["Q3"]["labs"]), ["a"])
        jobs = steps["Q1"]["labs"]["a"]
        self.assertEqual(len(jobs), 3 * 6)
        first = jobs[0]["command"]
        self.assertTrue(first[0].endswith("latency-bench.py"))
        for flag, value in (("--name", "parity-q1-r1-opus-1"), ("--payload", "opus"),
                            ("--seconds", "40"), ("--udp-clients", "2"),
                            ("--load-listeners", "40"), ("--set", "cpus=2-15")):
            self.assertEqual(first[first.index(flag) + 1], value)
        runs = [job["command"] for job in jobs[:6] if job["command"][0].endswith("change-run.py")]
        self.assertEqual(len(runs), 4)
        self.assertEqual([("--dry" in command) for command in runs], [False, True, False, True])
        self.assertEqual(runs[0][runs[0].index("--") + 3:],
                         ["roll", "edges", "--sites", "london"])
        self.assertEqual(steps["Q0"]["labs"]["a"][2]["command"][
            steps["Q0"]["labs"]["a"][2]["command"].index("--set") + 1], "node_cpus=0")
        self.assertTrue(all(job["lab"] == "b" and "cpus=16-29" in job["command"]
                            for job in steps["Q2"]["labs"]["b"]))
        load = steps["Q3"]["load"]
        self.assertEqual(load["up"]["command"][1:], ["up", "cpus=16-29"])
        burned = [job["command"][2] for job in load["round"] if "burn" in job["command"]]
        self.assertIn("lon-edge-1", burned)
        self.assertNotIn("lon-dist-c", burned)
        self.assertNotIn("relay-primary", burned)
        video = load["round"][-1]["command"]
        self.assertEqual(video[video.index("--video-mbps") + 1], "247")
        self.assertIn("--no-up", video)

    def test_plan_and_compare_run_nothing(self):
        with mock.patch.object(parity, "execute", side_effect=AssertionError("ran")), \
                mock.patch.object(parity, "OUT", pathlib.Path(tempfile.mkdtemp())), \
                mock.patch.object(parity, "collect", lambda layouts, repeats:
                                  {key: copy.deepcopy(value) for key, value in agreeing().items()
                                   if key in layouts}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(parity.main(["--plan", "--layouts", "Q1", "--repeats", "1"]), 0)
            self.assertEqual(list(json.loads(out.getvalue())), ["Q1"])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(parity.main(["--compare"]), 0)
            self.assertIn("the layouts agree", out.getvalue())
            floor = json.loads((parity.OUT / "floor.json").read_text())
            self.assertEqual(floor["outside_share_pct"], 2.5)
            self.assertTrue((parity.OUT / "report.txt").is_file())
            with self.assertRaises(SystemExit):
                parity.main(["--layouts", "Q9", "--plan"])


if __name__ == "__main__":
    unittest.main()
