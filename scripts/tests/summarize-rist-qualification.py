#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SUMMARIZER = (
    ROOT
    / "scripts"
    / "multicloud-qualification"
    / "summarize-rist-qualification.py"
)


def write_ndjson(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )


def metrics(slots: int, continuity_errors: int = 0) -> str:
    return "\n".join(
        (
            f"av_contrib_mpeg_ts_slots_total {slots}",
            f"av_contrib_mpeg_ts_bytes_total {slots * 1316}",
            f"av_contrib_mpeg_ts_continuity_errors_total {continuity_errors}",
            "av_contrib_mpeg_ts_dropped_bytes_total 0",
            "",
        )
    )


def proxy_sample(recovered: int = 10) -> dict:
    return {
        "elapsed_ms": 10_000,
        "drop_every": 100,
        "media_first_transmissions": 1_000,
        "injected_drops": 10,
        "recovered_forwards": recovered,
        "expired_drops": 0,
        "feedback_datagrams": 10,
        "nack_requests": 10,
    }


def host_samples(cpu: float = 20.0) -> list[dict]:
    return [
        {
            "timestamp_unix_ns": index * 1_000_000_000,
            "host_cpu_percent": None if index == 0 else cpu,
            "process_cpu_capacity_percent": None if index == 0 else 5.0,
            "memory_total_bytes": 8_000_000_000,
            "memory_available_bytes": 6_000_000_000,
            "rss_bytes": 100_000_000,
        }
        for index in range(11)
    ]


class RistQualificationSummaryTests(unittest.TestCase):
    def invoke(
        self,
        root: Path,
        *,
        recovered: int = 10,
        continuity_after: int = 0,
        cpu: float = 20.0,
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        proxy = root / "proxy.ndjson"
        before = root / "before.prom"
        after = root / "after.prom"
        host = root / "host.ndjson"
        output = root / "summary.json"
        write_ndjson(proxy, [proxy_sample(recovered)])
        before.write_text(metrics(100), encoding="utf-8")
        after.write_text(metrics(1_100, continuity_after), encoding="utf-8")
        write_ndjson(host, host_samples(cpu))
        result = subprocess.run(
            [
                "python3",
                str(SUMMARIZER),
                "--loss-proxy",
                str(proxy),
                "--metrics-before",
                str(before),
                "--metrics-after",
                str(after),
                "--host",
                str(host),
                "--output",
                str(output),
                "--duration-seconds",
                "10",
                "--expected-drop-every",
                "100",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result, json.loads(output.read_text(encoding="utf-8"))

    def test_complete_recovery_and_clean_continuity_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, report = self.invoke(Path(temporary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["passed"])
        self.assertEqual(report["loss"]["injected_drops"], 10)
        self.assertEqual(
            report["continuity"]["deltas"]["av_contrib_mpeg_ts_slots_total"],
            1_000,
        )

    def test_recovery_shortfall_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, report = self.invoke(Path(temporary), recovered=9)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["gates"]["all_loss_recovered"])

    def test_mpeg_ts_continuity_damage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, report = self.invoke(Path(temporary), continuity_after=1)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["gates"]["mpeg_ts_continuity"])

    def test_excessive_host_cpu_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, report = self.invoke(Path(temporary), cpu=90.0)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["gates"]["host_cpu_headroom"])


if __name__ == "__main__":
    unittest.main()
