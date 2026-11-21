#!/usr/bin/env python3
"""RaptorQ FEC UDP to clients at an edge, on Linode: the steps of
`client-udp-lab.py` on real instances.

One London region of dedicated instances (deploy/linode-small-mesh/
topology-client-udp.json). The load host runs the sources (the DAW test
source for Opus and FLAC, the AEP1 sender for 24-bit PCM, and ffmpeg for
the 4K video over RTMP) and the clients: for each stream one
`needletail-udp-client`, assigned an edge by a distributor's entry point and
checking every part against the edge's LL-HLS part, and beside it one LL-HLS
reader. Loss is put on the client UDP datagrams only: a netem on the ingress
of the load host's public interface that matches the edges' client UDP
port, so that SSH and HTTP are not shaped.

  linode-client-udp.py settings     the deploy settings (for `push --set`)
  linode-client-udp.py prepare      install ffmpeg, the clip and the audio
  linode-client-udp.py run [--seconds 45] [--steps loss-0,loss-1,loss-5,drain]
  linode-client-udp.py stop

The whole run:

  scripts/linode-small-mesh-lab.py up --topology deploy/linode-small-mesh/topology-client-udp.json
  scripts/linode-small-mesh-lab.py watchdog --hours 1.5
  scripts/linode-small-mesh-deploy.py push $(scripts/linode-client-udp.py settings)
  scripts/linode-client-udp.py prepare
  scripts/linode-client-udp.py run
  scripts/linode-small-mesh-lab.py down

Evidence: target/linode-qualification/client-udp/<run>/.
"""

from __future__ import annotations

import json
import pathlib
import shlex
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import small_mesh_common as common  # noqa: E402
import edge_lab as lab  # noqa: E402

REMOTE_CA = "/usr/local/share/ca-certificates/needletail-qualification.crt"
REMOTE_MEDIA = "/var/lib/needletail-test-media"
REMOTE_RUNS = "/var/lib/needletail-runs/client-udp"
HTTP_PORT = 19444
UDP_PORT = 19446
RTMP_PORT = 19350
STREAMS = {"video-4k": 1, "flac": 2001, "opus": 3001, "pcm": 4001}
SETTINGS = {
    **lab.DEFAULT_OVERRIDES,
    "playback_address": "public",
    "daw_hls_formats": "flac,opus,pcm",
    "client_udp_port": UDP_PORT,
}
LOSS = {"loss-0": 0.0, "loss-1": 1.0, "loss-5": 5.0, "drain": 0.0}


def state() -> dict:
    return common.load_state()


def node(name: str) -> dict:
    return state()["nodes"][name]


def load_host() -> dict:
    return next(item for item in state()["nodes"].values() if item["role"] == "load")


def ssh(item: dict, command: str, timeout: int = 120, check: bool = False) -> str:
    return common.ssh_text(item, command, check=check, timeout=timeout)


def settings() -> None:
    print(" ".join(f"--set {key}={json.dumps(value, separators=(',', ':'))}"
                   for key, value in SETTINGS.items()))


def prepare() -> None:
    host = load_host()
    for path in (lab.VIDEO_CLIP, lab.DAW_TRACKS, lab.AUDIO_PCM):
        if not path.exists():
            sys.exit(f"source material is missing: {path}")
    ssh(host, ("export DEBIAN_FRONTEND=noninteractive; command -v ffmpeg >/dev/null 2>&1 || "
               "(apt-get update -q >/dev/null 2>&1; apt-get install -y -q ffmpeg >/dev/null 2>&1); "
               f"mkdir -p {REMOTE_MEDIA}/video {REMOTE_MEDIA}/daw-tracks {REMOTE_RUNS}; "
               "ffmpeg -version | head -1"), timeout=900, check=True)
    common.rsync_to(host, [str(lab.VIDEO_CLIP)], f"{REMOTE_MEDIA}/video/", timeout=1800)
    common.rsync_to(host, [str(lab.DAW_TRACKS) + "/"], f"{REMOTE_MEDIA}/daw-tracks/",
                    timeout=1800)
    common.rsync_to(host, [str(lab.AUDIO_PCM)], f"{REMOTE_MEDIA}/", timeout=1800)
    print(ssh(host, "needletail-udp-client --help | head -1; needletail-reader --help | head -1; "
                    "daw-test-source --help 2>&1 | head -1; ls -la " + REMOTE_MEDIA))


def start_sources(seconds: int) -> None:
    host = load_host()
    contributor = next(item for item in state()["nodes"].values()
                       if item["role"] == "contributor")
    target = contributor["vlan_ipv4"]
    video = (f"ffmpeg -hide_banner -loglevel error -nostdin -re -stream_loop -1 "
             f"-i {REMOTE_MEDIA}/video/{lab.VIDEO_CLIP.name} -t {seconds} "
             f"-map 0:v:0 -map 0:a:0 -c copy -f flv rtmp://{target}:{RTMP_PORT}/live/1")
    daw = (f"env RUST_LOG=info DAW_TEST_SOURCE_START_UNIX_NS=$(( $(date +%s%N) + 3000000000 )) "
           f"daw-test-source --direct-contributor --loop-prepared-pcm {target}:27100 "
           f"{seconds} {REMOTE_MEDIA}/daw-tracks")
    pcm = (f"aep1-48k-probe send --target {target}:27100 "
           f"--session-id $(( $(date +%s%N) + 2000000000 )) --group-id 0 "
           f"--duration-seconds {seconds} --payload pcm --channels 2 --group-channels 8 "
           f"--pcm-s24le-file {REMOTE_MEDIA}/{lab.AUDIO_PCM.name} "
           f"--repair-percent 12 --min-repair-symbols 1")
    script = f"mkdir -p {REMOTE_RUNS}; "
    for name, command in (("video", video), ("daw", daw), ("pcm", pcm)):
        script += f"(nohup {command} >{REMOTE_RUNS}/source-{name}.log 2>&1 </dev/null &); "
    ssh(host, script, check=True)


def stop() -> None:
    ssh(load_host(), "pkill -x ffmpeg; pkill -x daw-test-source; pkill -x aep1-48k-probe; "
                     "pkill -x needletail-udp-client; pkill -x needletail-reader; "
                     + clear_loss_script() + "; true")


def clear_loss_script() -> str:
    return "tc qdisc del dev ifb0 root 2>/dev/null; tc qdisc del dev eth0 ingress 2>/dev/null"


def set_loss(percent: float) -> None:
    """Random loss on the client UDP datagrams that reach the load host."""
    host = load_host()
    if not percent:
        ssh(host, clear_loss_script() + "; true")
        return
    script = (
        clear_loss_script() + "; "
        "modprobe ifb 2>/dev/null; ip link add ifb0 type ifb 2>/dev/null; ip link set ifb0 up; "
        "tc qdisc add dev eth0 handle ffff: ingress && "
        "tc filter add dev eth0 parent ffff: protocol ip prio 1 u32 "
        f"match ip protocol 17 0xff match ip sport {UDP_PORT} 0xffff "
        "action mirred egress redirect dev ifb0 && "
        f"tc qdisc add dev ifb0 root netem limit 100000 loss random {percent}%")
    ssh(host, script, check=True)


def wait_live(entry: dict, stream: int, timeout: float = 90) -> bool:
    host = load_host()
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = ssh(host, (f"curl -s --cacert {REMOTE_CA} --resolve mesh.needletail.test:{HTTP_PORT}:"
                         f"{entry['public_ipv4']} https://mesh.needletail.test:{HTTP_PORT}"
                         f"/live/{stream}/stream.m3u8 | grep -c EXT-X-PART: || true"))
        if out.strip().isdigit() and int(out.strip()) > 0:
            return True
        time.sleep(2)
    return False


def run_step(step: str, seconds: int, directory: pathlib.Path, entry: dict) -> dict:
    host = load_host()
    step_dir = directory / step
    step_dir.mkdir(parents=True, exist_ok=True)
    set_loss(LOSS[step])
    base = f"https://{entry['public_ipv4']}:{HTTP_PORT}"
    commands = {}
    for name, stream in STREAMS.items():
        session = f"{step}-{name}"
        commands[f"udp-{name}"] = (
            f"needletail-udp-client --entry {base} --stream {stream} --tls-ca {REMOTE_CA} "
            f"--server-name mesh.needletail.test --duration-seconds {seconds} --verify-http "
            f"--session {session}")
        commands[f"hls-{name}"] = (
            f"needletail-reader --entry {base} --stream {stream} --tls-ca {REMOTE_CA} "
            f"--server-name mesh.needletail.test --duration-seconds {seconds} "
            f"--session {session}-hls --buffer-ms 1000 --report {REMOTE_RUNS}/{step}-hls-{name}.json")
    results: dict = {}

    def one(key: str) -> None:
        text = ssh(host, f"{commands[key]} 2>{REMOTE_RUNS}/{step}-{key}.err", timeout=seconds + 120)
        if key.startswith("hls-"):
            text = ssh(host, f"cat {REMOTE_RUNS}/{step}-{key}.json")
        (step_dir / f"{key}.json").write_text(text)
        try:
            results[key] = json.loads(text)
        except ValueError:
            results[key] = {"error": ssh(host, f"tail -c 600 {REMOTE_RUNS}/{step}-{key}.err")}

    threads = [threading.Thread(target=one, args=(key,)) for key in commands]
    for thread in threads:
        thread.start()
    drained = []
    if step == "drain":
        time.sleep(seconds / 3)
        edge = sorted(item["name"] for item in state()["nodes"].values() if item["role"] == "edge")[0]
        out = common.ssh_text(host, (
            f"curl -s -o /dev/null -w '%{{http_code}}' --cacert {REMOTE_CA} -X POST "
            f"--resolve mesh.needletail.test:{HTTP_PORT}:{entry['public_ipv4']} "
            f"-H 'content-type: application/json' -d '{{\"node_id\":\"{edge}\"}}' "
            f"https://mesh.needletail.test:{HTTP_PORT}/api/control/drain-node"), check=False)
        drained.append((edge, out.strip(), time.time()))
    for thread in threads:
        thread.join()
    for edge, _status, _at in drained:
        common.ssh_text(host, (
            f"curl -s -o /dev/null --cacert {REMOTE_CA} -X POST "
            f"--resolve mesh.needletail.test:{HTTP_PORT}:{entry['public_ipv4']} "
            f"-H 'content-type: application/json' -d '{{\"node_id\":\"{edge}\"}}' "
            f"https://mesh.needletail.test:{HTTP_PORT}/api/control/resume-node"), check=False)
    set_loss(0)
    sys.path.insert(0, str(HERE))
    import importlib.util
    spec = importlib.util.spec_from_file_location("client_udp_lab", HERE / "client-udp-lab.py")
    local = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(local)
    verdicts = {}
    for key, report in sorted(results.items()):
        if not key.startswith("udp-"):
            continue
        problems = local.judge(report, step)
        if step == "drain" and not any(edge in (report.get("caches") or [])[:1]
                                       for edge, *_ in drained):
            problems = [p for p in problems if p != "no move after the drain"]
        verdicts[key] = {"pass": not problems, "problems": problems,
                         **{k: report.get(k) for k in (
                             "received_parts", "missing_parts", "gap_parts", "recovered_parts",
                             "datagram_loss", "final_min_repair", "final_repair_permille",
                             "publish_to_arrival_ms", "available_to_arrival_ms",
                             "time_to_first_part_ms", "moves", "caches", "datagrams",
                             "source_datagrams", "payload_bytes")},
                         "identical": (report.get("verify") or {}).get("identical")}
    hls = {key: {k: (report.get("summary") or report).get(k)
                 for k in ("parts_received", "parts_missing", "stalls", "moves", "error")}
           for key, report in results.items() if key.startswith("hls-")}
    return {"step": step, "loss_percent": LOSS[step], "drained": drained, "udp": verdicts,
            "hls": hls,
            "pass": len(verdicts) == len(STREAMS) and all(v["pass"] for v in verdicts.values())}


def run(seconds: int, steps: list[str]) -> None:
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-client-udp"
    directory = common.QUALIFICATION_ROOT / "client-udp" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    entry = node("lon-dist-a")
    stop()
    start_sources(len(steps) * (seconds + 40) + 120)
    live = {name: wait_live(entry, stream) for name, stream in STREAMS.items()}
    print(json.dumps({"run": run_id, "live": live}), flush=True)
    result = {"run": run_id, "state": {name: {k: item.get(k) for k in ("role", "type", "region")}
                                       for name, item in state()["nodes"].items()},
              "settings": SETTINGS, "live": live, "steps": []}
    for step in steps:
        outcome = run_step(step, seconds, directory, entry)
        result["steps"].append(outcome)
        print(json.dumps({"step": step, "pass": outcome["pass"], "udp": {
            k: [v["pass"], v["received_parts"], v["missing_parts"], v["gap_parts"],
                v["recovered_parts"], v["identical"], len(v["moves"] or []), v["problems"][:2]]
            for k, v in outcome["udp"].items()}, "hls": outcome["hls"]}), flush=True)
        time.sleep(3)
    stop()
    # Edge counters of the client UDP service.
    host = load_host()
    counters = {}
    for item in state()["nodes"].values():
        if item["role"] in ("edge", "distributor"):
            text = ssh(host, (f"curl -s --cacert {REMOTE_CA} --resolve mesh.needletail.test:"
                              f"{HTTP_PORT}:{item['public_ipv4']} "
                              f"'https://mesh.needletail.test:{HTTP_PORT}/api/assignment?stream=1'"))
            try:
                counters[item["name"]] = json.loads(text).get("client_udp")
            except ValueError:
                counters[item["name"]] = None
    result["client_udp_counters"] = counters
    result["pass"] = all(step["pass"] for step in result["steps"])
    (directory / "result.json").write_text(json.dumps(result, indent=1) + "\n")
    print(directory / "result.json")
    print("PASS" if result["pass"] else "FAIL")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["settings", "prepare", "run", "stop"])
    parser.add_argument("--seconds", type=int, default=45)
    parser.add_argument("--steps", default="loss-0,loss-1,loss-5,drain")
    args = parser.parse_args()
    if args.command == "settings":
        settings()
    elif args.command == "prepare":
        prepare()
    elif args.command == "run":
        run(args.seconds, [step for step in args.steps.split(",") if step])
    else:
        stop()


if __name__ == "__main__":
    main()
