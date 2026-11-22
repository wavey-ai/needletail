#!/usr/bin/env python3
"""Decode the nt-opus parts a listener saved and time the 3 kHz markers in them.

  validate-opus.py [--units opus-units] [--probe aep1-48k-probe] [--decoder auto|probe|ffmpeg]

The parts are opaque LL-HLS parts of the private SoundKit Opus rendition
(stream 3001): SoundKit v2 packets, one Opus frame each. units.csv in the
units directory names each part's file and URI (partN).

Decoders:

probe   aep1-48k-probe decode-soundkit-opus, the consumer of receive-hls: it
        chooses Opus Custom (48, 64, 72-sample frames, no signalling byte) or
        standard Opus by each packet's frame count. Opus Custom cannot be
        put in Ogg (OpusHead cannot describe it), so its streams need this.
ffmpeg  the old way, standard Opus only: the raw packets in Ogg/OpusHead,
        decoded by ffmpeg.
auto    the probe when it is found (--probe or NEEDLETAIL_PROBE_BIN, else
        aep1-48k-probe on PATH), else ffmpeg.

Writes nt-opus.pcm (S16LE stereo) and prints each marker onset's offset
from a whole second of the content.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys

import numpy as np


def ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04c11db7) & 0xffffffff if crc & 0x80000000 else (crc << 1) & 0xffffffff
    return crc


def page(serial: int, seq: int, granule: int, packet: bytes, flags: int = 0) -> bytes:
    segs = [255] * (len(packet) // 255) + [len(packet) % 255]
    head = b"OggS" + bytes([0, flags]) + struct.pack("<qIII", granule, serial, seq, 0) + bytes([len(segs)]) + bytes(segs)
    crc = ogg_crc(head + packet)
    return head[:22] + struct.pack("<I", crc) + head[26:] + packet


def load_parts(units: pathlib.Path) -> list[tuple[int, bytes]]:
    rows = list(csv.DictReader(open(units / "units.csv")))
    return sorted((int(re.search(r"part(\d+)", r["uri"]).group(1)), (units / r["file"]).read_bytes())
                  for r in rows)


def find_probe(given: str | None) -> str | None:
    for candidate in (given, os.environ.get("NEEDLETAIL_PROBE_BIN"), shutil.which("aep1-48k-probe")):
        if candidate and pathlib.Path(candidate).is_file():
            return candidate
    return None


def decode_with_probe(probe: str, parts: list[tuple[int, bytes]], protection: str,
                      out: pathlib.Path) -> int:
    """Decode the parts in order with the probe's consumer; returns the frame
    size (samples per packet) it reports."""
    stream = out.with_suffix(".soundkit")
    # SoundKit v2 packets are self-delimiting: the parts in order, back to
    # back, are one stream for the consumer.
    stream.write_bytes(b"".join(body for _, body in parts))
    result = subprocess.run([probe, "decode-soundkit-opus", "--soundkit-opus-protection", protection,
                             "--decoded-pcm-output", str(out), str(stream)],
                            check=True, capture_output=True, text=True)
    report = json.loads(result.stdout)
    frames = report.get("opus_frame_samples") or []
    print("probe decode:", report.get("opus_mode"), "Opus, frame", frames,
          "packets", report.get("decoded_opus_packets"), "frames", report.get("decoded_pcm_frames"))
    if len(frames) != 1:
        sys.exit(f"expected one Opus frame size, the probe reports {frames}")
    return int(frames[0])


def decode_with_ffmpeg(parts: list[tuple[int, bytes]], header_bytes: int, frame: int,
                       out: pathlib.Path) -> int:
    """Standard Opus only: the raw packets in Ogg, decoded by ffmpeg."""
    ogg = page(1, 0, 0, b"OpusHead" + bytes([1, 2]) + struct.pack("<HIh", 0, 48000, 0) + bytes([0]), 2)
    ogg += page(1, 1, 0, b"OpusTags" + struct.pack("<I", 4) + b"test" + struct.pack("<I", 0))
    for i, (_number, body) in enumerate(parts):
        ogg += page(1, 2 + i, (i + 1) * frame, body[header_bytes:])
    ogg_path = out.with_suffix(".ogg")
    ogg_path.write_bytes(ogg)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(ogg_path), "-f", "s16le", "-ac", "2",
                    "-ar", "48000", str(out)], check=True)
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--units", default="opus-units", help="directory with units.csv and the parts")
    parser.add_argument("--decoder", choices=("auto", "probe", "ffmpeg"), default="auto")
    parser.add_argument("--probe", help="aep1-48k-probe (with decode-soundkit-opus)")
    parser.add_argument("--protection", choices=("plain", "encrypted"), default="plain",
                        help="packet protection of the parts (encrypted needs a key: not here)")
    parser.add_argument("--frame-samples", type=int, default=240,
                        help="ffmpeg only: samples per part (the probe reads them from the packets)")
    parser.add_argument("--header-bytes", type=int, default=20,
                        help="ffmpeg only: SoundKit v2 header bytes before each packet")
    parser.add_argument("--offset-samples", type=int, default=512,
                        help="samples the decoded content runs behind the loop (the DAW buffer)")
    parser.add_argument("--out", default="nt-opus.pcm")
    args = parser.parse_args()

    parts = load_parts(pathlib.Path(args.units))
    print("parts", len(parts), "numbers", parts[0][0], "..", parts[-1][0], "size", len(parts[0][1]),
          "first bytes", parts[0][1][:24].hex())
    out = pathlib.Path(args.out)
    probe = find_probe(args.probe) if args.decoder in ("auto", "probe") else None
    if args.decoder == "probe" and probe is None:
        sys.exit("no aep1-48k-probe: give --probe or NEEDLETAIL_PROBE_BIN")
    if probe is not None:
        frame = decode_with_probe(probe, parts, args.protection, out)
    else:
        print("ffmpeg decode (standard Opus only; Opus Custom parts need --decoder probe)")
        frame = decode_with_ffmpeg(parts, args.header_bytes, args.frame_samples, out)

    pcm = np.fromfile(out, dtype=np.int16).reshape(-1, 2)[:, 0].astype(np.float64)
    print("decoded samples", len(pcm), "expected", len(parts) * frame)
    t = np.arange(len(pcm)) / 48000.0
    win = 48
    power = np.abs(np.convolve(pcm * np.exp(-2j * np.pi * 3000 * t), np.ones(win), "valid")) / win
    total = np.sqrt(np.convolve(pcm * pcm, np.ones(win), "valid") / win) + 1
    ratio = power / total
    i = 0
    first = parts[0][0]
    while i < len(ratio):
        if ratio[i] > 0.45 and power[i] > 400:
            content = first * frame + i - args.offset_samples
            offset = (content + 24000) % 48000 - 24000
            print("marker onset near decoded sample", i, "-> content sample", content,
                  "; offset from a whole second:", offset, "samples =", round(offset / 48, 2), "ms")
            i += 24000
        else:
            i += 1


if __name__ == "__main__":
    main()
