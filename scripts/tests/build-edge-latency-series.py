#!/usr/bin/env python3

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BUILDER = (
    ROOT / "scripts/multicloud-qualification/build-edge-latency-series.py"
)


def percentile(value: float) -> dict:
    return {
        "count": 1,
        "sample_count": 1,
        "min": value,
        "p50": value,
        "p95": value,
        "p99": value,
        "max": value,
    }


class EdgeLatencySeriesTests(unittest.TestCase):
    def test_builder_keeps_edges_transports_and_tracks_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result_directory = Path(temporary)
            edge = result_directory / "edge-london"
            edge.mkdir()
            udp_bucket = {
                "format": "flac",
                "start_offset_ms": 0,
                "start_unix_ns": 1_000,
                "expected_epochs": 200,
                "received_epochs": 200,
                "missing_epochs": 0,
                "deadline_misses": 0,
                "erasure_epochs": 0,
                "discontinuity_epochs": 0,
                "render_ready_latency_ms": percentile(25.0),
            }
            hls_bucket = {
                "start_offset_ms": 0,
                "start_unix_ns": 1_000,
                "expected_parts": 4,
                "received_parts": 4,
                "missing_parts": 0,
                "deadline_misses": 0,
                "availability_latency_ms": percentile(250.0),
            }
            (edge / "udp-group-0.json").write_text(
                json.dumps(
                    {
                        "formats": ["flac"],
                        "latency_time_series": [udp_bucket],
                    }
                ),
                encoding="utf-8",
            )
            (edge / "hls-track-0.json").write_text(
                json.dumps(
                    {
                        "stream_id": 1,
                        "latency_time_series": [hls_bucket],
                    }
                ),
                encoding="utf-8",
            )
            output = result_directory / "series.json"

            subprocess.run(
                [
                    "python3",
                    str(BUILDER),
                    str(result_directory),
                    str(output),
                ],
                check=True,
            )
            report = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(report["alignment"], "source_media_pts")
            self.assertTrue(report["complete"])
            self.assertEqual(report["issues"], [])
            self.assertEqual(len(report["series"]), 2)
            self.assertEqual(report["series"][0]["transport"], "udp_fec")
            self.assertEqual(report["series"][0]["points"][0]["p99_ms"], 25.0)
            self.assertEqual(report["series"][1]["transport"], "ll_hls")
            self.assertEqual(report["series"][1]["points"][0]["p99_ms"], 250.0)

    def test_builder_records_an_empty_probe_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result_directory = Path(temporary)
            edge = result_directory / "edge-japan"
            edge.mkdir()
            (edge / "hls-track-2.json").write_text("", encoding="utf-8")
            (edge / "hls-track-2.err").write_text(
                "Error: Remote reset: 0x0\n", encoding="utf-8"
            )
            udp_bucket = {
                "format": "flac",
                "start_offset_ms": 0,
                "start_unix_ns": 1_000,
                "expected_epochs": 200,
                "received_epochs": 200,
                "missing_epochs": 0,
                "deadline_misses": 0,
                "erasure_epochs": 0,
                "discontinuity_epochs": 0,
                "render_ready_latency_ms": percentile(25.0),
            }
            (edge / "udp-group-0.json").write_text(
                json.dumps(
                    {
                        "formats": ["flac"],
                        "latency_time_series": [udp_bucket],
                    }
                ),
                encoding="utf-8",
            )
            output = result_directory / "series.json"

            subprocess.run(
                [
                    "python3",
                    str(BUILDER),
                    str(result_directory),
                    str(output),
                ],
                check=True,
            )
            report = json.loads(output.read_text(encoding="utf-8"))

            self.assertFalse(report["complete"])
            self.assertEqual(len(report["series"]), 1)
            self.assertEqual(report["issues"][0]["node"], "edge-japan")
            self.assertEqual(
                report["issues"][0]["diagnostic"], "Error: Remote reset: 0x0"
            )

    def test_builder_uses_null_latency_when_a_bucket_has_no_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result_directory = Path(temporary)
            edge = result_directory / "edge-london"
            edge.mkdir()
            empty_summary = percentile(0.0)
            empty_summary["count"] = 0
            empty_summary["sample_count"] = 0
            (edge / "hls-track-0.json").write_text(
                json.dumps(
                    {
                        "stream_id": 1,
                        "latency_time_series": [
                            {
                                "start_offset_ms": 0,
                                "start_unix_ns": 1_000,
                                "expected_parts": 4,
                                "received_parts": 0,
                                "missing_parts": 4,
                                "deadline_misses": 0,
                                "availability_latency_ms": empty_summary,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = result_directory / "series.json"

            subprocess.run(
                [
                    "python3",
                    str(BUILDER),
                    str(result_directory),
                    str(output),
                ],
                check=True,
            )
            report = json.loads(output.read_text(encoding="utf-8"))

            point = report["series"][0]["points"][0]
            self.assertIsNone(point["p99_ms"])
            self.assertEqual(point["missing"], 4)


if __name__ == "__main__":
    unittest.main()
