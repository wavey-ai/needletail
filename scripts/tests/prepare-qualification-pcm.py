#!/usr/bin/env python3
"""Tests for the no-FFmpeg qualification PCM preparer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    ROOT
    / "scripts"
    / "multicloud-qualification"
    / "prepare-qualification-pcm.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_qualification_pcm", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
PREPARE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PREPARE
SPEC.loader.exec_module(PREPARE)


def riff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padding = b"\0" if len(payload) & 1 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


def write_pcm_wav(
    path: Path,
    frames: list[bytes],
    *,
    format_tag: int = 1,
    channels: int = 2,
    sample_rate_hz: int = 48_000,
    bits_per_sample: int = 24,
) -> None:
    bytes_per_frame = channels * (bits_per_sample // 8)
    fmt = struct.pack(
        "<HHIIHH",
        format_tag,
        channels,
        sample_rate_hz,
        sample_rate_hz * bytes_per_frame,
        bytes_per_frame,
        bits_per_sample,
    )
    body = (
        b"WAVE"
        + riff_chunk(b"JUNK", b"x")
        + riff_chunk(b"fmt ", fmt)
        + riff_chunk(b"data", b"".join(frames))
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def sample_frame(value: int) -> bytes:
    sample = value.to_bytes(3, "little", signed=False)
    return sample + sample


class QualificationPcmTest(unittest.TestCase):
    def setUp(self) -> None:
        test_parent = ROOT / "target" / "script-tests"
        test_parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="prepare-qualification-pcm-",
            dir=test_parent,
        )
        self.work = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_sources(self) -> tuple[list[Path], list[list[bytes]]]:
        sources = []
        source_frames = []
        for index, frame_count in enumerate(range(2, 10)):
            frames = [sample_frame(index * 100 + frame) for frame in range(frame_count)]
            path = self.work / f"track-{index}.wav"
            write_pcm_wav(path, frames)
            sources.append(path)
            source_frames.append(frames)
        return sources, source_frames

    def test_prepares_project_aligned_repeated_tracks(self) -> None:
        sources, source_frames = self.make_sources()
        output = self.work / "prepared"

        metadata = PREPARE.prepare_fixtures(
            sources,
            output,
            target_frames=25,
        )

        self.assertEqual(metadata["project_cycle_frames"], 9)
        self.assertEqual(metadata["final_partial_cycle_frames"], 7)
        self.assertEqual(metadata["target_bytes_per_track"], 150)
        for index, frames in enumerate(source_frames):
            cycle = b"".join(frames) + bytes((9 - len(frames)) * 6)
            expected = (cycle * 3)[:150]
            path = output / f"source-track-{index:02d}.s24le"
            self.assertEqual(path.read_bytes(), expected)

        manifest_lines = (output / "manifest.sha256").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(manifest_lines), 8)
        for index, line in enumerate(manifest_lines):
            digest, relative_path = line.split("  ", 1)
            self.assertEqual(relative_path, f"source-track-{index:02d}.s24le")
            self.assertEqual(
                digest,
                hashlib.sha256((output / relative_path).read_bytes()).hexdigest(),
            )

        saved_metadata = json.loads(
            (output / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved_metadata, metadata)

    def test_rejects_non_pcm_format_without_conversion(self) -> None:
        sources, _ = self.make_sources()
        write_pcm_wav(
            sources[3],
            [sample_frame(1)],
            format_tag=0xFFFE,
        )

        with self.assertRaisesRegex(PREPARE.PreparationError, "must contain stereo"):
            PREPARE.prepare_fixtures(
                sources,
                self.work / "prepared",
                target_frames=25,
            )

    def test_rejects_a_source_longer_than_the_fixture(self) -> None:
        sources, _ = self.make_sources()

        with self.assertRaisesRegex(PREPARE.PreparationError, "longest source"):
            PREPARE.prepare_fixtures(
                sources,
                self.work / "prepared",
                target_frames=8,
            )

    def test_replaces_only_after_the_new_set_is_complete(self) -> None:
        sources, _ = self.make_sources()
        output = self.work / "prepared"
        PREPARE.prepare_fixtures(sources, output, target_frames=25)
        old_hash = hashlib.sha256(
            (output / "source-track-00.s24le").read_bytes()
        ).hexdigest()
        write_pcm_wav(sources[0], [sample_frame(999), sample_frame(998)])

        PREPARE.prepare_fixtures(
            sources,
            output,
            target_frames=25,
            replace=True,
        )

        new_hash = hashlib.sha256(
            (output / "source-track-00.s24le").read_bytes()
        ).hexdigest()
        self.assertNotEqual(new_hash, old_hash)
        self.assertFalse(list(self.work.glob(".prepared.prepare-*")))
        self.assertFalse(list(self.work.glob(".prepared.previous-*")))


if __name__ == "__main__":
    unittest.main()
