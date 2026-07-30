use needletail_mission_control::{MeshStatus, OPERATIONS_SNAPSHOT_SCHEMA};

#[test]
fn documentation_fixture_matches_the_browser_contract_without_election_proof() {
    let snapshot: MeshStatus =
        serde_json::from_str(include_str!("../fixtures/documentation-preview.json"))
            .expect("documentation preview fixture must match the Operations model");

    assert_eq!(snapshot.schema, OPERATIONS_SNAPSHOT_SCHEMA);
    assert_eq!(snapshot.nodes.len(), 10);
    assert_eq!(snapshot.topology_links.len(), 16);
    assert_eq!(
        snapshot
            .contributor
            .as_ref()
            .map(|contributor| contributor.service.as_str()),
        Some("av-contrib")
    );
    assert!(!snapshot.orchestration.collector.reported());
    assert!(!snapshot.orchestration.collector.has_committed_live_lease());
}
