use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};

pub const MAX_FORWARDING_PARENTS: usize = 2;
pub const MIN_FAST_BACKBONE_NODES: usize = 3;
pub const MAX_FAST_BACKBONE_NODES: usize = 5;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NodeRole {
    Origin,
    Backbone,
    RegionalRelay,
    PlaybackEdge,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailureDomain {
    pub provider: String,
    pub region: String,
    pub asn: u32,
    pub zone: String,
}

impl FailureDomain {
    fn is_independent_from(&self, other: &Self, requirement: FailureDiversityRequirement) -> bool {
        match requirement {
            FailureDiversityRequirement::ProviderRegionAsnAndZone => {
                self.provider != other.provider
                    && self.region != other.region
                    && self.asn != other.asn
                    && self.zone != other.zone
            }
            FailureDiversityRequirement::RegionAndZone => {
                self.region != other.region && self.zone != other.zone
            }
        }
    }
}

/// Failure-domain policy for a route validation stage. Production uses the
/// full requirement. A declared single-provider qualification can exercise
/// inter-region mechanics while retaining an explicit production-readiness
/// gap for provider and ASN diversity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureDiversityRequirement {
    ProviderRegionAsnAndZone,
    RegionAndZone,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RelayNode {
    pub node_id: String,
    pub level: u16,
    pub role: NodeRole,
    pub failure_domain: FailureDomain,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ParentRole {
    Primary,
    Secondary,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParentLink {
    pub parent_node_id: String,
    pub child_node_id: String,
    pub role: ParentRole,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TopologyLimits {
    pub max_origin_children: usize,
    pub max_downstream_children: usize,
}

impl Default for TopologyLimits {
    fn default() -> Self {
        Self {
            max_origin_children: 4,
            max_downstream_children: 64,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RelayTopology {
    pub generation: u64,
    pub nodes: Vec<RelayNode>,
    pub parent_links: Vec<ParentLink>,
    pub limits: TopologyLimits,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PolicyViolation {
    pub code: String,
    pub detail: String,
}

impl PolicyViolation {
    fn new(code: &str, detail: impl Into<String>) -> Self {
        Self {
            code: code.to_owned(),
            detail: detail.into(),
        }
    }
}

impl RelayTopology {
    pub fn validate(&self) -> Result<(), Vec<PolicyViolation>> {
        self.validate_with_diversity(FailureDiversityRequirement::ProviderRegionAsnAndZone)
    }

    pub fn validate_with_diversity(
        &self,
        diversity: FailureDiversityRequirement,
    ) -> Result<(), Vec<PolicyViolation>> {
        let mut violations = Vec::new();
        if self.generation == 0 {
            violations.push(PolicyViolation::new(
                "generation_zero",
                "desired-state generations start at one",
            ));
        }
        if self.limits.max_origin_children == 0 || self.limits.max_downstream_children == 0 {
            violations.push(PolicyViolation::new(
                "fanout_limit_zero",
                "origin and downstream fanout limits must be positive",
            ));
        }

        let mut nodes = HashMap::with_capacity(self.nodes.len());
        for node in &self.nodes {
            if node.node_id.is_empty() {
                violations.push(PolicyViolation::new(
                    "empty_node_id",
                    "every relay node requires an identity",
                ));
                continue;
            }
            if nodes.insert(node.node_id.as_str(), node).is_some() {
                violations.push(PolicyViolation::new(
                    "duplicate_node_id",
                    format!("node {} appears more than once", node.node_id),
                ));
            }
            if node.failure_domain.provider.is_empty()
                || node.failure_domain.region.is_empty()
                || node.failure_domain.zone.is_empty()
                || node.failure_domain.asn == 0
            {
                violations.push(PolicyViolation::new(
                    "incomplete_failure_domain",
                    format!(
                        "node {} requires provider, region, ASN, and zone",
                        node.node_id
                    ),
                ));
            }
        }

        let origins = self
            .nodes
            .iter()
            .filter(|node| node.role == NodeRole::Origin)
            .collect::<Vec<_>>();
        if origins.len() != 1 {
            violations.push(PolicyViolation::new(
                "origin_count",
                format!(
                    "a stream topology requires one origin; found {}",
                    origins.len()
                ),
            ));
        }
        for origin in &origins {
            if origin.level != 0 {
                violations.push(PolicyViolation::new(
                    "origin_level",
                    format!("origin {} must be level zero", origin.node_id),
                ));
            }
        }
        for node in self
            .nodes
            .iter()
            .filter(|node| node.role != NodeRole::Origin && node.level == 0)
        {
            violations.push(PolicyViolation::new(
                "non_origin_level_zero",
                format!("node {} must follow the origin level", node.node_id),
            ));
        }

        let mut links_seen = HashSet::with_capacity(self.parent_links.len());
        let mut links_by_child: HashMap<&str, Vec<&ParentLink>> = HashMap::new();
        let mut children_by_parent: HashMap<&str, HashSet<&str>> = HashMap::new();
        for link in &self.parent_links {
            let key = (
                link.parent_node_id.as_str(),
                link.child_node_id.as_str(),
                link.role,
            );
            if !links_seen.insert(key) {
                violations.push(PolicyViolation::new(
                    "duplicate_parent_link",
                    format!(
                        "{} -> {} repeats the {:?} relationship",
                        link.parent_node_id, link.child_node_id, link.role
                    ),
                ));
            }
            links_by_child
                .entry(link.child_node_id.as_str())
                .or_default()
                .push(link);
            children_by_parent
                .entry(link.parent_node_id.as_str())
                .or_default()
                .insert(link.child_node_id.as_str());

            let Some(parent) = nodes.get(link.parent_node_id.as_str()) else {
                violations.push(PolicyViolation::new(
                    "unknown_parent",
                    format!("parent {} is absent", link.parent_node_id),
                ));
                continue;
            };
            let Some(child) = nodes.get(link.child_node_id.as_str()) else {
                violations.push(PolicyViolation::new(
                    "unknown_child",
                    format!("child {} is absent", link.child_node_id),
                ));
                continue;
            };
            if parent.level >= child.level {
                violations.push(PolicyViolation::new(
                    "non_acyclic_level",
                    format!(
                        "{} at level {} cannot parent {} at level {}",
                        parent.node_id, parent.level, child.node_id, child.level
                    ),
                ));
            }
        }

        for node in &self.nodes {
            let links = links_by_child
                .get(node.node_id.as_str())
                .map(Vec::as_slice)
                .unwrap_or_default();
            if node.role == NodeRole::Origin {
                if !links.is_empty() {
                    violations.push(PolicyViolation::new(
                        "origin_has_parent",
                        format!("origin {} has an upstream parent", node.node_id),
                    ));
                }
                continue;
            }
            if links.len() > MAX_FORWARDING_PARENTS {
                violations.push(PolicyViolation::new(
                    "too_many_parents",
                    format!("node {} has {} upstream parents", node.node_id, links.len()),
                ));
            }
            let primary = links
                .iter()
                .filter(|link| link.role == ParentRole::Primary)
                .count();
            let secondary = links
                .iter()
                .filter(|link| link.role == ParentRole::Secondary)
                .count();
            if primary != 1 {
                violations.push(PolicyViolation::new(
                    "primary_parent_count",
                    format!("node {} requires one primary parent", node.node_id),
                ));
            }
            if secondary > 1 {
                violations.push(PolicyViolation::new(
                    "secondary_parent_count",
                    format!("node {} has {} secondary parents", node.node_id, secondary),
                ));
            }
            if let (Some(primary_link), Some(secondary_link)) = (
                links.iter().find(|link| link.role == ParentRole::Primary),
                links.iter().find(|link| link.role == ParentRole::Secondary),
            ) {
                if primary_link.parent_node_id == secondary_link.parent_node_id {
                    violations.push(PolicyViolation::new(
                        "duplicate_parent_role",
                        format!("node {} assigns both roles to one parent", node.node_id),
                    ));
                } else if let (Some(primary_node), Some(secondary_node)) = (
                    nodes.get(primary_link.parent_node_id.as_str()),
                    nodes.get(secondary_link.parent_node_id.as_str()),
                ) {
                    if !primary_node
                        .failure_domain
                        .is_independent_from(&secondary_node.failure_domain, diversity)
                    {
                        violations.push(PolicyViolation::new(
                            "parent_failure_domain_overlap",
                            format!(
                                "parents {} and {} for {} require distinct provider, region, ASN, and zone",
                                primary_node.node_id, secondary_node.node_id, node.node_id
                            ),
                        ));
                    }
                }
            }
        }

        for (parent_id, children) in &children_by_parent {
            let limit = nodes
                .get(parent_id)
                .map(|node| {
                    if node.role == NodeRole::Origin {
                        self.limits.max_origin_children
                    } else {
                        self.limits.max_downstream_children
                    }
                })
                .unwrap_or(self.limits.max_downstream_children);
            if children.len() > limit {
                violations.push(PolicyViolation::new(
                    "downstream_fanout_exceeded",
                    format!(
                        "parent {parent_id} has {} children; limit is {limit}",
                        children.len()
                    ),
                ));
            }
        }

        let relationship_limit = self.nodes.len().saturating_sub(origins.len()) * 2;
        if self.parent_links.len() > relationship_limit {
            violations.push(PolicyViolation::new(
                "relationship_bound_exceeded",
                format!(
                    "{} relationships exceed the approximately 2N bound of {}",
                    self.parent_links.len(),
                    relationship_limit
                ),
            ));
        }

        if violations.is_empty() {
            Ok(())
        } else {
            Err(violations)
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeliveryClass {
    Interactive,
    PremiumLive,
    MassBroadcast,
    ResilientContribution,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LatencyPolicy {
    pub max_inter_region_relay_hops: u8,
    pub max_path_stretch_millis: u16,
    pub max_relay_processing_p95_us: u32,
    pub max_media_queue_p95_us: u32,
    pub max_jitter_p95_us: u32,
    pub max_loss_ppm: u32,
    pub max_deadline_miss_ppm: u32,
}

impl LatencyPolicy {
    pub fn for_class(class: DeliveryClass) -> Self {
        match class {
            DeliveryClass::Interactive => Self {
                max_inter_region_relay_hops: 1,
                max_path_stretch_millis: 1_150,
                max_relay_processing_p95_us: 1_000,
                max_media_queue_p95_us: 5_000,
                max_jitter_p95_us: 5_000,
                max_loss_ppm: 50_000,
                max_deadline_miss_ppm: 1_000,
            },
            DeliveryClass::PremiumLive => Self {
                max_inter_region_relay_hops: 2,
                max_path_stretch_millis: 1_250,
                max_relay_processing_p95_us: 1_000,
                max_media_queue_p95_us: 5_000,
                max_jitter_p95_us: 10_000,
                max_loss_ppm: 100_000,
                max_deadline_miss_ppm: 5_000,
            },
            DeliveryClass::MassBroadcast => Self {
                max_inter_region_relay_hops: 2,
                max_path_stretch_millis: 1_500,
                max_relay_processing_p95_us: 2_000,
                max_media_queue_p95_us: 10_000,
                max_jitter_p95_us: 25_000,
                max_loss_ppm: 150_000,
                max_deadline_miss_ppm: 10_000,
            },
            DeliveryClass::ResilientContribution => Self {
                max_inter_region_relay_hops: 2,
                max_path_stretch_millis: 1_250,
                max_relay_processing_p95_us: 1_000,
                max_media_queue_p95_us: 5_000,
                max_jitter_p95_us: 10_000,
                max_loss_ppm: 100_000,
                max_deadline_miss_ppm: 1_000,
            },
        }
    }

    pub fn validate_measurement(
        &self,
        measurement: &RouteMeasurement,
    ) -> Result<(), Vec<PolicyViolation>> {
        let mut violations = Vec::new();
        if measurement.best_direct_rtt_us == 0 {
            violations.push(PolicyViolation::new(
                "direct_rtt_zero",
                "route selection requires a measured direct-path baseline",
            ));
        }
        if measurement.selected_path_rtt_us == 0 {
            violations.push(PolicyViolation::new(
                "selected_rtt_zero",
                "route selection requires a measured candidate-path RTT",
            ));
        } else if measurement.best_direct_rtt_us != 0
            && u128::from(measurement.selected_path_rtt_us) * 1_000
                > u128::from(measurement.best_direct_rtt_us)
                    * u128::from(self.max_path_stretch_millis)
        {
            violations.push(PolicyViolation::new(
                "path_stretch_exceeded",
                format!(
                    "selected RTT {}us exceeds {} permille of direct RTT {}us",
                    measurement.selected_path_rtt_us,
                    self.max_path_stretch_millis,
                    measurement.best_direct_rtt_us
                ),
            ));
        }
        if measurement.inter_region_relay_hops > self.max_inter_region_relay_hops {
            violations.push(PolicyViolation::new(
                "relay_hop_limit_exceeded",
                format!(
                    "{} inter-region relay hops exceed limit {}",
                    measurement.inter_region_relay_hops, self.max_inter_region_relay_hops
                ),
            ));
        }
        if measurement.relay_processing_p95_us > self.max_relay_processing_p95_us {
            violations.push(PolicyViolation::new(
                "relay_processing_budget_exceeded",
                format!(
                    "relay processing p95 {}us exceeds {}us",
                    measurement.relay_processing_p95_us, self.max_relay_processing_p95_us
                ),
            ));
        }
        if measurement.media_queue_p95_us > self.max_media_queue_p95_us {
            violations.push(PolicyViolation::new(
                "media_queue_budget_exceeded",
                format!(
                    "media queue p95 {}us exceeds {}us",
                    measurement.media_queue_p95_us, self.max_media_queue_p95_us
                ),
            ));
        }
        if measurement.jitter_p95_us > self.max_jitter_p95_us {
            violations.push(PolicyViolation::new(
                "jitter_budget_exceeded",
                format!(
                    "path jitter p95 {}us exceeds {}us",
                    measurement.jitter_p95_us, self.max_jitter_p95_us
                ),
            ));
        }
        if measurement.loss_ppm > self.max_loss_ppm {
            violations.push(PolicyViolation::new(
                "loss_budget_exceeded",
                format!(
                    "path loss {}ppm exceeds {}ppm",
                    measurement.loss_ppm, self.max_loss_ppm
                ),
            ));
        }
        if measurement.deadline_miss_ppm > self.max_deadline_miss_ppm {
            violations.push(PolicyViolation::new(
                "deadline_miss_budget_exceeded",
                format!(
                    "deadline misses {}ppm exceed {}ppm",
                    measurement.deadline_miss_ppm, self.max_deadline_miss_ppm
                ),
            ));
        }
        if violations.is_empty() {
            Ok(())
        } else {
            Err(violations)
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteMeasurement {
    pub best_direct_rtt_us: u64,
    pub selected_path_rtt_us: u64,
    pub inter_region_relay_hops: u8,
    pub relay_processing_p95_us: u32,
    pub media_queue_p95_us: u32,
    pub jitter_p95_us: u32,
    pub loss_ppm: u32,
    pub deadline_miss_ppm: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteCohort {
    pub stream_id: String,
    pub destination_region: String,
    pub destination_asn: u32,
    pub rendition: String,
    pub delivery_class: DeliveryClass,
}

/// A long-lived carrier session between two trusted backbone relays.
///
/// These sessions provide ready connectivity. Compiled [`ParentLink`] sets are
/// the exclusive authority for per-stream media forwarding.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackboneSessionLink {
    pub left_node_id: String,
    pub right_node_id: String,
}

/// The small, failure-domain-diverse session overlay used by the interactive
/// lane to keep direct and one-backbone-hop paths ready.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackboneSessionOverlay {
    pub generation: u64,
    pub trusted_node_ids: Vec<String>,
    pub session_links: Vec<BackboneSessionLink>,
}

impl BackboneSessionOverlay {
    pub fn validate(&self, node_catalog: &[RelayNode]) -> Result<(), Vec<PolicyViolation>> {
        let mut violations = Vec::new();
        if self.generation == 0 {
            violations.push(PolicyViolation::new(
                "overlay_generation_zero",
                "backbone session-overlay generations start at one",
            ));
        }
        if !(MIN_FAST_BACKBONE_NODES..=MAX_FAST_BACKBONE_NODES)
            .contains(&self.trusted_node_ids.len())
        {
            violations.push(PolicyViolation::new(
                "backbone_node_count",
                format!(
                    "the interactive backbone requires {MIN_FAST_BACKBONE_NODES} to {MAX_FAST_BACKBONE_NODES} trusted nodes; found {}",
                    self.trusted_node_ids.len()
                ),
            ));
        }

        let catalog = node_catalog
            .iter()
            .map(|node| (node.node_id.as_str(), node))
            .collect::<HashMap<_, _>>();
        let mut member_ids = HashSet::with_capacity(self.trusted_node_ids.len());
        let mut members = Vec::with_capacity(self.trusted_node_ids.len());
        for node_id in &self.trusted_node_ids {
            if !member_ids.insert(node_id.as_str()) {
                violations.push(PolicyViolation::new(
                    "duplicate_backbone_node",
                    format!("trusted backbone node {node_id} appears more than once"),
                ));
                continue;
            }
            let Some(node) = catalog.get(node_id.as_str()).copied() else {
                violations.push(PolicyViolation::new(
                    "unknown_backbone_node",
                    format!("trusted backbone node {node_id} is absent from the catalog"),
                ));
                continue;
            };
            if node.role != NodeRole::Backbone {
                violations.push(PolicyViolation::new(
                    "backbone_role_required",
                    format!("trusted overlay member {node_id} requires the backbone role"),
                ));
            }
            members.push(node);
        }

        for (index, left) in members.iter().enumerate() {
            for right in members.iter().skip(index + 1) {
                if !left.failure_domain.is_independent_from(
                    &right.failure_domain,
                    FailureDiversityRequirement::ProviderRegionAsnAndZone,
                ) {
                    violations.push(PolicyViolation::new(
                        "backbone_failure_domain_overlap",
                        format!(
                            "backbone nodes {} and {} require distinct provider, region, ASN, and zone",
                            left.node_id, right.node_id
                        ),
                    ));
                }
            }
        }

        let mut sessions_seen = HashSet::with_capacity(self.session_links.len());
        let mut degree: HashMap<&str, usize> =
            member_ids.iter().map(|node_id| (*node_id, 0)).collect();
        for session in &self.session_links {
            if session.left_node_id == session.right_node_id {
                violations.push(PolicyViolation::new(
                    "backbone_session_self_link",
                    format!(
                        "backbone session endpoint {} requires a distinct peer",
                        session.left_node_id
                    ),
                ));
                continue;
            }
            let left_known = member_ids.contains(session.left_node_id.as_str());
            let right_known = member_ids.contains(session.right_node_id.as_str());
            if !left_known || !right_known {
                violations.push(PolicyViolation::new(
                    "backbone_session_unknown_endpoint",
                    format!(
                        "backbone session {} <-> {} requires two trusted overlay members",
                        session.left_node_id, session.right_node_id
                    ),
                ));
                continue;
            }
            let key = if session.left_node_id < session.right_node_id {
                (
                    session.left_node_id.as_str(),
                    session.right_node_id.as_str(),
                )
            } else {
                (
                    session.right_node_id.as_str(),
                    session.left_node_id.as_str(),
                )
            };
            if !sessions_seen.insert(key) {
                violations.push(PolicyViolation::new(
                    "duplicate_backbone_session",
                    format!(
                        "backbone session {} <-> {} appears more than once",
                        session.left_node_id, session.right_node_id
                    ),
                ));
                continue;
            }
            *degree.entry(session.left_node_id.as_str()).or_default() += 1;
            *degree.entry(session.right_node_id.as_str()).or_default() += 1;
        }

        if member_ids.len() >= MIN_FAST_BACKBONE_NODES {
            let minimum_degree = 2.max(member_ids.len().saturating_sub(2));
            for node_id in &member_ids {
                let node_degree = degree.get(node_id).copied().unwrap_or_default();
                if node_degree < minimum_degree {
                    violations.push(PolicyViolation::new(
                        "backbone_session_resilience",
                        format!(
                            "backbone node {node_id} requires sessions with at least {minimum_degree} trusted peers; found {node_degree}"
                        ),
                    ));
                }
            }
        }

        if violations.is_empty() {
            Ok(())
        } else {
            Err(violations)
        }
    }

    fn contains(&self, node_id: &str) -> bool {
        self.trusted_node_ids
            .iter()
            .any(|member_id| member_id == node_id)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "shape", rename_all = "snake_case")]
pub enum InteractiveRoutePath {
    Direct {
        ingress_node_id: String,
        edge_node_id: String,
    },
    OneBackboneHop {
        ingress_node_id: String,
        backbone_node_id: String,
        edge_node_id: String,
    },
}

impl InteractiveRoutePath {
    fn ingress_node_id(&self) -> &str {
        match self {
            Self::Direct {
                ingress_node_id, ..
            }
            | Self::OneBackboneHop {
                ingress_node_id, ..
            } => ingress_node_id,
        }
    }

    fn edge_node_id(&self) -> &str {
        match self {
            Self::Direct { edge_node_id, .. } | Self::OneBackboneHop { edge_node_id, .. } => {
                edge_node_id
            }
        }
    }

    fn edge_parent_node_id(&self) -> &str {
        match self {
            Self::Direct {
                ingress_node_id, ..
            } => ingress_node_id,
            Self::OneBackboneHop {
                backbone_node_id, ..
            } => backbone_node_id,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InteractiveRouteCandidate {
    pub candidate_id: String,
    pub path: InteractiveRoutePath,
    pub measurement: RouteMeasurement,
}

impl InteractiveRouteCandidate {
    fn validate_shape(
        &self,
        node_catalog: &[RelayNode],
        overlay: &BackboneSessionOverlay,
    ) -> Result<(), Vec<PolicyViolation>> {
        let mut violations = Vec::new();
        if self.candidate_id.is_empty() {
            violations.push(PolicyViolation::new(
                "empty_route_candidate_id",
                "interactive route candidates require an identity",
            ));
        }
        let catalog = node_catalog
            .iter()
            .map(|node| (node.node_id.as_str(), node))
            .collect::<HashMap<_, _>>();
        let ingress = catalog.get(self.path.ingress_node_id()).copied();
        let edge = catalog.get(self.path.edge_node_id()).copied();
        match ingress {
            Some(node) if node.role != NodeRole::Origin => {
                violations.push(PolicyViolation::new(
                    "interactive_ingress_role",
                    format!(
                        "interactive ingress {} requires the origin role",
                        node.node_id
                    ),
                ));
            }
            None => violations.push(PolicyViolation::new(
                "unknown_interactive_ingress",
                format!(
                    "interactive ingress {} is absent from the catalog",
                    self.path.ingress_node_id()
                ),
            )),
            _ => {}
        }
        match edge {
            Some(node) if node.role != NodeRole::PlaybackEdge => {
                violations.push(PolicyViolation::new(
                    "interactive_edge_role",
                    format!(
                        "interactive destination {} requires the playback-edge role",
                        node.node_id
                    ),
                ));
            }
            None => violations.push(PolicyViolation::new(
                "unknown_interactive_edge",
                format!(
                    "interactive destination {} is absent from the catalog",
                    self.path.edge_node_id()
                ),
            )),
            _ => {}
        }

        match &self.path {
            InteractiveRoutePath::Direct {
                ingress_node_id,
                edge_node_id,
            } => {
                if ingress_node_id == edge_node_id {
                    violations.push(PolicyViolation::new(
                        "interactive_path_cycle",
                        "a direct interactive path requires distinct ingress and edge nodes",
                    ));
                }
                if self.measurement.inter_region_relay_hops != 0 {
                    violations.push(PolicyViolation::new(
                        "direct_path_relay_hop",
                        "a direct interactive path has zero relay hops",
                    ));
                }
                if let (Some(ingress), Some(edge)) = (ingress, edge) {
                    if ingress.level >= edge.level {
                        violations.push(PolicyViolation::new(
                            "interactive_non_acyclic_level",
                            format!(
                                "direct path {} at level {} must precede {} at level {}",
                                ingress.node_id, ingress.level, edge.node_id, edge.level
                            ),
                        ));
                    }
                }
            }
            InteractiveRoutePath::OneBackboneHop {
                ingress_node_id,
                backbone_node_id,
                edge_node_id,
            } => {
                if ingress_node_id == backbone_node_id
                    || backbone_node_id == edge_node_id
                    || ingress_node_id == edge_node_id
                {
                    violations.push(PolicyViolation::new(
                        "interactive_path_cycle",
                        "a one-backbone-hop path requires three distinct nodes",
                    ));
                }
                if !overlay.contains(backbone_node_id) {
                    violations.push(PolicyViolation::new(
                        "backbone_outside_trusted_overlay",
                        format!(
                            "interactive backbone {backbone_node_id} requires trusted overlay membership"
                        ),
                    ));
                }
                let backbone = catalog.get(backbone_node_id.as_str()).copied();
                match backbone {
                    Some(node) if node.role != NodeRole::Backbone => {
                        violations.push(PolicyViolation::new(
                            "interactive_backbone_role",
                            format!(
                                "interactive relay {} requires the backbone role",
                                node.node_id
                            ),
                        ));
                    }
                    None => violations.push(PolicyViolation::new(
                        "unknown_interactive_backbone",
                        format!(
                            "interactive backbone {backbone_node_id} is absent from the catalog"
                        ),
                    )),
                    _ => {}
                }
                if let (Some(ingress), Some(backbone), Some(edge)) = (ingress, backbone, edge) {
                    if ingress.level >= backbone.level || backbone.level >= edge.level {
                        violations.push(PolicyViolation::new(
                            "interactive_non_acyclic_level",
                            format!(
                                "interactive levels must increase from {} ({}) through {} ({}) to {} ({})",
                                ingress.node_id,
                                ingress.level,
                                backbone.node_id,
                                backbone.level,
                                edge.node_id,
                                edge.level
                            ),
                        ));
                    }
                }
            }
        }

        if violations.is_empty() {
            Ok(())
        } else {
            Err(violations)
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PendingRouteReadiness {
    Establishing,
    Warm,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PendingInteractivePrimary {
    pub desired_generation: u64,
    pub candidate: InteractiveRouteCandidate,
    pub readiness: PendingRouteReadiness,
}

/// Active interactive routing state. `primary` carries live source symbols,
/// `secondary` is warm for repair and immediate takeover, and `pending_primary`
/// is established before activation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InteractiveRouteSelection {
    pub generation: u64,
    pub cohort: RouteCohort,
    pub primary: InteractiveRouteCandidate,
    pub secondary: InteractiveRouteCandidate,
    pub pending_primary: Option<PendingInteractivePrimary>,
}

impl InteractiveRouteSelection {
    pub fn validate(
        &self,
        node_catalog: &[RelayNode],
        overlay: &BackboneSessionOverlay,
    ) -> Result<(), Vec<PolicyViolation>> {
        let mut violations = Vec::new();
        if self.generation == 0 {
            violations.push(PolicyViolation::new(
                "route_generation_zero",
                "interactive route generations start at one",
            ));
        }
        if self.cohort.delivery_class != DeliveryClass::Interactive {
            violations.push(PolicyViolation::new(
                "interactive_delivery_class_required",
                "interactive route state requires an interactive cohort",
            ));
        }
        if let Err(mut overlay_violations) = overlay.validate(node_catalog) {
            violations.append(&mut overlay_violations);
        }

        let policy = LatencyPolicy::for_class(DeliveryClass::Interactive);
        for candidate in [&self.primary, &self.secondary] {
            if let Err(mut shape_violations) = candidate.validate_shape(node_catalog, overlay) {
                violations.append(&mut shape_violations);
            }
            if let Err(mut measurement_violations) =
                policy.validate_measurement(&candidate.measurement)
            {
                violations.append(&mut measurement_violations);
            }
        }
        if self.primary.candidate_id == self.secondary.candidate_id {
            violations.push(PolicyViolation::new(
                "duplicate_active_route_candidate",
                "primary and secondary route candidates require distinct identities",
            ));
        }
        if !routes_share_endpoints(&self.primary, &self.secondary) {
            violations.push(PolicyViolation::new(
                "route_endpoint_mismatch",
                "primary and secondary routes must serve the same ingress and playback edge",
            ));
        }
        if !routes_have_independent_parents(&self.primary, &self.secondary, node_catalog) {
            violations.push(PolicyViolation::new(
                "active_parent_failure_domain_overlap",
                "primary and warm-secondary parents require independent failure domains",
            ));
        }

        if let Some(pending) = &self.pending_primary {
            if pending.desired_generation <= self.generation {
                violations.push(PolicyViolation::new(
                    "route_generation_not_newer",
                    format!(
                        "pending generation {} must follow active generation {}",
                        pending.desired_generation, self.generation
                    ),
                ));
            }
            if pending.candidate.candidate_id == self.primary.candidate_id
                || pending.candidate.candidate_id == self.secondary.candidate_id
            {
                violations.push(PolicyViolation::new(
                    "pending_route_candidate_already_active",
                    format!(
                        "pending candidate {} already has an active route role",
                        pending.candidate.candidate_id
                    ),
                ));
            }
            if let Err(mut shape_violations) =
                pending.candidate.validate_shape(node_catalog, overlay)
            {
                violations.append(&mut shape_violations);
            }
            if let Err(mut measurement_violations) =
                policy.validate_measurement(&pending.candidate.measurement)
            {
                violations.append(&mut measurement_violations);
            }
            if !routes_share_endpoints(&self.primary, &pending.candidate) {
                violations.push(PolicyViolation::new(
                    "route_endpoint_mismatch",
                    "a pending primary must serve the active ingress and playback edge",
                ));
            }
            if !routes_have_independent_parents(&self.primary, &pending.candidate, node_catalog) {
                violations.push(PolicyViolation::new(
                    "pending_primary_failure_domain_overlap",
                    "the pending primary and retiring primary require independent failure domains",
                ));
            }
        }

        if violations.is_empty() {
            Ok(())
        } else {
            Err(violations)
        }
    }

    pub fn compile(
        generation: u64,
        cohort: RouteCohort,
        candidates: Vec<InteractiveRouteCandidate>,
        node_catalog: &[RelayNode],
        overlay: &BackboneSessionOverlay,
    ) -> Result<Self, Vec<PolicyViolation>> {
        let mut violations = Vec::new();
        if generation == 0 {
            violations.push(PolicyViolation::new(
                "route_generation_zero",
                "interactive route generations start at one",
            ));
        }
        if cohort.delivery_class != DeliveryClass::Interactive {
            violations.push(PolicyViolation::new(
                "interactive_delivery_class_required",
                "the fast-path compiler accepts interactive cohorts",
            ));
        }
        if let Err(mut overlay_violations) = overlay.validate(node_catalog) {
            violations.append(&mut overlay_violations);
        }

        let mut candidate_ids = HashSet::with_capacity(candidates.len());
        for candidate in &candidates {
            if !candidate_ids.insert(candidate.candidate_id.as_str()) {
                violations.push(PolicyViolation::new(
                    "duplicate_route_candidate_id",
                    format!(
                        "interactive candidate {} appears more than once",
                        candidate.candidate_id
                    ),
                ));
            }
            if let Err(mut shape_violations) = candidate.validate_shape(node_catalog, overlay) {
                violations.append(&mut shape_violations);
            }
        }
        if !violations.is_empty() {
            return Err(violations);
        }

        let policy = LatencyPolicy::for_class(DeliveryClass::Interactive);
        let mut eligible = candidates
            .into_iter()
            .filter(|candidate| policy.validate_measurement(&candidate.measurement).is_ok())
            .collect::<Vec<_>>();
        eligible.sort_by(|left, right| {
            left.measurement
                .deadline_miss_ppm
                .cmp(&right.measurement.deadline_miss_ppm)
                .then_with(|| {
                    left.measurement
                        .selected_path_rtt_us
                        .cmp(&right.measurement.selected_path_rtt_us)
                })
                .then_with(|| {
                    left.measurement
                        .jitter_p95_us
                        .cmp(&right.measurement.jitter_p95_us)
                })
                .then_with(|| left.measurement.loss_ppm.cmp(&right.measurement.loss_ppm))
                .then_with(|| left.candidate_id.cmp(&right.candidate_id))
        });

        let Some(primary) = eligible.first().cloned() else {
            return Err(vec![PolicyViolation::new(
                "no_eligible_interactive_route",
                "no measured route satisfies the interactive latency limits",
            )]);
        };
        let secondary = eligible
            .into_iter()
            .skip(1)
            .find(|candidate| {
                routes_share_endpoints(&primary, candidate)
                    && routes_have_independent_parents(&primary, candidate, node_catalog)
            })
            .ok_or_else(|| {
                vec![PolicyViolation::new(
                    "no_independent_interactive_secondary",
                    "the interactive route requires an eligible warm secondary in an independent failure domain",
                )]
            })?;

        Ok(Self {
            generation,
            cohort,
            primary,
            secondary,
            pending_primary: None,
        })
    }

    /// Returns the media forwarding links selected for this cohort. The
    /// [`BackboneSessionOverlay`] remains the carrier-connectivity layer.
    pub fn forwarding_links(&self) -> Vec<ParentLink> {
        let mut links = Vec::with_capacity(4);
        append_candidate_links(&mut links, &self.primary, ParentRole::Primary);
        append_candidate_links(&mut links, &self.secondary, ParentRole::Secondary);
        links
    }

    pub fn begin_make_before_break(
        &mut self,
        desired_generation: u64,
        candidate: InteractiveRouteCandidate,
        node_catalog: &[RelayNode],
        overlay: &BackboneSessionOverlay,
    ) -> Result<(), Vec<PolicyViolation>> {
        let mut violations = Vec::new();
        if desired_generation <= self.generation {
            violations.push(PolicyViolation::new(
                "route_generation_not_newer",
                format!(
                    "desired generation {desired_generation} must follow active generation {}",
                    self.generation
                ),
            ));
        }
        if self.pending_primary.is_some() {
            violations.push(PolicyViolation::new(
                "route_transition_in_progress",
                "the active route already has a pending primary",
            ));
        }
        if candidate.candidate_id == self.primary.candidate_id
            || candidate.candidate_id == self.secondary.candidate_id
        {
            violations.push(PolicyViolation::new(
                "pending_route_candidate_already_active",
                format!(
                    "candidate {} already has an active route role",
                    candidate.candidate_id
                ),
            ));
        }
        if let Err(mut overlay_violations) = overlay.validate(node_catalog) {
            violations.append(&mut overlay_violations);
        }
        if let Err(mut shape_violations) = candidate.validate_shape(node_catalog, overlay) {
            violations.append(&mut shape_violations);
        }
        if let Err(mut measurement_violations) =
            LatencyPolicy::for_class(DeliveryClass::Interactive)
                .validate_measurement(&candidate.measurement)
        {
            violations.append(&mut measurement_violations);
        }
        if !routes_share_endpoints(&self.primary, &candidate) {
            violations.push(PolicyViolation::new(
                "route_endpoint_mismatch",
                "a pending primary must serve the active ingress and playback edge",
            ));
        }
        if !routes_have_independent_parents(&self.primary, &candidate, node_catalog) {
            violations.push(PolicyViolation::new(
                "pending_primary_failure_domain_overlap",
                "the pending primary and retiring primary require independent failure domains",
            ));
        }
        if violations.is_empty() {
            self.pending_primary = Some(PendingInteractivePrimary {
                desired_generation,
                candidate,
                readiness: PendingRouteReadiness::Establishing,
            });
            Ok(())
        } else {
            Err(violations)
        }
    }

    pub fn mark_pending_warm(&mut self, candidate_id: &str) -> Result<(), PolicyViolation> {
        let Some(pending) = self.pending_primary.as_mut() else {
            return Err(PolicyViolation::new(
                "pending_primary_absent",
                "a pending primary must be established before it becomes warm",
            ));
        };
        if pending.candidate.candidate_id != candidate_id {
            return Err(PolicyViolation::new(
                "pending_primary_identity_mismatch",
                format!(
                    "pending primary {} does not match acknowledgement {candidate_id}",
                    pending.candidate.candidate_id
                ),
            ));
        }
        pending.readiness = PendingRouteReadiness::Warm;
        Ok(())
    }

    pub fn activate_warm_pending(&mut self) -> Result<(), PolicyViolation> {
        let Some(pending) = self.pending_primary.take() else {
            return Err(PolicyViolation::new(
                "pending_primary_absent",
                "a pending primary must be warm before activation",
            ));
        };
        if pending.readiness != PendingRouteReadiness::Warm {
            self.pending_primary = Some(pending);
            return Err(PolicyViolation::new(
                "pending_primary_not_warm",
                "the current primary remains active until its replacement is warm",
            ));
        }

        let retiring_primary = std::mem::replace(&mut self.primary, pending.candidate);
        self.secondary = retiring_primary;
        self.generation = pending.desired_generation;
        Ok(())
    }

    pub fn promote_warm_secondary(
        &mut self,
        desired_generation: u64,
    ) -> Result<(), PolicyViolation> {
        if desired_generation <= self.generation {
            return Err(PolicyViolation::new(
                "route_generation_not_newer",
                format!(
                    "desired generation {desired_generation} must follow active generation {}",
                    self.generation
                ),
            ));
        }
        if self.pending_primary.is_some() {
            return Err(PolicyViolation::new(
                "route_transition_in_progress",
                "finish the pending-primary transition before promoting the warm secondary",
            ));
        }
        std::mem::swap(&mut self.primary, &mut self.secondary);
        self.generation = desired_generation;
        Ok(())
    }
}

fn routes_share_endpoints(
    left: &InteractiveRouteCandidate,
    right: &InteractiveRouteCandidate,
) -> bool {
    left.path.ingress_node_id() == right.path.ingress_node_id()
        && left.path.edge_node_id() == right.path.edge_node_id()
}

fn routes_have_independent_parents(
    left: &InteractiveRouteCandidate,
    right: &InteractiveRouteCandidate,
    node_catalog: &[RelayNode],
) -> bool {
    let left_parent_id = left.path.edge_parent_node_id();
    let right_parent_id = right.path.edge_parent_node_id();
    if left_parent_id == right_parent_id {
        return false;
    }
    let catalog = node_catalog
        .iter()
        .map(|node| (node.node_id.as_str(), node))
        .collect::<HashMap<_, _>>();
    match (catalog.get(left_parent_id), catalog.get(right_parent_id)) {
        (Some(left_parent), Some(right_parent)) => left_parent.failure_domain.is_independent_from(
            &right_parent.failure_domain,
            FailureDiversityRequirement::ProviderRegionAsnAndZone,
        ),
        _ => false,
    }
}

fn append_candidate_links(
    links: &mut Vec<ParentLink>,
    candidate: &InteractiveRouteCandidate,
    edge_role: ParentRole,
) {
    let (ingress_node_id, backbone_node_id, edge_node_id) = match &candidate.path {
        InteractiveRoutePath::Direct {
            ingress_node_id,
            edge_node_id,
        } => (ingress_node_id, None, edge_node_id),
        InteractiveRoutePath::OneBackboneHop {
            ingress_node_id,
            backbone_node_id,
            edge_node_id,
        } => (ingress_node_id, Some(backbone_node_id), edge_node_id),
    };
    if let Some(backbone_node_id) = backbone_node_id {
        let backbone_link = ParentLink {
            parent_node_id: ingress_node_id.clone(),
            child_node_id: backbone_node_id.clone(),
            role: ParentRole::Primary,
        };
        if !links.contains(&backbone_link) {
            links.push(backbone_link);
        }
    }
    links.push(ParentLink {
        parent_node_id: backbone_node_id
            .cloned()
            .unwrap_or_else(|| ingress_node_id.clone()),
        child_node_id: edge_node_id.clone(),
        role: edge_role,
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(id: &str, level: u16, role: NodeRole, domain: u32) -> RelayNode {
        RelayNode {
            node_id: id.to_owned(),
            level,
            role,
            failure_domain: FailureDomain {
                provider: format!("provider-{domain}"),
                region: format!("region-{domain}"),
                asn: 64_500 + domain,
                zone: format!("zone-{domain}"),
            },
        }
    }

    fn valid_topology() -> RelayTopology {
        RelayTopology {
            generation: 7,
            nodes: vec![
                node("origin", 0, NodeRole::Origin, 0),
                node("backbone-a", 1, NodeRole::Backbone, 1),
                node("backbone-b", 1, NodeRole::Backbone, 2),
                node("edge", 2, NodeRole::PlaybackEdge, 3),
            ],
            parent_links: vec![
                ParentLink {
                    parent_node_id: "origin".into(),
                    child_node_id: "backbone-a".into(),
                    role: ParentRole::Primary,
                },
                ParentLink {
                    parent_node_id: "origin".into(),
                    child_node_id: "backbone-b".into(),
                    role: ParentRole::Primary,
                },
                ParentLink {
                    parent_node_id: "backbone-a".into(),
                    child_node_id: "edge".into(),
                    role: ParentRole::Primary,
                },
                ParentLink {
                    parent_node_id: "backbone-b".into(),
                    child_node_id: "edge".into(),
                    role: ParentRole::Secondary,
                },
            ],
            limits: TopologyLimits::default(),
        }
    }

    fn codes(topology: &RelayTopology) -> HashSet<String> {
        topology
            .validate()
            .expect_err("topology should be rejected")
            .into_iter()
            .map(|violation| violation.code)
            .collect()
    }

    fn interactive_catalog() -> Vec<RelayNode> {
        vec![
            node("origin", 0, NodeRole::Origin, 0),
            node("backbone-a", 1, NodeRole::Backbone, 1),
            node("backbone-b", 1, NodeRole::Backbone, 2),
            node("backbone-c", 1, NodeRole::Backbone, 3),
            node("edge", 2, NodeRole::PlaybackEdge, 4),
        ]
    }

    fn backbone_overlay() -> BackboneSessionOverlay {
        BackboneSessionOverlay {
            generation: 9,
            trusted_node_ids: vec![
                "backbone-a".into(),
                "backbone-b".into(),
                "backbone-c".into(),
            ],
            session_links: vec![
                BackboneSessionLink {
                    left_node_id: "backbone-a".into(),
                    right_node_id: "backbone-b".into(),
                },
                BackboneSessionLink {
                    left_node_id: "backbone-b".into(),
                    right_node_id: "backbone-c".into(),
                },
                BackboneSessionLink {
                    left_node_id: "backbone-c".into(),
                    right_node_id: "backbone-a".into(),
                },
            ],
        }
    }

    fn route_measurement(selected_path_rtt_us: u64, relay_hops: u8) -> RouteMeasurement {
        RouteMeasurement {
            best_direct_rtt_us: 18_000,
            selected_path_rtt_us,
            inter_region_relay_hops: relay_hops,
            relay_processing_p95_us: if relay_hops == 0 { 0 } else { 700 },
            media_queue_p95_us: 2_500,
            jitter_p95_us: 800,
            loss_ppm: 50,
            deadline_miss_ppm: 0,
        }
    }

    fn direct_candidate(id: &str, rtt_us: u64) -> InteractiveRouteCandidate {
        InteractiveRouteCandidate {
            candidate_id: id.into(),
            path: InteractiveRoutePath::Direct {
                ingress_node_id: "origin".into(),
                edge_node_id: "edge".into(),
            },
            measurement: route_measurement(rtt_us, 0),
        }
    }

    fn backbone_candidate(
        id: &str,
        backbone_node_id: &str,
        rtt_us: u64,
    ) -> InteractiveRouteCandidate {
        InteractiveRouteCandidate {
            candidate_id: id.into(),
            path: InteractiveRoutePath::OneBackboneHop {
                ingress_node_id: "origin".into(),
                backbone_node_id: backbone_node_id.into(),
                edge_node_id: "edge".into(),
            },
            measurement: route_measurement(rtt_us, 1),
        }
    }

    fn interactive_cohort() -> RouteCohort {
        RouteCohort {
            stream_id: "concert/main".into(),
            destination_region: "region-4".into(),
            destination_asn: 64_504,
            rendition: "1080p".into(),
            delivery_class: DeliveryClass::Interactive,
        }
    }

    fn error_codes(result: Result<(), Vec<PolicyViolation>>) -> HashSet<String> {
        result
            .expect_err("policy should be rejected")
            .into_iter()
            .map(|violation| violation.code)
            .collect()
    }

    #[test]
    fn accepts_dual_parent_acyclic_topology() {
        valid_topology().validate().unwrap();
    }

    #[test]
    fn rejects_forwarding_to_an_earlier_level() {
        let mut topology = valid_topology();
        topology.parent_links[2].parent_node_id = "edge".into();
        topology.parent_links[2].child_node_id = "backbone-a".into();
        assert!(codes(&topology).contains("non_acyclic_level"));
    }

    #[test]
    fn rejects_a_third_parent() {
        let mut topology = valid_topology();
        topology
            .nodes
            .push(node("backbone-c", 1, NodeRole::Backbone, 4));
        topology.parent_links.push(ParentLink {
            parent_node_id: "origin".into(),
            child_node_id: "backbone-c".into(),
            role: ParentRole::Primary,
        });
        topology.parent_links.push(ParentLink {
            parent_node_id: "backbone-c".into(),
            child_node_id: "edge".into(),
            role: ParentRole::Secondary,
        });
        assert!(codes(&topology).contains("too_many_parents"));
    }

    #[test]
    fn rejects_overlapping_parent_failure_domains() {
        let mut topology = valid_topology();
        topology.nodes[2].failure_domain = topology.nodes[1].failure_domain.clone();
        assert!(codes(&topology).contains("parent_failure_domain_overlap"));
    }

    #[test]
    fn rejects_origin_fanout_over_limit() {
        let mut topology = valid_topology();
        topology.limits.max_origin_children = 1;
        assert!(codes(&topology).contains("downstream_fanout_exceeded"));
    }

    #[test]
    fn interactive_policy_accepts_a_tight_one_hop_route() {
        let policy = LatencyPolicy::for_class(DeliveryClass::Interactive);
        policy
            .validate_measurement(&RouteMeasurement {
                best_direct_rtt_us: 100_000,
                selected_path_rtt_us: 110_000,
                inter_region_relay_hops: 1,
                relay_processing_p95_us: 800,
                media_queue_p95_us: 2_500,
                jitter_p95_us: 1_000,
                loss_ppm: 50,
                deadline_miss_ppm: 0,
            })
            .unwrap();
    }

    #[test]
    fn interactive_policy_rejects_slow_or_deep_routes() {
        let policy = LatencyPolicy::for_class(DeliveryClass::Interactive);
        let violations = policy
            .validate_measurement(&RouteMeasurement {
                best_direct_rtt_us: 100_000,
                selected_path_rtt_us: 130_000,
                inter_region_relay_hops: 2,
                relay_processing_p95_us: 1_200,
                media_queue_p95_us: 6_000,
                jitter_p95_us: 1_000,
                loss_ppm: 50,
                deadline_miss_ppm: 10,
            })
            .unwrap_err()
            .into_iter()
            .map(|violation| violation.code)
            .collect::<HashSet<_>>();
        assert!(violations.contains("path_stretch_exceeded"));
        assert!(violations.contains("relay_hop_limit_exceeded"));
        assert!(violations.contains("relay_processing_budget_exceeded"));
        assert!(violations.contains("media_queue_budget_exceeded"));
    }

    #[test]
    fn interactive_policy_enforces_deadline_loss_and_jitter_quality() {
        let policy = LatencyPolicy::for_class(DeliveryClass::Interactive);
        let violations = policy
            .validate_measurement(&RouteMeasurement {
                best_direct_rtt_us: 100_000,
                selected_path_rtt_us: 105_000,
                inter_region_relay_hops: 1,
                relay_processing_p95_us: 800,
                media_queue_p95_us: 2_500,
                jitter_p95_us: policy.max_jitter_p95_us + 1,
                loss_ppm: policy.max_loss_ppm + 1,
                deadline_miss_ppm: policy.max_deadline_miss_ppm + 1,
            })
            .unwrap_err()
            .into_iter()
            .map(|violation| violation.code)
            .collect::<HashSet<_>>();
        assert!(violations.contains("jitter_budget_exceeded"));
        assert!(violations.contains("loss_budget_exceeded"));
        assert!(violations.contains("deadline_miss_budget_exceeded"));
    }

    #[test]
    fn accepts_a_three_node_resilient_backbone_session_overlay() {
        backbone_overlay().validate(&interactive_catalog()).unwrap();
    }

    #[test]
    fn backbone_overlay_enforces_size_resilience_and_failure_domains() {
        let catalog = interactive_catalog();
        let mut too_small = backbone_overlay();
        too_small.trusted_node_ids.pop();
        too_small.session_links.truncate(1);
        assert!(error_codes(too_small.validate(&catalog)).contains("backbone_node_count"));

        let mut sparse = backbone_overlay();
        sparse.session_links.pop();
        assert!(error_codes(sparse.validate(&catalog)).contains("backbone_session_resilience"));

        let mut overlapping_catalog = catalog;
        overlapping_catalog[2].failure_domain = overlapping_catalog[1].failure_domain.clone();
        assert!(
            error_codes(backbone_overlay().validate(&overlapping_catalog))
                .contains("backbone_failure_domain_overlap")
        );
    }

    #[test]
    fn accepts_a_five_node_near_complete_backbone_overlay() {
        let mut catalog = interactive_catalog();
        catalog.insert(4, node("backbone-d", 1, NodeRole::Backbone, 5));
        catalog.insert(5, node("backbone-e", 1, NodeRole::Backbone, 6));
        let node_ids = [
            "backbone-a",
            "backbone-b",
            "backbone-c",
            "backbone-d",
            "backbone-e",
        ];
        let overlay = BackboneSessionOverlay {
            generation: 10,
            trusted_node_ids: node_ids.iter().map(|node_id| (*node_id).into()).collect(),
            session_links: (0..node_ids.len())
                .flat_map(|left| ((left + 1)..node_ids.len()).map(move |right| (left, right)))
                .filter(|&(left, right)| (left, right) != (0, 1))
                .map(|(left, right)| BackboneSessionLink {
                    left_node_id: node_ids[left].into(),
                    right_node_id: node_ids[right].into(),
                })
                .collect(),
        };
        overlay.validate(&catalog).unwrap();
    }

    #[test]
    fn compiles_direct_primary_and_one_hop_warm_secondary() {
        let catalog = interactive_catalog();
        let overlay = backbone_overlay();
        let slow_path = backbone_candidate("via-b", "backbone-b", 25_000);
        let selection = InteractiveRouteSelection::compile(
            11,
            interactive_cohort(),
            vec![
                slow_path,
                backbone_candidate("via-a", "backbone-a", 20_000),
                direct_candidate("direct", 18_000),
            ],
            &catalog,
            &overlay,
        )
        .unwrap();

        assert_eq!(selection.primary.candidate_id, "direct");
        assert_eq!(selection.secondary.candidate_id, "via-a");
        assert!(selection.pending_primary.is_none());
        selection.validate(&catalog, &overlay).unwrap();

        let forwarding_links = selection.forwarding_links();
        assert_eq!(forwarding_links.len(), 3);
        assert!(!forwarding_links.iter().any(|link| {
            link.parent_node_id == "backbone-b" && link.child_node_id == "backbone-c"
        }));
        RelayTopology {
            generation: selection.generation,
            nodes: catalog
                .into_iter()
                .filter(|node| matches!(node.node_id.as_str(), "origin" | "backbone-a" | "edge"))
                .collect(),
            parent_links: forwarding_links,
            limits: TopologyLimits::default(),
        }
        .validate()
        .unwrap();
    }

    #[test]
    fn compiler_requires_an_independent_warm_secondary() {
        let error = InteractiveRouteSelection::compile(
            11,
            interactive_cohort(),
            vec![
                direct_candidate("direct-a", 18_000),
                direct_candidate("direct-b", 18_100),
            ],
            &interactive_catalog(),
            &backbone_overlay(),
        )
        .unwrap_err();
        assert!(error
            .into_iter()
            .any(|violation| violation.code == "no_independent_interactive_secondary"));
    }

    #[test]
    fn compiler_prefers_deadline_success_before_marginal_rtt_gain() {
        let catalog = interactive_catalog();
        let overlay = backbone_overlay();
        let mut marginally_faster = backbone_candidate("fast-with-misses", "backbone-a", 17_500);
        marginally_faster.measurement.deadline_miss_ppm = 900;
        let selection = InteractiveRouteSelection::compile(
            11,
            interactive_cohort(),
            vec![
                marginally_faster,
                direct_candidate("deadline-clean", 18_000),
                backbone_candidate("warm-clean", "backbone-b", 20_000),
            ],
            &catalog,
            &overlay,
        )
        .unwrap();

        assert_eq!(selection.primary.candidate_id, "deadline-clean");
        assert_eq!(selection.secondary.candidate_id, "warm-clean");
    }

    #[test]
    fn make_before_break_keeps_the_primary_until_replacement_is_warm() {
        let catalog = interactive_catalog();
        let overlay = backbone_overlay();
        let mut selection = InteractiveRouteSelection::compile(
            11,
            interactive_cohort(),
            vec![
                direct_candidate("direct", 18_000),
                backbone_candidate("via-a", "backbone-a", 20_000),
            ],
            &catalog,
            &overlay,
        )
        .unwrap();
        let active_links = selection.forwarding_links();

        selection
            .begin_make_before_break(
                12,
                backbone_candidate("via-b", "backbone-b", 19_000),
                &catalog,
                &overlay,
            )
            .unwrap();
        selection.validate(&catalog, &overlay).unwrap();
        assert_eq!(selection.primary.candidate_id, "direct");
        assert_eq!(selection.forwarding_links(), active_links);
        assert_eq!(
            selection.activate_warm_pending().unwrap_err().code,
            "pending_primary_not_warm"
        );
        assert_eq!(selection.primary.candidate_id, "direct");

        selection.mark_pending_warm("via-b").unwrap();
        selection.activate_warm_pending().unwrap();
        assert_eq!(selection.generation, 12);
        assert_eq!(selection.primary.candidate_id, "via-b");
        assert_eq!(selection.secondary.candidate_id, "direct");
        assert!(selection.pending_primary.is_none());
        selection.validate(&catalog, &overlay).unwrap();

        RelayTopology {
            generation: selection.generation,
            nodes: catalog
                .into_iter()
                .filter(|node| matches!(node.node_id.as_str(), "origin" | "backbone-b" | "edge"))
                .collect(),
            parent_links: selection.forwarding_links(),
            limits: TopologyLimits::default(),
        }
        .validate()
        .unwrap();
    }
}
