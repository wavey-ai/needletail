#!/usr/bin/env python3
"""Do two local labs on one host measure what one lab alone measures? (C 6.1)

  lab-parity.py [--repeats 3] [--layouts Q0,Q1,Q2,Q3] [--plan | --compare]

Runs a set K in each layout, --repeats times, then compares. K, on lab `a`:
  a clean latency-bench pass of opus-1 and one of pcm-1: 40 s, two UDP
  clients a host, 40 crowd listeners an edge in London and New York;
  change-run of roll-edges (scripts/edge_lab.py roll edges --sites london,
  360 s probes) and of roll-forwarders (scripts/edge_lab.py roll forwarders,
  480 s: a roll of four forwarders may take two minutes, and the referee's
  scorer needs 120 no-change windows of its length after it), each also with
  --dry (its null).
The layouts:
  Q0  one lab, every node on CPUs of its own (node_cpus=0), as the earlier
      proofs ran;
  Q1  one lab on cpus=2-15, nothing else on the host;
  Q2  labs a (cpus=2-15) and b (cpus=16-29) at once, both running K;
  Q3  lab a runs K while lab b (cpus=16-29) burns every node (edge_lab.py
      burn, each node's burn started again as it ends) and carries a 247
      Mbit/s 4K pass (latency-bench.py --video-mbps 247) over and over, until
      K is done; the share of K's time that each was running is recorded.
Q0 and Q1 are refused while another lab is registered on the host (the CL1
registry), and another lab registered when they end fails them.

Each invocation has a run id, in the names of its latency-bench and
change-run results, and target/lab-parity/layouts.json keeps, for each
layout, the id of the invocation that ran it last, its jobs' exits and the
labs seen: --layouts Q1 runs Q1 again and the comparison reads that run only.

Compared for each host and lane: the steady longest gap (arrival to arrival,
the first 5 s left out), the maximum and the p99 of the clean passes, and
change-window.py's verdicts. Pass:
  - every layout measured: each its --repeats clean passes for each host and
    lane, and its --repeats scored change and null runs of each scenario
    (a void, unscored, interrupted or otherwise faulty run is not measured),
    and every latency-bench and load job exited 0;
  - 0 missing everywhere;
  - every --dry run passes in every layout;
  - the verdicts the same in Q1, Q2 and Q3 (passes of the repeats, for each
    scenario, kind and probe);
  - the median p99 of lab a in Q2 and in Q3 within 0.15 ms of Q1's, and
    Q1's within 0.15 ms of Q0's;
  - the steady longest gap in Q2 and Q3 no more than Q1's plus one unit.
If Q1's own repeats spread by more than 0.15 ms (p99, max - min) the bound is
not widened: p99- and silence-gated proofs then run in one lab on
cpus=2-29 with the host otherwise idle, and a second lab is used only for
proofs that count parts and moves. Two labs may run gated proofs only when
all four layouts were measured and agree. The report says which.

The accounting floor (C 2.12): the largest outside share of Q1's change-run
runs, written to target/lab-parity/floor.json (with its layout, cpus=2-15)
when Q1 was measured in full with no other lab on the host; change-run.py
reads it (a run on that layout more than 2 points above it is void).

--plan prints every command and runs nothing; --compare compares what the
recorded runs left and runs nothing. Results: target/lab-parity/parity.json,
report.txt, layouts.json, floor.json. Exit 0 when the layouts agree. About
two hours with three repeats; nothing else may run on the host meanwhile.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import pathlib
import statistics
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import edge_lab as lab  # noqa: E402


def module(name: str, file: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / file)
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[name] = loaded
        spec.loader.exec_module(loaded)
    return sys.modules[name]


bench = module("latency_bench", "latency-bench.py")
change_run = module("change_run", "change-run.py")

OUT = ROOT / "target" / "lab-parity"
CHANGE_RUNS = ROOT / "target" / "change-run"
LAYOUTS = {
    "Q0": {"a": "node_cpus=0"},
    "Q1": {"a": "cpus=2-15"},
    "Q2": {"a": "cpus=2-15", "b": "cpus=16-29"},
    "Q3": {"a": "cpus=2-15", "b": "cpus=16-29"},
}
# The layouts measured with nothing else on the host.
ALONE = ("Q0", "Q1")
CLEAN_LANES = {"opus-1": "opus", "pcm-1": "pcm"}
CLEAN_SECONDS = 40
# latency-bench's UDP clients: one record per host of these.
CLEAN_HOSTS = list(bench.READER_HOSTS)
# edge_lab.py is not executable: the change runs it with this interpreter.
CHANGES = {
    "roll-edges": {"seconds": 360, "command": [sys.executable, str(SCRIPTS / "edge_lab.py"),
                                               "roll", "edges", "--sites", "london"]},
    "roll-forwarders": {"seconds": 480, "command": [sys.executable, str(SCRIPTS / "edge_lab.py"),
                                                    "roll", "forwarders"]},
}
P99_BOUND_MS = 0.15
SKIP_SECONDS = 5
BURN_SECONDS = 60
VIDEO_MBPS = 247


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", file=sys.stderr, flush=True)


def new_run_id() -> str:
    return time.strftime("%Y%m%d%H%M%S")


# ------------------------------------------------------------------ the plan

def clean_name(run_id: str, layout: str, repeat: int, lane: str) -> str:
    return f"parity-{run_id}-{layout.lower()}-r{repeat}-{lane}"


def change_name(run_id: str, layout: str, scenario: str) -> str:
    return f"parity-{run_id}-{layout.lower()}-{scenario}"


def k_jobs(layout: str, ident: str, repeat: int, run_id: str) -> list[dict]:
    """One pass of K on lab `ident` in `layout`: each job a command and the
    lab it runs on. A latency-bench job is judged by its exit (it exits 0
    when it measured); a change-run job by the runs it leaves (it exits 1
    when a change fails the referee's rule, which is a result)."""
    setting = LAYOUTS[layout][ident]
    jobs = []
    for lane, payload in CLEAN_LANES.items():
        jobs.append({"lab": ident, "judge_exit": True, "command": [
            str(HERE / "latency-bench.py"), "--name", clean_name(run_id, layout, repeat, lane),
            "--payload", payload, "--seconds", str(CLEAN_SECONDS), "--udp-clients", "2",
            "--load-listeners", "40", "--load-sites", "london,newyork", "--set", setting]})
    for scenario, change in CHANGES.items():
        for dry in (False, True):
            jobs.append({"lab": ident, "judge_exit": False, "command": [
                str(HERE / "change-run.py"), "run",
                "--name", change_name(run_id, layout, scenario), "--lanes", "opus-1,pcm-1",
                "--seconds", str(change["seconds"]), "--set", setting,
                *(["--dry"] if dry else []), "--", *change["command"]]})
    return jobs


def load_jobs() -> list[dict]:
    """Q3's load on lab b, each job run again as it ends: every node's burn
    and the 4K pass."""
    nodes = [name for name, role, *_ in lab.NODES
             if role != "backbone" and name not in lab.DEFAULT_SLOTS]
    jobs = [{"lab": "b", "judge_exit": True, "name": f"burn {name}",
             "command": [str(SCRIPTS / "edge_lab.py"), "burn", name, str(BURN_SECONDS)]}
            for name in nodes]
    jobs.append({"lab": "b", "judge_exit": True, "name": "4k", "command": [
        str(HERE / "latency-bench.py"), "--name", "parity-q3-b-4k", "--no-up", "--keep-up",
        "--video-mbps", str(VIDEO_MBPS), "--seconds", str(BURN_SECONDS), "--udp-clients", "0"]})
    return jobs


def plan(layouts: list[str], repeats: int, run_id: str) -> dict:
    """What runs in each layout: K on lab a (and on b in Q2), and Q3's load."""
    out = {}
    for layout in layouts:
        labs = {ident: [job for repeat in range(1, repeats + 1)
                        for job in k_jobs(layout, ident, repeat, run_id)]
                for ident in (("a", "b") if layout == "Q2" else ("a",))}
        out[layout] = {"labs": labs}
        if layout == "Q3":
            out[layout]["load"] = {
                "up": {"lab": "b", "judge_exit": True,
                       "command": [str(SCRIPTS / "edge_lab.py"), "up", LAYOUTS["Q3"]["b"]]},
                "loop": load_jobs(),
                "down": {"lab": "b", "judge_exit": True,
                         "command": [str(SCRIPTS / "edge_lab.py"), "down"]}}
    return out


# ------------------------------------------------------------------ running it

def execute(job: dict) -> int:
    env = {**os.environ, "NEEDLETAIL_EDGE_LAB_ID": job["lab"]}
    env.pop("NEEDLETAIL_EDGE_LAB_WORK", None)
    log(f"lab {job['lab']}: {' '.join(job['command'])}")
    return subprocess.run([sys.executable, *job["command"]], env=env, cwd=ROOT).returncode


def job_record(job: dict, code: int) -> dict:
    return {"command": job["command"], "lab": job["lab"], "judge_exit": job["judge_exit"],
            "exit": code}


def run_lab(jobs: list[dict], codes: list) -> None:
    for job in jobs:
        codes.append(job_record(job, execute(job)))


def run_again(job: dict, done: threading.Event, runs: list) -> None:
    """Run `job` again as soon as it ends, until `done`; each run's
    [began, ended, exit]. A failed run stops the loop (it is a failure of
    the layout)."""
    while not done.is_set():
        began = time.time()
        code = execute(job)
        runs.append([began, time.time(), code])
        if code != 0:
            return


def covered_share(runs: list, start: float, end: float) -> float:
    """The share of [start, end] that the runs' intervals cover."""
    if end <= start:
        return 0.0
    covered, reach = 0.0, start
    for began, ended, _code in sorted(runs):
        began, ended = max(began, reach), min(ended, end)
        if ended > began:
            covered += ended - began
            reach = ended
    return round(covered / (end - start), 3)


def other_labs() -> list[str]:
    """The registered labs other than lab a, as `id (owner, CPUs)`."""
    return [f"{entry['id']} ({entry['owner']}, worktree {entry['worktree']})"
            for entry in lab.registered_labs() if entry["id"] != "a"]


def run_layout(layout: str, steps: dict) -> dict:
    """K on each lab of the layout at once; in Q3 the load on lab b, each
    job started again as it ends, until lab a's K is done. The record of
    the layout: its jobs' exits, and in Q3 each load job's runs and the
    share of K's time it was running."""
    record: dict = {"jobs": [], "load": {}}
    threads = [threading.Thread(target=run_lab, args=(jobs, record["jobs"]))
               for jobs in steps["labs"].values()]
    load = steps.get("load")
    if load:
        code = execute(load["up"])
        record["jobs"].append(job_record(load["up"], code))
        if code != 0:
            raise SystemExit("Q3: lab b did not come up")
    done = threading.Event()
    loops = []
    record["started_unix"] = time.time()
    for thread in threads:
        thread.start()
    try:
        for job in (load or {}).get("loop", []):
            runs: list = []
            record["load"][job["name"]] = {"command": job["command"], "runs": runs}
            loops.append(threading.Thread(target=run_again, args=(job, done, runs)))
        for item in loops:
            item.start()
        for thread in threads:
            thread.join()
    finally:
        record["ended_unix"] = time.time()
        done.set()
        for item in loops:
            item.join()
        if load:
            record["jobs"].append(job_record(load["down"], execute(load["down"])))
    for item in record["load"].values():
        item["share"] = covered_share(item["runs"], record["started_unix"],
                                      record["ended_unix"])
    return record


def load_state() -> dict:
    try:
        return json.loads((OUT / "layouts.json").read_text())
    except (OSError, ValueError):
        return {}


def save_layout(layout: str, record: dict) -> None:
    """layouts.json: the last run of each layout, written as each ends."""
    state = load_state()
    state[layout] = record
    OUT.mkdir(parents=True, exist_ok=True)
    temporary = OUT / "layouts.json.tmp"
    temporary.write_text(json.dumps(state, indent=1) + "\n")
    temporary.replace(OUT / "layouts.json")


# ------------------------------------------------------------------ what the runs left

def bench_dir(name: str, ident: str) -> pathlib.Path:
    """latency-bench's directory of a run of lab `ident` (C 2.12)."""
    return bench.OUT_ROOT / ident / name


def steady_gap_ms(path: pathlib.Path) -> float | None:
    """The longest silence between arrivals of a UDP client's record, the
    first SKIP_SECONDS after its first arrival left out."""
    try:
        with path.open() as handle:
            arrivals = sorted(int(row["arrival_unix_us"]) for row in csv.DictReader(handle))
    except (OSError, KeyError, ValueError):
        return None
    if not arrivals:
        return None
    steady = [t for t in arrivals if t >= arrivals[0] + SKIP_SECONDS * 1_000_000]
    gaps = [b - a for a, b in zip(steady, steady[1:])]
    return round(max(gaps) / 1000, 3) if gaps else None


def clean_figures(directory: pathlib.Path, lane: str) -> dict:
    """{lane@host: {p99_ms, max_ms, gap_ms, missing}} of a clean pass."""
    try:
        hosts = json.loads((directory / "hops.json").read_text())["udp_clients"]["hosts"]
    except (OSError, ValueError, KeyError):
        return {}
    out = {}
    for host, item in hosts.items():
        latency = item.get("unit_latency_ms") or {}
        gaps = [gap for gap in (steady_gap_ms(path)
                                for path in sorted(directory.glob(f"{host}.uc*.arrivals.csv")))
                if gap is not None]
        out[f"{lane}@{host}"] = {"p99_ms": latency.get("p99"), "max_ms": latency.get("max"),
                                 "gap_ms": max(gaps) if gaps else None,
                                 "missing": item.get("missing")}
    return out


def change_figures(scenario: pathlib.Path) -> dict:
    """{kind: [{status, reasons, labels, interference, directory}]} of a
    scenario's runs (a run with no status was interrupted)."""
    out: dict = {"change": [], "null": []}
    for run in change_run.runs_of(scenario):
        out[run["kind"]].append({"status": run.get("status") or "interrupted",
                                 **{key: run.get(key) for key in
                                    ("reasons", "labels", "interference", "directory")}})
    return out


def collect(state: dict) -> dict:
    """What lab a left in each layout's recorded run."""
    out = {}
    for layout, record in state.items():
        run_id, repeats = record["run_id"], record["repeats"]
        clean: dict = {}
        for repeat in range(1, repeats + 1):
            for lane in CLEAN_LANES:
                for key, figures in clean_figures(
                        bench_dir(clean_name(run_id, layout, repeat, lane), "a"), lane).items():
                    clean.setdefault(key, []).append(figures)
        changes = {scenario: change_figures(CHANGE_RUNS / "a"
                                            / change_name(run_id, layout, scenario))
                   for scenario in CHANGES}
        out[layout] = {**record, "clean": clean, "changes": changes}
    return out


# ------------------------------------------------------------------ the comparison

def median(values: list) -> float | None:
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def worst(values: list) -> float | None:
    values = [value for value in values if value is not None]
    return max(values) if values else None


def unit_ms(key: str) -> float:
    lane = key.split("@")[0]
    return change_run.LANES[lane]["epoch_samples"] * 1000 / bench.SAMPLE_RATE


def measured(run: dict) -> bool:
    """A run the comparison may use: scored by the referee's rule, with
    nothing wrong with the run itself (void, a probe too short or faulty, the
    command's exit not the scenario's)."""
    return run["status"] in change_run.SCORED and not run.get("reasons")


def verdicts(changes: dict) -> dict:
    """Passes of the measured runs for each scenario, kind and probe:
    {"scenario/kind/probe": [passes, runs]}."""
    out: dict = {}
    for scenario, kinds in changes.items():
        for kind, runs in kinds.items():
            for run in filter(measured, runs):
                for per in (run.get("labels") or {}).values():
                    for probe, result in per.items():
                        counts = out.setdefault(f"{scenario}/{kind}/{probe}", [0, 0])
                        counts[0] += result.get("pass") is True
                        counts[1] += 1
    return out


def completeness(layout: str, data: dict, repeats: int) -> list[str]:
    """Why a layout's run cannot be compared, or nothing."""
    out = []
    if data["repeats"] != repeats:
        out.append(f"{layout}: not measured with --repeats {repeats} (its run had "
                   f"{data['repeats']})")
    for job in data["jobs"]:
        if job["judge_exit"] and job["exit"] != 0:
            out.append(f"{layout} lab {job['lab']}: `{' '.join(job['command'])}` exited "
                       f"{job['exit']}")
    for name, item in data.get("load", {}).items():
        for _began, _ended, code in item["runs"]:
            if code != 0:
                out.append(f"{layout} lab b's load: {name} exited {code}")
        if not item["runs"]:
            out.append(f"{layout} lab b's load: {name} never ran")
    for other in data.get("other_labs", []):
        out.append(f"{layout}: lab {other} ran on the host during it")
    for lane in CLEAN_LANES:
        for host in CLEAN_HOSTS:
            key = f"{lane}@{host}"
            rows = data["clean"].get(key, [])
            if len(rows) != repeats:
                out.append(f"{layout} {key}: not measured: {len(rows)} of {repeats} clean passes")
            elif any(row["p99_ms"] is None for row in rows):
                out.append(f"{layout} {key}: not measured: a clean pass has no p99")
    for scenario, kinds in data["changes"].items():
        for kind in ("change", "null"):
            runs = kinds.get(kind, [])
            for run in runs:
                if not measured(run):
                    out.append(f"{layout} {scenario} {kind}: {run.get('directory')} was "
                               f"{run['status']}: " + "; ".join(run.get("reasons") or []))
            count = sum(1 for run in runs if measured(run))
            if count != repeats:
                out.append(f"{layout} {scenario} {kind}: not measured: {count} of {repeats} "
                           "scored runs")
    return out


def compare(results: dict, repeats: int) -> dict:
    """The verdict of 6.1 from the figures of each layout."""
    failures: list[str] = []
    incomplete = set()
    for layout in LAYOUTS:
        if layout not in results:
            failures.append(f"{layout}: not measured (lab-parity.py --layouts {layout})")
            incomplete.add(layout)
            continue
        reasons = completeness(layout, results[layout], repeats)
        failures += reasons
        if reasons:
            incomplete.add(layout)
    figures: dict = {}
    for layout, data in results.items():
        for key, rows in data["clean"].items():
            p99s = [row["p99_ms"] for row in rows]
            figures.setdefault(key, {})[layout] = {
                "p99_median_ms": median(p99s),
                "p99_spread_ms": (round(max(p99s) - min(p99s), 3)
                                  if p99s and None not in p99s else None),
                "max_ms": worst([row["max_ms"] for row in rows]),
                "gap_ms": worst([row["gap_ms"] for row in rows]),
                "repeats": len(rows)}
            for row in rows:
                if row["missing"] != 0:
                    failures.append(f"{layout} {key}: {row['missing']} missing in a clean pass")
        for scenario, kinds in data["changes"].items():
            for kind, runs in kinds.items():
                for run in runs:
                    for label, per in (run.get("labels") or {}).items():
                        for probe, result in per.items():
                            if result.get("missing"):
                                failures.append(f"{layout} {scenario} {kind} {label} {probe}: "
                                                f"{result['missing']} missing")
                    if kind == "null" and measured(run) and run["status"] != "pass":
                        failures.append(f"{layout} {scenario}: a null run was {run['status']} "
                                        f"({run.get('directory')})")
    for key, per in figures.items():
        def near(layout: str, reference: str) -> None:
            if layout not in per or reference not in per:
                return
            ours, theirs = per[layout]["p99_median_ms"], per[reference]["p99_median_ms"]
            if ours is None or theirs is None:
                failures.append(f"{key}: no p99 in {layout if ours is None else reference}")
            elif abs(ours - theirs) > P99_BOUND_MS + 1e-9:
                failures.append(f"{key}: median p99 {ours:g} ms in {layout} against {theirs:g} ms "
                                f"in {reference}, more than {P99_BOUND_MS} ms apart")
        near("Q1", "Q0")
        for layout in ("Q2", "Q3"):
            near(layout, "Q1")
            if layout in per and "Q1" in per:
                ours, theirs = per[layout]["gap_ms"], per["Q1"]["gap_ms"]
                if ours is None or theirs is None:
                    failures.append(f"{key}: no steady gap in {layout if ours is None else 'Q1'}")
                elif ours > theirs + unit_ms(key) + 1e-9:
                    failures.append(f"{key}: steady longest gap {ours:g} ms in {layout} against "
                                    f"{theirs:g} ms in Q1, more than one unit more")
    tallies = {layout: verdicts(data["changes"]) for layout, data in results.items()}
    same = [layout for layout in ("Q1", "Q2", "Q3") if layout in tallies]
    for layout in same[1:]:
        for key in sorted(set(tallies[layout]) | set(tallies[same[0]])):
            if tallies[layout].get(key) != tallies[same[0]].get(key):
                failures.append(f"{key}: change-window.py passed {tallies[layout].get(key)} "
                                f"in {layout} against {tallies[same[0]].get(key)} in {same[0]}")
    spread = [key for key, per in figures.items()
              if (per.get("Q1") or {}).get("p99_spread_ms") is not None
              and per["Q1"]["p99_spread_ms"] > P99_BOUND_MS + 1e-9]
    shares = [(run.get("interference") or {}).get("outside_share_pct")
              for kinds in (results.get("Q1") or {}).get("changes", {}).values()
              for runs in kinds.values() for run in runs]
    shares = [share for share in shares if share is not None]
    load_share = {name: item["share"]
                  for name, item in (results.get("Q3") or {}).get("load", {}).items()}
    return {"pass": not failures, "failures": failures, "figures": figures,
            "verdicts": tallies, "q1_spreads": spread, "q3_load_share": load_share,
            "two_labs_for_gated_proofs": not spread and not failures,
            "floor_pct": max(shares) if shares and "Q1" not in incomplete else None}


def report_lines(verdict: dict) -> list[str]:
    lines = ["lab parity (C 6.1): " + ("the layouts agree" if verdict["pass"] else "FAIL")]
    lines += [f"  {item}" for item in verdict["failures"]]
    if verdict["q1_spreads"]:
        lines.append("Q1's own repeats spread by more than "
                     f"{P99_BOUND_MS} ms on {', '.join(verdict['q1_spreads'])}: p99- and "
                     "silence-gated proofs run in one lab on cpus=2-29, the host otherwise "
                     "idle; a second lab only for proofs that count parts and moves")
    elif verdict["two_labs_for_gated_proofs"]:
        lines.append("two labs (a on cpus=2-15, b on cpus=16-29) may run gated proofs at once")
    else:
        lines.append("gated proofs run in one lab until the four layouts are measured and agree")
    lines.append("floor (Q1's largest outside share): "
                 + ("not measured" if verdict["floor_pct"] is None
                    else f"{verdict['floor_pct']:.2f}%"))
    if verdict["q3_load_share"]:
        lines.append("Q3, share of K's time lab b's load ran: " + ", ".join(
            f"{name} {share:.0%}" for name, share in sorted(verdict["q3_load_share"].items())))
    lines.append("host and lane            layout  p99 median  spread  max      steady gap (ms)")
    for key, per in sorted(verdict["figures"].items()):
        for layout, row in sorted(per.items()):
            lines.append(f"  {key:<22} {layout:<6}  {row['p99_median_ms']!s:<10}  "
                         f"{row['p99_spread_ms']!s:<6}  {row['max_ms']!s:<7}  {row['gap_ms']}")
    for layout, tallies in sorted(verdict["verdicts"].items()):
        for key, (passes, runs) in sorted(tallies.items()):
            lines.append(f"  {layout} {key}: {passes} of {runs} pass")
    return lines


def write(verdict: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "parity.json").write_text(json.dumps(verdict, indent=1) + "\n")
    (OUT / "report.txt").write_text("\n".join(report_lines(verdict)) + "\n")
    if verdict["floor_pct"] is not None:
        (OUT / "floor.json").write_text(json.dumps(
            {"outside_share_pct": verdict["floor_pct"], "layout": LAYOUTS["Q1"]["a"],
             "measured_unix": int(time.time())}, indent=1) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--layouts", default=",".join(LAYOUTS),
                        help="the layouts to run (the comparison is always of all four)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the commands; run nothing")
    mode.add_argument("--compare", action="store_true",
                      help="compare what the recorded runs left; run nothing")
    args = parser.parse_args(argv)
    layouts = [item.strip() for item in args.layouts.split(",") if item.strip()]
    if not layouts or any(layout not in LAYOUTS for layout in layouts):
        raise SystemExit(f"--layouts: of {', '.join(LAYOUTS)}")
    if args.repeats < 1:
        raise SystemExit("--repeats is 1 or more")
    run_id = new_run_id()
    steps = plan(layouts, args.repeats, run_id)
    if args.plan:
        print(json.dumps(steps, indent=1))
        return 0
    if not args.compare:
        for layout in layouts:
            if layout in ALONE and other_labs():
                raise SystemExit(f"{layout} runs with nothing else on the host, and "
                                 f"{', '.join(other_labs())} is registered")
            record = run_layout(layout, steps[layout])
            record.update({"run_id": run_id, "repeats": args.repeats,
                           "other_labs": other_labs() if layout in ALONE else []})
            save_layout(layout, record)
    verdict = compare(collect(load_state()), args.repeats)
    write(verdict)
    print("\n".join(report_lines(verdict)))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
