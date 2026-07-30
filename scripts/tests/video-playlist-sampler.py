#!/usr/bin/env python3
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SAMPLER = ROOT / "scripts" / "multicloud-qualification" / "video-playlist-sampler.py"
SPEC = importlib.util.spec_from_file_location("video_playlist_sampler", SAMPLER)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VideoPlaylistSamplerTest(unittest.TestCase):
    def test_playlist_media_end_and_part_progress_are_deterministic(self) -> None:
        playlist = """#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-07-30T12:00:00.000Z
#EXT-X-PART:DURATION=0.250,URI="part40.mp4"
#EXT-X-PART:DURATION=0.250,URI="part41.mp4"
"""
        arrival_ns = 1_785_412_800_750_000_000
        parsed = MODULE.parse_playlist(playlist, arrival_ns)
        self.assertEqual(parsed["latest_part_number"], 41)
        self.assertEqual(parsed["parts_after_program_date_time"], 2)
        self.assertEqual(parsed["latest_media_end_unix_ns"], 1_785_412_800_500_000_000)
        self.assertEqual(parsed["live_edge_latency_ms"], 250.0)

    def test_stop_file_finishes_sampler_and_publishes_atomic_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stop_file = directory / "sampler.stop"
            status_file = directory / "sampler.status"
            stop_file.touch()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SAMPLER),
                    "--duration-seconds",
                    "10",
                    "--interval-ms",
                    "200",
                    "--server-name",
                    "qualification.example.test",
                    "--status-file",
                    str(status_file),
                    "--stop-file",
                    str(stop_file),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(status_file.read_text(encoding="ascii"), "0\n")
            self.assertEqual(list(directory.glob(".sampler.status.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
