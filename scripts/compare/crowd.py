#!/usr/bin/env python3
"""A needletail-load crowd on one region's edges, through its entry points.

Integration 6 cell 6 (lead, 26 Nov): a 1 ms Opus Custom crowd ramped onto
London's two edges from a load host, 20 listeners more every 10 s, as real
clients join (every distributor and edge of the region answers
/api/assignment; a refused listener is spilled by the entry). The edges'
own counters (cpu_admit, refusals by reason, heavy, fast plane) come from
compare-run.py's samplers, started and collected here.

    crowd.py --session crowd-opus1 --source opus48 --site lon --host lon-view \
        --step 20 --every 10 --seconds 300

--source none leaves the sources alone and --no-samplers the samplers (a
crowd beside a compare-run.py step, which runs both).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shlex
import time

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("compare_run", HERE / "compare-run.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)
common = run.common

STREAMS = {"opus48": 3001, "pcm": 4001}
LOAD_BIN = "/usr/local/bin/needletail-load"


def ramp(step: int, every: int, seconds: int) -> str:
    """step more listeners every `every` s, from `step` at 0, for `seconds`."""
    return ",".join(f"{step * (i + 1)}@{i * every}" for i in range(max(1, seconds // every)))


def start_source(session, source: str) -> int:
    """The AEP1 sender of the crowd's stream on lon-src, started on a loop
    boundary as compare-audio.py starts it; returns its START (Unix s)."""
    contributor = session.node("contrib-lon")
    payload, samples = ("opus", 48) if source == "opus48" else ("pcm", 48)
    run.src(session, "pkill -x aep1-48k-probe; rm -f crowd-audio.start; " + run.background("crowd-audio", (
        "NOW=$(date +%s); START=$(( (NOW / 64 + 1) * 64 )); "
        "[ $(( START - NOW )) -lt 5 ] && START=$(( START + 64 )); echo $START >crowd-audio.start; "
        f"/usr/local/bin/aep1-48k-probe send --target {contributor['vlan_ipv4']}:27100 "
        f"--session-id ${{START}}001000000 --group-id 0 --duration-seconds 3600 "
        f"--payload {payload} --epoch-samples {samples} --channels 2 --group-channels 8 "
        f"--pcm-s24le-file {run.REMOTE}/clips/audio.s24le --repair-percent 12 --min-repair-symbols 1")))
    start = ""
    for _ in range(20):
        start = run.src(session, "cat crowd-audio.start 2>/dev/null", check=False).strip()
        if start:
            break
        time.sleep(0.5)
    if not start:
        raise SystemExit("the crowd's source wrote no start: " + run.src(session, "tail -c 400 crowd-audio.log",
                                                                         check=False))
    time.sleep(max(0.0, int(start) + 5 - time.time()))
    return int(start)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", required=True)
    parser.add_argument("--source", choices=("opus48", "pcm", "none"), default="opus48")
    parser.add_argument("--stream", type=int, default=0, help="default: the --source's stream")
    parser.add_argument("--site", default="lon")
    parser.add_argument("--host", default="lon-view", help="the load host the crowd runs on")
    parser.add_argument("--step", type=int, default=20, help="listeners added each --every s")
    parser.add_argument("--every", type=int, default=10)
    parser.add_argument("--listeners", type=int, default=0, help="a fixed crowd instead of a ramp")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--no-samplers", action="store_true")
    parser.add_argument("--load-args", default="", help="extra needletail-load arguments")
    args = parser.parse_args()
    stream = args.stream or STREAMS.get(args.source, 0)
    if not stream:
        raise SystemExit("--stream is needed with --source none")
    session = run.Session(args.session)
    (session.dir / "lab-nodes.json").write_text(json.dumps(
        {name: {"public_ipv4": node.get("public_ipv4"), "vlan_ipv4": node.get("vlan_ipv4"),
                "role": node.get("role"), "host_uuid": node.get("host_uuid")}
         for name, node in session.nodes.items()}, indent=1) + "\n")
    names = sorted(name for name, node in session.nodes.items()
                   if node.get("role") in ("distributor", "edge") and name.startswith(args.site + "-"))
    entries = ",".join(f"https://{session.nodes[name]['public_ipv4']}:19444" for name in names)
    edges = [name for name in names if session.nodes[name]["role"] == "edge"]
    plan = f"{args.listeners}@0" if args.listeners else ramp(args.step, args.every, args.seconds)
    count = args.listeners or args.step * max(1, args.seconds // args.every)
    command = (f"{LOAD_BIN} --entry {entries} --tls-ca {run.CA} --server-name {run.SERVER_NAME} "
               f"--stream {stream} --listeners {count} --ramp {plan} --duration {args.seconds} "
               f"--out crowd --label {args.session}-{args.site} --quiet {args.load_args}").strip()
    sampled = [] if args.no_samplers else run.start_samplers(session)
    started_source = None
    if args.source != "none":
        started_source = start_source(session, args.source)
    step_name = f"crowd.{args.site}.{args.source}"
    run.log(f"{step_name}: {count} listeners ({plan[:80]}...) on {', '.join(edges)} from {args.host}")
    script = ("S0=$(grep '^cpu ' /proc/stat)\n" + f"( {command} ) >crowd.log 2>&1\n"
              + "echo \"$S0\" >cpu.txt; grep '^cpu ' /proc/stat >>cpu.txt")
    result = run.run_hosts(session, step_name, {args.host: script}, args.seconds + 30)
    meta = {"step": step_name, "phase": "crowd", "session": args.session, "site": args.site,
            "host": args.host, "stream": stream, "source": args.source, "source_start_unix": started_source,
            "entries": names, "edges": edges, "ramp": plan, "listeners": count, "every_s": args.every,
            "step_listeners": args.step, "command": shlex.split(command), **result}
    (session.dir / step_name / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    if args.source != "none":
        run.src(session, "pkill -x aep1-48k-probe; true", check=False)
    if sampled:
        run.collect_samplers(session, sampled)
    record = {"session": args.session, "steps": [{"step": step_name, "seconds": result["seconds"],
                                                   "host_errors": len(result["host_errors"])}],
              "lab_run": session.state["run_id"], "ended_unix": time.time()}
    existing = session.dir / "session.json"
    history = json.loads(existing.read_text()) if existing.is_file() else []
    history.append(record)
    existing.write_text(json.dumps(history, indent=1) + "\n")
    run.log(f"{step_name}: done in {result['seconds']} s; host errors: {result['host_errors'] or 'none'}")


if __name__ == "__main__":
    main()
