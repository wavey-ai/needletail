#!/usr/bin/env python3
"""Publish one H.264 programme through the small-instance Linode mesh and
measure it at viewers in each region.

A source host sends a prepared MPEG-TS file in real time to the contributor,
over RTMP by default. The contributor packages fMP4 parts. The relay DAG carries them to
the regional distributors, and each distributor replicates them to its edges
over the private VLAN. Viewers run on the listener host of each region.

Viewers:

* one continuous reader on the last edge of each region;
* for each window: one reader on every distributor and every edge, and
  `--viewers` concurrent viewers on the first `--load-edges` edges.

Every reader checks the structure and the timeline of each fMP4 part. No
reader decodes video.

Latency zero point. A video part has no capture timestamp that the mesh can
see. Each run first measures, at the first London distributor, when parts
become available in its cache. The median of that availability is the zero
point of every reader in the run. A latency in the report is therefore the
time from availability at the first hop after the contributor to receipt by
the viewer. It does not include the encoder or the part duration that the
contributor waits to fill a part.

The windows, the node samplers and the summary are those of
linode-small-mesh-soak.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import pathlib
import shlex
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import small_mesh_common as common  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "small_mesh_soak", pathlib.Path(__file__).resolve().parent / "linode-small-mesh-soak.py")
soak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soak)

PROBE = soak.PROBE
TLS = soak.TLS
HTTP_PORT = soak.HTTP_PORT
STREAM_ID = 1
GUARD_MS = 20
RIST_PORT = 27000
RTMP_PORT = 19350
CONTRIB_METRICS = (
    "curl -sk --max-time 5 https://127.0.0.1:19443/metrics | grep -E "
    "'^av_contrib_(fmp4|relay_session|mpeg_ts|publication|rist)' | grep -v '^#' | head -80")


def address(node: dict) -> str:
    return node["vlan_ipv4"].split("/")[0]


def reader(target: dict, plan: dict, offset_ms: int, window: int, deadline_ms: int) -> str:
    return (f"{PROBE} receive-hls --edge {address(target)}:{HTTP_PORT} {TLS} --transport h3 "
            f"--stream-id {STREAM_ID} --session-id {plan['session_id']} "
            f"--duration-seconds {plan['media_seconds']} --part-ms {plan['part_ms']} "
            f"--deadline-ms {deadline_ms} --render-buffer-ms 0 --tail-seconds {plan['tail']} "
            f"--start-offset-ms {offset_ms} --window-seconds {window} "
            f"--abandon-after-ms {soak.ABANDON_AFTER_MS + plan['part_ms']} "
            "--object-sequence-origin 0 --expected-audio-codec h264")


def viewers(edge: dict, plan: dict, offset_ms: int) -> str:
    return (f"{PROBE} load-hls --edge {address(edge)}:{HTTP_PORT} {TLS} --transport h3 "
            f"--stream-id {STREAM_ID} --session-id {plan['session_id']} "
            f"--duration-seconds {plan['media_seconds']} --part-ms {plan['part_ms']} "
            f"--deadline-ms {plan['load_deadline_ms']} --tail-seconds {plan['tail']} "
            f"--readers {plan['readers']} --start-offset-ms {offset_ms} "
            f"--abandon-after-ms {soak.ABANDON_AFTER_MS + plan['part_ms']} "
            f"--window-seconds {plan['window']} --object-sequence-origin 0 "
            "--expected-audio-codec h264")


def decode_check(plan: dict, node: dict) -> str:
    """Decode ten seconds of the rendition with ffmpeg, read from the HLS
    playlist of one node over HTTPS. ffmpeg reads completed segments, not
    parts, so this also checks the segments that the node serves."""
    url = (f"https://{node['name']}.{common.TLS_SERVER_NAME}:{HTTP_PORT}"
           f"/live/{STREAM_ID}/stream.m3u8")
    name = f"decode@{node['name']}"
    return (f"(ffmpeg -v error -xerror -nostdin -progress {name}.progress -i {url} -t 10 "
            f"-map 0:v:0 -f null - >{name}.log 2>&1; echo $? >{name}.rc) &\n")


def site_driver(plan: dict, distributors: list[dict], edges: list[dict]) -> str:
    session_s = plan["session_id"] // 1_000_000_000
    first_ms = plan["first_offset"] * 1000
    script = f"#!/bin/bash\ncd {plan['remote']}\n"
    if edges:
        script += soak.launch(f"c@{edges[-1]['name']}@hls-video", reader(
            edges[-1], plan, first_ms, plan["duration"], plan["media_deadline_ms"]))
    for window in range(plan["windows"]):
        offset_ms = first_ms + window * plan["window"] * 1000
        start = session_s + offset_ms // 1000 - 25
        script += f"while [ $(date +%s) -lt {start} ]; do sleep 1; done\n"
        prefix = f"w{window:02d}"
        for node in distributors + edges:
            script += soak.launch(f"{prefix}@{node['name']}@hls-video", reader(
                node, plan, offset_ms, plan["window"], plan["media_deadline_ms"]))
        if plan["readers"]:
            for edge in edges[:plan["load_edges"]]:
                script += soak.launch(f"{prefix}@{edge['name']}@load",
                                      viewers(edge, plan, offset_ms))
        if window == 0 and plan.get("decode_check") and distributors and edges \
                and distributors[0]["site"] == plan["decode_check"]:
            script += f"while [ $(date +%s) -lt {session_s + offset_ms // 1000 + 12} ]; do sleep 1; done\n"
            script += decode_check(plan, distributors[0]) + decode_check(plan, edges[-1])
    return script + "echo done >driver.done\n"


def calibrate(plan: dict, host: dict, distributor: dict, session0: int) -> dict:
    """Median availability of parts in the cache of the first distributor,
    against the provisional zero `session0`."""
    command = (f"{PROBE} receive-hls --edge {address(distributor)}:{HTTP_PORT} {TLS} "
               f"--transport h3 --stream-id {STREAM_ID} --session-id {session0} "
               f"--duration-seconds 86400 --part-ms {plan['part_ms']} --deadline-ms 600000 "
               "--render-buffer-ms 0 --tail-seconds 4 --start-offset-ms 8000 "
               "--window-seconds 6 --object-sequence-origin 0 --expected-audio-codec h264")
    output = common.ssh_text(host, command + " 2>/tmp/needletail-calibration.err; "
                             "tail -c 300 /tmp/needletail-calibration.err >&2", check=False,
                             timeout=90)
    report = json.loads(output)
    cache = report["publication_to_cache_latency_ms"]
    client = report["availability_latency_ms"]
    return {
        "distributor": distributor["name"], "parts": report["received_parts"],
        "expected_parts": report["expected_parts"],
        "verified_parts": report["fmp4_video_parts_verified"],
        "source": "cache availability header" if cache["count"] else "client receipt",
        "median_ms": cache["p50"] if cache["count"] else client["p50"],
        "min_ms": cache["min"] if cache["count"] else client["min"],
        "p99_ms": cache["p99"] if cache["count"] else client["p99"],
        "cache_to_client_p50_ms": report["cache_to_client_latency_ms"]["p50"],
    }


def start(args: argparse.Namespace) -> dict:
    state = common.load_state()
    settings = json.loads((common.ARTIFACT_DIR / "settings.json").read_text())
    contributor = common.nodes_by_role(state, "contributor")[0]
    distributors = common.nodes_by_role(state, "distributor")
    edges = common.nodes_by_role(state, "edge")
    loads = common.nodes_by_role(state, "load")
    services = [node for node in state["nodes"].values() if node["role"] != "load"]
    source_host = state["nodes"][args.source_host]
    if args.sites:
        wanted = set(args.sites.split(","))
        loads = [node for node in loads if node["site"] in wanted]

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{args.name}"
    result_dir = common.RUNS_DIR / run_id
    result_dir.mkdir(parents=True)
    remote = f"{soak.REMOTE_RUNS}/{run_id}"
    windows = -(-args.duration // args.window)
    duration = windows * args.window
    plan = {
        "schema": "needletail.linode-small-mesh-video-plan.v1", "profile": "video",
        "run_id": run_id, "name": args.name, "label": args.label,
        "lab_run": state["run_id"], "hourly_usd": state["hourly_usd"],
        "node_count": len(state["nodes"]), "remote": remote,
        "clip": args.clip, "audio": not args.no_audio, "ingest": args.ingest,
        "duration": duration, "window": args.window, "windows": windows,
        "part_ms": args.part_ms, "tail": args.tail, "readers": args.viewers,
        "load_edges": args.load_edges, "latency_guard_ms": GUARD_MS,
        "media_deadline_ms": args.deadline_ms + GUARD_MS,
        "load_deadline_ms": args.viewer_deadline_ms + GUARD_MS,
        "streams": [{"name": "video", "stream_id": STREAM_ID, "codec": "h264"}],
        "settings": settings, "source_process": [args.source_host, "ffmpeg"],
        "sampler_rules": soak.sampler_rules(distributors, edges),
        "decode_check": source_host["site"] if args.decode_check else None,
        "listener_sites": sorted({node["site"] for node in loads}),
        "nodes": {node["name"]: {key: node.get(key) for key in (
            "role", "site", "region", "type", "public_ipv4", "vlan_ipv4")}
            for node in state["nodes"].values()},
    }
    if settings["part_ms"] != args.part_ms or settings["fmp4_part_ms"] != args.part_ms:
        raise SystemExit(
            f"the mesh is deployed with part_ms={settings['part_ms']} and "
            f"fmp4_part_ms={settings['fmp4_part_ms']}; restart it with {args.part_ms}")
    # A run takes about 90 s more than its windows: arming, calibration, the
    # lead before the first window, and the reader tail.
    sampler_seconds = duration + 100

    def arm_sampler(node: dict) -> None:
        ports = " ".join(plan["sampler_rules"].get(node["name"], []))
        common.ssh(node, f"mkdir -p {remote} && cat >{soak.SAMPLER_PATH}",
                   stdin=soak.SAMPLER.encode())
        common.ssh(node, (
            f"cd {remote} && (setsid nohup python3 {soak.SAMPLER_PATH} {remote}/sampler.log "
            f"{sampler_seconds} {ports} >sampler.err 2>&1 </dev/null & echo $! >sampler.pid)"))

    def arm_load_sampler(node: dict) -> None:
        common.ssh(node, f"mkdir -p {remote} && cat >{soak.SAMPLER_PATH}",
                   stdin=soak.SAMPLER.encode())
        common.ssh(node, (
            f"cd {remote} && for cpu in $(seq 0 $(( $(nproc) - 1 ))); do "
            f"(setsid nohup python3 {soak.SAMPLER_PATH} {remote}/sampler-cpu$cpu.log "
            f"{sampler_seconds} cpu=$cpu >sampler.err 2>&1 </dev/null &); done"))

    if args.decode_check:
        # ffmpeg verifies the certificate against the system store and the
        # host name, so the listener host trusts the lab CA and resolves one
        # name for each node of its region.
        names = "\n".join(
            f"{address(node)} {node['name']}.{common.TLS_SERVER_NAME}"
            for node in distributors + edges if node["site"] == source_host["site"])
        common.ssh(source_host, (
            "update-ca-certificates >/dev/null 2>&1; "
            f"grep -q '{common.TLS_SERVER_NAME}' /etc/hosts || cat >>/etc/hosts <<'HOSTS'\n"
            f"{names}\nHOSTS"))
    # A new contributor process starts a new source epoch: object sequence 0.
    common.ssh(contributor, "systemctl restart needletail-contrib.service; sleep 3; "
                            "systemctl is-active needletail-contrib.service")
    common.parallel(services, arm_sampler)
    common.parallel(common.nodes_by_role(state, "load"), arm_load_sampler)
    (result_dir / "contributor-metrics-start.txt").write_text(
        common.ssh_text(contributor, CONTRIB_METRICS, check=False))
    (result_dir / "snapshots.json").write_text(json.dumps(
        [{"label": "start", "nodes": soak.snapshot_all(services)}]) + "\n")

    session0 = int(common.ssh_text(source_host, "date +%s%N")) + 5_000_000_000
    media_seconds = duration + 180
    audio = "-an" if args.no_audio else "-map 0:a:0?"
    read_clip = (
        f"while [ $(date +%s%N) -lt {session0} ]; do sleep 0.02; done; "
        f"ffmpeg -hide_banner -loglevel warning -nostdin -re -stream_loop -1 "
        f"-i {shlex.quote(args.clip)} -t {media_seconds} -map 0:v:0 {audio} -c copy ")
    if args.ingest == "rist":
        # The pure-Rust RIST sender of av-contrib reads MPEG-TS from stdin.
        sender = (read_clip + "-muxdelay 0 -muxpreload 0 -f mpegts - "
                  f"| /usr/local/bin/rist-send {address(contributor)}:{RIST_PORT}")
    else:
        sender = (read_clip + f"-f flv rtmp://{address(contributor)}:{RTMP_PORT}"
                  "/live/needletail-lab")
    common.ssh(source_host, (
        "pkill -x ffmpeg; pkill -x rist-send; "
        f"mkdir -p {remote} && cd {remote} && test -r {shlex.quote(args.clip)} && "
        f"(setsid nohup sh -c {shlex.quote(sender)} >sender.log 2>&1 </dev/null &)"))
    london = [node for node in distributors if node["site"] == source_host["site"]]
    time.sleep(max(0.0, session0 / 1e9 + 7.0 - time.time()))
    calibration = calibrate(plan, source_host, (london or distributors)[0], session0)
    if calibration["parts"] != calibration["expected_parts"]:
        raise RuntimeError(f"calibration reader was incomplete: {calibration}")
    plan["calibration"] = calibration
    plan["session_id"] = (session0 + int(calibration["median_ms"] * 1_000_000)
                          - GUARD_MS * 1_000_000)
    # The first window starts on a two-second boundary a little ahead of now.
    elapsed = time.time() - plan["session_id"] / 1e9
    plan["first_offset"] = int(math.ceil((elapsed + 16) / 2.0) * 2)
    plan["media_seconds"] = media_seconds

    def arm_load(node: dict) -> None:
        site = node["site"]
        driver = site_driver(
            plan, [item for item in distributors if item["site"] == site],
            [item for item in edges if item["site"] == site])
        common.ssh(node, f"mkdir -p {remote} && cat >{remote}/driver.sh", stdin=driver.encode())
        common.ssh(node, f"cd {remote} && chmod +x driver.sh && "
                         "(setsid nohup ./driver.sh >driver.log 2>&1 </dev/null &)")

    common.parallel(loads, arm_load)
    (result_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    soak.log(f"video run {run_id}: zero is {calibration['median_ms']:.1f} ms after the "
             f"sender start ({calibration['source']}); {windows} windows of {args.window} s "
             f"from offset {plan['first_offset']} s")
    return plan


def run(args: argparse.Namespace) -> None:
    plan = start(args)
    result_dir = common.RUNS_DIR / plan["run_id"]
    soak.watch(plan, result_dir)
    contributor = soak.nodes_of(plan, "contributor")[0]
    (result_dir / "contributor-metrics-end.txt").write_text(
        common.ssh_text(contributor, CONTRIB_METRICS, check=False))
    source = plan["nodes"][plan["source_process"][0]] | {"name": plan["source_process"][0]}
    common.ssh(source, "pkill -x ffmpeg; pkill -x rist-send; true", check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--clip", required=True, help="MPEG-TS file on the source host")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--ingest", choices=("rtmp", "rist"), default="rtmp")
    parser.add_argument("--part-ms", type=int, required=True)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--window", type=int, default=120)
    parser.add_argument("--viewers", type=int, default=0,
                        help="concurrent viewers on each loaded edge")
    parser.add_argument("--load-edges", type=int, default=1)
    parser.add_argument("--deadline-ms", type=int, default=1000)
    parser.add_argument("--viewer-deadline-ms", type=int, default=1000)
    parser.add_argument("--tail", type=int, default=8)
    parser.add_argument("--sites", default="", help="regions that get viewers (default all)")
    parser.add_argument("--source-host", default="lon-load")
    parser.add_argument("--decode-check", action="store_true",
                        help="decode ten seconds with ffmpeg from a distributor and an edge")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
