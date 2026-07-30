#!/usr/bin/env python3
import argparse
import json
import math
from pathlib import Path


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * probability) - 1)
    return ordered[min(index, len(ordered) - 1)]


def summarize(path: Path) -> dict:
    samples = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        samples.append(json.loads(line))
    successful = [
        sample
        for sample in samples
        if sample.get("curl_exit_code") == 0
        and sample.get("http_code") == 200
        and isinstance(sample.get("live_edge_latency_ms"), (int, float))
    ]
    latencies = [float(sample["live_edge_latency_ms"]) for sample in successful]
    requests = [float(sample["request_ms"]) for sample in successful]
    part_numbers = [
        int(sample["latest_part_number"])
        for sample in successful
        if isinstance(sample.get("latest_part_number"), int)
    ]
    part_steps = [
        current - previous
        for previous, current in zip(part_numbers, part_numbers[1:])
        if current > previous
    ]
    return {
        "samples": len(samples),
        "successful_samples": len(successful),
        "success_fraction": len(successful) / len(samples) if samples else 0,
        "live_edge_latency_ms": {
            "minimum": min(latencies) if latencies else None,
            "median": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "maximum": max(latencies) if latencies else None,
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
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()

    edge_summaries = {}
    for path in sorted(args.run_directory.glob("*/playlist.ndjson")):
        edge_summaries[path.parent.name] = summarize(path)
    result = {
        "schema": "needletail.multicloud-video-summary.v1",
        "edges": edge_summaries,
        "passed": bool(edge_summaries)
        and all(
            summary["successful_samples"] > 0
            and summary["success_fraction"] >= 0.9
            for summary in edge_summaries.values()
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
