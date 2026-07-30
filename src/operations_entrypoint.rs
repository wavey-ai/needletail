//! Fail-closed discovery for the elected Needletail Operations collector.
//!
//! The durable controller writes one assignment after its quorum commits the
//! collector term and fencing generation. The HTTP entry point reads that
//! assignment. It redirects only while the assignment has a valid quorum lease
//! and its endpoint matches the operator allowlist.

use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::fmt;
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, PermissionsExt};

pub const OPERATIONS_WELL_KNOWN_PATH: &str = "/.well-known/needletail-operations";
pub const MAX_ASSIGNMENT_BYTES: usize = 16 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OperationsCollectorAssignment {
    pub schema_version: u16,
    pub authority: String,
    pub term: u64,
    pub fencing_generation: u64,
    pub leader_node_id: String,
    pub leader_region: String,
    pub quorum_healthy: bool,
    pub voters_online: u16,
    pub voters_total: u16,
    pub committed_at_unix_ms: u64,
    pub lease_expires_unix_ms: u64,
    pub public_endpoint: String,
}

#[derive(Clone, Debug)]
pub struct OperationsEntrypointPolicy {
    expected_authority: String,
    entrypoint_host: String,
    allowed_endpoints: BTreeSet<String>,
    lease_safety_margin_ms: u64,
    max_clock_skew_ms: u64,
    max_lease_duration_ms: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct OperationsEndpoint {
    url: String,
    host: String,
    default_tls_port: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OperationsAssignmentError {
    InvalidPolicy(&'static str),
    Unavailable,
    InvalidSchema,
    WrongAuthority,
    InvalidLeader,
    InvalidQuorum,
    InvalidTerm,
    InvalidCommitTime,
    InvalidLease,
    ExpiredLease,
    InvalidEndpoint,
    EndpointNotAllowed,
    RedirectLoop,
}

impl fmt::Display for OperationsAssignmentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::InvalidPolicy(message) => message,
            Self::Unavailable => "the controller assignment is unavailable",
            Self::InvalidSchema => "the controller assignment schema is not supported",
            Self::WrongAuthority => "the controller assignment has the wrong authority",
            Self::InvalidLeader => "the controller assignment has an invalid leader identity",
            Self::InvalidQuorum => "the controller assignment does not prove a healthy quorum",
            Self::InvalidTerm => "the controller assignment has an invalid term or generation",
            Self::InvalidCommitTime => "the controller assignment has an invalid commit time",
            Self::InvalidLease => "the controller assignment has an invalid lease",
            Self::ExpiredLease => "the controller assignment lease is not safe to use",
            Self::InvalidEndpoint => "the controller assignment endpoint is invalid",
            Self::EndpointNotAllowed => {
                "the controller assignment endpoint is not in the allowlist"
            }
            Self::RedirectLoop => "the controller assignment points to the global entry point",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for OperationsAssignmentError {}

impl OperationsEntrypointPolicy {
    pub fn new(
        expected_authority: String,
        entrypoint_host: String,
        allowed_endpoints: Vec<String>,
        lease_safety_margin_ms: u64,
        max_clock_skew_ms: u64,
        max_lease_duration_ms: u64,
    ) -> Result<Self, OperationsAssignmentError> {
        if !valid_identifier(&expected_authority) {
            return Err(OperationsAssignmentError::InvalidPolicy(
                "the expected authority is invalid",
            ));
        }
        let entrypoint_host = canonical_hostname(&entrypoint_host).ok_or(
            OperationsAssignmentError::InvalidPolicy("the entry point host is invalid"),
        )?;
        if allowed_endpoints.is_empty() {
            return Err(OperationsAssignmentError::InvalidPolicy(
                "the endpoint allowlist is empty",
            ));
        }
        if max_lease_duration_ms == 0
            || lease_safety_margin_ms >= max_lease_duration_ms
            || lease_safety_margin_ms < max_clock_skew_ms
        {
            return Err(OperationsAssignmentError::InvalidPolicy(
                "the lease timing policy is invalid",
            ));
        }

        let mut canonical_endpoints = BTreeSet::new();
        for configured in allowed_endpoints {
            let endpoint = parse_operations_endpoint(&configured).ok_or(
                OperationsAssignmentError::InvalidPolicy("an endpoint allowlist value is invalid"),
            )?;
            if endpoint.host == entrypoint_host && endpoint.default_tls_port {
                return Err(OperationsAssignmentError::InvalidPolicy(
                    "an allowed endpoint points to the global entry point",
                ));
            }
            canonical_endpoints.insert(endpoint.url);
        }

        Ok(Self {
            expected_authority,
            entrypoint_host,
            allowed_endpoints: canonical_endpoints,
            lease_safety_margin_ms,
            max_clock_skew_ms,
            max_lease_duration_ms,
        })
    }

    pub fn resolve(
        &self,
        assignment: &OperationsCollectorAssignment,
        now_unix_ms: u64,
    ) -> Result<String, OperationsAssignmentError> {
        if assignment.schema_version != 1 {
            return Err(OperationsAssignmentError::InvalidSchema);
        }
        if assignment.authority != self.expected_authority {
            return Err(OperationsAssignmentError::WrongAuthority);
        }
        if !valid_identifier(&assignment.leader_node_id)
            || !valid_identifier(&assignment.leader_region)
        {
            return Err(OperationsAssignmentError::InvalidLeader);
        }
        if assignment.term == 0 || assignment.fencing_generation == 0 {
            return Err(OperationsAssignmentError::InvalidTerm);
        }
        if !assignment.quorum_healthy
            || assignment.voters_total < 3
            || assignment.voters_total.is_multiple_of(2)
            || assignment.voters_online > assignment.voters_total
            || assignment.voters_online <= assignment.voters_total / 2
        {
            return Err(OperationsAssignmentError::InvalidQuorum);
        }
        if assignment.committed_at_unix_ms > now_unix_ms.saturating_add(self.max_clock_skew_ms) {
            return Err(OperationsAssignmentError::InvalidCommitTime);
        }
        let lease_duration = assignment
            .lease_expires_unix_ms
            .checked_sub(assignment.committed_at_unix_ms)
            .ok_or(OperationsAssignmentError::InvalidLease)?;
        if lease_duration == 0 || lease_duration > self.max_lease_duration_ms {
            return Err(OperationsAssignmentError::InvalidLease);
        }
        if assignment.lease_expires_unix_ms
            <= now_unix_ms.saturating_add(self.lease_safety_margin_ms)
        {
            return Err(OperationsAssignmentError::ExpiredLease);
        }

        let requested = parse_operations_endpoint(&assignment.public_endpoint)
            .ok_or(OperationsAssignmentError::InvalidEndpoint)?;
        if requested.host == self.entrypoint_host && requested.default_tls_port {
            return Err(OperationsAssignmentError::RedirectLoop);
        }
        if !self.allowed_endpoints.contains(&requested.url) {
            return Err(OperationsAssignmentError::EndpointNotAllowed);
        }
        Ok(requested.url)
    }

    pub fn resolve_file(
        &self,
        assignment_path: &Path,
        now_unix_ms: u64,
    ) -> Result<String, OperationsAssignmentError> {
        let parent = assignment_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .ok_or(OperationsAssignmentError::Unavailable)?;
        let parent_metadata =
            fs::symlink_metadata(parent).map_err(|_| OperationsAssignmentError::Unavailable)?;
        if !parent_metadata.file_type().is_dir() || parent_metadata.file_type().is_symlink() {
            return Err(OperationsAssignmentError::Unavailable);
        }
        #[cfg(unix)]
        if parent_metadata.permissions().mode() & 0o022 != 0 {
            return Err(OperationsAssignmentError::Unavailable);
        }
        let path_metadata = fs::symlink_metadata(assignment_path)
            .map_err(|_| OperationsAssignmentError::Unavailable)?;
        if !path_metadata.file_type().is_file() || path_metadata.file_type().is_symlink() {
            return Err(OperationsAssignmentError::Unavailable);
        }
        #[cfg(unix)]
        if path_metadata.permissions().mode() & 0o022 != 0 {
            return Err(OperationsAssignmentError::Unavailable);
        }
        let file =
            File::open(assignment_path).map_err(|_| OperationsAssignmentError::Unavailable)?;
        #[cfg(unix)]
        {
            let opened_metadata = file
                .metadata()
                .map_err(|_| OperationsAssignmentError::Unavailable)?;
            if path_metadata.dev() != opened_metadata.dev()
                || path_metadata.ino() != opened_metadata.ino()
            {
                return Err(OperationsAssignmentError::Unavailable);
            }
        }
        let mut bytes = Vec::with_capacity(MAX_ASSIGNMENT_BYTES.min(4096));
        file.take((MAX_ASSIGNMENT_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| OperationsAssignmentError::Unavailable)?;
        if bytes.len() > MAX_ASSIGNMENT_BYTES {
            return Err(OperationsAssignmentError::Unavailable);
        }
        let assignment: OperationsCollectorAssignment =
            serde_json::from_slice(&bytes).map_err(|_| OperationsAssignmentError::Unavailable)?;
        self.resolve(&assignment, now_unix_ms)
    }
}

fn parse_operations_endpoint(input: &str) -> Option<OperationsEndpoint> {
    if input.is_empty()
        || input
            .bytes()
            .any(|byte| byte.is_ascii_control() || byte.is_ascii_whitespace())
    {
        return None;
    }
    let remainder = input.strip_prefix("https://")?;
    if remainder.contains(['?', '#', '\\', '@']) {
        return None;
    }
    let (authority, path) = remainder.split_once('/')?;
    if path != "mesh" || authority.is_empty() {
        return None;
    }
    if authority.matches(':').count() > 1 {
        return None;
    }
    let (host, port) = match authority.split_once(':') {
        Some((host, port)) => {
            let port = port.parse::<u16>().ok()?;
            if port == 0 {
                return None;
            }
            (host, Some(port))
        }
        None => (authority, None),
    };
    let host = canonical_hostname(host)?;
    let port_suffix = match port {
        Some(443) | None => String::new(),
        Some(port) => format!(":{port}"),
    };
    Some(OperationsEndpoint {
        url: format!("https://{host}{port_suffix}/mesh"),
        host,
        default_tls_port: matches!(port, Some(443) | None),
    })
}

fn canonical_hostname(input: &str) -> Option<String> {
    if input.is_empty() || input.len() > 253 || input.ends_with('.') {
        return None;
    }
    let canonical = input.to_ascii_lowercase();
    for label in canonical.split('.') {
        if label.is_empty()
            || label.len() > 63
            || label.starts_with('-')
            || label.ends_with('-')
            || !label
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        {
            return None;
        }
    }
    Some(canonical)
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: u64 = 2_000_000;

    fn policy() -> OperationsEntrypointPolicy {
        OperationsEntrypointPolicy::new(
            "needletail-controller".to_owned(),
            "ops.example.com".to_owned(),
            vec![
                "https://ops-eu.example.com/mesh".to_owned(),
                "https://ops-apac.example.com:8443/mesh".to_owned(),
            ],
            5_000,
            1_000,
            30_000,
        )
        .unwrap()
    }

    fn assignment() -> OperationsCollectorAssignment {
        OperationsCollectorAssignment {
            schema_version: 1,
            authority: "needletail-controller".to_owned(),
            term: 7,
            fencing_generation: 12,
            leader_node_id: "relay-primary-amsterdam".to_owned(),
            leader_region: "europe-west4".to_owned(),
            quorum_healthy: true,
            voters_online: 3,
            voters_total: 5,
            committed_at_unix_ms: NOW - 1_000,
            lease_expires_unix_ms: NOW + 10_000,
            public_endpoint: "https://ops-eu.example.com/mesh".to_owned(),
        }
    }

    #[test]
    fn resolves_a_quorum_committed_allowlisted_assignment() {
        assert_eq!(
            policy().resolve(&assignment(), NOW).unwrap(),
            "https://ops-eu.example.com/mesh"
        );
    }

    #[test]
    fn canonicalizes_default_tls_port_before_allowlist_comparison() {
        let mut assignment = assignment();
        assignment.public_endpoint = "https://OPS-EU.EXAMPLE.COM:443/mesh".to_owned();
        assert_eq!(
            policy().resolve(&assignment, NOW).unwrap(),
            "https://ops-eu.example.com/mesh"
        );
    }

    #[test]
    fn allows_a_regional_endpoint_on_a_nondefault_port_of_the_entrypoint_host() {
        let policy = OperationsEntrypointPolicy::new(
            "needletail-controller".to_owned(),
            "ops.example.com".to_owned(),
            vec!["https://ops.example.com:19444/mesh".to_owned()],
            5_000,
            1_000,
            30_000,
        )
        .unwrap();
        let mut assignment = assignment();
        assignment.public_endpoint = "https://ops.example.com:19444/mesh".to_owned();
        assert_eq!(
            policy.resolve(&assignment, NOW).unwrap(),
            "https://ops.example.com:19444/mesh"
        );
    }

    #[test]
    fn rejects_minority_even_when_the_assignment_claims_quorum_health() {
        let mut assignment = assignment();
        assignment.voters_online = 2;
        assert_eq!(
            policy().resolve(&assignment, NOW),
            Err(OperationsAssignmentError::InvalidQuorum)
        );
    }

    #[test]
    fn rejects_even_voter_sets() {
        let mut assignment = assignment();
        assignment.voters_total = 4;
        assignment.voters_online = 3;
        assert_eq!(
            policy().resolve(&assignment, NOW),
            Err(OperationsAssignmentError::InvalidQuorum)
        );
    }

    #[test]
    fn rejects_expired_and_nearly_expired_leases() {
        let mut assignment = assignment();
        assignment.lease_expires_unix_ms = NOW + 5_000;
        assert_eq!(
            policy().resolve(&assignment, NOW),
            Err(OperationsAssignmentError::ExpiredLease)
        );
    }

    #[test]
    fn rejects_unbounded_future_leases() {
        let mut assignment = assignment();
        assignment.lease_expires_unix_ms = assignment.committed_at_unix_ms + 30_001;
        assert_eq!(
            policy().resolve(&assignment, NOW),
            Err(OperationsAssignmentError::InvalidLease)
        );
    }

    #[test]
    fn rejects_uncommitted_future_state() {
        let mut assignment = assignment();
        assignment.committed_at_unix_ms = NOW + 1_001;
        assignment.lease_expires_unix_ms = assignment.committed_at_unix_ms + 10_000;
        assert_eq!(
            policy().resolve(&assignment, NOW),
            Err(OperationsAssignmentError::InvalidCommitTime)
        );
    }

    #[test]
    fn rejects_open_redirect_inputs() {
        for endpoint in [
            "http://ops-eu.example.com/mesh",
            "https://attacker.example/mesh",
            "https://ops-eu.example.com@attacker.example/mesh",
            "https://ops-eu.example.com/mesh?next=https://attacker.example",
            "https://ops-eu.example.com/mesh/../other",
        ] {
            let mut assignment = assignment();
            assignment.public_endpoint = endpoint.to_owned();
            assert!(policy().resolve(&assignment, NOW).is_err(), "{endpoint}");
        }
    }

    #[test]
    fn refuses_to_configure_a_redirect_loop() {
        assert_eq!(
            OperationsEntrypointPolicy::new(
                "needletail-controller".to_owned(),
                "ops.example.com".to_owned(),
                vec!["https://ops.example.com/mesh".to_owned()],
                5_000,
                1_000,
                30_000,
            )
            .unwrap_err(),
            OperationsAssignmentError::InvalidPolicy(
                "an allowed endpoint points to the global entry point"
            )
        );
    }

    #[test]
    fn strict_schema_rejects_unknown_assignment_fields() {
        let json = r#"{
            "schema_version": 1,
            "authority": "needletail-controller",
            "term": 7,
            "fencing_generation": 12,
            "leader_node_id": "relay-primary-amsterdam",
            "leader_region": "europe-west4",
            "quorum_healthy": true,
            "voters_online": 3,
            "voters_total": 5,
            "committed_at_unix_ms": 1999000,
            "lease_expires_unix_ms": 2010000,
            "public_endpoint": "https://ops-eu.example.com/mesh",
            "legacy_leader": true
        }"#;
        assert!(serde_json::from_str::<OperationsCollectorAssignment>(json).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn refuses_symlinked_and_writable_assignment_files() {
        use std::env;
        use std::fs;
        use std::os::unix::fs::{symlink, PermissionsExt};
        use std::process;
        use std::sync::atomic::{AtomicU64, Ordering};

        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        let directory = env::temp_dir().join(format!(
            "needletail-ops-entrypoint-{}-{}",
            process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let real = directory.join("real.json");
        fs::write(&real, serde_json::to_vec(&assignment()).unwrap()).unwrap();
        let linked = directory.join("linked.json");
        symlink(&real, &linked).unwrap();
        assert_eq!(
            policy().resolve_file(&linked, NOW),
            Err(OperationsAssignmentError::Unavailable)
        );

        fs::set_permissions(&real, fs::Permissions::from_mode(0o666)).unwrap();
        assert_eq!(
            policy().resolve_file(&real, NOW),
            Err(OperationsAssignmentError::Unavailable)
        );
        fs::remove_dir_all(directory).unwrap();
    }
}
