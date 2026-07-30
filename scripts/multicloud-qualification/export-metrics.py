#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path


EDGE_NAMES = (
    "edge-london",
    "edge-tokyo",
    "edge-sydney",
    "edge-australia",
    "edge-japan",
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def audio_rows(run_directory: Path, run: dict) -> list[dict]:
    rows = []
    for edge in EDGE_NAMES:
        edge_directory = run_directory / edge
        for udp_path in sorted(edge_directory.glob("udp-group-*.json")):
            group = udp_path.stem.rsplit("-", 1)[-1]
            hls_path = edge_directory / f"hls-group-{group}.json"
            if not hls_path.exists():
                continue
            udp = load_json(udp_path)
            hls = load_json(hls_path)
            rows.append(
                {
                    "run_id": run.get("run_id"),
                    "kind": "lossless_audio",
                    "protocol": "FLAC",
                    "edge": edge,
                    "group": int(group),
                    "tracks": run.get("tracks"),
                    "channels": run.get("channels"),
                    "duration_seconds": run.get("duration_seconds"),
                    "passed": run.get("passed"),
                    "missing_units": udp.get("missing_epochs", 0)
                    + hls.get("missing_parts", 0),
                    "deadline_misses": udp.get("deadline_misses", 0)
                    + hls.get("deadline_misses", 0),
                    "direct_udp_p50_ms": udp.get("latency_ms", {}).get("p50"),
                    "direct_udp_p95_ms": udp.get("latency_ms", {}).get("p95"),
                    "llhls_p50_ms": hls.get("availability_latency_ms", {}).get("p50"),
                    "llhls_p95_ms": hls.get("availability_latency_ms", {}).get("p95"),
                    "render_p95_ms": hls.get("estimated_render_latency_ms", {}).get("p95"),
                }
            )
    return rows


def video_rows(run_directory: Path, run: dict) -> list[dict]:
    summary_path = run_directory / "summary.json"
    if not summary_path.exists():
        return []
    summary = load_json(summary_path)
    rows = []
    for edge, metrics in summary.get("edges", {}).items():
        latency = metrics.get("live_edge_latency_ms", {})
        request = metrics.get("request_ms", {})
        rows.append(
            {
                "run_id": run.get("run_id"),
                "kind": "video",
                "protocol": run.get("protocol", "").upper(),
                "edge": edge,
                "group": None,
                "tracks": None,
                "channels": None,
                "duration_seconds": run.get("duration_seconds"),
                "passed": run.get("passed"),
                "missing_units": None,
                "deadline_misses": None,
                "direct_udp_p50_ms": None,
                "direct_udp_p95_ms": None,
                "llhls_p50_ms": latency.get("median"),
                "llhls_p95_ms": latency.get("p95"),
                "render_p95_ms": request.get("p95"),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runs",
        type=Path,
        default=Path("target/multicloud-qualification/runs"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("target/multicloud-qualification/metrics"),
    )
    args = parser.parse_args()

    rows = []
    for run_path in sorted(args.runs.glob("*/run.json")):
        run = load_json(run_path)
        if run.get("schema") in {
            "needletail.multicloud-lossless-run.v2",
            "needletail.multicloud-pcm-run.v1",
        }:
            rows.extend(audio_rows(run_path.parent, run))
        elif run.get("schema") == "needletail.multicloud-video-run.v1":
            rows.extend(video_rows(run_path.parent, run))

    args.output.mkdir(parents=True, exist_ok=True)
    json_path = args.output / "metrics.json"
    csv_path = args.output / "metrics.csv"
    json_path.write_text(json.dumps({"rows": rows}, indent=2, sort_keys=True) + "\n")
    fieldnames = [
        "run_id",
        "kind",
        "protocol",
        "edge",
        "group",
        "tracks",
        "channels",
        "duration_seconds",
        "passed",
        "missing_units",
        "deadline_misses",
        "direct_udp_p50_ms",
        "direct_udp_p95_ms",
        "llhls_p50_ms",
        "llhls_p95_ms",
        "render_p95_ms",
    ]
    with csv_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(json_path)
    print(csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
