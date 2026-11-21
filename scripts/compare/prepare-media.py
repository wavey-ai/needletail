#!/usr/bin/env python3
"""Make the timestamped source clips of the network comparison.

Every system in the comparison receives the same bytes: one H.264 elementary
stream for each bit rate, one Opus file, one AAC file and one PCM file.

The clip is a clock. It is 64 seconds long and the senders play it in a loop
that is locked to the wall clock: frame `i` is sent when the Unix time is
`i / fps` past a multiple of 64 seconds. A viewer that knows which frame it
shows therefore knows when that frame left the source, with no side channel.
64 seconds is a whole number of video frames (24, 25, 30, 50 and 60 fps), of
20 ms Opus packets and of 1,024-sample AAC frames at 48 kHz, so every loop is
seamless for every codec.

The frame number is burnt into the top 192 lines of every frame as two rows
of 40 cells of 96 pixels (see CODE below). A cell is white or black, and the
second row is the inverse of the first, so a reader compares two cells and
needs no threshold. The cells survive a transcode to 360p.

The audio carries a marker: a 10 ms burst of 3 kHz at every whole second of
the loop, and a 30 ms burst at second 0.

Run on a host with ffmpeg (libx264, libopus) and numpy. Output, for each
rung NAME:

  OUT/NAME/video.h264   Annex B, access unit delimiters, no B-frames
  OUT/NAME/video-pli.h264  the same frames with a key frame every 8 frames:
                        a WebRTC publisher sends from it after a picture loss
                        indication, as a live encoder sends a key frame on
                        request, and returns to video.h264 at its next key frame
  OUT/audio.opus        Ogg Opus, 20 ms packets
  OUT/audio.aac         ADTS AAC-LC
  OUT/audio.s24le       48 kHz 24-bit stereo PCM (the lossless lane)
  OUT/reference.mkv     the composed frames, near-lossless, for VMAF/PSNR
  OUT/media.json        what was made, and how
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import wave

import numpy as np

LOOP_SECONDS = 64
CELLS = 40
CELL_PX = 96            # at 2160 lines
SYNC = [1, 0, 1, 0]
INDEX_BITS = 24
CHECK_BITS = 12
PLI_GOP = 8            # key frame interval of the recovery encode

# name: (width, height, nominal kbit/s, encoder target kbit/s)
#
# The encoder runs at a constant bit rate with a buffer of an eighth of a
# second, and x264 then fills 15% to 37% of the stream with filler data.
# Needletail's packager drops filler, and so do WebRTC packetizers, so every
# sender of the comparison drops it. The encoder target is raised so that the
# video that remains is close to the nominal rate. media.json records the
# rate without filler: that is the rate every system carries.
RUNGS = {
    "1080p6": (1920, 1080, 6000, 7560),
    "2160p8": (3840, 2160, 8000, 12600),
    "2160p16": (3840, 2160, 16000, 19700),
    "2160p25": (3840, 2160, 25000, 30400),
    "2160p40": (3840, 2160, 40000, 48100),
    "2160p80": (3840, 2160, 80000, 94700),
    # Above 80 Mbit/s: the raw throughput rungs. They have no recovery encode
    # (RECOVERY_UP_TO): no browser decodes them, they exist to find the ceiling.
    "2160p120": (3840, 2160, 120000, 142000),
    "2160p160": (3840, 2160, 160000, 189000),
    "2160p240": (3840, 2160, 240000, 284000),
    "2160p320": (3840, 2160, 320000, 378000),
}
RECOVERY_UP_TO = 80000


def video_kbps(path: pathlib.Path) -> tuple[float, float, int]:
    """Bit rate with and without filler data (NAL type 12), and the largest frame."""
    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    zero = data == 0
    starts = np.flatnonzero(zero[:-3] & zero[1:-2] & (data[2:-1] == 1)) + 3
    kinds = data[starts] & 0x1f
    ends = np.append(starts[1:] - 3, len(data))
    sizes = ends - starts
    filler = int(sizes[kinds == 12].sum())
    aud = np.flatnonzero(kinds == 9)
    frame_sizes = np.add.reduceat(np.where(kinds == 12, 0, sizes), aud) if len(aud) else sizes
    total = len(data)
    return (round(total * 8 / LOOP_SECONDS / 1000, 1),
            round((total - filler) * 8 / LOOP_SECONDS / 1000, 1), int(frame_sizes.max()))


def code_row(index: int) -> list[int]:
    """Top row of the code of one frame: sync, 24-bit index, inverse of its low 12 bits."""
    bits = list(SYNC)
    bits += [(index >> shift) & 1 for shift in range(INDEX_BITS - 1, -1, -1)]
    check = (~index) & ((1 << CHECK_BITS) - 1)
    bits += [(check >> shift) & 1 for shift in range(CHECK_BITS - 1, -1, -1)]
    assert len(bits) == CELLS
    return bits


def write_code(path: pathlib.Path, frames: int) -> None:
    out = np.zeros((frames, 2, CELLS), dtype=np.uint8)
    for index in range(frames):
        row = np.array(code_row(index), dtype=np.uint8)
        out[index, 0] = row * 219 + 16
        out[index, 1] = (1 - row) * 219 + 16
    path.write_bytes(out.tobytes())


def run(*command: str) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def make_audio(source: pathlib.Path, out: pathlib.Path) -> dict:
    with wave.open(str(source), "rb") as handle:
        assert handle.getframerate() == 48000 and handle.getnchannels() == 2
        assert handle.getsampwidth() == 3
        raw = handle.readframes(LOOP_SECONDS * 48000)
    data = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
    samples = (data[:, 0].astype(np.int32) | (data[:, 1].astype(np.int32) << 8)
               | (data[:, 2].astype(np.int8).astype(np.int32) << 16)).reshape(-1, 2)
    assert samples.shape[0] == LOOP_SECONDS * 48000, "the music cut is shorter than the loop"
    samples = samples.astype(np.float64) * 0.7     # headroom for the marker
    # A short fade at each end, so that the loop point does not click.
    fade = np.linspace(0.0, 1.0, 480)
    samples[:480] *= fade[:, None]
    samples[-480:] *= fade[::-1, None]
    amplitude = 0.25 * (1 << 23)
    for second in range(LOOP_SECONDS):
        length = 1440 if second == 0 else 480
        t = np.arange(length) / 48000.0
        burst = amplitude * np.sin(2 * np.pi * 3000.0 * t)
        start = second * 48000
        samples[start:start + length] += burst[:, None]
    mixed = np.clip(np.round(samples), -(1 << 23), (1 << 23) - 1).astype(np.int32)
    packed = np.zeros((mixed.size, 3), dtype=np.uint8)
    flat = mixed.reshape(-1)
    packed[:, 0] = flat & 0xFF
    packed[:, 1] = (flat >> 8) & 0xFF
    packed[:, 2] = (flat >> 16) & 0xFF
    pcm = out / "audio.s24le"
    pcm.write_bytes(packed.tobytes())
    raw_in = ["-f", "s24le", "-ar", "48000", "-ac", "2", "-i", str(pcm)]
    run("ffmpeg", "-v", "error", "-y", *raw_in, "-c:a", "libopus", "-b:a", "256k",
        "-vbr", "off", "-frame_duration", "20", "-application", "audio",
        str(out / "audio.opus"))
    run("ffmpeg", "-v", "error", "-y", *raw_in, "-c:a", "aac", "-b:a", "256k",
        "-f", "adts", str(out / "audio.aac"))
    return {
        "source": source.name, "seconds": LOOP_SECONDS,
        "pcm": {"file": "audio.s24le", "sha256": hashlib.sha256(pcm.read_bytes()).hexdigest(),
                "format": "48 kHz, 24-bit, stereo"},
        "opus": {"file": "audio.opus", "bit_rate": 256000, "packet_ms": 20},
        "aac": {"file": "audio.aac", "bit_rate": 256000},
        "marker": "3 kHz burst, 10 ms at every second of the loop, 30 ms at second 0, "
                  "at -12 dBFS; music at -3 dB",
    }


def compose_filter(width: int, height: int, fps: int) -> str:
    picture = round(width * 1714 / 3840 / 2) * 2
    top = (height - picture) // 2
    strip = CELL_PX * 2 * height // 2160
    return (f"[0:v]fps={fps},scale={width}:{picture}:flags=lanczos,"
            f"pad={width}:{height}:0:{top}:black[v];"
            f"[1:v]scale={width}:{strip}:flags=neighbor[c];"
            f"[v][c]overlay=0:0,format=yuv420p[o]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--video", required=True, help="4K source of at least 64 seconds")
    parser.add_argument("--audio", required=True, help="48 kHz 24-bit stereo WAV of 64 seconds")
    parser.add_argument("--out", required=True)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--gop-seconds", type=int, default=2)
    parser.add_argument("--preset", default="fast")
    parser.add_argument("--rungs", default=",".join(RUNGS))
    parser.add_argument("--threads", default="0")
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frames = LOOP_SECONDS * args.fps
    code = out / "code.gray"
    write_code(code, frames)
    audio_done = out / "audio.json"
    if not audio_done.is_file():
        audio_done.write_text(json.dumps(make_audio(pathlib.Path(args.audio), out)) + "\n")
    media = {
        "schema": "needletail.compare-media.v1",
        "loop_seconds": LOOP_SECONDS, "fps": args.fps, "frames": frames,
        "gop_frames": args.gop_seconds * args.fps,
        "video_source": pathlib.Path(args.video).name,
        "code": {"cells": CELLS, "rows": 2, "cell_px_at_2160": CELL_PX,
                 "layout": "sync 1010, 24-bit frame number, inverse of its low 12 bits; "
                           "row 2 is the inverse of row 1"},
        "audio": json.loads(audio_done.read_text()),
        "rungs": {},
    }
    inputs = ["-i", args.video, "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{CELLS}x2",
              "-r", str(args.fps), "-i", str(code)]
    reference = out / "reference.mkv"
    if not reference.is_file():
        run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
            compose_filter(3840, 2160, args.fps), "-map", "[o]", "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-qp", "8", "-threads", args.threads,
            str(reference))
    for name in args.rungs.split(","):
        width, height, nominal, kbps = RUNGS[name]
        target = out / name
        target.mkdir(exist_ok=True)
        video = target / "video.h264"
        # A buffer of an eighth of a second keeps a key frame small enough for a
        # paced real-time sender. No B-frames: WebRTC receivers do not reorder.
        x264 = (f"keyint={media['gop_frames']}:min-keyint={media['gop_frames']}:scenecut=0:"
                f"bframes=0:nal-hrd=cbr:vbv-maxrate={kbps}:vbv-bufsize={kbps // 8}:"
                "aud=1:repeat-headers=1:sliced-threads=0:force-cfr=1")
        if not video.is_file():
            run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                compose_filter(width, height, args.fps), "-map", "[o]",
                "-frames:v", str(frames), "-c:v", "libx264", "-preset", args.preset,
                "-profile:v", "high", "-b:v", f"{kbps}k", "-x264-params", x264,
                "-threads", args.threads, "-f", "h264", str(video))
        recovery = target / "video-pli.h264"
        if not recovery.is_file() and nominal <= RECOVERY_UP_TO:
            run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                compose_filter(width, height, args.fps), "-map", "[o]",
                "-frames:v", str(frames), "-c:v", "libx264", "-preset", args.preset,
                "-profile:v", "high", "-b:v", f"{kbps}k", "-x264-params",
                x264.replace(f"keyint={media['gop_frames']}:min-keyint={media['gop_frames']}",
                             f"keyint={PLI_GOP}:min-keyint={PLI_GOP}"),
                "-threads", args.threads, "-f", "h264", str(recovery))
        size = video.stat().st_size
        media["rungs"][name] = {
            "width": width, "height": height, "nominal_kbps": nominal, "encoder_kbps": kbps,
            "bytes": size, "kbps_with_filler": video_kbps(video)[0],
            "kbps": video_kbps(video)[1], "largest_frame_bytes": video_kbps(video)[2],
            "recovery_kbps": video_kbps(recovery)[1] if recovery.is_file() else None,
            "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
            "x264": f"preset {args.preset}, profile high, {x264}",
            "recovery_gop_frames": PLI_GOP if recovery.is_file() else None,
            "recovery_sha256": hashlib.sha256(recovery.read_bytes()).hexdigest()
            if recovery.is_file() else None,
        }
        (out / "media.json").write_text(json.dumps(media, indent=2) + "\n")
        print(f"{name}: {media['rungs'][name]['kbps']} kbit/s of video, "
              f"{media['rungs'][name]['kbps_with_filler']} with filler", flush=True)
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout
    media["ffmpeg"] = version.splitlines()[0]
    (out / "media.json").write_text(json.dumps(media, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
