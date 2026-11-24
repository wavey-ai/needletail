#!/usr/bin/env python3
"""The stall hunt's report (D-hunt.md 7.4) and its decision rules (7.5),
from what scripts/linode-stall-hunt.py collected.

    hunt-report.py DIR [--json OUT] [--lengths 8,30]

DIR is a hunt run's directory (target/stall-hunt/<run id>/, or its copy in
the run archive): hunt.json (periods, sessions, nodes with their physical
host, replacements), nodes/<node>/ (watch.ndjson of needletail-nodecheck
watch, stalls.ndjson polled from /api/stalls, touch files and marks, the
replaced instance's records under before-replace/), sessions/<S>/<host>/
(the probes, the sender's lateness journal and source.start).

It prints, in this order:
 1. per node and period (soak rounds, sessions, the time between two of
    them, the whole run): stops of 20 ms and of 100 ms or more by scope and
    cause, F (stopped ms a node-minute, the hunt's measure) from the watch
    and from the service's own journal (they must agree within 10 %),
    starved ms, steal, the share of seconds that were mostly the guest
    kernel's; a node whose service could not pin every tick thread
    (`tick_pinned` below `tick_threads` in its /api/mesh/local) has its
    scope marked untrusted (D-hunt.md 4);
 2. by physical host: the nodes that share one, and their stops (a node's
    stopped ticks merged, all CPUs together) within 50 ms of each other
    against the count expected by chance;
 3. the share of all frozen time on the worst quarter of the instances,
    and each node's rank by soak against its rank by session (a node with
    nothing is in no worst set, and a tie takes the worst rank of the tie:
    names never decide);
 4. per session and listener, every arrival silence of 3 ms or more (two
    unit spacings on the 2.5 ms lane) joined, in this order, to a node that
    was not run (the source host, the contributor), the sender's pacer or
    encode, the link, the contributor's read, its publish, the stall
    events of the nodes on the listener's path and the listener host's
    watch; the count and summed ms per cause and the unexplained rest;
 5. per region and pair of edges seen, the first of the two per part
    (publish to commit: what a listener held on those two gets) and each
    edge's commit gaps of 100 ms or more;
 6. the null of every probe (scripts/compare/null-window.py, the referee's
    arithmetic), on the listener's stamp, the kernel's and publish to
    kernel;
 7. per session, E3's episodes before publish (0.5, 1 and 2 ms over the
    median) with the side of each, and the 2.5 ms lane's split (7.7);
 8. one line per rule R1 to R6, the wave 7 control, and 3.1's acceptance.
It refuses (exit 1, no report) a run whose /api/hops pull fell inside a
session (D-hunt.md 2.5 and 4: the pull first-touches about 100 MB on the
node). Exit 0: a report.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import importlib.util
import itertools
import json
import math
import pathlib
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import stall_records as sr  # noqa: E402

CPUS = {"g6-dedicated-2": 2, "g6-dedicated-4": 4, "g6-dedicated-8": 8}
MESH_ROLES = ("contributor", "distributor", "edge")
SAMPLE_RATE = 48_000
EPOCH_SAMPLES = {"opus-1": 48, "pcm-1": 48, "opus-2.5": 120}
SILENCE_MS = 3.0
COHOST_WITHIN_US = 50_000
CHANCE_P = 0.01
COMMIT_GAP_MS = 100.0
LATE_ON_BOTH_MS = 20.0
EPISODE_ABOVE_US = (500, 1000, 2000)
AGREE_SHARE = 0.10
# Below this a node's F is no stop at all (agreement is not asked of it).
F_NOISE = 1.0
RATIO = 5.0
R6_F = 5.0
R4_MS = 20.0
SPLIT_25_MS = 0.05


def load_null_window():
    spec = importlib.util.spec_from_file_location("null_window", HERE / "null-window.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ the run

class Run:
    """A hunt run's directory, read once."""

    def __init__(self, directory) -> None:
        self.dir = pathlib.Path(directory)
        self.record = json.loads((self.dir / "hunt.json").read_text())
        self.nodes: dict[str, dict] = self.record.get("nodes") or {}
        self.periods: list[dict] = self.record.get("periods") or []
        self._watch: dict[str, list] = {}
        self._journal: dict[str, list] = {}

    def node_dir(self, name: str) -> pathlib.Path:
        return self.dir / "nodes" / name

    def watch(self, name: str) -> list[dict]:
        """The node's watch record, the replaced instance's included."""
        if name not in self._watch:
            items = sr.read_ndjson(self.node_dir(name) / "before-replace" / "watch.ndjson")
            items += sr.read_ndjson(self.node_dir(name) / "watch.ndjson")
            self._watch[name] = sr.dedupe(items)
        return self._watch[name]

    def journal(self, name: str) -> list[dict]:
        """The service's own stall journal, as polled."""
        if name not in self._journal:
            self._journal[name] = sr.read_stalls(self.node_dir(name) / "stalls.ndjson")
        return self._journal[name]

    def role(self, name: str) -> str | None:
        return (self.nodes.get(name) or {}).get("role")

    def mesh(self) -> list[str]:
        return sorted(name for name in self.nodes if self.role(name) in MESH_ROLES)

    def cpus(self, name: str) -> int:
        return CPUS.get((self.nodes.get(name) or {}).get("type")) \
            or len(sr.cpus_of(self.watch(name))) or 1

    def host_uuid(self, name: str) -> str | None:
        found = (self.nodes.get(name) or {}).get("host_uuid")
        for label in ("last", "first"):
            found = found or ((self.record.get("hosts") or {}).get(label) or {}) \
                .get("host_uuid", {}).get(name)
        return found

    def contributor(self) -> str | None:
        names = [name for name in self.nodes if self.role(name) == "contributor"]
        return names[0] if names else None

    def span_us(self) -> tuple[int, int] | None:
        """From the first to the last line of any watch record."""
        times = [sr.at_us(item) for name in self.nodes for item in self.watch(name)]
        times = [value for value in times if value]
        return (min(times), max(times)) if times else None

    def period(self, name: str) -> tuple[int, int] | None:
        for item in self.periods:
            if item["name"] == name:
                return int(item["from_unix"] * 1e6), int(item["to_unix"] * 1e6)
        return None

    def periods_of(self, kind: str) -> list[tuple[str, int, int]]:
        return [(item["name"], int(item["from_unix"] * 1e6), int(item["to_unix"] * 1e6))
                for item in self.periods if item.get("kind") == kind]

    def arms(self) -> dict:
        return self.record.get("arms") or {}

    def unpinned(self) -> dict[str, str]:
        """{node: "pinned of threads"} for the nodes whose service ran a
        tick thread it could not pin, at any poll of /api/mesh/local."""
        out = {}
        for name in self.mesh():
            for item in sr.read_ndjson(self.node_dir(name) / "mesh-local.ndjson"):
                stall = (item.get("body") or {}).get("stall") or {}
                threads, pinned = stall.get("tick_threads"), stall.get("tick_pinned")
                if threads is not None and pinned is not None and int(pinned) < int(threads):
                    out[name] = f"{pinned} of {threads}"
        return out


class Refused(Exception):
    """The run's record breaks a rule of the hunt: no report is made of it."""


def hops_inside_sessions(run: Run) -> list[str]:
    """Every /api/hops pull whose time meets a session's (D-hunt.md 4)."""
    sessions = run.periods_of("session")
    out = []
    for item in run.record.get("sessions") or []:
        pull = item.get("hops_pull") or {}
        if pull.get("from_unix") is None:
            continue
        start = int(pull["from_unix"] * 1e6)
        end = int((pull.get("to_unix") or pull["from_unix"]) * 1e6)
        for name, begin, finish in sessions:
            if start <= finish and end >= begin:
                out.append(f"the /api/hops pull after {item.get('name')} "
                           f"({pull['from_unix']} to {pull.get('to_unix')}) is inside {name}")
    return out


def f_over(events: list[dict], spans: list[tuple[int, int]], cpus: int) -> float | None:
    """F (stopped ms a node-minute) over several periods together."""
    minutes = sum(end - start for start, end in spans) / 60e6
    if minutes <= 0:
        return None
    total = sum(sr.stopped_ms(events, start, end, cpus) for start, end in spans)
    return round(total / minutes, 3)


# ----------------------------------------------------- 1. nodes and periods

def agree(a: float | None, b: float | None) -> bool | None:
    """The journal and the watch agree within 10 % (both under F_NOISE: yes)."""
    if a is None or b is None:
        return None
    if max(a, b) < F_NOISE:
        return True
    return abs(a - b) <= AGREE_SHARE * max(a, b)


def between(periods: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """The time between two consecutive periods: ("after <name>", from, to)."""
    ordered = sorted(periods, key=lambda item: item[1])
    return [(f"after {a[0]}", a[2], b[1]) for a, b in zip(ordered, ordered[1:]) if b[1] > a[2]]


def node_periods(run: Run) -> list[dict]:
    span = run.span_us()
    if span is None:
        return []
    periods = [(item["name"], int(item["from_unix"] * 1e6), int(item["to_unix"] * 1e6))
               for item in run.periods if item.get("kind") in ("soak", "session")]
    periods = sorted(periods + between(periods), key=lambda item: item[1])
    periods.append(("run", span[0], span[1] + 1))
    unpinned = run.unpinned()
    rows = []
    for name in sorted(run.nodes):
        watch, journal, cpus = run.watch(name), run.journal(name), run.cpus(name)
        for label, start, end in periods:
            lines = sr.seconds(watch, start, end)
            f_watch = f_over(watch, [(start, end)], cpus) if watch else None
            f_journal = f_over(journal, [(start, end)], cpus) if journal else None
            rows.append({
                "node": name, "period": label, "stops": sr.stop_counts(watch, start, end),
                "scope_trusted": name not in unpinned, "tick_pinned": unpinned.get(name),
                "f_watch": f_watch, "f_journal": f_journal, "agree": agree(f_watch, f_journal),
                "steal_permille": sr.steal_permille(watch, start, end),
                "kernel_share": round(sum(1 for line in lines if sr.kernel_second(line))
                                      / len(lines), 4) if lines else None})
    return rows


# ------------------------------------------------------- 2. physical hosts

def host_groups(run: Run) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in sorted(run.nodes):
        uuid = run.host_uuid(name)
        if uuid:
            groups.setdefault(uuid, []).append(name)
    return {uuid: names for uuid, names in groups.items() if len(names) > 1}


def poisson_tail(observed: int, expected: float) -> float:
    """P(X >= observed) for X Poisson with mean `expected`."""
    if observed <= 0:
        return 1.0
    if expected <= 0:
        return 0.0
    below = sum(math.exp(-expected) * expected ** k / math.factorial(k) for k in range(observed))
    return max(0.0, 1.0 - below)


def simultaneity(stops: dict[str, list[tuple[int, int]]], pairs: list[tuple[str, str]],
                 seconds: float, within_us: int = COHOST_WITHIN_US) -> list[dict]:
    """Per pair, the stops of the first (each node's stopped ticks merged
    into stops, `stall_records.stops`: a stop of a whole 2-CPU node is one
    stop, not two) that come within `within_us` of a stop of the second,
    against the count two independent records would give by chance:
    n_a n_b (2w + mean length a + mean length b) / T."""
    out = []
    for a, b in pairs:
        mine, theirs = stops.get(a, []), stops.get(b, [])
        observed = sum(1 for start, end in mine
                       if any(max(start, other_start) - min(end, other_end) <= within_us
                              for other_start, other_end in theirs))

        def mean_us(items: list[tuple[int, int]]) -> float:
            return sum(end - start for start, end in items) / len(items) if items else 0.0

        reach_s = (2 * within_us + mean_us(mine) + mean_us(theirs)) / 1e6
        expected = len(mine) * len(theirs) * reach_s / seconds if seconds > 0 else 0.0
        p = poisson_tail(observed, expected)
        out.append({"pair": [a, b], "observed": observed, "expected": round(expected, 4),
                    "p": round(p, 6), "above_chance": observed >= 2 and p < CHANCE_P})
    return out


def cohosting(run: Run) -> dict:
    groups = host_groups(run)
    span = run.span_us()
    seconds = (span[1] - span[0]) / 1e6 if span else 0
    stops = {name: sr.stops(run.watch(name)) for name in run.nodes}
    pairs = [(names[i], names[j]) for names in groups.values()
             for i in range(len(names)) for j in range(i + 1, len(names))]
    found = simultaneity(stops, pairs, seconds)
    return {"groups": groups, "pairs": found,
            "above_chance": any(item["above_chance"] for item in found)}


# ------------------------------------------------- 3. where the time went

def worst_quarter(values: dict[str, float]) -> dict:
    """The quarter or fewer of the instances with the most of a value, and
    their share of all of it. A node with nothing is in no worst set. The
    share is the quarter's largest values', whoever holds them; `worst`
    names the nodes above the quarter's last value, and `tied` the nodes
    of a tie that the quarter's cut runs through (so that no name decides
    who is in it)."""
    total = sum(values.values())
    count = max(1, len(values) // 4)
    top = sorted((value for value in values.values() if value > 0), reverse=True)[:count]
    if not top:
        return {"worst": [], "tied": [], "tied_places": 0, "share": None,
                "total_ms": round(total, 3)}
    cut = top[-1]
    above = sorted((name for name, value in values.items() if value > cut),
                   key=lambda name: (-values[name], name))
    at_cut = sorted(name for name, value in values.items() if value == cut)
    fits = len(above) + len(at_cut) <= count
    return {"worst": above + (at_cut if fits else []), "tied": [] if fits else at_cut,
            "tied_places": 0 if fits else count - len(above),
            "share": round(sum(top) / total, 4), "total_ms": round(total, 3)}


def ranks(values: dict[str, float]) -> dict[str, int]:
    """1 for the worst (largest) value; nodes of equal value all take the
    worst (largest) rank of their tie."""
    return {name: sum(1 for other in values.values() if other >= value)
            for name, value in values.items()}


def node_figures(run: Run) -> dict:
    """Each node's frozen ms over the run, F over the soak rounds and over
    the sessions (all from its watch record)."""
    soaks = [(start, end) for _name, start, end in run.periods_of("soak")]
    sessions = [(start, end) for _name, start, end in run.periods_of("session")]
    out = {}
    for name in sorted(run.nodes):
        watch, cpus = run.watch(name), run.cpus(name)
        out[name] = {"frozen_ms": sr.stopped_ms(watch, cpus=cpus),
                     "soak_f": f_over(watch, soaks, cpus) or 0.0,
                     "session_f": f_over(watch, sessions, cpus) or 0.0}
    return out


# ------------------------------------------------------ 4. silences joined

def read_frames(path) -> list[dict]:
    """A probe's per-unit CSV (ntcmp --frames), audio rows."""
    out = []
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("kind") != "a":
                continue
            out.append(row)
    return out


def silences(rows: list[dict], min_ms: float | None = None) -> list[dict]:
    """Arrival silences of a probe: [{"from_us", "to_us", "ms", "source_us"}]
    (source_us: the source time of the unit that ended the silence). The
    threshold is 3 ms, or two unit spacings when that is more."""
    units = sorted((int(row["complete_unix_us"]), int(row["complete_unix_us"])
                    - int(row["latency_us"])) for row in rows)
    gaps = [b[0] - a[0] for a, b in zip(units, units[1:])]
    if not gaps:
        return []
    spacing = sorted(gaps)[len(gaps) // 2] / 1000
    threshold = min_ms if min_ms is not None else max(SILENCE_MS, 2 * spacing)
    return [{"from_us": a[0], "to_us": b[0], "ms": (b[0] - a[0]) / 1000, "source_us": b[1]}
            for a, b in zip(units, units[1:]) if (b[0] - a[0]) / 1000 >= threshold]


def gap_join(found: list[dict], sources: list[tuple[str, list[dict]]],
             margin_us: int = 5000) -> dict:
    """Each silence joined to the first source with an event between the
    source time of the unit that ended it (less a margin) and its end."""
    by_cause: dict[str, dict] = {}
    for gap in found:
        cause = sr.first_cause(gap["source_us"] - margin_us, gap["to_us"], sources)
        gap["cause"] = cause[0] if cause else "unexplained"
        slot = by_cause.setdefault(gap["cause"], {"n": 0, "ms": 0.0})
        slot["n"] += 1
        slot["ms"] = round(slot["ms"] + gap["ms"], 3)
    return {"silences": len(found), "by_cause": by_cause,
            "unexplained": by_cause.get("unexplained", {"n": 0, "ms": 0.0})}


def probe_edge(csv_path: pathlib.Path) -> str | None:
    """The edge that served a probe: an LL-HLS probe's pathway, or the UDP
    client's one cache."""
    stem = csv_path.name[:-len(".probe.csv")]
    report = csv_path.with_name(f"{stem}.probe.json")
    client = csv_path.with_name(f"{stem}.uc.json")
    try:
        if client.is_file():
            caches = json.loads(client.read_text()).get("caches") or []
            return caches[0] if len(caches) == 1 else None
        if report.is_file():
            detail = json.loads(report.read_text()).get("detail") or {}
            return (detail.get("variant") or {}).get("PATHWAY-ID")
    except (OSError, ValueError):
        return None
    return None


def session_dir(run: Run, session: str) -> pathlib.Path:
    return run.dir / "sessions" / session


def sender_lines(run: Run, session: str) -> list[dict]:
    lines = []
    for path in sorted(session_dir(run, session).glob("*/sender.ndjson")):
        lines += sr.read_ndjson(path)
    return lines


def source_host(run: Run, session: str) -> str | None:
    found = sorted(session_dir(run, session).glob("*/sender.ndjson"))
    return found[0].parent.name if found else None


def path_nodes(run: Run, host: str, edge: str | None) -> list[str]:
    """The nodes on a listener's path: its edge, the distributors of its
    site and of the contributor's, the contributor."""
    site = (run.nodes.get(host) or {}).get("site")
    contributor = run.contributor()
    home = (run.nodes.get(contributor) or {}).get("site") if contributor else None
    names = [edge] if edge in run.nodes else []
    names += sorted(name for name in run.nodes if run.role(name) == "distributor"
                    and run.nodes[name].get("site") in (site, home))
    return names + ([contributor] if contributor else [])


def listener_sources(run: Run, session: str, host: str, edge: str | None) -> list:
    sender = sr.sender_events(sender_lines(run, session))
    contributor = run.contributor()
    journal = run.journal(contributor) if contributor else []
    source = source_host(run, session)
    sources = sr.source_sides(sender, journal, run.watch(source) if source else [],
                              contributor or "contributor", source or "source host")
    for name in path_nodes(run, host, edge):
        if name == contributor:
            continue
        for cause in ("stopped", "starved", "own", "waited"):
            sources.append((f"{name} {cause}", [event for event in run.journal(name)
                                                 if event.get("cause") == cause]))
    sources.append((f"{host} (listener host) stopped", sr.stopped_ticks(run.watch(host), 0)))
    sources.append((f"{host} (listener host) starved",
                    [event for event in sr.ticks(run.watch(host))
                     if sr.tick_cause(event) == "starved"]))
    return sources


def session_probes(run: Run, session: str) -> list[pathlib.Path]:
    return sorted(session_dir(run, session).glob("*/*.probe.csv"))


def silence_table(run: Run) -> dict:
    out = {}
    for name, _start, _end in run.periods_of("session"):
        per = {}
        for path in session_probes(run, name):
            host = path.parent.name
            edge = probe_edge(path)
            joined = gap_join(silences(read_frames(path)),
                              listener_sources(run, name, host, edge))
            per[f"{host}/{path.name}"] = {"edge": edge, **joined}
        out[name] = per
    return out


# ------------------------------------------------- 5. the first of two edges

def edge_commits(probes: list[tuple[str, list[dict]]]) -> tuple[dict, dict]:
    """From LL-HLS probes [(edge, rows)]: {edge: {part: commit_us}} and
    {part: published_us}. A part's commit is the edge's own stamp, the same
    for every probe it served."""
    commits: dict[str, dict[int, int]] = {}
    published: dict[int, int] = {}
    for edge, rows in probes:
        for row in rows:
            if not row.get("edge_committed_unix_us") or not row.get("part"):
                continue
            part = int(row["part"])
            commits.setdefault(edge, {}).setdefault(part, int(row["edge_committed_unix_us"]))
            if row.get("published_unix_us"):
                published[part] = int(row["published_unix_us"])
    return commits, published


def commit_gaps(commits: dict[str, dict[int, int]], min_ms: float = COMMIT_GAP_MS,
                since_us: int | None = None, until_us: int | None = None) -> dict:
    """Each edge's commit gaps of `min_ms` or more between consecutive
    parts: {edge: [(from_us, to_us, ms)]}."""
    out = {}
    for edge, by_part in commits.items():
        parts = sorted(by_part)
        gaps = []
        for a, b in zip(parts, parts[1:]):
            start, end = by_part[a], by_part[b]
            if (end - start) / 1000 < min_ms:
                continue
            if (since_us is not None and start < since_us) or (until_us is not None and start > until_us):
                continue
            gaps.append((start, end, round((end - start) / 1000, 3)))
        out[edge] = gaps
    return out


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def first_of_two(commits: dict[str, dict[int, int]], published: dict[int, int],
                 edges: list[str], since_us: int | None = None,
                 until_us: int | None = None) -> dict:
    """Per part that both edges of a pair committed, the earlier commit's
    distance from publish (ms): what a listener held on those two gets."""
    if len(edges) != 2 or any(edge not in commits for edge in edges):
        return {"parts": 0}
    both = set(commits[edges[0]]) & set(commits[edges[1]])
    values, late = [], 0
    for part in both:
        if part not in published:
            continue
        if (since_us is not None and published[part] < since_us) \
                or (until_us is not None and published[part] > until_us):
            continue
        each = [(commits[edge][part] - published[part]) / 1000 for edge in edges]
        values.append(min(each))
        late += all(value > LATE_ON_BOTH_MS for value in each)
    return {"parts": len(values), "p50_ms": pct(values, 50), "p99_ms": pct(values, 99),
            "max_ms": max(values) if values else None, "late_on_both": late}


def hls_probes(paths: list[pathlib.Path]) -> list[tuple[str, list[dict]]]:
    out = []
    for path in paths:
        edge = probe_edge(path)
        if edge and path.with_name(path.name[:-len(".probe.csv")] + ".probe.json").is_file():
            out.append((edge, read_frames(path)))
    return out


def first_of_pairs(commits: dict[str, dict[int, int]], published: dict[int, int],
                   since_us: int | None, until_us: int | None) -> dict:
    """Per region (an edge's name up to its first dash), the first of two
    for every pair of the region's edges seen: {region: {"a+b": figure}};
    a region with one edge seen has no pair."""
    regions: dict[str, list[str]] = {}
    for edge in commits:
        regions.setdefault(edge.split("-")[0], []).append(edge)
    return {region: {f"{a}+{b}": first_of_two(commits, published, [a, b], since_us, until_us)
                     for a, b in itertools.combinations(sorted(edges), 2)}
            for region, edges in sorted(regions.items())}


def edges_table(run: Run) -> dict:
    out = {}
    for name, start, end in run.periods_of("session"):
        commits, published = edge_commits(hls_probes(session_probes(run, name)))
        out[name] = {"gaps": commit_gaps(commits, since_us=start, until_us=end),
                     "first_of_two": first_of_pairs(commits, published, start, end)}
    return out


# ----------------------------------------------------------------- 6. null

def null_table(run: Run, lengths, null_window=None) -> dict:
    null_window = null_window or load_null_window()
    cw = null_window.referee()
    out = {}
    for name, _start, _end in run.periods_of("session"):
        frames, arrivals = [], []
        for path in session_probes(run, name):
            stem = path.name[:-len(".probe.csv")]
            paired = path.with_name(f"{stem}.arrivals.csv")
            frames.append(str(path))
            arrivals.append(str(paired) if paired.is_file() else None)
        probes = []
        for frame, paired in zip(frames, arrivals):
            probes += null_window.analyse(cw, [frame], [paired] if paired else [], lengths,
                                          5.0, 97.5, 120)["probes"]
        out[name] = probes
    return out


# -------------------------------------------------------- 7. the source side

def ideal_clock(start_unix_s: int, epoch_samples: int):
    """A part's ideal instant (us): its last sample spoken, the session's
    loop START plus (part + 1) epochs (seq = epoch)."""
    epoch_us = epoch_samples * 1e6 / SAMPLE_RATE
    return lambda part: int(start_unix_s * 1_000_000 + round((part + 1) * epoch_us))


def published_stamps(paths: list[pathlib.Path]) -> dict[int, int]:
    """part -> published (us) from the first probe that has the column."""
    for path in paths:
        stem = path.name[:-len(".probe.csv")]
        arrivals = path.with_name(f"{stem}.arrivals.csv")
        found: dict[int, int] = {}
        if arrivals.is_file():
            with open(arrivals, newline="") as handle:
                for row in csv.DictReader(handle):
                    if row.get("published_unix_us") and int(row["published_unix_us"]):
                        found[int(row["part"])] = int(row["published_unix_us"])
        else:
            for row in read_frames(path):
                if row.get("published_unix_us") and row.get("part"):
                    found[int(row["part"])] = int(row["published_unix_us"])
        if found:
            return found
    return {}


def source_table(run: Run) -> dict:
    out = {}
    sessions = {item["name"]: item for item in run.record.get("sessions") or []}
    contributor = run.contributor()
    for name, _start, _end in run.periods_of("session"):
        session = sessions.get(name) or {}
        host = source_host(run, name)
        start_file = session_dir(run, name) / (host or "") / "source.start"
        if not host or not start_file.is_file():
            out[name] = {"error": "no source.start"}
            continue
        samples = EPOCH_SAMPLES.get(session.get("config"), 48)
        ideal = ideal_clock(int(start_file.read_text().split()[0]), samples)
        published = published_stamps(session_probes(run, name))
        lateness = {part: stamp - ideal(part) for part, stamp in published.items()}
        sides = sr.source_sides(sr.sender_events(sender_lines(run, name)),
                                run.journal(contributor) if contributor else [],
                                run.watch(host), contributor or "contributor", host)
        entry = {"config": session.get("config"), "parts": len(lateness)}
        for above in EPISODE_ABOVE_US:
            found = sr.episodes(lateness, above)
            entry[f"over_{above}us"] = sr.attribute(found, ideal, sides)
        entry["split_25"] = lane_split(run, name)
        out[name] = entry
    return out


def find_values(body, key: str) -> list[float]:
    """Every number under `key` anywhere in a JSON body."""
    found = []
    if isinstance(body, dict):
        for name, value in body.items():
            if name == key and isinstance(value, (int, float)):
                found.append(float(value))
            else:
                found += find_values(value, key)
    elif isinstance(body, list):
        for value in body:
            found += find_values(value, key)
    return found


def lane_split(run: Run, session: str) -> dict:
    """One clock at a time (7.7): the sender's encode and lateness at its
    first datagram (its sec lines) and the contributor's receive to
    publish (its pacing record), medians over the session."""
    period = run.period(session)
    lines = [line for line in sender_lines(run, session) if line.get("kind") == "sec"]
    out = {}
    for key in ("encode_p50_us", "late_p50_us"):
        values = [float(line[key]) for line in lines if line.get(key) is not None]
        out[key] = statistics.median(values) if values else None
    contributor = run.contributor()
    pacing = sr.read_ndjson(run.node_dir(contributor) / "pacing.ndjson") if contributor else []
    for key in ("rx_to_publish_p50_us", "rx_to_publish_p99_us"):
        values = []
        for item in pacing:
            if period and not (period[0] <= item.get("unix", 0) * 1e6 <= period[1]):
                continue
            values += find_values(item.get("body"), key)
        out[key] = statistics.median(values) if values else None
    return out


# ---------------------------------------------------------------- 8. rules

def rule_r1(figures: dict, cohost: dict, replaced: list[dict], replaced_f: dict) -> dict:
    """The host (H13)."""
    frozen = {name: item["frozen_ms"] for name, item in figures.items()}
    quarter = worst_quarter(frozen)
    if not quarter["share"]:
        return {"fires": False, "line": "R1 (host): no frozen time on any node"}
    soak = worst_quarter({name: item["soak_f"] for name, item in figures.items()})
    # A soak that found no stop cannot name the bad instances; of a tie at
    # the quarter's cut, every node must be in the worst half.
    soak_worst = soak["worst"] + soak["tied"]
    session_rank = ranks({name: item["session_f"] for name, item in figures.items()})
    half = math.ceil(len(figures) / 2)
    consistent = bool(soak_worst) and all(session_rank[name] <= half for name in soak_worst)
    concentrated = quarter["share"] >= 0.5
    fires = concentrated and consistent
    notes = []
    if cohost.get("above_chance"):
        notes.append("co-hosted nodes stop together above chance: the host rule of 2.10 is "
                     "the reason and is kept as written")
    for item in replaced:
        name = item.get("name")
        before, after = item.get("host_uuid_before"), item.get("host_uuid_after")
        old, new = replaced_f.get(name, (None, None))
        if before and after and before == after and old and new is not None and new >= 0.5 * old:
            notes.append(f"{name}: the new instance is on the same host ({after}) and as bad "
                         f"(F {new} against {old}): a platform matter for the owner")
        elif name:
            notes.append(f"{name}: replaced, host {'unchanged' if before == after else 'changed'}, "
                         f"F before {old}, after {new}")
    line = (f"R1 (host): {100 * quarter['share']:.0f}% of the frozen time on "
            f"{quarter_names(quarter)}; the worst by soak "
            f"{'are' if consistent else 'are not'} in the worst half by session: "
            + ("FIRES: the boot check (node_check_seconds, limits from this record) with "
               "D12's replace-once" if fires else "does not fire"))
    return {"fires": fires, "share": quarter["share"], "worst": quarter["worst"],
            "tied": quarter["tied"],
            "soak_worst": soak_worst, "consistent": consistent, "notes": notes,
            "line": line + ("; " + "; ".join(notes) if notes else "")}


def quarter_names(quarter: dict) -> str:
    named = ", ".join(quarter["worst"])
    if not quarter["tied"]:
        return named
    tied = f"{quarter['tied_places']} of {', '.join(quarter['tied'])} (tied at the cut)"
    return f"{named} and {tied}" if named else tied


def ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if b == 0:
        return math.inf if a > 0 else None
    return a / b


def touch_marks(run: Run, name: str) -> dict[str, int]:
    path = run.node_dir(name) / "touch-marks.txt"
    marks = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                marks[parts[0]] = int(float(parts[1]) * 1e6)
    return marks


def rule_r2(run: Run) -> dict:
    """First touch (H10)."""
    arms = run.arms()
    warmed = set(arms.get("warmed", []))
    touch = sorted(name for name in run.nodes if touch_marks(run, name))
    soaks = [(start, end) for _name, start, end in run.periods_of("soak")]
    ratios = {}
    for name in touch:
        marks = touch_marks(run, name)
        if not {"fresh", "thp-never", "again", "end"} <= set(marks):
            continue
        watch, cpus = run.watch(name), run.cpus(name)
        fresh = f_over(watch, [(marks["fresh"], marks["thp-never"])], cpus)
        rest = f_over(watch, [(marks["again"], marks["end"])] + soaks, cpus)
        ratios[name] = ratio(fresh, rest)
    cold = [name for name in ratios if name not in warmed]
    warm = [name for name in ratios if name in warmed]
    first = bool(cold) and all((ratios[name] or 0) >= RATIO for name in cold) \
        and all((ratios[name] or 0) < RATIO for name in warm)
    s1 = run.period("S1")
    mesh = run.mesh()
    control = set(arms.get("control", {}))
    f_s1 = {name: f_over(run.watch(name), [s1], run.cpus(name)) or 0.0 for name in mesh} if s1 else {}
    warmed_f = [f_s1[name] for name in mesh if name in warmed and name in f_s1]
    cold_f = [f_s1[name] for name in mesh if name not in warmed and name not in control
              and name in f_s1]
    cold_median = statistics.median(cold_f) if cold_f else None
    second = bool(warmed_f) and bool(cold_median) and \
        statistics.median(warmed_f) <= cold_median / RATIO
    if first and second:
        verdict = "FIRES: node_warm defaults true and is part of readiness"
    elif first:
        verdict = ("first touch on bare nodes but not in the mesh: the host takes pages back or "
                   "the trigger is load; no warm pass")
    else:
        verdict = "does not fire"
    shown = ", ".join(f"{name} {'-' if value is None else 'inf' if value == math.inf else f'{value:.1f}'}x"
                      for name, value in sorted(ratios.items()))
    return {"fires": first and second, "touch_ratios": ratios, "first": first,
            "second": second, "warmed_s1_f": warmed_f, "cold_s1_median_f": cold_median,
            "line": f"R2 (first touch): fresh against re-touch and soak {shown or 'no record'}; "
                    f"warmed mesh F in S1 {warmed_f} against cold median {cold_median}: {verdict}"}


def rule_r3(run: Run) -> dict:
    """Huge pages."""
    out = {}
    for name in sorted(run.nodes):
        marks = touch_marks(run, name)
        if not {"fresh", "thp-never", "again"} <= set(marks):
            continue
        slow = [line for line in sr.read_ndjson(run.node_dir(name) / "touch-fresh.ndjson")
                + sr.read_ndjson(run.node_dir(name) / "touch-thp-never.ndjson")
                if line.get("kind") == "touch" and int(line.get("dur_us") or 0) > 1000]
        stops = sr.stopped_ticks(run.watch(name), 1.0)

        def rate(start: int, end: int) -> float:
            count = sum(1 for line in slow if start <= int(line["t_us"]) < end)
            count += sum(1 for event in stops if start <= int(event["t_us"]) < end)
            return count / ((end - start) / 60e6)

        out[name] = ratio(rate(marks["fresh"], marks["thp-never"]),
                          rate(marks["thp-never"], marks["again"]))
    fires = any((value or 0) >= RATIO for value in out.values())
    return {"fires": fires, "ratios": out,
            "line": "R3 (huge pages): " + (", ".join(
                f"{name} always/never {'-' if value is None else 'inf' if value == math.inf else f'{value:.1f}'}x"
                for name, value in out.items()) or "no touch record") + ": "
            + ("FIRES: D11 (--thp off for the services) and no warm pass" if fires
               else "does not fire")}


def rule_r4(run: Run) -> dict:
    """Our own threads (H9)."""
    found = [{"node": name, **event} for name in run.mesh() for event in run.journal(name)
             if event.get("kind") == "class_pass" and event.get("cause") == "own"
             and int(event.get("dur_us") or 0) >= R4_MS * 1000]
    return {"fires": bool(found), "events": found,
            "line": "R4 (our own threads): " + (
                "FIRES: " + "; ".join(f"{item['node']} {item['thread']} {item['dur_us']} us at "
                                      f"{item['t_us']}" for item in found[:10])
                + " (a fault of av-mesh, fixed there)" if found else "no class pass of 20 ms "
                "or more of our own")}


def rule_r5(run: Run) -> dict:
    """The guest kernel."""
    found = []
    for name in sorted(run.nodes):
        watch = run.watch(name)
        by_cpu: dict[int, list[dict]] = {}
        for line in sorted(sr.seconds(watch), key=lambda item: int(item["t_us"])):
            by_cpu.setdefault(int(line.get("cpu", -1)), []).append(line)
        ends = {cpu: [int(line["t_us"]) for line in lines] for cpu, lines in by_cpu.items()}

        def covering(cpu: int, at_us: int) -> dict | None:
            """The sec line of `cpu` whose second holds `at_us`: a line is
            written at the end of the time it covers, [t_us - span_us, t_us]."""
            lines = by_cpu.get(cpu) or []
            index = bisect.bisect_left(ends.get(cpu) or [], at_us)
            if index < len(lines) and int(lines[index]["t_us"]) \
                    - int(lines[index].get("span_us") or 1_000_000) <= at_us:
                return lines[index]
            return None

        for event in sr.ticks(watch):
            if sr.tick_cause(event) != "starved":
                continue
            line = covering(int(event.get("cpu", -1)), int(event["t_us"]))
            vm = covering(0, int(event["t_us"])) or {}
            if line and sr.kernel_second(line) and (int(vm.get("compact_stall") or 0)
                                                    or int(vm.get("allocstall") or 0)):
                found.append({"node": name, "t_us": event["t_us"], "dur_us": event["dur_us"],
                              "compact_stall": vm.get("compact_stall"),
                              "allocstall": vm.get("allocstall")})
    return {"fires": bool(found), "events": found,
            "line": "R5 (guest kernel): " + (f"{len(found)} starved ticks in kernel seconds with "
                                             "compaction or reclaim (reported; no product change "
                                             "unless R3 fires)" if found else "none")}


def rule_r6(run: Run) -> dict:
    """Nothing."""
    first, last = run.period("S1"), run.period("S4")
    if not first or not last:
        return {"fires": False, "line": "R6 (nothing): S1 to S4 not in the record"}
    span = (first[0], last[1])
    figures = {name: f_over(run.watch(name), [span], run.cpus(name)) or 0.0 for name in run.nodes}
    worst = max(figures.values(), default=0.0)
    fires = bool(figures) and worst < R6_F
    return {"fires": fires, "max_f": worst,
            "line": f"R6 (nothing): the largest F from S1 to S4 is {worst:.2f} ms a node-minute: "
                    + ("FIRES: one repeat on fresh instances, then stop" if fires
                       else "does not fire")}


def control_line(run: Run) -> str:
    """lon-edge-2 (the wave 7 control) against lon-edge-1: fault counts of
    the class threads' events only (one edge against one decides nothing
    about stops)."""
    parts = []
    for name in ("lon-edge-1", "lon-edge-2"):
        events = [event for event in run.journal(name) if "minflt" in event]
        parts.append(f"{name} minflt {sum(int(e.get('minflt') or 0) for e in events)} majflt "
                     f"{sum(int(e.get('majflt') or 0) for e in events)} over {len(events)} events")
    return "control (wave 9 defaults off on lon-edge-2): " + "; ".join(parts)


def replaced_figures(run: Run) -> dict:
    """F of each replaced node's old instance (its watch to the replace)
    and of the new one (after it)."""
    out = {}
    for item in run.record.get("replaced") or []:
        name, at = item.get("name"), int((item.get("unix") or 0) * 1e6)
        old = sr.read_stalls(run.node_dir(name) / "before-replace" / "watch.ndjson")
        new = sr.read_stalls(run.node_dir(name) / "watch.ndjson")
        cpus = run.cpus(name)

        def f_of(events: list[dict], start: int | None, end: int | None) -> float | None:
            times = [sr.at_us(item) for item in events if sr.at_us(item)]
            if not times:
                return None
            start, end = start or min(times), end or max(times) + 1
            return f_over(events, [(start, end)], cpus)
        out[name] = (f_of(old, None, at), f_of(new, at, None))
    return out


def acceptance(null: dict, source: dict) -> list[str]:
    """3.1, per component."""
    lines = []
    for session, probes in null.items():
        for probe in probes:
            verdict = probe.get("acceptance") or {}
            if verdict.get("judged"):
                lines.append(f"3.1 steady state {session} {pathlib.Path(probe['frames']).name} "
                             f"[{probe['basis']}]: " + ("met" if verdict["met"] else
                                                       "NOT met: " + "; ".join(verdict["failures"])))
    for session, entry in source.items():
        if "error" in entry:
            continue
        over_1 = len(entry["over_1000us"]["episodes"])
        unattributed = entry["over_500us"]["unattributed"]
        lines.append(f"3.1 source component {session}: {over_1} episodes more than 1 ms over the "
                     f"median, {unattributed} over 0.5 ms unattributed: "
                     + ("met" if not over_1 and not unattributed else "NOT met"))
    return lines


# ---------------------------------------------------------------- the report

def build(run: Run, lengths=(8, 30), null_window=None) -> dict:
    inside = hops_inside_sessions(run)
    if inside:
        raise Refused("; ".join(inside))
    figures = node_figures(run)
    cohost = cohosting(run)
    null = null_table(run, lengths, null_window)
    source = source_table(run)
    replaced = run.record.get("replaced") or []
    rules = {"R1": rule_r1(figures, cohost, replaced, replaced_figures(run)),
             "R2": rule_r2(run), "R3": rule_r3(run), "R4": rule_r4(run),
             "R5": rule_r5(run), "R6": rule_r6(run)}
    return {"schema": "needletail.hunt-report.v1", "run_id": run.record.get("run_id"),
            "periods": node_periods(run), "unpinned": run.unpinned(), "hosts": cohost,
            "concentration": {**worst_quarter({name: item["frozen_ms"]
                                               for name, item in figures.items()}),
                              "figures": figures,
                              "soak_rank": ranks({n: i["soak_f"] for n, i in figures.items()}),
                              "session_rank": ranks({n: i["session_f"]
                                                     for n, i in figures.items()})},
            "silences": silence_table(run), "edges": edges_table(run), "null": null,
            "source": source, "rules": rules, "control": control_line(run),
            "acceptance": acceptance(null, source)}


def fmt(value, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def format_report(report: dict) -> str:
    lines = [f"stall hunt {report.get('run_id')}", "", "1. nodes and periods "
             "(F: stopped ms a node-minute, watch / journal)"]
    for row in report["periods"]:
        stops = row["stops"]["stopped"]
        lines.append(
            f"  {row['node']:<12} {row['period']:<8} stops>=20ms all {stops['over_20ms']['all']} "
            f"one {stops['over_20ms']['one']}, >=100ms all {stops['over_100ms']['all']} one "
            f"{stops['over_100ms']['one']}; starved {row['stops']['starved']['over_20ms']} "
            f"({row['stops']['starved_ms']} ms); F {fmt(row['f_watch'])} / {fmt(row['f_journal'])}"
            + ("" if row["agree"] is not False else " DISAGREE")
            + f"; steal {fmt(row['steal_permille'], 1)} permille; kernel seconds "
            f"{fmt(row['kernel_share'], 3)}"
            + ("" if row["scope_trusted"] else
               f"; scope untrusted (tick threads pinned {row['tick_pinned']})"))
    lines += ["", "2. physical hosts"]
    for uuid, names in sorted(report["hosts"]["groups"].items()):
        lines.append(f"  {uuid}: {', '.join(names)}")
    for item in report["hosts"]["pairs"]:
        lines.append(f"  {' and '.join(item['pair'])}: {item['observed']} stops within 50 ms, "
                     f"{item['expected']} by chance (p {item['p']})"
                     + (" ABOVE CHANCE" if item["above_chance"] else ""))
    if not report["hosts"]["groups"]:
        lines.append("  no two nodes share a host")
    concentration = report["concentration"]
    lines += ["", f"3. {fmt(100 * concentration['share'] if concentration['share'] else None, 0)}% "
              f"of {concentration['total_ms']} frozen ms on {quarter_names(concentration)}"]
    for name in sorted(concentration["figures"]):
        lines.append(f"  {name:<12} soak rank {concentration['soak_rank'][name]:>2}, session rank "
                     f"{concentration['session_rank'][name]:>2}")
    lines += ["", "4. silences at the listeners, joined"]
    for session, probes in report["silences"].items():
        for probe, item in probes.items():
            causes = ", ".join(f"{cause} {slot['n']} ({slot['ms']:.1f} ms)"
                               for cause, slot in sorted(item["by_cause"].items()))
            lines.append(f"  {session} {probe} ({item['edge']}): {item['silences']} silences: "
                         f"{causes or 'none'}")
    lines += ["", "5. the first of two edges, and commit gaps of 100 ms or more"]
    for session, item in report["edges"].items():
        for region, pairs in item["first_of_two"].items():
            if not pairs:
                lines.append(f"  {session} {region}: one edge seen, no pair")
            for pair, figure in pairs.items():
                lines.append(f"  {session} {region} {pair}: {figure['parts']} parts, publish to "
                             f"first commit p50 {fmt(figure.get('p50_ms'))} p99 "
                             f"{fmt(figure.get('p99_ms'))} max {fmt(figure.get('max_ms'))} ms, "
                             f"late on both {figure.get('late_on_both')}")
        for edge, gaps in sorted(item["gaps"].items()):
            if gaps:
                lines.append(f"  {session} {edge}: " + ", ".join(
                    f"{start / 1e6:.3f} -> {end / 1e6:.3f} ({ms:.0f})" for start, end, ms in gaps))
    lines += ["", "6. the null of every probe"]
    null_window = load_null_window()
    for session, probes in report["null"].items():
        text = null_window.format_result({"probes": probes})
        lines += [f"  {session}: {line}" for line in text.splitlines()]
    lines += ["", "7. the source side (published minus ideal; episodes over the median)"]
    for session, entry in report["source"].items():
        if "error" in entry:
            lines.append(f"  {session}: {entry['error']}")
            continue
        for above in EPISODE_ABOVE_US:
            item = entry[f"over_{above}us"]
            sides = ", ".join(f"{side} {slot['n']}" for side, slot in sorted(item["by_side"].items()))
            lines.append(f"  {session} ({entry['config']}) over {above / 1000:g} ms: "
                         f"{len(item['episodes'])} episodes: {sides or 'none'}")
        split = entry["split_25"]
        lines.append(f"  {session} one clock at a time: sender encode p50 "
                     f"{fmt(split['encode_p50_us'], 0)} us, late p50 {fmt(split['late_p50_us'], 0)} us; "
                     f"contributor receive to publish p50 {fmt(split['rx_to_publish_p50_us'], 0)} "
                     f"p99 {fmt(split['rx_to_publish_p99_us'], 0)} us")
    lines.append("  " + split_verdict(report["source"]))
    lines += ["", "8. rules (fixed before the run)"]
    lines += [f"  {rule['line']}" for rule in report["rules"].values()]
    lines.append(f"  {report['control']}")
    lines += [f"  {line}" for line in report["acceptance"]]
    return "\n".join(lines)


def split_verdict(source: dict) -> str:
    """7.7: a contributor change is considered only if receive to publish
    of the 2.5 ms epochs is 0.05 ms or more above the 1 ms lanes'."""
    def median_of(configs) -> float | None:
        values = [entry["split_25"]["rx_to_publish_p50_us"] for entry in source.values()
                  if entry.get("config") in configs
                  and entry.get("split_25", {}).get("rx_to_publish_p50_us") is not None]
        return statistics.median(values) if values else None
    long_lane, short_lanes = median_of(("opus-2.5",)), median_of(("opus-1", "pcm-1"))
    if long_lane is None or short_lanes is None:
        return "2.5 ms split: no pacing record of both lanes"
    more = (long_lane - short_lanes) / 1000
    return (f"2.5 ms split: receive to publish {more:+.3f} ms against the 1 ms lanes: "
            + ("a contributor change is considered" if more >= SPLIT_25_MS
               else "no contributor change"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dir")
    parser.add_argument("--json", default=None)
    parser.add_argument("--lengths", default="8,30", help="the null's window lengths, seconds")
    args = parser.parse_args()
    if not (pathlib.Path(args.dir) / "hunt.json").is_file():
        sys.exit(f"{args.dir}/hunt.json is missing: not a hunt run's directory")
    try:
        report = build(Run(args.dir), [int(item) for item in args.lengths.split(",") if item])
    except Refused as refused:
        sys.exit(f"no report of {args.dir}: {refused}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(format_report(report))


if __name__ == "__main__":
    main()
