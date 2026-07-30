//! Bounded assembly of the canonical global Operations snapshot.
//!
//! Every media node remains the source of its own coarse status document. Only
//! the quorum-elected collector calls this assembler. Followers copy the
//! resulting document; they do not poll the fleet independently.

use crate::operations_controller::{OperationsControllerState, OPERATIONS_SNAPSHOT_SCHEMA};
use anyhow::{anyhow, Result};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};

const MAX_EVENTS: usize = 256;
const MAX_STREAMS: usize = 4096;

#[derive(Clone, Debug)]
pub struct NodeSnapshot {
    pub expected_node_id: String,
    pub snapshot: Option<Value>,
    pub error: Option<String>,
}

pub fn assemble_operations_snapshot(
    controller: &OperationsControllerState,
    sources: &[NodeSnapshot],
    contributor: Option<Value>,
    additional_sources: &[NodeSnapshot],
    topology_links: Vec<Value>,
    now_unix_ms: u64,
    stale_after_ms: u64,
) -> Result<Value> {
    controller
        .validate()
        .map_err(|error| anyhow!("invalid controller state: {error}"))?;
    if controller.assignment.leader_node_id.is_empty() {
        return Err(anyhow!("controller state has no leader"));
    }

    let all_sources = sources.iter().chain(additional_sources).collect::<Vec<_>>();
    let mut good = Vec::new();
    let mut stale_nodes = Vec::new();
    let mut seen_source_ids = BTreeSet::new();
    for source in &all_sources {
        if !seen_source_ids.insert(source.expected_node_id.clone()) {
            return Err(anyhow!(
                "duplicate Operations source {}",
                source.expected_node_id
            ));
        }
        let Some(snapshot) = source.snapshot.as_ref() else {
            stale_nodes.push(json!({
                "node_id": source.expected_node_id,
                "region": "",
                "updated_unix_ms": 0,
                "age_ms": stale_after_ms.saturating_add(1),
                "error": source.error,
            }));
            continue;
        };
        let reported_node_id = snapshot
            .pointer("/node/node_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if reported_node_id != source.expected_node_id {
            stale_nodes.push(json!({
                "node_id": source.expected_node_id,
                "region": "",
                "updated_unix_ms": 0,
                "age_ms": stale_after_ms.saturating_add(1),
                "error": "node identity mismatch",
            }));
            continue;
        }
        let updated = snapshot
            .get("updated_unix_ms")
            .and_then(Value::as_u64)
            .unwrap_or_default();
        let age = now_unix_ms.saturating_sub(updated);
        if updated == 0 || age > stale_after_ms {
            stale_nodes.push(json!({
                "node_id": source.expected_node_id,
                "region": snapshot.pointer("/node/region").and_then(Value::as_str).unwrap_or_default(),
                "updated_unix_ms": updated,
                "age_ms": age,
                "error": source.error,
            }));
        }
        good.push(snapshot);
    }
    if good.is_empty() {
        return Err(anyhow!("no valid Operations node snapshots are available"));
    }

    let base = good
        .iter()
        .find(|snapshot| {
            snapshot.pointer("/node/node_id").and_then(Value::as_str)
                == Some(controller.assignment.leader_node_id.as_str())
        })
        .copied()
        .unwrap_or(good[0]);
    let mut output = base
        .as_object()
        .cloned()
        .ok_or_else(|| anyhow!("base Operations snapshot is not an object"))?;

    let nodes = unique_objects(
        good.iter()
            .filter_map(|snapshot| snapshot.get("node"))
            .cloned(),
        |value| string_key(value, &["node_id"]),
        all_sources.len().saturating_add(16),
    );
    let node_ids = nodes
        .iter()
        .filter_map(|node| node.get("node_id").and_then(Value::as_str))
        .collect::<BTreeSet<_>>();
    let edge_services = unique_objects(
        good.iter()
            .flat_map(|snapshot| array_values(snapshot, "edge_services")),
        |value| string_key(value, &["node_id"]),
        sources.len(),
    );
    let relay_nodes = unique_objects(
        good.iter()
            .flat_map(|snapshot| array_values(snapshot, "relay_nodes")),
        |value| string_key(value, &["node_id"]),
        sources.len(),
    );
    let streams = unique_objects(
        good.iter()
            .flat_map(|snapshot| array_values(snapshot, "streams")),
        |value| {
            string_key(
                value,
                &[
                    "node_id",
                    "stream_id_text",
                    "stream_id",
                    "output_stream_id_text",
                ],
            )
        },
        MAX_STREAMS,
    );
    let connections = unique_objects(
        good.iter()
            .flat_map(|snapshot| array_values(snapshot, "connections")),
        |value| string_key(value, &["node_id", "peer_node_id", "peer", "connection_id"]),
        MAX_STREAMS,
    );
    let alerts = recent_unique_events(
        good.iter()
            .flat_map(|snapshot| array_values(snapshot, "alerts")),
        MAX_EVENTS,
    );
    let activity = recent_unique_events(
        good.iter()
            .flat_map(|snapshot| array_values(snapshot, "activity")),
        MAX_EVENTS,
    );

    let expected_node_count = all_sources.len();
    let fresh_count = expected_node_count
        .saturating_sub(stale_nodes.len())
        .min(node_ids.len());
    let awaiting = expected_node_count.saturating_sub(node_ids.len());
    let aggregate = aggregate_nodes(&nodes);
    let lease_remaining_ms = controller
        .assignment
        .lease_expires_unix_ms
        .saturating_sub(now_unix_ms);
    let collector = json!({
        "authority": controller.assignment.authority,
        "role": "leader",
        "leader_node_id": controller.assignment.leader_node_id,
        "leader_region": controller.assignment.leader_region,
        "term": controller.assignment.term,
        "fencing_generation": controller.assignment.fencing_generation,
        "quorum_healthy": controller.assignment.quorum_healthy,
        "voters_online": controller.assignment.voters_online,
        "voters_total": controller.assignment.voters_total,
        "lease_remaining_ms": lease_remaining_ms,
        "last_leadership_change_unix_ms": controller.leadership_acquired_unix_ms,
        "last_change_reason": "quorum election committed",
        "public_endpoint": controller.assignment.public_endpoint,
        "nodes_current": fresh_count,
        "nodes_stale": stale_nodes.len(),
        "nodes_awaiting": awaiting,
    });

    let mut orchestration = output
        .remove("orchestration")
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default();
    orchestration.insert("collector".to_owned(), collector);
    orchestration.insert("telemetry_peers".to_owned(), Value::Array(Vec::new()));
    orchestration.insert(
        "telemetry_fec".to_owned(),
        json!({
            "enabled": false,
        }),
    );
    orchestration.insert(
        "telemetry_collection".to_owned(),
        json!({
            "mode": "elected-https-pull",
            "single_fleet_poller": true,
            "sources_expected": all_sources.len(),
            "sources_current": fresh_count,
            "sources_stale": stale_nodes.len(),
            "snapshot_endpoint": controller.snapshot_endpoint,
        }),
    );

    output.insert(
        "schema".to_owned(),
        Value::String(OPERATIONS_SNAPSHOT_SCHEMA.to_owned()),
    );
    output.insert("updated_unix_ms".to_owned(), Value::from(now_unix_ms));
    output.insert("nodes".to_owned(), Value::Array(nodes));
    output.insert("edge_services".to_owned(), Value::Array(edge_services));
    output.insert("relay_nodes".to_owned(), Value::Array(relay_nodes));
    output.insert("streams".to_owned(), Value::Array(streams));
    output.insert("connections".to_owned(), Value::Array(connections));
    output.insert("alerts".to_owned(), Value::Array(alerts));
    output.insert("activity".to_owned(), Value::Array(activity));
    output.insert("aggregate".to_owned(), aggregate);
    output.insert(
        "telemetry".to_owned(),
        json!({
            "stale_after_ms": stale_after_ms,
            "fresh_remote_count": fresh_count.saturating_sub(1),
            "stale_remote_count": stale_nodes.len(),
            "stale_nodes": stale_nodes,
        }),
    );
    output.insert("orchestration".to_owned(), Value::Object(orchestration));
    output.insert("topology_links".to_owned(), Value::Array(topology_links));
    if let Some(contributor) = contributor {
        output.insert("contributor".to_owned(), contributor);
    }
    output
        .entry("publication".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    output
        .entry("delivery".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    output
        .entry("routes".to_owned())
        .or_insert_with(|| Value::Array(Vec::new()));

    Ok(Value::Object(output))
}

pub fn snapshot_matches_controller(
    snapshot: &Value,
    controller: &OperationsControllerState,
    now_unix_ms: u64,
    stale_after_ms: u64,
) -> bool {
    snapshot.get("schema").and_then(Value::as_str) == Some(OPERATIONS_SNAPSHOT_SCHEMA)
        && snapshot
            .pointer("/orchestration/collector/authority")
            .and_then(Value::as_str)
            == Some(controller.assignment.authority.as_str())
        && snapshot
            .pointer("/orchestration/collector/leader_node_id")
            .and_then(Value::as_str)
            == Some(controller.assignment.leader_node_id.as_str())
        && snapshot
            .pointer("/orchestration/collector/term")
            .and_then(Value::as_u64)
            == Some(controller.assignment.term)
        && snapshot
            .pointer("/orchestration/collector/fencing_generation")
            .and_then(Value::as_u64)
            == Some(controller.assignment.fencing_generation)
        && snapshot
            .get("updated_unix_ms")
            .and_then(Value::as_u64)
            .is_some_and(|updated| now_unix_ms.saturating_sub(updated) <= stale_after_ms)
}

fn array_values<'a>(snapshot: &'a Value, key: &str) -> impl Iterator<Item = Value> + 'a {
    snapshot
        .get(key)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .cloned()
}

fn string_key(value: &Value, fields: &[&str]) -> Option<String> {
    let mut parts = Vec::new();
    for field in fields {
        if let Some(value) = value.get(*field) {
            if let Some(text) = value.as_str() {
                if !text.is_empty() {
                    parts.push(format!("{field}={text}"));
                }
            } else if let Some(number) = value.as_u64() {
                parts.push(format!("{field}={number}"));
            }
        }
    }
    (!parts.is_empty()).then(|| parts.join("\u{1f}"))
}

fn unique_objects<I, F>(values: I, key: F, limit: usize) -> Vec<Value>
where
    I: IntoIterator<Item = Value>,
    F: Fn(&Value) -> Option<String>,
{
    let mut unique = BTreeMap::new();
    for value in values {
        let Some(key) = key(&value) else {
            continue;
        };
        unique.entry(key).or_insert(value);
        if unique.len() >= limit {
            break;
        }
    }
    unique.into_values().collect()
}

fn recent_unique_events<I>(values: I, limit: usize) -> Vec<Value>
where
    I: IntoIterator<Item = Value>,
{
    let mut events = unique_objects(
        values,
        |value| {
            string_key(
                value,
                &[
                    "node_id",
                    "code",
                    "message",
                    "seen_unix_ms",
                    "last_seen_unix_ms",
                ],
            )
        },
        limit.saturating_mul(2),
    );
    events.sort_by_key(event_time);
    if events.len() > limit {
        events.drain(..events.len() - limit);
    }
    events
}

fn event_time(value: &Value) -> u64 {
    ["seen_unix_ms", "last_seen_unix_ms", "updated_unix_ms"]
        .iter()
        .find_map(|field| value.get(*field).and_then(Value::as_u64))
        .unwrap_or_default()
}

fn aggregate_nodes(nodes: &[Value]) -> Value {
    let sum = |field: &str| {
        nodes
            .iter()
            .filter_map(|node| node.get(field).and_then(Value::as_u64))
            .fold(0_u64, u64::saturating_add)
    };
    json!({
        "node_count": nodes.len(),
        "total_storage_bytes": sum("total_storage_bytes"),
        "used_storage_bytes": sum("used_storage_bytes"),
        "total_egress_capacity_bps": sum("egress_capacity_bps"),
        "contributor_streams": sum("contributor_streams"),
        "active_streams": sum("active_streams"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::operations_controller::{OperationsControllerState, CONTROLLER_STATE_SCHEMA};
    use crate::operations_entrypoint::OperationsCollectorAssignment;

    fn controller() -> OperationsControllerState {
        OperationsControllerState {
            schema: CONTROLLER_STATE_SCHEMA.to_owned(),
            assignment: OperationsCollectorAssignment {
                schema_version: 1,
                authority: "needletail-controller".to_owned(),
                term: 8,
                fencing_generation: 8,
                leader_node_id: "edge-london".to_owned(),
                leader_region: "europe-west2".to_owned(),
                quorum_healthy: true,
                voters_online: 3,
                voters_total: 3,
                committed_at_unix_ms: 1_000,
                lease_expires_unix_ms: 31_000,
                public_endpoint: "https://ops-london.example.com/mesh".to_owned(),
            },
            snapshot_endpoint: "https://ops-london.example.com:19444/api/mesh".to_owned(),
            snapshot_address: "192.0.2.10:19444".parse().unwrap(),
            leadership_acquired_unix_ms: 1_000,
            observed_at_unix_ms: 1_000,
        }
    }

    fn source(node_id: &str, updated_unix_ms: u64) -> NodeSnapshot {
        NodeSnapshot {
            expected_node_id: node_id.to_owned(),
            snapshot: Some(json!({
                "updated_unix_ms": updated_unix_ms,
                "node": {
                    "node_id": node_id,
                    "region": "test",
                    "total_storage_bytes": 100,
                    "used_storage_bytes": 10,
                    "egress_capacity_bps": 1000,
                    "contributor_streams": 1,
                    "active_streams": 1
                },
                "nodes": [{"node_id": "duplicate-that-must-not-be-imported"}],
                "edge_services": [{"node_id": node_id}],
                "streams": [],
                "alerts": [],
                "activity": [],
                "orchestration": {}
            })),
            error: None,
        }
    }

    #[test]
    fn assembles_one_node_per_source_and_embeds_the_committed_fence() {
        let snapshot = assemble_operations_snapshot(
            &controller(),
            &[source("edge-london", 10_000), source("edge-tokyo", 10_000)],
            None,
            &[],
            vec![json!({"from_node_id": "edge-london", "to_node_id": "edge-tokyo"})],
            10_500,
            5_000,
        )
        .unwrap();
        assert_eq!(
            snapshot.get("schema").and_then(Value::as_str),
            Some(OPERATIONS_SNAPSHOT_SCHEMA)
        );
        assert_eq!(snapshot["nodes"].as_array().unwrap().len(), 2);
        assert_eq!(snapshot["aggregate"]["node_count"], 2);
        assert_eq!(
            snapshot["orchestration"]["collector"]["fencing_generation"],
            8
        );
        assert!(snapshot_matches_controller(
            &snapshot,
            &controller(),
            11_000,
            5_000
        ));
    }

    #[test]
    fn rejects_duplicate_expected_sources_and_stale_copied_snapshots() {
        let duplicate = source("edge-london", 10_000);
        assert!(assemble_operations_snapshot(
            &controller(),
            &[duplicate.clone(), duplicate],
            None,
            &[],
            Vec::new(),
            10_500,
            5_000,
        )
        .is_err());

        let snapshot = assemble_operations_snapshot(
            &controller(),
            &[source("edge-london", 10_000)],
            None,
            &[],
            Vec::new(),
            10_500,
            5_000,
        )
        .unwrap();
        assert!(!snapshot_matches_controller(
            &snapshot,
            &controller(),
            20_000,
            5_000
        ));
    }
}
