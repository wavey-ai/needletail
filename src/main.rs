use anyhow::{anyhow, bail, Context, Result};
use clap::Parser;
use needletail::relay_topology::{
    FailureDomain, NodeRole, ParentLink, ParentRole, RelayNode, RelayTopology, TopologyLimits,
};
use needletail::service_plan::{
    CarrierLink, CarrierProfile, CompiledService, CompiledServicePlan, DeploymentPurpose,
    FailoverControlLink, FailoverPolicy, RelayProgram, RelaySymbolLane,
};
use std::ffi::OsStr;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{ExitStatus, Stdio};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::{Child, Command};
use tokio::task::JoinHandle;
use tokio::time::{sleep, timeout};

const DEFAULT_HOST: &str = "local.bitneedle.com";
const CONTRIB_NODE_ID: &str = "contrib";
const PRIMARY_RELAY_NODE_ID: &str = "relay-primary";
const SECONDARY_RELAY_NODE_ID: &str = "relay-secondary";
const EDGE_NODE_ID: &str = "edge";

#[derive(Debug, Parser)]
#[command(
    name = "needletail",
    about = "Orchestrate the local Needletail realtime media constellation"
)]
struct Args {
    #[arg(long)]
    contrib_root: Option<PathBuf>,

    #[arg(long)]
    mesh_root: Option<PathBuf>,

    #[arg(long, default_value = DEFAULT_HOST)]
    host: String,

    #[arg(long)]
    cert: Option<PathBuf>,

    #[arg(long)]
    key: Option<PathBuf>,

    #[arg(long)]
    no_build: bool,

    #[arg(long, env = "NEEDLETAIL_MISSION_CONTROL_DIST")]
    mission_control_dist: Option<PathBuf>,

    #[arg(long)]
    no_mission_control_build: bool,

    #[arg(long, default_value_t = 1)]
    stream_id: u64,

    #[arg(long, env = "AV_LL_HLS_PART_MS", default_value_t = 50)]
    part_ms: u64,

    #[arg(long, default_value_t = 19444)]
    uk_http_port: u16,

    #[arg(long, default_value_t = 19445)]
    us_http_port: u16,

    #[arg(long, default_value_t = 19446)]
    secondary_relay_http_port: u16,

    #[arg(long, default_value_t = 19443)]
    contrib_http_port: u16,

    #[arg(long, default_value = "127.0.0.1:29101")]
    uk_mesh: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:29201")]
    us_mesh: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:29301")]
    secondary_relay_mesh: SocketAddr,

    #[arg(long)]
    uk_peer: Option<SocketAddr>,

    #[arg(long)]
    us_peer: Option<SocketAddr>,

    #[arg(long, default_value = "127.0.0.1:22001")]
    uk_fec: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:22002")]
    us_fec: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:22101")]
    uk_media_fec: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:22102")]
    us_media_fec: SocketAddr,

    /// Independent repair-lane socket on the UK playback edge. Both relay
    /// lanes converge into the same canonical-object assembler.
    #[arg(long, default_value = "127.0.0.1:22201")]
    uk_relay_secondary_bind: SocketAddr,

    /// Primary RelaySession receive socket on the playback edge.
    #[arg(long, default_value = "127.0.0.1:22200")]
    edge_relay_primary_bind: SocketAddr,

    /// Stable source socket from the primary backbone relay to the edge.
    #[arg(long, default_value = "127.0.0.1:22401")]
    primary_relay_forward_bind: SocketAddr,

    /// Stable source socket from the warm backbone relay to the edge.
    #[arg(long, default_value = "127.0.0.1:22402")]
    secondary_relay_forward_bind: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:22103")]
    edge_media_fec: SocketAddr,

    /// Fixed contributor source socket for the primary/source lane.
    #[arg(long, default_value = "127.0.0.1:22301")]
    contrib_relay_primary_bind: SocketAddr,

    /// Fixed contributor source socket for the warm-secondary/repair lane.
    #[arg(long, default_value = "127.0.0.1:22302")]
    contrib_relay_secondary_bind: SocketAddr,

    /// Optional one-way carrier emulator/tunnel endpoint between the
    /// contributor and primary backbone relay.
    #[arg(long)]
    contrib_primary_via: Option<SocketAddr>,

    /// Optional one-way carrier emulator/tunnel endpoint between the
    /// contributor and warm backbone relay.
    #[arg(long)]
    contrib_secondary_via: Option<SocketAddr>,

    /// Optional one-way carrier emulator/tunnel endpoint on the primary
    /// backbone-to-edge source lane.
    #[arg(long)]
    primary_edge_via: Option<SocketAddr>,

    /// Optional one-way carrier emulator/tunnel endpoint on the warm
    /// backbone-to-edge repair lane.
    #[arg(long)]
    secondary_edge_via: Option<SocketAddr>,

    #[arg(long, default_value_t = 1)]
    relay_topology_generation: u64,

    #[arg(long, default_value_t = 1)]
    relay_subscription_id: u64,

    #[arg(long, default_value_t = 1_000)]
    relay_deadline_ms: u64,

    #[arg(long)]
    contrib_fec_target: Option<SocketAddr>,

    #[arg(long)]
    contrib_media_fec_target: Option<SocketAddr>,

    #[arg(long, default_value = "127.0.0.1:27300")]
    uk_telemetry: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:27301")]
    us_telemetry: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:27302")]
    secondary_relay_telemetry: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:27000")]
    rist_bind: SocketAddr,

    #[arg(long, default_value = "0x11223344")]
    rist_flow_id: String,

    #[arg(long, default_value = "127.0.0.1:27001")]
    srt_bind: SocketAddr,

    #[arg(long, default_value = "127.0.0.1:19350")]
    rtmp_bind: SocketAddr,

    #[arg(long, default_value = "obs-local")]
    rtmp_stream_key: String,

    #[arg(long, default_value_t = 25)]
    health_timeout_seconds: u64,

    #[arg(long, hide = true)]
    exit_after_ready: bool,
}

struct Service {
    name: String,
    child: Child,
    stdout_task: Option<JoinHandle<Result<()>>>,
    stderr_task: Option<JoinHandle<Result<()>>>,
}

struct TlsMaterial {
    cert: PathBuf,
    key: PathBuf,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    validate_local_relay_wiring(&args)?;
    let needletail_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let workspace_root = needletail_root
        .parent()
        .context("Needletail checkout has no parent directory")?;
    let default_contrib_root = workspace_root.join("av-contrib");
    let contrib_root = resolve_contrib_root(&args, &default_contrib_root)?;
    let mesh_root = resolve_mesh_root(&args, &contrib_root)?;
    let tls = resolve_tls_material(&args, &contrib_root)?;
    let rust_log = std::env::var("RUST_LOG").unwrap_or_else(|_| {
        "av_mesh=debug,av_contrib=debug,hls=debug,rtmp_ingress=debug,upload_response=debug,playlists=debug,av_web_service=debug,rist_mio=info,rist_core=info".into()
    });
    println!(
        "[orchestrator] config host={} stream_id={} part_ms={} rust_log={}",
        args.host, args.stream_id, args.part_ms, rust_log
    );
    if let Ok(value) = std::env::var("AV_LL_HLS_PART_MS") {
        println!("[orchestrator] AV_LL_HLS_PART_MS={value}");
    }

    if !args.no_build {
        run_build(
            &mesh_root,
            ["build", "--locked", "--release", "--bin", "av-mesh"],
            "av-mesh build",
        )
        .await?;
        run_build(
            &contrib_root,
            ["build", "--locked", "--release", "--bin", "av-contrib"],
            "av-contrib build",
        )
        .await?;
    }

    let mission_control_dist = resolve_mission_control_dist(&args, &needletail_root);
    if !args.no_build && !args.no_mission_control_build {
        run_mission_control_build(&needletail_root, &mission_control_dist).await?;
    }
    if mission_control_dist.join("index.html").exists() {
        println!(
            "[orchestrator] Needletail Operations assets: {}",
            mission_control_dist.display()
        );
    } else {
        println!(
            "[orchestrator] Operations setup response active at /mesh; build product assets into {}",
            mission_control_dist.display()
        );
    }

    let mesh_bin = target_release_bin(&mesh_root, "av-mesh");
    let contrib_bin = target_release_bin(&contrib_root, "av-contrib");
    ensure_executable(&mesh_bin, "av-mesh")?;
    ensure_executable(&contrib_bin, "av-contrib")?;

    let relay_plan = compile_local_relay_plan(&args)?;
    let contrib_relay_args = compiled_relay_arguments(&relay_plan, CONTRIB_NODE_ID)?;
    let primary_relay_args = compiled_relay_arguments(&relay_plan, PRIMARY_RELAY_NODE_ID)?;
    let secondary_relay_args = compiled_relay_arguments(&relay_plan, SECONDARY_RELAY_NODE_ID)?;
    let edge_relay_args = compiled_relay_arguments(&relay_plan, EDGE_NODE_ID)?;
    println!(
        "[orchestrator] compiled RelaySession graph generation={} subscription={} services={}",
        relay_plan.topology_generation,
        relay_plan.subscription_id,
        relay_plan.services.len()
    );

    let mut services = Vec::new();
    let result = async {
        services.push(
            spawn_service(
                PRIMARY_RELAY_NODE_ID,
                &mesh_bin,
                &mesh_root,
                mesh_node_args(MeshNodeLaunch {
                    region: "backbone-primary",
                    node_id: PRIMARY_RELAY_NODE_ID,
                    mesh_bind: args.us_mesh,
                    peer: None,
                    http_port: args.us_http_port,
                    fec_bind: args.uk_fec,
                    media_fec_bind: args.uk_media_fec,
                    telemetry_bind: args.us_telemetry,
                    telemetry_peers: vec![args.uk_telemetry],
                    telemetry_fec_bind: None,
                    telemetry_fec_targets: vec![args.uk_telemetry],
                    telemetry_snapshots_fec_only: true,
                    stream_id: args.stream_id,
                    part_ms: args.part_ms,
                    host: &args.host,
                    cert: &tls.cert,
                    key: &tls.key,
                    relay_arguments: primary_relay_args,
                }),
                &rust_log,
                &[(
                    "NEEDLETAIL_MISSION_CONTROL_DIST",
                    mission_control_dist.display().to_string(),
                )],
            )
            .await?,
        );
        services.push(
            spawn_service(
                SECONDARY_RELAY_NODE_ID,
                &mesh_bin,
                &mesh_root,
                mesh_node_args(MeshNodeLaunch {
                    region: "backbone-secondary",
                    node_id: SECONDARY_RELAY_NODE_ID,
                    mesh_bind: args.secondary_relay_mesh,
                    peer: None,
                    http_port: args.secondary_relay_http_port,
                    fec_bind: args.us_fec,
                    media_fec_bind: args.us_media_fec,
                    telemetry_bind: args.secondary_relay_telemetry,
                    telemetry_peers: vec![args.uk_telemetry],
                    telemetry_fec_bind: None,
                    telemetry_fec_targets: vec![args.uk_telemetry],
                    telemetry_snapshots_fec_only: true,
                    stream_id: args.stream_id,
                    part_ms: args.part_ms,
                    host: &args.host,
                    cert: &tls.cert,
                    key: &tls.key,
                    relay_arguments: secondary_relay_args,
                }),
                &rust_log,
                &[(
                    "NEEDLETAIL_MISSION_CONTROL_DIST",
                    mission_control_dist.display().to_string(),
                )],
            )
            .await?,
        );
        services.push(
            spawn_service(
                EDGE_NODE_ID,
                &mesh_bin,
                &mesh_root,
                mesh_node_args(MeshNodeLaunch {
                    region: "playback-edge",
                    node_id: EDGE_NODE_ID,
                    mesh_bind: args.uk_mesh,
                    peer: None,
                    http_port: args.uk_http_port,
                    fec_bind: args.edge_relay_primary_bind,
                    media_fec_bind: args.edge_media_fec,
                    telemetry_bind: args.uk_telemetry,
                    telemetry_peers: vec![args.us_telemetry, args.secondary_relay_telemetry],
                    telemetry_fec_bind: Some(args.uk_telemetry),
                    telemetry_fec_targets: Vec::new(),
                    telemetry_snapshots_fec_only: false,
                    stream_id: args.stream_id,
                    part_ms: args.part_ms,
                    host: &args.host,
                    cert: &tls.cert,
                    key: &tls.key,
                    relay_arguments: edge_relay_args,
                }),
                &rust_log,
                &[(
                    "NEEDLETAIL_MISSION_CONTROL_DIST",
                    mission_control_dist.display().to_string(),
                )],
            )
            .await?,
        );

        wait_for_health(
            PRIMARY_RELAY_NODE_ID,
            args.us_http_port,
            Duration::from_secs(args.health_timeout_seconds),
            &args.host,
            &mut services,
        )
        .await?;
        wait_for_health(
            SECONDARY_RELAY_NODE_ID,
            args.secondary_relay_http_port,
            Duration::from_secs(args.health_timeout_seconds),
            &args.host,
            &mut services,
        )
        .await?;
        wait_for_health(
            EDGE_NODE_ID,
            args.uk_http_port,
            Duration::from_secs(args.health_timeout_seconds),
            &args.host,
            &mut services,
        )
        .await?;

        services.push(
            spawn_service(
                "contrib",
                &contrib_bin,
                &contrib_root,
                contrib_args(&args, &tls.cert, &tls.key, contrib_relay_args),
                &rust_log,
                &[],
            )
            .await?,
        );
        wait_for_health(
            "contrib",
            args.contrib_http_port,
            Duration::from_secs(args.health_timeout_seconds),
            &args.host,
            &mut services,
        )
        .await?;

        require_https_resource(
            "contributor status snapshot",
            &args.host,
            args.contrib_http_port,
            "/api/status",
        )
        .await?;
        require_https_resource(
            "playback edge snapshot",
            &args.host,
            args.uk_http_port,
            "/api/mesh",
        )
        .await?;
        require_https_resource(
            "primary relay snapshot",
            &args.host,
            args.us_http_port,
            "/api/mesh",
        )
        .await?;
        require_https_resource(
            "secondary relay snapshot",
            &args.host,
            args.secondary_relay_http_port,
            "/api/mesh",
        )
        .await?;
        for path in [
            "/mesh",
            "/needletail-mission-control.css",
            "/needletail_mission_control.js",
            "/needletail_mission_control_bg.wasm",
        ] {
            require_https_resource("Needletail Operations", &args.host, args.uk_http_port, path)
                .await?;
        }
        require_https_resource(
            "primary relay Needletail Operations",
            &args.host,
            args.us_http_port,
            "/mesh",
        )
        .await?;
        require_https_resource(
            "secondary relay Needletail Operations",
            &args.host,
            args.secondary_relay_http_port,
            "/mesh",
        )
        .await?;

        print_ready(&args);

        if args.exit_after_ready {
            shutdown_services(&mut services).await;
            return Ok(());
        }

        supervise_until_exit_or_ctrl_c(&mut services).await
    }
    .await;

    if result.is_err() {
        shutdown_services(&mut services).await;
    }
    result
}

fn resolve_contrib_root(args: &Args, default_contrib_root: &Path) -> Result<PathBuf> {
    let root = args
        .contrib_root
        .clone()
        .or_else(|| std::env::var_os("AV_CONTRIB_ROOT").map(PathBuf::from))
        .unwrap_or_else(|| default_contrib_root.to_path_buf());
    let manifest = root.join("Cargo.toml");
    if !manifest.exists() {
        bail!(
            "could not find av-contrib Cargo.toml at {}; pass --contrib-root or set AV_CONTRIB_ROOT",
            manifest.display()
        );
    }
    Ok(root)
}

fn resolve_mesh_root(args: &Args, contrib_root: &Path) -> Result<PathBuf> {
    let root = args
        .mesh_root
        .clone()
        .or_else(|| std::env::var_os("AV_MESH_ROOT").map(PathBuf::from))
        .unwrap_or_else(|| contrib_root.join("..").join("av-mesh"));
    let manifest = root.join("Cargo.toml");
    if !manifest.exists() {
        bail!(
            "could not find av-mesh Cargo.toml at {}; pass --mesh-root or set AV_MESH_ROOT",
            manifest.display()
        );
    }
    Ok(root)
}

fn compile_local_relay_plan(args: &Args) -> Result<CompiledServicePlan> {
    local_relay_program(args)
        .compile()
        .map_err(|error| anyhow!("local RelaySession program is invalid: {error}"))
}

fn compiled_relay_arguments(plan: &CompiledServicePlan, node_id: &str) -> Result<Vec<String>> {
    plan.services
        .iter()
        .find(|service| service.node_id() == node_id)
        .map(CompiledService::relay_arguments)
        .with_context(|| format!("compiled RelaySession plan omitted {node_id}"))
}

fn local_relay_program(args: &Args) -> RelayProgram {
    fn node(node_id: &str, level: u16, role: NodeRole, region: &str, zone: &str) -> RelayNode {
        RelayNode {
            node_id: node_id.to_owned(),
            level,
            role,
            failure_domain: FailureDomain {
                provider: "local-qualification".to_owned(),
                region: region.to_owned(),
                asn: 64_500,
                zone: zone.to_owned(),
            },
        }
    }

    fn link(
        parent_node_id: &str,
        child_node_id: &str,
        role: ParentRole,
        lane: RelaySymbolLane,
        sender_bind: SocketAddr,
        receiver_bind: SocketAddr,
        via: Option<SocketAddr>,
    ) -> CarrierLink {
        CarrierLink {
            parent_node_id: parent_node_id.to_owned(),
            child_node_id: child_node_id.to_owned(),
            role,
            lane,
            sender_bind,
            sender_peer: via.unwrap_or(sender_bind),
            receiver_bind,
            receiver_target: via.unwrap_or(receiver_bind),
        }
    }

    RelayProgram {
        purpose: DeploymentPurpose::LocalQualification,
        carrier: CarrierProfile::ControlledPrivateUdp,
        subscription_id: args.relay_subscription_id,
        media_deadline_ms: args.relay_deadline_ms,
        audio_epoch_redundant_ingress: false,
        source_path_observation: None,
        secondary_path_observation: None,
        failover_policy: Some(FailoverPolicy {
            // Keep the warm lane active and sample it often enough that a 50 ms
            // media cadence can recover within the 250 ms interruption budget.
            primary_silence_ms: 100,
            primary_recovery_ms: 500,
            secondary_warm_ms: 300,
            heartbeat_ms: 25,
            lease_ms: 300,
        }),
        failover_control_links: vec![FailoverControlLink {
            forwarder_node_id: SECONDARY_RELAY_NODE_ID.to_owned(),
            controller_node_id: EDGE_NODE_ID.to_owned(),
            controller_bind: SocketAddr::from(([127, 0, 0, 1], 22_501)),
            controller_peer: SocketAddr::from(([127, 0, 0, 1], 22_501)),
            listener_bind: SocketAddr::from(([127, 0, 0, 1], 22_502)),
            listener_target: SocketAddr::from(([127, 0, 0, 1], 22_502)),
        }],
        topology: RelayTopology {
            generation: args.relay_topology_generation,
            nodes: vec![
                node(CONTRIB_NODE_ID, 0, NodeRole::Origin, "origin", "origin-a"),
                node(
                    PRIMARY_RELAY_NODE_ID,
                    1,
                    NodeRole::Backbone,
                    "primary-backbone",
                    "primary-a",
                ),
                node(
                    SECONDARY_RELAY_NODE_ID,
                    1,
                    NodeRole::Backbone,
                    "secondary-backbone",
                    "secondary-a",
                ),
                node(
                    EDGE_NODE_ID,
                    2,
                    NodeRole::PlaybackEdge,
                    "playback-edge",
                    "edge-a",
                ),
            ],
            parent_links: vec![
                ParentLink {
                    parent_node_id: CONTRIB_NODE_ID.to_owned(),
                    child_node_id: PRIMARY_RELAY_NODE_ID.to_owned(),
                    role: ParentRole::Primary,
                },
                ParentLink {
                    parent_node_id: CONTRIB_NODE_ID.to_owned(),
                    child_node_id: SECONDARY_RELAY_NODE_ID.to_owned(),
                    role: ParentRole::Primary,
                },
                ParentLink {
                    parent_node_id: PRIMARY_RELAY_NODE_ID.to_owned(),
                    child_node_id: EDGE_NODE_ID.to_owned(),
                    role: ParentRole::Primary,
                },
                ParentLink {
                    parent_node_id: SECONDARY_RELAY_NODE_ID.to_owned(),
                    child_node_id: EDGE_NODE_ID.to_owned(),
                    role: ParentRole::Secondary,
                },
            ],
            limits: TopologyLimits {
                max_origin_children: 2,
                max_downstream_children: 4,
            },
        },
        carrier_links: vec![
            link(
                CONTRIB_NODE_ID,
                PRIMARY_RELAY_NODE_ID,
                ParentRole::Primary,
                RelaySymbolLane::Source,
                args.contrib_relay_primary_bind,
                args.uk_fec,
                args.contrib_primary_via,
            ),
            link(
                CONTRIB_NODE_ID,
                SECONDARY_RELAY_NODE_ID,
                ParentRole::Primary,
                RelaySymbolLane::SourceAndRepair,
                args.contrib_relay_secondary_bind,
                args.us_fec,
                args.contrib_secondary_via,
            ),
            link(
                PRIMARY_RELAY_NODE_ID,
                EDGE_NODE_ID,
                ParentRole::Primary,
                RelaySymbolLane::Source,
                args.primary_relay_forward_bind,
                args.edge_relay_primary_bind,
                args.primary_edge_via,
            ),
            link(
                SECONDARY_RELAY_NODE_ID,
                EDGE_NODE_ID,
                ParentRole::Secondary,
                RelaySymbolLane::Repair,
                args.secondary_relay_forward_bind,
                args.uk_relay_secondary_bind,
                args.secondary_edge_via,
            ),
        ],
    }
}

fn validate_local_relay_wiring(args: &Args) -> Result<()> {
    let mut sockets = vec![
        ("primary backbone RelaySession ingress", args.uk_fec),
        ("secondary backbone RelaySession ingress", args.us_fec),
        (
            "playback edge primary RelaySession ingress",
            args.edge_relay_primary_bind,
        ),
        (
            "playback edge secondary RelaySession ingress",
            args.uk_relay_secondary_bind,
        ),
        (
            "contributor primary RelaySession source",
            args.contrib_relay_primary_bind,
        ),
        (
            "contributor secondary RelaySession source",
            args.contrib_relay_secondary_bind,
        ),
        (
            "primary backbone RelaySession forward source",
            args.primary_relay_forward_bind,
        ),
        (
            "secondary backbone RelaySession forward source",
            args.secondary_relay_forward_bind,
        ),
        (
            "playback edge failover controller",
            SocketAddr::from(([127, 0, 0, 1], 22_501)),
        ),
        (
            "secondary backbone failover listener",
            SocketAddr::from(([127, 0, 0, 1], 22_502)),
        ),
    ];
    for (name, endpoint) in [
        (
            "contributor primary carrier intermediary",
            args.contrib_primary_via,
        ),
        (
            "contributor secondary carrier intermediary",
            args.contrib_secondary_via,
        ),
        ("primary edge carrier intermediary", args.primary_edge_via),
        (
            "secondary edge carrier intermediary",
            args.secondary_edge_via,
        ),
    ] {
        if let Some(endpoint) = endpoint {
            sockets.push((name, endpoint));
        }
    }
    for (index, (left_name, left)) in sockets.iter().enumerate() {
        if let Some((right_name, _)) = sockets[index + 1..].iter().find(|(_, right)| right == left)
        {
            bail!("{left_name} and {right_name} require distinct sockets; both resolve to {left}");
        }
    }
    if args.relay_topology_generation == 0 {
        bail!("--relay-topology-generation must be positive");
    }
    if args.relay_subscription_id == 0 {
        bail!("--relay-subscription-id must be positive");
    }
    if args.relay_deadline_ms == 0 {
        bail!("--relay-deadline-ms must be positive");
    }
    compile_local_relay_plan(args)?;
    Ok(())
}

async fn run_build<I, S>(cwd: &Path, args: I, name: &str) -> Result<()>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    println!("[orchestrator] running {name} in {}", cwd.display());
    let status = Command::new("cargo")
        .args(args)
        .current_dir(cwd)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status()
        .await
        .with_context(|| format!("failed to start {name}"))?;
    if !status.success() {
        bail!("{name} failed with {status}");
    }
    Ok(())
}

fn resolve_mission_control_dist(args: &Args, needletail_root: &Path) -> PathBuf {
    args.mission_control_dist
        .clone()
        .unwrap_or_else(|| needletail_root.join("mission-control").join("dist"))
}

async fn run_mission_control_build(
    needletail_root: &Path,
    mission_control_dist: &Path,
) -> Result<()> {
    let mission_control_root = needletail_root.join("mission-control");
    let build_script = mission_control_root.join("scripts/build.sh");
    if !build_script.exists() {
        bail!(
            "Needletail Operations source is required at {}",
            build_script.display()
        );
    }
    println!(
        "[orchestrator] building Needletail Operations in {}",
        mission_control_root.display()
    );
    let status = Command::new("sh")
        .arg(&build_script)
        .arg(mission_control_dist)
        .current_dir(&mission_control_root)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status()
        .await
        .with_context(|| "failed to start Needletail Operations asset build")?;
    if !status.success() {
        bail!("Needletail Operations build failed with {status}");
    }
    Ok(())
}

fn target_release_bin(root: &Path, name: &str) -> PathBuf {
    let target_dir = std::env::var_os("CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("target"));
    target_dir
        .join("release")
        .join(format!("{}{}", name, std::env::consts::EXE_SUFFIX))
}

fn ensure_executable(path: &Path, name: &str) -> Result<()> {
    if path.exists() {
        Ok(())
    } else {
        bail!(
            "{name} release binary not found at {}; run without --no-build first",
            path.display()
        )
    }
}

fn resolve_tls_material(args: &Args, contrib_root: &Path) -> Result<TlsMaterial> {
    let default_tls_dir = contrib_root.join("..").join("tls").join(DEFAULT_HOST);
    let cert = args
        .cert
        .clone()
        .unwrap_or_else(|| default_tls_dir.join("fullchain.pem"));
    let key = args
        .key
        .clone()
        .unwrap_or_else(|| default_tls_dir.join("privkey.pem"));

    if !cert.exists() {
        bail!(
            "TLS certificate not found at {}; pass --cert or restore ../tls/{}/fullchain.pem",
            cert.display(),
            DEFAULT_HOST
        );
    }
    if !key.exists() {
        bail!(
            "TLS key not found at {}; pass --key or restore ../tls/{}/privkey.pem",
            key.display(),
            DEFAULT_HOST
        );
    }

    Ok(TlsMaterial { cert, key })
}

struct MeshNodeLaunch<'a> {
    region: &'a str,
    node_id: &'a str,
    mesh_bind: SocketAddr,
    peer: Option<SocketAddr>,
    http_port: u16,
    fec_bind: SocketAddr,
    media_fec_bind: SocketAddr,
    telemetry_bind: SocketAddr,
    telemetry_peers: Vec<SocketAddr>,
    telemetry_fec_bind: Option<SocketAddr>,
    telemetry_fec_targets: Vec<SocketAddr>,
    telemetry_snapshots_fec_only: bool,
    stream_id: u64,
    part_ms: u64,
    host: &'a str,
    cert: &'a Path,
    key: &'a Path,
    relay_arguments: Vec<String>,
}

fn mesh_node_args(launch: MeshNodeLaunch<'_>) -> Vec<String> {
    let mut args = vec![
        "--cert".into(),
        launch.cert.display().to_string(),
        "--key".into(),
        launch.key.display().to_string(),
        "--region".into(),
        launch.region.into(),
        "--node-id".into(),
        launch.node_id.into(),
        "--mesh-bind".into(),
        launch.mesh_bind.to_string(),
        "--http-port".into(),
        launch.http_port.to_string(),
        "--playback-base-url".into(),
        format!("https://{}:{}/live", launch.host, launch.http_port),
        "--fec-bind".into(),
        launch.fec_bind.to_string(),
        "--media-fec-bind".into(),
        launch.media_fec_bind.to_string(),
        "--telemetry-bind".into(),
        launch.telemetry_bind.to_string(),
        "--telemetry-dns-name".into(),
        launch.host.into(),
        "--telemetry-interval-ms".into(),
        "5000".into(),
        "--stream-id".into(),
        launch.stream_id.to_string(),
        "--part-ms".into(),
        launch.part_ms.to_string(),
        "--parts-per-segment".into(),
        "2".into(),
        "--window-parts".into(),
        "24".into(),
        "--slot-kb".into(),
        "2048".into(),
    ];
    if let Some(peer) = launch.peer {
        args.extend(["--peer".into(), peer.to_string()]);
    }
    for telemetry_peer in launch.telemetry_peers {
        args.extend(["--telemetry-peer".into(), telemetry_peer.to_string()]);
    }
    if let Some(bind) = launch.telemetry_fec_bind {
        args.extend(["--telemetry-fec-bind".into(), bind.to_string()]);
    }
    for target in launch.telemetry_fec_targets {
        args.extend(["--telemetry-fec-target".into(), target.to_string()]);
    }
    if launch.telemetry_snapshots_fec_only {
        args.push("--telemetry-snapshots-fec-only".into());
    }
    args.extend(launch.relay_arguments);
    args
}

fn contrib_args(args: &Args, cert: &Path, key: &Path, relay_arguments: Vec<String>) -> Vec<String> {
    let fec_target = args.contrib_fec_target.unwrap_or(args.uk_fec);
    let media_fec_target = args.contrib_media_fec_target.unwrap_or(args.uk_media_fec);
    let mut service_args = vec![
        "--cert".into(),
        cert.display().to_string(),
        "--key".into(),
        key.display().to_string(),
        "--http-port".into(),
        args.contrib_http_port.to_string(),
        "--mesh-fec-target".into(),
        fec_target.to_string(),
        "--mesh-media-fec-target".into(),
        media_fec_target.to_string(),
        "--stream-id".into(),
        args.stream_id.to_string(),
        "--rist-stream-id".into(),
        args.stream_id.to_string(),
        "--srt-stream-id".into(),
        args.stream_id.to_string(),
        "--rtmp-stream-id".into(),
        args.stream_id.to_string(),
        "--fmp4-part-ms".into(),
        args.part_ms.to_string(),
        "--rist-bind".into(),
        args.rist_bind.to_string(),
        "--rist-flow-id".into(),
        args.rist_flow_id.clone(),
        "--srt-bind".into(),
        args.srt_bind.to_string(),
        "--rtmp-bind".into(),
        args.rtmp_bind.to_string(),
    ];
    service_args.extend(relay_arguments);
    service_args
}

async fn spawn_service(
    name: &str,
    binary: &Path,
    cwd: &Path,
    args: Vec<String>,
    rust_log: &str,
    extra_env: &[(&str, String)],
) -> Result<Service> {
    println!(
        "[orchestrator] starting {name}: {} {}",
        binary.display(),
        args.join(" ")
    );
    println!("[orchestrator] {name} RUST_LOG={rust_log}");
    let mut command = Command::new(binary);
    command
        .args(&args)
        .current_dir(cwd)
        .env("RUST_LOG", rust_log)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    for (key, value) in extra_env {
        println!("[orchestrator] {name} {key}={value}");
        command.env(key, value);
    }
    let mut child = command
        .spawn()
        .with_context(|| format!("failed to start {name}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow!("failed to capture stdout for {name}"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow!("failed to capture stderr for {name}"))?;

    Ok(Service {
        name: name.to_owned(),
        child,
        stdout_task: Some(tokio::spawn(prefix_lines(name.to_owned(), stdout))),
        stderr_task: Some(tokio::spawn(prefix_lines(name.to_owned(), stderr))),
    })
}

async fn prefix_lines<R>(name: String, reader: R) -> Result<()>
where
    R: AsyncRead + Unpin,
{
    let mut lines = BufReader::new(reader).lines();
    while let Some(line) = lines.next_line().await? {
        println!("[{name}] {line}");
    }
    Ok(())
}

async fn wait_for_health(
    name: &str,
    port: u16,
    timeout_duration: Duration,
    host: &str,
    services: &mut [Service],
) -> Result<()> {
    let deadline = Instant::now() + timeout_duration;
    let url = format!("https://{host}:{port}/up");
    let mut attempt: u64 = 0;
    while Instant::now() < deadline {
        attempt = attempt.saturating_add(1);
        if let Some((service, status)) = first_exited(services)? {
            bail!("{service} exited while waiting for {name} health: {status}");
        }
        println!("[orchestrator] health check attempt {attempt} for {name}: {url}");
        if curl_ok(host, port, &url).await {
            println!("[orchestrator] {name} healthy at {url}");
            return Ok(());
        }
        sleep(Duration::from_millis(250)).await;
    }
    bail!("{name} did not become healthy at {url}");
}

async fn curl_ok(host: &str, port: u16, url: &str) -> bool {
    let resolve = format!("{host}:{port}:127.0.0.1");
    match Command::new("curl")
        .args(["-fs", "--resolve", &resolve, url])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
    {
        Ok(status) => status.success(),
        Err(_) => false,
    }
}

async fn require_https_resource(label: &str, host: &str, port: u16, path: &str) -> Result<()> {
    let url = format!("https://{host}:{port}{path}");
    if curl_ok(host, port, &url).await {
        println!("[orchestrator] verified {label}: {url}");
        Ok(())
    } else {
        bail!("{label} did not return a successful response at {url}")
    }
}

fn first_exited(services: &mut [Service]) -> Result<Option<(String, ExitStatus)>> {
    for service in services {
        if let Some(status) = service
            .child
            .try_wait()
            .with_context(|| format!("failed to poll {}", service.name))?
        {
            return Ok(Some((service.name.clone(), status)));
        }
    }
    Ok(None)
}

fn print_ready(args: &Args) {
    println!();
    println!("[orchestrator] Needletail local stack ready");
    println!(
        "[orchestrator] OBS RTMP server: rtmp://{}:{}/live",
        args.host,
        args.rtmp_bind.port()
    );
    println!(
        "[orchestrator] OBS RTMP stream key: {}",
        args.rtmp_stream_key
    );
    println!(
        "[orchestrator] OBS SRT caller URL: srt://{}:{}?mode=caller",
        args.host,
        args.srt_bind.port()
    );
    println!(
        "[orchestrator] RIST URL: rist://{}:{} profile=main flow_id={}",
        args.host,
        args.rist_bind.port(),
        args.rist_flow_id
    );
    println!(
        "[orchestrator] playback edge: https://{}:{}/live/{}/stream.m3u8",
        args.host, args.uk_http_port, args.stream_id
    );
    println!(
        "[orchestrator] primary relay status: https://{}:{}/api/mesh",
        args.host, args.us_http_port
    );
    println!(
        "[orchestrator] warm relay status: https://{}:{}/api/mesh",
        args.host, args.secondary_relay_http_port
    );
    println!(
        "[orchestrator] contrib status: https://{}:{}/api/status",
        args.host, args.contrib_http_port
    );
    println!(
        "[orchestrator] contrib events: https://{}:{}/api/status/events",
        args.host, args.contrib_http_port
    );
    println!(
        "[orchestrator] RelaySession source lane: {} -> {} -> {} -> {}",
        args.contrib_relay_primary_bind,
        args.uk_fec,
        args.primary_relay_forward_bind,
        args.edge_relay_primary_bind
    );
    println!(
        "[orchestrator] RelaySession warm repair lane: {} -> {} -> {} -> {}",
        args.contrib_relay_secondary_bind,
        args.us_fec,
        args.secondary_relay_forward_bind,
        args.uk_relay_secondary_bind
    );
    println!(
        "[orchestrator] RelaySession desired state: generation={} subscription={}",
        args.relay_topology_generation, args.relay_subscription_id
    );
    println!(
        "[orchestrator] LL-HLS part target: {}ms (override with AV_LL_HLS_PART_MS or --part-ms)",
        args.part_ms
    );
    println!(
        "[orchestrator] LL-HLS tail path for stream {}: /live/{}/tail?mode=part",
        args.stream_id, args.stream_id
    );
    println!(
        "[orchestrator] Operations: https://{}:{}/mesh",
        args.host, args.uk_http_port
    );
    println!(
        "[orchestrator] primary relay Operations: https://{}:{}/mesh",
        args.host, args.us_http_port
    );
    println!(
        "[orchestrator] warm relay Operations: https://{}:{}/mesh",
        args.host, args.secondary_relay_http_port
    );
    println!("[orchestrator] logs from all services are prefixed below");
    println!();
}

async fn supervise_until_exit_or_ctrl_c(services: &mut [Service]) -> Result<()> {
    loop {
        tokio::select! {
            signal = tokio::signal::ctrl_c() => {
                signal.context("failed to wait for ctrl-c")?;
                println!("[orchestrator] ctrl-c received, stopping services");
                shutdown_services(services).await;
                return Ok(());
            }
            _ = sleep(Duration::from_millis(250)) => {
                if let Some((service, status)) = first_exited(services)? {
                    println!("[orchestrator] {service} exited with {status}, stopping stack");
                    shutdown_services(services).await;
                    if status.success() {
                        return Ok(());
                    }
                    bail!("{service} exited with {status}");
                }
            }
        }
    }
}

async fn shutdown_services(services: &mut [Service]) {
    for service in services.iter_mut() {
        match service.child.try_wait() {
            Ok(Some(_)) => {}
            Ok(None) => {
                println!("[orchestrator] stopping {}", service.name);
                let _ = service.child.start_kill();
            }
            Err(_) => {}
        }
    }

    for service in services.iter_mut() {
        match timeout(Duration::from_secs(5), service.child.wait()).await {
            Ok(Ok(status)) => println!("[orchestrator] {} stopped with {status}", service.name),
            Ok(Err(error)) => println!(
                "[orchestrator] failed waiting for {}: {error}",
                service.name
            ),
            Err(_) => println!("[orchestrator] timed out waiting for {}", service.name),
        }
    }

    for service in services.iter_mut() {
        if let Some(task) = service.stdout_task.take() {
            let _ = task.await;
        }
        if let Some(task) = service.stderr_task.take() {
            let _ = task.await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_args() -> Args {
        Args::try_parse_from(["needletail"]).expect("default local arguments")
    }

    fn value_after<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
        args.windows(2)
            .find(|pair| pair[0] == flag)
            .map(|pair| pair[1].as_str())
    }

    #[test]
    fn local_compiler_emits_source_seeded_dual_parent_dag() {
        let args = default_args();
        validate_local_relay_wiring(&args).expect("valid fixed local relay sockets");
        let plan = compile_local_relay_plan(&args).expect("compile local plan");
        assert_eq!(plan.services.len(), 4);

        let contrib_relay_args =
            compiled_relay_arguments(&plan, CONTRIB_NODE_ID).expect("contributor relay args");
        let contrib_args = contrib_args(
            &args,
            Path::new("cert.pem"),
            Path::new("key.pem"),
            contrib_relay_args,
        );
        assert_eq!(
            value_after(&contrib_args, "--relay-primary-bind"),
            Some("127.0.0.1:22301")
        );
        assert_eq!(
            value_after(&contrib_args, "--relay-primary-target"),
            Some("127.0.0.1:22001")
        );
        assert_eq!(
            value_after(&contrib_args, "--relay-secondary-bind"),
            Some("127.0.0.1:22302")
        );
        assert_eq!(
            value_after(&contrib_args, "--relay-secondary-target"),
            Some("127.0.0.1:22002")
        );
        assert!(contrib_args.contains(&"--relay-secondary-seed-source".to_owned()));
        assert!(contrib_args.contains(&"--relay-exclusive".to_owned()));

        let primary =
            compiled_relay_arguments(&plan, PRIMARY_RELAY_NODE_ID).expect("primary relay args");
        assert_eq!(
            value_after(&primary, "--relay-primary-bind"),
            Some("127.0.0.1:22001")
        );
        assert!(primary.contains(&"127.0.0.1:22401=127.0.0.1:22200,source".to_owned()));

        let secondary =
            compiled_relay_arguments(&plan, SECONDARY_RELAY_NODE_ID).expect("secondary relay args");
        assert_eq!(
            value_after(&secondary, "--relay-primary-bind"),
            Some("127.0.0.1:22002")
        );
        assert!(secondary.contains(&"--relay-primary-promoted".to_owned()));
        assert!(secondary.contains(&"127.0.0.1:22402=127.0.0.1:22201,repair".to_owned()));

        let edge = compiled_relay_arguments(&plan, EDGE_NODE_ID).expect("edge relay args");
        assert_eq!(
            value_after(&edge, "--relay-primary-bind"),
            Some("127.0.0.1:22200")
        );
        assert_eq!(
            value_after(&edge, "--relay-primary-peer"),
            Some("127.0.0.1:22401")
        );
        assert_eq!(
            value_after(&edge, "--relay-secondary-bind"),
            Some("127.0.0.1:22201")
        );
        assert_eq!(
            value_after(&edge, "--relay-secondary-peer"),
            Some("127.0.0.1:22402")
        );
    }

    #[test]
    fn local_qualification_disables_legacy_mesh_peer_forwarding() {
        let args = default_args();
        let plan = compile_local_relay_plan(&args).expect("compile local plan");
        let mesh_args = mesh_node_args(MeshNodeLaunch {
            region: "playback-edge",
            node_id: EDGE_NODE_ID,
            mesh_bind: args.uk_mesh,
            peer: None,
            http_port: args.uk_http_port,
            fec_bind: args.edge_relay_primary_bind,
            media_fec_bind: args.edge_media_fec,
            telemetry_bind: args.uk_telemetry,
            telemetry_peers: vec![args.us_telemetry, args.secondary_relay_telemetry],
            telemetry_fec_bind: Some(args.uk_telemetry),
            telemetry_fec_targets: Vec::new(),
            telemetry_snapshots_fec_only: false,
            stream_id: args.stream_id,
            part_ms: args.part_ms,
            host: &args.host,
            cert: Path::new("cert.pem"),
            key: Path::new("key.pem"),
            relay_arguments: compiled_relay_arguments(&plan, EDGE_NODE_ID)
                .expect("edge relay arguments"),
        });
        assert!(!mesh_args.iter().any(|arg| arg == "--peer"));
        assert!(mesh_args
            .iter()
            .any(|arg| arg == "--relay-controlled-local"));
        assert_eq!(
            value_after(&mesh_args, "--telemetry-interval-ms"),
            Some("5000")
        );
        assert_eq!(
            value_after(&mesh_args, "--telemetry-fec-bind"),
            Some("127.0.0.1:27300")
        );
    }

    #[test]
    fn compiled_carriers_preserve_explicit_impairment_intermediaries() {
        let args = Args::try_parse_from([
            "needletail",
            "--contrib-primary-via",
            "127.0.0.1:22901",
            "--contrib-secondary-via",
            "127.0.0.1:22902",
            "--primary-edge-via",
            "127.0.0.1:22903",
            "--secondary-edge-via",
            "127.0.0.1:22904",
        ])
        .expect("carrier intermediaries");
        let plan = compile_local_relay_plan(&args).expect("compile intermediary plan");

        let contrib = compiled_relay_arguments(&plan, CONTRIB_NODE_ID).expect("contrib");
        assert_eq!(
            value_after(&contrib, "--relay-primary-target"),
            Some("127.0.0.1:22901")
        );
        assert_eq!(
            value_after(&contrib, "--relay-secondary-target"),
            Some("127.0.0.1:22902")
        );

        let primary = compiled_relay_arguments(&plan, PRIMARY_RELAY_NODE_ID).expect("primary");
        assert_eq!(
            value_after(&primary, "--relay-primary-peer"),
            Some("127.0.0.1:22901")
        );
        assert!(primary.contains(&"127.0.0.1:22401=127.0.0.1:22903,source".to_owned()));

        let edge = compiled_relay_arguments(&plan, EDGE_NODE_ID).expect("edge");
        assert_eq!(
            value_after(&edge, "--relay-primary-peer"),
            Some("127.0.0.1:22903")
        );
        assert_eq!(
            value_after(&edge, "--relay-secondary-peer"),
            Some("127.0.0.1:22904")
        );
    }

    #[test]
    fn local_relay_wiring_rejects_socket_aliases_and_zero_generations() {
        let collision =
            Args::try_parse_from(["needletail", "--uk-relay-secondary-bind", "127.0.0.1:22001"])
                .expect("parse collision");
        assert!(validate_local_relay_wiring(&collision)
            .expect_err("socket alias must fail")
            .to_string()
            .contains("distinct sockets"));

        let zero_generation =
            Args::try_parse_from(["needletail", "--relay-topology-generation", "0"])
                .expect("parse zero generation");
        assert!(validate_local_relay_wiring(&zero_generation)
            .expect_err("zero generation must fail")
            .to_string()
            .contains("must be positive"));
    }
}
