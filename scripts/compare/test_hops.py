#!/usr/bin/env python3
"""Tests of hops.py: the join on (stream, seq) and the percentiles, on
synthetic journals.  python3 -m unittest scripts/compare/test_hops.py"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("hops", HERE / "hops.py")
hops = importlib.util.module_from_spec(_spec)
sys.modules["hops"] = hops
_spec.loader.exec_module(hops)

STREAM = 4001
START_US = 1_795_000_000_000_000
NODES = {
    "contrib-lon": {"role": "contributor", "site": "london", "addrs": ["10.90.0.5", "10.77.1.5"]},
    "relay-primary": {"role": "backbone", "site": "london", "addrs": ["10.90.0.6", "10.77.1.6"]},
    "lon-dist-a": {"role": "distributor", "site": "london", "addrs": ["10.90.0.11", "10.77.1.11"]},
    "lon-dist-b": {"role": "distributor", "site": "london", "addrs": ["10.90.0.12", "10.77.1.12"]},
    "lon-edge-1": {"role": "edge", "site": "london", "addrs": ["10.90.0.21", "10.77.1.21"],
                   "chrony_offset_s": -0.00002},
    "new-dist-a": {"role": "distributor", "site": "newyork", "addrs": ["10.90.2.11", "10.77.2.11"]},
    "new-edge-1": {"role": "edge", "site": "newyork", "addrs": ["10.90.2.21", "10.77.2.21"]},
}
SEQS = range(100, 200)
SPIKE = 150          # this part waits 10 ms longer at lon-edge-1 before it is committed


def spoken(seq: int, offset: int = 0) -> int:
    return START_US + (seq - offset + 1) * 5000


def event(node, stage, seq, t, **extra):
    return {"node": node, "stage": stage, "stream": STREAM, "seq": seq, "t_us": t, "_node": node, **extra}


def journal(seq_offset: int = 0) -> list[dict]:
    """Every node's events for parts 100..199. seq = epoch + seq_offset."""
    out = []
    for seq in SEQS:
        epoch = seq - seq_offset
        s = spoken(seq, seq_offset)
        out += [
            event("contrib-lon", "src_first", seq, s + 100, epoch=epoch),
            event("contrib-lon", "src_last", seq, s + 200, epoch=epoch),
            event("contrib-lon", "packaged", seq, s + 300, epoch=epoch),
            event("contrib-lon", "sent", seq, s + 500),
            event("relay-primary", "relay_first", seq, s + 1500),
            event("relay-primary", "relay_forward", seq, s + 1600),
            # Both London distributors have the part; lon-dist-a feeds the edge.
            event("lon-dist-a", "relay_first", seq, s + 2600),
            event("lon-dist-a", "committed", seq, s + 2800),
            event("lon-dist-a", "watermark", seq, s + 2900),
            event("lon-dist-a", "mesh_sent", seq, s + 3000, peer="10.77.1.21:12001"),
            event("lon-dist-a", "mesh_sent", seq, s + 3400, peer="10.77.1.22:12001"),
            event("lon-dist-b", "relay_first", seq, s + 2000),
            event("lon-dist-b", "committed", seq, s + 2100),
            event("lon-dist-b", "mesh_sent", seq, s + 2200, peer="10.77.1.23:12001"),
            event("lon-edge-1", "mesh_first", seq, s + 3300, peer="10.77.1.11:29101"),
            event("lon-edge-1", "mesh_decoded", seq, s + 3400),
            event("lon-edge-1", "edge_committed", seq, s + 3600 + (10_000 if seq == SPIKE else 0)),
            event("lon-edge-1", "reply", seq, s + 3700 + (10_000 if seq == SPIKE else 0), n=1),
            event("lon-edge-1", "reply", seq, s + 3900 + (10_000 if seq == SPIKE else 0), n=2),
            event("new-dist-a", "relay_first", seq, s + 40_000),
            event("new-dist-a", "committed", seq, s + 40_200),
            event("new-dist-a", "watermark", seq, s + 40_200),
            event("new-dist-a", "mesh_sent", seq, s + 40_300, peer="10.90.2.21:12001"),
            event("new-edge-1", "mesh_first", seq, s + 41_300, peer="10.90.2.11:29101"),
            event("new-edge-1", "edge_committed", seq, s + 41_500),
            event("new-edge-1", "reply", seq, s + 41_600, n=1),
            # Another stream's part with the same number must not join.
            {**event("lon-edge-1", "edge_committed", seq, s + 1), "stream": 2001},
        ]
    return out


def write_probe(directory: pathlib.Path, name: str, old_format: bool = False) -> pathlib.Path:
    """A London listener of lon-edge-1 whose replies are the edge's second."""
    path = directory / name
    lines = ["kind,index,latency_us,complete_unix_us,first_unix_us,bytes,key,packets"
             + ("" if old_format else ",published_unix_us,cache_available_unix_us,"
                                      "edge_committed_unix_us,reply_unix_us,part")]
    for seq in SEQS:
        s = spoken(seq)
        extra = 10_000 if seq == SPIKE else 0
        complete = s + 4200 + extra
        row = f"a,{seq % 12800},{complete - s},{complete},{seq},1440,0,1"
        if not old_format:
            row += f",{s + 300},{s + 3600 + extra},{s + 3600 + extra},{s + 3900 + extra},{seq}"
        lines.append(row)
    path.write_text("\n".join(lines) + "\n")
    (directory / name.replace(".csv", ".json")).write_text(
        json.dumps({"detail": {"playlist_url_host": "10.90.0.11:19444"}}))
    return path


def hop(report: dict, name: str) -> dict:
    return next(item for item in report["hops"] if item["hop"] == name)


def stage(report: dict, name: str) -> dict:
    return next(item for item in report["from_source"] if item["stage"] == name)


class Numbers(unittest.TestCase):
    def test_percentile_is_nearest_rank_like_ntcmp(self):
        values = list(range(1, 101))            # 1..100
        self.assertEqual(hops.percentile(values, 0.5), 51)   # round(99 * 0.5) = 50 (0-based)
        self.assertEqual(hops.percentile(values, 0.99), 99)
        self.assertEqual(hops.percentile([7.0], 0.99), 7.0)

    def test_distribution(self):
        got = hops.distribution([0.3] * 99 + [10.3])
        self.assertEqual(got["count"], 100)
        self.assertEqual(got["p50"], 0.3)
        self.assertEqual(got["p99"], 0.3)
        self.assertEqual(got["max"], 10.3)
        self.assertAlmostEqual(got["mean"], 0.4)
        self.assertIsNone(hops.distribution([]))

    def test_chrony_field_5_is_the_system_offset(self):
        line = "A9FEA97B,169.254.169.123,3,1795000000.123,-0.000012345,0.000001,0.000003,-12.5,0.1,0.2,0.0001,0.001,64.0,Normal"
        self.assertAlmostEqual(hops.parse_chrony(line), -0.000012345)
        self.assertIsNone(hops.parse_chrony(""))

    def test_host_of(self):
        self.assertEqual(hops.host_of("10.77.1.11:29101"), "10.77.1.11")
        self.assertEqual(hops.host_of("[fe80::1]:443"), "fe80::1")
        self.assertEqual(hops.host_of("10.0.0.1"), "10.0.0.1")
        self.assertIsNone(hops.host_of(None))

    def test_ndjson_skips_bad_lines(self):
        text = '{"node":"a","stage":"sent","stream":1,"seq":2,"t_us":3}\nnot json\n{"stage":"x"}\n\n'
        got = hops.parse_ndjson(text, "contrib-lon")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["_node"], "contrib-lon")


class Join(unittest.TestCase):
    def setUp(self):
        self.directory = pathlib.Path(tempfile.mkdtemp())

    def test_every_hop_of_the_edge_parts(self):
        result = hops.analyse(NODES, journal(), [], STREAM, START_US)
        self.assertTrue(result["seq_check"]["seq_equals_epoch"])
        self.assertEqual(result["seq_check"]["parts"], 100)
        london = result["regions"]["london"]["edge_parts"]
        self.assertEqual(london["parts"], 100)
        expected = {
            "source -> contrib.src_last": 0.2,
            "contrib.src_last -> contrib.packaged": 0.1,
            "contrib.packaged -> contrib.sent": 0.2,
            "contrib.sent -> primary.relay_first": 1.0,
            "primary.relay_first -> primary.relay_forward": 0.1,
            # The parent is lon-dist-a (the edge's mesh_first peer), not the
            # distributor that had the part first.
            "primary.relay_forward -> parent.relay_first": 1.0,
            "parent.relay_first -> parent.committed": 0.2,
            "parent.committed -> parent.watermark": 0.1,
            # mesh_sent to this edge, not to the other child.
            "parent.watermark -> parent.mesh_sent": 0.1,
            "parent.mesh_sent -> edge.mesh_first": 0.3,
            "edge.mesh_first -> edge.edge_committed": 0.3,
            # The first reply.
            "edge.edge_committed -> edge.reply": 0.1,
        }
        for name, ms in expected.items():
            self.assertEqual(hop(london, name)["p50"], ms, name)
            self.assertEqual(hop(london, name)["count"], 100, name)
        spike = hop(london, "edge.mesh_first -> edge.edge_committed")
        self.assertEqual(spike["p99"], 0.3)
        self.assertEqual(spike["max"], 10.3)
        self.assertEqual(stage(london, "edge.reply")["p50"], 3.7)
        self.assertEqual(stage(london, "edge.reply")["max"], 13.7)
        newyork = result["regions"]["newyork"]["edge_parts"]
        self.assertEqual(hop(newyork, "parent.mesh_sent -> edge.mesh_first")["p50"], 1.0)
        self.assertEqual(stage(newyork, "edge.edge_committed")["p50"], 41.5)
        self.assertEqual(result["regions"]["all"]["edge_parts"]["parts"], 200)
        self.assertIn("edge.mesh_decoded", result["stages_seen"])

    def test_listener_parts_join_the_probe(self):
        probe = hops.read_probe(str(write_probe(self.directory, "nt-pcm.probe.csv")))
        result = hops.analyse(NODES, journal(), [probe], STREAM, START_US)
        listener = result["regions"]["london"]["listener_parts"]
        self.assertEqual(listener["parts"], 100)
        # The listener's reply is its own (the header): the edge's second reply.
        self.assertEqual(hop(listener, "edge.edge_committed -> edge.reply")["p50"], 0.3)
        self.assertEqual(hop(listener, "edge.reply -> listener.complete")["p50"], 0.3)
        self.assertEqual(stage(listener, "listener.complete")["p50"], 4.2)
        self.assertEqual(stage(listener, "listener.complete")["max"], 14.2)
        self.assertEqual(result["listeners"][0]["edge_found_by_reply"], 100)
        # ntcmp's spoken time (complete - latency) is the journal's.
        self.assertEqual(result["probe_spoken_minus_journal_spoken_ms"]["max"], 0.0)
        self.assertEqual(result["probe_spoken_minus_journal_spoken_ms"]["min"], 0.0)

    def test_old_probe_files_and_no_source_clock(self):
        # An eight-column file: the part is the fifth column, no headers.
        # Without a source clock, the spoken time is the listener's own.
        probe = hops.read_probe("london=" + str(write_probe(self.directory, "old.probe.csv", old_format=True)))
        self.assertEqual(probe["site"], "london")
        result = hops.analyse(NODES, journal(), [probe], STREAM, None)
        listener = result["regions"]["london"]["listener_parts"]
        self.assertEqual(listener["parts"], 100)
        self.assertEqual(stage(listener, "listener.complete")["p50"], 4.2)
        self.assertEqual(stage(listener, "contrib.sent")["p50"], 0.5)
        # The playlist host is a distributor, not an edge: the edge is not known,
        # so no edge stage, and no reply from the journal.
        self.assertEqual(result["listeners"][0]["edge_by_playlist_host"], None)
        names = [item["hop"] for item in listener["hops"]]
        self.assertNotIn("edge.reply -> listener.complete", names)

    def two_london_edges(self) -> tuple[dict, list[dict]]:
        """lon-edge-2 also serves London and commits and replies within 2 ms of
        lon-edge-1. lon-edge-1 has used its 4 journaled replies on other readers
        before the listener's, so the listener's own reply is not in its journal."""
        nodes = {**NODES, "lon-edge-2": {"role": "edge", "site": "london",
                                         "addrs": ["10.90.0.22", "10.77.1.22"]}}
        events = [item for item in journal()
                  if not (item["_node"] == "lon-edge-1" and item["stage"] == "reply")]
        for seq in SEQS:
            s = spoken(seq)
            for n in range(1, 5):
                events.append(event("lon-edge-1", "reply", seq, s + 3600 + 10 * n, n=n))
            events += [
                event("lon-edge-2", "mesh_first", seq, s + 3500, peer="10.77.1.11:29101"),
                event("lon-edge-2", "edge_committed", seq, s + 3800),
                # 100 µs from the listener's reply on lon-edge-1 (s + 3900), and
                # nearer to it than any reply that lon-edge-1 journaled.
                event("lon-edge-2", "reply", seq, s + 4000 if seq != SPIKE else s + 13_800, n=1),
            ]
        return nodes, events

    def write_late_probe(self, host: str) -> dict:
        """A London listener of lon-edge-1, its 5th reader: reply s + 3900."""
        path = write_probe(self.directory, "late.probe.csv")
        (self.directory / "late.probe.json").write_text(
            json.dumps({"detail": {"playlist_url_host": host}}))
        return hops.read_probe(str(path))

    def test_a_near_reply_on_another_edge_is_not_the_listeners(self):
        nodes, events = self.two_london_edges()
        # The playlist host is not an edge: the edge comes from the headers.
        probe = self.write_late_probe("10.90.0.11:19444")
        result = hops.analyse(nodes, events, [probe], STREAM, START_US)
        found = result["listeners"][0]
        # No exact reply anywhere; lon-edge-2's reply 100 µs away is not taken.
        self.assertEqual(found["edge_found_by_reply"], 0)
        # The listener's edge-committed header is lon-edge-1's journal event.
        self.assertEqual(found["edge_found_by_edge_committed"], 100)
        self.assertEqual(found["edge_unknown"], 0)
        listener = result["regions"]["london"]["listener_parts"]
        # lon-edge-1's mesh_first (s + 3300), not lon-edge-2's (s + 3500).
        self.assertEqual(stage(listener, "edge.mesh_first")["p50"], 3.3)
        self.assertEqual(hop(listener, "parent.mesh_sent -> edge.mesh_first")["p50"], 0.3)
        self.assertEqual(hop(listener, "edge.edge_committed -> edge.reply")["p50"], 0.3)

    def test_the_playlist_host_edge_wins(self):
        nodes, events = self.two_london_edges()
        # Make lon-edge-2's reply exactly the listener's: the host still wins.
        events = [item if not (item["_node"] == "lon-edge-2" and item["stage"] == "reply")
                  else {**item, "t_us": item["t_us"] - 100} for item in events]
        probe = self.write_late_probe("10.90.0.21:19444")
        result = hops.analyse(nodes, events, [probe], STREAM, START_US)
        found = result["listeners"][0]
        self.assertEqual(found["edge_by_playlist_host"], "lon-edge-1")
        self.assertEqual(found["edge_found_by_reply"], 0)
        listener = result["regions"]["london"]["listener_parts"]
        self.assertEqual(stage(listener, "edge.mesh_first")["p50"], 3.3)

    def test_an_exact_reply_finds_the_edge(self):
        nodes, events = self.two_london_edges()
        probe = self.write_late_probe("10.90.0.11:19444")
        for row in probe["rows"].values():
            row["reply_us"] += 100              # lon-edge-2's reply exactly
            row["edge_committed_us"] = None
        result = hops.analyse(nodes, events, [probe], STREAM, START_US)
        found = result["listeners"][0]
        self.assertEqual(found["edge_found_by_reply"], 99)   # the spike part's reply differs
        self.assertEqual(found["edge_unknown"], 1)

    def test_seq_not_epoch_is_reported_and_epoch_wins(self):
        result = hops.analyse(NODES, journal(seq_offset=7), [], STREAM, START_US)
        self.assertFalse(result["seq_check"]["seq_equals_epoch"])
        self.assertEqual(result["seq_check"]["seq_minus_epoch"], {"7": 100})
        # The source time comes from the epoch, so the hops are unchanged.
        london = result["regions"]["london"]["edge_parts"]
        self.assertEqual(hop(london, "source -> contrib.src_last")["p50"], 0.2)

    def test_a_journal_that_starts_late_in_the_window_is_reported(self):
        # lon-edge-1's ring was overwritten: it lost its first 40 parts.
        events = [item for item in journal()
                  if not (item["_node"] == "lon-edge-1" and item["seq"] < 140)]
        since = spoken(100)
        # The synthetic run is half a second long: count 100 ms as late.
        with mock.patch.object(hops, "LATE_JOURNAL_US", 100_000):
            result = hops.analyse(NODES, events, [], STREAM, START_US, since_us=since)
            whole = hops.analyse(NODES, journal(), [], STREAM, START_US, since_us=since)
        self.assertEqual(list(result["journal_starts_late"]), ["lon-edge-1"])
        self.assertEqual(result["journal_starts_late"]["lon-edge-1"], 0.203)
        # Its mesh_first of part 140; the other stream's event is not counted.
        self.assertEqual(result["nodes"]["lon-edge-1"]["first_event_us"], spoken(140) + 3300)
        self.assertIn("WARNING: the journal starts late", hops.format_summary(result))
        self.assertIn("lon-edge-1 +0.2 s", hops.format_summary(result))
        self.assertEqual(whole["journal_starts_late"], {})
        self.assertNotIn("WARNING", hops.format_summary(whole))

    def test_time_window(self):
        since = spoken(150) + 0
        result = hops.analyse(NODES, journal(), [], STREAM, START_US, since_us=since)
        london = result["regions"]["london"]["edge_parts"]
        self.assertEqual(london["parts"], 50)

    def test_command_line_on_a_saved_journal(self):
        raw = self.directory / "raw.ndjson"
        hops.save_raw(raw, NODES, journal())
        probe = write_probe(self.directory, "nt-pcm.probe.csv")
        out = self.directory / "hops.json"
        summary = self.directory / "summary.txt"
        # --session-id is the sender's: START + 5 ms.
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = hops.main(["--journal", str(raw), "--probe", str(probe), "--session-id",
                              str((START_US + 5000) * 1000), "--out", str(out), "--summary", str(summary)])
        self.assertEqual(printed.getvalue(), summary.read_text())
        self.assertEqual(code, 0)
        result = json.loads(out.read_text())
        self.assertEqual(result["source"]["source_start_us"], START_US)
        london = result["regions"]["london"]["listener_parts"]
        self.assertEqual(stage(london, "listener.complete")["p50"], 4.2)
        self.assertEqual(result["nodes"]["lon-edge-1"]["chrony_offset_s"], -0.00002)
        text = summary.read_text()
        self.assertIn("london / listener_parts: 100 parts", text)
        self.assertIn("edge.reply -> listener.complete", text)


if __name__ == "__main__":
    unittest.main()
