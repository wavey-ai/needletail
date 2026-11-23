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

This module is imported by scripts/linode-stall-hunt.py (which node to
replace), scripts/compare/hunt-report.py (the report) and by any collector
that wants a node's journal for a phase: `fetch_stalls` is that one call.
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
    time), the contributor's read and its publish."""
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
