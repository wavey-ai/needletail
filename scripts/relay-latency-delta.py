#!/usr/bin/env python3
import argparse
import json
import sys
from typing import Any, Optional


PROCESSING_BOUNDS_US = [
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    25000,
    50000,
    100000,
    250000,
    500000,
    1000000,
]

PUBLICATION_BOUNDS_US = [
    1000,
    2500,
    5000,
    10000,
    25000,
    50000,
    75000,
    100000,
    125000,
    150000,
    175000,
    200000,
    250000,
    500000,
    1000000,
    2000000,
]


def as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def delta(after: int, before: int) -> int:
    return after - before if after >= before else after


def percentile(
    count: int,
    buckets: list[int],
    bounds: list[int],
    pct: int,
    max_us: int,
) -> Optional[int]:
    if count <= 0:
        return None
    rank = (count * pct + 99) // 100
    for bucket_count, bound in zip(buckets, bounds):
        if bucket_count >= rank:
            return bound
    if bounds:
        return max(bounds[-1], max_us)
    return max_us if max_us > 0 else None


def session_metric(
    node: dict[str, Any],
    before: dict[str, Any],
    prefix: str,
    bounds: list[int],
) -> dict[str, Any]:
    after_count = as_int(node.get(f"{prefix}_count"))
    before_count = as_int(before.get(f"{prefix}_count"))
    count = delta(after_count, before_count)
    after_buckets = [as_int(value) for value in node.get(f"{prefix}_buckets", [])]
    before_buckets = [as_int(value) for value in before.get(f"{prefix}_buckets", [])]
    buckets = []
    for index, after_value in enumerate(after_buckets):
        before_value = before_buckets[index] if index < len(before_buckets) else 0
        buckets.append(delta(after_value, before_value))
    if count and buckets:
        monotonic = []
        previous = 0
        for value in buckets:
            previous = max(previous, value)
            monotonic.append(previous)
        buckets = monotonic
    after_max_us = as_int(node.get(f"{prefix}_max_us"))
    return {
        "count": count,
        "sum_us": delta(
            as_int(node.get(f"{prefix}_sum_us")),
            as_int(before.get(f"{prefix}_sum_us")),
        ),
        "p50_us": percentile(count, buckets, bounds, 50, after_max_us),
        "p95_us": percentile(count, buckets, bounds, 95, after_max_us),
        "p99_us": percentile(count, buckets, bounds, 99, after_max_us),
        "after_max_us": after_max_us,
        "buckets": buckets,
    }


def root_node(snapshot: dict[str, Any]) -> dict[str, Any]:
    node = snapshot.get("node") or {}
    return {
        "role": "playback_edge",
        "node_id": node.get("node_id") or "playback-edge",
        "region": node.get("region") or "region-pending",
        "relay_session": snapshot.get("relay_session") or {},
    }


def all_nodes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [root_node(snapshot)]
    for relay_node in snapshot.get("relay_nodes") or []:
        nodes.append(
            {
                "role": "relay_node",
                "node_id": relay_node.get("node_id") or "relay",
                "region": relay_node.get("region") or "region-pending",
                "relay_session": relay_node.get("relay_session") or {},
            }
        )
    return nodes


def node_key(node: dict[str, Any]) -> str:
    return f"{node['role']}:{node['node_id']}:{node['region']}"


def metric_nodes(
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    prefix: str,
    bounds: list[int],
) -> list[dict[str, Any]]:
    before_by_key = {node_key(node): node for node in all_nodes(before_snapshot)}
    rows = []
    for node in all_nodes(after_snapshot):
        before = before_by_key.get(node_key(node), {})
        metric = session_metric(
            node["relay_session"],
            before.get("relay_session") or {},
            prefix,
            bounds,
        )
        metric.update(
            {
                "role": node["role"],
                "node_id": node["node_id"],
                "region": node["region"],
            }
        )
        rows.append(metric)
    return rows


def max_percentile(nodes: list[dict[str, Any]], field: str) -> Optional[int]:
    values = [as_int(node.get(field)) for node in nodes if node.get(field) is not None]
    return max(values) if values else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    args = parser.parse_args()

    with open(args.before, "r", encoding="utf-8") as handle:
        before = json.load(handle)
    with open(args.after, "r", encoding="utf-8") as handle:
        after = json.load(handle)

    processing_nodes = metric_nodes(
        before,
        after,
        "processing_duration",
        PROCESSING_BOUNDS_US,
    )
    publication_nodes = metric_nodes(
        before,
        after,
        "publication_to_available",
        PUBLICATION_BOUNDS_US,
    )

    json.dump(
        {
            "schema": "needletail.relay-latency-delta.v1",
            "relay_processing": {
                "nodes": processing_nodes,
                "max_p95_us": max_percentile(processing_nodes, "p95_us"),
                "max_p99_us": max_percentile(processing_nodes, "p99_us"),
            },
            "publication_to_available": {
                "nodes": publication_nodes,
                "max_p95_us": max_percentile(publication_nodes, "p95_us"),
                "max_p99_us": max_percentile(publication_nodes, "p99_us"),
            },
        },
        fp=sys.stdout,
    )
    print()


if __name__ == "__main__":
    main()
