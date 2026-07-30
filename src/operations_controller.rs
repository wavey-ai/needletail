//! Shared records for the quorum-backed Operations controller and snapshot
//! assembler.
//!
//! The etcd election key is the authority. `OperationsControllerState` is only
//! a local, fail-closed projection of the current quorum-committed leader.

use crate::operations_entrypoint::OperationsCollectorAssignment;
use serde::{Deserialize, Serialize};
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::net::SocketAddr;
use std::path::Path;
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};

pub const CONTROLLER_AUTHORITY: &str = "needletail-controller";
pub const CONTROLLER_CANDIDATE_SCHEMA: &str = "needletail.operations-candidate.v1";
pub const CONTROLLER_STATE_SCHEMA: &str = "needletail.operations-controller-state.v1";
pub const OPERATIONS_SNAPSHOT_SCHEMA: &str = "needletail.operations-snapshot.v1";
pub const MAX_CONTROLLER_STATE_BYTES: usize = 32 * 1024;
pub const MAX_OPERATIONS_SNAPSHOT_BYTES: usize = 8 * 1024 * 1024;

static TEMP_FILE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OperationsCandidate {
    pub schema: String,
    pub node_id: String,
    pub region: String,
    pub public_endpoint: String,
    pub snapshot_endpoint: String,
    pub snapshot_address: SocketAddr,
}

impl OperationsCandidate {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema != CONTROLLER_CANDIDATE_SCHEMA {
            return Err("unsupported candidate schema");
        }
        if !valid_identifier(&self.node_id) || !valid_identifier(&self.region) {
            return Err("invalid candidate identity");
        }
        validate_https_path(&self.public_endpoint, "/mesh")?;
        validate_https_path(&self.snapshot_endpoint, "/api/mesh")?;
        if self.snapshot_address.ip().is_unspecified() || self.snapshot_address.port() == 0 {
            return Err("invalid candidate snapshot address");
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OperationsControllerState {
    pub schema: String,
    pub assignment: OperationsCollectorAssignment,
    pub snapshot_endpoint: String,
    pub snapshot_address: SocketAddr,
    pub leadership_acquired_unix_ms: u64,
    pub observed_at_unix_ms: u64,
}

impl OperationsControllerState {
    pub fn from_assignment(
        assignment: OperationsCollectorAssignment,
        candidate: &OperationsCandidate,
        leadership_acquired_unix_ms: u64,
        observed_at_unix_ms: u64,
    ) -> Result<Self, &'static str> {
        candidate.validate()?;
        if assignment.leader_node_id != candidate.node_id
            || assignment.leader_region != candidate.region
            || assignment.public_endpoint != candidate.public_endpoint
        {
            return Err("candidate does not match the committed assignment");
        }
        if leadership_acquired_unix_ms == 0
            || leadership_acquired_unix_ms > assignment.committed_at_unix_ms
        {
            return Err("invalid leadership acquisition time");
        }
        Ok(Self {
            schema: CONTROLLER_STATE_SCHEMA.to_owned(),
            assignment,
            snapshot_endpoint: candidate.snapshot_endpoint.clone(),
            snapshot_address: candidate.snapshot_address,
            leadership_acquired_unix_ms,
            observed_at_unix_ms,
        })
    }

    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema != CONTROLLER_STATE_SCHEMA {
            return Err("unsupported controller-state schema");
        }
        if !valid_identifier(&self.assignment.leader_node_id)
            || !valid_identifier(&self.assignment.leader_region)
        {
            return Err("invalid controller-state leader identity");
        }
        validate_https_path(&self.assignment.public_endpoint, "/mesh")?;
        validate_https_path(&self.snapshot_endpoint, "/api/mesh")?;
        if self.snapshot_address.ip().is_unspecified() || self.snapshot_address.port() == 0 {
            return Err("invalid controller-state snapshot address");
        }
        if self.leadership_acquired_unix_ms == 0
            || self.leadership_acquired_unix_ms > self.assignment.committed_at_unix_ms
        {
            return Err("invalid controller-state leadership acquisition time");
        }
        Ok(())
    }

    pub fn lease_is_current(&self, now_unix_ms: u64, safety_margin_ms: u64) -> bool {
        self.assignment.quorum_healthy
            && self.assignment.term > 0
            && self.assignment.fencing_generation > 0
            && self.assignment.voters_total >= 3
            && !self.assignment.voters_total.is_multiple_of(2)
            && self.assignment.voters_online > self.assignment.voters_total / 2
            && self.assignment.lease_expires_unix_ms > now_unix_ms.saturating_add(safety_margin_ms)
    }
}

pub fn read_controller_state(path: &Path) -> io::Result<OperationsControllerState> {
    let bytes = read_regular_file(path, MAX_CONTROLLER_STATE_BYTES)?;
    let state = serde_json::from_slice::<OperationsControllerState>(&bytes)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    state
        .validate()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    Ok(state)
}

pub fn write_controller_state(path: &Path, state: &OperationsControllerState) -> io::Result<()> {
    state
        .validate()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
    atomic_write_json(path, state, MAX_CONTROLLER_STATE_BYTES)
}

pub fn atomic_write_json<T: Serialize>(
    target: &Path,
    value: &T,
    max_bytes: usize,
) -> io::Result<()> {
    let parent = secure_parent(target)?;
    let file_name = target
        .file_name()
        .filter(|name| !name.is_empty())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid target file name"))?;
    let mut bytes = serde_json::to_vec(value)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    bytes.push(b'\n');
    if bytes.len() > max_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "serialized value exceeds its bounded file size",
        ));
    }

    let sequence = TEMP_FILE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let mut temporary_name = OsString::from(".");
    temporary_name.push(file_name);
    temporary_name.push(format!(".{}.{}.tmp", process::id(), sequence));
    let temporary_path = parent.join(temporary_name);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut temporary = options.open(&temporary_path)?;
    let result = (|| {
        temporary.write_all(&bytes)?;
        #[cfg(unix)]
        temporary.set_permissions(fs::Permissions::from_mode(0o644))?;
        temporary.sync_all()?;
        fs::rename(&temporary_path, target)?;
        File::open(parent)?.sync_all()
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary_path);
    }
    result
}

pub fn read_regular_file(path: &Path, max_bytes: usize) -> io::Result<Vec<u8>> {
    let parent = secure_parent(path)?;
    let path_metadata = fs::symlink_metadata(path)?;
    if !path_metadata.file_type().is_file() || path_metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "state path is not a regular file",
        ));
    }
    #[cfg(unix)]
    if path_metadata.permissions().mode() & 0o022 != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "state file is group- or world-writable",
        ));
    }
    let file = File::open(path)?;
    #[cfg(unix)]
    {
        let opened_metadata = file.metadata()?;
        if opened_metadata.dev() != path_metadata.dev()
            || opened_metadata.ino() != path_metadata.ino()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "state file changed while opening",
            ));
        }
    }
    let mut bytes = Vec::with_capacity(max_bytes.min(4096));
    file.take((max_bytes + 1) as u64).read_to_end(&mut bytes)?;
    if bytes.len() > max_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "state file exceeds its bounded size",
        ));
    }
    let _ = parent;
    Ok(bytes)
}

fn secure_parent(path: &Path) -> io::Result<&Path> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid state path"))?;
    let metadata = fs::symlink_metadata(parent)?;
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "state parent is not a regular directory",
        ));
    }
    #[cfg(unix)]
    if metadata.permissions().mode() & 0o022 != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "state parent is group- or world-writable",
        ));
    }
    Ok(parent)
}

fn validate_https_path(value: &str, expected_path: &str) -> Result<(), &'static str> {
    let remainder = value
        .strip_prefix("https://")
        .ok_or("endpoint must use HTTPS")?;
    if remainder
        .bytes()
        .any(|byte| byte.is_ascii_control() || byte.is_ascii_whitespace())
        || remainder.contains(['?', '#', '\\', '@'])
    {
        return Err("endpoint contains invalid characters");
    }
    let (authority, path) = remainder
        .split_once('/')
        .ok_or("endpoint path is missing")?;
    if authority.is_empty() || format!("/{path}") != expected_path {
        return Err("endpoint path is invalid");
    }
    Ok(())
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
    use std::time::{SystemTime, UNIX_EPOCH};

    fn assignment() -> OperationsCollectorAssignment {
        OperationsCollectorAssignment {
            schema_version: 1,
            authority: CONTROLLER_AUTHORITY.to_owned(),
            term: 11,
            fencing_generation: 11,
            leader_node_id: "edge-london".to_owned(),
            leader_region: "europe-west2".to_owned(),
            quorum_healthy: true,
            voters_online: 3,
            voters_total: 3,
            committed_at_unix_ms: 1_000,
            lease_expires_unix_ms: 31_000,
            public_endpoint: "https://ops-london.example.com/mesh".to_owned(),
        }
    }

    fn candidate() -> OperationsCandidate {
        OperationsCandidate {
            schema: CONTROLLER_CANDIDATE_SCHEMA.to_owned(),
            node_id: "edge-london".to_owned(),
            region: "europe-west2".to_owned(),
            public_endpoint: "https://ops-london.example.com/mesh".to_owned(),
            snapshot_endpoint: "https://ops-london.example.com:19444/api/mesh".to_owned(),
            snapshot_address: "192.0.2.10:19444".parse().unwrap(),
        }
    }

    #[test]
    fn controller_state_requires_matching_candidate_and_live_majority() {
        let state =
            OperationsControllerState::from_assignment(assignment(), &candidate(), 1_000, 1_100)
                .unwrap();
        assert!(state.lease_is_current(20_000, 5_000));
        assert!(!state.lease_is_current(27_000, 5_000));

        let mut wrong = candidate();
        wrong.node_id = "edge-tokyo".to_owned();
        assert!(
            OperationsControllerState::from_assignment(assignment(), &wrong, 1_000, 1_100).is_err()
        );
    }

    #[test]
    fn atomic_state_round_trip_rejects_writable_parent() {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!("needletail-controller-state-{suffix}"));
        fs::create_dir(&directory).unwrap();
        #[cfg(unix)]
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let path = directory.join("state.json");
        let state =
            OperationsControllerState::from_assignment(assignment(), &candidate(), 1_000, 1_100)
                .unwrap();
        write_controller_state(&path, &state).unwrap();
        assert_eq!(read_controller_state(&path).unwrap(), state);
        fs::remove_file(path).unwrap();
        fs::remove_dir(directory).unwrap();
    }
}
