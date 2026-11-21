#!/usr/bin/env python3
"""Condense the evidence of an edge campaign run into one versioned file.

  edge-campaign-evidence.py --run-id ID --provider local|linode --out FILE \\
      [--cleanup KEY=VALUE ...] [--note TEXT ...] DIR [DIR ...]

Each DIR holds `<scenario>/evidence.json` files as `edge-campaign.py run` and
`linode-edge-campaign.py run` write them. When a scenario is in more than one
DIR, the last one counts and the earlier results are listed as `earlier`.

The file keeps, for each scenario: the question, the criterion, the
conditions, the events, every check with its detail, the measurements, the
summary of each group of readers, and the CPU and memory of each node. The
reports of single readers, the node samples and the logs stay in the raw
directories. `--markdown` prints the summary table instead.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUMMARY_KEYS = (
    "readers", "readers_ok", "parts_received", "parts_missing", "readers_stalled", "stalls",
    "stalled_ms", "longest_stall_ms", "max_behind_ms", "startup_ms_p50", "startup_ms_p99",
    "startup_ms_max", "latency_p50_ms_median", "latency_p99_ms_worst", "moves",
    "request_errors", "rejections", "entry_refusals", "readers_by_first_cache",
    "readers_by_final_cache", "load_host_busy_percent", "no_report")
NODE_KEYS = ("cpu_percent_mean", "cpu_percent_p95", "cpu_percent_max", "throttled_ms",
             "memory_mb_max", "memory_mb_last", "fds_max", "threads_max",
             "udp_rcvbuf_errors", "oom_kills", "not_running_samples")


def trim(value, limit: int = 600):
    """Keep a measurement readable: long lists and texts are cut."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + " …"
    if isinstance(value, list):
        return [trim(item, limit) for item in value[:40]]
    if isinstance(value, dict):
        return {key: trim(item, limit) for key, item in list(value.items())[:60]}
    return value


def condense(evidence: dict) -> dict:
    readers = {}
    for key, job in evidence.get("readers", {}).items():
        summary = job.get("summary", {})
        readers[key] = {"host": job.get("host"), "stream": job.get("stream"),
                        "seconds": job.get("seconds"),
                        **{name: summary[name] for name in SUMMARY_KEYS if name in summary}}
    nodes = {name: {key: values[key] for key in NODE_KEYS if key in values}
             for name, values in evidence.get("nodes", {}).items() if isinstance(values, dict)}
    return {
        "scenario": evidence["scenario"], "group": evidence["group"],
        "question": evidence["question"], "criterion": evidence["criterion"],
        "result": evidence["result"], "gap": evidence.get("gap"),
        "started_utc": evidence["started_utc"], "seconds": evidence.get("seconds"),
        "conditions": evidence.get("conditions", {}),
        "events": trim(evidence.get("events", [])[:30], 200),
        "checks": [{"check": check["check"], "pass": bool(check["ok"]),
                    "detail": trim(check.get("detail"), 400)}
                   for check in evidence.get("checks", [])],
        "measurements": trim(evidence.get("measurements", {})),
        "readers": readers, "nodes": nodes,
        "notes": trim(evidence.get("notes", [])),
    }


def revision(path: pathlib.Path) -> dict:
    def git(*arguments):
        return subprocess.run(["git", "-C", str(path), *arguments], capture_output=True,
                              text=True).stdout.strip()
    return {"branch": git("branch", "--show-current"), "commit": git("rev-parse", "HEAD"),
            "clean": git("status", "--porcelain", "--untracked-files=no") == ""}


def load(directories: list[pathlib.Path]) -> tuple[list[dict], dict]:
    scenarios: dict[str, dict] = {}
    lab = {}
    for directory in directories:
        for path in sorted(directory.glob("*/evidence.json")):
            evidence = json.loads(path.read_text())
            item = condense(evidence)
            item["raw"] = str(path.parent)
            earlier = scenarios.get(item["scenario"])
            if earlier:
                item["earlier"] = earlier.get("earlier", []) + [{
                    "result": earlier["result"], "started_utc": earlier["started_utc"],
                    "failed": [check["check"] for check in earlier["checks"]
                               if not check["pass"]], "raw": earlier["raw"]}]
            scenarios[item["scenario"]] = item
            lab = evidence.get("lab") or lab
    ordered = sorted(scenarios.values(), key=lambda item: item["started_utc"])
    return ordered, lab


def headline(item: dict) -> str:
    """One line of what a scenario showed: its failed checks, or what its
    players and readers saw."""
    failed = [check for check in item["checks"] if not check["pass"]]
    if failed:
        return "; ".join(f"{check['check']}: {check['detail']}" for check in failed[:2])
    parts = []
    for key, value in item["measurements"].items():
        if key.startswith("hlsjs") and isinstance(value, dict) and "media_played_s" in value:
            parts.append(f"hls.js: {value.get('media_played_s')} s of media in "
                         f"{value.get('wall_s')} s, {value.get('stalled_ms')} ms stalled, "
                         f"{value.get('frames_dropped')} of {value.get('frames')} frames "
                         "dropped")
    groups = [group for group in item["readers"].values() if group.get("readers")]
    if groups:
        total = sum(group.get("readers", 0) for group in groups)
        served = sum(group.get("readers_ok", 0) for group in groups)
        missing = sum(group.get("parts_missing", 0) for group in groups)
        received = sum(group.get("parts_received", 0) for group in groups)
        stalled = sum(group.get("readers_stalled", 0) for group in groups)
        longest = max(group.get("longest_stall_ms", 0) for group in groups)
        moves = sum(group.get("moves", 0) for group in groups)
        parts.append(f"{served} of {total} readers served, {missing} of {received + missing} "
                     f"parts missing, {stalled} readers stalled (longest {longest} ms), "
                     f"{moves} moves")
    if not parts:
        parts = [str(check["detail"]) for check in item["checks"][:2]]
    return "; ".join(parts)


def markdown(scenarios: list[dict]) -> str:
    lines = ["| Scenario | Result | What the run showed |", "| --- | --- | --- |"]
    for item in scenarios:
        text = headline(item).replace("|", "/").replace("\n", " ")[:260]
        result = item["result"] + (f" ({len(item['earlier'])} earlier)" if item.get("earlier")
                                   else "")
        lines.append(f"| `{item['scenario']}` | {result} | {text} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("directories", nargs="+", type=pathlib.Path)
    parser.add_argument("--run-id")
    parser.add_argument("--provider", choices=("local", "linode"), default="local")
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--cleanup", action="append", default=[])
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    scenarios, lab = load(args.directories)
    if args.markdown:
        print(markdown(scenarios))
        return
    counts: dict[str, int] = {}
    for item in scenarios:
        counts[item["result"]] = counts.get(item["result"], 0) + 1
    failed = [item["scenario"] for item in scenarios if item["result"] == "FAIL"]
    cleanup = {}
    for item in args.cleanup:
        key, _, value = item.partition("=")
        cleanup[key] = json.loads(value)
    workspace = ROOT.parent
    document = {
        "schema": "needletail.edge-campaign.v1",
        "run_id": args.run_id,
        "provider": "linode" if args.provider == "linode" else "local-namespaces",
        "raw_artifact_directory": ", ".join(str(directory) for directory in args.directories),
        "passed": not failed,
        "result": ("every scenario passed or is a recorded gap" if not failed else "failed"),
        "counts": counts,
        "lab": lab,
        "revisions": {name: revision(workspace / directory) for name, directory in (
            ("needletail", ROOT.name), ("av-mesh", "av-mesh-features"),
            ("playlists", "playlists-features"), ("av-contrib", "av-contrib-features"))},
        "notes": args.note,
        "scenarios": scenarios,
        "cleanup": cleanup,
    }
    if failed:
        document["failed_gate"] = (f"{len(failed)} of {len(scenarios)} scenarios failed: "
                                   + ", ".join(failed))
    text = json.dumps(document, indent=1, sort_keys=False) + "\n"
    if args.out:
        args.out.write_text(text)
        print(f"{args.out}: {len(scenarios)} scenarios, {counts}, {len(text) // 1024} kB")
    else:
        print(text)


if __name__ == "__main__":
    main()
