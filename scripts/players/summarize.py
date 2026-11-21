#!/usr/bin/env python3
"""Print the key numbers of hlsjs-play / ffmpeg-play JSON reports, one block per report.

    python3 scripts/players/summarize.py scripts/players/samples/*.json
"""

from __future__ import annotations

import json
import sys


def stat(value: dict | None, key: str = "p50"):
    return None if not value else value.get(key)


def hlsjs(report: dict) -> list[str]:
    startup, playback, stalls = report["startup"], report["playback"], report["stalls"]
    frames, latency, loads, playlists = report.get("frames"), report["latency"], report["loads"], report["playlists"]
    steering, errors, network = report["steering"], report["errors"], report["network"]
    lines = [
        f"  browser {report['browser']['version']}, hls.js {report['hlsjs']['version']}, lowLatencyMode={report['harness']['low_latency']}",
        f"  startup: first frame {startup.get('time_to_first_frame_ms')} ms, playing {startup.get('time_to_playing_ms')} ms"
        f" (manifest {startup.get('manifest_loaded_ms')} ms, first playlist {startup.get('first_level_loaded_ms')} ms, first media {startup.get('first_media_loaded_ms')} ms)",
        f"  playback: {playback['media_played_s']} s of media in {playback['wall_after_first_playing_s']} s after first playing"
        f" (ratio {playback['media_played_ratio']}), wall {playback['wall_s']} s, jumped {playback['media_jumped_s']} s, {len(playback['seeks'])} seek(s)",
        f"  stalls: total {stalls['total_stalled_ms']} ms; playhead {len(stalls['playhead'])} ({stalls['playhead_total_ms']} ms),"
        f" waiting {len(stalls['element_waiting'])} ({stalls['element_waiting_total_ms']} ms),"
        f" hls.js bufferStalledError {stalls['hls_buffer_stalled_error_count']}",
    ]
    for stall in stalls["playhead"][:12]:
        lines.append(f"    playhead stall at {stall['start_ms']} ms for {stall['duration_ms']} ms (buffer ahead {stall['buffer_ahead_s']} s, resolved={stall['resolved']})")
    if frames:
        lines.append(f"  frames: {frames['total']} decoded, {frames['dropped']} dropped ({frames['dropped_ratio']}), {frames['presented']} presented,"
                     f" {frames['video_width']}x{frames['video_height']}, {frames['decoded_fps_over_played_time']} fps over played time")
    lines.append(f"  latency: hls.latency p50 {stat(latency['hls_latency_s'])} s max {stat(latency['hls_latency_s'], 'max')} s"
                 f" (target {stat(latency['hls_target_latency_s'])} s); PDT latency p50 {stat(latency['program_date_time_latency_ms'])} ms"
                 f" max {stat(latency['program_date_time_latency_ms'], 'max')} ms; buffer p50 {stat(report['buffer']['forward_buffer_s'])} s"
                 f" min {stat(report['buffer']['forward_buffer_s'], 'min')} s")
    lines.append(f"  loads: {loads['parts']} parts, {loads['whole_segments']} whole segments; playlists {playlists['loaded']}"
                 f" (blocking {playlists['with_hls_msn_and_part']}, msn only {playlists['with_hls_msn_only']}, plain {playlists['without_directives']},"
                 f" _HLS_skip {playlists['with_hls_skip']}); attribute changes {len(playlists['attribute_changes'])}")
    raw = playlists.get("raw_checks")
    if raw:
        lines.append(f"  playlist text checks: {raw['responses']} responses, {raw['without_complete_segment']} without a complete segment")
        for url, item in raw["per_url"].items():
            lines.append(f"    {url}: TARGETDURATION values {item['target_duration_values']}, PART-TARGET {item['part_target_values']},"
                         f" HOLD-BACK {item['hold_back_values']}, MEDIA-SEQUENCE {item['media_sequence_first']}..{item['media_sequence_last']}"
                         f" with {item['media_sequence_decreases']} decrease(s)")
        for event in raw["events"][:12]:
            lines.append(f"    {event['t']} ms {event['kind']}: {event['from']} -> {event['to']} ({event['segments']} segments, {event['parts']} parts listed)")
    lines.append(f"  steering: {steering['steering_manifest_load_count']} manifest loads, final pathway {steering['final_pathway']},"
                 f" pathways {steering['final_pathways']}, host changes {steering['host_changes']}")
    for move in steering["host_timeline"]:
        lines.append(f"    {move['t']} ms: {move['from']} -> {move['to']} (first request: {move['first_request']}, pathway {move['pathway']})")
    lines.append(f"  network: {network['requests']} responses, protocols {network['by_protocol']}, status {network['by_status']};"
                 f" CORS: disable-web-security needed: {network['cors']['web_security_needed_to_be_disabled']}")
    lines.append(f"  errors: {errors['count']} ({errors['fatal']} fatal) {errors['by_details']}")
    for error in errors["list"][:10]:
        lines.append(f"    {error['t']} ms {error['type']}/{error['details']} fatal={error['fatal']} code={error['response_code']} {error['message'] or ''} {(error['url'] or '')[:90]}")
    for recovery in errors["recoveries"][:6]:
        lines.append(f"    recovery at {recovery['t']} ms: {recovery['action']} after {recovery['after']}")
    return lines


def ffmpeg(report: dict) -> list[str]:
    probe, progress, stalls, log = report.get("ffprobe"), report["progress"], report["stalls"], report["log"]
    lines = [f"  {report['harness']['ffmpeg_version']}"]
    if probe:
        described = "; ".join(
            f"#{s.get('index')} {s.get('codec_name')} {s.get('profile', '')} {s.get('width', '')}x{s.get('height', '')} {s.get('avg_frame_rate', '')}"
            if s.get("codec_type") == "video" else f"#{s.get('index')} {s.get('codec_name')} {s.get('sample_rate')} Hz {s.get('channels')} ch {s.get('sample_fmt', '')}"
            for s in probe["streams"])
        lines.append(f"  ffprobe: opened={probe['opened']} exit={probe['exit_code']} in {probe['wall_s']} s, {len(probe['programs'])} program(s): {described}")
        for line in probe["stderr_first"][:4]:
            lines.append(f"    ffprobe: {line[:200]}")
    lines.append(f"  startup: first decoded frame {report['startup']['time_to_first_decoded_frame_ms']} ms")
    lines.append(f"  progress: {progress['media_after_first_frame_s']} s of media in {progress['wall_after_first_frame_s']} s after the first frame"
                 f" (speed {progress['speed_factor']}), frames {progress['frames']}, drop {progress['drop_frames']}")
    lines.append(f"  stalls: {stalls['count']}, total {stalls['total_stalled_ms']} ms")
    for stall in stalls["list"][:8]:
        lines.append(f"    stall at {stall['start_ms']} ms for {stall['duration_ms']} ms")
    lines.append(f"  log: {log['counts']} by category {log['by_category']} http codes {log['http_error_codes']}")
    for entry in log["first"][:8]:
        lines.append(f"    {entry['t_ms']} ms [{entry['level']}] [{entry['component']}] {entry['text'][:220]}")
    lines.append(f"  opens: {report['opens']['by_kind']} by host {report['opens']['by_host']}; exit {report['exit']}")
    return lines


def main() -> int:
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        verdict = report["verdict"]
        print(f"{path}\n  url {report['harness']['url']}\n  VERDICT {verdict['result']}: {verdict['reason']}")
        for note in verdict.get("notes", []):
            print(f"    note: {note}")
        body = hlsjs(report) if report["harness"]["name"] == "hlsjs-play" else ffmpeg(report)
        print("\n".join(body))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
