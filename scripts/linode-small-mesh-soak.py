#!/usr/bin/env python3
"""Run a multi-hour audio soak through the small-instance Linode mesh.

One soak is one continuous publication from the London contributor. The mesh
nodes run only their service. Every listener runs on the load host of its
region and reaches the distributors and edges across the private VLAN.

Listeners:

* continuous, for the whole soak: native UDP and WebTransport on each
  distributor, and one FLAC and one Opus LL-HLS reader on the last edge of
  each region;
* windowed, one process for each window (one hour by default): FLAC and Opus
  LL-HLS readers on every edge, one FLAC reader on every distributor, and the
  many-reader load on the first edges of each region.

A windowed reader that fails loses one window and not the soak, and its report
is collected when the window ends. The continuous readers carry a per-second
time series for the event analysis.

Each mesh node runs a sampler: CPU, steal, UDP errors and interface bytes each
second, stalls of the node above 250 ms, and on distributors the packet count
of each relay lane twice a second.

Subcommands:
  run       start the soak, collect each window, write the summary (blocks)
  collect   collect and summarize a soak again, for example after an
            interrupted `run`
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import shlex
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import small_mesh_common as common  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "small_mesh_run", pathlib.Path(__file__).resolve().parent / "linode-small-mesh-run.py")
mesh_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mesh_run)

PROBE = mesh_run.PROBE
TLS = mesh_run.TLS
HTTP_PORT = mesh_run.HTTP_PORT
REMOTE_RUNS = mesh_run.REMOTE_RUNS
SAMPLER_PATH = "/usr/local/bin/needletail-soak-sampler.py"
MEDIA_DEADLINE_MS = 1000
# A reader gives up a part that its edge still lacks this long after the part
# was due, counts it as missing, and keeps reading the later parts on time.
ABANDON_AFTER_MS = 2500

SAMPLER = r'''
import subprocess, sys, time
out_path, seconds = sys.argv[1], float(sys.argv[2])
# Counter rules: in:PORT, insrc:ADDRESS:PORT (received), out:ADDRESS:PORT (sent).
# cpu=N pins this sampler to one vCPU, so a stall of that vCPU is recorded.
import os
rules = [value.split(":") for value in sys.argv[3:] if not value.startswith("cpu=")]
for value in sys.argv[3:]:
    if value.startswith("cpu="):
        os.sched_setaffinity(0, {int(value[4:])})
ports = rules
CHAINS = {"INPUT": "NTSOAKI", "OUTPUT": "NTSOAKO"}

def sh(command):
    return subprocess.run(command, shell=True, capture_output=True, text=True).stdout

if rules:
    for hook, chain in CHAINS.items():
        sh(f"iptables -N {chain} 2>/dev/null || iptables -F {chain}")
    for rule in rules:
        if rule[0] == "in":
            sh(f"iptables -A NTSOAKI -p udp --dport {rule[1]}")
        elif rule[0] == "insrc":
            sh(f"iptables -A NTSOAKI -p udp -s {rule[1]} --dport {rule[2]}")
        else:
            sh(f"iptables -A NTSOAKO -p udp -d {rule[1]} --dport {rule[2]}")
    sh("iptables -C INPUT ! -i lo -j NTSOAKI 2>/dev/null || iptables -I INPUT 1 ! -i lo -j NTSOAKI")
    sh("iptables -C OUTPUT ! -o lo -j NTSOAKO 2>/dev/null || iptables -I OUTPUT 1 ! -o lo -j NTSOAKO")

def first_line(path):
    try:
        with open(path) as handle:
            return handle.readline().strip()
    except OSError:
        return "0"

def udp_line():
    with open("/proc/net/snmp") as handle:
        rows = [line for line in handle if line.startswith("Udp:")]
    return " ".join(rows[1].split()[1:7])

def lanes():
    # Received counters first, then sent counters, each in rule order.
    rows = (sh("iptables -L NTSOAKI -nvx").splitlines()[2:]
            + sh("iptables -L NTSOAKO -nvx").splitlines()[2:])
    return " ".join(" ".join(row.split()[:2]) for row in rows)

end = time.time() + seconds
last_tick = time.monotonic()
next_second = next_lane = 0.0
with open(out_path, "a", buffering=1) as out:
    while time.time() < end:
        time.sleep(0.05)
        now = time.monotonic()
        wall = time.time()
        if now - last_tick > 0.25:
            out.write(f"X {wall:.3f} {(now - last_tick) * 1000:.0f}\n")
        if ports and now >= next_lane:
            out.write(f"L {wall:.3f} {lanes()}\n")
            next_lane = now + 0.5
        if now >= next_second:
            stat = " ".join(first_line("/proc/stat").split()[1:9])
            nic = " ".join(first_line(f"/sys/class/net/{name}/statistics/{key}_bytes")
                           for name in ("eth0", "eth1") for key in ("rx", "tx"))
            out.write(f"S {wall:.3f} {stat} | {udp_line()} | {nic}\n")
            next_second = now + 1.0
        last_tick = time.monotonic()
if rules:
    sh("iptables -D INPUT ! -i lo -j NTSOAKI; iptables -D OUTPUT ! -o lo -j NTSOAKO")
    for chain in CHAINS.values():
        sh(f"iptables -F {chain}; iptables -X {chain}")
'''

SNAPSHOT = mesh_run.SNAPSHOT.replace(
    "relay_session:($mesh.relay_session // null)}'",
    "relay_session:($mesh.relay_session // null),"
    "canonical_gap_skip:($mesh.canonical_gap_skip // null),"
    "edge_hole_repair_requests:($mesh.edge_hole_repair_requests // null)}'")
assert "canonical_gap_skip" in SNAPSHOT


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S', time.gmtime())} {message}", flush=True)


def retry(function, attempts: int = 4, pause: float = 8.0):
    for attempt in range(attempts):
        try:
            return function()
        except Exception as error:  # noqa: BLE001 - a soak outlives one SSH failure
            if attempt == attempts - 1:
                raise
            log(f"retry after: {str(error)[-200:]}")
            time.sleep(pause)


def launch(name: str, command: str) -> str:
    # A reader may reconnect after a reset. Its report counts each reconnect.
    return (f"NEEDLETAIL_PROBE_H3_RECONNECT_LIMIT=1000 nohup {command} "
            f">'{name}.json' 2>'{name}.err' </dev/null &\n"
            f"echo $! >'{name}.pid'\n")


def hls(target: dict, stream: dict, plan: dict, offset_ms: int, window: int | None) -> str:
    command = (f"{PROBE} receive-hls --edge {target['vlan_ipv4'].split('/')[0]}:{HTTP_PORT} "
               f"{TLS} --transport h3 --stream-id {stream['stream_id']} "
               f"--session-id {plan['session_id']} --duration-seconds {plan['duration']} "
               f"--part-ms {plan['part_ms']} --deadline-ms {MEDIA_DEADLINE_MS} "
               f"--render-buffer-ms 150 --tail-seconds {plan['tail']} "
               f"--abandon-after-ms {ABANDON_AFTER_MS} "
               f"--expected-audio-codec {stream['codec']}")
    if stream["codec"] == "soundkit-opus":
        command += " --expected-pcm-channels 2 --soundkit-opus-protection plain"
    if window is not None:
        command += f" --start-offset-ms {offset_ms} --window-seconds {window}"
    if offset_ms:
        # Read when the window is launched: see detect_origins.
        command += f" --object-sequence-origin $(cat origin-{stream['name']})"
    return command


def load_command(edge: dict, stream: dict, plan: dict, offset_ms: int) -> str:
    return (f"{PROBE} load-hls --edge {edge['vlan_ipv4'].split('/')[0]}:{HTTP_PORT} {TLS} "
            f"--transport h3 --stream-id {stream['stream_id']} "
            f"--session-id {plan['session_id']} --duration-seconds {plan['duration']} "
            f"--part-ms {plan['part_ms']} --deadline-ms {plan['load_deadline_ms']} "
            f"--tail-seconds {plan['tail']} --readers {plan['readers']} "
            f"--abandon-after-ms {ABANDON_AFTER_MS} --start-offset-ms {offset_ms} "
            f"--window-seconds {plan['window']} "
            f"--expected-audio-codec {stream['codec']}"
            + (f" --object-sequence-origin $(cat origin-{stream['name']})"
               if offset_ms else ""))


def site_driver(load: dict, plan: dict, distributors: list[dict], edges: list[dict]) -> str:
    """The script that one load host runs for the whole soak."""
    session_s = plan["session_id"] // 1_000_000_000
    flac = plan["streams"][0]
    script = f"#!/bin/bash\ncd {plan['remote']}\n"
    for index, node in enumerate(distributors):
        address = node["vlan_ipv4"].split("/")[0]
        script += launch(f"c@{node['name']}@udp", (
            f"{PROBE} receive-udp --relay {address}:{mesh_run.relay_port(node)} "
            f"--bind 0.0.0.0:{27101 + index} --session-id {plan['session_id']} "
            f"--group-id 0 --group-count 1 --formats flac "
            f"--duration-seconds {plan['duration']} --deadline-ms {MEDIA_DEADLINE_MS} "
            f"--tail-seconds {plan['tail']}"))
    if edges:
        # The last edge carries no many-reader load.
        for stream in plan["streams"]:
            script += launch(f"c@{edges[-1]['name']}@hls-{stream['name']}",
                             hls(edges[-1], stream, plan, 0, None))
    # A WebTransport session that receives no datagram reaches the QUIC idle
    # timeout, so these readers connect shortly before the source epoch.
    script += f"while [ $(date +%s) -lt {session_s - 12} ]; do sleep 1; done\n"
    for node in distributors:
        address = node["vlan_ipv4"].split("/")[0]
        script += launch(f"c@{node['name']}@webtransport", (
            f"{PROBE} receive-webtransport --edge {address}:{HTTP_PORT} {TLS} "
            f"--session-id {plan['session_id']} --group-id 0 --formats flac "
            f"--duration-seconds {plan['duration']} --deadline-ms {MEDIA_DEADLINE_MS} "
            f"--tail-seconds {plan['tail']}"))
    for window in range(plan["windows"]):
        offset_ms = window * plan["window"] * 1000
        start = session_s + window * plan["window"] - 25
        script += f"while [ $(date +%s) -lt {start} ]; do sleep 1; done\n"
        prefix = f"w{window:02d}"
        for node in distributors + edges:
            # A distributor already serves the native UDP and WebTransport
            # readers. It gets one lossless LL-HLS reader for the comparison
            # with its edges. Every edge gets both representations.
            wanted = plan["streams"] if node["role"] == "edge" else plan["streams"][:1]
            for stream in wanted:
                script += launch(f"{prefix}@{node['name']}@hls-{stream['name']}",
                                 hls(node, stream, plan, offset_ms, plan["window"]))
        for edge in edges[:plan["load_edges"]]:
            if plan["readers"]:
                script += launch(f"{prefix}@{edge['name']}@load",
                                 load_command(edge, flac, plan, offset_ms))
    return script + "echo done >driver.done\n"


MESH_PORT = 29101
CA_FILE = "/usr/local/share/ca-certificates/needletail-qualification.crt"


def playlist_sequences(load: dict, edge: dict, stream_id: int) -> list[int]:
    """Part object sequences that one edge lists for a stream, read from the
    load host of its region."""
    address = edge["vlan_ipv4"].split("/")[0]
    name = common.TLS_SERVER_NAME
    output = common.ssh_text(load, (
        f"curl -s --max-time 5 --cacert {CA_FILE} "
        f"--resolve {name}:{HTTP_PORT}:{address} "
        f"https://{name}:{HTTP_PORT}/live/{stream_id}/stream.m3u8 "
        "| grep -o 'part[0-9]*' | tr -dc '0-9\\n' | sort -n | tr '\\n' ' '"),
        check=False, timeout=30)
    return [int(value) for value in output.split()]


def site_pairs(plan: dict) -> list[tuple[dict, dict]]:
    """(load host, first edge) for each region."""
    pairs = []
    for load in nodes_of(plan, "load"):
        edges = [edge for edge in nodes_of(plan, "edge") if edge["site"] == load["site"]]
        if edges:
            pairs.append((load, edges[0]))
    return pairs


def detect_origins(plan: dict, result_dir: pathlib.Path) -> None:
    """Find the object sequence of the first part of this publication.

    Opaque FLAC and SoundKit parts carry no timestamp. A reader that starts
    with the publication takes the first listed part as offset zero. A reader
    of a later window needs the same origin to place its parts exactly. The
    playlists list the parts of the newest segments only, so each listener
    host reads them one and two seconds into the publication, while the first
    part is still listed. Call this before the source epoch.
    """
    name = common.TLS_SERVER_NAME

    def read_site(pair: tuple[dict, dict]) -> dict:
        load, edge = pair
        address = edge["vlan_ipv4"].split("/")[0]
        script = ""
        for delay_ms in (1000, 1900):
            at = plan["session_id"] + delay_ms * 1_000_000
            script += f"while [ $(date +%s%N) -lt {at} ]; do sleep 0.02; done\n"
            for stream in plan["streams"]:
                script += (
                    f"echo \"{stream['name']} $(curl -s --max-time 3 --cacert {CA_FILE} "
                    f"--resolve {name}:{HTTP_PORT}:{address} "
                    f"https://{name}:{HTTP_PORT}/live/{stream['stream_id']}/stream.m3u8 "
                    "| grep -o 'part[0-9]*' | tr -dc '0-9\\n' | sort -n | head -1)\"\n")
        seen: dict = {}
        for line in common.ssh_text(load, script, check=False, timeout=120).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                before = plan["sequence_before"].get(parts[0])
                if before is None or int(parts[1]) > before:
                    seen[parts[0]] = min(int(parts[1]), seen.get(parts[0], int(parts[1])))
        return seen

    pairs = site_pairs(plan)
    sites = common.parallel([{"name": load["name"], "pair": (load, edge)}
                             for load, edge in pairs],
                            lambda item: read_site(item["pair"]))
    origins: dict = {}
    for stream in plan["streams"]:
        values = [site[stream["name"]] for site in sites.values() if stream["name"] in site]
        if values:
            origins[stream["name"]] = min(values)
            if len(set(values)) > 1 or len(values) < len(pairs):
                log(f"WARNING: {stream['name']} origin differs between sites: {sites}")
    plan["origins"] = origins
    plan["origin_observations"] = sites
    (result_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    if len(origins) != len(plan["streams"]):
        log(f"WARNING: object sequence origins not found ({sites}); "
            "readers of later windows will fail")
        return
    script = "; ".join(f"echo {value} >{plan['remote']}/origin-{name}"
                       for name, value in origins.items())
    retry(lambda: common.parallel(nodes_of(plan, "load"),
                                  lambda node: common.ssh(node, script)))
    log(f"object sequence origins: {origins}")


def sampler_rules(distributors: list[dict], edges: list[dict]) -> dict:
    """Packet counters for each node: the two relay lanes of a distributor,
    and each distributor-to-edge cache-mesh path at both of its ends."""
    rules: dict = {}
    for node in distributors:
        port = mesh_run.relay_port(node)
        rules[node["name"]] = [f"in:{port}", f"in:{port + 1}"] + [
            f"out:{edge['vlan_ipv4'].split('/')[0]}:{MESH_PORT}"
            for edge in edges if edge["site"] == node["site"]]
    for node in edges:
        rules[node["name"]] = [
            f"insrc:{parent['vlan_ipv4'].split('/')[0]}:{MESH_PORT}"
            for parent in distributors if parent["site"] == node["site"]]
    return rules


def snapshot_all(nodes: list[dict]) -> dict:
    def one(node: dict):
        try:
            return json.loads(common.ssh_text(node, SNAPSHOT, timeout=60))
        except Exception as error:  # noqa: BLE001
            return {"error": str(error)[-200:]}
    return common.parallel(nodes, one)


def start(args: argparse.Namespace) -> dict:
    state = common.load_state()
    settings = json.loads((common.ARTIFACT_DIR / "settings.json").read_text())
    contributor = common.nodes_by_role(state, "contributor")[0]
    distributors = common.nodes_by_role(state, "distributor")
    edges = common.nodes_by_role(state, "edge")
    loads = common.nodes_by_role(state, "load")
    services = [node for node in state["nodes"].values() if node["role"] != "load"]

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{args.name}"
    result_dir = common.RUNS_DIR / run_id
    result_dir.mkdir(parents=True)
    remote = f"{REMOTE_RUNS}/{run_id}"
    windows = -(-args.duration // args.window)
    base = settings["daw_hls_base_stream_id"]
    streams = [{"name": "flac0", "stream_id": base, "codec": "opaque-flac"},
               {"name": "opus0", "stream_id": base + 1000, "codec": "soundkit-opus"}]
    clock_ns = int(common.ssh_text(contributor, "date +%s%N"))
    plan = {
        "schema": "needletail.linode-small-mesh-soak-plan.v1",
        "run_id": run_id, "name": args.name, "label": args.label,
        "lab_run": state["run_id"], "hourly_usd": state["hourly_usd"],
        "node_count": len(state["nodes"]), "remote": remote,
        "session_id": clock_ns + args.start_delay * 1_000_000_000,
        "duration": args.duration, "window": args.window, "windows": windows,
        "part_ms": settings["part_ms"], "tail": args.tail, "readers": args.readers,
        "load_edges": args.load_edges, "load_deadline_ms": args.load_deadline_ms,
        "media_deadline_ms": MEDIA_DEADLINE_MS, "streams": streams,
        "track_directory": args.track_directory, "settings": settings,
        "sampler_rules": sampler_rules(distributors, edges),
        "nodes": {node["name"]: {key: node.get(key) for key in (
            "role", "site", "region", "type", "public_ipv4", "vlan_ipv4")}
            for node in state["nodes"].values()},
    }

    def arm_load(node: dict) -> None:
        site = node["site"]
        driver = site_driver(
            node, plan,
            [item for item in distributors if item["site"] == site],
            [item for item in edges if item["site"] == site])
        common.ssh(node, f"mkdir -p {remote} && cat >{remote}/driver.sh", stdin=driver.encode())
        common.ssh(node, f"cd {remote} && chmod +x driver.sh && "
                         "(setsid nohup ./driver.sh >driver.log 2>&1 </dev/null &)")

    def arm_sampler(node: dict) -> None:
        ports = " ".join(plan["sampler_rules"].get(node["name"], []))
        common.ssh(node, f"mkdir -p {remote} && cat >{SAMPLER_PATH}", stdin=SAMPLER.encode())
        common.ssh(node, (
            f"cd {remote} && (setsid nohup python3 {SAMPLER_PATH} {remote}/sampler.log "
            f"{args.duration + args.start_delay + 20} {ports} "
            ">sampler.err 2>&1 </dev/null & echo $! >sampler.pid)"))

    def arm_load_sampler(node: dict) -> None:
        """One stall detector for each vCPU of a listener host: a reader that
        is late because its own host stalled must not be blamed on the mesh."""
        common.ssh(node, f"mkdir -p {remote} && cat >{SAMPLER_PATH}", stdin=SAMPLER.encode())
        common.ssh(node, (
            f"cd {remote} && for cpu in $(seq 0 $(( $(nproc) - 1 ))); do "
            f"(setsid nohup python3 {SAMPLER_PATH} {remote}/sampler-cpu$cpu.log "
            f"{args.duration + args.start_delay + 20} cpu=$cpu "
            ">sampler.err 2>&1 </dev/null &); done"))

    common.parallel(services, arm_sampler)
    common.parallel(loads, arm_load_sampler)
    common.parallel(loads, arm_load)
    # The newest part of any earlier publication that the playlists still list.
    plan["sequence_before"] = {}
    for stream in streams:
        listed = [value for load, edge in site_pairs(plan)
                  for value in playlist_sequences(load, edge, stream["stream_id"])]
        plan["sequence_before"][stream["name"]] = max(listed) if listed else None
    source = (
        f"mkdir -p {remote} && cd {remote} && (setsid nohup env RUST_LOG=info "
        f"DAW_TEST_SOURCE_START_UNIX_NS={plan['session_id']} "
        f"DAW_TEST_SOURCE_CAPACITY_OUTPUT={remote}/daw-capacity.ndjson "
        f"/usr/local/bin/daw-test-source --direct-contributor --loop-prepared-pcm "
        f"127.0.0.1:27100 {args.duration} {shlex.quote(args.track_directory)} "
        ">source.log 2>source.err </dev/null &) ; sleep 1; pgrep -x daw-test-source | head -1")
    plan["source_pid"] = common.ssh_text(contributor, source)
    margin = (plan["session_id"] - int(common.ssh_text(contributor, "date +%s%N"))) / 1e9
    plan["start_margin_seconds"] = round(margin, 1)
    if margin < 2:
        raise RuntimeError(f"setup left only {margin:.1f} s before the source epoch")
    (result_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (result_dir / "snapshots.json").write_text(json.dumps(
        [{"label": "start", "nodes": snapshot_all(services)}]) + "\n")
    log(f"soak {run_id} armed: {windows} windows of {args.window} s, "
        f"source epoch in {margin:.0f} s")
    return plan


def load_plan(run_id: str) -> tuple[dict, pathlib.Path]:
    result_dir = common.RUNS_DIR / run_id
    return json.loads((result_dir / "plan.json").read_text()), result_dir


def nodes_of(plan: dict, role: str) -> list[dict]:
    return sorted(({"name": name, **node} for name, node in plan["nodes"].items()
                   if node["role"] == role), key=lambda node: node["name"])


def add_snapshot(plan: dict, result_dir: pathlib.Path, label: str) -> None:
    services = [node for role in ("contributor", "backbone", "distributor", "edge")
                for node in nodes_of(plan, role)]
    path = result_dir / "snapshots.json"
    snapshots = json.loads(path.read_text())
    if any(item["label"] == label for item in snapshots):
        return
    snapshots.append({"label": label, "nodes": snapshot_all(services)})
    path.write_text(json.dumps(snapshots) + "\n")


def collect_window(plan: dict, result_dir: pathlib.Path, prefix: str,
                   wait_seconds: int = 60) -> None:
    """Fetch the reports of one window (or of the continuous readers)."""
    remote = plan["remote"]

    def one(node: dict) -> None:
        wait = (f"cd {remote} || exit 0; deadline=$(( $(date +%s) + {wait_seconds} )); "
                "while [ $(date +%s) -lt $deadline ]; do alive=0; "
                f"for f in {prefix}@*.pid; do [ -e \"$f\" ] || continue; "
                "kill -0 $(cat \"$f\") 2>/dev/null && alive=1; done; "
                "[ $alive = 0 ] && break; sleep 2; done; "
                f"for f in {prefix}@*.pid; do [ -e \"$f\" ] || continue; "
                "kill $(cat \"$f\") 2>/dev/null || true; done")
        common.ssh(node, wait, check=False, timeout=wait_seconds + 60)
        target = result_dir / "raw" / node["name"]
        target.mkdir(parents=True, exist_ok=True)
        command = ["rsync", "-az", "--include", f"{prefix}@*.json",
                   "--include", f"{prefix}@*.err", "--include", "decode@*",
                   "--exclude", "*",
                   "-e", "ssh " + " ".join(common.ssh_base()),
                   f"root@{node['public_ipv4']}:{remote}/", str(target) + "/"]
        import subprocess
        result = subprocess.run(command, capture_output=True, timeout=900)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace")[-300:])

    retry(lambda: common.parallel(nodes_of(plan, "load"), one))


def read_report(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        error = path.with_suffix(".err")
        text = error.read_text(errors="replace").strip() if error.is_file() else ""
        return {"error": text[-400:] or "empty report"}


def reports(plan: dict, result_dir: pathlib.Path, prefix: str) -> dict:
    """{(node, lane): summarized report} for one window prefix."""
    found = {}
    for path in sorted((result_dir / "raw").glob(f"*/{prefix}@*.json")):
        _, node, lane = path.stem.split("@", 2)
        report = read_report(path)
        found[(node, lane)] = (report if "error" in report
                               else mesh_run.summarize_report(report))
    return found


def window_table(plan: dict, result_dir: pathlib.Path, window: int) -> dict:
    """Totals for each site and lane for one window, and the strict verdict."""
    found = reports(plan, result_dir, f"w{window:02d}")
    role = {name: node["role"] for name, node in plan["nodes"].items()}
    site = {name: node["site"] for name, node in plan["nodes"].items()}
    table: dict = {}
    failures: list[str] = []
    for (node, lane), report in sorted(found.items()):
        key = ("many" if lane == "load" else f"{role[node]}-{lane.replace('hls-', '')}")
        row = table.setdefault(site[node], {}).setdefault(key, {
            "readers": 0, "errors": 0, "expected": 0, "received": 0, "missing": 0,
            "deadline_misses": 0, "integrity_mismatches": 0, "wire_mbps": 0.0,
            "p50": [], "p99": [], "max": []})
        if "error" in report:
            row["errors"] += 1
            if role[node] == "edge":
                failures.append(f"{node} {lane}: reader failed: {report['error'][-160:]}")
            continue
        row["readers"] += report.get("readers", 1)
        for name in ("expected", "received", "missing", "deadline_misses",
                     "integrity_mismatches"):
            row[name] += report.get(name) or 0
        # A video run places its zero a guard before the first-hop
        # availability, so that no latency is clipped at zero. Remove it here.
        guard = plan.get("latency_guard_ms", 0)
        latency = {name: round(value - guard, 3)
                   for name, value in (report.get("latency_ms") or {}).items()}
        for name in ("p50", "p99", "max"):
            if name in latency:
                row[name].append(latency[name])
        row["wire_mbps"] = round(row["wire_mbps"] + (report.get("wire_mbps") or 0.0), 2)
        row["reconnects"] = row.get("reconnects", 0) + (report.get("reconnects") or 0)
        if role[node] != "edge":
            continue
        deadline = (plan["load_deadline_ms"] if lane == "load"
                    else plan["media_deadline_ms"]) - guard
        if report.get("missing"):
            failures.append(f"{node} {lane}: {report['missing']} missing units")
        if report.get("readers_failed"):
            failures.append(f"{node} {lane}: {report['readers_failed']} readers failed")
        if report.get("integrity_mismatches"):
            failures.append(f"{node} {lane}: {report['integrity_mismatches']} "
                            "integrity mismatches")
        if latency.get("p99", 0) > deadline:
            failures.append(f"{node} {lane}: p99 {latency['p99']} ms is above "
                            f"the {deadline} ms deadline")
    for lanes in table.values():
        for row in lanes.values():
            row["p50"] = round(statistics.mean(row["p50"]), 2) if row["p50"] else None
            row["p99"] = round(max(row["p99"]), 2) if row["p99"] else None
            row["max"] = round(max(row["max"]), 1) if row["max"] else None
    listener_sites = plan.get("listener_sites")
    expected_edge_reports = sum(
        len(plan["streams"]) for node in plan["nodes"].values()
        if node["role"] == "edge" and (not listener_sites or node["site"] in listener_sites))
    edge_reports = sum(1 for (node, lane) in found if role[node] == "edge" and lane != "load")
    if edge_reports < expected_edge_reports:
        failures.append(f"only {edge_reports} of {expected_edge_reports} edge reader "
                        "reports were collected")
    return {"window": window, "sites": table, "failures": failures,
            "passed": not failures}


def print_window(result: dict) -> None:
    for site_name, lanes in result["sites"].items():
        cells = []
        for key in sorted(lanes, key=lambda name: (not name.startswith("edge"), name)):
            row = lanes[key]
            cells.append(f"{key} {row['received']}/{row['expected']} miss={row['missing']} "
                         f"dl={row['deadline_misses']} bad={row['integrity_mismatches']} "
                         f"p50={row['p50']} p99={row['p99']}"
                         + (f" {row['wire_mbps']} Mbit/s" if row.get("wire_mbps") else "")
                         + (f" ERR={row['errors']}" if row["errors"] else ""))
        log(f"  w{result['window']:02d} {site_name:<8} " + " | ".join(cells))
    log(f"  w{result['window']:02d} verdict: "
        + ("PASS" if result["passed"] else "FAIL: " + "; ".join(result["failures"][:6])))


def source_alive(plan: dict) -> bool:
    name, process = plan.get("source_process", ["", "daw-test-source"])
    node = (plan["nodes"][name] | {"name": name}) if name else nodes_of(plan, "contributor")[0]
    return bool(common.ssh_text(node, f"pgrep -x {process} | head -1", check=False).strip())


def schedule_blackout(plan: dict, site: str, at_seconds: int, milliseconds: int) -> None:
    """Drop both relay lanes at every distributor of one region for a fixed
    time. Both distributors then miss the same parts, as they do when they
    share a host that stalls, so no parent can supply those parts to an edge."""
    start_ns = plan["session_id"] + at_seconds * 1_000_000_000

    def schedule(node: dict) -> None:
        port = mesh_run.relay_port(node)
        rule = f"INPUT ! -i lo -p udp -m multiport --dports {port},{port + 1} -j DROP"
        common.ssh(node, (
            f"nohup sh -c 'while [ $(date +%s%N) -lt {start_ns} ]; do sleep 0.01; done; "
            f"iptables -I {rule}; sleep {milliseconds / 1000}; iptables -D {rule}' "
            ">/dev/null 2>&1 </dev/null &"))

    targets = [node for node in nodes_of(plan, "distributor") if node["site"] == site]
    common.parallel(targets, schedule)
    plan["blackout"] = {"site": site, "at_seconds": at_seconds, "milliseconds": milliseconds,
                        "nodes": [node["name"] for node in targets]}
    log(f"blackout of {milliseconds} ms scheduled at +{at_seconds} s on "
        + ", ".join(node["name"] for node in targets))


def run(args: argparse.Namespace) -> None:
    plan = start(args)
    result_dir = common.RUNS_DIR / plan["run_id"]
    session_s = plan["session_id"] / 1e9
    if args.blackout_site:
        schedule_blackout(plan, args.blackout_site, args.blackout_at, args.blackout_ms)
        (result_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    if plan["windows"] > 1:
        try:
            detect_origins(plan, result_dir)
        except Exception as error:  # noqa: BLE001
            log(f"WARNING: origin detection failed: {str(error)[-200:]}")
    watch(plan, result_dir)


def watch(plan: dict, result_dir: pathlib.Path) -> None:
    """Collect each window when it ends, then finish the run."""
    session_s = plan["session_id"] / 1e9 + plan.get("first_offset", 0)
    for window in range(plan["windows"]):
        end = session_s + min((window + 1) * plan["window"], plan["duration"])
        while time.time() < end + plan["tail"] + 12:
            time.sleep(min(300, max(1, end + plan["tail"] + 12 - time.time())))
            try:
                if time.time() < end and not source_alive(plan):
                    log("WARNING: the source process is not running")
            except Exception as error:  # noqa: BLE001
                log(f"liveness check failed: {str(error)[-120:]}")
        try:
            add_snapshot(plan, result_dir, f"w{window:02d}")
            collect_window(plan, result_dir, f"w{window:02d}")
            result = window_table(plan, result_dir, window)
            (result_dir / f"window-{window:02d}.json").write_text(
                json.dumps(result, indent=2) + "\n")
            print_window(result)
        except Exception as error:  # noqa: BLE001 - keep the soak running
            log(f"window {window} collection failed: {str(error)[-300:]}")
    finish(plan, result_dir)


def finish(plan: dict, result_dir: pathlib.Path) -> None:
    """Collect everything that remains and write the summary."""
    remote = plan["remote"]
    collect_window(plan, result_dir, "c", wait_seconds=plan["tail"] + 40)
    for window in range(plan["windows"]):
        collect_window(plan, result_dir, f"w{window:02d}", wait_seconds=5)
    add_snapshot(plan, result_dir, "end")

    def fetch(node: dict) -> None:
        target = result_dir / "raw" / node["name"]
        target.mkdir(parents=True, exist_ok=True)
        since = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.gmtime(plan["session_id"] / 1e9 - 30))
        common.ssh(node, (
            f"cd {remote} 2>/dev/null || exit 0; "
            # The sampler removes its packet counters when it ends by itself.
            "for i in $(seq 1 90); do kill -0 $(cat sampler.pid 2>/dev/null) 2>/dev/null "
            "|| break; sleep 1; done; "
            f"journalctl -u needletail-mesh -u needletail-contrib --utc "
            f"--since '{since}' -o short-iso-precise 2>/dev/null "
            "| grep -E 'canonical contiguous watermark|WARN|ERROR|expired|failover|promot' "
            "| tail -n 20000 >journal.log; gzip -f sampler.log 2>/dev/null; "
            "cat sampler-cpu*.log 2>/dev/null | grep '^X' | sort -k2 >listener-stalls.log; "
            "true"),
            check=False, timeout=300)
        for name in ("sampler.log.gz", "journal.log", "source.log", "source.err",
                     "sender.log", "driver.log", "listener-stalls.log"):
            try:
                common.rsync_from(node, f"{remote}/{name}", str(target) + "/")
            except RuntimeError:
                pass

    everyone = [node for role in ("contributor", "backbone", "distributor", "edge", "load")
                for node in nodes_of(plan, role)]
    retry(lambda: common.parallel(everyone, fetch))
    summarize(plan, result_dir)


def bucket_events(series: list[dict], field: str) -> list[dict]:
    """Merge consecutive one-second buckets that have loss or lateness."""
    events: list[dict] = []
    for bucket in series:
        missing = bucket.get(field, 0)
        late = bucket.get("deadline_misses", 0)
        if not missing and not late:
            continue
        second = bucket["start_offset_ms"] // 1000
        if events and second - events[-1]["end_s"] <= 2:
            events[-1]["end_s"] = second
            events[-1]["missing"] += missing
            events[-1]["late"] += late
        else:
            events.append({"start_s": second, "end_s": second,
                           "missing": missing, "late": late})
    return events


def sampler_rows(path: pathlib.Path) -> dict:
    import gzip
    rows: dict = {"S": [], "L": [], "X": []}
    if not path.is_file():
        return rows
    with gzip.open(path, "rt", errors="replace") as handle:
        for line in handle:
            parts = line.replace("|", " ").split()
            if len(parts) < 3 or parts[0] not in rows:
                continue
            try:
                rows[parts[0]].append([float(value) for value in parts[1:]])
            except ValueError:
                continue
    return rows


def lane_view(rows: dict, start: float, end: float) -> dict:
    """What a distributor's relay lanes and host did around one event."""
    lanes = [row for row in rows["L"]
             if len(row) >= 5 and start - 3 <= row[0] <= end + 3]

    def silent(index: int) -> float:
        """Longest time the packet counter of one lane did not move."""
        longest, changed_at, previous = 0.0, None, None
        for row in lanes:
            if previous is None or row[index] != previous:
                changed_at = row[0]
            else:
                longest = max(longest, row[0] - changed_at)
            previous = row[index]
        return round(longest, 2)

    seconds = [row for row in rows["S"] if start - 3 <= row[0] <= end + 3 and len(row) >= 15]
    view = {"primary_lane_silent_s": silent(1), "secondary_lane_silent_s": silent(3),
            "host_stalls_ms": [int(row[1]) for row in rows["X"]
                               if start - 3 <= row[0] <= end + 3]}
    if len(seconds) >= 2:
        first, last = seconds[0], seconds[-1]
        total = sum(last[1:9]) - sum(first[1:9])
        view["steal_percent"] = round((last[8] - first[8]) * 100 / max(total, 1), 1)
        view["busy_percent"] = round(
            100 - (last[4] + last[5] - first[4] - first[5]) * 100 / max(total, 1), 1)
        view["udp_in_errors"] = int(last[11] - first[11])
        view["udp_rcvbuf_errors"] = int(last[13] - first[13])
    return view


def classify(view: dict) -> str:
    primary = view.get("primary_lane_silent_s", 0) >= 0.9
    secondary = view.get("secondary_lane_silent_s", 0) >= 0.9
    if view.get("host_stalls_ms"):
        return "node stall"
    if primary and secondary:
        return "both relay lanes silent"
    if primary:
        return "primary lane silent"
    if secondary:
        return "secondary lane silent"
    if view.get("udp_rcvbuf_errors", 0) > 0:
        return "receive buffer overflow"
    return "partial loss on a live lane"


def summarize(plan: dict, result_dir: pathlib.Path) -> None:
    session_s = plan["session_id"] / 1e9
    windows = [window_table(plan, result_dir, window) for window in range(plan["windows"])]
    continuous = reports(plan, result_dir, "c")
    site = {name: node["site"] for name, node in plan["nodes"].items()}

    # Per-second series of every edge reader, for the cover check.
    edge_series: dict = {}
    for path in sorted((result_dir / "raw").glob("*/w*@*@hls-*.json")):
        _, node, lane = path.stem.split("@", 2)
        if plan["nodes"][node]["role"] != "edge":
            continue
        for bucket in read_report(path).get("latency_time_series", []):
            second = bucket["start_offset_ms"] // 1000
            entry = edge_series.setdefault((site[node], second), [0, 0, 0.0])
            entry[0] += bucket.get("missing_parts", 0)
            entry[1] += bucket.get("deadline_misses", 0)
            entry[2] = max(entry[2], bucket["availability_latency_ms"].get("max", 0.0))

    events = []
    for node in nodes_of(plan, "distributor"):
        rows = sampler_rows(result_dir / "raw" / node["name"] / "sampler.log.gz")
        load = next(item["name"] for item in nodes_of(plan, "load")
                    if item["site"] == node["site"])
        raw = read_report(result_dir / "raw" / load / f"c@{node['name']}@udp.json")
        for event in bucket_events(raw.get("latency_time_series", []), "missing_epochs"):
            start, end = session_s + event["start_s"], session_s + event["end_s"] + 1
            view = lane_view(rows, start, end)
            cover = [edge_series.get((node["site"], second), [0, 0, 0.0])
                     for second in range(event["start_s"] - 1, event["end_s"] + 4)]
            events.append({
                "distributor": node["name"], "site": node["site"], "lane": "native_udp",
                "offset_s": event["start_s"], "seconds": event["end_s"] - event["start_s"] + 1,
                "utc": time.strftime("%H:%M:%S", time.gmtime(start)),
                "missing_epochs": event["missing"], "late_epochs": event["late"],
                "cause": classify(view), **view,
                "edge_missing_parts": sum(item[0] for item in cover),
                "edge_late_parts": sum(item[1] for item in cover),
                "edge_worst_latency_ms": round(max(item[2] for item in cover), 1),
            })
    events.sort(key=lambda event: (event["offset_s"], event["distributor"]))

    gap_skips = {}
    for node in nodes_of(plan, "distributor"):
        journal = result_dir / "raw" / node["name"] / "journal.log"
        lines = journal.read_text(errors="replace").splitlines() if journal.is_file() else []
        skips = [line for line in lines if "canonical contiguous watermark" in line]
        gap_skips[node["name"]] = {
            "logged_releases": len(skips),
            "first": [re.sub(r"\s+", " ", line)[:260] for line in skips[:3]]}

    # Cache-mesh datagrams sent by each distributor and received by each edge.
    final = {}
    for node in nodes_of(plan, "distributor") + nodes_of(plan, "edge"):
        rows = sampler_rows(result_dir / "raw" / node["name"] / "sampler.log.gz")["L"]
        if rows:
            final[node["name"]] = rows[-1][1:]
    mesh_paths = []
    rules = plan.get("sampler_rules", {})
    for edge in nodes_of(plan, "edge"):
        for index, rule in enumerate(rules.get(edge["name"], [])):
            parent = next((node for node in nodes_of(plan, "distributor")
                           if node["vlan_ipv4"].split("/")[0] == rule.split(":")[1]), None)
            if not parent or edge["name"] not in final or parent["name"] not in final:
                continue
            parent_rules = rules[parent["name"]]
            wanted = f"out:{edge['vlan_ipv4'].split('/')[0]}:{MESH_PORT}"
            if wanted not in parent_rules:
                continue
            sent = final[parent["name"]][parent_rules.index(wanted) * 2]
            received = final[edge["name"]][index * 2]
            mesh_paths.append({
                "from": parent["name"], "to": edge["name"], "site": edge["site"],
                "datagrams_sent": int(sent), "datagrams_received": int(received),
                "datagrams_lost": int(sent - received),
                "loss_ppm": round((sent - received) * 1e6 / max(sent, 1), 1)})

    # Stalls of the listener hosts themselves (any vCPU, above 250 ms).
    listener_stalls = {}
    for load in nodes_of(plan, "load"):
        path = result_dir / "raw" / load["name"] / "listener-stalls.log"
        rows = [line.split() for line in path.read_text().splitlines()] if path.is_file() else []
        listener_stalls[load["name"]] = {
            "stalls": len(rows),
            "longest_ms": max((int(row[2]) for row in rows), default=0),
            "offsets_s": [round(float(row[1]) - session_s) for row in rows][:200]}

    snapshots = json.loads((result_dir / "snapshots.json").read_text())
    usage: dict = {}
    for before, after in zip(snapshots, snapshots[1:]):
        for name, later in after["nodes"].items():
            earlier = before["nodes"].get(name, {})
            if "error" in later or "error" in earlier or not earlier:
                continue
            item = mesh_run.node_usage(earlier, later)
            if later.get("canonical_gap_skip") and earlier.get("canonical_gap_skip"):
                item["gap_skip"] = {
                    key: later["canonical_gap_skip"][key] - earlier["canonical_gap_skip"][key]
                    for key in ("skips", "skipped_parts", "idle_flushes")}
            if later.get("edge_hole_repair_requests") is not None:
                item["edge_hole_repair_requests"] = (
                    later["edge_hole_repair_requests"]
                    - (earlier.get("edge_hole_repair_requests") or 0))
            usage.setdefault(name, {})[after["label"]] = item

    failures = [f"w{window['window']:02d}: {failure}"
                for window in windows for failure in window["failures"]]
    # Every window must be placed on the same timeline as the first one.
    anchor_warnings = []
    for window in windows[1:]:
        for site_name, lanes in window["sites"].items():
            for key in [name for name in lanes if name.startswith("edge-")]:
                first = windows[0]["sites"].get(site_name, {}).get(key, {}).get("p50")
                later = lanes.get(key, {}).get("p50")
                if first is not None and later is not None and abs(later - first) > 25:
                    anchor_warnings.append(
                        f"w{window['window']:02d} {site_name} {key}: p50 {later} ms "
                        f"against {first} ms in the first window")
    for (node, lane), report in continuous.items():
        if plan["nodes"][node]["role"] == "edge":
            if "error" in report:
                failures.append(f"continuous {node} {lane}: {report['error'][-160:]}")
            elif report.get("missing") or report.get("integrity_mismatches"):
                failures.append(f"continuous {node} {lane}: {report.get('missing')} missing, "
                                f"{report.get('integrity_mismatches')} integrity mismatches")
    summary = {
        "schema": "needletail.linode-small-mesh-soak.v1",
        "run_id": plan["run_id"], "name": plan["name"], "label": plan["label"],
        "lab_run": plan["lab_run"], "node_count": plan["node_count"],
        "hourly_usd": plan["hourly_usd"], "session_id": plan["session_id"],
        "duration_seconds": plan["duration"], "window_seconds": plan["window"],
        "media_deadline_ms": plan["media_deadline_ms"],
        "load_deadline_ms": plan["load_deadline_ms"],
        "readers_per_loaded_edge": plan["readers"], "loaded_edges_per_site": plan["load_edges"],
        "streams": plan["streams"], "settings": plan["settings"],
        "pass_rule": ("every edge reader: zero missing units, zero integrity mismatches, "
                      "p99 at or below the deadline, in every window"),
        "passed": not failures, "failures": failures,
        "timeline_warnings": anchor_warnings,
        "windows": windows,
        "continuous": {f"{node}@{lane}": report
                       for (node, lane), report in sorted(continuous.items())},
        "blackout": plan.get("blackout"),
        "loss_events": events, "gap_skips": gap_skips,
        "cache_mesh_paths": mesh_paths, "listener_host_stalls": listener_stalls,
        "usage": usage,
    }
    (result_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for window in windows:
        print_window(window)
    for name, item in listener_stalls.items():
        log(f"listener host {name}: {item['stalls']} stalls above 250 ms, "
            f"longest {item['longest_ms']} ms")
    lossy = [path for path in mesh_paths if path["datagrams_lost"] > 0]
    log(f"cache-mesh paths with datagram loss: {len(lossy)} of {len(mesh_paths)}")
    for path in sorted(lossy, key=lambda item: -item["datagrams_lost"])[:12]:
        log(f"  {path['from']} -> {path['to']}: {path['datagrams_lost']} of "
            f"{path['datagrams_sent']} ({path['loss_ppm']} ppm)")
    log(f"loss events at distributors: {len(events)}")
    for event in events[:40]:
        log(f"  +{event['offset_s']}s {event['distributor']} {event['missing_epochs']} epochs "
            f"{event['cause']}; edges missing={event['edge_missing_parts']} "
            f"late={event['edge_late_parts']} worst={event['edge_worst_latency_ms']} ms")
    for warning in anchor_warnings[:12]:
        log(f"timeline warning: {warning}")
    log(f"soak verdict: {'PASS' if summary['passed'] else 'FAIL'} "
        f"({len(failures)} failures)")
    print(f"\nevidence: {result_dir}/summary.json")


def collect(args: argparse.Namespace) -> None:
    plan, result_dir = load_plan(args.run_id)
    if args.summarize_only:
        summarize(plan, result_dir)
    else:
        finish(plan, result_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--name", required=True)
    runner.add_argument("--label", default="")
    runner.add_argument("--duration", type=int, default=21600)
    runner.add_argument("--window", type=int, default=3600)
    runner.add_argument("--readers", type=int, default=10)
    runner.add_argument("--load-edges", type=int, default=2)
    runner.add_argument("--load-deadline-ms", type=int, default=400)
    runner.add_argument("--tail", type=int, default=6)
    runner.add_argument("--start-delay", type=int, default=50)
    runner.add_argument("--track-directory",
                        default="/var/lib/needletail-test-media/daw-nexus-album-1-track-pcm-600s")
    runner.add_argument("--blackout-site", default="",
                        help="drop both relay lanes at every distributor of this region")
    runner.add_argument("--blackout-at", type=int, default=60)
    runner.add_argument("--blackout-ms", type=int, default=1500)
    runner.set_defaults(handler=run)
    collector = sub.add_parser("collect")
    collector.add_argument("run_id")
    collector.add_argument("--summarize-only", action="store_true")
    collector.set_defaults(handler=collect)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
