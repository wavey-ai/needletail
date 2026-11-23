//! Compile validated Needletail relay programs into executable service state.
//!
//! The topology describes who may forward to whom. `CarrierLink` binds every
//! directed relationship to stable sender/receiver sockets and an explicit
//! RaptorQ symbol lane. The compiler then emits the exact `av-contrib` and
//! `av-mesh` arguments reconciled by a host agent or used by local qualification.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::net::SocketAddr;

use serde::{Deserialize, Serialize};

use crate::relay_topology::{
    FailureDiversityRequirement, FailureDomain, NodeRole, OriginReplica, ParentRole,
    PolicyViolation, RelayTopology, TopologyPolicy, DEFAULT_MAX_EDGES_PER_FORWARDER,
    MAX_ORIGINS_PER_STREAM,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeploymentPurpose {
    LocalQualification,
    SingleProviderQualification,
    MultiProviderQualification,
    Production,
}

impl DeploymentPurpose {
    const fn diversity(self) -> FailureDiversityRequirement {
        match self {
            Self::LocalQualification => FailureDiversityRequirement::DistinctNodes,
            Self::SingleProviderQualification => FailureDiversityRequirement::RegionAndZone,
            Self::MultiProviderQualification | Self::Production => {
                FailureDiversityRequirement::ProviderRegionAsnAndZone
            }
        }
    }

    fn readiness_gaps(self) -> Vec<String> {
        match self {
            Self::LocalQualification => vec![
                "physical_host_diversity_pending".to_owned(),
                "provider_asn_diversity_pending".to_owned(),
                "authenticated_public_carrier_pending".to_owned(),
            ],
            Self::SingleProviderQualification => vec![
                "provider_asn_diversity_pending".to_owned(),
                "authenticated_public_carrier_pending".to_owned(),
            ],
            Self::MultiProviderQualification => {
                vec!["authenticated_public_carrier_pending".to_owned()]
            }
            Self::Production => Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CarrierProfile {
    /// Direct UDP inside an explicitly controlled private network. This is the
    /// benchmark and private-VPC qualification carrier.
    ControlledPrivateUdp,
    /// Direct UDP on explicitly allow-listed public addresses for a controlled
    /// single-provider qualification where no cross-region private fabric is
    /// available. This is evidence-only and is never production-ready.
    ControlledPublicUdp,
    /// Public-Internet production carrier seam. Service argument emission is
    /// gated until the QUIC Datagram backend is enabled in both services.
    QuicDatagram,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RelaySymbolLane {
    Source,
    Repair,
    /// A bounded origin-to-backbone seed carrying source first and repair. It
    /// keeps the second backbone relay ready for immediate promotion.
    SourceAndRepair,
}

impl RelaySymbolLane {
    const fn forward_argument(self) -> &'static str {
        match self {
            Self::Source => "source",
            Self::Repair => "repair",
            Self::SourceAndRepair => "all",
        }
    }
}

/// Runtime socket ownership for one directed topology relationship.
///
/// `sender_bind` and `receiver_bind` are local service binds. `sender_peer` is
/// the stable source address admitted by the child, while `receiver_target` is
/// the address used by the parent. The observed addresses may intentionally be
/// a tunnel, NAT, or qualification emulator endpoint rather than either local
/// bind.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CarrierLink {
    pub parent_node_id: String,
    pub child_node_id: String,
    pub role: ParentRole,
    pub lane: RelaySymbolLane,
    pub sender_bind: SocketAddr,
    pub sender_peer: SocketAddr,
    pub receiver_bind: SocketAddr,
    pub receiver_target: SocketAddr,
}

/// Bounded automatic promotion policy shared by the edge detector and warm
/// forwarder lease. All durations are deterministic integer milliseconds.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailoverPolicy {
    pub primary_silence_ms: u64,
    pub primary_recovery_ms: u64,
    pub secondary_warm_ms: u64,
    pub heartbeat_ms: u64,
    pub lease_ms: u64,
}

/// Controlled-private command carrier for one secondary relationship. The
/// controller is the child/edge and the listener is the warm parent relay.
/// Public deployments map the same compiled relationship to authenticated
/// reliable carrier control.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailoverControlLink {
    pub forwarder_node_id: String,
    pub controller_node_id: String,
    pub controller_bind: SocketAddr,
    pub controller_peer: SocketAddr,
    pub listener_bind: SocketAddr,
    pub listener_target: SocketAddr,
}

/// Controller observation for the complete selected source route. Integer
/// units keep compiled desired state deterministic across platforms.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourcePathObservation {
    pub source: String,
    pub observed_at_unix_ms: Option<u64>,
    pub best_direct_rtt_us: u64,
    pub rtt_us: u64,
    pub jitter_us: u64,
    pub loss_ppm: u64,
    pub queue_delay_us: u64,
}

impl CarrierLink {
    fn topology_key(&self) -> (&str, &str, ParentRole) {
        (&self.parent_node_id, &self.child_node_id, self.role)
    }
}

/// One lane of the standby contributor: its own sender socket and the socket
/// on which an origin child takes it, the child's free secondary lane. The
/// fields mean what `CarrierLink`'s do; the parent is the standby and there
/// is no parent role, because the standby is not a topology parent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandbyCarrierLink {
    pub child_node_id: String,
    pub lane: RelaySymbolLane,
    pub sender_bind: SocketAddr,
    pub sender_peer: SocketAddr,
    pub receiver_bind: SocketAddr,
    pub receiver_target: SocketAddr,
}

/// The standby contributor: a replica of the origin that always publishes
/// the same parts as the origin, on lanes of its own to the origin's two
/// children. Each child merges the two by first arrival; nothing elects,
/// promotes or demotes a contributor. It is not a node of the topology.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OriginStandby {
    pub node_id: String,
    pub failure_domain: FailureDomain,
    pub carrier_links: Vec<StandbyCarrierLink>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RelayProgram {
    pub purpose: DeploymentPurpose,
    pub carrier: CarrierProfile,
    pub subscription_id: u64,
    pub media_deadline_ms: u64,
    /// Send exact AEP1 datagrams to the warm ingress as one fixed redundant
    /// origin publication relationship. Disabled by default; relays own fanout.
    #[serde(default)]
    pub audio_epoch_redundant_ingress: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_path_observation: Option<SourcePathObservation>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub secondary_path_observation: Option<SourcePathObservation>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub failover_policy: Option<FailoverPolicy>,
    #[serde(default)]
    pub failover_control_links: Vec<FailoverControlLink>,
    /// Legacy: contributor -> backbone relays -> distributors. Merged
    /// (low-latency item 4): the distributors of the contributor's region
    /// are the first forwarders, fed by the contributor directly, and every
    /// other forwarder has two of them, or two forwarders of the level
    /// above, as parents (see `TopologyPolicy::merged_forwarders`).
    #[serde(default, skip_serializing_if = "ForwardingShape::is_legacy")]
    pub forwarding: ForwardingShape,
    /// Nodes whose two parents share a failure domain by design, as the
    /// children of the merged topology's two first forwarders do. Each is a
    /// production-readiness gap of the compiled plan; production refuses
    /// any.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub shared_domain_parents: Vec<String>,
    /// Playback edges per forwarder of a merged topology (default 16).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_edges_per_forwarder: Option<usize>,
    /// The standby contributor (resilient mesh): a second `av-contrib` with
    /// the origin's generation, subscription, deadline and path
    /// observations, one lane to each origin child. The first hop is then
    /// two contributors by two ingresses and nothing else.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origin_standby: Option<OriginStandby>,
    pub topology: RelayTopology,
    pub carrier_links: Vec<CarrierLink>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ForwardingShape {
    #[default]
    Legacy,
    Merged,
}

impl ForwardingShape {
    #[must_use]
    pub fn is_legacy(&self) -> bool {
        *self == Self::Legacy
    }
}

/// The production-readiness gap of a node whose two parents share a
/// failure domain by declaration.
fn shared_domain_parent_gap(node_id: &str) -> String {
    format!("shared_parent_failure_domain:{node_id}")
}

/// The production-readiness gap of a standby contributor that is not
/// independent of the origin by the stage's own diversity rule: one failure
/// takes both contributors.
fn origin_standby_domain_gap(node_id: &str) -> String {
    format!("origin_standby_shares_failure_domain:{node_id}")
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledParent {
    pub parent_node_id: String,
    pub bind: SocketAddr,
    pub peer: SocketAddr,
    pub lane: RelaySymbolLane,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledForward {
    pub child_node_id: String,
    pub bind: SocketAddr,
    pub target: SocketAddr,
    pub lane: RelaySymbolLane,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledFailoverListener {
    pub bind: SocketAddr,
    pub peer: SocketAddr,
    pub forward_target: SocketAddr,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledFailoverController {
    pub bind: SocketAddr,
    pub target: SocketAddr,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContribRelayService {
    pub node_id: String,
    pub audio_epoch_ingress_target: SocketAddr,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub audio_epoch_redundant_ingress_target: Option<SocketAddr>,
    pub primary: CompiledForward,
    pub warm_secondary: CompiledForward,
    pub topology_generation: u64,
    pub subscription_id: u64,
    pub media_deadline_ms: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_path_observation: Option<SourcePathObservation>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub secondary_path_observation: Option<SourcePathObservation>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MeshRelayService {
    pub node_id: String,
    pub primary_parent: CompiledParent,
    pub secondary_parent: Option<CompiledParent>,
    /// The standby contributor on an origin child's secondary lane: a
    /// second, always admitted source of the origin's symbols, never with a
    /// secondary parent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub standby_origin_parent: Option<CompiledParent>,
    pub forwards: Vec<CompiledForward>,
    pub failover_listeners: Vec<CompiledFailoverListener>,
    pub failover_controller: Option<CompiledFailoverController>,
    pub failover_policy: Option<FailoverPolicy>,
    pub max_downstream_children: usize,
    pub topology_generation: u64,
    pub subscription_id: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "service", rename_all = "snake_case")]
pub enum CompiledService {
    AvContrib(ContribRelayService),
    AvMesh(MeshRelayService),
}

impl CompiledService {
    #[must_use]
    pub fn node_id(&self) -> &str {
        match self {
            Self::AvContrib(service) => &service.node_id,
            Self::AvMesh(service) => &service.node_id,
        }
    }

    /// Exact relay-related CLI state consumed by the current native services.
    #[must_use]
    pub fn relay_arguments(&self) -> Vec<String> {
        match self {
            Self::AvContrib(service) => service.relay_arguments(),
            Self::AvMesh(service) => service.relay_arguments(),
        }
    }
}

impl ContribRelayService {
    fn relay_arguments(&self) -> Vec<String> {
        let mut args = vec![
            "--audio-epoch-ingress-target".to_owned(),
            self.audio_epoch_ingress_target.to_string(),
            "--relay-local-id".to_owned(),
            self.node_id.clone(),
            "--relay-primary-bind".to_owned(),
            self.primary.bind.to_string(),
            "--relay-primary-target".to_owned(),
            self.primary.target.to_string(),
            "--relay-primary-id".to_owned(),
            self.primary.child_node_id.clone(),
            "--relay-secondary-bind".to_owned(),
            self.warm_secondary.bind.to_string(),
            "--relay-secondary-target".to_owned(),
            self.warm_secondary.target.to_string(),
            "--relay-secondary-id".to_owned(),
            self.warm_secondary.child_node_id.clone(),
            "--relay-secondary-seed-source".to_owned(),
            "--relay-exclusive".to_owned(),
            "--relay-topology-generation".to_owned(),
            self.topology_generation.to_string(),
            "--relay-subscription-id".to_owned(),
            self.subscription_id.to_string(),
            "--relay-deadline-ms".to_owned(),
            self.media_deadline_ms.to_string(),
        ];
        if let Some(target) = self.audio_epoch_redundant_ingress_target {
            args.extend([
                "--audio-epoch-redundant-ingress-target".to_owned(),
                target.to_string(),
            ]);
        }
        if let Some(observation) = &self.source_path_observation {
            append_path_observation_arguments(&mut args, "--relay-path", observation);
        }
        if let Some(observation) = &self.secondary_path_observation {
            append_path_observation_arguments(&mut args, "--relay-secondary-path", observation);
        }
        args
    }
}

fn append_path_observation_arguments(
    args: &mut Vec<String>,
    prefix: &str,
    observation: &SourcePathObservation,
) {
    args.extend([
        format!("{prefix}-loss-fraction"),
        format!("{:.6}", observation.loss_ppm as f64 / 1_000_000.0),
        format!("{prefix}-best-direct-rtt-ms"),
        format!("{:.3}", observation.best_direct_rtt_us as f64 / 1_000.0),
        format!("{prefix}-rtt-ms"),
        format!("{:.3}", observation.rtt_us as f64 / 1_000.0),
        format!("{prefix}-jitter-ms"),
        format!("{:.3}", observation.jitter_us as f64 / 1_000.0),
        format!("{prefix}-queue-delay-ms"),
        format!("{:.3}", observation.queue_delay_us as f64 / 1_000.0),
    ]);
    if let Some(observed_at_unix_ms) = observation.observed_at_unix_ms {
        args.extend([
            format!("{prefix}-observed-at-unix-ms"),
            observed_at_unix_ms.to_string(),
        ]);
    }
}

impl MeshRelayService {
    fn relay_arguments(&self) -> Vec<String> {
        let mut args = vec![
            "--relay-controlled-local".to_owned(),
            "--relay-primary-bind".to_owned(),
            self.primary_parent.bind.to_string(),
            "--relay-primary-peer".to_owned(),
            self.primary_parent.peer.to_string(),
            "--relay-primary-id".to_owned(),
            self.primary_parent.parent_node_id.clone(),
        ];
        if self.primary_parent.lane == RelaySymbolLane::SourceAndRepair {
            args.push("--relay-primary-promoted".to_owned());
        }
        if let Some(secondary) = &self.secondary_parent {
            args.extend([
                "--relay-secondary-bind".to_owned(),
                secondary.bind.to_string(),
                "--relay-secondary-peer".to_owned(),
                secondary.peer.to_string(),
                "--relay-secondary-id".to_owned(),
                secondary.parent_node_id.clone(),
            ]);
            if secondary.lane == RelaySymbolLane::SourceAndRepair
                || self.failover_controller.is_some()
            {
                args.push("--relay-secondary-promoted".to_owned());
            }
        }
        if let Some(standby) = &self.standby_origin_parent {
            // Promoted: it sends everything the origin sends, from the
            // first datagram. No failover controller runs for it.
            args.extend([
                "--relay-secondary-bind".to_owned(),
                standby.bind.to_string(),
                "--relay-secondary-peer".to_owned(),
                standby.peer.to_string(),
                "--relay-secondary-id".to_owned(),
                standby.parent_node_id.clone(),
                "--relay-secondary-promoted".to_owned(),
            ]);
        }
        args.extend([
            "--relay-topology-generation".to_owned(),
            self.topology_generation.to_string(),
            "--relay-subscription-id".to_owned(),
            self.subscription_id.to_string(),
            "--relay-max-downstream-children".to_owned(),
            self.max_downstream_children.to_string(),
        ]);
        for forward in &self.forwards {
            args.extend([
                "--relay-forward".to_owned(),
                format!(
                    "{}={},{}",
                    forward.bind,
                    forward.target,
                    forward.lane.forward_argument()
                ),
            ]);
        }
        for listener in &self.failover_listeners {
            args.extend([
                "--relay-failover-listener".to_owned(),
                format!(
                    "{}={},{}",
                    listener.bind, listener.peer, listener.forward_target
                ),
            ]);
        }
        if let Some(controller) = &self.failover_controller {
            args.extend([
                "--relay-failover-controller".to_owned(),
                format!("{}={}", controller.bind, controller.target),
            ]);
        }
        if let Some(policy) = &self.failover_policy {
            args.extend([
                "--relay-primary-silence-ms".to_owned(),
                policy.primary_silence_ms.to_string(),
                "--relay-primary-recovery-ms".to_owned(),
                policy.primary_recovery_ms.to_string(),
                "--relay-secondary-warm-ms".to_owned(),
                policy.secondary_warm_ms.to_string(),
                "--relay-failover-heartbeat-ms".to_owned(),
                policy.heartbeat_ms.to_string(),
                "--relay-failover-lease-ms".to_owned(),
                policy.lease_ms.to_string(),
            ]);
        }
        args
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompiledServicePlan {
    pub purpose: DeploymentPurpose,
    pub carrier: CarrierProfile,
    pub topology_generation: u64,
    pub subscription_id: u64,
    /// These are explicit qualification limitations, not silent policy
    /// relaxations. An empty set is required for a production plan.
    pub production_readiness_gaps: Vec<String>,
    pub services: Vec<CompiledService>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServicePlanError {
    Topology(Vec<PolicyViolation>),
    InvalidProgram(Vec<String>),
}

impl fmt::Display for ServicePlanError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Topology(violations) => write!(
                formatter,
                "relay topology failed validation: {}",
                violations
                    .iter()
                    .map(|violation| format!("{}: {}", violation.code, violation.detail))
                    .collect::<Vec<_>>()
                    .join("; ")
            ),
            Self::InvalidProgram(violations) => write!(
                formatter,
                "relay service program failed validation: {}",
                violations.join("; ")
            ),
        }
    }
}

impl std::error::Error for ServicePlanError {}

impl RelayProgram {
    /// Every lane of the standby contributor, with the standby.
    fn standby_links(&self) -> impl Iterator<Item = (&OriginStandby, &StandbyCarrierLink)> {
        self.origin_standby.iter().flat_map(|standby| {
            standby
                .carrier_links
                .iter()
                .map(move |link| (standby, link))
        })
    }

    pub fn compile(&self) -> Result<CompiledServicePlan, ServicePlanError> {
        let policy = TopologyPolicy {
            diversity: self.purpose.diversity(),
            shared_domain_parents: self.shared_domain_parents.clone(),
            merged_forwarders: self.forwarding == ForwardingShape::Merged,
            max_edges_per_forwarder: self
                .max_edges_per_forwarder
                .unwrap_or(DEFAULT_MAX_EDGES_PER_FORWARDER),
        };
        self.topology
            .validate_with_policy(&policy)
            .map_err(ServicePlanError::Topology)?;
        if let Some(standby) = &self.origin_standby {
            self.topology
                .validate_origin_replicas(&[OriginReplica {
                    node_id: &standby.node_id,
                    failure_domain: &standby.failure_domain,
                    child_node_ids: standby
                        .carrier_links
                        .iter()
                        .map(|link| link.child_node_id.as_str())
                        .collect(),
                }])
                .map_err(ServicePlanError::Topology)?;
        }

        let mut violations = Vec::new();
        if self.purpose == DeploymentPurpose::Production && !self.shared_domain_parents.is_empty() {
            violations.push(
                "production refuses declared parent failure-domain gaps (shared_domain_parents)"
                    .to_owned(),
            );
        }
        if self.max_edges_per_forwarder == Some(0) {
            violations.push("max_edges_per_forwarder must be positive".to_owned());
        }
        if self.subscription_id == 0 {
            violations.push("subscription_id must be positive".to_owned());
        }
        if self.media_deadline_ms == 0 {
            violations.push("media_deadline_ms must be positive".to_owned());
        }
        for (field, observation) in [
            ("source_path_observation", &self.source_path_observation),
            (
                "secondary_path_observation",
                &self.secondary_path_observation,
            ),
        ] {
            if let Some(observation) = observation {
                validate_path_observation(field, observation, &mut violations);
            } else if self.purpose == DeploymentPurpose::Production {
                violations.push(format!("production requires {field}"));
            }
        }
        if let Some(policy) = &self.failover_policy {
            if policy.primary_silence_ms == 0
                || policy.primary_recovery_ms < policy.primary_silence_ms
                || policy.secondary_warm_ms < policy.primary_silence_ms
            {
                violations.push(
                    "failover policy requires positive silence and recovery/warm windows at least as large as silence"
                        .to_owned(),
                );
            }
            if policy.heartbeat_ms == 0
                || policy.lease_ms < policy.heartbeat_ms.saturating_mul(3)
                || policy.lease_ms > 60_000
            {
                violations.push(
                    "failover policy lease must be at most 60000ms and at least three heartbeat intervals"
                        .to_owned(),
                );
            }
        } else if !self.failover_control_links.is_empty() {
            violations.push("failover control links require failover_policy".to_owned());
        }
        match (self.purpose, self.carrier) {
            (
                DeploymentPurpose::Production,
                CarrierProfile::ControlledPrivateUdp | CarrierProfile::ControlledPublicUdp,
            ) => violations
                .push("production public relay links require the QUIC Datagram carrier".to_owned()),
            (_, CarrierProfile::QuicDatagram) => violations.push(
                "QUIC Datagram argument emission awaits the service carrier backend".to_owned(),
            ),
            _ => {}
        }

        let nodes = self
            .topology
            .nodes
            .iter()
            .map(|node| (node.node_id.as_str(), node))
            .collect::<HashMap<_, _>>();
        let topology_links = self
            .topology
            .parent_links
            .iter()
            .map(|link| {
                (
                    (
                        link.parent_node_id.as_str(),
                        link.child_node_id.as_str(),
                        link.role,
                    ),
                    link,
                )
            })
            .collect::<HashMap<_, _>>();
        let mut carrier_keys = HashSet::with_capacity(self.carrier_links.len());
        let mut incoming_by_child: HashMap<&str, Vec<&CarrierLink>> =
            HashMap::with_capacity(self.topology.nodes.len());
        let mut outgoing_by_parent: HashMap<&str, Vec<&CarrierLink>> =
            HashMap::with_capacity(self.topology.nodes.len());
        let mut sender_binds = HashSet::with_capacity(self.carrier_links.len());
        let mut receiver_binds = HashSet::with_capacity(self.carrier_links.len());
        for link in &self.carrier_links {
            let key = link.topology_key();
            if !carrier_keys.insert(key) {
                violations.push(format!(
                    "carrier link {} -> {} {:?} is duplicated",
                    link.parent_node_id, link.child_node_id, link.role
                ));
            }
            incoming_by_child
                .entry(link.child_node_id.as_str())
                .or_default()
                .push(link);
            outgoing_by_parent
                .entry(link.parent_node_id.as_str())
                .or_default()
                .push(link);
            if !topology_links.contains_key(&key) {
                violations.push(format!(
                    "carrier link {} -> {} {:?} has no topology relationship",
                    link.parent_node_id, link.child_node_id, link.role
                ));
            }
            if link.sender_bind.port() == 0
                || link.sender_peer.port() == 0
                || link.receiver_bind.port() == 0
                || link.receiver_target.port() == 0
            {
                violations.push(format!(
                    "carrier link {} -> {} requires non-zero ports",
                    link.parent_node_id, link.child_node_id
                ));
            }
            if nodes
                .get(link.parent_node_id.as_str())
                .is_some_and(|node| node.role == NodeRole::Origin)
                && link.sender_bind.ip().is_unspecified()
            {
                violations.push(format!(
                    "origin carrier link {} -> {} requires an explicit sender IP",
                    link.parent_node_id, link.child_node_id
                ));
            }
            if !sender_binds.insert((link.parent_node_id.as_str(), link.sender_bind)) {
                violations.push(format!(
                    "node {} reuses RelaySession sender bind {}",
                    link.parent_node_id, link.sender_bind
                ));
            }
            if !receiver_binds.insert((link.child_node_id.as_str(), link.receiver_bind)) {
                violations.push(format!(
                    "node {} reuses RelaySession receiver bind {}",
                    link.child_node_id, link.receiver_bind
                ));
            }
            match (link.role, link.lane) {
                (ParentRole::Primary, RelaySymbolLane::Source)
                | (ParentRole::Primary, RelaySymbolLane::SourceAndRepair)
                | (ParentRole::Secondary, RelaySymbolLane::Repair) => {}
                _ => violations.push(format!(
                    "carrier link {} -> {} assigns {:?} lane to {:?} parent role",
                    link.parent_node_id, link.child_node_id, link.lane, link.role
                )),
            }
        }
        for topology_link in &self.topology.parent_links {
            let key = (
                topology_link.parent_node_id.as_str(),
                topology_link.child_node_id.as_str(),
                topology_link.role,
            );
            if !carrier_keys.contains(&key) {
                violations.push(format!(
                    "topology relationship {} -> {} {:?} has no carrier link",
                    topology_link.parent_node_id, topology_link.child_node_id, topology_link.role
                ));
            }
        }
        // The standby's sockets, after every topology carrier's, so that a
        // child's receiver bind already in use is the standby's violation.
        for (standby, link) in self.standby_links() {
            if link.sender_bind.port() == 0
                || link.sender_peer.port() == 0
                || link.receiver_bind.port() == 0
                || link.receiver_target.port() == 0
            {
                violations.push(format!(
                    "origin standby link {} -> {} requires non-zero ports",
                    standby.node_id, link.child_node_id
                ));
            }
            if link.sender_bind.ip().is_unspecified() || link.sender_peer.ip().is_unspecified() {
                violations.push(format!(
                    "origin standby link {} -> {} requires an explicit sender IP",
                    standby.node_id, link.child_node_id
                ));
            }
            if !sender_binds.insert((standby.node_id.as_str(), link.sender_bind)) {
                violations.push(format!(
                    "node {} reuses RelaySession sender bind {}",
                    standby.node_id, link.sender_bind
                ));
            }
            if !receiver_binds.insert((link.child_node_id.as_str(), link.receiver_bind)) {
                violations.push(format!(
                    "node {} reuses RelaySession receiver bind {} for origin standby {}",
                    link.child_node_id, link.receiver_bind, standby.node_id
                ));
            }
        }

        let secondary_carriers = self
            .carrier_links
            .iter()
            .filter(|link| link.role == ParentRole::Secondary)
            .collect::<Vec<_>>();
        let secondary_carrier_by_nodes = secondary_carriers
            .iter()
            .map(|carrier| {
                (
                    (
                        carrier.parent_node_id.as_str(),
                        carrier.child_node_id.as_str(),
                    ),
                    *carrier,
                )
            })
            .collect::<HashMap<_, _>>();
        let mut control_keys = HashSet::with_capacity(self.failover_control_links.len());
        let mut controller_nodes = HashSet::with_capacity(self.failover_control_links.len());
        let mut controls_by_forwarder: HashMap<&str, Vec<&FailoverControlLink>> =
            HashMap::with_capacity(self.failover_control_links.len());
        let mut control_by_controller = HashMap::with_capacity(self.failover_control_links.len());
        let mut control_binds = HashSet::with_capacity(self.failover_control_links.len() * 2);
        for control in &self.failover_control_links {
            let key = (
                control.forwarder_node_id.as_str(),
                control.controller_node_id.as_str(),
            );
            if !control_keys.insert(key) {
                violations.push(format!(
                    "failover control {} -> {} is duplicated",
                    control.forwarder_node_id, control.controller_node_id
                ));
            }
            controls_by_forwarder
                .entry(control.forwarder_node_id.as_str())
                .or_default()
                .push(control);
            if !controller_nodes.insert(control.controller_node_id.as_str()) {
                violations.push(format!(
                    "node {} has more than one failover controller relationship",
                    control.controller_node_id
                ));
            }
            control_by_controller.insert(control.controller_node_id.as_str(), control);
            let carrier = secondary_carrier_by_nodes.get(&key);
            if carrier.is_none() {
                violations.push(format!(
                    "failover control {} -> {} has no secondary carrier relationship",
                    control.forwarder_node_id, control.controller_node_id
                ));
            }
            if [
                control.controller_bind,
                control.controller_peer,
                control.listener_bind,
                control.listener_target,
            ]
            .iter()
            .any(|address| address.port() == 0)
            {
                violations.push(format!(
                    "failover control {} -> {} requires non-zero ports",
                    control.forwarder_node_id, control.controller_node_id
                ));
            }
            if control.controller_bind.ip().is_unspecified()
                || control.listener_bind.ip().is_unspecified()
            {
                violations.push(format!(
                    "failover control {} -> {} requires explicit controller and listener bind IPs",
                    control.forwarder_node_id, control.controller_node_id
                ));
            }
            for (node_id, address) in [
                (control.controller_node_id.as_str(), control.controller_bind),
                (control.forwarder_node_id.as_str(), control.listener_bind),
            ] {
                if !control_binds.insert((node_id, address))
                    || sender_binds.contains(&(node_id, address))
                    || receiver_binds.contains(&(node_id, address))
                {
                    violations.push(format!(
                        "node {node_id} reuses failover control bind {address}"
                    ));
                }
            }
        }
        for carrier in &secondary_carriers {
            let has_control = control_keys.contains(&(
                carrier.parent_node_id.as_str(),
                carrier.child_node_id.as_str(),
            ));
            if !has_control {
                violations.push(format!(
                    "secondary carrier {} -> {} requires an automatic failover control link",
                    carrier.parent_node_id, carrier.child_node_id
                ));
            }
        }
        if !secondary_carriers.is_empty() && self.failover_policy.is_none() {
            violations.push("secondary carriers require failover_policy".to_owned());
        }

        let origin = self
            .topology
            .nodes
            .iter()
            .find(|node| node.role == NodeRole::Origin)
            .expect("topology validation requires one origin");
        let origin_links = outgoing_by_parent
            .get(origin.node_id.as_str())
            .map(Vec::as_slice)
            .unwrap_or_default();
        let primary_origin_links = origin_links
            .iter()
            .filter(|link| link.lane == RelaySymbolLane::Source)
            .collect::<Vec<_>>();
        let warm_origin_links = origin_links
            .iter()
            .filter(|link| link.lane == RelaySymbolLane::SourceAndRepair)
            .collect::<Vec<_>>();
        if primary_origin_links.len() != 1 || warm_origin_links.len() != 1 {
            violations.push(format!(
                "origin {} requires one source path and one source-and-repair warm path",
                origin.node_id
            ));
        }
        // A standby that one failure takes together with the origin is a
        // gap of the plan, by the same diversity rule as a node's parents.
        let standby_domain_gap = self.origin_standby.as_ref().and_then(|standby| {
            (!standby
                .failure_domain
                .is_independent_from(&origin.failure_domain, self.purpose.diversity()))
            .then(|| origin_standby_domain_gap(&standby.node_id))
        });
        if self.purpose == DeploymentPurpose::Production && standby_domain_gap.is_some() {
            violations.push(
                "production refuses an origin standby that shares the origin's failure domain"
                    .to_owned(),
            );
        }
        for (standby, link) in self.standby_links() {
            // Each contributor keeps the origin's shape: sources to one
            // child, sources and repair to the other.
            let origin_lane = origin_links
                .iter()
                .find(|origin_link| origin_link.child_node_id == link.child_node_id)
                .map(|origin_link| origin_link.lane);
            if origin_lane.is_some_and(|lane| lane != link.lane) {
                violations.push(format!(
                    "origin standby link {} -> {} assigns {:?} lane; origin {} sends it {:?}",
                    standby.node_id,
                    link.child_node_id,
                    link.lane,
                    origin.node_id,
                    origin_lane.expect("origin lane")
                ));
            }
        }

        for node in self
            .topology
            .nodes
            .iter()
            .filter(|node| node.role != NodeRole::Origin)
        {
            let incoming = incoming_by_child
                .get(node.node_id.as_str())
                .map(Vec::as_slice)
                .unwrap_or_default();
            let primary = incoming
                .iter()
                .filter(|link| link.role == ParentRole::Primary)
                .count();
            let secondary = incoming
                .iter()
                .filter(|link| link.role == ParentRole::Secondary)
                .count();
            if primary != 1 || secondary > 1 {
                violations.push(format!(
                    "node {} carrier slots require one primary and at most one secondary",
                    node.node_id
                ));
            }
        }

        if !violations.is_empty() {
            return Err(ServicePlanError::InvalidProgram(violations));
        }

        let primary_origin = primary_origin_links[0];
        let warm_origin = warm_origin_links[0];
        let contrib = CompiledService::AvContrib(ContribRelayService {
            node_id: origin.node_id.clone(),
            audio_epoch_ingress_target: primary_origin.receiver_target,
            audio_epoch_redundant_ingress_target: self
                .audio_epoch_redundant_ingress
                .then_some(warm_origin.receiver_target),
            primary: compiled_forward(primary_origin),
            warm_secondary: compiled_forward(warm_origin),
            topology_generation: self.topology.generation,
            subscription_id: self.subscription_id,
            media_deadline_ms: self.media_deadline_ms,
            source_path_observation: self.source_path_observation.clone(),
            secondary_path_observation: self.secondary_path_observation.clone(),
        });

        let mut services = vec![contrib];
        let mut standby_parents = HashMap::new();
        if let Some(standby) = &self.origin_standby {
            let lane = |lane: RelaySymbolLane| {
                standby
                    .carrier_links
                    .iter()
                    .find(|link| link.lane == lane)
                    .expect("validated standby lanes")
            };
            let primary_standby = lane(RelaySymbolLane::Source);
            let warm_standby = lane(RelaySymbolLane::SourceAndRepair);
            services.push(CompiledService::AvContrib(ContribRelayService {
                node_id: standby.node_id.clone(),
                audio_epoch_ingress_target: primary_standby.receiver_target,
                audio_epoch_redundant_ingress_target: self
                    .audio_epoch_redundant_ingress
                    .then_some(warm_standby.receiver_target),
                primary: primary_standby.compiled_forward(),
                warm_secondary: warm_standby.compiled_forward(),
                topology_generation: self.topology.generation,
                subscription_id: self.subscription_id,
                media_deadline_ms: self.media_deadline_ms,
                source_path_observation: self.source_path_observation.clone(),
                secondary_path_observation: self.secondary_path_observation.clone(),
            }));
            for link in &standby.carrier_links {
                standby_parents.insert(
                    link.child_node_id.as_str(),
                    link.compiled_parent(&standby.node_id),
                );
            }
        }
        let contributors = services.len();
        debug_assert!(contributors <= MAX_ORIGINS_PER_STREAM);
        for node in self
            .topology
            .nodes
            .iter()
            .filter(|node| node.role != NodeRole::Origin)
        {
            let incoming = incoming_by_child
                .get(node.node_id.as_str())
                .expect("validated incoming carriers");
            let primary = incoming
                .iter()
                .copied()
                .find(|link| link.role == ParentRole::Primary)
                .expect("validated primary carrier");
            let secondary = incoming
                .iter()
                .copied()
                .find(|link| link.role == ParentRole::Secondary);
            let mut forwards = outgoing_by_parent
                .get(node.node_id.as_str())
                .into_iter()
                .flatten()
                .copied()
                .map(compiled_forward)
                .collect::<Vec<_>>();
            forwards.sort_by(|left, right| left.child_node_id.cmp(&right.child_node_id));
            let mut failover_listeners = controls_by_forwarder
                .get(node.node_id.as_str())
                .into_iter()
                .flatten()
                .copied()
                .map(|control| {
                    let carrier = secondary_carrier_by_nodes
                        .get(&(
                            control.forwarder_node_id.as_str(),
                            control.controller_node_id.as_str(),
                        ))
                        .copied()
                        .expect("validated failover carrier");
                    CompiledFailoverListener {
                        bind: control.listener_bind,
                        peer: control.controller_peer,
                        forward_target: carrier.receiver_target,
                    }
                })
                .collect::<Vec<_>>();
            failover_listeners.sort_by_key(|listener| listener.forward_target);
            let failover_controller = control_by_controller
                .get(node.node_id.as_str())
                .copied()
                .map(|control| CompiledFailoverController {
                    bind: control.controller_bind,
                    target: control.listener_target,
                });
            let failover_policy = (!failover_listeners.is_empty() || failover_controller.is_some())
                .then(|| {
                    self.failover_policy
                        .clone()
                        .expect("validated failover policy")
                });
            services.push(CompiledService::AvMesh(MeshRelayService {
                node_id: node.node_id.clone(),
                primary_parent: compiled_parent(primary),
                secondary_parent: secondary.map(compiled_parent),
                standby_origin_parent: standby_parents.remove(node.node_id.as_str()),
                forwards,
                failover_listeners,
                failover_controller,
                failover_policy,
                max_downstream_children: self.topology.limits.max_downstream_children,
                topology_generation: self.topology.generation,
                subscription_id: self.subscription_id,
            }));
        }
        services.sort_by(|left, right| left.node_id().cmp(right.node_id()));

        // The validated topology catalog is authoritative. This assertion also
        // prevents future compilation paths from silently omitting a node. The
        // standby contributor is the one service that is not a topology node.
        debug_assert_eq!(services.len(), nodes.len() + contributors - 1);
        debug_assert!(standby_parents.is_empty());
        let mut production_readiness_gaps = self.purpose.readiness_gaps();
        let mut shared = self
            .shared_domain_parents
            .iter()
            .map(|node_id| shared_domain_parent_gap(node_id))
            .collect::<Vec<_>>();
        shared.sort();
        shared.dedup();
        production_readiness_gaps.extend(shared);
        production_readiness_gaps.extend(standby_domain_gap);
        Ok(CompiledServicePlan {
            purpose: self.purpose,
            carrier: self.carrier,
            topology_generation: self.topology.generation,
            subscription_id: self.subscription_id,
            production_readiness_gaps,
            services,
        })
    }
}

fn validate_path_observation(
    field: &str,
    observation: &SourcePathObservation,
    violations: &mut Vec<String>,
) {
    if observation.source.trim().is_empty() || observation.source.len() > 64 {
        violations.push(format!("{field}.source must contain 1 through 64 bytes"));
    }
    if observation.loss_ppm > 1_000_000 {
        violations.push(format!("{field}.loss_ppm exceeds 1000000"));
    }
    for (metric, value) in [
        ("best_direct_rtt_us", observation.best_direct_rtt_us),
        ("rtt_us", observation.rtt_us),
        ("jitter_us", observation.jitter_us),
        ("queue_delay_us", observation.queue_delay_us),
    ] {
        if value > 60_000_000 {
            violations.push(format!("{field}.{metric} exceeds 60000000"));
        }
    }
    if observation.rtt_us > 0 && observation.best_direct_rtt_us == 0 {
        violations.push(format!(
            "{field}.best_direct_rtt_us must be positive when rtt_us is observed"
        ));
    }
}

fn compiled_parent(link: &CarrierLink) -> CompiledParent {
    CompiledParent {
        parent_node_id: link.parent_node_id.clone(),
        bind: link.receiver_bind,
        peer: link.sender_peer,
        lane: link.lane,
    }
}

fn compiled_forward(link: &CarrierLink) -> CompiledForward {
    CompiledForward {
        child_node_id: link.child_node_id.clone(),
        bind: link.sender_bind,
        target: link.receiver_target,
        lane: link.lane,
    }
}

impl StandbyCarrierLink {
    fn compiled_parent(&self, standby_node_id: &str) -> CompiledParent {
        CompiledParent {
            parent_node_id: standby_node_id.to_owned(),
            bind: self.receiver_bind,
            peer: self.sender_peer,
            lane: self.lane,
        }
    }

    fn compiled_forward(&self) -> CompiledForward {
        CompiledForward {
            child_node_id: self.child_node_id.clone(),
            bind: self.sender_bind,
            target: self.receiver_target,
            lane: self.lane,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::relay_topology::{ParentLink, RelayNode, TopologyLimits};

    fn address(port: u16) -> SocketAddr {
        SocketAddr::from(([127, 0, 0, 1], port))
    }

    fn link(
        parent: &str,
        child: &str,
        role: ParentRole,
        lane: RelaySymbolLane,
        sender_port: u16,
        receiver_port: u16,
    ) -> CarrierLink {
        CarrierLink {
            parent_node_id: parent.to_owned(),
            child_node_id: child.to_owned(),
            role,
            lane,
            sender_bind: address(sender_port),
            sender_peer: address(sender_port),
            receiver_bind: address(receiver_port),
            receiver_target: address(receiver_port),
        }
    }

    fn node(node_id: &str, level: u16, role: NodeRole, region: &str, zone: &str) -> RelayNode {
        RelayNode {
            node_id: node_id.to_owned(),
            level,
            role,
            failure_domain: FailureDomain {
                provider: "qualification-cloud".to_owned(),
                region: region.to_owned(),
                asn: 64_500,
                zone: zone.to_owned(),
            },
        }
    }

    fn qualification_program() -> RelayProgram {
        let parent_links = vec![
            ParentLink {
                parent_node_id: "contrib".to_owned(),
                child_node_id: "relay-a".to_owned(),
                role: ParentRole::Primary,
            },
            ParentLink {
                parent_node_id: "contrib".to_owned(),
                child_node_id: "relay-b".to_owned(),
                role: ParentRole::Primary,
            },
            ParentLink {
                parent_node_id: "relay-a".to_owned(),
                child_node_id: "edge".to_owned(),
                role: ParentRole::Primary,
            },
            ParentLink {
                parent_node_id: "relay-b".to_owned(),
                child_node_id: "edge".to_owned(),
                role: ParentRole::Secondary,
            },
        ];
        RelayProgram {
            purpose: DeploymentPurpose::SingleProviderQualification,
            carrier: CarrierProfile::ControlledPrivateUdp,
            subscription_id: 9,
            media_deadline_ms: 1_000,
            audio_epoch_redundant_ingress: false,
            source_path_observation: Some(SourcePathObservation {
                source: "qualification-probe".to_owned(),
                observed_at_unix_ms: Some(1_784_102_400_000),
                best_direct_rtt_us: 246_727,
                rtt_us: 253_429,
                jitter_us: 510,
                loss_ppm: 10_000,
                queue_delay_us: 2_000,
            }),
            secondary_path_observation: Some(SourcePathObservation {
                source: "qualification-probe".to_owned(),
                observed_at_unix_ms: Some(1_784_102_400_000),
                best_direct_rtt_us: 246_727,
                rtt_us: 261_500,
                jitter_us: 620,
                loss_ppm: 2_000,
                queue_delay_us: 1_000,
            }),
            failover_policy: Some(FailoverPolicy {
                primary_silence_ms: 250,
                primary_recovery_ms: 2_000,
                secondary_warm_ms: 750,
                heartbeat_ms: 100,
                lease_ms: 1_000,
            }),
            failover_control_links: vec![FailoverControlLink {
                forwarder_node_id: "relay-b".to_owned(),
                controller_node_id: "edge".to_owned(),
                controller_bind: address(22_501),
                controller_peer: address(22_501),
                listener_bind: address(22_502),
                listener_target: address(22_502),
            }],
            forwarding: ForwardingShape::Legacy,
            shared_domain_parents: Vec::new(),
            max_edges_per_forwarder: None,
            origin_standby: None,
            topology: RelayTopology {
                generation: 7,
                nodes: vec![
                    node("contrib", 0, NodeRole::Origin, "europe-west2", "a"),
                    node("relay-a", 1, NodeRole::Backbone, "europe-west1", "b"),
                    node("relay-b", 1, NodeRole::Backbone, "us-east1", "c"),
                    node("edge", 2, NodeRole::PlaybackEdge, "asia-east1", "d"),
                ],
                parent_links,
                limits: TopologyLimits {
                    max_origin_children: 2,
                    max_downstream_children: 4,
                },
            },
            carrier_links: vec![
                link(
                    "contrib",
                    "relay-a",
                    ParentRole::Primary,
                    RelaySymbolLane::Source,
                    22_301,
                    22_001,
                ),
                link(
                    "contrib",
                    "relay-b",
                    ParentRole::Primary,
                    RelaySymbolLane::SourceAndRepair,
                    22_302,
                    22_002,
                ),
                link(
                    "relay-a",
                    "edge",
                    ParentRole::Primary,
                    RelaySymbolLane::Source,
                    22_303,
                    22_003,
                ),
                link(
                    "relay-b",
                    "edge",
                    ParentRole::Secondary,
                    RelaySymbolLane::Repair,
                    22_304,
                    22_004,
                ),
            ],
        }
    }

    #[test]
    fn compiles_four_host_source_seeded_dual_parent_routing() {
        let plan = qualification_program().compile().expect("compile plan");
        assert_eq!(plan.services.len(), 4);
        assert_eq!(
            plan.production_readiness_gaps,
            vec![
                "provider_asn_diversity_pending",
                "authenticated_public_carrier_pending"
            ]
        );

        let contrib = plan
            .services
            .iter()
            .find(|service| service.node_id() == "contrib")
            .expect("contrib");
        let contrib_args = contrib.relay_arguments();
        let CompiledService::AvContrib(contrib_service) = contrib else {
            panic!("origin compiled as mesh service");
        };
        assert_eq!(
            contrib_service.audio_epoch_ingress_target,
            contrib_service.primary.target
        );
        assert_eq!(contrib_service.audio_epoch_redundant_ingress_target, None);
        assert!(contrib_args.windows(2).any(|pair| {
            pair == [
                "--audio-epoch-ingress-target".to_owned(),
                contrib_service.primary.target.to_string(),
            ]
        }));
        assert!(!contrib_args.contains(&"--audio-epoch-redundant-ingress-target".to_owned()));
        assert!(contrib_args.contains(&"--relay-secondary-seed-source".to_owned()));
        assert!(contrib_args.contains(&"relay-a".to_owned()));
        assert!(contrib_args.contains(&"relay-b".to_owned()));
        assert!(contrib_args.windows(2).any(|pair| {
            pair == [
                "--relay-path-loss-fraction".to_owned(),
                "0.010000".to_owned(),
            ]
        }));
        assert!(contrib_args.windows(2).any(|pair| {
            pair == [
                "--relay-secondary-path-rtt-ms".to_owned(),
                "261.500".to_owned(),
            ]
        }));
        assert!(contrib_args
            .windows(2)
            .any(|pair| { pair == ["--relay-path-rtt-ms".to_owned(), "253.429".to_owned()] }));
        assert!(contrib_args.windows(2).any(|pair| {
            pair == [
                "--relay-path-best-direct-rtt-ms".to_owned(),
                "246.727".to_owned(),
            ]
        }));

        let relay_b = plan
            .services
            .iter()
            .find(|service| service.node_id() == "relay-b")
            .expect("relay b");
        let relay_b_args = relay_b.relay_arguments();
        assert!(relay_b_args.contains(&"--relay-primary-promoted".to_owned()));
        assert!(relay_b_args.contains(&"127.0.0.1:22304=127.0.0.1:22004,repair".to_owned()));
        assert!(relay_b_args.contains(&"--relay-failover-listener".to_owned()));
        assert!(
            relay_b_args.contains(&"127.0.0.1:22502=127.0.0.1:22501,127.0.0.1:22004".to_owned())
        );

        let edge = plan
            .services
            .iter()
            .find(|service| service.node_id() == "edge")
            .expect("edge");
        let edge_args = edge.relay_arguments();
        assert!(edge_args.contains(&"--relay-primary-peer".to_owned()));
        assert!(edge_args.contains(&"--relay-secondary-peer".to_owned()));
        assert!(edge_args.contains(&"--relay-secondary-promoted".to_owned()));
        assert!(edge_args.contains(&"--relay-failover-controller".to_owned()));
        assert!(edge_args.contains(&"127.0.0.1:22501=127.0.0.1:22502".to_owned()));
        assert!(edge_args
            .windows(2)
            .any(|pair| { pair == ["--relay-primary-recovery-ms".to_owned(), "2000".to_owned()] }));
    }

    #[test]
    fn local_qualification_accepts_distinct_nodes_in_one_failure_domain() {
        let mut program = qualification_program();
        let shared_domain = program
            .topology
            .nodes
            .iter()
            .find(|node| node.node_id == "relay-a")
            .expect("relay a")
            .failure_domain
            .clone();
        program
            .topology
            .nodes
            .iter_mut()
            .find(|node| node.node_id == "relay-b")
            .expect("relay b")
            .failure_domain = shared_domain;

        program.purpose = DeploymentPurpose::LocalQualification;
        let plan = program.compile().expect("same-domain local qualification");
        assert!(plan
            .production_readiness_gaps
            .contains(&"physical_host_diversity_pending".to_owned()));
    }

    #[test]
    fn redundant_audio_epoch_ingress_is_explicit_and_bounded_to_two_targets() {
        let mut program = qualification_program();
        program.audio_epoch_redundant_ingress = true;
        let plan = program.compile().expect("compile redundant ingress plan");
        let contrib = plan
            .services
            .iter()
            .find(|service| service.node_id() == "contrib")
            .expect("contrib");
        let CompiledService::AvContrib(service) = contrib else {
            panic!("origin compiled as mesh service");
        };
        assert_eq!(
            service.audio_epoch_redundant_ingress_target,
            Some(service.warm_secondary.target)
        );
        let args = service.relay_arguments();
        assert_eq!(
            args.iter()
                .filter(|argument| {
                    matches!(
                        argument.as_str(),
                        "--audio-epoch-ingress-target" | "--audio-epoch-redundant-ingress-target"
                    )
                })
                .count(),
            2
        );
    }

    #[test]
    fn compiles_two_origin_fanout_links_into_three_independent_edge_caches() {
        let mut program = qualification_program();
        for (
            edge,
            region,
            zone,
            primary_sender,
            primary_receiver,
            secondary_sender,
            secondary_receiver,
            controller,
            listener,
        ) in [
            (
                "edge-new-york",
                "us-east4",
                "e",
                22_305,
                22_005,
                22_306,
                22_006,
                22_503,
                22_504,
            ),
            (
                "edge-sydney",
                "australia-southeast1",
                "f",
                22_307,
                22_007,
                22_308,
                22_008,
                22_505,
                22_506,
            ),
        ] {
            program
                .topology
                .nodes
                .push(node(edge, 2, NodeRole::PlaybackEdge, region, zone));
            program.topology.parent_links.extend([
                ParentLink {
                    parent_node_id: "relay-a".to_owned(),
                    child_node_id: edge.to_owned(),
                    role: ParentRole::Primary,
                },
                ParentLink {
                    parent_node_id: "relay-b".to_owned(),
                    child_node_id: edge.to_owned(),
                    role: ParentRole::Secondary,
                },
            ]);
            program.carrier_links.extend([
                link(
                    "relay-a",
                    edge,
                    ParentRole::Primary,
                    RelaySymbolLane::Source,
                    primary_sender,
                    primary_receiver,
                ),
                link(
                    "relay-b",
                    edge,
                    ParentRole::Secondary,
                    RelaySymbolLane::Repair,
                    secondary_sender,
                    secondary_receiver,
                ),
            ]);
            program.failover_control_links.push(FailoverControlLink {
                forwarder_node_id: "relay-b".to_owned(),
                controller_node_id: edge.to_owned(),
                controller_bind: address(controller),
                controller_peer: address(controller),
                listener_bind: address(listener),
                listener_target: address(listener),
            });
        }

        let plan = program.compile().expect("compile multi-edge plan");
        assert_eq!(plan.services.len(), 6);

        let contrib = plan
            .services
            .iter()
            .find(|service| service.node_id() == "contrib")
            .expect("contrib");
        let CompiledService::AvContrib(contrib) = contrib else {
            panic!("contributor compiled as mesh service");
        };
        assert_eq!(contrib.primary.child_node_id, "relay-a");
        assert_eq!(contrib.warm_secondary.child_node_id, "relay-b");

        for relay in ["relay-a", "relay-b"] {
            let service = plan
                .services
                .iter()
                .find(|service| service.node_id() == relay)
                .expect("backbone relay");
            let CompiledService::AvMesh(service) = service else {
                panic!("backbone compiled as contributor service");
            };
            assert_eq!(service.forwards.len(), 3);
            if relay == "relay-b" {
                assert_eq!(service.failover_listeners.len(), 3);
            }
        }

        for edge in ["edge", "edge-new-york", "edge-sydney"] {
            let service = plan
                .services
                .iter()
                .find(|service| service.node_id() == edge)
                .expect("playback edge");
            let CompiledService::AvMesh(service) = service else {
                panic!("edge compiled as contributor service");
            };
            assert!(service.secondary_parent.is_some());
            assert!(service.failover_controller.is_some());
        }
    }

    #[test]
    fn production_keeps_provider_and_asn_diversity_as_a_hard_gate() {
        let mut program = qualification_program();
        program.purpose = DeploymentPurpose::Production;
        program.carrier = CarrierProfile::QuicDatagram;
        let error = program
            .compile()
            .expect_err("same-provider production plan");
        assert!(matches!(error, ServicePlanError::Topology(_)));
    }

    #[test]
    fn multi_provider_qualification_enforces_diversity_without_claiming_production() {
        let mut program = qualification_program();
        program.purpose = DeploymentPurpose::MultiProviderQualification;
        program.carrier = CarrierProfile::ControlledPublicUdp;
        let relay_b = program
            .topology
            .nodes
            .iter_mut()
            .find(|node| node.node_id == "relay-b")
            .expect("relay-b");
        relay_b.failure_domain.provider = "independent-cloud".to_owned();
        relay_b.failure_domain.asn = 64_501;

        let plan = program.compile().expect("multi-provider qualification");
        assert_eq!(
            plan.production_readiness_gaps,
            vec!["authenticated_public_carrier_pending"]
        );
    }

    #[test]
    fn controlled_public_udp_retains_the_public_carrier_readiness_gap() {
        let mut program = qualification_program();
        program.carrier = CarrierProfile::ControlledPublicUdp;
        let plan = program.compile().expect("single-provider public UDP lab");
        assert_eq!(plan.carrier, CarrierProfile::ControlledPublicUdp);
        assert!(plan
            .production_readiness_gaps
            .contains(&"authenticated_public_carrier_pending".to_owned()));
    }

    #[test]
    fn rejects_lane_role_mismatch_and_missing_topology_carriers() {
        let mut program = qualification_program();
        program.carrier_links[3].lane = RelaySymbolLane::Source;
        program.carrier_links.pop();
        let error = program.compile().expect_err("incomplete carriers");
        let ServicePlanError::InvalidProgram(violations) = error else {
            panic!("expected service program violations");
        };
        assert!(violations
            .iter()
            .any(|violation| violation.contains("has no carrier link")));
    }

    #[test]
    fn rejects_unspecified_origin_sender_identity() {
        let mut program = qualification_program();
        program.carrier_links[0].sender_bind = "0.0.0.0:22301".parse().expect("address");

        let error = program.compile().expect_err("unspecified origin sender");
        let ServicePlanError::InvalidProgram(violations) = error else {
            panic!("expected service program violations");
        };
        assert!(violations.iter().any(|violation| {
            violation
                .contains("origin carrier link contrib -> relay-a requires an explicit sender IP")
        }));
    }

    #[test]
    fn rejects_out_of_bounds_source_path_observation() {
        let mut program = qualification_program();
        program
            .source_path_observation
            .as_mut()
            .expect("path observation")
            .loss_ppm = 1_000_001;

        let error = program.compile().expect_err("invalid path observation");
        let ServicePlanError::InvalidProgram(violations) = error else {
            panic!("expected service program violations");
        };
        assert!(violations
            .iter()
            .any(|violation| violation.contains("loss_ppm exceeds 1000000")));
    }

    #[test]
    fn rejects_out_of_bounds_secondary_path_observation() {
        let mut program = qualification_program();
        program
            .secondary_path_observation
            .as_mut()
            .expect("secondary path observation")
            .loss_ppm = 1_000_001;

        let error = program.compile().expect_err("invalid path observation");
        let ServicePlanError::InvalidProgram(violations) = error else {
            panic!("expected service program violations");
        };
        assert!(violations
            .iter()
            .any(|violation| violation.contains("secondary_path_observation.loss_ppm")));
    }

    /// The merged topology (low-latency item 4) as the deploy compiles it:
    /// the contributor feeds the two London forwarders, which feed the two
    /// Sydney forwarders crossed, a source lane from one and a repair lane
    /// with failover control from the other.
    fn merged_program() -> RelayProgram {
        let mut program = qualification_program();
        program.forwarding = ForwardingShape::Merged;
        program.shared_domain_parents = vec!["f3".to_owned(), "f4".to_owned()];
        let parent = |parent: &str, child: &str, role| ParentLink {
            parent_node_id: parent.to_owned(),
            child_node_id: child.to_owned(),
            role,
        };
        program.topology = RelayTopology {
            generation: 1,
            nodes: vec![
                node("contrib", 0, NodeRole::Origin, "gb-lon", "gb-lon"),
                node("f1", 1, NodeRole::Backbone, "gb-lon", "gb-lon"),
                node("f2", 1, NodeRole::Backbone, "gb-lon", "gb-lon"),
                node(
                    "f3",
                    2,
                    NodeRole::RegionalRelay,
                    "ap-southeast",
                    "ap-southeast",
                ),
                node(
                    "f4",
                    2,
                    NodeRole::RegionalRelay,
                    "ap-southeast",
                    "ap-southeast",
                ),
            ],
            parent_links: vec![
                parent("contrib", "f1", ParentRole::Primary),
                parent("contrib", "f2", ParentRole::Primary),
                parent("f1", "f3", ParentRole::Primary),
                parent("f2", "f3", ParentRole::Secondary),
                parent("f2", "f4", ParentRole::Primary),
                parent("f1", "f4", ParentRole::Secondary),
            ],
            limits: TopologyLimits {
                max_origin_children: 2,
                max_downstream_children: 4,
            },
        };
        program.carrier_links = vec![
            link(
                "contrib",
                "f1",
                ParentRole::Primary,
                RelaySymbolLane::Source,
                22_301,
                22_001,
            ),
            link(
                "contrib",
                "f2",
                ParentRole::Primary,
                RelaySymbolLane::SourceAndRepair,
                22_302,
                22_002,
            ),
            link(
                "f1",
                "f3",
                ParentRole::Primary,
                RelaySymbolLane::Source,
                22_401,
                22_200,
            ),
            link(
                "f2",
                "f3",
                ParentRole::Secondary,
                RelaySymbolLane::Repair,
                22_402,
                22_201,
            ),
            link(
                "f2",
                "f4",
                ParentRole::Primary,
                RelaySymbolLane::Source,
                22_411,
                22_210,
            ),
            link(
                "f1",
                "f4",
                ParentRole::Secondary,
                RelaySymbolLane::Repair,
                22_412,
                22_211,
            ),
        ];
        let control = |forwarder: &str, controller: &str, port: u16| FailoverControlLink {
            forwarder_node_id: forwarder.to_owned(),
            controller_node_id: controller.to_owned(),
            controller_bind: address(port),
            controller_peer: address(port),
            listener_bind: address(port + 1),
            listener_target: address(port + 1),
        };
        program.failover_control_links =
            vec![control("f2", "f3", 22_501), control("f1", "f4", 22_511)];
        program
    }

    fn mesh_service<'a>(plan: &'a CompiledServicePlan, node_id: &str) -> &'a MeshRelayService {
        plan.services
            .iter()
            .find_map(|service| match service {
                CompiledService::AvMesh(mesh) if mesh.node_id == node_id => Some(mesh),
                _ => None,
            })
            .expect("mesh service")
    }

    #[test]
    fn compiles_the_merged_topology_with_its_gaps_recorded() {
        let plan = merged_program().compile().expect("compile merged plan");
        assert_eq!(plan.services.len(), 5);
        assert_eq!(
            plan.production_readiness_gaps,
            vec![
                "provider_asn_diversity_pending",
                "authenticated_public_carrier_pending",
                "shared_parent_failure_domain:f3",
                "shared_parent_failure_domain:f4",
            ]
        );
        let contrib = plan
            .services
            .iter()
            .find_map(|service| match service {
                CompiledService::AvContrib(contrib) => Some(contrib),
                CompiledService::AvMesh(_) => None,
            })
            .expect("contrib");
        // The contributor's two lanes end at the first forwarders.
        assert_eq!(contrib.primary.child_node_id, "f1");
        assert_eq!(contrib.warm_secondary.child_node_id, "f2");
        let f1 = mesh_service(&plan, "f1");
        assert_eq!(f1.primary_parent.parent_node_id, "contrib");
        assert!(f1.secondary_parent.is_none());
        assert_eq!(
            f1.forwards
                .iter()
                .map(|forward| (forward.child_node_id.as_str(), forward.lane))
                .collect::<Vec<_>>(),
            [
                ("f3", RelaySymbolLane::Source),
                ("f4", RelaySymbolLane::Repair)
            ]
        );
        assert_eq!(f1.failover_listeners.len(), 1);
        assert_eq!(f1.failover_listeners[0].forward_target, address(22_211));
        let f3 = mesh_service(&plan, "f3");
        assert_eq!(f3.primary_parent.parent_node_id, "f1");
        assert_eq!(f3.secondary_parent.as_ref().unwrap().parent_node_id, "f2");
        let args = f3.relay_arguments();
        assert!(args.contains(&"--relay-secondary-promoted".to_owned()));
        assert!(args.contains(&"127.0.0.1:22501=127.0.0.1:22502".to_owned()));
        let f4 = mesh_service(&plan, "f4");
        assert_eq!(f4.primary_parent.parent_node_id, "f2");
        assert_eq!(f4.secondary_parent.as_ref().unwrap().parent_node_id, "f1");
    }

    #[test]
    fn the_merged_topology_never_passes_its_shared_parent_domains_silently() {
        let mut program = merged_program();
        program.shared_domain_parents.clear();
        let Err(ServicePlanError::Topology(violations)) = program.compile() else {
            panic!("two London parents passed under RegionAndZone");
        };
        assert_eq!(violations.len(), 2, "{violations:?}");
        assert!(violations
            .iter()
            .all(|violation| violation.code == "parent_failure_domain_overlap"));

        let mut program = merged_program();
        program.purpose = DeploymentPurpose::MultiProviderQualification;
        program.carrier = CarrierProfile::ControlledPublicUdp;
        // Provider and ASN overlap too; gaps make distinct nodes enough.
        program
            .compile()
            .expect("declared gaps under multi-provider");
        program.purpose = DeploymentPurpose::Production;
        let Err(ServicePlanError::InvalidProgram(violations)) = program.compile() else {
            panic!("production accepted a declared gap");
        };
        assert!(violations
            .iter()
            .any(|violation| violation.contains("shared_domain_parents")));
    }

    #[test]
    fn the_merged_contributor_feeds_only_its_regions_first_forwarders() {
        let mut program = merged_program();
        // The contributor's warm lane to a Sydney forwarder instead.
        program.topology.parent_links[1].child_node_id = "f3".to_owned();
        program.carrier_links[1].child_node_id = "f3".to_owned();
        let Err(ServicePlanError::Topology(violations)) = program.compile() else {
            panic!("a direct lane to another region compiled");
        };
        assert!(violations
            .iter()
            .any(|violation| violation.code == "origin_child_not_region_first_forwarder"));
    }

    #[test]
    fn a_legacy_program_serialises_without_the_merged_fields() {
        let json = serde_json::to_value(qualification_program()).unwrap();
        for field in [
            "forwarding",
            "shared_domain_parents",
            "max_edges_per_forwarder",
            "origin_standby",
        ] {
            assert!(json.get(field).is_none(), "{field}");
        }
        let merged = serde_json::to_value(merged_program()).unwrap();
        assert_eq!(merged["forwarding"], "merged");
        assert!(merged.get("origin_standby").is_none());
        let back: RelayProgram = serde_json::from_value(merged).unwrap();
        assert_eq!(back, merged_program());
    }

    fn standby_address(port: u16) -> SocketAddr {
        SocketAddr::from(([127, 0, 0, 2], port))
    }

    fn standby_link(
        child: &str,
        lane: RelaySymbolLane,
        sender_port: u16,
        receiver_port: u16,
    ) -> StandbyCarrierLink {
        StandbyCarrierLink {
            child_node_id: child.to_owned(),
            lane,
            sender_bind: standby_address(sender_port),
            sender_peer: standby_address(sender_port),
            receiver_bind: address(receiver_port),
            receiver_target: address(receiver_port),
        }
    }

    /// The merged program with the standby contributor as the deploy
    /// compiles it (resilient mesh, workstream A 2.7): `contrib-b` on each
    /// first forwarder's free secondary lane, so f1 takes the origin on
    /// 22001 and the standby on 22002, and f2 the reverse. Each contributor
    /// sends sources to f1 and sources and repair to f2.
    fn two_contributor_program() -> RelayProgram {
        let mut program = merged_program();
        program.origin_standby = Some(OriginStandby {
            node_id: "contrib-b".to_owned(),
            failure_domain: node("contrib-b", 0, NodeRole::Origin, "gb-lon", "gb-lon")
                .failure_domain,
            carrier_links: vec![
                standby_link("f1", RelaySymbolLane::Source, 22_301, 22_002),
                standby_link("f2", RelaySymbolLane::SourceAndRepair, 22_302, 22_001),
            ],
        });
        program
    }

    fn standby_of(program: &mut RelayProgram) -> &mut OriginStandby {
        program.origin_standby.as_mut().expect("standby")
    }

    fn topology_codes(program: &RelayProgram) -> Vec<String> {
        let Err(ServicePlanError::Topology(violations)) = program.compile() else {
            panic!("expected topology violations");
        };
        violations
            .into_iter()
            .map(|violation| violation.code)
            .collect()
    }

    fn program_violations(program: &RelayProgram) -> Vec<String> {
        let Err(ServicePlanError::InvalidProgram(violations)) = program.compile() else {
            panic!("expected service program violations");
        };
        violations
    }

    fn pair(args: &[String], flag: &str, value: &str) -> bool {
        args.windows(2)
            .any(|pair| pair[0] == flag && pair[1] == value)
    }

    #[test]
    fn compiles_two_contributors_by_two_ingresses() {
        let single = merged_program().compile().expect("one contributor");
        // In a debug build this also proves the service count assertion
        // with a standby: one service more than the topology has nodes.
        let plan = two_contributor_program()
            .compile()
            .expect("two contributors");
        assert_eq!(plan.services.len(), 6);
        let contributors = plan
            .services
            .iter()
            .filter_map(|service| match service {
                CompiledService::AvContrib(contrib) => Some(contrib),
                CompiledService::AvMesh(_) => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(contributors.len(), 2);
        let (origin, standby) = (contributors[0], contributors[1]);
        assert_eq!(origin.node_id, "contrib");
        assert_eq!(standby.node_id, "contrib-b");

        // The standby keeps the origin's shape on sockets of its own: its
        // source lane to f1, its source-and-repair lane to f2.
        assert_eq!(standby.primary.child_node_id, "f1");
        assert_eq!(standby.primary.lane, RelaySymbolLane::Source);
        assert_eq!(standby.primary.bind, standby_address(22_301));
        assert_eq!(standby.primary.target, address(22_002));
        assert_eq!(standby.warm_secondary.child_node_id, "f2");
        assert_eq!(
            standby.warm_secondary.lane,
            RelaySymbolLane::SourceAndRepair
        );
        assert_eq!(standby.warm_secondary.bind, standby_address(22_302));
        assert_eq!(standby.warm_secondary.target, address(22_001));
        // One session identity and one deadline: what makes the two
        // contributors' datagrams equal.
        assert_eq!(standby.topology_generation, origin.topology_generation);
        assert_eq!(standby.subscription_id, origin.subscription_id);
        assert_eq!(standby.media_deadline_ms, origin.media_deadline_ms);
        assert_eq!(
            standby.source_path_observation,
            origin.source_path_observation
        );
        assert_eq!(
            standby.secondary_path_observation,
            origin.secondary_path_observation
        );
        let standby_args = standby.relay_arguments();
        assert!(pair(&standby_args, "--relay-local-id", "contrib-b"));
        assert!(pair(&standby_args, "--relay-primary-id", "f1"));
        assert!(pair(&standby_args, "--relay-secondary-id", "f2"));

        // Each first forwarder takes the standby on its secondary lane,
        // admitted in full, with no failover controller for it.
        for (forwarder, bind, peer, lane) in [
            ("f1", 22_002, 22_301, RelaySymbolLane::Source),
            ("f2", 22_001, 22_302, RelaySymbolLane::SourceAndRepair),
        ] {
            let service = mesh_service(&plan, forwarder);
            assert_eq!(service.primary_parent.parent_node_id, "contrib");
            assert!(service.secondary_parent.is_none());
            assert!(service.failover_controller.is_none());
            assert_eq!(
                service.standby_origin_parent,
                Some(CompiledParent {
                    parent_node_id: "contrib-b".to_owned(),
                    bind: address(bind),
                    peer: standby_address(peer),
                    lane,
                })
            );
            let args = service.relay_arguments();
            assert!(pair(&args, "--relay-primary-id", "contrib"));
            assert!(pair(
                &args,
                "--relay-secondary-bind",
                &address(bind).to_string()
            ));
            assert!(pair(
                &args,
                "--relay-secondary-peer",
                &standby_address(peer).to_string()
            ));
            assert!(pair(&args, "--relay-secondary-id", "contrib-b"));
            // The four arguments of the secondary lane, once each (the
            // warm window is the failover policy of its children).
            assert_eq!(
                args.iter()
                    .filter(|argument| {
                        argument.starts_with("--relay-secondary-")
                            && *argument != "--relay-secondary-warm-ms"
                    })
                    .map(String::as_str)
                    .collect::<Vec<_>>(),
                [
                    "--relay-secondary-bind",
                    "--relay-secondary-peer",
                    "--relay-secondary-id",
                    "--relay-secondary-promoted"
                ]
            );
            assert!(!args.contains(&"--relay-failover-controller".to_owned()));
            // Nothing else of the forwarder moves.
            let mut without = service.clone();
            without.standby_origin_parent = None;
            assert_eq!(&without, mesh_service(&single, forwarder));
        }

        // The origin and every node below the first forwarders compile
        // exactly as with one contributor.
        for unchanged in ["contrib", "f3", "f4"] {
            let find = |plan: &CompiledServicePlan| {
                plan.services
                    .iter()
                    .find(|service| service.node_id() == unchanged)
                    .cloned()
                    .expect("service")
            };
            assert_eq!(find(&plan), find(&single), "{unchanged}");
        }

        // Both contributors are in London: declared, never silent.
        let mut gaps = single.production_readiness_gaps.clone();
        gaps.push("origin_standby_shares_failure_domain:contrib-b".to_owned());
        assert_eq!(plan.production_readiness_gaps, gaps);
    }

    #[test]
    fn a_plan_with_one_contributor_serialises_as_before() {
        let plan = serde_json::to_string(&merged_program().compile().unwrap()).unwrap();
        assert!(!plan.contains("standby"), "{plan}");
        let plan = serde_json::to_value(two_contributor_program().compile().unwrap()).unwrap();
        let f1 = plan["services"]
            .as_array()
            .unwrap()
            .iter()
            .find(|service| service["node_id"] == "f1")
            .unwrap();
        assert_eq!(f1["service"], "av_mesh");
        assert_eq!(f1["standby_origin_parent"]["parent_node_id"], "contrib-b");
        assert_eq!(f1["standby_origin_parent"]["bind"], "127.0.0.1:22002");
        assert_eq!(f1["standby_origin_parent"]["peer"], "127.0.0.2:22301");
        assert_eq!(f1["standby_origin_parent"]["lane"], "source");
        assert!(f1["secondary_parent"].is_null());
        let contributors = plan["services"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|service| service["service"] == "av_contrib")
            .map(|service| service["node_id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert_eq!(contributors, ["contrib", "contrib-b"]);
    }

    #[test]
    fn the_standby_reads_as_the_deploy_writes_it() {
        let standby: OriginStandby = serde_json::from_str(
            r#"{"node_id": "contrib-b",
                "failure_domain": {"provider": "linode", "region": "gb-lon", "asn": 63949, "zone": "gb-lon"},
                "carrier_links": [
                 {"child_node_id": "lon-dist-a", "lane": "source",
                  "sender_bind": "10.77.1.7:22301", "sender_peer": "10.77.1.7:22301",
                  "receiver_bind": "0.0.0.0:22002", "receiver_target": "10.77.1.11:22002"},
                 {"child_node_id": "lon-dist-b", "lane": "source_and_repair",
                  "sender_bind": "10.77.1.7:22302", "sender_peer": "10.77.1.7:22302",
                  "receiver_bind": "0.0.0.0:22001", "receiver_target": "10.77.1.12:22001"}]}"#,
        )
        .expect("the standby of workstream A 2.9");
        assert_eq!(standby.node_id, "contrib-b");
        assert_eq!(standby.failure_domain.asn, 63_949);
        assert_eq!(standby.carrier_links.len(), 2);
        assert_eq!(standby.carrier_links[0].child_node_id, "lon-dist-a");
        assert_eq!(standby.carrier_links[0].lane, RelaySymbolLane::Source);
        assert_eq!(
            standby.carrier_links[1].lane,
            RelaySymbolLane::SourceAndRepair
        );
        assert_eq!(
            standby.carrier_links[1].receiver_target,
            "10.77.1.12:22001".parse::<SocketAddr>().unwrap()
        );
        let program = two_contributor_program();
        let json = serde_json::to_value(&program).unwrap();
        assert_eq!(json["origin_standby"]["node_id"], "contrib-b");
        let back: RelayProgram = serde_json::from_value(json).unwrap();
        assert_eq!(back, program);
    }

    #[test]
    fn a_standby_outside_the_origins_failure_domain_adds_no_gap() {
        let single = merged_program().compile().unwrap();
        let mut program = two_contributor_program();
        standby_of(&mut program).failure_domain =
            node("contrib-b", 0, NodeRole::Origin, "nl-ams", "nl-ams").failure_domain;
        // Another region and zone are independent for a single-provider
        // qualification.
        let plan = program.compile().expect("standby in another region");
        assert_eq!(
            plan.production_readiness_gaps,
            single.production_readiness_gaps
        );
        // Provider and ASN are still shared: a gap where those count.
        program.purpose = DeploymentPurpose::MultiProviderQualification;
        program.carrier = CarrierProfile::ControlledPublicUdp;
        let plan = program
            .compile()
            .expect("standby under multi-provider rules");
        assert!(plan
            .production_readiness_gaps
            .contains(&"origin_standby_shares_failure_domain:contrib-b".to_owned()));
        // Production never carries the gap: it refuses the program.
        program.purpose = DeploymentPurpose::Production;
        assert!(program_violations(&program)
            .iter()
            .any(|violation| violation.contains("origin standby that shares")));
    }

    #[test]
    fn the_standby_is_never_a_topology_node() {
        for node_id in ["contrib", "f1", "f3", ""] {
            let mut program = two_contributor_program();
            standby_of(&mut program).node_id = node_id.to_owned();
            assert_eq!(
                topology_codes(&program),
                ["origin_replica_node_id"],
                "{node_id:?}"
            );
        }
    }

    #[test]
    fn the_standby_has_one_link_to_each_origin_child_and_to_nothing_else() {
        // No link to f2.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links.pop();
        assert_eq!(topology_codes(&program), ["origin_replica_children"]);

        // Both links to f1.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[1].child_node_id = "f1".to_owned();
        assert_eq!(
            topology_codes(&program),
            ["origin_replica_children", "origin_replica_children"]
        );

        // A lane straight to a remote forwarder, beside the two: the
        // standby's fan-out would follow the regions.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links.push(standby_link(
            "f3",
            RelaySymbolLane::Source,
            22_303,
            22_003,
        ));
        assert_eq!(topology_codes(&program), ["origin_replica_children"]);

        // And instead of one of the two.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[1].child_node_id = "f4".to_owned();
        assert_eq!(
            topology_codes(&program),
            ["origin_replica_children", "origin_replica_children"]
        );
    }

    #[test]
    fn a_third_ingress_is_refused_with_a_standby() {
        // A third first forwarder under both contributors: 2 x 3.
        let mut program = two_contributor_program();
        program.topology.limits.max_origin_children = 3;
        program
            .topology
            .nodes
            .push(node("f5", 1, NodeRole::Backbone, "gb-lon", "gb-lon"));
        program.topology.parent_links.push(ParentLink {
            parent_node_id: "contrib".to_owned(),
            child_node_id: "f5".to_owned(),
            role: ParentRole::Primary,
        });
        program.carrier_links.push(link(
            "contrib",
            "f5",
            ParentRole::Primary,
            RelaySymbolLane::Source,
            22_303,
            22_003,
        ));
        standby_of(&mut program).carrier_links.push(standby_link(
            "f5",
            RelaySymbolLane::Source,
            22_303,
            22_004,
        ));
        assert_eq!(topology_codes(&program), ["origin_replica_fanout"]);
    }

    #[test]
    fn an_origin_child_with_a_secondary_parent_takes_no_standby() {
        // The legacy program with relay-b one level down and relay-a as
        // its secondary parent: it compiles, and relay-b's secondary lane
        // is then not free for a standby.
        let mut program = qualification_program();
        for node in &mut program.topology.nodes {
            match node.node_id.as_str() {
                "relay-b" => node.level = 2,
                "edge" => node.level = 3,
                _ => {}
            }
        }
        program.topology.parent_links.push(ParentLink {
            parent_node_id: "relay-a".to_owned(),
            child_node_id: "relay-b".to_owned(),
            role: ParentRole::Secondary,
        });
        program.carrier_links.push(link(
            "relay-a",
            "relay-b",
            ParentRole::Secondary,
            RelaySymbolLane::Repair,
            22_305,
            22_005,
        ));
        program.failover_control_links.push(FailoverControlLink {
            forwarder_node_id: "relay-a".to_owned(),
            controller_node_id: "relay-b".to_owned(),
            controller_bind: address(22_503),
            controller_peer: address(22_503),
            listener_bind: address(22_504),
            listener_target: address(22_504),
        });
        program.compile().expect("the program without a standby");
        program.origin_standby = Some(OriginStandby {
            node_id: "contrib-b".to_owned(),
            failure_domain: node("contrib-b", 0, NodeRole::Origin, "europe-west3", "z")
                .failure_domain,
            carrier_links: vec![
                standby_link("relay-a", RelaySymbolLane::Source, 22_301, 22_012),
                standby_link("relay-b", RelaySymbolLane::SourceAndRepair, 22_302, 22_011),
            ],
        });
        assert_eq!(
            topology_codes(&program),
            ["origin_replica_child_has_secondary_parent"]
        );
    }

    #[test]
    fn the_standby_keeps_the_lane_of_the_origins_link_to_each_child() {
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[1].lane = RelaySymbolLane::Source;
        let violations = program_violations(&program);
        assert_eq!(violations.len(), 1, "{violations:?}");
        assert!(
            violations[0].contains("origin standby link contrib-b -> f2 assigns Source lane"),
            "{violations:?}"
        );
        // Swapped lanes are two contributors of two shapes.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[0].lane = RelaySymbolLane::SourceAndRepair;
        standby_of(&mut program).carrier_links[1].lane = RelaySymbolLane::Source;
        assert_eq!(program_violations(&program).len(), 2);
        // A repair-only lane is no contributor lane at all.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[0].lane = RelaySymbolLane::Repair;
        assert!(program_violations(&program)[0].contains("assigns Repair lane"));
    }

    #[test]
    fn the_standby_needs_explicit_sender_addresses_and_ports() {
        let unspecified: SocketAddr = "0.0.0.0:22301".parse().unwrap();
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[0].sender_bind = unspecified;
        let violations = program_violations(&program);
        assert_eq!(violations.len(), 1, "{violations:?}");
        assert!(violations[0]
            .contains("origin standby link contrib-b -> f1 requires an explicit sender IP"));
        // The peer is the standby's identity at the ingress.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[0].sender_peer = unspecified;
        assert!(program_violations(&program)[0].contains("requires an explicit sender IP"));

        for field in 0..4 {
            let mut program = two_contributor_program();
            let link = &mut standby_of(&mut program).carrier_links[1];
            let address = match field {
                0 => &mut link.sender_bind,
                1 => &mut link.sender_peer,
                2 => &mut link.receiver_bind,
                _ => &mut link.receiver_target,
            };
            address.set_port(0);
            let violations = program_violations(&program);
            assert!(
                violations.iter().any(|violation| violation
                    .contains("origin standby link contrib-b -> f2 requires non-zero ports")),
                "{field}: {violations:?}"
            );
        }
    }

    #[test]
    fn the_standby_takes_only_free_sockets() {
        // f1's primary lane already binds 22001 for the origin.
        let mut program = two_contributor_program();
        let link = &mut standby_of(&mut program).carrier_links[0];
        link.receiver_bind = address(22_001);
        link.receiver_target = address(22_001);
        let violations = program_violations(&program);
        assert_eq!(violations.len(), 1, "{violations:?}");
        assert!(violations[0].contains(
            "node f1 reuses RelaySession receiver bind 127.0.0.1:22001 for origin standby contrib-b"
        ));

        // f1's failover listener for f4 binds 22512.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[0].receiver_bind = address(22_512);
        let violations = program_violations(&program);
        assert_eq!(violations.len(), 1, "{violations:?}");
        assert!(violations[0].contains("node f1 reuses failover control bind 127.0.0.1:22512"));

        // One sender socket for both of the standby's lanes.
        let mut program = two_contributor_program();
        standby_of(&mut program).carrier_links[1].sender_bind = standby_address(22_301);
        let violations = program_violations(&program);
        assert_eq!(violations.len(), 1, "{violations:?}");
        assert!(violations[0].contains("node contrib-b reuses RelaySession sender bind"));
    }
}
