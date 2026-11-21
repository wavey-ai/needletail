#!/usr/bin/env python3
"""Tests of the part settings of the deploy tool and the lab: the smallest
parts by default, the version 1 keys mapped, a saved settings file of
version 1 never overriding the new defaults, and the PART-TARGET rule of the
spec checker reported as information.
python3 -m unittest scripts/test_part_settings.py"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402
import hls_spec  # noqa: E402

_spec = importlib.util.spec_from_file_location("deploy", HERE / "linode-small-mesh-deploy.py")
deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy)


def value_of(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def contributor_service() -> dict:
    lane = {"target": "10.0.0.2:1", "bind": "0.0.0.0:2", "child_node_id": "relay"}
    return {"primary": lane, "warm_secondary": lane, "audio_epoch_ingress_target": "10.0.0.2:3",
            "topology_generation": 1, "subscription_id": 1, "media_deadline_ms": 1000}


class Settings(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        for target, value in ((deploy, "SETTINGS_FILE"), ):
            patcher = mock.patch.object(target, value, self.dir / "settings.json")
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(deploy.common, "ARTIFACT_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, overrides=()):
        err = io.StringIO()
        with redirect_stderr(err):
            settings = deploy.load_settings(list(overrides))
        return settings, err.getvalue()

    def test_the_defaults_are_the_contract(self):
        settings, _ = self.load()
        for key, value in (("part_us", 2500), ("segment_ms", 1000), ("window_ms", 12000),
                           ("audio_part", "epoch"), ("video_part", "frame"),
                           ("pcm_epoch_samples", 48), ("opus_epoch_samples", 120),
                           ("settings_version", 2)):
            self.assertEqual(settings[key], value, key)
        plan = deploy.part_plan(settings)
        self.assertEqual(plan["mesh_args"], ["--part-us", "2500", "--segment-ms", "1000",
                                             "--window-ms", "12000"])

    def test_mesh_and_contributor_arguments(self):
        settings, _ = self.load()
        node = {"name": "lon-edge-1", "role": "edge", "site": "london", "region": "gb-lon",
                "public_ipv4": "10.90.0.21", "vlan_ipv4": "10.77.1.21"}
        args = deploy.common_mesh_args(node, settings, {"nodes": {node["name"]: node}})
        self.assertEqual(value_of(args, "--part-us"), "2500")
        self.assertEqual(value_of(args, "--segment-ms"), "1000")
        self.assertEqual(value_of(args, "--window-ms"), "12000")
        for old in ("--part-ms", "--parts-per-segment", "--window-parts", "--part-hold-back-ms"):
            self.assertNotIn(old, args)
        contrib = deploy.contributor_args(contributor_service(), settings)
        self.assertEqual(value_of(contrib, "--audio-part"), "epoch")
        self.assertEqual(value_of(contrib, "--video-part"), "frame")
        self.assertNotIn("--fmp4-part-ms", contrib)
        self.assertNotIn("--fmp4-video-part-ms", contrib)

    def test_a_saved_version_1_file_does_not_override_the_defaults(self):
        (self.dir / "settings.json").write_text(json.dumps({
            "part_ms": 5, "parts_per_segment": 200, "window_parts": 2400, "fmp4_part_ms": 5,
            "fmp4_video_part_ms": 200, "slot_kb": 4096, "mesh_sync_interval_ms": 2}))
        settings, err = self.load()
        self.assertIn("dropped the saved version 1 part settings", err)
        self.assertEqual(deploy.part_plan(settings)["mesh_args"][:2], ["--part-us", "2500"])
        self.assertEqual(deploy.part_plan(settings)["audio_part"], "epoch")
        self.assertEqual(deploy.part_plan(settings)["video_part"], "frame")
        for key in deploy.LEGACY_PART_KEYS:
            self.assertIsNone(settings[key], key)
        # Other saved settings are kept.
        self.assertEqual(settings["slot_kb"], 4096)
        self.assertEqual(settings["mesh_sync_interval_ms"], 2)
        saved = json.loads((self.dir / "settings.json").read_text())
        self.assertEqual(saved["settings_version"], 2)
        self.assertIsNone(saved["part_ms"])

    def test_old_keys_given_in_this_run_are_mapped(self):
        settings, _ = self.load(["part_ms=5", "parts_per_segment=200", "window_parts=2400",
                                 "fmp4_part_ms=5", "fmp4_video_part_ms=200"])
        plan = deploy.part_plan(settings)
        self.assertEqual(plan["mesh_args"], ["--part-us", "5000", "--parts-per-segment", "200",
                                             "--window-parts", "2400"])
        self.assertEqual(plan["audio_part"], "5000us")
        self.assertEqual(plan["video_part"], "200ms")
        # Saved in a version 2 file: a later run without --set keeps them.
        settings, err = self.load()
        self.assertNotIn("dropped", err)
        self.assertEqual(deploy.part_plan(settings), plan)
        # KEY=null clears one.
        settings, _ = self.load(["part_ms=null", "parts_per_segment=null", "window_parts=null"])
        self.assertEqual(deploy.part_plan(settings)["part_us"], 2500)
        self.assertEqual(deploy.part_plan(settings)["audio_part"], "5000us")

    def test_a_role_limited_restart_keeps_the_pushed_parts(self):
        # The README's restore recipe, pushed; then compare-audio.py's
        # `restart --roles contributor --set daw_hls_formats=...`.
        pushed, _ = self.load(["part_ms=5", "fmp4_part_ms=5"])
        restarted, err = self.load(["daw_hls_formats=pcm"])
        self.assertNotIn("dropped", err)
        self.assertEqual(deploy.part_plan(restarted), deploy.part_plan(pushed))
        self.assertEqual(restarted["daw_hls_formats"], "pcm")
        # A plain `plan` rewrites the file with the same parts.
        again, _ = self.load()
        self.assertEqual(deploy.part_plan(again)["mesh_args"][:2], ["--part-us", "5000"])
        saved = json.loads((self.dir / "settings.json").read_text())
        self.assertEqual((saved["part_ms"], saved["settings_version"]), (5, 2))

    def test_only_a_version_1_file_loses_its_part_keys(self):
        for version in (None, 1, "1"):
            saved = {"part_ms": 5, "fmp4_part_ms": 5}
            if version is not None:
                saved["settings_version"] = version
            settings, dropped = deploy.merge_settings(saved, {})
            self.assertEqual(dropped, {"part_ms": 5, "fmp4_part_ms": 5}, version)
            self.assertIsNone(settings["part_ms"])
        settings, dropped = deploy.merge_settings({"settings_version": 2, "part_ms": 5}, {})
        self.assertEqual((dropped, settings["part_ms"]), ({}, 5))
        # Given in this run, a version 1 key counts.
        settings, dropped = deploy.merge_settings({"part_ms": 5}, {"part_ms": 10})
        self.assertEqual((dropped, settings["part_ms"]), ({}, 10))

    def test_fmp4_part_ms_alone_was_the_video_part_too(self):
        settings, _ = self.load(["fmp4_part_ms=200"])
        plan = deploy.part_plan(settings)
        self.assertEqual((plan["audio_part"], plan["video_part"]), ("200000us", "200ms"))
        settings, _ = self.load(["fmp4_part_ms=2.5"])
        plan = deploy.part_plan(settings)
        self.assertEqual((plan["audio_part"], plan["video_part"]), ("2500us", "frame"))

    def test_fractional_ms_parts_become_us(self):
        settings, _ = self.load(["part_ms=0.25"])
        self.assertEqual(deploy.part_plan(settings)["part_us"], 250)
        self.assertEqual(deploy.deployed_part_ms(settings), 0.25)

    def test_new_keys_persist_and_are_validated(self):
        settings, _ = self.load(["part_us=1000", "audio_part=48samples", "video_part=40ms",
                                 "part_hold_back_ms=250"])
        plan = deploy.part_plan(settings)
        self.assertEqual(plan["audio_part"], "48samples")
        self.assertEqual(plan["video_part"], "40ms")
        self.assertIn("--part-hold-back-ms", plan["mesh_args"])
        settings, _ = self.load()
        self.assertEqual(settings["part_us"], 1000)
        for bad in (["audio_part=2.5ms"], ["video_part=2"], ["part_us=0"], ["part_ms=0.0001"],
                    ["pcm_epoch_samples=6"], ["opus_epoch_samples=100"]):
            with self.assertRaises(SystemExit, msg=bad):
                self.load(bad)

    def test_epoch_samples_by_payload(self):
        settings, _ = self.load()
        self.assertEqual(deploy.epoch_samples(settings, "pcm"), 48)
        self.assertEqual(deploy.epoch_samples(settings, "flac"), 48)
        self.assertEqual(deploy.epoch_samples(settings, "opus"), 120)
        settings, _ = self.load(["pcm_epoch_samples=12"])
        self.assertEqual(deploy.epoch_samples(settings, "pcm"), 12)
        self.assertEqual(deploy.epoch_samples(settings, "flac"), 32)


class Lab(unittest.TestCase):
    def test_the_lab_defaults_are_the_smallest_parts(self):
        for key in deploy.LEGACY_PART_KEYS:
            self.assertNotIn(key, lab.DEFAULT_OVERRIDES)
        settings, dropped = deploy.merge_settings({}, lab.DEFAULT_OVERRIDES)
        self.assertFalse(dropped)
        plan = deploy.part_plan(settings)
        self.assertEqual(plan["mesh_args"], ["--part-us", "2500", "--segment-ms", "1000",
                                             "--window-ms", "12000"])
        self.assertEqual((plan["audio_part"], plan["video_part"]), ("epoch", "frame"))

    def test_the_probe_sender_follows_the_lab_settings(self):
        work = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.object(lab, "WORK", work):
            lab.save("settings.json", {"pcm_epoch_samples": 24, "opus_epoch_samples": 240})
            self.assertEqual(lab.probe_epoch_samples("pcm"), 24)
            self.assertEqual(lab.probe_epoch_samples("flac"), 32)
            self.assertEqual(lab.probe_epoch_samples("opus"), 240)
            lab.save("settings.json", {})
            self.assertEqual(lab.probe_epoch_samples("flac"), 48)


class SpecChecker(unittest.TestCase):
    def playlist(self, part_target: str, duration: str, count: int) -> dict:
        parts = "".join(f'#EXT-X-PART:DURATION={duration},URI="part{i}.bin"\n' for i in range(count))
        return hls_spec.parse_media(
            "#EXTM3U\n#EXT-X-VERSION:9\n#EXT-X-TARGETDURATION:1\n"
            f"#EXT-X-PART-INF:PART-TARGET={part_target}\n"
            f"#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK={float(part_target) * 3},"
            "HOLD-BACK=3\n#EXT-X-MEDIA-SEQUENCE:10\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-11-21T00:00:00.000Z\n"
            f"{parts}#EXTINF:1.0,\nseg10.bin\n"
            f'#EXT-X-PART:DURATION={duration},URI="part{count}.bin"\n'
            f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="part{count + 1}.bin"\n')

    def test_a_part_target_below_a_round_trip_is_information(self):
        found = hls_spec.summarise(hls_spec.check_media(self.playlist("0.0025", "0.0025", 400),
                                                        video=False, fmp4=False))
        self.assertFalse(found["fail"])
        self.assertNotIn(26, [item["rule"] for item in found["warn"]])
        self.assertEqual([item["rule"] for item in found["info"]], [26])

    def test_rounded_part_durations_still_fail(self):
        # 400 parts written as 0.003 against PART-TARGET 0.0025: too long.
        found = hls_spec.summarise(hls_spec.check_media(self.playlist("0.0025", "0.003", 400),
                                                        video=False, fmp4=False))
        self.assertIn(12, [item["rule"] for item in found["fail"]])


if __name__ == "__main__":
    unittest.main()
