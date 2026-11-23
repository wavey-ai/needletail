#!/usr/bin/env python3
"""Keep a run's raw data in the archive (owner, 23 Nov: "make sure we keep all
this data so we can create detailed reports and graphs").

    archive-run.py collect-nodes RUN_ID        # before `down`: every node's journals
    archive-run.py store RUN_ID --sessions a,b [--cost-usd X] [--heads repo=sha,...]
                   [--settings FILE] [--log FILE ...] [--extra DIR ...]

The archive is NEEDLETAIL_DATA_ROOT (default ~/needletail-data), one directory
per run id (the lab's run id, e.g. cmpd5), never inside a repository or a
build directory. Nothing in it is ever deleted: `store` copies with rsync and
never removes, so it can be run again as a run goes on.

  <run>/sessions/<session>/            the harness's run directory, verbatim:
      <step>/meta.json                 step settings, lanes, impairment
      <step>/<host>/<lane>.probe.csv   one line per unit received (latency_us,
                                       complete_unix_us, unit number, ...)
      <step>/<host>/<lane>.probe.json  the probe's summary and histograms
      <step>/<host>/*.arrivals.csv     UDP client: every datagram's arrival
      samplers/<node>.<t>.log          5 s: /proc/stat, /proc/net/dev, service
                                       CPU ticks and RSS (raw)
      samplers/<node>.stats.<t>.log    1 s: /api/stats, UDP errors, socket drops
      samplers/<node>.stalls.<t>.json  the node's stop journal (/api/stalls)
      samplers/hosts.<t>.txt           physical host of every node
  <run>/sessions/<session>/samplers-csv/<node>.cpu-net.csv
                                       the 5 s samples as CSV, units in names
  <run>/sessions/<session>/samplers-csv/<node>.stats.ndjson
                                       the 1 s samples as NDJSON
  <run>/nodes/<node>/journal.txt.gz    every service's journal since boot
  <run>/nodes/<node>/api-stats.json, api-stalls.json
  <run>/lab/                           lab state, topology, settings, cost
  <run>/logs/                          the run's driver logs
  <run>/lead/                          files the lead's tools add (listed)
  <run>/manifest.json                  run id, times, heads, settings, topology,
                                       regions, listener counts per step, cost,
                                       and every file with what it holds
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pathlib
import re
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
RUNS = ROOT / "target" / "compare" / "runs"
DATA = pathlib.Path(os.environ.get("NEEDLETAIL_DATA_ROOT", pathlib.Path.home() / "needletail-data"))

WHAT = [
    (r"/meta\.json$", "step settings: lanes, listeners, impairment, renditions"),
    (r"\.probe(-\d+)?\.csv$", "per-unit records of one listener: kind,index,latency_us,complete_unix_us,"
                              "number,bytes,key,packets,server timestamps (us)"),
    (r"\.probe(-\d+)?\.json$", "one listener's probe summary: counts, latency histograms (ms), first audio (ms)"),
    (r"\.join-\d+\.json$", "one new listener's time to first audio (ms)"),
    (r"\.arrivals\.csv$", "UDP client: every datagram's arrival (unix us)"),
    (r"\.uc\.json$", "UDP client report: fast lane, FEC, repair counters"),
    (r"\.udp\.json$", "UDP client report"),
    (r"analysis\.json$", "cached analysis of the step (analysis_version inside)"),
    (r"samplers/[^/]+\.stats\.\d+\.log$", "1 s raw: unix_s | UDP errors | socket drops | /api/stats JSON"),
    (r"samplers/[^/]+\.stalls\.\d+\.json$", "the node's stop journal (/api/stalls)"),
    (r"samplers/hosts\.\d+\.txt$", "physical Linode host of every node"),
    (r"samplers/[^/]+\.\d+\.log$", "5 s raw: unix_s, /proc/stat cpu ticks, /proc/net/dev bytes, service ticks and RSS (kB)"),
    (r"samplers-csv/.*\.cpu-net\.csv$", "5 s samples as CSV (ticks are USER_HZ 100/s; bytes cumulative)"),
    (r"samplers-csv/.*\.stats\.ndjson$", "1 s samples as NDJSON"),
    (r"session\.json$", "the session's steps, lanes, phases, times, LiveKit Cloud egress"),
    (r"nodes/.*/journal\.txt\.gz$", "the node's service journals since boot (gzip)"),
    (r"nodes/.*/api-.*\.json$", "the node's API at collection time"),
    (r"lab/", "lab state, topology, settings, cost"),
    (r"logs/", "driver logs of the run"),
    (r"lead/", "files added by the lead's tools"),
    (r"\.ndjson$", "hop journal or other per-event records"),
    (r"errors\.log$", "the host's command errors for the step"),
    (r"\.(ingest\.log|err|log)$", "a client's or tool's own log"),
    (r"\.player\.json$", "Chrome player's report (WebRTC stats, latency)"),
]


def describe(rel: str) -> str:
    for pattern, text in WHAT:
        if re.search(pattern, rel):
            return text
    return ""


LINK_DEST: pathlib.Path | None = None   # an earlier verbatim copy: identical files are hard-linked


def rsync(src: pathlib.Path, dst: pathlib.Path, link: pathlib.Path | None = None) -> None:
    (dst if src.is_dir() else dst.parent).mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a"] + ([f"--link-dest={link}"] if link and link.is_dir() else []) + [f"{src}/" if src.is_dir() else str(src),
                    f"{dst}/" if src.is_dir() else str(dst)], check=True)


CPU = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice"]


def convert_samplers(session_dir: pathlib.Path) -> None:
    out = session_dir / "samplers-csv"
    out.mkdir(exist_ok=True)
    for path in sorted((session_dir / "samplers").glob("*.log")):
        name = path.name
        if ".stats." in name:
            lines = []
            for line in path.read_text(errors="replace").splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2:
                    continue
                rec: dict = {"unix_s": float(parts[0]) if parts[0] else None}
                for part in parts[1:]:
                    if part.startswith("sockdrops"):
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
                    for item in fields[1:]:
                        iface, rx, tx = item.split(":")
                        row[f"net_{iface}_rx_bytes"], row[f"net_{iface}_tx_bytes"] = rx, tx
                elif len(fields) == 4:
                    service, _pid, ticks, rss = fields
                    row[f"{service}_cpu_ticks"], row[f"{service}_rss_kb"] = ticks, rss
            for key in row:
                if key not in columns:
                    columns.append(key)
            rows.append(row)
        text = ",".join(columns) + "\n" + "".join(",".join(str(r.get(c, "")) for c in columns) + "\n" for r in rows)
        (out / (name[:-4] + ".cpu-net.csv")).write_text(text)


def collect_nodes(run_id: str) -> None:
    import small_mesh_common as common
    state = common.load_state()
    if state.get("run_id") != run_id:
        sys.exit(f"the active lab is {state.get('run_id')}, not {run_id}")
    base = DATA / run_id / "nodes"

    def one(item: dict) -> None:
        d = base / item["name"]
        d.mkdir(parents=True, exist_ok=True)
        text = common.ssh_text(item, "journalctl -b --no-pager -o short-iso-precise 2>/dev/null | tail -n 2000000",
                               check=False, timeout=600)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        with gzip.open(d / f"journal.{stamp}.txt.gz", "wt") as f:
            f.write(text)
        if item["role"] in ("contributor", "distributor", "edge", "backbone"):
            port = 19443 if item["role"] == "contributor" else 19444
            for api in ("stats", "stalls"):
                t = common.ssh_text(item, f"curl -sk --max-time 20 'https://{item['public_ipv4']}:{port}/api/{api}'",
                                    check=False, timeout=60)
                (d / f"api-{api}.{stamp}.json").write_text(t + "\n")

    common.parallel(list(state["nodes"].values()), one)
    lab = DATA / run_id / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    (lab / f"state.{int(time.time())}.json").write_text(json.dumps(state, indent=1) + "\n")
    hosts = subprocess.run([sys.executable, str(ROOT / "scripts" / "linode-small-mesh-lab.py"), "hosts"],
                           capture_output=True, text=True, check=False)
    (lab / f"hosts.{int(time.time())}.txt").write_text(hosts.stdout + hosts.stderr)
    print(f"collected {len(state['nodes'])} nodes into {base}")


def store(args) -> None:
    run = DATA / args.run_id
    run.mkdir(parents=True, exist_ok=True)
    sessions = [s for s in args.sessions.split(",") if s]
    for name in sessions:
        rsync(RUNS / name, run / "sessions" / name,
              pathlib.Path(args.link_dest) / name if args.link_dest else None)
        if (run / "sessions" / name / "samplers").is_dir():
            convert_samplers(run / "sessions" / name)
    for path in args.log or []:
        rsync(pathlib.Path(path), run / "logs" / pathlib.Path(path).name)
    for path in args.extra or []:
        p = pathlib.Path(path)
        rsync(p, run / "extra" / p.name)
    lab = run / "lab"
    lab.mkdir(exist_ok=True)
    if args.settings:
        rsync(pathlib.Path(args.settings), lab / pathlib.Path(args.settings).name)
    removed = ROOT / "target" / "linode-qualification" / f"small-mesh-lab-{args.run_id}.removed.json"
    for p in (removed, ROOT / "target" / "linode-qualification" / f"small-mesh-watchdog-{args.run_id}.log"):
        if p.is_file():
            rsync(p, lab / p.name)
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    states = sorted(lab.glob("state.*.json")) + ([lab / removed.name] if (lab / removed.name).is_file() else [])
    topology = {}
    if states:
        st = json.loads(states[-1].read_text())
        topology = {n: {k: v.get(k) for k in ("role", "region", "type", "site", "host_uuid", "id")}
                    for n, v in (st.get("nodes") or {}).items()}
    steps, started, ended = [], [], []
    for name in sessions:
        sj = run / "sessions" / name / "session.json"
        for record in (json.loads(sj.read_text()) if sj.is_file() else []):
            started.append(record.get("started_unix"))
            ended.append(record.get("ended_unix"))
        for meta in sorted((run / "sessions" / name).glob("*/meta.json")):
            m = json.loads(meta.read_text())
            per_lane: dict = {}
            for report in meta.parent.glob("*/*.json"):
                hit = re.match(r"(.+?)\.(probe(-\d+)?|join-\d+)\.json$", report.name)
                if hit:
                    per_lane[hit.group(1)] = per_lane.get(hit.group(1), 0) + 1
            steps.append({"session": name, "step": meta.parent.name, "started_unix_s": m.get("started_unix"),
                          "seconds": m.get("seconds"), "lanes": m.get("lanes"), "netem": m.get("netem"),
                          "listeners_per_lane": per_lane})
    files = []
    for p in sorted(run.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            rel = str(p.relative_to(run))
            files.append({"path": rel, "bytes": p.stat().st_size, "holds": describe("/" + rel)})
    heads = dict(manifest.get("code_heads") or {})
    for item in (args.heads or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            heads[k] = v
    manifest.update({
        "run_id": args.run_id, "archive_root": str(DATA), "written_unix_s": time.time(),
        "sessions": sorted(set((manifest.get("sessions") or []) + sessions)),
        "started_unix_s": min([t for t in started if t] or [None]) if any(started) else manifest.get("started_unix_s"),
        "ended_unix_s": max([t for t in ended if t] or [None]) if any(ended) else manifest.get("ended_unix_s"),
        "code_heads": heads,
        "deploy_settings_file": args.settings and str(pathlib.Path("lab") / pathlib.Path(args.settings).name),
        "topology": topology or manifest.get("topology"),
        "regions": sorted({v.get("region") for v in (topology or {}).values() if v.get("region")}),
        "steps": steps,
        "lab_cost_usd": args.cost_usd if args.cost_usd is not None else manifest.get("lab_cost_usd"),
        "files": files,
    })
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"{run}: {len(files)} files, {sum(f['bytes'] for f in files) / 1e9:.2f} GB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect-nodes")
    c.add_argument("run_id")
    s = sub.add_parser("store")
    s.add_argument("run_id")
    s.add_argument("--sessions", default="")
    s.add_argument("--cost-usd", type=float)
    s.add_argument("--heads", default="")
    s.add_argument("--settings")
    s.add_argument("--log", action="append")
    s.add_argument("--extra", action="append")
    s.add_argument("--link-dest", help="a verbatim copy of target/compare/runs: identical files are hard-linked")
    args = ap.parse_args()
    if args.command == "collect-nodes":
        collect_nodes(args.run_id)
    else:
        store(args)


if __name__ == "__main__":
    main()
