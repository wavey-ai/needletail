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
    pub media_object_clock_id: String,
    pub media_object_clock_confidence: String,
    pub media_object_clock_estimated_error_ms: u64,
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

#[derive(Clone, Copy, Debug, Default, Deserialize)]
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
    pub expired_objects: Option<u64>,
    pub deadline_hits: Option<u64>,
    pub deadline_misses: Option<u64>,
    pub last_deadline_unix_us: Option<u64>,
    pub last_deadline_headroom_us: Option<u64>,
}

impl RelayEmission {
    pub fn repair_overhead_percent(self) -> Option<f64> {
        let total = self.source_datagrams.saturating_add(self.repair_datagrams);
        (total > 0).then(|| self.repair_datagrams as f64 * 100.0 / total as f64)
    }

    pub fn errors(self) -> u64 {
        self.encode_errors
            .saturating_add(self.source_errors)
            .saturating_add(self.repair_errors)
    }
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
    pub updated_unix_ms: u64,
    pub node: EdgeNode,
    #[serde(alias = "relay_ingress")]
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
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct EdgeNode {
    pub node_id: String,
    pub region: String,
    pub continent: String,
    pub latitude: f64,
    pub longitude: f64,
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
    pub repaired_objects: u64,
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
    #[serde(alias = "contiguous_watermark")]
    pub contiguous_object: Option<u64>,
    #[serde(alias = "head_watermark")]
    pub head_object: Option<u64>,
    #[serde(alias = "gaps")]
    pub gap_count: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct DeliverySnapshot {
    #[serde(alias = "class")]
    pub delivery_class: Option<String>,
    #[serde(alias = "topology_generation")]
    pub generation: Option<u64>,
    pub route_state: Option<String>,
    pub route_ready: Option<bool>,
    #[serde(alias = "topology", alias = "lane")]
    pub fabric: Option<String>,
    pub path_stretch: Option<f64>,
    pub stream_id_text: Option<String>,
    pub destination: Option<String>,
    pub primary: Option<RouteLane>,
    pub secondary: Option<RouteLane>,
}

impl DeliverySnapshot {
    pub fn has_program(&self) -> bool {
        self.delivery_class.is_some()
            || self.generation.is_some()
            || self.route_state.is_some()
            || self.fabric.is_some()
            || self.primary.is_some()
            || self.secondary.is_some()
    }

    pub fn fabric_label(&self) -> Option<&'static str> {
        if let Some(fabric) = self.fabric.as_deref() {
            let lower = fabric.to_ascii_lowercase();
            if lower.contains("latency") || lower.contains("fast") || lower.contains("direct") {
                return Some("Low-latency lane");
            }
            if lower.contains("dag") || lower.contains("scalable") || lower.contains("regional") {
                return Some("Scalable DAG");
            }
        }
        match self.delivery_class.as_deref() {
            Some("interactive") => Some("Low-latency lane"),
            Some("premium-live" | "premium_live" | "mass-broadcast" | "mass_broadcast") => {
                Some("Scalable DAG")
            }
            _ => None,
        }
    }

    pub fn readiness_label(&self) -> &'static str {
        if self.route_ready == Some(true)
            || self.route_state.as_deref().is_some_and(|state| {
                matches!(
                    state.to_ascii_lowercase().as_str(),
                    "ready" | "active" | "compiled" | "installed"
                )
            })
        {
            "ready"
        } else if self.primary.is_some() || self.secondary.is_some() {
            "carrier configured"
        } else {
            "awaiting route program"
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
    if status.runtime.ingest_latency.has_samples() {
        &status.runtime.ingest_latency
    } else if status.runtime.mesh_forward.media_duration.has_samples() {
        &status.runtime.mesh_forward.media_duration
    } else {
        &status.runtime.mesh_forward.stream_duration
    }
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

pub fn publication_from_contrib(status: &ContribStatus) -> PublicationSnapshot {
    if status.publication.contiguous_object.is_some()
        || status.publication.head_object.is_some()
        || status.publication.gap_count.is_some()
    {
        return status.publication.clone();
    }
    PublicationSnapshot {
        contiguous_object: None,
        head_object: status
            .runtime
            .streams
            .iter()
            .filter_map(|stream| stream.latest_fmp4_sequence)
            .max(),
        gap_count: None,
    }
}

pub fn publication_from_edge(status: &MeshStatus) -> PublicationSnapshot {
    if status.publication.contiguous_object.is_some()
        || status.publication.head_object.is_some()
        || status.publication.gap_count.is_some()
    {
        return status.publication.clone();
    }
    let gaps = status
        .streams
        .iter()
        .filter_map(|stream| stream.gap_count)
        .collect::<Vec<_>>();
    PublicationSnapshot {
        contiguous_object: status
            .streams
            .iter()
            .filter_map(|stream| stream.contiguous_object)
            .max(),
        head_object: status
            .streams
            .iter()
            .filter_map(|stream| stream.head_object.or(stream.latest_local_part))
            .max(),
        gap_count: (!gaps.is_empty()).then(|| gaps.into_iter().sum()),
    }
}

pub fn effective_delivery(
    contrib: Option<&ContribStatus>,
    edge: Option<&MeshStatus>,
) -> DeliverySnapshot {
    if let Some(delivery) = edge
        .map(|status| &status.delivery)
        .filter(|d| d.has_program())
    {
        return delivery.clone();
    }
    if let Some(delivery) = contrib
        .map(|status| &status.delivery)
        .filter(|delivery| delivery.has_program())
    {
        return delivery.clone();
    }
    let Some(status) = contrib else {
        return DeliverySnapshot::default();
    };
    let carrier = status.mesh.relay_carrier.clone();
    let trust = status.mesh.relay_trust.clone().or_else(|| {
        (carrier.as_deref() == Some("private-udp")).then(|| "controlled network".to_owned())
    });
    DeliverySnapshot {
        delivery_class: Some(if status.mesh.relay_secondary_configured {
            "premium_live".to_owned()
        } else {
            "interactive".to_owned()
        }),
        generation: (status.mesh.relay_topology_generation > 0)
            .then_some(status.mesh.relay_topology_generation),
        route_state: edge.map(|edge| {
            if edge.relay_session.primary_sessions > 0
                && edge.relay_session.secondary_sessions > 0
                && edge.relay_session.errors() == 0
            {
                "active".to_owned()
            } else {
                "warming".to_owned()
            }
        }),
        route_ready: edge.map(|edge| {
            edge.relay_session.primary_sessions > 0
                && edge.relay_session.secondary_sessions > 0
                && edge.relay_session.errors() == 0
        }),
        fabric: Some(if status.mesh.relay_secondary_configured {
            "dual_parent_dag".to_owned()
        } else {
            "direct_low_latency".to_owned()
        }),
        path_stretch: (status.mesh.relay_path_rtt_ms.is_finite()
            && status.mesh.relay_path_best_direct_rtt_ms.is_finite()
            && status.mesh.relay_path_rtt_ms > 0.0
            && status.mesh.relay_path_best_direct_rtt_ms > 0.0)
            .then(|| status.mesh.relay_path_rtt_ms / status.mesh.relay_path_best_direct_rtt_ms),
        primary: (status.mesh.relay_primary_configured
            || status.mesh.relay_primary_target.is_some())
        .then(|| RouteLane {
            node_id: status.mesh.relay_primary_id.clone(),
            target: status.mesh.relay_primary_target.clone(),
            carrier: carrier.clone(),
            trust: trust.clone(),
            state: Some("active source".to_owned()),
            rtt_us: finite_positive_milliseconds_to_us(status.mesh.relay_path_rtt_ms),
            jitter_us: finite_positive_milliseconds_to_us(status.mesh.relay_path_jitter_ms),
            loss_ppm: finite_fraction_to_ppm(status.mesh.relay_path_loss_fraction),
            ..RouteLane::default()
        }),
        secondary: (status.mesh.relay_secondary_configured
            || status.mesh.relay_secondary_target.is_some())
        .then(|| RouteLane {
            node_id: status.mesh.relay_secondary_id.clone(),
            target: status.mesh.relay_secondary_target.clone(),
            carrier,
            trust,
            state: Some("warm repair".to_owned()),
            ..RouteLane::default()
        }),
        ..DeliverySnapshot::default()
    }
}

fn finite_positive_milliseconds_to_us(value: f64) -> Option<u64> {
    (value.is_finite() && value > 0.0).then(|| (value * 1_000.0).round() as u64)
}

fn finite_fraction_to_ppm(value: f64) -> Option<u64> {
    (value.is_finite() && value > 0.0).then(|| (value.clamp(0.0, 1.0) * 1_000_000.0).round() as u64)
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
            events.push(OperationalEvent {
                source: EventSource::Delivery,
                level: "error".to_owned(),
                code: "relay_failover_secondary_unavailable".to_owned(),
                message: "Primary source is silent and the warm secondary is not ready."
                    .to_owned(),
                count: status
                    .relay_session
                    .failover_secondary_unavailable_events
                    .max(1),
                seen_unix_ms: status
                    .relay_session
                    .failover_controller_last_transition_unix_ms
                    .max(status.updated_unix_ms),
                context: Some(status.node.node_id.clone()),
            });
        }
        if status.relay_session.failover_command_send_errors > 0 {
            events.push(OperationalEvent {
                source: EventSource::Delivery,
                level: "error".to_owned(),
                code: "relay_failover_control_send_error".to_owned(),
                message: "The edge could not refresh its warm-secondary control lease."
                    .to_owned(),
                count: status.relay_session.failover_command_send_errors,
                seen_unix_ms: status.updated_unix_ms,
                context: Some(status.node.node_id.clone()),
            });
        }
        events.extend(status.relay_nodes.iter().filter_map(|node| {
            (node.relay_session.failover_lease_expirations > 0).then(|| OperationalEvent {
                source: EventSource::Delivery,
                level: "warning".to_owned(),
                code: "relay_failover_lease_expired".to_owned(),
                message: "A warm relay returned to repair-only after its promotion lease expired."
                    .to_owned(),
                count: node.relay_session.failover_lease_expirations,
                seen_unix_ms: node
                    .relay_session
                    .failover_listener_last_transition_unix_ms
                    .max(status.updated_unix_ms),
                context: Some(node.node_id.clone()),
            })
        }));
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

fn include_delivery_event(code: &str) -> bool {
    let code = code.to_ascii_lowercase();
    ![
        "peer",
        "replica",
        "provision",
        "control_",
        "private_discovery",
        "mesh_single_node",
        "mesh_no_links",
        "mesh_unknown",
        "mesh_snapshot",
    ]
    .iter()
    .any(|obsolete| code.contains(obsolete))
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
        "media_object_clock_id":"av-contrib-wall-v1","media_object_clock_confidence":"estimated",
        "media_object_clock_estimated_error_ms":1000},
      "listeners":[
        {"protocol":"rist","enabled":true,"bind":"0.0.0.0:27000","output_stream_id":"42","output_hls_path":"/42/stream.m3u8","backend":"pure","profile":"main","flow_id":"0x11223344"},
        {"protocol":"srt","enabled":true,"bind":"0.0.0.0:27001","output_stream_id":"42","output_hls_path":"/42/stream.m3u8"}
      ],
      "runtime":{
        "relay_session":{"objects_sent":7,"source_datagrams":20,"repair_datagrams":5,"last_deadline_headroom_us":12000},
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
      "relay_session":{"primary_sessions":1,"secondary_sessions":1,"authenticated_sessions":1,"decoded_objects":6,"repaired_objects":2,"source_datagrams":20,"repair_datagrams":5,"publication_to_available_count":6,"publication_to_available_sum_us":1200000,"publication_to_available_max_us":240000,"publication_to_available_buckets":[0,0,0,0,0,0,0,0,0,0,0,0,6,6,6,6],"publication_clock_error_max_us":5000,"failover_controller_state":"healthy","failover_controller_enabled":1,"failover_commands_sent":12,"failover_promotions":1,"failover_demotions":1,"failover_primary_source_age_ms":12,"failover_secondary_repair_age_ms":24,"failover_last_detection_us":351000,"failover_last_promotion_to_source_us":88000,"failover_max_media_gap_us":103000},
      "relay_nodes":[{"node_id":"relay-warm","region":"us-east","relay_session":{"secondary_sessions":1,"controlled_sessions":1,"downstream_children":1,"source_datagrams":20,"repair_datagrams":5,"forwarded_repair_datagrams":5,"forward_duration_count":5,"forward_duration_max_us":73,"forward_duration_buckets":[5,5,5],"publication_to_available_count":6,"publication_to_available_sum_us":1200000,"publication_to_available_max_us":240000,"publication_to_available_buckets":[0,0,0,0,0,0,0,0,0,0,0,0,6,6,6,6],"publication_clock_error_max_us":5000,"failover_listeners":1,"failover_commands_received":12,"failover_promotions_applied":1,"failover_demotions_applied":1}}],
      "aggregate":{"node_count":2,"active_streams":1},
      "telemetry":{"fresh_remote_count":1,"stale_remote_count":0},
      "orchestration":{"control_dispatch_ready":true},
      "nodes":[{"node_id":"edge-lon","region":"eu-west","total_storage_bytes":1000,"used_storage_bytes":400}],
      "edge_services":[{"node_id":"edge-lon","region":"eu-west","playback_base_url":"https://edge.example","active_readers":4,"responses_total":15,"response_duration_count":10,"response_duration_p95_us":900,"response_duration_buckets":[0,0,2,10]}],
      "streams":[{"node_id":"edge-lon","stream_id_text":"42","latest_local_part":8,"latest_mesh_part":8,"last_ingest_age_ms":20,"stale_threshold_ms":3000}],
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
        assert!(contrib.runtime.relay_session.deadline_hits.is_none());
        let route = effective_delivery(Some(&contrib), None);
        assert_eq!(
            route.primary.as_ref().and_then(|lane| lane.rtt_us),
            Some(13_500)
        );
        assert_eq!(
            route.primary.as_ref().and_then(|lane| lane.jitter_us),
            Some(300)
        );
        assert_eq!(
            route.primary.as_ref().and_then(|lane| lane.loss_ppm),
            Some(10_000)
        );

        let edge: MeshStatus = serde_json::from_str(EDGE_PARTIAL).unwrap();
        assert_eq!(edge.relay_session.authenticated_sessions, 1);
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
        assert_eq!(edge.relay_nodes[0].relay_session.failover_listeners, 1);
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
    fn partial_snapshots_use_defaults() {
        let contrib: ContribStatus = serde_json::from_str("{}").unwrap();
        let edge: MeshStatus = serde_json::from_str("{}").unwrap();
        assert_eq!(contrib.runtime.relay_session.errors(), 0);
        assert_eq!(edge.relay_session.errors(), 0);
        assert!(contributor_latency(&contrib).percentile_us(95).is_none());
    }

    #[test]
    fn cumulative_histogram_produces_requested_percentiles() {
        let buckets = vec![0, 0, 5, 10, 50, 95, 99, 100, 100, 100, 100, 100, 100];
        assert_eq!(histogram_percentile_us(100, &buckets, 50), Some(2_500));
        assert_eq!(histogram_percentile_us(100, &buckets, 95), Some(5_000));
        assert_eq!(histogram_percentile_us(100, &buckets, 99), Some(10_000));
    }

    #[test]
    fn delivery_program_supports_both_product_fabrics() {
        let interactive: DeliverySnapshot = serde_json::from_str(
            r#"{"class":"interactive","topology_generation":42,"path_stretch":1.07,"route_state":"ready"}"#,
        )
        .unwrap();
        assert_eq!(interactive.fabric_label(), Some("Low-latency lane"));
        assert_eq!(interactive.readiness_label(), "ready");

        let broadcast: DeliverySnapshot =
            serde_json::from_str(r#"{"class":"mass_broadcast","topology":"dual-parent-dag"}"#)
                .unwrap();
        assert_eq!(broadcast.fabric_label(), Some("Scalable DAG"));
    }

    #[test]
    fn current_publication_heads_remain_visible_during_rollout() {
        let status: ContribStatus = serde_json::from_str(CONTRIB_PARTIAL).unwrap();
        let publication = publication_from_contrib(&status);
        assert_eq!(publication.head_object, Some(8));
        assert!(publication.gap_count.is_none());
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
        edge.streams = vec![EdgeStream::default(); 100];
        assert_eq!(bounded_nodes(&edge).len(), MAX_NODE_ROWS);
        assert_eq!(bounded_edges(&edge).len(), MAX_EDGE_ROWS);
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
    }

    #[test]
    fn configured_carriers_do_not_claim_a_compiled_route() {
        let contrib: ContribStatus = serde_json::from_str(CONTRIB_PARTIAL).unwrap();
        let delivery = effective_delivery(Some(&contrib), None);
        assert_eq!(delivery.readiness_label(), "carrier configured");
        assert!(delivery.generation.is_none());
        assert_eq!(
            delivery.primary.unwrap().state.as_deref(),
            Some("active source")
        );
        assert_eq!(
            delivery.secondary.unwrap().state.as_deref(),
            Some("warm repair")
        );
    }

    #[test]
    fn installed_dual_parent_sessions_surface_an_active_compiled_dag() {
        let mut contrib: ContribStatus = serde_json::from_str(CONTRIB_PARTIAL).unwrap();
        contrib.mesh.relay_topology_generation = 7;
        contrib.mesh.relay_primary_id = Some("relay-primary".to_owned());
        contrib.mesh.relay_secondary_id = Some("relay-secondary".to_owned());
        let mut edge: MeshStatus = serde_json::from_str(EDGE_PARTIAL).unwrap();
        edge.relay_session.controlled_sessions = 2;
        edge.relay_session.datagrams_rejected = 0;

        let delivery = effective_delivery(Some(&contrib), Some(&edge));
        assert_eq!(delivery.fabric_label(), Some("Scalable DAG"));
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
