#!/usr/bin/env python3

import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
EXPORTER = ROOT / "scripts/multicloud-qualification/export-metrics.py"


def percentile(p50: float, p95: float) -> dict:
    return {
        "count": 10,
        "sample_count": 10,
        "min": p50,
        "p50": p50,
        "p95": p95,
        "p99": p95,
        "max": p95,
    }


def run_report(run_id: str = "current-v9") -> dict:
    return {
        "schema": "needletail.multicloud-lossless-run.v9",
        "run_id": run_id,
        "tracks": 1,
        "channels": 2,
        "duration_seconds": 10,
        "group_count": 1,
        "outcomes": {
            "mesh": {"selected": True, "passed": True},
            "playback": {"selected": True, "passed": False},
            "playback_flac": {"selected": True, "passed": False},
            "playback_opus": {"selected": True, "passed": True},
        },
        "passed": False,
    }


def udp_report() -> dict:
    return {
        "schema": "needletail.aep1-48k-probe.receive.v2",
        "formats": ["flac"],
        "expected_epochs": 10,
        "received_epochs": 9,
        "missing_epochs": 1,
        "deadline_misses": 2,
        "latency_ms": percentile(20.0, 30.0),
        "render_ready_latency_ms": percentile(25.0, 35.0),
    }


def hls_report(audio_format: str, missing_parts: int, p50: float, p95: float,) -> dict:
    return {
        "schema": "needletail.aep1-48k-probe.hls-receive.v6",
        "expected_audio_codec": audio_format,
        "expected_parts": 10,
        "received_parts": 10 - missing_parts,
        "missing_parts": missing_parts,
        "deadline_misses": missing_parts + 2,
        "availability_latency_ms": percentile(p50, p95),
        "estimated_render_latency_ms": percentile(p50 + 100, p95 + 100),
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def invoke_exporter(runs: Path, output: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(EXPORTER), "--runs", str(runs), "--output", str(output),],
        check=True,
        capture_output=True,
        text=True,
    )


class ExportMetricsTests(unittest.TestCase):
    def test_v9_rows_keep_udp_flac_and_opus_metrics_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            run = runs / "current-v9"
            edge = run / "edge-london"
            output = root / "metrics"
            write_json(run / "run.json", run_report())
            write_json(edge / "udp-group-0.json", udp_report())
            write_json(
                edge / "hls-track-0.json", hls_report("flac", 3, 200.0, 250.0),
            )
            write_json(
                edge / "hls-opus-track-0.json", hls_report("opus", 4, 300.0, 350.0),
            )
            # The removed compatibility name must not affect current exports.
            (edge / "hls-group-0.json").write_text("", encoding="utf-8")

            invoke_exporter(runs, output)

            report = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
            self.assertTrue(report["complete"])
            self.assertEqual(report["quarantined_artifacts"], 0)
            self.assertEqual(len(report["rows"]), 3)
            rows = {(row["protocol"], row["format"]): row for row in report["rows"]}
            self.assertEqual(
                set(rows),
                {("UDP/FEC", "FLAC"), ("LL-HLS", "FLAC"), ("LL-HLS", "Opus"),},
            )
            self.assertEqual(rows[("UDP/FEC", "FLAC")]["missing_units"], 1)
            self.assertTrue(rows[("UDP/FEC", "FLAC")]["passed"])
            self.assertEqual(rows[("UDP/FEC", "FLAC")]["deadline_misses"], 2)
            self.assertEqual(rows[("UDP/FEC", "FLAC")]["direct_udp_p95_ms"], 30.0)
            self.assertIsNone(rows[("UDP/FEC", "FLAC")]["llhls_p95_ms"])
            self.assertEqual(rows[("LL-HLS", "FLAC")]["missing_units"], 3)
            self.assertFalse(rows[("LL-HLS", "FLAC")]["passed"])
            self.assertEqual(rows[("LL-HLS", "FLAC")]["llhls_p95_ms"], 250.0)
            self.assertIsNone(rows[("LL-HLS", "FLAC")]["direct_udp_p95_ms"])
            self.assertEqual(rows[("LL-HLS", "Opus")]["missing_units"], 4)
            self.assertTrue(rows[("LL-HLS", "Opus")]["passed"])
            self.assertEqual(rows[("LL-HLS", "Opus")]["llhls_p95_ms"], 350.0)

            with (output / "metrics.csv").open(
                encoding="utf-8", newline=""
            ) as csv_file:
                csv_rows = list(csv.DictReader(csv_file))
            self.assertEqual(len(csv_rows), 3)
            self.assertEqual(
                {(row["protocol"], row["format"]) for row in csv_rows}, set(rows),
            )
            quarantine = json.loads(
                (output / "quarantine.json").read_text(encoding="utf-8")
            )
            self.assertEqual(quarantine["artifacts"], [])

    def test_current_video_summary_is_still_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            run = runs / "video"
            output = root / "metrics"
            write_json(
                run / "run.json",
                {
                    "schema": "needletail.multicloud-video-run.v1",
                    "run_id": "video",
                    "protocol": "srt",
                    "duration_seconds": 600,
                    "passed": True,
                },
            )
            write_json(
                run / "summary.json",
                {
                    "schema": "needletail.multicloud-video-summary.v1",
                    "edges": {
                        "edge-london": {
                            "live_edge_latency_ms": {"median": 500.0, "p95": 550.0,},
                            "request_ms": {"p95": 20.0},
                        }
                    },
                },
            )

            invoke_exporter(runs, output)

            report = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
            self.assertTrue(report["complete"])
            self.assertEqual(len(report["rows"]), 1)
            self.assertEqual(report["rows"][0]["kind"], "video")
            self.assertEqual(report["rows"][0]["protocol"], "SRT")
            self.assertEqual(report["rows"][0]["llhls_p95_ms"], 550.0)

    def test_empty_and_incomplete_artifacts_are_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            run = runs / "current-v9"
            edge = run / "edge-japan"
            output = root / "metrics"
            write_json(run / "run.json", run_report())
            write_json(edge / "udp-group-0.json", udp_report())
            edge.mkdir(parents=True, exist_ok=True)
            (edge / "hls-track-0.json").write_text("", encoding="utf-8")
            (edge / "hls-track-0.err").write_text(
                "remote reset during probe\n", encoding="utf-8"
            )
            write_json(
                edge / "hls-opus-track-0.json",
                {
                    "schema": "needletail.aep1-48k-probe.hls-receive.v6",
                    "expected_audio_codec": "opus",
                },
            )
            broken_run = runs / "broken-run"
            broken_run.mkdir(parents=True)
            (broken_run / "run.json").write_text("", encoding="utf-8")
            (runs / "missing-run").mkdir()
            output.mkdir()
            (output / "metrics.json").write_text("previous metrics\n", encoding="utf-8")

            result = invoke_exporter(runs, output)

            self.assertIn("quarantined 4 incomplete artifact(s)", result.stderr)
            report = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
            self.assertFalse(report["complete"])
            self.assertEqual(report["quarantined_artifacts"], 4)
            self.assertEqual(len(report["rows"]), 1)
            self.assertEqual(report["rows"][0]["protocol"], "UDP/FEC")
            quarantine = json.loads(
                (output / "quarantine.json").read_text(encoding="utf-8")
            )
            self.assertEqual(quarantine["artifact_count"], 4)
            entries = {entry["artifact"]: entry for entry in quarantine["artifacts"]}
            self.assertEqual(
                entries["current-v9/edge-japan/hls-track-0.json"]["reason"],
                "artifact is empty",
            )
            self.assertEqual(
                entries["current-v9/edge-japan/hls-track-0.json"]["diagnostic"],
                "remote reset during probe",
            )
            self.assertIn(
                "missing required field",
                entries["current-v9/edge-japan/hls-opus-track-0.json"]["reason"],
            )
            self.assertEqual(
                entries["broken-run/run.json"]["reason"], "artifact is empty",
            )
            self.assertEqual(
                entries["missing-run/run.json"]["reason"], "artifact is missing",
            )
            self.assertEqual(
                (edge / "hls-track-0.json").read_text(encoding="utf-8"), "",
            )
            self.assertEqual(
                list(output.glob(".*.tmp")), [],
            )

    def test_non_finite_latency_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            run = runs / "current-v9"
            edge = run / "edge-london"
            output = root / "metrics"
            write_json(run / "run.json", run_report())
            report = udp_report()
            report["latency_ms"]["p95"] = float("nan")
            write_json(edge / "udp-group-0.json", report)
            write_json(
                edge / "hls-track-0.json", hls_report("flac", 0, 200.0, 250.0),
            )
            write_json(
                edge / "hls-opus-track-0.json", hls_report("opus", 0, 300.0, 350.0),
            )

            invoke_exporter(runs, output)

            metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
            self.assertFalse(metrics["complete"])
            self.assertEqual(len(metrics["rows"]), 2)
            quarantine = json.loads(
                (output / "quarantine.json").read_text(encoding="utf-8")
            )
            self.assertEqual(quarantine["artifact_count"], 1)
            self.assertIn(
                "non-standard JSON numeric constant",
                quarantine["artifacts"][0]["reason"],
            )

    def test_atomic_write_keeps_previous_file_when_replace_fails(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "needletail_export_metrics", EXPORTER
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exporter)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "metrics.json"
            output.write_text("previous\n", encoding="utf-8")
            with mock.patch.object(
                exporter.os,
                "replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    exporter.atomic_write_text(output, "new\n")

            self.assertEqual(output.read_text(encoding="utf-8"), "previous\n")
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
