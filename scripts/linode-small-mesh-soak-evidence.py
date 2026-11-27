#!/usr/bin/env python3
"""Condense soak and video run summaries into one versioned evidence document.

Usage: linode-small-mesh-soak-evidence.py RUN_NAME [RUN_NAME ...] >runs.json

Each RUN_NAME selects the newest run directory whose name ends with it. The
output keeps, for each run, the result of every window for each region and
lane, the continuous readers, the loss events at the distributors, the
datagram loss of each cache-mesh path, the listener-host stalls, and the
resource use of each role. Raw reports and samplers stay under target/.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import small_mesh_common as common  # noqa: E402

SETTING_KEYS = (
    "part_ms", "fmp4_part_ms", "parts_per_segment", "window_parts",
    "part_us", "segment_ms", "window_ms", "audio_part", "video_part",
    "mesh_sync_interval_ms", "mesh_symbol_size", "mesh_repair_ratio",
    "mesh_repair_symbols", "path_loss_ppm", "relay_min_repair_symbols",
    "media_deadline_ms", "extra_distributor_args", "daw_hls_packaging",
    "daw_hls_formats",
)
ROLES = ("contributor", "backbone", "distributor", "edge")


def role_usage(usage: dict, roles: dict, label: str) -> dict:
    """Resource use of each role in one window."""
    out = {}
    for role in ROLES:
        items = [windows[label] for name, windows in usage.items()
                 if roles.get(name) == role and label in windows]
        if not items:
            continue
        entry = {
            "nodes": len(items),
            "service_cpu_percent_mean": round(
                statistics.mean(item["service_cpu_percent"] for item in items), 1),
            "service_cpu_percent_max": max(item["service_cpu_percent"] for item in items),
            "host_cpu_percent_max": max(item["host_cpu_percent"] for item in items),
            "steal_percent_max": max(item["steal_percent"] for item in items),
            "memory_mb_max": max(item["memory_mb"] for item in items),
            "public_tx_mbps_mean": round(
                statistics.mean(item["public_tx_kbps"] for item in items) / 1000, 2),
            "vlan_tx_mbps_mean": round(
                statistics.mean(item["vlan_tx_kbps"] for item in items) / 1000, 2),
            "udp_receive_errors": sum(item.get("udp_receive_errors", 0) for item in items),
            "service_restarts": sum(item["restarts_during_run"] for item in items),
            "clock_offset_ms_abs_max": max(
                abs(item.get("clock_offset_ms", 0)) for item in items),
        }
        relay = [item["relay_session"] for item in items if item.get("relay_session")]
        if relay and role in ("distributor", "backbone"):
            entry["relay_fec_recovered_objects"] = sum(
                item.get("fec_recovered_objects", 0) for item in relay)
            entry["relay_expired_objects"] = sum(
                item.get("expired_objects", 0) for item in relay)
        mesh = [item["cache_mesh"] for item in items if item.get("cache_mesh")]
        if mesh and role == "edge":
            entry["cache_mesh_repaired_objects"] = sum(
                item.get("rx_repaired_objects", 0) for item in mesh)
            entry["cache_mesh_expired_objects"] = sum(
                item.get("rx_expired_objects", 0) for item in mesh)
        skips = [item["gap_skip"] for item in items if item.get("gap_skip")]
        if skips and role == "distributor":
            entry["gap_skips"] = sum(item["skips"] for item in skips)
            entry["gap_skipped_parts"] = sum(item["skipped_parts"] for item in skips)
        if role == "edge":
            entry["hole_repair_requests"] = sum(
                item.get("edge_hole_repair_requests") or 0 for item in items)
        out[role] = entry
    return out


def condense(run_dir: pathlib.Path) -> dict:
    summary = json.loads((run_dir / "summary.json").read_text())
    plan = json.loads((run_dir / "plan.json").read_text())
    roles = {name: node["role"] for name, node in plan["nodes"].items()}
    labels = [f"w{window['window']:02d}" for window in summary["windows"]]
    out = {
        "run_id": summary["run_id"], "name": summary["name"], "label": summary["label"],
        "profile": plan.get("profile", "audio"),
        "node_count": summary["node_count"], "hourly_usd": summary["hourly_usd"],
        "duration_seconds": summary["duration_seconds"],
        "window_seconds": summary["window_seconds"],
        "media_deadline_ms": summary["media_deadline_ms"] - plan.get("latency_guard_ms", 0),
        "many_reader_deadline_ms": (summary["load_deadline_ms"]
                                    - plan.get("latency_guard_ms", 0)),
        "readers_per_loaded_edge": summary["readers_per_loaded_edge"],
        "loaded_edges_per_site": summary["loaded_edges_per_site"],
        "settings": {key: summary["settings"].get(key) for key in SETTING_KEYS},
        "pass_rule": summary["pass_rule"], "passed": summary["passed"],
        "failures": summary["failures"][:40],
        "timeline_warnings": summary.get("timeline_warnings", [])[:20],
        "windows": [{"window": window["window"], "passed": window["passed"],
                     "sites": window["sites"],
                     "usage": role_usage(summary["usage"], roles, label)}
                    for window, label in zip(summary["windows"], labels)],
        "continuous_readers": summary["continuous"],
        "blackout": summary.get("blackout"),
        "loss_events_at_distributors": {
            "count": len(summary["loss_events"]),
            "missing_epochs": sum(event["missing_epochs"]
                                  for event in summary["loss_events"]),
            "by_cause": {}, "by_site": {},
            "edge_parts_missing_during_events": sum(
                event["edge_missing_parts"] for event in summary["loss_events"]),
            "edge_parts_late_during_events": sum(
                event["edge_late_parts"] for event in summary["loss_events"]),
            "events": summary["loss_events"][:300],
        },
        "gap_skip_log_lines": {name: item["logged_releases"]
                               for name, item in summary["gap_skips"].items()},
        "cache_mesh_paths": summary.get("cache_mesh_paths", []),
        "listener_host_stalls": {
            name: {"stalls": item["stalls"], "longest_ms": item["longest_ms"],
                   "offsets_s": item["offsets_s"][:40]}
            for name, item in summary.get("listener_host_stalls", {}).items()},
    }
    events = out["loss_events_at_distributors"]
    for event in summary["loss_events"]:
        events["by_cause"][event["cause"]] = events["by_cause"].get(event["cause"], 0) + 1
        events["by_site"][event["site"]] = events["by_site"].get(event["site"], 0) + 1
    if plan.get("profile") == "video":
        out["video"] = {key: plan.get(key) for key in (
            "clip", "audio", "ingest", "calibration", "first_offset")}
        out["video"]["clip"] = pathlib.Path(plan["clip"]).name
    return out


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    runs = []
    for name in sys.argv[1:]:
        matches = sorted(path for path in common.RUNS_DIR.glob(f"*-{name}")
                         if path.name.split("-", 1)[1] == name)
        if not matches:
            sys.exit(f"no run directory ends with -{name}")
        runs.append(condense(matches[-1]))
    json.dump({"runs": runs}, sys.stdout, indent=2)
    print(f"condensed {len(runs)} runs", file=sys.stderr)


if __name__ == "__main__":
    main()
