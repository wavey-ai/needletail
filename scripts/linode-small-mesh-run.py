#!/usr/bin/env python3
"""Run one measured publication through the small-instance Linode mesh.

One run publishes a single programme from the London contributor and observes
it simultaneously on every delivery lane:

* native UDP with RaptorQ FEC and WebTransport datagrams on each regional
  distributor (the relay-DAG leaves): the 1:1 lowest-latency lanes;
* LL-HLS over HTTP/3 on each distributor and on each cache-mesh edge, read
  locally (cache latency) and from the regional load host across the private
  VLAN (in-region listener);
* optional many-reader LL-HLS load against the edges: the 1:many lane.

Every receiver verifies media integrity itself; this tool gathers the reports,
service CPU, memory, cache-mesh wire bytes and clock state, and writes one
summary. Nothing is hidden when a lane fails: the summary carries the error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shlex
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import small_mesh_common as common  # noqa: E402

_deploy_spec = importlib.util.spec_from_file_location(
    "small_mesh_deploy", pathlib.Path(__file__).resolve().parent / "linode-small-mesh-deploy.py")
deploy = importlib.util.module_from_spec(_deploy_spec)
_deploy_spec.loader.exec_module(deploy)

PROBE = "/usr/local/bin/aep1-48k-probe"
# The epochs that this tool's sources send. The checkers (aep1-48k-probe
# receive-hls and load-hls --part-ms, soundkit-part-check) take whole ms
# parts, and with audio_part "epoch" a part is one epoch: 48 samples (1 ms)
# for the AEP1 sender and the DAW source's lossless lane, and 240-sample (5 ms)
# Opus frames from the DAW source, whose 120-sample default (2.5 ms) the
# checkers cannot count.
PROBE_EPOCH_SAMPLES = 48
DAW_LOSSLESS_EPOCH_SAMPLES = 48
# The DAW source's Opus frame and bitrate: the low-latency Opus lane's
# default, 1 ms Opus Custom (48 samples, a whole-ms part) at 320 kb/s CBR.
DAW_OPUS_FRAME_SAMPLES = 48
DAW_OPUS_BITRATE = 320000


def daw_epoch_args(lossless_samples: int, opus_samples: int,
                   opus_bitrate: int | None = None) -> str:
    """daw-test-source arguments that fix its epochs and its Opus bitrate
    (rather than rely on the defaults of the build that is installed)."""
    return (f"--lossless-epoch-samples {lossless_samples} "
            f"--opus-frame-samples {opus_samples} "
            + (f"--opus-bitrate {opus_bitrate} " if opus_bitrate else ""))


def aep1_lane(settings: dict) -> bool:
    """Whether the deploy rendered the contributor's legacy AEP1 lane, which
    alone feeds the distributors' native UDP (`receive-udp --relay`) and
    WebTransport (WAVEY-AUDIO-EPOCH) readers. The setting audio_epoch_lane
    (wave 7 item 1, default false); a settings file saved before it existed
    rendered the lane."""
    return bool(settings.get("audio_epoch_lane", True))


def assign_part_ms(settings: dict, streams: list[dict]) -> None:
    """Give each stream (with its source `epoch_samples`) the whole-ms part
    that its checkers are told; SystemExit naming every stream whose part is
    not a whole number of ms."""
    # A settings file written before a key existed lacks it: the defaults
    # fill it in (nothing saved is dropped).
    settings = {**deploy.DEFAULT_SETTINGS, **settings}
    errors = []
    for stream in streams:
        try:
            stream["part_ms"] = deploy.whole_part_ms(settings, stream["epoch_samples"],
                                                     f"the parts of {stream['name']}")
        except ValueError as error:
            errors.append(str(error))
    if errors:
        raise SystemExit("this run's checkers take whole ms parts: " + "; ".join(errors))
TLS = (f"--server-name {common.TLS_SERVER_NAME} "
       "--tls-ca /usr/local/share/ca-certificates/needletail-qualification.crt")
REMOTE_RUNS = "/var/lib/needletail-runs"
HTTP_PORT = 19444

SNAPSHOT = r"""
service=none
for candidate in needletail-mesh needletail-contrib; do
  if systemctl cat $candidate.service >/dev/null 2>&1; then service=$candidate; fi
done
port=19444; [ "$service" = needletail-contrib ] && port=19443
local_json=$(curl -sk --max-time 4 https://127.0.0.1:$port/api/mesh/local 2>/dev/null || true)
[ -n "$local_json" ] && echo "$local_json" | jq -e . >/dev/null 2>&1 || local_json=null
jq -n \
  --argjson timestamp_ns "$(date +%s%N)" \
  --arg service "$service" \
  --argjson cpu_ns "$(systemctl show $service.service -p CPUUsageNSec --value 2>/dev/null | grep -E '^[0-9]+$' || echo 0)" \
  --argjson memory_bytes "$(systemctl show $service.service -p MemoryCurrent --value 2>/dev/null | grep -E '^[0-9]+$' || echo 0)" \
  --argjson restarts "$(systemctl show $service.service -p NRestarts --value 2>/dev/null | grep -E '^[0-9]+$' || echo 0)" \
  --arg cpu_stat "$(head -1 /proc/stat)" \
  --arg udp "$(awk '/^Udp:/{n++; if (n==2) print}' /proc/net/snmp)" \
  --arg clock "$(chronyc -c tracking 2>/dev/null)" \
  --argjson eth0_tx "$(cat /sys/class/net/eth0/statistics/tx_bytes)" \
  --argjson eth0_rx "$(cat /sys/class/net/eth0/statistics/rx_bytes)" \
  --argjson eth1_tx "$(cat /sys/class/net/eth1/statistics/tx_bytes 2>/dev/null || echo 0)" \
  --argjson eth1_rx "$(cat /sys/class/net/eth1/statistics/rx_bytes 2>/dev/null || echo 0)" \
  --argjson mesh "$local_json" \
  '{timestamp_ns:$timestamp_ns,service:$service,cpu_ns:$cpu_ns,memory_bytes:$memory_bytes,
    restarts:$restarts,cpu_stat:$cpu_stat,udp:$udp,clock:$clock,
    eth0_tx:$eth0_tx,eth0_rx:$eth0_rx,eth1_tx:$eth1_tx,eth1_rx:$eth1_rx,
    mesh_fec:($mesh.mesh_fec // null),relay_session:($mesh.relay_session // null)}'
"""


def relay_port(node: dict) -> int:
    args = (common.ARTIFACT_DIR / "nodes" / f"{node['name']}.args").read_text().split("\n")
    return int(args[args.index("--fec-bind") + 1].rsplit(":", 1)[1])


def background(name: str, command: str) -> str:
    return (f"nohup {command} >{name}.json 2>{name}.err </dev/null &\n"
            f"echo $! >{name}.pid\n")


def hls_command(edge: str, stream_id: int, args: argparse.Namespace, session_id: int,
                codec: str, response_ms: int | None = None, part_ms: int | None = None) -> str:
    command = (f"{PROBE} receive-hls --edge {edge} {TLS} --transport h3 "
               f"--stream-id {stream_id} --session-id {session_id} "
               f"--duration-seconds {args.duration} --part-ms {part_ms or args.part_ms} "
               f"--deadline-ms {args.deadline_ms} --render-buffer-ms 150 "
               f"--tail-seconds {args.tail} --expected-audio-codec {codec}")
    if codec in ("opus", "soundkit-opus", "ipcm"):
        command += " --expected-pcm-channels 2"
    if response_ms:
        command += f" --response-ms {response_ms}"
    return command


def stream_plan(args: argparse.Namespace, settings: dict, group_id: int) -> dict:
    """Map the publication onto relay groups and LL-HLS stream identifiers."""
    base = settings["daw_hls_base_stream_id"]
    opaque = settings["daw_hls_packaging"] == "opaque"
    if args.source == "daw":
        formats = settings["daw_hls_formats"].split(",")
        streams = []
        for index in range(args.tracks):
            if "flac" in formats:
                streams.append({"name": f"flac{index}", "stream_id": base + index,
                                "codec": "opaque-flac" if opaque else "flac",
                                "epoch_samples": args.daw_lossless_epoch_samples})
            if "opus" in formats:
                streams.append({"name": f"opus{index}", "stream_id": base + 1000 + index,
                                "codec": args.opus_codec,
                                "epoch_samples": args.daw_opus_frame_samples})
        return {"group_id": 0, "group_count": args.tracks, "streams": streams}
    return {"group_id": group_id, "group_count": 1, "streams": [
        {"name": "flac0", "stream_id": base + group_id,
         "codec": "opaque-flac" if opaque else "flac", "epoch_samples": args.epoch_samples}]}


# A port and its video twin when the deploy splits that kind of endpoint by
# class: a fault on the audio port alone would leave 4K untouched.
class_split_ports = deploy.class_split_ports


def loss_commands(ports: int | list[int], percent: float, chain: str) -> tuple[str, str]:
    """Random loss of `percent` on the UDP ports, and the command that takes
    it away and prints how many datagrams it dropped."""
    ports = [ports] if isinstance(ports, int) else list(ports)
    probability = percent / 100.0
    apply = (f"iptables -N {chain} 2>/dev/null || iptables -F {chain}; "
             f"iptables -A {chain} -m statistic --mode random "
             f"--probability {probability} -j DROP")
    remove = (f"dropped=$(iptables -L {chain} -nvx 2>/dev/null | "
              "awk '$3 == \"DROP\" {print $1; exit}'); ")
    for port in ports:
        rule = f"INPUT ! -i lo -p udp --dport {port} -j {chain}"
        apply += f"; iptables -C {rule} 2>/dev/null || iptables -I {rule.replace('INPUT', 'INPUT 1', 1)}"
        remove += f"while iptables -C {rule} 2>/dev/null; do iptables -D {rule}; done; "
    remove += (f"iptables -F {chain} 2>/dev/null; iptables -X {chain} 2>/dev/null; "
               "echo ${dropped:-0}")
    return apply, remove


def percentile_block(report: dict, key: str) -> dict | None:
    block = report.get(key)
    if not isinstance(block, dict) or not block.get("count"):
        return None
    return {name: round(block[name], 3) for name in ("p50", "p95", "p99", "max")}


def summarize_report(report: dict) -> dict:
    lane = report.get("lane", "unknown")
    out: dict = {"lane": lane}
    if "expected_epochs" in report:
        out.update(expected=report["expected_epochs"], received=report["received_epochs"],
                   missing=report["missing_epochs"])
        out["latency_ms"] = percentile_block(report, "latency_ms")
        for key in ("raptorq_shards_recovered", "erasure_epochs", "missing_pcm_frames",
                    "discontinuity_epochs", "unexpected_payload_epochs",
                    "duplicate_or_late_epochs"):
            if key in report:
                out[key] = report[key]
    elif "expected_parts" in report:
        out.update(expected=report["expected_parts"], received=report["received_parts"],
                   missing=report["missing_parts"])
        out["latency_ms"] = percentile_block(report, "availability_latency_ms")
        out["publication_to_cache_ms"] = percentile_block(
            report, "publication_to_cache_latency_ms")
        out["cache_to_client_ms"] = percentile_block(report, "cache_to_client_latency_ms")
        mismatches = sum(value for key, value in report.items()
                         if key.endswith("_mismatches") and isinstance(value, int))
        verified = sum(value for key, value in report.items()
                       if key.endswith("_parts_verified") and isinstance(value, int))
        out.update(integrity_mismatches=mismatches, parts_verified=verified,
                   non_contiguous_pts=report.get("non_contiguous_pts"),
                   reconnects=report.get("connection_reconnects"),
                   tls_verified=report.get("tls_certificate_verified"))
    else:
        out["lane"] = "ll_hls_load"
        readers = report.get("readers_requested", 0)
        out.update(
            readers=readers,
            readers_completed=report.get("readers_completed"),
            readers_failed=report.get("readers_failed"),
            expected=readers * report.get("expected_parts_per_reader", 0),
            received=report.get("received_parts_total"),
            missing=report.get("missing_parts_total"),
            media_responses=report.get("media_responses_total"),
            parts_per_response=report.get("parts_per_response"),
            wire_mbps=round(report.get("wire_bytes_total", 0) * 8
                            / max(report.get("duration_seconds", 1), 1) / 1e6, 2),
            integrity_mismatches=sum(
                value for key, value in report.items()
                if key.endswith("_mismatches_total") and isinstance(value, int)),
            non_contiguous_pts=report.get("non_contiguous_pts_total"),
            passed=report.get("passed"),
            deadline_misses=report.get("deadline_misses_total"))
        # Distribution across readers of each reader's own p99.
        out["latency_ms"] = percentile_block(report, "availability_p99_ms_across_readers")
        out["first_part_p99_ms"] = percentile_block(
            report, "first_part_to_response_p99_ms_across_readers")
        out["final_part_p99_ms"] = percentile_block(
            report, "final_part_to_response_p99_ms_across_readers")
        out["connection_setup_ms"] = percentile_block(report, "connection_setup_ms")
        return out
    out["deadline_misses"] = report.get("deadline_misses")
    return out


def cpu_idle_fields(stat: str) -> tuple[int, int]:
    values = [int(value) for value in stat.split()[1:]]
    return sum(values), values[7] if len(values) > 7 else 0


def cpu_idle(stat: str) -> int:
    values = [int(value) for value in stat.split()[1:]]
    return values[3] + values[4]


def node_usage(before: dict, after: dict) -> dict:
    seconds = (after["timestamp_ns"] - before["timestamp_ns"]) / 1e9
    total_before, steal_before = cpu_idle_fields(before["cpu_stat"])
    total_after, steal_after = cpu_idle_fields(after["cpu_stat"])
    usage = {
        "window_seconds": round(seconds, 2),
        "service_cpu_percent": round(
            (after["cpu_ns"] - before["cpu_ns"]) / 1e9 / seconds * 100, 2),
        "steal_percent": round(
            (steal_after - steal_before) * 100 / max(total_after - total_before, 1), 2),
        "host_cpu_percent": round(100 - (cpu_idle(after["cpu_stat"]) - cpu_idle(
            before["cpu_stat"])) * 100 / max(total_after - total_before, 1), 2),
        "memory_mb": round(after["memory_bytes"] / 1048576, 1),
        "restarts_during_run": after["restarts"] - before["restarts"],
        "public_tx_kbps": round((after["eth0_tx"] - before["eth0_tx"]) * 8 / seconds / 1000, 1),
        "vlan_tx_kbps": round((after["eth1_tx"] - before["eth1_tx"]) * 8 / seconds / 1000, 1),
        "vlan_rx_kbps": round((after["eth1_rx"] - before["eth1_rx"]) * 8 / seconds / 1000, 1),
    }
    clock = after.get("clock", "").split(",")
    if len(clock) > 10:
        usage["clock_offset_ms"] = round(float(clock[4]) * 1000, 3)
        usage["clock_root_dispersion_ms"] = round(float(clock[11]) * 1000, 3)
    udp_before, udp_after = before["udp"].split(), after["udp"].split()
    if len(udp_before) > 5 and len(udp_after) > 5:
        usage["udp_receive_errors"] = int(udp_after[3]) - int(udp_before[3])
        usage["udp_rcvbuf_errors"] = int(udp_after[5]) - int(udp_before[5])
    if before.get("mesh_fec") and after.get("mesh_fec"):
        fec_before, fec_after = before["mesh_fec"], after["mesh_fec"]
        usage["cache_mesh"] = {
            key: fec_after[key] - fec_before[key] for key in (
                "tx_objects", "tx_wire_bytes", "tx_protected_bytes", "rx_wire_bytes",
                "rx_decoded_objects", "rx_decoded_bytes", "rx_repaired_objects",
                "rx_presumed_lost_source_datagrams", "rx_decode_errors",
                "rx_expired_objects") if key in fec_after
        }
    if before.get("relay_session") and after.get("relay_session"):
        relay_before, relay_after = before["relay_session"], after["relay_session"]
        usage["relay_session"] = {
            key: relay_after[key] - relay_before[key] for key in (
                "datagrams_received", "duplicate_datagrams", "decoded_objects",
                "fec_recovered_objects", "fec_recovered_source_symbols",
                "expired_objects", "datagrams_rejected") if key in relay_after
        }
    return usage


def run(args: argparse.Namespace) -> None:
    state = common.load_state()
    settings = json.loads((common.ARTIFACT_DIR / "settings.json").read_text())
    if args.source != "daw" and args.epoch_samples < 32:
        raise SystemExit("the AEP1 sender sends FLAC: --epoch-samples is at least 32")
    contributor = common.nodes_by_role(state, "contributor")[0]
    distributors = common.nodes_by_role(state, "distributor")
    edges = common.nodes_by_role(state, "edge")
    loads = common.nodes_by_role(state, "load")
    services = [node for node in state["nodes"].values()
                if node["role"] in ("contributor", "backbone", "distributor", "edge", "load")]
    if args.sites:
        wanted = set(args.sites.split(","))
        distributors = [node for node in distributors if node["site"] in wanted]
        edges = [node for node in edges if node["site"] in wanted]
        loads = [node for node in loads if node["site"] in wanted]

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{args.name}"
    result_dir = common.RUNS_DIR / run_id
    result_dir.mkdir(parents=True)
    remote = f"{REMOTE_RUNS}/{run_id}"
    group_id = 30000 + int(time.time()) % 30000
    plan = stream_plan(args, settings, group_id)
    # The checkers are told each stream's audio part, not the caches'
    # nominal part: with audio_part "epoch" a part is one source epoch.
    if not plan["streams"]:
        raise SystemExit("no LL-HLS stream to read: daw_hls_formats has neither flac nor opus")
    assign_part_ms(settings, plan["streams"])
    args.part_ms = plan["streams"][0]["part_ms"]
    opus_parts = [item["part_ms"] for item in plan["streams"] if item["name"].startswith("opus")]
    args.opus_part_ms = opus_parts[0] if opus_parts else args.part_ms

    loss_removals: list[tuple[dict, str]] = []
    if args.loss_relay:
        for node in distributors:
            apply, remove = loss_commands(
                class_split_ports(relay_port(node), settings, "relay"), args.loss_relay,
                "NTSMRELAY")
            common.ssh(node, apply)
            loss_removals.append((node, remove))
    if args.loss_mesh:
        for node in edges:
            apply, remove = loss_commands(
                class_split_ports(deploy.MESH_PORT, settings, "mesh"), args.loss_mesh,
                "NTSMMESH")
            common.ssh(node, apply)
            loss_removals.append((node, remove))

    try:
        clock_ns = int(common.ssh_text(contributor, "date +%s%N"))
        session_id = clock_ns + args.start_delay * 1_000_000_000

        # Without the AEP1 lane nothing feeds the distributors' native UDP
        # and WebTransport readers: they are not started.
        with_aep1 = aep1_lane(settings)
        if not with_aep1:
            print("audio_epoch_lane is off: no native UDP or WebTransport reader "
                  "at the distributors (their LL-HLS readers run)")

        def remote_probe_script(node: dict) -> str:
            """Every reader for the site's distributors and edges, on the load host."""
            script = ""
            targets = [item for item in distributors + edges if item["site"] == node["site"]]
            for index, target in enumerate(targets):
                address = target["vlan_ipv4"]
                prefix = f"T@{target['name']}@"
                if target["role"] == "distributor" and with_aep1:
                    formats = "flac" if args.source != "daw" else args.udp_formats
                    script += background(prefix + "udp", (
                        f"{PROBE} receive-udp --relay {address}:{relay_port(target)} "
                        f"--bind 0.0.0.0:{27101 + index} --session-id {session_id} "
                        f"--group-id {plan['group_id']} --group-count {plan['group_count']} "
                        f"--formats {formats} --duration-seconds {args.duration} "
                        f"--deadline-ms {args.deadline_ms} --tail-seconds {args.tail}"))
                    if plan["group_count"] == 1 and not args.no_webtransport:
                        script += background(prefix + "webtransport", (
                            f"{PROBE} receive-webtransport --edge {address}:{HTTP_PORT} {TLS} "
                            f"--session-id {session_id} --group-id {plan['group_id']} "
                            f"--formats {formats} --duration-seconds {args.duration} "
                            f"--deadline-ms {args.deadline_ms} --tail-seconds {args.tail}"))
                for stream in plan["streams"]:
                    command = hls_command(f"{address}:{HTTP_PORT}", stream["stream_id"],
                                          args, session_id, stream["codec"],
                                          part_ms=stream["part_ms"])
                    if args.verify_opus_crc and stream["codec"] == "soundkit-opus":
                        command += (f" --raw-part-output-dir "
                                    f"{remote}/crc@{target['name']}@{stream['name']}")
                    script += background(prefix + f"hls-local-{stream['name']}", command)
            return script

        def arm(node: dict) -> None:
            script = f"set -eu\nmkdir -p {remote}\ncd {remote}\n"
            local = f"127.0.0.1:{HTTP_PORT}"
            if args.remote_probes:
                if node["role"] != "load":
                    return
                script += remote_probe_script(node)
            if node["role"] == "distributor" and with_aep1:
                formats = "flac" if args.source != "daw" else args.udp_formats
                script += background("udp", (
                    f"{PROBE} receive-udp --relay 127.0.0.1:{relay_port(node)} "
                    f"--bind 0.0.0.0:27101 --session-id {session_id} "
                    f"--group-id {plan['group_id']} --group-count {plan['group_count']} "
                    f"--formats {formats} --duration-seconds {args.duration} "
                    f"--deadline-ms {args.deadline_ms} --tail-seconds {args.tail}"))
                if plan["group_count"] == 1 and not args.no_webtransport:
                    script += background("webtransport", (
                        f"{PROBE} receive-webtransport --edge {local} {TLS} "
                        f"--session-id {session_id} --group-id {plan['group_id']} "
                        f"--formats {formats} --duration-seconds {args.duration} "
                        f"--deadline-ms {args.deadline_ms} --tail-seconds {args.tail}"))
            if node["role"] in ("distributor", "edge"):
                for stream in plan["streams"]:
                    command = hls_command(
                        local, stream["stream_id"], args, session_id, stream["codec"],
                        part_ms=stream["part_ms"])
                    if (args.raw_parts and node["role"] == "edge"
                            and node["name"] == edges[0]["name"]
                            and stream["name"] in ("opus0", "flac0")):
                        command += f" --raw-part-output-dir {remote}/raw-{stream['name']}"
                    elif (args.verify_opus_crc and node["role"] == "edge"
                          and stream["codec"] == "soundkit-opus"):
                        command += f" --raw-part-output-dir {remote}/crc-{stream['name']}"
                    script += background(f"hls-local-{stream['name']}", command)
            if node["role"] == "load":
                site_edges = [edge for edge in edges if edge["site"] == node["site"]]
                for edge_index, edge in enumerate(site_edges):
                    target = f"{edge['vlan_ipv4']}:{HTTP_PORT}"
                    stream = plan["streams"][0]
                    if args.remote_probes:
                        args.late_join = 0
                    load_stream = next(
                        (item for item in plan["streams"]
                         if item["name"] == args.load_stream), stream)
                    if not args.remote_probes:
                        script += background(f"hls-vlan-{edge['name']}", hls_command(
                            target, stream["stream_id"], args, session_id, stream["codec"],
                            part_ms=stream["part_ms"]))
                    if args.late_join:
                        script += background(f"hls-late-{edge['name']}", hls_command(
                            target, stream["stream_id"], args, session_id,
                            stream["codec"], part_ms=stream["part_ms"])
                            + f" --start-offset-ms {args.late_join * 1000}")
                    if args.readers and (not args.load_edges
                                         or edge_index < args.load_edges):
                        command = (
                            f"{PROBE} load-hls --edge {target} {TLS} --transport h3 "
                            f"--stream-id {load_stream['stream_id']} "
                            f"--session-id {session_id} "
                            f"--duration-seconds {args.duration} "
                            f"--part-ms {load_stream['part_ms']} "
                            f"--deadline-ms {args.load_deadline_ms} "
                            f"--tail-seconds {args.tail} --readers {args.readers} "
                            f"--expected-audio-codec {load_stream['codec']}")
                        if args.response_ms:
                            command += f" --response-ms {args.response_ms}"
                        script += background(f"load-{edge['name']}", command)
            common.ssh(node, script)

        receivers = loads if args.remote_probes else distributors + edges + loads
        common.parallel(receivers, arm)
        if args.blackout_ms:
            # Drop every primary-lane datagram at each distributor for a fixed
            # window, timed on the node clock: a long-haul path outage.
            start_ns = session_id + args.blackout_at * 1_000_000_000

            def schedule_blackout(node: dict) -> None:
                rules = [f"INPUT ! -i lo -p udp --dport {port} -j DROP"
                         for port in class_split_ports(relay_port(node), settings, "relay")]
                insert = "; ".join(f"iptables -I {rule}" for rule in rules)
                delete = "; ".join(f"iptables -D {rule}" for rule in rules)
                common.ssh(node, (
                    f"nohup sh -c 'while [ $(date +%s%N) -lt {start_ns} ]; do sleep 0.01; done; "
                    f"{insert}; sleep {args.blackout_ms / 1000}; {delete}; "
                    f"date +%s%N >{remote}/blackout-end' >/dev/null 2>&1 </dev/null &"))

            targets = [node for node in distributors
                       if not args.blackout_nodes or node["name"] in args.blackout_nodes.split(",")]
            common.parallel(targets, schedule_blackout)
        before = common.parallel(services, lambda node: json.loads(
            common.ssh_text(node, SNAPSHOT, timeout=60)))
        margin = (session_id - int(common.ssh_text(contributor, "date +%s%N"))) / 1e9
        if margin < 2:
            raise RuntimeError(f"receiver setup left only {margin:.1f} s before start")

        if args.source == "daw":
            source_command = (
                f"mkdir -p {remote}; cd {remote}; "
                f"RUST_LOG=info DAW_TEST_SOURCE_START_UNIX_NS={session_id} "
                f"DAW_TEST_SOURCE_CAPACITY_OUTPUT={remote}/daw-capacity.ndjson "
                f"/usr/local/bin/daw-test-source --direct-contributor --prepared-pcm "
                + daw_epoch_args(args.daw_lossless_epoch_samples, args.daw_opus_frame_samples,
                                 args.daw_opus_bitrate)
                + f"127.0.0.1:27100 {args.duration} {shlex.quote(args.track_directory)} "
                f">source.log 2>source.err; echo exit=$?")
        else:
            pcm = (f"--pcm-s24le-file {shlex.quote(args.pcm_file)} "
                   if args.source == "lori" else f"--signal {args.signal} ")
            source_command = (
                f"mkdir -p {remote}; cd {remote}; {PROBE} send --target 127.0.0.1:27100 "
                f"--session-id {session_id} --group-id {plan['group_id']} "
                f"--duration-seconds {args.duration} --payload flac "
                f"--epoch-samples {args.epoch_samples} --channels 2 "
                f"--group-channels 8 {pcm}--repair-percent {args.repair_percent} "
                f"--min-repair-symbols {args.min_repair_symbols} "
                f">source.json 2>source.err; echo exit=$?")
        failover: dict = {}
        if args.failover_at:
            import threading
            victim = state["nodes"][args.failover_node]
            local_offset_ns = clock_ns - time.time_ns()

            def exercise_failover() -> None:
                stop_at = session_id + args.failover_at * 1_000_000_000
                time.sleep(max(0, (stop_at - local_offset_ns - time.time_ns()) / 1e9))
                failover["stopped_unix_ns"] = int(common.ssh_text(
                    victim, "systemctl stop needletail-mesh.service; date +%s%N"))
                time.sleep(args.failover_restore_after)
                failover["restored_unix_ns"] = int(common.ssh_text(
                    victim, "systemctl start needletail-mesh.service; date +%s%N"))
                failover["stopped_offset_ms"] = round(
                    (failover["stopped_unix_ns"] - session_id) / 1e6)
                failover["restored_offset_ms"] = round(
                    (failover["restored_unix_ns"] - session_id) / 1e6)

            threading.Thread(target=exercise_failover, daemon=True).start()
        source_exit = common.ssh_text(
            contributor, source_command, timeout=args.duration + args.start_delay + 120)
        after = common.parallel(services, lambda node: json.loads(
            common.ssh_text(node, SNAPSHOT, timeout=60)))

        def collect(node: dict) -> None:
            wait = (f"cd {remote} 2>/dev/null || exit 0; "
                    f"deadline=$(( $(date +%s) + {args.tail + 25} )); "
                    "while [ $(date +%s) -lt $deadline ]; do alive=0; "
                    "for f in *.pid; do [ -e \"$f\" ] || continue; "
                    "kill -0 $(cat $f) 2>/dev/null && alive=1; done; "
                    "[ $alive = 0 ] && break; sleep 1; done; "
                    "for f in *.pid; do [ -e \"$f\" ] || continue; "
                    "kill $(cat $f) 2>/dev/null || true; done")
            common.ssh(node, wait, check=False, timeout=args.tail + 90)
            if args.verify_opus_crc and args.remote_probes and node["role"] == "load":
                common.ssh(node, (
                    f"cd {remote} && for d in crc@*; do [ -d \"$d\" ] || continue; "
                    "t=${d#crc@}; target=${t%%@*}; stream=${t#*@}; "
                    f"/usr/local/bin/soundkit-part-check \"$d\" {args.opus_part_ms} "
                    ">\"T@${target}@opus-crc-${stream}.json\" 2>/dev/null; "
                    "rm -rf \"$d\"; done"), check=False, timeout=900)
            if args.verify_opus_crc and node["role"] == "edge":
                # Check every saved Opus part on the node; keep only the verdict.
                common.ssh(node, (
                    f"cd {remote} && for d in crc-*; do [ -d \"$d\" ] || continue; "
                    f"/usr/local/bin/soundkit-part-check \"$d\" {args.opus_part_ms} "
                    f">opus-${{d}}.json 2>opus-${{d}}.err; rm -rf \"$d\"; done"),
                    check=False, timeout=600)
            target = result_dir / node["name"]
            target.mkdir(exist_ok=True)
            common.rsync_from(node, f"{remote}/", str(target) + "/")
            # Remote readers are filed under the node they measured.
            for path in target.glob("T@*"):
                _, measured, name = path.name.split("@", 2)
                (result_dir / measured).mkdir(exist_ok=True)
                path.rename(result_dir / measured / name)

        common.parallel(receivers + [contributor], collect)
    finally:
        dropped = {}
        for node, remove in loss_removals:
            dropped[node["name"]] = common.ssh_text(node, remove, check=False)

    summary = {
        "schema": "needletail.linode-small-mesh-run.v1",
        "run_id": run_id, "name": args.name, "label": args.label,
        "lab_run": state["run_id"], "node_count": len(state["nodes"]),
        "hourly_usd": state["hourly_usd"],
        "source": args.source, "duration_seconds": args.duration,
        "tracks": args.tracks if args.source == "daw" else 1,
        "session_id": session_id, "group_id": plan["group_id"],
        "streams": plan["streams"], "source_exit": source_exit,
        "loss": {"relay_percent": args.loss_relay, "mesh_percent": args.loss_mesh,
                 "dropped_datagrams": dropped},
        "blackout": ({"ms": args.blackout_ms, "at_seconds": args.blackout_at,
                      "nodes": args.blackout_nodes or "all distributors"}
                     if args.blackout_ms else None),
        "failover": ({"node": args.failover_node, **failover}
                     if args.failover_at else None),
        "probe_host": "regional load host over the VLAN" if args.remote_probes
                      else "the measured node",
        "readers_per_edge": args.readers, "response_ms": args.response_ms,
        "settings": settings, "nodes": {},
    }
    for node in sorted(state["nodes"].values(), key=lambda item: item["name"]):
        entry: dict = {"role": node["role"], "site": node["site"],
                       "type": node["type"], "reports": {}}
        node_dir = result_dir / node["name"]
        if node_dir.is_dir():
            for path in sorted(node_dir.glob("*.json")):
                name = path.stem
                try:
                    report = json.loads(path.read_text())
                except json.JSONDecodeError:
                    error = (node_dir / f"{name}.err").read_text(errors="replace")
                    entry["reports"][name] = {"error": error.strip()[-400:] or "empty report"}
                    continue
                if report.get("schema") == "needletail.soundkit-part-check.v1":
                    entry.setdefault("opus_crc", {})[name.split("crc-", 1)[1]] = report
                    continue
                if name == "source":
                    entry["source"] = {key: report.get(key) for key in (
                        "epochs", "source_datagrams", "repair_datagrams", "wire_bytes",
                        "lossless_payload_bytes", "pcm_reference_bytes",
                        "wire_overhead_ratio", "input_pcm_frames")}
                    continue
                entry["reports"][name] = summarize_report(report)
        for stream_name, crc in entry.get("opus_crc", {}).items():
            reader = entry["reports"].get(f"hls-local-{stream_name}")
            if reader is None or "error" in reader:
                continue
            # The probe counts a mismatch for each part without the encrypted
            # flag. Packet integrity is the CRC32 verdict over the same parts.
            reader["probe_policy_mismatches"] = reader.get("integrity_mismatches")
            reader["integrity_mismatches"] = (
                crc["crc_mismatches"] + crc["crc_missing"] + crc["malformed_parts"]
                + crc["frame_count_mismatches"] + crc["repeated_pts"])
            reader["crc_verified_packets"] = crc["crc_verified"]
            reader["crc_checked_parts"] = crc["parts"]
        if node["name"] in before and node["name"] in after:
            entry["usage"] = node_usage(before[node["name"]], after[node["name"]])
        summary["nodes"][node["name"]] = entry
    (result_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print_summary(summary)
    print(f"\nevidence: {result_dir}/summary.json")


def print_summary(summary: dict) -> None:
    print(f"run {summary['run_id']}  source={summary['source']} "
          f"duration={summary['duration_seconds']}s  {summary['source_exit']}")
    header = (f"{'NODE':<16}{'LANE':<26}{'EXP':>7}{'GOT':>7}{'MISS':>6}{'DL':>5}"
              f"{'BAD':>5}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}")
    print(header)
    for name, entry in summary["nodes"].items():
        for report_name, report in entry["reports"].items():
            if "error" in report:
                print(f"{name:<16}{report_name:<26} ERROR {report['error'][-150:]}")
                continue
            latency = (report.get("latency_ms") or report.get("availability_latency_ms")
                       or {})
            expected = report.get("expected", report.get("expected_parts", "-"))
            received = report.get("received", report.get("received_parts", "-"))
            missing = report.get("missing", report.get("missing_parts", "-"))
            bad = report.get("integrity_mismatches", report.get("erasure_epochs", "-"))
            print(f"{name:<16}{report_name:<26}{expected!s:>7}{received!s:>7}"
                  f"{missing!s:>6}{report.get('deadline_misses')!s:>5}{bad!s:>5}"
                  + "".join(f"{latency.get(key, float('nan')):>9.2f}"
                            for key in ("p50", "p95", "p99", "max")))
    print(f"\n{'NODE':<16}{'ROLE':<12}{'CPU%':>7}{'HOST%':>7}{'STEAL%':>8}{'MEM MB':>8}"
          f"{'PUB kbps':>10}{'VLAN tx':>9}{'VLAN rx':>9}{'CLK ms':>8}{'UDPERR':>7}")
    for name, entry in summary["nodes"].items():
        usage = entry.get("usage")
        if not usage:
            continue
        print(f"{name:<16}{entry['role']:<12}{usage['service_cpu_percent']:>7.1f}"
              f"{usage['host_cpu_percent']:>7.1f}{usage['steal_percent']:>8.2f}{usage['memory_mb']:>8.1f}"
              f"{usage['public_tx_kbps']:>10.0f}{usage['vlan_tx_kbps']:>9.0f}"
              f"{usage['vlan_rx_kbps']:>9.0f}{usage.get('clock_offset_ms', 0):>8.3f}"
              f"{usage.get('udp_receive_errors', 0):>7}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--source", choices=("synthetic", "lori", "daw"), default="synthetic")
    parser.add_argument("--signal", default="decorrelated")
    parser.add_argument("--pcm-file", default="/var/lib/needletail-test-media/lori-stereo.s24le")
    parser.add_argument("--track-directory",
                        default="/var/lib/needletail-test-media/daw-nexus-album-1-track-pcm-300s")
    parser.add_argument("--tracks", type=int, default=1)
    parser.add_argument("--udp-formats", default="flac")
    parser.add_argument("--opus-codec", default="opus")
    parser.add_argument("--epoch-samples", type=int, default=PROBE_EPOCH_SAMPLES,
                        help="samples of one epoch of the AEP1 sender (synthetic, lori; FLAC, "
                             "at least 32); one part each with audio_part epoch")
    parser.add_argument("--daw-lossless-epoch-samples", type=int,
                        default=DAW_LOSSLESS_EPOCH_SAMPLES,
                        help="the DAW source's lossless epoch (daw-test-source "
                             "--lossless-epoch-samples)")
    parser.add_argument("--daw-opus-frame-samples", type=int, default=DAW_OPUS_FRAME_SAMPLES,
                        choices=(48, 64, 72, 120, 240, 480, 960),
                        help="the DAW source's Opus frame (--opus-frame-samples); 120 (2.5 ms) "
                             "parts are not a whole ms, which the checkers need. 48 (1 ms) is "
                             "the lane's Opus Custom default (whole ms; needs the opus-custom-1ms "
                             "builds of daw-nexus and av-contrib, and opaque packaging)")
    parser.add_argument("--daw-opus-bitrate", type=int, default=DAW_OPUS_BITRATE,
                        help="the DAW source's Opus bitrate, CBR (--opus-bitrate): 320000, the "
                             "lane's default; 256000 is the lower-bandwidth option at 1 ms")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--tail", type=int, default=4)
    parser.add_argument("--start-delay", type=int, default=14)
    parser.add_argument("--deadline-ms", type=int, default=1000)
    parser.add_argument("--repair-percent", type=int, default=20)
    parser.add_argument("--min-repair-symbols", type=int, default=1)
    parser.add_argument("--loss-relay", type=float, default=0.0,
                        help="percent of primary relay datagrams dropped at each distributor")
    parser.add_argument("--loss-mesh", type=float, default=0.0,
                        help="percent of cache-mesh datagrams dropped at each edge")
    parser.add_argument("--readers", type=int, default=0,
                        help="many-reader LL-HLS load per edge from the regional load host")
    parser.add_argument("--response-ms", type=int, default=0)
    parser.add_argument("--load-stream", default="",
                        help="stream name for the many-reader load, for example opus0")
    parser.add_argument("--load-edges", type=int, default=0,
                        help="load only the first N edges per site (0 = every edge)")
    parser.add_argument("--load-deadline-ms", type=int, default=250)
    parser.add_argument("--arrival-window-ms", type=int, default=2000)
    parser.add_argument("--late-join", type=int, default=0)
    parser.add_argument("--no-webtransport", action="store_true")
    parser.add_argument("--failover-at", type=int, default=0,
                        help="stop the failover node this many seconds into the stream")
    parser.add_argument("--failover-restore-after", type=int, default=15)
    parser.add_argument("--failover-node", default="relay-primary")
    parser.add_argument("--remote-probes", action="store_true",
                        help="run every reader on the regional load host, not on the "
                             "single-vCPU node that it measures")
    parser.add_argument("--verify-opus-crc", action="store_true",
                        help="verify the SoundKit packet CRC32 of every Opus part on every edge")
    parser.add_argument("--blackout-ms", type=int, default=0,
                        help="drop the whole primary lane at distributors for this long")
    parser.add_argument("--blackout-at", type=int, default=10)
    parser.add_argument("--blackout-nodes", default="",
                        help="comma-separated distributors to black out (default: all)")
    parser.add_argument("--raw-parts", action="store_true",
                        help="keep raw LL-HLS part bodies from the first edge")
    parser.add_argument("--sites", default="")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
