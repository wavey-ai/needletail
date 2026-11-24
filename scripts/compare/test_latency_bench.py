#!/usr/bin/env python3
"""Tests of latency-bench.py's arguments and commands; nothing is started.
python3 -m unittest scripts/compare/test_latency_bench.py"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import os
import io
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("latency_bench", HERE / "latency-bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["latency_bench"] = bench
_spec.loader.exec_module(bench)
_ARCHIVE: list = []


def setUpModule():
    """A pass's run goes into a temporary archive, never ~/needletail-data."""
    data = tempfile.mkdtemp()
    _ARCHIVE.append(data)
    for patcher in (mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": data}),
                    mock.patch.object(bench.archived_run, "code_paths", lambda *_: [])):
        patcher.start()
        _ARCHIVE.append(patcher)


def tearDownModule():
    for patcher in _ARCHIVE[1:]:
        patcher.stop()
    shutil.rmtree(_ARCHIVE[0], ignore_errors=True)


def parse(*argv: str):
    return bench.build_parser().parse_args(["--name", "t", *argv])


def dry_run(*argv: str) -> dict:
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = bench.main(["--name", "t", "--dry-run", *argv])
    assert code == 0
    return json.loads(out.getvalue())


class Arguments(unittest.TestCase):
    def test_flags_reach_every_role(self):
        args = parse("--mesh-args", "--relay-forward-nonblocking false", "--edge-args", "--x 1",
                     "--contrib-args", "--publication-shard-mix false",
                     "--set", "extra_distributor_args=[\"--y\"]", "--set", "mesh_sync_interval_ms=20")
        settings = dict(item.split("=", 1) for item in bench.lab_overrides(args))
        journal = ["--hop-journal-capacity", "1200000"]
        self.assertEqual(settings["daw_hls_formats"], "pcm")
        self.assertEqual(settings["pcm_epoch_samples"], "48")
        self.assertEqual(json.loads(settings["extra_backbone_args"]), ["--relay-forward-nonblocking", "false"] + journal)
        self.assertEqual(json.loads(settings["extra_distributor_args"]),
                         ["--y", "--relay-forward-nonblocking", "false"] + journal)
        self.assertEqual(json.loads(settings["extra_edge_args"]),
                         ["--relay-forward-nonblocking", "false", "--x", "1"] + journal)
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
                         ["daw_hls_formats=opus", "opus_epoch_samples=48"])

    def test_the_caches_keep_12_s_of_the_run_parts(self):
        # The lab renders through the deploy tool, which counts the window in
        # parts of the shortest epoch: 12 s of 0.25 ms parts, not 1.2 s.
        deploy = bench.lab.ROOT / "scripts" / "linode-small-mesh-deploy.py"
        spec = importlib.util.spec_from_file_location("deploy_for_bench", deploy)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for argv, parts in ((("--epoch-samples", "12"), "48000"), ((), "12000"),
                            (("--payload", "opus"), "12000")):
            plain = [item for item in bench.lab_overrides(parse(*argv))
                     if not item.startswith("extra_")]
            settings, _ = module.merge_settings({}, {**bench.lab.DEFAULT_OVERRIDES,
                                                     **module.parse_overrides(plain)})
            args = module.part_plan(settings)["mesh_args"]
            self.assertEqual(args[args.index("--window-parts") + 1], parts, argv)

    def test_the_journal_holds_the_whole_run(self):
        # 10 events of each part at the run's part rate over the run and 60 s,
        # never below the default. 48-sample parts: 1,000 a second.
        self.assertEqual(bench.journal_capacity(parse("--seconds", "10")), 700_000)
        self.assertEqual(bench.journal_capacity(parse("--seconds", "600")), 6_600_000)
        self.assertEqual(bench.journal_capacity(parse("--seconds", "600", "--journal-capacity", "7")), 7)
        # 12-sample parts: 4,000 a second; Opus (1 ms Opus Custom by default): 1,000.
        self.assertEqual(bench.journal_capacity(parse("--seconds", "60", "--epoch-samples", "12")),
                         4_800_000)
        self.assertEqual(bench.journal_capacity(parse("--seconds", "60", "--payload", "opus")),
                         1_200_000)
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
        # 5 ms of 1 ms parts in flight over HTTP/2.
        self.assertEqual(h2[h2.index("--window") + 1], "5")
        self.assertEqual(plan["reader_windows"], {"h2": 5, "h3": 40})
        self.assertEqual(by_kind[("lon-readers", "h2")]["window"], 5)
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

    def test_the_start_up_counts_with_skip_seconds_0(self):
        """Wave 9 (cold start): --skip-seconds 0 keeps the first seconds in
        every figure (the readers' and the window's); 5 by default."""
        for given, expected in ((None, "5"), ("0", "0"), ("2", "2")):
            plan = dry_run(*(["--skip-seconds", given] if given else []))
            h2 = plan["readers"][0]["command"]
            self.assertEqual(h2[h2.index("--skip-seconds") + 1], expected)
            self.assertEqual(plan["hops"]["skip_seconds"], int(expected))

    def test_no_up_refuses_service_flags(self):
        with self.assertRaises(SystemExit):
            bench.main(["--name", "t", "--dry-run", "--no-up", "--mesh-args", "--a b"])
        self.assertIsNone(dry_run("--no-up")["lab_up"])

    def test_opus_by_the_probe(self):
        plan = dry_run("--payload", "opus")
        start = plan["loop_start_unix"]
        sender = plan["sender"]["command"]
        self.assertEqual(sender[sender.index("--payload") + 1], "opus")
        self.assertEqual(sender[sender.index("--epoch-samples") + 1], "48")
        self.assertEqual(sender[sender.index("--session-id") + 1], str(start * 10**9 + 1_000_000))
        self.assertEqual(plan["hops"]["stream"], 3001)
        self.assertEqual(plan["epoch_us"], 1000.0)
        # Opus parts are recognised by number: part N is epoch N of the loop.
        self.assertEqual(plan["media"]["index_command"][2:], ["numbered:nt-opus-48s",
                                                              plan["media"]["index"], "1.0", "0"])
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
        with self.assertRaises(SystemExit):
            bench.main(["--name", "t", "--dry-run", "--h2-window", "0"])
        with self.assertRaises(SystemExit):
            bench.main(["--name", "t", "--dry-run", "--h2-window-ms", "0"])

    def test_the_http2_window_keeps_up_with_the_part_rate(self):
        # A reader with W parts in flight reads at most W parts per round
        # trip. On the lab's reader links a round trip is about 2 ms (0.5 ms
        # netem each way, and the edge): window 1 reads 500 parts a second,
        # fewer than the 1,000 to 4,000 of the run. The default window keeps
        # 5 ms in flight at every size: 2.5 times the rate, room to catch up.
        round_trip_ms = 2.0
        for samples in (240, 120, 48, 32, 24, 12):
            window = bench.reader_windows(parse("--epoch-samples", str(samples),
                                                "--payload", "opus" if samples in (120, 240)
                                                else "pcm"))["h2"]
            read_per_second = window * 1000 / round_trip_ms
            self.assertGreaterEqual(read_per_second, 2.4 * bench.parts_per_second(samples), samples)
        self.assertEqual(bench.reader_windows(parse("--epoch-samples", "12"))["h2"], 20)
        self.assertEqual(bench.reader_windows(parse("--payload", "opus"))["h2"], 5)
        self.assertEqual(bench.reader_windows(parse("--payload", "opus", "--epoch-samples", "120"))["h2"], 2)
        # An explicit count wins; window 1 is still there to measure.
        self.assertEqual(bench.reader_windows(parse("--h2-window", "1"))["h2"], 1)
        self.assertEqual(bench.reader_windows(parse("--h2-window-ms", "20"))["h2"], 20)


def frames_csv(path: pathlib.Path, rows: list[tuple[int, int, int]], audio: int = 0) -> None:
    """An ntcmp record: video rows (part, complete_us, published_us), and
    some audio rows that must be left out."""
    lines = ["kind,index,latency_us,complete_unix_us,first_unix_us,bytes,key,packets,"
             "published_unix_us,cache_available_unix_us,edge_committed_unix_us,reply_unix_us,part"]
    for part, complete, published in rows:
        lines.append(f"v,-1,0,{complete},{complete - 500},520000,0,1,{published},,,,{part}")
    for index in range(audio):
        lines.append(f"a,{index},1000,{2_000_000 + index},{index},1440,0,1,1,,,,{index}")
    path.write_text("\n".join(lines) + "\n")


class Video(unittest.TestCase):
    """4K at a chosen rate (wave 5 W3)."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_rate_or_a_clip_measures_4k_in_every_region(self):
        plan = dry_run("--video-mbps", "200", "--seconds", "30")
        source = plan["video"]["source"]
        self.assertTrue(source["synthetic"])
        self.assertTrue(source["path"].endswith("media/synthetic-4k-200m-50fps-10s.ts"))
        prepare = source["prepare"]
        self.assertEqual(prepare[prepare.index("-b:v") + 1], "200M")
        self.assertEqual(prepare[prepare.index("-maxrate") + 1], "200M")
        self.assertIn("testsrc2=size=3840x2160:rate=50", prepare)
        self.assertTrue(plan["video"]["sample_nodes"])
        kinds = sorted((item["host"], item["kind"]) for item in plan["video"]["viewers"])
        self.assertEqual(kinds, [("lon-readers", "hls"), ("lon-readers", "udp"),
                                 ("new-readers", "hls"), ("new-readers", "udp")])
        hls = next(item for item in plan["video"]["viewers"]
                   if item["kind"] == "hls" and item["host"] == "new-readers")["command"]
        self.assertIn("https://10.90.2.11:19444/live/1/master.m3u8", hls[hls.index("--url") + 1])
        self.assertIn("--http3", hls)
        self.assertEqual(hls[hls.index("--window") + 1], "40")
        self.assertEqual(hls[hls.index("--duration") + 1], "30")
        self.assertNotIn("--audio-index", hls)
        udp = next(item for item in plan["video"]["viewers"] if item["kind"] == "udp")["command"]
        self.assertEqual(udp[udp.index("--stream") + 1], "1")
        self.assertEqual(udp[udp.index("--entry") + 1], "https://10.90.0.11:19444")
        # The 4K stream's parts share the journal: 50 a second more.
        self.assertEqual(plan["hops"]["journal_capacity"], 10 * 1050 * 90)
        # A named clip of ~/media/4k-encodes, and counts of each viewer.
        (self.dir / "crowd50-40m.ts").write_bytes(b"")
        with mock.patch.object(bench, "CLIPS", self.dir):
            plan = dry_run("--video-clip", "crowd50-40m", "--video-readers", "2",
                           "--video-udp-clients", "0", "--hosts", "lon-readers")
        self.assertEqual(plan["video"]["source"]["path"], str(self.dir / "crowd50-40m.ts"))
        self.assertEqual([item["kind"] for item in plan["video"]["viewers"]], ["hls"])
        hls = plan["video"]["viewers"][0]["command"]
        self.assertEqual(hls[hls.index("--count") + 1], "2")

    def test_plain_video_keeps_the_audio_gate_as_it_was(self):
        plan = dry_run("--video")
        self.assertEqual(plan["video"]["source"]["path"], str(bench.lab.VIDEO_CLIP))
        self.assertEqual(plan["video"]["viewers"], [])
        self.assertFalse(plan["video"]["sample_nodes"])
        self.assertEqual(plan["hops"]["journal_capacity"], 1_200_000)
        self.assertIsNone(dry_run()["video"]["source"])

    def test_bad_4k_arguments_are_refused(self):
        for argv in (["--video-clip", "no-such-clip"], ["--video-mbps", "0"],
                     ["--video-mbps", "80", "--video-window", "0"],
                     ["--video-mbps", "80", "--video-readers", "-1"],
                     ["--video-mbps", "80", "--frame-deadline-ms", "0"]):
            with self.assertRaises(SystemExit, msg=argv), \
                    contextlib.redirect_stdout(io.StringIO()):
                bench.main(["--name", "t", "--dry-run", *argv])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parse("--video-mbps", "80", "--video-clip", "bbb60-25m")

    def test_frames_on_time(self):
        # 100 frames, 20 ms apart, 30 ms from publish to viewer; part 150
        # is lost; part 160 takes 150 ms (late at a 100 ms deadline, on time
        # at 200), part 170 completes before the window.
        rows = []
        for part in range(100, 200):
            published = 1_000_000 + part * 20_000
            if part == 150:
                continue
            latency = 150_000 if part == 160 else 30_000
            rows.append((part, published + latency, published))
        rows.append((120, 1_000_000 + 120 * 20_000 + 90_000, 1_000_000 + 120 * 20_000))  # a repeat
        path = self.dir / "lon-readers.4k.h3.frames.csv"
        frames_csv(path, rows, audio=5)
        parts = bench.read_frames(path)
        self.assertEqual(len(parts), 99)
        self.assertEqual(parts[120][0] - parts[120][1], 30_000)    # the earliest completion
        verdict = bench.frame_verdict(parts, 100.0)
        self.assertEqual((verdict["expected"], verdict["received"], verdict["missing"]), (100, 99, 1))
        self.assertEqual((verdict["late"], verdict["on_time"]), (1, 98))
        self.assertEqual(verdict["on_time_fraction"], 0.98)
        self.assertEqual(verdict["floor_ms"], 30.0)
        self.assertEqual(verdict["latency_ms"]["max"], 150.0)
        self.assertEqual(bench.frame_verdict(parts, 200.0)["late"], 0)
        # The window: only parts that completed inside it.
        since = 1_000_000 + 170 * 20_000 + 29_000
        self.assertEqual(min(bench.read_frames(path, since_us=since)), 170)
        self.assertEqual(bench.frame_verdict({}, 100.0)["on_time_fraction"], None)

    def test_udp_viewers(self):
        report = {"expected_parts": 500, "received_parts": 500, "missing_parts": 0,
                  "fast_lane_parts": 498, "cache_lane_parts": 2, "moves": [],
                  "publish_to_arrival_ms": {"count": 500, "p50": 3.1, "p95": 4.0, "p99": 6.0,
                                            "max": 40.0}}
        verdict = bench.udp_verdict(report, 100.0)
        self.assertTrue(verdict["whole"])
        self.assertEqual(verdict["tail_ms"], 36.9)
        self.assertFalse(bench.udp_verdict({**report, "missing_parts": 1}, 100.0)["whole"])
        self.assertFalse(bench.udp_verdict(report, 30.0)["whole"])
        self.assertEqual(bench.udp_verdict(None, 100.0), {"error": "no report"})

    def test_the_report_of_every_region(self):
        published = [1_000_000 + part * 20_000 for part in range(10)]
        frames_csv(self.dir / "lon-readers.4k.h3.frames.csv",
                   [(part, published[part] + 30_000, published[part]) for part in range(10)])
        # Two readers on one host (ntcmp numbers their records); one late frame.
        frames_csv(self.dir / "new-readers.4k.h3.frames-000.csv",
                   [(part, published[part] + 70_000, published[part]) for part in range(10)])
        frames_csv(self.dir / "new-readers.4k.h3.frames-001.csv",
                   [(part, published[part] + (300_000 if part == 4 else 70_000), published[part])
                    for part in range(10)])
        (self.dir / "lon-readers.4k.udp0.json").write_text(json.dumps(
            {"expected_parts": 10, "received_parts": 10, "missing_parts": 0,
             "publish_to_arrival_ms": {"count": 10, "p50": 2.0, "max": 3.0}}))
        items = [
            {"host": "lon-readers", "site": "london", "kind": "hls",
             "frames": str(self.dir / "lon-readers.4k.h3.frames.csv")},
            {"host": "lon-readers", "site": "london", "kind": "udp",
             "report": str(self.dir / "lon-readers.4k.udp0.json")},
            {"host": "new-readers", "site": "newyork", "kind": "hls",
             "frames": str(self.dir / "new-readers.4k.h3.frames.csv")},
            {"host": "new-readers", "site": "newyork", "kind": "udp",
             "report": str(self.dir / "new-readers.4k.udp0.json")}]
        regions = bench.video_report(items, 100.0, None, None)
        self.assertTrue(regions["london"]["all_on_time"])
        self.assertEqual(regions["london"]["frames"]["on_time_fraction"], 1.0)
        newyork = regions["newyork"]
        self.assertEqual(len(newyork["readers"]), 2)
        self.assertEqual(newyork["frames"], {"expected": 20, "received": 20, "missing": 0,
                                             "late": 1, "on_time": 19, "on_time_fraction": 0.95})
        self.assertFalse(newyork["all_on_time"])
        self.assertEqual(newyork["udp_clients"][0]["error"], "no report")
        source = {"label": "crowd50-40m", "video_mbps": 41.6, "fps": 50.0}
        text = "\n".join(bench.video_summary_lines(source, regions, 100.0))
        self.assertIn("4K: crowd50-40m (41.6 Mbit/s, 50 fps measured)", text)
        self.assertIn("newyork: frames expected 20 received 20 missing 0 late 1 on time 95.00%; "
                      "all on time: NO", text)
        self.assertIn("london: frames expected 10 received 10 missing 0 late 0 on time 100.00%; "
                      "all on time: yes", text)
        nodes = {"lon-edge-1": {"cpu_percent_mean": 41.2, "cpu_percent_p95": 60.0,
                                "cpu_percent_max": 80.5, "throttled_ms": 0, "udp_rcvbuf_errors": 3},
                 "relay-primary": {"samples": 3, "running_samples": 0}}
        lines = bench.node_summary_lines(nodes)
        self.assertEqual(len(lines), 3)
        self.assertIn("lon-edge-1", lines[2])
        self.assertIn("rcvbuf drops 3", lines[2])

    def test_the_client_loss_goes_on_the_access_link(self):
        self.assertEqual(bench.client_loss_spec("loss 5%"), "limit 50000 delay 0.50ms loss 5%")
        self.assertEqual(bench.client_loss_spec("loss gemodel 1% 20%"),
                         "limit 50000 delay 0.50ms loss gemodel 1% 20%")
        plan = dry_run("--client-loss", "loss 5%")
        self.assertEqual(plan["reader_netem"], "limit 50000 delay 0.50ms loss 5%")
        for bad in (["--client-loss", "delay 3ms loss 1%"], ["--client-loss", " "],
                    ["--client-loss", "loss 1%", "--reader-loss", "2"]):
            with self.assertRaises(SystemExit, msg=bad):
                bench.main(["--name", "t", "--dry-run", *bad])

    def test_audio_udp_clients_on_every_reader_host(self):
        plan = dry_run("--payload", "opus", "--udp-clients", "2", "--udp-client-args",
                       "--fast-lane false")
        clients = plan["udp_clients"]
        self.assertEqual([(item["host"], item["site"]) for item in clients],
                         [("lon-readers", "london")] * 2 + [("new-readers", "newyork")] * 2)
        command = clients[1]["command"]
        self.assertEqual(command[command.index("--stream") + 1], "3001")
        self.assertEqual(command[command.index("--arrivals") + 1], clients[1]["arrivals"])
        self.assertTrue(clients[1]["arrivals"].endswith("lon-readers.uc1.arrivals.csv"))
        self.assertEqual(command[-2:], ["--fast-lane", "false"])
        self.assertEqual(dry_run("--udp-clients", "0")["udp_clients"], [])
        with self.assertRaises(SystemExit):
            bench.main(["--name", "t", "--dry-run", "--udp-clients", "-1"])

    def test_the_udp_lane_report(self):
        """Unit latency from the source clock of the journal (seq = epoch),
        publish -> arrival, the lanes, the wire per part and the hop of the
        UDP clients' parts at their edge."""
        start_us, epoch_us = 1_000_000_000, 1000.0
        arrivals = self.dir / "lon-readers.uc0.arrivals.csv"
        lines = ["part,arrival_unix_us,bytes,published_unix_us,available_unix_us"]
        for part in range(100):
            spoken = start_us + round((part + 1) * epoch_us)
            lines.append(f"{part},{spoken + (2_000 if part != 7 else 9_000)},304,{spoken + 1_000},0")
        arrivals.write_text("\n".join(lines) + "\n")
        report = self.dir / "lon-readers.uc0.uc.json"
        report.write_text(json.dumps({
            "caches": ["lon-edge-1"], "moves": [], "expected_parts": 100, "received_parts": 100,
            "missing_parts": 0, "gap_parts": 0, "fast_lane_asked": True, "fast_lane_parts": 90,
            "cache_lane_parts": 10, "wire_bytes": 35_400, "wire_per_payload": 1.1644,
            "wire_bits_per_second": 2_832_000.0, "final_compact_plan": "C0.32.1",
            "compact": {"nack_parts_sent": 1, "retransmits": 1, "recovered_by_window": 0}}))
        items = [{"host": "lon-readers", "site": "london", "kind": "udp",
                  "report": str(report), "arrivals": str(arrivals)},
                 {"host": "new-readers", "site": "newyork", "kind": "udp",
                  "report": str(self.dir / "none.uc.json"),
                  "arrivals": str(self.dir / "none.arrivals.csv")}]
        events = [{"node": "contrib-lon", "stage": "src_last", "stream": 4001, "seq": part,
                   "t_us": start_us + part * 1000 + 500, "epoch": part} for part in range(100)]
        events += [{"node": "lon-edge-1", "stage": "udp_sent", "stream": 4001, "seq": part,
                    "t_us": start_us + (part + 1) * 1000 + 1_500, "peer": "10.90.0.101:4000"}
                   for part in range(100)]
        nodes = {"contrib-lon": {"role": "contributor", "site": "london", "addrs": ["10.90.0.5"]},
                 "lon-edge-1": {"role": "edge", "site": "london", "addrs": ["10.90.0.21"]}}
        hosts, probes = bench.udp_lane_report(items, nodes, events, 4001, start_us, epoch_us,
                                              None, None)
        london = hosts["lon-readers"]
        self.assertEqual(london["unit_latency_ms"]["p50"], 2.0)
        self.assertEqual(london["unit_latency_ms"]["max"], 9.0)
        self.assertEqual(london["publish_to_arrival_ms"]["p50"], 1.0)
        self.assertEqual((london["fast_lane_parts"], london["cache_lane_parts"]), (90, 10))
        self.assertEqual(london["wire_bytes_per_part"], 354.0)
        self.assertEqual(london["missing"], 0)
        self.assertEqual(london["clients"][0]["compact_plan"], "C0.32.1")
        self.assertEqual(hosts["new-readers"]["clients"][0]["error"], "no report")
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["host_ip"], "10.90.0.21")
        # A part the journal does not hold takes seq = epoch.
        source = bench.source_clock(nodes, events[:10], 4001, start_us, epoch_us, None, None)
        self.assertEqual(source(50), start_us + 51_000)
        result = bench.hops.analyse(nodes, events, probes, 4001, start_us, epoch_us)
        listener = result["regions"]["london"]["listener_parts"]
        sent = next(item for item in listener["hops"]
                    if item["hop"] == "edge.udp_sent -> listener.complete")
        self.assertEqual(sent["p50"], 0.5)
        text = "\n".join(bench.udp_summary_lines(hosts))
        self.assertIn("lon-readers (london): unit latency 2.0/2.0/2.0/9.0", text)
        self.assertIn("fast lane 90 cache lane 10", text)
        hop_text = "\n".join(bench.udp_hop_lines({"london": {"listener_parts": listener}}))
        self.assertIn("edge.udp_sent -> listener.complete", hop_text)

    def test_time_to_first_audio_in_the_udp_report(self):
        """Wave 7 item 8: each client's time to first part and its steps,
        and the host's spread of it; a client before item 8 gives the
        total alone."""
        clients = []
        for index, startup in enumerate([
                {"assignment_ms": 3.25, "playlist_ms": 9.5, "first_request_ms": 3.3,
                 "token_ms": 3.9, "subscribed_ms": 4.6, "first_part_ms": 4.8,
                 "first_part_after_named": 0},
                None]):
            report = self.dir / f"lon-readers.uc{index}.uc.json"
            report.write_text(json.dumps({
                "caches": ["lon-edge-1"], "moves": [], "expected_parts": 0,
                "received_parts": 0, "missing_parts": 0,
                "time_to_first_part_ms": 4.8 if startup else 300.0,
                **({"startup": startup} if startup else {})}))
            clients.append({"host": "lon-readers", "site": "london", "kind": "udp",
                            "report": str(report),
                            "arrivals": str(self.dir / f"none{index}.csv")})
        hosts, _ = bench.udp_lane_report(clients, {}, [], 4001, None, 1000.0, None, None)
        london = hosts["lon-readers"]
        self.assertEqual(london["time_to_first_part_ms"]["count"], 2)
        self.assertEqual(london["time_to_first_part_ms"]["max"], 300.0)
        text = "\n".join(bench.udp_summary_lines(hosts))
        self.assertIn("first part 4.8 ms (assignment 3.2, playlist 9.5, request 3.3, token 3.9, "
                      "OK 4.6)", text)
        self.assertIn("first part 300.0 ms", text)

    def test_the_clip_rate_from_ffprobe(self):
        # 50 frames a second of 500,000 bytes: 200 Mbit/s.
        text = "\n".join(f"{index / 50:.6f},500000," for index in range(100)) + "\nbad\n"
        self.assertEqual(bench.clip_rate(text), {"video_mbps": 200.0, "fps": 50.0})
        self.assertEqual(bench.clip_rate(""), {})

    def test_a_4k_run_end_to_end_with_the_lab_faked(self):
        """run() with the lab, the viewers and the journals faked: the 4K
        source is started with the chosen clip, the viewers' records are read
        back into the per-region report, the nodes are sampled, and the
        summary names it all."""
        tools = self.dir / "tools"
        tools.mkdir()
        for name in ("ntcmp", "needletail-udp-client"):
            (tools / name).write_text("")
        started: dict = {"sources": [], "commands": []}
        source = {"label": "synthetic 200 Mbit/s 50 fps", "path": str(self.dir / "s.ts"),
                  "synthetic": True, "nominal_mbps": 200.0, "fps": 50}

        class Process:
            pid = 1

            def __init__(self, command, stdout=None, **_kwargs):
                started["commands"].append(command)
                if "--frames" in command:
                    # Inside the run's window: the readers "end" 0.3 s later.
                    now = time.time_ns() // 1000
                    rows = [(part, now + part * 2_000 + 35_000, now + part * 2_000)
                            for part in range(50)]
                    frames_csv(pathlib.Path(command[command.index("--frames") + 1]), rows)
                elif "--stream" in command and stdout is not None:
                    stdout.write(json.dumps({"expected_parts": 50, "received_parts": 50,
                                             "missing_parts": 0, "fast_lane_parts": 50,
                                             "publish_to_arrival_ms": {"count": 50, "p50": 3.0,
                                                                       "max": 9.0}}).encode())
                    stdout.flush()

            def wait(self, timeout=None):
                time.sleep(0.3)
                return 0

            def poll(self):
                return 0

        class Sampler:
            def __init__(self, path, *_a, **_k):
                pass

            def start(self):
                return self

            def stop(self):
                return {"lon-dist-a": {"cpu_percent_mean": 90.0, "cpu_percent_p95": 120.0,
                                       "cpu_percent_max": 150.0, "throttled_ms": 12,
                                       "udp_rcvbuf_errors": 0}}

        lab = bench.lab
        patches = [
            mock.patch.object(bench, "OUT_ROOT", self.dir / "out"),
            mock.patch.object(lab, "WORK", self.dir / "lab"),
            mock.patch.object(lab, "LAB_ID", None),
            mock.patch.dict(bench.os.environ, {"NEEDLETAIL_ALLOW_LIVE": "1"}),
            mock.patch.object(bench, "SKIP_SECONDS", 0),
            mock.patch.object(bench, "prepare_media", lambda item: None),
            mock.patch.object(bench, "prepare_video",
                              lambda item: {**item, "video_mbps": 208.0, "fps": 50.0}),
            mock.patch.object(bench, "loop_start", lambda now: int(now)),
            mock.patch.object(bench, "stop_sender", lambda process: None),
            mock.patch.object(bench.subprocess, "Popen", Process),
            mock.patch.object(bench.hops, "collect_edge_lab", lambda since, stream: ({}, [])),
            mock.patch.object(lab, "up", lambda overrides: {}),
            mock.patch.object(lab, "down", lambda quiet=False: None),
            mock.patch.object(lab, "wait_live", lambda *a, **k: None),
            mock.patch.object(lab, "enter", lambda name, source=False: []),
            mock.patch.object(lab, "load", lambda name, default=None: {}),
            mock.patch.object(lab, "save", lambda name, value: None),
            mock.patch.object(lab, "Sampler", Sampler),
            mock.patch.object(lab, "start_sources",
                              lambda seconds, video=True, audio=None, clip=None:
                              started["sources"].append(str(clip))),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = bench.main(["--name", "4k", "--video-mbps", "200", "--seconds", "1",
                               "--ntcmp", str(tools / "ntcmp"),
                               "--udp-client", str(tools / "needletail-udp-client")])
        self.assertEqual(code, 0)
        self.assertEqual(started["sources"], [source["path"].replace(
            str(self.dir / "s.ts"), str(bench.synthetic_clip(200, 50, 10)))])
        out = self.dir / "out" / "4k"
        result = json.loads((out / "hops.json").read_text())
        video = result["video"]
        self.assertEqual(video["source"]["video_mbps"], 208.0)
        for site in ("london", "newyork"):
            region = video["regions"][site]
            self.assertEqual(region["frames"]["expected"], 50, site)
            self.assertEqual(region["frames"]["on_time"], 50, site)
            self.assertTrue(region["all_on_time"], site)
            self.assertTrue(region["udp_clients"][0]["whole"], site)
        self.assertEqual(result["bench"]["nodes"]["lon-dist-a"]["cpu_percent_max"], 150.0)
        text = (out / "summary.txt").read_text()
        self.assertIn("4K: synthetic 200 Mbit/s 50 fps (208 Mbit/s, 50 fps measured)", text)
        self.assertIn("london: frames expected 50 received 50 missing 0 late 0 on time 100.00%", text)
        self.assertIn("lon-dist-a", text)
        self.assertIn("4K hops (stream 1)", text)
        # The audio UDP client of each reader host ran beside the 4K viewers.
        self.assertEqual(sorted(result["udp_clients"]["hosts"]), ["lon-readers", "new-readers"])
        self.assertIn("UDP client lane", text)
        self.assertTrue(printed.getvalue().endswith(text))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_the_synthetic_source_has_the_rate_it_was_asked_for(self):
        path = self.dir / "synthetic.ts"
        command = bench.synthetic_command(path, 40, 50, 2)
        subprocess.run(command, check=True, capture_output=True, timeout=300)
        pathlib.Path(str(path) + ".part").replace(path)
        measured = bench.probe_clip(path)
        self.assertEqual(round(measured["fps"]), 50)
        # Within 10% of the rate asked for (VBV of one second).
        self.assertLess(abs(measured["video_mbps"] - 40) / 40, 0.10, measured)
        streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                  "stream=codec_type,width,height", "-of", "csv=p=0", str(path)],
                                 capture_output=True, text=True).stdout
        self.assertIn("video,3840,2160", streams)
        self.assertIn("audio", streams)


class EdgeLoad(unittest.TestCase):
    """Wave 8: needletail-load crowds on the edges, the edges' counters and
    their class threads' wake."""

    def test_the_load_ramp_is_per_edge(self):
        self.assertEqual(bench.load_ramp("32@0,96@20", 4), "128@0,384@20")
        with self.assertRaises(SystemExit):
            bench.load_ramp("32", 4)

    def test_a_crowd_for_each_site_over_its_edges(self):
        args = parse("--load-listeners", "64", "--load-sites", "london,newyork",
                     "--load-args=--silence-nack false", "--payload", "opus")
        edges = {"london": ["lon-edge-1", "lon-edge-2"], "newyork": ["new-edge-1"]}
        with mock.patch.object(bench.lab, "edges", side_effect=lambda site: edges[site]):
            items = bench.load_commands(args, pathlib.Path("/x"))
        self.assertEqual([item["host"] for item in items], ["lon-readers-2", "new-readers-2"])
        london = items[0]["command"]
        self.assertEqual(london[london.index("--listeners") + 1], "128")
        self.assertEqual(london[london.index("--ramp") + 1], "128@0")
        self.assertEqual(london[london.index("--edges") + 1],
                         "10.90.0.21:19446,10.90.0.22:19446")
        self.assertEqual(london[-2:], ["--silence-nack", "false"])
        self.assertEqual(bench.load_commands(parse(), pathlib.Path("/x")), [])

    def test_a_crowd_through_the_assignment_and_the_gauges(self):
        """Phase AM 10: --load-assign gives needletail-load every distributor
        and edge of the site as entry points; AM 8: the gauge lines."""
        args = parse("--load-listeners", "8", "--load-assign")
        with mock.patch.object(bench.lab, "edges", return_value=["lon-edge-1"]), \
                mock.patch.object(bench.lab, "distributors", return_value=["lon-dist-a"]):
            command = bench.load_commands(args, pathlib.Path("/x"))[0]["command"]
        entries = command[command.index("--entry") + 1].split(",")
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(entry.startswith("https://") and entry.endswith(":19444")
                            for entry in entries), entries)
        self.assertEqual(command[command.index("--tls-ca") + 1], bench.lab.ca_file())
        # needletail-load refuses --edges with --entry.
        self.assertNotIn("--edges", command)
        rows = [{"t": 1.0, "lon-edge-1": {"subscribers": 64, "load_permille": 700,
                                          "egress_observed_bps": 70e6,
                                          "advertised_free_bps": 15e6}},
                {"t": 2.0, "lon-edge-1": {"subscribers": 64, "load_permille": 800,
                                          "egress_observed_bps": 80e6,
                                          "advertised_free_bps": 5e6}}]
        lines = bench.gauge_lines(rows)
        self.assertIn("lon-edge-1: 64, 800/750, 80.0/75.0, 5.0", lines[-1])

    def test_node_events_parse_in_time_order(self):
        events = bench.parse_node_events("kill:lon-dist-c:30,start:lon-dist-c:12,drain:lon-edge-1:20")
        self.assertEqual(events, [(12.0, "start", "lon-dist-c"), (20.0, "drain", "lon-edge-1"),
                                  (30.0, "kill", "lon-dist-c")])
        with self.assertRaises(SystemExit):
            bench.parse_node_events("explode:lon-dist-c:3")
        self.assertTrue(parse("--node-events", "start:lon-dist-c:12", "--sample-forwarders")
                        .sample_forwarders)

    def test_the_forwarders_and_the_first_parent_changes(self):
        """Phase AM 12: a slot joins; one edge's first parent of the stream
        moves to it."""
        def row(t, slot_served, a_served):
            return {"t": t,
                    "lon-dist-a": {"fast_plane": {"lease_children": 3},
                                   "cache_mesh": {"children": 3, "first_parent_pairs": 2,
                                                  "warm_parts_sent": 0,
                                                  "watermark_stall_max_us": 2500},
                                   "served": a_served},
                    **({"lon-dist-c": {"fast_plane": {"lease_children": len(slot_served)},
                                       "cache_mesh": {"children": len(slot_served),
                                                      "first_parent_pairs": len(slot_served)},
                                       "served": slot_served}} if slot_served is not None
                       else {})}
        rows = [row(0.0, None, [["10.0.0.21:29101", 3001, 0], ["10.0.0.22:29101", 3001, 0]]),
                row(1.0, [["10.0.0.22:29101", 3001, 0]], [["10.0.0.21:29101", 3001, 0],
                                                          ["10.0.0.22:29101", 3001, 1]])]
        lines, report = bench.forwarder_lines(rows, [{"action": "start", "node": "lon-dist-c",
                                                      "second": 1.0}], 3001)
        self.assertEqual(report["pairs"], 2)
        self.assertEqual(report["first_parent_changes"],
                         [{"t": 1.0, "child": "10.0.0.22", "from": "lon-dist-a",
                           "to": "lon-dist-c"}])
        self.assertIn("lon-dist-c: 1/1, 1/1, 1", "\n".join(lines))
        self.assertIn("start lon-dist-c at 1.0 s", lines[-1])

    def test_the_edges_counters_over_the_window(self):
        before = {"lon-edge-1": {"nack_parts": 10, "subscriptions": 1,
                                 "fast_lane": {"fanout_shed": 3, "class_repair_max_us": 5}}}
        after = {"lon-edge-1": {"nack_parts": 25, "subscriptions": 65,
                                "fast_lane": {"fanout_shed": 7, "clients": 65,
                                              "class_repair_max_us": 900}}}
        delta = bench.edge_stats_delta(before, after)["lon-edge-1"]
        self.assertEqual(delta["nack_parts"], 15)
        self.assertEqual(delta["subscriptions"], 64)
        self.assertEqual(delta["fast"]["fanout_shed"], 4)
        self.assertEqual(delta["fast"]["class_repair_max_us"], 900)
        self.assertEqual(delta["fast_clients"], 65)
        self.assertIn("nack parts 15", "\n".join(bench.edge_stats_lines({"lon-edge-1": delta})))

    def test_the_class_thread_wake_of_each_edge(self):
        events = []
        for seq in range(100):
            rx = 1_000_000 + seq * 1_000
            events += [{"_node": "lon-edge-1", "stage": "relay_first", "seq": seq, "t_us": rx},
                       {"_node": "lon-edge-1", "stage": "relay_woken", "seq": seq,
                        "t_us": rx + (2_000 if seq == 50 else 50)},
                       {"_node": "lon-edge-1", "stage": "udp_sent", "seq": seq,
                        "t_us": rx + (2_100 if seq == 50 else 150)},
                       {"_node": "lon-dist-a", "stage": "relay_first", "seq": seq, "t_us": rx}]
        report = bench.edge_wake_report(events, None, None)
        self.assertEqual(list(report), ["lon-edge-1"])
        self.assertEqual(report["lon-edge-1"]["wake_over_1ms"], 1)
        self.assertEqual(report["lon-edge-1"]["wake_ms"]["max"], 2.0)
        self.assertEqual(report["lon-edge-1"]["woken_to_sent_ms"]["p50"], 0.1)
        self.assertIn("1 over 1 ms", "\n".join(bench.edge_wake_lines(report)))


class UnreadPayloads(unittest.TestCase):
    """Renditions nobody reads, for wave 7 item 5."""

    def test_each_unread_payload_is_published_and_sent_once(self):
        plan = dry_run("--payload", "opus", "--unread-payloads", "pcm, flac,opus,pcm")
        self.assertIn("daw_hls_formats=opus,pcm,flac", plan["lab_up"])
        args = parse("--payload", "opus", "--unread-payloads", "pcm,flac")
        commands = bench.unread_sender_commands(args, 1_000, "/loop.s24le")
        self.assertEqual(len(commands), 2)
        self.assertEqual([command[command.index("--payload") + 1] for command in commands],
                         ["pcm", "flac"])
        groups = [command[command.index("--group-id") + 1] for command in commands]
        self.assertEqual(groups, ["0", "0"])
        sessions = [command[command.index("--session-id") + 1] for command in commands]
        self.assertEqual(len(set(sessions)), 2)
        with self.assertRaises(SystemExit):
            bench.unread_payloads(parse("--unread-payloads", "mp3"))

    def test_the_source_host_sends_every_lane_to_each_contributor(self):
        args = parse("--payload", "opus", "--unread-payloads", "pcm")
        self.assertEqual(bench.sender_place(args), ("contrib-lon", ["127.0.0.1:27100"]))
        args = parse("--payload", "opus", "--unread-payloads", "pcm", "--sender-host", "lon-src")
        host, targets = bench.sender_place(args)
        self.assertEqual(host, "lon-src")
        self.assertEqual(targets, ["10.77.1.5:27100", "10.77.1.7:27100"])
        for command in [bench.sender_command(args, 1_000, "/loop.s24le"),
                        *bench.unread_sender_commands(args, 1_000, "/loop.s24le")]:
            self.assertEqual([command[i + 1] for i, item in enumerate(command)
                              if item == "--target"], targets)


class NodeTraffic(unittest.TestCase):
    """Datagrams and bytes per node from /proc/PID/net/dev (wave 7)."""

    NETDEV = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets "
        "errs drop fifo colls carrier compressed\n"
        "    lo:       0       0    0    0    0     0          0         0        0       0"
        "    0    0    0     0       0          0\n"
        "  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"
        "  eth1: 3000 30 0 0 0 0 0 0 4000 40 0 0 0 0 0 0\n")

    def test_the_counters_of_each_interface_are_read(self):
        with mock.patch.object(bench.lab.pathlib.Path, "read_text", return_value=self.NETDEV):
            counters = bench.lab.read_netdev(1)
        self.assertEqual(counters, {
            "eth0_rx_bytes": 1000, "eth0_rx_packets": 10, "eth0_tx_bytes": 2000,
            "eth0_tx_packets": 20, "eth1_rx_bytes": 3000, "eth1_rx_packets": 30,
            "eth1_tx_bytes": 4000, "eth1_tx_packets": 40})

    def test_rates_and_datagrams_per_part(self):
        samples = [{"at": 100.0, "eth0_tx_packets": 0, "eth0_tx_bytes": 0,
                    "eth1_tx_packets": 0, "eth1_tx_bytes": 0},
                   {"at": 110.0, "eth0_tx_packets": 20_000, "eth0_tx_bytes": 25_000_000,
                    "eth1_tx_packets": 160_000, "eth1_tx_bytes": 100_000_000}]
        rates = bench.lab.netdev_rates(samples)
        self.assertEqual(rates["eth0_tx_pps"], 2000.0)
        self.assertEqual(rates["eth0_tx_mbps"], 20.0)
        self.assertEqual(rates["eth1_tx_pps"], 16000.0)
        lines = bench.node_traffic_lines({"contrib-lon": rates}, 1000.0)
        self.assertIn("16.00/part", lines[-1])
        self.assertIn("2.00/part", lines[-1])
        self.assertEqual(bench.node_traffic_lines({"x": {"cpu_percent_mean": 1}}, 1000.0), [])


class PerLab(unittest.TestCase):
    """Two labs of one host (NEEDLETAIL_EDGE_LAB_ID): each pass under its
    lab's id, the media shared and made under a lock, a pass holding its
    lab and refusing binaries outside a frozen set."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for patcher in (mock.patch.object(bench, "OUT_ROOT", self.dir / "out"),
                        mock.patch.object(bench, "MEDIA", self.dir / "out" / "media"),
                        mock.patch.object(bench.lab, "WORK", self.dir / "lab")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_pass_of_a_lab_with_an_id_goes_under_the_id(self):
        with mock.patch.object(bench.lab, "LAB_ID", "a"):
            plan = dry_run()
        self.assertEqual(plan["out"], str(self.dir / "out" / "a" / "t"))
        # The media are every lab's.
        self.assertEqual(plan["media"]["loop"], str(self.dir / "out" / "media" / "loop64.s24le"))
        with mock.patch.object(bench.lab, "LAB_ID", None):
            self.assertEqual(dry_run()["out"], str(self.dir / "out" / "t"))
        self.assertEqual(bench.bench_dir("n", "b"), self.dir / "out" / "b" / "n")

    def test_the_media_are_made_once_under_the_lock_and_renamed(self):
        source = self.dir / "audio.s24le"
        source.write_bytes(bytes(range(12)) * 2)
        index = bench.MEDIA / "nt-opus-48s.index.json"
        item = {"loop": str(bench.MEDIA / "loop64.s24le"), "index": str(index), "from": str(source),
                "index_command": [sys.executable, "-c",
                                  "import sys; open(sys.argv[1], 'w').write('{}')", str(index)]}
        bench.MEDIA.mkdir(parents=True)
        done = threading.Event()
        with open(bench.MEDIA / ".lock", "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            worker = threading.Thread(target=lambda: (bench.prepare_media(item), done.set()))
            with mock.patch.object(bench, "LOOP_BYTES", 12):
                worker.start()
                self.assertFalse(done.wait(0.5), "the media were made while another lab held the lock")
                fcntl.flock(handle, fcntl.LOCK_UN)
                worker.join(10)
        self.assertTrue(done.is_set())
        self.assertEqual(pathlib.Path(item["loop"]).read_bytes(), bytes(range(12)))
        self.assertEqual(index.read_text(), "{}")
        self.assertEqual(sorted(path.name for path in bench.MEDIA.iterdir()),
                         [".lock", "loop64.s24le", "nt-opus-48s.index.json"])
        # Made once: a second pass finds them and runs nothing.
        item["index_command"] = ["false"]
        with mock.patch.object(bench, "LOOP_BYTES", 12):
            bench.prepare_media(item)

    def test_a_pass_holds_its_lab(self):
        seen = {}

        def run(args, steps, record):
            seen["holder"] = bench.guard.holder(bench.lab.WORK)
            return 0

        with mock.patch.object(bench, "run", run), \
                mock.patch.dict(os.environ, {"NEEDLETAIL_ALLOW_LIVE": "1"}):
            self.assertEqual(bench.main(["--name", "held"]), 0)
        self.assertEqual(seen["holder"]["pid"], os.getpid())
        self.assertEqual(seen["holder"]["command"], "latency-bench.py --name held")
        self.assertIsNone(bench.guard.holder(bench.lab.WORK))

    def test_a_pass_refuses_a_binary_outside_a_frozen_set(self):
        live = self.dir / "needletail-udp-client"
        live.write_text("")
        with mock.patch.object(bench, "run", side_effect=AssertionError("the pass ran")), \
                mock.patch.object(bench.guard, "lab_binaries", side_effect=dict), \
                mock.patch.dict(os.environ):
            os.environ.pop("NEEDLETAIL_ALLOW_LIVE", None)
            with self.assertRaisesRegex(SystemExit, r"not in a frozen set .*: needletail-udp-client "):
                bench.main(["--name", "live", "--udp-client", str(live)])
            # A pass without UDP listeners does not run the client.
            with mock.patch.object(bench, "run", return_value=0):
                self.assertEqual(bench.main(["--name", "live", "--udp-clients", "0",
                                             "--udp-client", str(live)]), 0)

    def test_a_pass_on_a_running_lab_checks_what_the_lab_runs(self):
        """--no-up: the gate is the copies the lab's `up` took, not the
        shell's exports."""
        frozen_set = self.dir / "sets" / "w30"
        frozen_set.mkdir(parents=True)
        (frozen_set / "av-mesh").write_bytes(b"frozen build")
        (frozen_set / "build.json").write_text(json.dumps(
            {"schema": bench.guard.BIN_SET_SCHEMA, "label": "w30", "binaries": {"av-mesh": {
                "sha256": hashlib.sha256(b"frozen build").hexdigest()}}}))
        built = self.dir / "built"
        built.mkdir()
        (built / "av-mesh").write_bytes(b"live build")
        running = self.dir / "lab" / "bin" / "a" / "av-mesh"
        running.parent.mkdir(parents=True)
        running.write_bytes(b"live build")
        (self.dir / "lab" / "binaries.json").write_text(json.dumps({"a": {"av-mesh": str(running)}}))
        arguments = ["--name", "x", "--no-up", "--udp-clients", "0"]
        with mock.patch.object(bench.guard, "BIN_SETS", self.dir / "sets"), \
                mock.patch.dict(os.environ):
            os.environ.pop("NEEDLETAIL_ALLOW_LIVE", None)
            # The shell exports a frozen set, but the lab was brought up
            # from a live build: refused.
            with mock.patch.object(bench, "run", side_effect=AssertionError("the pass ran")), \
                    mock.patch.object(bench.guard, "lab_binaries",
                                      return_value={"av-mesh": frozen_set / "av-mesh"}):
                with self.assertRaisesRegex(SystemExit, r"av-mesh .*the running lab's"):
                    bench.main(arguments)
            # The lab runs a copy of a frozen binary: a shell without the
            # exports passes.
            running.write_bytes(b"frozen build")
            with mock.patch.object(bench, "run", return_value=0), \
                    mock.patch.object(bench.guard, "lab_binaries",
                                      return_value={"av-mesh": built / "av-mesh"}):
                self.assertEqual(bench.main(arguments), 0)


if __name__ == "__main__":
    unittest.main()
