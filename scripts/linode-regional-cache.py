#!/usr/bin/env python3
"""Run the regional LL-HLS cache demonstrations on the small-instance Linode lab.

The demonstrations are those of `local-region-lab.py`: demand replication
within a region, load balance with admission, failover, drain, assignment and
pinning. Here every node is a Linode, the cache network is the private VLAN of
each region, and the relay lanes cross the public Internet. No loss is
injected.

  linode-regional-cache.py prepare          install the sources on the lab
  linode-regional-cache.py demo [name ...]  run demonstrations (default: all)
  linode-regional-cache.py status           what each cache holds and serves
  linode-regional-cache.py stop-sources     stop the publications

Create the lab and install the services first:

  scripts/linode-small-mesh-lab.py up --topology deploy/linode-small-mesh/topology-regional-cache.json
  scripts/linode-small-mesh-lab.py watchdog --hours 3
  NEEDLETAIL_AV_MESH_ROOT=../av-mesh scripts/linode-small-mesh-deploy.py push $(scripts/linode-regional-cache.py settings)

The readers run on the load host of each region and reach the caches over the
VLAN. The 4K source is `ffmpeg` on the London load host, which sends a
prepared MPEG-TS file to the contributor over RTMP. The audio source is the
probe on the contributor. Records are written under
target/linode-qualification/small-mesh-runs/<run id>/demos/.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import small_mesh_common as common  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "local_region_lab", pathlib.Path(__file__).resolve().parent / "local-region-lab.py")
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)

HTTP_PORT = 19444
REMOTE_CA = "/usr/local/share/ca-certificates/needletail-qualification.crt"
REMOTE_MEDIA = "/var/lib/needletail-test-media"
REMOTE_RUNS = "/var/lib/needletail-runs/regional-cache"
READER = "/usr/local/bin/needletail-reader"
PROBE = "/usr/local/bin/aep1-48k-probe"
RTMP_PORT = 19350

# The settings of the run. One node configuration carries 5 ms audio parts and
# 4K video parts of one frame, as in the local lab. An edge is limited to
# 100 Mbit/s of egress, so that it admits five 4K readers.
SETTINGS = {
    "part_ms": 5,
    "parts_per_segment": 200,
    "window_parts": 2400,
    "fmp4_part_ms": 5,
    "mesh_sync_interval_ms": 2,
    "mesh_symbol_size": 900,
    "mesh_max_symbol_size": 16000,
    "telemetry_interval_ms": 500,
    "playback_address": "vlan",
    "cache_mesh_replication": "demand",
    "cache_mesh_demand_idle_ms": 8000,
    "cache_mesh_lease_ms": 3000,
    # Warming a 4K copy faster than this takes the processor of a one-vCPU
    # edge away from the readers that it already serves.
    "cache_mesh_warm_kbps": 20000,
    "edge_egress_capacity_bps": 100_000_000,
    "steering_ttl_seconds": 1,
    "extra_edge_args": ["--egress-session-idle-seconds", "10"],
    "extra_distributor_args": ["--udp-socket-buffer-bytes", "2097152"],
}


def state() -> dict:
    return common.load_state()


def configure() -> dict:
    """Point the demonstrations at the nodes of the Linode lab."""
    lab_state = state()
    nodes = sorted(lab_state["nodes"].values(), key=lambda item: item["name"])
    lab.NODES = [(item["name"], item["role"], item["site"], item["region"],
                  item["public_ipv4"], item.get("vlan_ipv4")) for item in nodes]
    lab.HTTP_PORTS = {item["name"]: HTTP_PORT for item in nodes}
    run_file = common.QUALIFICATION_ROOT / "regional-cache-run.txt"
    if run_file.is_file():
        run_id = run_file.read_text().strip()
    else:
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-regional-cache"
        run_file.write_text(run_id + "\n")
    lab.WORK = common.RUNS_DIR / run_id
    (lab.WORK / "demos").mkdir(parents=True, exist_ok=True)
    (lab.WORK / "impairment.json").write_text(json.dumps({
        "network": "Linode private VLAN in each region and the public Internet "
                   "between regions; no loss or delay is injected"}))
    lab.http_json = http_json
    lab.http_text = http_text
    lab.Readers = Readers
    lab.kill_service = kill_service
    lab.restart_service = restart_service
    lab.signal_service = signal_service
    lab.start_sources = start_sources
    return lab_state


def by_name(name: str) -> dict:
    return state()["nodes"][name]


def load_host(site: str) -> dict:
    hosts = [item for item in state()["nodes"].values()
             if item["role"] == "load" and item["site"] == site]
    if not hosts:
        sys.exit(f"the lab has no load host in {site}")
    return hosts[0]


def remote_curl(name: str, path: str, extra: list[str], timeout: float) -> str:
    command = ["curl", "-sS", "--cacert", REMOTE_CA,
               "--resolve", f"{lab.SERVER_NAME}:{HTTP_PORT}:127.0.0.1",
               "-m", str(timeout), *extra,
               f"https://{lab.SERVER_NAME}:{HTTP_PORT}{path}"]
    return common.ssh_text(by_name(name), " ".join(shlex.quote(item) for item in command),
                           check=False, timeout=int(timeout) + 30)


def http_json(name: str, path: str, method: str = "GET", body: dict | None = None,
              timeout: float = 8.0):
    extra = ["-X", method, "-o", "-", "-w", "\n%{http_code}"]
    if body is not None:
        extra += ["-H", "content-type: application/json", "-d", json.dumps(body)]
    output = remote_curl(name, path, extra, timeout)
    text, _, status = output.rpartition("\n")
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = text
    return int(status) if status.strip().isdigit() else 0, parsed


def http_text(name: str, path: str, headers: dict | None = None,
              timeout: float = 8.0) -> tuple[int, str, str]:
    extra = ["-D", "-", "-o", "-"]
    for key, value in (headers or {}).items():
        extra += ["-H", f"{key}: {value}"]
    output = remote_curl(name, path, extra, timeout).replace("\r\n", "\n")
    head, _, body = output.partition("\n\n")
    status = int(head.split()[1]) if head.startswith("HTTP/") else 0
    return status, head, body


class Readers:
    """One needletail-reader process on the load host of the entry point's region."""

    def __init__(self, label: str, stream: int, entries: list[str], seconds: int,
                 readers: int = 1, session: str | None = None, cache: str | None = None,
                 steering: bool = False, buffer_ms: int = 1000, spread_ms: int = 0,
                 request_timeout_ms: int = 1500, record_parts: bool = False):
        self.label = label
        self.host = load_host(lab.node(entries[0])["site"])
        self.remote_report = f"{REMOTE_RUNS}/{label}.json"
        self.report_path = lab.WORK / "demos" / f"{label}.reader.json"
        self.report_path.unlink(missing_ok=True)
        command = [READER, "--stream", str(stream),
                   "--session", session or label, "--readers", str(readers),
                   "--duration-seconds", str(seconds), "--buffer-ms", str(buffer_ms),
                   "--request-timeout-ms", str(request_timeout_ms),
                   "--arrival-spread-ms", str(spread_ms),
                   "--tls-ca", REMOTE_CA, "--server-name", lab.SERVER_NAME,
                   "--report", self.remote_report]
        for entry in entries:
            command += ["--entry", lab.base_url(entry)]
        if cache:
            command += ["--cache", cache]
        if steering:
            command.append("--steering")
        if record_parts:
            command.append("--record-parts")
        remote = (f"mkdir -p {REMOTE_RUNS}; rm -f {self.remote_report}; "
                  + " ".join(shlex.quote(item) for item in command)
                  + f" >{REMOTE_RUNS}/{label}.log 2>&1; true")
        self.process = subprocess.Popen(
            ["ssh", *common.ssh_base(), f"root@{self.host['public_ipv4']}", remote],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait(self, timeout: float = 900) -> dict:
        self.process.wait(timeout=timeout)
        report = common.ssh_text(self.host, f"cat {self.remote_report}", check=False)
        if not report.strip():
            log = common.ssh_text(self.host, f"tail -c 800 {REMOTE_RUNS}/{self.label}.log",
                                  check=False)
            raise RuntimeError(f"reader {self.label} wrote no report: {log}")
        self.report_path.write_text(report)
        return json.loads(report)


def kill_service(name: str) -> None:
    """Kill the cache at once and keep it down until it is started again."""
    common.ssh(by_name(name),
               "systemctl kill -s KILL needletail-mesh.service; "
               "systemctl stop needletail-mesh.service")


def restart_service(name: str) -> None:
    common.ssh(by_name(name), "systemctl start needletail-mesh.service")


def signal_service(name: str, stop: bool) -> None:
    """Freeze the cache, or let it run again."""
    common.ssh(by_name(name),
               f"systemctl kill -s {'STOP' if stop else 'CONT'} needletail-mesh.service")


def clip_name() -> str:
    return lab.VIDEO_CLIP.name


def start_sources(seconds: int, video: bool = True, audio: bool = True) -> None:
    marker = lab.WORK / "sources.json"
    started = json.loads(marker.read_text()) if marker.is_file() else {}
    contributor = common.nodes_by_role(state(), "contributor")[0]
    if video and "video" not in started:
        source = load_host(contributor["site"])
        command = (
            f"mkdir -p {REMOTE_RUNS}; nohup ffmpeg -hide_banner -loglevel error -nostdin "
            f"-re -stream_loop -1 -i {REMOTE_MEDIA}/video/{clip_name()} -t {seconds} "
            f"-map 0:v:0 -an -c copy -f flv "
            f"rtmp://{contributor['vlan_ipv4']}:{RTMP_PORT}/live/needletail-lab "
            f">{REMOTE_RUNS}/source-video.log 2>&1 </dev/null &")
        common.ssh(source, command)
        started["video"] = int(time.time())
    if audio and "audio" not in started:
        command = (
            f"mkdir -p {REMOTE_RUNS}; nohup {PROBE} send --target 127.0.0.1:27100 "
            f"--session-id $(( $(date +%s%N) + 3000000000 )) --group-id 0 "
            f"--duration-seconds {seconds} --payload flac --channels 2 "
            f"--group-channels 8 --pcm-s24le-file {REMOTE_MEDIA}/lori-stereo.s24le "
            f"--repair-percent 12 --min-repair-symbols 1 "
            f">{REMOTE_RUNS}/source-audio.json 2>{REMOTE_RUNS}/source-audio.err </dev/null &")
        common.ssh(contributor, command)
        started["audio"] = int(time.time())
    marker.write_text(json.dumps(started))


def stop_sources() -> None:
    lab_state = state()
    contributor = common.nodes_by_role(lab_state, "contributor")[0]
    common.ssh(load_host(contributor["site"]), "pkill -x ffmpeg || true", check=False)
    common.ssh(contributor, "pkill -f 'aep1-48k-probe send' || true", check=False)
    (lab.WORK / "sources.json").unlink(missing_ok=True)
    print("sources stopped")


def prepare() -> None:
    """Install ffmpeg on the source host and upload the clip and the PCM file."""
    lab_state = state()
    contributor = common.nodes_by_role(lab_state, "contributor")[0]
    source = load_host(contributor["site"])
    for path in (lab.VIDEO_CLIP, lab.AUDIO_PCM):
        if not path.is_file():
            sys.exit(f"source file is missing: {path}")
    common.ssh(source, (
        "export DEBIAN_FRONTEND=noninteractive; "
        "command -v ffmpeg >/dev/null 2>&1 || "
        "(apt-get update -q >/dev/null 2>&1; apt-get install -y -q ffmpeg >/dev/null 2>&1); "
        f"mkdir -p {REMOTE_MEDIA}/video; ffmpeg -version | head -1"), timeout=900)
    common.rsync_to(source, [str(lab.VIDEO_CLIP)], f"{REMOTE_MEDIA}/video/", timeout=1800)
    common.ssh(contributor, f"mkdir -p {REMOTE_MEDIA}")
    common.rsync_to(contributor, [str(lab.AUDIO_PCM)],
                    f"{REMOTE_MEDIA}/lori-stereo.s24le", timeout=1800)
    for site in sorted({item["site"] for item in lab_state["nodes"].values()
                        if item["role"] == "load"}):
        common.ssh(load_host(site), f"{READER} --help >/dev/null && mkdir -p {REMOTE_RUNS}")
    print(f"prepared: {clip_name()} on {source['name']}, "
          f"{lab.AUDIO_PCM.name} on {contributor['name']}")


NODE_SAMPLE = r"""
service=needletail-mesh
systemctl cat needletail-contrib.service >/dev/null 2>&1 && service=needletail-contrib
cpu=$(systemctl show $service.service -p CPUUsageNSec --value 2>/dev/null || echo 0)
memory=$(systemctl show $service.service -p MemoryCurrent --value 2>/dev/null || echo 0)
restarts=$(systemctl show $service.service -p NRestarts --value 2>/dev/null || echo 0)
udp=$(awk '/^Udp: [0-9]/{print $4" "$6}' /proc/net/snmp)
steal=$(awk '/^cpu /{print $9" "$2+$3+$4+$5+$6+$7+$8+$9}' /proc/stat)
vlan_tx=$(cat /sys/class/net/eth1/statistics/tx_bytes 2>/dev/null || echo 0)
public_tx=$(cat /sys/class/net/eth0/statistics/tx_bytes 2>/dev/null || echo 0)
echo "$(date +%s%N) ${cpu:-0} ${memory:-0} ${restarts:-0} $udp $steal $vlan_tx $public_tx"
"""


def sample_nodes() -> dict:
    """Counters of every node: service CPU time, memory, UDP errors, traffic."""
    lab_state = state()
    nodes = [item for item in lab_state["nodes"].values()]

    def sample(item: dict) -> dict:
        fields = common.ssh_text(item, NODE_SAMPLE, check=False, timeout=60).split()
        keys = ("unix_ns", "cpu_ns", "memory_bytes", "restarts", "udp_in_errors",
                "udp_rcvbuf_errors", "steal_ticks", "total_ticks", "vlan_tx_bytes",
                "public_tx_bytes")
        return {key: int(value) if value.isdigit() else 0
                for key, value in zip(keys, fields)}

    return common.parallel(nodes, sample)


def node_usage(before: dict, after: dict) -> dict:
    usage = {}
    for name in sorted(after):
        first, last = before.get(name), after[name]
        if not first or last.get("unix_ns", 0) <= first.get("unix_ns", 0):
            continue
        seconds = (last["unix_ns"] - first["unix_ns"]) / 1e9
        ticks = max(1, last["total_ticks"] - first["total_ticks"])
        usage[name] = {
            "service_cpu_percent": round(
                (last["cpu_ns"] - first["cpu_ns"]) / 1e9 / seconds * 100, 1),
            "memory_mb": round(last["memory_bytes"] / 1048576),
            "restarts": last["restarts"] - first["restarts"],
            "udp_receive_errors": last["udp_in_errors"] - first["udp_in_errors"],
            "steal_percent": round(
                (last["steal_ticks"] - first["steal_ticks"]) * 100 / ticks, 2),
            "vlan_tx_mbit_s": round(
                (last["vlan_tx_bytes"] - first["vlan_tx_bytes"]) * 8 / seconds / 1e6, 2),
            "public_tx_mbit_s": round(
                (last["public_tx_bytes"] - first["public_tx_bytes"]) * 8 / seconds / 1e6, 2),
            "public_tx_gb": round(
                (last["public_tx_bytes"] - first["public_tx_bytes"]) / 1e9, 2),
        }
    return usage


def demo(names: list[str]) -> None:
    names = names or list(lab.DEMOS)
    unknown = [name for name in names if name not in lab.DEMOS]
    if unknown:
        sys.exit(f"unknown demonstration: {' '.join(unknown)}")
    lab_state = configure()
    start_sources(seconds=3000)
    results = {}
    usage = {}
    for name in names:
        before = sample_nodes()
        try:
            results[name] = lab.DEMOS[name]()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"  ERROR {name}: {error}")
            results[name] = False
        usage[name] = node_usage(before, sample_nodes())
        (lab.WORK / "demos" / f"{name}.nodes.json").write_text(
            json.dumps(usage[name], indent=1) + "\n")
    summary_path = lab.WORK / "demos" / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {
        "results": {}}
    summary["results"].update(results)
    summary.update({
        "finished_unix": int(time.time()),
        "run_id": lab.WORK.name,
        "lab_run_id": lab_state.get("run_id"),
        "clip": clip_name(),
        "settings": SETTINGS,
        "nodes": {item["name"]: {"role": item["role"], "site": item["site"],
                                 "type": item["type"]}
                  for item in lab_state["nodes"].values()},
        "impairment": json.loads((lab.WORK / "impairment.json").read_text()),
    })
    summary_path.write_text(json.dumps(summary, indent=1) + "\n")
    print("\n" + "  ".join(f"{name}={'PASS' if ok else 'FAIL'}"
                           for name, ok in results.items()))
    print(f"records: {lab.WORK / 'demos'}")
    sys.exit(0 if all(results.values()) else 1)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command, rest = sys.argv[1], sys.argv[2:]
    if command == "settings":
        print(" ".join("--set " + shlex.quote(f"{key}={json.dumps(value)}")
                       for key, value in SETTINGS.items()))
    elif command == "prepare":
        configure()
        prepare()
    elif command == "demo":
        demo(rest)
    elif command == "status":
        configure()
        lab.status()
    elif command == "stop-sources":
        configure()
        stop_sources()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
