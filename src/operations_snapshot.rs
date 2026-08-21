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
    let publication = canonical_publication_summary(output.get("publication"), &streams);
    let connections = unique_objects(
        good.iter()
            .flat_map(|snapshot| array_values(snapshot, "connections")),
        |value| string_key(value, &["node_id", "peer_node_id", "peer", "connection_id"]),
        MAX_STREAMS,
    );
    let link_observations = unique_objects(
        good.iter()
            .flat_map(|snapshot| array_values(snapshot, "link_observations")),
        |value| string_key(value, &["from_node_id", "to_node_id", "role"]),
        MAX_STREAMS,
    );
    let topology_links = enrich_topology_links(topology_links, &link_observations, now_unix_ms);
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
    output.insert(
        "link_observations".to_owned(),
        Value::Array(link_observations),
    );
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
        output.insert(
            "contributor".to_owned(),
            canonical_contributor_snapshot(contributor),
        );
    }
    output.insert("publication".to_owned(), publication);
    output
        .entry("delivery".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    output
        .entry("routes".to_owned())
        .or_insert_with(|| Value::Array(Vec::new()));

    Ok(Value::Object(output))
}

fn enrich_topology_links(
    topology_links: Vec<Value>,
    observations: &[Value],
    now_unix_ms: u64,
) -> Vec<Value> {
    let observations = observations
        .iter()
        .filter_map(|observation| {
            let key = string_key(observation, &["from_node_id", "to_node_id", "role"])?;
            Some((key, observation))
        })
        .collect::<BTreeMap<_, _>>();

    topology_links
        .into_iter()
        .map(|mut link| {
            let Some(object) = link.as_object_mut() else {
                return link;
            };
            let Some(key) = string_key(
                &Value::Object(object.clone()),
                &["from_node_id", "to_node_id", "role"],
            ) else {
                return link;
            };
            let Some(observation) = observations.get(&key).and_then(|value| value.as_object())
            else {
                object.insert(
                    "reporting".to_owned(),
                    Value::String("unreported".to_owned()),
                );
                return link;
            };
            let observed_unix_ms = observation
                .get("observed_unix_ms")
                .and_then(Value::as_u64)
                .unwrap_or_default();
            let fresh_for_ms = observation
                .get("fresh_for_ms")
                .and_then(Value::as_u64)
                .unwrap_or_default();
            let fresh = observed_unix_ms > 0
                && fresh_for_ms > 0
                && now_unix_ms.saturating_sub(observed_unix_ms) <= fresh_for_ms;
            object.insert(
                "reporting".to_owned(),
                Value::String(if fresh { "reported" } else { "stale" }.to_owned()),
            );
            object.insert("observed_unix_ms".to_owned(), Value::from(observed_unix_ms));
            if fresh {
                for field in [
                    "state",
                    "method",
                    "rtt_ms",
                    "jitter_ms",
                    "loss_percent",
                    "throughput_bps",
                    "received_bytes_total",
                    "sample_count",
                ] {
                    if let Some(value) = observation.get(field) {
                        object.insert(field.to_owned(), value.clone());
                    }
                }
            }
            link
        })
        .collect()
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

fn canonical_publication_summary(existing: Option<&Value>, streams: &[Value]) -> Value {
    let mut publication = existing
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if publication_fields_complete(&publication) || !one_stream_identity(streams) {
        return Value::Object(publication);
    }

    insert_if_missing(
        &mut publication,
        "canonical_epoch",
        common_u64(streams, "canonical_epoch"),
    );
    insert_if_missing(
        &mut publication,
        "canonical_epoch_activation_delay_us",
        complete_u64_values(streams, "canonical_epoch_activation_delay_us")
            .and_then(|values| values.into_iter().max()),
    );
    insert_if_missing(
        &mut publication,
        "contiguous_object",
        complete_u64_values(streams, "contiguous_object")
            .and_then(|values| values.into_iter().min()),
    );
    insert_if_missing(
        &mut publication,
        "head_object",
        complete_u64_values(streams, "head_object").and_then(|values| values.into_iter().max()),
    );
    insert_if_missing(
        &mut publication,
        "gap_count",
        complete_u64_values(streams, "gap_count").and_then(|values| values.into_iter().max()),
    );
    Value::Object(publication)
}

fn canonical_contributor_snapshot(mut contributor: Value) -> Value {
    let Some(object) = contributor.as_object_mut() else {
        return contributor;
    };
    let mut publication = object
        .get("publication")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let streams = object
        .get("runtime")
        .and_then(|runtime| runtime.get("streams"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    if streams.len() == 1 {
        insert_if_missing(
            &mut publication,
            "canonical_epoch",
            object
                .get("mesh")
                .and_then(|mesh| mesh.get("media_object_source_epoch"))
                .and_then(Value::as_u64),
        );
        let head = streams[0]
            .get("latest_fmp4_sequence")
            .and_then(Value::as_u64);
        insert_if_missing(&mut publication, "head_object", head);
        insert_if_missing(&mut publication, "contiguous_object", head);
        insert_if_missing(&mut publication, "gap_count", head.map(|_| 0));
    }

    object.insert("publication".to_owned(), Value::Object(publication));
    contributor
}

fn publication_fields_complete(publication: &Map<String, Value>) -> bool {
    [
        "canonical_epoch",
        "canonical_epoch_activation_delay_us",
        "contiguous_object",
        "head_object",
        "gap_count",
    ]
    .iter()
    .all(|field| publication.get(*field).and_then(Value::as_u64).is_some())
}

fn one_stream_identity(streams: &[Value]) -> bool {
    let identities = streams
        .iter()
        .map(|stream| {
            stream
                .get("stream_id_text")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
                .or_else(|| {
                    stream
                        .get("stream_id")
                        .and_then(Value::as_u64)
                        .map(|value| value.to_string())
                })
        })
        .collect::<Option<Vec<_>>>();
    let Some(identities) = identities else {
        return false;
    };
    !identities.is_empty() && identities.into_iter().collect::<BTreeSet<_>>().len() == 1
}

fn complete_u64_values(streams: &[Value], field: &str) -> Option<Vec<u64>> {
    let values = streams
        .iter()
        .filter_map(|stream| stream.get(field).and_then(Value::as_u64))
        .collect::<Vec<_>>();
    (!streams.is_empty() && values.len() == streams.len()).then_some(values)
}

fn common_u64(streams: &[Value], field: &str) -> Option<u64> {
    let values = complete_u64_values(streams, field)?;
    let first = values[0];
    values.iter().all(|value| *value == first).then_some(first)
}

fn insert_if_missing(publication: &mut Map<String, Value>, field: &str, value: Option<u64>) {
    if publication.get(field).and_then(Value::as_u64).is_none() {
        if let Some(value) = value {
            publication.insert(field.to_owned(), Value::from(value));
        }
    }
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
                "link_observations": [],
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
    fn joins_fresh_link_observations_into_configured_topology() {
        let mut child = source("edge-tokyo", 10_000);
        child.snapshot.as_mut().unwrap()["link_observations"] = json!([{
            "from_node_id": "relay-osaka",
            "to_node_id": "edge-tokyo",
            "role": "primary",
            "state": "measured",
            "method": "icmp_and_udp_ingress",
            "rtt_ms": 8.25,
            "jitter_ms": 0.5,
            "loss_percent": 0.0,
            "throughput_bps": 1_500_000,
            "received_bytes_total": 12_000,
            "sample_count": 10,
            "observed_unix_ms": 10_400,
            "fresh_for_ms": 10_000
        }]);
        let snapshot = assemble_operations_snapshot(
            &controller(),
            &[source("edge-london", 10_000), child],
            None,
            &[],
            vec![json!({
                "from_node_id": "relay-osaka",
                "to_node_id": "edge-tokyo",
                "role": "primary",
                "state": "configured"
            })],
            10_500,
            5_000,
        )
        .unwrap();

        let link = &snapshot["topology_links"][0];
        assert_eq!(link["reporting"], "reported");
        assert_eq!(link["state"], "measured");
        assert_eq!(link["rtt_ms"], 8.25);
        assert_eq!(link["throughput_bps"], 1_500_000);
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

    #[test]
    fn derives_single_stream_publication_summaries_from_canonical_rows() {
        let edge_streams = vec![
            json!({
                "node_id": "edge-london",
                "stream_id_text": "1",
                "canonical_epoch": 42,
                "canonical_epoch_activation_delay_us": 180_000,
                "contiguous_object": 98,
                "head_object": 100,
                "gap_count": 2
            }),
            json!({
                "node_id": "edge-tokyo",
                "stream_id_text": "1",
                "canonical_epoch": 42,
                "canonical_epoch_activation_delay_us": 220_000,
                "contiguous_object": 99,
                "head_object": 101,
                "gap_count": 1
            }),
        ];
        let publication = canonical_publication_summary(None, &edge_streams);
        assert_eq!(publication["canonical_epoch"], 42);
        assert_eq!(publication["canonical_epoch_activation_delay_us"], 220_000);
        assert_eq!(publication["contiguous_object"], 98);
        assert_eq!(publication["head_object"], 101);
        assert_eq!(publication["gap_count"], 2);

        let contributor = canonical_contributor_snapshot(json!({
            "mesh": {"media_object_source_epoch": 42},
            "runtime": {
                "streams": [{
                    "stream_id_text": "1",
                    "latest_fmp4_sequence": 103
                }]
            }
        }));
        assert_eq!(contributor["publication"]["canonical_epoch"], 42);
        assert_eq!(contributor["publication"]["contiguous_object"], 103);
        assert_eq!(contributor["publication"]["head_object"], 103);
        assert_eq!(contributor["publication"]["gap_count"], 0);
    }

    #[test]
    fn does_not_conflate_multiple_publications_or_incomplete_fleet_rows() {
        let multiple_streams = vec![
            json!({
                "node_id": "edge-london",
                "stream_id_text": "1",
                "canonical_epoch": 42,
                "contiguous_object": 8,
                "head_object": 8,
                "gap_count": 0
            }),
            json!({
                "node_id": "edge-london",
                "stream_id_text": "2",
                "canonical_epoch": 52,
                "contiguous_object": 18,
                "head_object": 18,
                "gap_count": 0
            }),
        ];
        assert_eq!(
            canonical_publication_summary(None, &multiple_streams),
            json!({})
        );

        let incomplete = vec![
            json!({
                "node_id": "edge-london",
                "stream_id_text": "1",
                "canonical_epoch": 42,
                "contiguous_object": 8,
                "head_object": 8,
                "gap_count": 0
            }),
            json!({
                "node_id": "edge-tokyo",
                "stream_id_text": "1",
                "canonical_epoch": 42
            }),
        ];
        let publication = canonical_publication_summary(None, &incomplete);
        assert_eq!(publication["canonical_epoch"], 42);
        assert!(publication.get("head_object").is_none());
        assert!(publication.get("contiguous_object").is_none());
        assert!(publication.get("gap_count").is_none());
    }
}
