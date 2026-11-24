#!/usr/bin/env python3
"""Tests of hunt-report.py (D-hunt.md 7, as revised after the wave 9 run),
offline on synthetic hunt records: the join of a silence to its cause and
the unexplained rest; nodes grouped by physical host and their stops at one
instant against chance; a stop of the class thread classified from the
sampler by each rule of stall_records.classify (not run by run delay, by a
stopped tick, by the guest's reclaim, by a real-time neighbour; blocked in
our code on a named wait, on page faults; waited for input; undetermined;
no sampler); one synthetic input for each decision rule H1 to H7; a run
whose /api/hops pull fell inside a session is refused; unpinned tick
threads make the scope untrusted; the first of two edges for every pair; a
whole report from a synthetic run with an LL-HLS probe at each edge and a
sampler; and, where the referee's files are on this machine, E1's table of
wave 7 and the wave 9 first session's 13 stops of 100 ms or more with the
London twins stopped together for 446 ms.
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
# Wave 9's first session (the referee's cmpd5) and its lab's addresses.
WAVE9 = WAVE7.parents[1] / "waudio9" / "audio.opus-2.5.baseline.clean"
WAVE9_STATE = pathlib.Path.home() / "needletail-data" / "cmpd5" / "lab" / "state.20261123T235445Z.json"
WAVE9_T0 = 1_795_477_184


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


def sample(t_us: int, delay_ms: float = 0.0, run_ms: float = 0.0, voluntary: int = 0,
           busy_us: int = 500, over_50ms: int = 0, minflt: int = 0, waits: dict | None = None,
           vm: dict | None = None, threads: list | None = None, pid: int = 7, tid: int = 9,
           cpu: int = 0) -> dict:
    """A sampler line (scripts/hunt-sampler.sh) with the totals given."""
    mesh = {"relay_session.fast_plane.audio_loop_busy_max_us": busy_us,
            "relay_session.fast_plane.audio_loop_busy_over_50ms": over_50ms,
            "relay_session.fast_plane.audio_thread_minflt": minflt,
            "relay_session.fast_plane.audio_thread_majflt": 0}
    for key, value in (waits or {}).items():
        mesh[f"relay_session.edge_clients.{key}"] = value
    return {"t_us": int(t_us), "pid": pid, "tid": tid, "tid_by": "fifo", "cpu": cpu,
            "schedstat": [int(run_ms * 1e6), int(delay_ms * 1e6), 100],
            "csw": [voluntary, 0], "vm": {"pgfault": 1, "allocstall_normal": 0, **(vm or {})},
            "memfree_kb": 1_000_000,
            "threads": threads or [[tid, "FF", 10, cpu, "needletail-fast"]], "mesh": mesh}


STOP = (T + 2_100_000, T + 2_500_000)          # a 400 ms stop inside second 2


class Classify(unittest.TestCase):
    def lines(self, **after) -> list[dict]:
        """Lines at 1, 2, 3, 4 s; the line at 3 s carries `after`'s growth."""
        base = dict(delay_ms=10.0, run_ms=100.0, voluntary=50, busy_us=800,
                    waits={"class_compact_max_us": 200, "fanout_dispatch_max_us": 900})
        grown = {**base, **after}
        return [sample(T + 1_000_000, **base), sample(T + 2_000_000, **base),
                sample(T + 3_000_000, **grown), sample(T + 4_000_000, **grown)]

    def test_runnable_and_not_run(self):
        found = sr.classify(*STOP, self.lines(delay_ms=10.0 + 390, voluntary=51))
        self.assertEqual(found["class"], "not run")
        self.assertEqual(found["where"], "not named: the hypervisor or the guest kernel")
        self.assertAlmostEqual(found["figures"]["run_delay_ms"], 390.0)
        reclaim = sr.classify(*STOP, self.lines(delay_ms=400.0, vm={"allocstall_normal": 3}))
        self.assertEqual(reclaim["where"], "the guest kernel: direct reclaim or compaction")
        neighbour = self.lines(delay_ms=400.0)
        neighbour[2]["threads"].append([12, "FF", 10, 0, "nt-fanout-audio0"])
        found = sr.classify(*STOP, neighbour)
        self.assertEqual(found["where"],
                         "another real-time thread on CPU 0: nt-fanout-audio0 (12)")

    def test_a_stopped_tick_of_its_cpu_is_the_hypervisor(self):
        watch = [tick(0, STOP[1], 390 * MS), tick(1, STOP[1], 390 * MS)]
        found = sr.classify(*STOP, self.lines(), [], watch)
        self.assertEqual(found["class"], "not run")
        self.assertEqual(found["where"], "the hypervisor: the vCPU was not run")
        # A stopped tick of another CPU (scope one) is not this thread's.
        other = [tick(1, STOP[1], 390 * MS, cause="stopped", scope="one")]
        self.assertNotEqual(sr.classify(*STOP, self.lines(), [], other)["class"], "not run")

    def test_blocked_in_our_code_on_a_named_wait(self):
        found = sr.classify(*STOP, self.lines(delay_ms=11.0, voluntary=53, busy_us=395_000,
                                              waits={"class_compact_max_us": 380_000}))
        self.assertEqual(found["class"], "blocked in our code")
        self.assertIn("the compact path", found["wait"])
        # The fan-out hand-off counter that did not rise is not named.
        found = sr.classify(*STOP, self.lines(
            delay_ms=11.0, voluntary=53, over_50ms=1, minflt=2400,
            waits={"fanout_dispatch_max_us": 900}))
        self.assertEqual(found["class"], "blocked in our code")
        self.assertEqual(found["wait"], "page faults on the class thread (2400 minor, 0 major)")
        # The journal's own long pass is evidence too; with no switch it is a spin.
        journal = [{"kind": "class_pass", "thread": "audio", "cpu": 0, "t_us": STOP[1],
                    "dur_us": 390 * MS, "cause": "own"}]
        found = sr.classify(*STOP, self.lines(delay_ms=11.0, run_ms=490.0), journal)
        self.assertEqual(found["class"], "blocked in our code")
        self.assertTrue(found["wait"].startswith("no switch: a spin"))

    def test_waited_for_input_undetermined_and_no_sampler(self):
        self.assertEqual(sr.classify(*STOP, self.lines(delay_ms=11.0, voluntary=60))["class"],
                         "waited for input")
        self.assertEqual(sr.classify(*STOP, self.lines(delay_ms=110.0))["class"], "undetermined")
        self.assertEqual(sr.classify(*STOP, [])["class"], "no sampler")
        restarted = self.lines()
        restarted[2]["pid"] = restarted[3]["pid"] = 8
        self.assertEqual(sr.classify(*STOP, restarted)["class"], "undetermined")


def stop(edge: str, start_s: float, ms: float, cls: str = "not run", session: str = "S1",
         role: str = "first", **more) -> dict:
    begin = int(T + start_s * 1e6)
    return {"session": session, "role": role, "edge": edge, "from_us": begin,
            "to_us": begin + int(ms * 1000), "ms": ms, "class": cls, "why": "test", **more}


EDGES = ["lon-edge-1", "lon-edge-2", "lon-edge-3", "lon-edge-4", "tok-edge-1", "tok-edge-2"]


def table(*stops: dict, seen=EDGES, roles=None) -> dict:
    """An edges table: {session: {"role", "seen", "stops", "twins", ...}}."""
    out = {}
    for name, role in (roles or {"S1": "first"}).items():
        mine = [item for item in stops if item["session"] == name]
        out[name] = {"role": role, "seen": list(seen), "stops": mine, "over_20ms": {},
                     "twins": report.twin_overlaps(mine), "first_of_two": {}}
    return out


class Rules(unittest.TestCase):
    def test_h1_the_fix_per_edge(self):
        clean = report.rule_fix(table(), EDGES)
        self.assertTrue(clean["fires"])
        self.assertIn("NO STOP REMAINS", clean["line"])
        found = report.rule_fix(table(stop("lon-edge-1", 5, 670), seen=EDGES[:-1]), EDGES)
        self.assertFalse(found["fires"])
        self.assertEqual(found["stopped"], ["lon-edge-1"])
        self.assertEqual(found["unmeasured"], ["tok-edge-2"])
        self.assertEqual(found["verdicts"]["lon-edge-2"], "no stop of 100 ms or more")
        self.assertEqual(found["verdicts"]["lon-edge-1"], "1 stops: lon-edge-1 670 ms (not run)")

    def test_h2_the_class_of_each_stop(self):
        roles = {"S1": "first", "S4": "repeat-first"}
        hosts = {"lon-edge-1": "h1", "lon-edge-2": "h1", "tok-edge-1": "h3"}
        replaced = [{"name": "lon-edge-1", "unix": S + 100, "host_uuid_before": "h1",
                     "host_uuid_after": "h2"}]
        before = [stop("lon-edge-1", 5, 670, where="the hypervisor: the vCPU was not run"),
                  stop("lon-edge-2", 7, 300, where="the hypervisor: the vCPU was not run")]
        clean_after = report.rule_classes(table(*before, roles=roles), hosts, replaced)
        self.assertTrue(clean_after["fires"]["not_run"])
        self.assertIn("co-hosted: h1", clean_after["line"])
        self.assertIn("the replaced instance is clean: the instance", clean_after["line"])
        again = before + [stop("lon-edge-1", 200, 500, session="S4", role="repeat-first",
                               where="the hypervisor: the vCPU was not run")]
        same = report.rule_classes(table(*again, roles=roles), hosts,
                                   [{**replaced[0], "host_uuid_after": "h1"}])
        self.assertIn("a platform matter for the owner (same host h1)", same["line"])
        other = report.rule_classes(table(*again, roles=roles), hosts, replaced)
        self.assertIn("not one instance but guests under first load", other["line"])
        ours = report.rule_classes(table(
            stop("tok-edge-1", 5, 400, "blocked in our code", wait="the compact path"),
            stop("tok-edge-2", 9, 200, "blocked in our code", wait="the compact path"),
            stop("lon-edge-3", 9, 150, "waited for input")), hosts, [])
        self.assertTrue(ours["fires"]["our_code"])
        self.assertTrue(ours["fires"]["input"])
        self.assertIn("OUR CODE: the compact path (2)", ours["line"])
        nothing = report.rule_classes(table(), hosts, [])
        self.assertIn("no stop of 100 ms or more in the run", nothing["line"])

    def test_h3_the_arena_warm_up(self):
        arms = {"no_arena_warm": ["tok-edge-1", "tok-edge-2"]}
        roles = {"S1": "first", "S2": "second"}
        matters = report.rule_arena(table(
            stop("tok-edge-1", 5, 600, "blocked in our code", wait="page faults"),
            stop("lon-edge-1", 5, 100, "not run"),
            stop("tok-edge-2", 5, 900, session="S2", role="second"), roles=roles), arms, EDGES)
        self.assertTrue(matters["fires"], matters["line"])
        # 300 ms an edge without it (the S2 stop is not a first session),
        # 25 with it.
        self.assertEqual((matters["without"], matters["with"]), (300.0, 25.0))
        self.assertIn("its region is the other variable", matters["line"])
        clean = report.rule_arena(table(), arms, EDGES)
        self.assertFalse(clean["fires"])
        self.assertIn("no stop with or without it", clean["line"])
        same = report.rule_arena(table(stop("tok-edge-1", 5, 400, "blocked in our code",
                                            wait="x"),
                                       stop("lon-edge-1", 5, 400), stop("lon-edge-2", 9, 400)),
                                 arms, EDGES)
        self.assertFalse(same["fires"])
        self.assertIn("no effect shown", same["line"])

    def test_h4_first_touch(self):
        arms = {"touched": ["lon-edge-1", "lon-edge-3", "tok-edge-1"]}
        matters = report.rule_touch(table(stop("lon-edge-2", 5, 700), stop("lon-edge-4", 6, 300)),
                                    arms, EDGES)
        self.assertTrue(matters["fires"], matters["line"])
        self.assertIn("node_warm", matters["line"])
        # Stops on the untouched edges that are our own waits do not make
        # first touch the cause.
        ours = report.rule_touch(table(stop("lon-edge-2", 5, 700, "blocked in our code",
                                            wait="the compact path")), arms, EDGES)
        self.assertFalse(ours["fires"])
        touched_too = report.rule_touch(table(stop("lon-edge-2", 5, 700),
                                              stop("lon-edge-1", 5, 700)), arms, EDGES)
        self.assertFalse(touched_too["fires"])

    def test_h5_twins_stopped_together(self):
        apart = report.rule_twins(table(stop("lon-edge-1", 5, 300), stop("lon-edge-2", 9, 300),
                                        stop("tok-edge-1", 5.1, 300)))
        self.assertFalse(apart["fires"])
        self.assertIn("two paths hide every stop", apart["line"])
        together = report.rule_twins(table(stop("lon-edge-1", 5.0, 690),
                                           stop("lon-edge-2", 5.039, 446),
                                           stop("lon-edge-3", 20.0, 200),
                                           stop("lon-edge-4", 20.240, 150)))
        self.assertTrue(together["fires"])
        self.assertEqual([(item["edges"], item["both_ms"]) for item in together["together"]],
                         [(["lon-edge-1", "lon-edge-2"], 446.0),
                          (["lon-edge-3", "lon-edge-4"], 0.0)])

    def test_h6_pcm_first(self):
        roles = {"S4": "repeat-first", "S5": "repeat-second"}
        opus_only = report.rule_pcm_first(table(
            stop("lon-edge-1", 5, 300, session="S5", role="repeat-second"), roles=roles))
        self.assertIn("only the Opus session stops", opus_only["line"])
        pcm = report.rule_pcm_first(table(
            stop("lon-edge-1", 5, 300, session="S4", role="repeat-first"), roles=roles))
        self.assertIn("whatever its codec", pcm["line"])
        clean = report.rule_pcm_first(table(roles=roles))
        self.assertFalse(clean["fires"])
        self.assertIn("not the process's", clean["line"])
        self.assertIn("no repeat", report.rule_pcm_first(table())["line"])

    def test_h7_churn(self):
        roles = {"S3": "churn"}
        starts = {"S3": [T + 0, T + 62_000_000, T + 124_000_000]}
        joins = report.rule_churn(table(stop("lon-edge-1", 63.0, 300, session="S3", role="churn"),
                                        stop("lon-edge-2", 125.5, 200, session="S3",
                                             role="churn"),
                                        stop("lon-edge-3", 40.0, 200, session="S3",
                                             role="churn"), roles=roles), starts)
        self.assertTrue(joins["fires"])
        self.assertEqual((joins["at_join"], joins["elsewhere"]), (2, 1))
        elsewhere = report.rule_churn(table(stop("lon-edge-3", 40.0, 200, session="S3",
                                                 role="churn"), roles=roles), starts)
        self.assertFalse(elsewhere["fires"])
        self.assertIn("not at the joins", elsewhere["line"])
        self.assertIn("churn is clean", report.rule_churn(table(roles=roles), starts)["line"])


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


def write_hls_probe(folder: pathlib.Path, name: str, address: str, published: dict,
                    commits: dict) -> None:
    """An LL-HLS probe at one edge: its report names the host of its media
    playlist; its rows carry every part's publish and commit stamps."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.probe.json").write_text(json.dumps(
        {"detail": {"playlist_url_host": f"{address}:19444"}}))
    with open(folder / f"{name}.probe.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["kind", "index", "latency_us", "complete_unix_us", "first_unix_us",
                         "part", "published_unix_us", "edge_committed_unix_us"])
        for part, at in sorted(commits.items()):
            writer.writerow(["a", part, at + 2000 - published[part], at + 2000, part, part,
                             published[part], at])


class WholeReport(unittest.TestCase):
    def test_a_report_of_a_small_run(self):
        """S1 at 100 to 170 s: lon-edge-1 stops 400 ms at 120 s, blocked in
        our code by its sampler; lon-edge-2 commits every part on time; the
        sender is late once."""
        built = Builder({"contrib-lon": {"role": "contributor", "type": "g6-dedicated-4"},
                         "lon-src": {"role": "load", "type": "g6-dedicated-4"},
                         "lon-view": {"role": "load", "type": "g6-dedicated-4"},
                         "lon-dist-a": {"role": "distributor", "host_uuid": "h1"},
                         "lon-edge-1": {"role": "edge", "host_uuid": "h1",
                                        "public_ipv4": "192.0.2.1"},
                         "lon-edge-2": {"role": "edge", "host_uuid": "h2",
                                        "public_ipv4": "192.0.2.2"}},
                        [period("S1", "session", 100, 170)],
                        sessions=[{"name": "S1", "config": "opus-1", "role": "first"}],
                        arms={"touched": ["lon-edge-1"], "no_arena_warm": []})
        try:
            for name in built.record["nodes"]:
                built.node(name, "watch.ndjson", seconds_of(T, T + 200 * 1_000_000))
            start = int(S) + 90
            session = built.dir / "sessions" / "S1"
            (session / "lon-src").mkdir(parents=True)
            (session / "lon-src" / "source.start").write_text(f"{start}\n")
            write_ndjson(session / "lon-src" / "sender.ndjson", [
                {"kind": "late", "epoch": 20_000, "deadline_us": start * 1_000_000 + 20_001_000,
                 "woke_late_us": 3000, "encode_us": 90, "late_first_us": 3000,
                 "late_last_us": 3100}])
            published = {part: start * 1_000_000 + (part + 1) * 1000 + 100
                         + (3000 if 20_000 <= part < 20_003 else 0)
                         for part in range(10_000, 75_000)}
            on_time = {part: at + 500 for part, at in published.items()}
            held = dict(on_time)
            stopped_at = T + 120_000_000
            for part, at in published.items():
                if stopped_at - 500 <= at < stopped_at + 400_000:
                    held[part] = stopped_at + 400_000 + 500
            write_hls_probe(session / "lon-view", "hls-lon-edge-1-1", "192.0.2.1", published, held)
            write_hls_probe(session / "lon-view", "hls-lon-edge-2-1", "192.0.2.2", published,
                            on_time)
            lines = [sample(T + second * 1_000_000, delay_ms=5.0, voluntary=10 * second,
                            busy_us=800 if second <= 120 else 401_000)
                     for second in range(95, 175)]
            built.node("lon-edge-1", "sampler.ndjson", lines)
            built.node("lon-edge-2", "sampler.ndjson", lines[:1])

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
        edges = built_report["edges"]["S1"]
        self.assertEqual(edges["seen"], ["lon-edge-1", "lon-edge-2"])
        self.assertEqual(len(edges["stops"]), 1)
        found = edges["stops"][0]
        self.assertEqual((found["edge"], found["class"]), ("lon-edge-1", "blocked in our code"))
        self.assertAlmostEqual(found["ms"], 400.5, delta=1.5)
        rules = built_report["rules"]
        self.assertEqual(rules["H1"]["stopped"], ["lon-edge-1"])
        self.assertEqual(rules["H1"]["verdicts"]["lon-edge-2"], "no stop of 100 ms or more")
        self.assertTrue(rules["H2"]["fires"]["our_code"])
        self.assertFalse(rules["H5"]["fires"])
        # lon-edge-2 hides lon-edge-1's stop: nothing is late on both.
        pair = edges["first_of_two"]["lon"]["lon-edge-1+lon-edge-2"]
        self.assertEqual(pair["late_on_both"], 0)
        source = built_report["source"]["S1"]
        self.assertEqual(source["over_2000us"]["by_side"], {"pacer": {"n": 1, "ms": 3.0}})
        for rule in ("H1", "H2", "H3", "H4", "H5", "H6", "H7"):
            self.assertIn(f"{rule} (", text)
        self.assertIn("BLOCKED IN OUR CODE", text)
        self.assertIn("first session, lon-edge-2: no stop of 100 ms or more", text)


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



@unittest.skipUnless((WAVE9 / "lon-view" / "nt-popus.probe.json").is_file()
                     and WAVE9_STATE.is_file(), "the wave 9 files are not on this machine")
class Wave9(unittest.TestCase):
    def test_the_first_session_s_stops_and_the_london_twins(self):
        """w9-stall-analysis.md section 1: from every LL-HLS probe of the
        first session, mapped to its edge by the lab's addresses, 13 commit
        stops of 100 ms or more, 5,146 ms in all; the London twins stopped
        together for 446 ms (and 31 ms just before); new-edge-1 clean; no
        sampler then, so no stop can be classified."""
        state = json.loads(WAVE9_STATE.read_text())
        addresses = {node["public_ipv4"]: name for name, node in state["nodes"].items()}
        paths = sorted(path for path in WAVE9.glob("*/nt-*.probe.csv")
                       if not path.name.startswith("nt-uc"))
        commits, _published = report.edge_commits(report.hls_probes(paths, addresses))
        gaps = report.commit_gaps(commits, report.STOP_MS)
        found = {edge: [(round(a / 1e6 - WAVE9_T0, 3), round(b / 1e6 - WAVE9_T0, 3), round(ms))
                        for a, b, ms in items] for edge, items in gaps.items() if items}
        self.assertEqual(found, {
            "lon-edge-1": [(14.304, 14.974, 670), (17.137, 17.827, 690),
                           (25.529, 26.866, 1337), (48.295, 48.63, 335)],
            "lon-edge-2": [(16.871, 17.169, 298), (17.176, 17.622, 446)],
            "tok-edge-2": [(46.348, 46.836, 488), (56.5, 56.648, 148), (71.427, 71.552, 125)],
            "tok-edge-1": [(43.302, 43.488, 187)],
            "syd-edge-2": [(23.813, 23.98, 168), (45.353, 45.505, 152)],
            "new-edge-2": [(18.699, 18.8, 102)]})
        stops = [{"session": "S1", "role": "first", "edge": edge, "from_us": a, "to_us": b,
                  "ms": ms, **sr.classify(a, b, [])} for edge, items in gaps.items()
                 for a, b, ms in items]
        self.assertEqual(len(stops), 13)
        self.assertEqual(round(sum(item["ms"] for item in stops)), 5146)
        self.assertEqual({item["class"] for item in stops}, {"no sampler"})
        edges = sorted(commits)
        edges_table = {"S1": {"role": "first", "seen": edges, "stops": stops, "over_20ms": {},
                              "twins": report.twin_overlaps(stops), "first_of_two": {}}}
        twins = report.rule_twins(edges_table)
        self.assertTrue(twins["fires"])
        self.assertEqual([(item["edges"], round(item["both_ms"])) for item in twins["together"]],
                         [(["lon-edge-1", "lon-edge-2"], 31), (["lon-edge-1", "lon-edge-2"], 446)])
        fix = report.rule_fix(edges_table, edges)
        self.assertEqual(fix["verdicts"]["new-edge-1"], "no stop of 100 ms or more")
        self.assertEqual(len(fix["stopped"]), 6)


if __name__ == "__main__":
    unittest.main()
