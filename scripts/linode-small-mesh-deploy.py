#!/usr/bin/env python3
"""Deploy the Needletail mesh onto the small-instance Linode lab.

The relay DAG (contributor -> two backbone relays -> regional distributors) is
described by a generated relay program and compiled by `needletail-compile`,
exactly as the larger qualification labs do. Regional playback edges are not
DAG members: they join their region's LL-HLS cache mesh over the private VLAN
and receive cache objects from the two regional distributors.

Subcommands:
  plan      measure routes, generate the relay program, compile it, and render
            one argument file per service under the artifact directory
  push      bootstrap hosts, install binaries, TLS, and services, then restart
  restart   re-render arguments from current settings and restart services
  status    show service state, clock offset, and cache-mesh role per node

Tuning knobs live in the settings file and can be changed with
`--set key=value` on plan, push, or restart.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import small_mesh_common as common  # noqa: E402

SETTINGS_FILE = common.ARTIFACT_DIR / "settings.json"
DEFAULT_SETTINGS = {
    "part_ms": 5,
    "parts_per_segment": 200,
    "window_parts": 2400,
    "slot_kb": 2048,
    "response_ms": None,
    "mesh_sync_interval_ms": 20,
    "mesh_repair_ratio": 0.03,
    "mesh_repair_symbols": 1,
    "mesh_max_repair_symbols": 32,
    "mesh_symbol_size": 1316,
    "mesh_max_symbol_size": 0,
    "cache_mesh_max_parents": 2,
    "cache_mesh_max_children": 8,
    # Regional cache features: a distributor pushes a playlist to an edge only
    # while the edge has readers for it ("demand"), or always ("all").
    "cache_mesh_replication": "demand",
    "cache_mesh_lease_ms": 5000,
    "cache_mesh_demand_idle_ms": 30000,
    "cache_mesh_warm_parts": 0,
    "cache_mesh_warm_kbps": 20000,
    "cache_mesh_socket_buffer_bytes": 0,
    # Limits of one edge. They are what admission and assignment enforce.
    "edge_egress_capacity_bps": None,
    "edge_session_limit": 0,
    # Address in the playback URL that a cache advertises: "public", or "vlan"
    # when readers reach the caches over the private network.
    "playback_address": "public",
    "assignment_spread_percent": 60,
    "assignment_alternates": 2,
    "steering_ttl_seconds": 5,
    "telemetry_interval_ms": 1000,
    # Kernel settings of a node that serves readers. A reader far from its
    # cache gets a 4K playlist only when the cache finds the rate of the path
    # quickly: CUBIC leaves slow start early on a path with jitter and then
    # needs ten seconds to reach 16 Mbit/s. A live stream also pauses between
    # parts, and the window must not close in each pause.
    "node_sysctls": {
        "net.ipv4.tcp_congestion_control": "bbr",
        "net.ipv4.tcp_slow_start_after_idle": "0",
    },
    "path_loss_ppm": 20000,
    "relay_min_repair_symbols": 0,
    "failover_primary_silence_ms": 100,
    "failover_primary_recovery_ms": 500,
    "failover_secondary_warm_ms": 300,
    "failover_heartbeat_ms": 25,
    "failover_lease_ms": 300,
    "media_deadline_ms": 1000,
    "fmp4_part_ms": 200,
    # Part and segment duration of a stream with video. None uses
    # `fmp4_part_ms` and the default segment of the contributor. With them one
    # contributor carries 5 ms audio parts beside video parts of several
    # frames. Set the segment to the key-frame interval of the encoder.
    "fmp4_video_part_ms": None,
    "fmp4_video_segment_ms": None,
    "daw_hls_packaging": "opaque",
    "daw_hls_formats": "flac,opus",
    "daw_hls_base_stream_id": 2001,
    "tokio_worker_threads": None,
    "egress_capacity_bps": 1000000000,
    "extra_distributor_args": [],
    "extra_edge_args": [],
    "extra_backbone_args": [],
    "extra_contributor_args": [],
    "rust_log_mesh": "av_mesh=info,av_web_service=warn",
    "rust_log_contrib": "av_contrib=info,av_web_service=warn",
}

SITES = {
    "london": ("eu", 51.5074, -0.1278),
    "amsterdam": ("eu", 52.3676, 4.9041),
    "frankfurt": ("eu", 50.1109, 8.6821),
    "newyork": ("na", 40.7128, -74.0060),
    "tokyo": ("apac", 35.6762, 139.6503),
    "osaka": ("apac", 34.6937, 135.5023),
    "sydney": ("apac", -33.8688, 151.2093),
    "singapore": ("apac", 1.3521, 103.8198),
    "saopaulo": ("sa", -23.5505, -46.6333),
    "losangeles": ("na", 34.0522, -118.2437),
    "mumbai": ("apac", 19.0760, 72.8777),
}

MESH_PORT = 29101
HTTP_PORT = 19444
CONTRIB_HTTP_PORT = 19443
TELEMETRY_PORT = 27300
MEDIA_FEC_PORT = 22101
DAW_MEDIA_PORT = 27100


def load_settings(overrides: list[str]) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.is_file():
        settings.update(json.loads(SETTINGS_FILE.read_text()))
    for override in overrides:
        key, _, raw = override.partition("=")
        if key not in DEFAULT_SETTINGS:
            sys.exit(f"unknown setting: {key}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        settings[key] = value
    common.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    return settings


def relay_node_id(node: dict) -> str:
    if node["role"] == "contributor":
        return "contrib"
    return node["name"]


def link_addresses(a: dict, b: dict) -> tuple[str, str]:
    """Private VLAN addresses when both ends share a site VLAN, else public."""
    if a["site"] == b["site"] and a.get("vlan_ipv4") and b.get("vlan_ipv4"):
        return a["vlan_ipv4"], b["vlan_ipv4"]
    return a["public_ipv4"], b["public_ipv4"]


def measure_rtt_us(source: dict, target_ip: str, count: int = 7) -> tuple[int, int]:
    output = common.ssh_text(
        source, f"ping -q -c {count} -i 0.2 -W 2 {target_ip}", timeout=60
    )
    for line in output.splitlines():
        if line.startswith("rtt "):
            values = line.split("=")[1].strip().split()[0].split("/")
            return int(float(values[1]) * 1000), int(float(values[3]) * 1000)
    raise RuntimeError(f"could not measure {source['name']} path to {target_ip}")


def backbone(state: dict) -> tuple[dict, dict]:
    relays = common.nodes_by_role(state, "backbone")
    primary = [node for node in relays if node["name"].endswith("primary")]
    secondary = [node for node in relays if node["name"].endswith("secondary")]
    if len(primary) != 1 or len(secondary) != 1:
        sys.exit("the lab needs exactly one *-primary and one *-secondary backbone")
    return primary[0], secondary[0]


def build_program(state: dict, settings: dict, routes: dict) -> dict:
    contributors = common.nodes_by_role(state, "contributor")
    if len(contributors) != 1:
        sys.exit("the lab needs exactly one contributor")
    contrib = contributors[0]
    primary, secondary = backbone(state)
    distributors = common.nodes_by_role(state, "distributor")
    if not distributors:
        sys.exit("the lab needs at least one distributor")

    def domain(node: dict) -> dict:
        return {"provider": "linode", "region": node["region"], "asn": 63949,
                "zone": node["region"]}

    topology_nodes = [
        {"node_id": "contrib", "level": 0, "role": "origin",
         "failure_domain": domain(contrib)},
        {"node_id": relay_node_id(primary), "level": 1, "role": "backbone",
         "failure_domain": domain(primary)},
        {"node_id": relay_node_id(secondary), "level": 1, "role": "backbone",
         "failure_domain": domain(secondary)},
    ]
    parent_links = [
        {"parent_node_id": "contrib", "child_node_id": relay_node_id(primary),
         "role": "primary"},
        {"parent_node_id": "contrib", "child_node_id": relay_node_id(secondary),
         "role": "primary"},
    ]
    contrib_to_primary = link_addresses(contrib, primary)
    contrib_to_secondary = link_addresses(contrib, secondary)
    carrier_links = [
        {"parent_node_id": "contrib", "child_node_id": relay_node_id(primary),
         "role": "primary", "lane": "source",
         "sender_bind": f"{contrib_to_primary[0]}:22301",
         "sender_peer": f"{contrib_to_primary[0]}:22301",
         "receiver_bind": "0.0.0.0:22001",
         "receiver_target": f"{contrib_to_primary[1]}:22001"},
        {"parent_node_id": "contrib", "child_node_id": relay_node_id(secondary),
         "role": "primary", "lane": "source_and_repair",
         "sender_bind": f"{contrib_to_secondary[0]}:22302",
         "sender_peer": f"{contrib_to_secondary[0]}:22302",
         "receiver_bind": "0.0.0.0:22002",
         "receiver_target": f"{contrib_to_secondary[1]}:22002"},
    ]
    failover_links = []
    hunted = load_hunted_ports()

    def sender_port(parent: dict, node: dict, default: int) -> int:
        entry = hunted.get(f"{relay_node_id(parent)}->{relay_node_id(node)}")
        return entry["port"] if entry else default

    for index, node in enumerate(distributors):
        node_id = relay_node_id(node)
        offset = index * 10
        topology_nodes.append({"node_id": node_id, "level": 2,
                               "role": "playback_edge",
                               "failure_domain": domain(node)})
        parent_links.append({"parent_node_id": relay_node_id(primary),
                             "child_node_id": node_id, "role": "primary"})
        parent_links.append({"parent_node_id": relay_node_id(secondary),
                             "child_node_id": node_id, "role": "secondary"})
        primary_pair = link_addresses(primary, node)
        secondary_pair = link_addresses(secondary, node)
        carrier_links.append({
            "parent_node_id": relay_node_id(primary), "child_node_id": node_id,
            "role": "primary", "lane": "source",
            "sender_bind": f"{primary_pair[0]}:{sender_port(primary, node, 22401 + offset)}",
            "sender_peer": f"{primary_pair[0]}:{sender_port(primary, node, 22401 + offset)}",
            "receiver_bind": f"0.0.0.0:{22200 + offset}",
            "receiver_target": f"{primary_pair[1]}:{22200 + offset}"})
        carrier_links.append({
            "parent_node_id": relay_node_id(secondary), "child_node_id": node_id,
            "role": "secondary", "lane": "repair",
            "sender_bind": f"{secondary_pair[0]}:{sender_port(secondary, node, 22402 + offset)}",
            "sender_peer": f"{secondary_pair[0]}:{sender_port(secondary, node, 22402 + offset)}",
            "receiver_bind": f"0.0.0.0:{22201 + offset}",
            "receiver_target": f"{secondary_pair[1]}:{22201 + offset}"})
        failover_links.append({
            "forwarder_node_id": relay_node_id(secondary),
            "controller_node_id": node_id,
            "controller_bind": f"{secondary_pair[1]}:{22501 + offset}",
            "controller_peer": f"{secondary_pair[1]}:{22501 + offset}",
            "listener_bind": f"{secondary_pair[0]}:{22502 + offset}",
            "listener_target": f"{secondary_pair[0]}:{22502 + offset}"})

    def observation(rtt_us: int, jitter_us: int, loss_ppm: int) -> dict:
        return {
            "source": "linode-icmp-route-probe+controlled-loss-profile",
            "observed_at_unix_ms": int(time.time() * 1000),
            "best_direct_rtt_us": routes["best_direct_rtt_us"],
            "rtt_us": rtt_us, "jitter_us": jitter_us,
            "loss_ppm": loss_ppm, "queue_delay_us": 0,
        }

    return {
        "purpose": "single_provider_qualification",
        "carrier": "controlled_public_udp",
        "subscription_id": 1,
        "media_deadline_ms": settings["media_deadline_ms"],
        "audio_epoch_redundant_ingress": True,
        "source_path_observation": observation(
            routes["primary_rtt_us"], routes["primary_jitter_us"],
            settings["path_loss_ppm"]),
        "secondary_path_observation": observation(
            routes["secondary_rtt_us"], routes["secondary_jitter_us"], 0),
        "failover_policy": {
            "primary_silence_ms": settings["failover_primary_silence_ms"],
            "primary_recovery_ms": settings["failover_primary_recovery_ms"],
            "secondary_warm_ms": settings["failover_secondary_warm_ms"],
            "heartbeat_ms": settings["failover_heartbeat_ms"],
            "lease_ms": settings["failover_lease_ms"],
        },
        "failover_control_links": failover_links,
        "topology": {
            "generation": 1,
            "nodes": topology_nodes,
            "parent_links": parent_links,
            "limits": {"max_origin_children": 2,
                       "max_downstream_children": max(4, len(distributors))},
        },
        "carrier_links": carrier_links,
    }


HUNT_FILE = common.ARTIFACT_DIR / "ecmp-ports.json"
HUNT_CANDIDATES = 40

HUNT_RECEIVER = r"""
import json, socket, struct, sys, time
port, seconds = int(sys.argv[1]), float(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", port)); sock.settimeout(0.5)
best = {}
deadline = time.time() + seconds
while time.time() < deadline:
    try:
        data, _ = sock.recvfrom(64)
    except OSError:
        continue
    arrival = time.time_ns()
    source_port, sent = struct.unpack("!IQ", data[:12])
    delay = (arrival - sent) / 1e6
    if source_port not in best or delay < best[source_port]:
        best[source_port] = delay
print(json.dumps(best))
"""

HUNT_SENDER = r"""
import socket, struct, sys, time
bind, target, port, first, count = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
for source_port in range(first, first + count):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((bind, source_port))
    except OSError:
        continue
    for _ in range(8):
        sock.sendto(struct.pack("!IQ", source_port, time.time_ns()), (target, port))
        time.sleep(0.01)
    sock.close()
"""


def load_hunted_ports() -> dict:
    return json.loads(HUNT_FILE.read_text()) if HUNT_FILE.is_file() else {}


def command_hunt(args: argparse.Namespace) -> None:
    """Pick, per public relay-to-distributor link, the UDP source port whose
    ECMP path has the lowest one-way delay to the real receiver port.

    Routers hash the 5-tuple, so the source port alone selects among parallel
    long-haul paths that differ by several milliseconds. Clocks are chrony
    synchronised, so receiver arrival minus sender timestamp ranks the paths.
    The distributor's mesh service is stopped briefly to free its ingest ports.
    """
    state = common.load_state()
    primary, secondary = backbone(state)
    distributors = common.nodes_by_role(state, "distributor")
    for node in distributors + [primary, secondary]:
        script = HUNT_RECEIVER if node["role"] == "distributor" else HUNT_SENDER
        common.ssh(node, f"cat >/tmp/needletail-hunt.py <<'PY'\n{script}\nPY")
    links = []
    for index, node in enumerate(distributors):
        for lane, (parent, receiver_port) in enumerate((
                (primary, 22200 + index * 10), (secondary, 22201 + index * 10))):
            pair = link_addresses(parent, node)
            if pair[0] == parent.get("vlan_ipv4"):
                continue  # same-site VLAN link: one L2 path, nothing to hunt
            links.append({
                "key": f"{relay_node_id(parent)}->{relay_node_id(node)}",
                "parent": parent, "node": node, "bind": pair[0], "target": pair[1],
                "receiver_port": receiver_port,
                "first": 24000 + (index * 2 + lane) * 64,
                "default": (22401 if lane == 0 else 22402) + index * 10,
            })

    def hunt_distributor(node: dict) -> dict:
        mine = [link for link in links if link["node"]["name"] == node["name"]]
        if not mine:
            return {}
        start = "systemctl stop needletail-mesh.service; sleep 0.5; " + " ".join(
            f"(python3 /tmp/needletail-hunt.py {link['receiver_port']} 14 "
            f">/tmp/needletail-hunt-{link['receiver_port']}.json 2>/dev/null &);"
            for link in mine)
        common.ssh(node, start)
        time.sleep(1.5)
        try:
            for link in mine:
                # The default port is probed too, as the comparison baseline.
                common.ssh(link["parent"], (
                    f"python3 /tmp/needletail-hunt.py {link['bind']} {link['target']} "
                    f"{link['receiver_port']} {link['first']} {HUNT_CANDIDATES}; "
                    f"python3 /tmp/needletail-hunt.py {link['bind']} {link['target']} "
                    f"{link['receiver_port']} {link['default'] + 2000} 1"), timeout=120)
            time.sleep(13)
            found = {}
            for link in mine:
                raw = json.loads(common.ssh_text(
                    node, f"cat /tmp/needletail-hunt-{link['receiver_port']}.json") or "{}")
                delays = {int(port): delay for port, delay in raw.items()}
                baseline = delays.pop(link["default"] + 2000, None)
                if not delays:
                    continue
                ordered = sorted(delays.values())
                best_port = min(delays, key=delays.get)
                found[link["key"]] = {
                    "port": best_port, "one_way_ms": round(delays[best_port], 3),
                    "candidates": len(delays),
                    "median_ms": round(ordered[len(ordered) // 2], 3),
                    "worst_ms": round(ordered[-1], 3),
                    "reference_port_ms": None if baseline is None else round(baseline, 3),
                }
            return found
        finally:
            common.ssh(node, "systemctl start needletail-mesh.service", check=False)

    results = common.parallel(distributors, hunt_distributor, workers=4)
    hunted = {key: value for found in results.values() for key, value in found.items()}
    HUNT_FILE.write_text(json.dumps(hunted, indent=2, sort_keys=True) + "\n")
    for key in sorted(hunted):
        item = hunted[key]
        print(f"{key:<34} port {item['port']}  best {item['one_way_ms']:.2f} ms  "
              f"median {item['median_ms']:.2f}  worst {item['worst_ms']:.2f}  "
              f"({item['candidates']} ports)")
    print("run `restart` to apply the selected source ports")


def measure_routes(state: dict) -> dict:
    """Route seeds for the adaptive RaptorQ controller: the farthest distributor."""
    contrib = common.nodes_by_role(state, "contributor")[0]
    primary, secondary = backbone(state)
    distributors = common.nodes_by_role(state, "distributor")

    def route(node: dict) -> dict:
        direct, _ = measure_rtt_us(contrib, node["public_ipv4"])
        primary_leg, primary_jitter = measure_rtt_us(
            primary, link_addresses(primary, node)[1])
        secondary_leg, secondary_jitter = measure_rtt_us(
            secondary, link_addresses(secondary, node)[1])
        return {"direct_rtt_us": direct, "primary_leg_rtt_us": primary_leg,
                "primary_leg_jitter_us": primary_jitter,
                "secondary_leg_rtt_us": secondary_leg,
                "secondary_leg_jitter_us": secondary_jitter}

    per_distributor = common.parallel(distributors, route)
    contrib_primary, contrib_primary_jitter = measure_rtt_us(
        contrib, link_addresses(contrib, primary)[1])
    contrib_secondary, contrib_secondary_jitter = measure_rtt_us(
        contrib, link_addresses(contrib, secondary)[1])
    for name, values in per_distributor.items():
        values["primary_route_rtt_us"] = contrib_primary + values["primary_leg_rtt_us"]
        values["secondary_route_rtt_us"] = (
            contrib_secondary + values["secondary_leg_rtt_us"])
    farthest = max(per_distributor.values(), key=lambda v: v["primary_route_rtt_us"])
    return {
        "measured_unix": int(time.time()),
        "contrib_primary_rtt_us": contrib_primary,
        "contrib_secondary_rtt_us": contrib_secondary,
        "best_direct_rtt_us": farthest["direct_rtt_us"],
        "primary_rtt_us": farthest["primary_route_rtt_us"],
        "primary_jitter_us": contrib_primary_jitter + farthest["primary_leg_jitter_us"],
        "secondary_rtt_us": farthest["secondary_route_rtt_us"],
        "secondary_jitter_us": (
            contrib_secondary_jitter + farthest["secondary_leg_jitter_us"]),
        "distributors": per_distributor,
    }


def compile_plan(program: dict) -> dict:
    program_path = common.ARTIFACT_DIR / "relay-program.json"
    plan_path = common.ARTIFACT_DIR / "compiled-plan.json"
    program_path.write_text(json.dumps(program, indent=2) + "\n")
    compiler = common.ROOT / "target" / "release" / "needletail-compile"
    if not compiler.is_file():
        sys.exit("build needletail-compile first: cargo build --release --bin needletail-compile")
    result = subprocess.run(
        [str(compiler), "--program", str(program_path), "--pretty"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(f"relay program did not compile:\n{result.stderr}")
    plan_path.write_text(result.stdout)
    return json.loads(result.stdout)


def geo_args(node: dict) -> list[str]:
    continent, latitude, longitude = SITES[node["site"]]
    return ["--region", node["region"], "--continent", continent,
            f"--latitude={latitude}", f"--longitude={longitude}"]


def common_mesh_args(node: dict, settings: dict, state: dict) -> list[str]:
    site_mates = [
        other for other in state["nodes"].values()
        if other["site"] == node["site"] and other["name"] != node["name"]
        and other["role"] in ("distributor", "edge")
    ]
    private_ip = node.get("vlan_ipv4") or node["public_ipv4"]
    playback_ip = (private_ip if settings["playback_address"] == "vlan"
                   else node["public_ipv4"])
    args = [
        "/usr/local/bin/av-mesh",
        "--cert", "/etc/needletail/tls/fullchain.pem",
        "--key", "/etc/needletail/tls/privkey.pem",
        *geo_args(node),
        "--node-id", relay_node_id(node),
        "--http-port", str(HTTP_PORT),
        "--playback-base-url", f"https://{playback_ip}:{HTTP_PORT}/live",
        "--media-fec-bind", f"0.0.0.0:{MEDIA_FEC_PORT}",
        "--telemetry-bind", f"0.0.0.0:{TELEMETRY_PORT}",
        "--telemetry-dns-name", common.TLS_SERVER_NAME,
        "--telemetry-private-ipv4", private_ip,
        "--telemetry-interval-ms", str(settings["telemetry_interval_ms"]),
        "--stream-id", "1",
        "--part-ms", str(settings["part_ms"]),
        "--parts-per-segment", str(settings["parts_per_segment"]),
        "--window-parts", str(settings["window_parts"]),
        "--slot-kb", str(settings["slot_kb"]),
    ]
    capacity = settings["egress_capacity_bps"]
    if node["role"] == "edge":
        capacity = settings["edge_egress_capacity_bps"] or capacity
        args += ["--edge-session-limit", str(settings["edge_session_limit"])]
    args += ["--egress-capacity-bps", str(capacity)]
    if node["role"] in ("distributor", "edge"):
        # Every node of a region can be an entry point for its readers.
        args += [
            "--assignment-spread-percent", str(settings["assignment_spread_percent"]),
            "--assignment-alternates", str(settings["assignment_alternates"]),
            "--steering-ttl-seconds", str(settings["steering_ttl_seconds"]),
        ]
    if settings["response_ms"]:
        args += ["--response-ms", str(settings["response_ms"])]
    if node["role"] in ("distributor", "edge"):
        for mate in sorted(site_mates, key=lambda item: item["name"]):
            mate_ip = mate.get("vlan_ipv4") or mate["public_ipv4"]
            args += ["--telemetry-peer", f"{mate_ip}:{TELEMETRY_PORT}"]
    return args


def cache_mesh_args(node: dict, settings: dict, state: dict) -> list[str]:
    """Regional cache plane: distributors push to edges over the site VLAN."""
    if not node.get("vlan_ipv4"):
        return ["--mesh-bind", f"0.0.0.0:{MESH_PORT}"]
    args = [
        "--mesh-bind", f"{node['vlan_ipv4']}:{MESH_PORT}",
        "--cache-mesh-role", node["role"],
        "--cache-mesh-region", f"{node['region']}-cache",
        "--cache-mesh-max-parents", str(settings["cache_mesh_max_parents"]),
        "--cache-mesh-max-children", str(settings["cache_mesh_max_children"]),
        "--mesh-sync-interval-ms", str(settings["mesh_sync_interval_ms"]),
        "--mesh-repair-ratio", str(settings["mesh_repair_ratio"]),
        "--mesh-repair-symbols", str(settings["mesh_repair_symbols"]),
        "--mesh-max-repair-symbols", str(settings["mesh_max_repair_symbols"]),
        "--mesh-symbol-size", str(settings["mesh_symbol_size"]),
        "--mesh-max-symbol-size", str(settings["mesh_max_symbol_size"]),
        "--cache-mesh-replication", settings["cache_mesh_replication"],
        "--cache-mesh-lease-ms", str(settings["cache_mesh_lease_ms"]),
        "--cache-mesh-demand-idle-ms", str(settings["cache_mesh_demand_idle_ms"]),
        "--cache-mesh-warm-parts", str(settings["cache_mesh_warm_parts"]),
        "--cache-mesh-warm-kbps", str(settings["cache_mesh_warm_kbps"]),
        "--cache-mesh-socket-buffer-bytes",
        str(settings["cache_mesh_socket_buffer_bytes"]),
    ]
    peer_role = "edge" if node["role"] == "distributor" else "distributor"
    for peer in sorted(state["nodes"].values(), key=lambda item: item["name"]):
        if (peer["site"] == node["site"] and peer["role"] == peer_role
                and peer.get("vlan_ipv4")):
            args += ["--peer", f"{peer['vlan_ipv4']}:{MESH_PORT}"]
    return args


def relay_args(service: dict) -> list[str]:
    """Port of deploy/gcp-lab/av-mesh-run for one compiled av_mesh service."""
    primary = service["primary_parent"]
    args = [
        "--fec-bind", primary["bind"],
        "--relay-controlled-local",
        "--relay-primary-bind", primary["bind"],
        "--relay-primary-peer", primary["peer"],
        "--relay-primary-id", primary["parent_node_id"],
    ]
    if primary["lane"] == "source_and_repair":
        args.append("--relay-primary-promoted")
    secondary = service.get("secondary_parent")
    if secondary:
        args += [
            "--relay-secondary-bind", secondary["bind"],
            "--relay-secondary-peer", secondary["peer"],
            "--relay-secondary-id", secondary["parent_node_id"],
        ]
        if secondary["lane"] == "source_and_repair" or service.get("failover_controller"):
            args.append("--relay-secondary-promoted")
    args += [
        "--relay-topology-generation", str(service["topology_generation"]),
        "--relay-subscription-id", str(service["subscription_id"]),
        "--relay-max-downstream-children", str(service["max_downstream_children"]),
    ]
    for forward in service.get("forwards", []):
        lane = "all" if forward["lane"] == "source_and_repair" else forward["lane"]
        args += ["--relay-forward", f"{forward['bind']}={forward['target']},{lane}"]
    for listener in service.get("failover_listeners") or []:
        args += ["--relay-failover-listener",
                 f"{listener['bind']}={listener['peer']},{listener['forward_target']}"]
    controller = service.get("failover_controller")
    if controller:
        args += ["--relay-failover-controller",
                 f"{controller['bind']}={controller['target']}"]
    policy = service.get("failover_policy")
    if policy:
        args += [
            "--relay-primary-silence-ms", str(policy["primary_silence_ms"]),
            "--relay-primary-recovery-ms", str(policy["primary_recovery_ms"]),
            "--relay-secondary-warm-ms", str(policy["secondary_warm_ms"]),
            "--relay-failover-heartbeat-ms", str(policy["heartbeat_ms"]),
            "--relay-failover-lease-ms", str(policy["lease_ms"]),
        ]
    return args


def contributor_args(service: dict, settings: dict) -> list[str]:
    """Port of deploy/gcp-lab/av-contrib-run."""
    args = [
        "/usr/local/bin/av-contrib",
        "--cert", "/etc/needletail/tls/fullchain.pem",
        "--key", "/etc/needletail/tls/privkey.pem",
        "--http-port", str(CONTRIB_HTTP_PORT),
        "--mesh-fec-target", service["primary"]["target"],
        "--mesh-media-fec-target", service["primary"]["target"],
        "--audio-epoch-ingress-target", service["audio_epoch_ingress_target"],
        "--daw-media-bind", f"0.0.0.0:{DAW_MEDIA_PORT}",
        "--daw-hls-queue-capacity", "4096",
        "--daw-hls-packaging", settings["daw_hls_packaging"],
        "--daw-hls-formats", settings["daw_hls_formats"],
        "--daw-hls-base-stream-id", str(settings["daw_hls_base_stream_id"]),
        "--stream-id", "1", "--rist-stream-id", "1", "--rtmp-stream-id", "1",
        "--fmp4-part-ms", str(settings["fmp4_part_ms"]),
        *(["--fmp4-video-part-ms", str(settings["fmp4_video_part_ms"])]
          if settings.get("fmp4_video_part_ms") else []),
        *(["--fmp4-video-segment-ms", str(settings["fmp4_video_segment_ms"])]
          if settings.get("fmp4_video_segment_ms") else []),
        "--wall-clock-estimated-error-ms", "5",
        "--rist-bind", "0.0.0.0:27000", "--rtmp-bind", "0.0.0.0:19350",
        "--relay-local-id", "contrib",
        "--relay-primary-bind", service["primary"]["bind"],
        "--relay-primary-target", service["primary"]["target"],
        "--relay-primary-id", service["primary"]["child_node_id"],
        "--relay-secondary-bind", service["warm_secondary"]["bind"],
        "--relay-secondary-target", service["warm_secondary"]["target"],
        "--relay-secondary-id", service["warm_secondary"]["child_node_id"],
        "--relay-secondary-seed-source",
        "--relay-exclusive",
        "--relay-topology-generation", str(service["topology_generation"]),
        "--relay-subscription-id", str(service["subscription_id"]),
        "--relay-deadline-ms", str(service["media_deadline_ms"]),
        "--relay-min-repair-symbols", str(settings["relay_min_repair_symbols"]),
    ]
    if service.get("audio_epoch_redundant_ingress_target"):
        args += ["--audio-epoch-redundant-ingress-target",
                 service["audio_epoch_redundant_ingress_target"]]
    for key, prefix in (("source_path_observation", "--relay-path"),
                        ("secondary_path_observation", "--relay-secondary-path")):
        observed = service.get(key)
        if not observed:
            continue
        args += [
            f"{prefix}-loss-fraction", str(observed["loss_ppm"] / 1_000_000),
            f"{prefix}-best-direct-rtt-ms", str(observed["best_direct_rtt_us"] / 1000),
            f"{prefix}-rtt-ms", str(observed["rtt_us"] / 1000),
            f"{prefix}-jitter-ms", str(observed["jitter_us"] / 1000),
            f"{prefix}-queue-delay-ms", str(observed["queue_delay_us"] / 1000),
        ]
        if observed.get("observed_at_unix_ms") is not None:
            args += [f"{prefix}-observed-at-unix-ms", str(observed["observed_at_unix_ms"])]
    return args + list(settings["extra_contributor_args"])


def render(state: dict, settings: dict, plan: dict) -> dict[str, dict]:
    """Return {node name: {service, args, env}} for every service node."""
    services = {
        (item["service"], item["node_id"]): item for item in plan["services"]
    }
    rendered: dict[str, dict] = {}
    for node in state["nodes"].values():
        role = node["role"]
        if role == "contributor":
            args = contributor_args(services[("av_contrib", "contrib")], settings)
            rendered[node["name"]] = {
                "service": "needletail-contrib", "args": args,
                "rust_log": settings["rust_log_contrib"]}
            continue
        if role not in ("backbone", "distributor", "edge"):
            continue
        args = common_mesh_args(node, settings, state)
        if role in ("backbone", "distributor"):
            args += relay_args(services[("av_mesh", relay_node_id(node))])
        if role == "backbone":
            args += ["--mesh-bind", f"0.0.0.0:{MESH_PORT}", "--edge-webtransport"]
            args += list(settings["extra_backbone_args"])
        elif role == "distributor":
            args += cache_mesh_args(node, settings, state)
            args += ["--edge-webtransport"]
            args += list(settings["extra_distributor_args"])
        else:
            args += cache_mesh_args(node, settings, state)
            args += ["--edge-http3"]
            args += list(settings["extra_edge_args"])
        rendered[node["name"]] = {
            "service": "needletail-mesh", "args": args,
            "rust_log": settings["rust_log_mesh"]}
    out = common.ARTIFACT_DIR / "nodes"
    out.mkdir(parents=True, exist_ok=True)
    for name, item in rendered.items():
        (out / f"{name}.args").write_text("\n".join(item["args"]) + "\n")
    return rendered


def ensure_tls() -> None:
    tls = common.ARTIFACT_DIR / "tls"
    if (tls / "fullchain.pem").is_file() and (tls / "privkey.pem").is_file():
        return
    tls.mkdir(parents=True, exist_ok=True)
    name = common.TLS_SERVER_NAME
    run = lambda *command: subprocess.run(command, check=True, capture_output=True)
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
        "-out", str(tls / "ca.key"))
    run("openssl", "req", "-x509", "-new", "-key", str(tls / "ca.key"),
        "-sha256", "-days", "30", "-subj", "/CN=Needletail small-mesh lab CA",
        "-out", str(tls / "ca.crt"))
    # PKCS#8 ("BEGIN PRIVATE KEY"): the contributor's RTMP frontend does not
    # accept a SEC1 "EC PRIVATE KEY" document.
    run("openssl", "genpkey", "-algorithm", "EC",
        "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(tls / "privkey.pem"))
    run("openssl", "req", "-new", "-key", str(tls / "privkey.pem"),
        "-subj", f"/CN={name}", "-out", str(tls / "leaf.csr"))
    (tls / "leaf.ext").write_text(
        f"subjectAltName=DNS:{name},DNS:*.{name}\n"
        "keyUsage=digitalSignature\nextendedKeyUsage=serverAuth,clientAuth\n"
        "basicConstraints=CA:FALSE\n")
    run("openssl", "x509", "-req", "-in", str(tls / "leaf.csr"),
        "-CA", str(tls / "ca.crt"), "-CAkey", str(tls / "ca.key"),
        "-CAcreateserial", "-days", "30", "-sha256",
        "-extfile", str(tls / "leaf.ext"), "-out", str(tls / "leaf.crt"))
    (tls / "fullchain.pem").write_text(
        (tls / "leaf.crt").read_text() + (tls / "ca.crt").read_text())
    (tls / "privkey.pem").chmod(0o600)


BOOTSTRAP = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v chronyc >/dev/null 2>&1 || ! command -v iptables >/dev/null 2>&1 \
  || ! command -v jq >/dev/null 2>&1 || ! command -v rsync >/dev/null 2>&1; then
  for attempt in 1 2 3 4 5; do
    if apt-get update -q >/dev/null 2>&1 && apt-get install -y -q \
      chrony iptables iproute2 jq rsync procps >/dev/null 2>&1; then break; fi
    sleep 5
  done
fi
command -v chronyc >/dev/null
cat >/etc/sysctl.d/60-needletail-udp.conf <<'EOF'
net.core.rmem_max=67108864
net.core.wmem_max=67108864
net.core.rmem_default=8388608
net.core.wmem_default=8388608
net.core.netdev_max_backlog=4096
EOF
sysctl -q --system
if ! id needletail >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/needletail --create-home \
    --shell /usr/sbin/nologin needletail
fi
install -d -m 755 /etc/needletail /var/lib/needletail-test-media /var/lib/needletail-runs
install -d -m 750 -o needletail -g needletail /etc/needletail/tls
cat >/etc/chrony/sources.d/needletail.sources <<'EOF'
server time.cloudflare.com iburst minpoll 4 maxpoll 6
server time.google.com iburst minpoll 4 maxpoll 6
EOF
if grep -q '^pool ' /etc/chrony/chrony.conf; then
  sed -i 's/^pool .*/pool 2.debian.pool.ntp.org iburst minpoll 4 maxpoll 6/' /etc/chrony/chrony.conf
fi
systemctl disable --now systemd-timesyncd.service >/dev/null 2>&1 || true
systemctl enable chrony.service >/dev/null 2>&1
systemctl restart chrony.service
cat >/usr/local/bin/needletail-run-args <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
mapfile -t args <"$1"
exec "${args[@]}"
EOF
chmod 755 /usr/local/bin/needletail-run-args
ip -4 -br addr show | awk '{print $1"="$3}' | tr '\n' ' '
"""


def unit_text(service: str, rust_log: str, settings: dict) -> str:
    description = ("Needletail contributor" if service == "needletail-contrib"
                   else "Needletail AV Mesh node")
    threads = settings["tokio_worker_threads"]
    thread_line = f"Environment=TOKIO_WORKER_THREADS={threads}\n" if threads else ""
    return f"""[Unit]
Description={description} (small-mesh lab)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=needletail
Group=needletail
Environment=RUST_LOG={rust_log}
{thread_line}ExecStart=/usr/local/bin/needletail-run-args /etc/needletail/{service}.args
Restart=always
RestartSec=2
NoNewPrivileges=yes
PrivateDevices=yes
ProtectHome=yes
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
"""


def binaries_for(role: str) -> list[pathlib.Path]:
    target = "target/x86_64-unknown-linux-gnu/release"
    # NEEDLETAIL_AV_MESH_ROOT selects another av-mesh checkout, such as a worktree
    # that carries a candidate patch.
    mesh = pathlib.Path(os.environ.get(
        "NEEDLETAIL_AV_MESH_ROOT", str(common.WORKSPACE / "av-mesh"))) / target
    contrib = common.WORKSPACE / "av-contrib" / target
    daw = common.WORKSPACE / "daw-nexus" / target
    probe = contrib / "aep1-48k-probe"
    if role == "contributor":
        return [contrib / "av-contrib", probe, daw / "daw-test-source"]
    if role == "load":
        return [probe, mesh / "needletail-reader",
                common.WORKSPACE / "av-mesh" / target / "h3-static-capacity",
                common.ROOT / "deploy" / "linode-small-mesh" / "soundkit-part-check"
                / target / "soundkit-part-check"]
    checker = (common.ROOT / "deploy" / "linode-small-mesh" / "soundkit-part-check"
               / target / "soundkit-part-check")
    return [mesh / "av-mesh", probe, checker]


def node_sysctls(settings: dict) -> dict:
    """Kernel settings of every mesh node, from the `node_sysctls` setting."""
    return {str(key): str(value) for key, value in (settings.get("node_sysctls") or {}).items()}


def install_node(node: dict, rendered: dict, settings: dict, binaries: bool) -> str:
    if binaries:
        sources = [str(path) for path in binaries_for(node["role"])]
        for source in sources:
            if not pathlib.Path(source).is_file():
                raise RuntimeError(f"binary is missing: {source}")
        common.rsync_to(node, sources, "/usr/local/bin/")
    tls = common.ARTIFACT_DIR / "tls"
    common.rsync_to(node, [str(tls / "fullchain.pem"), str(tls / "privkey.pem")],
                    "/etc/needletail/tls/")
    common.rsync_to(node, [str(tls / "ca.crt")],
                    "/usr/local/share/ca-certificates/needletail-qualification.crt")
    commands = [
        "chown needletail:needletail /etc/needletail/tls/*.pem",
        "chmod 644 /etc/needletail/tls/fullchain.pem "
        "/usr/local/share/ca-certificates/needletail-qualification.crt",
        "chmod 600 /etc/needletail/tls/privkey.pem",
    ]
    sysctls = node_sysctls(settings)
    if sysctls:
        lines = "\n".join(f"{key} = {value}" for key, value in sysctls.items())
        commands += [
            "modprobe tcp_bbr 2>/dev/null || true",
            f"cat >/etc/sysctl.d/90-needletail.conf <<'SYSCTL'\n{lines}\nSYSCTL",
            "sysctl -q -p /etc/sysctl.d/90-needletail.conf",
        ]
    item = rendered.get(node["name"])
    if item:
        service = item["service"]
        args_file = common.ARTIFACT_DIR / "nodes" / f"{node['name']}.args"
        common.rsync_to(node, [str(args_file)], f"/etc/needletail/{service}.args")
        unit = unit_text(service, item["rust_log"], settings)
        commands += [
            f"chmod 644 /etc/needletail/{service}.args",
            f"cat >/etc/systemd/system/{service}.service <<'UNIT'\n{unit}UNIT",
            "systemctl daemon-reload",
            f"systemctl enable {service}.service >/dev/null 2>&1",
            f"systemctl restart {service}.service",
            "sleep 2",
            f"systemctl is-active {service}.service",
        ]
    return common.ssh_text(node, "set -e\n" + "\n".join(commands))


def command_plan(args: argparse.Namespace) -> tuple[dict, dict, dict]:
    state = common.load_state()
    settings = load_settings(args.set)
    routes_path = common.ARTIFACT_DIR / "routes.json"
    if args.remeasure or not routes_path.is_file():
        routes = measure_routes(state)
        routes_path.write_text(json.dumps(routes, indent=2) + "\n")
    else:
        routes = json.loads(routes_path.read_text())
    program = build_program(state, settings, routes)
    plan = compile_plan(program)
    rendered = render(state, settings, plan)
    print(f"compiled {len(plan['services'])} relay services; rendered "
          f"{len(rendered)} service argument files; primary route "
          f"{routes['primary_rtt_us']} us, secondary {routes['secondary_rtt_us']} us")
    return state, settings, rendered


def command_push(args: argparse.Namespace) -> None:
    state = common.load_state()
    nodes = list(state["nodes"].values())
    ensure_tls()
    if not args.skip_bootstrap:
        interfaces = common.parallel(
            nodes, lambda node: common.ssh_text(node, BOOTSTRAP, timeout=900))
        for node in nodes:
            expected = node.get("vlan_ipv4")
            if expected and f"={expected}/" not in interfaces[node["name"]]:
                raise RuntimeError(
                    f"{node['name']}: VLAN address {expected} is not configured: "
                    f"{interfaces[node['name']]}")
        print(f"bootstrapped {len(nodes)} hosts; every VLAN address is present")
    args.remeasure = args.remeasure or not (common.ARTIFACT_DIR / "routes.json").is_file()
    state, settings, rendered = command_plan(args)
    # Parents first so children find a listening relay when they start.
    order = ["backbone", "distributor", "edge", "contributor", "load"]
    for role in order:
        group = common.nodes_by_role(state, role)
        if group:
            common.parallel(
                group,
                lambda node: install_node(node, rendered, settings, not args.skip_binaries))
            print(f"installed {len(group)} {role} node(s)")


def command_restart(args: argparse.Namespace) -> None:
    args.remeasure = getattr(args, "remeasure", False)
    state, settings, rendered = command_plan(args)
    roles = args.roles.split(",") if args.roles else [
        "backbone", "distributor", "edge", "contributor"]
    for role in ["backbone", "distributor", "edge", "contributor"]:
        if role not in roles:
            continue
        group = common.nodes_by_role(state, role)
        if group:
            common.parallel(
                group, lambda node: install_node(node, rendered, settings, False))
            print(f"restarted {len(group)} {role} node(s)")


def command_status(args: argparse.Namespace) -> None:
    state = common.load_state()

    def probe(node: dict) -> str:
        return common.ssh_text(node, r"""
service=none
for candidate in needletail-mesh needletail-contrib; do
  if systemctl cat $candidate.service >/dev/null 2>&1; then service=$candidate; fi
done
active=$(systemctl is-active $service.service 2>/dev/null || true)
restarts=$(systemctl show $service.service -p NRestarts --value 2>/dev/null || echo -)
memory=$(systemctl show $service.service -p MemoryCurrent --value 2>/dev/null || echo 0)
offset=$(chronyc -c tracking 2>/dev/null | awk -F, '{printf "%.3f", $5*1000}')
steal=$(awk '/^cpu /{printf "%.2f", $9*100/($2+$3+$4+$5+$6+$7+$8+$9)}' /proc/stat)
speed=$(python3 -c "
import hashlib, time
block = b'x' * 65536
best = 0.0
for _ in range(3):
    start = time.perf_counter(); digest = hashlib.sha256()
    for _ in range(1500): digest.update(block)
    best = max(best, 1500 * 65536 / 1e6 / (time.perf_counter() - start))
print(round(best))")
echo "$service $active restarts=$restarts mem_mb=$((${memory:-0}/1048576)) clock_offset_ms=$offset steal_pct=$steal load=$(cut -d' ' -f1 /proc/loadavg) cpu_mb_s=$speed"
""", check=False, timeout=60)

    results = common.parallel(list(state["nodes"].values()), probe)
    speeds = {}
    for name, line in results.items():
        if "cpu_mb_s=" in line:
            speeds[name] = float(line.rsplit("cpu_mb_s=", 1)[1] or 0)
    median = sorted(speeds.values())[len(speeds) // 2] if speeds else 0
    slow = sorted(name for name, speed in speeds.items() if speed < 0.75 * median)
    for name in sorted(results):
        node = state["nodes"][name]
        flag = "  <-- SLOW shared CPU" if name in slow else ""
        print(f"{name:<18} {node['role']:<12} {results[name]}{flag}")
    # Shared-CPU hosts vary by 2x in single-thread speed without reporting steal.
    print(f"median single-thread speed {median:.0f} MB/s; slow nodes "
          f"(<75% of median): {' '.join(slow) or 'none'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--set", action="append", default=[])
    plan.add_argument("--remeasure", action="store_true")
    plan.set_defaults(handler=command_plan)
    push = sub.add_parser("push")
    push.add_argument("--set", action="append", default=[])
    push.add_argument("--remeasure", action="store_true")
    push.add_argument("--skip-bootstrap", action="store_true")
    push.add_argument("--skip-binaries", action="store_true")
    push.set_defaults(handler=command_push)
    restart = sub.add_parser("restart")
    restart.add_argument("--set", action="append", default=[])
    restart.add_argument("--roles", default="")
    restart.set_defaults(handler=command_restart)
    sub.add_parser("hunt").set_defaults(handler=command_hunt)
    sub.add_parser("status").set_defaults(handler=command_status)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
