#!/usr/bin/env python3
"""The stall hunt's report and its decision rules (D-hunt.md 7, as revised
after the wave 9 run), from what scripts/linode-stall-hunt.py collected.

    hunt-report.py DIR [--json OUT] [--null [--jobs N] [--lengths 8,30]]

DIR is a hunt run's directory (target/stall-hunt/<run id>/, or its copy in
the run archive): hunt.json (periods, sessions with their roles, nodes with
their addresses and physical hosts, the arms, the replacement),
nodes/<node>/ (stalls.ndjson of the service's /api/stalls, watch.ndjson of
needletail-nodecheck watch, touch marks and vmstat snapshots, system.txt;
sampler.ndjson of scripts/hunt-sampler.sh in a run of the local lab; the
replaced instance's records under before-replace/), samplers/ (the
referee's samplers, compare-run.py collect_samplers: NODE.heartbeat.T.log,
NODE.sys.T.log, NODE.meshlocal.T.log, NODE.nic.T.txt), sessions/<S>/<host>/
(the probes, the crowd's intervals.jsonl, rounds.txt, the sender's lateness
journal and source.start; the 4K probes' v.json of each rung of the
GSO/GRO matrix).

An edge's stop is its own: a run of parts whose commit minus publish
(from its LL-HLS probe) stood 100 ms or more above its p50, as the hunt's
operator read them (hunt-lab.md (a)); a stop that every other edge measured
in the session had at the same time (the source's: input every edge waited
for) is listed as shared, never as an edge's stop. Each stop names the
physical host it happened on (a replaced instance keeps its old host for
its old stops).

A stop is classified from the referee's samplers when the run has them
(stall_records.classify_heartbeat): "kernel" when a soft lockup of the
node's kernel log overlaps it (with what the kernel was doing: e.g. zeroing
a fresh huge page on first touch); "not run" when a vCPU's heartbeat woke
late by about the stop's length (every vCPU: the whole guest); "blocked in
our code" when the heartbeats were on time and a counter of /api/mesh/local,
a long class pass or a stack dumped just after names the wait; "waited for
input" when nothing does. A thread's system time is not read: a vCPU that
is not run charges its pause to the thread current on it. Without the
referee's samplers, from the hunt's own sampler (stall_records.classify).

The large files are read once each, streamed: the meshlocal sampler (a
node's whole /api/mesh/local a second) is indexed by time and only the
lines a stop needs are parsed; the sys sampler keeps the class threads'
lines; a session's probes are read once for the edges and the silences.
The null of every probe (section 5) runs the referee's scorer over every
window and takes minutes on a large run: it is computed with --null.

It prints, in this order:
 1. per node and period (sessions, the time between two of them, the whole
    run): stops of 20 ms and of 100 ms or more by scope and cause, F
    (stopped ms a node-minute) from the watch and from the service's
    journal (they must agree within 10 %), starved ms, steal, the share of
    seconds that were mostly the guest kernel's; a node whose service could
    not pin every tick thread (its sampler's tick_pinned) has its scope
    marked untrusted, and its journal's ticks count only on the class
    thread's own CPU;
 2. by physical host: the nodes that share one, and their stops within
    50 ms of each other against the count expected by chance;
 3. per session and edge, every commit stop of 100 ms or more (from the
    LL-HLS probe at that edge, in time order), each classified from the
    edge's sampler, journal and watch (stall_records.classify): not run
    (where), blocked in our code (which wait), waited for input, or
    undetermined; the UDP clients that reached each edge; and per region
    and pair of edges the first of the two per part and the stops of both
    twins at once;
 4. per session and listener, every arrival silence of 3 ms or more (two
    unit spacings on the 2.5 ms lane) joined to its first cause;
 5. the null of every probe (scripts/compare/null-window.py);
 6. per session, the source side's episodes and the 2.5 ms lane's split;
 7. the decision rules, fixed before the run, one line each, and 3.1.
It refuses (exit 1, no report) a run whose /api/hops pull fell inside a
session (a pull first-touches 50 to 100 MB on the node). Exit 0: a report.

The rules:
 H1 the fix: per edge, does a stop of 100 ms or more remain in the first
    session (on the fresh deploy)? An edge without a probe of its commits,
    or that no UDP client reached, is "not measured";
 H2 the class of every stop of 100 ms or more in the run: not run (the
    hypervisor: the instances and the hosts the stops happened on
    compared, and the replaced instance against its predecessor), the
    kernel (its soft lockups, with where), blocked in our
    code (the named waits: a fault of av-mesh, fixed there), waited for
    input (upstream or the kernel's delivery: the London hop journals);
 H3 the arena warm-up: in the first sessions of a process (the first
    session and the repeat), stop ms an edge without it against with it;
 H4 first touch: in the first session, stop ms an edge of the untouched
    edges against the touched (a touched edge whose touch did not hold and
    end cleanly, or made the kernel swap or evict, is left out; free page
    reporting is stated);
 H5 twins: the stops of two edges of a region at once (overlapping or
    within 50 ms), and the parts late on both;
 H6 PCM first: after the restart, the PCM session's stops against the
    Opus session's after it;
 H7 churn: the churn session's stops within 5 s of a round's joins
    against the rest;
 H8 GSO and GRO (the wave 9 4K run): per combination of the senders' GSO
    and the receivers' GRO (the defaults first), the mesh nodes' stops of
    100 ms or more by their heartbeats (the whole guest when every vCPU's
    heartbeat was late at once; in the kernel when a soft lockup of its
    kernel log overlaps), the sys sampler's gaps, by region, in the first audio
    session and at each 4K rung, and whether each view host got the 4K
    whole: which of GSO and GRO makes the stops, or neither.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import itertools
import json
import math
import operator
import os
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
# An edge's stop: a commit gap of this length or more (wave 9's question).
STOP_MS = 100.0
SHORT_STOP_MS = 20.0
LATE_ON_BOTH_MS = 20.0
# Twins stop together when their stops overlap or come this close.
TWIN_WITHIN_US = 50_000
# An edge's episode is the same as another edge's (a stop of their common
# input) when their parts' publish times overlap or come this close.
SHARED_WITHIN_US = 50_000
# Another edge's episode makes one shared when it is at least this share of
# it (and no more than STOP_MS shorter).
SHARED_SHARE = 0.5
# Late parts this few apart are one episode (the hunt operator's reading).
EPISODE_JOIN_PARTS = 5
EPISODE_ABOVE_US = (500, 1000, 2000)
AGREE_SHARE = 0.10
# Below this a node's F is no stop at all (agreement is not asked of it).
F_NOISE = 1.0
# An arm "matters" when the other arm's stop ms an edge are this many times
# its own and at least STOP_MS.
RATIO = 5.0
# A churn stop "at a join" begins within this long after a round starts.
JOIN_WINDOW_S = 5.0
# The first session of a process: the fresh deploy's, the repeat's (a run
# before the 4K run), and each GSO/GRO combination's after its push.
FIRST_OF_PROCESS = ("first", "repeat-first", "repeat-second", "matrix-first")
# A gap of the referee's sys sampler (one line a second) this long or more
# is a stop of the whole guest's user space (the 4K run's finding).
SAMPLER_GAP_MS = 2000.0
# The NIC offloads that the GSO/GRO question is about (ethtool -k).
NIC_OFFLOADS = ("generic-segmentation-offload", "generic-receive-offload",
                "rx-gro-list", "rx-udp-gro-forwarding", "tx-udp-segmentation")
SPLIT_25_MS = 0.05
# A period's watch and journal lines are read with this margin on either
# side (an event is dated by its end; its length reaches back from there).
PERIOD_MARGIN_US = 120_000_000
# The meshlocal lines read for a figure that holds for a process's life.
MESHLOCAL_SAMPLE_S = 60.0


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
        self._sampler: dict[str, list] = {}
        self._referee: dict[tuple[str, str], object] = {}
        self._unpinned: dict[str, str] | None = None
        self._frames: dict[pathlib.Path, list[dict]] = {}
        self._frames_session: pathlib.Path | None = None
        self._session_cache: dict[tuple, object] = {}

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
        """The service's own stall journal, as polled, the replaced
        instance's included."""
        if name not in self._journal:
            items = sr.read_ndjson(self.node_dir(name) / "before-replace" / "stalls.ndjson")
            items += sr.read_ndjson(self.node_dir(name) / "stalls.ndjson")
            self._journal[name] = sr.dedupe(items)
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

    def host_at(self, name: str, at_us: int | None) -> str | None:
        """The physical host a node's instance had at `at_us`: a replaced
        instance keeps its own host for what happened before the replace
        (hunt.json `replaced`: unix, host_uuid_before), the replacement's
        after it."""
        for item in sorted((item for item in self.record.get("replaced") or []
                            if item.get("name") == name),
                           key=lambda item: item.get("unix") or 0):
            if at_us is not None and item.get("unix") and at_us < item["unix"] * 1e6:
                return item.get("host_uuid_before") or self.host_uuid(name)
        return self.host_uuid(name)

    def frames(self, path) -> list[dict]:
        """A probe's audio rows (read_frames), kept for the session it
        belongs to only (the edges and the silences read the same probes;
        a 9 GB run's probes do not all fit in memory as rows)."""
        path = pathlib.Path(path)
        session = path.parent.parent
        if session != self._frames_session:
            self._frames, self._frames_session = {}, session
        if path not in self._frames:
            self._frames[path] = read_frames(path)
        return self._frames[path]

    def cached(self, key: tuple, make):
        if key not in self._session_cache:
            self._session_cache[key] = make()
        return self._session_cache[key]

    def lockups(self, name: str) -> list[dict]:
        """The soft lockups of the node's kernel logs, the replaced
        instance's included (sr.parse_kernel_log)."""
        key = (name, "lockups")
        if key not in self._referee:
            found = []
            for path in (self.node_dir(name) / "before-replace" / "kernel.log",
                         self.node_dir(name) / "kernel.log"):
                if path.is_file():
                    found += [{**item, "instance": "before-replace" if
                               path.parent.name == "before-replace" else "last"}
                              for item in sr.parse_kernel_log(path.read_text(errors="replace"))]
            seen, out = set(), []
            for item in sorted(found, key=lambda item: item["t_us"]):
                mark = (item["t_us"], item["cpu"], item["comm"])
                if mark not in seen:
                    seen.add(mark)
                    out.append(item)
            self._referee[key] = out
        return self._referee[key]

    def stacks(self, name: str) -> list[dict]:
        """The stack dumps of the node's extra sampler (extra.log), the
        replaced instance's included; only the stack lines are parsed."""
        key = (name, "stacks")
        if key not in self._referee:
            lines = []
            for path in (self.node_dir(name) / "before-replace" / "extra.log",
                         self.node_dir(name) / "extra.log"):
                if path.is_file():
                    with open(path, errors="replace") as handle:
                        lines += [line for line in handle if " X stack " in line]
            self._referee[key] = sorted(sr.parse_stacks("".join(lines)),
                                        key=lambda item: item["t_us"])
        return self._referee[key]

    def contributor(self) -> str | None:
        names = [name for name in self.nodes if self.role(name) == "contributor"]
        return names[0] if names else None

    def span_us(self) -> tuple[int, int] | None:
        """From the first to the last line of any watch record."""
        if "_span" not in self.__dict__:
            times = [value for name in self.nodes for value
                     in (sr.at_us(item) for item in self.watch(name)) if value]
            self._span = (min(times), max(times)) if times else None
        return self._span

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
        tick thread it could not pin, at any line of its sampler (its cut of
        /api/mesh/local: stall.tick_threads and stall.tick_pinned)."""
        if self._unpinned is None:
            self._unpinned = {}
            for name in self.mesh():
                for line in self.sampler(name) + self.meshlocal_sample(name):
                    threads = sr.mesh_value(line, "tick_threads")
                    pinned = sr.mesh_value(line, "tick_pinned")
                    if threads is not None and pinned is not None and pinned < threads:
                        self._unpinned[name] = f"{pinned} of {threads}"
        return self._unpinned

    def referee_files(self, name: str, kind: str, suffix: str = "log") -> list[pathlib.Path]:
        """The referee's sampler files of a node and kind, every call's
        (samplers/NODE.KIND.T.log), in time order."""
        return sorted((self.dir / "samplers").glob(f"{name}.{kind}.*.{suffix}"),
                      key=lambda path: path.name)

    def referee_text(self, name: str, kind: str, suffix: str = "log") -> str:
        """The referee's sampler files of a node and kind, joined (small
        files only: the heartbeat and NIC files)."""
        return "\n".join(path.read_text(errors="replace")
                         for path in self.referee_files(name, kind, suffix))

    def has_referee(self, name: str) -> bool:
        return any((self.dir / "samplers").glob(f"{name}.heartbeat.*.log"))

    def heartbeat(self, name: str) -> list[dict]:
        key = (name, "heartbeat")
        if key not in self._referee:
            self._referee[key] = sr.parse_heartbeat(self.referee_text(name, "heartbeat"))
        return self._referee[key]

    def _sys(self, name: str) -> None:
        """One streamed pass over the node's sys sampler: the times of its
        seconds, and the task lines of the class threads (the only ones a
        stop's class reads: sr.class_thread)."""
        times, tasks = set(), []
        marker = f"({sr.CLASS_THREAD})".encode()
        for path in self.referee_files(name, "sys"):
            last = None
            with open(path, "rb") as handle:
                for line in handle:
                    cut = line.find(b" ")
                    if line[cut + 1:cut + 3] != b"S ":
                        continue
                    stamp = line[:cut]
                    if stamp != last:
                        try:
                            times.add(int(float(stamp) * 1e6))
                        except ValueError:
                            continue
                        last = stamp
                    if marker in line and line[cut + 3:cut + 8] == b"task ":
                        parts = line.decode(errors="replace").rstrip("\n").split(" ", 4)
                        task = sr.parse_task_stat(parts[4]) if len(parts) == 5 else None
                        if task:
                            try:
                                tasks.append({"t_us": int(float(parts[0]) * 1e6),
                                              "pid": int(parts[3]), **task})
                            except ValueError:
                                continue
        self._referee[(name, "sys_times")] = sorted(times)
        self._referee[(name, "tasks")] = sorted(tasks, key=lambda item: (item["t_us"],
                                                                          item["tid"]))

    def tasks(self, name: str) -> list[dict]:
        """The class threads' task lines of the node's sys sampler."""
        if (name, "tasks") not in self._referee:
            self._sys(name)
        return self._referee[(name, "tasks")]

    def sys_seconds(self, name: str) -> list[int]:
        if (name, "sys_times") not in self._referee:
            self._sys(name)
        return self._referee[(name, "sys_times")]

    def meshlocal_index(self, name: str) -> list[tuple[int, pathlib.Path, int]]:
        """(t_us, file, offset) of every line of the node's meshlocal
        sampler, oldest first: its lines (a node's whole /api/mesh/local,
        a few hundred kB each) are parsed only when one is read."""
        key = (name, "meshlocal_index")
        if key not in self._referee:
            index = []
            for path in self.referee_files(name, "meshlocal"):
                with open(path, "rb") as handle:
                    offset = 0
                    for line in handle:
                        stamp = line[:line.find(b" ")] if b" " in line[:40] else b""
                        try:
                            index.append((int(float(stamp) * 1e6), path, offset))
                        except ValueError:
                            pass
                        offset += len(line)
            index.sort(key=lambda item: item[0])
            self._referee[key] = index
        return self._referee[key]

    def meshlocal_line(self, entry: tuple[int, pathlib.Path, int]) -> dict | None:
        key = ("meshlocal_line", entry[1], entry[2])
        if key not in self._referee:
            with open(entry[1], "rb") as handle:
                handle.seek(entry[2])
                line = handle.readline().decode(errors="replace")
            found = sr.parse_meshlocal(line)
            self._referee[key] = found[0] if found else None
        return self._referee[key]

    def meshlocal_around(self, start_us: int, end_us: int, name: str) -> list[dict]:
        """The meshlocal lines just before `start_us` and just after
        `end_us` (what a stop's class reads), parsed alone."""
        import bisect
        index = self.meshlocal_index(name)
        times = [item[0] for item in index]
        out = []
        before = bisect.bisect_right(times, start_us) - 1
        after = bisect.bisect_left(times, end_us)
        for at in (before, after):
            if 0 <= at < len(index):
                line = self.meshlocal_line(index[at])
                if line is not None:
                    out.append(line)
        return out

    def meshlocal(self, name: str) -> list[dict]:
        """Every meshlocal line parsed (tests and small runs; a stop's class
        reads meshlocal_around)."""
        return [line for line in (self.meshlocal_line(entry)
                                  for entry in self.meshlocal_index(name)) if line]

    def meshlocal_sample(self, name: str, every_s: float = MESHLOCAL_SAMPLE_S) -> list[dict]:
        """One meshlocal line every `every_s` (and the first and last): what
        a figure that holds for a process's life (its tick threads pinned)
        needs, without parsing every line."""
        index = self.meshlocal_index(name)
        out, last = [], None
        for at, entry in enumerate(index):
            if last is None or entry[0] - last >= every_s * 1e6 or at == len(index) - 1:
                line = self.meshlocal_line(entry)
                if line is not None:
                    out.append(line)
                last = entry[0]
        return out

    def nic(self, name: str) -> dict[str, str]:
        """The node's offloads of NIC_OFFLOADS by interface (`ethtool -k`
        of its last NIC file): {"eth0 generic-receive-offload": "on"}."""
        files = sorted((self.dir / "samplers").glob(f"{name}.nic.*.txt"))
        return nic_offloads(files[-1].read_text(errors="replace")) if files else {}

    def classify(self, edge: str, begin: int, finish: int, trusted: bool) -> dict:
        """A stop of an edge, by the referee's samplers when the run has
        them, else by the hunt's own sampler."""
        if self.has_referee(edge):
            return sr.classify_heartbeat(begin, finish, self.heartbeat(edge), self.tasks(edge),
                                         self.meshlocal_around(begin, finish, edge),
                                         self.journal(edge), self.lockups(edge),
                                         self.stacks(edge))
        return sr.classify(begin, finish, self.sampler(edge), self.journal(edge),
                           self.watch(edge), trusted)

    def sampler(self, name: str) -> list[dict]:
        """The node's sampler lines, the replaced instance's included."""
        if name not in self._sampler:
            self._sampler[name] = sr.sampler_lines(
                sr.read_ndjson(self.node_dir(name) / "before-replace" / "sampler.ndjson")
                + sr.read_ndjson(self.node_dir(name) / "sampler.ndjson"))
        return self._sampler[name]

    def node_file(self, name: str, file: str) -> str | None:
        """A small file of the node's records (the instance's own, else the
        replaced one's)."""
        for path in (self.node_dir(name) / file, self.node_dir(name) / "before-replace" / file):
            if path.is_file():
                return path.read_text(errors="replace")
        return None

    def addresses(self) -> dict[str, str]:
        """{public address: node}, every address a node had (a replaced
        instance's included)."""
        out = {}
        for item in self.record.get("replaced") or []:
            if item.get("public_ipv4_before"):
                out[item["public_ipv4_before"]] = item["name"]
        for name, node in self.nodes.items():
            if node.get("public_ipv4"):
                out[node["public_ipv4"]] = name
        return out

    def edges(self) -> list[str]:
        return sorted(name for name in self.nodes if self.role(name) == "edge")

    def roles(self) -> dict[str, str]:
        """{session: role}."""
        return {item["name"]: item.get("role") for item in self.record.get("sessions") or []
                if item.get("name")}

    def round_starts(self, session: str) -> list[int]:
        """The churn rounds' starts (us), from every view host's rounds.txt."""
        out = []
        for path in sorted((self.dir / "sessions" / session).glob("*/rounds.txt")):
            for line in path.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2:
                    out.append(int(float(parts[1]) * 1e6))
        return sorted(out)


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
    import bisect
    for name in sorted(run.nodes):
        watch, journal, cpus = run.watch(name), run.journal(name), run.cpus(name)
        watch_at = [sr.at_us(item) or 0 for item in watch]
        journal_at = [sr.at_us(item) or 0 for item in journal]
        whole = sorted(watch_at) == watch_at and sorted(journal_at) == journal_at
        for label, start, end in periods:
            if whole:
                # Only the lines of the period (and a margin for an event's
                # length on either side): the same figures, read once.
                part = watch[bisect.bisect_left(watch_at, start - PERIOD_MARGIN_US):
                             bisect.bisect_right(watch_at, end + PERIOD_MARGIN_US)]
                own = journal[bisect.bisect_left(journal_at, start - PERIOD_MARGIN_US):
                              bisect.bisect_right(journal_at, end + PERIOD_MARGIN_US)]
            else:
                part, own = watch, journal
            lines = sr.seconds(part, start, end)
            f_watch = f_over(part, [(start, end)], cpus) if watch else None
            f_journal = f_over(own, [(start, end)], cpus) if journal else None
            rows.append({
                "node": name, "period": label, "stops": sr.stop_counts(part, start, end),
                "scope_trusted": name not in unpinned, "tick_pinned": unpinned.get(name),
                "f_watch": f_watch, "f_journal": f_journal, "agree": agree(f_watch, f_journal),
                "steal_permille": sr.steal_permille(part, start, end),
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


def lockup_table(run: Run) -> dict[str, list[dict]]:
    """Every soft lockup the nodes' kernels reported in the run, per node,
    with the host it happened on: {node: [{"t_us", "cpu", "stuck_s",
    "comm", "work", "text", "host"}]}."""
    out = {}
    for name in sorted(run.nodes):
        found = [{"t_us": item["t_us"], "cpu": item["cpu"], "stuck_s": item["stuck_s"],
                  "comm": item["comm"], "work": item["work"], "text": sr.lockup_text(item),
                  "host": run.host_at(name, item["t_us"])} for item in run.lockups(name)]
        if found:
            out[name] = found
    return out


def lockup_line(lockups: dict[str, list[dict]]) -> str:
    """The soft lockups by node and host, and what the kernel was doing."""
    if not lockups:
        return "no soft lockup in any node's kernel log"
    parts = []
    for name, items in sorted(lockups.items()):
        by_host: dict[str, list[dict]] = {}
        for item in items:
            by_host.setdefault(short_host(item["host"]), []).append(item)
        for host, mine in sorted(by_host.items()):
            works: dict[str, int] = {}
            for item in mine:
                works[item["work"]] = works.get(item["work"], 0) + 1
            parts.append(f"{name} {len(mine)} on host {host} ("
                         + ", ".join(f"{work} {count}" for work, count in sorted(works.items()))
                         + ")")
    return "soft lockups: " + "; ".join(parts)


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


# ------------------------------------------------------ 4. silences joined

class Frames:
    """A probe's audio rows (ntcmp --frames, kind "a") read by columns, the
    numbers parsed once: complete_us, latency_us, part, published_us,
    committed_us (None where the CSV has no value or no column). Iterating
    gives each row as a dict of the CSV's strings, as before."""

    COLUMNS = {"complete_us": "complete_unix_us", "latency_us": "latency_us", "part": "part",
               "published_us": "published_unix_us", "committed_us": "edge_committed_unix_us"}

    def __init__(self, header: list[str], rows: list[list[str]]) -> None:
        self.header, self.rows = header, rows
        self._where = {name: index for index, name in enumerate(header)}

    def __getattr__(self, attribute: str) -> list:
        """A column, parsed when first asked for (the edges read three, the
        silences two)."""
        column = Frames.COLUMNS.get(attribute)
        if column is None or attribute.startswith("_"):
            raise AttributeError(attribute)
        rows, index = self.rows, self._where.get(column)
        if index is None:
            values = [None] * len(rows)
        else:
            try:
                strings = list(map(operator.itemgetter(index), rows))
            except IndexError:
                strings = [row[index] if index < len(row) else "" for row in rows]
            values = (list(map(int, strings)) if "" not in strings
                      else [int(value) if value else None for value in strings])
        setattr(self, attribute, values)
        return values

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return (dict(zip(self.header, row)) for row in self.rows)


def read_frames(path) -> Frames:
    """A probe's per-unit CSV (ntcmp --frames), audio rows."""
    with open(path, newline="") as handle:
        text = handle.read()
    if '"' in text:
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader, [])
        rows = [row for row in reader if row and row[0] == "a"]
    else:
        # Numbers only (ntcmp writes no quoted field): split by hand.
        lines = text.splitlines()
        header = lines[0].split(",") if lines else []
        rows = [line.split(",") for line in lines[1:] if line.startswith("a,")]
    return Frames(header, rows)


def silences(rows: list[dict], min_ms: float | None = None) -> list[dict]:
    """Arrival silences of a probe: [{"from_us", "to_us", "ms", "source_us"}]
    (source_us: the source time of the unit that ended the silence). The
    threshold is 3 ms, or two unit spacings when that is more."""
    if isinstance(rows, Frames):
        units = sorted((complete, complete - latency) for complete, latency
                       in zip(rows.complete_us, rows.latency_us)
                       if complete is not None and latency is not None)
    else:
        units = sorted((int(row["complete_unix_us"]), int(row["complete_unix_us"])
                        - int(row["latency_us"])) for row in rows)
    gaps = [b[0] - a[0] for a, b in zip(units, units[1:])]
    if not gaps:
        return []
    spacing = sorted(gaps)[len(gaps) // 2] / 1000
    threshold = min_ms if min_ms is not None else max(SILENCE_MS, 2 * spacing)
    return [{"from_us": a[0], "to_us": b[0], "ms": (b[0] - a[0]) / 1000, "source_us": b[1]}
            for a, b in zip(units, units[1:]) if (b[0] - a[0]) / 1000 >= threshold]


_EVENT_INDEXES: dict[int, tuple] = {}


class EventIndex:
    """One source's events (not `sec` lines) indexed by the end of their
    interval: the events that meet a window are found by bisection, not by
    a scan of the whole list (sr.overlapping's answer, fast)."""

    def __init__(self, events: list[dict]) -> None:
        import bisect
        self._bisect = bisect
        spans = sorted((sr.interval(event) + (index,))
                       for index, event in enumerate(events)
                       if event.get("kind") != "sec" and event.get("t_us"))
        spans.sort(key=lambda item: item[1])
        self.ends = [end for _begin, end, _index in spans]
        self.spans = spans
        self.events = events
        self.longest = max((end - begin for begin, end, _index in spans), default=0)

    @classmethod
    def of(cls, events: list[dict]) -> "EventIndex":
        """The index of a list, built once while the list lives (the same
        journal is a source of every listener on its path)."""
        kept = _EVENT_INDEXES.get(id(events))
        if kept is None or kept[0] is not events:
            kept = (events, cls(events))
            _EVENT_INDEXES[id(events)] = kept
        return kept[1]

    def overlapping(self, start_us: int, end_us: int) -> list[dict]:
        lo = self._bisect.bisect_left(self.ends, start_us)
        hi = self._bisect.bisect_right(self.ends, end_us + self.longest)
        return [self.events[index] for begin, _end, index in self.spans[lo:hi]
                if begin <= end_us]


def first_cause(start_us: int, end_us: int, indexed: list[tuple[str, EventIndex]]):
    """sr.first_cause over indexed sources."""
    for side, index in indexed:
        found = index.overlapping(start_us, end_us)
        if found:
            return side, max(found, key=lambda event: int(event.get("dur_us") or 0))
    return None


def gap_join(found: list[dict], sources: list[tuple[str, list[dict]]],
             margin_us: int = 5000) -> dict:
    """Each silence joined to the first source with an event between the
    source time of the unit that ended it (less a margin) and its end."""
    by_cause: dict[str, dict] = {}
    indexed = [(side, EventIndex.of(events)) for side, events in sources] if found else []
    for gap in found:
        cause = first_cause(gap["source_us"] - margin_us, gap["to_us"], indexed)
        gap["cause"] = cause[0] if cause else "unexplained"
        slot = by_cause.setdefault(gap["cause"], {"n": 0, "ms": 0.0})
        slot["n"] += 1
        slot["ms"] = round(slot["ms"] + gap["ms"], 3)
    return {"silences": len(found), "by_cause": by_cause,
            "unexplained": by_cause.get("unexplained", {"n": 0, "ms": 0.0})}


def probe_edge(csv_path: pathlib.Path, addresses: dict[str, str] | None = None) -> str | None:
    """The edge that served a probe: an LL-HLS probe's by the host its
    media playlist came from (the lab's addresses), or, in a record without
    them (the referee's), by its pathway; the UDP client's one cache."""
    stem = csv_path.name[:-len(".probe.csv")]
    report = csv_path.with_name(f"{stem}.probe.json")
    client = csv_path.with_name(f"{stem}.uc.json")
    try:
        if client.is_file():
            caches = json.loads(client.read_text()).get("caches") or []
            return caches[0] if len(caches) == 1 else None
        if report.is_file():
            detail = json.loads(report.read_text()).get("detail") or {}
            if addresses:
                host = str(detail.get("playlist_url_host") or "").rsplit(":", 1)[0]
                return addresses.get(host)
            return (detail.get("variant") or {}).get("PATHWAY-ID")
    except (OSError, ValueError):
        return None
    return None


def session_dir(run: Run, session: str) -> pathlib.Path:
    return run.dir / "sessions" / session


def sender_lines(run: Run, session: str) -> list[dict]:
    def read() -> list[dict]:
        lines = []
        for path in sorted(session_dir(run, session).glob("*/sender.ndjson")):
            lines += sr.read_ndjson(path)
        return lines
    return run.cached((session, "sender_lines"), read)


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
    sender = run.cached((session, "sender_events"),
                        lambda: sr.sender_events(sender_lines(run, session)))
    contributor = run.contributor()
    journal = run.journal(contributor) if contributor else []
    source = source_host(run, session)
    sources = list(run.cached((session, "source_sides"), lambda: sr.source_sides(
        sender, journal, run.watch(source) if source else [], contributor or "contributor",
        source or "source host")))
    for name in path_nodes(run, host, edge):
        if name == contributor:
            continue
        for cause in ("stopped", "starved", "own", "waited"):
            sources.append((f"{name} {cause}", run.cached(
                ("journal_cause", name, cause),
                lambda name=name, cause=cause: [event for event in run.journal(name)
                                                if event.get("cause") == cause])))
    sources.append((f"{host} (listener host) stopped", run.cached(
        ("stopped_ticks", host), lambda: sr.stopped_ticks(run.watch(host), 0))))
    sources.append((f"{host} (listener host) starved", run.cached(
        ("starved_ticks", host), lambda: [event for event in sr.ticks(run.watch(host))
                                          if sr.tick_cause(event) == "starved"])))
    return sources


def session_probes(run: Run, session: str) -> list[pathlib.Path]:
    return sorted(session_dir(run, session).glob("*/*.probe.csv"))


def silence_table(run: Run, only: list[str] | None = None) -> dict:
    out = {}
    addresses = run.addresses()
    for name, _start, _end in run.periods_of("session"):
        if only is not None and name not in only:
            continue
        per = {}
        for path in session_probes(run, name):
            host = path.parent.name
            edge = probe_edge(path, addresses)
            joined = gap_join(silences(run.frames(path)),
                              listener_sources(run, name, host, edge))
            per[f"{host}/{path.name}"] = {"edge": edge, **joined}
        out[name] = per
    return out


# ------------------------------ 3. the edges' stops, classified; the twins

def edge_commits(probes: list[tuple[str, list[dict]]]) -> tuple[dict, dict]:
    """From LL-HLS probes [(edge, rows)]: {edge: {part: commit_us}} and
    {part: published_us}. A part's commit is the edge's own stamp, the same
    for every probe it served."""
    commits: dict[str, dict[int, int]] = {}
    published: dict[int, int] = {}
    for edge, rows in probes:
        if isinstance(rows, Frames):
            mine = commits.setdefault(edge, {})
            for part, commit, stamp in zip(rows.part, rows.committed_us, rows.published_us):
                if not commit or part is None:
                    continue
                if part not in mine:
                    mine[part] = commit
                if stamp:
                    published[part] = stamp
            if not mine:
                del commits[edge]
            continue
        for row in rows:
            if not row.get("edge_committed_unix_us") or not row.get("part"):
                continue
            part = int(row["part"])
            commits.setdefault(edge, {}).setdefault(part, int(row["edge_committed_unix_us"]))
            if row.get("published_unix_us"):
                published[part] = int(row["published_unix_us"])
    return commits, published


def commit_gaps(commits: dict[str, dict[int, int]], min_ms: float = STOP_MS,
                since_us: int | None = None, until_us: int | None = None) -> dict:
    """Each edge's commit gaps of `min_ms` or more, between consecutive
    commits in time order (a part repaired late does not open a gap):
    {edge: [(from_us, to_us, ms)]}, those that begin in [since, until]."""
    out = {}
    for edge, by_part in commits.items():
        times = sorted(by_part.values())
        gaps = []
        for start, end in zip(times, times[1:]):
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
                 until_us: int | None = None, exclude: list[tuple[int, int]] = ()) -> dict:
    """Per part that both edges of a pair committed, the earlier commit's
    distance from publish (ms): what a listener held on those two gets.
    `late_on_both`: the parts each edge committed LATE_ON_BOTH_MS or more
    over its own p50 of commit minus publish, those published inside
    `exclude` (the stops every edge shared) left out."""
    if len(edges) != 2 or any(edge not in commits for edge in edges):
        return {"parts": 0}
    first, second = commits[edges[0]], commits[edges[1]]
    values, late = [], 0
    # Late is late for that edge: over its own p50 of commit minus publish
    # by LATE_ON_BOTH_MS (a Tokyo edge is 110 ms from the London source).
    usual = [lateness_of(mine, published)[1] for mine in (first, second)]
    low = -math.inf if since_us is None else since_us
    high = math.inf if until_us is None else until_us
    limit = LATE_ON_BOTH_MS * 1000
    for part in first.keys() & second.keys():
        stamp = published.get(part)
        if stamp is None or not low <= stamp <= high:
            continue
        a, b = first[part] - stamp, second[part] - stamp
        values.append(min(a, b) / 1000)
        if a - usual[0] > limit and b - usual[1] > limit and not any(
                start - SHARED_WITHIN_US <= stamp <= end + SHARED_WITHIN_US
                for start, end in exclude):
            late += 1
    return {"parts": len(values), "p50_ms": pct(values, 50), "p99_ms": pct(values, 99),
            "max_ms": max(values) if values else None, "late_on_both": late}


def hls_probes(paths: list[pathlib.Path], addresses: dict[str, str] | None = None,
               reader=None) -> list[tuple[str, list[dict]]]:
    out = []
    for path in paths:
        edge = probe_edge(path, addresses)
        if edge and path.with_name(path.name[:-len(".probe.csv")] + ".probe.json").is_file():
            out.append((edge, (reader or read_frames)(path)))
    return out


def udp_clients(folder: pathlib.Path, addresses: dict[str, str] | None = None) -> dict[str, int]:
    """The UDP clients that reached each edge in a session (its directory
    of view hosts): every cache a UDP probe used (its uc.json `caches`, node
    names) and, per edge of a needletail-load crowd (`*load/intervals.jsonl`,
    edges keyed by address), the most of its listeners subscribed in an
    interval in which they received parts. {edge: clients}."""
    out: dict[str, int] = {}
    for path in sorted(folder.glob("*/*.uc.json")):
        try:
            caches = json.loads(path.read_text()).get("caches") or []
        except (OSError, ValueError):
            continue
        for cache in caches:
            name = (addresses or {}).get(str(cache).rsplit(":", 1)[0], cache)
            out[name] = out.get(name, 0) + 1
    for path in sorted(folder.glob("*/*load/intervals.jsonl")):
        most: dict[str, int] = {}
        for line in sr.read_ndjson(path):
            for key, figure in (line.get("edges") or {}).items():
                name = (addresses or {}).get(key.rsplit(":", 1)[0], key)
                if int(figure.get("parts") or 0) > 0:
                    most[name] = max(most.get(name, 0), int(figure.get("up") or 0))
        for name, count in most.items():
            out[name] = out.get(name, 0) + count
    return out


def first_of_pairs(commits: dict[str, dict[int, int]], published: dict[int, int],
                   since_us: int | None, until_us: int | None,
                   exclude: list[tuple[int, int]] = ()) -> dict:
    """Per region (an edge's name up to its first dash), the first of two
    for every pair of the region's edges seen: {region: {"a+b": figure}};
    a region with one edge seen has no pair."""
    regions: dict[str, list[str]] = {}
    for edge in commits:
        regions.setdefault(edge.split("-")[0], []).append(edge)
    return {region: {f"{a}+{b}": first_of_two(commits, published, [a, b], since_us, until_us,
                                              exclude)
                     for a, b in itertools.combinations(sorted(edges), 2)}
            for region, edges in sorted(regions.items())}


def twin_overlaps(stops: list[dict], within_us: int = TWIN_WITHIN_US) -> list[dict]:
    """Stops of two edges of one region at once (overlapping, or apart by
    `within_us` or less): [{"edges", "from_us", "to_us", "both_ms"}];
    `both_ms` is the time both were stopped (0 when only close)."""
    out = []
    for a, b in itertools.combinations(sorted(stops, key=lambda item: item["from_us"]), 2):
        if a["edge"] == b["edge"] or a["edge"].split("-")[0] != b["edge"].split("-")[0]:
            continue
        apart = max(a["from_us"], b["from_us"]) - min(a["to_us"], b["to_us"])
        if apart <= within_us:
            out.append({"edges": sorted([a["edge"], b["edge"]]),
                        "from_us": min(a["from_us"], b["from_us"]),
                        "to_us": max(a["to_us"], b["to_us"]),
                        "both_ms": round(max(0, -apart) / 1000, 3)})
    return out


_LATENESS: dict[int, tuple] = {}


def lateness_of(commits: dict[int, int], published: dict[int, int]) -> tuple[list, int]:
    """An edge's parts as (part, published, commit) in part order and its
    p50 of commit minus publish (us), computed once per commits dict."""
    kept = _LATENESS.get(id(commits))
    if kept is not None and kept[0] is commits and kept[1] is published:
        return kept[2], kept[3]
    rows = sorted((part, published[part], commit) for part, commit in commits.items()
                  if part in published)
    lateness = sorted(commit - pub for _part, pub, commit in rows)
    p50 = lateness[len(lateness) // 2] if lateness else 0
    _LATENESS[id(commits)] = (commits, published, rows, p50)
    return rows, p50


def own_episodes(commits: dict[int, int], published: dict[int, int], min_ms: float,
                 since_us: int | None = None, until_us: int | None = None) -> list[dict]:
    """An edge's own lateness episodes (the hunt operator's reading,
    hunt-lab.md (a)): per part, commit minus publish above the edge's p50
    of it; runs of parts `min_ms` or more above it (parts at most
    EPISODE_JOIN_PARTS apart joined): [{"first_part", "last_part",
    "pub_from_us", "pub_to_us", "from_us" (when the first part would have
    committed: its publish plus the p50), "to_us" (the last commit), "ms"
    (the most a part was above the p50)}], those whose first part was
    published in [since, until]. Without publish stamps: the commit gaps
    (commit_gaps), each one episode."""
    if not commits:
        return []
    rows, p50 = lateness_of(commits, published)
    if not rows:
        return [{"first_part": None, "last_part": None, "pub_from_us": a, "pub_to_us": b,
                 "from_us": a, "to_us": b, "ms": ms}
                for a, b, ms in commit_gaps({"e": commits}, min_ms, since_us, until_us)["e"]]
    out: list[dict] = []
    current = None
    for part, pub, commit in rows:
        excess = (commit - pub - p50) / 1000
        if excess < min_ms:
            continue
        if current is not None and part - current["last_part"] <= EPISODE_JOIN_PARTS:
            current["last_part"] = part
            current["pub_to_us"] = pub
            current["to_us"] = max(current["to_us"], commit)
            current["ms"] = max(current["ms"], round(excess, 3))
            continue
        current = {"first_part": part, "last_part": part, "pub_from_us": pub, "pub_to_us": pub,
                   "from_us": pub + p50, "to_us": commit, "ms": round(excess, 3)}
        out.append(current)
    return [item for item in out
            if (since_us is None or item["pub_from_us"] >= since_us)
            and (until_us is None or item["pub_from_us"] <= until_us)]


def coverage(commits: dict[int, int], published: dict[int, int]) -> tuple[int, int] | None:
    """The publish times (else commit times) an edge's probe spans."""
    stamps = [published[part] for part in commits if part in published] or list(commits.values())
    return (min(stamps), max(stamps)) if stamps else None


def covers(span: tuple[int, int] | None, a_us: int, b_us: int) -> bool:
    """The edge's probe has parts published before `a_us` and after `b_us`."""
    return span is not None and span[0] <= a_us and span[1] >= b_us


def split_shared(episodes: dict[str, list[dict]], short: dict[str, list[dict]],
                 commits: dict[str, dict[int, int]], published: dict[int, int],
                 within_us: int = SHARED_WITHIN_US) -> tuple[dict[str, list[dict]], list[dict]]:
    """Each edge's episodes split: an episode is shared (input every edge
    waited for: the source's stops) when two or more edges are measured and
    every other edge whose probe covers its time has an episode (`short`:
    SHORT_STOP_MS or more) overlapping it in publish time, within
    `within_us`, about as long: at least SHARED_SHARE of it and no more than
    STOP_MS shorter (tok-edge-2's 2,337 ms against the source's 2,251 is
    shared; its 4,793 ms with a 20 ms blip elsewhere is its own). Returns ({edge: own episodes}, [shared, merged across the
    edges: {"from_us", "to_us", "ms" (the most of any edge), "edges"}])."""
    own: dict[str, list[dict]] = {}
    shared: list[dict] = []
    measured = sorted(commits)
    spans = {edge: coverage(commits[edge], published) for edge in measured}
    for edge, items in episodes.items():
        own[edge] = []
        for item in items:
            a, b = item["pub_from_us"], item["pub_to_us"]
            others = [other for other in measured if other != edge
                      and covers(spans[other], a, b)]
            # As long, or nearly: the other edge waited out the same input
            # (a blip of its own inside a long stop of this one is not it).
            needed = max(SHORT_STOP_MS, SHARED_SHARE * item["ms"], item["ms"] - STOP_MS)
            together = bool(others) and all(
                any(other_item["pub_from_us"] - within_us <= b
                    and other_item["pub_to_us"] + within_us >= a
                    and other_item["ms"] >= needed
                    for other_item in short.get(other, [])) for other in others)
            if not together:
                own[edge].append(item)
                continue
            for slot in shared:
                if slot["from_us"] - within_us <= b and slot["to_us"] + within_us >= a:
                    slot["from_us"], slot["to_us"] = min(slot["from_us"], a), max(slot["to_us"], b)
                    slot["ms"] = max(slot["ms"], item["ms"])
                    slot["edges"] = sorted(set(slot["edges"]) | {edge})
                    break
            else:
                shared.append({"from_us": a, "to_us": b, "ms": item["ms"], "edges": [edge]})
    return own, sorted(shared, key=lambda slot: slot["from_us"])


def edges_table(run: Run, only: list[str] | None = None) -> dict:
    """Per session: each edge's own stops of 100 ms or more (commit minus
    publish above its p50, from its LL-HLS probe; each classified and with
    the physical host it happened on), the stops every edge shared (the
    source's: input waited for, never an edge's stop), the count of own
    stops of 20 ms or more, the edges seen, the first of two per pair (parts
    inside a shared stop left out), and the twins' stops at once."""
    out = {}
    roles = run.roles()
    addresses = run.addresses()
    unpinned = run.unpinned()
    for name, start, end in run.periods_of("session"):
        if only is not None and name not in only:
            continue
        _LATENESS.clear()
        probes = hls_probes(session_probes(run, name), addresses, run.frames)
        commits, published = edge_commits(probes)
        # Each edge's own publish stamps: a part its probe has no stamp for
        # is not one of its rows (another probe's stamp would join two of
        # its stops across parts it never showed).
        stamped: dict[str, dict[int, int]] = {}
        for edge, rows in probes:
            stamped.setdefault(edge, {}).update(edge_commits([(edge, rows)])[1])
        short = {edge: own_episodes(by_part, stamped.get(edge, published), SHORT_STOP_MS)
                 for edge, by_part in commits.items()}
        windowed = {edge: [item for item in items if start <= item["pub_from_us"] <= end]
                    for edge, items in short.items()}
        # The stops: episodes at STOP_MS itself (as the operator read them:
        # two stops 40 parts apart stay two), the shared ones left out.
        long = {edge: [item for item in own_episodes(by_part, stamped.get(edge, published),
                                                     STOP_MS)
                       if start <= item["pub_from_us"] <= end]
                for edge, by_part in commits.items()}
        own_long, _shared_long = split_shared(long, short, commits, published)
        own, shared = split_shared(windowed, short, commits, published)
        stops = []
        for edge, items in sorted(own_long.items()):
            for item in items:
                begin, finish = item["from_us"], item["to_us"]
                stops.append({"session": name, "role": roles.get(name), "edge": edge,
                              "from_us": begin, "to_us": finish, "ms": item["ms"],
                              "host": run.host_at(edge, begin),
                              **run.classify(edge, begin, finish, edge not in unpinned)})
        over_20 = {edge: len(items) for edge, items in own.items()}
        out[name] = {"role": roles.get(name), "seen": sorted(commits), "stops": stops,
                     "shared": [slot for slot in shared if slot["ms"] >= STOP_MS],
                     "udp_clients": udp_clients(session_dir(run, name), addresses),
                     "over_20ms": over_20, "twins": twin_overlaps(stops),
                     "first_of_two": first_of_pairs(commits, published, start, end,
                                                    [(slot["from_us"], slot["to_us"])
                                                     for slot in shared])}
    return out


# ----------------------------------------------------------------- 5. null

def _null_one(item: tuple) -> list[dict]:
    """One probe's null (a worker of null_table)."""
    frame, paired, lengths = item
    null_window = load_null_window()
    return null_window.analyse(null_window.referee(), [frame], [paired] if paired else [],
                               lengths, 5.0, 97.5, 120)["probes"]


def null_table(run: Run, lengths, null_window=None, jobs: int = 1) -> dict:
    work = []
    for name, _start, _end in run.periods_of("session"):
        for path in session_probes(run, name):
            stem = path.name[:-len(".probe.csv")]
            paired = path.with_name(f"{stem}.arrivals.csv")
            work.append((name, (str(path), str(paired) if paired.is_file() else None,
                                tuple(lengths))))
    if jobs > 1 and null_window is None:
        import concurrent.futures
        with concurrent.futures.ProcessPoolExecutor(jobs, mp_context=_fork_context()) as pool:
            results = list(pool.map(_null_one, [item for _name, item in work]))
    else:
        null_window = null_window or load_null_window()
        cw = null_window.referee()
        results = [null_window.analyse(cw, [frame], [paired] if paired else [], list(lengths),
                                       5.0, 97.5, 120)["probes"]
                   for _name, (frame, paired, lengths) in work]
    out = {name: [] for name, _start, _end in run.periods_of("session")}
    for (name, _item), probes in zip(work, results):
        out[name] += probes
    return out


# -------------------------------------------------------- 6. the source side

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
                reader = csv.reader(handle)
                header = next(reader, [])
                if "part" in header and "published_unix_us" in header:
                    part_at, stamp_at = header.index("part"), header.index("published_unix_us")
                    for row in reader:
                        if len(row) > max(part_at, stamp_at) and row[stamp_at] \
                                and int(row[stamp_at]):
                            found[int(row[part_at])] = int(row[stamp_at])
        else:
            frames = read_frames(path)
            found = {part: stamp for part, stamp in zip(frames.part, frames.published_us)
                     if part is not None and stamp}
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


# ---------------------------------------------------------------- 7. rules

def short_host(uuid: str | None) -> str:
    """A physical host's id as the reports quote it: its first 8 characters."""
    return (uuid or "-")[:8]


def all_stops(edges: dict, roles=None) -> list[dict]:
    """The stops of the edges table, of the sessions of `roles` (all)."""
    return [stop for item in edges.values() if roles is None or item["role"] in roles
            for stop in item["stops"]]


def ms_per_edge(stops: list[dict], names) -> float | None:
    """Summed stop ms of `names`, an edge."""
    names = set(names)
    if not names:
        return None
    return round(sum(stop["ms"] for stop in stops if stop["edge"] in names) / len(names), 3)


def stop_list(stops: list[dict]) -> str:
    return ", ".join(f"{stop['edge']} {stop['ms']:.0f} ms ({stop['class']})" for stop in stops)


def rule_fix(edges: dict, names: list[str]) -> dict:
    """H1: per edge, a stop of 100 ms or more in the first session? An edge
    is measured when its LL-HLS probe saw its commits and UDP clients
    reached it in that session (the fix is on the path to UDP clients); a
    stop counts whether or not a client was there."""
    first = [item for item in edges.values() if item["role"] == "first"]
    if not first:
        return {"fires": False, "verdicts": {}, "line": "H1 (the fix): no first session in "
                                                        "the record"}
    item = first[0]
    clients = item.get("udp_clients") or {}
    verdicts, unmeasured, stopped = {}, [], []
    for edge in names:
        mine = [stop for stop in item["stops"] if stop["edge"] == edge]
        if edge not in item["seen"]:
            verdicts[edge] = "not measured: no probe of it"
            unmeasured.append(edge)
        elif mine:
            verdicts[edge] = f"{len(mine)} stops: " + stop_list(mine) + (
                "" if clients.get(edge) else " (no UDP client reached it)")
            stopped.append(edge)
        elif not clients.get(edge):
            verdicts[edge] = "not measured: no UDP client reached it"
            unmeasured.append(edge)
        else:
            verdicts[edge] = f"no stop of 100 ms or more ({clients[edge]} UDP clients)"
    fires = not stopped and not unmeasured
    line = "H1 (the fix): " + (
        "NO STOP REMAINS: no edge stopped for 100 ms or more in the first session on the fresh "
        "deploy, with UDP clients at every edge" if fires else
        (f"stops remain on {', '.join(stopped)}" if stopped else "no stop on the edges measured")
        + (f"; not measured: {', '.join(unmeasured)}" if unmeasured else ""))
    return {"fires": fires, "verdicts": verdicts, "stopped": stopped,
            "unmeasured": unmeasured, "line": line}


def replaced_after(stops: list[dict], replaced: list[dict]) -> list[dict]:
    """Each replaced edge: its stops before the replace and after it."""
    out = []
    for item in replaced:
        at = int((item.get("unix") or 0) * 1e6)
        name = item.get("name")
        mine = [stop for stop in stops if stop["edge"] == name]
        out.append({"name": name, "host_before": item.get("host_uuid_before"),
                    "host_after": item.get("host_uuid_after"),
                    "before": [stop for stop in mine if stop["from_us"] < at],
                    "after": [stop for stop in mine if stop["from_us"] >= at]})
    return out


def rule_classes(edges: dict, hosts: dict[str, str | None], replaced: list[dict]) -> dict:
    """H2: what each stop of the run was, and what follows."""
    stops = all_stops(edges)
    by_class: dict[str, dict] = {}
    for stop in stops:
        slot = by_class.setdefault(stop["class"], {"n": 0, "ms": 0.0})
        slot["n"] += 1
        slot["ms"] = round(slot["ms"] + stop["ms"], 3)
    if not stops:
        return {"fires": {}, "by_class": {}, "line": (
            "H2 (each stop): no stop of 100 ms or more in the run: one repeat on fresh "
            "instances, then stop; the instruments stay in")}
    parts, fires = [], {}
    blocked = [stop for stop in stops if stop["class"] == "blocked in our code"]
    if blocked:
        waits: dict[str, int] = {}
        for stop in blocked:
            waits[stop["wait"]] = waits.get(stop["wait"], 0) + 1
        fires["our_code"] = True
        parts.append("OUR CODE: " + "; ".join(f"{wait} ({count})" for wait, count
                                              in sorted(waits.items(), key=lambda kv: -kv[1]))
                     + ": a fault of av-mesh, fixed at the named wait")
    not_run = [stop for stop in stops if stop["class"] == "not run"]
    if not_run:
        fires["not_run"] = True
        edges_hit: dict[str, list[dict]] = {}
        for stop in not_run:
            edges_hit.setdefault(stop["edge"], []).append(stop)
        where = sorted({stop["where"] for stop in not_run})
        stop_hosts: dict[str, set[str]] = {}
        for stop in not_run:
            host = stop.get("host") or hosts.get(stop["edge"])
            if host:
                stop_hosts.setdefault(host, set()).add(stop["edge"])
        shared = sorted(short_host(host) for host, names in stop_hosts.items() if len(names) > 1)
        outcome = "no replacement in the record"
        verdicts = []
        for item in replaced_after(stops, replaced):
            again = [stop for stop in item["after"] if stop["class"] in ("not run", "kernel")]
            same = item["host_before"] and item["host_before"] == item["host_after"]
            if not again:
                verdicts.append("clean")
            elif same:
                verdicts.append(f"same host {short_host(item['host_after'])}")
            else:
                verdicts.append("other host")
        if verdicts:
            outcome = ("the replaced instance is clean: the instance; the boot check "
                       "(node_check_seconds) with D12's replace-once" if all(
                           verdict == "clean" for verdict in verdicts)
                       else "the replacement on the same host stops again: a platform matter "
                            "for the owner (" + "; ".join(verdicts) + ")"
                       if any(verdict.startswith("same host") for verdict in verdicts)
                       else "the replacement on another host stops again: not one instance "
                            "but guests under first load (H10, broadened): the record goes "
                            "to the owner; two paths at every tier are the answer")
            outcome += "; replaced: " + "; ".join(
                f"{item['name']} {short_host(item['host_before'])} -> "
                f"{short_host(item['host_after'])} ({len(item['before'])} stops before, "
                f"{len(item['after'])} after)" for item in replaced_after(stops, replaced))
        by_host: dict[tuple[str, str], list[dict]] = {}
        for stop in not_run:
            by_host.setdefault((stop["edge"], stop.get("host") or hosts.get(stop["edge"]) or "-"),
                               []).append(stop)
        parts.append("NOT RUN (" + "; ".join(where) + "): "
                     + ", ".join(f"{edge} {len(items)} ({sum(s['ms'] for s in items):.0f} ms, "
                                 f"host {short_host(host)})"
                                 for (edge, host), items in sorted(by_host.items()))
                     + (f"; co-hosted: {', '.join(shared)}" if shared else "")
                     + f"; {outcome}")
    kernel = [stop for stop in stops if stop["class"] == "kernel"]
    if kernel:
        fires["kernel"] = True
        works: dict[str, int] = {}
        for stop in kernel:
            for text in (stop.get("figures") or {}).get("soft_lockups") or [stop.get("where", "")]:
                works[text.split(" (")[0]] = works.get(text.split(" (")[0], 0) + 1
        parts.append(f"IN THE KERNEL (soft lockups, {len(kernel)} stops, "
                     f"{sum(stop['ms'] for stop in kernel):.0f} ms): "
                     + "; ".join(f"{work} ({count})" for work, count in sorted(works.items()))
                     + ", on " + ", ".join(sorted({f"{stop['edge']} (host "
                                                   f"{short_host(stop.get('host'))})"
                                                   for stop in kernel})))
    waited = [stop for stop in stops if stop["class"] == "waited for input"]
    if waited:
        fires["input"] = True
        parts.append(f"WAITED FOR INPUT ({len(waited)}): upstream or the kernel's delivery; "
                     "read the London hop journals of these stops")
    rest = {cls: slot for cls, slot in by_class.items() if cls in ("undetermined", "no sampler")}
    if rest:
        parts.append("; ".join(f"{cls} {slot['n']}" for cls, slot in sorted(rest.items())))
    return {"fires": fires, "by_class": by_class,
            "line": "H2 (each stop): " + " | ".join(parts)}


def arm_rule(label: str, stops: list[dict], arm: list[str], others: list[str],
             evidence) -> dict:
    """An arm against the rest: `others` stop RATIO times as much an edge
    or more, STOP_MS or more, and one of their stops is `evidence`."""
    mine, theirs = ms_per_edge(stops, arm), ms_per_edge(stops, others)
    if mine is None or theirs is None:
        return {"fires": False, "line": f"{label}: no arm in the record"}
    shown = f"{theirs:.0f} ms an edge without, {mine:.0f} with"
    if theirs < STOP_MS and mine < STOP_MS:
        return {"fires": False, "without": theirs, "with": mine,
                "line": f"{label}: no stop with or without it ({shown}): not what removes them"}
    matters = theirs >= STOP_MS and theirs >= RATIO * mine and any(
        evidence(stop) for stop in stops if stop["edge"] in set(others))
    return {"fires": matters, "without": theirs, "with": mine,
            "line": f"{label}: {shown}: " + ("MATTERS" if matters else "no effect shown")}


def rule_arena(edges: dict, arms: dict, names: list[str]) -> dict:
    """H3: the arena warm-up, in the first sessions of a process."""
    without = [name for name in names if name in set(arms.get("no_arena_warm") or [])]
    found = arm_rule("H3 (arena warm-up, first sessions of a process)",
                     all_stops(edges, FIRST_OF_PROCESS),
                     [name for name in names if name not in set(without)], without,
                     lambda stop: stop["class"] == "blocked in our code")
    found["line"] += ("; " + ", ".join(without) + " without it: its region is the other "
                      "variable") if without else ""
    return found


def rule_touch(edges: dict, arms: dict, names: list[str],
               checks: dict[str, dict] | None = None) -> dict:
    """H4: first touch of the guest's memory, in the first session. A
    touched edge whose touch is not shown to have held and been given back
    cleanly (`checks`, from touch_checks: no release mark, an unfinished
    touch, swap or refaults during it) is in neither arm. Evidence on the
    untouched edges: a stop not run, or blocked on faults in the pass or on
    reclaim. Free page reporting on a touched edge is stated: the host may
    take the given-back pages again before the session."""
    checks = checks or {}
    armed = [name for name in names if name in set(arms.get("touched") or [])]
    left_out = {name: checks[name]["excluded"] for name in armed
                if (checks.get(name) or {}).get("excluded")}
    touched = [name for name in armed if name not in left_out]
    found = arm_rule("H4 (first touch, first session)", all_stops(edges, ("first",)), touched,
                     [name for name in names if name not in set(armed)],
                     lambda stop: stop["class"] in ("not run", "kernel")
                     or stop.get("wait_kind") in ("faults", "reclaim"))
    if found["fires"]:
        found["line"] += ": node_warm (the whole guest) defaults true and is part of readiness"
    if left_out:
        found["line"] += "; left out: " + "; ".join(f"{name} ({why})" for name, why
                                                     in sorted(left_out.items()))
    reporting = sorted(name for name in touched if (checks.get(name) or {}).get("reporting"))
    if reporting:
        found["line"] += (f"; free page reporting on {', '.join(reporting)}: the host may take "
                          "the given-back pages again before the session, so there the arm "
                          "tests the guest's own first touch, not the host's backing (H10)")
    unknown = sorted(name for name in touched if (checks.get(name) or {}).get("reporting") is None)
    if unknown and checks:
        found["line"] += f"; free page reporting not known on {', '.join(unknown)}"
    found["left_out"] = left_out
    found["reporting"] = reporting
    return found


# A touch is held cleanly when none of these grew from its start to the end
# of the first session (the kernel swapped or evicted for it).
TOUCH_PRESSURE_KEYS = ("pswpin", "pswpout", "workingset_refault_anon", "workingset_refault_file")
# virtio-balloon's free page reporting (VIRTIO_BALLOON_F_REPORTING, bit 5 of
# the device's features file, a string of 0 and 1 by bit).
BALLOON_REPORTING_BIT = 5


def balloon_reporting(system: str | None) -> bool | None:
    """Whether the guest's balloon reports free pages to the host, from the
    `balloon-features:` line of the node's system.txt (None: not known)."""
    for line in (system or "").splitlines():
        if line.startswith("balloon-features:"):
            bits = line.split(":", 1)[1].strip()
            if len(bits) > BALLOON_REPORTING_BIT and set(bits) <= {"0", "1"}:
                return bits[BALLOON_REPORTING_BIT] == "1"
    return None


def touch_checks(run: Run) -> dict[str, dict]:
    """Per touched edge: {"excluded": why or None, "reporting": bool|None}.
    Excluded: its touch-marks.txt has no `released` line or says
    `unfinished`; or its sampler shows swap or refaults growing from the
    touch's start to the end of the first session; or no sampler lines
    span that time."""
    touch = run.record.get("touch") or {}
    first = [name for name, role in run.roles().items() if role == "first"]
    span = run.period(first[0]) if first else None
    out = {}
    for name in (run.arms().get("touched") or []):
        why = None
        marks = run.node_file(name, "touch-marks.txt") or ""
        released = [line.split() for line in marks.splitlines() if line.startswith("released ")]
        if not released:
            why = "no release mark: it may have held its memory through the session"
        elif released[-1][-1] != "finished":
            why = "the touch had not finished when it was given back"
        elif touch.get("from_unix") is None or span is None:
            why = "no touch time or first session in the record"
        else:
            pair = sr.around(run.sampler(name), int(touch["from_unix"] * 1e6), span[1])
            snapshots = (vmstat_of(run.node_file(name, "vmstat-touch.txt")),
                         vmstat_of(run.node_file(name, "vmstat-after-first.txt")))
            if pair is None and None not in snapshots:
                # The referee's samplers take no swap counters: the hunt's
                # own /proc/vmstat at the touch and after the first session.
                pair = tuple({"vm": snapshot} for snapshot in snapshots)
            if pair is None:
                why = ("no sampler line (or vmstat snapshot) before the touch and after the "
                       "first session")
            else:
                (vm_a, vm_b) = ((line.get("vm") or {}) for line in pair)
                grown = {key: int(vm_b.get(key, 0)) - int(vm_a.get(key, 0))
                         for key in TOUCH_PRESSURE_KEYS}
                grown = {key: value for key, value in grown.items() if value > 0}
                if grown:
                    why = "the kernel swapped or evicted for it: " + ", ".join(
                        f"{key} +{value}" for key, value in sorted(grown.items()))
        out[name] = {"excluded": why,
                     "reporting": balloon_reporting(run.node_file(name, "system.txt"))}
    return out


def vmstat_of(text: str | None) -> dict | None:
    """A /proc/vmstat snapshot as {key: value}; None without one."""
    if not text:
        return None
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            out[parts[0]] = int(parts[1])
    return out or None


def rule_twins(edges: dict) -> dict:
    """H5: two edges of a region stopped at once, and parts late on both."""
    together = [(name, item) for name, item in edges.items() for item in item["twins"]]
    late = {name: sum(figure.get("late_on_both") or 0 for pairs in item["first_of_two"].values()
                      for figure in pairs.values()) for name, item in edges.items()}
    late = {name: count for name, count in late.items() if count}
    line = "H5 (twins): " + (
        "TWINS STOP TOGETHER: " + "; ".join(
            f"{name} {'+'.join(item['edges'])} {item['both_ms']:.0f} ms both"
            for name, item in together)
        if together else "no two edges of a region stopped at once: two paths hide every stop")
    if late:
        line += "; parts late on both: " + ", ".join(f"{name} {count}" for name, count
                                                      in sorted(late.items()))
    return {"fires": bool(together), "together": [item for _name, item in together],
            "late_on_both": late, "line": line}


def rule_pcm_first(edges: dict) -> dict:
    """H6: after the restart, the PCM session first, then Opus."""
    pcm = all_stops(edges, ("repeat-first",))
    opus = all_stops(edges, ("repeat-second",))
    if not any(item["role"] == "repeat-first" for item in edges.values()):
        return {"fires": False, "line": "H6 (PCM first): no repeat in the record"}
    shown = f"PCM {len(pcm)} stops, Opus after it {len(opus)}"
    verdict = ("the first session of a process stops whatever its codec: time since start"
               if pcm else
               "only the Opus session stops: the Opus stream (3001) and its compact path"
               if opus else
               "a restarted process on a guest that has run a session is clean: the first "
               "session's stops are the guest's or the instance's, not the process's")
    return {"fires": bool(pcm or opus), "pcm": len(pcm), "opus": len(opus),
            "line": f"H6 (PCM first): {shown}: {verdict}"}


def rule_churn(edges: dict, rounds: dict[str, list[int]]) -> dict:
    """H7: the churn session's stops at its rounds' joins."""
    churn = [(name, item) for name, item in edges.items() if item["role"] == "churn"]
    if not churn:
        return {"fires": False, "line": "H7 (churn): no churn session in the record"}
    at_join, rest = [], []
    for name, item in churn:
        starts = rounds.get(name) or []
        for stop in item["stops"]:
            near = any(0 <= stop["from_us"] - start <= JOIN_WINDOW_S * 1e6 for start in starts)
            (at_join if near else rest).append(stop)
    fires = bool(at_join) and len(at_join) >= len(rest)
    line = (f"H7 (churn): {len(at_join)} stops within {JOIN_WINDOW_S:g} s of a round's joins, "
            f"{len(rest)} elsewhere: " + ("JOINS TRIGGER STOPS" if fires else
                                          "churn is clean" if not at_join and not rest
                                          else "not at the joins"))
    return {"fires": fires, "at_join": len(at_join), "elsewhere": len(rest), "line": line}


def nic_offloads(text: str) -> dict[str, str]:
    """{"<interface> <offload>": "on"|"off"} of NIC_OFFLOADS from a node's
    NIC file (the referee's NIC_INFO: "== eth0 offloads", then ethtool -k)."""
    out, interface, section = {}, None, None
    for line in (text or "").splitlines():
        if line.startswith("== "):
            words = line[3:].split()
            interface, section = (words[0], words[1]) if len(words) >= 2 else (None, None)
            continue
        if section != "offloads" or ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        if key in NIC_OFFLOADS:
            out[f"{interface} {key}"] = value.split()[0] if value.split() else ""
    return out


def node_stops(run: Run, name: str, start: int, end: int) -> list[dict]:
    """A node's stops of STOP_MS or more inside [start, end] by its
    heartbeats (sr.heartbeat_stops), each with its class: "kernel" when a
    soft lockup of its kernel log overlaps it (where: what the kernel was
    doing), else "not run" ("whole guest" when every vCPU's heartbeat was
    late at once); and its sys sampler's gaps of SAMPLER_GAP_MS or more.
    No thread's system time is read (a vCPU not run charges its pause to
    the thread current on it)."""
    beats = run.heartbeat(name)
    cpus = sorted({beat["cpu"] for beat in beats})
    lockups = run.lockups(name)
    out = []
    for stop in sr.heartbeat_stops(beats, STOP_MS):
        if not (start <= stop["from_us"] < end):
            continue
        hit = [item for item in lockups
               if sr.overlap((item["from_us"], item["t_us"]), (stop["from_us"], stop["to_us"])) > 0]
        out.append({"node": name, **stop, "whole_guest": len(cpus) > 1 and stop["cpus"] == cpus,
                    "class": "kernel" if hit else "not run",
                    "host": run.host_at(name, stop["from_us"]),
                    "soft_lockups": [sr.lockup_text(item) for item in hit]})
    seconds = run.sys_seconds(name)
    import bisect
    lo = bisect.bisect_left(seconds, start - 5_000_000)
    hi = bisect.bisect_right(seconds, end)
    seconds = seconds[lo:hi]
    for a, b in zip(seconds, seconds[1:]):
        if (b - a) / 1000 >= SAMPLER_GAP_MS and a < end and b > start:
            out.append({"node": name, "from_us": a, "to_us": b, "ms": round((b - a) / 1000, 3),
                        "cpus": cpus, "whole_guest": True, "class": "sampler gap",
                        "host": run.host_at(name, a), "soft_lockups": []})
    return out


def video_wholeness(run: Run, session: str) -> dict[str, dict]:
    """Each view host's 4K probe of a rung (sessions/<S>/<host>/v.json, the
    referee's ntcmp report): parts missing, stalls and whether whole."""
    out = {}
    for path in sorted((run.dir / "sessions" / session).glob("*/v.json")):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            out[path.parent.name] = {"whole": None}
            continue
        readers = report.get("readers") or [report]
        missing = sum(int(reader.get("parts_missing") or 0) for reader in readers)
        stalls = sum(len(reader.get("stall_events") or []) for reader in readers)
        out[path.parent.name] = {"parts_missing": missing, "stalls": stalls,
                                 "whole": missing == 0 and stalls == 0}
    return out


def matrix_table(run: Run) -> dict:
    """Per combination of the GSO/GRO matrix (its periods: the first audio
    session and each 4K rung): every mesh node's heartbeat stops and sys
    sampler gaps, by region; the audio session's edge commit stops are in
    the edges table; and each view host's 4K wholeness."""
    out: dict[str, dict] = {}
    sites = {name: (run.nodes.get(name) or {}).get("site") for name in run.mesh()}
    for item in run.periods:
        combination = item.get("combination")
        if not combination or item.get("kind") not in ("session", "video"):
            continue
        entry = out.setdefault(combination["name"], {
            "gso": combination["gso"], "gro": combination["gro"],
            "realtime": combination.get("realtime", True), "parts": {}})
        part = "audio" if item["kind"] == "session" else item.get("rung") or item["name"]
        start, end = int(item["from_unix"] * 1e6), int(item["to_unix"] * 1e6)
        stops = [stop for name in run.mesh() if run.has_referee(name)
                 for stop in node_stops(run, name, start, end)]
        by_site: dict[str, dict] = {}
        for stop in stops:
            slot = by_site.setdefault(sites.get(stop["node"]) or "-", {"n": 0, "ms": 0.0})
            slot["n"] += 1
            slot["ms"] = round(slot["ms"] + stop["ms"], 3)
        entry["parts"][part] = {"session": item["name"], "stops": stops, "by_site": by_site,
                                **({"video": video_wholeness(run, item["name"])}
                                   if item["kind"] == "video" else {})}
    return dict(sorted(out.items()))


def rule_gso_gro(matrix: dict, nics: dict[str, dict] | None = None) -> dict:
    """H8: which of the senders' GSO and the receivers' GRO makes the
    stops (the wave 9 4K run). A combination "stops" when any mesh node
    has a heartbeat stop or a sampler gap of 100 ms or more in any of its
    parts. GSO is the cause when both combinations with it on stop and
    both without it do not; GRO the same; both when only the one with
    both off is clean; neither when every combination stops. A
    combination with the class threads not real-time (M5) is left out of
    that verdict and compared with the defaults on its own."""
    if not matrix:
        return {"fires": False, "line": "H8 (GSO and GRO): no GSO/GRO matrix in the record"}

    def stopped(entry: dict) -> bool:
        return any(part["stops"] for part in entry["parts"].values())

    realtime_off = {name: entry for name, entry in matrix.items()
                    if entry.get("realtime", True) is False}
    matrix = {name: entry for name, entry in matrix.items() if name not in realtime_off}
    state = {(entry["gso"], entry["gro"]): stopped(entry) for entry in matrix.values()}
    shown = "; ".join(
        f"{name} GSO {'on' if entry['gso'] else 'off'} GRO {'on' if entry['gro'] else 'off'}: "
        + (", ".join(f"{part} " + (", ".join(f"{site} {slot['n']} ({slot['ms']:.0f} ms)"
                                              for site, slot in sorted(item["by_site"].items()))
                                   or "clean")
                     + ("" if "video" not in item else " [4K whole at "
                        + (", ".join(host for host, figure in sorted(item["video"].items())
                                     if figure.get("whole")) or "none") + "]")
                     for part, item in entry["parts"].items()))
        for name, entry in matrix.items())
    if len(state) < 4:
        verdict = "not every combination ran: no verdict"
    elif not any(state.values()):
        verdict = "no stop in any combination: not reproduced (one repeat, then stop)"
    elif all(state.values()):
        verdict = "EVERY COMBINATION STOPS: neither GSO nor GRO; the platform or another cause"
    elif state[(True, True)] and state[(True, False)] and not state[(False, True)] \
            and not state[(False, False)]:
        verdict = "SENDER GSO: the stops come with GSO on and go with it off"
    elif state[(True, True)] and state[(False, True)] and not state[(True, False)] \
            and not state[(False, False)]:
        verdict = "RECEIVER GRO: the stops come with GRO on and go with it off"
    elif not state[(False, False)] and all(value for key, value in state.items()
                                            if key != (False, False)):
        verdict = "BOTH: only with GSO and GRO both off are there no stops"
    elif not state[(True, True)]:
        verdict = "the defaults are clean: " + ", ".join(
            f"GSO {'on' if gso else 'off'} GRO {'on' if gro else 'off'} stops"
            for (gso, gro), value in sorted(state.items(), reverse=True) if value)
    else:
        verdict = "no single cause: " + ", ".join(
            f"GSO {'on' if gso else 'off'} GRO {'on' if gro else 'off'} "
            + ("stops" if value else "clean") for (gso, gro), value
            in sorted(state.items(), reverse=True))
    kinds = sorted({stop["class"] for entry in matrix.values()
                    for part in entry["parts"].values() for stop in part["stops"]})
    line = f"H8 (GSO and GRO): {verdict}" + (f" ({', '.join(kinds)})" if kinds else "") + \
        f": {shown}"
    realtime = None
    for name, entry in realtime_off.items():
        defaults = state.get((entry["gso"], entry["gro"]))
        off = stopped(entry)
        realtime = (
            "NOT REAL-TIME IS CLEAN: the stops go with the class threads' FIFO pinning"
            if defaults and not off else
            "not real-time stops too: not the class threads' scheduling" if off else
            "not real-time clean, as the defaults" if defaults is False else
            "not real-time clean (no defaults to compare)")
        line += (f"; {name} ({realtime}): "
                 + ", ".join(f"{part} " + (", ".join(
                     f"{site} {slot['n']} ({slot['ms']:.0f} ms)"
                     for site, slot in sorted(item["by_site"].items())) or "clean")
                     for part, item in entry["parts"].items()))
    differ = nic_differences(nics or {})
    if differ:
        line += "; NIC offloads differ: " + "; ".join(differ)
    return {"fires": any(state.values()), "state": {f"gso={k[0]},gro={k[1]}": v
                                                     for k, v in state.items()},
            "verdict": verdict, "line": line,
            **({"realtime_off": realtime} if realtime else {})}


def nic_differences(nics: dict[str, dict]) -> list[str]:
    """The nodes whose offloads differ from most nodes', by offload."""
    out = []
    keys = sorted({key for offloads in nics.values() for key in offloads})
    for key in keys:
        values = {name: offloads.get(key) for name, offloads in nics.items() if key in offloads}
        if len(set(values.values())) < 2:
            continue
        common = max(set(values.values()), key=list(values.values()).count)
        odd = sorted(name for name, value in values.items() if value != common)
        out.append(f"{key} {common} but " + ", ".join(f"{name} {values[name]}" for name in odd))
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

def _fork_context():
    """Workers forked from this process: they find this module (loaded by
    path, as the tests and the hunt driver load it) under its own name."""
    import multiprocessing
    import types
    module = sys.modules.get(__name__)
    if module is None or getattr(module, "_session_tables", None) is not _session_tables:
        module = types.ModuleType(__name__)
        module._session_tables = _session_tables
        module._null_one = _null_one
        sys.modules[__name__] = module
    return multiprocessing.get_context("fork")


def _session_tables(item: tuple[str, str, dict]) -> tuple[dict, dict]:
    """One session's edges and silences tables (a worker of build); the
    run's unpinned nodes come from the parent (read once)."""
    directory, name, unpinned = item
    run = Run(directory)
    run._unpinned = dict(unpinned)
    return edges_table(run, [name]), silence_table(run, [name])


def build(run: Run, lengths=(8, 30), null_window=None, null: bool = True,
          jobs: int = 1) -> dict:
    """The report. `null`: the null of every probe (section 5, the
    referee's own scorer over every window of every probe: minutes on a
    large run; `--null` asks for it, with `--jobs` processes)."""
    inside = hops_inside_sessions(run)
    if inside:
        raise Refused("; ".join(inside))
    nulls = null_table(run, lengths, null_window, jobs) if null else {}
    # One session at a time: its probes are read once for the edges and the
    # silences, then let go; with `jobs`, the sessions in processes of their
    # own (each reads the node records it needs).
    edges, silence = {}, {}
    names = [name for name, _start, _end in run.periods_of("session")]
    if jobs > 1 and len(names) > 1:
        import concurrent.futures
        with concurrent.futures.ProcessPoolExecutor(min(jobs, len(names)),
                                                    mp_context=_fork_context()) as pool:
            for found_edges, found_silence in pool.map(
                    _session_tables, [(str(run.dir), name, run.unpinned()) for name in names]):
                edges.update(found_edges)
                silence.update(found_silence)
        edges = {name: edges[name] for name in names if name in edges}
        silence = {name: silence[name] for name in names if name in silence}
    else:
        for name in names:
            edges.update(edges_table(run, [name]))
            silence.update(silence_table(run, [name]))
    source = source_table(run)
    matrix = matrix_table(run)
    names = run.edges()
    arms = run.arms()
    lockups = lockup_table(run)
    rules = {"H1": rule_fix(edges, names),
             "H2": rule_classes(edges, {name: run.host_uuid(name) for name in names},
                                run.record.get("replaced") or []),
             "H3": rule_arena(edges, arms, names),
             "H4": rule_touch(edges, arms, names, touch_checks(run)),
             "H5": rule_twins(edges), "H6": rule_pcm_first(edges),
             "H7": rule_churn(edges, {name: run.round_starts(name) for name, item
                                      in edges.items() if item["role"] == "churn"}),
             "H8": rule_gso_gro(matrix, {name: run.nic(name) for name in run.mesh()})}
    rules["H2"]["line"] += f" | {lockup_line(lockups)}"
    rules["H2"]["lockups"] = {name: len(items) for name, items in lockups.items()}
    return {"schema": "needletail.hunt-report.v2", "run_id": run.record.get("run_id"),
            "periods": node_periods(run), "unpinned": run.unpinned(), "hosts": cohosting(run),
            "lockups": lockups,
            "edges": edges, "matrix": matrix, "silences": silence, "null": nulls,
            "null_computed": null, "source": source,
            "rules": rules, "acceptance": acceptance(nulls, source)}


def fmt(value, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def stop_line(stop: dict) -> str:
    detail = stop.get("where") or stop.get("wait") or ""
    host = f" on host {short_host(stop['host'])}" if stop.get("host") else ""
    return (f"{stop['from_us'] / 1e6:.3f} -> {stop['to_us'] / 1e6:.3f} ({stop['ms']:.0f} ms)"
            f"{host}: {stop['class'].upper()}" + (f", {detail}" if detail else "")
            + f" [{stop['why']}]")


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
    lines.append("  " + lockup_line(report.get("lockups") or {}))
    lines += ["", "3. the edges' commit stops of 100 ms or more, each classified; the twins"]
    for session, item in report["edges"].items():
        lines.append(f"  {session} ({item['role']}): edges seen {', '.join(item['seen']) or 'none'}"
                     "; UDP clients " + (", ".join(
                         f"{edge} {count}" for edge, count
                         in sorted((item.get("udp_clients") or {}).items())) or "none")
                     + "; stops of 20 ms or more " + (", ".join(
                         f"{edge} {count}" for edge, count in sorted(item["over_20ms"].items())
                         if count) or "none"))
        for slot in item.get("shared") or []:
            lines.append(f"    shared by every edge measured (upstream, not an edge's stop): "
                         f"{slot['from_us'] / 1e6:.3f} ({slot['ms']:.0f} ms, "
                         f"{len(slot['edges'])} edges)")
        for stop in item["stops"]:
            lines.append(f"    {stop['edge']:<12} {stop_line(stop)}")
        for twin in item["twins"]:
            lines.append(f"    twins {'+'.join(twin['edges'])} stopped at once: "
                         f"{twin['from_us'] / 1e6:.3f} -> {twin['to_us'] / 1e6:.3f}, "
                         f"{twin['both_ms']:.0f} ms both")
        for region, pairs in item["first_of_two"].items():
            if not pairs:
                lines.append(f"    {region}: one edge seen, no pair")
            for pair, figure in pairs.items():
                lines.append(f"    {region} {pair}: {figure['parts']} parts, publish to first "
                             f"commit p50 {fmt(figure.get('p50_ms'))} p99 "
                             f"{fmt(figure.get('p99_ms'))} max {fmt(figure.get('max_ms'))} ms, "
                             f"late on both {figure.get('late_on_both')}")
    lines += ["", "4. silences at the listeners, joined"]
    for session, probes in report["silences"].items():
        for probe, item in probes.items():
            causes = ", ".join(f"{cause} {slot['n']} ({slot['ms']:.1f} ms)"
                               for cause, slot in sorted(item["by_cause"].items()))
            lines.append(f"  {session} {probe} ({item['edge']}): {item['silences']} silences: "
                         f"{causes or 'none'}")
    lines += ["", "5. the null of every probe"]
    if not report.get("null_computed", True):
        lines.append("  not computed (--null computes it: the referee's scorer over every "
                     "window of every probe, minutes on a large run; --jobs N in parallel)")
    null_window = load_null_window()
    for session, probes in report["null"].items():
        text = null_window.format_result({"probes": probes})
        lines += [f"  {session}: {line}" for line in text.splitlines()]
    lines += ["", "6. the source side (published minus ideal; episodes over the median)"]
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
    lines += ["", "the GSO/GRO matrix: the mesh nodes' stops of 100 ms or more (heartbeats) "
                  "and sampler gaps"]
    for name, entry in (report.get("matrix") or {}).items():
        for part, item in entry["parts"].items():
            lines.append(f"  {name} GSO {'on' if entry['gso'] else 'off'} GRO "
                         f"{'on' if entry['gro'] else 'off'} {part}: "
                         + (", ".join(f"{stop['node']} {stop['ms']:.0f} ms {stop['class']}"
                                      + (" whole guest" if stop.get("whole_guest") else "")
                                      for stop in item["stops"]) or "no stop")
                         + ("" if "video" not in item else "; 4K " + (", ".join(
                             f"{host} {'whole' if figure.get('whole') else 'NOT whole'}"
                             for host, figure in sorted(item["video"].items())) or "no probe")))
    lines += ["", "7. rules (fixed before the run)"]
    lines += [f"  {rule['line']}" for rule in report["rules"].values()]
    for edge, verdict in sorted(report["rules"]["H1"].get("verdicts", {}).items()):
        lines.append(f"    first session, {edge}: {verdict}")
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
    parser.add_argument("--null", action="store_true",
                        help="also the null of every probe (section 5 and 3.1's steady state): "
                             "the referee's scorer over every window, minutes on a large run")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1),
                        help="processes: the sessions' tables, one session each, and --null's "
                             "probes (default the CPUs, at most 8; 1: none)")
    args = parser.parse_args()
    if not (pathlib.Path(args.dir) / "hunt.json").is_file():
        sys.exit(f"{args.dir}/hunt.json is missing: not a hunt run's directory")
    try:
        report = build(Run(args.dir), [int(item) for item in args.lengths.split(",") if item],
                       null=args.null, jobs=args.jobs)
    except Refused as refused:
        sys.exit(f"no report of {args.dir}: {refused}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(format_report(report))


if __name__ == "__main__":
    main()
