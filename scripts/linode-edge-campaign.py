#!/usr/bin/env python3
"""Run scenarios of the edge fault campaign on the small-instance Linode lab.

The scenarios are those of `edge-campaign.py`. Here every node is a Linode,
the cache network is the private VLAN of each region, the relay lanes cross
the public Internet, and a service has the one vCPU and 1 GB of its Nanode
and no other limit. Faults are injected on the instances: `systemctl` for a
kill, a freeze or a limit, and `tc netem` on the VLAN interface of a node for
loss on the cache network. The readers run on the load host of each region.
The 4K source is `ffmpeg` on the London load host, which sends a prepared
MPEG-TS file to the contributor over RTMP: the contributor build for the lab
has no SRT listener, because libsrt does not cross-compile with zig.

  linode-edge-campaign.py settings           the deploy settings of the run
  linode-edge-campaign.py prepare            install the sources on the lab
  linode-edge-campaign.py list
  linode-edge-campaign.py run [scenario ...] (default: the Linode set)
  linode-edge-campaign.py summary
  linode-edge-campaign.py stop-sources

Create the lab and install the services first:

  scripts/linode-small-mesh-lab.py up --topology deploy/linode-small-mesh/topology-regional-cache.json \
      --watchdog-hours 4 --budget-usd B
  scripts/linode-small-mesh-deploy.py push $(scripts/linode-edge-campaign.py settings)

Evidence is written under target/linode-qualification/small-mesh-runs/<run>/.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shlex
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import small_mesh_common as common  # noqa: E402
import edge_lab as lab  # noqa: E402
import edge_campaign_core as core  # noqa: E402

_spec = importlib.util.spec_from_file_location("edge_campaign", HERE / "edge-campaign.py")
campaign = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(campaign)
_deploy_spec = importlib.util.spec_from_file_location(
    "small_mesh_deploy", HERE / "linode-small-mesh-deploy.py")
deploy = importlib.util.module_from_spec(_deploy_spec)
_deploy_spec.loader.exec_module(deploy)

REMOTE_CA = "/usr/local/share/ca-certificates/needletail-qualification.crt"
REMOTE_MEDIA = "/var/lib/needletail-test-media"
REMOTE_RUNS = "/var/lib/needletail-runs/edge-campaign"
READER = "/usr/local/bin/needletail-reader"
DAW_SOURCE = "/usr/local/bin/daw-test-source"
HTTP_PORT = 19444

# The settings of the run: those of the local lab. The readers reach the
# caches at their public addresses, so that a player on another network can
# be pointed at the lab as well.
SETTINGS = {
    **lab.DEFAULT_OVERRIDES,
    "playback_address": "public",
}
RTMP_PORT = 19350

# The scenarios that need real instances or a real network.
LINODE_SET = [
    "baseline", "spec-playlists",
    "capacity-edge-flac", "capacity-edge-opus", "capacity-edge-4k",
    "admission-refuses-cleanly", "flash-crowd", "mixed-edge", "slow-and-stuck-readers",
    "edge-kill", "edge-freeze-resume", "edge-rolling-restart", "distributor-kill",
    "distributors-both-kill", "relay-primary-kill", "contributor-freeze",
    "mesh-loss-0.5pct", "mesh-loss-2pct", "mesh-burst-loss", "mesh-partition-edge-heals",
    "edge-cpu-starved", "edge-out-of-memory", "region-lost",
    "capacity-scales-with-edges", "capacity-entry-joins",
    "clock-skew-edge", "clock-skew-distributor", "clock-skew-contributor", "edge-disk-full",
    "player-hlsjs-4k", "player-hlsjs-4k-second-region", "player-hlsjs-cache-kill",
    "player-hlsjs-region-lost",
    "ladder-playlists", "ladder-hlsjs-browser-cap", "ladder-hlsjs-browser-steps",
    "long-tail-one-stream-too-many", "long-tail-more-slots", "capacity-seventeen-streams",
]


def state() -> dict:
    return common.load_state()


def by_name(name: str) -> dict:
    nodes = state()["nodes"]
    if name in nodes:
        return nodes[name]
    site = {"lon-readers": "london", "lon-readers-2": "london",
            "new-readers": "newyork"}.get(name)
    if site:
        return next(item for item in nodes.values()
                    if item["role"] == "load" and item["site"] == site)
    raise lab.LabError(f"unknown node: {name}")


def service_of(name: str) -> str:
    return ("needletail-contrib" if by_name(name)["role"] == "contributor"
            else "needletail-mesh") + ".service"


def remote(name: str, command: str, check: bool = False, timeout: int = 60) -> str:
    return common.ssh_text(by_name(name), command, check=check, timeout=timeout)


# ------------------------------------------------------------------ the lab

def configure() -> pathlib.Path:
    """Point the campaign at the nodes of the Linode lab."""
    lab_state = state()
    nodes = sorted(lab_state["nodes"].values(), key=lambda item: item["name"])
    order = {"contributor": 0, "backbone": 1, "distributor": 2, "edge": 3}
    lab.NODES = [(item["name"], item["role"], item["site"], item["region"],
                  item["public_ipv4"], item.get("vlan_ipv4"))
                 for item in sorted(nodes, key=lambda item: (order.get(item["role"], 9),
                                                             item["name"]))
                 if item["role"] in order]
    hosts = {item["site"]: item for item in nodes if item["role"] == "load"}
    lab.READER_HOSTS = [("lon-readers", "london", hosts["london"]["public_ipv4"]),
                        ("lon-readers-2", "london", hosts["london"]["public_ipv4"]),
                        ("new-readers", "newyork", hosts["newyork"]["public_ipv4"])]
    lab.HTTP_PORT = HTTP_PORT
    run_file = common.QUALIFICATION_ROOT / "edge-campaign-run.txt"
    if run_file.is_file():
        run_id = run_file.read_text().strip()
    else:
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-edge-campaign"
        run_file.write_text(run_id + "\n")
    directory = common.RUNS_DIR / run_id
    (directory / "scenarios").mkdir(parents=True, exist_ok=True)
    lab.WORK = directory
    return directory


def curl(name: str, path: str, method: str = "GET", body: dict | None = None,
         timeout: float = 8.0, headers: dict | None = None, via: str | None = None,
         http3: bool = False) -> tuple[int, str, str]:
    port = lab.CONTRIB_HTTP_PORT if by_name(name)["role"] == "contributor" else HTTP_PORT
    command = ["curl", "-sS", "--cacert", REMOTE_CA, "--resolve",
               f"{lab.SERVER_NAME}:{port}:127.0.0.1", "-X", method, "-m", str(timeout),
               "-D", "-", "-o", "-", f"https://{lab.SERVER_NAME}:{port}{path}"]
    for key, value in (headers or {}).items():
        command += ["-H", f"{key}: {value}"]
    if body is not None:
        command += ["-H", "content-type: application/json", "-d", json.dumps(body)]
    result = common.ssh(by_name(name), " ".join(shlex.quote(item) for item in command),
                        check=False, timeout=int(timeout) + 30)
    output = result.stdout.decode(errors="replace")
    head, _, text = output.partition("\r\n\r\n")
    while text.startswith("HTTP/"):
        head, _, text = text.partition("\r\n\r\n")
    fields = head.split()
    status = int(fields[1]) if head.startswith("HTTP/") and len(fields) > 1 else 0
    return status, head, text


def kill_service(name: str) -> None:
    service = service_of(name)
    remote(name, f"systemctl kill -s KILL {service}; systemctl stop {service}")


def start_service(name: str, **_kwargs) -> int:
    remote(name, f"systemctl start {service_of(name)}")
    return 0


def terminate_service(name: str, wait: float = 10.0) -> None:
    remote(name, f"systemctl stop {service_of(name)}", timeout=int(wait) + 60)


def freeze_service(name: str, frozen: bool) -> None:
    remote(name, f"systemctl kill -s {'STOP' if frozen else 'CONT'} {service_of(name)}")


def restart_service(name: str, **_kwargs) -> int:
    remote(name, f"systemctl restart {service_of(name)}")
    return 0


def service_pid(name: str) -> int | None:
    text = remote(name, f"systemctl show {service_of(name)} -p MainPID --value")
    return int(text) if text.isdigit() and int(text) > 0 else None


def set_limits(name: str, cpu_percent: int | None = None,
               memory_mb: int | None = None) -> None:
    properties = []
    if cpu_percent is not None:
        properties.append("CPUQuota=" + (f"{cpu_percent}%" if cpu_percent < 100 else ""))
    if memory_mb is not None:
        properties.append("MemoryMax=" + (f"{memory_mb}M" if memory_mb < 1000 else "infinity"))
    remote(name, f"systemctl set-property --runtime {service_of(name)} "
                 + " ".join(properties))


NETEM_IN = (
    "modprobe ifb 2>/dev/null; ip link add ifb0 type ifb 2>/dev/null; ip link set ifb0 up; "
    "tc qdisc add dev {device} handle ffff: ingress 2>/dev/null; "
    "tc filter replace dev {device} parent ffff: protocol all prio 1 u32 match u32 0 0 "
    "action mirred egress redirect dev ifb0; ")


def device(name: str, network: str) -> str:
    # A reader host reaches the caches at their public addresses. Shaping its
    # public interface would shape the control connection of this tool too.
    if by_name(name)["role"] == "load":
        raise lab.LabError("the link of a reader host is not shaped on Linode")
    if network != "vlan":
        raise lab.LabError("only the private network of a node is shaped on Linode")
    return "eth1"


def deployed_settings() -> dict:
    """The settings that the deploy tool last rendered (its settings file),
    for the host qdisc that the nodes carry."""
    try:
        saved = json.loads(deploy.SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    return {**deploy.DEFAULT_SETTINGS, **saved}


def host_qdisc() -> str:
    return deploy.class_plan(deployed_settings())["host_qdisc"]


def heal_node(node: dict, settings: dict | None = None) -> None:
    """Take the faults off a node's VLAN interface and put the deploy's host
    qdisc back on it: deleting the root alone would leave the cache mesh
    without the class qdisc for the rest of the run."""
    common.ssh(node, "tc qdisc del dev ifb0 root 2>/dev/null; true", check=False)
    deploy.apply_host_qdisc(node, settings or deployed_settings(), clear=("eth1",))


def set_net(name: str, network: str, direction: str, spec: str | None) -> None:
    dev = device(name, network)
    if spec in (None, "clear"):
        heal_node(by_name(name))
        return
    words = " ".join(word for word in spec.split() if word != "ordered")
    # Delete first: `replace` keeps the netem options that it is not given.
    # What the node sends passes the fault and then the host qdisc.
    script = ""
    if direction in ("out", "both"):
        script += deploy.netem_under_host_qdisc(dev, words, host_qdisc())
    if direction in ("in", "both"):
        script += (NETEM_IN.format(device=dev) + "tc qdisc del dev ifb0 root 2>/dev/null; "
                   f"tc qdisc add dev ifb0 root netem {words}; ")
    remote(name, script, check=True)


def set_link(name: str, network: str, up: bool) -> None:
    """Cut the private network of a node with total loss, or restore it. The
    interface stays up."""
    if up:
        set_net(name, network, "both", None)
    else:
        set_net(name, network, "both", "limit 1000 loss 100%")


def heal_all() -> None:
    nodes = [by_name(name) for name in lab.names()]
    settings = deployed_settings()
    common.parallel(nodes, lambda item: heal_node(item, settings))


def join_load(host: str, entry: str, stream: int, rate: int, seconds: int,
              label: str) -> dict:
    node = by_name(host)
    common.rsync_to(node, [str(HERE / "join-load.py")], "/usr/local/bin/")
    text = common.ssh_text(node, (
        f"ulimit -n 65536; python3 /usr/local/bin/join-load.py "
        f"--url {lab.base_url(entry)}/live/{stream}/master.m3u8 --rate {rate} "
        f"--seconds {seconds} --ca {REMOTE_CA} --server-name {lab.SERVER_NAME} "
        f"--label {label} --processes 4"), check=True, timeout=seconds + 180)
    return json.loads(text.strip().splitlines()[-1])


def node_log(name: str) -> str:
    return remote(name, f"journalctl -u {service_of(name)} --since '-10 min' --no-pager "
                        "| grep -F 'every playlist slot' | tail -200", timeout=60)


def set_clock(name: str, offset_seconds: int | None) -> None:
    """Step the clock of an instance away from true time, or give it back to
    chrony."""
    if offset_seconds is None:
        remote(name, "systemctl start chrony 2>/dev/null || systemctl start chronyd; "
                     "sleep 1; chronyc makestep >/dev/null; chronyc -c tracking | cut -d, -f5",
               timeout=60)
        return
    remote(name, "systemctl stop chrony 2>/dev/null || systemctl stop chronyd; "
                 f"date -s @$(( $(date +%s) + {int(offset_seconds)} )) >/dev/null", check=True)


def fill_disk(name: str, full: bool) -> int:
    """Fill the root disk of an instance with one file, or remove the file.
    Returns the megabytes that were free."""
    if not full:
        remote(name, "rm -f /var/needletail-disk-full.bin")
        return 0
    free = remote(name, "df -m --output=avail / | tail -1").strip()
    remote(name, "fallocate -l $(df -B1 --output=avail / | tail -1) "
                 "/var/needletail-disk-full.bin 2>/dev/null || "
                 "dd if=/dev/zero of=/var/needletail-disk-full.bin bs=1M 2>/dev/null; "
                 "df -m --output=avail / | tail -1", timeout=300)
    return int(free) if free.isdigit() else 0


def unavailable(*_args, **_kwargs):
    raise lab.LabError("this fault cannot be injected on the Linode lab")


SAMPLE = r"""
service=needletail-mesh
systemctl cat needletail-contrib.service >/dev/null 2>&1 && service=needletail-contrib
pid=$(systemctl show $service.service -p MainPID --value)
group=/sys/fs/cgroup$(systemctl show $service.service -p ControlGroup --value)
usage=$(awk '/^usage_usec/{print $2}' $group/cpu.stat 2>/dev/null)
throttled=$(awk '/^throttled_usec/{print $2}' $group/cpu.stat 2>/dev/null)
memory=$(cat $group/memory.current 2>/dev/null)
anon=$(awk '/^anon /{print $2}' $group/memory.stat 2>/dev/null)
oom=$(awk '/^oom_kill /{print $2}' $group/memory.events 2>/dev/null)
fds=$(ls /proc/$pid/fd 2>/dev/null | wc -l)
threads=$(awk '/^Threads:/{print $2}' /proc/$pid/status 2>/dev/null)
udp=$(awk '/^Udp: [0-9]/{print $6}' /proc/net/snmp)
steal=$(awk '/^cpu /{print $9" "$2+$3+$4+$5+$6+$7+$8+$9}' /proc/stat)
echo "${pid:-0} ${usage:-0} ${throttled:-0} ${memory:-0} ${anon:-0} ${oom:-0} ${fds:-0} ${threads:-0} ${udp:-0} $steal"
"""

_samples: dict[str, tuple[float, dict]] = {}
_samples_lock = threading.Lock()


def sample(name: str, max_age: float = 1.5) -> dict:
    """Counters of the service of one node, read over SSH."""
    with _samples_lock:
        cached = _samples.get(name)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
    fields = remote(name, SAMPLE, timeout=40).split()
    keys = ("pid", "usage_usec", "throttled_usec", "memory_bytes", "memory_anon_bytes",
            "memory_oom_kill", "fds", "threads", "udp_rcvbuf_errors", "steal_ticks",
            "total_ticks")
    row = {key: int(value) if value.isdigit() else 0 for key, value in zip(keys, fields)}
    row["at"] = time.time()
    row["running"] = row.get("pid", 0) > 0
    row["sockets"] = 0
    with _samples_lock:
        _samples[name] = (time.time(), row)
    return row


class RemoteSampler:
    """Samples every node every five seconds, as `edge_lab.Sampler` does."""

    def __init__(self, path: pathlib.Path, interval: float = 5.0, app: bool = True):
        self.path = path
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.samples: dict[str, list[dict]] = {}

    def start(self) -> "RemoteSampler":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        return self

    def stop(self) -> dict:
        self.stop_event.set()
        self.thread.join(timeout=60)
        return self.summary()

    def loop(self) -> None:
        with open(self.path, "a") as handle:
            while not self.stop_event.is_set():
                started = time.time()
                try:
                    rows = common.parallel(
                        [by_name(name) for name in lab.names()],
                        lambda item: sample(item["name"], max_age=0))
                except Exception as error:  # noqa: BLE001 - a sampler must not die
                    rows = {"error": {"at": started, "message": str(error)[:200]}}
                for name, row in rows.items():
                    self.samples.setdefault(name, []).append(row)
                handle.write(json.dumps(rows) + "\n")
                handle.flush()
                self.stop_event.wait(max(0.5, self.interval - (time.time() - started)))


# ------------------------------------------------------------------ readers

class RemoteReaderJob(core.ReaderJob):
    """`needletail-reader` on the load host of a region."""

    remote_report = ""
    host_node: dict = {}

    def wait(self, grace: float = 40.0) -> None:
        try:
            self.process.wait(timeout=max(1.0, self.started_at + self.seconds + grace
                                          - time.time()))
        except subprocess.TimeoutExpired:
            self.process.kill()

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            common.ssh(self.host_node, f"pkill -f {shlex.quote(self.remote_report)} || true",
                       check=False)

    def report(self) -> dict:
        if self._report is None:
            text = common.ssh_text(self.host_node, f"cat {self.remote_report}", check=False,
                                   timeout=120)
            try:
                self._report = json.loads(text)
                self.report_path.write_text(text)
                rows = [line.split() for line in common.ssh_text(
                    self.host_node, f"cat {self.remote_report[:-5]}.cpu", check=False).splitlines()]
                if len(rows) == 2:
                    busy = float(rows[1][0]) - float(rows[0][0])
                    idle = float(rows[1][1]) - float(rows[0][1])
                    percent = round(100 * busy / max(busy + idle, 1), 1)
                    self._report.setdefault("summary", {})["load_host_busy_percent"] = percent
                    if percent > 80:
                        print(f"      (the load host {self.host_node['name']} was {percent}% "
                              f"busy during {self.key}: its readers may stall for want of "
                              "CPU on the load host)", flush=True)
            except ValueError:
                self._report = {"summary": {"readers": self.count, "readers_ok": 0,
                                            "no_report": True}, "readers": []}
        return self._report


def remote_readers(self, key: str, stream: int, count: int, seconds: int,
                   host: str = "lon-readers", entries: list[str] | None = None,
                   site: str | None = None, **options) -> core.ReaderJob:
    host_node = by_name(host)
    site = site or host_node["site"]
    entries = entries or lab.distributors(site)
    label = f"{self.scenario.name}-{key}"
    remote_report = f"{REMOTE_RUNS}/{label}.json"
    command = [READER]
    for entry in entries:
        command += ["--entry", lab.base_url(entry)]
    command += ["--stream", str(stream), "--session", label, "--readers", str(count),
                "--duration-seconds", str(seconds), "--tls-ca", REMOTE_CA,
                "--server-name", lab.SERVER_NAME, "--report", remote_report]
    for name, value in options.items():
        flag = "--" + name.replace("_", "-")
        if value is True:
            command.append(flag)
        elif value is not None and value is not False:
            command += [flag, str(value)]
    # The load host must not be the limit of a test: record how busy its
    # CPUs were while these readers ran.
    stat = "awk '/^cpu /{print $2+$3+$4+$6+$7+$8+$9, $5+$6}' /proc/stat"
    script = (f"mkdir -p {REMOTE_RUNS}; rm -f {remote_report}; ulimit -n 65536; "
              f"{stat} >{REMOTE_RUNS}/{label}.cpu; "
              + " ".join(shlex.quote(item) for item in command)
              + f" >{REMOTE_RUNS}/{label}.log 2>&1; {stat} >>{REMOTE_RUNS}/{label}.cpu; true")
    process = subprocess.Popen(
        ["ssh", *common.ssh_base(), f"root@{host_node['public_ipv4']}", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    job = RemoteReaderJob(key, host, stream, count, seconds,
                          self.directory / f"{key}.reader.json", process, time.time())
    job.remote_report = remote_report
    job.host_node = host_node
    self.jobs[key] = job
    self.event(f"{count} readers of stream {stream} start on {host_node['name']} ({key})")
    return job


# ------------------------------------------------------------------ sources

def deployed(key: str):
    """The value of a setting that the scenario in hand deployed."""
    for item in _deployed["overrides"] or []:
        name, _, value = item.partition("=")
        if name == key:
            return json.loads(value)
    return None


def album_directory() -> str:
    tracks = deployed("daw_album_tracks") or 1
    return f"{REMOTE_MEDIA}/daw-tracks" if tracks == 1 else f"{REMOTE_MEDIA}/daw-tracks-{tracks}"


def start_sources(seconds: int = 6 * 3600, **_kwargs) -> None:
    contributor = common.nodes_by_role(state(), "contributor")[0]
    source = by_name("lon-readers")
    target = contributor["vlan_ipv4"]
    if deployed("hls_renditions"):
        # An encoder that sends a ladder: each rendition under the key of
        # its stream.
        inputs = " ".join(f"-re -stream_loop -1 -i {REMOTE_MEDIA}/video/{path.name}"
                          for _stream, path, *_ in lab.LADDER)
        outputs = " ".join(
            f"-map {index}:v:0 -map {index}:a:0 -c copy -t {seconds} -f flv "
            f"rtmp://{target}:{RTMP_PORT}/live/{stream}"
            for index, (stream, *_rest) in enumerate(lab.LADDER))
        video = f"ffmpeg -hide_banner -loglevel error -nostdin {inputs} {outputs}"
    else:
        video = (f"ffmpeg -hide_banner -loglevel error -nostdin -re -stream_loop -1 "
                 f"-i {REMOTE_MEDIA}/video/{lab.VIDEO_CLIP.name} -t {seconds} "
                 f"-map 0:v:0 -map 0:a:0 -c copy "
                 f"-f flv rtmp://{target}:{RTMP_PORT}/live/needletail-lab")
    common.ssh(source, (
        f"mkdir -p {REMOTE_RUNS}; pgrep -x ffmpeg >/dev/null || "
        f"(nohup {video} >{REMOTE_RUNS}/source-video.log 2>&1 </dev/null &)"))
    # The audio source encodes each track twice. With eight tracks it takes
    # most of a CPU, so it runs on the load host and not beside the
    # contributor: on the one vCPU of the contributor it produced a ninth of
    # the parts.
    common.ssh(source, (
        f"mkdir -p {REMOTE_RUNS}; pgrep -x daw-test-source >/dev/null || "
        f"(RUST_LOG=info DAW_TEST_SOURCE_START_UNIX_NS=$(( $(date +%s%N) + 3000000000 )) "
        f"nohup {DAW_SOURCE} --direct-contributor --loop-prepared-pcm {target}:27100 "
        f"{seconds} {album_directory()} >{REMOTE_RUNS}/source-audio.log 2>&1 "
        f"</dev/null &)"))


def stop_sources() -> None:
    contributor = common.nodes_by_role(state(), "contributor")[0]
    common.ssh(by_name("lon-readers"), "pkill -x ffmpeg; pkill -x daw-test-source; true",
               check=False)
    common.ssh(contributor, "pkill -x daw-test-source || true", check=False)


def prepare() -> None:
    """Install ffmpeg on the source host and upload the clip and the audio."""
    contributor = common.nodes_by_role(state(), "contributor")[0]
    source = by_name("lon-readers")
    for path in (lab.VIDEO_CLIP, lab.DAW_TRACKS):
        if not path.exists():
            sys.exit(f"source material is missing: {path}")
    common.ssh(source, (
        "export DEBIAN_FRONTEND=noninteractive; "
        "command -v ffmpeg >/dev/null 2>&1 || "
        "(apt-get update -q >/dev/null 2>&1; apt-get install -y -q ffmpeg >/dev/null 2>&1); "
        f"mkdir -p {REMOTE_MEDIA}/video; ffmpeg -version | head -1"), timeout=900)
    common.rsync_to(source, [str(lab.VIDEO_CLIP)], f"{REMOTE_MEDIA}/video/", timeout=1800)
    daw_source = common.WORKSPACE / "daw-nexus" / "target" / "x86_64-unknown-linux-gnu" \
        / "release" / "daw-test-source"
    common.rsync_to(source, [str(daw_source)], "/usr/local/bin/")
    common.ssh(source, f"mkdir -p {REMOTE_MEDIA}/daw-tracks")
    common.rsync_to(source, [str(lab.DAW_TRACKS) + "/"],
                    f"{REMOTE_MEDIA}/daw-tracks/", timeout=1800)
    ladder = [str(path) for _stream, path, *_ in lab.LADDER[1:] if path.exists()]
    if ladder:
        common.rsync_to(source, ladder, f"{REMOTE_MEDIA}/video/", timeout=1800)
    for tracks in (2, 8):
        for album in sorted(lab.DAW_TRACKS.parent.glob(
                f"daw-nexus-album-{tracks}-track-pcm-*"))[-1:]:
            common.ssh(source, f"mkdir -p {REMOTE_MEDIA}/daw-tracks-{tracks}")
            common.rsync_to(source, [str(album) + "/"],
                            f"{REMOTE_MEDIA}/daw-tracks-{tracks}/", timeout=3600)
    for host in ("lon-readers", "new-readers"):
        common.ssh(by_name(host), f"{READER} --help >/dev/null && mkdir -p {REMOTE_RUNS}")
    print(f"prepared: {lab.VIDEO_CLIP.name} and {lab.DAW_TRACKS.name} on {source['name']}; "
          f"the contributor is {contributor['name']}")


# ------------------------------------------------------------------ the run

# What this process has deployed. `None`: nothing yet, so the first scenario
# deploys its settings whatever an earlier run left on the lab. A run that
# began on the settings of the run before it played 17 streams where it
# meant to play three.
_deployed = {"overrides": None}


def deploy_settings(overrides: list[str]) -> None:
    """Restart the services with the settings of a scenario."""
    command = ["python3", str(HERE / "linode-small-mesh-deploy.py"), "restart"]
    for key, value in SETTINGS.items():
        command += ["--set", f"{key}={json.dumps(value)}"]
    for item in overrides:
        command += ["--set", item]
    # A source does not survive a restart of the contributor: stop both, and
    # `restore` starts them again.
    stop_sources()
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise lab.LabError(f"deploy restart failed: {result.stderr[-600:]}")
    _deployed["overrides"] = list(overrides)
    time.sleep(8)


def ensure_lab(overrides: list[str], fresh: bool = False, audio: str = "daw") -> None:
    if overrides != _deployed["overrides"]:
        print(f"  (restarting the services with {overrides or 'the settings of the run'})",
              flush=True)
        deploy_settings(overrides)
    restore()


def restore() -> None:
    heal_all()

    def recover(item: dict) -> None:
        service = service_of(item["name"])
        common.ssh(item, (
            "rm -f /var/needletail-disk-full.bin; "
            "systemctl is-active -q chrony || (systemctl start chrony; sleep 1; "
            "chronyc makestep >/dev/null); "
            f"systemctl kill -s CONT {service} 2>/dev/null; "
            f"systemctl set-property --runtime {service} CPUQuota= MemoryMax=infinity; "
            f"systemctl is-active -q {service} || systemctl start {service}"), check=False)

    common.parallel([by_name(name) for name in lab.names()], recover)
    start_sources()
    for stream, site in ((core.VIDEO, "london"), (core.AUDIO, "london"),
                         (core.VIDEO, "newyork")):
        lab.wait_live(stream, site, 90)
    core.idle_edges(40)


def local_player(ctx, key: str, url: str, seconds: int, host: str = "lon-readers",
                 wait: bool = True, extra: list[str] | None = None):
    """hls.js on the operator host, across the public Internet to the lab."""
    report = ctx.directory / f"{key}.hlsjs.json"
    report.unlink(missing_ok=True)
    command = ["node", str(campaign.PLAYERS / "hlsjs-play.mjs"), "--url", url,
               "--seconds", str(seconds), "--report", str(report), "--quiet",
               "--session-id", f"{ctx.scenario.name}-{key}", *(extra or [])]
    log = open(ctx.directory / f"{key}.hlsjs.log", "ab")
    process = subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                               start_new_session=True)
    ctx.event(f"hls.js starts on the operator host ({key})")
    if wait:
        try:
            process.wait(timeout=seconds + 90)
        except subprocess.TimeoutExpired:
            process.kill()
    return process, report


def install() -> pathlib.Path:
    """Replace the local lab with the Linode lab under the campaign."""
    directory = configure()
    lab.base_url = lambda name: f"https://{by_name(name)['public_ipv4']}:{HTTP_PORT}"
    lab.binary = lambda label: {"needletail-reader": READER}.get(label, label)
    lab.ca_file = lambda: REMOTE_CA
    lab.curl = curl
    lab.kill_service = kill_service
    lab.start_service = start_service
    lab.terminate_service = terminate_service
    lab.freeze_service = freeze_service
    lab.restart_service = restart_service
    lab.service_pid = service_pid
    lab.set_limits = set_limits
    lab.set_net = set_net
    lab.set_link = set_link
    lab.heal_all = heal_all
    lab.set_clock = set_clock
    lab.node_log = node_log
    lab.join_load = join_load
    lab.fill_disk = fill_disk
    lab.set_wan = unavailable
    lab.set_wan_link = unavailable
    lab.set_tcp_per_packet = lambda *_args, **_kwargs: None
    lab.start_sources = start_sources
    lab.stop_sources = stop_sources
    lab.cgroup_dir = lambda name: name
    lab.read_cgroup = lambda name: sample(name) if name else {}
    lab.read_process = lambda pid: {}
    lab.NANODE = {"cpu_percent": 100, "memory_mb": 1024, "instance": "Linode g6-nanode-1"}
    # Every Linode node, the contributor too, has the limits of its instance.
    lab.CONTRIBUTOR_LIMITS = lab.NANODE
    real_sampler = lab.Sampler
    RemoteSampler.summary = real_sampler.summary
    lab.Sampler = RemoteSampler
    core.Context.readers = remote_readers
    core.ensure_lab = ensure_lab
    core.restore = restore
    core.binary_digests = lambda: {"deployed_from": "x86_64-unknown-linux-gnu release builds"}
    campaign.hlsjs = local_player
    return directory


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command, arguments = sys.argv[1], sys.argv[2:]
    if command == "settings":
        print(" ".join("--set " + shlex.quote(f"{key}={json.dumps(value)}")
                       for key, value in SETTINGS.items()))
        return
    directory = install()
    if command == "prepare":
        prepare()
    elif command == "stop-sources":
        stop_sources()
    elif command == "list":
        for name in LINODE_SET:
            item = core.REGISTRY.get(name)
            print(f"{name:30} {item.question if item else '(not defined)'}")
    elif command == "run":
        chosen = arguments or LINODE_SET
        out = directory / "scenarios"
        for name in chosen:
            item = core.REGISTRY.get(name)
            if item is None:
                sys.exit(f"no scenario named {name}")
            try:
                core.run_scenario(item, out)
            except lab.LabError as error:
                print(f"  ({name} did not run: {error})", flush=True)
            try:
                restore()
            except (lab.LabError, RuntimeError) as error:
                print(f"  (restore failed: {error})", flush=True)
        campaign.print_summary(out)
        print(f"evidence: {out}")
    elif command == "summary":
        campaign.print_summary(directory / "scenarios")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
