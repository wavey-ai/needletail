#!/usr/bin/env python3
"""Export current multicloud qualification results as lane-specific metrics."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


LOSSLESS_RUN_SCHEMA = "needletail.multicloud-lossless-run.v9"
VIDEO_RUN_SCHEMA = "needletail.multicloud-video-run.v1"
VIDEO_SUMMARY_SCHEMA = "needletail.multicloud-video-summary.v1"
UDP_REPORT_SCHEMA = "needletail.aep1-48k-probe.receive.v2"
HLS_REPORT_SCHEMA = "needletail.aep1-48k-probe.hls-receive.v6"

FIELDNAMES = [
    "run_id",
    "kind",
    "protocol",
    "format",
    "edge",
    "group",
    "tracks",
    "channels",
    "duration_seconds",
    "passed",
    "unit",
    "expected_units",
    "received_units",
    "missing_units",
    "deadline_misses",
    "direct_udp_p50_ms",
    "direct_udp_p95_ms",
    "llhls_p50_ms",
    "llhls_p95_ms",
    "render_p95_ms",
]


class ArtifactError(ValueError):
    """A qualification artifact cannot safely contribute a metric row."""


def reject_nonstandard_json_constant(value: str) -> None:
    raise ArtifactError(f"non-standard JSON numeric constant {value!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runs", type=Path, default=Path("target/multicloud-qualification/runs"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("target/multicloud-qualification/metrics"),
    )
    return parser.parse_args()


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ArtifactError("artifact is missing") from error
    except OSError as error:
        raise ArtifactError(f"artifact could not be read: {error}") from error
    if not source.strip():
        raise ArtifactError("artifact is empty")
    try:
        value = json.loads(source, parse_constant=reject_nonstandard_json_constant)
    except json.JSONDecodeError as error:
        raise ArtifactError(
            f"invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    if not isinstance(value, dict):
        raise ArtifactError("top-level JSON value is not an object")
    return value


def required_value(document: dict[str, Any], field: str) -> Any:
    if field not in document:
        raise ArtifactError(f"missing required field {field!r}")
    return document[field]


def required_nonnegative_integer(document: dict[str, Any], field: str) -> int:
    value = required_value(document, field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactError(f"field {field!r} is not a nonnegative integer")
    return value


def required_positive_integer(document: dict[str, Any], field: str) -> int:
    value = required_nonnegative_integer(document, field)
    if value == 0:
        raise ArtifactError(f"field {field!r} is not a positive integer")
    return value


def required_boolean(document: dict[str, Any], field: str) -> bool:
    value = required_value(document, field)
    if not isinstance(value, bool):
        raise ArtifactError(f"field {field!r} is not a boolean")
    return value


def required_string(document: dict[str, Any], field: str) -> str:
    value = required_value(document, field)
    if not isinstance(value, str) or not value:
        raise ArtifactError(f"field {field!r} is not a nonempty string")
    return value


def required_percentile(
    document: dict[str, Any], summary_field: str, percentile: str
) -> int | float | None:
    summary = required_value(document, summary_field)
    if not isinstance(summary, dict):
        raise ArtifactError(f"field {summary_field!r} is not an object")
    value = required_value(summary, percentile)
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ArtifactError(
                f"field {summary_field!r}.{percentile} is not numeric or null"
            )
        if not math.isfinite(value) or value < 0:
            raise ArtifactError(
                f"field {summary_field!r}.{percentile} is not finite and nonnegative"
            )
    return value


def validate_schema(
    document: dict[str, Any], expected_schema: str, artifact_name: str
) -> None:
    schema = required_string(document, "schema")
    if schema != expected_schema:
        raise ArtifactError(
            f"{artifact_name} schema is {schema!r}; expected {expected_schema!r}"
        )


def outcome_selected(run: dict[str, Any], outcome_name: str) -> bool:
    outcomes = required_value(run, "outcomes")
    if not isinstance(outcomes, dict):
        raise ArtifactError("field 'outcomes' is not an object")
    outcome = outcomes.get(outcome_name)
    if not isinstance(outcome, dict):
        raise ArtifactError(f"missing required outcome {outcome_name!r}")
    return required_boolean(outcome, "selected")


def outcome_passed(run: dict[str, Any], outcome_name: str) -> bool:
    outcomes = required_value(run, "outcomes")
    if not isinstance(outcomes, dict):
        raise ArtifactError("field 'outcomes' is not an object")
    outcome = outcomes.get(outcome_name)
    if not isinstance(outcome, dict):
        raise ArtifactError(f"missing required outcome {outcome_name!r}")
    return required_boolean(outcome, "passed")


def required_unit_counts(
    report: dict[str, Any],
    expected_field: str,
    received_field: str,
    missing_field: str,
) -> tuple[int, int, int]:
    expected = required_nonnegative_integer(report, expected_field)
    received = required_nonnegative_integer(report, received_field)
    missing = required_nonnegative_integer(report, missing_field)
    if received > expected or missing != expected - received:
        raise ArtifactError(
            f"fields {received_field!r} and {missing_field!r} "
            f"do not reconcile with {expected_field!r}"
        )
    return expected, received, missing


def report_diagnostic(path: Path) -> str:
    error_path = path.with_suffix(".err")
    if not error_path.is_file():
        return ""
    try:
        return error_path.read_text(encoding="utf-8").strip()[:4096]
    except OSError:
        return ""


def relative_artifact(path: Path, runs_directory: Path) -> str:
    try:
        return str(path.relative_to(runs_directory))
    except ValueError:
        return str(path)


def quarantine_artifact(
    quarantine: list[dict[str, Any]],
    runs_directory: Path,
    run_directory: Path,
    run_id: str | None,
    path: Path,
    error: ArtifactError,
) -> None:
    entry = {
        "run_id": run_id or run_directory.name,
        "artifact": relative_artifact(path, runs_directory),
        "reason": str(error),
    }
    diagnostic = report_diagnostic(path)
    if diagnostic:
        entry["diagnostic"] = diagnostic
    quarantine.append(entry)


def common_audio_row(
    run: dict[str, Any],
    edge: str,
    group: int,
    protocol: str,
    audio_format: str,
    passed: bool,
) -> dict[str, Any]:
    return {
        "run_id": required_string(run, "run_id"),
        "kind": "lossless_audio",
        "protocol": protocol,
        "format": audio_format,
        "edge": edge,
        "group": group,
        "tracks": required_positive_integer(run, "tracks"),
        "channels": required_positive_integer(run, "channels"),
        "duration_seconds": required_positive_integer(run, "duration_seconds"),
        "passed": passed,
    }


def udp_row(
    run: dict[str, Any], edge: str, group: int, report: dict[str, Any]
) -> dict[str, Any]:
    validate_schema(report, UDP_REPORT_SCHEMA, "UDP report")
    formats = required_value(report, "formats")
    if not isinstance(formats, list) or "flac" not in formats:
        raise ArtifactError("UDP report does not contain the FLAC format")
    expected, received, missing = required_unit_counts(
        report, "expected_epochs", "received_epochs", "missing_epochs"
    )
    row = common_audio_row(
        run, edge, group, "UDP/FEC", "FLAC", outcome_passed(run, "mesh")
    )
    row.update(
        {
            "unit": "epoch",
            "expected_units": expected,
            "received_units": received,
            "missing_units": missing,
            "deadline_misses": required_nonnegative_integer(report, "deadline_misses"),
            "direct_udp_p50_ms": required_percentile(report, "latency_ms", "p50"),
            "direct_udp_p95_ms": required_percentile(report, "latency_ms", "p95"),
            "llhls_p50_ms": None,
            "llhls_p95_ms": None,
            "render_p95_ms": required_percentile(
                report, "render_ready_latency_ms", "p95"
            ),
        }
    )
    return row


def hls_row(
    run: dict[str, Any],
    edge: str,
    group: int,
    audio_format: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    validate_schema(report, HLS_REPORT_SCHEMA, "LL-HLS report")
    expected_codec = required_string(report, "expected_audio_codec")
    if expected_codec.lower() != audio_format.lower():
        raise ArtifactError(
            f"LL-HLS report codec is {expected_codec!r}; "
            f"expected {audio_format.lower()!r}"
        )
    expected, received, missing = required_unit_counts(
        report, "expected_parts", "received_parts", "missing_parts"
    )
    outcome_name = (
        "playback_flac" if audio_format.lower() == "flac" else "playback_opus"
    )
    row = common_audio_row(
        run, edge, group, "LL-HLS", audio_format, outcome_passed(run, outcome_name),
    )
    row.update(
        {
            "unit": "part",
            "expected_units": expected,
            "received_units": received,
            "missing_units": missing,
            "deadline_misses": required_nonnegative_integer(report, "deadline_misses"),
            "direct_udp_p50_ms": None,
            "direct_udp_p95_ms": None,
            "llhls_p50_ms": required_percentile(
                report, "availability_latency_ms", "p50"
            ),
            "llhls_p95_ms": required_percentile(
                report, "availability_latency_ms", "p95"
            ),
            "render_p95_ms": required_percentile(
                report, "estimated_render_latency_ms", "p95"
            ),
        }
    )
    return row


def add_audio_artifact(
    rows: list[dict[str, Any]],
    quarantine: list[dict[str, Any]],
    runs_directory: Path,
    run_directory: Path,
    run: dict[str, Any],
    edge: str,
    group: int,
    path: Path,
    audio_format: str | None,
) -> None:
    try:
        report = load_json_object(path)
        if audio_format is None:
            row = udp_row(run, edge, group, report)
        else:
            row = hls_row(run, edge, group, audio_format, report)
        rows.append(row)
    except ArtifactError as error:
        quarantine_artifact(
            quarantine, runs_directory, run_directory, run.get("run_id"), path, error,
        )


def audio_rows(
    runs_directory: Path,
    run_directory: Path,
    run: dict[str, Any],
    quarantine: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    validate_schema(run, LOSSLESS_RUN_SCHEMA, "lossless run")
    required_string(run, "run_id")
    required_positive_integer(run, "tracks")
    required_positive_integer(run, "channels")
    required_positive_integer(run, "duration_seconds")
    required_boolean(run, "passed")
    group_count = required_positive_integer(run, "group_count")
    mesh_selected = outcome_selected(run, "mesh")
    playback_flac_selected = outcome_selected(run, "playback_flac")
    playback_opus_selected = outcome_selected(run, "playback_opus")
    edge_directories = sorted(
        path for path in run_directory.glob("edge-*") if path.is_dir()
    )
    if not edge_directories:
        raise ArtifactError("lossless run does not contain any edge directories")

    rows: list[dict[str, Any]] = []
    for edge_directory in edge_directories:
        edge = edge_directory.name
        for group in range(group_count):
            if mesh_selected:
                add_audio_artifact(
                    rows,
                    quarantine,
                    runs_directory,
                    run_directory,
                    run,
                    edge,
                    group,
                    edge_directory / f"udp-group-{group}.json",
                    None,
                )
            if playback_flac_selected:
                add_audio_artifact(
                    rows,
                    quarantine,
                    runs_directory,
                    run_directory,
                    run,
                    edge,
                    group,
                    edge_directory / f"hls-track-{group}.json",
                    "FLAC",
                )
            if playback_opus_selected:
                add_audio_artifact(
                    rows,
                    quarantine,
                    runs_directory,
                    run_directory,
                    run,
                    edge,
                    group,
                    edge_directory / f"hls-opus-track-{group}.json",
                    "Opus",
                )
    return rows


def video_rows(run_directory: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    validate_schema(run, VIDEO_RUN_SCHEMA, "video run")
    summary = load_json_object(run_directory / "summary.json")
    validate_schema(summary, VIDEO_SUMMARY_SCHEMA, "video summary")
    edges = required_value(summary, "edges")
    if not isinstance(edges, dict) or not edges:
        raise ArtifactError("video summary does not contain edge metrics")
    rows = []
    for edge, metrics in sorted(edges.items()):
        if not isinstance(metrics, dict):
            raise ArtifactError(f"video metrics for {edge!r} are not an object")
        rows.append(
            {
                "run_id": required_string(run, "run_id"),
                "kind": "video",
                "protocol": required_string(run, "protocol").upper(),
                "format": None,
                "edge": edge,
                "group": None,
                "tracks": None,
                "channels": None,
                "duration_seconds": required_positive_integer(run, "duration_seconds"),
                "passed": required_boolean(run, "passed"),
                "unit": None,
                "expected_units": None,
                "received_units": None,
                "missing_units": None,
                "deadline_misses": None,
                "direct_udp_p50_ms": None,
                "direct_udp_p95_ms": None,
                "llhls_p50_ms": required_percentile(
                    metrics, "live_edge_latency_ms", "median"
                ),
                "llhls_p95_ms": required_percentile(
                    metrics, "live_edge_latency_ms", "p95"
                ),
                "render_p95_ms": required_percentile(metrics, "request_ms", "p95"),
            }
        )
    return rows


def atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def csv_contents(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDNAMES, lineterminator="\n",)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def main() -> int:
    args = parse_args()
    rows: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    skipped_runs: list[dict[str, str]] = []
    run_directories = (
        sorted(path for path in args.runs.iterdir() if path.is_dir())
        if args.runs.is_dir()
        else []
    )
    for run_directory in run_directories:
        run_path = run_directory / "run.json"
        try:
            run = load_json_object(run_path)
        except ArtifactError as error:
            quarantine_artifact(
                quarantine, args.runs, run_directory, None, run_path, error,
            )
            continue

        schema = run.get("schema")
        try:
            if schema == LOSSLESS_RUN_SCHEMA:
                rows.extend(audio_rows(args.runs, run_directory, run, quarantine,))
            elif schema == VIDEO_RUN_SCHEMA:
                rows.extend(video_rows(run_directory, run))
            elif isinstance(schema, str):
                skipped_runs.append(
                    {
                        "run_id": str(run.get("run_id") or run_directory.name),
                        "schema": schema,
                    }
                )
            else:
                raise ArtifactError("run does not declare a schema")
        except ArtifactError as error:
            artifact = (
                run_directory / "summary.json"
                if schema == VIDEO_RUN_SCHEMA
                else run_path
            )
            quarantine_artifact(
                quarantine,
                args.runs,
                run_directory,
                run.get("run_id"),
                artifact,
                error,
            )

    metrics_report = {
        "schema": "needletail.multicloud-qualification-metrics.v1",
        "complete": not quarantine,
        "quarantined_artifacts": len(quarantine),
        "skipped_runs": skipped_runs,
        "rows": rows,
    }
    quarantine_report = {
        "schema": "needletail.multicloud-qualification-quarantine.v1",
        "artifact_count": len(quarantine),
        "artifacts": quarantine,
    }

    json_path = args.output / "metrics.json"
    csv_path = args.output / "metrics.csv"
    quarantine_path = args.output / "quarantine.json"
    atomic_write_text(
        json_path, json.dumps(metrics_report, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(csv_path, csv_contents(rows))
    atomic_write_text(
        quarantine_path, json.dumps(quarantine_report, indent=2, sort_keys=True) + "\n",
    )

    print(json_path)
    print(csv_path)
    print(quarantine_path)
    if quarantine:
        print(
            f"warning: quarantined {len(quarantine)} incomplete artifact(s); "
            f"see {quarantine_path}",
            file=sys.stderr,
        )
    if skipped_runs:
        print(
            f"note: skipped {len(skipped_runs)} run(s) with unsupported schemas",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
