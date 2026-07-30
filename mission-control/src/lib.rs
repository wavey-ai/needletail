use serde::Deserialize;

pub const HISTOGRAM_BOUNDS_US: [u64; 13] = [
    100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000,
    1_000_000,
];
pub const PUBLICATION_AVAILABILITY_BOUNDS_US: [u64; 16] = [
    1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 75_000, 100_000, 125_000, 150_000, 175_000,
    200_000, 250_000, 500_000, 1_000_000, 2_000_000,
];

pub const MAX_STREAM_ROWS: usize = 12;
pub const MAX_NODE_ROWS: usize = 16;
pub const MAX_EDGE_ROWS: usize = 12;
pub const MAX_SESSION_ROWS: usize = 12;
pub const MAX_EVENT_ROWS: usize = 16;
pub const OPERATIONS_SNAPSHOT_SCHEMA: &str = "needletail.operations-snapshot.v1";
pub const OPERATIONS_AUTHORITY: &str = "needletail-controller";
pub const MAX_OPERATIONS_LEASE_MS: u64 = 30_000;

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ContribStatus {
    pub service: String,
    pub status: String,
    pub updated_unix_ms: u64,
    pub default_stream_id: String,
    pub advertised_hls_stream_id: String,
    pub advertised_hls_path: String,
    pub mesh: ContribRelayConfig,
    pub hls: HlsConfig,
    pub fec: FecConfig,
    pub listeners: Vec<ListenerStatus>,
    pub runtime: ContribRuntime,
    pub alerts: Vec<ContribAlert>,
    pub health: ContribHealth,
    pub activity: Vec<ContribActivity>,
    pub publication: PublicationSnapshot,
    pub delivery: DeliverySnapshot,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ContribHealth {
    pub state: String,
    pub stale_threshold_ms: u64,
    pub input_seen: bool,
    pub fmp4_input_seen: bool,
    pub output_seen: bool,
    pub last_input_age_ms: Option<u64>,
    pub last_fmp4_input_age_ms: Option<u64>,
    pub last_output_age_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ContribRelayConfig {
    pub relay_primary_configured: bool,
    pub relay_secondary_configured: bool,
    pub relay_carrier: Option<String>,
    pub relay_trust: Option<String>,
    pub relay_primary_id: Option<String>,
    pub relay_primary_target: Option<String>,
    pub relay_primary_bind: Option<String>,
    pub relay_secondary_id: Option<String>,
    pub relay_secondary_target: Option<String>,
    pub relay_secondary_bind: Option<String>,
    pub relay_secondary_source_seeded: bool,
    pub relay_exclusive: bool,
    pub relay_topology_generation: u64,
    pub relay_subscription_id: u64,
    pub relay_deadline_ms: u64,
    pub relay_path_observation_source: String,
    pub relay_path_loss_fraction: f64,
    pub relay_path_best_direct_rtt_ms: f64,
    pub relay_path_rtt_ms: f64,
    pub relay_path_jitter_ms: f64,
    pub relay_path_queue_delay_ms: f64,
    pub relay_path_observed_at_unix_ms: Option<u64>,
    pub relay_secondary_path_observation_source: String,
    pub relay_secondary_path_loss_fraction: f64,
    pub relay_secondary_path_best_direct_rtt_ms: f64,
    pub relay_secondary_path_rtt_ms: f64,
    pub relay_secondary_path_jitter_ms: f64,
    pub relay_secondary_path_queue_delay_ms: f64,
    pub relay_secondary_path_observed_at_unix_ms: Option<u64>,
    pub media_object_clock_id: String,
    pub media_object_clock_confidence: String,
    pub media_object_clock_estimated_error_ms: u64,
    pub media_object_source_epoch: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct HlsConfig {
    pub part_target_ms: u64,
    pub segment_target_ms: u64,
    pub playlist_target_duration_ms: u64,
    pub playlist_count: u64,
    pub playlist_buffer_kb: u64,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct FecConfig {
    pub repair_symbols: u64,
    pub symbol_size: u64,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ListenerStatus {
    pub protocol: String,
    pub enabled: bool,
    pub bind: Option<String>,
    pub output_stream_id: String,
    pub output_hls_path: String,
    pub backend: Option<String>,
    pub profile: Option<String>,
    pub flow_id: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ContribRuntime {
    pub raw_http: RawHttpRuntime,
    pub media_access_units: MediaRuntime,
    pub mesh_forward: ForwardRuntime,
    pub relay_session: RelayEmission,
    pub mpeg_ts: MpegTsRuntime,
    pub rtmp: RtmpRuntime,
    pub fmp4: Fmp4Runtime,
    pub hls: HlsRuntime,
    pub ingest_sessions: IngestSessionsRuntime,
    pub streams: Vec<ContribStream>,
    pub protocols: Vec<ProtocolRuntime>,
    /// Forward-compatible direct ingest-to-relay histogram.
    pub ingest_latency: DurationHistogram,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct RelayEmission {
    pub objects_sent: u64,
    pub encode_errors: u64,
    pub source_datagrams: u64,
    pub source_datagram_bytes: u64,
    pub source_errors: u64,
    pub repair_datagrams: u64,
    pub repair_datagram_bytes: u64,
    pub repair_errors: u64,
    pub repair_primary_fallback_objects: u64,
    pub primary_lane_objects_succeeded: u64,
    pub primary_lane_objects_failed: u64,
    pub primary_lane_state: String,
    pub secondary_lane_objects_succeeded: u64,
    pub secondary_lane_objects_failed: u64,
    pub secondary_lane_state: String,
    pub surviving_lane_objects: u64,
    pub all_lanes_failed_objects: u64,
    pub expired_objects: Option<u64>,
    pub expired_symbols: Option<u64>,
    pub deadline_hits: Option<u64>,
    pub deadline_misses: Option<u64>,
    pub last_deadline_unix_us: Option<u64>,
    pub last_deadline_headroom_us: Option<u64>,
    pub stages: RelayPipelineStages,
}

impl RelayEmission {
    pub fn repair_overhead_percent(&self) -> Option<f64> {
        let total = self.source_datagrams.saturating_add(self.repair_datagrams);
        (total > 0).then(|| self.repair_datagrams as f64 * 100.0 / total as f64)
    }

    pub fn errors(&self) -> u64 {
        self.encode_errors
            .saturating_add(self.all_lanes_failed_objects)
    }

    pub fn lane_failures(&self) -> u64 {
        self.primary_lane_objects_failed
            .saturating_add(self.secondary_lane_objects_failed)
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct RelayPipelineStages {
    pub total: DurationHistogram,
    pub encode_wait: DurationHistogram,
    pub encode: DurationHistogram,
    pub schedule: DurationHistogram,
    pub primary_source_send: DurationHistogram,
    pub secondary_source_send: DurationHistogram,
    pub primary_repair_send: DurationHistogram,
    pub secondary_repair_send: DurationHistogram,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct RawHttpRuntime {
    pub requests: u64,
    pub chunks: u64,
    pub bytes: u64,
    pub datagrams: u64,
    pub last_seen_unix_ms: Option<u64>,
    pub last_seen_age_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct MediaRuntime {
    pub requests: u64,
    pub payload_bytes: u64,
    pub datagrams: u64,
    pub last_seen_unix_ms: Option<u64>,
    pub last_seen_age_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ForwardRuntime {
    pub stream_payloads: u64,
    pub stream_payload_bytes: u64,
    pub stream_datagrams: u64,
    pub stream_datagram_bytes: u64,
    pub stream_errors: u64,
    pub stream_last_unix_ms: Option<u64>,
    pub stream_last_age_ms: Option<u64>,
    pub stream_duration: DurationHistogram,
    pub stream_stages: ForwardStages,
    pub media_payloads: u64,
    pub media_payload_bytes: u64,
    pub media_datagrams: u64,
    pub media_datagram_bytes: u64,
    pub media_errors: u64,
    pub media_last_unix_ms: Option<u64>,
    pub media_last_age_ms: Option<u64>,
    pub media_duration: DurationHistogram,
    pub media_stages: ForwardStages,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ForwardStages {
    pub encode_wait: DurationHistogram,
    pub encode: DurationHistogram,
    pub send: DurationHistogram,
    pub telemetry: DurationHistogram,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct MpegTsRuntime {
    pub slots: u64,
    pub bytes: u64,
    pub last_seen_unix_ms: Option<u64>,
    pub last_seen_age_ms: Option<u64>,
    pub continuity_errors: u64,
    pub continuity_dropped_bytes: u64,
    pub payload_drops: u64,
    pub payload_drop_bytes: u64,
    pub last_error_unix_ms: Option<u64>,
    pub last_error_age_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct RtmpRuntime {
    pub access_units: u64,
    pub bytes: u64,
    pub last_seen_unix_ms: Option<u64>,
    pub last_seen_age_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Fmp4Runtime {
    pub parts: u64,
    pub bytes: u64,
    pub init_bytes: u64,
    pub publish_errors: u64,
    pub last_publish_unix_ms: Option<u64>,
    pub last_publish_age_ms: Option<u64>,
    pub video_codec: Option<String>,
    pub video_width: Option<u64>,
    pub video_height: Option<u64>,
    pub video_parts: u64,
    pub video_access_units: u64,
    pub audio_codec: Option<String>,
    pub audio_parts: u64,
    pub audio_access_units: u64,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct HlsRuntime {
    pub responses_total: u64,
    pub response_errors: u64,
    pub response_not_found: u64,
    pub last_response_unix_ms: Option<u64>,
    pub last_response_age_ms: Option<u64>,
    pub recent_responses: Vec<HttpResponseEvent>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct HttpResponseEvent {
    pub unix_ms: u64,
    pub method: String,
    pub path: String,
    pub query: Option<String>,
    pub status: u16,
    pub bytes: u64,
    pub duration_us: u64,
    pub content_type: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct IngestSessionsRuntime {
    pub active: usize,
    pub started: u64,
    pub ended: u64,
    pub recent: Vec<IngestSession>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct IngestSession {
    pub session_id: u64,
    pub protocol: String,
    pub stream_id_text: String,
    pub output_stream_id_text: Option<String>,
    pub output_stream_idx: Option<usize>,
    pub peer: Option<String>,
    pub path: Option<String>,
    pub state: String,
    pub started_unix_ms: u64,
    pub last_seen_unix_ms: u64,
    pub ended_unix_ms: Option<u64>,
    pub age_ms: u64,
    pub body_slots: u64,
    pub bytes: u64,
    pub access_units: u64,
    pub end_reason: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ProtocolRuntime {
    pub protocol: String,
    pub units: u64,
    pub bytes: u64,
    pub active_sessions: usize,
    pub ended_sessions: usize,
    pub last_seen_unix_ms: Option<u64>,
    pub last_seen_age_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ContribStream {
    pub stream_id_text: String,
    pub state: String,
    pub input_units: u64,
    pub input_bytes: u64,
    pub mesh_payloads: u64,
    pub mesh_payload_bytes: u64,
    pub mesh_datagrams: u64,
    pub mesh_datagram_bytes: u64,
    pub mesh_errors: u64,
    pub fmp4_parts: u64,
    pub fmp4_bytes: u64,
    pub fmp4_init_bytes: u64,
    pub fmp4_publish_errors: u64,
    pub latest_fmp4_sequence: Option<u64>,
    pub video_codec: Option<String>,
    pub video_width: Option<u64>,
    pub video_height: Option<u64>,
    pub video_parts: u64,
    pub video_access_units: u64,
    pub audio_codec: Option<String>,
    pub audio_parts: u64,
    pub audio_access_units: u64,
    pub last_input_unix_ms: Option<u64>,
    pub last_input_age_ms: Option<u64>,
    pub last_mesh_forward_unix_ms: Option<u64>,
    pub last_mesh_forward_age_ms: Option<u64>,
    pub last_fmp4_unix_ms: Option<u64>,
    pub last_fmp4_age_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ContribAlert {
    pub level: String,
    pub code: String,
    pub message: String,
    pub count: u64,
    pub last_seen_unix_ms: Option<u64>,
    pub stream_id_text: Option<String>,
    pub protocol: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct ContribActivity {
    pub level: String,
    pub code: String,
    pub message: String,
    pub datagrams: Option<u64>,
    pub sequence: Option<u64>,
    pub seen_unix_ms: u64,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct MeshStatus {
    pub schema: String,
    pub updated_unix_ms: u64,
    pub contributor: Option<ContribStatus>,
    pub node: EdgeNode,
    pub relay_session: RelayIngress,
    pub relay_nodes: Vec<RelayNodeSession>,
    pub aggregate: FleetAggregate,
    pub telemetry: TelemetryHealth,
    pub orchestration: OperationsReadiness,
    pub nodes: Vec<EdgeNode>,
    pub edge_services: Vec<EdgeService>,
    pub streams: Vec<EdgeStream>,
    pub alerts: Vec<MeshAlert>,
    pub activity: Vec<MeshActivity>,
    pub publication: PublicationSnapshot,
    pub delivery: DeliverySnapshot,
    pub routes: Vec<DeliverySnapshot>,
    pub topology_links: Vec<NetworkTopologyLink>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct EdgeNode {
    pub node_id: String,
    pub provider: String,
    pub region: String,
    pub zone: String,
    pub role: String,
    pub continent: String,
    pub latitude: Option<f64>,
    pub longitude: Option<f64>,
    pub public_endpoint: Option<String>,
    pub updated_unix_ms: Option<u64>,
    pub total_storage_bytes: u64,
    pub used_storage_bytes: u64,
    pub egress_capacity_bps: u64,
    pub contributor_streams: u64,
    pub active_streams: u64,
    pub draining: bool,
}

impl EdgeNode {
    pub fn storage_percent(&self) -> Option<f64> {
        (self.total_storage_bytes > 0)
            .then(|| self.used_storage_bytes as f64 * 100.0 / self.total_storage_bytes as f64)
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct RelayIngress {
    pub primary_sessions: u64,
    pub secondary_sessions: u64,
    pub authenticated_sessions: u64,
    pub controlled_sessions: u64,
    pub active_objects: u64,
    pub completed_objects: u64,
    pub active_object_bytes: u64,
    pub buffered_datagrams: u64,
    pub datagrams_received: u64,
    pub datagrams_rejected: u64,
    pub source_datagrams: u64,
    pub repair_datagrams: u64,
    pub duplicate_datagrams: u64,
    pub decoded_objects: u64,
    pub repair_assisted_objects: u64,
    pub fec_recovered_objects: u64,
    pub fec_recovered_source_symbols: u64,
    pub expired_objects: u64,
    pub conflict_drops: u64,
    pub authentication_drops: u64,
    pub deadline_drops: u64,
    pub downstream_children: u64,
    pub forwarded_source_datagrams: u64,
    pub forwarded_repair_datagrams: u64,
    pub forwarded_bytes: u64,
    pub forward_errors: u64,
    pub forward_filtered_datagrams: u64,
    pub warm_source_buffered_datagrams: u64,
    pub warm_source_buffered_bytes: u64,
    pub warm_source_replayed_datagrams: u64,
    pub warm_source_replayed_bytes: u64,
    pub warm_source_expired_datagrams: u64,
    pub warm_source_retired_datagrams: u64,
    pub warm_source_evicted_datagrams: u64,
    pub processing_duration_count: u64,
    pub processing_duration_sum_us: u64,
    pub processing_duration_max_us: u64,
    pub processing_duration_buckets: Vec<u64>,
    pub forward_duration_count: u64,
    pub forward_duration_sum_us: u64,
    pub forward_duration_max_us: u64,
    pub forward_duration_buckets: Vec<u64>,
    pub publication_to_available_count: u64,
    pub publication_to_available_sum_us: u64,
    pub publication_to_available_max_us: u64,
    pub publication_to_available_buckets: Vec<u64>,
    pub publication_clock_error_max_us: u64,
    pub publication_clock_unusable_objects: u64,
    pub failover_controller_state: String,
    pub failover_controller_enabled: u64,
    pub failover_commands_sent: u64,
    pub failover_command_send_errors: u64,
    pub failover_promotions: u64,
    pub failover_demotions: u64,
    pub failover_secondary_unavailable_events: u64,
    pub failover_primary_source_age_ms: u64,
    pub failover_secondary_repair_age_ms: u64,
    pub failover_last_detection_us: u64,
    pub failover_last_promotion_to_source_us: u64,
    pub failover_last_media_gap_us: u64,
    pub failover_max_media_gap_us: u64,
    pub failover_controller_last_transition_unix_ms: u64,
    pub failover_listeners: u64,
    pub failover_promoted_children: u64,
    pub failover_commands_received: u64,
    pub failover_commands_rejected: u64,
    pub failover_lease_expirations: u64,
    pub failover_promotions_applied: u64,
    pub failover_demotions_applied: u64,
    pub failover_listener_last_transition_unix_ms: u64,
}

impl RelayIngress {
    pub fn errors(&self) -> u64 {
        self.datagrams_rejected
            .saturating_add(self.failover_command_send_errors)
            .saturating_add(self.failover_commands_rejected)
    }

    pub fn repair_overhead_percent(&self) -> Option<f64> {
        let total = self.source_datagrams.saturating_add(self.repair_datagrams);
        (total > 0).then(|| self.repair_datagrams as f64 * 100.0 / total as f64)
    }

    pub fn forward_percentile_us(&self, percentile: u64) -> Option<u64> {
        histogram_percentile_us(
            self.forward_duration_count,
            &self.forward_duration_buckets,
            percentile,
        )
    }

    pub fn processing_percentile_us(&self, percentile: u64) -> Option<u64> {
        histogram_percentile_us(
            self.processing_duration_count,
            &self.processing_duration_buckets,
            percentile,
        )
    }

    pub fn publication_to_available_percentile_us(&self, percentile: u64) -> Option<u64> {
        histogram_percentile_us_with_bounds(
            self.publication_to_available_count,
            &self.publication_to_available_buckets,
            &PUBLICATION_AVAILABILITY_BOUNDS_US,
            percentile,
        )
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct RelayNodeSession {
    pub node_id: String,
    pub region: String,
    pub relay_session: RelayIngress,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct FleetAggregate {
    pub node_count: usize,
    pub total_storage_bytes: u64,
    pub used_storage_bytes: u64,
    pub total_egress_capacity_bps: u64,
    pub contributor_streams: u64,
    pub active_streams: u64,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct TelemetryHealth {
    pub stale_after_ms: u64,
    pub fresh_remote_count: usize,
    pub stale_remote_count: usize,
    pub stale_nodes: Vec<TelemetryNodeHealth>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct TelemetryNodeHealth {
    pub node_id: String,
    pub region: String,
    pub updated_unix_ms: u64,
    pub age_ms: u64,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct OperationsReadiness {
    pub control_dispatch_ready: bool,
    pub telemetry_fec: TelemetryFecStatus,
    pub collector: OperationsCollectorStatus,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct OperationsCollectorStatus {
    pub authority: String,
    pub role: String,
    pub leader_node_id: Option<String>,
    pub leader_region: Option<String>,
    pub term: Option<u64>,
    pub fencing_generation: Option<u64>,
    pub quorum_healthy: Option<bool>,
    pub voters_online: Option<usize>,
    pub voters_total: Option<usize>,
    pub lease_remaining_ms: Option<u64>,
    pub last_leadership_change_unix_ms: Option<u64>,
    pub last_change_reason: Option<String>,
    pub public_endpoint: Option<String>,
    pub ingest_endpoint: Option<String>,
    pub nodes_current: Option<usize>,
    pub nodes_stale: Option<usize>,
    pub nodes_awaiting: Option<usize>,
}

impl OperationsCollectorStatus {
    pub fn reported(&self) -> bool {
        self.leader_node_id.is_some()
            || self.term.is_some()
            || self.fencing_generation.is_some()
            || self.quorum_healthy.is_some()
    }

    pub fn has_committed_live_lease(&self) -> bool {
        self.quorum_healthy == Some(true)
            && self.authority == OPERATIONS_AUTHORITY
            && matches!(
                self.role.to_ascii_lowercase().as_str(),
                "leader" | "collector"
            )
            && self
                .leader_node_id
                .as_ref()
                .is_some_and(|leader| !leader.trim().is_empty())
            && self.term.is_some_and(|term| term > 0)
            && self
                .fencing_generation
                .is_some_and(|generation| generation > 0)
            && self
                .voters_total
                .is_some_and(|total| total >= 3 && total % 2 == 1)
            && self
                .voters_online
                .zip(self.voters_total)
                .is_some_and(|(online, total)| online <= total && online > total / 2)
            && self
                .lease_remaining_ms
                .is_some_and(|remaining| (1..=MAX_OPERATIONS_LEASE_MS).contains(&remaining))
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct NetworkTopologyLink {
    pub from_node_id: String,
    pub to_node_id: String,
    pub role: String,
    pub state: String,
    pub throughput_bps: Option<u64>,
    pub rtt_us: Option<u64>,
    pub generation: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct TelemetryFecStatus {
    pub enabled: bool,
    pub collector_bind: Option<String>,
    pub publisher_targets: usize,
    pub interval_ms: u64,
    pub rate_bps: u64,
    pub queue_blocks: usize,
    pub queue_bytes: usize,
    pub snapshots_submitted: u64,
    pub snapshots_replaced: u64,
    pub snapshots_oversized: u64,
    pub blocks_encoded: u64,
    pub source_datagrams_sent: u64,
    pub repair_datagrams_sent: u64,
    pub sent_bytes: u64,
    pub skipped_repair_datagrams: u64,
    pub send_drops: u64,
    pub received_datagrams: u64,
    pub received_bytes: u64,
    pub decoded_snapshots: u64,
    pub duplicate_snapshots: u64,
    pub encode_errors: u64,
    pub receive_errors: u64,
    pub decode_errors: u64,
    pub ingest_errors: u64,
    pub last_received_unix_ms: Option<u64>,
    pub last_decoded_unix_ms: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct EdgeService {
    pub node_id: String,
    pub region: String,
    pub continent: String,
    pub playback_base_url: Option<String>,
    pub active_readers: u64,
    pub requests_served: u64,
    pub bytes_served: u64,
    pub llhls_tail_requests: u64,
    pub responses_total: u64,
    pub response_errors: u64,
    pub response_not_found: u64,
    pub last_response_unix_ms: Option<u64>,
    pub response_duration_count: u64,
    pub response_duration_sum_us: u64,
    pub response_duration_p50_us: Option<u64>,
    pub response_duration_p95_us: Option<u64>,
    pub response_duration_p99_us: Option<u64>,
    pub response_duration_buckets: Vec<u64>,
    pub recent_responses: Vec<HttpResponseEvent>,
    pub draining: bool,
}

impl EdgeService {
    pub fn percentile_us(&self, percentile: u64) -> Option<u64> {
        match percentile {
            50 if self.response_duration_p50_us.is_some() => self.response_duration_p50_us,
            95 if self.response_duration_p95_us.is_some() => self.response_duration_p95_us,
            99 if self.response_duration_p99_us.is_some() => self.response_duration_p99_us,
            _ => histogram_percentile_us(
                self.response_duration_count,
                &self.response_duration_buckets,
                percentile,
            ),
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct EdgeStream {
    pub node_id: String,
    pub stream_id: u64,
    pub stream_id_text: String,
    pub latest_local_part: Option<u64>,
    pub latest_local_part_bytes: Option<u64>,
    pub latest_local_part_duration_ms: Option<u64>,
    pub latest_local_part_age_ms: Option<u64>,
    pub latest_mesh_part: Option<u64>,
    pub canonical_epoch: Option<u64>,
    pub canonical_epoch_activation_delay_us: Option<u64>,
    pub bytes_received: u64,
    pub datagrams_received: u64,
    pub mesh_lag_parts: Option<u64>,
    pub last_ingest_age_ms: Option<u64>,
    pub stale_threshold_ms: Option<u64>,
    pub contiguous_object: Option<u64>,
    pub head_object: Option<u64>,
    pub gap_count: Option<u64>,
}

impl EdgeStream {
    pub fn stale(&self) -> Option<bool> {
        Some(self.last_ingest_age_ms? > self.stale_threshold_ms?)
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct MeshAlert {
    pub level: String,
    pub code: String,
    pub message: String,
    pub count: u64,
    pub last_seen_unix_ms: Option<u64>,
    pub node_id: Option<String>,
    pub stream_id_text: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct MeshActivity {
    pub level: String,
    pub code: String,
    pub message: String,
    pub count: u64,
    pub seen_unix_ms: u64,
    pub node_id: Option<String>,
    pub stream_id_text: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct PublicationSnapshot {
    pub canonical_epoch: Option<u64>,
    pub canonical_epoch_activation_delay_us: Option<u64>,
    pub contiguous_object: Option<u64>,
    pub head_object: Option<u64>,
    pub gap_count: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct DeliverySnapshot {
    pub delivery_class: Option<String>,
    pub generation: Option<u64>,
    pub route_state: Option<String>,
    pub route_ready: Option<bool>,
    pub fabric: Option<String>,
    pub path_stretch: Option<f64>,
    pub stream_id_text: Option<String>,
    pub destination: Option<String>,
    pub primary: Option<RouteLane>,
    pub secondary: Option<RouteLane>,
}

impl DeliverySnapshot {
    pub fn has_assignment(&self) -> bool {
        self.delivery_class.is_some()
            || self.generation.is_some()
            || self.route_state.is_some()
            || self.route_ready.is_some()
            || self.fabric.is_some()
            || self.primary.is_some()
            || self.secondary.is_some()
    }

    pub fn fabric_label(&self) -> Option<&'static str> {
        match self.fabric.as_deref() {
            Some("direct_low_latency") => Some("Direct / one-hop overlay"),
            Some("dual_parent_dag") => Some("Dual-parent DAG"),
            _ => None,
        }
    }

    pub fn readiness_label(&self) -> &'static str {
        if !self.has_assignment() {
            return "awaiting route assignment";
        }
        let complete_assignment = self.generation.is_some_and(|generation| generation > 0)
            && matches!(
                self.delivery_class.as_deref(),
                Some("interactive" | "premium_live" | "mass_broadcast")
            )
            && self.fabric_label().is_some()
            && self.primary.is_some();
        if !complete_assignment {
            return "assignment incomplete";
        }
        let state = self.route_state.as_deref();
        let state_ready = matches!(state, Some("ready" | "active"));
        let state_unready = matches!(
            state,
            Some("pending" | "warming" | "degraded" | "failed" | "unavailable")
        );
        if state.is_some() && !state_ready && !state_unready {
            return "route state unrecognized";
        }
        match self.route_ready {
            Some(true) if state_unready => "inconsistent route state",
            Some(false) if state_ready => "inconsistent route state",
            Some(true) => "ready",
            Some(false) => "not ready",
            None => "readiness unreported",
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct RouteLane {
    pub node_id: Option<String>,
    pub target: Option<String>,
    pub carrier: Option<String>,
    pub trust: Option<String>,
    pub state: Option<String>,
    pub observation_source: Option<String>,
    pub rtt_us: Option<u64>,
    pub jitter_us: Option<u64>,
    pub loss_ppm: Option<u64>,
    pub deadline_miss_ppm: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct DurationHistogram {
    pub count: u64,
    pub sum_us: u64,
    pub p50_us: Option<u64>,
    pub p95_us: Option<u64>,
    pub p99_us: Option<u64>,
    pub buckets: Vec<u64>,
}

impl DurationHistogram {
    pub fn percentile_us(&self, percentile: u64) -> Option<u64> {
        match percentile {
            50 if self.p50_us.is_some() => self.p50_us,
            95 if self.p95_us.is_some() => self.p95_us,
            99 if self.p99_us.is_some() => self.p99_us,
            _ => histogram_percentile_us(self.count, &self.buckets, percentile),
        }
    }

    pub fn has_samples(&self) -> bool {
        self.count > 0 || self.p50_us.is_some() || self.p95_us.is_some() || self.p99_us.is_some()
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EventSource {
    Contributor,
    Delivery,
}

impl EventSource {
    pub fn label(&self) -> &'static str {
        match self {
            Self::Contributor => "Contributor",
            Self::Delivery => "Delivery",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OperationalEvent {
    pub source: EventSource,
    pub level: String,
    pub code: String,
    pub message: String,
    pub count: u64,
    pub seen_unix_ms: u64,
    pub context: Option<String>,
}

pub fn contributor_latency(status: &ContribStatus) -> &DurationHistogram {
    &status.runtime.ingest_latency
}

pub fn histogram_percentile_us(count: u64, buckets: &[u64], percentile: u64) -> Option<u64> {
    histogram_percentile_us_with_bounds(count, buckets, &HISTOGRAM_BOUNDS_US, percentile)
}

fn histogram_percentile_us_with_bounds(
    count: u64,
    buckets: &[u64],
    bounds: &[u64],
    percentile: u64,
) -> Option<u64> {
    if count == 0 || buckets.is_empty() || percentile == 0 || percentile > 100 {
        return None;
    }
    let rank = count.saturating_mul(percentile).saturating_add(99) / 100;
    buckets
        .iter()
        .zip(bounds)
        .find_map(|(bucket_count, bound)| (*bucket_count >= rank).then_some(*bound))
}

pub fn effective_delivery(edge: Option<&MeshStatus>) -> DeliverySnapshot {
    edge.map(|status| status.delivery.clone())
        .unwrap_or_default()
}

pub fn bounded_contrib_streams(status: &ContribStatus) -> Vec<ContribStream> {
    status
        .runtime
        .streams
        .iter()
        .take(MAX_STREAM_ROWS)
        .cloned()
        .collect()
}

pub fn bounded_edge_streams(status: &MeshStatus) -> Vec<EdgeStream> {
    status
        .streams
        .iter()
        .take(MAX_STREAM_ROWS)
        .cloned()
        .collect()
}

pub fn bounded_nodes(status: &MeshStatus) -> Vec<EdgeNode> {
    status.nodes.iter().take(MAX_NODE_ROWS).cloned().collect()
}

pub fn bounded_edges(status: &MeshStatus) -> Vec<EdgeService> {
    status
        .edge_services
        .iter()
        .take(MAX_EDGE_ROWS)
        .cloned()
        .collect()
}

pub fn bounded_relay_nodes(status: &MeshStatus) -> Vec<RelayNodeSession> {
    status
        .relay_nodes
        .iter()
        .take(MAX_NODE_ROWS)
        .cloned()
        .collect()
}

pub fn bounded_ingest_sessions(status: &ContribStatus) -> Vec<IngestSession> {
    status
        .runtime
        .ingest_sessions
        .recent
        .iter()
        .take(MAX_SESSION_ROWS)
        .cloned()
        .collect()
}

fn transition_or_snapshot_time(transition_unix_ms: u64, snapshot_unix_ms: u64) -> u64 {
    if transition_unix_ms == 0 {
        snapshot_unix_ms
    } else {
        transition_unix_ms
    }
}

pub fn operational_alerts(
    contrib: Option<&ContribStatus>,
    edge: Option<&MeshStatus>,
) -> Vec<OperationalEvent> {
    let mut events = Vec::new();
    if let Some(status) = contrib {
        events.extend(status.alerts.iter().map(|event| {
            OperationalEvent {
                source: EventSource::Contributor,
                level: event.level.clone(),
                code: event.code.clone(),
                message: event.message.clone(),
                count: event.count,
                seen_unix_ms: event.last_seen_unix_ms.unwrap_or(status.updated_unix_ms),
                context: event
                    .stream_id_text
                    .clone()
                    .or_else(|| event.protocol.clone()),
            }
        }));
        if status.runtime.relay_session.deadline_misses.unwrap_or(0) > 0 {
            push_derived_event(
                &mut events,
                OperationalEvent {
                    source: EventSource::Contributor,
                    level: "warning".to_owned(),
                    code: "relay_emission_deadline_missed".to_owned(),
                    message:
                        "One or more canonical objects missed the contributor emission deadline."
                            .to_owned(),
                    count: status.runtime.relay_session.deadline_misses.unwrap_or(0),
                    seen_unix_ms: status.updated_unix_ms,
                    context: Some(status.advertised_hls_stream_id.clone()),
                },
            );
        }
        if status.runtime.relay_session.expired_symbols.unwrap_or(0) > 0 {
            push_derived_event(
                &mut events,
                OperationalEvent {
                    source: EventSource::Contributor,
                    level: "warning".to_owned(),
                    code: "relay_symbol_expired".to_owned(),
                    message:
                        "Deadline expiry dropped one or more RaptorQ symbols at the contributor."
                            .to_owned(),
                    count: status.runtime.relay_session.expired_symbols.unwrap_or(0),
                    seen_unix_ms: status.updated_unix_ms,
                    context: Some(status.advertised_hls_stream_id.clone()),
                },
            );
        }
    }
    if let Some(status) = edge {
        events.extend(
            status
                .alerts
                .iter()
                .filter(|event| include_delivery_event(&event.code))
                .map(|event| OperationalEvent {
                    source: EventSource::Delivery,
                    level: event.level.clone(),
                    code: event.code.clone(),
                    message: delivery_event_message(&event.code, &event.message),
                    count: event.count,
                    seen_unix_ms: event.last_seen_unix_ms.unwrap_or(status.updated_unix_ms),
                    context: event
                        .node_id
                        .clone()
                        .or_else(|| event.stream_id_text.clone()),
                }),
        );
        if status.relay_session.failover_controller_state == "secondary_unavailable" {
            push_derived_event(
                &mut events,
                OperationalEvent {
                    source: EventSource::Delivery,
                    level: "error".to_owned(),
                    code: "relay_failover_secondary_unavailable".to_owned(),
                    message: "Primary source is silent and the warm secondary is not ready."
                        .to_owned(),
                    count: status
                        .relay_session
                        .failover_secondary_unavailable_events
                        .max(1),
                    seen_unix_ms: transition_or_snapshot_time(
                        status
                            .relay_session
                            .failover_controller_last_transition_unix_ms,
                        status.updated_unix_ms,
                    ),
                    context: Some(status.node.node_id.clone()),
                },
            );
        }
        if status.relay_session.failover_command_send_errors > 0 {
            push_derived_event(
                &mut events,
                OperationalEvent {
                    source: EventSource::Delivery,
                    level: "error".to_owned(),
                    code: "relay_failover_control_send_error".to_owned(),
                    message: "The edge could not refresh its warm-secondary control lease."
                        .to_owned(),
                    count: status.relay_session.failover_command_send_errors,
                    seen_unix_ms: status.updated_unix_ms,
                    context: Some(status.node.node_id.clone()),
                },
            );
        }
    }
    sort_and_bound_events(&mut events);
    events
}

pub fn operational_activity(
    contrib: Option<&ContribStatus>,
    edge: Option<&MeshStatus>,
) -> Vec<OperationalEvent> {
    let mut events = Vec::new();
    if let Some(status) = contrib {
        events.extend(status.activity.iter().map(|event| OperationalEvent {
            source: EventSource::Contributor,
            level: event.level.clone(),
            code: event.code.clone(),
            message: event.message.clone(),
            count: event.datagrams.unwrap_or(1),
            seen_unix_ms: event.seen_unix_ms,
            context: event.sequence.map(|sequence| format!("object {sequence}")),
        }));
    }
    if let Some(status) = edge {
        events.extend(
            status
                .activity
                .iter()
                .filter(|event| include_delivery_event(&event.code))
                .map(|event| OperationalEvent {
                    source: EventSource::Delivery,
                    level: event.level.clone(),
                    code: event.code.clone(),
                    message: delivery_event_message(&event.code, &event.message),
                    count: event.count,
                    seen_unix_ms: event.seen_unix_ms,
                    context: event
                        .node_id
                        .clone()
                        .or_else(|| event.stream_id_text.clone()),
                }),
        );
        let derived_events = status
            .relay_nodes
            .iter()
            .filter(|node| node.relay_session.failover_lease_expirations > 0)
            .map(|node| OperationalEvent {
                source: EventSource::Delivery,
                level: "info".to_owned(),
                code: "relay_failover_lease_expired".to_owned(),
                message: "A warm relay returned to repair-only after its promotion lease expired."
                    .to_owned(),
                count: node.relay_session.failover_lease_expirations,
                seen_unix_ms: transition_or_snapshot_time(
                    node.relay_session.failover_listener_last_transition_unix_ms,
                    status.updated_unix_ms,
                ),
                context: Some(node.node_id.clone()),
            })
            .collect::<Vec<_>>();
        for event in derived_events {
            push_derived_event(&mut events, event);
        }
    }
    sort_and_bound_events(&mut events);
    events
}

fn sort_and_bound_events(events: &mut Vec<OperationalEvent>) {
    events.sort_by(|left, right| {
        right
            .seen_unix_ms
            .cmp(&left.seen_unix_ms)
            .then_with(|| left.code.cmp(&right.code))
    });
    events.truncate(MAX_EVENT_ROWS);
}

fn push_derived_event(events: &mut Vec<OperationalEvent>, event: OperationalEvent) {
    if events.iter().any(|current| {
        current.source == event.source
            && current.code == event.code
            && current.context == event.context
    }) {
        return;
    }
    events.push(event);
}

fn include_delivery_event(code: &str) -> bool {
    !matches!(
        code,
        "close_node"
            | "control_failures"
            | "control_skipped"
            | "linode_private_discovery_inactive"
            | "mesh_no_links"
            | "mesh_single_node"
            | "mesh_snapshot"
            | "mesh_unknown_peers"
            | "provision_node"
            | "replica_request"
            | "warm_stream"
    )
}

fn delivery_event_message(code: &str, message: &str) -> String {
    match code {
        "nodes_draining" => "One or more playback nodes are draining.".to_owned(),
        "mesh_stream_stale" => "One or more regional stream deliveries are stale.".to_owned(),
        "mesh_stream_lagging" => {
            "One or more regional stream deliveries are behind the publication head.".to_owned()
        }
        "telemetry_peer_unavailable" => {
            "One or more service telemetry feeds are reconnecting.".to_owned()
        }
        "telemetry_snapshot_stale" => "One or more node telemetry snapshots are stale.".to_owned(),
        _ => message.to_owned(),
    }
}

/// Converts two monotonic counter observations into a per-second rate.
/// Counter resets and sub-second samples are ignored instead of producing a
/// misleading spike.
pub fn monotonic_rate_per_second(previous: u64, current: u64, elapsed_ms: u64) -> Option<f64> {
    if current < previous || elapsed_ms < 1_000 {
        return None;
    }
    Some(current.saturating_sub(previous) as f64 * 1_000.0 / elapsed_ms as f64)
}

pub fn state_tone(state: &str) -> &'static str {
    match state.to_ascii_lowercase().as_str() {
        "healthy"
        | "active"
        | "ready"
        | "current"
        | "publishing"
        | "listening"
        | "compiled"
        | "installed"
        | "accepting traffic"
        | "serving"
        | "sessions established"
        | "active source"
        | "warm repair" => "healthy",
        "attention" | "degraded" | "stale" | "lagging" | "pending" | "warming" => "warn",
        "down" | "stalled" | "error" | "failed" | "fatal" | "unavailable" => "error",
        _ => "warn",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONTRIB_PARTIAL: &str = r#"{
      "service":"av-contrib",
      "status":"active",
      "updated_unix_ms":1784102400100,
      "advertised_hls_stream_id":"42",
      "advertised_hls_path":"/42/stream.m3u8",
      "mesh":{"relay_primary_configured":true,"relay_secondary_configured":true,
        "relay_carrier":"private-udp","relay_primary_target":"127.0.0.1:12001",
        "relay_secondary_target":"127.0.0.1:12002","relay_deadline_ms":750,
        "relay_path_observation_source":"controller-seeded","relay_path_loss_fraction":0.01,
        "relay_path_best_direct_rtt_ms":12.0,"relay_path_rtt_ms":13.5,"relay_path_jitter_ms":0.3,"relay_path_queue_delay_ms":1.0,
        "relay_path_observed_at_unix_ms":1784102400000,
        "relay_secondary_path_observation_source":"controller-seeded","relay_secondary_path_loss_fraction":0.002,
        "relay_secondary_path_best_direct_rtt_ms":12.0,"relay_secondary_path_rtt_ms":13.9,
        "relay_secondary_path_jitter_ms":0.2,"relay_secondary_path_queue_delay_ms":0.5,
        "relay_secondary_path_observed_at_unix_ms":1784102400000,
        "media_object_clock_id":"av-contrib-wall-v1","media_object_clock_confidence":"estimated",
        "media_object_clock_estimated_error_ms":1000,"media_object_source_epoch":1784151600000001},
      "publication":{"canonical_epoch":1784151600000001,"head_object":8},
      "listeners":[
        {"protocol":"rist","enabled":true,"bind":"0.0.0.0:27000","output_stream_id":"42","output_hls_path":"/42/stream.m3u8","backend":"pure","profile":"main","flow_id":"0x11223344"},
        {"protocol":"srt","enabled":true,"bind":"0.0.0.0:27001","output_stream_id":"42","output_hls_path":"/42/stream.m3u8"}
      ],
      "runtime":{
        "relay_session":{"objects_sent":7,"source_datagrams":20,"repair_datagrams":5,"primary_lane_objects_succeeded":6,"primary_lane_objects_failed":1,"primary_lane_state":"healthy","secondary_lane_objects_succeeded":7,"secondary_lane_objects_failed":0,"secondary_lane_state":"healthy","surviving_lane_objects":1,"all_lanes_failed_objects":0,"expired_objects":1,"expired_symbols":2,"deadline_hits":6,"deadline_misses":1,"last_deadline_headroom_us":12000,
          "stages":{"total":{"count":7,"p95_us":2500},"encode_wait":{"count":7,"p95_us":100},"encode":{"count":7,"p95_us":700},"schedule":{"count":7,"p95_us":100},"primary_source_send":{"count":20,"p95_us":250},"secondary_source_send":{"count":20,"p95_us":250},"secondary_repair_send":{"count":5,"p95_us":250}}},
        "mesh_forward":{"media_duration":{"count":100,"p95_us":2500},"media_stages":{"encode":{"count":100,"p95_us":700}}},
        "mpeg_ts":{"slots":200,"continuity_errors":2},
        "fmp4":{"parts":9,"video_codec":"h264","video_width":1920,"video_height":1080,"audio_codec":"aac"},
        "ingest_sessions":{"active":1,"started":2,"recent":[{"session_id":9,"protocol":"rist","stream_id_text":"42","state":"active","bytes":1316}]},
        "protocols":[{"protocol":"rist","units":200,"bytes":263200,"active_sessions":1}],
        "streams":[{"stream_id_text":"42","state":"publishing","latest_fmp4_sequence":8,"video_codec":"h264","audio_codec":"aac"}]
      },
      "alerts":[{"level":"warn","code":"mpeg_ts_input_damage","message":"Input damage detected.","count":2,"protocol":"mpeg-ts"}],
      "activity":[{"level":"info","code":"fmp4_part","message":"Part published.","sequence":8,"seen_unix_ms":1784102400090}],
      "health":{"state":"active","input_seen":true,"output_seen":true,"last_input_age_ms":10,"last_output_age_ms":15}
    }"#;

    const EDGE_PARTIAL: &str = r#"{
      "updated_unix_ms":1784102400200,
      "node":{"node_id":"edge-lon","region":"eu-west","continent":"EU","total_storage_bytes":1000,"used_storage_bytes":400,"active_streams":1},
      "relay_session":{"primary_sessions":1,"secondary_sessions":1,"authenticated_sessions":1,"decoded_objects":6,"repair_assisted_objects":2,"fec_recovered_objects":1,"fec_recovered_source_symbols":3,"source_datagrams":20,"repair_datagrams":5,"warm_source_buffered_datagrams":4,"warm_source_buffered_bytes":5200,"warm_source_replayed_datagrams":7,"warm_source_replayed_bytes":9100,"processing_duration_count":8,"processing_duration_sum_us":3200,"processing_duration_max_us":900,"processing_duration_buckets":[0,2,6,8,8,8,8,8,8,8,8,8,8],"publication_to_available_count":6,"publication_to_available_sum_us":1200000,"publication_to_available_max_us":240000,"publication_to_available_buckets":[0,0,0,0,0,0,0,0,0,0,0,0,6,6,6,6],"publication_clock_error_max_us":5000,"failover_controller_state":"healthy","failover_controller_enabled":1,"failover_commands_sent":12,"failover_promotions":1,"failover_demotions":1,"failover_primary_source_age_ms":12,"failover_secondary_repair_age_ms":24,"failover_last_detection_us":351000,"failover_last_promotion_to_source_us":88000,"failover_max_media_gap_us":103000},
      "relay_nodes":[{"node_id":"relay-warm","region":"us-east","relay_session":{"secondary_sessions":1,"controlled_sessions":1,"downstream_children":1,"source_datagrams":20,"repair_datagrams":5,"forwarded_repair_datagrams":5,"processing_duration_count":5,"processing_duration_sum_us":1500,"processing_duration_max_us":500,"processing_duration_buckets":[1,3,5,5,5,5,5,5,5,5,5,5,5],"forward_duration_count":5,"forward_duration_max_us":73,"forward_duration_buckets":[5,5,5],"publication_to_available_count":6,"publication_to_available_sum_us":1200000,"publication_to_available_max_us":240000,"publication_to_available_buckets":[0,0,0,0,0,0,0,0,0,0,0,0,6,6,6,6],"publication_clock_error_max_us":5000,"failover_listeners":1,"failover_commands_received":12,"failover_promotions_applied":1,"failover_demotions_applied":1}}],
      "aggregate":{"node_count":2,"active_streams":1},
      "telemetry":{"fresh_remote_count":1,"stale_remote_count":0},
      "orchestration":{"control_dispatch_ready":true},
      "publication":{"canonical_epoch":1784151600000001,"canonical_epoch_activation_delay_us":180000,"contiguous_object":8,"head_object":8,"gap_count":0},
      "delivery":{"delivery_class":"premium_live","generation":7,"route_state":"active","route_ready":true,"fabric":"dual_parent_dag","path_stretch":1.125,
        "primary":{"node_id":"relay-primary","target":"127.0.0.1:12001","carrier":"private-udp","trust":"controlled network","state":"active source","observation_source":"controller","rtt_us":13500,"jitter_us":300,"loss_ppm":10000},
        "secondary":{"node_id":"relay-secondary","target":"127.0.0.1:12002","carrier":"private-udp","trust":"controlled network","state":"warm repair","observation_source":"controller","rtt_us":13900,"jitter_us":200,"loss_ppm":2000}},
      "nodes":[{"node_id":"edge-lon","region":"eu-west","total_storage_bytes":1000,"used_storage_bytes":400}],
      "edge_services":[{"node_id":"edge-lon","region":"eu-west","playback_base_url":"https://edge.example","active_readers":4,"responses_total":15,"response_duration_count":10,"response_duration_p95_us":900,"response_duration_buckets":[0,0,2,10]}],
      "streams":[{"node_id":"edge-lon","stream_id_text":"42","latest_local_part":8008,"latest_mesh_part":8,"canonical_epoch":1784151600000001,"canonical_epoch_activation_delay_us":180000,"contiguous_object":8,"head_object":8,"gap_count":0,"mesh_lag_parts":0,"last_ingest_age_ms":20,"stale_threshold_ms":3000}],
      "alerts":[{"level":"warn","code":"mesh_stream_lagging","message":"legacy wording","count":1,"stream_id_text":"42"},{"level":"warn","code":"mesh_unknown_peers","message":"obsolete topology","count":2}],
      "activity":[{"level":"info","code":"edge_response","message":"Part served.","count":1,"seen_unix_ms":1784102400180},{"level":"info","code":"provision_node","message":"obsolete control","count":1,"seen_unix_ms":1784102400190}]
    }"#;

    #[test]
    fn realistic_partial_snapshots_parse_current_service_shapes() {
        let contrib: ContribStatus = serde_json::from_str(CONTRIB_PARTIAL).unwrap();
        assert_eq!(contrib.listeners.len(), 2);
        assert_eq!(contrib.runtime.ingest_sessions.active, 1);
        assert_eq!(contrib.runtime.fmp4.video_codec.as_deref(), Some("h264"));
        assert_eq!(contrib.runtime.relay_session.objects_sent, 7);
        assert_eq!(
            contrib.runtime.relay_session.repair_overhead_percent(),
            Some(20.0)
        );
        assert_eq!(contrib.runtime.relay_session.deadline_hits, Some(6));
        assert_eq!(contrib.runtime.relay_session.deadline_misses, Some(1));
        assert_eq!(contrib.runtime.relay_session.expired_objects, Some(1));
        assert_eq!(contrib.runtime.relay_session.primary_lane_objects_failed, 1);
        assert_eq!(contrib.runtime.relay_session.primary_lane_state, "healthy");
        assert_eq!(
            contrib.runtime.relay_session.secondary_lane_state,
            "healthy"
        );
        assert_eq!(contrib.runtime.relay_session.surviving_lane_objects, 1);
        assert_eq!(contrib.runtime.relay_session.all_lanes_failed_objects, 0);
        assert_eq!(contrib.runtime.relay_session.lane_failures(), 1);
        assert_eq!(
            contrib
                .runtime
                .relay_session
                .stages
                .encode
                .percentile_us(95),
            Some(700)
        );
        assert_eq!(
            contrib
                .runtime
                .relay_session
                .stages
                .secondary_repair_send
                .percentile_us(95),
            Some(250)
        );
        let edge: MeshStatus = serde_json::from_str(EDGE_PARTIAL).unwrap();
        assert_eq!(edge.relay_session.authenticated_sessions, 1);
        assert_eq!(edge.relay_session.fec_recovered_objects, 1);
        assert_eq!(edge.relay_session.fec_recovered_source_symbols, 3);
        assert_eq!(edge.relay_session.warm_source_buffered_datagrams, 4);
        assert_eq!(edge.relay_session.warm_source_replayed_datagrams, 7);
        assert_eq!(edge.relay_session.processing_percentile_us(50), Some(500));
        assert_eq!(edge.relay_session.processing_percentile_us(95), Some(1_000));
        assert_eq!(
            edge.relay_session
                .publication_to_available_percentile_us(95),
            Some(250_000)
        );
        assert_eq!(edge.relay_session.publication_clock_error_max_us, 5_000);
        assert_eq!(edge.relay_session.failover_controller_state, "healthy");
        assert_eq!(edge.relay_session.failover_promotions, 1);
        assert_eq!(edge.relay_session.failover_max_media_gap_us, 103_000);
        assert_eq!(edge.edge_services[0].percentile_us(95), Some(900));
        assert_eq!(edge.nodes[0].storage_percent(), Some(40.0));
        assert_eq!(edge.relay_nodes.len(), 1);
        assert_eq!(
            edge.relay_nodes[0].relay_session.forwarded_repair_datagrams,
            5
        );
        assert_eq!(
            edge.relay_nodes[0]
                .relay_session
                .processing_percentile_us(95),
            Some(500)
        );
        assert_eq!(edge.relay_nodes[0].relay_session.failover_listeners, 1);
        assert_eq!(edge.publication.contiguous_object, Some(8));
        assert_eq!(edge.publication.head_object, Some(8));
        assert_eq!(edge.publication.gap_count, Some(0));
        assert_eq!(
            edge.publication.canonical_epoch_activation_delay_us,
            Some(180_000)
        );
        assert_eq!(
            edge.publication.canonical_epoch,
            Some(1_784_151_600_000_001)
        );
        let route = effective_delivery(Some(&edge));
        assert_eq!(
            route.primary.as_ref().and_then(|lane| lane.rtt_us),
            Some(13_500)
        );
        assert_eq!(
            route.secondary.as_ref().and_then(|lane| lane.rtt_us),
            Some(13_900)
        );
        assert_eq!(
            edge.relay_nodes[0]
                .relay_session
                .failover_promotions_applied,
            1
        );
        assert_eq!(
            edge.relay_nodes[0].relay_session.forward_percentile_us(95),
            Some(100)
        );
    }

    #[test]
    fn global_snapshot_embeds_the_canonical_contributor_snapshot() {
        let contributor = serde_json::from_str::<serde_json::Value>(CONTRIB_PARTIAL).unwrap();
        let status: MeshStatus = serde_json::from_value(serde_json::json!({
            "schema": OPERATIONS_SNAPSHOT_SCHEMA,
            "updated_unix_ms": 1_784_102_400_300_u64,
            "contributor": contributor
        }))
        .unwrap();

        assert_eq!(status.schema, OPERATIONS_SNAPSHOT_SCHEMA);
        let contributor = status.contributor.expect("embedded contributor snapshot");
        assert_eq!(contributor.service, "av-contrib");
        assert_eq!(contributor.publication.head_object, Some(8));
    }

    #[test]
    fn removed_snapshot_aliases_are_not_interpreted() {
        let status: MeshStatus = serde_json::from_str(
            r#"{
                "relay_ingress": {"primary_sessions": 9},
                "relay_session": {"repaired_objects": 7},
                "publication": {
                    "source_epoch": 42,
                    "contiguous_watermark": 8,
                    "head_watermark": 9,
                    "gaps": 1
                },
                "delivery": {
                    "class": "interactive",
                    "topology_generation": 4,
                    "topology": "direct"
                }
            }"#,
        )
        .unwrap();

        assert_eq!(status.relay_session.primary_sessions, 0);
        assert_eq!(status.relay_session.repair_assisted_objects, 0);
        assert_eq!(status.publication.canonical_epoch, None);
        assert_eq!(status.publication.contiguous_object, None);
        assert_eq!(status.publication.head_object, None);
        assert_eq!(status.publication.gap_count, None);
        assert!(!status.delivery.has_assignment());
    }

    #[test]
    fn partial_snapshots_use_defaults() {
        let contrib: ContribStatus = serde_json::from_str("{}").unwrap();
        let edge: MeshStatus = serde_json::from_str("{}").unwrap();
        assert_eq!(contrib.runtime.relay_session.errors(), 0);
        assert_eq!(edge.relay_session.errors(), 0);
        assert!(contributor_latency(&contrib).percentile_us(95).is_none());
    }

    #[test]
    fn operations_telemetry_status_parses_bounded_transport_counters() {
        let edge: MeshStatus = serde_json::from_str(
            r#"{
                "orchestration": {
                    "telemetry_fec": {
                        "enabled": true,
                        "interval_ms": 5000,
                        "rate_bps": 32000,
                        "queue_blocks": 1,
                        "snapshots_submitted": 12,
                        "decoded_snapshots": 11,
                        "send_drops": 1,
                        "last_decoded_unix_ms": 1784490000000
                    }
                }
            }"#,
        )
        .unwrap();
        let status = edge.orchestration.telemetry_fec;
        assert!(status.enabled);
        assert_eq!(status.interval_ms, 5_000);
        assert_eq!(status.rate_bps, 32_000);
        assert_eq!(status.queue_blocks, 1);
        assert_eq!(status.snapshots_submitted, 12);
        assert_eq!(status.decoded_snapshots, 11);
        assert_eq!(status.send_drops, 1);
    }

    #[test]
    fn global_collector_and_topology_links_parse_as_optional_rollout_state() {
        let edge: MeshStatus = serde_json::from_str(
            r#"{
                "orchestration": {
                    "collector": {
                        "authority": "needletail-controller",
                        "role": "collector",
                        "leader_node_id": "relay-primary-amsterdam",
                        "leader_region": "europe-west4",
                        "term": 18,
                        "fencing_generation": 43,
                        "quorum_healthy": true,
                        "voters_online": 3,
                        "voters_total": 3,
                        "lease_remaining_ms": 1120,
                        "nodes_current": 9,
                        "nodes_stale": 1,
                        "nodes_awaiting": 0
                    }
                },
                "topology_links": [{
                    "from_node_id": "relay-primary-amsterdam",
                    "to_node_id": "relay-regional-osaka",
                    "role": "primary",
                    "state": "healthy",
                    "throughput_bps": 12000000,
                    "rtt_us": 184000,
                    "generation": 43
                }]
            }"#,
        )
        .unwrap();

        let collector = edge.orchestration.collector;
        assert!(collector.reported());
        assert!(collector.has_committed_live_lease());
        assert_eq!(
            collector.leader_node_id.as_deref(),
            Some("relay-primary-amsterdam")
        );
        assert_eq!(collector.fencing_generation, Some(43));
        assert_eq!(collector.voters_online, Some(3));
        assert_eq!(edge.topology_links.len(), 1);
        assert_eq!(edge.topology_links[0].role, "primary");
        assert_eq!(edge.topology_links[0].throughput_bps, Some(12_000_000));
    }

    #[test]
    fn collector_health_requires_every_split_brain_fence() {
        let committed = OperationsCollectorStatus {
            authority: "needletail-controller".to_owned(),
            role: "collector".to_owned(),
            leader_node_id: Some("collector-a".to_owned()),
            term: Some(8),
            fencing_generation: Some(21),
            quorum_healthy: Some(true),
            voters_online: Some(3),
            voters_total: Some(5),
            lease_remaining_ms: Some(1_000),
            ..OperationsCollectorStatus::default()
        };
        assert!(committed.has_committed_live_lease());

        let mut incomplete = committed.clone();
        incomplete.fencing_generation = None;
        assert!(!incomplete.has_committed_live_lease());

        let mut expired = committed.clone();
        expired.lease_remaining_ms = Some(0);
        assert!(!expired.has_committed_live_lease());

        let mut candidate = committed.clone();
        candidate.role = "candidate".to_owned();
        assert!(!candidate.has_committed_live_lease());

        let mut unbounded = committed.clone();
        unbounded.lease_remaining_ms = Some(MAX_OPERATIONS_LEASE_MS + 1);
        assert!(!unbounded.has_committed_live_lease());

        let mut wrong_authority = committed;
        wrong_authority.authority = "other-controller".to_owned();
        assert!(!wrong_authority.has_committed_live_lease());

        let invalid_voters = OperationsCollectorStatus {
            authority: "needletail-controller".to_owned(),
            role: "collector".to_owned(),
            leader_node_id: Some("collector-a".to_owned()),
            term: Some(8),
            fencing_generation: Some(21),
            quorum_healthy: Some(true),
            voters_online: Some(3),
            voters_total: Some(4),
            lease_remaining_ms: Some(1_000),
            ..OperationsCollectorStatus::default()
        };
        assert!(!invalid_voters.has_committed_live_lease());
    }

    #[test]
    fn cumulative_histogram_produces_requested_percentiles() {
        let buckets = vec![0, 0, 5, 10, 50, 95, 99, 100, 100, 100, 100, 100, 100];
        assert_eq!(histogram_percentile_us(100, &buckets, 50), Some(2_500));
        assert_eq!(histogram_percentile_us(100, &buckets, 95), Some(5_000));
        assert_eq!(histogram_percentile_us(100, &buckets, 99), Some(10_000));
    }

    #[test]
    fn monotonic_counter_rates_ignore_resets_and_short_samples() {
        assert_eq!(monotonic_rate_per_second(1_000, 2_000, 5_000), Some(200.0));
        assert_eq!(monotonic_rate_per_second(2_000, 1_000, 5_000), None);
        assert_eq!(monotonic_rate_per_second(1_000, 2_000, 999), None);
    }

    #[test]
    fn operator_state_tones_mark_unavailable_routes_as_errors() {
        for state in ["down", "error", "failed", "fatal", "stalled", "unavailable"] {
            assert_eq!(state_tone(state), "error");
        }
        assert_eq!(state_tone("degraded"), "warn");
        assert_eq!(state_tone("active"), "healthy");
    }

    #[test]
    fn contributor_latency_does_not_substitute_mesh_histograms() {
        let mut status = ContribStatus::default();
        status.runtime.mesh_forward.media_duration = DurationHistogram {
            count: 1,
            p95_us: Some(2_500),
            ..DurationHistogram::default()
        };
        assert!(!contributor_latency(&status).has_samples());
    }

    #[test]
    fn delivery_assignment_accepts_only_current_fabric_names() {
        let interactive: DeliverySnapshot = serde_json::from_str(
            r#"{"delivery_class":"interactive","generation":42,"path_stretch":1.07,"route_state":"ready","route_ready":true,"fabric":"direct_low_latency","primary":{"node_id":"relay-primary"}}"#,
        )
        .unwrap();
        assert_eq!(interactive.fabric_label(), Some("Direct / one-hop overlay"));
        assert_eq!(interactive.readiness_label(), "ready");

        let broadcast: DeliverySnapshot = serde_json::from_str(
            r#"{"delivery_class":"mass_broadcast","generation":9,"fabric":"dual_parent_dag","primary":{"node_id":"relay-primary"}}"#,
        )
        .unwrap();
        assert_eq!(broadcast.fabric_label(), Some("Dual-parent DAG"));

        for removed in [
            r#"{"delivery_class":"premium-live"}"#,
            r#"{"delivery_class":"mass-broadcast"}"#,
            r#"{"fabric":"dual-parent-dag"}"#,
            r#"{"fabric":"regional-scalable"}"#,
        ] {
            let delivery: DeliverySnapshot = serde_json::from_str(removed).unwrap();
            assert_eq!(delivery.fabric_label(), None);
        }
    }

    #[test]
    fn delivery_readiness_surfaces_conflicting_canonical_fields() {
        for contradictory in [
            DeliverySnapshot {
                delivery_class: Some("interactive".to_owned()),
                generation: Some(1),
                fabric: Some("direct_low_latency".to_owned()),
                primary: Some(RouteLane::default()),
                route_state: Some("active".to_owned()),
                route_ready: Some(false),
                ..DeliverySnapshot::default()
            },
            DeliverySnapshot {
                delivery_class: Some("interactive".to_owned()),
                generation: Some(1),
                fabric: Some("direct_low_latency".to_owned()),
                primary: Some(RouteLane::default()),
                route_state: Some("failed".to_owned()),
                route_ready: Some(true),
                ..DeliverySnapshot::default()
            },
        ] {
            assert_eq!(contradictory.readiness_label(), "inconsistent route state");
        }
        let state_without_proof = DeliverySnapshot {
            delivery_class: Some("interactive".to_owned()),
            generation: Some(1),
            fabric: Some("direct_low_latency".to_owned()),
            primary: Some(RouteLane::default()),
            route_state: Some("active".to_owned()),
            ..DeliverySnapshot::default()
        };
        assert_eq!(
            state_without_proof.readiness_label(),
            "readiness unreported"
        );
        let readiness_without_assignment = DeliverySnapshot {
            route_ready: Some(true),
            ..DeliverySnapshot::default()
        };
        assert_eq!(
            readiness_without_assignment.readiness_label(),
            "assignment incomplete"
        );
        let unknown_state = DeliverySnapshot {
            delivery_class: Some("interactive".to_owned()),
            generation: Some(1),
            fabric: Some("direct_low_latency".to_owned()),
            primary: Some(RouteLane::default()),
            route_state: Some("installed".to_owned()),
            route_ready: Some(true),
            ..DeliverySnapshot::default()
        };
        assert_eq!(unknown_state.readiness_label(), "route state unrecognized");
    }

    #[test]
    fn canonical_contributor_publication_is_parsed() {
        let status: ContribStatus = serde_json::from_str(CONTRIB_PARTIAL).unwrap();
        assert_eq!(status.publication.head_object, Some(8));
        assert!(status.publication.gap_count.is_none());
        assert_eq!(
            status.publication.canonical_epoch,
            Some(1_784_151_600_000_001)
        );
    }

    #[test]
    fn bounded_helpers_cap_untrusted_snapshot_arrays() {
        let mut contrib: ContribStatus = serde_json::from_str(CONTRIB_PARTIAL).unwrap();
        contrib.runtime.streams = (0..100)
            .map(|index| ContribStream {
                stream_id_text: index.to_string(),
                ..ContribStream::default()
            })
            .collect();
        contrib.runtime.ingest_sessions.recent = (0..100)
            .map(|session_id| IngestSession {
                session_id,
                ..IngestSession::default()
            })
            .collect();
        assert_eq!(bounded_contrib_streams(&contrib).len(), MAX_STREAM_ROWS);
        assert_eq!(bounded_ingest_sessions(&contrib).len(), MAX_SESSION_ROWS);

        let mut edge: MeshStatus = serde_json::from_str(EDGE_PARTIAL).unwrap();
        edge.nodes = vec![EdgeNode::default(); 100];
        edge.edge_services = vec![EdgeService::default(); 100];
        edge.relay_nodes = vec![RelayNodeSession::default(); 100];
        edge.streams = vec![EdgeStream::default(); 100];
        assert_eq!(bounded_nodes(&edge).len(), MAX_NODE_ROWS);
        assert_eq!(bounded_edges(&edge).len(), MAX_EDGE_ROWS);
        assert_eq!(bounded_relay_nodes(&edge).len(), MAX_NODE_ROWS);
        assert_eq!(bounded_edge_streams(&edge).len(), MAX_STREAM_ROWS);
    }

    #[test]
    fn product_activity_filters_retired_control_surface_and_stays_bounded() {
        let contrib: ContribStatus = serde_json::from_str(CONTRIB_PARTIAL).unwrap();
        let mut edge: MeshStatus = serde_json::from_str(EDGE_PARTIAL).unwrap();
        edge.activity.extend((0..100).map(|index| MeshActivity {
            level: "info".to_owned(),
            code: format!("edge_delivery_{index}"),
            message: format!("Delivery event {index}."),
            count: 1,
            seen_unix_ms: 1784102400300 + index,
            ..MeshActivity::default()
        }));
        let activity = operational_activity(Some(&contrib), Some(&edge));
        assert_eq!(activity.len(), MAX_EVENT_ROWS);
        assert!(activity.iter().all(|event| event.code != "provision_node"));

        let alerts = operational_alerts(Some(&contrib), Some(&edge));
        assert!(alerts
            .iter()
            .all(|event| event.code != "mesh_unknown_peers"));
        assert!(alerts.iter().any(|event| {
            event.message
                == "One or more regional stream deliveries are behind the publication head."
        }));
        assert!(alerts
            .iter()
            .any(|event| event.code == "relay_emission_deadline_missed"));
        assert!(alerts
            .iter()
            .any(|event| event.code == "relay_symbol_expired"));
    }

    #[test]
    fn delivery_event_filter_removes_only_exact_retired_codes() {
        for retired in [
            "close_node",
            "control_failures",
            "control_skipped",
            "linode_private_discovery_inactive",
            "mesh_no_links",
            "mesh_single_node",
            "mesh_snapshot",
            "mesh_unknown_peers",
            "provision_node",
            "replica_request",
            "warm_stream",
        ] {
            assert!(!include_delivery_event(retired));
        }
        for current in [
            "collector_peer_fenced",
            "operations_control_plane_unavailable",
            "telemetry_peer_unavailable",
        ] {
            assert!(include_delivery_event(current));
        }
    }

    #[test]
    fn canonical_alert_wins_over_a_derived_duplicate() {
        let mut edge: MeshStatus = serde_json::from_str(EDGE_PARTIAL).unwrap();
        edge.relay_session.failover_controller_state = "secondary_unavailable".to_owned();
        edge.relay_session.failover_secondary_unavailable_events = 9;
        edge.alerts.push(MeshAlert {
            level: "critical".to_owned(),
            code: "relay_failover_secondary_unavailable".to_owned(),
            message: "Canonical producer message.".to_owned(),
            count: 4,
            last_seen_unix_ms: Some(edge.updated_unix_ms - 10),
            node_id: Some(edge.node.node_id.clone()),
            ..MeshAlert::default()
        });

        let duplicates = operational_alerts(None, Some(&edge))
            .into_iter()
            .filter(|event| event.code == "relay_failover_secondary_unavailable")
            .collect::<Vec<_>>();
        assert_eq!(duplicates.len(), 1);
        assert_eq!(duplicates[0].message, "Canonical producer message.");
        assert_eq!(duplicates[0].count, 4);
    }

    #[test]
    fn synthetic_failover_alerts_keep_runtime_transition_times() {
        let mut edge: MeshStatus = serde_json::from_str(EDGE_PARTIAL).unwrap();
        edge.updated_unix_ms = 1_784_102_500_000;
        edge.relay_session.failover_controller_state = "secondary_unavailable".to_owned();
        edge.relay_session.failover_secondary_unavailable_events = 2;
        edge.relay_session
            .failover_controller_last_transition_unix_ms = 1_784_102_400_123;

        let alerts = operational_alerts(None, Some(&edge));
        let unavailable = alerts
            .iter()
            .find(|event| event.code == "relay_failover_secondary_unavailable")
            .unwrap();
        assert_eq!(unavailable.seen_unix_ms, 1_784_102_400_123);

        edge.relay_session
            .failover_controller_last_transition_unix_ms = 0;
        let alerts = operational_alerts(None, Some(&edge));
        let unavailable = alerts
            .iter()
            .find(|event| event.code == "relay_failover_secondary_unavailable")
            .unwrap();
        assert_eq!(unavailable.seen_unix_ms, edge.updated_unix_ms);

        let relay = edge.relay_nodes.first_mut().unwrap();
        relay.relay_session.failover_lease_expirations = 3;
        relay
            .relay_session
            .failover_listener_last_transition_unix_ms = 1_784_102_401_234;
        assert!(operational_alerts(None, Some(&edge))
            .iter()
            .all(|event| event.code != "relay_failover_lease_expired"));
        let activity = operational_activity(None, Some(&edge));
        let expiry = activity
            .iter()
            .find(|event| event.code == "relay_failover_lease_expired")
            .unwrap();
        assert_eq!(expiry.level, "info");
        assert_eq!(expiry.seen_unix_ms, 1_784_102_401_234);
    }

    #[test]
    fn missing_canonical_delivery_does_not_invent_an_assignment() {
        let delivery = effective_delivery(None);
        assert!(!delivery.has_assignment());
        assert_eq!(delivery.readiness_label(), "awaiting route assignment");
    }

    #[test]
    fn canonical_delivery_assignment_is_used_without_synthesis() {
        let edge: MeshStatus = serde_json::from_str(EDGE_PARTIAL).unwrap();
        let delivery = effective_delivery(Some(&edge));
        assert_eq!(delivery.fabric_label(), Some("Dual-parent DAG"));
        assert_eq!(delivery.readiness_label(), "ready");
        assert_eq!(delivery.generation, Some(7));
        assert!((delivery.path_stretch.expect("measured stretch") - 1.125).abs() < 0.000_001);
        assert_eq!(
            delivery.primary.and_then(|lane| lane.node_id).as_deref(),
            Some("relay-primary")
        );
        assert_eq!(
            delivery.secondary.and_then(|lane| lane.node_id).as_deref(),
            Some("relay-secondary")
        );
    }
}
