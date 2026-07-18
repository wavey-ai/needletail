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
    FailureDiversityRequirement, NodeRole, ParentRole, PolicyViolation, RelayTopology,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeploymentPurpose {
    LocalQualification,
    SingleProviderQualification,
    Production,
}

impl DeploymentPurpose {
    const fn diversity(self) -> FailureDiversityRequirement {
        match self {
            Self::LocalQualification => FailureDiversityRequirement::DistinctNodes,
            Self::SingleProviderQualification => FailureDiversityRequirement::RegionAndZone,
            Self::Production => FailureDiversityRequirement::ProviderRegionAsnAndZone,
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
    pub topology: RelayTopology,
    pub carrier_links: Vec<CarrierLink>,
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
    pub fn compile(&self) -> Result<CompiledServicePlan, ServicePlanError> {
        self.topology
            .validate_with_diversity(self.purpose.diversity())
            .map_err(ServicePlanError::Topology)?;

        let mut violations = Vec::new();
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

        let secondary_carriers = self
            .carrier_links
            .iter()
            .filter(|link| link.role == ParentRole::Secondary)
            .collect::<Vec<_>>();
        let mut control_keys = HashSet::with_capacity(self.failover_control_links.len());
        let mut controller_nodes = HashSet::with_capacity(self.failover_control_links.len());
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
            if !controller_nodes.insert(control.controller_node_id.as_str()) {
                violations.push(format!(
                    "node {} has more than one failover controller relationship",
                    control.controller_node_id
                ));
            }
            let carrier = secondary_carriers.iter().find(|carrier| {
                carrier.parent_node_id == control.forwarder_node_id
                    && carrier.child_node_id == control.controller_node_id
            });
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
        let origin_links = self
            .carrier_links
            .iter()
            .filter(|link| link.parent_node_id == origin.node_id)
            .collect::<Vec<_>>();
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

        for node in self
            .topology
            .nodes
            .iter()
            .filter(|node| node.role != NodeRole::Origin)
        {
            let incoming = self
                .carrier_links
                .iter()
                .filter(|link| link.child_node_id == node.node_id)
                .collect::<Vec<_>>();
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
        for node in self
            .topology
            .nodes
            .iter()
            .filter(|node| node.role != NodeRole::Origin)
        {
            let primary = self
                .carrier_links
                .iter()
                .find(|link| link.child_node_id == node.node_id && link.role == ParentRole::Primary)
                .expect("validated primary carrier");
            let secondary = self.carrier_links.iter().find(|link| {
                link.child_node_id == node.node_id && link.role == ParentRole::Secondary
            });
            let mut forwards = self
                .carrier_links
                .iter()
                .filter(|link| link.parent_node_id == node.node_id)
                .map(compiled_forward)
                .collect::<Vec<_>>();
            forwards.sort_by(|left, right| left.child_node_id.cmp(&right.child_node_id));
            let mut failover_listeners = self
                .failover_control_links
                .iter()
                .filter(|control| control.forwarder_node_id == node.node_id)
                .map(|control| {
                    let carrier = self
                        .carrier_links
                        .iter()
                        .find(|carrier| {
                            carrier.parent_node_id == control.forwarder_node_id
                                && carrier.child_node_id == control.controller_node_id
                                && carrier.role == ParentRole::Secondary
                        })
                        .expect("validated failover carrier");
                    CompiledFailoverListener {
                        bind: control.listener_bind,
                        peer: control.controller_peer,
                        forward_target: carrier.receiver_target,
                    }
                })
                .collect::<Vec<_>>();
            failover_listeners.sort_by_key(|listener| listener.forward_target);
            let failover_controller = self
                .failover_control_links
                .iter()
                .find(|control| control.controller_node_id == node.node_id)
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
        // prevents future compilation paths from silently omitting a node.
        debug_assert_eq!(services.len(), nodes.len());
        Ok(CompiledServicePlan {
            purpose: self.purpose,
            carrier: self.carrier,
            topology_generation: self.topology.generation,
            subscription_id: self.subscription_id,
            production_readiness_gaps: self.purpose.readiness_gaps(),
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::relay_topology::{FailureDomain, ParentLink, RelayNode, TopologyLimits};

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
}
