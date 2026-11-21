#!/usr/bin/env python3
"""Turn the records of comparison sessions into one evidence file.

  compare-analyse.py --out evidence.json SESSION [SESSION ...]

For every step of every session (see compare-run.py) it reads what each
viewer recorded and computes the same quantities for every system:

probe (no decoding; `ntcmp sub` and `ntcmp hls`)
  delivery latency   time from the moment a frame is due at the source to the
                     moment all of its bytes are at the viewer
  stalls             a viewer that plays every frame a fixed 250 ms after the
                     median delivery latency: a stall is a run of frames that
                     are missing, cannot be decoded (a frame before them since
                     the last key frame is missing) or arrive after their turn
  first frame        start of the viewer to its first complete key frame

player (headless Chrome)
  glass-to-glass     presentation time of a frame minus the time it was due at
                     the source, read from the code burnt into the frame
  freezes            a gap between presented frames above
                     max(3 x frame interval, frame interval + 150 ms), as in
                     the W3C WebRTC statistics

The first five seconds after a viewer's first frame and the last three
seconds of a step are left out of latency and stall figures, so that joining
and leaving viewers are not measured as steady state.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
RUNS = ROOT / "target" / "compare" / "runs"
LOOP_MS = 64000.0
FPS = 24.0
FRAME_MS = 1000.0 / FPS
HEAD_MS = 5000.0
TAIL_MS = 3000.0
STALL_MARGIN_MS = 250.0
SITES = {"lon": "London", "new": "New York", "tok": "Tokyo", "syd": "Sydney"}


def dist(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    pick = lambda q: round(ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))], 1)
    return {"n": len(ordered), "min": round(ordered[0], 1), "p50": pick(0.5), "p95": pick(0.95),
            "p99": pick(0.99), "max": round(ordered[-1], 1)}


def load(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def probe_frames(path: pathlib.Path) -> tuple[list, list]:
    """(video, audio) rows of a probe record: (index, latency ms, complete ms, key, bytes)."""
    video, audio = [], []
    if not path.is_file():
        return video, audio
    with path.open() as handle:
        for row in csv.DictReader(handle):
            item = (int(row["index"]), int(row["latency_us"]) / 1000.0,
                    int(row["complete_unix_us"]) / 1000.0, row["key"] == "1", int(row["bytes"]))
            (video if row["kind"] == "v" else audio).append(item)
    return video, audio


def analyse_probe(summary: dict, csv_path: pathlib.Path, end_ms: float | None) -> dict:
    out = {
        "error": summary.get("error") or None,
        "connect_ms": summary.get("connect_ms"), "first_byte_ms": summary.get("first_packet_ms"),
        "first_frame_ms": summary.get("first_frame_ms"),
        "first_key_frame_ms": summary.get("first_key_frame_ms"),
        "frames": summary.get("video_frames"),
        "frames_incomplete": summary.get("video_frames_incomplete"),
        "frames_unrecognised": summary.get("video_frames_unrecognised"),
        "frames_from_recovery_encode": summary.get("video_frames_from_recovery_encode"),
        "packets": summary.get("video_packets"),
        "duplicate_packets": summary.get("video_duplicate_packets"),
        "sequence_gaps": summary.get("video_sequence_gaps"),
    }
    rates = summary.get("kbps_by_second") or []
    if len(rates) > 8:
        out["kbps"] = round(statistics.median(rates[3:-2]))
    detail = summary.get("detail") or {}
    for key in ("livekit_server", "codec_video", "codec_audio", "mode", "http", "counts",
                "variant", "part_target"):
        if key in detail:
            out[key] = detail[key]
    video, audio = probe_frames(csv_path)
    video = [row for row in video if row[0] >= 0]
    if not video:
        out["latency_ms"] = {k: v for k, v in (summary.get("video_latency_ms") or {}).items()
                             if k in ("p50", "p95", "p99", "max", "min")} or None
        if summary.get("audio_latency_ms"):
            out["audio_latency_ms"] = {k: v for k, v in summary["audio_latency_ms"].items()
                                       if k in ("p50", "p95", "p99", "max", "min")}
        return out
    first = min(row[2] for row in video)
    last = max(row[2] for row in video)
    lo = first + HEAD_MS
    hi = (end_ms if end_ms else last) - TAIL_MS
    # Source frame number of each record, from its completion time and latency.
    by_number = {}
    for index, latency, complete, key, size in video:
        number = round((complete - latency) / FRAME_MS)
        if number not in by_number or complete < by_number[number][1]:
            by_number[number] = (latency, complete, key)
    window = sorted(n for n, (latency, complete, key) in by_number.items() if lo <= complete <= hi)
    if len(window) < 24:
        return out
    latencies = [by_number[n][0] for n in window]
    median = statistics.median(latencies)
    out["latency_ms"] = dist(latencies)
    out["window_s"] = round((hi - lo) / 1000.0, 1)
    turn = median + STALL_MARGIN_MS
    missing = late = undecodable = 0
    stalls, stall_ms, run, longest = 0, 0.0, 0.0, 0.0
    broken = False
    for number in range(window[0], window[-1] + 1):
        record = by_number.get(number)
        bad_for = 0.0
        if record is None:
            missing += 1
            broken = True
            bad_for = FRAME_MS
        else:
            latency, _, key = record
            if key:
                broken = False
            if broken:
                undecodable += 1
                bad_for = FRAME_MS
            elif latency > turn:
                late += 1
                bad_for = min(latency - turn, 10000.0)
        if bad_for:
            if not run:
                stalls += 1
            run = max(run + FRAME_MS, bad_for) if record is None or broken else max(run, bad_for)
        elif run:
            stall_ms += run
            longest = max(longest, run)
            run = 0.0
    stall_ms += run
    longest = max(longest, run)
    expected = window[-1] - window[0] + 1
    out.update({
        "frames_expected": expected, "frames_missing": missing, "frames_late": late,
        "frames_undecodable": undecodable,
        "delivered_percent": round(100.0 * (expected - missing) / expected, 3),
        # A frame is on time when it arrived, can be decoded, and was not later
        # than 250 ms after the median.
        "on_time_percent": round(100.0 * (expected - missing - undecodable - late) / expected, 3),
        "longest_stall_ms": round(longest),
        "stalls": stalls, "stall_ms": round(stall_ms),
        "stalls_per_minute": round(stalls * 60000.0 / (hi - lo), 2),
        "stall_ms_per_minute": round(stall_ms * 60000.0 / (hi - lo)),
        "jitter_margin_ms": round(max(latencies) - median, 1),
    })
    audio = [row for row in audio if row[0] >= 0 and lo <= row[2] <= hi]
    if audio:
        out["audio_latency_ms"] = dist([row[1] for row in audio])
        numbers = sorted({round((row[2] - row[1]) / 20.0) for row in audio})
        out["audio_packets_expected"] = numbers[-1] - numbers[0] + 1
        out["audio_packets_missing"] = numbers[-1] - numbers[0] + 1 - len(numbers)
        out["audio_kbps"] = round(sum(row[4] for row in audio) * 8 / (hi - lo))
    return out


def modular_latency(wall: float, index: int) -> float:
    latency = (wall - index * FRAME_MS) % LOOP_MS
    return latency - LOOP_MS if latency > LOOP_MS - 2000 else latency


def analyse_player(record: dict, end_ms: float | None) -> dict:
    out = {
        "error": (record.get("error") or "")[:300] or None,
        "first_frame_ms": record.get("first_frame_ms"),
        "sizes": record.get("sizes"), "chrome_cpu_cores": record.get("chrome_cpu_cores"),
        "unreadable_frames": record.get("unreadable_frames"),
        "chrome": record.get("chrome"), "cores": record.get("cores"),
    }
    events = record.get("events") or []
    out["events"] = [e for e in events if any(word in e[1] for word in (
        "error", "failed", "waiting", "stalled", "lk-", "hls-level", "hls-playlist", "pc-disconnected",
        "pc-failed"))][:30]
    frames = [f for f in record.get("frames") or [] if f[0] >= 0]
    if len(frames) < 24:
        return out
    first = frames[0][1]
    last = frames[-1][1]
    lo = first + HEAD_MS
    hi = (end_ms if end_ms else last) - TAIL_MS
    out["first_frame_latency_ms"] = round(modular_latency(frames[0][1], frames[0][0]), 1)
    latencies, distinct, previous = [], 0, None
    freezes, freeze_ms, longest = 0, 0.0, 0.0
    threshold = max(3 * FRAME_MS, FRAME_MS + 150.0)
    for index, wall, _media, _presented in frames:
        if previous is not None and index == previous[0]:
            continue
        if lo <= wall <= hi:
            distinct += 1
            latencies.append(modular_latency(wall, index))
            if previous is not None and previous[1] >= lo:
                gap = wall - previous[1]
                if gap > threshold:
                    freezes += 1
                    freeze_ms += gap - FRAME_MS
                    longest = max(longest, gap - FRAME_MS)
        previous = (index, wall)
    in_window = [f for f in frames if lo <= f[1] <= hi]
    if len(in_window) < 2 or hi <= lo:
        return out
    presented = in_window[-1][3] - in_window[0][3] + 1
    seconds = (hi - lo) / 1000.0
    out.update({
        "window_s": round(seconds, 1),
        "glass_to_glass_ms": dist(latencies),
        "presented_fps": round(presented / seconds, 2),
        "freezes": freezes, "freeze_ms": round(freeze_ms), "longest_freeze_ms": round(longest),
        "freezes_per_minute": round(freezes * 60.0 / seconds, 2),
        "freeze_ms_per_minute": round(freeze_ms * 60.0 / seconds),
    })
    stats = record.get("stats") or []
    if stats:
        final = stats[-1]
        keep = ("packetsLost", "nackCount", "pliCount", "framesDecoded", "framesDropped", "freezeCount",
                "totalFreezesDuration", "frameWidth", "frameHeight", "decoderImplementation",
                "retransmittedPacketsReceived", "latency", "targetLatency", "buffered", "dropped",
                "decoded", "rate", "rtt")
        out["final_stats"] = {key: final[key] for key in keep if final.get(key) is not None}
        rates = [s["kbps"] for s in stats[3:-1] if s.get("kbps")]
        if rates:
            out["kbps"] = round(statistics.median(rates))
        if final.get("jitterBufferEmittedCount"):
            out["jitter_buffer_ms"] = round(
                1000.0 * final["jitterBufferDelay"] / final["jitterBufferEmittedCount"], 1)
        hls = [s["latency"] for s in stats[5:] if s.get("latency")]
        if hls:
            out["hls_js_latency_s"] = round(statistics.median(hls), 3)
    # Audio marker: a burst starts at every whole second of the clip, so the
    # audio latency is known modulo one second. The video latency picks the second.
    markers = [m for m in record.get("audio_markers") or [] if lo <= m[0] <= hi]
    if markers and latencies:
        video = statistics.median(latencies)
        values = []
        for wall, _length in markers:
            fraction = wall % 1000.0
            turns = round((video - fraction) / 1000.0)
            values.append(fraction + 1000.0 * turns)
        out["audio_glass_to_glass_ms"] = dist(values)
        out["audio_minus_video_ms"] = round(statistics.median(values) - video, 1)
    return out


def analyse_reader(record: dict) -> dict:
    summary = record.get("summary") or {}
    keep = ("readers", "readers_ok", "parts_received", "parts_missing", "underruns",
            "max_behind_ms", "moves", "rejections", "readers_by_final_cache")
    return {key: summary.get(key) for key in keep}


ANALYSIS_VERSION = 2


def analyse_step(path: pathlib.Path) -> dict | None:
    """The analysis of a step is kept beside its records and reused."""
    cache = path / "analysis.json"
    meta_time = (path / "meta.json").stat().st_mtime if (path / "meta.json").is_file() else 0
    if cache.is_file() and cache.stat().st_mtime > meta_time:
        kept = load(cache)
        if kept and kept.get("analysis_version") == ANALYSIS_VERSION:
            return kept
    step = analyse_step_now(path)
    if step is not None:
        step["analysis_version"] = ANALYSIS_VERSION
        cache.write_text(json.dumps(step) + "\n")
    return step


def analyse_step_now(path: pathlib.Path) -> dict | None:
    meta = load(path / "meta.json")
    if meta is None:
        return None
    step = {key: meta.get(key) for key in ("step", "lane", "phase", "detail", "rung", "netem",
                                            "viewers", "started_unix", "seconds", "source_failed")}
    source = meta.get("source") or {}
    step["source"] = {name.replace(".status", ""): value for name, value in source.items()
                      if name != "logs" and name.replace(".status", "").startswith(meta["lane"])}
    if meta.get("host_errors"):
        step["host_errors"] = {name: text[-200:] for name, text in meta["host_errors"].items()}
    hosts = {}
    for host_dir in sorted(p for p in path.iterdir() if p.is_dir()):
        host = {}
        ends = []
        records = {}
        for file in sorted(host_dir.glob("*.json")):
            record = load(file)
            if record is None:
                continue
            records[file.stem] = record
            if "frames" in record and "callbacks" in record and record["frames"]:
                ends.append(record["frames"][-1][1])
            elif "started_unix_ms" in record and "duration_seconds" in record:
                ends.append(record["started_unix_ms"] + record["duration_seconds"] * 1000.0)
        end_ms = min(ends) if ends else None
        for name, record in records.items():
            if "callbacks" in record:
                host.setdefault("players", {})[name] = analyse_player(record, end_ms)
            elif "video_frames" in record:
                host.setdefault("probes", {})[name] = analyse_probe(
                    record, host_dir / f"{name}.csv", end_ms)
            elif "summary" in record:
                host.setdefault("readers", {})[name] = analyse_reader(record)
        cpu = host_dir / "cpu.txt"
        if cpu.is_file():
            lines = [list(map(int, line.split()[1:9])) for line in cpu.read_text().splitlines()[:2]]
            if len(lines) == 2:
                delta = [b - a for a, b in zip(*lines)]
                total = sum(delta) or 1
                host["host_cpu_busy_percent"] = round(100.0 * (total - delta[3] - delta[4]) / total, 1)
                host["host_cpu_steal_percent"] = round(100.0 * delta[7] / total, 2)
        netem = host_dir / "netem.log"
        if netem.is_file():
            host["netem"] = [line.strip() for line in netem.read_text().splitlines()
                             if "netem" in line or "Sent" in line][:6]
        if host:
            hosts[host_dir.name] = host
    step["hosts"] = hosts
    return step


def many(probes: list[dict]) -> dict:
    """Summary of the probes of a fan-out step."""
    good = [p for p in probes if not p.get("error") and (p.get("frames") or 0) > 24]
    out = {"viewers": len(probes), "viewers_with_video": len(good)}
    if good:
        p50 = [p["latency_ms"]["p50"] for p in good if p.get("latency_ms")]
        p99 = [p["latency_ms"]["p99"] for p in good if p.get("latency_ms")]
        out.update({
            "latency_p50_ms_median_viewer": round(statistics.median(p50), 1) if p50 else None,
            "latency_p50_ms_worst_viewer": max(p50) if p50 else None,
            "latency_p99_ms_worst_viewer": max(p99) if p99 else None,
            "frames_incomplete_total": sum(p.get("frames_incomplete") or 0 for p in good),
            "frames_total": sum(p.get("frames") or 0 for p in good),
            "kbps_median_viewer": round(statistics.median([p["kbps"] for p in good if p.get("kbps")]))
            if any(p.get("kbps") for p in good) else None,
            "first_key_frame_ms_median": round(statistics.median(
                [p["first_key_frame_ms"] for p in good if (p.get("first_key_frame_ms") or -1) >= 0]))
            if any((p.get("first_key_frame_ms") or -1) >= 0 for p in good) else None,
        })
    errors = sorted({(p.get("error") or "")[:120] for p in probes if p.get("error")})
    if errors:
        out["errors"] = errors[:5]
    return out


_spec = importlib.util.spec_from_file_location(
    "compare_audio_analyse", pathlib.Path(__file__).resolve().parent / "compare-audio-analyse.py")
audio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audio)

PLAYER_KEEP = ("error", "first_frame_ms", "sizes", "chrome_cpu_cores", "presented_fps", "freezes",
               "freeze_ms", "longest_freeze_ms", "freezes_per_minute", "freeze_ms_per_minute", "kbps",
               "jitter_buffer_ms", "hls_js_latency_s", "audio_minus_video_ms", "window_s")
PROBE_KEEP = ("error", "connect_ms", "first_byte_ms", "first_key_frame_ms", "frames_expected",
              "frames_missing", "frames_late", "frames_undecodable", "frames_from_recovery_encode",
              "on_time_percent", "delivered_percent", "stalls", "stall_ms", "longest_stall_ms",
              "stalls_per_minute", "stall_ms_per_minute", "kbps", "window_s", "audio_packets_missing",
              "audio_kbps", "counts", "livekit_server", "frames_incomplete", "frames")


def slim(step: dict) -> dict:
    """The versioned form of a step: the figures that the tables use."""
    out = {key: value for key, value in step.items() if key not in ("hosts", "source", "analysis_version")}
    hosts = {}
    for name, host in step["hosts"].items():
        kept = {key: host[key] for key in ("host_cpu_busy_percent", "host_cpu_steal_percent") if key in host}
        for kind, keep in (("probes", PROBE_KEEP), ("players", PLAYER_KEEP)):
            for label, record in (host.get(kind) or {}).items():
                if "viewers" in record:          # the summary of a crowd
                    kept.setdefault(kind, {})[label] = record
                    continue
                item = {key: record[key] for key in keep if record.get(key) is not None}
                for key in ("latency_ms", "glass_to_glass_ms", "audio_latency_ms", "audio_glass_to_glass_ms"):
                    if record.get(key):
                        item[key] = {k: record[key].get(k) for k in ("p50", "p95", "p99", "max")}
                kept.setdefault(kind, {})[label] = item
        if host.get("readers"):
            kept["readers"] = host["readers"]
        if kept:
            hosts[name] = kept
    out["hosts"] = hosts
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sessions", nargs="+")
    parser.add_argument("--slim", action="store_true",
                        help="the versioned form: only the figures that the tables use")
    parser.add_argument("--out", required=True)
    parser.add_argument("--runs", default=str(RUNS))
    args = parser.parse_args()
    evidence = {"schema": "needletail.compare.v1", "frame_ms": FRAME_MS,
                "head_ms_left_out": HEAD_MS, "tail_ms_left_out": TAIL_MS,
                "stall_margin_ms": STALL_MARGIN_MS, "sessions": {}}
    for name in args.sessions:
        directory = pathlib.Path(args.runs) / name
        if not directory.is_dir():
            sys.exit(f"no such session: {directory}")
        steps = {}
        samplers = audio.load_samplers(directory)
        for path in sorted(p for p in directory.iterdir() if p.is_dir() and p.name != "samplers"):
            step = analyse_step(path)
            if step is None:
                continue
            step = dict(step)
            if step.get("phase") == "fanout":
                for host in step["hosts"].values():
                    probes = list((host.get("probes") or {}).values())
                    if len(probes) > 1:
                        host["probes"] = {"all": many(probes)}
            if samplers and step.get("started_unix"):
                step["nodes"] = audio.node_usage(samplers, step["started_unix"] + 8,
                                                 step["started_unix"] + max(20, (step.get("seconds") or 40) - 8))
            steps[step["step"]] = slim(step) if args.slim else step
        evidence["sessions"][name] = {"record": load(directory / "session.json"), "steps": steps}
        print(f"{name}: {len(steps)} steps")
    evidence = audio.scrub(evidence)
    if args.slim:
        lines = []
        for name, session in evidence["sessions"].items():
            steps = session.pop("steps")
            head = json.dumps(session, separators=(",", ":"))[:-1]
            body = ",\n".join(f"  {json.dumps(key)}:{json.dumps(value, separators=(',', ':'))}"
                              for key, value in steps.items())
            lines.append(f" {json.dumps(name)}:{head},\"steps\":{{\n{body}\n }}}}")
        top = {key: value for key, value in evidence.items() if key != "sessions"}
        text = json.dumps(top, separators=(",", ":"))[:-1] + ",\"sessions\":{\n" + ",\n".join(lines) + "\n}}\n"
        json.loads(text)
        pathlib.Path(args.out).write_text(text)
    else:
        pathlib.Path(args.out).write_text(json.dumps(evidence, indent=1) + "\n")


if __name__ == "__main__":
    main()
