#!/usr/bin/env python3
"""Tests of part-sweep.py: its commands and its table; nothing is started.
python3 -m unittest scripts/compare/test_part_sweep.py"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("part_sweep", HERE / "part-sweep.py")
sweep = importlib.util.module_from_spec(_spec)
sys.modules["part_sweep"] = sweep
_spec.loader.exec_module(sweep)

_bench_spec = importlib.util.spec_from_file_location("latency_bench", HERE / "latency-bench.py")
bench = importlib.util.module_from_spec(_bench_spec)
sys.modules["latency_bench"] = bench
_bench_spec.loader.exec_module(bench)


def hops_json(directory: pathlib.Path, name: str, samples: int, h3_p50: float, london_p99: float,
              note: str | None = None) -> None:
    run = directory / name
    run.mkdir(parents=True)
    result = {
        "source": {"epoch_samples": samples},
        "regions": {
            "all": {"listener_parts": {"from_source": [{"stage": "listener.complete", "p50": 0, "p99": 0}]}},
            "london": {"listener_parts": {"from_source": [
                {"stage": "edge.reply", "count": 9, "p50": 1.0, "p99": 2.0},
                {"stage": "listener.complete", "count": 900, "p50": london_p99 - 1, "p99": london_p99}]}},
            "newyork": {"edge_parts": {"from_source": []}},
        },
        "bench": {"readers": [
            {"report": "lon-readers.h2.probe.json", "units": 100, "gaps": 0,
             "latency_ms": {"p50": h3_p50 + 1, "p99": h3_p50 + 2}},
            {"report": "lon-readers.h3.probe.json", "units": 100, "gaps": 0,
             "latency_ms": {"p50": h3_p50, "p99": h3_p50 + 1}},
            {"report": "new-readers.h3.probe-001.json", "units": 0, "error": "no audio",
             "latency_ms": {}},
        ], "epoch_note": note, "journal_full": []},
    }
    (run / "hops.json").write_text(json.dumps(result))


class Commands(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(sweep.parse_sizes(sweep.DEFAULT_SIZES),
                         [("pcm", 48), ("pcm", 24), ("pcm", 12), ("flac", 48), ("flac", 32), ("opus", 120)])
        for bad in ("pcm", "aac:48", "pcm:0", "pcm:x", ""):
            with self.assertRaises(SystemExit, msg=bad):
                sweep.parse_sizes(bad)

    def test_the_windows_are_the_same_time_at_every_size(self):
        self.assertEqual(sweep.window_parts(120, 100), 40)    # 2.5 ms
        self.assertEqual(sweep.window_parts(48, 100), 100)    # 1 ms
        self.assertEqual(sweep.window_parts(24, 100), 200)
        self.assertEqual(sweep.window_parts(12, 100), 400)
        self.assertEqual(sweep.window_parts(12, 1000), 400)   # at most 400 parts
        self.assertEqual(sweep.window_parts(32, 100), 150)    # 0.667 ms
        self.assertEqual(sweep.window_parts(960, 1), 1)
        # HTTP/2: 5 ms in flight, one part of 5 ms as before.
        self.assertEqual([sweep.window_parts(n, 5.0) for n in (240, 120, 48, 32, 24, 12)],
                         [1, 2, 5, 8, 10, 20])

    def test_dry_run_commands_are_latency_bench_runs(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sweep.main(["--name", "s", "--dry-run", "--seconds", "30",
                                         "--bench-args", '--mesh-args="--x 1"']), 0)
        commands = json.loads(out.getvalue())
        self.assertEqual(list(commands), ["s-pcm-48", "s-pcm-24", "s-pcm-12", "s-flac-48", "s-flac-32",
                                          "s-opus-120"])
        for name, command in commands.items():
            payload, samples = name.split("-")[1:]
            self.assertEqual(command[1], str(sweep.BENCH))
            argv = command[2:]
            self.assertEqual(argv[argv.index("--payload") + 1], payload)
            self.assertEqual(argv[argv.index("--epoch-samples") + 1], samples)
            self.assertEqual(argv[argv.index("--seconds") + 1], "30")
            self.assertEqual(argv[-1], "--mesh-args=--x 1")
            # Every command is one that latency-bench accepts.
            with contextlib.redirect_stdout(io.StringIO()) as plan:
                self.assertEqual(bench.main([*argv, "--dry-run"]), 0)
            planned = json.loads(plan.getvalue())
            self.assertEqual((planned["payload"], planned["epoch_samples"]), (payload, int(samples)))
            # Neither reader is left at one part in flight below 5 ms parts.
            self.assertEqual(planned["reader_windows"]["h2"], sweep.window_parts(int(samples), 5.0))
            self.assertEqual(planned["reader_windows"]["h3"], sweep.window_parts(int(samples), 100))


class Table(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        patcher = mock.patch.object(sweep, "OUT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_report_marks_the_fastest_lossless_size(self):
        hops_json(self.root, "s-pcm-48", 48, h3_p50=4.0, london_p99=6.0)
        hops_json(self.root, "s-pcm-24", 24, h3_p50=3.5, london_p99=5.5)
        hops_json(self.root, "s-pcm-12", 12, h3_p50=3.8, london_p99=5.0, note="the sender left no summary")
        hops_json(self.root, "s-opus-120", 120, h3_p50=1.0, london_p99=1.0)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sweep.main(["--name", "s", "--report-only",
                                         "--sizes", "pcm:48,pcm:24,pcm:12,flac:32,opus:120"]), 0)
        result = json.loads((self.root / "s" / "sweep.json").read_text())
        rows = {f"{row['payload']}:{row['epoch_samples']}": row for row in result["rows"]}
        self.assertEqual(rows["pcm:24"]["readers"]["lon-readers.h3"], {"p50": 3.5, "p99": 4.5, "units": 100,
                                                                        "gaps": 0})
        self.assertEqual(rows["pcm:24"]["regions"], {"london": {"p50": 4.5, "p99": 5.5, "parts": 900}})
        self.assertIn("error", rows["pcm:24"]["readers"]["new-readers.h3-001"])
        self.assertEqual(rows["pcm:12"]["warning"], "the sender left no summary")
        self.assertEqual(rows["flac:32"]["error"], "no hops.json")
        self.assertEqual(rows["pcm:12"]["part_ms"], 0.25)
        self.assertEqual(rows["pcm:12"]["parts_per_second"], 4000)
        text = out.getvalue()
        self.assertEqual(text, (self.root / "s" / "sweep.txt").read_text())
        line = {row.split("  ")[0]: row for row in text.splitlines() if row.startswith(("pcm", "opus"))}
        self.assertIn("3.50* / 4.50*", line["pcm 24"])       # h3 reader: pcm 24 is the fastest
        self.assertIn("5.00*", line["pcm 12"])                # source -> listener london p99
        self.assertNotIn("*", line["opus 120"])               # Opus is not a lossless size
        self.assertIn("flac 32: no hops.json", text)
        # Each size says what each lane had in flight.
        self.assertEqual(rows["pcm:12"]["windows"], {"h2": 20, "h3": 400})
        self.assertEqual(result["h2_window_ms"], 5.0)
        self.assertIn("in flight h2/h3", text)
        self.assertIn("20/400", line["pcm 12"])


if __name__ == "__main__":
    unittest.main()
