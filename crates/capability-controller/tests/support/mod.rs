#![allow(dead_code)]

use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use capability_controller::{
    AdmissionClientProfile, AuthorizationMode, BrokerAuthorizationRequest, CapabilityController,
    CapabilityTelemetryEvent, Codec, ControllerConfig, DesiredMediaState, Ed25519CapabilityIssuer,
    EntropyError, EntropySource, FeatureGates, InMemoryExchangeStore, LifetimePolicy,
    RouteCandidate, RouteSelector, TelemetrySink, WarmRepairPath,
};
use ed25519_dalek::SigningKey;
use media_object::{
    AudienceId, AuthorizationFactId, ContributorId, EdgeId, EffectiveRole, EndpointId,
    LiveMonitorTransport, MediaAuthorizationFactV1, MediaAuthorizationFactV1Params,
    MediaAuthorizationRequestV1, MediaClass, MediaEndpointTransport, Operation, ParticipantId,
    SessionId, SessionWorkflowMode, SourceId, SubjectId, TenantId,
};
use sha2::{Digest, Sha256};

pub const NOW: i64 = 1_784_131_200;
pub const THUMBPRINT: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

#[derive(Clone, Default)]
pub struct CounterEntropy {
    counter: Arc<AtomicU64>,
}

impl EntropySource for CounterEntropy {
    fn fill_bytes(&self, destination: &mut [u8]) -> Result<(), EntropyError> {
        let mut offset = 0;
        while offset < destination.len() {
            let count = self.counter.fetch_add(1, Ordering::Relaxed);
            let digest = Sha256::digest(count.to_be_bytes());
            let length = (destination.len() - offset).min(digest.len());
            destination[offset..offset + length].copy_from_slice(&digest[..length]);
            offset += length;
        }
        Ok(())
    }
}

#[derive(Clone)]
pub struct MockIdentity {
    fact: Arc<Mutex<MediaAuthorizationFactV1>>,
    calls: Arc<AtomicUsize>,
}

impl MockIdentity {
    pub fn new(fact: MediaAuthorizationFactV1) -> Self {
        Self {
            fact: Arc::new(Mutex::new(fact)),
            calls: Arc::new(AtomicUsize::new(0)),
        }
    }

    pub fn calls(&self) -> usize {
        self.calls.load(Ordering::Acquire)
    }

    pub fn replace(&self, fact: MediaAuthorizationFactV1) {
        *self.fact.lock().unwrap() = fact;
    }
}

impl capability_controller::IdentityAuthorizationClient for MockIdentity {
    fn authorize(
        &self,
        _session_id: &SessionId,
        _request: &MediaAuthorizationRequestV1,
        _now: i64,
    ) -> capability_controller::Result<MediaAuthorizationFactV1> {
        self.calls.fetch_add(1, Ordering::AcqRel);
        Ok(self.fact.lock().unwrap().clone())
    }
}

#[derive(Clone, Default)]
pub struct RecordingTelemetry {
    events: Arc<Mutex<Vec<CapabilityTelemetryEvent>>>,
}

impl RecordingTelemetry {
    pub fn events(&self) -> Vec<CapabilityTelemetryEvent> {
        self.events.lock().unwrap().clone()
    }
}

impl TelemetrySink for RecordingTelemetry {
    fn record(&self, event: CapabilityTelemetryEvent) {
        self.events.lock().unwrap().push(event);
    }
}

pub type TestController =
    CapabilityController<MockIdentity, InMemoryExchangeStore, CounterEntropy, RecordingTelemetry>;

pub struct TestIds {
    pub tenant: TenantId,
    pub session: SessionId,
    pub participant: ParticipantId,
    pub endpoint: EndpointId,
    pub contributor: ContributorId,
    pub source: SourceId,
    pub talkback_audience: AudienceId,
    pub primary_edge: EdgeId,
    pub repair_edge: EdgeId,
}

impl TestIds {
    pub fn new() -> Self {
        Self {
            tenant: TenantId::new("ten_wavey").unwrap(),
            session: SessionId::new("ses_mix").unwrap(),
            participant: ParticipantId::new("par_producer").unwrap(),
            endpoint: EndpointId::new("ep_logic").unwrap(),
            contributor: ContributorId::new("con_logic").unwrap(),
            source: SourceId::new("src_mix").unwrap(),
            talkback_audience: AudienceId::new("aud_producer_return").unwrap(),
            primary_edge: EdgeId::new("edge_lon").unwrap(),
            repair_edge: EdgeId::new("edge_ams").unwrap(),
        }
    }
}

pub struct Harness {
    pub controller: Arc<TestController>,
    pub identity: MockIdentity,
    pub telemetry: RecordingTelemetry,
    pub ids: TestIds,
    pub verification_key: [u8; 32],
}

pub fn harness(operation: Operation) -> Harness {
    harness_with_gates(operation, FeatureGates::enabled())
}

pub fn harness_with_gates(operation: Operation, feature_gates: FeatureGates) -> Harness {
    let ids = TestIds::new();
    let fact = fact(&ids, operation, 3, 7, NOW, Some(NOW + 3_600));
    let identity = MockIdentity::new(fact);
    let telemetry = RecordingTelemetry::default();
    let signing_key = SigningKey::from_bytes(&[7; 32]);
    let verification_key = signing_key.verifying_key().to_bytes();
    let issuer = Ed25519CapabilityIssuer::new("key_active_01", signing_key, Vec::new()).unwrap();
    let controller = CapabilityController::new(
        identity.clone(),
        RouteSelector::default(),
        issuer,
        InMemoryExchangeStore::default(),
        CounterEntropy::default(),
        telemetry.clone(),
        ControllerConfig::new(
            "https://control.infidelity.io",
            "av-contrib",
            "av-mesh",
            "take-service",
            "av-mesh",
            "https://media.infidelity.io/v1/playback/bootstrap",
            feature_gates,
            LifetimePolicy::default(),
        )
        .unwrap(),
    );
    Harness {
        controller: Arc::new(controller),
        identity,
        telemetry,
        ids,
        verification_key,
    }
}

pub fn fact(
    ids: &TestIds,
    operation: Operation,
    subject_grant_epoch: u64,
    media_policy_version: u64,
    evaluated_at: i64,
    access_expires_at: Option<i64>,
) -> MediaAuthorizationFactV1 {
    MediaAuthorizationFactV1::new(MediaAuthorizationFactV1Params {
        authorization_fact_id: AuthorizationFactId::new("maf_current").unwrap(),
        session_id: ids.session.clone(),
        session_epoch: 9,
        media_authorization_epoch: 14,
        subject_grant_epoch,
        media_policy_version,
        participant_id: ids.participant.clone(),
        endpoint_id: ids.endpoint.clone(),
        effective_role: EffectiveRole::Producer,
        access_expires_at,
        allowed_operations: vec![operation],
        allowed_media_classes: vec![MediaClass::Program],
        allowed_source_ids: vec![ids.source.clone()],
        allowed_audience_ids: Vec::new(),
        requested_operation: operation,
        requested_media_class: MediaClass::Program,
        take_id: None,
        workflow_mode: SessionWorkflowMode::MixReview,
        evaluated_at,
    })
    .unwrap()
}

pub fn talkback_fact(
    ids: &TestIds,
    operation: Operation,
    subject_grant_epoch: u64,
    media_policy_version: u64,
    evaluated_at: i64,
    access_expires_at: Option<i64>,
) -> MediaAuthorizationFactV1 {
    MediaAuthorizationFactV1::new(MediaAuthorizationFactV1Params {
        authorization_fact_id: AuthorizationFactId::new("maf_talkback").unwrap(),
        session_id: ids.session.clone(),
        session_epoch: 9,
        media_authorization_epoch: 14,
        subject_grant_epoch,
        media_policy_version,
        participant_id: ids.participant.clone(),
        endpoint_id: ids.endpoint.clone(),
        effective_role: EffectiveRole::Producer,
        access_expires_at,
        allowed_operations: vec![operation],
        allowed_media_classes: vec![MediaClass::Talkback],
        allowed_source_ids: Vec::new(),
        allowed_audience_ids: vec![ids.talkback_audience.clone()],
        requested_operation: operation,
        requested_media_class: MediaClass::Talkback,
        take_id: None,
        workflow_mode: SessionWorkflowMode::MixReview,
        evaluated_at,
    })
    .unwrap()
}

pub fn native_request(ids: &TestIds) -> BrokerAuthorizationRequest {
    BrokerAuthorizationRequest {
        mode: AuthorizationMode::NativeMedia,
        subject: SubjectId::new("sub_zeroth_01").unwrap(),
        endpoint_id: ids.endpoint.clone(),
        operation: Operation::Publish,
        media_class: MediaClass::Program,
        source_ids: vec![ids.source.clone()],
        audience_ids: Vec::<AudienceId>::new(),
        take_id: None,
        client_key_thumbprint: Some(THUMBPRINT.to_owned()),
        requested_channels: 2,
        requested_sample_rate_hz: None,
        requested_frame_duration_us: None,
        requested_frame_samples: None,
        requested_bitrate: 512_000,
        requested_datagram_bytes: 1_200,
        client: AdmissionClientProfile {
            supported_transports: vec![MediaEndpointTransport::NativeDatagram],
            supported_codecs: vec![Codec::Opus],
            requested_live_transport: LiveMonitorTransport::Opus,
            max_channels: 2,
            receiver_allowance_ms: 25,
        },
    }
}

pub fn browser_request(ids: &TestIds) -> BrokerAuthorizationRequest {
    BrokerAuthorizationRequest {
        mode: AuthorizationMode::BrowserPlayback,
        subject: SubjectId::new("sub_zeroth_01").unwrap(),
        endpoint_id: ids.endpoint.clone(),
        operation: Operation::Subscribe,
        media_class: MediaClass::Program,
        source_ids: vec![ids.source.clone()],
        audience_ids: Vec::<AudienceId>::new(),
        take_id: None,
        client_key_thumbprint: Some(THUMBPRINT.to_owned()),
        requested_channels: 2,
        requested_sample_rate_hz: None,
        requested_frame_duration_us: None,
        requested_frame_samples: None,
        requested_bitrate: 256_000,
        requested_datagram_bytes: 1_200,
        client: AdmissionClientProfile {
            supported_transports: vec![MediaEndpointTransport::WebtransportDatagram],
            supported_codecs: vec![Codec::Opus],
            requested_live_transport: LiveMonitorTransport::Auto,
            max_channels: 2,
            receiver_allowance_ms: 25,
        },
    }
}

pub fn talkback_request(
    ids: &TestIds,
    mode: AuthorizationMode,
    operation: Operation,
) -> BrokerAuthorizationRequest {
    BrokerAuthorizationRequest {
        mode,
        subject: SubjectId::new("sub_zeroth_01").unwrap(),
        endpoint_id: ids.endpoint.clone(),
        operation,
        media_class: MediaClass::Talkback,
        source_ids: Vec::new(),
        audience_ids: vec![ids.talkback_audience.clone()],
        take_id: None,
        client_key_thumbprint: Some(THUMBPRINT.to_owned()),
        requested_channels: 1,
        requested_sample_rate_hz: Some(48_000),
        requested_frame_duration_us: Some(5_000),
        requested_frame_samples: Some(240),
        requested_bitrate: 96_000,
        requested_datagram_bytes: 512,
        client: AdmissionClientProfile {
            supported_transports: vec![match mode {
                AuthorizationMode::NativeMedia => MediaEndpointTransport::NativeDatagram,
                AuthorizationMode::BrowserPlayback | AuthorizationMode::BrowserTalkback => {
                    MediaEndpointTransport::WebtransportDatagram
                }
            }],
            supported_codecs: vec![Codec::Opus],
            requested_live_transport: LiveMonitorTransport::Opus,
            max_channels: 1,
            receiver_allowance_ms: 10,
        },
    }
}

pub fn candidate(
    ids: &TestIds,
    transport: MediaEndpointTransport,
    edge_id: EdgeId,
    rtt_ms: u32,
) -> RouteCandidate {
    RouteCandidate {
        edge_id,
        binding_generation: 8,
        topology_generation: 52,
        desired_state_active: true,
        healthy_until: NOW + 300,
        probe_observed_at: NOW,
        estimated_rtt_ms: rtt_ms,
        transport,
        codecs: vec![Codec::Opus],
        deadline_classes: vec![capability_controller::DeadlineClass::LiveMonitor],
        media_classes: vec![MediaClass::Program],
        source_ids: vec![ids.source.clone()],
        audience_ids: Vec::new(),
        max_channels: 2,
        max_bitrate: 1_000_000,
        max_datagram_bytes: 1_200,
        available_bitrate: 10_000_000,
        available_session_slots: 100,
        cost_score: 1,
        cost_allowed: true,
        origin: "https://media-lon.infidelity.io".to_owned(),
        path: "/v1/playback/descriptor".to_owned(),
        warm_repair: Some(WarmRepairPath {
            edge_id: ids.repair_edge.clone(),
            healthy_until: NOW + 300,
            independent_failure_domain: true,
        }),
    }
}

pub fn talkback_candidate(
    ids: &TestIds,
    transport: MediaEndpointTransport,
    edge_id: EdgeId,
    rtt_ms: u32,
) -> RouteCandidate {
    RouteCandidate {
        edge_id,
        binding_generation: 8,
        topology_generation: 52,
        desired_state_active: true,
        healthy_until: NOW + 300,
        probe_observed_at: NOW,
        estimated_rtt_ms: rtt_ms,
        transport,
        codecs: vec![Codec::Opus],
        deadline_classes: vec![capability_controller::DeadlineClass::Interactive],
        media_classes: vec![MediaClass::Talkback],
        source_ids: Vec::new(),
        audience_ids: vec![ids.talkback_audience.clone()],
        max_channels: 1,
        max_bitrate: 128_000,
        max_datagram_bytes: 1_200,
        available_bitrate: 10_000_000,
        available_session_slots: 100,
        cost_score: 1,
        cost_allowed: true,
        origin: "https://media-lon.infidelity.io".to_owned(),
        path: "/v1/talkback/descriptor".to_owned(),
        warm_repair: None,
    }
}

pub fn desired<'a>(
    ids: &'a TestIds,
    candidates: &'a [RouteCandidate],
    require_repair: bool,
) -> DesiredMediaState<'a> {
    DesiredMediaState {
        tenant_id: &ids.tenant,
        session_id: &ids.session,
        topology_generation: 52,
        class_authorization_epoch: Some(4),
        contributor_id: Some(&ids.contributor),
        require_independent_repair: require_repair,
        route_candidates: candidates,
    }
}
