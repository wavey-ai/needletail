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
        journal = ["--hop-journal-capacity", "1200000"]
        self.assertEqual(settings["daw_hls_formats"], "pcm")
        self.assertEqual(settings["pcm_epoch_samples"], "48")
        self.assertEqual(json.loads(settings["extra_backbone_args"]), ["--mesh-sync-on-publish", "false"] + journal)
        self.assertEqual(json.loads(settings["extra_distributor_args"]),
                         ["--y", "--mesh-sync-on-publish", "false"] + journal)
        self.assertEqual(json.loads(settings["extra_edge_args"]),
                         ["--mesh-sync-on-publish", "false", "--x", "1"] + journal)
        self.assertEqual(json.loads(settings["extra_contributor_args"]),
                         ["--publication-shard-mix", "false"] + journal)
        self.assertEqual(settings["mesh_sync_interval_ms"], "20")

    def test_no_flags_change_nothing_but_the_format_the_epoch_and_the_journal(self):
        journal = json.dumps(["--hop-journal-capacity", "1200000"])
        self.assertEqual(bench.lab_overrides(parse()),
                         ["daw_hls_formats=pcm", "pcm_epoch_samples=48"]
                         + [f"extra_{role}_args={journal}" for role in
                            ("backbone", "distributor", "edge", "contributor")])
        self.assertEqual(bench.lab_overrides(parse("--payload", "opus"))[:2],
                         ["daw_hls_formats=opus", "opus_epoch_samples=120"])

    def test_the_journal_holds_the_whole_run(self):
        # 10 events of each part at the run's part rate over the run and 60 s,
        # never below the default. 48-sample parts: 1,000 a second.
        self.assertEqual(bench.journal_capacity(parse("--seconds", "10")), 700_000)
        self.assertEqual(bench.journal_capacity(parse("--seconds", "600")), 6_600_000)
        self.assertEqual(bench.journal_capacity(parse("--seconds", "600", "--journal-capacity", "7")), 7)
        # 12-sample parts: 4,000 a second; Opus 120 samples: 400.
        self.assertEqual(bench.journal_capacity(parse("--seconds", "60", "--epoch-samples", "12")),
                         4_800_000)
        self.assertEqual(bench.journal_capacity(parse("--seconds", "60", "--payload", "opus")),
                         480_000)
        self.assertEqual(bench.journal_capacity(parse("--seconds", "1", "--payload", "opus",
                                                      "--epoch-samples", "960")), 262_144)
        # The busiest node writes about 9 events of each part.
        self.assertGreater(bench.journal_capacity(parse("--seconds", "600")), 9 * 1000 * (600 + 30))
        # A role that sets its own keeps it, and the flag is not given twice.
        args = parse("--seconds", "600", "--edge-args", "--hop-journal-capacity 99",
                     "--contrib-args=--hop-journal-capacity=5")
        settings = {key: json.loads(value) for key, value in
                    (item.split("=", 1) for item in bench.lab_overrides(args)) if key.startswith("extra_")}
        self.assertEqual(settings["extra_edge_args"], ["--hop-journal-capacity", "99"])
        self.assertEqual(settings["extra_contributor_args"], ["--hop-journal-capacity=5"])
        self.assertEqual(settings["extra_backbone_args"], ["--hop-journal-capacity", "6600000"])
        self.assertEqual(dry_run("--seconds", "600")["hops"]["journal_capacity"], 6_600_000)
        self.assertIsNone(dry_run("--no-up")["hops"]["journal_capacity"])

    def test_a_full_journal_is_reported(self):
        events = [{"_node": "contrib-lon"}] * 5 + [{"_node": "lon-edge-1"}] * 4
        self.assertEqual(bench.journal_full(events, 5), ["contrib-lon"])
        self.assertEqual(bench.journal_full(events, 6), [])
        self.assertEqual(bench.journal_full(events, None), [])

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
        # One 48-sample epoch (1 ms) after the loop start.
        self.assertEqual(sender[sender.index("--session-id") + 1], str(start * 10**9 + 1_000_000))
        self.assertEqual(sender[sender.index("--payload") + 1], "pcm")
        self.assertEqual(sender[sender.index("--epoch-samples") + 1], "48")
        self.assertEqual(plan["media"]["index_command"][-1], "1.0")
        self.assertTrue(plan["media"]["index"].endswith("pcm-48s.index.json"))
        self.assertEqual(h2[h2.index("--lane") + 1], "nt-pcm-h2")
        self.assertEqual(sender[sender.index("--target") + 1], "127.0.0.1:27100")
        self.assertEqual(sender[-2:], ["--pace", "tokio"])
        self.assertEqual(plan["hops"]["source_start_ns"], start * 10**9)
        self.assertIn("loss random 5.0%", plan["reader_netem"])

    def test_no_up_refuses_service_flags(self):
        with self.assertRaises(SystemExit):
            bench.main(["--name", "t", "--dry-run", "--no-up", "--mesh-args", "--a b"])
        self.assertIsNone(dry_run("--no-up")["lab_up"])

    def test_opus_by_the_probe(self):
        plan = dry_run("--payload", "opus")
        start = plan["loop_start_unix"]
        sender = plan["sender"]["command"]
        self.assertEqual(sender[sender.index("--payload") + 1], "opus")
        self.assertEqual(sender[sender.index("--epoch-samples") + 1], "120")
        self.assertEqual(sender[sender.index("--session-id") + 1], str(start * 10**9 + 2_500_000))
        self.assertEqual(plan["hops"]["stream"], 3001)
        self.assertEqual(plan["epoch_us"], 2500.0)
        # Opus parts are recognised by number: part N is epoch N of the loop.
        self.assertEqual(plan["media"]["index_command"][2:], ["numbered:nt-opus-120s",
                                                              plan["media"]["index"], "2.5", "0"])
        h3 = next(item["command"] for item in plan["readers"] if item["lane"] == "h3")
        self.assertIn("/live/3001/master.m3u8", h3[h3.index("--url") + 1])
        self.assertEqual(h3[h3.index("--lane") + 1], "nt-opus-h3")

    def test_the_halving_sizes(self):
        for samples, ms, session in (("24", "0.5", 500_000), ("12", "0.25", 250_000)):
            plan = dry_run("--epoch-samples", samples)
            sender = plan["sender"]["command"]
            self.assertEqual(sender[sender.index("--session-id") + 1],
                             str(plan["loop_start_unix"] * 10**9 + session))
            self.assertEqual(plan["media"]["index_command"][-1], ms)
        plan = dry_run("--payload", "flac", "--epoch-samples", "32")
        self.assertEqual(plan["hops"]["stream"], 2001)
        self.assertEqual(plan["media"]["index_command"][2], "numbered:nt-flac-32s")
        # 32 samples are 0.666... ms: ntcmp reads the text back to 32 samples.
        text = plan["media"]["index_command"][4]
        self.assertEqual(int(float(text) * 48000 / 1000), 32)
        sender = plan["sender"]["command"]
        self.assertEqual(sender[sender.index("--session-id") + 1],
                         str(plan["loop_start_unix"] * 10**9 + 666_667))

    def test_sizes_the_payload_cannot_take_are_refused(self):
        for argv in (["--epoch-samples", "6"], ["--payload", "flac", "--epoch-samples", "24"],
                     ["--payload", "opus", "--epoch-samples", "100"]):
            with self.assertRaises(SystemExit, msg=argv):
                bench.main(["--name", "t", "--dry-run", *argv])

    def test_the_epoch_is_the_senders(self):
        steps = {"epoch_samples": 48, "payload": "pcm"}
        self.assertEqual(bench.run_epoch(steps, None)["epoch_us"], 1000.0)
        self.assertIsNotNone(bench.sender_mismatch(steps, None))
        summary = {"epochs": 9, "epoch_samples": 48, "payload": "pcm", "send_lateness_ms": {"p50": 0.01}}
        self.assertIsNone(bench.sender_mismatch(steps, summary))
        self.assertEqual(bench.run_epoch(steps, summary)["from"], "sender_summary")
        self.assertEqual(bench.sender_digest(summary)["send_lateness_ms"], {"p50": 0.01})
        other = {**summary, "epoch_samples": 240}
        self.assertEqual(bench.run_epoch(steps, other)["epoch_us"], 5000.0)
        self.assertIn("240-sample", bench.sender_mismatch(steps, other))

    def test_window_is_bounded(self):
        with self.assertRaises(SystemExit):
            bench.main(["--name", "t", "--dry-run", "--h3-window", "401"])


if __name__ == "__main__":
    unittest.main()
