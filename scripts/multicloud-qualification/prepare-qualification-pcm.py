#!/usr/bin/env python3
"""Prepare synchronized stereo S24LE qualification tracks without FFmpeg."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import uuid
from typing import BinaryIO, Iterable


SAMPLE_RATE_HZ = 48_000
CHANNELS = 2
BITS_PER_SAMPLE = 24
BYTES_PER_SAMPLE = BITS_PER_SAMPLE // 8
BYTES_PER_FRAME = CHANNELS * BYTES_PER_SAMPLE
DURATION_SECONDS = 600
TARGET_FRAMES = SAMPLE_RATE_HZ * DURATION_SECONDS
TARGET_BYTES = TARGET_FRAMES * BYTES_PER_FRAME
TRACK_COUNT = 8
COPY_BUFFER_BYTES = 1024 * 1024
SILENCE_BUFFER = bytes(COPY_BUFFER_BYTES)
MANIFEST_NAME = "manifest.sha256"
METADATA_NAME = "metadata.json"


class PreparationError(ValueError):
    """Report an invalid input or fixture layout."""


@dataclasses.dataclass(frozen=True)
class PcmWav:
    path: Path
    file_bytes: int
    data_offset: int
    data_bytes: int
    frames: int


def _read_exact(source: BinaryIO, byte_count: int, description: str) -> bytes:
    value = source.read(byte_count)
    if len(value) != byte_count:
        raise PreparationError(f"{description} is incomplete")
    return value


def inspect_pcm_wav(path: Path) -> PcmWav:
    """Read RIFF metadata and accept only stereo 48 kHz 24-bit integer PCM."""

    path = path.resolve()
    try:
        file_bytes = path.stat().st_size
    except OSError as error:
        raise PreparationError(f"cannot read {path}: {error}") from error
    if not path.is_file():
        raise PreparationError(f"{path} is not a regular file")
    if file_bytes < 12:
        raise PreparationError(f"{path} is too short to be a RIFF WAVE file")

    fmt_fields = None
    data_region = None
    with path.open("rb") as source:
        header = _read_exact(source, 12, f"{path} RIFF header")
        riff_id, riff_bytes, wave_id = struct.unpack("<4sI4s", header)
        if riff_id != b"RIFF" or wave_id != b"WAVE":
            raise PreparationError(f"{path} is not a RIFF WAVE file")
        riff_end = riff_bytes + 8
        if riff_end != file_bytes:
            raise PreparationError(
                f"{path} RIFF size is {riff_end} bytes; file size is {file_bytes} bytes"
            )

        position = 12
        while position < riff_end:
            if riff_end - position < 8:
                raise PreparationError(f"{path} has an incomplete RIFF chunk header")
            source.seek(position)
            chunk_id, chunk_bytes = struct.unpack(
                "<4sI", _read_exact(source, 8, f"{path} RIFF chunk header")
            )
            payload_offset = position + 8
            payload_end = payload_offset + chunk_bytes
            padded_end = payload_end + (chunk_bytes & 1)
            if padded_end > riff_end:
                name = chunk_id.decode("ascii", errors="replace")
                raise PreparationError(f"{path} has an incomplete {name!r} chunk")

            if chunk_id == b"fmt ":
                if fmt_fields is not None:
                    raise PreparationError(f"{path} contains more than one fmt chunk")
                if chunk_bytes < 16:
                    raise PreparationError(f"{path} has an incomplete fmt chunk")
                source.seek(payload_offset)
                fmt_fields = struct.unpack(
                    "<HHIIHH", _read_exact(source, 16, f"{path} fmt chunk")
                )
            elif chunk_id == b"data":
                if data_region is not None:
                    raise PreparationError(f"{path} contains more than one data chunk")
                data_region = (payload_offset, chunk_bytes)

            position = padded_end

    if fmt_fields is None:
        raise PreparationError(f"{path} does not contain a fmt chunk")
    if data_region is None:
        raise PreparationError(f"{path} does not contain a data chunk")

    (
        format_tag,
        channels,
        sample_rate_hz,
        byte_rate,
        block_align,
        bits_per_sample,
    ) = fmt_fields
    expected_byte_rate = SAMPLE_RATE_HZ * BYTES_PER_FRAME
    expected = (
        1,
        CHANNELS,
        SAMPLE_RATE_HZ,
        expected_byte_rate,
        BYTES_PER_FRAME,
        BITS_PER_SAMPLE,
    )
    if fmt_fields != expected:
        actual = (
            f"format={format_tag}, channels={channels}, "
            f"sample_rate={sample_rate_hz}, byte_rate={byte_rate}, "
            f"block_align={block_align}, bits_per_sample={bits_per_sample}"
        )
        raise PreparationError(
            f"{path} must contain stereo 48 kHz 24-bit little-endian PCM; got {actual}"
        )

    data_offset, data_bytes = data_region
    if data_bytes == 0:
        raise PreparationError(f"{path} contains no PCM frames")
    if data_bytes % BYTES_PER_FRAME != 0:
        raise PreparationError(
            f"{path} data size {data_bytes} is not a whole stereo S24LE frame count"
        )
    return PcmWav(
        path=path,
        file_bytes=file_bytes,
        data_offset=data_offset,
        data_bytes=data_bytes,
        frames=data_bytes // BYTES_PER_FRAME,
    )


def _copy_region(
    source: BinaryIO,
    output: BinaryIO,
    byte_count: int,
    digest: "hashlib._Hash",
) -> None:
    remaining = byte_count
    while remaining:
        block = source.read(min(remaining, COPY_BUFFER_BYTES))
        if not block:
            raise PreparationError("a source WAVE data chunk changed during preparation")
        output.write(block)
        digest.update(block)
        remaining -= len(block)


def _write_silence(
    output: BinaryIO, byte_count: int, digest: "hashlib._Hash"
) -> None:
    remaining = byte_count
    while remaining:
        block = SILENCE_BUFFER[: min(remaining, len(SILENCE_BUFFER))]
        output.write(block)
        digest.update(block)
        remaining -= len(block)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(COPY_BUFFER_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _write_track(
    source_info: PcmWav,
    project_frames: int,
    target_frames: int,
    destination: Path,
) -> str:
    temporary = destination.with_name(f".{destination.name}.part-{uuid.uuid4().hex}")
    digest = hashlib.sha256()
    remaining_frames = target_frames
    try:
        with source_info.path.open("rb") as source, temporary.open("xb") as output:
            while remaining_frames:
                audio_frames = min(source_info.frames, remaining_frames)
                source.seek(source_info.data_offset)
                _copy_region(
                    source,
                    output,
                    audio_frames * BYTES_PER_FRAME,
                    digest,
                )
                remaining_frames -= audio_frames
                if remaining_frames == 0:
                    break

                silence_frames = min(
                    project_frames - source_info.frames,
                    remaining_frames,
                )
                _write_silence(
                    output,
                    silence_frames * BYTES_PER_FRAME,
                    digest,
                )
                remaining_frames -= silence_frames

            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    expected_bytes = target_frames * BYTES_PER_FRAME
    actual_bytes = destination.stat().st_size
    if actual_bytes != expected_bytes:
        raise PreparationError(
            f"{destination} is {actual_bytes} bytes; expected {expected_bytes} bytes"
        )
    written_hash = digest.hexdigest()
    verified_hash = _hash_file(destination)
    if verified_hash != written_hash:
        raise PreparationError(f"{destination} failed its SHA-256 verification")
    return verified_hash


def _write_atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.part-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_directory(stage: Path, destination: Path, replace: bool) -> None:
    if destination.is_symlink():
        raise PreparationError(f"refusing to replace output symlink {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise PreparationError(f"output path {destination} is not a directory")
        if not replace:
            raise PreparationError(
                f"output directory {destination} exists; use --replace to replace it"
            )
        backup = destination.with_name(
            f".{destination.name}.previous-{uuid.uuid4().hex}"
        )
        os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except BaseException:
            os.replace(backup, destination)
            raise
        shutil.rmtree(backup)
    else:
        os.replace(stage, destination)


def _validate_inputs(
    input_paths: Iterable[Path], output_dir: Path, target_frames: int
) -> list[PcmWav]:
    paths = [path.resolve() for path in input_paths]
    if len(paths) != TRACK_COUNT:
        raise PreparationError(f"give exactly {TRACK_COUNT} input WAVE files")
    if len(set(paths)) != TRACK_COUNT:
        raise PreparationError("each input WAVE path must be different")

    output_resolved = output_dir.resolve()
    for path in paths:
        if path == output_resolved or output_resolved in path.parents:
            raise PreparationError(f"input {path} cannot be inside the output directory")

    sources = [inspect_pcm_wav(path) for path in paths]
    project_frames = max(source.frames for source in sources)
    if project_frames > target_frames:
        raise PreparationError(
            f"the longest source has {project_frames} frames; "
            f"the fixture has only {target_frames} frames"
        )
    return sources


def prepare_fixtures(
    input_paths: Iterable[Path],
    output_dir: Path,
    *,
    target_frames: int = TARGET_FRAMES,
    replace: bool = False,
) -> dict:
    """Prepare and atomically publish one synchronized eight-track fixture set."""

    if target_frames <= 0:
        raise PreparationError("target frame count must be positive")
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    sources = _validate_inputs(input_paths, output_dir, target_frames)
    if output_dir.exists() and not replace:
        raise PreparationError(
            f"output directory {output_dir} exists; use --replace to replace it"
        )

    project_frames = max(source.frames for source in sources)
    target_bytes = target_frames * BYTES_PER_FRAME
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.prepare-",
            dir=output_dir.parent,
        )
    )
    published = False
    try:
        tracks = []
        manifest_lines = []
        for index, source in enumerate(sources):
            output_name = f"source-track-{index:02d}.s24le"
            output_path = stage / output_name
            output_hash = _write_track(
                source,
                project_frames,
                target_frames,
                output_path,
            )
            manifest_lines.append(f"{output_hash}  {output_name}\n")
            tracks.append(
                {
                    "index": index,
                    "source_name": source.path.name,
                    "source_wav_bytes": source.file_bytes,
                    "source_wav_sha256": _hash_file(source.path),
                    "source_frames": source.frames,
                    "source_pcm_bytes": source.data_bytes,
                    "silence_frames_per_cycle": project_frames - source.frames,
                    "output_path": output_name,
                    "output_bytes": target_bytes,
                    "output_sha256": output_hash,
                }
            )

        metadata = {
            "schema": "needletail.prepared-pcm-fixtures.v1",
            "track_count": TRACK_COUNT,
            "sample_format": "s24le",
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "channels": CHANNELS,
            "bits_per_sample": BITS_PER_SAMPLE,
            "bytes_per_frame": BYTES_PER_FRAME,
            "duration_seconds": target_frames / SAMPLE_RATE_HZ,
            "target_frames": target_frames,
            "target_bytes_per_track": target_bytes,
            "project_cycle_frames": project_frames,
            "complete_project_cycles": target_frames // project_frames,
            "final_partial_cycle_frames": target_frames % project_frames,
            "manifest_path": MANIFEST_NAME,
            "tracks": tracks,
        }
        _write_atomic_text(stage / MANIFEST_NAME, "".join(manifest_lines))
        _write_atomic_text(
            stage / METADATA_NAME,
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        )

        expected_names = {
            *(f"source-track-{index:02d}.s24le" for index in range(TRACK_COUNT)),
            MANIFEST_NAME,
            METADATA_NAME,
        }
        actual_names = {path.name for path in stage.iterdir()}
        if actual_names != expected_names:
            raise PreparationError("the prepared fixture directory has unexpected files")
        for track in tracks:
            path = stage / track["output_path"]
            if path.stat().st_size != target_bytes:
                raise PreparationError(f"{path} failed exact size validation")
            if _hash_file(path) != track["output_sha256"]:
                raise PreparationError(f"{path} failed final SHA-256 validation")

        _publish_directory(stage, output_dir, replace)
        published = True
        return metadata
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare eight synchronized 600-second stereo 48 kHz S24LE files "
            "from RIFF WAVE PCM sources without conversion."
        )
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing output directory after preparation succeeds",
    )
    parser.add_argument("output_directory", type=Path)
    parser.add_argument(
        "input_wav",
        nargs=TRACK_COUNT,
        type=Path,
        metavar="INPUT_WAV",
    )
    return parser


def main() -> int:
    parser = _argument_parser()
    arguments = parser.parse_args()
    try:
        metadata = prepare_fixtures(
            arguments.input_wav,
            arguments.output_directory,
            replace=arguments.replace,
        )
    except (OSError, PreparationError) as error:
        parser.exit(1, f"error: {error}\n")
    print(
        json.dumps(
            {
                "output_directory": str(arguments.output_directory.resolve()),
                "track_count": metadata["track_count"],
                "target_frames": metadata["target_frames"],
                "target_bytes_per_track": metadata["target_bytes_per_track"],
                "project_cycle_frames": metadata["project_cycle_frames"],
                "manifest": MANIFEST_NAME,
                "metadata": METADATA_NAME,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
