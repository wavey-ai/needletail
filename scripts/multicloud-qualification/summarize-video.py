#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime
import json
import math
import re
from pathlib import Path
from typing import Any


SAMPLER_STATUS_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\n?$")


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * probability) - 1)
    return ordered[min(index, len(ordered) - 1)]


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return parsed


def fraction(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 1:
        raise argparse.ArgumentTypeError("must be greater than zero and at most one")
    return parsed


def utc_timestamp_ns(value: str) -> int:
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return int(parsed.timestamp() * 1_000_000_000)


def read_samples(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return [], [f"could not read {path.name}: {error}"]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(f"line {line_number}: invalid JSON: {error.msg}")
            continue
        if not isinstance(sample, dict):
            errors.append(f"line {line_number}: sample is not an object")
            continue
        samples.append(sample)
    return samples, errors


def read_sampler_status(path: Path) -> tuple[int | None, list[str]]:
    try:
        source = path.read_text(encoding="ascii")
    except OSError as error:
        return None, [f"could not read sampler.status: {error}"]
    if not SAMPLER_STATUS_PATTERN.fullmatch(source):
        return None, ["sampler.status is not one unsigned decimal exit status"]
    return int(source), []


def integer_field(sample: dict[str, Any], name: str) -> int | None:
    value = sample.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def finite_number_field(sample: dict[str, Any], name: str) -> float | None:
    value = sample.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rendered = float(value)
    return rendered if math.isfinite(rendered) else None


def successful_sample(sample: dict[str, Any]) -> bool:
    return (
        sample.get("curl_exit_code") == 0
        and sample.get("http_code") == 200
        and finite_number_field(sample, "live_edge_latency_ms") is not None
        and finite_number_field(sample, "request_ms") is not None
        and integer_field(sample, "latest_media_end_unix_ns") is not None
        and integer_field(sample, "latest_part_number") is not None
    )


def summarize_edge(
    edge_directory: Path,
    *,
    sampler_duration_seconds: int,
    expected_active_duration_seconds: int,
    sample_interval_ms: int,
    active_started_ns: int,
    active_ended_ns: int,
    minimum_sample_coverage: float,
    duration_tolerance_ms: float,
    minimum_success_fraction: float,
    maximum_p95_live_edge_latency_ms: float,
    maximum_live_edge_age_ms: float,
) -> dict[str, Any]:
    samples, input_errors = read_samples(edge_directory / "playlist.ndjson")
    sampler_status, status_errors = read_sampler_status(
        edge_directory / "sampler.status"
    )
    input_errors.extend(status_errors)

    timestamps = [integer_field(sample, "request_start_unix_ns") for sample in samples]
    timestamps_valid = all(timestamp is not None for timestamp in timestamps)
    valid_timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    timestamps_monotonic = timestamps_valid and all(
        current >= previous
        for previous, current in zip(valid_timestamps, valid_timestamps[1:])
    )
    sample_span_ms = (
        (valid_timestamps[-1] - valid_timestamps[0]) / 1_000_000
        if valid_timestamps
        else None
    )

    expected_samples = math.ceil(sampler_duration_seconds * 1000 / sample_interval_ms)
    minimum_samples = max(1, math.ceil(expected_samples * minimum_sample_coverage))
    maximum_samples = expected_samples + 1
    expected_span_ms = max(0, (expected_samples - 1) * sample_interval_ms)
    minimum_span_ms = max(0.0, expected_span_ms - duration_tolerance_ms)
    maximum_span_ms = expected_span_ms + duration_tolerance_ms

    active_samples = [
        sample
        for sample in samples
        if (
            (timestamp := integer_field(sample, "request_start_unix_ns")) is not None
            and active_started_ns <= timestamp <= active_ended_ns
        )
    ]
    successful = [sample for sample in samples if successful_sample(sample)]
    active_successful = [
        sample for sample in active_samples if successful_sample(sample)
    ]
    active_window_duration_ms = (active_ended_ns - active_started_ns) / 1_000_000
    expected_active_samples = math.ceil(
        expected_active_duration_seconds * 1000 / sample_interval_ms
    )
    minimum_active_samples = max(
        1, math.ceil(expected_active_samples * minimum_sample_coverage)
    )
    maximum_active_samples = (
        math.ceil(
            (expected_active_duration_seconds * 1000 + duration_tolerance_ms)
            / sample_interval_ms
        )
        + 1
    )
    minimum_active_duration_ms = max(
        0.0, expected_active_duration_seconds * 1000 - duration_tolerance_ms
    )
    maximum_active_duration_ms = (
        expected_active_duration_seconds * 1000 + duration_tolerance_ms
    )
    latencies = [
        finite_number_field(sample, "live_edge_latency_ms")
        for sample in active_successful
    ]
    requests = [
        finite_number_field(sample, "request_ms") for sample in active_successful
    ]
    latencies = [value for value in latencies if value is not None]
    requests = [value for value in requests if value is not None]
    part_numbers = [
        integer_field(sample, "latest_part_number") for sample in active_successful
    ]
    part_numbers = [value for value in part_numbers if value is not None]
    media_ends = [
        integer_field(sample, "latest_media_end_unix_ns")
        for sample in active_successful
    ]
    media_ends = [value for value in media_ends if value is not None]
    part_steps = [
        current - previous
        for previous, current in zip(part_numbers, part_numbers[1:])
        if current > previous
    ]
    media_advanced = (
        len(part_numbers) >= 2
        and part_numbers[-1] > part_numbers[0]
        and len(media_ends) >= 2
        and media_ends[-1] > media_ends[0]
    )
    final_live_edge_age_ms = (
        (active_ended_ns - media_ends[-1]) / 1_000_000 if media_ends else None
    )
    p95_live_edge_latency_ms = percentile(latencies, 0.95)
    minimum_live_edge_latency_ms = min(latencies) if latencies else None
    success_fraction = len(successful) / len(samples) if samples else 0
    active_success_fraction = (
        len(active_successful) / len(active_samples) if active_samples else 0
    )

    checks = {
        "input_valid": not input_errors,
        "sampler_exit_zero": sampler_status == 0,
        "timestamps_monotonic": timestamps_monotonic,
        "sample_count_in_bounds": minimum_samples <= len(samples) <= maximum_samples,
        "sample_duration_in_bounds": sample_span_ms is not None
        and minimum_span_ms <= sample_span_ms <= maximum_span_ms,
        "active_duration_in_bounds": minimum_active_duration_ms
        <= active_window_duration_ms
        <= maximum_active_duration_ms,
        "active_sample_count_in_bounds": minimum_active_samples
        <= len(active_samples)
        <= maximum_active_samples,
        "success_fraction": active_success_fraction >= minimum_success_fraction,
        "media_advancing": media_advanced,
        "live_edge_latency_non_negative": minimum_live_edge_latency_ms is not None
        and minimum_live_edge_latency_ms >= 0,
        "p95_live_edge_latency": p95_live_edge_latency_ms is not None
        and p95_live_edge_latency_ms <= maximum_p95_live_edge_latency_ms,
        "final_live_edge_age": final_live_edge_age_ms is not None
        and final_live_edge_age_ms >= 0
        and final_live_edge_age_ms <= maximum_live_edge_age_ms,
    }
    return {
        "sampler_exit_status": sampler_status,
        "input_errors": input_errors,
        "samples": len(samples),
        "expected_samples": {
            "target": expected_samples,
            "minimum": minimum_samples,
            "maximum": maximum_samples,
        },
        "sample_span_ms": sample_span_ms,
        "expected_sample_span_ms": {
            "target": expected_span_ms,
            "minimum": minimum_span_ms,
            "maximum": maximum_span_ms,
        },
        "successful_samples": len(successful),
        "success_fraction": success_fraction,
        "active_samples": len(active_samples),
        "expected_active_samples": {
            "target": expected_active_samples,
            "minimum": minimum_active_samples,
            "maximum": maximum_active_samples,
        },
        "active_window_duration_ms": active_window_duration_ms,
        "expected_active_window_duration_ms": {
            "target": expected_active_duration_seconds * 1000,
            "minimum": minimum_active_duration_ms,
            "maximum": maximum_active_duration_ms,
        },
        "active_successful_samples": len(active_successful),
        "active_success_fraction": active_success_fraction,
        "live_edge_latency_ms": {
            "minimum": minimum_live_edge_latency_ms,
            "median": percentile(latencies, 0.5),
            "p95": p95_live_edge_latency_ms,
            "p99": percentile(latencies, 0.99),
            "maximum": max(latencies) if latencies else None,
            "final": final_live_edge_age_ms,
        },
        "request_ms": {
            "median": percentile(requests, 0.5),
            "p95": percentile(requests, 0.95),
            "maximum": max(requests) if requests else None,
        },
        "part_progress": {
            "first": part_numbers[0] if part_numbers else None,
            "last": part_numbers[-1] if part_numbers else None,
            "maximum_observed_step": max(part_steps) if part_steps else None,
        },
        "media_end_progress": {
            "first_unix_ns": media_ends[0] if media_ends else None,
            "last_unix_ns": media_ends[-1] if media_ends else None,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    parser.add_argument(
        "--expected-edge",
        action="append",
        required=True,
        help="edge directory required in the result; repeat for every edge",
    )
    parser.add_argument(
        "--sampler-duration-seconds", type=positive_integer, required=True
    )
    parser.add_argument(
        "--expected-active-duration-seconds", type=positive_integer, required=True
    )
    parser.add_argument("--sample-interval-ms", type=positive_integer, required=True)
    parser.add_argument("--active-started-at", type=utc_timestamp_ns, required=True)
    parser.add_argument("--active-ended-at", type=utc_timestamp_ns, required=True)
    parser.add_argument("--minimum-sample-coverage", type=fraction, default=0.95)
    parser.add_argument(
        "--duration-tolerance-ms", type=non_negative_float, default=2_000.0
    )
    parser.add_argument("--minimum-success-fraction", type=fraction, default=0.9)
    parser.add_argument(
        "--maximum-p95-live-edge-latency-ms", type=non_negative_float, default=2_000.0,
    )
    parser.add_argument(
        "--maximum-live-edge-age-ms", type=non_negative_float, default=3_000.0,
    )
    args = parser.parse_args()

    if args.active_ended_at <= args.active_started_at:
        parser.error("--active-ended-at must be later than --active-started-at")
    if len(set(args.expected_edge)) != len(args.expected_edge):
        parser.error("--expected-edge values must be unique")
    for edge in args.expected_edge:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", edge):
            parser.error(f"invalid expected edge name: {edge}")

    observed_edges = sorted(
        path.parent.name
        for path in args.run_directory.glob("*/playlist.ndjson")
        if path.parent.is_dir()
    )
    expected_edges = sorted(args.expected_edge)
    missing_edges = sorted(set(expected_edges) - set(observed_edges))
    unexpected_edges = sorted(set(observed_edges) - set(expected_edges))
    edge_summaries = {
        edge: summarize_edge(
            args.run_directory / edge,
            sampler_duration_seconds=args.sampler_duration_seconds,
            expected_active_duration_seconds=args.expected_active_duration_seconds,
            sample_interval_ms=args.sample_interval_ms,
            active_started_ns=args.active_started_at,
            active_ended_ns=args.active_ended_at,
            minimum_sample_coverage=args.minimum_sample_coverage,
            duration_tolerance_ms=args.duration_tolerance_ms,
            minimum_success_fraction=args.minimum_success_fraction,
            maximum_p95_live_edge_latency_ms=args.maximum_p95_live_edge_latency_ms,
            maximum_live_edge_age_ms=args.maximum_live_edge_age_ms,
        )
        for edge in expected_edges
    }
    exact_edge_set = not missing_edges and not unexpected_edges
    result = {
        "schema": "needletail.multicloud-video-summary.v1",
        "criteria": {
            "sampler_duration_seconds": args.sampler_duration_seconds,
            "expected_active_duration_seconds": (args.expected_active_duration_seconds),
            "sample_interval_ms": args.sample_interval_ms,
            "minimum_sample_coverage": args.minimum_sample_coverage,
            "duration_tolerance_ms": args.duration_tolerance_ms,
            "minimum_success_fraction": args.minimum_success_fraction,
            "maximum_p95_live_edge_latency_ms": (args.maximum_p95_live_edge_latency_ms),
            "maximum_live_edge_age_ms": args.maximum_live_edge_age_ms,
        },
        "expected_edges": expected_edges,
        "observed_edges": observed_edges,
        "missing_edges": missing_edges,
        "unexpected_edges": unexpected_edges,
        "checks": {"exact_edge_set": exact_edge_set},
        "edges": edge_summaries,
        "passed": exact_edge_set
        and all(summary["passed"] for summary in edge_summaries.values()),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
