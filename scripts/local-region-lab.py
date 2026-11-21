#!/usr/bin/env python3
"""Run two regions of the Needletail mesh on this host and demonstrate the
regional LL-HLS cache features on them.

Every service is one process in one rootless network namespace. Each node has
its own addresses on the loopback interface, and the service arguments are the
ones that the Linode lab deploy tool renders, so the local lab and the cloud
lab run the same configuration.

Loss on the private cache network is injected at each edge: the cache mesh
drops a share of the datagrams that it receives (`cache_loss_ppm`, default
2,000, which is 0.2%). `up netem=on` uses `tc netem` instead, for delay and
loss on the cache network and the relay lanes. It needs the `sch_netem`,
`sch_prio` and `cls_u32` kernel modules.

  local-region-lab.py up [key=value ...]     start every service
  local-region-lab.py down                   stop everything
  local-region-lab.py status                 what each cache holds and serves
  local-region-lab.py sh <command...>        run a command in the namespace
  local-region-lab.py demo [name ...]        run demonstrations (default: all)

Demonstrations: replicate, pin, steering, balance, failover, drain, stall,
parent, region. With NEEDLETAIL_AUDIO_SOURCE=daw the audio source also
publishes HQ SoundKit Opus, and `hq` demonstrates that stream.
`demo` starts the sources that it needs and writes a JSON record of each
demonstration under target/local-region-lab/demos/.

Sibling checkouts are found under the workspace root. Override them with
NEEDLETAIL_AV_MESH_ROOT, NEEDLETAIL_AV_CONTRIB_BIN and NEEDLETAIL_PROBE_BIN.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import pathlib
import shlex
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent
WORK = ROOT / "target" / "local-region-lab"
MESH_ROOT = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AV_MESH_ROOT", str(WORKSPACE / "av-mesh")))
AV_MESH = MESH_ROOT / "target" / "release" / "av-mesh"
READER = MESH_ROOT / "target" / "release" / "needletail-reader"
AV_CONTRIB = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AV_CONTRIB_BIN",
    str(WORKSPACE / "av-contrib" / "target" / "release" / "av-contrib")))
PROBE = pathlib.Path(os.environ.get(
    "NEEDLETAIL_PROBE_BIN",
    str(WORKSPACE / "av-contrib" / "target" / "release" / "aep1-48k-probe")))
VIDEO_CLIP = pathlib.Path(os.environ.get(
    "NEEDLETAIL_VIDEO_CLIP",
    str(pathlib.Path.home() / "media" / "4k-encodes" / "tos24-16m.ts")))
AUDIO_PCM = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AUDIO_PCM",
    str(WORKSPACE / "needletail" / "target" / "linode-qualification" / "media"
        / "daw-nexus-album-1-track-pcm-300s" / "source-track-00.s24le")))
# The audio source. "probe" publishes lossless FLAC. "daw" is the DAW Nexus
# qualification source, which publishes lossless FLAC and HQ SoundKit Opus.
AUDIO_SOURCE = os.environ.get("NEEDLETAIL_AUDIO_SOURCE", "probe")
DAW_SOURCE = pathlib.Path(os.environ.get(
    "NEEDLETAIL_DAW_SOURCE_BIN",
    str(WORKSPACE / "daw-nexus" / "target" / "x86_64-unknown-linux-gnu" / "release"
        / "daw-test-source")))
DAW_TRACKS = pathlib.Path(os.environ.get(
    "NEEDLETAIL_DAW_TRACKS",
    str(WORKSPACE / "needletail" / "target" / "linode-qualification" / "media"
        / "daw-nexus-album-1-track-pcm-600s")))
SERVER_NAME = "mesh.needletail.test"

# name, role, site, region, public address, private (VLAN) address
NODES = [
    ("contrib-lon", "contributor", "london", "gb-lon", "10.90.0.5", "10.77.1.5"),
    ("relay-primary", "backbone", "london", "gb-lon", "10.90.0.6", "10.77.1.6"),
    ("relay-secondary", "backbone", "amsterdam", "nl-ams", "10.90.1.2", None),
    ("lon-dist-a", "distributor", "london", "gb-lon", "10.90.0.11", "10.77.1.11"),
    ("lon-dist-b", "distributor", "london", "gb-lon", "10.90.0.12", "10.77.1.12"),
    ("lon-edge-1", "edge", "london", "gb-lon", "10.90.0.21", "10.77.1.21"),
    ("lon-edge-2", "edge", "london", "gb-lon", "10.90.0.22", "10.77.1.22"),
    ("lon-edge-3", "edge", "london", "gb-lon", "10.90.0.23", "10.77.1.23"),
    ("lon-edge-4", "edge", "london", "gb-lon", "10.90.0.24", "10.77.1.24"),
    ("lon-load", "load", "london", "gb-lon", "10.90.0.31", "10.77.1.31"),
    ("new-dist-a", "distributor", "newyork", "us-east", "10.90.2.11", "10.77.2.11"),
    ("new-dist-b", "distributor", "newyork", "us-east", "10.90.2.12", "10.77.2.12"),
    ("new-edge-1", "edge", "newyork", "us-east", "10.90.2.21", "10.77.2.21"),
    ("new-edge-2", "edge", "newyork", "us-east", "10.90.2.22", "10.77.2.22"),
    ("new-load", "load", "newyork", "us-east", "10.90.2.31", "10.77.2.31"),
]
HTTP_PORTS = {name: 19450 + index for index, (name, *_rest) in enumerate(NODES)}
VIDEO_STREAM = 1
AUDIO_STREAM = 2001
HQ_STREAM = 3001

# One node configuration carries the smallest parts: one AEP1 epoch per audio
# part and one frame per video part, on caches with a 2.5 ms nominal part, a
# 1 s segment and a window of 12 s of the shortest part
# (linode-small-mesh-deploy.py's defaults; see its window_parts_for).
DEFAULT_OVERRIDES = {
    "part_us": 2500,
    "segment_ms": 1000,
    "window_ms": 12000,
    "audio_part": "epoch",
    "video_part": "frame",
    "mesh_sync_interval_ms": 2,
    "mesh_symbol_size": 900,
    "mesh_max_symbol_size": 16000,
    "telemetry_interval_ms": 500,
    "playback_address": "vlan",
    "cache_mesh_demand_idle_ms": 8000,
    "cache_mesh_lease_ms": 3000,
    "cache_mesh_warm_kbps": 20000,
    # A small edge: 100 Mbit/s of egress, admission at 85 Mbit/s.
    "edge_egress_capacity_bps": 100_000_000,
    "steering_ttl_seconds": 1,
}
CACHE_LOSS_PPM = 2000
NETEM_CACHE_MESH = "delay 1ms 0.2ms loss 0.2%"
NETEM_RELAY = "delay 12ms 1ms loss 0.1%"


def node(name: str) -> dict:
    for item, role, site, region, public, vlan in NODES:
        if item == name:
            return {"name": item, "role": role, "site": site, "region": region,
                    "public_ipv4": public, "vlan_ipv4": vlan}
    sys.exit(f"unknown node: {name}")


def home(name: str) -> str:
    item = node(name)
    return item["vlan_ipv4"] or item["public_ipv4"]


def base_url(name: str) -> str:
    return f"https://{home(name)}:{HTTP_PORTS[name]}"


def state() -> dict:
    return {"run_id": "local", "hourly_usd": 0.0,
            "nodes": {name: {**node(name), "type": "local"} for name, *_ in NODES}}


def ns_pid() -> int:
    return int((WORK / "ns.pid").read_text())


def nsenter(command: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        ["nsenter", "--preserve-credentials", "-U", "-n", "-t", str(ns_pid()),
         *command], **kwargs)


def ns_run(command: list[str], check: bool = True, timeout: float = 120) -> str:
    result = subprocess.run(
        ["nsenter", "--preserve-credentials", "-U", "-n", "-t", str(ns_pid()),
         *command], capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)[:120]} failed: "
            f"{result.stderr.decode(errors='replace')[-600:]}")
    return result.stdout.decode(errors="replace")


def deploy_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    import small_mesh_common as common
    spec = importlib.util.spec_from_file_location(
        "deploy", ROOT / "scripts" / "linode-small-mesh-deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    artifacts = WORK / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    common.ARTIFACT_DIR = artifacts
    deploy.SETTINGS_FILE = artifacts / "settings.json"
    deploy.HUNT_FILE = artifacts / "ecmp-ports.json"
    return deploy


def render(overrides: list[str]) -> dict:
    deploy = deploy_module()
    deploy.ensure_tls()
    settings_file = WORK / "artifacts" / "settings.json"
    settings_file.unlink(missing_ok=True)
    merged = [f"{key}={json.dumps(value)}" for key, value in DEFAULT_OVERRIDES.items()]
    settings = deploy.load_settings(merged + overrides)
    routes = {"best_direct_rtt_us": 24000, "primary_rtt_us": 24000,
              "primary_jitter_us": 1000, "secondary_rtt_us": 26000,
              "secondary_jitter_us": 1000}
    lab = state()
    plan = deploy.compile_plan(deploy.build_program(lab, settings, routes))
    rendered = deploy.render(lab, settings, plan)
    tls = WORK / "artifacts" / "tls"
    out = {}
    for name, item in rendered.items():
        address = home(name)
        args = list(item["args"])
        args[0] = str(AV_CONTRIB if item["service"] == "needletail-contrib" else AV_MESH)
        fixed = []
        index = 0
        while index < len(args):
            arg = args[index]
            following = args[index + 1] if index + 1 < len(args) else None
            if arg in ("--cert", "--key"):
                fixed += [arg, str(tls / pathlib.Path(following).name)]
                index += 2
            elif arg == "--http-port":
                fixed += [arg, str(HTTP_PORTS[name])]
                index += 2
            elif arg == "--playback-base-url":
                fixed += [arg, f"{base_url(name)}/live"]
                index += 2
            elif arg in ("--media-fec-bind", "--telemetry-bind", "--mesh-bind",
                         "--daw-media-bind", "--rist-bind", "--rtmp-bind"):
                fixed += [arg, following.replace("0.0.0.0", address)]
                index += 2
            else:
                fixed.append(arg)
                index += 1
        if item["service"] == "needletail-contrib":
            fixed += ["--srt-bind", f"{address}:27001", "--srt-stream-id",
                      str(VIDEO_STREAM)]
        elif node(name)["role"] == "edge":
            fixed += ["--fec-bind", f"{address}:12001"]
        out[name] = {"args": fixed, "rust_log": item["rust_log"]}
    (WORK / "rendered.json").write_text(json.dumps(out, indent=1))
    return out


def start_service(name: str, item: dict) -> int:
    log = open(WORK / "logs" / f"{name}.log", "ab")
    process = nsenter(item["args"], env={**os.environ, "RUST_LOG": item["rust_log"]},
                      stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                      start_new_session=True)
    return process.pid


def load_pids() -> dict:
    try:
        return json.loads((WORK / "pids.json").read_text())
    except OSError:
        return {}


def save_pids(pids: dict) -> None:
    (WORK / "pids.json").write_text(json.dumps(pids))


def service_pid(name: str) -> int | None:
    """PID of the service process, not of the nsenter wrapper that started it."""
    pid = load_pids().get(name)
    if pid is None or not pathlib.Path(f"/proc/{pid}").exists():
        return None
    children = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True,
                              text=True).stdout.split()
    return int(children[0]) if children else pid


def kill_service(name: str) -> None:
    """Stop a service at once, as a crash or a lost host does."""
    os.kill(service_pid(name), signal.SIGKILL)


def signal_service(name: str, stop: bool) -> None:
    """Freeze a service, or let it run again. A frozen service keeps its
    connections open and answers nothing, as a stalled host does."""
    os.kill(service_pid(name), signal.SIGSTOP if stop else signal.SIGCONT)


def restart_service(name: str) -> None:
    rendered = json.loads((WORK / "rendered.json").read_text())
    pids = load_pids()
    pids[name] = start_service(name, rendered[name])
    save_pids(pids)


def netem(enable: bool) -> None:
    ns_run(["tc", "qdisc", "del", "dev", "lo", "root"], check=False)
    if not enable:
        return
    # Band 3 carries the cache mesh (the private network of a region). Band 4
    # carries the relay lanes between regions.
    script = " && ".join([
        "tc qdisc add dev lo root handle 1: prio bands 4 "
        "priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0",
        f"tc qdisc add dev lo parent 1:3 handle 30: netem {NETEM_CACHE_MESH}",
        f"tc qdisc add dev lo parent 1:4 handle 40: netem {NETEM_RELAY}",
        "tc filter add dev lo parent 1: protocol ip prio 1 u32 "
        "match ip protocol 17 0xff match ip dport 29101 0xffff flowid 1:3",
        "tc filter add dev lo parent 1: protocol ip prio 2 u32 "
        "match ip protocol 17 0xff match ip dst 10.90.0.0/16 "
        "match ip dport 22200 0xff00 flowid 1:4",
    ])
    ns_run(["bash", "-c", script])


def up(overrides: list[str]) -> None:
    for binary in (AV_MESH, READER, AV_CONTRIB, PROBE):
        if not binary.is_file():
            sys.exit(f"binary is missing: {binary}")
    down(quiet=True)
    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    for log in (WORK / "logs").glob("*.log"):
        log.unlink()
    holder = subprocess.Popen(
        ["unshare", "-Urn", "sleep", "infinity"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (WORK / "ns.pid").write_text(str(holder.pid))
    time.sleep(0.5)
    addresses = [address for _n, _r, _s, _g, public, vlan in NODES
                 for address in (public, vlan) if address]
    ns_run(["bash", "-c", "ip link set lo up; " + "; ".join(
        f"ip addr add {address}/32 dev lo" for address in addresses)])
    use_netem = "netem=on" in overrides
    loss_ppm = CACHE_LOSS_PPM
    for item in overrides:
        if item.startswith("cache_loss_ppm="):
            loss_ppm = int(item.split("=", 1)[1])
    overrides = [item for item in overrides
                 if not item.startswith(("netem=", "cache_loss_ppm="))]
    if use_netem:
        try:
            netem(True)
            loss_ppm = 0
        except RuntimeError as error:
            sys.exit(f"tc netem is not available on this kernel: {error}")
    # A reader asks for a part many times a second. One that has been silent
    # for ten seconds has left, and its share of the edge is free again.
    edge_args = ["--egress-session-idle-seconds", "10"]
    if loss_ppm:
        edge_args += ["--cache-mesh-test-receive-loss-ppm", str(loss_ppm)]
    overrides.append("extra_edge_args=" + json.dumps(edge_args))
    (WORK / "impairment.json").write_text(json.dumps(
        {"netem": use_netem, "cache_mesh_receive_loss_ppm": loss_ppm,
         "netem_cache_mesh": NETEM_CACHE_MESH if use_netem else None,
         "netem_relay": NETEM_RELAY if use_netem else None}))
    rendered = render(overrides)
    pids = {}
    roles = {name: role for name, role, *_ in NODES}
    for role in ("backbone", "distributor", "edge", "contributor"):
        for name, item in rendered.items():
            if roles[name] == role:
                pids[name] = start_service(name, item)
        time.sleep(1.0)
    save_pids(pids)
    time.sleep(1.5)
    dead = [name for name, pid in pids.items()
            if not pathlib.Path(f"/proc/{pid}").exists()]
    print(f"started {len(pids)} services in namespace {ns_pid()}; "
          + (f"tc netem on" if use_netem
             else f"cache-mesh receive loss {loss_ppm} ppm at each edge"))
    if dead:
        for name in dead:
            print(f"--- {name} did not start")
            print((WORK / "logs" / f"{name}.log").read_text()[-1500:])
        sys.exit(1)


def down(quiet: bool = False) -> None:
    for pid in list(load_pids().values()) + list(load_sources().values()):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.killpg(ns_pid(), signal.SIGKILL)
    except (OSError, ValueError):
        pass
    for path in ("pids.json", "ns.pid", "sources.json"):
        (WORK / path).unlink(missing_ok=True)
    if not quiet:
        print("down")


# ---------------------------------------------------------------- sources

def load_sources() -> dict:
    try:
        return json.loads((WORK / "sources.json").read_text())
    except OSError:
        return {}


def start_sources(seconds: int, video: bool = True, audio: bool = True) -> None:
    sources = load_sources()
    contributor = home("contrib-lon")
    if video and "video" not in sources:
        log = open(WORK / "logs" / "source-video.log", "ab")
        process = nsenter(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
             "-stream_loop", "-1", "-i", str(VIDEO_CLIP), "-t", str(seconds),
             "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-muxdelay", "0",
             "-muxpreload", "0", "-f", "mpegts",
             f"srt://{contributor}:27001?mode=caller&transtype=live&latency=80"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["video"] = process.pid
    if audio and "audio" not in sources and AUDIO_SOURCE == "daw":
        log = open(WORK / "logs" / "source-audio.log", "ab")
        process = nsenter(
            [str(DAW_SOURCE), "--direct-contributor", "--prepared-pcm",
             f"{contributor}:27100", str(seconds), str(DAW_TRACKS)],
            env={**os.environ, "RUST_LOG": "info",
                 "DAW_TEST_SOURCE_START_UNIX_NS": str(time.time_ns() + 3_000_000_000),
                 "DAW_TEST_SOURCE_CAPACITY_OUTPUT": str(WORK / "logs" / "daw-capacity.ndjson")},
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["audio"] = process.pid
    if audio and "audio" not in sources:
        log = open(WORK / "logs" / "source-audio.log", "ab")
        session = time.time_ns() + 2_000_000_000
        process = nsenter(
            [str(PROBE), "send", "--target", f"{contributor}:27100",
             "--session-id", str(session), "--group-id", "0",
             "--duration-seconds", str(seconds), "--payload", "flac",
             "--channels", "2", "--group-channels", "8",
             "--pcm-s24le-file", str(AUDIO_PCM),
             "--repair-percent", "12", "--min-repair-symbols", "1"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        sources["audio"] = process.pid
    (WORK / "sources.json").write_text(json.dumps(sources))


# ---------------------------------------------------------------- HTTP

def http_json(name: str, path: str, method: str = "GET", body: dict | None = None,
              timeout: float = 5.0):
    """Call a node from inside the namespace. Returns (status, parsed JSON)."""
    command = ["curl", "-sS", "--cacert", str(WORK / "artifacts" / "tls" / "ca.crt"),
               "--resolve", f"{SERVER_NAME}:{HTTP_PORTS[name]}:{home(name)}",
               "-X", method, "-m", str(timeout), "-o", "-", "-w", "\n%{http_code}",
               f"https://{SERVER_NAME}:{HTTP_PORTS[name]}{path}"]
    if body is not None:
        command += ["-H", "content-type: application/json", "-d", json.dumps(body)]
    output = ns_run(command, check=False, timeout=timeout + 5)
    text, _, status = output.rpartition("\n")
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = text
    return int(status or 0), parsed


def http_text(name: str, path: str, headers: dict | None = None,
              timeout: float = 5.0) -> tuple[int, str, str]:
    """Returns (status, response headers, body)."""
    command = ["curl", "-sS", "--cacert", str(WORK / "artifacts" / "tls" / "ca.crt"),
               "--resolve", f"{SERVER_NAME}:{HTTP_PORTS[name]}:{home(name)}",
               "-m", str(timeout), "-D", "-", "-o", "-",
               f"https://{SERVER_NAME}:{HTTP_PORTS[name]}{path}"]
    for key, value in (headers or {}).items():
        command += ["-H", f"{key}: {value}"]
    output = ns_run(command, check=False, timeout=timeout + 5)
    head, _, body = output.partition("\r\n\r\n")
    status = int(head.split()[1]) if head.startswith("HTTP/") else 0
    return status, head, body


def mesh_local(name: str) -> dict:
    status, snapshot = http_json(name, "/api/mesh/local")
    if status != 200 or not isinstance(snapshot, dict):
        raise RuntimeError(f"{name}: /api/mesh/local answered {status}")
    return snapshot


def edge_view(name: str) -> dict:
    """What one cache holds and serves, from its own advertisement."""
    snapshot = mesh_local(name)
    edge = next((item for item in snapshot["edge_services"]
                 if item["node_id"] == name), {})
    replication = snapshot["cache_replication"]
    return {
        "node": name,
        "playlists": {str(item["stream_id"]): {
            "head_part": item["head_part"], "sessions": item["sessions"],
            "bitrate_mbps": round((item.get("bitrate_bps") or 0) / 1e6, 2)}
            for item in edge.get("playlists", [])},
        "subscribed": replication["subscribed_playlists"],
        "served_subscriptions": len(replication["served_subscriptions"]),
        "sessions": edge.get("active_playback_sessions", 0),
        "load_permille": edge.get("load_permille", 0),
        "committed_mbps": round(edge.get("committed_bps", 0) / 1e6, 1),
        "observed_mbps": round(edge.get("egress_observed_bps", 0) / 1e6, 1),
        "draining": edge.get("draining", False),
        "rejected": edge.get("egress_rejected_requests", 0),
        "mesh_rx_mbit": snapshot["mesh_fec"].get("rx_wire_bytes", 0) * 8 / 1e6,
    }


def edges(site: str = "london") -> list[str]:
    return [name for name, role, where, *_ in NODES if role == "edge" and where == site]


def distributors(site: str = "london") -> list[str]:
    return [name for name, role, where, *_ in NODES
            if role == "distributor" and where == site]


def wait_for(description: str, predicate, timeout: float = 30.0, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (RuntimeError, KeyError, StopIteration) as error:
            last = error
        time.sleep(interval)
    raise RuntimeError(f"timed out waiting for {description}; last: {last}")


def distributor_head(stream: int, site: str = "london") -> int | None:
    snapshot = mesh_local(distributors(site)[0])
    for item in snapshot["streams"]:
        if item["stream_id"] == stream and item.get("latest_mesh_part") is not None:
            if (item.get("last_ingest_age_ms") or 0) < 3000:
                return item["latest_mesh_part"]
    return None


def wait_for_streams(streams: list[int]) -> None:
    for stream in streams:
        wait_for(f"stream {stream} at the London distributors",
                 lambda stream=stream: distributor_head(stream) is not None, timeout=40)


def control(name: str, kind: str, target: str) -> dict:
    status, command = http_json(name, f"/api/control/{kind}", method="POST",
                                body={"node_id": target})
    if status != 202:
        raise RuntimeError(f"{name}: control {kind} answered {status}: {command}")
    return command


# ---------------------------------------------------------------- readers

class Readers:
    """One needletail-reader process, which runs one or more sessions."""

    def __init__(self, label: str, stream: int, entries: list[str], seconds: int,
                 readers: int = 1, session: str | None = None, cache: str | None = None,
                 steering: bool = False, buffer_ms: int = 1000, spread_ms: int = 0,
                 request_timeout_ms: int = 1500, record_parts: bool = False):
        self.label = label
        self.report_path = WORK / "demos" / f"{label}.reader.json"
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.unlink(missing_ok=True)
        command = [str(READER), "--stream", str(stream),
                   "--session", session or label, "--readers", str(readers),
                   "--duration-seconds", str(seconds), "--buffer-ms", str(buffer_ms),
                   "--request-timeout-ms", str(request_timeout_ms),
                   "--arrival-spread-ms", str(spread_ms),
                   "--tls-ca", str(WORK / "artifacts" / "tls" / "ca.crt"),
                   "--server-name", SERVER_NAME, "--report", str(self.report_path)]
        for entry in entries:
            command += ["--entry", base_url(entry)]
        if cache:
            command += ["--cache", cache]
        if steering:
            command.append("--steering")
        if record_parts:
            command.append("--record-parts")
        self.log = open(WORK / "logs" / f"reader-{label}.log", "wb")
        self.process = nsenter(command, stdout=self.log, stderr=self.log,
                               stdin=subprocess.DEVNULL, start_new_session=True)

    def wait(self, timeout: float = 600) -> dict:
        self.process.wait(timeout=timeout)
        if not self.report_path.is_file():
            raise RuntimeError(
                f"reader {self.label} wrote no report: "
                f"{(WORK / 'logs' / f'reader-{self.label}.log').read_text()[-800:]}")
        return json.loads(self.report_path.read_text())


def reader_line(report: dict) -> dict:
    summary = report["summary"]
    return {key: summary[key] for key in (
        "readers", "readers_ok", "parts_received", "parts_missing", "underruns",
        "max_behind_ms", "moves", "rejections", "readers_by_first_cache",
        "readers_by_final_cache")}


class Check:
    """Collects the assertions of one demonstration."""

    def __init__(self, name: str):
        self.name = name
        self.results: list[dict] = []
        self.evidence: dict = {}
        print(f"\n=== {name}")

    def expect(self, description: str, passed: bool, detail="") -> None:
        self.results.append({"check": description, "pass": bool(passed),
                             "detail": detail})
        print(f"  {'PASS' if passed else 'FAIL'}  {description}"
              + (f"  [{detail}]" if detail != "" else ""))

    def note(self, key: str, value) -> None:
        self.evidence[key] = value

    def finish(self) -> bool:
        passed = all(item["pass"] for item in self.results)
        record = {"demonstration": self.name, "pass": passed,
                  "finished_unix": int(time.time()), "checks": self.results,
                  "evidence": self.evidence}
        out = WORK / "demos"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{self.name}.json").write_text(json.dumps(record, indent=1) + "\n")
        print(f"  => {'PASS' if passed else 'FAIL'}")
        return passed


# ---------------------------------------------------------------- demonstrations

def demo_replicate() -> bool:
    """Feature 1: a cache holds a playlist only while it has readers for it."""
    check = Check("replicate")
    wait_for_streams([VIDEO_STREAM, AUDIO_STREAM])
    # Earlier demonstrations can leave copies that have not gone idle yet.
    wait_for("every London edge to release its playlists",
             lambda: all(not edge_view(name)["subscribed"] for name in edges()),
             timeout=40)
    before = {name: edge_view(name) for name in edges()}
    check.note("before", before)
    check.expect("no edge holds a playlist before a reader arrives",
                 all(not view["playlists"] and not view["subscribed"]
                     for view in before.values()))
    served = sum(len(mesh_local(name)["cache_replication"]["served_subscriptions"])
                 for name in distributors())
    check.expect("the distributors push nothing to the edges", served == 0,
                 f"{served} subscriptions")

    # Demand arrives: a 4K reader on the first edge and a lossless audio
    # reader on the second. Each is pinned, so the demand is at a known cache.
    video_edge, audio_edge, *other_edges = edges()
    video = Readers("replicate-video", VIDEO_STREAM, [video_edge], 14,
                    cache=video_edge, record_parts=True)
    audio = Readers("replicate-audio", AUDIO_STREAM, [audio_edge], 14,
                    cache=audio_edge)
    time.sleep(7)
    during = {name: edge_view(name) for name in edges()}
    check.note("during", during)
    check.expect(f"{video_edge} holds only the 4K playlist",
                 during[video_edge]["subscribed"] == [VIDEO_STREAM],
                 during[video_edge]["subscribed"])
    check.expect(f"{audio_edge} holds only the audio playlist",
                 during[audio_edge]["subscribed"] == [AUDIO_STREAM],
                 during[audio_edge]["subscribed"])
    check.expect("the other edges hold nothing",
                 all(not during[name]["subscribed"] for name in other_edges))
    served = {name: mesh_local(name)["cache_replication"]["served_subscriptions"]
              for name in distributors()}
    check.note("served_subscriptions", served)
    check.expect("each distributor serves exactly those two subscriptions",
                 all(len(items) == 2 for items in served.values()),
                 {name: len(items) for name, items in served.items()})
    new_york = {name: edge_view(name) for name in edges("newyork")}
    check.expect("no New York edge holds a London reader's playlist",
                 all(not view["subscribed"] for view in new_york.values()))

    video_report = video.wait()
    audio_report = audio.wait()
    check.note("video_reader", reader_line(video_report))
    check.note("audio_reader", reader_line(audio_report))
    video_reader = video_report["readers"][0]
    check.expect("the 4K reader received every part with no underrun",
                 video_report["summary"]["readers_ok"] == 1
                 and video_reader["parts_missing"] == 0
                 and video_reader["underruns"] == 0,
                 f"{video_reader['parts_received']} parts, "
                 f"startup {video_reader['startup_ms']} ms")
    audio_reader = audio_report["readers"][0]
    check.expect("the audio reader received every part with no underrun",
                 audio_report["summary"]["readers_ok"] == 1
                 and audio_reader["parts_missing"] == 0
                 and audio_reader["underruns"] == 0,
                 f"{audio_reader['parts_received']} parts, "
                 f"startup {audio_reader['startup_ms']} ms")
    check.note("video_startup_ms", video_reader["startup_ms"])
    check.note("audio_startup_ms", audio_reader["startup_ms"])

    # The edge renders the playlist of its distributor.
    status, _, edge_playlist = http_text(video_edge, f"/live/{VIDEO_STREAM}/stream.m3u8")
    check.expect("the edge playlist marks independent parts and has a program date",
                 status == 200 and "INDEPENDENT=YES" in edge_playlist
                 and "#EXT-X-PROGRAM-DATE-TIME:20" in edge_playlist
                 and "#EXT-X-INDEPENDENT-SEGMENTS" in edge_playlist)
    _, _, distributor_playlist = http_text(
        distributors()[0], f"/live/{VIDEO_STREAM}/stream.m3u8")

    def segment_starts(playlist: str) -> dict:
        """Start time of each segment that lists its parts, by first part."""
        starts, clock, first_part = {}, None, None
        for line in playlist.splitlines():
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                stamp = line.split(":", 1)[1].replace("Z", "+00:00")
                clock = datetime.datetime.fromisoformat(stamp).timestamp()
            elif line.startswith("#EXT-X-PART:") and first_part is None:
                first_part = line.split('URI="part')[1].split(".")[0]
                if clock is not None:
                    starts[first_part] = clock
            elif line.startswith("#EXTINF:") and clock is not None:
                clock += float(line.split(":")[1].rstrip(","))
                first_part = None
        return starts

    # A reader can move between caches when they cut the stream into the same
    # segments and map them to the same program date and time.
    edge_starts = segment_starts(edge_playlist)
    distributor_starts = segment_starts(distributor_playlist)
    shared = set(edge_starts) & set(distributor_starts)
    worst = max((abs(edge_starts[part] - distributor_starts[part]) for part in shared),
                default=None)
    check.note("segment_start_difference_s", worst)
    check.expect("the edge and its distributor cut the same segments at the same "
                 "program time",
                 bool(shared) and worst is not None and worst <= 0.1,
                 f"{len(shared)} segments in both playlists, "
                 f"largest difference {worst if worst is None else round(worst, 3)} s")

    def segment_numbers(playlist: str) -> list:
        return [line.strip() for line in playlist.splitlines() if line.startswith("seg")]

    check.note("edge_segment_names", segment_numbers(edge_playlist)[-3:])
    check.note("distributor_segment_names", segment_numbers(distributor_playlist)[-3:])

    # Demand goes: the copies are released.
    released = wait_for(
        "the edges to release the playlists",
        lambda: all(not edge_view(name)["subscribed"] for name in edges()),
        timeout=30)
    after = {name: edge_view(name) for name in edges()}
    check.note("after", after)
    check.expect("every edge released its playlist after the readers left",
                 bool(released) and all(not view["playlists"] for view in after.values()))
    served = sum(len(mesh_local(name)["cache_replication"]["served_subscriptions"])
                 for name in distributors())
    check.expect("the distributors stopped pushing", served == 0, f"{served} subscriptions")
    return check.finish()


def demo_balance() -> bool:
    """Feature 2: readers spread by load, and a full cache admits no more."""
    check = Check("balance")
    wait_for_streams([VIDEO_STREAM])
    wait_for("the London edges to be idle",
             lambda: all(edge_view(name)["sessions"] == 0 for name in edges()),
             timeout=90, interval=2)
    # Each edge admits 85 Mbit/s. A 4K reader is entitled to about 16 Mbit/s,
    # so an edge takes five. Four readers for each edge need every edge.
    count = 4 * len(edges())
    readers = Readers("balance", VIDEO_STREAM, distributors(), 26,
                      readers=count, session="balance", spread_ms=12000,
                      request_timeout_ms=2500)
    time.sleep(20)
    views = {name: edge_view(name) for name in edges()}
    check.note("edges_during", views)
    report = readers.wait()
    summary = report["summary"]
    check.note("readers", reader_line(report))
    sessions = {name: view["sessions"] for name, view in views.items()}
    check.expect(f"{count} 4K readers were all served", summary["readers_ok"] == count,
                 f"{summary['readers_ok']} of {count}")
    check.expect("no reader had an underrun or a missing part",
                 summary["underruns"] == 0 and summary["parts_missing"] == 0,
                 f"max behind {summary['max_behind_ms']} ms")
    check.expect("no edge was loaded beyond its admission limit",
                 all(view["load_permille"] <= 1000 and view["committed_mbps"] <= 85.5
                     for view in views.values()),
                 {name: view["committed_mbps"] for name, view in views.items()})
    check.expect("no edge was idle while another was full",
                 min(sessions.values()) >= 2 and max(sessions.values()) <= 5, sessions)
    check.expect("a copy was added on every edge as demand grew",
                 all(view["subscribed"] == [VIDEO_STREAM] for view in views.values()))

    # The region is now nearly full. More readers than it has room for:
    # the caches reject the surplus, and the admitted readers are unharmed.
    wait_for("the London edges to be idle",
             lambda: all(edge_view(name)["sessions"] == 0 for name in edges()),
             timeout=90, interval=2)
    surplus_count = 6 * len(edges())
    surplus = Readers("balance-surplus", VIDEO_STREAM, distributors()[:1], 20,
                      readers=surplus_count, session="surplus", spread_ms=8000,
                      request_timeout_ms=2500)
    time.sleep(15)
    full = {name: edge_view(name) for name in edges()}
    check.note("edges_full", full)
    report = surplus.wait()
    summary = report["summary"]
    check.note("surplus_readers", reader_line(report))
    served = [reader for reader in report["readers"] if reader["parts_received"] > 0]
    # An edge admits 85 Mbit/s. At the peak rate of this clip that is four or
    # five readers on each of the four edges.
    check.expect("the region admitted what fits and refused the rest",
                 4 * len(edges()) <= len(served) <= 5 * len(edges()),
                 f"{len(served)} of {surplus_count} served")
    check.expect("every admitted reader played without an underrun",
                 all(reader["underruns"] == 0 and reader["parts_missing"] == 0
                     for reader in served))
    check.expect("every edge stayed at or below its admission limit",
                 all(view["committed_mbps"] <= 85.5 for view in full.values()),
                 {name: view["committed_mbps"] for name, view in full.items()})
    return check.finish()


def reader_caches(report: dict) -> dict:
    return {reader["session"]: reader["final_cache"] for reader in report["readers"]}


def playlist_sessions(name: str, stream: int) -> int:
    return edge_view(name)["playlists"].get(str(stream), {}).get("sessions", 0)


def demo_failover(cases=None) -> bool:
    """Feature 2: a cache that fails moves its readers without a gap."""
    passed = True
    for label, stream in cases or (("failover-video", VIDEO_STREAM),
                                   ("failover-audio", AUDIO_STREAM)):
        check = Check(label)
        wait_for_streams([stream])
        wait_for("the London edges to be idle",
                 lambda stream=stream: all(
                     playlist_sessions(name, stream) == 0 for name in edges()),
                 timeout=60, interval=1)
        # Six readers ask the distributors, which are the entry points here,
        # so that any edge can fail.
        readers = Readers(label, stream, distributors(), 26, readers=6,
                          session=label, steering=True, record_parts=True)
        time.sleep(10)
        sessions = {name: playlist_sessions(name, stream) for name in edges()}
        kill = max(sessions, key=sessions.get)
        check.note("sessions_before", sessions)
        kill_service(kill)
        killed_at = time.time()
        check.note("killed", kill)
        time.sleep(5)
        status, assignment = http_json(
            distributors()[0], f"/api/assignment?stream={stream}&sid=after-failure")
        report = readers.wait()
        # Restart the cache for the demonstrations that follow.
        restart_service(kill)
        check.note("readers", reader_line(report))
        everyone = report["readers"]
        # A reader that a full cache turned away when it arrived was never a
        # reader of that cache. Count the moves that the failure caused.
        moved = [reader for reader in everyone
                 if any(move["from"] == kill and move["reason"] != "rejected"
                        for move in reader["moves"])]
        check.note("moves", [move for reader in everyone for move in reader["moves"]])
        check.expect(f"the failed cache ({kill}) had readers", sessions[kill] >= 2,
                     sessions)
        check.expect("every reader of the failed cache moved to another cache",
                     len(moved) == sessions[kill]
                     and all(reader["final_cache"] != kill for reader in everyone),
                     f"{len(moved)} readers moved")
        check.expect("every reader kept playing: no part missing",
                     all(reader["ok"] and reader["parts_missing"] == 0
                         for reader in everyone))
        check.expect("no reader had an underrun with a 1 s buffer",
                     all(reader["underruns"] == 0 for reader in everyone),
                     f"worst part {max(reader['max_behind_ms'] for reader in everyone)} ms "
                     f"behind schedule")
        gap = max(reader["max_gap_ms"] for reader in moved) if moved else None
        check.note("longest_gap_between_parts_ms", gap)
        check.note("worst_behind_ms", max(reader["max_behind_ms"] for reader in everyone))
        unmoved = [reader for reader in everyone if reader not in moved]
        check.expect("readers of the other caches were not moved",
                     all(move["reason"] == "rejected" for reader in unmoved
                         for move in reader["moves"]),
                     f"{len(unmoved)} readers stayed")
        # A moved reader has the parts in order across the move.
        ordered = True
        for reader in moved:
            parts = [record["part"] for record in reader["parts"]]
            ordered = ordered and parts == list(range(parts[0], parts[0] + len(parts)))
        check.expect("the part sequence is continuous across the move", ordered)
        check.expect("the entry point stopped assigning the failed cache",
                     status == 200 and all(cache["node_id"] != kill
                                           for cache in assignment["assigned"]),
                     [cache["node_id"] for cache in (assignment or {}).get("assigned", [])])
        check.note("seconds_from_kill_to_report", round(time.time() - killed_at, 1))
        passed = check.finish() and passed
        wait_for(f"{kill} to serve again",
                 lambda kill=kill: mesh_local(kill)["node"]["node_id"] == kill, timeout=30)
        time.sleep(3)
    return passed


def demo_drain() -> bool:
    """Feature 2: a drained cache hands its readers to another cache."""
    passed = True
    for label, stream, drain in (("drain-video", VIDEO_STREAM, edges()[2]),
                                 ("drain-audio", AUDIO_STREAM, edges()[-1])):
        check = Check(label)
        wait_for_streams([stream])
        # Three readers pinned to the cache that will drain. A pinned reader
        # has one cache in its list, so only the redirect can move it.
        pinned = Readers(label, stream, [drain], 22, readers=3, session=label,
                         cache=drain, record_parts=True)
        # One reader that follows Content Steering from another entry point.
        steered = Readers(f"{label}-steered", stream, distributors()[:1], 22,
                          session=f"{label}-steered", steering=True, record_parts=True)
        time.sleep(8)
        before = edge_view(drain)
        command = control(drain, "drain-node", drain)
        check.note("control", command["status"])
        time.sleep(4)
        during = edge_view(drain)
        status, assignment = http_json(
            distributors()[0], f"/api/assignment?stream={stream}&sid=new-reader")
        check.note("assignment_during_drain", assignment)
        report = pinned.wait()
        steered_report = steered.wait()
        after = edge_view(drain)
        control(drain, "resume-node", drain)
        check.note("readers", reader_line(report))
        check.note("edge", {"before": before, "during": during, "after": after})
        had = before["playlists"].get(str(stream), {}).get("sessions", 0)
        check.expect("the cache had the pinned readers", had >= 3, f"{had} sessions")
        check.expect("the draining cache advertises that it drains", during["draining"])
        check.expect("the entry point assigns no new reader to it",
                     status == 200 and all(cache["node_id"] != drain
                                           for cache in assignment["assigned"]),
                     [cache["node_id"] for cache in (assignment or {}).get("assigned", [])])
        everyone = report["readers"]
        check.expect("every reader left the draining cache",
                     all(reader["final_cache"] != drain for reader in everyone),
                     reader_caches(report))
        check.expect("each reader moved by a redirect of its playlist reload",
                     all(any(move["reason"] == "drain-redirect" for move in reader["moves"])
                         for reader in everyone))
        check.expect("no part was missing and no reader had an underrun",
                     all(reader["ok"] and reader["parts_missing"] == 0
                         and reader["underruns"] == 0 for reader in everyone),
                     f"worst part {max(reader['max_behind_ms'] for reader in everyone)} ms "
                     f"behind schedule")
        ordered = True
        for reader in everyone:
            parts = [record["part"] for record in reader["parts"]]
            ordered = ordered and parts == list(range(parts[0], parts[0] + len(parts)))
        check.expect("the part sequence is continuous across the move", ordered)
        check.note("moves", [move for reader in everyone for move in reader["moves"]])
        check.note("worst_behind_ms", max(reader["max_behind_ms"] for reader in everyone))
        steered_reader = steered_report["readers"][0]
        check.note("steered_reader", {
            "first_cache": steered_reader["first_cache"],
            "final_cache": steered_reader["final_cache"],
            "moves": steered_reader["moves"]})
        check.expect("the steered reader ended on a cache that is not draining",
                     steered_reader["ok"] and steered_reader["final_cache"] != drain
                     and steered_reader["underruns"] == 0)
        remaining = after["playlists"].get(str(stream), {}).get("sessions", 0)
        check.expect("the drained cache has no reader of the playlist left",
                     remaining == 0, f"{remaining} sessions")
        passed = check.finish() and passed
        time.sleep(2)
    return passed


def demo_pin() -> bool:
    """Feature 3: assignment, stickiness, and a pin to a named cache."""
    check = Check("pin")
    wait_for_streams([VIDEO_STREAM, AUDIO_STREAM])
    # The same session gets the same answer from every entry point. Entry
    # points decide from the advertisements of the caches, so they agree once
    # they have all seen the latest ones: within a telemetry interval.
    answers = {}

    def entry_points_agree() -> bool:
        for entry in distributors() + edges():
            status, body = http_json(
                entry, f"/api/assignment?stream={VIDEO_STREAM}&sid=viewer-42")
            answers[entry] = (status, [cache["node_id"] for cache in body["assigned"]]
                              if status == 200 else body)
        orders = {tuple(order) for status, order in answers.values() if status == 200}
        return len(orders) == 1 and all(status == 200 for status, _ in answers.values())

    started = time.time()
    try:
        agreed = bool(wait_for("the entry points to agree", entry_points_agree,
                               timeout=6, interval=0.5))
    except RuntimeError:
        agreed = False
    check.note("assignment_by_entry_point", answers)
    check.note("seconds_until_entry_points_agreed", round(time.time() - started, 1))
    entry = distributors()[0]
    check.expect("every entry point of the region gives the session the same caches",
                 agreed, answers[entry][1])
    assigned = answers[entry][1][0]
    check.expect("the assigned cache is a London edge", assigned in edges())

    # Different sessions spread over the caches.
    first = {}
    for index in range(40):
        _, body = http_json(entry,
                            f"/api/assignment?stream={VIDEO_STREAM}&sid=spread-{index}")
        first[body["assigned"][0]["node_id"]] = first.get(body["assigned"][0]["node_id"], 0) + 1
    check.note("forty_sessions_first_cache", first)
    check.expect("forty new sessions are spread over every edge",
                 len(first) == len(edges()) and min(first.values()) >= 4, first)

    # A pinned reader gets exactly the named cache, on both media.
    for label, stream, cache in (("pin-video", VIDEO_STREAM, edges()[-1]),
                                 ("pin-audio", AUDIO_STREAM, edges()[-2])):
        report = Readers(label, stream, distributors()[1:2], 8, cache=cache,
                         record_parts=True).wait()
        reader = report["readers"][0]
        check.note(label, {"assigned": reader["assigned"],
                           "parts_by_cache": reader["parts_by_cache"],
                           "startup_ms": reader["startup_ms"]})
        check.expect(f"a reader pinned to {cache} is served only by {cache}",
                     reader["ok"] and reader["assigned"] == [cache]
                     and list(reader["parts_by_cache"]) == [cache]
                     and not reader["moves"],
                     f"{reader['parts_received']} parts")

    pinned = edges()[1]
    status, master, body = http_text(
        entry, f"/live/{VIDEO_STREAM}/master.m3u8?sid=viewer-42&cache={pinned}")
    check.expect("the pinned Multivariant Playlist names only that cache",
                 status == 200 and body.count("#EXT-X-STREAM-INF") == 1
                 and home(pinned) in body)
    status, _, _ = http_text(
        entry, f"/live/{VIDEO_STREAM}/master.m3u8?cache={edges('newyork')[0]}")
    check.expect("a cache of another region cannot be pinned from London",
                 status == 404, f"HTTP {status}")
    status, body = http_json(
        distributors("newyork")[0], f"/api/assignment?stream={VIDEO_STREAM}&sid=viewer-42")
    check.expect("the New York entry point assigns only New York caches",
                 status == 200 and all(cache["node_id"] in edges("newyork")
                                       for cache in body["assigned"]),
                 [cache["node_id"] for cache in body["assigned"]] if status == 200 else body)

    # The Multivariant Playlist carries the assignment as steering pathways.
    # Readers have come and gone since the first question, so ask again.
    _, body = http_json(entry, f"/api/assignment?stream={VIDEO_STREAM}&sid=viewer-43")
    assigned = body["assigned"][0]["node_id"]
    status, _, body = http_text(
        entry, f"/live/{VIDEO_STREAM}/master.m3u8?sid=viewer-43")
    check.note("master_playlist", body)
    # The entry point remembers the session: a second request names the same
    # cache first.
    _, _, again = http_text(
        entry, f"/live/{VIDEO_STREAM}/master.m3u8?sid=viewer-43")
    check.expect("the session gets the same cache when it asks again",
                 again.split("\n")[4] == body.split("\n")[4])
    check.expect("the Multivariant Playlist lists the assigned cache first, as a pathway",
                 status == 200 and f'PATHWAY-ID="{assigned}"' in body.split("\n")[2]
                 and "#EXT-X-CONTENT-STEERING:SERVER-URI=" in body)
    return check.finish()


def demo_steering() -> bool:
    """Feature 3: a reader stays on its cache, and moves when steered."""
    check = Check("steering")
    wait_for_streams([VIDEO_STREAM])
    entry = distributors()[0]
    status, body = http_json(entry,
                             f"/api/assignment?stream={VIDEO_STREAM}&sid=sticky-1")
    assigned = [cache["node_id"] for cache in body["assigned"]]
    current = assigned[1]
    status, steering = http_json(
        entry,
        f"/live/{VIDEO_STREAM}/steering.json?sid=sticky-1&_HLS_pathway={current}")
    check.note("steering_manifest", steering)
    check.expect("a reader that already uses a cache is kept on it",
                 status == 200 and steering["PATHWAY-PRIORITY"][0] == current,
                 steering.get("PATHWAY-PRIORITY") if status == 200 else status)
    check.expect("the manifest names its reload interval and reload address",
                 status == 200 and steering["TTL"] == 1
                 and steering["RELOAD-URI"].startswith("steering.json?sid=sticky-1"))
    control(current, "drain-node", current)
    moved = wait_for(
        "the entry point to see the drain",
        lambda: http_json(
            entry,
            f"/live/{VIDEO_STREAM}/steering.json?sid=sticky-1&_HLS_pathway={current}"
        )[1]["PATHWAY-PRIORITY"][0] != current, timeout=10, interval=0.25)
    _, steering = http_json(
        entry,
        f"/live/{VIDEO_STREAM}/steering.json?sid=sticky-1&_HLS_pathway={current}")
    control(current, "resume-node", current)
    check.expect("after its cache drains the reader is steered to another cache",
                 bool(moved) and current not in steering["PATHWAY-PRIORITY"],
                 steering["PATHWAY-PRIORITY"])
    return check.finish()


def demo_stall() -> bool:
    """Tiny nodes: the mesh routes around a cache that stalls."""
    check = Check("stall")
    wait_for_streams([VIDEO_STREAM])
    wait_for("the London edges to be idle",
             lambda: all(playlist_sessions(name, VIDEO_STREAM) == 0 for name in edges()),
             timeout=60, interval=1)
    # A reader gives a cache 600 ms to answer, inside its 1 s buffer.
    readers = Readers("stall", VIDEO_STREAM, distributors(), 26, readers=6,
                      session="stall", steering=True, record_parts=True,
                      request_timeout_ms=600)
    time.sleep(10)
    sessions = {name: playlist_sessions(name, VIDEO_STREAM) for name in edges()}
    stalled = max(sessions, key=sessions.get)
    check.note("sessions_before", sessions)
    check.note("stalled", stalled)
    signal_service(stalled, stop=True)
    try:
        time.sleep(6)
        status, assignment = http_json(
            distributors()[0], f"/api/assignment?stream={VIDEO_STREAM}&sid=during-stall")
        report = readers.wait()
    finally:
        signal_service(stalled, stop=False)
    everyone = report["readers"]
    moved = [reader for reader in everyone
             if any(move["from"] == stalled and move["reason"] != "rejected"
                    for move in reader["moves"])]
    check.note("readers", reader_line(report))
    check.note("moves", [move for reader in everyone for move in reader["moves"]])
    check.expect(f"the stalled cache ({stalled}) had readers", sessions[stalled] >= 2,
                 sessions)
    check.expect("every reader of the stalled cache moved to another cache",
                 len(moved) == sessions[stalled]
                 and all(reader["final_cache"] != stalled for reader in everyone),
                 f"{len(moved)} readers moved")
    check.expect("no part was missing and no reader had an underrun",
                 all(reader["ok"] and reader["parts_missing"] == 0
                     and reader["underruns"] == 0 for reader in everyone),
                 f"worst part {max(reader['max_behind_ms'] for reader in everyone)} ms "
                 f"behind schedule")
    check.note("worst_behind_ms", max(reader["max_behind_ms"] for reader in everyone))
    check.expect("the entry point stopped assigning the stalled cache",
                 status == 200 and all(cache["node_id"] != stalled
                                       for cache in assignment["assigned"]),
                 [cache["node_id"] for cache in (assignment or {}).get("assigned", [])])
    time.sleep(3)
    return check.finish()


def demo_parent() -> bool:
    """Feature 1: a copy has two parents, and the loss of one is not heard."""
    check = Check("parent")
    wait_for_streams([VIDEO_STREAM, AUDIO_STREAM])
    lost = distributors()[0]
    entry = distributors()[1]
    video = Readers("parent-video", VIDEO_STREAM, [entry], 24, readers=3,
                    session="parent-video", record_parts=True)
    audio = Readers("parent-audio", AUDIO_STREAM, [entry], 24, readers=3,
                    session="parent-audio", record_parts=True)
    time.sleep(9)
    kill_service(lost)
    time.sleep(8)
    served = mesh_local(entry)["cache_replication"]["served_subscriptions"]
    video_report, audio_report = video.wait(), audio.wait()
    restart_service(lost)
    check.note("lost_distributor", lost)
    check.note("video_readers", reader_line(video_report))
    check.note("audio_readers", reader_line(audio_report))
    everyone = video_report["readers"] + audio_report["readers"]
    check.expect("every reader kept its cache", all(not reader["moves"] for reader in everyone))
    check.expect("no part was missing and no reader had an underrun",
                 all(reader["ok"] and reader["parts_missing"] == 0
                     and reader["underruns"] == 0 for reader in everyone),
                 f"worst part {max(reader['max_behind_ms'] for reader in everyone)} ms "
                 f"behind schedule")
    check.note("worst_behind_ms", max(reader["max_behind_ms"] for reader in everyone))
    check.expect("the other distributor served the subscriptions", len(served) >= 2,
                 f"{len(served)} subscriptions")
    wait_for(f"{lost} to serve again",
             lambda: mesh_local(lost)["node"]["node_id"] == lost, timeout=40)
    # The restarted distributor is used again by the next reader's cache.
    time.sleep(6)
    after = Readers("parent-after", VIDEO_STREAM, [entry], 10, readers=2,
                    session="parent-after")
    time.sleep(6)
    restored = mesh_local(lost)["cache_replication"]["served_subscriptions"]
    report = after.wait()
    check.expect("the restarted distributor serves subscriptions again",
                 len(restored) >= 1 and report["summary"]["readers_ok"] == 2,
                 f"{len(restored)} subscriptions")
    return check.finish()


def demo_region() -> bool:
    """Features 1 and 3 in a second region: readers stay in their region."""
    check = Check("region")
    wait_for("the streams at the New York distributors",
             lambda: distributor_head(VIDEO_STREAM, "newyork") is not None
             and distributor_head(AUDIO_STREAM, "newyork") is not None, timeout=40)
    wait_for("every London edge to release its playlists",
             lambda: all(not edge_view(name)["subscribed"] for name in edges()),
             timeout=40)
    video = Readers("region-video", VIDEO_STREAM, distributors("newyork"), 16,
                    readers=3, session="region-video")
    audio = Readers("region-audio", AUDIO_STREAM, distributors("newyork"), 16,
                    readers=3, session="region-audio")
    time.sleep(8)
    london = {name: edge_view(name)["subscribed"] for name in edges()}
    new_york = {name: edge_view(name) for name in edges("newyork")}
    video_report, audio_report = video.wait(), audio.wait()
    check.note("new_york_edges", new_york)
    check.note("video_readers", reader_line(video_report))
    check.note("audio_readers", reader_line(audio_report))
    everyone = video_report["readers"] + audio_report["readers"]
    check.expect("every New York reader was served by a New York cache",
                 all(reader["ok"] and set(reader["parts_by_cache"]) <= set(edges("newyork"))
                     for reader in everyone),
                 sorted({cache for reader in everyone for cache in reader["parts_by_cache"]}))
    check.expect("no part was missing and no reader had an underrun",
                 all(reader["parts_missing"] == 0 and reader["underruns"] == 0
                     for reader in everyone),
                 f"worst part {max(reader['max_behind_ms'] for reader in everyone)} ms "
                 f"behind schedule")
    check.expect("the New York edges hold the playlists that their readers need",
                 any(view["subscribed"] for view in new_york.values()),
                 {name: view["subscribed"] for name, view in new_york.items()})
    check.expect("no London edge holds a playlist for a New York reader",
                 all(not subscribed for subscribed in london.values()), london)
    check.note("video_startup_ms", [reader["startup_ms"] for reader in video_report["readers"]])
    check.note("audio_startup_ms", [reader["startup_ms"] for reader in audio_report["readers"]])
    return check.finish()


def demo_hq() -> bool:
    """HQ audio: the SoundKit Opus stream of the same programme, one reader and
    many, through the same three features."""
    check = Check("hq")
    wait_for_streams([HQ_STREAM])
    wait_for("every London edge to release its playlists",
             lambda: all(not edge_view(name)["subscribed"] for name in edges()),
             timeout=40)
    edge = edges()[0]
    report = Readers("hq-pinned", HQ_STREAM, distributors()[:1], 12, cache=edge,
                     record_parts=True)
    time.sleep(6)
    held = {name: edge_view(name)["subscribed"] for name in edges()}
    reader = report.wait()["readers"][0]
    check.note("pinned_reader", {key: reader[key] for key in (
        "parts_received", "parts_missing", "underruns", "max_behind_ms", "startup_ms",
        "parts_by_cache")})
    check.expect("one pinned reader received every HQ part with no underrun",
                 reader["ok"] and reader["parts_missing"] == 0 and reader["underruns"] == 0
                 and list(reader["parts_by_cache"]) == [edge],
                 f"{reader['parts_received']} parts, startup {reader['startup_ms']} ms")
    check.expect("only that edge holds the HQ playlist",
                 held[edge] == [HQ_STREAM]
                 and all(not streams for name, streams in held.items() if name != edge),
                 held)
    many = Readers("hq-many", HQ_STREAM, distributors(), 14, readers=12, session="hq-many")
    summary = many.wait()["summary"]
    check.note("twelve_readers", reader_line({"summary": summary}))
    check.expect("twelve readers received every HQ part with no underrun",
                 summary["readers_ok"] == 12 and summary["parts_missing"] == 0
                 and summary["underruns"] == 0,
                 f"worst part {summary['max_behind_ms']} ms behind, "
                 f"{summary['readers_by_final_cache']}")
    passed = check.finish()
    time.sleep(3)
    return demo_failover(cases=[("failover-hq", HQ_STREAM)]) and passed


DEMOS = {
    "replicate": demo_replicate,
    "pin": demo_pin,
    "steering": demo_steering,
    "balance": demo_balance,
    "failover": demo_failover,
    "drain": demo_drain,
    "stall": demo_stall,
    "parent": demo_parent,
    "region": demo_region,
}
if AUDIO_SOURCE == "daw":
    DEMOS["hq"] = demo_hq


def demo(names: list[str]) -> None:
    names = names or list(DEMOS)
    unknown = [name for name in names if name not in DEMOS]
    if unknown:
        sys.exit(f"unknown demonstration: {' '.join(unknown)}; "
                 f"choose from {' '.join(DEMOS)}")
    start_sources(seconds=900)
    results = {}
    for name in names:
        try:
            results[name] = DEMOS[name]()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"  ERROR {name}: {error}")
            results[name] = False
    # Memory of each cache after every demonstration. An edge has released
    # its copies by now, so this is what the copies left behind.
    resident = {}
    for name, role, *_ in NODES:
        pid = service_pid(name) if role in ("edge", "distributor") else None
        if pid:
            fields = pathlib.Path(f"/proc/{pid}/statm").read_text().split()
            resident[name] = round(int(fields[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576)
    summary = {"finished_unix": int(time.time()), "results": results,
               "clip": VIDEO_CLIP.name, "resident_memory_mb": resident,
               "impairment": json.loads((WORK / "impairment.json").read_text())}
    print("resident memory, MB:", resident)
    (WORK / "demos" / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print("\n" + "  ".join(f"{name}={'PASS' if ok else 'FAIL'}"
                           for name, ok in results.items()))
    sys.exit(0 if all(results.values()) else 1)


def status() -> None:
    for site in ("london", "newyork"):
        for name in distributors(site):
            snapshot = mesh_local(name)
            streams = {item["stream_id"]: item.get("latest_mesh_part")
                       for item in snapshot["streams"]}
            served = snapshot["cache_replication"]["served_subscriptions"]
            print(f"{name:<12} streams {streams} serves {len(served)} subscriptions")
        for name in edges(site):
            view = edge_view(name)
            print(f"{name:<12} holds {view['playlists']} sessions {view['sessions']} "
                  f"load {view['load_permille']}/1000 draining {view['draining']}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command, rest = sys.argv[1], sys.argv[2:]
    if command == "up":
        up(rest)
    elif command == "down":
        down()
    elif command == "status":
        status()
    elif command == "sh":
        sys.exit(nsenter(rest).wait())
    elif command == "demo":
        demo(rest)
    elif command == "sources":
        start_sources(seconds=int(rest[0]) if rest else 900)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
