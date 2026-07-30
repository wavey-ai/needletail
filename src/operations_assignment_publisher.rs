//! Atomic publication boundary for committed Operations collector assignments.
//!
//! This module does not elect a collector or grant a lease. A durable
//! controller may call it only after its consensus log commits the assignment.
//! Publication validates the browser-facing projection, rejects local
//! term/fence rollback, and atomically replaces the file consumed by
//! `needletail-ops-entrypoint`.

use crate::operations_entrypoint::{
    OperationsAssignmentError, OperationsCollectorAssignment, OperationsEntrypointPolicy,
    MAX_ASSIGNMENT_BYTES,
};
use std::ffi::OsString;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};

static TEMP_FILE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OperationsPublicationOutcome {
    Published,
    Unchanged,
}

#[derive(Debug)]
pub enum OperationsPublicationError {
    UnsafeAssignment(OperationsAssignmentError),
    InvalidTarget,
    ExistingStateUnavailable,
    TransitionRejected(&'static str),
    Serialization(serde_json::Error),
    Io(io::Error),
}

impl fmt::Display for OperationsPublicationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsafeAssignment(error) => {
                write!(
                    formatter,
                    "refusing to publish an unsafe assignment: {error}"
                )
            }
            Self::InvalidTarget => formatter.write_str("the assignment target path is invalid"),
            Self::ExistingStateUnavailable => formatter.write_str(
                "the existing assignment cannot be validated; refusing to erase its fence",
            ),
            Self::TransitionRejected(message) => formatter.write_str(message),
            Self::Serialization(error) => {
                write!(formatter, "failed to serialize the assignment: {error}")
            }
            Self::Io(error) => write!(formatter, "failed to publish the assignment: {error}"),
        }
    }
}

impl std::error::Error for OperationsPublicationError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::UnsafeAssignment(error) => Some(error),
            Self::Serialization(error) => Some(error),
            Self::Io(error) => Some(error),
            Self::InvalidTarget | Self::ExistingStateUnavailable | Self::TransitionRejected(_) => {
                None
            }
        }
    }
}

impl From<io::Error> for OperationsPublicationError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// Serializes access across publisher instances and processes on one host.
///
/// The caller remains responsible for ensuring there is exactly one durable
/// consensus authority. Direct writers that bypass this type are outside the
/// trust boundary.
pub struct OperationsAssignmentPublisher {
    assignment_path: PathBuf,
    policy: OperationsEntrypointPolicy,
    publication_lock: Mutex<()>,
}

impl OperationsAssignmentPublisher {
    pub fn new(assignment_path: PathBuf, policy: OperationsEntrypointPolicy) -> Self {
        Self {
            assignment_path,
            policy,
            publication_lock: Mutex::new(()),
        }
    }

    pub fn assignment_path(&self) -> &Path {
        &self.assignment_path
    }

    /// Publishes the browser-facing projection of a quorum-committed record.
    ///
    /// This method validates the lease at `now_unix_ms`; it does not create,
    /// renew, or independently prove that lease.
    pub fn publish_committed(
        &self,
        assignment: &OperationsCollectorAssignment,
        now_unix_ms: u64,
    ) -> Result<OperationsPublicationOutcome, OperationsPublicationError> {
        let _guard = self.publication_lock.lock().map_err(|_| {
            OperationsPublicationError::TransitionRejected("the local publication lock is poisoned")
        })?;
        let _file_guard = acquire_publication_lock(&self.assignment_path)?;

        let mut next = assignment.clone();
        next.public_endpoint = self
            .policy
            .resolve(&next, now_unix_ms)
            .map_err(OperationsPublicationError::UnsafeAssignment)?;

        if let Some(current) = read_existing_assignment(&self.assignment_path)? {
            match validate_transition(&current, &next)? {
                OperationsPublicationOutcome::Unchanged => {
                    return Ok(OperationsPublicationOutcome::Unchanged);
                }
                OperationsPublicationOutcome::Published => {}
            }
        }

        atomic_write_assignment(&self.assignment_path, &next)?;
        Ok(OperationsPublicationOutcome::Published)
    }

    /// Immediately fail-closes discovery while retaining the highest observed
    /// term and generation on disk.
    ///
    /// A withdrawn fence cannot be reactivated at the same term/generation.
    /// Recovery therefore requires a newer quorum-committed generation.
    pub fn withdraw(&self) -> Result<OperationsPublicationOutcome, OperationsPublicationError> {
        let _guard = self.publication_lock.lock().map_err(|_| {
            OperationsPublicationError::TransitionRejected("the local publication lock is poisoned")
        })?;
        let _file_guard = acquire_publication_lock(&self.assignment_path)?;
        let Some(mut current) = read_existing_assignment(&self.assignment_path)? else {
            return Ok(OperationsPublicationOutcome::Unchanged);
        };
        if !current.quorum_healthy {
            return Ok(OperationsPublicationOutcome::Unchanged);
        }
        current.quorum_healthy = false;
        current.voters_online = 0;
        atomic_write_assignment(&self.assignment_path, &current)?;
        Ok(OperationsPublicationOutcome::Published)
    }
}

fn acquire_publication_lock(target: &Path) -> Result<File, OperationsPublicationError> {
    let parent = target
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or(OperationsPublicationError::InvalidTarget)?;
    let parent_metadata = fs::symlink_metadata(parent).map_err(OperationsPublicationError::Io)?;
    if !parent_metadata.file_type().is_dir() || parent_metadata.file_type().is_symlink() {
        return Err(OperationsPublicationError::InvalidTarget);
    }
    #[cfg(unix)]
    if parent_metadata.permissions().mode() & 0o022 != 0 {
        return Err(OperationsPublicationError::InvalidTarget);
    }
    let file_name = target
        .file_name()
        .filter(|name| !name.is_empty())
        .ok_or(OperationsPublicationError::InvalidTarget)?;
    let mut lock_name = OsString::from(".");
    lock_name.push(file_name);
    lock_name.push(".lock");
    let lock_path = parent.join(lock_name);

    let before_open = match fs::symlink_metadata(&lock_path) {
        Ok(metadata) => {
            validate_lock_metadata(&metadata)?;
            Some(metadata)
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(OperationsPublicationError::Io(error)),
    };
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    #[cfg(unix)]
    options.mode(0o600);
    let lock_file = options.open(&lock_path)?;
    let opened_metadata = lock_file.metadata()?;
    validate_lock_metadata(&opened_metadata)?;
    #[cfg(unix)]
    if before_open.as_ref().is_some_and(|metadata| {
        metadata.dev() != opened_metadata.dev() || metadata.ino() != opened_metadata.ino()
    }) {
        return Err(OperationsPublicationError::InvalidTarget);
    }

    lock_file.lock()?;
    let locked_path_metadata =
        fs::symlink_metadata(&lock_path).map_err(OperationsPublicationError::Io)?;
    validate_lock_metadata(&locked_path_metadata)?;
    #[cfg(unix)]
    if locked_path_metadata.dev() != opened_metadata.dev()
        || locked_path_metadata.ino() != opened_metadata.ino()
    {
        return Err(OperationsPublicationError::InvalidTarget);
    }
    Ok(lock_file)
}

fn validate_lock_metadata(metadata: &fs::Metadata) -> Result<(), OperationsPublicationError> {
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err(OperationsPublicationError::InvalidTarget);
    }
    #[cfg(unix)]
    if metadata.permissions().mode() & 0o022 != 0 {
        return Err(OperationsPublicationError::InvalidTarget);
    }
    Ok(())
}

fn read_existing_assignment(
    path: &Path,
) -> Result<Option<OperationsCollectorAssignment>, OperationsPublicationError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(OperationsPublicationError::Io(error)),
    };
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err(OperationsPublicationError::ExistingStateUnavailable);
    }
    #[cfg(unix)]
    if metadata.permissions().mode() & 0o022 != 0 {
        return Err(OperationsPublicationError::ExistingStateUnavailable);
    }
    let file = match File::open(path) {
        Ok(file) => file,
        Err(error) => return Err(OperationsPublicationError::Io(error)),
    };
    #[cfg(unix)]
    {
        let opened_metadata = file.metadata().map_err(OperationsPublicationError::Io)?;
        if metadata.dev() != opened_metadata.dev() || metadata.ino() != opened_metadata.ino() {
            return Err(OperationsPublicationError::ExistingStateUnavailable);
        }
    }
    let mut bytes = Vec::with_capacity(MAX_ASSIGNMENT_BYTES.min(4096));
    file.take((MAX_ASSIGNMENT_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(OperationsPublicationError::Io)?;
    if bytes.len() > MAX_ASSIGNMENT_BYTES {
        return Err(OperationsPublicationError::ExistingStateUnavailable);
    }
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|_| OperationsPublicationError::ExistingStateUnavailable)
}

fn validate_transition(
    current: &OperationsCollectorAssignment,
    next: &OperationsCollectorAssignment,
) -> Result<OperationsPublicationOutcome, OperationsPublicationError> {
    if current == next {
        return Ok(OperationsPublicationOutcome::Unchanged);
    }
    if next.term < current.term {
        return Err(OperationsPublicationError::TransitionRejected(
            "the controller assignment term would move backwards",
        ));
    }
    if next.fencing_generation < current.fencing_generation {
        return Err(OperationsPublicationError::TransitionRejected(
            "the controller fencing generation would move backwards",
        ));
    }
    if next.term > current.term && next.fencing_generation <= current.fencing_generation {
        return Err(OperationsPublicationError::TransitionRejected(
            "a newer controller term must advance the fencing generation",
        ));
    }
    if next.committed_at_unix_ms < current.committed_at_unix_ms {
        return Err(OperationsPublicationError::TransitionRejected(
            "the controller commit time would move backwards",
        ));
    }
    let same_fence =
        next.term == current.term && next.fencing_generation == current.fencing_generation;
    if !current.quorum_healthy && same_fence {
        return Err(OperationsPublicationError::TransitionRejected(
            "a withdrawn controller fence cannot be reactivated",
        ));
    }
    if same_fence {
        if next.lease_expires_unix_ms < current.lease_expires_unix_ms {
            return Err(OperationsPublicationError::TransitionRejected(
                "the controller lease expiry would move backwards",
            ));
        }
        if next.authority != current.authority
            || next.leader_node_id != current.leader_node_id
            || next.leader_region != current.leader_region
            || next.public_endpoint != current.public_endpoint
            || next.voters_total != current.voters_total
        {
            return Err(OperationsPublicationError::TransitionRejected(
                "leader identity, endpoint, or voter set changed without a new fence",
            ));
        }
        if next.committed_at_unix_ms == current.committed_at_unix_ms {
            return Err(OperationsPublicationError::TransitionRejected(
                "assignment state changed without a newer quorum commit",
            ));
        }
    }
    Ok(OperationsPublicationOutcome::Published)
}

fn atomic_write_assignment(
    target: &Path,
    assignment: &OperationsCollectorAssignment,
) -> Result<(), OperationsPublicationError> {
    let parent = target
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or(OperationsPublicationError::InvalidTarget)?;
    let parent_metadata = fs::symlink_metadata(parent).map_err(OperationsPublicationError::Io)?;
    if !parent_metadata.file_type().is_dir() || parent_metadata.file_type().is_symlink() {
        return Err(OperationsPublicationError::InvalidTarget);
    }
    #[cfg(unix)]
    if parent_metadata.permissions().mode() & 0o022 != 0 {
        return Err(OperationsPublicationError::InvalidTarget);
    }
    let file_name = target
        .file_name()
        .filter(|name| !name.is_empty())
        .ok_or(OperationsPublicationError::InvalidTarget)?;

    let mut bytes =
        serde_json::to_vec(assignment).map_err(OperationsPublicationError::Serialization)?;
    bytes.push(b'\n');
    if bytes.len() > MAX_ASSIGNMENT_BYTES {
        return Err(OperationsPublicationError::UnsafeAssignment(
            OperationsAssignmentError::Unavailable,
        ));
    }

    let sequence = TEMP_FILE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let temporary_name = format!(
        ".{}.{}.{}.tmp",
        file_name.to_string_lossy(),
        process::id(),
        sequence
    );
    let temporary_path = parent.join(temporary_name);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut temporary = options.open(&temporary_path)?;

    let write_result = (|| -> Result<(), OperationsPublicationError> {
        temporary.write_all(&bytes)?;
        #[cfg(unix)]
        temporary.set_permissions(fs::Permissions::from_mode(0o644))?;
        temporary.sync_all()?;
        fs::rename(&temporary_path, target)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if write_result.is_err() {
        let _ = fs::remove_file(&temporary_path);
    }
    write_result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::operations_entrypoint::OperationsEntrypointPolicy;
    use std::env;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Arc, Barrier};
    use std::thread;

    const NOW: u64 = 2_000_000;
    static TEST_DIRECTORY_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            let sequence = TEST_DIRECTORY_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let path = env::temp_dir().join(format!(
                "needletail-operations-publisher-{}-{sequence}",
                process::id()
            ));
            fs::create_dir(&path).unwrap();
            #[cfg(unix)]
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            Self(path)
        }

        fn assignment_path(&self) -> PathBuf {
            self.0.join("operations-collector.json")
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn policy() -> OperationsEntrypointPolicy {
        OperationsEntrypointPolicy::new(
            "needletail-controller".to_owned(),
            "ops.example.test".to_owned(),
            vec!["https://ops-eu.example.test/mesh".to_owned()],
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
            public_endpoint: "https://ops-eu.example.test/mesh".to_owned(),
        }
    }

    #[test]
    fn atomically_publishes_a_safe_assignment_for_the_entrypoint() {
        let directory = TestDirectory::new();
        let path = directory.assignment_path();
        let policy = policy();
        let publisher = OperationsAssignmentPublisher::new(path.clone(), policy.clone());

        assert_eq!(
            publisher.publish_committed(&assignment(), NOW).unwrap(),
            OperationsPublicationOutcome::Published
        );
        assert_eq!(
            policy.resolve_file(&path, NOW).unwrap(),
            "https://ops-eu.example.test/mesh"
        );
        #[cfg(unix)]
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o644
        );
    }

    #[test]
    fn rejects_term_and_fence_rollback() {
        let directory = TestDirectory::new();
        let path = directory.assignment_path();
        let publisher = OperationsAssignmentPublisher::new(path.clone(), policy());
        publisher.publish_committed(&assignment(), NOW).unwrap();

        let mut rollback = assignment();
        rollback.term -= 1;
        rollback.committed_at_unix_ms += 1;
        rollback.lease_expires_unix_ms += 1;
        assert!(matches!(
            publisher.publish_committed(&rollback, NOW),
            Err(OperationsPublicationError::TransitionRejected(_))
        ));
        let retained: OperationsCollectorAssignment =
            serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(retained, assignment());
    }

    #[test]
    fn withdrawal_is_fail_closed_and_requires_a_new_fence() {
        let directory = TestDirectory::new();
        let path = directory.assignment_path();
        let policy = policy();
        let publisher = OperationsAssignmentPublisher::new(path.clone(), policy.clone());
        publisher.publish_committed(&assignment(), NOW).unwrap();

        assert_eq!(
            publisher.withdraw().unwrap(),
            OperationsPublicationOutcome::Published
        );
        assert_eq!(
            policy.resolve_file(&path, NOW),
            Err(OperationsAssignmentError::InvalidQuorum)
        );
        assert!(matches!(
            publisher.publish_committed(&assignment(), NOW),
            Err(OperationsPublicationError::TransitionRejected(_))
        ));

        let mut replacement = assignment();
        replacement.fencing_generation += 1;
        replacement.committed_at_unix_ms += 1;
        replacement.lease_expires_unix_ms += 1;
        assert_eq!(
            publisher.publish_committed(&replacement, NOW).unwrap(),
            OperationsPublicationOutcome::Published
        );
    }

    #[test]
    fn identical_publication_is_idempotent() {
        let directory = TestDirectory::new();
        let publisher = OperationsAssignmentPublisher::new(directory.assignment_path(), policy());
        publisher.publish_committed(&assignment(), NOW).unwrap();
        assert_eq!(
            publisher.publish_committed(&assignment(), NOW).unwrap(),
            OperationsPublicationOutcome::Unchanged
        );
    }

    #[test]
    fn concurrent_publishers_retain_the_highest_committed_fence() {
        let directory = TestDirectory::new();
        let path = directory.assignment_path();
        OperationsAssignmentPublisher::new(path.clone(), policy())
            .publish_committed(&assignment(), NOW)
            .unwrap();

        let mut lower = assignment();
        lower.term += 1;
        lower.fencing_generation += 1;
        lower.committed_at_unix_ms += 1;
        lower.lease_expires_unix_ms += 1;

        let mut higher = assignment();
        higher.term += 2;
        higher.fencing_generation += 2;
        higher.committed_at_unix_ms += 2;
        higher.lease_expires_unix_ms += 2;

        let barrier = Arc::new(Barrier::new(3));
        let lower_thread = {
            let barrier = Arc::clone(&barrier);
            let path = path.clone();
            thread::spawn(move || {
                let publisher = OperationsAssignmentPublisher::new(path, policy());
                barrier.wait();
                publisher.publish_committed(&lower, NOW)
            })
        };
        let higher_thread = {
            let barrier = Arc::clone(&barrier);
            let path = path.clone();
            thread::spawn(move || {
                let publisher = OperationsAssignmentPublisher::new(path, policy());
                barrier.wait();
                publisher.publish_committed(&higher, NOW)
            })
        };
        barrier.wait();

        let lower_result = lower_thread.join().unwrap();
        let higher_result = higher_thread.join().unwrap();
        assert!(
            matches!(
                lower_result,
                Ok(OperationsPublicationOutcome::Published)
                    | Err(OperationsPublicationError::TransitionRejected(_))
            ),
            "unexpected lower-fence result: {lower_result:?}"
        );
        assert_eq!(
            higher_result.unwrap(),
            OperationsPublicationOutcome::Published
        );

        let retained: OperationsCollectorAssignment =
            serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(retained.term, 9);
        assert_eq!(retained.fencing_generation, 14);
    }

    #[test]
    fn a_new_fence_may_use_a_shorter_bounded_lease() {
        let directory = TestDirectory::new();
        let publisher = OperationsAssignmentPublisher::new(directory.assignment_path(), policy());
        publisher.publish_committed(&assignment(), NOW).unwrap();

        let mut replacement = assignment();
        replacement.fencing_generation += 1;
        replacement.committed_at_unix_ms = NOW;
        replacement.lease_expires_unix_ms = NOW + 8_000;
        assert_eq!(
            publisher.publish_committed(&replacement, NOW).unwrap(),
            OperationsPublicationOutcome::Published
        );
    }

    #[test]
    fn corrupt_existing_state_is_not_silently_erased() {
        let directory = TestDirectory::new();
        let path = directory.assignment_path();
        fs::write(&path, b"not-json\n").unwrap();
        let publisher = OperationsAssignmentPublisher::new(path.clone(), policy());

        assert!(matches!(
            publisher.publish_committed(&assignment(), NOW),
            Err(OperationsPublicationError::ExistingStateUnavailable)
        ));
        assert_eq!(fs::read(path).unwrap(), b"not-json\n");
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_existing_state_is_rejected() {
        use std::os::unix::fs::symlink;

        let directory = TestDirectory::new();
        let outside = directory.0.join("outside.json");
        fs::write(&outside, serde_json::to_vec(&assignment()).unwrap()).unwrap();
        let path = directory.assignment_path();
        symlink(&outside, &path).unwrap();
        let publisher = OperationsAssignmentPublisher::new(path, policy());

        assert!(matches!(
            publisher.publish_committed(&assignment(), NOW),
            Err(OperationsPublicationError::ExistingStateUnavailable)
        ));
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_process_lock_is_rejected() {
        use std::os::unix::fs::symlink;

        let directory = TestDirectory::new();
        let outside = directory.0.join("outside.lock");
        fs::write(&outside, b"outside\n").unwrap();
        let lock_path = directory.0.join(".operations-collector.json.lock");
        symlink(&outside, lock_path).unwrap();
        let publisher = OperationsAssignmentPublisher::new(directory.assignment_path(), policy());

        assert!(matches!(
            publisher.publish_committed(&assignment(), NOW),
            Err(OperationsPublicationError::InvalidTarget)
        ));
    }
}
