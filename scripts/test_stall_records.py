#!/usr/bin/env python3
"""Tests of scripts/stall_records.py, the reader of the stall journal and
of `needletail-nodecheck watch` files (D-hunt.md 2.1, 2.2): a merged event
written twice counts once; a tick is stopped or starved by its run delay
and its scope is all only when every other CPU stopped over half of it;
F averages the stopped ticks of 20 ms or more over the CPUs; steal and the
guest kernel's share are read from the sec lines over the time they cover.
python3 -m unittest scripts/test_stall_records.py"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import stall_records  # noqa: E402

T = 1_795_381_726_000_000


def tick(cpu: int, end_us: int, dur_us: int, run_delay_us: int = 10, **more) -> dict:
    return {"kind": "tick", "thread": f"tick-{cpu}", "cpu": cpu, "t_us": end_us,
            "dur_us": dur_us, "span_us": dur_us, "n": 1, "run_delay_us": run_delay_us, **more}


def sec(cpu: int, t_us: int, **ticks) -> dict:
    return {"kind": "sec", "cpu": cpu, "t_us": t_us, "span_us": 1_000_000,
            **{key: 0 for key in ("user", "system", "irq", "softirq", "steal")}, **ticks}


class Records(unittest.TestCase):
    def test_a_merged_event_written_again_counts_once(self):
        first = {"kind": "class_pass", "thread": "audio", "cpu": 1, "t_us": T + 3000,
                 "dur_us": 3000, "span_us": 3000, "n": 1}
        grown = {**first, "t_us": T + 9000, "dur_us": 4000, "span_us": 9000, "n": 2}
        other = {**first, "thread": "video"}
        kept = stall_records.dedupe([first, other, grown])
        self.assertEqual(len(kept), 2)
        self.assertIn(grown, kept)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "watch.ndjson"
            path.write_text("\n".join(json.dumps(item) for item in [first, grown])
                            + '\n{"kind": "tick", "t_us"')     # cut off at the end
            self.assertEqual(stall_records.read_stalls(path), [grown])

    def test_cause_and_scope_of_a_tick(self):
        stopped = tick(0, T + 700_000, 673_000, run_delay_us=212)
        starved = tick(0, T, 50_000, run_delay_us=40_000)
        self.assertEqual(stall_records.tick_cause(stopped), "stopped")
        self.assertEqual(stall_records.tick_cause(starved), "starved")
        both = [stopped, tick(1, T + 690_000, 600_000), sec(0, T), sec(1, T)]
        self.assertEqual(stall_records.tick_scope(stopped, both), "all")
        one = [stopped, tick(1, T + 690_000, 100_000), sec(0, T), sec(1, T)]
        self.assertEqual(stall_records.tick_scope(stopped, one), "one")
        # The journal's own word is kept.
        self.assertEqual(stall_records.tick_scope({**stopped, "scope": "one"}, both), "one")

    def test_frozen_time_per_minute(self):
        events = [sec(0, T), sec(1, T),
                  tick(0, T + 10_000_000, 100_000), tick(1, T + 10_000_000, 100_000),
                  tick(0, T + 20_000_000, 10_000),                  # under 20 ms
                  tick(1, T + 30_000_000, 60_000, run_delay_us=50_000)]   # starved
        self.assertAlmostEqual(stall_records.stopped_ms(events), 100.0)
        self.assertAlmostEqual(stall_records.frozen_per_minute(events, T, T + 30_000_000), 200.0)
        self.assertAlmostEqual(stall_records.stopped_ms(events, cpus=4), 50.0)
        counts = stall_records.stop_counts(events)
        self.assertEqual(counts["stopped"]["over_20ms"], {"all": 2, "one": 0})
        self.assertEqual(counts["stopped"]["over_100ms"], {"all": 2, "one": 0})
        self.assertEqual(counts["starved"]["over_20ms"], 1)

    def test_steal_and_the_guest_kernel(self):
        lines = [sec(0, T, steal=28), sec(1, T, steal=0, system=70, softirq=5)]
        self.assertEqual(stall_records.steal_permille(lines), 140.0)
        self.assertFalse(stall_records.kernel_second(lines[0]))
        self.assertTrue(stall_records.kernel_second(lines[1]))
        self.assertIsNone(stall_records.steal_permille([]))


class SamplerFigures(unittest.TestCase):
    def test_run_delay_counts_long_waits_of_one_thread_only(self):
        rows = [[1, 7, 9, 0], [2, 7, 9, 10_000_000], [3, 7, 9, 310_000_000],
                [4, 8, 9, 0], [5, 8, 9, 400_000_000], [6, 8, 9, None]]
        # 300 ms in one second; 10 ms is under the threshold; the restart
        # (pid 7 to 8) is no wait; 400 ms of the new process counts.
        self.assertAlmostEqual(stall_records.run_delay_ms(rows, 50.0), 700.0)

    def test_a_counter_is_found_by_its_name_on_any_path(self):
        line = {"mesh": {"relay_session.fast_plane.audio_loop_busy_max_us": 812,
                         "other.audio_loop_busy_max_us": 900, "stall.stopped_ms": 0}}
        self.assertEqual(stall_records.mesh_value(line, "audio_loop_busy_max_us"), 900)
        self.assertIsNone(stall_records.mesh_value(line, "fanout_started"))
        self.assertIsNone(stall_records.mesh_value({"mesh": None}, "stopped_ms"))


if __name__ == "__main__":
    unittest.main()
