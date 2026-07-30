use anyhow::{anyhow, bail, Context, Result};
use clap::Parser;
use needletail::operations_controller::{
    atomic_write_json, read_controller_state, OperationsControllerState,
    MAX_OPERATIONS_SNAPSHOT_BYTES,
};
use needletail::operations_snapshot::{
    assemble_operations_snapshot, snapshot_matches_controller, NodeSnapshot,
};
use reqwest::{Client, Url};
use serde::Deserialize;
use serde_json::Value;
use std::fs;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::time::{interval, timeout, MissedTickBehavior};

const SOURCE_CONFIG_SCHEMA: &str = "needletail.operations-sources.v1";
const MAX_SOURCE_RESPONSE_BYTES: usize = 2 * 1024 * 1024;

#[derive(Debug, Parser)]
#[command(
    name = "needletail-operations-collector",
    about = "Assemble or copy the single quorum-elected global Operations snapshot"
)]
struct Args {
    #[arg(long, env = "NEEDLETAIL_NODE_ID")]
    node_id: String,

    #[arg(long, env = "NEEDLETAIL_CONTROLLER_STATE_FILE")]
    controller_state_file: PathBuf,

    #[arg(long, env = "NEEDLETAIL_OPERATIONS_SNAPSHOT_FILE")]
    snapshot_file: PathBuf,

    #[arg(long, env = "NEEDLETAIL_OPERATIONS_SOURCES_FILE")]
    sources_file: PathBuf,

    #[arg(
        long,
        env = "NEEDLETAIL_OPERATIONS_POLL_INTERVAL_MS",
        default_value_t = 1_000
    )]
    poll_interval_ms: u64,

    #[arg(
        long,
        env = "NEEDLETAIL_OPERATIONS_REQUEST_TIMEOUT_MS",
        default_value_t = 2_500
    )]
    request_timeout_ms: u64,

    #[arg(
        long,
        env = "NEEDLETAIL_OPS_LEASE_SAFETY_MARGIN_MS",
        default_value_t = 5_000
    )]
    lease_safety_margin_ms: u64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OperationsSources {
    schema: String,
    stale_after_ms: u64,
    nodes: Vec<HttpSource>,
    contributor: Option<HttpSource>,
    contributor_node: Option<Value>,
    #[serde(default)]
    topology_links: Vec<Value>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HttpSource {
    node_id: String,
    endpoint: String,
    address: SocketAddr,
}

struct PreparedSource {
    source: HttpSource,
    client: Client,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    if args.poll_interval_ms == 0 || args.request_timeout_ms == 0 {
        bail!("Operations poll and request timeouts must be positive");
    }
    let sources = read_sources(&args.sources_file)?;
    let prepared_nodes = sources
        .nodes
        .iter()
        .cloned()
        .map(|source| prepare_source_for_node(source, &args.node_id))
        .collect::<Result<Vec<_>>>()?;
    let prepared_contributor = sources
        .contributor
        .clone()
        .map(prepare_source)
        .transpose()?;
    ensure_secure_parent(&args.snapshot_file)?;

    let mut ticker = interval(Duration::from_millis(args.poll_interval_ms));
    ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        ticker.tick().await;
        let now = now_unix_ms();
        let controller = match read_controller_state(&args.controller_state_file) {
            Ok(controller) if controller.lease_is_current(now, args.lease_safety_margin_ms) => {
                controller
            }
            Ok(_) => {
                eprintln!("Operations collector is fenced: no current quorum lease");
                continue;
            }
            Err(error) => {
                eprintln!("Operations collector state unavailable: {error}");
                continue;
            }
        };
        let result = if controller.assignment.leader_node_id == args.node_id {
            assemble_leader_snapshot(
                &controller,
                &prepared_nodes,
                prepared_contributor.as_ref(),
                sources.contributor_node.as_ref(),
                &sources.topology_links,
                sources.stale_after_ms,
                args.request_timeout_ms,
            )
            .await
        } else {
            copy_leader_snapshot(&controller, sources.stale_after_ms, args.request_timeout_ms).await
        };
        match result {
            Ok(snapshot) => {
                if let Err(error) = atomic_write_json(
                    &args.snapshot_file,
                    &snapshot,
                    MAX_OPERATIONS_SNAPSHOT_BYTES,
                ) {
                    eprintln!("failed to publish the Operations snapshot: {error}");
                }
            }
            Err(error) => eprintln!("Operations snapshot update failed: {error:#}"),
        }
    }
}

fn read_sources(path: &Path) -> Result<OperationsSources> {
    let bytes = fs::read(path)
        .with_context(|| format!("failed to read Operations sources {}", path.display()))?;
    if bytes.len() > 1024 * 1024 {
        bail!("Operations sources file exceeds 1 MiB");
    }
    let sources: OperationsSources =
        serde_json::from_slice(&bytes).context("failed to decode Operations sources")?;
    if sources.schema != SOURCE_CONFIG_SCHEMA {
        bail!("unsupported Operations sources schema");
    }
    if sources.nodes.len() < 3 || sources.nodes.len() > 1024 {
        bail!("Operations sources must contain between 3 and 1024 mesh nodes");
    }
    if sources.stale_after_ms == 0 || sources.stale_after_ms > 60_000 {
        bail!("Operations source freshness must be between 1 and 60000 ms");
    }
    let mut identities = std::collections::BTreeSet::new();
    for source in &sources.nodes {
        validate_source(source, "/api/mesh/local")?;
        if !identities.insert(&source.node_id) {
            bail!("duplicate Operations source {}", source.node_id);
        }
    }
    if let Some(contributor) = &sources.contributor {
        validate_source(contributor, "/api/status")?;
    }
    match (&sources.contributor, &sources.contributor_node) {
        (Some(contributor), Some(node)) => {
            if node.get("node_id").and_then(Value::as_str) != Some(contributor.node_id.as_str()) {
                bail!("Operations contributor node identity does not match its source");
            }
        }
        (None, None) => {}
        _ => bail!("Operations contributor source and node metadata must be configured together"),
    }
    Ok(sources)
}

fn validate_source(source: &HttpSource, expected_path: &str) -> Result<()> {
    if source.node_id.is_empty()
        || !source
            .node_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        bail!("invalid Operations source node identity");
    }
    let url = Url::parse(&source.endpoint).context("invalid Operations source endpoint")?;
    if url.scheme() != "https"
        || url.path() != expected_path
        || url.query().is_some()
        || url.fragment().is_some()
        || url.host_str().is_none()
    {
        bail!("Operations source endpoint must be HTTPS {expected_path}");
    }
    if source.address.ip().is_unspecified() || source.address.port() == 0 {
        bail!("invalid Operations source address");
    }
    Ok(())
}

fn prepare_source(source: HttpSource) -> Result<PreparedSource> {
    let url = Url::parse(&source.endpoint)?;
    let hostname = url
        .host_str()
        .ok_or_else(|| anyhow!("Operations source endpoint has no hostname"))?;
    let client = Client::builder()
        .https_only(true)
        .redirect(reqwest::redirect::Policy::none())
        .resolve(hostname, source.address)
        .build()
        .context("failed to build Operations source client")?;
    Ok(PreparedSource { source, client })
}

fn prepare_source_for_node(mut source: HttpSource, local_node_id: &str) -> Result<PreparedSource> {
    if source.node_id == local_node_id {
        source
            .address
            .set_ip(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST));
    }
    prepare_source(source)
}

async fn assemble_leader_snapshot(
    controller: &OperationsControllerState,
    nodes: &[PreparedSource],
    contributor: Option<&PreparedSource>,
    contributor_node: Option<&Value>,
    topology_links: &[Value],
    stale_after_ms: u64,
    request_timeout_ms: u64,
) -> Result<Value> {
    if !controller.lease_is_current(now_unix_ms(), 0) {
        bail!("collector lease expired before fleet polling");
    }
    let request_timeout = Duration::from_millis(request_timeout_ms);
    let mut tasks = Vec::with_capacity(nodes.len());
    for prepared in nodes {
        let client = prepared.client.clone();
        let source = prepared.source.clone();
        let node_id = source.node_id.clone();
        tasks.push((
            node_id,
            tokio::spawn(
                async move { fetch_json(&client, &source.endpoint, request_timeout).await },
            ),
        ));
    }
    let mut snapshots = Vec::with_capacity(nodes.len());
    for (node_id, task) in tasks {
        match task.await {
            Ok(Ok(snapshot)) => snapshots.push(NodeSnapshot {
                expected_node_id: node_id,
                snapshot: Some(snapshot),
                error: None,
            }),
            Ok(Err(error)) => snapshots.push(NodeSnapshot {
                expected_node_id: node_id,
                snapshot: None,
                error: Some(error.to_string()),
            }),
            Err(error) => snapshots.push(NodeSnapshot {
                expected_node_id: node_id,
                snapshot: None,
                error: Some(error.to_string()),
            }),
        }
    }
    let now = now_unix_ms();
    let (contributor, contributor_source) = match (contributor, contributor_node) {
        (Some(prepared), Some(node)) => {
            match fetch_json(&prepared.client, &prepared.source.endpoint, request_timeout).await {
                Ok(status) => {
                    let mut node = node.clone();
                    if let Some(object) = node.as_object_mut() {
                        object.insert("updated_unix_ms".to_owned(), Value::from(now));
                    }
                    (
                        Some(status),
                        Some(NodeSnapshot {
                            expected_node_id: prepared.source.node_id.clone(),
                            snapshot: Some(serde_json::json!({
                                "updated_unix_ms": now,
                                "node": node,
                            })),
                            error: None,
                        }),
                    )
                }
                Err(error) => (
                    None,
                    Some(NodeSnapshot {
                        expected_node_id: prepared.source.node_id.clone(),
                        snapshot: None,
                        error: Some(error.to_string()),
                    }),
                ),
            }
        }
        (None, None) => (None, None),
        _ => bail!("Operations contributor source configuration is inconsistent"),
    };
    if !controller.lease_is_current(now, 0) {
        bail!("collector lease expired while polling the fleet");
    }
    assemble_operations_snapshot(
        controller,
        &snapshots,
        contributor,
        contributor_source.as_slice(),
        topology_links.to_vec(),
        now,
        stale_after_ms,
    )
}

async fn copy_leader_snapshot(
    controller: &OperationsControllerState,
    stale_after_ms: u64,
    request_timeout_ms: u64,
) -> Result<Value> {
    let source = HttpSource {
        node_id: controller.assignment.leader_node_id.clone(),
        endpoint: controller.snapshot_endpoint.clone(),
        address: controller.snapshot_address,
    };
    validate_source(&source, "/api/mesh")?;
    let prepared = prepare_source(source)?;
    let snapshot = fetch_json(
        &prepared.client,
        &prepared.source.endpoint,
        Duration::from_millis(request_timeout_ms),
    )
    .await?;
    let now = now_unix_ms();
    if !controller.lease_is_current(now, 0)
        || !snapshot_matches_controller(&snapshot, controller, now, stale_after_ms)
    {
        bail!("leader snapshot does not match the current committed generation");
    }
    Ok(snapshot)
}

async fn fetch_json(client: &Client, endpoint: &str, request_timeout: Duration) -> Result<Value> {
    let response = timeout(request_timeout, client.get(endpoint).send())
        .await
        .context("Operations source request timed out")??
        .error_for_status()
        .context("Operations source returned an error status")?;
    let declared = response.content_length().unwrap_or_default();
    if declared > MAX_SOURCE_RESPONSE_BYTES as u64 {
        bail!("Operations source response exceeds 2 MiB");
    }
    let bytes = timeout(request_timeout, response.bytes())
        .await
        .context("Operations source body timed out")??;
    if bytes.len() > MAX_SOURCE_RESPONSE_BYTES {
        bail!("Operations source response exceeds 2 MiB");
    }
    serde_json::from_slice(&bytes).context("failed to decode Operations source response")
}

fn ensure_secure_parent(path: &Path) -> Result<()> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or_else(|| anyhow!("invalid snapshot path {}", path.display()))?;
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
    fn source_validation_requires_exact_https_routes() {
        let source = HttpSource {
            node_id: "edge-london".to_owned(),
            endpoint: "https://ops.example.com:19444/api/mesh/local".to_owned(),
            address: "192.0.2.1:19444".parse().unwrap(),
        };
        assert!(validate_source(&source, "/api/mesh/local").is_ok());
        assert!(validate_source(&source, "/api/mesh").is_err());

        let mut insecure = source;
        insecure.endpoint = "http://ops.example.com/api/mesh/local".to_owned();
        assert!(validate_source(&insecure, "/api/mesh/local").is_err());
    }

    #[test]
    fn local_source_uses_loopback_instead_of_public_hairpinning() {
        let source = HttpSource {
            node_id: "edge-london".to_owned(),
            endpoint: "https://ops.example.com:19444/api/mesh/local".to_owned(),
            address: "192.0.2.1:19444".parse().unwrap(),
        };
        let prepared = prepare_source_for_node(source, "edge-london").unwrap();
        assert_eq!(prepared.source.address, "127.0.0.1:19444".parse().unwrap());
    }
}
