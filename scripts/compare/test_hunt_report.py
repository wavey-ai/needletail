#!/usr/bin/env python3
"""Tests of hunt-report.py (D-hunt.md 7.4, 7.5), offline on synthetic hunt
records: the join of a silence to its cause in the fixed order and the
unexplained rest; nodes grouped by physical host and their stops at one
instant against chance (a stop of a whole node counted once); one
synthetic input for each rule R1 to R6, R1 the same whatever the nodes are
called; a run whose /api/hops pull fell inside a session is refused; a
node with unpinned tick threads has its scope marked untrusted; the first
of two edges for every pair of a region's edges; a whole report from a
synthetic run; and, when the referee's wave 7 files are on this machine,
E1's table of edge commit gaps and the first of two edges per region.
python3 -m unittest test_hunt_report.py (in scripts/compare)"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("hunt_report", HERE / "hunt-report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)
sys.path.insert(0, str(HERE.parent))
import stall_records as sr  # noqa: E402

T = 1_795_384_000_000_000          # us
S = T / 1e6                        # s
MS = 1000
# The referee's worktree: NEEDLETAIL_COMPARE_ROOT, else beside this one.
WAVE7 = (pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT",
                                     str(HERE.parents[2] / "needletail-compare")))
         / "target" / "compare" / "runs" / "waudio7" / "audio.opus-2.5.baseline.clean")


def tick(cpu: int, end_us: int, dur_us: int, run_delay_us: int = 10, **more) -> dict:
    return {"kind": "tick", "thread": f"tick-{cpu}", "cpu": cpu, "t_us": int(end_us),
            "dur_us": int(dur_us), "span_us": int(dur_us), "n": 1,
            "run_delay_us": run_delay_us, **more}


def sec(cpu: int, t_us: int, **ticks) -> dict:
    return {"kind": "sec", "cpu": cpu, "t_us": int(t_us), "span_us": 1_000_000,
            **{key: 0 for key in ("user", "system", "irq", "softirq", "steal")}, **ticks}


def seconds_of(start_us: int, end_us: int, cpus=(0, 1)) -> list[dict]:
    return [sec(cpu, t) for t in range(start_us, end_us, 1_000_000) for cpu in cpus]


def write_ndjson(path: pathlib.Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in items))


class Builder:
    """A hunt run's directory, as linode-stall-hunt.py leaves it."""

    def __init__(self, nodes: dict[str, dict], periods: list[dict], **record) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.record = {"schema": "needletail.stall-hunt.v1", "run_id": "hunttest",
                       "nodes": {name: {"type": "g6-dedicated-2", "site": "london", **node}
                                 for name, node in nodes.items()},
                       "periods": periods, "sessions": [], "replaced": [], "hosts": {},
                       **record}

    def node(self, name: str, file: str, items: list[dict]) -> None:
        write_ndjson(self.dir / "nodes" / name / file, items)

    def run(self):
        (self.dir / "hunt.json").write_text(json.dumps(self.record))
        return report.Run(self.dir)

    def close(self) -> None:
        self.tmp.cleanup()


def period(name: str, kind: str, start_s: float, end_s: float) -> dict:
    return {"name": name, "kind": kind, "from_unix": S + start_s, "to_unix": S + end_s}


class GapJoin(unittest.TestCase):
    def test_gap_join_names_a_cause_and_counts_the_unexplained(self):
        # 1 ms units, 2 ms mouth to ear; three holds of 20 ms at 1 s, 2 s
        # and 2.5 s (the held units arrive together at the release).
        rows = []
        for number in range(3000):
            source = T + number * MS
            arrival = source + 2 * MS
            for hold in (1000, 2000, 2500):
                if hold <= number < hold + 20:
                    arrival = T + (hold + 20) * MS + 2 * MS
            rows.append({"kind": "a", "latency_us": str(arrival - source),
                         "complete_unix_us": str(arrival), "part": str(number)})
        found = report.silences(rows)
        self.assertEqual([round(gap["ms"]) for gap in found], [21, 21, 21])
        sender = sr.sender_events([{"kind": "late", "epoch": 1000, "deadline_us": T + 1000 * MS,
                                    "woke_late_us": 19_000, "encode_us": 100,
                                    "late_first_us": 19_000, "late_last_us": 20_000}])
        edge_stop = tick(1, T + 2020 * MS, 20 * MS, cause="stopped", scope="all")
        # The edge also stopped at 1 s: the sender comes first in the order.
        edge_early = tick(1, T + 1015 * MS, 10 * MS, cause="stopped", scope="all")
        sources = sr.source_sides(sender, [], [], "contrib-lon", "lon-src")
        sources.append(("lon-edge-1 stopped", [edge_stop, edge_early]))
        joined = report.gap_join(found, sources)
        self.assertEqual(joined["silences"], 3)
        self.assertEqual(joined["by_cause"]["pacer"]["n"], 1)
        self.assertEqual(joined["by_cause"]["lon-edge-1 stopped"]["n"], 1)
        self.assertEqual(joined["unexplained"]["n"], 1)
        self.assertAlmostEqual(joined["unexplained"]["ms"], 21.0, delta=0.01)
        self.assertEqual([gap["cause"] for gap in found],
                         ["pacer", "lon-edge-1 stopped", "unexplained"])

    def test_the_2_5_ms_lane_counts_silences_of_two_spacings(self):
        rows = [{"kind": "a", "latency_us": "2000", "complete_unix_us": str(T + n * 2500 + 2000)}
                for n in range(1000)]
        rows[500]["complete_unix_us"] = str(T + 500 * 2500 + 2000 + 1000)  # 3.5 ms
        self.assertEqual(report.silences(rows), [])


class Hosts(unittest.TestCase):
    def test_host_grouping_and_simultaneity(self):
        built = Builder({"lon-edge-1": {"role": "edge", "host_uuid": "h1"},
                         "lon-edge-2": {"role": "edge", "host_uuid": "h1"},
                         "lon-dist-a": {"role": "distributor", "host_uuid": "h2"},
                         "lon-dist-b": {"role": "distributor"}},
                        [period("soak-1", "soak", 0, 600)],
                        hosts={"last": {"host_uuid": {"lon-dist-b": "h2"}}})
        try:
            base = seconds_of(T, T + 600_000_000)
            together = [T + k * 60_000_000 for k in range(1, 6)]
            built.node("lon-edge-1", "watch.ndjson",
                       base + [tick(0, end, 40 * MS) for end in together])
            built.node("lon-edge-2", "watch.ndjson",
                       base + [tick(1, end + 10 * MS, 40 * MS) for end in together])
            built.node("lon-dist-a", "watch.ndjson",
                       base + [tick(0, end + 7_000_000, 40 * MS) for end in together])
            built.node("lon-dist-b", "watch.ndjson",
                       base + [tick(0, end + 20_000_000, 40 * MS) for end in together])
            found = report.cohosting(built.run())
        finally:
            built.close()
        self.assertEqual(found["groups"], {"h1": ["lon-edge-1", "lon-edge-2"],
                                           "h2": ["lon-dist-a", "lon-dist-b"]})
        pairs = {tuple(item["pair"]): item for item in found["pairs"]}
        self.assertEqual(pairs[("lon-edge-1", "lon-edge-2")]["observed"], 5)
        self.assertTrue(pairs[("lon-edge-1", "lon-edge-2")]["above_chance"])
        self.assertEqual(pairs[("lon-dist-a", "lon-dist-b")]["observed"], 0)
        self.assertFalse(pairs[("lon-dist-a", "lon-dist-b")]["above_chance"])
        self.assertTrue(found["above_chance"])
        # Five stops of 40 ms each in 600 s: 0.0075 coincidences within
        # 50 ms by chance (5 x 5 x (100 + 40 + 40) ms / 600 s).
        self.assertAlmostEqual(pairs[("lon-edge-1", "lon-edge-2")]["expected"], 0.0075, places=4)

    def test_a_stop_of_a_whole_node_is_one_stop(self):
        """Two co-hosted 2-CPU nodes stop once together: one tick per CPU
        each. That is one coincidence, not above chance."""
        at = T + 300_000_000
        stops = {"a": sr.stops([tick(0, at, 40 * MS), tick(1, at, 40 * MS)]),
                 "b": sr.stops([tick(0, at + 4 * MS, 40 * MS), tick(1, at + 4 * MS, 40 * MS)])}
        self.assertEqual(stops["a"], [(at - 40 * MS, at)])
        found = report.simultaneity(stops, [("a", "b")], 600)[0]
        self.assertEqual(found["observed"], 1)
        self.assertFalse(found["above_chance"])
        # Overlapping ticks of different CPUs merge; apart ones stay two.
        merged = sr.stops([tick(0, at, 40 * MS), tick(1, at + 30 * MS, 40 * MS),
                           tick(0, at + 500 * MS, 25 * MS)])
        self.assertEqual(merged, [(at - 40 * MS, at + 30 * MS), (at + 475 * MS, at + 500 * MS)])


class Rules(unittest.TestCase):
    def figures(self, frozen: dict, soak: dict, session: dict) -> dict:
        return {name: {"frozen_ms": frozen[name], "soak_f": soak.get(name, 0.0),
                       "session_f": session.get(name, 0.0)} for name in frozen}

    def test_r1_the_host(self):
        names = [f"n{index}" for index in range(8)]
        frozen = {name: 10.0 for name in names}
        frozen["n3"] = 400.0
        fires = report.rule_r1(self.figures(frozen, {"n3": 30.0, "n1": 1.0},
                                            {"n3": 50.0, "n1": 2.0}), {}, [], {})
        self.assertTrue(fires["fires"], fires["line"])
        # The quarter is n3 and one of the seven tied at 10 ms.
        self.assertEqual(fires["worst"], ["n3"])
        self.assertEqual(fires["tied"], ["n0", "n1", "n2", "n4", "n5", "n6", "n7"])
        self.assertIn("n3 and 1 of n0, n1", fires["line"])
        spread = report.rule_r1(self.figures({name: 10.0 for name in names}, {"n3": 30.0},
                                             {"n3": 50.0}), {}, [], {})
        self.assertFalse(spread["fires"])
        # The soak named an instance that the sessions did not.
        unlike = report.rule_r1(self.figures(frozen, {"n5": 30.0, "n6": 20.0},
                                             {"n3": 50.0, "n0": 40, "n1": 30, "n2": 20}),
                                {}, [], {})
        self.assertFalse(unlike["fires"])
        same_host = report.rule_r1(
            self.figures(frozen, {"n3": 30.0}, {"n3": 50.0}), {"above_chance": True},
            [{"name": "n3", "host_uuid_before": "h9", "host_uuid_after": "h9"}],
            {"n3": (40.0, 35.0)})
        self.assertIn("platform matter", same_host["line"])
        self.assertIn("host rule of 2.10", same_host["line"])

    def test_r1_does_not_depend_on_names(self):
        """The review's case: n7 is the bad instance (soak F 30, session F
        50, 400 frozen ms); one other node has soak F 1 and session F 0.
        Whatever that node is called, the verdict is the same."""
        names = [f"n{index}" for index in range(8)]
        verdicts = set()
        for other in names[:-1]:
            frozen = {name: 0.0 for name in names}
            frozen["n7"] = 400.0
            figures = self.figures(frozen, {"n7": 30.0, other: 1.0}, {"n7": 50.0})
            verdicts.add(report.rule_r1(figures, {}, [], {})["fires"])
        self.assertEqual(verdicts, {False})
        # Nodes of equal value share the worst rank of their tie, and a node
        # at 0 is in no worst set.
        ranked = report.ranks({"a": 5.0, "b": 0.0, "c": 0.0, "d": 2.0})
        self.assertEqual(ranked, {"a": 1, "b": 4, "c": 4, "d": 2})
        self.assertEqual(report.worst_quarter({"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0})["worst"],
                         [])

    def touch_run(self, fresh_stops: dict[str, int], s1_f: dict[str, float]) -> Builder:
        nodes = {"lon-bare-1": {"role": "bare"}, "lon-bare-2": {"role": "bare"},
                 "tok-bare-1": {"role": "bare", "site": "tokyo"},
                 "lon-edge-1": {"role": "edge"}, "lon-edge-4": {"role": "edge"},
                 "tok-edge-1": {"role": "edge", "site": "tokyo"},
                 "lon-edge-3": {"role": "edge"}, "lon-dist-a": {"role": "distributor"}}
        built = Builder(nodes, [period("soak-1", "soak", 0, 60), period("S1", "session", 1200, 1270)],
                        arms={"warmed": ["lon-edge-3", "lon-dist-a", "lon-bare-2"]})
        marks = {"fresh": 1200, "thp-never": 1500, "again": 1800, "end": 1980}
        for name in nodes:
            items = seconds_of(T, T + 2000 * 1_000_000)
            for index in range(fresh_stops.get(name, 0)):
                items.append(tick(0, T + (1210 + index) * 1_000_000, 50 * MS))
            if s1_f.get(name):
                # F ms a node-minute in S1 (70 s, two CPUs).
                items.append(tick(0, T + 1230 * 1_000_000, s1_f[name] * 70 / 60 * 2 * MS))
            built.node(name, "watch.ndjson", items)
        for name in ("lon-bare-1", "lon-bare-2", "tok-bare-1"):
            path = built.dir / "nodes" / name / "touch-marks.txt"
            path.write_text("".join(f"{label} {S + at}\n" for label, at in marks.items()))
        return built

    def test_r2_first_touch(self):
        built = self.touch_run({"lon-bare-1": 20, "tok-bare-1": 20},
                               {"lon-edge-1": 200.0, "lon-edge-4": 150.0, "tok-edge-1": 300.0,
                                "lon-edge-3": 10.0, "lon-dist-a": 0.0})
        try:
            fires = report.rule_r2(built.run())
        finally:
            built.close()
        self.assertTrue(fires["fires"], fires["line"])
        built = self.touch_run({"lon-bare-1": 20, "tok-bare-1": 20, "lon-bare-2": 20},
                               {"lon-edge-1": 200.0, "lon-edge-3": 150.0})
        try:
            warmed_too = report.rule_r2(built.run())
        finally:
            built.close()
        self.assertFalse(warmed_too["fires"])
        self.assertFalse(warmed_too["first"])

    def test_r3_huge_pages(self):
        built = self.touch_run({}, {})
        try:
            slow = [{"kind": "touch", "t_us": T + (1250 + k) * 1_000_000, "page": k,
                     "dur_us": 4000} for k in range(10)]
            slow_never = [{"kind": "touch", "t_us": T + 1600 * 1_000_000, "page": 1,
                           "dur_us": 4000}]
            built.node("lon-bare-1", "touch-fresh.ndjson", slow)
            built.node("lon-bare-1", "touch-thp-never.ndjson", slow_never)
            fires = report.rule_r3(built.run())
        finally:
            built.close()
        self.assertTrue(fires["fires"], fires["line"])
        self.assertAlmostEqual(fires["ratios"]["lon-bare-1"], 10.0)
        self.assertIsNone(fires["ratios"]["lon-bare-2"])

    def test_r4_our_own_threads(self):
        built = Builder({"lon-edge-1": {"role": "edge"}}, [])
        try:
            built.node("lon-edge-1", "stalls.ndjson", [
                {"kind": "class_pass", "thread": "audio", "cpu": 1, "t_us": T, "dur_us": 25_000,
                 "span_us": 25_000, "n": 1, "cause": "own"},
                {"kind": "class_pass", "thread": "audio", "cpu": 1, "t_us": T + 60_000_000,
                 "dur_us": 90_000, "span_us": 90_000, "n": 1, "cause": "stopped"}])
            fires = report.rule_r4(built.run())
        finally:
            built.close()
        self.assertTrue(fires["fires"])
        self.assertEqual(len(fires["events"]), 1)
        self.assertIn("lon-edge-1 audio 25000 us", fires["line"])

    def test_r5_the_guest_kernel(self):
        built = Builder({"lon-edge-1": {"role": "edge"}}, [])
        try:
            # A sec line is written at the end of the second it covers: the
            # starved tick at 1.5 s is in the line of 2 s (kernel, with
            # compaction), not in the line of 1 s; the tick at 5.5 s is in
            # the line of 6 s (none), not in the kernel line of 5 s.
            built.node("lon-edge-1", "watch.ndjson", [
                sec(0, T + 1_000_000), sec(1, T + 1_000_000),
                sec(0, T + 2_000_000, compact_stall=3, allocstall=0),
                sec(1, T + 2_000_000, system=80),
                tick(1, T + 1_500_000, 30 * MS, run_delay_us=29_000),
                sec(0, T + 5_000_000, compact_stall=3), sec(1, T + 5_000_000, system=80),
                tick(1, T + 5_500_000, 30 * MS, run_delay_us=29_000)])
            fires = report.rule_r5(built.run())
        finally:
            built.close()
        self.assertTrue(fires["fires"])
        self.assertEqual([event["t_us"] for event in fires["events"]], [T + 1_500_000])

    def test_r6_nothing(self):
        def run_with(stop_ms: float):
            built = Builder({"lon-edge-1": {"role": "edge"}, "lon-bare-1": {"role": "bare"}},
                            [period("S1", "session", 0, 70), period("S4", "session", 1200, 1270)])
            for name in ("lon-edge-1", "lon-bare-1"):
                built.node(name, "watch.ndjson", seconds_of(T, T + 1300 * 1_000_000)
                           + [tick(0, T + 600 * 1_000_000, stop_ms * MS)])
            try:
                return report.rule_r6(built.run())
            finally:
                built.close()
        quiet = run_with(25)          # 12.5 ms over two CPUs in 21 minutes
        self.assertTrue(quiet["fires"], quiet["line"])
        loud = run_with(400)
        self.assertFalse(loud["fires"])


class Guards(unittest.TestCase):
    def test_a_hops_pull_inside_a_session_is_refused(self):
        built = Builder({"lon-edge-1": {"role": "edge"}},
                        [period("S1", "session", 0, 70), period("S2", "session", 100, 170)],
                        sessions=[{"name": "S1", "hops_pull": {"from_unix": S + 75,
                                                               "to_unix": S + 80}},
                                  {"name": "S2", "hops_pull": {"from_unix": S + 160,
                                                               "to_unix": S + 190}}])
        try:
            run = built.run()
            self.assertEqual(len(report.hops_inside_sessions(run)), 1)
            with self.assertRaises(report.Refused) as refused:
                report.build(run, [8])
        finally:
            built.close()
        self.assertIn("after S2", str(refused.exception))
        self.assertIn("inside S2", str(refused.exception))

    def test_unpinned_tick_threads_make_the_scope_untrusted(self):
        built = Builder({"lon-edge-1": {"role": "edge"}, "lon-edge-2": {"role": "edge"}},
                        [period("soak-1", "soak", 0, 60), period("S1", "session", 100, 170)])
        try:
            for name in ("lon-edge-1", "lon-edge-2"):
                built.node(name, "watch.ndjson", seconds_of(T, T + 200 * 1_000_000)
                           + [tick(0, T + 120_000_000, 30 * MS)])
            built.node("lon-edge-1", "mesh-local.ndjson", [
                {"unix": S + 1, "body": {"stall": {"tick_threads": 2, "tick_pinned": 2}}},
                {"unix": S + 6, "body": {"stall": {"tick_threads": 2, "tick_pinned": 1}}}])
            built.node("lon-edge-2", "mesh-local.ndjson", [
                {"unix": S + 1, "body": {"stall": {"tick_threads": 2, "tick_pinned": 2}}}])
            rows = report.node_periods(built.run())
        finally:
            built.close()
        trusted = {(row["node"], row["scope_trusted"]) for row in rows}
        self.assertEqual(trusted, {("lon-edge-1", False), ("lon-edge-2", True)})
        # The time between the soak and the session is a period of its own.
        periods = [row["period"] for row in rows if row["node"] == "lon-edge-1"]
        self.assertEqual(periods, ["soak-1", "after soak-1", "S1", "run"])

    def test_the_first_of_two_for_every_pair_of_edges(self):
        """Three edges of a region in the probes: each pair is reported; a
        stop of one edge is hidden in the pairs that hold another."""
        published = {part: T + part * MS for part in range(100)}
        commits = {edge: {part: at + 300 for part, at in published.items()}
                   for edge in ("lon-edge-1", "lon-edge-2", "lon-edge-3")}
        for part in range(40, 60):
            commits["lon-edge-1"][part] = T + 60 * MS + 300
        pairs = report.first_of_pairs(commits, published, None, None)
        self.assertEqual(sorted(pairs["lon"]), ["lon-edge-1+lon-edge-2", "lon-edge-1+lon-edge-3",
                                                "lon-edge-2+lon-edge-3"])
        self.assertEqual({pair: figure["max_ms"] for pair, figure in pairs["lon"].items()},
                         {"lon-edge-1+lon-edge-2": 0.3, "lon-edge-1+lon-edge-3": 0.3,
                          "lon-edge-2+lon-edge-3": 0.3})
        self.assertEqual({pair: figure["parts"] for pair, figure in pairs["lon"].items()},
                         {pair: 100 for pair in pairs["lon"]})
        # One edge alone has no pair.
        alone = report.first_of_pairs({"tok-edge-1": commits["lon-edge-1"]}, published,
                                         None, None)
        self.assertEqual(alone, {"tok": {}})


class WholeReport(unittest.TestCase):
    def test_a_report_of_a_small_run(self):
        built = Builder({"contrib-lon": {"role": "contributor", "type": "g6-dedicated-4"},
                         "lon-src": {"role": "load", "type": "g6-dedicated-4"},
                         "lon-view": {"role": "load", "type": "g6-dedicated-4"},
                         "lon-dist-a": {"role": "distributor", "host_uuid": "h1"},
                         "lon-edge-1": {"role": "edge", "host_uuid": "h1"}},
                        [period("soak-1", "soak", 0, 60), period("S1", "session", 100, 170)],
                        sessions=[{"name": "S1", "config": "opus-1"}],
                        arms={"warmed": ["lon-dist-a"], "control": {}})
        try:
            for name in built.record["nodes"]:
                built.node(name, "watch.ndjson", seconds_of(T, T + 200 * 1_000_000))
            built.node("lon-edge-1", "stalls.ndjson",
                       [tick(0, T + 120_000_000, 300 * MS, cause="stopped", scope="all")])
            start = int(S) + 90
            session = built.dir / "sessions" / "S1"
            (session / "lon-src").mkdir(parents=True)
            (session / "lon-src" / "source.start").write_text(f"{start}\n")
            write_ndjson(session / "lon-src" / "sender.ndjson", [
                {"kind": "late", "epoch": 20_000, "deadline_us": start * 1_000_000 + 20_001_000,
                 "woke_late_us": 3000, "encode_us": 90, "late_first_us": 3000,
                 "late_last_us": 3100}])
            (session / "lon-view").mkdir(parents=True)
            with open(session / "lon-view" / "uc-1.probe.csv", "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["kind", "index", "latency_us", "complete_unix_us",
                                 "first_unix_us", "part"])
                for part in range(10_000, 80_000):
                    source = start * 1_000_000 + (part + 1) * 1000
                    delay = 2000 + (3000 if 20_000 <= part < 20_003 else 0)
                    writer.writerow(["a", part, delay, source + delay, part, part])
            with open(session / "lon-view" / "uc-1.arrivals.csv", "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["part", "arrival_unix_us", "bytes", "published_unix_us",
                                 "available_unix_us", "kernel_unix_us"])
                for part in range(10_000, 80_000):
                    source = start * 1_000_000 + (part + 1) * 1000
                    late = 3000 if 20_000 <= part < 20_003 else 0
                    writer.writerow([part, source + 2000, 56, source + 100 + late, 0,
                                     source + 1900])
            (session / "lon-view" / "uc-1.uc.json").write_text(json.dumps({"caches": ["lon-edge-1"]}))

            class NoNull:
                @staticmethod
                def referee():
                    return None

                @staticmethod
                def analyse(*_args):
                    return {"probes": []}

            built_report = report.build(built.run(), [8], NoNull)
            text = report.format_report(built_report)
        finally:
            built.close()
        source = built_report["source"]["S1"]
        self.assertEqual(len(source["over_2000us"]["episodes"]), 1)
        self.assertEqual(source["over_2000us"]["by_side"], {"pacer": {"n": 1, "ms": 3.0}})
        self.assertEqual(built_report["hosts"]["groups"], {"h1": ["lon-dist-a", "lon-edge-1"]})
        for rule in ("R1", "R2", "R3", "R4", "R5", "R6"):
            self.assertIn(f"{rule} (", text)
        self.assertIn("3.1 source component S1: 1 episodes more than 1 ms", text)


@unittest.skipUnless((WAVE7 / "lon-view" / "nt-popus.probe.json").is_file(),
                     "the referee's wave 7 files are not on this machine")
class Wave7(unittest.TestCase):
    def test_e1_table_and_the_first_of_two(self):
        """E1: commit gaps of 100 ms or more of each edge in the first Opus
        minute (1709 to 1770 after 1795380000), from every LL-HLS probe of
        the region, and the first of two edges per region."""
        paths = sorted(WAVE7.glob("*/*.probe.csv"))
        commits, published = report.edge_commits(report.hls_probes(paths))
        base = 1_795_380_000
        gaps = report.commit_gaps(commits, 100, (base + 1709) * 1_000_000,
                                  (base + 1770) * 1_000_000)
        table = {edge: [(round(a / 1e6 - base, 3), round(b / 1e6 - base, 3), round(ms))
                        for a, b, ms in found] for edge, found in gaps.items() if found}
        self.assertEqual(table, {
            "lon-edge-1": [(1725.252, 1725.931, 679), (1725.938, 1726.381, 443),
                           (1752.492, 1752.792, 299)],
            "lon-edge-2": [(1729.282, 1729.514, 232), (1766.953, 1767.071, 118)],
            "tok-edge-2": [(1714.481, 1715.216, 735), (1737.278, 1737.536, 258)],
            "tok-edge-1": [(1741.153, 1741.356, 204)],
            "syd-edge-1": [(1743.521, 1743.943, 422), (1752.719, 1752.903, 184)],
            "syd-edge-2": [(1734.037, 1734.140, 102)],
            "new-edge-2": [(1713.169, 1713.281, 112), (1745.328, 1745.458, 130),
                           (1760.265, 1760.410, 145)]})
        since, until = (base + 1709) * 1_000_000, (base + 1770) * 1_000_000
        expected = {"lon": (0.22, 0.49, 22.51), "tok": (108.80, 109.36, 131.42),
                    "syd": (125.15, 125.57, 147.33), "new": (30.82, 31.19, 53.00)}
        for region, (p50, p99, most) in expected.items():
            edges = sorted(edge for edge in commits if edge.startswith(region + "-"))
            figure = report.first_of_two(commits, published, edges, since, until)
            self.assertAlmostEqual(figure["p50_ms"], p50, delta=0.01, msg=region)
            self.assertAlmostEqual(figure["max_ms"], most, delta=0.01, msg=region)
            # The p99 depends on which parts the window holds: within 0.05 ms.
            self.assertAlmostEqual(figure["p99_ms"], p99, delta=0.05, msg=region)


if __name__ == "__main__":
    unittest.main()
