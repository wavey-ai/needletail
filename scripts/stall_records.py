"""Stall records: what a node says about the times it was not run.

Two sources, one line format (docs/plans/resilient-mesh/D-hunt.md, 2.1 and
2.2), NDJSON, oldest first:

* a service's stall journal, `GET /api/stalls?since_us=T` on av-mesh (port
  19444) and av-contrib (19443);
* `needletail-nodecheck watch --out PATH`, the same tick threads in a
  process of its own.

An event line:
  {"node": "lon-edge-1", "t_us": 1795381726393150, "kind": "tick",
   "thread": "tick-1", "cpu": 1, "dur_us": 673004, "span_us": 673004, "n": 1,
   "run_delay_us": 212, "cause": "stopped", "scope": "all"}
`t_us` is the wall clock when the event was detected, so the time it covers
is [t_us - dur_us, t_us] (a merged event, n > 1: [t_us - span_us, t_us]).
A line of kind "sec" is one CPU's second: the growth of its user, system,
irq, softirq and steal ticks, and in a `watch` file its lateness figures
and, on CPU 0, the growth of the kernel's memory counters.

The measure of the hunt is F: the milliseconds a node was stopped per
minute, where "stopped" is the summed length of its `stopped` ticks of
20 ms or more, averaged over its CPUs (the same sum as `stall.stopped_ms`
of /api/mesh/local).

The service computes `cause` and `scope` when its journal is read. A
`watch` file may carry raw ticks; then they are computed here by the same
rule: a tick is `stopped` when its run delay is under a tenth of its
lateness (the thread slept and its timer came late: the CPU was not run),
else `starved`; its scope is `all` when every other CPU has a stopped tick
over half of it or more.

The hunt's sampler (scripts/hunt-sampler.sh) adds a line a second of one
service's class thread; `classify` reads a stop of that thread from it
(the rules are at the end of this module).

This module is imported by scripts/linode-stall-hunt.py (which edge to
replace), scripts/compare/hunt-report.py and scripts/compare/first-session.py
(the reports) and by any collector that wants a node's journal for a phase:
`fetch_stalls` is that one call.
"""

from __future__ import annotations

import json
import pathlib
import ssl
import urllib.request

MESH_HTTP_PORT = 19444
CONTRIB_HTTP_PORT = 19443
# A stop is counted from this length (D-hunt.md 2.1: the lab's measure).
STOP_MS = 20.0
# Clock ticks a second of /proc/stat (USER_HZ), the unit of a sec line.
TICKS_PER_SECOND = 100


def parse_ndjson(text: str) -> list[dict]:
    """The JSON objects of NDJSON text; a line that is not one (a record cut
    off by the end of a run) is left out."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def read_ndjson(path) -> list[dict]:
    path = pathlib.Path(path)
    if not path.is_file():
        return []
    return parse_ndjson(path.read_text(errors="replace"))


def line_key(item: dict) -> tuple:
    """What makes two lines one: an event that grew by a merge is written
    again with the same kind, thread and start (`t_us - span_us`), and a
    `sec` line is its CPU's at its time."""
    if item.get("kind") == "sec":
        return ("sec", item.get("node"), item.get("cpu"), item.get("t_us", item.get("unix_s")))
    span = int(item.get("span_us") or item.get("dur_us") or 0)
    return (item.get("kind"), item.get("node"), item.get("thread"), int(item.get("t_us") or 0) - span)


def dedupe(items: list[dict]) -> list[dict]:
    """A stall record as it holds: of two lines of one event the later
    replaces the earlier (a poll with `since_us` sees a merged event again,
    grown, and so does a `watch` file); oldest first."""
    kept: dict[tuple, dict] = {}
    for item in items:
        kept[line_key(item)] = item
    return sorted(kept.values(), key=lambda item: at_us(item) or 0)


def read_stalls(path) -> list[dict]:
    """A stall journal or `watch` file, each event once."""
    return dedupe(read_ndjson(path))


def fetch(address: str, port: int, path: str, timeout: float = 5.0) -> bytes:
    """GET https://address:port/path from a lab node. The lab's certificate
    names mesh.needletail.test, not the address, and the lab network is the
    lab firewall's: the name is not checked (as the autoscaler's polls)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(f"https://{address}:{port}{path}", timeout=timeout,
                                context=context) as response:
        return response.read()


def fetch_stalls(address: str, since_us: int = 0, port: int = MESH_HTTP_PORT,
                 timeout: float = 5.0) -> list[dict]:
    """A node's stall journal from `since_us` on: one call."""
    body = fetch(address, port, f"/api/stalls?since_us={int(since_us)}", timeout)
    return parse_ndjson(body.decode(errors="replace"))


def at_us(item: dict) -> int | None:
    """When a line was written: `t_us`, or a sec line's `unix_s`."""
    if item.get("t_us") is not None:
        return int(item["t_us"])
    if item.get("unix_s") is not None:
        return int(float(item["unix_s"]) * 1_000_000)
    return None


def interval(event: dict) -> tuple[int, int]:
    """The time an event covers, (start_us, end_us)."""
    end = int(event["t_us"])
    length = int(event.get("dur_us") or 0)
    if int(event.get("n") or 1) > 1:
        length = max(length, int(event.get("span_us") or 0))
    return end - length, end


def overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def tick_cause(event: dict) -> str:
    """`stopped` or `starved` of a tick event: the journal's own word, else
    the rule of 2.1."""
    if event.get("cause") in ("stopped", "starved"):
        return event["cause"]
    late = int(event.get("dur_us") or 0)
    return "stopped" if int(event.get("run_delay_us") or 0) * 10 < late else "starved"


def ticks(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("kind") == "tick" and event.get("t_us")]


def cpus_of(events: list[dict]) -> list[int]:
    """The CPUs a record speaks of (tick events and sec lines)."""
    return sorted({int(item["cpu"]) for item in events
                   if item.get("kind") in ("tick", "sec") and item.get("cpu") is not None})


def stopped_ticks(events: list[dict], min_ms: float = STOP_MS) -> list[dict]:
    return [event for event in ticks(events)
            if tick_cause(event) == "stopped" and int(event.get("dur_us") or 0) >= min_ms * 1000]


def stops(events: list[dict], min_ms: float = STOP_MS) -> list[tuple[int, int]]:
    """A node's stops: the union of the intervals of its stopped ticks of
    `min_ms` or more, over all its CPUs (a stop of the whole node is one
    tick per CPU, and one stop). [(start_us, end_us)], oldest first."""
    merged: list[list[int]] = []
    for begin, end in sorted(interval(event) for event in stopped_ticks(events, min_ms)):
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return [(begin, end) for begin, end in merged]


def tick_scope(event: dict, events: list[dict], cpus: list[int] | None = None) -> str:
    """`all` when every other CPU has a stopped tick over half of this one
    or more, else `one`: the journal's own word, else the rule of 2.1."""
    if event.get("scope") in ("all", "one"):
        return event["scope"]
    cpus = cpus if cpus is not None else cpus_of(events)
    mine = interval(event)
    half = (mine[1] - mine[0]) / 2
    for cpu in cpus:
        if cpu == event.get("cpu"):
            continue
        covered = sum(overlap(mine, interval(other)) for other in ticks(events)
                      if other.get("cpu") == cpu and tick_cause(other) == "stopped")
        if covered < half:
            return "one"
    return "all"


def stopped_ms(events: list[dict], since_us: int | None = None, until_us: int | None = None,
               cpus: int | None = None, min_ms: float = STOP_MS) -> float:
    """Milliseconds stopped in [since_us, until_us): the summed length of
    the stopped ticks of `min_ms` or more that ended in it, averaged over
    the node's CPUs (`cpus`, default those the record names)."""
    count = cpus or len(cpus_of(events)) or 1
    total = 0
    for event in stopped_ticks(events, min_ms):
        end = int(event["t_us"])
        if (since_us is not None and end < since_us) or (until_us is not None and end >= until_us):
            continue
        total += int(event["dur_us"])
    return total / 1000 / count


def frozen_per_minute(events: list[dict], since_us: int, until_us: int,
                      cpus: int | None = None) -> float | None:
    """F: stopped ms per minute of [since_us, until_us). None for an empty
    period."""
    if until_us <= since_us:
        return None
    return stopped_ms(events, since_us, until_us, cpus) / ((until_us - since_us) / 60e6)


def stop_counts(events: list[dict], since_us: int | None = None,
                until_us: int | None = None) -> dict:
    """Stops by length, scope and cause in a period: {"stopped": {"over_20ms":
    {"all": n, "one": n}, "over_100ms": {...}}, "starved": {"over_20ms": n,
    "over_100ms": n}}, with the starved milliseconds."""
    cpus = cpus_of(events)
    out = {"stopped": {"over_20ms": {"all": 0, "one": 0}, "over_100ms": {"all": 0, "one": 0}},
           "starved": {"over_20ms": 0, "over_100ms": 0}, "starved_ms": 0.0}
    for event in ticks(events):
        end, length = int(event["t_us"]), int(event.get("dur_us") or 0)
        if (since_us is not None and end < since_us) or (until_us is not None and end >= until_us):
            continue
        if length < STOP_MS * 1000:
            continue
        if tick_cause(event) == "stopped":
            scope = tick_scope(event, events, cpus)
            out["stopped"]["over_20ms"][scope] += 1
            if length >= 100_000:
                out["stopped"]["over_100ms"][scope] += 1
        else:
            out["starved"]["over_20ms"] += 1
            out["starved_ms"] += length / 1000
            if length >= 100_000:
                out["starved"]["over_100ms"] += 1
    out["starved_ms"] = round(out["starved_ms"] / (len(cpus) or 1), 3)
    return out


def seconds(events: list[dict], since_us: int | None = None,
            until_us: int | None = None) -> list[dict]:
    """The sec lines of a period."""
    out = []
    for item in events:
        if item.get("kind") != "sec":
            continue
        when = at_us(item)
        if when is None or (since_us is not None and when < since_us) \
                or (until_us is not None and when >= until_us):
            continue
        out.append(item)
    return out


def line_ticks(item: dict) -> float:
    """The clock ticks a sec line covers (`span_us`, about a second)."""
    return int(item.get("span_us") or 1_000_000) / 1e6 * TICKS_PER_SECOND


def steal_permille(events: list[dict], since_us: int | None = None,
                   until_us: int | None = None) -> float | None:
    """Steal over a period, in thousandths of the CPU time its sec lines
    cover. None without sec lines."""
    lines = seconds(events, since_us, until_us)
    if not lines:
        return None
    steal = sum(int(item.get("steal") or 0) for item in lines)
    return round(1000 * steal / sum(line_ticks(item) for item in lines), 2)


def kernel_second(item: dict) -> bool:
    """Whether a CPU's second was mostly the guest kernel's: system and
    softirq ticks over half of the time the line covers."""
    busy = int(item.get("system") or 0) + int(item.get("softirq") or 0)
    return busy * 2 > line_ticks(item)


# ------------------------------------------------- what delayed the audio

# A side of a delay before publish (D-hunt.md 3.1 and E3), and the
# contributor's stall journal kinds that name one (2.8).
CONTRIBUTOR_SIDES = (("src_gap", "link"), ("rx_wait", "contributor read"),
                     ("inline_publish", "contributor publish"),
                     ("lane_send", "contributor publish"))


def sender_events(lines: list[dict], min_late_us: int = 500) -> list[dict]:
    """The AEP1 sender's lateness journal (2.7) as events: each `late`
    line covers [its deadline, the deadline + late_last_us], its side the
    pacer (it woke late) or the encode (the encode took the time)."""
    out = []
    for line in lines:
        if line.get("kind") != "late" or line.get("deadline_us") is None:
            continue
        late = int(line.get("late_last_us") or line.get("late_first_us") or 0)
        if int(line.get("late_first_us") or late) < min_late_us:
            continue
        woke, encode = int(line.get("woke_late_us") or 0), int(line.get("encode_us") or 0)
        out.append({"kind": "late", "t_us": int(line["deadline_us"]) + late, "dur_us": late,
                    "span_us": late, "n": 1, "epoch": line.get("epoch"),
                    "side": "pacer" if woke >= encode else "encode"})
    return out


def overlapping(events: list[dict], start_us: int, end_us: int) -> list[dict]:
    """The events whose interval meets [start_us, end_us]."""
    out = []
    for event in events:
        if event.get("kind") == "sec" or not event.get("t_us"):
            continue
        begin, end = interval(event)
        if begin <= end_us and end >= start_us:
            out.append(event)
    return out


def first_cause(start_us: int, end_us: int, sources: list[tuple[str, list[dict]]]):
    """The first of `sources` ([(side, events)], in the order of the join)
    with an event over [start_us, end_us]: (side, event), or None."""
    for side, events in sources:
        found = overlapping(events, start_us, end_us)
        if found:
            return side, max(found, key=lambda event: int(event.get("dur_us") or 0))
    return None


def source_sides(sender: list[dict], contributor: list[dict], source_watch: list[dict],
                 contributor_name: str = "contributor",
                 source_name: str = "source host") -> list[tuple[str, list[dict]]]:
    """The sides of a delay before publish, in the order they are asked:
    a node that was not run (listed apart), the sender's pacer or encode,
    the link (an epoch arrived late at the contributor with the sender on
    time), the contributor's read and its publish.

    A departure from the order of D-hunt.md 7.4.4 (the sender's `late`
    lines first): a stopped source host or contributor comes before the
    sender's lines. A host that is not run makes the pacer wake late too,
    so its `late` lines are the stop's effect, not a second cause; 3.1
    lists such episodes apart, and they can only be listed apart if they
    are asked for first."""
    sides: list[tuple[str, list[dict]]] = [
        (f"stopped ({source_name})", stopped_ticks(source_watch, 0)),
        (f"stopped ({contributor_name})", stopped_ticks(contributor, 0))]
    sides += [("pacer", [event for event in sender if event["side"] == "pacer"]),
              ("encode", [event for event in sender if event["side"] == "encode"])]
    for kind, side in CONTRIBUTOR_SIDES:
        sides.append((side, [event for event in contributor if event.get("kind") == kind]))
    return sides


def episodes(lateness_us: dict[int, int], above_us: int) -> list[dict]:
    """E3's episodes: maximal runs of consecutive parts whose lateness
    (published minus the part's ideal instant) is more than `above_us`
    over the median. [{"first", "last", "parts", "excess_us"}]."""
    if not lateness_us:
        return []
    ordered = sorted(lateness_us.values())
    median = ordered[(len(ordered) - 1) // 2]
    out: list[dict] = []
    for part in sorted(lateness_us):
        excess = lateness_us[part] - median
        if excess <= above_us:
            continue
        if out and out[-1]["last"] == part - 1:
            out[-1]["last"] = part
            out[-1]["parts"] += 1
            out[-1]["excess_us"] = max(out[-1]["excess_us"], excess)
        else:
            out.append({"first": part, "last": part, "parts": 1, "excess_us": excess})
    return out


def attribute(found: list[dict], ideal_us, sides: list[tuple[str, list[dict]]],
              margin_us: int = 1000) -> dict:
    """Each episode's side: the first side with an event over the episode
    ([ideal of its first part - margin, ideal of its last + its excess +
    margin]). {"episodes": [...with "side"], "by_side": {side: {"n", "ms"}},
    "unattributed": n}."""
    by_side: dict[str, dict] = {}
    for episode in found:
        start = ideal_us(episode["first"]) - margin_us
        end = ideal_us(episode["last"]) + episode["excess_us"] + margin_us
        cause = first_cause(start, end, sides)
        episode["side"] = cause[0] if cause else "unattributed"
        slot = by_side.setdefault(episode["side"], {"n": 0, "ms": 0.0})
        slot["n"] += 1
        slot["ms"] = round(slot["ms"] + episode["excess_us"] / 1000, 3)
    return {"episodes": found, "by_side": by_side,
            "unattributed": by_side.get("unattributed", {}).get("n", 0)}


# ------------------------------------------------------------ the sampler
#
# scripts/hunt-sampler.sh writes a line a second (its header says what is
# in it). Of a stop of the class thread (an edge's commit gap, or a second
# of the sampler itself) it tells "runnable but not run" from "blocked in
# our code", by rules fixed before the hunt's lab (D-hunt.md 7):
#  - not run: a stopped tick of the thread's CPU (the watch's or the
#    service's own) over half the stop or more (the vCPU was not run: the
#    hypervisor), or the thread's run delay grew by half the stop or more
#    (it was runnable and waited: the guest kernel, another real-time
#    thread on its CPU, or the hypervisor);
#  - blocked in our code: run delay flat (under a tenth of the stop) and a
#    pass of the class loop of half the stop or more (audio_loop_busy_max_us
#    rose to it, audio_loop_busy_over_50ms grew on a stop of 100 ms or
#    less, or the journal has a
#    class_pass of its own over it), with voluntary switches (a wait) or
#    without (a spin); the wait is named by the counter that rose to half
#    the stop (the fan-out hand-off, the compact path, a send, a repair),
#    else by page faults in the long pass itself (the journal's class_pass
#    carries the thread's faults since its last once-a-second reading; the
#    sampler's fault counter is that reading, up to a second stale, so it
#    is shown and never names the wait), else by the guest's reclaim, else
#    "not named by a counter";
#  - waited for input: run delay flat and no long pass (the thread slept
#    in poll and nothing came: upstream, or the kernel did not deliver);
#  - undetermined: anything between (run delay between a tenth and half).
# Run delay is the growth between the two sampler lines around the stop
# less the thread's own rate of it in the BASELINE_PAIRS seconds before
# and after (its median): the window is the stop plus up to two seconds.
# A node whose service could not pin every tick thread (tick_pinned below
# tick_threads) has untrusted scope: its journal's ticks count only on the
# class thread's own CPU, never by `scope: all` (D-hunt.md 4).

NOT_RUN_SHARE = 0.5
FLAT_SHARE = 0.1
BUSY_SHARE = 0.5
# av-mesh relay.rs: audio_loop_busy_over_50ms counts the passes over this.
OVER_50MS_US = 50_000
BASELINE_PAIRS = 10
NAMED_WAITS = (("fanout_dispatch_max_us", "the hand-off to the fan-out threads"),
               ("class_compact_max_us", "the compact path (its store and ring locks, the "
                                        "part's encode)"),
               ("class_send_max_us", "a send to the clients"),
               ("class_repair_max_us", "a window repair"))
RECLAIM_KEYS = ("allocstall_dma", "allocstall_dma32", "allocstall_normal", "allocstall_movable",
                "compact_stall", "pgscan_direct")
CLASSES = ("not run", "blocked in our code", "waited for input", "undetermined", "no sampler")


def run_delay_ms(rows: list, min_ms: float,
                 spans: list[tuple[int, int]] | None = None) -> float:
    """The milliseconds a class thread waited runnable, counted from the
    sampler's seconds in which its run delay grew by `min_ms` or more.
    `rows`: [t_us, pid, tid, run delay ns] (the same thread from one row to
    the next only). With `spans` [(from_us, to_us)], only the seconds that
    end inside one of them."""
    total = 0.0
    for before, after in zip(rows, rows[1:]):
        if before[1:3] != after[1:3] or before[3] is None or after[3] is None:
            continue
        if spans is not None and not any(start <= int(after[0]) <= end for start, end in spans):
            continue
        grown = (int(after[3]) - int(before[3])) / 1e6
        if grown >= min_ms:
            total += grown
    return total


def sampler_lines(items: list[dict]) -> list[dict]:
    """A sampler's lines, oldest first."""
    return sorted((item for item in items if item.get("t_us") is not None),
                  key=lambda item: int(item["t_us"]))


# The counters the hunt's sampler cuts from a node's own /api/mesh/local,
# by name, at their paths there (av-mesh MeshApiSnapshot, b446cb5; held to
# its source by test_stall_hunt.MeshContract): the fan-out's counters under
# client_udp.fast_lane (FastLaneSnapshot), the class thread's under
# relay_session.fast_plane (FastPlaneSnapshot), the stall journal's under
# stall, the deadline drops in the node's status (/api/node's ingest).
# Paths are read as they are: a peer's telemetry (relay_nodes[i].*, nodes,
# peers, ...) is never one of them. scripts/hunt-sampler.sh's MESH_PATHS is
# the same list (a test says so).
MESH_PATHS = {
    **{name: f"stall.{name}" for name in (
        "tick_threads", "tick_pinned", "tick_late_max_us", "stopped_ms", "starved_ms")},
    **{name: f"client_udp.fast_lane.{name}" for name in (
        "fanout_started", "fanout_policies", "fanout_cpus", "fanout_threads",
        "fanout_first_dispatch_faults", "fanout_dispatch_max_us", "fanout_shed",
        "fanout_waited", "class_plan_grows", "class_compact_max_us", "class_send_max_us",
        "class_repair_max_us", "class_arena_warm_bytes")},
    **{name: f"relay_session.fast_plane.{name}" for name in (
        "audio_loop_busy_max_us", "audio_loop_busy_over_5ms", "audio_loop_busy_over_50ms",
        "audio_loop_gap_max_us", "audio_thread_minflt", "audio_thread_majflt",
        "process_minflt", "process_majflt", "kernel_dropped", "tee_dropped")},
    **{name: f"status.ingest.{name}" for name in (
        "audio_deadline_drops", "video_deadline_drops")},
}


def mesh_value(line: dict | None, key: str) -> int | None:
    """A counter of the line's /api/mesh/local cut, by its name, read at
    its path (MESH_PATHS); None when the line does not have it."""
    value = ((line or {}).get("mesh") or {}).get(MESH_PATHS[key])
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def around(lines: list[dict], start_us: int, end_us: int) -> tuple[dict, dict] | None:
    """The last sampler line at or before `start_us` and the first at or
    after `end_us`."""
    pair = around_index(lines, start_us, end_us)
    return None if pair is None else (lines[pair[0]], lines[pair[1]])


def around_index(lines: list[dict], start_us: int, end_us: int) -> tuple[int, int] | None:
    """`around`, as indexes of `lines` (oldest first)."""
    before = [index for index, line in enumerate(lines) if int(line["t_us"]) <= start_us]
    after = [index for index, line in enumerate(lines) if int(line["t_us"]) >= end_us]
    if not before or not after:
        return None
    return before[-1], after[0]


def same_thread(before: dict, after: dict) -> bool:
    return bool(before.get("pid") == after.get("pid") and before.get("tid") == after.get("tid")
                and before.get("tid") is not None and before.get("schedstat")
                and after.get("schedstat"))


def run_delay_rate(lines: list[dict], first: int, last: int,
                   pairs: int = BASELINE_PAIRS) -> float | None:
    """The class thread's usual run delay, ms a second: the median growth
    a second over up to `pairs` consecutive lines before line `first` and
    after line `last` (the same thread). None without one."""
    rates = []
    starts = list(range(max(0, first - pairs), first)) \
        + list(range(last, min(len(lines) - 1, last + pairs)))
    for at in starts:
        before, after = lines[at], lines[at + 1]
        seconds = (int(after["t_us"]) - int(before["t_us"])) / 1e6
        if seconds <= 0 or not same_thread(before, after):
            continue
        rates.append((after["schedstat"][1] - before["schedstat"][1]) / 1e6 / seconds)
    if not rates:
        return None
    rates.sort()
    middle = len(rates) // 2
    return rates[middle] if len(rates) % 2 else (rates[middle - 1] + rates[middle]) / 2


def grew(before: dict, after: dict, key: str) -> int | None:
    a, b = mesh_value(before, key), mesh_value(after, key)
    return None if a is None or b is None else b - a


def deltas(before: dict, after: dict) -> dict:
    """What changed between two sampler lines."""
    out: dict = {"interval_ms": round((int(after["t_us"]) - int(before["t_us"])) / 1000, 3),
                 "cpu": after.get("cpu") if after.get("cpu") is not None else before.get("cpu")}
    same = same_thread(before, after)
    out["same_thread"] = same
    if same:
        out["run_ms"] = round((after["schedstat"][0] - before["schedstat"][0]) / 1e6, 3)
        out["run_delay_ms"] = round((after["schedstat"][1] - before["schedstat"][1]) / 1e6, 3)
        if before.get("csw") and after.get("csw"):
            out["voluntary"] = after["csw"][0] - before["csw"][0]
            out["involuntary"] = after["csw"][1] - before["csw"][1]
    vm_a, vm_b = before.get("vm") or {}, after.get("vm") or {}
    out["reclaim"] = sum(int(vm_b.get(key, 0)) - int(vm_a.get(key, 0)) for key in RECLAIM_KEYS)
    out["thp_faults"] = int(vm_b.get("thp_fault_alloc", 0)) - int(vm_a.get("thp_fault_alloc", 0))
    out["busy_max_us"] = [mesh_value(before, "audio_loop_busy_max_us"),
                          mesh_value(after, "audio_loop_busy_max_us")]
    out["over_50ms"] = grew(before, after, "audio_loop_busy_over_50ms")
    out["minflt"] = grew(before, after, "audio_thread_minflt")
    out["majflt"] = grew(before, after, "audio_thread_majflt")
    out["waits"] = {key: mesh_value(after, key) for key, _name in NAMED_WAITS
                    if (grew(before, after, key) or 0) > 0}
    return out


def realtime_neighbour(lines: tuple[dict, dict], cpu) -> str | None:
    """Another real-time thread of the service last seen on `cpu`."""
    for line in lines:
        for tid, policy, _prio, last_cpu, name in line.get("threads") or []:
            if tid != line.get("tid") and policy in ("FF", "RR") and last_cpu == cpu:
                return f"{name} ({tid})"
    return None


def classify(start_us: int, end_us: int, lines: list[dict], journal: list[dict] = (),
             watch: list[dict] = (), scope_trusted: bool = True) -> dict:
    """One stop of a class thread, [start_us, end_us], by the rules above:
    {"class", "why", "where" (not run) or "wait" and "wait_kind" (blocked:
    "counter", "faults", "reclaim" or "unnamed"), "figures"}.
    `scope_trusted` False: the service ran a tick thread it could not pin,
    so its journal's ticks count only on the class thread's own CPU."""
    length_ms = (end_us - start_us) / 1000
    index = around_index(lines, start_us, end_us)
    pair = None if index is None else (lines[index[0]], lines[index[1]])
    figures = deltas(*pair) if pair else {}
    cpu = figures.get("cpu")
    half_us = (end_us - start_us) / 2
    for source, events in (("watch", watch), ("journal", journal)):
        for event in stopped_ticks(list(events), 0):
            if source == "journal" and not scope_trusted:
                if cpu is None or event.get("cpu") != cpu:
                    continue
            elif cpu is not None and event.get("cpu") != cpu and event.get("scope") != "all":
                continue
            if overlap(interval(event), (start_us, end_us)) >= half_us:
                return {"class": "not run", "figures": figures,
                        "where": "the hypervisor: the vCPU was not run",
                        "why": f"a stopped tick of CPU {event.get('cpu')} ({source}) over "
                               f"{overlap(interval(event), (start_us, end_us)) / 1000:.0f} of "
                               f"{length_ms:.0f} ms"}
    if not pair:
        return {"class": "no sampler", "figures": figures,
                "why": "no sampler line before and after the stop"}
    if not figures["same_thread"]:
        return {"class": "undetermined", "figures": figures,
                "why": "the service or its class thread changed inside the stop"}
    rate = run_delay_rate(lines, *index)
    figures["run_delay_rate_ms_s"] = None if rate is None else round(rate, 3)
    delay = max(0.0, figures["run_delay_ms"] - (rate or 0.0) * figures["interval_ms"] / 1000)
    figures["run_delay_excess_ms"] = round(delay, 3)
    if delay >= NOT_RUN_SHARE * length_ms:
        neighbour = realtime_neighbour(pair, cpu)
        where = ("the guest kernel: direct reclaim or compaction" if figures["reclaim"] > 0
                 else f"another real-time thread on CPU {cpu}: {neighbour}" if neighbour
                 else "not named: the hypervisor or the guest kernel")
        return {"class": "not run", "figures": figures, "where": where,
                "why": f"run delay grew {delay:.0f} ms over a {length_ms:.0f} ms stop"}
    passes = [event for event in journal if event.get("kind") == "class_pass"
              and event.get("cause") == "own" and event.get("thread", "audio") == "audio"
              and overlap(interval(event), (start_us, end_us)) >= half_us]
    busy_before, busy_after = figures["busy_max_us"]
    # audio_loop_busy_over_50ms counts passes over 50 ms: a pass of half the
    # stop or more only for a stop of 100 ms or less.
    long_pass = bool(passes) or (
        length_ms <= 2 * OVER_50MS_US / 1000 and (figures["over_50ms"] or 0) > 0) or (
        busy_after is not None and busy_after >= BUSY_SHARE * length_ms * 1000
        and busy_after > (busy_before or 0))
    if delay < FLAT_SHARE * length_ms and long_pass:
        named = [name for key, name in NAMED_WAITS
                 if (figures["waits"].get(key) or 0) >= BUSY_SHARE * length_ms * 1000]
        minor = sum(int(event.get("minflt") or 0) for event in passes)
        major = sum(int(event.get("majflt") or 0) for event in passes)
        if named:
            wait, kind = "; ".join(named), "counter"
        elif minor or major:
            wait, kind = (f"page faults on the class thread in the pass ({minor} minor, "
                          f"{major} major)"), "faults"
        elif figures["reclaim"] > 0:
            wait, kind = "the guest's reclaim under an allocation", "reclaim"
        else:
            wait, kind = "a lock or wait that no counter names (fanout-stall.md items 1 to 6)", \
                "unnamed"
            if (figures["minflt"] or 0) > 0 or (figures["majflt"] or 0) > 0:
                wait += (f"; the thread's fault counter rose ({figures['minflt']} minor, "
                         f"{figures['majflt']} major) but is its own once-a-second reading, "
                         "up to a second stale: not evidence of faults in the stop")
        switched = (figures.get("voluntary") or 0) > 0
        return {"class": "blocked in our code", "figures": figures, "wait_kind": kind,
                "wait": wait if switched else f"no switch: a spin or a long computation ({wait})",
                "why": f"run delay {delay:.1f} ms over its usual, a pass of the class loop "
                       f"over half the {length_ms:.0f} ms stop, {figures.get('voluntary')} "
                       "voluntary switches"}
    if delay < FLAT_SHARE * length_ms:
        return {"class": "waited for input", "figures": figures,
                "why": f"run delay {delay:.1f} ms and no long pass: the thread slept and "
                       "nothing came (upstream, or the kernel did not deliver)"}
    return {"class": "undetermined", "figures": figures,
            "why": f"run delay {delay:.0f} ms over a {length_ms:.0f} ms stop: between flat "
                   "and not run"}


def sampler_stops(lines: list[dict], min_ms: float) -> list[tuple[int, int]]:
    """The class thread's stops that its sampler alone shows (where no
    commit stamps are kept, as in a local lab): a second in which its run
    delay grew by `min_ms` or more, or its loop's longest pass rose to
    `min_ms` or more. Each is (end - length, end) at the line that showed
    it, its length the larger of the two, ready for `classify`."""
    out = []
    for before, after in zip(lines, lines[1:]):
        figures = deltas(before, after)
        delay = figures.get("run_delay_ms") or 0.0
        busy_before, busy_after = figures["busy_max_us"]
        busy = (busy_after / 1000 if busy_after is not None and busy_after > (busy_before or 0)
                else 0.0)
        length = max(delay, busy)
        if figures["same_thread"] and length >= min_ms:
            end = int(after["t_us"])
            out.append((end - int(length * 1000), end))
    return out


# --------------------------------------------- the referee's samplers' files
#
# The paid hunt samples with the referee's own samplers (needletail-compare
# scripts/compare/compare-run.py start_samplers, collect_samplers), which
# write per node, in the run's samplers/ directory, one file a kind and
# call (T: the Unix second of the call):
#   NODE.heartbeat.T.log  "HB cpu monotonic_ns realtime_ns boottime_ns
#                         late_ns policy": a SCHED_FIFO 99 thread pinned to
#                         each vCPU sleeps 10 ms; a line when it woke more
#                         than 5 ms late, and one a second
#   NODE.sys.T.log        "unix_s S kind ...": per-CPU /proc/stat (stat),
#                         /proc/interrupts (irq), /proc/softirqs (softirq),
#                         softnet_stat (softnet), and each av-mesh and
#                         av-contrib thread's raw /proc/PID/task/TID/stat
#                         (task PID <stat>: utime 14, stime 15, CPU 39,
#                         policy 41, in clock ticks of 10 ms)
#   NODE.meshlocal.T.log  "unix_s <the node's /api/mesh/local JSON>"
#   NODE.nic.T.txt        uname and ethtool -i/-k/-l/-S of eth0 and eth1

HEARTBEAT_SLEEP_US = 10_000
# A heartbeat that woke late by half a stop or more over the stop is late
# "by about the stop's length".
HEARTBEAT_SHARE = 0.5
# The thread's system time that rose by half the stop or more over it.
STIME_SHARE = 0.5
CLOCK_TICK_MS = 10.0
# Heartbeat lines this close before and after a stop say the vCPU's
# heartbeat ran (one line a second at least).
HEARTBEAT_COVER_US = 3_000_000
CLASS_THREAD = "needletail-fast"
REFEREE_CLASSES = ("not run", "kernel, interrupts off", "blocked in our code",
                   "waited for input", "no sampler")


def parse_heartbeat(text: str) -> list[dict]:
    """The heartbeat's lines: {"cpu", "t_us" (CLOCK_REALTIME of the wake),
    "late_us", "policy"}, oldest first, each once."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[0] != "HB":
            continue
        try:
            cpu, realtime, late = int(parts[1]), int(parts[3]), int(parts[5])
        except ValueError:
            continue
        out[(cpu, realtime)] = {"cpu": cpu, "t_us": realtime // 1000,
                                "late_us": max(0, late) // 1000,
                                "policy": parts[6] if len(parts) > 6 else None}
    return sorted(out.values(), key=lambda item: (item["t_us"], item["cpu"]))


def parse_task_stat(stat: str) -> dict | None:
    """One /proc/PID/task/TID/stat line: tid, comm, utime and stime (ticks),
    the CPU it last ran on, its policy (0 other, 1 FIFO, 2 RR)."""
    head, _, rest = stat.rpartition(")")
    if not head or "(" not in head:
        return None
    tid, _, comm = head.partition(" (")
    fields = rest.split()
    try:
        return {"tid": int(tid), "comm": comm, "utime": int(fields[11]),
                "stime": int(fields[12]), "cpu": int(fields[36]),
                "policy": int(fields[38]) if len(fields) > 38 else None}
    except (ValueError, IndexError):
        return None


def parse_sys_tasks(text: str) -> list[dict]:
    """The `task` lines of the referee's sys sampler: {"t_us", "pid",
    **parse_task_stat}, oldest first."""
    out = []
    for line in (text or "").splitlines():
        parts = line.split(" ", 4)
        if len(parts) < 5 or parts[1] != "S" or parts[2] != "task":
            continue
        try:
            t_us, pid = int(float(parts[0]) * 1e6), int(parts[3])
        except ValueError:
            continue
        task = parse_task_stat(parts[4])
        if task:
            out.append({"t_us": t_us, "pid": pid, **task})
    return sorted(out, key=lambda item: (item["t_us"], item["tid"]))


def sys_times(text: str) -> list[int]:
    """The times (us) of the sys sampler's seconds, one each."""
    out = set()
    for line in (text or "").splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[1] == "S":
            try:
                out.add(int(float(parts[0]) * 1e6))
            except ValueError:
                continue
    return sorted(out)


def parse_meshlocal(text: str) -> list[dict]:
    """The meshlocal sampler's seconds as sampler lines ({"t_us", "mesh"}:
    the counters of MESH_PATHS at their paths), oldest first."""
    out = []
    for line in (text or "").splitlines():
        stamp, _, body = line.partition(" ")
        try:
            t_us, document = int(float(stamp) * 1e6), json.loads(body)
        except ValueError:
            continue
        mesh = {}
        for path in MESH_PATHS.values():
            value = document
            for part in path.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                mesh[path] = value
        out.append({"t_us": t_us, "mesh": mesh})
    return sorted(out, key=lambda item: item["t_us"])


def class_thread(tasks: list[dict], at_us: int, comm: str = CLASS_THREAD) -> dict | None:
    """The audio class thread's task line at or before `at_us` (else the
    first after): of the threads named `comm` (/proc cuts the audio and
    video class threads' names alike), the real-time one, else the lowest
    tid (av-mesh starts the audio one first), of the latest process."""
    named = [task for task in tasks if task["comm"] == comm]
    if not named:
        return None
    before = [task for task in named if task["t_us"] <= at_us]
    pool = before or named
    pid = (before[-1] if before else named[0])["pid"]
    mine = [task for task in pool if task["pid"] == pid]
    fifo = sorted({task["tid"] for task in mine if task.get("policy") in (1, 2)})
    tid = fifo[0] if fifo else min(task["tid"] for task in mine)
    lines = [task for task in mine if task["tid"] == tid]
    return lines[-1] if before else lines[0]


def heartbeat_gap(beat: dict) -> tuple[int, int]:
    """The time a late wake was not run: its lateness, up to the wake."""
    return beat["t_us"] - beat["late_us"], beat["t_us"]


def heartbeat_late_ms(beats: list[dict], min_ms: float,
                      spans: list[tuple[int, int]] | None = None) -> float:
    """A node's "not run" milliseconds from its heartbeats: the late wakes
    of `min_ms` or more (those that ended inside `spans` when given),
    summed per vCPU, the most of any vCPU."""
    per_cpu: dict[int, float] = {}
    for beat in beats:
        if beat["late_us"] < min_ms * 1000:
            continue
        if spans is not None and not any(start <= beat["t_us"] <= end for start, end in spans):
            continue
        per_cpu[beat["cpu"]] = per_cpu.get(beat["cpu"], 0.0) + beat["late_us"] / 1000
    return round(max(per_cpu.values(), default=0.0), 3)


def heartbeat_stops(beats: list[dict], min_ms: float) -> list[dict]:
    """Every late wake of `min_ms` or more as a stop of its vCPU, and those
    of several vCPUs that overlap joined: {"from_us", "to_us", "ms",
    "cpus"}. A stop on every vCPU at once is the whole guest's."""
    late = sorted((heartbeat_gap(beat) + (beat["cpu"],) for beat in beats
                   if beat["late_us"] >= min_ms * 1000), key=lambda item: item[0])
    out: list[dict] = []
    for start, end, cpu in late:
        if out and start <= out[-1]["to_us"]:
            out[-1]["to_us"] = max(out[-1]["to_us"], end)
            out[-1]["cpus"] = sorted(set(out[-1]["cpus"]) | {cpu})
        else:
            out.append({"from_us": start, "to_us": end, "cpus": [cpu]})
    for item in out:
        item["ms"] = round((item["to_us"] - item["from_us"]) / 1000, 3)
    return out


def stime_rose_ms(tasks: list[dict], tid: int, start_us: int, end_us: int) -> float | None:
    """How much a thread's system time rose from its last line at or
    before `start_us` to its first at or after `end_us` (ms); None
    without both."""
    mine = [task for task in tasks if task["tid"] == tid]
    before = [task for task in mine if task["t_us"] <= start_us]
    after = [task for task in mine if task["t_us"] >= end_us]
    if not before or not after:
        return None
    return (after[0]["stime"] - before[-1]["stime"]) * CLOCK_TICK_MS


def kernel_threads(tasks: list[dict], cpu: int | None, start_us: int, end_us: int,
                   at_least_ms: float) -> list[dict]:
    """The service threads (on `cpu` when known) whose system time rose by
    `at_least_ms` or more over [start_us, end_us]."""
    out = []
    seen = set()
    for task in tasks:
        if task["tid"] in seen or (cpu is not None and task["cpu"] != cpu):
            continue
        seen.add(task["tid"])
        rose = stime_rose_ms(tasks, task["tid"], start_us, end_us)
        if rose is not None and rose >= at_least_ms:
            out.append({"tid": task["tid"], "comm": task["comm"], "stime_ms": rose})
    return out


def classify_heartbeat(start_us: int, end_us: int, beats: list[dict], tasks: list[dict],
                       mesh_lines: list[dict] = (), journal: list[dict] = ()) -> dict:
    """One stop of a node, [start_us, end_us], from the referee's samplers
    (D-hunt.md 7.5 as revised after the 4K run): the audio class thread's
    vCPU (its last CPU in the sys sampler; every vCPU when not known):
    - "not run": its heartbeat woke late by about the stop's length (half
      of it or more, over the stop), and no service thread's system time
      rose: the hypervisor did not run the vCPU (on every vCPU at once:
      the whole guest);
    - "kernel, interrupts off": its heartbeat was late and a service
      thread's system time rose by half the stop or more: that thread was
      in the kernel with interrupts off (a FIFO 99 thread is not run only
      then, or when the vCPU is not);
    - "blocked in our code": the heartbeat was on time (the vCPU ran and
      could have run the class thread); the wait is named from the
      node's /api/mesh/local counters (NAMED_WAITS) and the journal's
      class passes when they show a long pass; with counters that show
      none it "waited for input";
    - "no sampler": no heartbeat of that vCPU around the stop."""
    length_ms = (end_us - start_us) / 1000
    thread = class_thread(list(tasks), start_us)
    cpu = thread["cpu"] if thread else None
    figures: dict = {"cpu": cpu, "thread": None if thread is None else
                     f"{thread['comm']} ({thread['tid']})"}
    cpus = sorted({beat["cpu"] for beat in beats})
    mine = [beat for beat in beats if cpu is None or beat["cpu"] == cpu]
    covered = (any(start_us - HEARTBEAT_COVER_US <= beat["t_us"] <= start_us for beat in mine)
               and any(end_us <= beat["t_us"] <= end_us + HEARTBEAT_COVER_US for beat in mine))
    if not covered:
        return {"class": "no sampler", "figures": figures,
                "why": "no heartbeat line of the class thread's vCPU before and after the stop"}
    half_us = HEARTBEAT_SHARE * (end_us - start_us)
    late = [beat for beat in mine if overlap(heartbeat_gap(beat), (start_us, end_us)) >= half_us]
    every = sorted({beat["cpu"] for beat in beats
                    if overlap(heartbeat_gap(beat), (start_us, end_us)) >= half_us})
    figures["heartbeat_late_ms"] = round(max((beat["late_us"] for beat in late), default=0)
                                         / 1000, 3)
    figures["late_cpus"] = every
    if late:
        hot = kernel_threads(list(tasks), cpu, start_us, end_us, STIME_SHARE * length_ms)
        figures["kernel_threads"] = hot
        if hot:
            named = ", ".join(f"{item['comm']} ({item['tid']}) stime +{item['stime_ms']:.0f} ms"
                              for item in hot)
            return {"class": "kernel, interrupts off", "figures": figures,
                    "where": f"in the kernel with interrupts off: {named}",
                    "why": f"the heartbeat of CPU {late[0]['cpu']} woke "
                           f"{figures['heartbeat_late_ms']:.0f} ms late over a "
                           f"{length_ms:.0f} ms stop and {named}"}
        whole = len(cpus) > 1 and every == cpus
        return {"class": "not run", "figures": figures,
                "where": "the hypervisor: " + ("every vCPU at once, the whole guest" if whole
                                               else f"vCPU {late[0]['cpu']} was not run"),
                "why": f"the heartbeat of CPU {late[0]['cpu']} woke "
                       f"{figures['heartbeat_late_ms']:.0f} ms late over a {length_ms:.0f} ms "
                       "stop, no service thread's system time rose"}
    pair = around(list(mesh_lines), start_us, end_us) if mesh_lines else None
    passes = [event for event in journal if event.get("kind") == "class_pass"
              and event.get("cause") == "own" and event.get("thread", "audio") == "audio"
              and overlap(interval(event), (start_us, end_us)) >= half_us]
    waits = {}
    long_pass = bool(passes)
    if pair:
        before, after = pair
        busy_before = mesh_value(before, "audio_loop_busy_max_us")
        busy_after = mesh_value(after, "audio_loop_busy_max_us")
        over = grew(before, after, "audio_loop_busy_over_50ms")
        long_pass = long_pass or (length_ms <= 2 * OVER_50MS_US / 1000 and (over or 0) > 0) or (
            busy_after is not None and busy_after >= BUSY_SHARE * length_ms * 1000
            and busy_after > (busy_before or 0))
        waits = {key: mesh_value(after, key) for key, _name in NAMED_WAITS
                 if (grew(before, after, key) or 0) > 0}
    figures["waits"] = waits
    named = [name for key, name in NAMED_WAITS
             if (waits.get(key) or 0) >= BUSY_SHARE * length_ms * 1000]
    if pair and not long_pass and not named:
        return {"class": "waited for input", "figures": figures,
                "why": "the heartbeat was on time and the class loop had no long pass: the "
                       "thread slept and nothing came (upstream, or the kernel did not "
                       "deliver)"}
    if named:
        wait, kind = "; ".join(named), "counter"
    elif passes:
        minor = sum(int(event.get("minflt") or 0) for event in passes)
        major = sum(int(event.get("majflt") or 0) for event in passes)
        wait, kind = ((f"page faults on the class thread in the pass ({minor} minor, "
                       f"{major} major)"), "faults") if minor or major else \
            ("a lock or wait that no counter names (fanout-stall.md items 1 to 6)", "unnamed")
    else:
        wait, kind = ("a lock or wait that no counter names" if pair else
                      "not named: no /api/mesh/local counters around the stop"), "unnamed"
    return {"class": "blocked in our code", "figures": figures, "wait": wait, "wait_kind": kind,
            "why": f"the heartbeat of CPU {cpu if cpu is not None else 'any'} was on time over "
                   f"the {length_ms:.0f} ms stop: the vCPU ran"}
