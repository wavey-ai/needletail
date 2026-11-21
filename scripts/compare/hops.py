#!/usr/bin/env python3
"""Join the hop journals of every node of a Needletail lab and say where the
time of each part goes, from the instant its last sample was spoken to the
instant a listener had it.

Every node serves its journal at GET /api/hops?since_us=T&stream=S as NDJSON,
one line for each event of a part:

  {"node":..,"stage":..,"stream":..,"seq":..,"t_us":..,"peer":..?,"epoch":..?,"n":..?}

`seq` is the part number everywhere (the relay object sequence, the cache
slot and N of partN), so the events of one part join on (stream, seq).

Each part is followed along the path that it took to a listener:

  source            last sample of the part spoken (PCM: from the AEP1 epoch)
  contrib.src_last  last source datagram of the part at the contributor
  contrib.packaged  part cut
  contrib.sent      last source symbol handed to the kernel, primary lane
  primary.relay_first / primary.relay_forward   the backbone relay-primary
  parent.relay_first / parent.committed / parent.watermark / parent.mesh_sent
                    the distributor that fed the edge (found from the edge's
                    mesh_first `peer`), mesh_sent to that edge
  edge.mesh_first / edge.edge_committed / edge.reply
  listener.complete the part complete at a listener (ntcmp probe.csv)

For every region (the site of the edge) it reports the median, p95, p99 and
maximum of each hop (the delta between consecutive stages of one part) and
of every stage counted from the source. Parts are followed twice: once for
every part that every edge committed (journal only), and once for every part
that a listener received, when probe.csv files are given. A listener's reply
time and edge-committed time are the headers of its own response; its edge is
the edge whose journal holds that reply (or the host of its playlist).

Source time. The AEP1 sender of the harness starts its loop on a wall-clock
instant START and sends epoch k when its last sample has been spoken, at
START + (k+1) * 5 ms; compare-audio.py and latency-bench.py pass
--session-id START + 5 ms, so epoch k leaves at session_id + k * 5 ms. Give
--source-start-ns START (or --session-id, from which START = session_id -
5 ms). Without either, the spoken time is taken from the listener's own record
(probe.csv: complete - latency, which ntcmp computes from the same loop).
Whether part seq equals epoch id is checked and reported (seq_minus_epoch).

Clocks: the offset of every node from `chronyc -c tracking` (field 5, the
system time offset in seconds) is recorded, and in the local lab the clock
shift of any node moved with edge_lab.set_clock. Times are not corrected.

  hops.py --lab edge   --since-us T [--until-us T] [--probe [SITE=]probe.csv ...] --out hops.json
  hops.py --lab linode --since-us T ...
  hops.py --journal raw.ndjson ...         analyse a saved collection (--raw-out)
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
import time
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent
EPOCH_US = 5000
MESH_PORT = 19444
CONTRIB_PORT = 19443
QUANTILES = (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))

# The path of a part, in order: (label, where, stage).
CHAIN = [
    ("source", "source", None),
    ("contrib.src_last", "contrib", "src_last"),
    ("contrib.packaged", "contrib", "packaged"),
    ("contrib.sent", "contrib", "sent"),
    ("primary.relay_first", "primary", "relay_first"),
    ("primary.relay_forward", "primary", "relay_forward"),
    ("parent.relay_first", "parent", "relay_first"),
    ("parent.committed", "parent", "committed"),
    ("parent.watermark", "parent", "watermark"),
    ("parent.mesh_sent", "parent", "mesh_sent"),
    ("edge.mesh_first", "edge", "mesh_first"),
    ("edge.edge_committed", "edge", "edge_committed"),
    ("edge.reply", "edge", "reply"),
    ("listener.complete", "listener", None),
]
EPOCH_STAGES = ("src_first", "src_last", "recovered", "packaged")


# ------------------------------------------------------------------ numbers

def percentile(ordered: list[float], q: float) -> float:
    """Nearest rank, as ntcmp's report: the value at round((n-1) q)."""
    return ordered[int((len(ordered) - 1) * q + 0.5)]


def distribution(values: list[float]) -> dict | None:
    """Count, min, median, p95, p99, max and mean of values in ms."""
    if not values:
        return None
    ordered = sorted(values)
    out = {"count": len(ordered), "min": round(ordered[0], 3)}
    for name, q in QUANTILES:
        out[name] = round(percentile(ordered, q), 3)
    out["max"] = round(ordered[-1], 3)
    out["mean"] = round(sum(ordered) / len(ordered), 3)
    return out


def host_of(address: str | None) -> str | None:
    """The IP address of `ip:port`, `[v6]:port` or a bare address."""
    if not address:
        return None
    address = address.strip()
    if address.startswith("["):
        return address[1:address.index("]")] if "]" in address else address[1:]
    if address.count(":") == 1:
        return address.split(":")[0]
    return address


def parse_ndjson(text: str, node: str | None = None) -> list[dict]:
    """The events of one journal. `node` is the lab name of the node that
    served it; it is kept beside the node id that the event carries."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not all(key in event for key in ("stage", "stream", "seq", "t_us")):
            continue
        if node is not None:
            event["_node"] = node
        events.append(event)
    return events


def parse_chrony(text: str) -> float | None:
    """System time offset in seconds from `chronyc -c tracking`, field 5."""
    for line in text.splitlines():
        fields = line.strip().split(",")
        if len(fields) >= 5:
            try:
                return float(fields[4])
            except ValueError:
                continue
    return None


# ------------------------------------------------------------------ probes

def read_probe(spec: str) -> dict:
    """A listener record: `probe.csv` or `SITE=probe.csv`. The edge that
    served it is looked for in the report next to it (`.json`)."""
    site = None
    path = spec
    if "=" in spec and not pathlib.Path(spec).exists():
        site, path = spec.split("=", 1)
    path = pathlib.Path(path)
    rows: dict[int, dict] = {}
    with path.open() as handle:
        for row in csv.DictReader(handle):
            kind = row.get("kind")
            part = (row.get("part") or "").strip()
            if part:
                seq = int(part)
            elif kind == "a":
                seq = int(row["first_unix_us"])     # an audio row's number
            else:
                continue                            # a video row of an older file
            if seq < 0:
                continue
            complete = int(row["complete_unix_us"])
            if seq in rows and rows[seq]["complete_us"] <= complete:
                continue

            def field(name: str) -> int | None:
                value = (row.get(name) or "").strip()
                return int(value) if value and int(value) > 0 else None

            spoken = None
            if int(row["index"]) >= 0:
                spoken = complete - int(row["latency_us"])
            rows[seq] = {"complete_us": complete, "spoken_us": spoken,
                         "published_us": field("published_unix_us"),
                         "cache_available_us": field("cache_available_unix_us"),
                         "edge_committed_us": field("edge_committed_unix_us"),
                         "reply_us": field("reply_unix_us")}
    host_ip = None
    report = path.with_suffix(".json")
    if report.is_file():
        try:
            detail = json.loads(report.read_text()).get("detail") or {}
            host_ip = host_of(detail.get("playlist_url_host"))
        except ValueError:
            pass
    return {"path": str(path), "site": site, "host_ip": host_ip, "rows": rows}


# ------------------------------------------------------------------ the join

class Journal:
    """The events of every node, indexed for the join."""

    def __init__(self, nodes: dict, events: list[dict], stream: int | None,
                 since_us: int | None = None, until_us: int | None = None):
        self.nodes = nodes
        self.by_ip = {}
        for name, item in nodes.items():
            for address in item.get("addrs") or []:
                self.by_ip[address] = name
        self.first: dict[tuple, int] = {}          # (node, stage, stream, seq) -> earliest t
        self.mesh_sent: dict[tuple, int] = {}      # (node, stream, seq, peer node) -> earliest t
        self.sent_to: dict[tuple, tuple] = {}      # (stream, seq, peer node) -> (first sender, t)
        self.parts_at: dict[str, set] = {}         # node -> seqs it committed or received
        self.parent: dict[tuple, str] = {}         # (edge, stream, seq) -> parent node
        self.replies: dict[tuple, list[int]] = {}  # (edge, stream, seq) -> reply times
        self.epochs: dict[tuple, dict] = {}        # (stream, seq) -> {stage: max epoch}
        self.stage_names: set[tuple] = set()       # (role, stage)
        self.counts: dict[str, int] = {}
        for event in events:
            if stream is not None and int(event["stream"]) != stream:
                continue
            t = int(event["t_us"])
            if since_us is not None and t < since_us:
                continue
            if until_us is not None and t > until_us:
                continue
            node = event.get("_node") or event.get("node")
            key_part = (int(event["stream"]), int(event["seq"]))
            stage = event["stage"]
            self.counts[node] = self.counts.get(node, 0) + 1
            self.stage_names.add((self.role(node), stage))
            key = (node, stage) + key_part
            if key not in self.first or t < self.first[key]:
                self.first[key] = t
            peer = self.by_ip.get(host_of(event.get("peer")))
            if stage == "mesh_sent" and peer:
                sent = (node,) + key_part + (peer,)
                if sent not in self.mesh_sent or t < self.mesh_sent[sent]:
                    self.mesh_sent[sent] = t
                to = key_part + (peer,)
                if to not in self.sent_to or t < self.sent_to[to][1]:
                    self.sent_to[to] = (node, t)
            if stage in ("edge_committed", "mesh_first") and (stream is None or key_part[0] == stream):
                self.parts_at.setdefault(node, set()).add(key_part[1])
            if stage == "mesh_first" and peer and self.first[key] == t:
                self.parent[(node,) + key_part] = peer
            if stage == "reply":
                self.replies.setdefault((node,) + key_part, []).append(t)
            if stage in EPOCH_STAGES and event.get("epoch") is not None \
                    and self.role(node) == "contributor":
                slot = self.epochs.setdefault(key_part, {})
                slot[stage] = max(slot.get(stage, -1), int(event["epoch"]))

    def role(self, node: str) -> str:
        return (self.nodes.get(node) or {}).get("role", "unknown")

    def site(self, node: str | None) -> str | None:
        return (self.nodes.get(node) or {}).get("site") if node else None

    def named(self, role: str) -> list[str]:
        return sorted(name for name, item in self.nodes.items() if item.get("role") == role)

    def earliest(self, nodes: list[str], stage: str, stream: int, seq: int) -> int | None:
        times = [self.first.get((node, stage, stream, seq)) for node in nodes]
        times = [t for t in times if t is not None]
        return min(times) if times else None

    def last_epoch(self, stream: int, seq: int) -> int | None:
        """The AEP1 epoch whose last sample ends the part."""
        slot = self.epochs.get((stream, seq))
        if not slot:
            return None
        if "src_last" in slot:
            return slot["src_last"]
        return max(slot.values())

    def parent_of(self, edge: str, stream: int, seq: int) -> str | None:
        parent = self.parent.get((edge, stream, seq))
        if parent:
            return parent
        # No mesh_first with a known peer: the node that sent the part to
        # this edge first.
        best = self.sent_to.get((stream, seq, edge))
        return best[0] if best else None

    def edge_by_reply(self, stream: int, seq: int, reply_us: int, within_us: int) -> str | None:
        best = None
        for edge in self.named("edge"):
            for t in self.replies.get((edge, stream, seq), ()):
                gap = abs(t - reply_us)
                if gap <= within_us and (best is None or gap < best[1]):
                    best = (edge, gap)
        return best[0] if best else None


def follow(journal: Journal, stream: int, seq: int, edge: str | None, primary: list[str],
           source_us: int | None, listener: dict | None) -> dict[str, int]:
    """The time of every stage of the CHAIN for one part on its way to one
    edge (and listener)."""
    contrib = journal.named("contributor")
    parent = journal.parent_of(edge, stream, seq) if edge else None
    times: dict[str, int] = {}
    if source_us is not None:
        times["source"] = source_us
    for label, where, stage in CHAIN:
        t = None
        if where == "contrib":
            t = journal.earliest(contrib, stage, stream, seq)
        elif where == "primary":
            t = journal.earliest(primary, stage, stream, seq)
        elif where == "parent" and parent:
            if stage == "mesh_sent":
                t = journal.mesh_sent.get((parent, stream, seq, edge))
            else:
                t = journal.earliest([parent], stage, stream, seq)
        elif where == "edge":
            if stage == "reply" and listener and listener.get("reply_us"):
                t = listener["reply_us"]
            elif stage == "reply" and edge:
                replies = journal.replies.get((edge, stream, seq))
                t = min(replies) if replies else None
            elif edge:
                t = journal.earliest([edge], stage, stream, seq)
            if t is None and stage == "edge_committed" and listener:
                t = listener.get("edge_committed_us")
        elif where == "listener" and listener:
            t = listener["complete_us"]
        if t is not None:
            times[label] = t
    return times


class Pool:
    """Deltas of many parts, by hop and by stage from the source."""

    def __init__(self):
        self.parts = 0
        self.hops: dict[str, list[float]] = {}
        self.from_source: dict[str, list[float]] = {}

    def add(self, times: dict[str, int]) -> None:
        self.parts += 1
        labels = [label for label, _, _ in CHAIN]
        for before, after in zip(labels, labels[1:]):
            if before in times and after in times:
                self.hops.setdefault(f"{before} -> {after}", []).append(
                    (times[after] - times[before]) / 1000)
        if "source" in times:
            for label in labels[1:]:
                if label in times:
                    self.from_source.setdefault(label, []).append(
                        (times[label] - times["source"]) / 1000)

    def report(self) -> dict:
        labels = [label for label, _, _ in CHAIN]
        order = {f"{a} -> {b}": i for i, (a, b) in enumerate(zip(labels, labels[1:]))}
        return {
            "parts": self.parts,
            "hops": [{"hop": name, **distribution(values)}
                     for name, values in sorted(self.hops.items(), key=lambda item: order[item[0]])],
            "from_source": [{"stage": label, **distribution(self.from_source[label])}
                            for label in labels if label in self.from_source],
        }


def analyse(nodes: dict, events: list[dict], probes: list[dict], stream: int,
            source_start_us: int | None = None, epoch_us: int = EPOCH_US,
            primary: str = "relay-primary", since_us: int | None = None,
            until_us: int | None = None, reply_match_us: int = 2000) -> dict:
    journal = Journal(nodes, events, stream, since_us, until_us)
    primaries = [primary] if primary in nodes else journal.named("backbone")[:1]

    def source_of(seq: int, listener: dict | None = None) -> int | None:
        epoch = journal.last_epoch(stream, seq)
        if source_start_us is not None and epoch is not None:
            return source_start_us + (epoch + 1) * epoch_us
        if listener and listener.get("spoken_us") is not None:
            return listener["spoken_us"]
        return None

    # seq = epoch id? Checked on every part that the contributor saw.
    offsets: dict[int, int] = {}
    for (s, seq), slot in journal.epochs.items():
        first = slot.get("src_first", min(slot.values()))
        offsets[seq - first] = offsets.get(seq - first, 0) + 1
    checked = sum(offsets.values())
    seq_check = {"parts": checked,
                 "seq_minus_epoch": {str(k): v for k, v in sorted(offsets.items(), key=lambda i: -i[1])[:8]},
                 "seq_equals_epoch": bool(checked) and offsets.get(0, 0) >= 0.99 * checked}

    regions: dict[str, dict[str, Pool]] = {}

    def pool(region: str, kind: str) -> Pool:
        return regions.setdefault(region, {}).setdefault(kind, Pool())

    # Every part that every edge committed: the journal alone.
    for edge in journal.named("edge"):
        site = journal.site(edge) or "unknown"
        for seq in sorted(journal.parts_at.get(edge, ())):
            times = follow(journal, stream, seq, edge, primaries, source_of(seq), None)
            pool(site, "edge_parts").add(times)
            pool("all", "edge_parts").add(times)

    # Every part that a listener received.
    listeners = []
    spoken_check = []
    for probe in probes:
        host_edge = journal.by_ip.get(probe.get("host_ip"))
        if host_edge and journal.role(host_edge) != "edge":
            host_edge = None
        matched = by_reply = 0
        for seq, row in sorted(probe["rows"].items()):
            if since_us is not None and row["complete_us"] < since_us:
                continue
            if until_us is not None and row["complete_us"] > until_us:
                continue
            edge = None
            if row.get("reply_us"):
                edge = journal.edge_by_reply(stream, seq, row["reply_us"], reply_match_us)
                by_reply += edge is not None
            edge = edge or host_edge
            site = journal.site(edge) or probe.get("site") or "unknown"
            source = source_of(seq, row)
            if source_start_us is not None and row.get("spoken_us") is not None \
                    and journal.last_epoch(stream, seq) is not None:
                spoken_check.append((row["spoken_us"] - source) / 1000)
            times = follow(journal, stream, seq, edge, primaries, source, row)
            matched += 1
            pool(site, "listener_parts").add(times)
            pool("all", "listener_parts").add(times)
        listeners.append({"probe": probe["path"], "site": probe.get("site"), "parts": matched,
                          "edge_by_playlist_host": host_edge, "edge_found_by_reply": by_reply})

    return {
        "stream": stream,
        "window_us": {"since": since_us, "until": until_us},
        "source": {"source_start_us": source_start_us, "epoch_us": epoch_us,
                   "rule": "source = source_start + (epoch + 1) * epoch_us, epoch of src_last; "
                           "else the listener's complete - latency"},
        "primary_relay": primaries,
        "nodes": {name: {**{k: v for k, v in item.items() if k != "addrs"},
                         "events": journal.counts.get(name, 0)}
                  for name, item in sorted(nodes.items())},
        "seq_check": seq_check,
        "probe_spoken_minus_journal_spoken_ms": distribution(spoken_check),
        "stages_seen": sorted(f"{role}.{stage}" for role, stage in journal.stage_names),
        "listeners": listeners,
        "regions": {region: {kind: item.report() for kind, item in kinds.items()}
                    for region, kinds in sorted(regions.items())},
    }


def format_summary(result: dict) -> str:
    lines = [f"stream {result['stream']}; seq = epoch: {result['seq_check']['seq_equals_epoch']} "
             f"({result['seq_check']['parts']} parts; seq - epoch {result['seq_check']['seq_minus_epoch']})"]
    clocks = {name: item.get("chrony_offset_s") for name, item in result["nodes"].items()
              if item.get("chrony_offset_s") is not None}
    if clocks:
        worst = max(clocks.items(), key=lambda item: abs(item[1]))
        lines.append(f"chrony: {len(clocks)} nodes, largest offset {worst[0]} {worst[1] * 1e3:+.3f} ms")
    silent = [name for name, item in result["nodes"].items() if not item.get("events")]
    if silent:
        lines.append("no journal events from: " + ", ".join(silent))
    for region, kinds in result["regions"].items():
        for kind, report in kinds.items():
            lines.append("")
            lines.append(f"{region} / {kind}: {report['parts']} parts   (ms: n p50 p95 p99 max)")
            for item in report["hops"]:
                lines.append(f"  {item['hop']:<48} {item['count']:>7} {item['p50']:>8.3f} "
                             f"{item['p95']:>8.3f} {item['p99']:>8.3f} {item['max']:>9.3f}")
            for item in report["from_source"]:
                if item["stage"] in ("listener.complete", "edge.reply"):
                    lines.append(f"  source -> {item['stage']:<38} {item['count']:>7} {item['p50']:>8.3f} "
                                 f"{item['p95']:>8.3f} {item['p99']:>8.3f} {item['max']:>9.3f}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ collection

def hops_path(since_us: int | None, stream: int | None) -> str:
    query = {}
    if since_us is not None:
        query["since_us"] = since_us
    if stream is not None:
        query["stream"] = stream
    return "/api/hops" + ("?" + urllib.parse.urlencode(query) if query else "")


def collect_edge_lab(since_us: int | None, stream: int | None) -> tuple[dict, list[dict]]:
    """The journals of the local namespace lab (scripts/edge_lab.py)."""
    import concurrent.futures
    import subprocess
    sys.path.insert(0, str(SCRIPTS))
    import edge_lab as lab
    nodes, events = {}, []
    host_offset = None
    try:
        host_offset = parse_chrony(subprocess.run(["chronyc", "-c", "tracking"], capture_output=True,
                                                  text=True, timeout=5).stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    names = [name for name, *_ in lab.NODES]

    def fetch(name: str):
        return lab.curl(name, hops_path(since_us, stream), timeout=60)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        answers = dict(zip(names, pool.map(fetch, names)))
    for name in names:
        item = lab.node(name)
        status, _head, body = answers[name]
        shift = None
        shift_file = lab.WORK / "clock" / name
        if shift_file.is_file():
            try:
                shift = float(shift_file.read_text().strip())
            except ValueError:
                pass
        got = parse_ndjson(body, name) if status == 200 else []
        events += got
        nodes[name] = {"role": item["role"], "site": item["site"],
                       "addrs": [a for a in (item["public_ipv4"], item["vlan_ipv4"]) if a],
                       "http_status": status, "chrony_offset_s": host_offset, "clock_shift_s": shift}
    return nodes, events


def collect_linode(since_us: int | None, stream: int | None) -> tuple[dict, list[dict]]:
    """The journals of the Linode lab: each node is asked over ssh, on its own
    public address, and its chrony offset read in the same call."""
    sys.path.insert(0, str(SCRIPTS))
    import small_mesh_common as common
    state = common.load_state()
    wanted = [item for item in state["nodes"].values()
              if item["role"] in ("contributor", "backbone", "distributor", "edge")]
    marker = "#needletail-hops-chrony"

    def fetch(item: dict) -> str:
        port = CONTRIB_PORT if item["role"] == "contributor" else MESH_PORT
        url = f"https://{item['public_ipv4']}:{port}{hops_path(since_us, stream)}"
        return common.ssh_text(item, f"curl -sk --max-time 60 '{url}'; echo; echo '{marker}'; "
                                     "chronyc -c tracking 2>/dev/null; true", check=False, timeout=120)

    answers = common.parallel(wanted, fetch)
    nodes, events = {}, []
    for item in wanted:
        text = answers.get(item["name"], "")
        journal, _, chrony = text.partition(marker)
        got = parse_ndjson(journal, item["name"])
        events += got
        nodes[item["name"]] = {"role": item["role"], "site": item.get("site"),
                               "addrs": [a for a in (item.get("public_ipv4"), item.get("vlan_ipv4")) if a],
                               "chrony_offset_s": parse_chrony(chrony)}
    return nodes, events


def save_raw(path: pathlib.Path, nodes: dict, events: list[dict]) -> None:
    with path.open("w") as handle:
        handle.write(json.dumps({"_meta": {"nodes": nodes, "saved_unix": time.time()}}) + "\n")
        for event in events:
            handle.write(json.dumps(event) + "\n")


def load_raw(path: pathlib.Path) -> tuple[dict, list[dict]]:
    nodes, events = {}, []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if "_meta" in item:
            nodes = item["_meta"]["nodes"]
        else:
            events.append(item)
    return nodes, events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--lab", choices=("edge", "linode"), help="collect from this lab")
    source.add_argument("--journal", type=pathlib.Path, help="a collection saved with --raw-out")
    parser.add_argument("--since-us", type=int, help="Unix µs; events before are not asked for")
    parser.add_argument("--until-us", type=int, help="Unix µs; events after are left out")
    parser.add_argument("--stream", type=int, default=4001)
    parser.add_argument("--probe", action="append", default=[],
                        help="a listener's probe.csv, or SITE=probe.csv (repeat)")
    clock = parser.add_mutually_exclusive_group()
    clock.add_argument("--source-start-ns", type=int,
                       help="Unix ns at which sample 0 of AEP1 epoch 0 is spoken (the loop START)")
    clock.add_argument("--session-id", type=int,
                       help="the AEP1 sender's --session-id (ns); START = session_id - 5 ms")
    parser.add_argument("--epoch-us", type=int, default=EPOCH_US)
    parser.add_argument("--primary-relay", default="relay-primary")
    parser.add_argument("--reply-match-us", type=int, default=2000,
                        help="a listener's reply header and an edge's reply event this close are one reply")
    parser.add_argument("--raw-out", type=pathlib.Path, help="save the collected events (NDJSON)")
    parser.add_argument("--out", type=pathlib.Path, help="hops.json (default standard output)")
    parser.add_argument("--summary", type=pathlib.Path, help="also write the text summary here")
    args = parser.parse_args(argv)

    if args.journal:
        nodes, events = load_raw(args.journal)
    elif args.lab == "edge":
        nodes, events = collect_edge_lab(args.since_us, args.stream)
    else:
        nodes, events = collect_linode(args.since_us, args.stream)
    if args.raw_out:
        save_raw(args.raw_out, nodes, events)
    start_us = None
    if args.source_start_ns is not None:
        start_us = args.source_start_ns // 1000
    elif args.session_id is not None:
        start_us = args.session_id // 1000 - args.epoch_us
    probes = [read_probe(spec) for spec in args.probe]
    result = analyse(nodes, events, probes, args.stream, start_us, args.epoch_us,
                     args.primary_relay, args.since_us, args.until_us, args.reply_match_us)
    text = json.dumps(result, indent=1) + "\n"
    summary = format_summary(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(summary, end="")
    else:
        print(text, end="")
    if args.summary:
        args.summary.write_text(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
