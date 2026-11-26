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

--hosts (lead, 27 Nov: the run plan asks two load hosts a region) splits the
crowd across load hosts, each its share of every ramp step; the default is the
site's -nano and -view hosts. cmpd9's one load host saturated at 51 s, before
the first missing part, so the crowd's loss could not be told from the load
side's. After the run, judge() reads each host's per-second intervals and
writes judge.json: when each load side first saturated or dropped datagrams in
its own sockets, the first second with a missing part, and `readable_until_s`,
the last second before any load side failed: loss after it is not the mesh's.
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


def shares(total: int, hosts: int) -> list[int]:
    """total split over hosts, the first ones taking the remainder."""
    return [total // hosts + (1 if i < total % hosts else 0) for i in range(hosts)]


def judge(step_dir: pathlib.Path, hosts: list[str]) -> dict:
    """Each load side's health second by second (needletail-load's intervals,
    aligned by unix_ms), and how far the crowd's loss can be read."""
    sides, t0 = {}, None
    rows_by_host = {}
    for host in hosts:
        path = step_dir / host / "crowd" / "intervals.jsonl"
        rows = []
        if path.is_file():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("phase", "run") == "run" and row.get("unix_ms"):
                    rows.append(row)
        rows_by_host[host] = rows
        if rows:
            first = rows[0]["unix_ms"] - 1000.0 * float(rows[0].get("t") or 0)
            t0 = first if t0 is None else min(t0, first)
    for host, rows in rows_by_host.items():
        side = {"intervals": len(rows), "first_saturated_s": None, "first_socket_drop_s": None,
                "first_missing_s": None, "peak_target": max((r.get("target") or 0 for r in rows), default=0)}
        drops_before = 0
        missing_before = 0
        for r in rows:
            t = round((r["unix_ms"] - t0) / 1000.0, 1)
            total = r.get("total") or {}
            if r.get("load_host_saturated") and side["first_saturated_s"] is None:
                side["first_saturated_s"] = t
            drops = total.get("socket_drops") or 0
            if drops > drops_before and side["first_socket_drop_s"] is None:
                side["first_socket_drop_s"] = t
            missing = total.get("missing") or 0
            if missing > missing_before and side["first_missing_s"] is None:
                side["first_missing_s"] = t
            drops_before, missing_before = max(drops_before, drops), max(missing_before, missing)
        summary_path = step_dir / host / "crowd" / "summary.json"
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text())
            side["load_host_saturated"] = summary.get("load_host_saturated")
            side["load_host"] = {k: (summary.get("load_host") or {}).get(k) for k in (
                "first_saturated", "process_cpu_cores_max", "host_cpu_busy_max", "udp_rcvbuf_errors",
                "nic_rx_drops")}
        sides[host] = side
    failed = [s[k] for s in sides.values() for k in ("first_saturated_s", "first_socket_drop_s")
              if s[k] is not None]
    missing = [s["first_missing_s"] for s in sides.values() if s["first_missing_s"] is not None]
    until = min(failed) if failed else None
    first_missing = min(missing) if missing else None
    return {"hosts": sides, "readable_until_s": until, "first_missing_s": first_missing,
            "missing_readable": first_missing is not None and (until is None or first_missing < until),
            "rule": "loss is the mesh's only before every load side is still unsaturated and has dropped "
                    "nothing in its own sockets (readable_until_s; null: never failed)"}


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
    parser.add_argument("--hosts", default="", help="the load hosts the crowd is split across (default: the "
                        "site's -nano and -view)")
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
                   if node.get("role") in ("distributor", "edge") and name.startswith(args.site + "-")
                   and not node.get("long_haul") and "-lh-" not in name)   # relays serve no listeners
    entries = ",".join(f"https://{session.nodes[name]['public_ipv4']}:19444" for name in names)
    edges = [name for name in names if session.nodes[name]["role"] == "edge"]
    hosts = [h for h in (args.hosts.split(",") if args.hosts else [f"{args.site}-nano", f"{args.site}-view"])
             if h in session.nodes]
    if not hosts:
        raise SystemExit(f"no load host for the crowd of {args.site}")
    count = args.listeners or args.step * max(1, args.seconds // args.every)
    steps = shares(args.step, len(hosts))
    fixed = shares(args.listeners, len(hosts)) if args.listeners else None
    scripts, commands, plans = {}, {}, {}
    for i, host in enumerate(hosts):
        plan = f"{fixed[i]}@0" if fixed else ramp(steps[i], args.every, args.seconds)
        mine = fixed[i] if fixed else steps[i] * max(1, args.seconds // args.every)
        command = (f"{LOAD_BIN} --entry {entries} --tls-ca {run.CA} --server-name {run.SERVER_NAME} "
                   f"--stream {stream} --listeners {mine} --ramp {plan} --duration {args.seconds} "
                   f"--out crowd --label {args.session}-{args.site}-{host.split('-')[-1]} --quiet "
                   f"{args.load_args}").strip()
        scripts[host] = ("S0=$(grep '^cpu ' /proc/stat)\n" + f"( {command} ) >crowd.log 2>&1\n"
                         + "echo \"$S0\" >cpu.txt; grep '^cpu ' /proc/stat >>cpu.txt")
        commands[host], plans[host] = shlex.split(command), plan
    sampled = [] if args.no_samplers else run.start_samplers(session)
    started_source = None
    if args.source != "none":
        started_source = start_source(session, args.source)
    step_name = f"crowd.{args.site}.{args.source}"
    run.log(f"{step_name}: {count} listeners on {', '.join(edges)} from {', '.join(hosts)} "
            f"({', '.join(plans[h][:40] + '...' for h in hosts)})")
    result = run.run_hosts(session, step_name, scripts, args.seconds + 30)
    verdict = judge(session.dir / step_name, hosts)
    (session.dir / step_name / "judge.json").write_text(json.dumps(verdict, indent=1) + "\n")
    run.log(f"{step_name}: load sides readable until {verdict['readable_until_s']} s; first missing part "
            f"{verdict['first_missing_s']} s ({'readable' if verdict['missing_readable'] else 'not the mesh' if verdict['first_missing_s'] is not None else 'none'})")
    meta = {"step": step_name, "phase": "crowd", "session": args.session, "site": args.site,
            "hosts": hosts, "stream": stream, "source": args.source, "source_start_unix": started_source,
            "entries": names, "edges": edges, "ramps": plans, "listeners": count, "every_s": args.every,
            "step_listeners": args.step, "commands": commands, **result}
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
