#!/usr/bin/env python3
"""PSNR and SSIM of every rung of the clip against the composed 4K reference.

Every system in the comparison delivers the frames of a rung bit for bit (the
probes check a fingerprint of every frame), so the quality at a viewer is the
quality of the rung it received. A viewer that received frames of the recovery
encode has the quality of `video-pli.h264` for those frames.

  quality.py CLIPS_DIR OUT.json

Needs ffmpeg. The 1080p rung is scaled to 2160p (bicubic) before comparison.
"""
import json
import pathlib
import re
import subprocess
import sys


def measure(path: pathlib.Path, reference: pathlib.Path, scale: bool) -> dict:
    # Both inputs are numbered from zero, so that frame n meets frame n whatever
    # the first timestamp of a file is.
    first = ("[0:v]setpts=N/(24*TB),scale=3840:2160:flags=bicubic,split[a][b]" if scale
             else "[0:v]setpts=N/(24*TB),split[a][b]")
    graph = f"{first};[1:v]setpts=N/(24*TB),split[c][d];[a][c]psnr;[b][d]ssim"
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-r", "24", "-i", str(path), "-i", str(reference),
         "-lavfi", graph, "-frames:v", "1536", "-f", "null", "-"],
        capture_output=True, text=True)
    psnr = re.search(r"PSNR y:([\d.]+) u:([\d.]+) v:([\d.]+) average:([\d.]+) min:([\d.]+)", result.stderr)
    ssim = re.search(r"SSIM Y:([\d.]+) .*All:([\d.]+) \(([\d.]+)\)", result.stderr)
    if not psnr or not ssim:
        return {"error": result.stderr[-300:]}
    return {"psnr_y_db": float(psnr.group(1)), "psnr_average_db": float(psnr.group(4)),
            "psnr_min_db": float(psnr.group(5)), "ssim_y": float(ssim.group(1)),
            "ssim_all": float(ssim.group(2)), "ssim_db": float(ssim.group(3))}


def main() -> None:
    clips = pathlib.Path(sys.argv[1])
    media = json.loads((clips / "media.json").read_text())
    out = {}
    for name, rung in sorted(media["rungs"].items(), key=lambda item: item[1]["kbps"]):
        entry = {"kbps": rung["kbps"], "width": rung["width"], "height": rung["height"]}
        entry.update(measure(clips / name / "video.h264", clips / "reference.mkv", rung["height"] != 2160))
        recovery = clips / name / "video-pli.h264"
        if recovery.is_file():
            entry["recovery"] = measure(recovery, clips / "reference.mkv", rung["height"] != 2160)
        out[name] = entry
        print(name, entry, flush=True)
        pathlib.Path(sys.argv[2]).write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
