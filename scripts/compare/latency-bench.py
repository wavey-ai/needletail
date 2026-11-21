#!/usr/bin/env python3
"""Measure where the time of a PCM part goes, on the local namespace lab.

One run: start the lab of scripts/edge_lab.py (tc netem links, a node has 2
CPUs and 4 GB, the contributor 4 and 8) with a given set of extra service
flags, so that each low-latency change can be switched off and on; send the
lossless PCM loop with the AEP1 sender (aep1-48k-probe send, as
compare-audio.py does on lon-src) from the contributor's own host to its
ingest; run N ntcmp HLS readers of each kind, window 1 over HTTP/2 (as
needletail-reader) and window 40 over HTTP/3, on each reader host
(lon-readers and new-readers) for D seconds; then join the hop journals of
every node with the readers' records (hops.py). Linode is not used.

Results: target/latency-bench/NAME/
  hops.json      hops.py's report, with the run's settings under "bench"
  summary.txt    the hop table of each region, and each reader's latency
  hops.raw.ndjson, HOST.LANE.probe*.csv|json, sender.log, plan.json

Flags reach the services through the lab's settings:
  --mesh-args "..."          every av-mesh node (backbone, distributor, edge)
  --backbone-args/--distributor-args/--edge-args "..."   one role
  --contrib-args "..."       av-contrib
  --sender-args "..."        aep1-48k-probe send (e.g. "--pace tokio")
  --set key=value            any setting of linode-small-mesh-deploy.py
Write a single flag with `=`: --mesh-args=--some-flag (argparse reads a
lone "--some-flag" as an option of its own).
e.g. the A/B of the event-driven cache-mesh sender:
  latency-bench.py --name p1-on
  latency-bench.py --name p1-off --mesh-args "--mesh-sync-on-publish false"

--dry-run prints the plan (every command) and touches nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shlex
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import edge_lab as lab  # noqa: E402

_spec = importlib.util.spec_from_file_location("hops", HERE / "hops.py")
hops = importlib.util.module_from_spec(_spec)
sys.modules["hops"] = hops
_spec.loader.exec_module(hops)

OUT_ROOT = ROOT / "target" / "latency-bench"
MEDIA = OUT_ROOT / "media"
LOOP_SECONDS = 64                       # ntcmp's loop (tools/ntcmp/audio.go loopSecs)
LOOP_BYTES = LOOP_SECONDS * 48_000 * 6  # stereo 24-bit
PCM_STREAM = 4001                       # daw_hls_base_stream_id 2001 + the PCM offset 2000
EPOCH_NS = 5_000_000
READER_HOSTS = ["lon-readers", "new-readers"]
SKIP_SECONDS = 5


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--name", required=True, help="results go to target/latency-bench/NAME")
    parser.add_argument("--seconds", type=int, default=60, help="D: how long the readers listen")
    parser.add_argument("--readers", type=int, default=1, help="N: readers of each kind on each host")
    parser.add_argument("--hosts", default=",".join(READER_HOSTS), help="reader namespaces")
    parser.add_argument("--h2-window", type=int, default=1)
    parser.add_argument("--h3-window", type=int, default=40)
    for role in ("mesh", "backbone", "distributor", "edge", "contrib", "sender"):
        parser.add_argument(f"--{role}-args", default="", metavar="ARGS",
                            help=f"extra arguments of {role} (one string)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="a lab setting (value as JSON or a bare string); repeat")
    parser.add_argument("--reader-loss", type=float, default=0.0,
                        help="random loss in percent on the reader hosts' links, both directions")
    parser.add_argument("--reader-netem", default="",
                        help="a netem spec for the reader hosts' links instead (edge_lab net)")
    parser.add_argument("--no-up", action="store_true",
                        help="use the lab that is running (its flags are not changed)")
    parser.add_argument("--keep-up", action="store_true", help="leave the lab running afterwards")
    parser.add_argument("--ntcmp", default=str(ROOT / "target" / "ntcmp"))
    parser.add_argument("--pcm", default=str(lab.AUDIO_PCM),
                        help="stereo s24le PCM; its first 64 s are the loop")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; run nothing")
    return parser


def split(text: str) -> list[str]:
    return shlex.split(text) if text else []


def lab_overrides(args) -> list[str]:
    """The `key=value` settings of edge_lab.up for this run."""
    settings: dict[str, object] = {"daw_hls_formats": "pcm"}
    plain = []
    for item in args.set:
        key, _, value = item.partition("=")
        if key.startswith("extra_") and key.endswith("_args"):
            settings[key] = json.loads(value)
        else:
            plain.append(item)
    mesh = split(args.mesh_args)
    for role in ("backbone", "distributor", "edge"):
        extra = list(settings.get(f"extra_{role}_args") or []) + mesh + split(getattr(args, f"{role}_args"))
        if extra:
            settings[f"extra_{role}_args"] = extra
    contrib = list(settings.get("extra_contributor_args") or []) + split(args.contrib_args)
    if contrib:
        settings["extra_contributor_args"] = contrib
    out = [f"{key}={value if isinstance(value, str) else json.dumps(value)}"
           for key, value in settings.items()]
    return out + plain


def loop_start(now: float) -> int:
    """The next 64-second boundary at least 8 s away (compare-audio.py's)."""
    start = (int(now) // LOOP_SECONDS + 1) * LOOP_SECONDS
    if start - now < 8:
        start += LOOP_SECONDS
    return start


def sender_command(args, start: int, loop_file: str) -> list[str]:
    """The AEP1 PCM sender. It sends epoch k when its last sample has been
    spoken: the session id is the loop start plus one epoch."""
    session_id = start * 1_000_000_000 + EPOCH_NS
    return [lab.binary("aep1-48k-probe"), "send", "--target", "127.0.0.1:27100",
            "--session-id", str(session_id), "--group-id", "0",
            # Long enough for the lab to go live first; it is stopped at the end.
            "--duration-seconds", str(args.seconds + max(0, start - int(time.time())) + 300),
            "--payload", "pcm", "--channels", "2", "--group-channels", "8",
            "--pcm-s24le-file", loop_file, "--repair-percent", "12", "--min-repair-symbols", "1",
            *split(args.sender_args)]


def reader_commands(args, out: pathlib.Path, index: str) -> list[dict]:
    """ntcmp hls readers: window 1 over HTTP/2 and window 40 over HTTP/3 on
    each reader host, through the first distributor of the host's site."""
    readers = []
    for host in [item for item in args.hosts.split(",") if item]:
        site = lab.node(host)["site"]
        entry = lab.distributors(site)[0]
        for lane, window, http3 in (("h2", args.h2_window, False), ("h3", args.h3_window, True)):
            stem = out / f"{host}.{lane}"
            command = [args.ntcmp, "hls", "--lane", f"nt-pcm-{lane}", "--mode", "chase",
                       "--window", str(window), *(["--http3"] if http3 else []),
                       "--url", f"{lab.base_url(entry)}/live/{PCM_STREAM}/master.m3u8"
                                f"?sid=bench-{args.name}-{host}-{lane}-{{i}}",
                       "--ca", lab.ca_file(), "--server-name", lab.SERVER_NAME,
                       "--audio-index", index, "--duration", str(args.seconds),
                       "--skip-seconds", str(SKIP_SECONDS), "--label", f"{args.name}-{host}-{lane}",
                       "--count", str(args.readers),
                       *(["--spread-ms", str(min(10_000, 400 * args.readers))] if args.readers > 1 else []),
                       "--report", f"{stem}.probe.json", "--frames", f"{stem}.probe.csv"]
            readers.append({"host": host, "site": site, "lane": lane, "entry": entry, "command": command})
    return readers


def reader_netem(args) -> str | None:
    if args.reader_netem:
        return args.reader_netem
    if args.reader_loss:
        return lab.netem_spec(loss=args.reader_loss)
    return None


def plan(args) -> dict:
    out = OUT_ROOT / args.name
    loop_file = str(MEDIA / "loop64.s24le")
    index = str(MEDIA / "pcm-5ms.index.json")
    start = loop_start(time.time())
    return {
        "name": args.name, "out": str(out), "seconds": args.seconds, "readers_per_kind": args.readers,
        "lab_up": None if args.no_up else lab_overrides(args),
        "media": {"loop": loop_file, "index": index, "from": args.pcm,
                  "index_command": [args.ntcmp, "audio-index", loop_file, index, "5"]},
        "loop_start_unix": start,
        "sender": {"namespace": "contrib-lon", "command": sender_command(args, start, loop_file)},
        "readers": reader_commands(args, out, index),
        "reader_netem": reader_netem(args),
        "hops": {"stream": PCM_STREAM, "source_start_ns": start * 1_000_000_000,
                 "skip_seconds": SKIP_SECONDS},
    }


def prepare_media(item: dict) -> None:
    loop = pathlib.Path(item["loop"])
    if not loop.is_file() or loop.stat().st_size != LOOP_BYTES:
        MEDIA.mkdir(parents=True, exist_ok=True)
        with open(item["from"], "rb") as source:
            data = source.read(LOOP_BYTES)
        if len(data) != LOOP_BYTES:
            raise SystemExit(f"{item['from']} is shorter than {LOOP_SECONDS} s of stereo s24le")
        loop.write_bytes(data)
    if not pathlib.Path(item["index"]).is_file():
        subprocess.run(item["index_command"], check=True, capture_output=True)


def reader_summary(report: pathlib.Path) -> dict:
    try:
        data = json.loads(report.read_text())
    except (OSError, ValueError):
        return {"report": report.name, "error": "no report"}
    latency = data.get("audio_latency_ms") or {}
    detail = data.get("detail") or {}
    return {"report": report.name, "units": data.get("audio_packets"),
            "gaps": data.get("audio_sequence_gaps"), "error": data.get("error"),
            "latency_ms": {key: latency.get(key) for key in ("p50", "p95", "p99", "max")},
            "server_timing_ms": detail.get("server_timing_ms"), "counts": detail.get("counts")}


def run(args, steps: dict) -> int:
    out = pathlib.Path(steps["out"])
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.probe*"):
        old.unlink()
    (out / "plan.json").write_text(json.dumps(steps, indent=1) + "\n")
    if not pathlib.Path(args.ntcmp).is_file():
        raise SystemExit(f"{args.ntcmp} is missing: build it with scripts/compare/gobuild.sh")
    prepare_media(steps["media"])
    if steps["lab_up"] is None and not lab.load("services.json"):
        raise SystemExit("--no-up: the lab is not running")
    try:
        if steps["lab_up"] is not None:
            log(f"lab up: {' '.join(steps['lab_up'])}")
            lab.up(steps["lab_up"])
        nodes, events, since_us, ended_us = measure(args, steps, out)
    finally:
        if not args.keep_up and steps["lab_up"] is not None:
            lab.down(quiet=True)
    hops.save_raw(out / "hops.raw.ndjson", nodes, events)
    probes = []
    for item in steps["readers"]:
        for path in sorted(out.glob(f"{item['host']}.{item['lane']}.probe*.csv")):
            probes.append(hops.read_probe(f"{item['site']}={path}"))
    result = hops.analyse(nodes, events, probes, PCM_STREAM,
                          steps["hops"]["source_start_ns"] // 1000, hops.EPOCH_US,
                          since_us=since_us, until_us=ended_us)
    summaries = [reader_summary(path) for path in sorted(out.glob("*.probe*.json"))]
    result["bench"] = {**{key: value for key, value in steps.items() if key != "readers"},
                       "reader_commands": [item["command"] for item in steps["readers"]],
                       "readers": summaries, "window_us": [since_us, ended_us]}
    (out / "hops.json").write_text(json.dumps(result, indent=1) + "\n")
    lines = [f"latency bench {args.name}: {args.seconds} s, {args.readers} reader(s) of each kind "
             f"on {args.hosts}",
             f"lab settings: {' '.join(steps['lab_up'] or ['(running lab)'])}",
             f"sender: {' '.join(split(args.sender_args)) or '(defaults)'}; "
             f"reader links: {steps['reader_netem'] or 'clean'}", ""]
    for item in summaries:
        latency = item.get("latency_ms") or {}
        lines.append(f"{item['report']:<36} units {item.get('units')} gaps {item.get('gaps')} "
                     f"latency p50 {latency.get('p50')} p95 {latency.get('p95')} "
                     f"p99 {latency.get('p99')} max {latency.get('max')}"
                     + (f" error {item['error']}" if item.get("error") else ""))
    text = "\n".join(lines) + "\n\n" + hops.format_summary(result)
    (out / "summary.txt").write_text(text)
    print(text, end="")
    return 0


def measure(args, steps: dict, out: pathlib.Path) -> tuple[dict, list[dict], int, int]:
    """Send, listen and collect the journals. Returns the nodes, their
    events and the window of the readers' steady state."""
    # The loop start is fixed now that the lab is up.
    start = loop_start(time.time())
    steps["loop_start_unix"] = start
    steps["hops"]["source_start_ns"] = start * 1_000_000_000
    steps["sender"]["command"] = sender_command(args, start, steps["media"]["loop"])
    sender_log = open(out / "sender.log", "ab")
    sender = subprocess.Popen(lab.enter("contrib-lon") + steps["sender"]["command"], stdout=sender_log,
                              stderr=sender_log, stdin=subprocess.DEVNULL, start_new_session=True)
    sources = lab.load("sources.json")
    sources["bench-sender"] = sender.pid
    lab.save("sources.json", sources)
    readers, shaped = [], []
    try:
        log(f"sender starts at {start} ({start - time.time():.1f} s)")
        time.sleep(max(0.0, start - time.time()))
        for site in sorted({item["site"] for item in steps["readers"]}):
            lab.wait_live(PCM_STREAM, site, timeout=60)
        if steps["reader_netem"]:
            for host in sorted({item["host"] for item in steps["readers"]}):
                shaped.append(host)
                lab.set_net(host, "pub", "both", steps["reader_netem"])
        began_us = time.time_ns() // 1000
        for item in steps["readers"]:
            handle = open(out / f"{item['host']}.{item['lane']}.log", "ab")
            readers.append(subprocess.Popen(lab.enter(item["host"]) + item["command"], stdout=handle,
                                            stderr=handle, stdin=subprocess.DEVNULL,
                                            start_new_session=True))
        log(f"{len(readers)} reader processes for {args.seconds} s")
        for process in readers:
            try:
                process.wait(timeout=args.seconds + 90)
            except subprocess.TimeoutExpired:
                process.kill()
        ended_us = time.time_ns() // 1000
        since_us = began_us + SKIP_SECONDS * 1_000_000
        log("collecting the hop journals")
        nodes, events = hops.collect_edge_lab(since_us, PCM_STREAM)
    finally:
        for host in shaped:
            lab.set_net(host, "pub", "both", None)
        for process in readers:
            if process.poll() is None:
                process.kill()
        os_kill_group(sender.pid)
        sources = lab.load("sources.json")
        sources.pop("bench-sender", None)
        lab.save("sources.json", sources)
    return nodes, events, since_us, ended_us


def os_kill_group(pid: int) -> None:
    import os
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_up and any(getattr(args, f"{role}_args") for role in
                          ("mesh", "backbone", "distributor", "edge", "contrib")) or (args.no_up and args.set):
        raise SystemExit("--no-up keeps the running lab's flags: drop the service arguments or --no-up")
    for window in (args.h2_window, args.h3_window):
        if not 1 <= window <= 400:
            raise SystemExit("a reader window is 1 to 400 parts")
    steps = plan(args)
    if args.dry_run:
        print(json.dumps(steps, indent=1))
        return 0
    return run(args, steps)


if __name__ == "__main__":
    sys.exit(main())
