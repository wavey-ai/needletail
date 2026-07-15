use std::cmp::Ordering;
use std::fmt;

use media_object::{
    AudienceId, DescriptorId, EdgeId, EndpointId, LiveMonitorTransport, MediaClass,
    MediaEndpointDescriptorV1, MediaEndpointDescriptorV1Params, MediaEndpointTransport, Operation,
    SessionId, SourceId, TenantId, MEDIA_CONTROL_MAX_SCOPE_IDS,
};
use serde::{Deserialize, Serialize};

use crate::error::{
    ControllerError, ControllerErrorCode, ControllerErrorDetail, ControllerStage, Result,
    RouteRefusalReason,
};

const MAX_ROUTE_CANDIDATES: usize = 256;
const DEFAULT_MAX_PROBE_AGE_SECONDS: i64 = 10;
const MAX_FUTURE_PROBE_SKEW_SECONDS: i64 = 5;
const REDACTED: &str = "[REDACTED]";

/// Codec representation selected explicitly by route admission.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Codec {
    Opus,
    Pcm,
}

/// Deadline semantics used to keep live and bulk routes distinct.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DeadlineClass {
    Interactive,
    LiveMonitor,
    BulkTransfer,
}

/// Non-authoritative client capabilities considered during admission.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AdmissionClientProfile {
    pub supported_transports: Vec<MediaEndpointTransport>,
    pub supported_codecs: Vec<Codec>,
    pub requested_live_transport: LiveMonitorTransport,
    pub max_channels: u16,
    pub receiver_allowance_ms: u16,
}

/// Independent warm path attached to one primary candidate.
#[derive(Clone, Eq, PartialEq)]
pub struct WarmRepairPath {
    pub edge_id: EdgeId,
    pub healthy_until: i64,
    pub independent_failure_domain: bool,
}

impl fmt::Debug for WarmRepairPath {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("WarmRepairPath")
            .field("edge_id", &REDACTED)
            .field("healthy_until", &self.healthy_until)
            .field(
                "independent_failure_domain",
                &self.independent_failure_domain,
            )
            .finish()
    }
}

/// Trusted desired-state candidate compiled by Needletail topology control.
#[derive(Clone, Eq, PartialEq)]
pub struct RouteCandidate {
    pub edge_id: EdgeId,
    pub binding_generation: u64,
    pub topology_generation: u64,
    pub desired_state_active: bool,
    pub healthy_until: i64,
    pub probe_observed_at: i64,
    pub estimated_rtt_ms: u32,
    pub transport: MediaEndpointTransport,
    pub codecs: Vec<Codec>,
    pub deadline_classes: Vec<DeadlineClass>,
    pub media_classes: Vec<MediaClass>,
    pub source_ids: Vec<SourceId>,
    pub audience_ids: Vec<AudienceId>,
    pub max_channels: u16,
    pub max_bitrate: u64,
    pub max_datagram_bytes: u32,
    pub available_bitrate: u64,
    pub available_session_slots: u32,
    pub cost_score: u32,
    pub cost_allowed: bool,
    pub origin: String,
    pub path: String,
    pub warm_repair: Option<WarmRepairPath>,
}

impl fmt::Debug for RouteCandidate {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RouteCandidate")
            .field("edge_id", &REDACTED)
            .field("binding_generation", &self.binding_generation)
            .field("topology_generation", &self.topology_generation)
            .field("desired_state_active", &self.desired_state_active)
            .field("healthy_until", &self.healthy_until)
            .field("probe_observed_at", &self.probe_observed_at)
            .field("estimated_rtt_ms", &self.estimated_rtt_ms)
            .field("transport", &self.transport)
            .field("codecs", &self.codecs)
            .field("deadline_classes", &self.deadline_classes)
            .field("media_classes", &self.media_classes)
            .field("source_count", &self.source_ids.len())
            .field("audience_count", &self.audience_ids.len())
            .field("max_channels", &self.max_channels)
            .field("max_bitrate", &self.max_bitrate)
            .field("max_datagram_bytes", &self.max_datagram_bytes)
            .field("available_bitrate", &self.available_bitrate)
            .field("available_session_slots", &self.available_session_slots)
            .field("cost_score", &self.cost_score)
            .field("cost_allowed", &self.cost_allowed)
            .field("origin", &REDACTED)
            .field("path", &REDACTED)
            .field("warm_repair", &self.warm_repair)
            .finish()
    }
}

/// Exact trusted/requested context used for deterministic route selection.
pub struct RouteSelectionRequest<'a> {
    pub tenant_id: &'a TenantId,
    pub session_id: &'a SessionId,
    pub session_epoch: u64,
    pub endpoint_id: &'a EndpointId,
    pub descriptor_id: &'a DescriptorId,
    pub topology_generation: u64,
    pub operation: Operation,
    pub media_class: MediaClass,
    pub source_ids: &'a [SourceId],
    pub audience_ids: &'a [AudienceId],
    pub deadline_class: DeadlineClass,
    pub require_independent_repair: bool,
    pub requested_channels: u16,
    pub requested_bitrate: u64,
    pub requested_datagram_bytes: u32,
    pub client: &'a AdmissionClientProfile,
    pub now: i64,
    pub expires_at: i64,
}

impl fmt::Debug for RouteSelectionRequest<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RouteSelectionRequest")
            .field("tenant_id", &REDACTED)
            .field("session_id", &REDACTED)
            .field("session_epoch", &self.session_epoch)
            .field("endpoint_id", &REDACTED)
            .field("descriptor_id", &REDACTED)
            .field("topology_generation", &self.topology_generation)
            .field("operation", &self.operation)
            .field("media_class", &self.media_class)
            .field("source_count", &self.source_ids.len())
            .field("audience_count", &self.audience_ids.len())
            .field("deadline_class", &self.deadline_class)
            .field(
                "require_independent_repair",
                &self.require_independent_repair,
            )
            .field("requested_channels", &self.requested_channels)
            .field("requested_bitrate", &self.requested_bitrate)
            .field("requested_datagram_bytes", &self.requested_datagram_bytes)
            .field("client", &self.client)
            .field("now", &self.now)
            .field("expires_at", &self.expires_at)
            .finish()
    }
}

/// Qualified route, explicit limits, and non-authorizing endpoint descriptor.
pub struct RouteAdmission {
    pub(crate) descriptor: MediaEndpointDescriptorV1,
    pub(crate) edge_ids: Vec<EdgeId>,
    pub(crate) binding_generation: u64,
    pub(crate) topology_generation: u64,
    pub(crate) codec: Codec,
    pub(crate) max_channels: u16,
    pub(crate) max_bitrate: u64,
    pub(crate) max_datagram_bytes: u32,
}

impl RouteAdmission {
    /// Return the non-authorizing selected endpoint descriptor.
    #[must_use]
    pub const fn descriptor(&self) -> &MediaEndpointDescriptorV1 {
        &self.descriptor
    }

    /// Return the explicitly authorized primary and optional warm edge scope.
    #[must_use]
    pub fn edge_ids(&self) -> &[EdgeId] {
        &self.edge_ids
    }

    /// Return the selected live representation without claiming losslessness.
    #[must_use]
    pub const fn codec(&self) -> Codec {
        self.codec
    }
}

impl fmt::Debug for RouteAdmission {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RouteAdmission")
            .field("descriptor", &self.descriptor)
            .field("edge_count", &self.edge_ids.len())
            .field("binding_generation", &self.binding_generation)
            .field("topology_generation", &self.topology_generation)
            .field("codec", &self.codec)
            .field("max_channels", &self.max_channels)
            .field("max_bitrate", &self.max_bitrate)
            .field("max_datagram_bytes", &self.max_datagram_bytes)
            .finish()
    }
}

/// Deterministic lowest-qualified-route selector.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RouteSelector {
    max_probe_age_seconds: i64,
}

impl Default for RouteSelector {
    fn default() -> Self {
        Self {
            max_probe_age_seconds: DEFAULT_MAX_PROBE_AGE_SECONDS,
        }
    }
}

impl RouteSelector {
    /// Construct a selector with a bounded path-probe freshness requirement.
    ///
    /// # Errors
    ///
    /// Returns an error unless freshness is between one and 30 seconds.
    pub fn new(max_probe_age_seconds: i64) -> Result<Self> {
        if !(1..=30).contains(&max_probe_age_seconds) {
            return Err(ControllerError::new(
                ControllerErrorCode::InvalidControllerState,
                ControllerStage::RouteAdmission,
                "route probe freshness must be between one and 30 seconds",
            ));
        }
        Ok(Self {
            max_probe_age_seconds,
        })
    }

    /// Select the lowest-RTT qualified candidate with stable cost/ID tie breaks.
    ///
    /// # Errors
    ///
    /// Returns a stable refusal reason when no candidate meets every admission
    /// constraint. It never silently weakens transport, codec, repair, or limits.
    pub fn select(
        self,
        request: &RouteSelectionRequest<'_>,
        candidates: &[RouteCandidate],
    ) -> Result<RouteAdmission> {
        if candidates.is_empty() {
            return Err(route_refused(RouteRefusalReason::NoCandidates));
        }
        if candidates.len() > MAX_ROUTE_CANDIDATES
            || request.source_ids.len() > MEDIA_CONTROL_MAX_SCOPE_IDS
            || request.audience_ids.len() > MEDIA_CONTROL_MAX_SCOPE_IDS
        {
            return Err(ControllerError::new(
                ControllerErrorCode::InvalidRequest,
                ControllerStage::RouteAdmission,
                "route admission input exceeds its fixed bounds",
            ));
        }

        let mut admitted = Vec::new();
        let mut closest_refusal = (0_u8, RouteRefusalReason::NoQualifiedRoute);
        for candidate in candidates {
            match self.qualify(request, candidate) {
                Ok(admission) => admitted.push((candidate, admission)),
                Err((progress, reason)) => {
                    if progress > closest_refusal.0
                        || (progress == closest_refusal.0 && reason < closest_refusal.1)
                    {
                        closest_refusal = (progress, reason);
                    }
                }
            }
        }
        admitted.sort_by(|(left, _), (right, _)| compare_candidates(left, right));
        admitted
            .into_iter()
            .next()
            .map(|(_, admission)| admission)
            .ok_or_else(|| route_refused(closest_refusal.1))
    }

    fn qualify(
        self,
        request: &RouteSelectionRequest<'_>,
        candidate: &RouteCandidate,
    ) -> std::result::Result<RouteAdmission, (u8, RouteRefusalReason)> {
        let reject = |progress, reason| Err((progress, reason));
        if !candidate.desired_state_active {
            return reject(1, RouteRefusalReason::DesiredStateInactive);
        }
        if candidate.topology_generation != request.topology_generation {
            return reject(2, RouteRefusalReason::GenerationMismatch);
        }
        if candidate.healthy_until < request.expires_at {
            return reject(3, RouteRefusalReason::LeaseUnhealthy);
        }
        if candidate.probe_observed_at < request.now.saturating_sub(self.max_probe_age_seconds)
            || candidate.probe_observed_at
                > request.now.saturating_add(MAX_FUTURE_PROBE_SKEW_SECONDS)
        {
            return reject(4, RouteRefusalReason::ProbeStale);
        }
        if !candidate.deadline_classes.contains(&request.deadline_class) {
            return reject(5, RouteRefusalReason::DeadlineUnsupported);
        }
        if !candidate.media_classes.contains(&request.media_class) {
            return reject(6, RouteRefusalReason::MediaClassUnsupported);
        }
        if !contains_all(&candidate.source_ids, request.source_ids)
            || !contains_all(&candidate.audience_ids, request.audience_ids)
        {
            return reject(7, RouteRefusalReason::ScopeUnavailable);
        }
        if !request
            .client
            .supported_transports
            .contains(&candidate.transport)
        {
            return reject(8, RouteRefusalReason::TransportUnsupported);
        }
        let codec = selected_codec(request.client, candidate)
            .ok_or((9, RouteRefusalReason::CodecUnsupported))?;
        if request.requested_channels == 0
            || request.requested_channels > request.client.max_channels
            || request.requested_channels > candidate.max_channels
            || request.requested_bitrate == 0
            || request.requested_bitrate > candidate.max_bitrate
            || request.requested_bitrate > candidate.available_bitrate
            || request.requested_datagram_bytes == 0
            || request.requested_datagram_bytes > candidate.max_datagram_bytes
        {
            return reject(10, RouteRefusalReason::CapacityExceeded);
        }
        if candidate.available_session_slots == 0 {
            return reject(11, RouteRefusalReason::SessionCapacityExceeded);
        }
        if !candidate.cost_allowed {
            return reject(12, RouteRefusalReason::CostPolicyRefused);
        }

        let mut edge_ids = vec![candidate.edge_id.clone()];
        if request.require_independent_repair {
            let repair = candidate
                .warm_repair
                .as_ref()
                .filter(|repair| {
                    repair.healthy_until >= request.expires_at
                        && repair.independent_failure_domain
                        && repair.edge_id != candidate.edge_id
                })
                .ok_or((13, RouteRefusalReason::IndependentRepairUnavailable))?;
            edge_ids.push(repair.edge_id.clone());
        }

        let descriptor = MediaEndpointDescriptorV1::new(MediaEndpointDescriptorV1Params {
            descriptor_id: request.descriptor_id.clone(),
            tenant_id: request.tenant_id.clone(),
            session_id: request.session_id.clone(),
            session_epoch: request.session_epoch,
            endpoint_id: request.endpoint_id.clone(),
            edge_id: candidate.edge_id.clone(),
            binding_generation: candidate.binding_generation,
            topology_generation: candidate.topology_generation,
            transport: candidate.transport,
            origin: candidate.origin.clone(),
            path: candidate.path.clone(),
            expires_at: request.expires_at,
        })
        .map_err(|_error| (14, RouteRefusalReason::InvalidDescriptor))?;

        edge_ids.sort();
        Ok(RouteAdmission {
            descriptor,
            edge_ids,
            binding_generation: candidate.binding_generation,
            topology_generation: candidate.topology_generation,
            codec,
            max_channels: request.requested_channels,
            max_bitrate: request.requested_bitrate,
            max_datagram_bytes: request.requested_datagram_bytes,
        })
    }
}

fn selected_codec(client: &AdmissionClientProfile, candidate: &RouteCandidate) -> Option<Codec> {
    let requested = match client.requested_live_transport {
        LiveMonitorTransport::Opus | LiveMonitorTransport::Auto => Codec::Opus,
        LiveMonitorTransport::PcmIfAdmitted => Codec::Pcm,
    };
    (client.supported_codecs.contains(&requested) && candidate.codecs.contains(&requested))
        .then_some(requested)
}

fn contains_all<T: Eq>(available: &[T], requested: &[T]) -> bool {
    requested.iter().all(|item| available.contains(item))
}

fn compare_candidates(left: &RouteCandidate, right: &RouteCandidate) -> Ordering {
    left.estimated_rtt_ms
        .cmp(&right.estimated_rtt_ms)
        .then_with(|| left.cost_score.cmp(&right.cost_score))
        .then_with(|| transport_rank(left.transport).cmp(&transport_rank(right.transport)))
        .then_with(|| left.edge_id.as_str().cmp(right.edge_id.as_str()))
}

const fn transport_rank(transport: MediaEndpointTransport) -> u8 {
    match transport {
        MediaEndpointTransport::WebtransportDatagram => 0,
        MediaEndpointTransport::NativeDatagram => 1,
        MediaEndpointTransport::LlHls => 2,
    }
}

fn route_refused(reason: RouteRefusalReason) -> ControllerError {
    ControllerError::new(
        ControllerErrorCode::RouteRefused,
        ControllerStage::RouteAdmission,
        "no route satisfied every current generation, health, scope, and capacity constraint",
    )
    .with_detail(ControllerErrorDetail::Route(reason))
}
