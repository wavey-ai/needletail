#!/usr/bin/env python3

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "scripts/multicloud-qualification/source-capacity-report.py"


class SourceCapacityReportTests(unittest.TestCase):
    def write_fixture(self, directory: Path, stalled: bool) -> tuple[Path, Path, Path]:
        host_path = directory / "host.ndjson"
        daw_path = directory / "daw.ndjson"
        warning_path = directory / "warnings.log"
        host_lines = []
        daw_lines = []
        tracks = 4
        for second in range(12):
            timestamp = second * 1_000_000_000
            host_lines.append(
                {
                    "timestamp_unix_ns": timestamp,
                    "cpu_count": 16,
                    "host_cpu_percent": 20.0,
                    "process_cpu_capacity_percent": 5.0,
                    "load_1": 2.0,
                    "runnable_tasks": 2,
                    "memory_total_bytes": 32_000_000_000,
                    "memory_available_bytes": 24_000_000_000,
                    "rss_bytes": 1_000_000_000,
                }
            )
            encoded_second = min(second, 6) if stalled else second
            daw_lines.append(
                {
                    "timestamp_unix_ns": timestamp,
                    "tracks": [
                        {
                            "frames_dropped": 0,
                            "connection_failures": 0,
                        }
                        for _ in range(tracks)
                    ],
                    "server": {
                        "active_daw_connections": tracks,
                        "audio_frames_encoded": encoded_second * tracks * 200,
                        "udp_send_errors": 0,
                    },
                }
            )
        host_path.write_text(
            "".join(json.dumps(value) + "\n" for value in host_lines),
            encoding="utf-8",
        )
        daw_path.write_text(
            "".join(json.dumps(value) + "\n" for value in daw_lines),
            encoding="utf-8",
        )
        warning_path.write_text("", encoding="utf-8")
        return host_path, daw_path, warning_path

    def run_report(self, stalled: bool) -> tuple[subprocess.CompletedProcess[str], dict]:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            host, daw, warnings = self.write_fixture(directory, stalled)
            output = directory / "report.json"
            result = subprocess.run(
                [
                    "python3",
                    str(REPORT),
                    "--host",
                    str(host),
                    "--daw",
                    str(daw),
                    "--warnings",
                    str(warnings),
                    "--output",
                    str(output),
                    "--session-id",
                    "0",
                    "--duration-seconds",
                    "12",
                    "--tracks",
                    "4",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            return result, json.loads(output.read_text(encoding="utf-8"))

    def test_healthy_source_passes_all_capacity_gates(self) -> None:
        result, report = self.run_report(stalled=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["passed"])
        self.assertTrue(all(report["gates"].values()))

    def test_encoder_stall_fails_progress_gate(self) -> None:
        result, report = self.run_report(stalled=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["passed"])
        self.assertFalse(report["gates"]["encoder_progress"])


if __name__ == "__main__":
    unittest.main()
