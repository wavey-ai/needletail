use anyhow::{anyhow, bail, Context, Result};
use clap::Parser;
use etcd_client::{Certificate, Client, ConnectOptions, Identity, ProclaimOptions, TlsOptions};
use needletail::operations_assignment_publisher::OperationsAssignmentPublisher;
use needletail::operations_controller::{
    read_controller_state, write_controller_state, OperationsCandidate, OperationsControllerState,
    CONTROLLER_AUTHORITY, CONTROLLER_CANDIDATE_SCHEMA,
};
use needletail::operations_entrypoint::{
    OperationsCollectorAssignment, OperationsEntrypointPolicy,
};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::watch;
use tokio::task::JoinSet;
use tokio::time::{interval, sleep, timeout, MissedTickBehavior};

const COMMITTED_RECORD_SCHEMA: &str = "needletail.operations-committed-leader.v1";

#[derive(Debug, Parser)]
#[command(
    name = "needletail-controller-agent",
    about = "Project an etcd-quorum Operations election into fail-closed local state"
)]
struct Args {
    #[arg(long, env = "NEEDLETAIL_NODE_ID")]
    node_id: String,

    #[arg(long, env = "NEEDLETAIL_REGION")]
    region: String,

    #[arg(long, env = "NEEDLETAIL_OPS_PUBLIC_ENDPOINT")]
    public_endpoint: String,

    #[arg(long, env = "NEEDLETAIL_OPS_SNAPSHOT_ENDPOINT")]
    snapshot_endpoint: String,

    #[arg(long, env = "NEEDLETAIL_OPS_SNAPSHOT_ADDRESS")]
    snapshot_address: std::net::SocketAddr,

    #[arg(
        long = "etcd-endpoint",
        env = "NEEDLETAIL_CONTROLLER_ETCD_ENDPOINTS",
        value_delimiter = ',',
        required = true
    )]
    etcd_endpoints: Vec<String>,

    #[arg(long, env = "NEEDLETAIL_CONTROLLER_ETCD_CA_CERT")]
    etcd_ca_cert: PathBuf,

    #[arg(long, env = "NEEDLETAIL_CONTROLLER_ETCD_CLIENT_CERT")]
    etcd_client_cert: PathBuf,

    #[arg(long, env = "NEEDLETAIL_CONTROLLER_ETCD_CLIENT_KEY")]
    etcd_client_key: PathBuf,

    #[arg(
        long,
        env = "NEEDLETAIL_CONTROLLER_ELECTION_NAME",
        default_value = "/needletail/operations/collector"
    )]
    election_name: String,

    #[arg(long, env = "NEEDLETAIL_CONTROLLER_CANDIDATE", default_value_t = false)]
    candidate: bool,

    #[arg(
        long,
        env = "NEEDLETAIL_CONTROLLER_LEASE_TTL_SECONDS",
        default_value_t = 15
    )]
    lease_ttl_seconds: i64,

    #[arg(
        long,
        env = "NEEDLETAIL_CONTROLLER_RENEW_INTERVAL_MS",
        default_value_t = 3_000
    )]
    renew_interval_ms: u64,

    #[arg(
        long,
        env = "NEEDLETAIL_CONTROLLER_OBSERVE_INTERVAL_MS",
        default_value_t = 500
    )]
    observe_interval_ms: u64,

    #[arg(long, env = "NEEDLETAIL_OPS_ASSIGNMENT_FILE")]
    assignment_file: PathBuf,

    #[arg(long, env = "NEEDLETAIL_CONTROLLER_STATE_FILE")]
    controller_state_file: PathBuf,

    #[arg(long, env = "NEEDLETAIL_OPS_ENTRYPOINT_HOST")]
    entrypoint_host: String,

    #[arg(
        long = "allowed-endpoint",
        env = "NEEDLETAIL_OPS_ALLOWED_ENDPOINTS",
        value_delimiter = ',',
        required = true
    )]
    allowed_endpoints: Vec<String>,

    #[arg(
        long,
        env = "NEEDLETAIL_OPS_LEASE_SAFETY_MARGIN_MS",
        default_value_t = 5_000
    )]
    lease_safety_margin_ms: u64,

    #[arg(
        long,
        env = "NEEDLETAIL_OPS_MAX_CLOCK_SKEW_MS",
        default_value_t = 1_000
    )]
    max_clock_skew_ms: u64,
}

#[derive(Clone)]
struct EtcdConnection {
    endpoints: Vec<String>,
    options: ConnectOptions,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CommittedLeaderRecord {
    schema: String,
    assignment: OperationsCollectorAssignment,
    candidate: OperationsCandidate,
    leadership_acquired_unix_ms: u64,
}

impl CommittedLeaderRecord {
    fn validate(&self) -> Result<()> {
        if self.schema != COMMITTED_RECORD_SCHEMA {
            bail!("unsupported committed-leader schema");
        }
        self.candidate.validate().map_err(|error| anyhow!(error))?;
        if self.assignment.authority != CONTROLLER_AUTHORITY
            || self.assignment.leader_node_id != self.candidate.node_id
            || self.assignment.leader_region != self.candidate.region
            || self.assignment.public_endpoint != self.candidate.public_endpoint
            || self.assignment.term == 0
            || self.assignment.fencing_generation == 0
            || self.leadership_acquired_unix_ms == 0
            || self.leadership_acquired_unix_ms > self.assignment.committed_at_unix_ms
        {
            bail!("committed assignment does not match the elected candidate");
        }
        Ok(())
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    validate_args(&args)?;
    ensure_secure_state_parent(&args.assignment_file)?;
    ensure_secure_state_parent(&args.controller_state_file)?;

    let candidate = OperationsCandidate {
        schema: CONTROLLER_CANDIDATE_SCHEMA.to_owned(),
        node_id: args.node_id.clone(),
        region: args.region.clone(),
        public_endpoint: args.public_endpoint.clone(),
        snapshot_endpoint: args.snapshot_endpoint.clone(),
        snapshot_address: args.snapshot_address,
    };
    candidate.validate().map_err(|error| anyhow!(error))?;
    let connection = EtcdConnection {
        endpoints: args.etcd_endpoints.clone(),
        options: etcd_connect_options(&args)?,
    };
    let observer = tokio::spawn(observer_loop(
        connection.clone(),
        args.election_name.clone(),
        args.assignment_file.clone(),
        args.controller_state_file.clone(),
        args.entrypoint_host.clone(),
        args.allowed_endpoints.clone(),
        args.lease_safety_margin_ms,
        args.max_clock_skew_ms,
        args.lease_ttl_seconds as u64 * 1_000,
        args.observe_interval_ms,
    ));

    if args.candidate {
        tokio::select! {
            result = campaign_forever(&args, &connection, &candidate) => result,
            signal = tokio::signal::ctrl_c() => {
                signal.context("failed to wait for controller shutdown signal")?;
                Ok(())
            }
        }?;
    } else {
        tokio::signal::ctrl_c()
            .await
            .context("failed to wait for controller shutdown signal")?;
    }
    observer.abort();
    Ok(())
}

fn validate_args(args: &Args) -> Result<()> {
    if args.etcd_endpoints.len() < 3 || args.etcd_endpoints.len().is_multiple_of(2) {
        bail!("the controller requires an odd etcd voter set of at least three members");
    }
    if args
        .etcd_endpoints
        .iter()
        .any(|endpoint| !endpoint.starts_with("https://"))
    {
        bail!("all etcd endpoints must use HTTPS");
    }
    if !(5..=60).contains(&args.lease_ttl_seconds) {
        bail!("the controller lease TTL must be between 5 and 60 seconds");
    }
    if args.renew_interval_ms == 0
        || args.renew_interval_ms >= (args.lease_ttl_seconds as u64 * 1_000) / 2
    {
        bail!("the renew interval must be positive and shorter than half the lease TTL");
    }
    if args.observe_interval_ms == 0 {
        bail!("the observe interval must be positive");
    }
    Ok(())
}

fn etcd_connect_options(args: &Args) -> Result<ConnectOptions> {
    let ca = fs::read(&args.etcd_ca_cert)
        .with_context(|| format!("failed to read {}", args.etcd_ca_cert.display()))?;
    let cert = fs::read(&args.etcd_client_cert)
        .with_context(|| format!("failed to read {}", args.etcd_client_cert.display()))?;
    let key = fs::read(&args.etcd_client_key)
        .with_context(|| format!("failed to read {}", args.etcd_client_key.display()))?;
    let tls = TlsOptions::new()
        .ca_certificate(Certificate::from_pem(ca))
        .identity(Identity::from_pem(cert, key));
    Ok(ConnectOptions::new()
        .with_tls(tls)
        .with_connect_timeout(Duration::from_secs(3))
        .with_timeout(Duration::from_secs(5))
        .with_keep_alive(Duration::from_secs(2), Duration::from_secs(3))
        .with_keep_alive_while_idle(true)
        .with_require_leader(true))
}

async fn connect(connection: &EtcdConnection) -> Result<Client> {
    Client::connect(
        connection.endpoints.clone(),
        Some(connection.options.clone()),
    )
    .await
    .context("failed to connect to the controller quorum")
}

async fn campaign_forever(
    args: &Args,
    connection: &EtcdConnection,
    candidate: &OperationsCandidate,
) -> Result<()> {
    loop {
        match campaign_once(args, connection, candidate).await {
            Ok(()) => {}
            Err(error) => eprintln!("Operations campaign ended: {error:#}"),
        }
        sleep(Duration::from_secs(1)).await;
    }
}

async fn campaign_once(
    args: &Args,
    connection: &EtcdConnection,
    candidate: &OperationsCandidate,
) -> Result<()> {
    let mut client = connect(connection).await?;
    let lease = client
        .lease_grant(args.lease_ttl_seconds, None)
        .await
        .context("failed to grant an Operations election lease")?;
    let lease_id = lease.id();
    let (mut keeper, mut keepalive_stream) = client
        .lease_keep_alive(lease_id)
        .await
        .context("failed to open the Operations lease keepalive")?;
    let (lease_health_tx, mut lease_health_rx) = watch::channel(None::<(i64, u64)>);
    let renew_interval = Duration::from_millis(args.renew_interval_ms);
    let mut keepalive_task = tokio::spawn(async move {
        let mut ticker = interval(renew_interval);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        loop {
            ticker.tick().await;
            keeper
                .keep_alive()
                .await
                .context("failed to renew the Operations election lease")?;
            let response = timeout(Duration::from_secs(3), keepalive_stream.message())
                .await
                .context("timed out waiting for the renewed Operations lease")??
                .ok_or_else(|| anyhow!("Operations lease keepalive stream ended"))?;
            if response.ttl() <= 0 {
                bail!("the Operations election lease expired");
            }
            lease_health_tx
                .send(Some((response.ttl(), now_unix_ms())))
                .map_err(|_| anyhow!("Operations lease observer closed"))?;
        }
        #[allow(unreachable_code)]
        Ok::<(), anyhow::Error>(())
    });
    timeout(Duration::from_secs(3), async {
        while lease_health_rx.borrow().is_none() {
            lease_health_rx
                .changed()
                .await
                .map_err(|_| anyhow!("Operations lease keepalive ended"))?;
        }
        Ok::<(), anyhow::Error>(())
    })
    .await
    .context("timed out waiting for the initial Operations lease")??;

    let proposal = serde_json::to_vec(candidate)?;
    let mut campaign = tokio::select! {
        result = client.campaign(args.election_name.clone(), proposal, lease_id) => {
            result.context("failed to campaign for Operations leadership")?
        }
        result = &mut keepalive_task => {
            return Err(joined_keepalive_error(result));
        }
    };
    let leader = campaign
        .take_leader()
        .ok_or_else(|| anyhow!("the election response omitted its leader key"))?;
    let generation =
        u64::try_from(leader.rev()).context("the election returned an invalid revision")?;
    if generation == 0 {
        bail!("the election returned generation zero");
    }
    println!(
        "Operations leadership acquired node={} generation={generation}",
        candidate.node_id
    );

    let acquired_at_unix_ms = now_unix_ms();
    let mut ticker = interval(Duration::from_millis(args.renew_interval_ms));
    ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
    loop {
        ticker.tick().await;
        if keepalive_task.is_finished() {
            return Err(joined_keepalive_error(keepalive_task.await));
        }
        let (ttl, observed_at_unix_ms) = lease_health_rx
            .borrow()
            .as_ref()
            .copied()
            .ok_or_else(|| anyhow!("Operations lease health is unavailable"))?;
        let lease_expires_unix_ms =
            observed_at_unix_ms.saturating_add((ttl as u64).saturating_mul(1_000));
        if lease_expires_unix_ms <= now_unix_ms().saturating_add(args.lease_safety_margin_ms) {
            bail!("the Operations election lease is inside its safety margin");
        }
        let voters_online = probe_voters(connection).await;
        let voters_total =
            u16::try_from(connection.endpoints.len()).context("the etcd voter set is too large")?;
        if voters_online <= voters_total / 2 {
            bail!(
                "controller quorum is not observable: {voters_online}/{voters_total} voters online"
            );
        }
        let now = now_unix_ms();
        if lease_expires_unix_ms <= now.saturating_add(args.lease_safety_margin_ms) {
            bail!("the Operations election lease became unsafe while probing voters");
        }
        let assignment = OperationsCollectorAssignment {
            schema_version: 1,
            authority: CONTROLLER_AUTHORITY.to_owned(),
            term: generation,
            fencing_generation: generation,
            leader_node_id: candidate.node_id.clone(),
            leader_region: candidate.region.clone(),
            quorum_healthy: true,
            voters_online,
            voters_total,
            committed_at_unix_ms: now,
            lease_expires_unix_ms,
            public_endpoint: candidate.public_endpoint.clone(),
        };
        let record = CommittedLeaderRecord {
            schema: COMMITTED_RECORD_SCHEMA.to_owned(),
            assignment,
            candidate: candidate.clone(),
            leadership_acquired_unix_ms: acquired_at_unix_ms,
        };
        record.validate()?;
        client
            .proclaim(
                serde_json::to_vec(&record)?,
                Some(ProclaimOptions::new().with_leader(leader.clone())),
            )
            .await
            .context("failed to commit the refreshed Operations assignment")?;
    }
}

async fn probe_voters(connection: &EtcdConnection) -> u16 {
    let mut probes = JoinSet::new();
    for endpoint in &connection.endpoints {
        let endpoint = endpoint.clone();
        let options = connection.options.clone().with_require_leader(false);
        probes.spawn(async move {
            let Ok(Ok(mut client)) = timeout(
                Duration::from_secs(2),
                Client::connect([endpoint], Some(options)),
            )
            .await
            else {
                return false;
            };
            timeout(Duration::from_secs(2), client.status())
                .await
                .is_ok_and(|status| status.is_ok())
        });
    }
    let mut online = 0_u16;
    while let Some(result) = probes.join_next().await {
        if result.unwrap_or(false) {
            online = online.saturating_add(1);
        }
    }
    online
}

fn joined_keepalive_error(
    result: std::result::Result<Result<()>, tokio::task::JoinError>,
) -> anyhow::Error {
    match result {
        Ok(Ok(())) => anyhow!("Operations lease keepalive ended unexpectedly"),
        Ok(Err(error)) => error,
        Err(error) => anyhow!("Operations lease keepalive task failed: {error}"),
    }
}

#[allow(clippy::too_many_arguments)]
async fn observer_loop(
    connection: EtcdConnection,
    election_name: String,
    assignment_file: PathBuf,
    controller_state_file: PathBuf,
    entrypoint_host: String,
    allowed_endpoints: Vec<String>,
    lease_safety_margin_ms: u64,
    max_clock_skew_ms: u64,
    max_lease_duration_ms: u64,
    observe_interval_ms: u64,
) {
    let policy = match OperationsEntrypointPolicy::new(
        CONTROLLER_AUTHORITY.to_owned(),
        entrypoint_host,
        allowed_endpoints,
        lease_safety_margin_ms,
        max_clock_skew_ms,
        max_lease_duration_ms,
    ) {
        Ok(policy) => policy,
        Err(error) => {
            eprintln!("invalid Operations publication policy: {error}");
            return;
        }
    };
    let publisher = OperationsAssignmentPublisher::new(assignment_file, policy);
    let mut client: Option<Client> = None;
    loop {
        if client.is_none() {
            match connect(&connection).await {
                Ok(connected) => client = Some(connected),
                Err(error) => {
                    withdraw_if_expired(&publisher, &controller_state_file, lease_safety_margin_ms);
                    eprintln!("Operations observer disconnected: {error:#}");
                    sleep(Duration::from_millis(observe_interval_ms)).await;
                    continue;
                }
            }
        }
        let observed = timeout(
            Duration::from_secs(5),
            client
                .as_mut()
                .expect("client was connected above")
                .leader(election_name.clone()),
        )
        .await;
        match observed {
            Ok(Ok(response)) => {
                if let Some(kv) = response.kv() {
                    match publish_observed_record(
                        kv.value(),
                        &publisher,
                        &controller_state_file,
                        lease_safety_margin_ms,
                    ) {
                        Ok(()) => {}
                        Err(error) => {
                            withdraw_if_expired(
                                &publisher,
                                &controller_state_file,
                                lease_safety_margin_ms,
                            );
                            eprintln!("Operations observer rejected leader record: {error:#}");
                        }
                    }
                } else {
                    withdraw_if_expired(&publisher, &controller_state_file, lease_safety_margin_ms);
                }
            }
            Ok(Err(error)) => {
                eprintln!("Operations observer request failed: {error}");
                client = None;
                withdraw_if_expired(&publisher, &controller_state_file, lease_safety_margin_ms);
            }
            Err(_) => {
                eprintln!("Operations observer request timed out");
                client = None;
                withdraw_if_expired(&publisher, &controller_state_file, lease_safety_margin_ms);
            }
        }
        sleep(Duration::from_millis(observe_interval_ms)).await;
    }
}

fn publish_observed_record(
    bytes: &[u8],
    publisher: &OperationsAssignmentPublisher,
    controller_state_file: &Path,
    safety_margin_ms: u64,
) -> Result<()> {
    let record = serde_json::from_slice::<CommittedLeaderRecord>(bytes)
        .context("failed to decode the committed leader record")?;
    record.validate()?;
    let now = now_unix_ms();
    if record.assignment.lease_expires_unix_ms <= now.saturating_add(safety_margin_ms) {
        bail!("the committed leader lease is no longer safe");
    }
    publisher
        .publish_committed(&record.assignment, now)
        .context("failed to publish the committed assignment")?;
    let state = OperationsControllerState::from_assignment(
        record.assignment,
        &record.candidate,
        record.leadership_acquired_unix_ms,
        now,
    )
    .map_err(|error| anyhow!(error))?;
    write_controller_state(controller_state_file, &state)
        .context("failed to publish local controller state")?;
    Ok(())
}

fn withdraw_if_expired(
    publisher: &OperationsAssignmentPublisher,
    controller_state_file: &Path,
    safety_margin_ms: u64,
) {
    let Ok(mut state) = read_controller_state(controller_state_file) else {
        return;
    };
    if state.lease_is_current(now_unix_ms(), safety_margin_ms) {
        return;
    }
    if let Err(error) = publisher.withdraw() {
        eprintln!("failed to withdraw the expired Operations assignment: {error}");
        return;
    }
    state.assignment.quorum_healthy = false;
    state.assignment.voters_online = 0;
    state.observed_at_unix_ms = now_unix_ms();
    if let Err(error) = write_controller_state(controller_state_file, &state) {
        eprintln!("failed to withdraw local controller state: {error}");
    }
}

fn ensure_secure_state_parent(path: &Path) -> Result<()> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or_else(|| anyhow!("invalid state path {}", path.display()))?;
    fs::create_dir_all(parent).with_context(|| format!("failed to create {}", parent.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o755))
            .with_context(|| format!("failed to secure {}", parent.display()))?;
    }
    Ok(())
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn committed_record_rejects_candidate_assignment_mismatch() {
        let candidate = OperationsCandidate {
            schema: CONTROLLER_CANDIDATE_SCHEMA.to_owned(),
            node_id: "edge-london".to_owned(),
            region: "europe-west2".to_owned(),
            public_endpoint: "https://ops-london.example.com/mesh".to_owned(),
            snapshot_endpoint: "https://ops-london.example.com:19444/api/mesh".to_owned(),
            snapshot_address: "192.0.2.10:19444".parse().unwrap(),
        };
        let mut record = CommittedLeaderRecord {
            schema: COMMITTED_RECORD_SCHEMA.to_owned(),
            assignment: OperationsCollectorAssignment {
                schema_version: 1,
                authority: CONTROLLER_AUTHORITY.to_owned(),
                term: 7,
                fencing_generation: 7,
                leader_node_id: candidate.node_id.clone(),
                leader_region: candidate.region.clone(),
                quorum_healthy: true,
                voters_online: 3,
                voters_total: 3,
                committed_at_unix_ms: 1_000,
                lease_expires_unix_ms: 16_000,
                public_endpoint: candidate.public_endpoint.clone(),
            },
            candidate,
            leadership_acquired_unix_ms: 1_000,
        };
        assert!(record.validate().is_ok());
        record.assignment.leader_node_id = "edge-tokyo".to_owned();
        assert!(record.validate().is_err());
    }
}
