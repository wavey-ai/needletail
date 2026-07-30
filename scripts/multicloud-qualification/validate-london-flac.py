#!/usr/bin/env python3
"""Capture and verify one FLAC rendition from the London edge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import urljoin


SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_SAMPLE = 3
BYTES_PER_FRAME = CHANNELS * BYTES_PER_SAMPLE
NETWORK_EPOCH_FRAMES = 240
MAX_ALIGNMENT_FRAMES = NETWORK_EPOCH_FRAMES
DEFAULT_PLAYER_BASE = "https://needletail-london-20260727.bitneedle.com:19444"
MAP_PATTERN = re.compile(r'^#EXT-X-MAP:URI="([^"]+)"')
SEGMENT_NUMBER_PATTERN = re.compile(r"seg([0-9]+)\.mp4(?:$|[?#])")


class ValidationError(RuntimeError):
    """A qualification condition did not pass."""


@dataclass(frozen=True)
class PlaylistSegment:
    uri: str
    url: str
    duration_seconds: float
    number: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture fresh London LL-HLS FLAC segments and compare their decoded "
            "S24LE samples with a DAW_TEST_SOURCE_PCM_TAP_DIR source tap."
        )
    )
    parser.add_argument(
        "--source-pcm",
        type=Path,
        required=True,
        help="Source tap, such as $DAW_TEST_SOURCE_PCM_TAP_DIR/source-track-00.s24le.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stream-id", type=int, default=1)
    parser.add_argument("--player-base", default=DEFAULT_PLAYER_BASE)
    parser.add_argument(
        "--capture-seconds",
        type=float,
        default=10.0,
        help="Minimum complete-segment duration to capture.",
    )
    parser.add_argument(
        "--capture-timeout-seconds",
        type=float,
        help="Maximum wait for complete segments. The default is capture-seconds plus 30.",
    )
    parser.add_argument("--poll-interval-ms", type=int, default=250)
    parser.add_argument("--expected-segment-ms", type=int, default=1_000)
    parser.add_argument(
        "--max-alignment-frames",
        type=int,
        default=MAX_ALIGNMENT_FRAMES,
        help="Maximum exact source alignment search. Values above 240 are rejected.",
    )
    parser.add_argument(
        "--include-current-window",
        action="store_true",
        help="Validate retained segments instead of waiting for the next complete segment.",
    )
    return parser.parse_args()


def require_arguments(args: argparse.Namespace) -> None:
    if args.stream_id <= 0:
        raise ValidationError("--stream-id must be positive")
    if args.capture_seconds <= 0:
        raise ValidationError("--capture-seconds must be positive")
    if args.poll_interval_ms < 50:
        raise ValidationError("--poll-interval-ms must be at least 50")
    if args.expected_segment_ms <= 0:
        raise ValidationError("--expected-segment-ms must be positive")
    if not 0 <= args.max_alignment_frames <= MAX_ALIGNMENT_FRAMES:
        raise ValidationError("--max-alignment-frames must be between 0 and 240")
    if not args.source_pcm.is_file():
        raise ValidationError(f"source PCM tap does not exist: {args.source_pcm}")
    if args.source_pcm.stat().st_size % BYTES_PER_FRAME:
        raise ValidationError(
            "source PCM tap contains an incomplete stereo S24LE frame"
        )


def command_output(command: list[str], *, binary: bool = False) -> bytes | str:
    completed = subprocess.run(
        command, capture_output=True, check=False, text=not binary,
    )
    if completed.returncode != 0:
        stderr = (
            completed.stderr.decode(errors="replace") if binary else completed.stderr
        )
        raise ValidationError(
            f"{Path(command[0]).name} failed with exit code "
            f"{completed.returncode}: {stderr.strip()}"
        )
    return completed.stdout


def fetch(url: str) -> bytes:
    output = command_output(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "3",
            "--max-time",
            "5",
            "--header",
            "Cache-Control: no-cache",
            url,
        ],
        binary=True,
    )
    assert isinstance(output, bytes)
    return output


def parse_playlist(body: str, playlist_url: str) -> tuple[str, list[PlaylistSegment]]:
    map_uri = None
    segments: list[PlaylistSegment] = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        map_match = MAP_PATTERN.match(line)
        if map_match:
            map_uri = map_match.group(1)
        if line.startswith("#EXTINF:"):
            value = line.removeprefix("#EXTINF:").split(",", 1)[0]
            try:
                duration_seconds = float(value)
            except ValueError as error:
                raise ValidationError(
                    f"playlist has an invalid EXTINF value: {value}"
                ) from error
            media_index = index + 1
            while media_index < len(lines) and (
                not lines[media_index].strip()
                or lines[media_index].lstrip().startswith("#")
            ):
                media_index += 1
            if media_index >= len(lines):
                raise ValidationError("playlist EXTINF has no media URI")
            uri = lines[media_index].strip()
            number_match = SEGMENT_NUMBER_PATTERN.search(uri)
            segments.append(
                PlaylistSegment(
                    uri=uri,
                    url=urljoin(playlist_url, uri),
                    duration_seconds=duration_seconds,
                    number=int(number_match.group(1)) if number_match else None,
                )
            )
            index = media_index
        index += 1
    if map_uri is None:
        raise ValidationError("playlist has no EXT-X-MAP initialization segment")
    return urljoin(playlist_url, map_uri), segments


def write_bytes(path: Path, body: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(body)
    os.replace(temporary, path)


def capture_segments(
    args: argparse.Namespace, output_dir: Path, playlist_url: str,
) -> tuple[Path, list[tuple[PlaylistSegment, Path]]]:
    baseline_body = fetch(playlist_url)
    baseline_text = baseline_body.decode("utf-8")
    _, baseline_segments = parse_playlist(baseline_text, playlist_url)
    write_bytes(output_dir / "playlist-baseline.m3u8", baseline_body)
    baseline_uris = (
        set()
        if args.include_current_window
        else {segment.uri for segment in baseline_segments}
    )

    timeout_seconds = args.capture_timeout_seconds
    if timeout_seconds is None:
        timeout_seconds = args.capture_seconds + 30.0
    if timeout_seconds <= 0:
        raise ValidationError("--capture-timeout-seconds must be positive")
    deadline = time.monotonic() + timeout_seconds
    captured: list[tuple[PlaylistSegment, Path]] = []
    captured_uris: set[str] = set()
    captured_duration = 0.0
    init_url = None
    init_path = output_dir / "init.mp4"
    latest_playlist = baseline_body

    while time.monotonic() < deadline and captured_duration < args.capture_seconds:
        playlist_body = fetch(playlist_url)
        playlist_text = playlist_body.decode("utf-8")
        current_init_url, segments = parse_playlist(playlist_text, playlist_url)
        latest_playlist = playlist_body
        for segment in segments:
            if segment.uri in baseline_uris or segment.uri in captured_uris:
                continue
            if init_url is None:
                init_url = current_init_url
                write_bytes(init_path, fetch(init_url))
            elif current_init_url != init_url:
                raise ValidationError(
                    "FLAC initialization URI changed during the capture"
                )
            segment_path = output_dir / f"segment-{len(captured):06d}.mp4"
            write_bytes(segment_path, fetch(segment.url))
            captured.append((segment, segment_path))
            captured_uris.add(segment.uri)
            captured_duration += segment.duration_seconds
            if captured_duration >= args.capture_seconds:
                break
        if captured_duration < args.capture_seconds:
            time.sleep(args.poll_interval_ms / 1_000.0)

    write_bytes(output_dir / "playlist-final.m3u8", latest_playlist)
    if not captured:
        raise ValidationError("the London playlist published no complete fresh segment")
    if captured_duration < args.capture_seconds:
        raise ValidationError(
            f"captured {captured_duration:.3f}s, below the requested "
            f"{args.capture_seconds:.3f}s"
        )
    if not init_path.is_file():
        raise ValidationError(
            "the London edge did not return an initialization segment"
        )
    return init_path, captured


def assemble_capture(
    output_dir: Path, init_path: Path, segments: list[tuple[PlaylistSegment, Path]],
) -> Path:
    numbered = [segment.number for segment, _ in segments]
    if all(number is not None for number in numbered):
        actual = [int(number) for number in numbered if number is not None]
        expected = list(range(actual[0], actual[0] + len(actual)))
        if actual != expected:
            raise ValidationError(
                f"captured segment numbers are not contiguous: {actual}"
            )

    capture_path = output_dir / "london-flac.mp4"
    with capture_path.open("wb") as output:
        output.write(init_path.read_bytes())
        for _, segment_path in segments:
            output.write(segment_path.read_bytes())
    return capture_path


def probe_capture(capture_path: Path) -> tuple[dict, list[dict]]:
    output = command_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-err_detect",
            "explode",
            "-select_streams",
            "a:0",
            "-show_entries",
            (
                "stream=codec_name,codec_type,sample_rate,channels,"
                "bits_per_raw_sample,time_base:packet=pts,dts,duration,size"
            ),
            "-of",
            "json",
            str(capture_path),
        ]
    )
    assert isinstance(output, str)
    try:
        probe = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValidationError("ffprobe did not return valid JSON") from error
    streams = probe.get("streams", [])
    packets = probe.get("packets", [])
    if len(streams) != 1:
        raise ValidationError(f"capture has {len(streams)} selected audio streams")
    if not packets:
        raise ValidationError("capture has no FLAC packets")
    return streams[0], packets


def exact_integer(value: Fraction, description: str) -> int:
    if value.denominator != 1:
        raise ValidationError(f"{description} is not an exact sample-frame position")
    return value.numerator


def validate_packet_timeline(stream: dict, packets: list[dict]) -> tuple[int, int]:
    if stream.get("codec_type") != "audio" or stream.get("codec_name") != "flac":
        raise ValidationError(
            f"edge codec is {stream.get('codec_name')!r}, expected FLAC audio"
        )
    if int(stream.get("sample_rate", 0)) != SAMPLE_RATE:
        raise ValidationError(
            f"edge sample rate is {stream.get('sample_rate')}, expected {SAMPLE_RATE}"
        )
    if int(stream.get("channels", 0)) != CHANNELS:
        raise ValidationError(
            f"edge channel count is {stream.get('channels')}, expected {CHANNELS}"
        )
    if int(stream.get("bits_per_raw_sample", 0)) != 24:
        raise ValidationError("edge FLAC does not declare signed 24-bit source samples")
    try:
        time_base = Fraction(stream["time_base"])
    except (KeyError, ValueError, ZeroDivisionError) as error:
        raise ValidationError("edge FLAC stream has an invalid time base") from error

    previous_pts_frames = None
    first_pts_frames = None
    packets_with_duration = 0
    for index, packet in enumerate(packets):
        try:
            pts = int(packet["pts"])
            dts = int(packet["dts"])
            size = int(packet["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError(
                f"FLAC packet {index} has incomplete timing"
            ) from error
        if pts != dts:
            raise ValidationError(f"FLAC packet {index} has different PTS and DTS")
        if size <= 0:
            raise ValidationError(f"FLAC packet {index} has no media payload")
        pts_frames = exact_integer(
            Fraction(pts) * time_base * SAMPLE_RATE, f"FLAC packet {index} PTS",
        )
        if (
            previous_pts_frames is not None
            and pts_frames - previous_pts_frames != NETWORK_EPOCH_FRAMES
        ):
            delta_frames = pts_frames - previous_pts_frames - NETWORK_EPOCH_FRAMES
            raise ValidationError(
                f"FLAC packet {index} has a PTS discontinuity of "
                f"{float(delta_frames):.3f} frames"
            )
        if packet.get("duration") is not None:
            try:
                duration = int(packet["duration"])
            except (TypeError, ValueError) as error:
                raise ValidationError(
                    f"FLAC packet {index} has an invalid duration"
                ) from error
            packet_frames = exact_integer(
                Fraction(duration) * time_base * SAMPLE_RATE,
                f"FLAC packet {index} duration",
            )
            if packet_frames != NETWORK_EPOCH_FRAMES:
                raise ValidationError(
                    f"FLAC packet {index} has {packet_frames} frames, "
                    f"expected the fixed {NETWORK_EPOCH_FRAMES}-frame epoch"
                )
            packets_with_duration += 1
        if first_pts_frames is None:
            first_pts_frames = pts_frames
        previous_pts_frames = pts_frames

    assert first_pts_frames is not None
    if packets_with_duration not in (0, len(packets)):
        raise ValidationError(
            "ffprobe returned packet duration for only part of the capture"
        )
    return first_pts_frames, len(packets) * NETWORK_EPOCH_FRAMES


def decode_capture(capture_path: Path, decoded_path: Path) -> bytes:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(capture_path),
            "-map",
            "0:a:0",
            "-c:a",
            "pcm_s24le",
            "-f",
            "s24le",
            "-y",
            str(decoded_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise ValidationError(
            f"ffmpeg FLAC decode failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    decoded = decoded_path.read_bytes()
    if not decoded or len(decoded) % BYTES_PER_FRAME:
        raise ValidationError("decoded edge audio has an invalid stereo S24LE size")
    return decoded


def slices_are_equal(source: bytes, start: int, decoded: bytes) -> bool:
    end = start + len(decoded)
    if start < 0 or end > len(source):
        return False
    probe_bytes = min(len(decoded), 16 * 1024)
    if source[start : start + probe_bytes] != decoded[:probe_bytes]:
        return False
    if source[end - probe_bytes : end] != decoded[-probe_bytes:]:
        return False
    return source[start:end] == decoded


def first_mismatch_frame(source: bytes, start: int, decoded: bytes) -> int | None:
    if start < 0 or start >= len(source):
        return 0
    comparable = min(len(decoded), len(source) - start)
    for byte_index in range(comparable):
        if source[start + byte_index] != decoded[byte_index]:
            return byte_index // BYTES_PER_FRAME
    if comparable != len(decoded):
        return comparable // BYTES_PER_FRAME
    return None


def compare_source(
    source_path: Path, decoded: bytes, predicted_frame: int, max_alignment_frames: int,
) -> tuple[int, bytes, bytes]:
    source = source_path.read_bytes()
    decoded_frames = len(decoded) // BYTES_PER_FRAME
    candidates = range(
        max(0, predicted_frame - max_alignment_frames),
        predicted_frame + max_alignment_frames + 1,
    )
    exact_matches = []
    for candidate in candidates:
        start = candidate * BYTES_PER_FRAME
        if slices_are_equal(source, start, decoded):
            exact_matches.append(candidate)
    if not exact_matches:
        expected_start = predicted_frame * BYTES_PER_FRAME
        mismatch = first_mismatch_frame(source, expected_start, decoded)
        required_frames = predicted_frame + decoded_frames
        available_frames = len(source) // BYTES_PER_FRAME
        if available_frames < required_frames:
            raise ValidationError(
                f"source tap has {available_frames} frames but the edge window "
                f"requires at least {required_frames}"
            )
        raise ValidationError(
            f"edge PCM is not bit-exact within ±{max_alignment_frames} frames; "
            f"the PTS-derived comparison first differs at frame {mismatch}"
        )
    aligned_frame = min(
        exact_matches, key=lambda frame: (abs(frame - predicted_frame), frame),
    )
    start = aligned_frame * BYTES_PER_FRAME
    reference = source[start : start + len(decoded)]
    return aligned_frame, reference, source


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def validate(args: argparse.Namespace, output_dir: Path) -> dict:
    player_base = args.player_base.rstrip("/")
    playlist_url = f"{player_base}/live/{args.stream_id}/stream.m3u8"
    init_path, segments = capture_segments(args, output_dir, playlist_url)
    expected_segment_seconds = args.expected_segment_ms / 1_000.0
    for index, (segment, _) in enumerate(segments):
        is_startup_partial = (
            index == 0
            and segment.number == 0
            and 0 < segment.duration_seconds < expected_segment_seconds
        )
        duration_frames = round(segment.duration_seconds * SAMPLE_RATE)
        has_complete_epochs = duration_frames % NETWORK_EPOCH_FRAMES == 0
        if (
            abs(segment.duration_seconds - expected_segment_seconds) > 0.000_5
            and not (is_startup_partial and has_complete_epochs)
        ):
            raise ValidationError(
                f"{segment.uri} contains {segment.duration_seconds:.3f}s, "
                f"expected {expected_segment_seconds:.3f}s"
            )

    capture_path = assemble_capture(output_dir, init_path, segments)
    stream, packets = probe_capture(capture_path)
    predicted_frame, packet_frames = validate_packet_timeline(stream, packets)
    expected_frames = round(
        sum(segment.duration_seconds for segment, _ in segments) * SAMPLE_RATE
    )
    if packet_frames != expected_frames:
        raise ValidationError(
            f"FLAC packets contain {packet_frames} frames but the playlist "
            f"declares {expected_frames}"
        )
    decoded_path = output_dir / "london-decoded.s24le"
    decoded = decode_capture(capture_path, decoded_path)
    decoded_frames = len(decoded) // BYTES_PER_FRAME
    if decoded_frames != packet_frames:
        raise ValidationError(
            f"ffmpeg decoded {decoded_frames} frames but FLAC packets declare "
            f"{packet_frames}"
        )

    aligned_frame, reference, source = compare_source(
        args.source_pcm, decoded, predicted_frame, args.max_alignment_frames,
    )
    reference_path = output_dir / "source-reference.s24le"
    write_bytes(reference_path, reference)
    if decoded != reference:
        raise ValidationError("internal error: aligned source comparison changed")

    return {
        "schema": "needletail.london-flac-validation.v1",
        "passed": True,
        "playlist_url": playlist_url,
        "stream_id": args.stream_id,
        "source_pcm": str(args.source_pcm.resolve()),
        "source_snapshot_bytes": len(source),
        "source_snapshot_sha256": sha256(source),
        "captured_segments": [
            {
                "uri": segment.uri,
                "number": segment.number,
                "duration_ms": round(segment.duration_seconds * 1_000, 3),
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
            }
            for segment, path in segments
        ],
        "capture_bytes": capture_path.stat().st_size,
        "capture_sha256": sha256(capture_path.read_bytes()),
        "codec": stream["codec_name"],
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "bits_per_sample": 24,
        "network_epoch_frames": NETWORK_EPOCH_FRAMES,
        "flac_packets": len(packets),
        "decoded_frames": decoded_frames,
        "decoded_bytes": len(decoded),
        "first_pts_frame": predicted_frame,
        "aligned_source_frame": aligned_frame,
        "alignment_delta_frames": aligned_frame - predicted_frame,
        "max_alignment_frames": args.max_alignment_frames,
        "decoded_sha256": sha256(decoded),
        "reference_sha256": sha256(reference),
        "exact_pcm_match": True,
        "packet_pts_discontinuities": 0,
        "inserted_or_dropped_frames": 0,
        "interior_sample_mismatches": 0,
    }


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    report: dict = {
        "schema": "needletail.london-flac-validation.v1",
        "passed": False,
    }
    created_output = False
    try:
        require_arguments(args)
        output_dir.mkdir(parents=True, exist_ok=False)
        created_output = True
        report = validate(args, output_dir)
        write_bytes(
            output_dir / "validation.json",
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode(),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        report["error"] = str(error)
        if created_output:
            write_bytes(
                output_dir / "validation.json",
                (json.dumps(report, indent=2, sort_keys=True) + "\n").encode(),
            )
        print(f"London FLAC validation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
