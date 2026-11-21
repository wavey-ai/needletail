#!/usr/bin/env python3
"""Failover test: play a stream with hls.js through the Multivariant Playlist,
kill the cache that serves it part-way through, and report what the player did.

Run it from the repository root, outside the namespaces, with the lab named:

    export NEEDLETAIL_EDGE_LAB_WORK=.../target/edge-lab-players
    python3 scripts/players/failover-test.py --stream 1 --seconds 60 --kill-after 20 \\
        --report scripts/players/samples/hlsjs-failover-1.json

The killed service is started again after the player has finished (use
--no-restart to leave it down). The player report gets a `failover` section.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

if not os.environ.get("NEEDLETAIL_EDGE_LAB_WORK"):
    sys.exit("failover-test: set NEEDLETAIL_EDGE_LAB_WORK to the work directory of the lab to test; refusing to guess")
sys.path.insert(0, str(HERE.parent))
import edge_lab as lab  # noqa: E402


def read_status(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def analyse(report: dict, victim: str, victim_host: str, kill_ms: float, restart_ms: float | None) -> dict:
    fragments = report["loads"]["fragments"]
    media = [f for f in fragments if f["kind"] in ("part", "segment")]
    before = [f for f in media if f["t"] <= kill_ms]
    after = [f for f in media if f["t"] > kill_ms]
    after_other = [f for f in after if f["host"] != victim_host]
    errors = [e for e in report["errors"]["list"] if e["t"] >= kill_ms]
    stalls = report["stalls"]

    def overlapping(items: list[dict]) -> list[dict]:
        return [{**s, "start_after_kill_ms": round(s["start_ms"] - kill_ms, 1)}
                for s in items if s["start_ms"] + s["duration_ms"] >= kill_ms]

    steering = report["steering"]
    loads_after = [s for s in steering["steering_manifest_loads"] if s["t"] >= kill_ms]
    first_without = next((s for s in loads_after if victim not in (s["manifest"] or {}).get("PATHWAY-PRIORITY", [])), None)
    first_demoted = next((s for s in loads_after if ((s["manifest"] or {}).get("PATHWAY-PRIORITY") or [victim])[0] != victim), None)
    moves = [m for m in steering["host_timeline"] if m["t"] >= kill_ms]
    samples = report["samples"]
    lat_before = sorted(s["latency_s"] for s in samples if kill_ms - 10000 <= s["t"] < kill_ms and s["latency_s"])
    lat_after = sorted(s["latency_s"] for s in samples if s["t"] >= report["playback"]["wall_s"] * 1000 - 10000 and s["latency_s"])
    pdt_before = sorted(s["pdt_latency_ms"] for s in samples if kill_ms - 10000 <= s["t"] < kill_ms and s["pdt_latency_ms"] is not None)
    pdt_after = sorted(s["pdt_latency_ms"] for s in samples if s["t"] >= report["playback"]["wall_s"] * 1000 - 10000 and s["pdt_latency_ms"] is not None)
    median = lambda values: values[len(values) // 2] if values else None  # noqa: E731
    playhead = overlapping(stalls["playhead"])
    waiting = overlapping(stalls["element_waiting"])
    first_stall = min((s["start_ms"] for s in playhead + waiting), default=None)
    resumed = None
    if playhead or waiting:
        ends = [s["start_ms"] + s["duration_ms"] for s in playhead + waiting if s.get("resolved")]
        resumed = max(ends) if ends and all(s.get("resolved") for s in playhead + waiting) else None
    return {
        "victim": victim, "victim_host": victim_host,
        "kill_ms": round(kill_ms, 1), "restart_ms": round(restart_ms, 1) if restart_ms is not None else None,
        "last_media_from_victim_ms": before[-1]["t"] if before else None,
        "media_loaded_from_victim_after_kill": len([f for f in after if f["host"] == victim_host]),
        "first_media_from_another_cache_ms": after_other[0]["t"] if after_other else None,
        "first_media_from_another_cache": after_other[0] if after_other else None,
        "media_gap_after_kill_ms": round(after_other[0]["t"] - kill_ms, 1) if after_other else None,
        "media_gap_since_last_load_ms": round(after_other[0]["t"] - before[-1]["t"], 1) if after_other and before else None,
        "moved_to_host": after_other[0]["host"] if after_other else None,
        "moved_to_pathway": after_other[0]["pathway"] if after_other else None,
        "host_moves_after_kill": moves,
        "first_error_after_kill_ms": round(errors[0]["t"] - kill_ms, 1) if errors else None,
        "errors_after_kill": [{"after_kill_ms": round(e["t"] - kill_ms, 1), "type": e["type"], "details": e["details"], "fatal": e["fatal"],
                               "response_code": e["response_code"], "host": e["host"], "message": e["message"], "error_action": e["error_action"],
                               "pathway": e["pathway"]} for e in errors],
        "error_counts_after_kill": {d: sum(1 for e in errors if e["details"] == d) for d in sorted({e["details"] for e in errors})},
        "recoveries_after_kill": [r for r in report["errors"]["recoveries"] if r["t"] >= kill_ms],
        "playhead_stalls": playhead, "waiting_stalls": waiting,
        "first_stall_after_kill_ms": round(first_stall - kill_ms, 1) if first_stall is not None else None,
        "playback_resumed_after_kill_ms": round(resumed - kill_ms, 1) if resumed is not None else None,
        "stalled_ms": report["stalls"]["total_stalled_ms"],
        "steering_first_manifest_without_victim_after_kill_ms": round(first_without["t"] - kill_ms, 1) if first_without else None,
        "steering_first_manifest_with_victim_not_first_after_kill_ms": round(first_demoted["t"] - kill_ms, 1) if first_demoted else None,
        "steering_priority_at_end": steering["final_pathway_priority"],
        "pathway_at_end": steering["final_pathway"],
        "pathway_events_after_kill": [e for e in steering["pathway_events"] if e["t"] >= kill_ms],
        "level_events_after_kill": [e for e in steering["level_events"] if e["t"] >= kill_ms],
        "hls_latency_p50_s": {"10_s_before_kill": median(lat_before), "last_10_s": median(lat_after)},
        "pdt_latency_p50_ms": {"10_s_before_kill": median(pdt_before), "last_10_s": median(pdt_after)},
        "failed_requests_after_kill": [f for f in report["network"]["failed"] if f["t"] >= kill_ms][:40],
        "non_2xx_after_kill": [f for f in report["network"]["non_2xx"] if f["t"] >= kill_ms][:40],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stream", type=int, default=1)
    parser.add_argument("--entry", default="lon-dist-a", help="entry point (distributor) node name")
    parser.add_argument("--reader", default="lon-readers", help="reader host namespace")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--kill-after", type=float, default=20.0, help="seconds on the player clock before the cache is killed")
    parser.add_argument("--report", required=True)
    parser.add_argument("--sid", default=f"failover-{int(time.time())}")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--restart-during-run-after", type=float, default=None,
                        help="start the killed service again this many seconds after the kill, while the player still runs")
    parser.add_argument("--player-arg", action="append", default=[], help="extra argument for hlsjs-play.mjs, repeatable")
    args = parser.parse_args()

    entry = lab.node(args.entry)
    url = f"https://{entry['public_ipv4']}:{lab.HTTP_PORT}/live/{args.stream}/master.m3u8?sid={args.sid}"
    report_path = pathlib.Path(args.report).resolve()
    status_path = report_path.with_suffix(".status.json")
    status_path.unlink(missing_ok=True)
    command = lab.enter(args.reader) + [
        "node", str(HERE / "hlsjs-play.mjs"), "--url", url, "--seconds", str(args.seconds), "--report", str(report_path),
        "--status-file", str(status_path), "--label", f"failover-{args.stream}", "--quiet", *args.player_arg]
    print(f"player: {url}", flush=True)
    player = subprocess.Popen(command)
    victim = victim_host = None
    kill_epoch_ms = restart_epoch_ms = None
    by_address = {node[4]: node[0] for node in lab.NODES}
    try:
        while player.poll() is None:
            status = read_status(status_path)
            if status and victim is None and status["t"] >= args.kill_after * 1000.0:
                victim_host = status.get("host")
                victim = status.get("pathway") if status.get("pathway") in {n[0] for n in lab.NODES} else by_address.get((victim_host or "").split(":")[0])
                if not victim:
                    print(f"cannot tell which cache serves the player (status: {status}); not killing anything", flush=True)
                    victim = ""
                    continue
                try:
                    sessions = lab.edge_view(victim)["sessions"]
                except Exception as error:  # the view is extra evidence, not a requirement
                    sessions = f"unavailable ({error})"
                lab.kill_service(victim)
                kill_epoch_ms = time.time() * 1000.0
                print(f"t={status['t'] / 1000.0:.1f}s player is on {victim} ({victim_host}), {sessions} session(s) there; killed {victim}", flush=True)
            if (victim and kill_epoch_ms and restart_epoch_ms is None and args.restart_during_run_after is not None
                    and time.time() * 1000.0 - kill_epoch_ms >= args.restart_during_run_after * 1000.0):
                lab.start_service(victim)
                restart_epoch_ms = time.time() * 1000.0
                print(f"started {victim} again {args.restart_during_run_after:g} s after the kill", flush=True)
            time.sleep(0.1)
    finally:
        if player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=15)
            except subprocess.TimeoutExpired:
                player.kill()
        if victim and restart_epoch_ms is None and not args.no_restart:
            lab.start_service(victim)
            print(f"started {victim} again", flush=True)
            try:
                lab.wait_for(f"{victim} answers", lambda: lab.curl(victim, "/api/mesh/local")[0] == 200, timeout=30)
            except Exception as error:
                print(f"warning: {victim} did not answer after the restart: {error}", flush=True)
    status_path.unlink(missing_ok=True)

    report = json.loads(report_path.read_text())
    if victim and kill_epoch_ms:
        started = report["harness"]["started_epoch_ms"]
        report["failover"] = analyse(report, victim, victim_host, kill_epoch_ms - started,
                                     restart_epoch_ms - started if restart_epoch_ms else None)
        report_path.write_text(json.dumps(report, indent=1))
        summary = {k: v for k, v in report["failover"].items()
                   if k not in ("errors_after_kill", "failed_requests_after_kill", "non_2xx_after_kill", "first_media_from_another_cache",
                                "pathway_events_after_kill", "level_events_after_kill")}
        print(json.dumps(summary, indent=1))
    else:
        print("no cache was killed")
    print(f"verdict: {report['verdict']['result']} - {report['verdict']['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
