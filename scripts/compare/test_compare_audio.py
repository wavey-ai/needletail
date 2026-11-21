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
        self.assertIn("/index/nt-popus-120s.index.json", hls)
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
                          "nt-popus-120s.index.json"])
        units = {path.name: json.loads(path.read_text()) for path in made}
        self.assertEqual(units["nt-pcm-12s.index.json"]["frame_samples"], 12)
        self.assertEqual(units["nt-popus-120s.index.json"]["frame_samples"], 120)
        self.assertEqual(units["nt-popus-120s.index.json"]["kind"], "numbered")
        # Every lane's index is among them.
        names = {path.name[:-len(".index.json")] for path in made}
        for samples in audio.NT_PCM_SAMPLES:
            self.assertIn(audio.nt_index("nt-pcm", samples), names)
        self.assertIn(audio.nt_index("nt-popus", 48), names)


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


if __name__ == "__main__":
    unittest.main()
