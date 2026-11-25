#!/usr/bin/env python3
"""Keep a referee run's raw data in the archive (owner, 23 Nov: "make sure we
keep all this data so we can create detailed reports and graphs"), through
the shared writer scripts/run_archive.py (one manifest schema with the lead's
runs; docs: needletail docs/run-archive.md).

    archive-run.py create RUN_ID [--topology FILE] [--code PATH ...] [--settings FILE]
    archive-run.py collect-nodes RUN_ID      # before `down`; marks the run collected
    archive-run.py store RUN_ID --sessions a,b [--log FILE ...] [--extra DIR ...]
    archive-run.py lab RUN_ID [--hours-billed H] [--cost-usd X] [--deleted]
    archive-run.py finish RUN_ID [--status complete|failed] [--reason TEXT]
    archive-run.py adopt RUN_ID --kind legacy [--note TEXT]   # an old directory, in place

The run id is the lab's (e.g. cmpd5). Nothing is ever deleted or written over.
Layout of a run:
  sessions/<session>/...               the harness's run directory, verbatim
      <step>/meta.json, <step>/<host>/<lane>.probe.csv|.probe.json|.join-NN.json,
      *.arrivals.csv, *.uc.json, samplers/<node>.<t>.log, .stats.<t>.log,
      .stalls.<t>.json, hosts.<t>.txt, session.json
  sessions/<session>/samplers-csv/     the samplers as CSV / NDJSON, units in names
  nodes/<node>/journal.<t>.txt.gz, api-stats.<t>.json, api-stalls.<t>.json
  lab/state.<t>.json, lab/hosts.<t>.txt
  logs/, extra/, lead/ (the lead's tools), topology/, manifest.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
import run_archive as ra  # noqa: E402

RUNS = ROOT / "target" / "compare" / "runs"
TOOL = "compare-archive"
SITE_REGION = {"lon": "gb-lon", "new": "us-east", "tok": "ap-northeast", "syd": "ap-southeast"}

WHAT = [
    (r"/meta\.json$", "step", "step settings: lanes, listeners, impairment, renditions", None),
    (r"\.probe(-\d+)?\.csv$", "samples", "per-unit records of one listener",
     {"latency_us": "us", "complete_unix_us": "us", "first_unix_us": "unit number (audio)",
      "bytes": "bytes", "published_unix_us": "us", "cache_available_unix_us": "us",
      "edge_committed_unix_us": "us", "reply_unix_us": "us"}),
    (r"\.probe(-\d+)?\.json$", "summary", "one listener's probe summary and latency histograms", "ms"),
    (r"\.join-\d+\.json$", "summary", "one new listener's time to first audio", "ms"),
    (r"\.arrivals\.csv$", "samples", "UDP client: every datagram's arrival", "us"),
    (r"\.(uc|udp)\.json$", "summary", "UDP client report: fast lane, FEC, repair counters", None),
    (r"analysis\.json$", "analysis", "cached analysis of the step (analysis_version inside)", "ms"),
    (r"samplers/[^/]+\.stats\.\d+\.log$", "samples", "1 s raw: unix_s | UDP errors (with UdpMemErrors) | socket drops | vm (guest /proc/vmstat, MemFree) | cg (the service cgroup: memory.current, memory.events) | sockstat (UDP inuse, mem pages) | qdisc (root dropped:overlimits:requeues per device) | /api/stats", None),
    (r"samplers/[^/]+\.apinode\.\d+\.log$", "samples", "1 s raw: unix_s, then the node's /api/node JSON (relay_counters, fast lane and tee counters, missed_10s, warm)", None),
    (r"samplers/[^/]+\.meshlocal\.\d+\.log$", "samples", "1 s raw: unix_s, then the node's /api/mesh/local JSON (fast_plane faults, loop lag)", None),
    (r"samplers/[^/]+\.stalls\.\d+\.json$", "node-logs", "the node's stop journal (/api/stalls)", None),
    (r"samplers/[^/]+\.sys\.\d+\.log$", "samples", "1 s raw, 'unix_s S kind ...': per-CPU /proc/stat, /proc/interrupts, /proc/softirqs, /proc/net/softnet_stat, each service thread's /proc/<pid>/task/<tid>/stat (utime, stime, last CPU)", None),
    (r"samplers/[^/]+\.heartbeat\.\d+\.log$", "samples", "SCHED_FIFO-99 heartbeat pinned to each vCPU: 'HB cpu monotonic_ns realtime_ns boottime_ns late_ns policy', a line on each wake over 5 ms late and one a second", None),
    (r"samplers/[^/]+\.nic\.\d+\.txt$", "node-logs", "uname -a and ethtool -i/-k/-l/-S of eth0 and eth1 at sampler start", None),
    (r"samplers/hosts\.\d+\.txt$", "lab", "physical Linode host of every node", None),
    (r"samplers/[^/]+\.\d+\.log$", "samples", "1 s raw (5 s before 24 Nov): /proc/stat ticks, 'net iface:tx_bytes:tx_packets' from /proc/net/dev, service ticks, RSS", None),
    (r"samplers-csv/.*\.cpu-net\.csv$", "samples", "the 1 s samples as CSV (ticks at 100/s, tx bytes and packets cumulative)",
     {"unix_s": "s", "*_ticks": "1/100 s", "*_bytes": "bytes", "*_packets": "packets", "*_rss_kb": "kB"}),
    (r"samplers-csv/.*\.ndjson$", "samples", "1 s samples as NDJSON", {"unix_s": "s"}),
    (r"session\.json$", "session", "the session's steps, times, LiveKit Cloud egress", None),
    (r"\.player\.json$", "summary", "Chrome player's report (WebRTC stats, latency)", "ms"),
    (r"\.ndjson$", "node-logs", "hop journal or other per-event records", "us"),
    (r"(errors\.log|\.err|\.log)$", "log", "a host's, client's or tool's log", None),
]


def describe(rel: str):
    for pattern, kind, text, units in WHAT:
        if re.search(pattern, "/" + rel):
            return kind, text, units
    return "raw", "", None


def add_tree_described(run: ra.RunArchive, src: pathlib.Path, dest: str) -> int:
    """add_tree, then give each added file its kind, description and units."""
    added = set(run.add_tree(src, dest, kind="raw")["added"])
    with run._update() as manifest:
        for entry in manifest["files"]:
            if entry["path"] in added:
                kind, text, units = describe(entry["path"])
                entry.update(kind=kind, description=text, units=units)
    return len(added)


CPU = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice"]


def v_ok(field: str) -> bool:
    return field.split("=", 1)[1].isdigit()


def convert_samplers(samplers: pathlib.Path, out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(samplers.glob("*.log")):
        name = path.name
        if ".apinode." in name:
            continue        # kept raw only (see WHAT)
        if ".meshlocal." in name:
            lines = []
            for line in path.read_text(errors="replace").splitlines():
                head, _, body = line.partition(" ")
                try:
                    rec = {"unix_s": float(head)}
                except ValueError:
                    continue
                try:
                    rec["api_mesh_local"] = json.loads(body) if body.strip() else None
                except json.JSONDecodeError:
                    rec["api_mesh_local_raw"] = body
                lines.append(json.dumps(rec))
            (out / (name[:-4] + ".ndjson")).write_text("\n".join(lines) + "\n")
            continue
        if ".sys." in name or ".heartbeat." in name:
            continue        # kept raw only (see WHAT)
        if ".stats." in name:
            lines = []
            for line in path.read_text(errors="replace").splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2:
                    continue
                try:
                    rec: dict = {"unix_s": float(parts[0])}
                except ValueError:
                    continue
                for part in parts[1:]:
                    if part.startswith("vm "):
                        rec["guest_vm"] = {k: int(v) for k, v in
                                           (x.split("=", 1) for x in part.split()[1:] if "=" in x and v_ok(x))}
                    elif part.startswith("cg "):
                        fields = dict(x.split("=", 1) for x in part.split()[1:] if "=" in x)
                        rec["service_cgroup"] = {k: (int(v) if v.isdigit() else v) for k, v in fields.items()}
                    elif part.startswith("sockstat"):
                        rec["udp_sockstat"] = {k: int(v) for k, v in
                                               (x.split("=", 1) for x in part.split()[1:] if "=" in x and v_ok(x))}
                    elif part.startswith("qdisc"):
                        rec["qdisc_root"] = {dev: dict(zip(("dropped", "overlimits", "requeues"),
                                                           map(int, value.split(":"))))
                                             for dev, value in (x.split("=", 1) for x in part.split()[1:] if "=" in x)}
                    elif part.startswith("sockdrops"):
                        rec["socket_drops_total"] = {k: int(v) for k, v in
                                                     (x.rsplit("=", 1) for x in part.split()[1:] if "=" in x)}
                    elif part.startswith("{"):
                        try:
                            rec["api_stats"] = json.loads(part)
                        except json.JSONDecodeError:
                            rec["api_stats_raw"] = part
                    elif "Udp" in part:
                        rec["udp_errors_total"] = {k: int(v) for k, v in
                                                   (x.split("=", 1) for x in part.split() if "=" in x)}
                    elif part:
                        rec.setdefault("other", []).append(part)
                lines.append(json.dumps(rec))
            (out / (name[:-4] + ".ndjson")).write_text("\n".join(lines) + "\n")
            continue
        rows, columns = [], ["unix_s"] + [f"cpu_{c}_ticks" for c in CPU]
        for line in path.read_text(errors="replace").splitlines():
            parts = [p.strip() for p in line.split("|")]
            head = parts[0].split()
            if len(head) < 3 or head[1] != "cpu":
                continue
            row = {"unix_s": head[0]}
            row.update({f"cpu_{c}_ticks": v for c, v in zip(CPU, head[2:])})
            for part in parts[1:]:
                fields = part.split()
                if fields and fields[0] == "net":
                    # The sampler writes iface:tx_bytes:tx_packets (/proc/net/dev fields 9 and 10;
                    # no receive counters). Archives written before 25 Nov labelled these two
                    # columns rx_bytes and tx_bytes (see NOTE-net-columns.md in those runs).
                    for item in fields[1:]:
                        bits = item.split(":")
                        if len(bits) == 3:
                            row[f"net_{bits[0]}_tx_bytes"], row[f"net_{bits[0]}_tx_packets"] = bits[1], bits[2]
                elif len(fields) == 4:
                    service, _pid, ticks, rss = fields
                    row[f"{service}_cpu_ticks"], row[f"{service}_rss_kb"] = ticks, rss
            for key in row:
                if key not in columns:
                    columns.append(key)
            rows.append(row)
        (out / (name[:-4] + ".cpu-net.csv")).write_text(
            ",".join(columns) + "\n" + "".join(",".join(str(r.get(c, "")) for c in columns) + "\n" for r in rows))


def iso(unix: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(unix)) if unix else None


def cmd_create(args) -> None:
    code_paths = [pathlib.Path(p).expanduser() for p in args.code or []]
    try:
        # Since resilient-mesh c818421 the lab tool's `up` opens the run under
        # the lab's id before anything is billed: add to it.
        run = ra.RunArchive.open(args.run_id, tool=TOOL)
        if code_paths:
            run.set_code_heads(code_paths)
        if args.topology:
            run.set_topology(args.topology)
    except ra.ArchiveError:
        run = ra.RunArchive.create(args.run_id, "linode-lab", tool=TOOL, code_paths=code_paths,
                                   deploy_settings=json.loads(pathlib.Path(args.settings).read_text())
                                   if args.settings else None,
                                   topology=args.topology)
    if args.settings:
        run.add_file(args.settings, f"lab/{pathlib.Path(args.settings).name}", "settings", "deploy settings")
    print(run.path)


def cmd_collect(args) -> None:
    import small_mesh_common as common
    state = common.load_state()
    if state.get("run_id") != args.run_id:
        sys.exit(f"the active lab is {state.get('run_id')}, not {args.run_id}")
    run = ra.RunArchive.open(args.run_id, tool=TOOL)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    stage = pathlib.Path(tempfile.mkdtemp(prefix=f"collect-{args.run_id}-"))
    failed = []

    def one(item: dict) -> None:
        d = stage / item["name"]
        d.mkdir(parents=True, exist_ok=True)
        text = common.ssh_text(item, "journalctl -b --no-pager -o short-iso-precise 2>/dev/null",
                               check=False, timeout=900)
        if not text.strip():
            failed.append(item["name"])
        with gzip.open(d / f"journal.{stamp}.txt.gz", "wt") as f:
            f.write(text)
        if item["role"] in ("contributor", "distributor", "edge", "backbone"):
            port = 19443 if item["role"] == "contributor" else 19444
            for api in ("stats", "stalls"):
                t = common.ssh_text(item, f"curl -sk --max-time 20 'https://{item['public_ipv4']}:{port}/api/{api}'",
                                    check=False, timeout=60)
                (d / f"api-{api}.{stamp}.json").write_text(t + "\n")

    common.parallel(list(state["nodes"].values()), one)
    for d in sorted(stage.iterdir()):
        run.add_tree(d, f"nodes/{d.name}", kind="node-logs",
                     description="the node's journal since boot (gzip) and its API at collection", move=True)
    labdir = stage / "_lab"
    labdir.mkdir()
    (labdir / f"state.{stamp}.json").write_text(json.dumps(state, indent=1) + "\n")
    hosts = subprocess.run([sys.executable, str(ROOT / "scripts" / "linode-small-mesh-lab.py"), "hosts"],
                           capture_output=True, text=True, check=False)
    (labdir / f"hosts.{stamp}.txt").write_text(hosts.stdout + hosts.stderr)
    run.add_tree(labdir, "lab", kind="lab", description="lab state and the physical host of every node", move=True)
    shutil.rmtree(stage, ignore_errors=True)   # the staging dir, outside the archive, emptied by the moves
    instances = [{"label": f"ntsm-{state['run_id']}-{n}", "id": v.get("id"), "plan": v.get("type"),
                  "region": v.get("region"), "role": v.get("role")} for n, v in sorted(state["nodes"].items())]
    run.set_lab(run_tag=f"needletail-small-mesh-{state['run_id']}", instances=instances,
                created_at_utc=iso(state.get("created_unix")))
    run.mark_collected(not failed, failed)
    print(f"collected {len(state['nodes'])} nodes; missing: {failed or 'none'}")


def cmd_store(args) -> None:
    run = ra.RunArchive.open(args.run_id, tool=TOOL)
    for name in [s for s in args.sessions.split(",") if s]:
        src = RUNS / name
        n = add_tree_described(run, src, f"sessions/{name}")
        if (src / "samplers").is_dir():
            stage = pathlib.Path(tempfile.mkdtemp(prefix=f"samplers-{name}-"))
            convert_samplers(src / "samplers", stage)
            add_tree_described(run, stage, f"sessions/{name}/samplers-csv")
            shutil.rmtree(stage, ignore_errors=True)
        steps = 0
        for meta_path in sorted(src.glob("*/meta.json")):
            meta = json.loads(meta_path.read_text())
            per: dict = {}
            for report in meta_path.parent.glob("*/*.json"):
                hit = re.match(r"(.+?)\.(probe(-\d+)?|join-\d+)\.json$", report.name)
                if hit:
                    region = SITE_REGION.get(report.parent.name.split("-")[0], report.parent.name)
                    per.setdefault(hit.group(1), {}).setdefault(region, 0)
                    per[hit.group(1)][region] += 1
            for lane, by_region in sorted(per.items()):
                run.record_step(f"{name}/{meta_path.parent.name}", lane, sum(by_region.values()), by_region, 1,
                                at_utc=iso(meta.get("started_unix")), seconds=meta.get("seconds"),
                                netem=meta.get("netem"))
                steps += 1
        print(f"{name}: {n} files, {steps} listener steps")
    for path in args.log or []:
        run.add_file(path, f"logs/{pathlib.Path(path).name}", "log", "driver log of the run")
    for path in args.extra or []:
        p = pathlib.Path(path)
        if p.is_dir():
            add_tree_described(run, p, f"extra/{p.name}")
        else:
            kind, text, units = describe(p.name)
            run.add_file(p, f"extra/{p.name}", kind, text, units=units)


def cmd_lab(args) -> None:
    run = ra.RunArchive.open(args.run_id, tool=TOOL)
    lab = run.set_lab(hours_billed=args.hours_billed, cost_usd=args.cost_usd,
                      deleted_at_utc=ra.utc_now() if args.deleted else None)
    print(json.dumps({k: v for k, v in lab.items() if k != "instances"}))


def cmd_finish(args) -> None:
    print(ra.RunArchive.open(args.run_id, tool=TOOL).finish(args.status, args.reason))


def cmd_adopt(args) -> None:
    """Give a directory made before the shared writer (23 Nov) the shared
    manifest, in place: every file listed with its hash. An earlier manifest
    of this tool's first format is kept, renamed manifest.archive-run-v0.json."""
    path = ra._check_run_id(args.run_id, ra.data_root())
    old = path / ra.MANIFEST
    v0 = path / "manifest.archive-run-v0.json"
    data = {}
    if old.is_file():
        data = json.loads(old.read_text())
        if data.get("schema_version"):
            sys.exit(f"{args.run_id} already has a shared manifest")
        if v0.exists():
            sys.exit(f"{v0} exists")
        old.rename(v0)
    run = ra.RunArchive(args.run_id)
    run.tool = TOOL
    with run._locked():
        now = ra.utc_now()
        argv = ra.scrub_argv(sys.argv)
        manifest = ra._manifest_skeleton(args.run_id, args.kind)
        manifest.update(started_at_utc=iso(data.get("started_unix_s")) or now,
                        ended_at_utc=iso(data.get("ended_unix_s")) or now,
                        tool=TOOL, command=argv, host=socket.gethostname())
        manifest["invocations"].append({"tool": TOOL, "command": argv, "host": manifest["host"],
                                        "pid": os.getpid(), "at_utc": now, "via": "adopt"})
        manifest["regions"] = data.get("regions") or []
        manifest["topology"] = {"file": None, "nodes": [
            {"name": n, "plan": v.get("type"), "region": v.get("region"), "role": v.get("role"),
             "site": v.get("site")} for n, v in sorted((data.get("topology") or {}).items())]}
        manifest["lab"]["cost_usd"] = data.get("lab_cost_usd")
        manifest["lab"]["run_tag"] = f"needletail-small-mesh-{args.run_id}" if data.get("topology") else None
        for s in data.get("steps") or []:
            for lane, count in sorted((s.get("listeners_per_lane") or {}).items()):
                manifest["listener_steps"].append({
                    "step": f"{s['session']}/{s['step']}", "lane": lane, "listeners_total": count,
                    "listeners_by_region": {}, "paths_per_listener": 1,
                    "at_utc": iso(s.get("started_unix_s")), "seconds": s.get("seconds")})
        for p in sorted(path.rglob("*")):
            if not p.is_file() or p.name in (ra.MANIFEST, ra.LOCK):
                continue
            rel = p.relative_to(path).as_posix()
            size, digest = ra._hash_file(p, ra.HASH_LIMIT_BYTES)
            kind, text, units = describe(rel)
            if rel == v0.name:
                kind, text = "manifest", "this run's manifest in archive-run.py's first format (23 Nov)"
            manifest["files"].append(ra.RunArchive._entry(rel, size, digest, kind, text, units, str(p), TOOL))
        manifest["collected"] = {"complete": True, "at_utc": now, "missing": []}
        manifest["status"] = "complete"
        manifest["status_reason"] = "; ".join(x for x in (
            "imported after the lab was gone: node journals were not collected then",
            data.get("lab_cost_note"), data.get("note"), data.get("what"), args.note) if x)
        run._write(manifest)
    print(f"{args.run_id}: {len(manifest['files'])} files")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create")
    c.add_argument("run_id"); c.add_argument("--topology"); c.add_argument("--code", action="append")
    c.add_argument("--settings")
    c = sub.add_parser("collect-nodes")
    c.add_argument("run_id")
    c = sub.add_parser("store")
    c.add_argument("run_id"); c.add_argument("--sessions", default="")
    c.add_argument("--log", action="append"); c.add_argument("--extra", action="append")
    c = sub.add_parser("lab")
    c.add_argument("run_id"); c.add_argument("--hours-billed", type=float)
    c.add_argument("--cost-usd", type=float); c.add_argument("--deleted", action="store_true")
    c = sub.add_parser("finish")
    c.add_argument("run_id"); c.add_argument("--status", default="complete", choices=["complete", "failed"])
    c.add_argument("--reason")
    c = sub.add_parser("adopt")
    c.add_argument("run_id"); c.add_argument("--kind", default="legacy", choices=ra.KINDS)
    c.add_argument("--note")
    args = ap.parse_args()
    {"create": cmd_create, "collect-nodes": cmd_collect, "store": cmd_store, "lab": cmd_lab,
     "finish": cmd_finish, "adopt": cmd_adopt}[args.command](args)


if __name__ == "__main__":
    main()
