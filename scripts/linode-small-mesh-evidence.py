#!/usr/bin/env python3
"""Condense small-mesh run summaries into one versioned evidence document.

Usage: linode-small-mesh-evidence.py OUTPUT.json RUN_NAME [RUN_NAME ...]

Each RUN_NAME selects the newest run directory whose name ends with it. The
output keeps per-region, per-lane delivery and latency aggregates and the
resource figures that the narrative quotes; raw reports stay under target/.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import small_mesh_common as common  # noqa: E402

LANES = (
    ("distributor", "udp", "native_udp_fec"),
    ("distributor", "webtransport", "webtransport"),
    ("distributor", "hls-local", "ll_hls_distributor"),
    ("edge", "hls-local", "ll_hls_edge"),
    ("load", "hls-vlan", "ll_hls_edge_over_vlan"),
    ("load", "load-", "ll_hls_many_readers"),
)
SETTING_KEYS = (
    "part_ms", "fmp4_part_ms", "part_us", "segment_ms", "window_ms", "audio_part",
    "video_part", "mesh_sync_interval_ms", "mesh_symbol_size",
    "mesh_repair_ratio", "mesh_repair_symbols", "path_loss_ppm",
    "relay_min_repair_symbols", "window_parts", "response_ms",
    "extra_distributor_args", "daw_hls_packaging", "daw_hls_formats",
)


def lane_aggregate(nodes: dict, site: str, role: str, prefix: str) -> dict | None:
    reports = [
        report for entry in nodes.values()
        if entry["role"] == role and entry["site"] == site
        for name, report in entry["reports"].items() if name.startswith(prefix)
    ]
    if not reports:
        return None
    good = [report for report in reports
            if "error" not in report and report.get("latency_ms")]
    aggregate = {
        "readers": len(reports),
        "reader_errors": len(reports) - len(good),
        "expected": sum(report.get("expected") or 0 for report in good),
        "received": sum(report.get("received") or 0 for report in good),
        "deadline_misses": sum(report.get("deadline_misses") or 0 for report in good),
        "integrity_mismatches": sum(
            report.get("integrity_mismatches") or 0 for report in good),
    }
    aggregate["missing"] = aggregate["expected"] - aggregate["received"]
    if good:
        aggregate["latency_ms"] = {
            "p50_mean": round(statistics.mean(r["latency_ms"]["p50"] for r in good), 3),
            "p95_worst": round(max(r["latency_ms"]["p95"] for r in good), 3),
            "p99_worst": round(max(r["latency_ms"]["p99"] for r in good), 3),
            "max": round(max(r["latency_ms"]["max"] for r in good), 3),
        }
    if prefix == "load-":
        aggregate["concurrent_readers"] = sum(r.get("readers") or 0 for r in good)
    return aggregate


def role_usage(nodes: dict, role: str) -> dict | None:
    usages = [entry["usage"] for entry in nodes.values()
              if entry["role"] == role and "usage" in entry]
    if not usages:
        return None
    return {
        "nodes": len(usages),
        "service_cpu_percent_mean": round(
            statistics.mean(u["service_cpu_percent"] for u in usages), 2),
        "service_cpu_percent_max": max(u["service_cpu_percent"] for u in usages),
        # Runs recorded before the host-CPU column existed have no host figure.
        "host_cpu_percent_max": max(
            (u["host_cpu_percent"] for u in usages if "host_cpu_percent" in u),
            default=None),
        "steal_percent_max": max(u["steal_percent"] for u in usages),
        "memory_mb_max": max(u["memory_mb"] for u in usages),
        "public_tx_mbps_mean": round(
            statistics.mean(u["public_tx_kbps"] for u in usages) / 1000, 3),
        "vlan_tx_mbps_mean": round(
            statistics.mean(u["vlan_tx_kbps"] for u in usages) / 1000, 3),
        "vlan_rx_mbps_mean": round(
            statistics.mean(u["vlan_rx_kbps"] for u in usages) / 1000, 3),
        "udp_receive_errors": sum(u.get("udp_receive_errors", 0) for u in usages),
        "clock_offset_ms_abs_max": max(
            abs(u.get("clock_offset_ms", 0)) for u in usages),
        "service_restarts": sum(u["restarts_during_run"] for u in usages),
    }


def condense(summary: dict) -> dict:
    nodes = summary["nodes"]
    sites = sorted({entry["site"] for entry in nodes.values()})
    source = next((entry.get("source") for entry in nodes.values()
                   if entry.get("source")), None)
    out = {
        "run_id": summary["run_id"], "name": summary["name"],
        "label": summary["label"], "node_count": summary["node_count"],
        "hourly_usd": summary["hourly_usd"], "source": summary["source"],
        "tracks": summary["tracks"],
        "duration_seconds": summary["duration_seconds"],
        "loss": {key: summary["loss"][key]
                 for key in ("relay_percent", "mesh_percent")},
        "failover": summary.get("failover"),
        "readers_per_edge": summary["readers_per_edge"],
        "settings": {key: summary["settings"].get(key) for key in SETTING_KEYS},
        "sites": {}, "usage": {},
    }
    if source and source.get("lossless_payload_bytes"):
        out["lossless_payload_mbps"] = round(
            source["lossless_payload_bytes"] * 8 / summary["duration_seconds"] / 1e6, 3)
    for site in sites:
        lanes = {}
        for role, prefix, lane in LANES:
            aggregate = lane_aggregate(nodes, site, role, prefix)
            if aggregate:
                lanes[lane] = aggregate
        if lanes:
            out["sites"][site] = lanes
    for role in ("contributor", "backbone", "distributor", "edge", "load"):
        usage = role_usage(nodes, role)
        if usage:
            out["usage"][role] = usage
    totals = [lane for lanes in out["sites"].values() for lane in lanes.values()]
    out["totals"] = {
        "readers": sum(lane["readers"] for lane in totals),
        "reader_errors": sum(lane["reader_errors"] for lane in totals),
        "expected": sum(lane["expected"] for lane in totals),
        "received": sum(lane["received"] for lane in totals),
        "missing": sum(lane["missing"] for lane in totals),
        "deadline_misses": sum(lane["deadline_misses"] for lane in totals),
        "integrity_mismatches": sum(lane["integrity_mismatches"] for lane in totals),
    }
    return out


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    output = pathlib.Path(sys.argv[1])
    runs = []
    for name in sys.argv[2:]:
        # Exact name match: "loss-2pct" must not select "global-loss-2pct".
        matches = sorted(
            path for path in common.RUNS_DIR.glob(f"*-{name}")
            if path.name.split("-", 1)[1] == name)
        if not matches:
            sys.exit(f"no run directory ends with -{name}")
        runs.append(condense(json.loads((matches[-1] / "summary.json").read_text())))
    json.dump({"runs": runs}, sys.stdout if str(output) == "-" else output.open("w"),
              indent=2)
    print(f"condensed {len(runs)} runs", file=sys.stderr)


if __name__ == "__main__":
    main()
