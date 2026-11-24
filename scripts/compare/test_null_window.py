#!/usr/bin/env python3
"""Tests of null-window.py (D-hunt.md 2.11, 3.1 on the referee's bar of
23 Nov): the arithmetic is the referee's change-window.py, imported from
its worktree; the split by criterion adds up to the referee's own
null_fail_rate; the floor of the bar and 3.1's acceptance, whose limits
are calibrated on stationary probes (stationary probes of 300 s meet it at
8 s and at 30 s windows, a probe with a worse minute does not); and, when the
referee's wave 7 files are on this machine, E4's figures and departure
1's null rates (the latter with the referee's scorer as it stood before 23
Nov, read from its own history).
NEEDLETAIL_COMPARE_ROOT names the referee's worktree (default
<workspace>/needletail-compare); the tests that need it skip without it.
python3 -m unittest test_null_window.py (in scripts/compare)"""

from __future__ import annotations

import collections
import csv
import importlib.util
import pathlib
import random
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("null_window", HERE / "null-window.py")
null_window = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(null_window)

REFEREE = null_window.compare_root() / "scripts" / "compare" / "change-window.py"
RUNS = null_window.compare_root() / "target" / "compare" / "runs"
# The referee's scorer before its bar was calibrated on the null (23 Nov).
OLD_RULE_COMMIT = "6c8c525"
T0 = 1_795_381_000_000_000


def write_frames(path: pathlib.Path, seconds: int, latency_us, gap_after=None,
                 drop=frozenset(), spacing_us: int = 1000) -> None:
    """A probe of `seconds` with a unit every `spacing_us` (1 ms): unit n
    sourced at T0 + n spacings, arriving latency_us(n) later (gap_after(n):
    extra delay of the arrival; the units of `drop` never arrive)."""
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["kind", "index", "latency_us", "complete_unix_us", "first_unix_us"])
        for number in range(seconds * 1_000_000 // spacing_us):
            if number in drop:
                continue
            latency = latency_us(number) + (gap_after(number) if gap_after else 0)
            writer.writerow(["a", number, latency, T0 + number * spacing_us + latency, number])


def write_arrivals(path: pathlib.Path, seconds: int) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["part", "arrival_unix_us", "bytes", "published_unix_us",
                         "available_unix_us", "kernel_unix_us"])
        for number in range(seconds * 1000):
            writer.writerow([number, 0, 56, T0 + number * 1000 + 200, 0,
                             T0 + number * 1000 + 2000])


def needs_referee(test):
    return unittest.skipUnless(REFEREE.is_file(), f"{REFEREE} is not here (NEEDLETAIL_COMPARE_ROOT)")(test)


@needs_referee
class Null(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cw = null_window.referee()
        cls.directory = tempfile.TemporaryDirectory()
        rng = random.Random(7)
        jitter = [1500 + int(rng.expovariate(1 / 300)) for _ in range(310_000)]
        cls.steady = pathlib.Path(cls.directory.name) / "steady.probe.csv"
        write_frames(cls.steady, 310, lambda n: jitter[n])
        cls.arrivals = pathlib.Path(cls.directory.name) / "steady.arrivals.csv"
        write_arrivals(cls.arrivals, 310)
        # A stop of 300 ms every 20 s and five units lost at 100 s: a probe
        # whose steady state is not.
        cls.stalled = pathlib.Path(cls.directory.name) / "stalled.probe.csv"
        write_frames(cls.stalled, 310, lambda n: jitter[n],
                     lambda n: max(0, 300_000 - (n % 20_000) * 1000) if n % 20_000 < 300 else 0,
                     drop=frozenset(range(100_000, 100_005)))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_null_uses_the_referee_s_score(self):
        calls = []
        score = self.cw.score

        def counted(*args):
            calls.append(args)
            return score(*args)

        self.cw.score = counted
        try:
            result = null_window.analyse(self.cw, [str(self.steady)], [], [8], 5.0, 97.5, 120)
        finally:
            self.cw.score = score
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3:], (5_000_000, 97.5, 120))
        item = result["probes"][0]["lengths"]["8"]
        self.assertGreaterEqual(item["windows"], 120)
        # The split by criterion is the referee's null_fail_rate, window
        # for window.
        self.assertEqual(round(item["fails"]["any"] / item["windows"], 3), item["null_fail_rate"])
        self.assertEqual(item["scored"], item["windows"])
        # It is the referee's file that was loaded, from its worktree.
        self.assertEqual(pathlib.Path(self.cw.__file__).resolve(), REFEREE.resolve())

    def test_floor(self):
        result = null_window.analyse(self.cw, [str(self.steady), str(self.stalled)],
                                     [str(self.arrivals)], [8], 5.0, 97.5, 120)
        steady = [probe for probe in result["probes"] if probe["frames"] == str(self.steady)]
        stalled = [probe for probe in result["probes"] if probe["frames"] == str(self.stalled)]
        self.assertEqual([probe["basis"] for probe in steady],
                         ["listener", "kernel", "publish_to_kernel"])
        listener = steady[0]
        item = listener["lengths"]["8"]
        self.assertEqual(item["floor"], 0.025)
        # A stationary probe fails each criterion near its floor.
        self.assertLessEqual(item["rates"]["silence"], 2 * item["floor"])
        self.assertLessEqual(item["rates"]["p99"], 2 * item["floor"])
        self.assertEqual(item["fails"]["missing"], 0)
        self.assertTrue(listener["acceptance"]["judged"])
        self.assertTrue(listener["acceptance"]["met"], listener["acceptance"])
        # Publish to kernel is 1.8 ms flat: every window alike.
        publish = steady[2]["lengths"]["8"]
        self.assertEqual(publish["p99_range_ms"], [1.8, 1.8])
        # Lost units fail every window that holds them; stops this frequent
        # set the bar themselves (the steady longest silence says them).
        item = stalled[0]["lengths"]["8"]
        self.assertGreater(item["fails"]["missing"], 0)
        self.assertFalse(stalled[0]["acceptance"]["met"])
        self.assertTrue(any("miss units" in line for line in stalled[0]["acceptance"]["failures"]))
        self.assertGreaterEqual(stalled[0]["steady_gap_ms"], 299)
        self.assertGreaterEqual(item["gap_bar_ms"], 299)

    def test_acceptance_at_8_and_30_s_on_several_stationary_probes(self):
        """3.1 on the 23 Nov bar. At 30 s windows on a 310 s probe a
        stationary probe fails the silence criterion in up to 15 % of its
        windows (its largest silence fails every window that holds it), so
        a fixed limit of twice the floor (5 %) called most stationary
        probes NOT met. Four stationary probes of the 2.5 ms lane, on seeds
        the calibration does not use, meet the calibrated limits at both
        lengths; the same probe with a minute of wider spread does not."""
        rates = []
        with tempfile.TemporaryDirectory() as directory:
            for seed in range(4):
                rng = random.Random(1000 + seed)
                path = pathlib.Path(directory) / f"stationary-{seed}.probe.csv"
                write_frames(path, 310, lambda _n, rng=rng: 1500 + int(rng.expovariate(1 / 300)),
                             spacing_us=2500)
                probe = null_window.analyse(self.cw, [str(path)], [], [8, 30], 5.0, 97.5,
                                            120)["probes"][0]
                self.assertTrue(probe["acceptance"]["met"], (seed, probe["acceptance"]))
                rates.append(probe["lengths"]["30"]["rates"]["silence"])
                limit = probe["lengths"]["30"]["limit"]
                self.assertEqual((limit["probe_s"], limit["seeds"]), (310, 20))
            rng = random.Random(2000)
            worse = pathlib.Path(directory) / "worse.probe.csv"
            write_frames(worse, 310, lambda n: 1500 + int(rng.expovariate(
                1 / (3000 if 100_000 <= n * 2500 // 1000 < 160_000 else 300))), spacing_us=2500)
            probe = null_window.analyse(self.cw, [str(worse)], [], [8, 30], 5.0, 97.5,
                                        120)["probes"][0]
        # Twice the floor would have failed most of them on silence at 30 s.
        self.assertGreater(sum(1 for rate in rates if rate > 0.05), len(rates) // 2)
        self.assertFalse(probe["acceptance"]["met"])
        self.assertTrue(any("p99 fails" in line for line in probe["acceptance"]["failures"]))

    def test_a_short_probe_is_not_scored(self):
        short = pathlib.Path(self.directory.name) / "short.probe.csv"
        write_frames(short, 60, lambda n: 1500 + n % 7)
        probe = null_window.analyse(self.cw, [str(short)], [], [8], 5.0, 97.5, 120)["probes"][0]
        item = probe["lengths"]["8"]
        self.assertEqual(item["scored"], 0)
        self.assertIsNone(item["null_fail_rate"])
        self.assertIn("null too short", item["note"])
        self.assertFalse(probe["acceptance"]["judged"])
        self.assertIn("60 s", null_window.format_result({"probes": [probe]}))


def wave7(step: str, host: str, lane: str = "nt-uc") -> pathlib.Path:
    return RUNS / step / f"{host}-view" / f"{lane}.probe.csv"


# Departure 1's [R] inputs: the four cleanest wave 7 sessions.
CLEAN = ["waudio7-pin/audio.opus-2.5.baseline.clean",
         "waudio7-unpin/audio.opus-2.5.baseline.clean",
         "waudio7/audio.pcm.baseline.clean",
         "waudio7-std/audio.opus-2.5.baseline.clean"]
HOSTS = ("lon", "new", "tok", "syd")


@needs_referee
@unittest.skipUnless(all(wave7(step, host).is_file() for step in CLEAN for host in HOSTS),
                     "the referee's wave 7 probes are not on this machine")
class Wave7(unittest.TestCase):
    def test_e4_steady_figures(self):
        """E4: inside one clean 60 s probe (lon-view, waudio7-pin) the p99 of
        8 s windows reaches 6.61 ms and the longest steady silence is 12.2
        ms (New York 34.2, Sydney 25.6); 60 s is too short for the bar."""
        cw = null_window.referee()
        frames = [str(wave7(CLEAN[0], host)) for host in ("lon", "new", "syd")]
        probes = null_window.analyse(cw, frames, [], [8], 5.0, 97.5, 120)["probes"]
        london, newyork, sydney = probes
        self.assertAlmostEqual(london["steady_gap_ms"], 12.2, delta=0.06)
        self.assertAlmostEqual(newyork["steady_gap_ms"], 34.2, delta=0.06)
        self.assertAlmostEqual(sydney["steady_gap_ms"], 25.6, delta=0.06)
        item = london["lengths"]["8"]
        self.assertAlmostEqual(item["p99_range_ms"][1], 6.61, delta=0.01)
        self.assertEqual(item["scored"], 0)

    def test_e4_rates_under_the_old_bar(self):
        """Departure 1 [R]: 8 s windows from first arrival + 7 s to last
        arrival - 1.5 s, the bar of before 23 Nov: 144 of 704 fail (66 on
        silence, 125 on p99, none missing); by region London 22, New York
        30, Tokyo 21, Sydney 9 % (E4: 23, 30, 22, 9)."""
        try:
            text = subprocess.run(
                ["git", "-C", str(null_window.compare_root()), "show",
                 f"{OLD_RULE_COMMIT}:scripts/compare/change-window.py"],
                capture_output=True, check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest(f"the referee's history ({OLD_RULE_COMMIT}) is not readable here")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "change_window_before_23_nov.py"
            path.write_bytes(text)
            old = null_window.referee(path)
        totals, regions = collections.Counter(), collections.defaultdict(collections.Counter)
        for step in CLEAN:
            for host in HOSTS:
                rows = old.load(wave7(step, host))
                arrivals = sorted(row[1] for row in rows)
                t = arrivals[0] + 7_000_000
                while t + 8_000_000 <= arrivals[-1] - 1_500_000:
                    result = old.score(rows, t, t + 8_000_000, 5_000_000)
                    t += 1_000_000
                    if "error" in result:
                        continue
                    totals["windows"] += 1
                    regions[host]["windows"] += 1
                    totals["silence"] += result["longest_gap_ms"] > (
                        result["longest_gap_outside_ms"] + result["unit_spacing_ms"])
                    totals["p99"] += result["inside_ms"]["p99"] > result["outside_ms"]["p99"] + 1
                    totals["missing"] += result["missing"] > 0
                    if not result["pass"]:
                        totals["fails"] += 1
                        regions[host]["fails"] += 1
        self.assertEqual((totals["windows"], totals["fails"], totals["silence"], totals["p99"],
                          totals["missing"]), (704, 144, 66, 125, 0))
        rates = {host: 100 * regions[host]["fails"] / regions[host]["windows"] for host in HOSTS}
        for host, figure in zip(HOSTS, (23, 30, 22, 9)):
            self.assertAlmostEqual(rates[host], figure, delta=1.5, msg=host)


if __name__ == "__main__":
    unittest.main()
