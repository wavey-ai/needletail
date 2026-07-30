#!/usr/bin/env python3
from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUMMARIZER = ROOT / "scripts" / "multicloud-qualification" / "summarize-video.py"
STARTED_AT = datetime.datetime(2026, 7, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)
STARTED_NS = int(STARTED_AT.timestamp() * 1_000_000_000)


def samples(
    *,
    count: int = 10,
    failed_indices: set[int] | None = None,
    stalled: bool = False,
    latencies: list[float] | None = None,
    request_interval_ms: int = 200,
) -> list[dict]:
    failed_indices = failed_indices or set()
    latencies = latencies or [500.0] * count
    result = []
    for index in range(count):
        request_ns = STARTED_NS + index * request_interval_ms * 1_000_000
        if index in failed_indices:
            result.append(
                {
                    "request_start_unix_ns": request_ns,
                    "arrival_unix_ns": request_ns + 20_000_000,
                    "curl_exit_code": 28,
                    "http_code": 0,
                }
            )
            continue
        part_number = 100 if stalled else 100 + index
        media_end_ns = STARTED_NS + (0 if stalled else index * 200_000_000)
        result.append(
            {
                "request_start_unix_ns": request_ns,
                "arrival_unix_ns": request_ns + 20_000_000,
                "curl_exit_code": 0,
                "http_code": 200,
                "request_ms": 20.0,
                "latest_part_number": part_number,
                "latest_media_end_unix_ns": media_end_ns,
                "live_edge_latency_ms": latencies[index],
            }
        )
    return result


def write_edge(run_directory: Path, edge: str, values: list[dict], status: int = 0):
    edge_directory = run_directory / edge
    edge_directory.mkdir(parents=True)
    (edge_directory / "playlist.ndjson").write_text(
        "".join(f"{json.dumps(value, separators=(',', ':'))}\n" for value in values),
        encoding="utf-8",
    )
    (edge_directory / "sampler.status").write_text(f"{status}\n", encoding="ascii")


def summarize(
    run_directory: Path,
    expected_edges: list[str],
    *,
    maximum_latency_ms: float = 1_000,
    maximum_age_ms: float = 1_000,
    active_duration_seconds: float = 2,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    command = [
        sys.executable,
        str(SUMMARIZER),
        str(run_directory),
        "--sampler-duration-seconds",
        "2",
        "--expected-active-duration-seconds",
        "2",
        "--sample-interval-ms",
        "200",
        "--active-started-at",
        STARTED_AT.isoformat(),
        "--active-ended-at",
        (STARTED_AT + datetime.timedelta(seconds=active_duration_seconds)).isoformat(),
        "--minimum-sample-coverage",
        "0.9",
        "--duration-tolerance-ms",
        "1",
        "--minimum-success-fraction",
        "0.9",
        "--maximum-p95-live-edge-latency-ms",
        str(maximum_latency_ms),
        "--maximum-live-edge-age-ms",
        str(maximum_age_ms),
    ]
    for edge in expected_edges:
        command.extend(("--expected-edge", edge))
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    return completed, json.loads(completed.stdout)


class SummarizeVideoTest(unittest.TestCase):
    def test_all_expected_edges_pass_every_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            write_edge(run_directory, "edge-a", samples())
            write_edge(run_directory, "edge-b", samples())
            completed, summary = summarize(run_directory, ["edge-a", "edge-b"])
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(summary["passed"])
            self.assertTrue(summary["checks"]["exact_edge_set"])
            for edge in ("edge-a", "edge-b"):
                self.assertTrue(summary["edges"][edge]["passed"])
                self.assertTrue(all(summary["edges"][edge]["checks"].values()))
                self.assertEqual(
                    summary["edges"][edge]["expected_samples"],
                    {"target": 10, "minimum": 9, "maximum": 11},
                )
                self.assertEqual(summary["edges"][edge]["sample_span_ms"], 1800)

    def test_each_release_gate_fails_closed(self) -> None:
        cases = [
            ("short", samples(count=3), 0, {}, "sample_count_in_bounds",),
            (
                "duration",
                samples(request_interval_ms=100),
                0,
                {},
                "sample_duration_in_bounds",
            ),
            (
                "active-duration",
                samples(),
                0,
                {"active_duration_seconds": 1},
                "active_duration_in_bounds",
            ),
            ("stalled", samples(stalled=True), 0, {}, "media_advancing",),
            ("errors", samples(failed_indices={8, 9}), 0, {}, "success_fraction",),
            (
                "latency",
                samples(latencies=[2_500.0] * 10),
                0,
                {"maximum_age_ms": 3_000},
                "p95_live_edge_latency",
            ),
            (
                "negative-latency",
                samples(latencies=[-100.0] * 10),
                0,
                {},
                "live_edge_latency_non_negative",
            ),
            ("age", samples(), 0, {"maximum_age_ms": 100}, "final_live_edge_age",),
            ("sampler-exit", samples(), 7, {}, "sampler_exit_zero",),
        ]
        for name, values, status, options, failed_check in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                run_directory = Path(temporary)
                write_edge(run_directory, "edge-a", values, status=status)
                completed, summary = summarize(run_directory, ["edge-a"], **options)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertFalse(summary["passed"])
                self.assertFalse(summary["edges"]["edge-a"]["checks"][failed_check])

    def test_missing_expected_edge_cannot_disappear_from_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            write_edge(run_directory, "edge-a", samples())
            completed, summary = summarize(run_directory, ["edge-a", "edge-missing"])
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertFalse(summary["passed"])
            self.assertEqual(summary["missing_edges"], ["edge-missing"])
            self.assertIn("edge-missing", summary["edges"])
            self.assertFalse(summary["edges"]["edge-missing"]["passed"])


if __name__ == "__main__":
    unittest.main()
