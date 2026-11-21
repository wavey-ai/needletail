#!/usr/bin/env python3
"""Tests of latency-bench.py's arguments and commands; nothing is started.
python3 -m unittest scripts/compare/test_latency_bench.py"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("latency_bench", HERE / "latency-bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["latency_bench"] = bench
_spec.loader.exec_module(bench)


def parse(*argv: str):
    return bench.build_parser().parse_args(["--name", "t", *argv])


def dry_run(*argv: str) -> dict:
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = bench.main(["--name", "t", "--dry-run", *argv])
    assert code == 0
    return json.loads(out.getvalue())


class Arguments(unittest.TestCase):
    def test_flags_reach_every_role(self):
        args = parse("--mesh-args", "--mesh-sync-on-publish false", "--edge-args", "--x 1",
                     "--contrib-args", "--publication-shard-mix false",
                     "--set", "extra_distributor_args=[\"--y\"]", "--set", "mesh_sync_interval_ms=20")
        settings = dict(item.split("=", 1) for item in bench.lab_overrides(args))
        self.assertEqual(settings["daw_hls_formats"], "pcm")
        self.assertEqual(json.loads(settings["extra_backbone_args"]), ["--mesh-sync-on-publish", "false"])
        self.assertEqual(json.loads(settings["extra_distributor_args"]),
                         ["--y", "--mesh-sync-on-publish", "false"])
        self.assertEqual(json.loads(settings["extra_edge_args"]),
                         ["--mesh-sync-on-publish", "false", "--x", "1"])
        self.assertEqual(json.loads(settings["extra_contributor_args"]), ["--publication-shard-mix", "false"])
        self.assertEqual(settings["mesh_sync_interval_ms"], "20")

    def test_no_flags_change_nothing_but_the_format(self):
        self.assertEqual(bench.lab_overrides(parse()), ["daw_hls_formats=pcm"])

    def test_loop_start_is_a_64_second_boundary_8_s_away(self):
        self.assertEqual(bench.loop_start(640.0), 704)
        self.assertEqual(bench.loop_start(700.0), 768)       # 704 is only 4 s away
        self.assertEqual(bench.loop_start(695.9), 704)        # 8.1 s away
        self.assertEqual(bench.loop_start(696.5), 768)

    def test_dry_run_plan(self):
        plan = dry_run("--seconds", "45", "--readers", "3", "--sender-args", "--pace tokio",
                       "--reader-loss", "5")
        self.assertEqual(len(plan["readers"]), 4)
        by_kind = {(item["host"], item["lane"]): item for item in plan["readers"]}
        self.assertEqual(set(by_kind), {("lon-readers", "h2"), ("lon-readers", "h3"),
                                        ("new-readers", "h2"), ("new-readers", "h3")})
        h2 = by_kind[("lon-readers", "h2")]["command"]
        h3 = by_kind[("new-readers", "h3")]["command"]
        self.assertEqual(h2[h2.index("--window") + 1], "1")
        self.assertNotIn("--http3", h2)
        self.assertEqual(h3[h3.index("--window") + 1], "40")
        self.assertIn("--http3", h3)
        self.assertIn("https://10.90.2.11:19444/live/4001/master.m3u8", h3[h3.index("--url") + 1])
        self.assertEqual(h2[h2.index("--count") + 1], "3")
        self.assertEqual(h2[h2.index("--duration") + 1], "45")
        sender = plan["sender"]["command"]
        start = plan["loop_start_unix"]
        self.assertEqual(sender[sender.index("--session-id") + 1], str(start * 10**9 + 5_000_000))
        self.assertEqual(sender[sender.index("--payload") + 1], "pcm")
        self.assertEqual(sender[sender.index("--target") + 1], "127.0.0.1:27100")
        self.assertEqual(sender[-2:], ["--pace", "tokio"])
        self.assertEqual(plan["hops"]["source_start_ns"], start * 10**9)
        self.assertIn("loss random 5.0%", plan["reader_netem"])

    def test_no_up_refuses_service_flags(self):
        with self.assertRaises(SystemExit):
            bench.main(["--name", "t", "--dry-run", "--no-up", "--mesh-args", "--a b"])
        self.assertIsNone(dry_run("--no-up")["lab_up"])

    def test_window_is_bounded(self):
        with self.assertRaises(SystemExit):
            bench.main(["--name", "t", "--dry-run", "--h3-window", "401"])


if __name__ == "__main__":
    unittest.main()
