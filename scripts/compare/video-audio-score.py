#!/usr/bin/env python3
"""What 4K and the 1 ms audio lane cost each other, step by step (lead, 24 Nov).

For each throughput step of the given compare-run.py sessions (run with
--audio pcm, the default since 24 Nov), one row per region:
  - the 4K verdict of the step (compare-run.py's own: carried whole or not);
  - the audio listeners that ran through the step, scored by
    compare-audio-analyse.py exactly as in the audio sessions: missing % and
    mouth-to-ear p50/p99 of the LL-HLS listener (nt-pcm, the -nano host) and of
    the UDP client (nt-uc, the -view host);
  - which edges served the 4K UDP viewers and the audio UDP client (their
    reports' `caches`), so a row says whether the audio listener shared an
    edge with 4K. The LL-HLS listeners do not report their edge.

    video-audio-score.py SESSION [SESSION ...] [--out FILE.json] > FILE.md
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = load_module("compare_run", "compare-run.py")
audio = load_module("compare_audio_analyse", "compare-audio-analyse.py")


def caches(path: pathlib.Path) -> list[str]:
    try:
        return list(json.loads(path.read_text()).get("caches") or [])
    except (OSError, json.JSONDecodeError):
        return []


class Dir:
    def __init__(self, path: pathlib.Path):
        self.dir = path


def score(session: str) -> list[dict]:
    base = run.RUNS / session
    rows = []
    for step_dir in sorted(base.glob("nt.throughput.*"), key=lambda p: (p / "meta.json").stat().st_mtime
                           if (p / "meta.json").is_file() else 0):
        meta = json.loads((step_dir / "meta.json").read_text()) if (step_dir / "meta.json").is_file() else {}
        seconds = 40
        analysed = audio.analyse_step_now(step_dir) if meta.get("config") == "pcm" else None
        hosts = (analysed or {}).get("hosts") or {}
        for site in run.SITES:
            whole = {label: run.carried_by(Dir(base), step_dir.name, seconds, site, label)
                     for label in ("probe", "probe.uc")}
            video_edges = sorted({edge for host in (f"{site}-nano", f"{site}-view")
                                  for edge in caches(step_dir / host / "probe.uc.uc.json")})
            audio_edges = caches(step_dir / f"{site}-view" / "nt-uc.uc.json")
            row = {"session": session, "rung": meta.get("rung"), "site": site,
                   "audio": meta.get("audio"), "audio_start_unix": meta.get("audio_start_unix"),
                   "step_started_unix": meta.get("started_unix"),
                   "video_whole": any(whole.values()), "video_whole_by_mode": whole,
                   "video_udp_edges": video_edges, "audio_udp_edges": audio_edges,
                   "audio_udp_on_a_4k_edge": bool(set(video_edges) & set(audio_edges)) if audio_edges else None}
            for host, lane in ((f"{site}-nano", "nt-pcm"), (f"{site}-view", "nt-uc")):
                probe = (((hosts.get(host) or {}).get("lanes") or {}).get(lane) or {}).get("probe")
                if probe:
                    m2e = probe.get("mouth_to_ear_ms") or {}
                    row[lane] = {"missing_percent": probe.get("missing_percent"),
                                 "longest_gap_ms": probe.get("longest_gap_ms"),
                                 "mouth_to_ear_p50_ms": m2e.get("p50"), "mouth_to_ear_p99_ms": m2e.get("p99"),
                                 "error": probe.get("error")}
            rows.append(row)
    return rows


def cell(probe: dict | None) -> str:
    if not probe:
        return "–"
    if probe.get("error"):
        return "error"
    return (f"{probe.get('missing_percent')}% missing, "
            f"{probe.get('mouth_to_ear_p50_ms')} / {probe.get('mouth_to_ear_p99_ms')} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sessions", nargs="+")
    parser.add_argument("--out")
    args = parser.parse_args()
    rows = [row for session in args.sessions for row in score(session)]
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps({"schema": "video-audio-score.v1", "rows": rows},
                                                     indent=1) + "\n")
    print("| Session | Rung | Region | Audio | 4K whole | 4K UDP edges | Audio UDP edge (on a 4K edge?) | "
          "Audio LL-HLS (nt-pcm): missing, mouth to ear p50 / p99 | Audio UDP (nt-uc): missing, p50 / p99 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        shared = row["audio_udp_on_a_4k_edge"]
        print(f"| {row['session']} | {row['rung']} | {row['site']} | {row['audio']} | "
              f"{'yes' if row['video_whole'] else 'no'} | {', '.join(row['video_udp_edges']) or '–'} | "
              f"{', '.join(row['audio_udp_edges']) or '–'}"
              f"{'' if shared is None else (' (yes)' if shared else ' (no)')} | "
              f"{cell(row.get('nt-pcm'))} | {cell(row.get('nt-uc'))} |")


if __name__ == "__main__":
    sys.exit(main())
