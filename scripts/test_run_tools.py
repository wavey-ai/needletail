#!/usr/bin/env python3
"""Tests of the parts that the Linode run and soak tools tell their checkers:
each stream's own audio part in whole ms, never the caches' nominal part.
python3 -m unittest scripts/test_run_tools.py"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re
import sys
import unittest
import unittest.mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = load("small_mesh_run_test", "linode-small-mesh-run.py")
soak = load("small_mesh_soak_test", "linode-small-mesh-soak.py")
deploy = run.deploy

PART_MS = re.compile(r"--part-ms (\S+)")


def defaults(**overrides) -> dict:
    settings, _ = deploy.merge_settings({}, overrides)
    return settings


def soak_streams(settings: dict, lossless: int = 48, opus: int = 240) -> list[dict]:
    streams = [{"name": "flac0", "stream_id": 2001, "codec": "opaque-flac",
                "epoch_samples": lossless},
               {"name": "opus0", "stream_id": 3001, "codec": "soundkit-opus",
                "epoch_samples": opus}]
    run.assign_part_ms(settings, streams)
    return streams


def soak_plan(streams: list[dict]) -> dict:
    return {"session_id": 1, "duration": 60, "tail": 6, "load_deadline_ms": 400,
            "readers": 2, "window": 30, "streams": streams}


class Parts(unittest.TestCase):
    def test_soak_commands_under_the_default_settings(self):
        # Version 2 defaults: part_us 2500 (2.5 ms nominal parts) and
        # audio_part epoch. The checkers get each stream's epoch, as an integer.
        streams = soak_streams(defaults())
        edge = {"vlan_ipv4": "10.0.0.5/24"}
        plan = soak_plan(streams)
        flac = soak.hls(edge, streams[0], plan, 0, None)
        opus = soak.hls(edge, streams[1], plan, 0, None)
        load = soak.load_command(edge, streams[0], plan, 0)
        self.assertEqual(PART_MS.findall(flac), ["1"])
        self.assertEqual(PART_MS.findall(opus), ["5"])
        self.assertEqual(PART_MS.findall(load), ["1"])
        for command in (flac, opus, load):
            for value in PART_MS.findall(command):
                self.assertRegex(value, r"^[1-9][0-9]*$")

    def test_opus_at_2_5_ms_is_refused_early(self):
        with self.assertRaises(SystemExit) as caught:
            soak_streams(defaults(), opus=120)
        self.assertIn("opus0", str(caught.exception))
        self.assertIn("2.5 ms", str(caught.exception))
        with self.assertRaises(SystemExit):
            soak_streams(defaults(), lossless=24)

    def test_the_audio_part_setting_wins_over_the_epoch(self):
        streams = soak_streams(defaults(audio_part="5000us"), lossless=48, opus=120)
        self.assertEqual([item["part_ms"] for item in streams], [5, 5])
        # The README's restore recipe: 5 ms parts everywhere.
        streams = soak_streams(defaults(part_ms=5, fmp4_part_ms=5), lossless=48, opus=120)
        self.assertEqual([item["part_ms"] for item in streams], [5, 5])
        with self.assertRaises(SystemExit):
            soak_streams(defaults(audio_part="2500us"))

    def test_a_settings_file_without_the_new_keys(self):
        old = {"part_ms": 5, "fmp4_part_ms": 5, "daw_hls_base_stream_id": 2001}
        streams = soak_streams(old)
        self.assertEqual([item["part_ms"] for item in streams], [5, 5])

    def test_run_streams_carry_their_epochs(self):
        args = argparse.Namespace(source="daw", tracks=1, opus_codec="soundkit-opus",
                                  daw_lossless_epoch_samples=48, daw_opus_frame_samples=240,
                                  epoch_samples=48, duration=30, tail=4, deadline_ms=1000)
        settings = defaults(daw_hls_formats="flac,opus")
        plan = run.stream_plan(args, settings, 0)
        run.assign_part_ms(settings, plan["streams"])
        self.assertEqual({item["name"]: item["part_ms"] for item in plan["streams"]},
                         {"flac0": 1, "opus0": 5})
        args.part_ms = 99
        command = run.hls_command("10.0.0.5:19444", 3001, args, 1, "soundkit-opus",
                                  part_ms=plan["streams"][1]["part_ms"])
        self.assertEqual(PART_MS.findall(command), ["5"])
        args.source = "synthetic"
        plan = run.stream_plan(args, settings, 123)
        run.assign_part_ms(settings, plan["streams"])
        self.assertEqual(plan["streams"][0]["part_ms"], 1)

    def test_the_daw_source_is_told_its_epochs(self):
        self.assertEqual(run.daw_epoch_args(48, 240).split(),
                         ["--lossless-epoch-samples", "48", "--opus-frame-samples", "240"])
        # The lane's default: 1 ms Opus Custom at 320 kb/s.
        self.assertEqual((run.DAW_OPUS_FRAME_SAMPLES, run.DAW_OPUS_BITRATE), (48, 320000))
        self.assertEqual(run.daw_epoch_args(48, 48, 256000).split()[-2:],
                         ["--opus-bitrate", "256000"])



class AudioEpochReaders(unittest.TestCase):
    """Wave 7 review: without the legacy AEP1 lane (the default) nothing
    feeds the distributors' native UDP and WebTransport readers, so the soak
    and the run do not start them; a settings file saved before the setting
    existed rendered the lane."""

    def test_the_soak_starts_aep1_readers_only_with_the_lane(self):
        streams = soak_streams(defaults())
        distributor = {"name": "lon-dist-a", "vlan_ipv4": "10.0.0.11/24",
                       "role": "distributor", "relay_port": 22001}
        edge = {"name": "lon-edge-1", "vlan_ipv4": "10.0.0.21/24", "role": "edge"}
        for lane, expected in ((False, False), (True, True)):
            plan = {**soak_plan(streams), "remote": "/tmp/run", "windows": 1,
                    "load_edges": 0, "settings": defaults(audio_epoch_lane=lane)}
            with unittest.mock.patch.object(soak.mesh_run, "relay_port", return_value=22001):
                script = soak.site_driver({}, plan, [distributor], [edge])
            self.assertEqual("receive-udp" in script, expected, lane)
            self.assertEqual("receive-webtransport" in script, expected, lane)
            self.assertIn("receive-hls", script)

    def test_a_settings_file_without_the_setting_had_the_lane(self):
        self.assertTrue(run.aep1_lane({}))
        self.assertFalse(run.aep1_lane(defaults()))
        self.assertTrue(run.aep1_lane(defaults(audio_epoch_lane=True)))


if __name__ == "__main__":
    unittest.main()
