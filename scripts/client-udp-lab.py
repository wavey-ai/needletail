#!/usr/bin/env python3
"""End-to-end test of RaptorQ FEC UDP to clients at an edge, in the local lab.

Each client is a `needletail-udp-client` on a reader host. It is assigned an
edge by an entry point, exactly as an LL-HLS reader is, reads the playlist
of its stream from that edge, subscribes by UDP, and fetches every part that
it received over HTTP from the same edge to compare the bytes
(`--verify-http`). Beside each UDP client an LL-HLS reader reads the same
stream.

Streams: 4K video (1, H.264 over SRT, fMP4 parts), SoundKit Opus (3001) and
FLAC (2001) from the DAW test source, 24-bit PCM (4001) from the AEP1 sender.

Steps, each with every stream:

  loss-0      no loss on the reader host's link
  loss-1      1 % random loss on it, in both directions
  loss-5      5 % random loss
  drain       the edge that serves a client is drained 10 s into the step:
              the client must move to the next edge of its assignment and
              lose no part

A step passes when every UDP client received parts, none is missing (a part
that the edge skipped as a gap is counted apart: it is a hole of the mesh,
not of UDP), every received part is identical to the edge's LL-HLS part, and,
for the drain step, every client served by the drained edge moved.

  client-udp-lab.py run [--out DIR] [--steps loss-0,loss-1,loss-5,drain]
                        [--seconds 20] [--udp-port PORT] [--no-up]

It brings the lab up (`edge_lab.py up`) unless `--no-up`, starts the
sources, runs the steps and writes `result.json` and one JSON per client
to the output directory. It exits 1 when a step fails.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import edge_lab as lab  # noqa: E402

CLIENT = pathlib.Path(os.environ.get(
    "NEEDLETAIL_UDP_CLIENT_BIN", str(lab.MESH_ROOT / "target" / "release" / "needletail-udp-client")))
STREAMS = {"video-4k": 1, "flac": 2001, "opus": 3001, "pcm": 4001}
READER_HOST = "lon-readers"
ENTRY = "lon-dist-a"
OVERRIDES = [
    'daw_hls_formats="flac,opus,pcm"',
    # The contributor of the Linode run is a dedicated 4-vCPU instance.
    "limit.contrib-lon.cpu_percent=400",
    "limit.contrib-lon.memory_mb=4096",
]


def ca() -> str:
    return str(lab.WORK / "artifacts" / "tls" / "ca.crt")


def run_client(stream: int, seconds: int, out: pathlib.Path, udp_port: int | None,
               session: str, results: dict, key: str) -> None:
    command = [str(CLIENT), "--entry", lab.base_url(ENTRY), "--stream", str(stream),
               "--tls-ca", ca(), "--server-name", lab.SERVER_NAME,
               "--duration-seconds", str(seconds), "--verify-http", "--session", session]
    if udp_port:
        command += ["--udp-port", str(udp_port)]
    result = subprocess.run(lab.enter(READER_HOST) + command, capture_output=True,
                            timeout=seconds + 60)
    (out / f"{key}.json").write_bytes(result.stdout)
    (out / f"{key}.err").write_bytes(result.stderr)
    try:
        results[key] = json.loads(result.stdout)
    except ValueError:
        results[key] = {"error": result.stderr.decode(errors="replace")[-800:]}


def run_reader(stream: int, seconds: int, out: pathlib.Path, session: str,
               results: dict, key: str) -> None:
    command = [lab.binary("needletail-reader"), "--entry", lab.base_url(ENTRY),
               "--stream", str(stream), "--tls-ca", ca(), "--server-name", lab.SERVER_NAME,
               "--duration-seconds", str(seconds), "--session", session,
               "--buffer-ms", "1000", "--report", str(out / f"{key}.json")]
    result = subprocess.run(lab.enter("lon-readers-2") + command, capture_output=True,
                            timeout=seconds + 60)
    (out / f"{key}.err").write_bytes(result.stderr)
    try:
        results[key] = json.loads((out / f"{key}.json").read_text())
    except (OSError, ValueError):
        results[key] = {"error": result.stderr.decode(errors="replace")[-800:]}


def judge(report: dict, step: str) -> list[str]:
    problems = []
    if "error" in report:
        return [f"client failed: {report['error'][-300:]}"]
    if report.get("received_parts", 0) == 0:
        problems.append("no part received")
    if report.get("missing_parts", 0):
        problems.append(f"{report['missing_parts']} parts missing "
                        f"(e.g. {report.get('missing_examples', [])[:5]})")
    verify = report.get("verify") or {}
    if verify.get("different", 0):
        problems.append(f"{verify['different']} parts differ from LL-HLS")
    if verify.get("http_errors", 0):
        problems.append(f"{verify['http_errors']} parts not fetched over HTTP "
                        f"({verify.get('error_examples', [])[:2]})")
    if verify.get("identical", 0) != report.get("received_parts", 0):
        problems.append(f"{verify.get('identical', 0)} of {report.get('received_parts', 0)} "
                        "parts checked identical")
    if step == "drain" and not report.get("moves"):
        problems.append("no move after the drain")
    return problems


def apply_loss(percent: float) -> None:
    if percent:
        spec = lab.netem_spec(delay_ms=lab.ACCESS_DELAY_MS, loss=percent)
        lab.set_net(READER_HOST, "pub", "both", spec)
    else:
        lab.set_net(READER_HOST, "pub", "both", None)


def step_run(step: str, seconds: int, out: pathlib.Path, udp_port: int | None) -> dict:
    directory = out / step
    directory.mkdir(parents=True, exist_ok=True)
    loss = {"loss-0": 0.0, "loss-1": 1.0, "loss-5": 5.0, "drain": 0.0}[step]
    apply_loss(loss)
    results: dict = {}
    threads = []
    for name, stream in STREAMS.items():
        session = f"{step}-{name}"
        threads.append(threading.Thread(target=run_client, args=(
            stream, seconds, directory, udp_port, session, results, f"udp-{name}")))
        threads.append(threading.Thread(target=run_reader, args=(
            stream, seconds, directory, session + "-hls", results, f"hls-{name}")))
    for thread in threads:
        thread.start()
    drained = []
    if step == "drain":
        time.sleep(10)
        # Drain the edges that serve the UDP clients now.
        for edge in lab.edges():
            status, _ = lab.http_json(ENTRY, "/api/control/drain-node", "POST",
                                      {"node_id": edge})
            drained.append((edge, status))
            break
    for thread in threads:
        thread.join()
    for edge, _status in drained:
        lab.http_json(ENTRY, "/api/control/resume-node", "POST", {"node_id": edge})
    apply_loss(0)
    verdicts = {}
    for key, report in sorted(results.items()):
        if not key.startswith("udp-"):
            continue
        problems = judge(report, step)
        if step == "drain":
            # Only a client served by the drained edge has to move.
            served = report.get("caches", [])
            if not any(edge in served[:1] for edge, _ in drained):
                problems = [p for p in problems if p != "no move after the drain"]
        verdicts[key] = {
            "pass": not problems, "problems": problems,
            "received": report.get("received_parts"), "missing": report.get("missing_parts"),
            "gaps": report.get("gap_parts"), "recovered": report.get("recovered_parts"),
            "identical": (report.get("verify") or {}).get("identical"),
            "moves": report.get("moves"), "caches": report.get("caches"),
            "publish_to_arrival_ms": report.get("publish_to_arrival_ms"),
            "datagrams": report.get("datagrams"), "repair_datagrams": report.get("repair_datagrams"),
        }
    hls = {key: {k: report.get("summary", report).get(k)
                 for k in ("parts_received", "parts_missing", "stalls", "moves", "error")}
           if isinstance(report, dict) else report
           for key, report in results.items() if key.startswith("hls-")}
    return {"step": step, "loss_percent": loss, "drained": drained, "udp": verdicts,
            "hls": hls, "pass": all(v["pass"] for v in verdicts.values()) and len(verdicts) == len(STREAMS)}


def start_sources(seconds: int) -> None:
    lab.start_sources(seconds, video=True, audio="daw")
    # The 24-bit PCM rendition comes from the AEP1 sender, as in the
    # comparison of 21 November.
    sources = lab.load("sources.json")
    log = open(lab.WORK / "logs" / "source-pcm.log", "ab")
    process = subprocess.Popen(
        lab.enter("contrib-lon") + [
            lab.binary("aep1-48k-probe"), "send", "--target", "127.0.0.1:27100",
            "--session-id", str(time.time_ns() + 2_000_000_000), "--group-id", "0",
            "--duration-seconds", str(seconds), "--payload", "pcm",
            "--channels", "2", "--group-channels", "8",
            "--pcm-s24le-file", str(lab.AUDIO_PCM),
            "--repair-percent", "12", "--min-repair-symbols", "1"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    sources["pcm"] = process.pid
    lab.save("sources.json", sources)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--out", default=str(lab.WORK / "client-udp"))
    parser.add_argument("--steps", default="loss-0,loss-1,loss-5,drain")
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--udp-port", type=int, default=None,
                        help="client UDP port, when the entry point does not name one")
    parser.add_argument("--no-up", action="store_true")
    parser.add_argument("overrides", nargs="*")
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    steps = [step for step in args.steps.split(",") if step]
    if not args.no_up:
        lab.up(OVERRIDES + args.overrides)
    lab.stop_sources()
    start_sources(len(steps) * (args.seconds + 25) + 60)
    for stream in STREAMS.values():
        try:
            lab.wait_live(stream, timeout=60)
        except lab.LabError as error:
            print(f"stream {stream} is not live: {error}", file=sys.stderr)
    result = {"started_unix": time.time(), "binaries": lab.load("binaries.json"),
              "client": str(CLIENT), "steps": []}
    for step in steps:
        outcome = step_run(step, args.seconds, out, args.udp_port)
        result["steps"].append(outcome)
        print(json.dumps({"step": step, "pass": outcome["pass"],
                          "udp": {k: (v["pass"], v["received"], v["missing"], v["gaps"],
                                      v["recovered"], v["identical"], len(v["moves"] or []),
                                      v["problems"][:2])
                                  for k, v in outcome["udp"].items()}}), flush=True)
        time.sleep(3)
    lab.stop_sources()
    result["pass"] = all(step["pass"] for step in result["steps"])
    (out / "result.json").write_text(json.dumps(result, indent=1) + "\n")
    print("PASS" if result["pass"] else "FAIL")
    sys.exit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
