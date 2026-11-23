#!/usr/bin/env python3
"""Tests of lab-parity.py: its plan, how it runs a layout (the jobs faked)
and the comparison's arithmetic; nothing is started.
python3 -m unittest scripts/compare/test_lab_parity.py"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("lab_parity", HERE / "lab-parity.py")
parity = importlib.util.module_from_spec(_spec)
sys.modules["lab_parity"] = parity
_spec.loader.exec_module(parity)

KEYS = ("opus-1@lon-readers", "opus-1@new-readers", "pcm-1@lon-readers", "pcm-1@new-readers")


def clean(p99s: list[float], gap: float = 3.0, missing: int = 0) -> list[dict]:
    return [{"p99_ms": p99, "max_ms": p99 + 2, "gap_ms": gap, "missing": missing}
            for p99 in p99s]


def run(status: str = "pass", passed=True, missing: int = 0, share: float = 1.0,
        reasons=()) -> dict:
    return {"status": status, "directory": "d", "reasons": list(reasons),
            "interference": {"outside_share_pct": share},
            "labels": {"roll.edges": {key: {"pass": passed, "missing": missing}
                                      for key in KEYS}}}


def layout(p99s=(7.0, 7.05, 7.1), gap=3.0, share=1.0) -> dict:
    return {"run_id": "r1", "repeats": 3, "other_labs": [], "load": {},
            "jobs": [{"command": ["latency-bench.py"], "lab": "a", "judge_exit": True,
                      "exit": 0},
                     {"command": ["change-run.py"], "lab": "a", "judge_exit": False,
                      "exit": 1}],
            "clean": {key: clean(list(p99s), gap) for key in KEYS},
            "changes": {scenario: {"change": [run(share=share) for _ in range(3)],
                                   "null": [run(share=share) for _ in range(3)]}
                        for scenario in parity.CHANGES}}


def agreeing() -> dict:
    out = {"Q0": layout(), "Q1": layout(share=2.5), "Q2": layout(p99s=(7.1, 7.15, 7.2)),
           "Q3": layout(gap=4.0)}
    out["Q3"]["load"] = {"burn lon-edge-1": {"command": [], "runs": [[0, 60, 0]], "share": 0.98}}
    return out


class Comparison(unittest.TestCase):
    def test_layouts_that_agree_pass_and_q1_gives_the_floor(self):
        verdict = parity.compare(agreeing(), 3)
        self.assertEqual(verdict["failures"], [])
        self.assertTrue(verdict["pass"])
        self.assertTrue(verdict["two_labs_for_gated_proofs"])
        self.assertEqual(verdict["floor_pct"], 2.5)
        self.assertEqual(verdict["q3_load_share"], {"burn lon-edge-1": 0.98})
        row = verdict["figures"]["opus-1@lon-readers"]["Q2"]
        self.assertEqual(row, {"p99_median_ms": 7.15, "p99_spread_ms": 0.1, "max_ms": 9.2,
                               "gap_ms": 3.0, "repeats": 3})
        self.assertEqual(verdict["verdicts"]["Q1"]["roll-edges/null/pcm-1@new-readers"], [3, 3])
        self.assertIn("Q3, share of K's time lab b's load ran: burn lon-edge-1 98%",
                      parity.report_lines(verdict))

    def test_the_median_p99_is_held_to_0_15_ms(self):
        results = agreeing()
        # 7.05 + 0.15 is within the bound; 0.16 is not.
        results["Q2"]["clean"]["opus-1@lon-readers"] = clean([7.2, 7.2, 1.0])
        self.assertTrue(parity.compare(results, 3)["pass"])
        results["Q3"]["clean"]["opus-1@lon-readers"] = clean([7.21, 7.21, 7.0])
        failures = parity.compare(results, 3)["failures"]
        self.assertEqual(len(failures), 1)
        self.assertIn("median p99 7.21 ms in Q3 against 7.05 ms in Q1", failures[0])
        results = agreeing()
        results["Q0"]["clean"]["pcm-1@new-readers"] = clean([6.8, 6.8, 6.8])
        self.assertIn("in Q1 against 6.8 ms in Q0", parity.compare(results, 3)["failures"][0])

    def test_the_steady_gap_may_grow_by_one_unit(self):
        results = agreeing()
        results["Q2"]["clean"]["pcm-1@new-readers"] = clean([7.1, 7.1, 7.1], gap=4.0)
        self.assertTrue(parity.compare(results, 3)["pass"])
        results["Q2"]["clean"]["pcm-1@new-readers"] = clean([7.1, 7.1, 7.1], gap=4.001)
        failures = parity.compare(results, 3)["failures"]
        self.assertEqual(len(failures), 1)
        self.assertIn("steady longest gap 4.001 ms in Q2 against 3 ms in Q1", failures[0])
        self.assertEqual(parity.unit_ms("opus-1@x"), 1.0)
        self.assertEqual(parity.unit_ms("opus-2.5@x"), 2.5)

    def test_missing_anywhere_or_a_failed_null_fails(self):
        results = agreeing()
        results["Q0"]["clean"]["opus-1@lon-readers"][1]["missing"] = 2
        results["Q3"]["changes"]["roll-edges"]["change"][0] = run(missing=1)
        failures = parity.compare(results, 3)["failures"]
        self.assertTrue(any("Q0 opus-1@lon-readers: 2 missing" in item for item in failures))
        self.assertTrue(any("Q3 roll-edges change roll.edges" in item for item in failures))
        results = agreeing()
        results["Q0"]["changes"]["roll-forwarders"]["null"][2] = run("fail", passed=False)
        failures = parity.compare(results, 3)["failures"]
        self.assertTrue(any("Q0 roll-forwarders: a null run was fail" in item for item in failures))

    def test_the_verdicts_must_be_the_same_in_q1_q2_and_q3(self):
        results = agreeing()
        # Q0 is not held to the verdicts of the shared layouts.
        results["Q0"]["changes"]["roll-edges"]["change"][0] = run("fail", passed=False)
        self.assertTrue(parity.compare(results, 3)["pass"])
        results["Q3"]["changes"]["roll-edges"]["change"][0] = run("fail", passed=False)
        failures = parity.compare(results, 3)["failures"]
        self.assertEqual(len(failures), 4)
        self.assertIn("roll-edges/change/opus-1@lon-readers: change-window.py passed [2, 3] in "
                      "Q3 against [3, 3] in Q1", failures)

    def test_a_spread_q1_sends_gated_proofs_to_one_lab(self):
        results = agreeing()
        results["Q1"]["clean"]["opus-1@lon-readers"] = clean([7.0, 7.05, 7.16])
        verdict = parity.compare(results, 3)
        self.assertEqual(verdict["q1_spreads"], ["opus-1@lon-readers"])
        self.assertFalse(verdict["two_labs_for_gated_proofs"])
        self.assertTrue(any("one lab on cpus=2-29" in line for line in parity.report_lines(verdict)))

    def assert_not_measured(self, results: dict, text: str, floor: bool = True) -> None:
        verdict = parity.compare(results, 3)
        self.assertFalse(verdict["pass"])
        self.assertFalse(verdict["two_labs_for_gated_proofs"])
        self.assertTrue(any(text in item for item in verdict["failures"]),
                        (text, verdict["failures"]))
        self.assertEqual(verdict["floor_pct"] is not None, floor)
        self.assertIn("gated proofs run in one lab until the four layouts are measured and agree",
                      parity.report_lines(verdict))

    def test_what_was_not_measured_fails(self):
        # Nothing at all.
        verdict = parity.compare({}, 3)
        self.assertFalse(verdict["pass"])
        self.assertEqual(len(verdict["failures"]), 4)
        self.assertIsNone(verdict["floor_pct"])
        # A layout not run, alone or with others.
        self.assert_not_measured({"Q1": layout(), "Q2": layout()},
                                 "Q0: not measured (lab-parity.py --layouts Q0)")
        results = agreeing()
        del results["Q3"]
        self.assert_not_measured(results, "Q3: not measured")
        # A clean pass that left nothing (latency-bench wrote elsewhere).
        results = agreeing()
        results["Q1"]["clean"].pop("pcm-1@lon-readers")
        self.assert_not_measured(results, "Q1 pcm-1@lon-readers: not measured: 0 of 3 clean "
                                          "passes", floor=False)
        # A change-run job that left no run (a refused up, a missing tool).
        results = agreeing()
        results["Q2"]["changes"]["roll-forwarders"]["null"] = []
        self.assert_not_measured(results, "Q2 roll-forwarders null: not measured: 0 of 3 "
                                          "scored runs")
        # A void, unscored or faulty run is no measurement.
        for status, reasons in (("void", ["void: the outside share was 9%"]),
                                ("unscored", []), ("interrupted", ["interrupted"]),
                                ("fail", ["the command exited 3; the scenario expects 0"])):
            results = agreeing()
            results["Q1"]["changes"]["roll-edges"]["change"][1] = run(status, reasons=reasons)
            self.assert_not_measured(results, f"Q1 roll-edges change: d was {status}",
                                     floor=False)
        # A clean pass or a load job that exited non-zero.
        results = agreeing()
        results["Q2"]["jobs"].append({"command": ["latency-bench.py", "--name", "x"], "lab": "b",
                                      "judge_exit": True, "exit": 2})
        self.assert_not_measured(results, "Q2 lab b: `latency-bench.py --name x` exited 2")
        results = agreeing()
        results["Q3"]["load"]["burn lon-edge-1"]["runs"].append([60, 61, 1])
        self.assert_not_measured(results, "Q3 lab b's load: burn lon-edge-1 exited 1")
        # Another lab on the host while Q1 measured its floor.
        results = agreeing()
        results["Q1"]["other_labs"] = ["x (someone, worktree /w)"]
        self.assert_not_measured(results, "Q1: lab x (someone, worktree /w) ran on the host",
                                 floor=False)
        # Another number of repeats.
        results = agreeing()
        self.assertTrue(any("not measured with --repeats 2" in item
                            for item in parity.compare(results, 2)["failures"]))


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
        scenario = self.dir / "parity-r1-q1-roll-edges"
        for name, kind in (("change-01", "change"), ("null-01", "null"), ("change-02", "change")):
            (scenario / name).mkdir(parents=True)
            (scenario / name / "run.json").write_text(json.dumps(
                {"kind": kind, "status": "pass", "labels": {}, "directory": name}))
        (scenario / "change-03").mkdir()
        (scenario / "change-03" / "run.json").write_text(json.dumps({"kind": "change"}))
        figures = parity.change_figures(scenario)
        self.assertEqual([item["directory"] for item in figures["change"]],
                         ["change-01", "change-02", None])
        # A run.json with no status: its runner was killed.
        self.assertEqual(figures["change"][2]["status"], "interrupted")
        self.assertEqual(len(figures["null"]), 1)

    def test_only_the_recorded_invocation_of_a_layout_is_read(self):
        changes = self.dir / "change-run"
        for run_id, status in (("r1", "fail"), ("r2", "pass")):
            for scenario in parity.CHANGES:
                directory = changes / "a" / parity.change_name(run_id, "Q1", scenario) / "null-01"
                directory.mkdir(parents=True)
                (directory / "run.json").write_text(json.dumps(
                    {"kind": "null", "status": status, "labels": {}, "directory": "x"}))
        with mock.patch.object(parity, "CHANGE_RUNS", changes), \
                mock.patch.object(parity.bench, "OUT_ROOT", self.dir / "latency-bench"):
            results = parity.collect({"Q1": {"run_id": "r2", "repeats": 1, "jobs": []}})
        self.assertEqual([run["status"] for run in
                          results["Q1"]["changes"]["roll-edges"]["null"]], ["pass"])
        self.assertEqual(results["Q1"]["run_id"], "r2")


class Running(unittest.TestCase):
    def setUp(self):
        self.ran: list = []
        self.lock = threading.Lock()

    def execute(self, job: dict) -> int:
        with self.lock:
            self.ran.append((time.time(), job["lab"], job["command"]))
        command = job["command"]
        if "burn" in command:
            time.sleep(0.03)
        elif any(item.endswith("latency-bench.py") for item in command):
            time.sleep(0.15)
        elif any(item.endswith("change-run.py") for item in command):
            time.sleep(0.05)
        return 0

    def test_q3_burns_every_node_again_as_it_ends_until_k_is_done(self):
        steps = parity.plan(["Q3"], 1, "r1")["Q3"]
        with mock.patch.object(parity, "execute", self.execute):
            record = parity.run_layout("Q3", steps)
        k_end = max(t for t, ident, command in self.ran
                    if ident == "a" and not any(item.endswith("edge_lab.py") for item in command))
        burns = {name: item for name, item in record["load"].items() if name.startswith("burn")}
        self.assertIn("burn lon-edge-1", burns)
        for name, item in burns.items():
            # Each node's burn ran again and again for the whole of K.
            self.assertGreater(len(item["runs"]), 2, name)
            self.assertGreaterEqual(item["runs"][-1][1], k_end, name)
            self.assertGreater(item["share"], 0.8, name)
        self.assertGreater(len(record["load"]["4k"]["runs"]), 1)
        self.assertEqual([job["command"][1] for job in record["jobs"]
                          if job["command"][0].endswith("edge_lab.py")], ["up", "down"])
        self.assertEqual(len([job for job in record["jobs"] if job["lab"] == "a"]), 6)

    def test_a_failing_burn_is_not_run_again(self):
        def execute(job):
            return 3 if "burn" in job["command"] else self.execute(job)
        steps = parity.plan(["Q3"], 1, "r1")["Q3"]
        with mock.patch.object(parity, "execute", execute):
            record = parity.run_layout("Q3", steps)
        self.assertEqual(record["load"]["burn lon-edge-1"]["runs"][0][2], 3)
        self.assertEqual(len(record["load"]["burn lon-edge-1"]["runs"]), 1)

    def test_the_covered_share(self):
        self.assertEqual(parity.covered_share([[0, 4, 0], [5, 12, 0], [3, 6, 0]], 2, 10), 1.0)
        self.assertEqual(parity.covered_share([[0, 4, 0], [6, 8, 0]], 2, 10), 0.5)
        self.assertEqual(parity.covered_share([], 2, 10), 0.0)

    def test_q0_and_q1_refuse_another_lab_and_record_one_that_came(self):
        out = pathlib.Path(tempfile.mkdtemp())
        entry = {"id": "x", "owner": "someone", "worktree": "/w"}
        with mock.patch.object(parity, "OUT", out), \
                mock.patch.object(parity, "execute", self.execute), \
                mock.patch.object(parity.lab, "registered_labs", lambda: [entry]):
            with self.assertRaises(SystemExit) as refused:
                parity.main(["--layouts", "Q1", "--repeats", "1"])
        self.assertIn("x (someone, worktree /w) is registered", str(refused.exception))
        self.assertEqual(self.ran, [])
        seen = iter([[], [entry]])
        with mock.patch.object(parity, "OUT", out), \
                mock.patch.object(parity, "execute", self.execute), \
                mock.patch.object(parity, "collect", lambda state: {}), \
                mock.patch.object(parity.lab, "registered_labs", lambda: next(seen)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(parity.main(["--layouts", "Q1", "--repeats", "1"]), 1)
        state = json.loads((out / "layouts.json").read_text())
        self.assertEqual(state["Q1"]["other_labs"], ["x (someone, worktree /w)"])
        self.assertEqual(state["Q1"]["repeats"], 1)
        self.assertTrue(state["Q1"]["run_id"])


class Plan(unittest.TestCase):
    def test_k_in_every_layout(self):
        steps = parity.plan(list(parity.LAYOUTS), 3, "r1")
        self.assertEqual(sorted(steps["Q2"]["labs"]), ["a", "b"])
        self.assertEqual(sorted(steps["Q3"]["labs"]), ["a"])
        jobs = steps["Q1"]["labs"]["a"]
        self.assertEqual(len(jobs), 3 * 6)
        first = jobs[0]["command"]
        self.assertTrue(first[0].endswith("latency-bench.py"))
        self.assertTrue(jobs[0]["judge_exit"])
        for flag, value in (("--name", "parity-r1-q1-r1-opus-1"), ("--payload", "opus"),
                            ("--seconds", "40"), ("--udp-clients", "2"),
                            ("--load-listeners", "40"), ("--set", "cpus=2-15")):
            self.assertEqual(first[first.index(flag) + 1], value)
        runs = [job for job in jobs[:6] if job["command"][0].endswith("change-run.py")]
        self.assertEqual(len(runs), 4)
        self.assertFalse(any(job["judge_exit"] for job in runs))
        runs = [job["command"] for job in runs]
        self.assertEqual([("--dry" in command) for command in runs], [False, True, False, True])
        self.assertEqual([command[command.index("--name") + 1] for command in runs],
                         ["parity-r1-q1-roll-edges"] * 2 + ["parity-r1-q1-roll-forwarders"] * 2)
        # roll-forwarders' probes leave CW its windows after a two-minute roll.
        self.assertEqual([command[command.index("--seconds") + 1] for command in runs],
                         ["360", "360", "480", "480"])
        self.assertEqual(runs[0][runs[0].index("--") + 3:],
                         ["roll", "edges", "--sites", "london"])
        self.assertEqual(steps["Q0"]["labs"]["a"][2]["command"][
            steps["Q0"]["labs"]["a"][2]["command"].index("--set") + 1], "node_cpus=0")
        self.assertTrue(all(job["lab"] == "b" and "cpus=16-29" in job["command"]
                            for job in steps["Q2"]["labs"]["b"]))
        load = steps["Q3"]["load"]
        self.assertEqual(load["up"]["command"][1:], ["up", "cpus=16-29"])
        burned = [job["command"][2] for job in load["loop"] if "burn" in job["command"]]
        self.assertIn("lon-edge-1", burned)
        self.assertNotIn("lon-dist-c", burned)
        self.assertNotIn("relay-primary", burned)
        video = load["loop"][-1]["command"]
        self.assertEqual(video[video.index("--video-mbps") + 1], "247")
        self.assertIn("--no-up", video)

    def test_plan_and_compare_run_nothing(self):
        with mock.patch.object(parity, "execute", side_effect=AssertionError("ran")), \
                mock.patch.object(parity, "OUT", pathlib.Path(tempfile.mkdtemp())), \
                mock.patch.object(parity, "collect", lambda state:
                                  {key: copy.deepcopy(value) for key, value in agreeing().items()}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(parity.main(["--plan", "--layouts", "Q1", "--repeats", "1"]), 0)
            self.assertEqual(list(json.loads(out.getvalue())), ["Q1"])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(parity.main(["--compare"]), 0)
            self.assertIn("the layouts agree", out.getvalue())
            floor = json.loads((parity.OUT / "floor.json").read_text())
            self.assertEqual(floor["outside_share_pct"], 2.5)
            self.assertEqual(floor["layout"], "cpus=2-15")
            self.assertTrue((parity.OUT / "report.txt").is_file())
            with self.assertRaises(SystemExit):
                parity.main(["--layouts", "Q9", "--plan"])

    def test_an_incomplete_q1_writes_no_floor(self):
        results = agreeing()
        results["Q1"]["changes"]["roll-edges"]["null"].pop()
        with mock.patch.object(parity, "OUT", pathlib.Path(tempfile.mkdtemp())), \
                mock.patch.object(parity, "collect", lambda state: copy.deepcopy(results)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(parity.main(["--compare"]), 1)
            self.assertFalse((parity.OUT / "floor.json").exists())
        self.assertIn("floor (Q1's largest outside share): not measured", out.getvalue())


if __name__ == "__main__":
    unittest.main()
