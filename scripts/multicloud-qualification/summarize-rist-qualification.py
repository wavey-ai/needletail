#!/usr/bin/env python3
"""Apply loss, continuity, CPU, and memory gates to one RIST video run."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
from typing import Any


PROMETHEUS_COUNTERS = (
    "av_contrib_mpeg_ts_slots_total",
    "av_contrib_mpeg_ts_bytes_total",
    "av_contrib_mpeg_ts_continuity_errors_total",
    "av_contrib_mpeg_ts_dropped_bytes_total",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loss-proxy", type=Path, required=True)
    parser.add_argument("--metrics-before", type=Path, required=True)
    parser.add_argument("--metrics-after", type=Path, required=True)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--expected-drop-every", type=int, required=True)
    parser.add_argument("--minimum-sample-coverage", type=float, default=0.95)
    parser.add_argument("--maximum-sample-gap-ms", type=float, default=2_500.0)
    parser.add_argument("--host-cpu-p99-max", type=float, default=80.0)
    parser.add_argument("--process-capacity-p99-max", type=float, default=75.0)
    parser.add_argument("--memory-available-min", type=float, default=20.0)
    parser.add_argument("--rss-memory-max", type=float, default=70.0)
    return parser.parse_args()


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    values = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            values.append(value)
    return values


def read_prometheus_counters(path: Path) -> dict[str, int]:
    counters: dict[str, int] = {}
    pattern = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\s+([0-9]+(?:\.0+)?)$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.fullmatch(line.strip())
        if match is None or match.group(1) not in PROMETHEUS_COUNTERS:
            continue
        name, raw_value = match.groups()
        value = float(raw_value)
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            raise ValueError(f"{path} contains an invalid counter: {name}")
        if name in counters:
            raise ValueError(f"{path} contains a duplicate counter: {name}")
        counters[name] = int(value)
    missing = sorted(set(PROMETHEUS_COUNTERS) - counters.keys())
    if missing:
        raise ValueError(f"{path} omits counters: {', '.join(missing)}")
    return counters


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * quantile))
    return ordered[rank - 1]


def maximum_gap_ms(samples: list[dict[str, Any]]) -> float | None:
    timestamps = [int(sample["timestamp_unix_ns"]) for sample in samples]
    if len(timestamps) < 2:
        return None
    return max(
        (right - left) / 1_000_000
        for left, right in zip(timestamps, timestamps[1:])
    )


def ratio_percent(numerator: Any, denominator: Any) -> float:
    denominator_value = float(denominator)
    if denominator_value <= 0:
        raise ValueError("resource sample has a non-positive memory total")
    return 100.0 * float(numerator) / denominator_value


def require_finite_thresholds(args: argparse.Namespace) -> None:
    if args.duration_seconds <= 0:
        raise ValueError("duration must be positive")
    if args.expected_drop_every <= 0:
        raise ValueError("expected drop interval must be positive")
    thresholds = (
        args.minimum_sample_coverage,
        args.maximum_sample_gap_ms,
        args.host_cpu_p99_max,
        args.process_capacity_p99_max,
        args.memory_available_min,
        args.rss_memory_max,
    )
    if not all(math.isfinite(value) for value in thresholds):
        raise ValueError("RIST qualification thresholds must be finite")
    if not 0 < args.minimum_sample_coverage <= 1:
        raise ValueError("sample coverage must be greater than zero and at most one")
    if any(value < 0 for value in thresholds[1:]):
        raise ValueError("RIST qualification thresholds must be non-negative")


def main() -> int:
    args = parse_args()
    require_finite_thresholds(args)

    proxy_samples = read_ndjson(args.loss_proxy)
    if not proxy_samples:
        raise ValueError("loss proxy output has no samples")
    proxy = proxy_samples[-1]
    host = read_ndjson(args.host)
    before = read_prometheus_counters(args.metrics_before)
    after = read_prometheus_counters(args.metrics_after)
    counter_deltas = {
        name: after[name] - before[name] for name in PROMETHEUS_COUNTERS
    }

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
    memory_available = [
        ratio_percent(sample["memory_available_bytes"], sample["memory_total_bytes"])
        for sample in host
    ]
    rss_memory = [
        ratio_percent(sample["rss_bytes"], sample["memory_total_bytes"])
        for sample in host
    ]
    maximum_gap = maximum_gap_ms(host)
    expected_samples = math.ceil(
        args.duration_seconds * args.minimum_sample_coverage
    )
    host_cpu_p99 = percentile(host_cpu, 0.99)
    process_capacity_p99 = percentile(process_capacity, 0.99)
    minimum_memory_available = min(memory_available, default=None)
    maximum_rss_memory = max(rss_memory, default=None)

    injected_drops = int(proxy.get("injected_drops", 0))
    recovered_forwards = int(proxy.get("recovered_forwards", 0))
    expired_drops = int(proxy.get("expired_drops", 0))
    gates = {
        "loss_injected": int(proxy.get("drop_every", 0))
        == args.expected_drop_every
        and injected_drops > 0,
        "all_loss_recovered": recovered_forwards == injected_drops
        and expired_drops == 0,
        "recovery_feedback_observed": int(proxy.get("feedback_datagrams", 0)) > 0
        and int(proxy.get("nack_requests", 0)) > 0,
        "media_transmitted": int(proxy.get("media_first_transmissions", 0)) > 0,
        "mpeg_ts_advanced": counter_deltas["av_contrib_mpeg_ts_slots_total"] > 0
        and counter_deltas["av_contrib_mpeg_ts_bytes_total"] > 0,
        "mpeg_ts_continuity": counter_deltas[
            "av_contrib_mpeg_ts_continuity_errors_total"
        ]
        == 0,
        "mpeg_ts_no_dropped_bytes": counter_deltas[
            "av_contrib_mpeg_ts_dropped_bytes_total"
        ]
        == 0,
        "resource_sample_coverage": len(host) >= expected_samples,
        "resource_sample_gap": maximum_gap is not None
        and maximum_gap <= args.maximum_sample_gap_ms,
        "host_cpu_headroom": host_cpu_p99 is not None
        and host_cpu_p99 <= args.host_cpu_p99_max,
        "process_cpu_headroom": process_capacity_p99 is not None
        and process_capacity_p99 <= args.process_capacity_p99_max,
        "memory_headroom": minimum_memory_available is not None
        and minimum_memory_available >= args.memory_available_min,
        "rss_headroom": maximum_rss_memory is not None
        and maximum_rss_memory <= args.rss_memory_max,
    }
    report = {
        "schema": "needletail.rist-video-qualification.v1",
        "duration_seconds": args.duration_seconds,
        "thresholds": {
            "drop_every": args.expected_drop_every,
            "minimum_sample_coverage": args.minimum_sample_coverage,
            "maximum_sample_gap_ms": args.maximum_sample_gap_ms,
            "host_cpu_p99_max_percent": args.host_cpu_p99_max,
            "process_capacity_p99_max_percent": args.process_capacity_p99_max,
            "memory_available_min_percent": args.memory_available_min,
            "rss_memory_max_percent": args.rss_memory_max,
        },
        "loss": {
            "media_first_transmissions": int(
                proxy.get("media_first_transmissions", 0)
            ),
            "injected_drops": injected_drops,
            "recovered_forwards": recovered_forwards,
            "expired_drops": expired_drops,
            "feedback_datagrams": int(proxy.get("feedback_datagrams", 0)),
            "nack_requests": int(proxy.get("nack_requests", 0)),
        },
        "continuity": {
            "before": before,
            "after": after,
            "deltas": counter_deltas,
        },
        "resources": {
            "samples": len(host),
            "expected_samples": expected_samples,
            "maximum_sample_gap_ms": maximum_gap,
            "host_cpu_p99_percent": host_cpu_p99,
            "process_capacity_p99_percent": process_capacity_p99,
            "memory_available_min_percent": minimum_memory_available,
            "rss_memory_max_percent": maximum_rss_memory,
        },
        "gates": gates,
        "passed": all(gates.values()),
    }
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
