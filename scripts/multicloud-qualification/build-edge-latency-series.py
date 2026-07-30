#!/usr/bin/env python3
"""Build chart-ready UDP and LL-HLS latency series for each edge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_directory", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def percentile_points(
    buckets: list[dict[str, Any]],
    metric: str,
    expected_field: str,
    received_field: str,
    missing_field: str,
) -> list[dict[str, Any]]:
    points = []
    for bucket in buckets:
        summary = bucket[metric]
        has_samples = summary["sample_count"] > 0
        point = {
            "start_offset_ms": bucket["start_offset_ms"],
            "start_unix_ns": bucket["start_unix_ns"],
            "p50_ms": summary["p50"] if has_samples else None,
            "p95_ms": summary["p95"] if has_samples else None,
            "p99_ms": summary["p99"] if has_samples else None,
            "max_ms": summary["max"] if has_samples else None,
            "expected": bucket[expected_field],
            "received": bucket[received_field],
            "missing": bucket[missing_field],
            "deadline_misses": bucket["deadline_misses"],
        }
        if "erasure_epochs" in bucket:
            point["erasure_epochs"] = bucket["erasure_epochs"]
            point["discontinuity_epochs"] = bucket["discontinuity_epochs"]
        points.append(point)
    return points


def add_udp_series(series: list[dict[str, Any]], node: str, path: Path) -> None:
    report = read_json(path)
    track_index = int(path.stem.rsplit("-", 1)[1])
    for audio_format in report["formats"]:
        buckets = [
            bucket
            for bucket in report["latency_time_series"]
            if bucket["format"] == audio_format
        ]
        series.append(
            {
                "node": node,
                "track_index": track_index,
                "stream_id": track_index + 1,
                "transport": "udp_fec",
                "format": audio_format,
                "metric": "render_ready_latency_ms",
                "points": percentile_points(
                    buckets,
                    "render_ready_latency_ms",
                    "expected_epochs",
                    "received_epochs",
                    "missing_epochs",
                ),
            }
        )


def add_hls_series(
    series: list[dict[str, Any]], node: str, path: Path, audio_format: str
) -> None:
    report = read_json(path)
    track_index = int(path.stem.rsplit("-", 1)[1])
    series.append(
        {
            "node": node,
            "track_index": track_index,
            "stream_id": report["stream_id"],
            "transport": "ll_hls",
            "format": audio_format,
            "metric": "availability_latency_ms",
            "points": percentile_points(
                report["latency_time_series"],
                "availability_latency_ms",
                "expected_parts",
                "received_parts",
                "missing_parts",
            ),
        }
    )


def add_report(
    series: list[dict[str, Any]],
    issues: list[dict[str, str]],
    node: str,
    path: Path,
    transport: str,
    audio_format: str | None = None,
) -> None:
    try:
        if transport == "udp_fec":
            add_udp_series(series, node, path)
        else:
            if audio_format is None:
                raise ValueError("LL-HLS report does not specify an audio format")
            add_hls_series(series, node, path, audio_format)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        error_path = path.with_suffix(".err")
        diagnostic = ""
        if error_path.is_file():
            diagnostic = error_path.read_text(encoding="utf-8").strip()
        issues.append(
            {
                "node": node,
                "transport": transport,
                "format": audio_format or "unknown",
                "report": str(path.relative_to(path.parents[1])),
                "error": str(error),
                "diagnostic": diagnostic,
            }
        )


def main() -> int:
    args = parse_args()
    series: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    edge_directories = sorted(
        path for path in args.result_directory.glob("edge-*") if path.is_dir()
    )
    for edge_directory in edge_directories:
        node = edge_directory.name
        for path in sorted(edge_directory.glob("udp-group-*.json")):
            add_report(series, issues, node, path, "udp_fec")
        for path in sorted(edge_directory.glob("hls-track-*.json")):
            add_report(series, issues, node, path, "ll_hls", "flac")
        for path in sorted(edge_directory.glob("hls-opus-track-*.json")):
            add_report(series, issues, node, path, "ll_hls", "opus")
    if not series:
        raise SystemExit("no edge latency time series were found")
    report = {
        "schema": "needletail.edge-latency-time-series.v2",
        "bucket_ms": 1_000,
        "alignment": "source_media_pts",
        "complete": not issues,
        "issues": issues,
        "series": series,
    }
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
