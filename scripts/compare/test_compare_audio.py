#!/usr/bin/env python3
"""Tests of the Needletail lanes of compare-audio.py, its analysis and its
report: the AEP1 sender's Opus at 2.5 ms, PCM in one-epoch parts, and the UDP
client. Nothing is started.
python3 -m unittest scripts/compare/test_compare_audio.py"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audio = load("compare_audio", "compare-audio.py")
analyse = load("compare_audio_analyse", "compare-audio-analyse.py")
report = load("compare_audio_report", "compare-audio-report.py")


class FakeSession:
    name = "t"
    cloud = None

    def ip(self, name: str) -> str:
        return {"lon-dist-a": "10.0.0.11"}[name]


def option(command: str, flag: str) -> str:
    words = command.split()
    return words[words.index(flag) + 1]


class Lanes(unittest.TestCase):
    def test_opus_2_5_is_the_senders_opus_when_asked_for(self):
        lanes = audio.WEBRTC + list(audio.NEEDLETAIL) + [audio.UDP_LANE]
        a = audio.Audio(FakeSession(), "opus-2.5", lanes)
        self.assertEqual(a.nt_base, ["nt-popus"])
        self.assertEqual(a.nt, ["nt-popus", "nt-popus-w", "nt-popus-h3", "nt-udp"])
        self.assertEqual(a.nt_frame_ms(), {"nt-popus": 2.5})
        # Without nt-popus the DAW source's Opus is the lane, as before.
        a = audio.Audio(FakeSession(), "opus-2.5", ["nt-opus", "cf"])
        self.assertEqual(a.nt, ["nt-opus", "nt-opus-w", "nt-opus-h3"])
        self.assertEqual(audio.Audio(FakeSession(), "opus-5", lanes).nt_base, ["nt-opus"])
        self.assertEqual(audio.Audio(FakeSession(), "pcm", lanes).nt_frame_ms(), {"nt-pcm": 1.0})
        self.assertEqual(audio.Audio(FakeSession(), "pcm", lanes, 12).nt_frame_ms(), {"nt-pcm": 0.25})
        # No Needletail rendition: no UDP lane either.
        self.assertEqual(audio.Audio(FakeSession(), "pcm", ["cf", "nt-udp"]).nt, [])

    def test_the_aep1_sender_sends_one_epoch_after_the_loop_start(self):
        opus = audio.aep1_command("10.77.1.5:27100", "nt-popus", 120, "/a.s24le")
        self.assertEqual(option(opus, "--payload"), "opus")
        self.assertEqual(option(opus, "--epoch-samples"), "120")
        self.assertEqual(option(opus, "--session-id"), "${START}002500000")
        pcm = audio.aep1_command("10.77.1.5:27100", "nt-pcm", 48, "/a.s24le")
        self.assertEqual(option(pcm, "--payload"), "pcm")
        self.assertEqual(option(pcm, "--session-id"), "${START}001000000")
        self.assertEqual(option(audio.aep1_command("t", "nt-pcm", 12, "/a"), "--session-id"),
                         "${START}000250000")

    def test_reader_commands(self):
        lanes = list(audio.NEEDLETAIL) + [audio.UDP_LANE]
        a = audio.Audio(FakeSession(), "opus-2.5", lanes)
        hls = a.probe("nt-popus-h3", "lon", 60, 1, "tag")
        self.assertIn("/live/3001/master.m3u8", hls)
        self.assertIn("/index/nt-popus-120s-la.index.json", hls)
        self.assertIn("--window 40 --http3", hls)
        udp = a.probe("nt-udp", "lon", 60, 1, "tag")
        self.assertTrue(udp.startswith("needletail-udp-client --entry https://10.0.0.11:19444 --stream 3001 "))
        self.assertIn(">nt-udp.udp.json", udp)
        pcm = audio.Audio(FakeSession(), "pcm", lanes, 24).probe("nt-pcm", "lon", 60, 1, "tag")
        self.assertIn("/index/nt-pcm-24s.index.json", pcm)
        self.assertIn("/live/4001/", pcm)

    def test_the_http2_reader_keeps_5_ms_in_flight(self):
        # One request for each part reads one part per round trip: it falls
        # behind parts shorter than the round trip. The plain lane keeps 5 ms
        # of parts in flight, which was one request at 5 ms parts.
        lanes = list(audio.NEEDLETAIL)
        for config, samples, window in (("pcm", 48, 5), ("pcm", 24, 10), ("pcm", 12, 20),
                                        ("opus-2.5", 48, 2)):
            a = audio.Audio(FakeSession(), config, lanes, samples)
            base = a.nt_base[0]
            self.assertIn(f"--window {window} ", a.probe(base, "lon", 60, 1, "tag"), config)
            self.assertIn("--window 40 --http3", a.probe(base + "-h3", "lon", 60, 1, "tag"))
        self.assertEqual(audio.reader_flags("", 5.0), "--window 1")
        self.assertEqual(audio.reader_flags("-w", 1.0), "--window 40")


class Analysis(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def test_frame_of_a_lane(self):
        meta = {"nt_frame_ms": {"nt-popus": 2.5, "nt-pcm": 1.0}}
        self.assertEqual(analyse.nt_frame_ms(meta, "nt-popus-h3"), 2.5)
        self.assertEqual(analyse.nt_frame_ms(meta, "nt-pcm"), 1.0)
        self.assertEqual(analyse.nt_frame_ms({}, "nt-pcm-w"), 5.0)   # a run from before

    def test_the_daw_lanes_follow_the_source_they_had(self):
        lanes = list(audio.NEEDLETAIL)
        opus = audio.Audio(FakeSession(), "opus-5", lanes)
        flac = audio.Audio(FakeSession(), "opus-20", lanes)
        self.assertEqual(opus.nt_frame_ms(), {"nt-opus": 2.5})
        self.assertEqual(flac.nt_frame_ms(), {"nt-flac": 1.0})
        # The DAW source is told its epochs and buffer; the index matches them.
        command = audio.daw_command("10.0.0.2:27100")
        for flag, value in (("--buffer-frames", "64"), ("--opus-frame-samples", "120"),
                            ("--lossless-epoch-samples", "48")):
            self.assertIn(f"{flag} {value} ", command)
        self.assertIn("/index/nt-opus-120s-b64.index.json", opus.probe("nt-opus", "lon", 60, 1, "t"))
        self.assertIn("/index/nt-flac-48s-b64.index.json", flac.probe("nt-flac-h3", "lon", 60, 1, "t"))
        name, spec = audio.run.lab.daw_index("nt-opus")
        self.assertEqual(spec, [f"numbered:{name}", "2.5", str(64 + 120)])
        self.assertEqual(audio.run.lab.daw_index("nt-flac")[1], ["numbered:nt-flac-48s-b64", "1.0", "64"])
        # The first runs' indexes are what this formula gives their source.
        old = {"buffer_frames": 512, "opus_frame_samples": 240, "lossless_epoch_samples": 240}
        self.assertEqual(audio.run.lab.daw_index("nt-opus", old)[1][1:], ["5.0", "632"])
        self.assertEqual(audio.run.lab.daw_index("nt-flac", old)[1][1:], ["5.0", "512"])

    def test_the_daw_opus_lane_takes_an_opus_custom_frame(self):
        lanes = list(audio.NEEDLETAIL)
        try:
            audio.set_nt_opus_frame(48)
            opus = audio.Audio(FakeSession(), "opus-5", lanes)
            self.assertEqual(opus.nt_frame_ms(), {"nt-opus": 1.0})
            self.assertIn("--opus-frame-samples 48 ", audio.daw_command("10.0.0.2:27100"))
            self.assertIn("/index/nt-opus-48s-b64.index.json", opus.probe("nt-opus", "lon", 60, 1, "t"))
            # An Opus Custom mode's look-ahead is its frame (its overlap).
            self.assertEqual(audio.run.lab.daw_index("nt-opus")[1],
                             ["numbered:nt-opus-48s-b64", "1.0", str(64 + 48)])
            self.assertEqual(audio.run.lab.opus_lookahead_samples(72), 72)
            self.assertEqual(audio.run.lab.opus_lookahead_samples(240), 120)
        finally:
            audio.set_nt_opus_frame(120)
        self.assertEqual(audio.NEEDLETAIL["nt-opus"][1], "nt-opus-120s-b64")
        # Every frame the flag takes has an index for the media step.
        names = {name for name, _ in audio.run.lab.NT_INDEXES}
        for frame in audio.run.lab.NT_OPUS_FRAME_SAMPLES:
            self.assertIn(f"nt-opus-{frame}s-b64", names)
        # A lane whose step meta has no frame takes --nt-frame-ms.
        self.assertEqual(analyse.nt_frame_ms({}, "nt-pcm-w", 1.0), 1.0)

    def test_daw_meta_drives_the_analysis(self):
        meta = {"nt_frame_ms": {"nt-opus": 2.5}, "nt_daw": dict(audio.NT_DAW)}
        self.assertEqual(analyse.daw_source(meta, "nt-opus-h3"), (120, 64))
        self.assertEqual(analyse.daw_source(meta, "nt-flac"), (48, 64))
        self.assertEqual(analyse.nt_frame_ms(meta, "nt-opus-w"), 2.5)
        # Without nt_daw: the first runs' source, 240-sample epochs, 512-sample
        # buffers, a 5 ms frame even where nt_frame_ms said 2.5.
        old = {"nt_frame_ms": {"nt-opus": 2.5}}
        self.assertEqual(analyse.daw_source(old, "nt-opus"), (240, 512))
        self.assertEqual(analyse.nt_frame_ms(old, "nt-opus"), 5.0)
        # The wait for the buffer: epoch 0 ends one buffer into the loop's
        # silence; with 48-sample epochs in 64-sample buffers the waits repeat
        # every 4 epochs (3 buffers) and are under one buffer.
        waits = [analyse.daw_wait_ms(index, 48, 64) for index in range(12)]
        self.assertEqual(waits[:4], waits[4:8])
        self.assertTrue(all(0 <= wait < 64 / 48 for wait in waits))
        self.assertEqual(analyse.daw_wait_ms(3, 48, 64), 0.0)    # 192 - 64 = 128: a buffer edge
        # Epoch 0's content ends 16 samples before the end of the loop's last
        # buffer (it is one buffer behind): it waits 16 samples.
        self.assertAlmostEqual(analyse.daw_wait_ms(0, 48, 64), 16 / 48)
        # The default is the first runs' formula.
        self.assertEqual(analyse.daw_wait_ms(5), analyse.daw_wait_ms(5, 240, 512))

    def test_udp_lane_with_the_source_to_publish_of_the_hls_probe(self):
        lines = ["kind,index,latency_us,complete_unix_us,first_unix_us,bytes,key,packets,"
                 "published_unix_us,cache_available_unix_us,edge_committed_unix_us,reply_unix_us,part"]
        for i in range(100):
            spoken = 1_000_000_000 + i * 2500
            lines.append(f"a,{i},4000,{spoken + 4000},{i},120,0,1,{spoken + 700},0,0,0,{i}")
        lines.append("a,-1,0,1,1,120,0,1,5,0,0,0,1")          # an unknown unit is not counted
        (self.dir / "nt-popus.probe.csv").write_text("\n".join(lines) + "\n")
        record = {"received_parts": 24000, "missing_parts": 0, "recovered_parts": 12,
                  "publish_to_arrival_ms": {"p50": 1.8, "p99": 3.0}}
        meta = {"lanes": ["nt-popus", "nt-popus-w", "nt-popus-h3", "nt-udp"]}
        out = analyse.analyse_udp(record, self.dir, meta)
        self.assertEqual(out["received_parts"], 24000)
        self.assertEqual(out["source_to_publish_ms"]["p50"], 0.7)
        self.assertEqual(out["source_to_publish_ms"]["n"], 100)
        self.assertEqual(out["estimated_unit_latency_p50_ms"], 2.5)

    def test_a_step_keeps_the_udp_report(self):
        step = self.dir / "audio.opus-2.5.baseline.clean"
        host = step / "lon-view"
        host.mkdir(parents=True)
        (step / "meta.json").write_text(json.dumps({"step": step.name, "config": "opus-2.5", "lanes": [
            "nt-popus", "nt-udp"], "nt_frame_ms": {"nt-popus": 2.5}}))
        (host / "nt-udp.udp.json").write_text(json.dumps({"received_parts": 5, "missing_parts": 0,
                                                          "publish_to_arrival_ms": {"p50": 1.0}}))
        result = analyse.analyse_step_now(step)
        self.assertEqual(result["hosts"]["lon-view"]["lanes"]["nt-udp"]["udp"]["received_parts"], 5)

    def test_joins_are_summarised_per_lane(self):
        step = self.dir / "audio.opus-2.5.joins.3"
        host = step / "lon-view"
        host.mkdir(parents=True)
        (step / "meta.json").write_text(json.dumps({"step": step.name, "config": "opus-2.5", "phase": "joins",
                                                    "lanes": ["lkregion", "nt-uc"]}))
        for j, ms in enumerate((40.0, 60.0, 50.0), 1):
            (host / f"lkregion.join-{j:02d}.json").write_text(json.dumps({"first_audio_ms": ms + 100,
                                                                          "connect_ms": ms}))
            (host / f"nt-uc.join-{j:02d}.json").write_text(json.dumps({"first_audio_ms": ms}))
            (host / f"nt-uc.join-{j:02d}.uc.json").write_text(json.dumps({"fast_lane_parts": 1}))
        (host / "nt-uc.join-04.json").write_text(json.dumps({"first_audio_ms": -1, "error": "no entry"}))
        lanes = analyse.analyse_step_now(step)["hosts"]["lon-view"]["lanes"]
        self.assertEqual(lanes["lkregion"]["joins"]["first_audio_ms"]["p50"], 150.0)
        self.assertEqual(lanes["lkregion"]["joins"]["connect_ms"]["max"], 60.0)
        self.assertEqual((lanes["nt-uc"]["joins"]["joins"], lanes["nt-uc"]["joins"]["with_audio"]), (4, 3))
        self.assertEqual(lanes["nt-uc"]["joins"]["errors"], ["no entry"])
        self.assertNotIn("probe", lanes["nt-uc"])
        self.assertIn("joins", analyse.slim({"phase": "joins", "hosts": {"lon-view": {"lanes": lanes}}})
                      ["hosts"]["lon-view"]["lanes"]["nt-uc"])


class Scoreboard(unittest.TestCase):
    def test_view_host_first_and_each_metric_judged(self):
        board = load("scoreboard", "scoreboard.py")
        probe = lambda p50, p99, miss=0.0: {"probe": {"mouth_to_ear_ms": {"p50": p50, "p99": p99},
                                                      "missing_percent": miss}}
        steps = {
            "audio.opus-2.5.baseline.clean": {"nt_popus_samples": 48, "hosts": {
                "lon-view": {"lanes": {"nt-uc": probe(2.1, 3.0), "lkregion": probe(4.5, 6.2)}},
                "lon-nano": {"lanes": {"nt-uc": probe(9.0, 90.0), "lkregion": probe(1.0, 1.0)}}}},
            "audio.opus-2.5.impair.loss5": {"hosts": {
                "lon-view": {"lanes": {"nt-uc": probe(2.0, 9.0, -0.01), "lkregion": probe(4.0, 6.0, 4.9)}}}},
            "audio.opus-2.5.joins.20": {"hosts": {"lon-view": {"lanes": {
                "nt-uc": {"joins": {"first_audio_ms": {"p50": 60.0}}},
                "lkregion": {"joins": {"first_audio_ms": {"p50": 60.4}}}}}}},
        }
        rows = board.board({"sessions": {"s": {"steps": steps}}}, ["s"])
        got = {(r["metric"], r["region"]): (r["needletail"], r["result"]) for r in rows}
        self.assertEqual(got[("m2e_p50", "London")], (2.1, "win"))          # the view host, not the Nanode
        self.assertEqual(got[("m2e_p99_loss5", "London")], (9.0, "loss"))
        self.assertEqual(got[("missing_loss5", "London")], (-0.01, "win"))   # negative counts as 0
        self.assertNotIn(("missing_loss1", "London"), got)                   # no step: no row
        self.assertEqual(got[("first_audio", "London")], (60.0, "tie"))       # 60 against 60 at 0 digits
        self.assertTrue(all(r["rendition"].startswith("Opus, smallest frame") for r in rows))


class Joins(unittest.TestCase):
    def test_every_join_is_a_new_listener_with_its_own_session(self):
        class Session(FakeSession):
            dir = pathlib.Path(tempfile.mkdtemp())
            steps: list = []

            def ip(self, name):
                return "10.0.0.1"
        captured = {}

        def run_hosts(session, name, scripts, total):
            captured.update(scripts=scripts, total=total)
            (session.dir / name).mkdir(parents=True, exist_ok=True)
            return {"seconds": total, "host_errors": {}}
        saved = (audio.run.run_hosts, audio.SITES)
        audio.run.run_hosts, audio.SITES = run_hosts, ["lon"]
        try:
            a = audio.Audio(Session(), "opus-2.5", ["lkregion", "nt-popus", "nt-udp", "nt-uc"])
            a.status = lambda: {}
            a.joins(3)
        finally:
            audio.run.run_hosts, audio.SITES = saved
        script = captured["scripts"]["lon-view"]
        self.assertEqual(list(captured["scripts"]), ["lon-view"])
        self.assertNotIn("nt-udp", script)
        self.assertNotIn("$J", script)
        self.assertNotIn("--frames", script)
        for j in ("01", "02", "03"):
            self.assertIn(f"mv -f lkregion.probe.json lkregion.join-{j}.json", script)
            self.assertIn(f"--identity t-lon-join{j}-lkregion", script)
            self.assertIn(f"sid=t-lon-join{j}-nt-popus-h3-", script)
            self.assertIn(f"--session t-lon-join{j}-uc-0 ", script)
            self.assertIn(f"mv -f nt-uc.uc.json nt-uc.join-{j}.uc.json", script)
        self.assertEqual(script.count("--skip-seconds 1 "), 3 * 5)
        self.assertNotIn("--skip-seconds 5", script)
        meta = json.loads((Session.dir / "audio.opus-2.5.joins.3" / "meta.json").read_text())
        self.assertEqual((meta["phase"], meta["joins_per_lane"]), ("joins", 3))


ROOT = HERE.parent.parent
NTCMP = ROOT / "target" / "ntcmp"
LOOP = ROOT / "target" / "latency-bench" / "media" / "loop64.s24le"


class Indexes(unittest.TestCase):
    @unittest.skipUnless(NTCMP.is_file() and LOOP.is_file(), "needs target/ntcmp and the 64 s loop")
    def test_the_needletail_indexes_are_made(self):
        sys.path.insert(0, str(HERE))
        import compare_lab
        media = pathlib.Path(tempfile.mkdtemp())
        (media / "audio.s24le").symlink_to(LOOP)
        made = compare_lab.needletail_indexes(media, NTCMP)
        self.assertEqual([path.name for path in made],
                         ["nt-pcm-48s.index.json", "nt-pcm-24s.index.json", "nt-pcm-12s.index.json",
                          "nt-popus-120s-la.index.json", "nt-opus-120s-b64.index.json",
                          "nt-flac-48s-b64.index.json"])
        units = {path.name: json.loads(path.read_text()) for path in made}
        self.assertEqual(units["nt-pcm-12s.index.json"]["frame_samples"], 12)
        self.assertEqual(units["nt-popus-120s-la.index.json"]["frame_samples"], 120)
        self.assertEqual(units["nt-popus-120s-la.index.json"]["kind"], "numbered")
        # Every lane's index is among them.
        names = {path.name[:-len(".index.json")] for path in made}
        for samples in audio.NT_PCM_SAMPLES:
            self.assertIn(audio.nt_index("nt-pcm", samples), names)
        self.assertIn(audio.nt_index("nt-popus", 48), names)
        for base in ("nt-opus", "nt-flac"):
            self.assertIn(audio.nt_index(base, 48), names)
        self.assertEqual(units["nt-opus-120s-b64.index.json"]["frame_samples"], 120)
        self.assertEqual(units["nt-opus-120s-b64.index.json"]["preskip"], 184)
        self.assertEqual(units["nt-flac-48s-b64.index.json"]["frame_samples"], 48)
        self.assertEqual(units["nt-flac-48s-b64.index.json"]["preskip"], 64)


class Report(unittest.TestCase):
    def evidence(self, lanes: list[str]) -> pathlib.Path:
        path = pathlib.Path(tempfile.mkdtemp()) / "evidence.json"
        path.write_text(json.dumps({"sessions": {"s": {"steps": {"audio.opus-2.5.baseline.clean": {
            "hosts": {"lon-view": {"lanes": {lane: {} for lane in lanes}}}}}}}}))
        return path

    def test_the_opus_lane_is_the_one_the_evidence_has(self):
        e = report.Evidence(str(self.evidence(["nt-popus", "nt-popus-h3", "cf"])))
        self.assertEqual(e.lane("nt-h3", "opus-2.5"), "nt-popus-h3")
        self.assertEqual(e.lane("nt", "opus-2.5"), "nt-popus")
        self.assertEqual(e.lane("cf", "opus-2.5"), "cf")
        old = report.Evidence(str(self.evidence(["nt-opus", "nt-opus-h3"])))
        self.assertEqual(old.lane("nt-h3", "opus-2.5"), "nt-opus-h3")
        self.assertEqual(old.lane("nt", "pcm"), "nt-pcm")


class ChangeWindow(unittest.TestCase):
    """change-window.py: units missing, longest silence and p99 in a window."""

    cw = load("change_window", "change-window.py")
    T0 = 1_800_000_000_000_000  # Unix microseconds

    def frames(self, drop=(), stall=None):
        # 30 s of 1 ms units, 3 ms mouth to ear. stall=(first, last, ms): units
        # first..last are held and arrive together `ms` after the first was due.
        lines = ["kind,index,latency_us,complete_unix_us,first_unix_us,bytes,key,packets,"
                 "published_unix_us,cache_available_unix_us,edge_committed_unix_us,reply_unix_us,part"]
        for n in range(30_000):
            if n in drop:
                continue
            source = self.T0 + n * 1000
            at = source + 3000
            if stall and stall[0] <= n <= stall[1]:
                at = self.T0 + stall[0] * 1000 + stall[2] * 1000
            lines.append(f"a,{n % 1000},{at - source},{at},{n},80,0,1,,,,,{n}")
        path = pathlib.Path(tempfile.mkdtemp()) / "f.csv"
        path.write_text("\n".join(lines) + "\n")
        return self.cw.load(str(path))

    def window(self, rows):
        return self.cw.score(rows, self.T0 + 15_000_000, self.T0 + 18_000_000, 5_000_000)

    def test_clean_change_passes(self):
        r = self.window(self.frames())
        self.assertEqual(r["missing"], 0)
        self.assertTrue(r["pass"], r)

    def test_lost_units_fail(self):
        r = self.window(self.frames(drop=range(16_000, 16_005)))
        self.assertEqual(r["missing"], 5)
        self.assertFalse(r["pass"])

    def test_stall_fails_on_gap_and_p99(self):
        r = self.window(self.frames(stall=(16_000, 16_299, 300)))
        self.assertEqual(r["missing"], 0)
        self.assertGreaterEqual(r["longest_gap_ms"], 290)
        self.assertGreater(r["inside_ms"]["p99"], 100)
        self.assertFalse(r["pass"])

    def test_marks(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "marks.txt"
        path.write_text("start 100.5 drain lon-edge-1\nend 103 drain lon-edge-1\nstart 200 kill ingress\nend 201 kill ingress\n")
        w = self.cw.windows(type("A", (), {"window": None, "marks": str(path)})())
        self.assertEqual(w, {"drain lon-edge-1": (100.5, 103.0), "kill ingress": (200.0, 201.0)})


if __name__ == "__main__":
    unittest.main()
