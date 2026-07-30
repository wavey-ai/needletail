#!/usr/bin/env python3
"""Apply source capacity gates to one qualification run."""

from __future__ import annotations

import argparse
from collections import deque
import json
import math
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--daw", type=Path, required=True)
    parser.add_argument("--warnings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--tracks", type=int, required=True)
    parser.add_argument("--host-cpu-p99-max", type=float, default=80.0)
    parser.add_argument("--process-capacity-p99-max", type=float, default=75.0)
    parser.add_argument("--load-per-cpu-p99-max", type=float, default=0.75)
    parser.add_argument("--runnable-per-cpu-p99-max", type=float, default=0.75)
    parser.add_argument("--memory-available-min", type=float, default=20.0)
    parser.add_argument("--rss-memory-max", type=float, default=70.0)
    parser.add_argument("--encoder-rate-min", type=float, default=90.0)
    parser.add_argument("--max-sample-gap-ms", type=float, default=2_500.0)
    return parser.parse_args()


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    samples = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            samples.append(value)
    return samples


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * quantile))
    return ordered[rank - 1]


def max_gap_ms(samples: list[dict[str, Any]]) -> float | None:
    times = [int(sample["timestamp_unix_ns"]) for sample in samples]
    if len(times) < 2:
        return None
    return max((right - left) / 1_000_000 for left, right in zip(times, times[1:]))


def rolling_rate(
    samples: list[dict[str, Any]],
    value_path: tuple[str, ...],
    window_seconds: float = 3.0,
) -> list[float]:
    window: deque[dict[str, Any]] = deque()
    rates = []
    for sample in samples:
        window.append(sample)
        while len(window) > 1 and (
            int(sample["timestamp_unix_ns"])
            - int(window[0]["timestamp_unix_ns"])
            > (window_seconds + 1.0) * 1_000_000_000
        ):
            window.popleft()
        if len(window) < 2:
            continue
        elapsed = (
            int(sample["timestamp_unix_ns"])
            - int(window[0]["timestamp_unix_ns"])
        ) / 1_000_000_000
        if elapsed < window_seconds:
            continue
        first: Any = window[0]
        last: Any = sample
        for key in value_path:
            first = first[key]
            last = last[key]
        rates.append(max(0.0, float(last) - float(first)) / elapsed)
    return rates


def main() -> int:
    args = parse_args()
    start_ns = args.session_id
    end_ns = start_ns + args.duration_seconds * 1_000_000_000
    stable_start_ns = start_ns + 2_000_000_000
    stable_end_ns = end_ns - 2_000_000_000
    host = [
        sample
        for sample in read_ndjson(args.host)
        if start_ns <= int(sample["timestamp_unix_ns"]) < end_ns
    ]
    daw = [
        sample
        for sample in read_ndjson(args.daw)
        if start_ns <= int(sample["timestamp_unix_ns"]) < end_ns
    ]
    stable_daw = [
        sample
        for sample in daw
        if stable_start_ns <= int(sample["timestamp_unix_ns"]) < stable_end_ns
    ]

    cpu_count = int(host[0]["cpu_count"]) if host else 0
    host_cpu = [
        float(sample["host_cpu_percent"])
        for sample in host
        if sample.get("host_cpu_percent") is not None
    ]
    process_capacity = [
        float(sample["process_cpu_capacity_percent"])
        for sample in host
        if sample.get("process_cpu_capacity_percent") is not None
    ]
    load_per_cpu = (
        [float(sample["load_1"]) / cpu_count for sample in host] if cpu_count else []
    )
    runnable_per_cpu = (
        [float(sample["runnable_tasks"]) / cpu_count for sample in host]
        if cpu_count
        else []
    )
    memory_available = [
        100.0
        * float(sample["memory_available_bytes"])
        / float(sample["memory_total_bytes"])
        for sample in host
    ]
    rss_memory = [
        100.0
        * float(sample["rss_bytes"])
        / float(sample["memory_total_bytes"])
        for sample in host
    ]
    expected_samples = max(1, args.duration_seconds - 2)
    warning_count = sum(
        1
        for line in args.warnings.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    server_samples = [
        sample for sample in stable_daw if isinstance(sample.get("server"), dict)
    ]
    encoded_rates = rolling_rate(
        server_samples, ("server", "audio_frames_encoded")
    )
    expected_encoder_rate = args.tracks * 200.0
    minimum_encoder_rate = (
        min(encoded_rates) if encoded_rates else None
    )
    minimum_encoder_rate_percent = (
        None
        if minimum_encoder_rate is None or expected_encoder_rate == 0
        else 100.0 * minimum_encoder_rate / expected_encoder_rate
    )
    track_drops = max(
        (
            int(track["frames_dropped"])
            for sample in stable_daw
            for track in sample.get("tracks", [])
        ),
        default=0,
    )
    connection_failures = max(
        (
            int(track["connection_failures"])
            for sample in stable_daw
            for track in sample.get("tracks", [])
        ),
        default=0,
    )
    minimum_connections = min(
        (
            int(sample["server"]["active_daw_connections"])
            for sample in server_samples
        ),
        default=0,
    )
    udp_send_errors = max(
        (int(sample["server"]["udp_send_errors"]) for sample in server_samples),
        default=0,
    )

    metrics = {
        "host_samples": len(host),
        "daw_samples": len(daw),
        "cpu_count": cpu_count,
        "host_max_sample_gap_ms": max_gap_ms(host),
        "daw_max_sample_gap_ms": max_gap_ms(daw),
        "host_cpu_p99_percent": percentile(host_cpu, 0.99),
        "process_capacity_p99_percent": percentile(process_capacity, 0.99),
        "load_per_cpu_p99": percentile(load_per_cpu, 0.99),
        "runnable_per_cpu_p99": percentile(runnable_per_cpu, 0.99),
        "memory_available_min_percent": min(memory_available, default=None),
        "rss_memory_max_percent": max(rss_memory, default=None),
        "minimum_encoder_rate_packets_per_second": minimum_encoder_rate,
        "minimum_encoder_rate_percent": minimum_encoder_rate_percent,
        "minimum_active_daw_connections": minimum_connections,
        "track_frames_dropped": track_drops,
        "connection_failures": connection_failures,
        "udp_send_errors": udp_send_errors,
        "encoder_overload_warning_count": warning_count,
    }
    gates = {
        "host_sample_coverage": len(host) >= expected_samples,
        "daw_sample_coverage": len(daw) >= expected_samples,
        "host_sample_gap": max_gap_ms(host) is not None
        and max_gap_ms(host) <= args.max_sample_gap_ms,
        "daw_sample_gap": max_gap_ms(daw) is not None
        and max_gap_ms(daw) <= args.max_sample_gap_ms,
        "host_cpu_headroom": percentile(host_cpu, 0.99) is not None
        and percentile(host_cpu, 0.99) <= args.host_cpu_p99_max,
        "process_cpu_headroom": percentile(process_capacity, 0.99) is not None
        and percentile(process_capacity, 0.99) <= args.process_capacity_p99_max,
        "load_headroom": percentile(load_per_cpu, 0.99) is not None
        and percentile(load_per_cpu, 0.99) <= args.load_per_cpu_p99_max,
        "runnable_headroom": percentile(runnable_per_cpu, 0.99) is not None
        and percentile(runnable_per_cpu, 0.99) <= args.runnable_per_cpu_p99_max,
        "memory_headroom": min(memory_available, default=0.0)
        >= args.memory_available_min,
        "rss_headroom": max(rss_memory, default=100.0) <= args.rss_memory_max,
        "encoder_progress": minimum_encoder_rate_percent is not None
        and minimum_encoder_rate_percent >= args.encoder_rate_min,
        "all_tracks_connected": minimum_connections == args.tracks,
        "track_handoff_clean": track_drops == 0 and connection_failures == 0,
        "udp_send_clean": udp_send_errors == 0,
        "encoder_overload_log_clean": warning_count == 0,
    }
    report = {
        "schema": "needletail.multicloud-source-capacity.v1",
        "measurement_window": {
            "start_unix_ns": start_ns,
            "end_unix_ns": end_ns,
            "stable_start_unix_ns": stable_start_ns,
            "stable_end_unix_ns": stable_end_ns,
        },
        "thresholds": {
            "host_cpu_p99_max_percent": args.host_cpu_p99_max,
            "process_capacity_p99_max_percent": args.process_capacity_p99_max,
            "load_per_cpu_p99_max": args.load_per_cpu_p99_max,
            "runnable_per_cpu_p99_max": args.runnable_per_cpu_p99_max,
            "memory_available_min_percent": args.memory_available_min,
            "rss_memory_max_percent": args.rss_memory_max,
            "encoder_rate_min_percent": args.encoder_rate_min,
            "max_sample_gap_ms": args.max_sample_gap_ms,
        },
        "metrics": metrics,
        "gates": gates,
        "passed": all(gates.values()),
    }
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
