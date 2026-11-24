#!/usr/bin/env python3
"""Tests of hunt-report.py (D-hunt.md 7, as revised after the wave 9 run),
offline on synthetic hunt records: the join of a silence to its cause and
the unexplained rest; nodes grouped by physical host and their stops at one
instant against chance; a stop of the class thread classified from the
sampler by each rule of stall_records.classify (not run by run delay, by a
stopped tick, by the guest's reclaim, by a real-time neighbour; blocked in
our code on a named wait, on page faults in the pass, never on a stale
fault counter; waited for input, with run delay taken against the thread's
own rate; undetermined; no sampler); one synthetic input for each decision
rule H1 to H7 (H1: an edge no UDP client reached is not measured; H4: a
touch that did not hold cleanly is left out); the UDP clients of each edge;
a run whose /api/hops pull fell inside a session is refused; unpinned tick
threads (from the sampler) make the scope untrusted; a replaced edge keeps
its old journal; the first of two edges for every pair; a
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
        mesh[f"client_udp.fast_lane.{key}"] = value
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
        self.assertEqual(found["wait_kind"], "counter")
        # Faults in the long pass itself (the journal's class_pass carries
        # the thread's faults since its last reading) name the wait; the
        # fan-out hand-off counter that did not rise is not named.
        journal = [{"kind": "class_pass", "thread": "audio", "cpu": 0, "t_us": STOP[1],
                    "dur_us": 390 * MS, "cause": "own", "minflt": 2400, "majflt": 1}]
        found = sr.classify(*STOP, self.lines(delay_ms=11.0, voluntary=53,
                                              waits={"fanout_dispatch_max_us": 900}), journal)
        self.assertEqual(found["class"], "blocked in our code")
        self.assertEqual(found["wait"],
                         "page faults on the class thread in the pass (2400 minor, 1 major)")
        self.assertEqual(found["wait_kind"], "faults")
        # The journal's own long pass is evidence too; with no switch it is a spin.
        journal = [{"kind": "class_pass", "thread": "audio", "cpu": 0, "t_us": STOP[1],
                    "dur_us": 390 * MS, "cause": "own"}]
        found = sr.classify(*STOP, self.lines(delay_ms=11.0, run_ms=490.0), journal)
        self.assertEqual(found["class"], "blocked in our code")
        self.assertTrue(found["wait"].startswith("no switch: a spin"))

    def test_a_fault_counter_that_rose_in_the_window_does_not_name_the_wait(self):
        """One minor fault in the sampler's interval (the thread's own
        once-a-second reading, up to a second stale) around a 300 ms block
        with no class_pass faults: the wait is not "page faults", and H4
        does not take it as evidence."""
        stop = (STOP[0], STOP[0] + 100_000)    # 100 ms: a pass over 50 ms is half of it
        found = sr.classify(*stop, self.lines(delay_ms=11.0, voluntary=53, over_50ms=1,
                                              minflt=1))
        self.assertEqual(found["class"], "blocked in our code")
        self.assertEqual(found["wait_kind"], "unnamed")
        self.assertTrue(found["wait"].startswith("a lock or wait that no counter names"))
        self.assertIn("not evidence of faults in the stop", found["wait"])

    def test_a_pass_over_50_ms_is_no_evidence_for_a_longer_stop(self):
        """audio_loop_busy_over_50ms counts passes over 50 ms (av-mesh
        relay.rs): its growth shows a pass of half a 400 ms stop no more than
        a busy maximum that stayed at 0.8 ms does; on a 100 ms stop it does.
        The video class thread's own passes never count for the audio
        thread's stop."""
        lines = self.lines(delay_ms=11.0, voluntary=53, over_50ms=1)
        self.assertNotEqual(sr.classify(*STOP, lines)["class"], "blocked in our code")
        self.assertEqual(sr.classify(STOP[0], STOP[0] + 100_000, lines)["class"],
                         "blocked in our code")
        video = [{"kind": "class_pass", "thread": "video", "cpu": 1, "t_us": STOP[1],
                  "dur_us": 390 * MS, "cause": "own", "minflt": 2400, "majflt": 1}]
        found = sr.classify(*STOP, self.lines(delay_ms=11.0, voluntary=53), video)
        self.assertNotEqual(found["class"], "blocked in our code")

    def test_run_delay_is_measured_against_the_thread_s_own_rate(self):
        """A thread that waits 5 ms a second as a rule, sampled 2.5 s apart
        around a 100 ms stop: 12.5 ms of run delay is its usual, so the
        stop is not "undetermined" but "waited for input"."""
        lines = [sample(T + k * 1_000_000, delay_ms=5.0 * k, voluntary=10 * k)
                 for k in range(0, 10)]
        lines += [sample(T + 11_500_000 + k * 1_000_000, delay_ms=57.5 + 5.0 * k,
                         voluntary=115 + 10 * k) for k in range(0, 10)]
        stop = (T + 10_500_000, T + 10_600_000)
        found = sr.classify(*stop, lines)
        self.assertAlmostEqual(found["figures"]["run_delay_ms"], 12.5)
        self.assertAlmostEqual(found["figures"]["run_delay_rate_ms_s"], 5.0)
        self.assertAlmostEqual(found["figures"]["run_delay_excess_ms"], 0.0)
        self.assertEqual(found["class"], "waited for input")
        # Without neighbours to measure the rate, the raw growth is used.
        self.assertEqual(sr.classify(*stop, [lines[9], lines[10]])["class"], "undetermined")

    def test_an_unpinned_service_s_ticks_count_only_on_the_class_thread_s_cpu(self):
        """The service's journal has a stopped tick of CPU 1 marked `scope:
        all`; the class thread is on CPU 0. Trusted, that is the
        hypervisor; with a tick thread unpinned, the scope is not
        believed."""
        journal = [tick(1, STOP[1], 390 * MS, cause="stopped", scope="all")]
        trusted = sr.classify(*STOP, self.lines(), journal)
        self.assertEqual(trusted["class"], "not run")
        untrusted = sr.classify(*STOP, self.lines(), journal, scope_trusted=False)
        self.assertNotEqual(untrusted["class"], "not run")
        own_cpu = [tick(0, STOP[1], 390 * MS, cause="stopped", scope="one")]
        self.assertEqual(sr.classify(*STOP, self.lines(), own_cpu, scope_trusted=False)["class"],
                         "not run")

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


def table(*stops: dict, seen=EDGES, roles=None, clients=None) -> dict:
    """An edges table: {session: {"role", "seen", "stops", "twins", ...}};
    UDP clients reached every edge seen unless `clients` says otherwise."""
    out = {}
    for name, role in (roles or {"S1": "first"}).items():
        mine = [item for item in stops if item["session"] == name]
        out[name] = {"role": role, "seen": list(seen), "stops": mine, "over_20ms": {},
                     "udp_clients": dict(clients if clients is not None
                                         else {edge: 10 for edge in seen}),
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
        self.assertEqual(found["verdicts"]["lon-edge-2"],
                         "no stop of 100 ms or more (10 UDP clients)")
        self.assertEqual(found["verdicts"]["lon-edge-1"], "1 stops: lon-edge-1 670 ms (not run)")

    def test_h1_an_edge_no_udp_client_reached_is_not_measured(self):
        """Every edge's LL-HLS probe saw its commits and none stopped, but
        the crowd never reached lon-edge-3 and lon-edge-4 (an assignment
        put every listener on the first two): H1 does not say that no stop
        remains. A stop counts on an edge without a client all the same."""
        clients = {edge: 10 for edge in EDGES if edge not in ("lon-edge-3", "lon-edge-4")}
        found = report.rule_fix(table(clients=clients), EDGES)
        self.assertFalse(found["fires"])
        self.assertNotIn("NO STOP REMAINS", found["line"])
        self.assertEqual(found["unmeasured"], ["lon-edge-3", "lon-edge-4"])
        self.assertEqual(found["verdicts"]["lon-edge-3"], "not measured: no UDP client reached it")
        self.assertIn("not measured: lon-edge-3, lon-edge-4", found["line"])
        stopped = report.rule_fix(table(stop("lon-edge-4", 5, 300), clients=clients), EDGES)
        self.assertEqual(stopped["stopped"], ["lon-edge-4"])
        self.assertTrue(stopped["verdicts"]["lon-edge-4"].endswith("(no UDP client reached it)"))

    def test_udp_clients_from_the_probes_and_the_crowd(self):
        """A UDP probe's caches (node names) and a crowd's per-edge listeners
        subscribed while parts came (by address), per edge."""
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            (folder / "lon-view" / "load").mkdir(parents=True)
            (folder / "lon-view" / "uc-1.uc.json").write_text(json.dumps(
                {"caches": ["lon-edge-2", "lon-edge-1"]}))
            write_ndjson(folder / "lon-view" / "load" / "intervals.jsonl", [
                {"t": 1.0, "edges": {"192.0.2.1:19446": {"up": 0, "parts": 0},
                                     "192.0.2.3:19446": {"up": 10, "parts": 0}}},
                {"t": 2.0, "edges": {"192.0.2.1:19446": {"up": 10, "parts": 900},
                                     "192.0.2.3:19446": {"up": 10, "parts": 0}}}])
            found = report.udp_clients(folder, {"192.0.2.1": "lon-edge-1",
                                                "192.0.2.3": "lon-edge-3"})
        self.assertEqual(found, {"lon-edge-1": 11, "lon-edge-2": 1})

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
                                            wait="the compact path", wait_kind="counter")),
                                 arms, EDGES)
        self.assertFalse(ours["fires"])
        # Faults in the pass are evidence; a stale fault counter is not.
        faults = report.rule_touch(table(stop("lon-edge-2", 5, 700, "blocked in our code",
                                              wait="page faults ...", wait_kind="faults")),
                                   arms, EDGES)
        self.assertTrue(faults["fires"])
        stale = report.rule_touch(table(stop(
            "lon-edge-2", 5, 700, "blocked in our code",
            wait="a lock or wait ...; the thread's fault counter rose (1 minor, 0 major) ...",
            wait_kind="unnamed")), arms, EDGES)
        self.assertFalse(stale["fires"])
        touched_too = report.rule_touch(table(stop("lon-edge-2", 5, 700),
                                              stop("lon-edge-1", 5, 700)), arms, EDGES)
        self.assertFalse(touched_too["fires"])

    def test_h4_leaves_out_a_touch_that_did_not_hold_cleanly(self):
        """touch_checks over a run: lon-edge-1 released cleanly with no swap
        or refault (its balloon reports free pages); lon-edge-3 swapped in
        after its release; tok-edge-1 has no release mark. Only lon-edge-1
        is the touched arm; the others are in neither arm."""
        built = Builder({name: {"role": "edge"} for name in EDGES},
                        [period("touch", "touch", 0, 270), period("S1", "session", 300, 370)],
                        sessions=[{"name": "S1", "role": "first"}],
                        arms={"touched": ["lon-edge-1", "lon-edge-3", "tok-edge-1"]},
                        touch={"nodes": ["lon-edge-1", "lon-edge-3", "tok-edge-1"],
                               "from_unix": S + 0, "released_unix": S + 270})
        try:
            clean = [sample(T + k * 10_000_000, vm={"pswpin": 0, "workingset_refault_file": 4})
                     for k in range(-1, 40)]
            swapped = [sample(T + k * 10_000_000,
                              vm={"pswpin": 0 if k < 30 else 2048, "workingset_refault_file": 4})
                       for k in range(-1, 40)]
            for name, lines in (("lon-edge-1", clean), ("lon-edge-3", swapped),
                                ("tok-edge-1", clean)):
                built.node(name, "sampler.ndjson", lines)
            marks = f"touch {S} 3000 3384 384\nreleased {S + 270} finished\n"
            for name in ("lon-edge-1", "lon-edge-3"):
                (built.dir / "nodes" / name / "touch-marks.txt").write_text(marks)
            (built.dir / "nodes" / "tok-edge-1" / "touch-marks.txt").write_text(
                f"touch {S} 3000 3384 384\n")
            bits = "0" * 5 + "1" + "0" * 58
            (built.dir / "nodes" / "lon-edge-1" / "system.txt").write_text(
                f"thp: always\nballoon-features: {bits}\n")
            checks = report.touch_checks(built.run())
        finally:
            built.close()
        self.assertIsNone(checks["lon-edge-1"]["excluded"])
        self.assertTrue(checks["lon-edge-1"]["reporting"])
        self.assertIn("pswpin +2048", checks["lon-edge-3"]["excluded"])
        self.assertIn("no release mark", checks["tok-edge-1"]["excluded"])
        self.assertIsNone(checks["tok-edge-1"]["reporting"])
        arms = {"touched": ["lon-edge-1", "lon-edge-3", "tok-edge-1"]}
        found = report.rule_touch(table(stop("lon-edge-2", 5, 700), stop("lon-edge-4", 6, 300),
                                        stop("lon-edge-3", 6, 900)), arms, EDGES, checks)
        # lon-edge-3's 900 ms is in neither arm: 333 ms an untouched edge
        # (lon-edge-2, -4, tok-edge-2) against 0 on lon-edge-1.
        self.assertEqual((found["without"], found["with"]), (333.333, 0.0))
        self.assertTrue(found["fires"])
        self.assertIn("left out: lon-edge-3 (the kernel swapped", found["line"])
        self.assertIn("tok-edge-1 (no release mark", found["line"])
        self.assertIn("free page reporting on lon-edge-1", found["line"])
        self.assertEqual(report.balloon_reporting("balloon-features: " + "0" * 64), False)

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


def beats(cpus=(0, 1), start_s: float = -5.0, end_s: float = 10.0,
          late: dict | None = None) -> str:
    """The referee's heartbeat logs: a line a second on each vCPU from
    `start_s` to `end_s` (from S), and `late` {(cpu, wake_s): late_ms}."""
    lines = []
    second = start_s
    while second <= end_s:
        for cpu in cpus:
            t = int((S + second) * 1e9)
            lines.append(f"HB {cpu} {t} {t} {t} 0 fifo99")
        second += 1.0
    for (cpu, wake_s), late_ms in (late or {}).items():
        t = int((S + wake_s) * 1e9)
        lines.append(f"HB {cpu} {t} {t} {t} {int(late_ms * 1e6)} fifo99")
    return "\n".join(lines) + "\n"


def task_line(t_s: float, tid: int, comm: str, stime: int, cpu: int, policy: int = 1,
              pid: int = 7) -> str:
    """A `task` line of the referee's sys sampler (/proc/PID/task/TID/stat
    raw: utime is field 14, stime 15, the CPU 39, the policy 41)."""
    fields = ["S"] + ["0"] * 10 + ["5", str(stime)] + ["0"] * 23 + [str(cpu), "0", str(policy)]
    return f"{S + t_s:.6f} S task {pid} {tid} ({comm}) {' '.join(fields)}"


def sys_log(start_s: float = -5.0, end_s: float = 10.0, stime_at: dict | None = None,
            skip: tuple[float, float] | None = None) -> str:
    """The referee's sys sampler: a second's stat line and the class
    threads' task lines (audio tid 9 FIFO on CPU 0, video tid 10 on CPU
    1), stime rising as `stime_at` {second: ticks} says; no line inside
    `skip`."""
    lines, second, stime = [], start_s, 0
    while second <= end_s:
        if not (skip and skip[0] < second < skip[1]):
            stime = max([stime] + [ticks for at, ticks in (stime_at or {}).items()
                                   if at <= second])
            lines.append(f"{S + second:.6f} S stat cpu0 1 2 3 4 0 0 0 0")
            lines.append(task_line(second, 9, "needletail-fast", stime, 0))
            lines.append(task_line(second, 10, "needletail-fast", 0, 1, policy=0))
        second += 1.0
    return "\n".join(lines) + "\n"


def write_referee(built, name: str, heartbeat: str, system: str, meshlocal: str = "",
                  nic: str = "") -> None:
    folder = built.dir / "samplers"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.heartbeat.1795384100.log").write_text(heartbeat)
    (folder / f"{name}.sys.1795384100.log").write_text(system)
    if meshlocal:
        (folder / f"{name}.meshlocal.1795384100.log").write_text(meshlocal)
    if nic:
        (folder / f"{name}.nic.1795384000.txt").write_text(nic)


class RefereeSamplers(unittest.TestCase):
    """A stop classified from the referee's samplers (D-hunt.md 7.5 as
    revised after the 4K run): the heartbeat of the class thread's vCPU
    and the service threads' system time."""

    def classify(self, heartbeat: str, system: str, mesh: list[dict] = ()) -> dict:
        return sr.classify_heartbeat(T + 2_000_000, T + 2_600_000, sr.parse_heartbeat(heartbeat),
                                     sr.parse_sys_tasks(system), list(mesh))

    def test_the_three_classes_and_no_sampler(self):
        # CPU 0's heartbeat woke 600 ms late over the stop, no stime: not run.
        late = beats(late={(0, 2.6): 600})
        found = self.classify(late, sys_log())
        self.assertEqual(found["class"], "not run")
        self.assertEqual(found["figures"]["cpu"], 0)
        self.assertIn("vCPU 0 was not run", found["where"])
        # Every vCPU at once: the whole guest.
        found = self.classify(beats(late={(0, 2.6): 600, (1, 2.65): 650}), sys_log())
        self.assertIn("the whole guest", found["where"])
        # Late, and the audio class thread's stime rose 60 ticks: in the
        # kernel with interrupts off.
        found = self.classify(late, sys_log(stime_at={3.0: 60}))
        self.assertEqual(found["class"], "kernel, interrupts off")
        self.assertIn("needletail-fast (9) stime +600 ms", found["where"])
        # The heartbeat on time: the vCPU ran; blocked in our code, the
        # wait unnamed without the node's counters.
        found = self.classify(beats(), sys_log())
        self.assertEqual(found["class"], "blocked in our code")
        self.assertIn("no /api/mesh/local counters", found["wait"])
        # With the counters: a long pass named by its counter, or none.
        mesh = [{"t_us": T + 1_000_000, "mesh": {
                    "relay_session.fast_plane.audio_loop_busy_max_us": 500,
                    "client_udp.fast_lane.fanout_dispatch_max_us": 10}},
                {"t_us": T + 3_000_000, "mesh": {
                    "relay_session.fast_plane.audio_loop_busy_max_us": 590_000,
                    "client_udp.fast_lane.fanout_dispatch_max_us": 580_000}}]
        found = self.classify(beats(), sys_log(), mesh)
        self.assertEqual((found["class"], found["wait"]),
                         ("blocked in our code", "the hand-off to the fan-out threads"))
        flat = [{"t_us": T + 1_000_000, "mesh": {
                    "relay_session.fast_plane.audio_loop_busy_max_us": 500}},
                {"t_us": T + 3_000_000, "mesh": {
                    "relay_session.fast_plane.audio_loop_busy_max_us": 500}}]
        self.assertEqual(self.classify(beats(), sys_log(), flat)["class"], "waited for input")
        # No heartbeat of that vCPU after the stop.
        self.assertEqual(self.classify(beats(end_s=1.0), sys_log())["class"], "no sampler")

    def test_the_files_parse_as_the_referee_writes_them(self):
        lines = sr.parse_heartbeat("HB 1 5 1795384000123456789 7 12000000 fifo99\nnoise\n")
        self.assertEqual(lines, [{"cpu": 1, "t_us": 1795384000123456, "late_us": 12000,
                                  "policy": "fifo99"}])
        task = sr.parse_task_stat(task_line(0, 9, "needletail-fast", 42, 3).split(" ", 4)[4])
        self.assertEqual((task["tid"], task["comm"], task["stime"], task["cpu"], task["policy"]),
                         (9, "needletail-fast", 42, 3, 1))
        # A name with a space and parentheses.
        odd = sr.parse_task_stat("12 (tokio (rt) 1) " + " ".join(["S"] + ["0"] * 38))
        self.assertEqual((odd["tid"], odd["comm"]), (12, "tokio (rt) 1"))
        mesh = sr.parse_meshlocal(f"{S:.3f} " + json.dumps(
            {"client_udp": {"fast_lane": {"fanout_started": 2}},
             "relay_nodes": [{"relay_session": {"fast_plane": {"audio_loop_busy_max_us": 9}}}]}))
        self.assertEqual(mesh[0]["mesh"], {"client_udp.fast_lane.fanout_started": 2})
        self.assertEqual(sr.heartbeat_late_ms(sr.parse_heartbeat(beats(late={(0, 2.0): 300})),
                                              50.0), 300.0)
        nic = report.nic_offloads("== eth0 driver\ndriver: virtio_net\n== eth0 offloads\n"
                                  "generic-receive-offload: on\nrx-udp-gro-forwarding: off "
                                  "[fixed]\ntcp-segmentation-offload: on\n")
        self.assertEqual(nic, {"eth0 generic-receive-offload": "on",
                               "eth0 rx-udp-gro-forwarding": "off"})

    def test_an_edge_s_commit_stop_is_classified_by_the_referee_s_samplers(self):
        built = Builder({"lon-edge-1": {"role": "edge"}}, [period("S1", "session", 0, 10)],
                        sessions=[{"name": "S1", "role": "first"}])
        self.addCleanup(built.close)
        write_referee(built, "lon-edge-1", beats(late={(0, 2.6): 600}), sys_log())
        run = built.run()
        self.assertTrue(run.has_referee("lon-edge-1"))
        found = run.classify("lon-edge-1", T + 2_000_000, T + 2_600_000, True)
        self.assertEqual(found["class"], "not run")

    def test_h4_reads_the_vmstat_snapshots_without_the_hunt_s_sampler(self):
        """With the referee's samplers (no swap counters) a touch is judged
        from the hunt's /proc/vmstat at the touch and after S1."""
        built = Builder({"lon-edge-1": {"role": "edge"}, "lon-edge-3": {"role": "edge"}},
                        [period("touch", "touch", 0, 270), period("S1", "session", 300, 370)],
                        sessions=[{"name": "S1", "role": "first"}],
                        arms={"touched": ["lon-edge-1", "lon-edge-3"]},
                        touch={"nodes": ["lon-edge-1", "lon-edge-3"], "from_unix": S,
                               "released_unix": S + 270})
        self.addCleanup(built.close)
        marks = f"touch {S} 3000 3384 384\nreleased {S + 270} finished\n"
        for name, after in (("lon-edge-1", 0), ("lon-edge-3", 512)):
            folder = built.dir / "nodes" / name
            folder.mkdir(parents=True)
            (folder / "touch-marks.txt").write_text(marks)
            (folder / "vmstat-touch.txt").write_text("pswpin 0\npgfault 10\n")
            (folder / "vmstat-after-first.txt").write_text(f"pswpin {after}\npgfault 99\n")
        checks = report.touch_checks(built.run())
        self.assertIsNone(checks["lon-edge-1"]["excluded"])
        self.assertIn("pswpin +512", checks["lon-edge-3"]["excluded"])

    def test_h8_names_gso_gro_both_or_neither(self):
        """The GSO/GRO matrix: a combination stops when a mesh node's
        heartbeat was late 100 ms or more (or its sys sampler stopped)."""
        def matrix(stopping: set) -> dict:
            out = {}
            for name, gso, gro in (("M1", True, True), ("M2", False, True),
                                   ("M3", True, False), ("M4", False, False)):
                stops = [{"node": "new-dist-a", "ms": 2000.0, "class": "not run"}] \
                    if (gso, gro) in stopping else []
                out[name] = {"gso": gso, "gro": gro, "parts": {"2160p25": {
                    "stops": stops, "by_site": {"newyork": {"n": len(stops), "ms": 2000.0}}
                    if stops else {}, "video": {"new-view": {"whole": not stops}}}}}
            return out

        cases = {frozenset({(True, True), (True, False)}): "SENDER GSO",
                 frozenset({(True, True), (False, True)}): "RECEIVER GRO",
                 frozenset({(True, True), (False, True), (True, False)}): "BOTH",
                 frozenset({(True, True), (False, True), (True, False), (False, False)}):
                     "EVERY COMBINATION STOPS",
                 frozenset(): "not reproduced"}
        for stopping, verdict in cases.items():
            found = report.rule_gso_gro(matrix(set(stopping)))
            self.assertIn(verdict, found["line"], stopping)
        self.assertIn("no GSO/GRO matrix", report.rule_gso_gro({})["line"])
        differ = report.rule_gso_gro(matrix({(True, True)}), {
            "lon-dist-a": {"eth0 rx-udp-gro-forwarding": "off"},
            "tok-dist-a": {"eth0 rx-udp-gro-forwarding": "off"},
            "new-dist-a": {"eth0 rx-udp-gro-forwarding": "on"}})
        self.assertIn("NIC offloads differ: eth0 rx-udp-gro-forwarding off but new-dist-a on",
                      differ["line"])

    def test_the_matrix_table_from_a_run(self):
        """A combination's 4K rung: a 2 s stop of both vCPUs of a New York
        forwarder (no stime: not run, the whole guest), and a sampler gap;
        London clean; the view hosts' wholeness."""
        combination = {"name": "M1", "gso": True, "gro": True}
        built = Builder({"new-dist-a": {"role": "distributor", "site": "newyork"},
                         "lon-dist-a": {"role": "distributor", "site": "london"}},
                        [{**period("M1-V25", "video", 0, 10), "rung": "2160p25",
                          "combination": combination}])
        self.addCleanup(built.close)
        write_referee(built, "new-dist-a", beats(late={(0, 4.0): 2000, (1, 4.05): 2050}),
                      sys_log(skip=(1.5, 4.5)))
        write_referee(built, "lon-dist-a", beats(), sys_log())
        for host, missing in (("lon-view", 0), ("new-view", 41)):
            path = built.dir / "sessions" / "M1-V25" / host / "v.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"readers": [{"parts_missing": missing,
                                                     "stall_events": []}]}))
        table = report.matrix_table(built.run())
        part = table["M1"]["parts"]["2160p25"]
        classes = sorted((stop["node"], stop["class"], stop["whole_guest"])
                         for stop in part["stops"])
        self.assertEqual(classes, [("new-dist-a", "not run", True),
                                   ("new-dist-a", "sampler gap", True)])
        self.assertEqual(sorted(part["by_site"]), ["newyork"])
        self.assertEqual({host: item["whole"] for host, item in part["video"].items()},
                         {"lon-view": True, "new-view": False})


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
            # The sampler's cut of /api/mesh/local carries the pinning.
            pinned = sample(T + 1_000_000)
            pinned["mesh"].update({"stall.tick_threads": 2, "stall.tick_pinned": 2})
            one = sample(T + 6_000_000)
            one["mesh"].update({"stall.tick_threads": 2, "stall.tick_pinned": 1})
            built.node("lon-edge-1", "sampler.ndjson", [pinned, one])
            built.node("lon-edge-2", "sampler.ndjson", [pinned])
            run = built.run()
            rows = report.node_periods(run)
            self.assertEqual(run.unpinned(), {"lon-edge-1": "1 of 2"})
        finally:
            built.close()
        trusted = {(row["node"], row["scope_trusted"]) for row in rows}
        self.assertEqual(trusted, {("lon-edge-1", False), ("lon-edge-2", True)})
        # The time between the soak and the session is a period of its own.
        periods = [row["period"] for row in rows if row["node"] == "lon-edge-1"]
        self.assertEqual(periods, ["soak-1", "after soak-1", "S1", "run"])

    def test_a_replaced_edge_keeps_its_old_instance_s_journal(self):
        """lon-edge-1 was replaced: the old instance's stall journal (fetched
        into before-replace/) holds the long class_pass of its S1 stop, with
        its faults; the new instance's journal does not. The stop is still
        "blocked in our code", on faults in the pass."""
        built = Builder({"lon-edge-1": {"role": "edge"}}, [period("S1", "session", 0, 70)])
        try:
            built.node("lon-edge-1", "before-replace/stalls.ndjson", [
                {"kind": "class_pass", "thread": "audio", "cpu": 0, "t_us": STOP[1],
                 "dur_us": 390 * MS, "span_us": 390 * MS, "n": 1, "cause": "own",
                 "minflt": 120, "majflt": 0}])
            built.node("lon-edge-1", "stalls.ndjson", [
                {"kind": "class_pass", "thread": "audio", "cpu": 0, "t_us": T + 900_000_000,
                 "dur_us": 60 * MS, "span_us": 60 * MS, "n": 1, "cause": "own"}])
            built.node("lon-edge-1", "before-replace/sampler.ndjson",
                       [sample(T + k * 1_000_000, delay_ms=1.0, voluntary=10 * k)
                        for k in range(0, 6)])
            run = built.run()
            journal = run.journal("lon-edge-1")
            found = sr.classify(*STOP, run.sampler("lon-edge-1"), journal)
        finally:
            built.close()
        self.assertEqual(len(journal), 2)
        self.assertEqual(found["class"], "blocked in our code")
        self.assertEqual(found["wait_kind"], "faults")

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
            # The crowd reached both edges (by address, as needletail-load
            # names them without an entry point).
            write_ndjson(session / "lon-view" / "load" / "intervals.jsonl", [
                {"t": 1.0, "edges": {"192.0.2.1:19446": {"up": 20, "parts": 990},
                                     "192.0.2.2:19446": {"up": 20, "parts": 990}}}])

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
        self.assertEqual(rules["H1"]["verdicts"]["lon-edge-2"],
                         "no stop of 100 ms or more (20 UDP clients)")
        self.assertEqual(edges["udp_clients"], {"lon-edge-1": 20, "lon-edge-2": 20})
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
        self.assertIn("UDP clients lon-edge-1 20, lon-edge-2 20", text)


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
        # The UDP clients of the session: the referee's one nt-uc probe a
        # host, by its caches. syd-edge-2 had none, and stopped all the same.
        clients = report.udp_clients(WAVE9, addresses)
        self.assertEqual(sorted(clients), ["lon-edge-1", "lon-edge-2", "new-edge-1",
                                           "new-edge-2", "syd-edge-1", "tok-edge-1",
                                           "tok-edge-2"])
        edges_table = {"S1": {"role": "first", "seen": edges, "stops": stops, "over_20ms": {},
                              "udp_clients": clients,
                              "twins": report.twin_overlaps(stops), "first_of_two": {}}}
        twins = report.rule_twins(edges_table)
        self.assertTrue(twins["fires"])
        self.assertEqual([(item["edges"], round(item["both_ms"])) for item in twins["together"]],
                         [(["lon-edge-1", "lon-edge-2"], 31), (["lon-edge-1", "lon-edge-2"], 446)])
        fix = report.rule_fix(edges_table, edges)
        self.assertEqual(fix["verdicts"]["new-edge-1"], "no stop of 100 ms or more (1 UDP clients)")
        self.assertEqual(len(fix["stopped"]), 6)
        self.assertTrue(fix["verdicts"]["syd-edge-2"].endswith("(no UDP client reached it)"))
        self.assertFalse(fix["fires"])


if __name__ == "__main__":
    unittest.main()
