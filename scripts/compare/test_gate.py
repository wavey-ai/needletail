"""Offline tests of gate.py's figures and verdict (no lab)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("gate", HERE / "gate.py")
gate = importlib.util.module_from_spec(SPEC)
sys.modules["gate"] = gate
SPEC.loader.exec_module(gate)

S = 1_000_000  # µs


def arrivals(parts: range, fps: float = 24.0, start_us: int = 100 * S, latency_ms: float = 20.0,
             skip=(), late=None, gop: int = 48, key_offset: int = 0):
    """A viewer's arrivals: one part a frame, a key frame (3x the bytes)
    every gop parts at key_offset, `skip` never arrive, `late` {part: ms}."""
    out = {}
    for part in parts:
        if part in skip:
            continue
        published = start_us + int((part - parts[0]) * S / fps)
        extra = (late or {}).get(part, 0.0)
        size = 300_000 if part % gop == key_offset else 100_000
        out[part] = (published + int((latency_ms + extra) * 1000), size, published)
    return out


class Referee(unittest.TestCase):
    """The referee's rule: 99.5% on time, no stall over 500 ms."""

    def window(self, parts):
        first = 100 * S
        return first, first + int(len(parts) * S / 24.0)

    def test_every_frame_on_time_is_carried(self):
        parts = range(1000, 1960)  # 40 s at 24 fps
        since, until = self.window(parts)
        verdict = gate.referee_verdict(arrivals(parts), since, until, 24.0)
        self.assertTrue(verdict["carried"])
        self.assertEqual(verdict["on_time_fraction"], 1.0)
        self.assertEqual(verdict["longest_stall_ms"], 0.0)
        # The first 5 s and the last 3 s are left out.
        self.assertEqual(verdict["expected"], 960 - 5 * 24 - 3 * 24 + 1)

    def test_a_missing_part_spoils_its_group_of_pictures(self):
        parts = range(1000, 1960)
        since, until = self.window(parts)
        # Key frames at part % 48 == 0; part 1250 is lost, so 1250..1295
        # cannot be decoded (1296 is the next key frame): 46 frames, 1.9 s.
        verdict = gate.referee_verdict(arrivals(parts, skip={1250}), since, until, 24.0)
        self.assertEqual(verdict["key_offset"], 0)
        self.assertEqual(verdict["missing"], 1)
        self.assertEqual(verdict["undecodable"], 45)
        self.assertFalse(verdict["carried"])
        self.assertAlmostEqual(verdict["longest_stall_ms"], 46 * 1000 / 24, places=0)

    def test_a_few_late_frames_pass_and_a_long_run_of_them_does_not(self):
        parts = range(1000, 1960)
        since, until = self.window(parts)
        few = {1300: 400.0, 1500: 400.0}
        verdict = gate.referee_verdict(arrivals(parts, late=few), since, until, 24.0)
        self.assertEqual(verdict["late"], 2)
        self.assertTrue(verdict["carried"], verdict)
        run = {part: 300.0 for part in range(1300, 1314)}  # 14 frames = 583 ms
        verdict = gate.referee_verdict(arrivals(parts, late=run), since, until, 24.0)
        self.assertGreater(verdict["longest_stall_ms"], 500)
        self.assertFalse(verdict["carried"])

    def test_late_is_judged_against_the_median(self):
        parts = range(1000, 1960)
        since, until = self.window(parts)
        # 240 ms over a 20 ms median: on time (the rule is 250 ms).
        verdict = gate.referee_verdict(arrivals(parts, late={1400: 240.0}), since, until, 24.0)
        self.assertEqual(verdict["late"], 0)

    def test_no_arrivals_is_not_carried(self):
        self.assertFalse(gate.referee_verdict({}, 0, 10 * S, 24.0)["carried"])


class Nodes(unittest.TestCase):

    def test_cpu_share_and_cpu_per_gigabit_and_drops(self):
        samples = []
        for second in range(0, 12):
            samples.append({
                "lon-edge-1": {"at": 1000.0 + second, "running": True,
                               "usage_usec": second * 700_000,  # 70% of one CPU
                               "udp_socket_drops": {"23001": 0 if second < 6 else 5},
                               "throttled_usec": 0},
                "contrib-lon": {"at": 1000.0 + second, "running": True,
                                "usage_usec": second * 2_000_000,  # 200%
                                "udp_socket_drops": {}, "throttled_usec": 0}})
        roles = {"lon-edge-1": "edge", "contrib-lon": "contributor"}
        figures = gate.node_figures(samples, 1001.0, 1010.0, 250.0, roles)
        edge = figures["lon-edge-1"]
        self.assertAlmostEqual(edge["cpu_percent"], 70.0, places=0)
        self.assertAlmostEqual(edge["cpu_share"], 0.35, places=2)
        # 0.7 CPU s per s over 250 Mbit/s: 2.8 CPU s per Gbit.
        self.assertAlmostEqual(edge["cpu_s_per_gbit"], 2.8, places=1)
        self.assertEqual(edge["socket_drops"], {"23001": 5})
        self.assertAlmostEqual(figures["contrib-lon"]["cpu_share"], 0.5, places=2)


def figures(name, audio=None, video=None, nodes=None, counters=None, refused=None):
    return {"gate_pass": name, "name": name, "audio": audio or [], "video": video,
            "nodes": nodes or {}, "counters": counters or {}, "refused": refused or {}}


def client(site, p50, p99, missing=0, edge="lon-edge-1", has_4k=False):
    return {"site": site, "edge": edge, "edge_has_4k": has_4k, "missing": missing,
            "p50_ms": p50, "p99_ms": p99}


THRESHOLDS = json.loads((HERE / "gate-thresholds.json").read_text())
BASELINE = {"hosts": {"box": {"opus1-clean": {"london": {"p50_ms": 2.5, "p99_ms": 5.0},
                                              "newyork": {"p50_ms": 40.0, "p99_ms": 43.0}}}}}


def video(carried=True, share=1.0):
    return {"mbps": 247.0, "viewers": [{"site": "london", "missing_parts": 0,
                                        "referee": {"carried": carried, "on_time_fraction": share,
                                                    "longest_stall_ms": 0.0}}]}


def node(share, drops=0):
    return {"cpu_share": share, "socket_drops_total": drops}


class Verdict(unittest.TestCase):

    def good_run(self):
        alone = figures("opus1-clean", audio=[client("london", 2.5, 5.2),
                                              client("newyork", 40.0, 43.4)])
        k247 = figures("4k-247", audio=[client("london", 2.6, 5.5, edge="lon-edge-2"),
                                        client("london", 2.9, 7.0, has_4k=True)],
                       video=video(), nodes={"lon-edge-1": node(0.5), "contrib-lon": node(0.3)},
                       counters={"lon-edge-1": {"tee_dropped": 0}})
        return [alone, k247]

    def test_a_good_run_passes(self):
        judged = gate.verdict(self.good_run(), THRESHOLDS, BASELINE, host="box")
        self.assertEqual(judged["verdict"], "PASS", judged["failed"])

    def test_one_missing_audio_part_fails(self):
        run = self.good_run()
        run[0]["audio"][0]["missing"] = 1
        judged = gate.verdict(run, THRESHOLDS, BASELINE, host="box")
        self.assertEqual(judged["verdict"], "FAIL")
        self.assertIn("opus1-clean: audio missing", [c["check"] for c in judged["failed"]])

    def test_audio_p99_over_the_baseline_and_its_margin_fails(self):
        run = self.good_run()
        run[0]["audio"][0]["p99_ms"] = 6.2  # baseline 5.0 + 1.0
        judged = gate.verdict(run, THRESHOLDS, BASELINE, host="box")
        self.assertEqual([c["check"] for c in judged["failed"]], ["opus1-clean: london audio p99"])

    def test_another_host_has_no_baseline_and_is_not_judged_on_p99(self):
        run = self.good_run()
        run[0]["audio"][0]["p99_ms"] = 60.0
        judged = gate.verdict(run, THRESHOLDS, BASELINE, host="elsewhere")
        self.assertEqual(judged["verdict"], "PASS")

    def test_4k_not_carried_at_247_fails_but_another_rung_is_reported(self):
        run = self.good_run()
        run[1]["video"] = video(carried=False, share=0.97)
        judged = gate.verdict(run, THRESHOLDS, BASELINE, host="box")
        self.assertIn("4k-247: london 4K carried", [c["check"] for c in judged["failed"]])
        run = self.good_run()
        run.append(figures("4k-300", audio=[client("london", 2.6, 5.5)],
                           video=video(carried=False, share=0.5), nodes={"x": node(0.95, 9)}))
        judged = gate.verdict(run, THRESHOLDS, BASELINE, host="box")
        self.assertEqual(judged["verdict"], "PASS", judged["failed"])

    def test_drops_tee_drops_refusals_and_cpu_fail_at_247(self):
        for change, name in (
                (lambda r: r[1]["nodes"].update({"lon-edge-1": node(0.5, drops=3)}), "socket drops"),
                (lambda r: r[1]["counters"].update({"lon-edge-1": {"tee_dropped": 7}}), "tee drops"),
                (lambda r: r[1].update({"refused": {"lon-dist-a": 4}}), "parts refused"),
                (lambda r: r[1]["nodes"].update({"lon-edge-1": node(0.81)}), "node CPU share")):
            run = self.good_run()
            change(run)
            judged = gate.verdict(run, THRESHOLDS, BASELINE, host="box")
            self.assertEqual([c["check"] for c in judged["failed"]], [f"4k-247: {name}"])

    def test_audio_beside_4k_on_an_edge_without_it_is_held_to_the_alone_figures(self):
        run = self.good_run()
        # p50 0.3 ms over alone on the edge without 4K: over the 0.2 ms bound.
        run[1]["audio"][0]["p50_ms"] = 2.8
        judged = gate.verdict(run, THRESHOLDS, BASELINE, host="box")
        self.assertEqual([c["check"] for c in judged["failed"]],
                         ["4k-247: london audio p50 beside 4K (edge without 4K) - alone"])
        # The client on the 4K edge is reported, not held to it.
        run = self.good_run()
        run[1]["audio"][1]["p50_ms"] = 9.0
        self.assertEqual(gate.verdict(run, THRESHOLDS, BASELINE, host="box")["verdict"], "PASS")

    def test_no_checks_is_not_a_pass(self):
        self.assertEqual(gate.verdict([], THRESHOLDS, BASELINE, host="box")["verdict"], "FAIL")


class Plan(unittest.TestCase):

    def test_quick_and_full(self):
        quick = [item["name"] for item in gate.plan(True)]
        self.assertEqual(quick, ["opus1-clean", "pcm1-loss5", "opus25-burst", "4k-247"])
        full = [item["name"] for item in gate.plan(False)]
        self.assertEqual(len(full), 9 + 5)
        self.assertIn("pcm1-burst", full)
        self.assertEqual(full[-5:], ["4k-25", "4k-80", "4k-164", "4k-247", "4k-300"])

    def test_a_rung_takes_the_referee_clip_when_it_is_there(self):
        with tempfile.TemporaryDirectory() as media:
            media = pathlib.Path(media)
            (media / "2160p240").mkdir()
            (media / "2160p240" / "video.h264").write_bytes(b"")
            self.assertEqual(gate.rung_source("247", media),
                             ["--video-clip", str(media / "2160p240")])
            self.assertEqual(gate.rung_source("164", media), ["--video-mbps", "164"])
            self.assertEqual(gate.rung_source("300", media), ["--video-mbps", "300"])

    def test_the_bench_command_of_a_pass(self):
        item = gate.plan(True)[1]  # pcm1-loss5
        command = gate.bench_command(item, "r1", pathlib.Path("/nowhere"), 40, [])
        self.assertIn("--client-loss", command)
        self.assertEqual(command[command.index("--client-loss") + 1], "loss 5%")
        self.assertEqual(command[command.index("--payload") + 1], "pcm")
        self.assertIn("mesh_symbol_size=1316", command)
        k = gate.bench_command(gate.plan(True)[3], "r1", pathlib.Path("/nowhere"), 40, [])
        self.assertEqual(k[k.index("--video-mbps") + 1], "247")
        self.assertEqual(k[k.index("--video-readers") + 1], "0")


class Baseline(unittest.TestCase):

    def test_the_baseline_is_the_median_client_of_each_site(self):
        run = [figures("opus1-clean", audio=[client("london", 2.5, 5.0), client("london", 2.7, 6.0),
                                             client("london", 2.6, 5.5)]),
               figures("4k-247", audio=[client("london", 9, 9)], video=video())]
        self.assertEqual(gate.baseline_from(run),
                         {"opus1-clean": {"london": {"p50_ms": 2.6, "p99_ms": 5.5}}})


if __name__ == "__main__":
    unittest.main()


class RefereeSource(unittest.TestCase):
    """The lab sends a referee clip as the referee does (ntcmp flv into the
    feed's ffmpeg over a pipe)."""

    def test_a_clip_directory_of_prepare_media_is_a_referee_clip(self):
        sys.path.insert(0, str(HERE.parent))
        import edge_lab
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            clip = root / "clips6-full" / "2160p240"
            clip.mkdir(parents=True)
            self.assertIsNone(edge_lab.referee_clip(clip), "no video.h264")
            (clip / "video.h264").write_bytes(b"")
            (root / "clips6").mkdir()
            (root / "clips6" / "media.json").write_text(json.dumps(
                {"fps": 24, "rungs": {"2160p240": {"kbps": 247388.4}}}))
            self.assertEqual(edge_lab.referee_clip(clip),
                             {"rung": "2160p240", "fps": 24, "kbps": 247388.4})
            self.assertIsNone(edge_lab.referee_clip(root / "x.ts"))

    def test_the_feed_gives_its_ffmpeg_the_feeds_own_input(self):
        """video-feed.sh runs ffmpeg in the background, whose standard input
        would be /dev/null: an ffmpeg that reads -i pipe:0 needs the feed's."""
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            fake = root / "ffmpeg"
            fake.write_text("#!/bin/sh\nfor last; do :; done\ncat > \"$last\"\n")
            fake.chmod(0o755)
            out = root / "out-HOST"
            result = subprocess.run(
                [str(HERE.parent / "video-feed.sh"), "HOST", "--", str(root / "out-{host}")],
                input=b"frames", env={"FFMPEG": str(fake), "PATH": "/usr/bin:/bin"},
                capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(out.read_bytes(), b"frames")


class Archive(unittest.TestCase):

    def test_the_gate_never_takes_a_run_from_latency_bench(self):
        """latency-bench.py makes each pass's run and a run is refused to a
        tool that did not make it: the gate opens it, or makes it as the
        bench would, so every later pass can append."""
        import os
        sys.path.insert(0, str(HERE.parent))
        import run_archive
        with tempfile.TemporaryDirectory() as root:
            old = os.environ.get("NEEDLETAIL_DATA_ROOT")
            os.environ["NEEDLETAIL_DATA_ROOT"] = root
            try:
                gate.open_archive("g1")
                run_archive.RunArchive.create("g1", "local-lab", tool="latency-bench.py")
                run_archive.RunArchive.create("g2", "local-lab", tool="latency-bench.py")
                gate.open_archive("g2").write_json("x.json", {"a": 1})
                self.assertTrue((pathlib.Path(root) / "g2" / "x.json").is_file())
            finally:
                if old is None:
                    os.environ.pop("NEEDLETAIL_DATA_ROOT", None)
                else:
                    os.environ["NEEDLETAIL_DATA_ROOT"] = old
