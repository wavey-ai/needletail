#!/usr/bin/env python3
"""Tests of source-check.py and first-session.py (D-hunt.md 2.11, 6),
offline: no lab is started and nothing is run. A dry run of each prints
every command in the order the specification gives; source-check's table
splits each episode before publish by side (a stopped host, the pacer, the
link, the contributor's read) and counts the unattributed; first-session's
report sets each lab's first session against its warm one.
python3 -m unittest test_source_check.py (in scripts/compare)"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


source_check = load("source_check", "source-check.py")
first_session = load("first_session", "first-session.py")

START = 1_795_384_064


def refused(*_args, **_kwargs):
    raise AssertionError("a dry run ran something")


def dry(module, **args) -> list[str]:
    """A dry `run` of a tool: what it printed. Anything it ran would raise."""
    printed = io.StringIO()
    with mock.patch.object(module.subprocess, "run", refused), \
            mock.patch.object(module.subprocess, "Popen", refused), \
            mock.patch.object(module, "preflight", lambda: ["a problem"]), \
            contextlib.redirect_stdout(printed):
        status = module.command_run(argparse.Namespace(dry=True, **args))
    assert status == 0
    return printed.getvalue().splitlines()


class SourceCheckRun(unittest.TestCase):
    def test_a_dry_run_prints_every_command(self):
        lines = dry(source_check, seconds=600, payload="opus", epoch_samples=48, name="sc-test",
                    no_up=False)
        commands = [line for line in lines if line.startswith("$ ")]
        self.assertIn("# a real run would refuse: a problem", lines)
        self.assertIn("create sc-test --kind local-lab", commands[0])
        self.assertTrue(commands[1].endswith("edge_lab.py up"))
        self.assertIn("needletail-nodecheck watch", commands[2])
        sender = commands[3]
        for flag in ("edge_lab.py sh contrib-lon", "send --target 127.0.0.1:27100",
                     "--duration-seconds 600", "--payload opus", "--epoch-samples 48",
                     "--lateness-journal", "--repair-percent 12"):
            self.assertIn(flag, sender)
        session = sender.split("--session-id ")[1].split()[0]
        start = int(session[:-9])
        self.assertEqual(start % 64, 0)
        self.assertEqual(session[-9:], "001000000")
        self.assertIn("/api/stalls?since_us=", commands[4])
        self.assertIn("/api/hops?since_us=", commands[5])
        self.assertIn("stream=3001", commands[5])
        self.assertIn("/api/pacing", commands[6])
        self.assertIn("add-tree sc-test", commands[7])
        self.assertIn("finish sc-test --status complete", commands[8])
        self.assertFalse((source_check.OUT / "sc-test").exists())

    def test_the_loop_start(self):
        self.assertEqual(source_check.loop_start(START - 30), START)
        self.assertEqual(source_check.loop_start(START - 5), START + 64)


class SourceCheckTable(unittest.TestCase):
    def test_episodes_are_split_by_side(self):
        epoch = 1000
        ideal = lambda seq: START * 1_000_000 + (seq + 1) * epoch  # noqa: E731
        hops, sender, contributor, watch = [], [], [], []
        delay = {}                       # seq -> (arrival late, publish late) us
        for first, arrive, publish in ((10_000, 3000, 0),      # the pacer
                                       (20_000, 3000, 0),      # the link
                                       (30_000, 0, 3000),      # the contributor's read
                                       (40_000, 3000, 0),      # the host stopped
                                       (50_000, 0, 3000)):     # nothing said why
            for seq in range(first, first + 3):
                delay[seq] = (arrive, publish)
        for seq in range(5_000, 60_000):
            arrive, publish = delay.get(seq, (0, 0))
            src = ideal(seq) + 100 + arrive
            hops.append({"stage": "src_first", "stream": 3001, "seq": seq, "t_us": src})
            hops.append({"stage": "sent_first", "stream": 3001, "seq": seq,
                         "t_us": src + 80 + publish})
        sender.append({"kind": "late", "epoch": 10_000, "deadline_us": ideal(10_000),
                       "woke_late_us": 2900, "encode_us": 100, "late_first_us": 3000,
                       "late_last_us": 3000})
        contributor.append({"kind": "src_gap", "thread": "audio-hot-0", "cpu": 2,
                            "t_us": ideal(20_000) + 3100, "dur_us": 3000, "span_us": 3000, "n": 1,
                            "cause": "waited"})
        contributor.append({"kind": "rx_wait", "thread": "audio-hot-0", "cpu": 2,
                            "t_us": ideal(30_000) + 3100, "dur_us": 2900, "span_us": 2900, "n": 1,
                            "cause": "waited"})
        watch.append({"kind": "tick", "thread": "tick-3", "cpu": 3, "t_us": ideal(40_000) + 3000,
                      "dur_us": 3500, "span_us": 3500, "n": 1, "run_delay_us": 5,
                      "cause": "stopped", "scope": "all"})
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "source-check.json").write_text(json.dumps({
                "name": "sc", "start_unix": START, "payload": "opus", "epoch_samples": 48,
                "stream": 3001}))
            for file, items in (("contributor-hops.ndjson", hops), ("sender.ndjson", sender),
                                ("contributor-stalls.ndjson", contributor),
                                ("host-watch.ndjson", watch)):
                (root / file).write_text("".join(json.dumps(item) + "\n" for item in items))
            result = source_check.analyse(root)
        item = result["over_2000us"]
        self.assertEqual(len(item["episodes"]), 5)
        self.assertEqual([episode["side"] for episode in item["episodes"]],
                         ["pacer", "link", "contributor read", "stopped (the host)",
                          "unattributed"])
        self.assertEqual(item["unattributed"], 1)
        self.assertFalse(result["all_attributed"])
        self.assertEqual(result["baseline_over_1ms"], 5)
        rows = {row["first"]: row for row in result["over_500us"]["rows"]}
        self.assertAlmostEqual(rows[10_000]["woke_late_ms"], 2.9)
        self.assertAlmostEqual(rows[10_000]["link_ms"], 0.0)
        self.assertAlmostEqual(rows[20_000]["link_ms"], 3.0)
        self.assertAlmostEqual(rows[30_000]["rx_wait_ms"], 2.9)
        self.assertAlmostEqual(rows[30_000]["publish_ms"], 3.0)
        self.assertIn("3.1: 1 episodes over 0.5 ms unattributed (NOT met)",
                      source_check.format_report(result))


class FirstSession(unittest.TestCase):
    def test_a_dry_run_prints_every_command_in_the_referee_s_order(self):
        lines = dry(first_session, labs=2, idle_s=990.0, burn_s=20, seconds=70, payload="opus",
                    name="fs-test")
        commands = [line for line in lines if line.startswith(("$ ", "# idle"))]
        ups = [index for index, line in enumerate(commands) if line.endswith("edge_lab.py up")]
        self.assertEqual(len(ups), 2)
        lab = commands[ups[0]:ups[1]]
        kinds = []
        for line in lab:
            for word, kind in (("edge_lab.py up", "up"), ("nodecheck watch", "watch"),
                               ("# idle", "idle"), ("edge_lab.py burn", "burn"),
                               ("edge_lab.py restart contrib-lon", "restart"),
                               ("-first --no-up", "first"),
                               ("-warm --no-up", "warm"), ("/api/stalls", "stalls"),
                               ("edge_lab.py down", "down")):
                if word in line and (not kinds or kinds[-1] != kind):
                    kinds.append(kind)
        self.assertEqual(kinds, ["up", "watch", "idle", "burn", "restart", "first", "stalls",
                                 "warm", "stalls", "down"])
        burns = [line for line in lab if "edge_lab.py burn" in line]
        self.assertEqual(len(burns), len(first_session.mesh_nodes()))
        self.assertNotIn("lon-dist-c", " ".join(burns))
        first = next(line for line in lab if "-first --no-up" in line)
        for flag in ("--keep-up", "--skip-seconds 0", "--udp-clients 2", "--load-listeners 40",
                     "--load-assign", "--payload opus", "--seconds 70"):
            self.assertIn(flag, first)
        self.assertIn("# idle, as the referee's lab before its first session: 990 s", lines)
        self.assertFalse((first_session.OUT / "fs-test").exists())

    def test_the_report_sets_first_against_warm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bench = root / "bench"
            run = root / "run"
            for lab, first_p99 in ((1, 3.1), (2, 3.6)):
                sessions = {}
                for session, p99 in (("first", first_p99), ("warm", 3.0)):
                    name = f"fs-lab{lab}-{session}"
                    (bench / name).mkdir(parents=True)
                    (bench / name / "hops.json").write_text(json.dumps({"udp_clients": {"hosts": {
                        "lon-readers": {"unit_latency_ms": {"p99": p99, "max": 9.0},
                                        "missing": 0}}}}))
                    sessions[session] = {"bench": name}
                    (run / f"lab{lab}" / session).mkdir(parents=True)
                    (run / f"lab{lab}" / session / "lon-edge-1.stalls.ndjson").write_text(
                        json.dumps({"kind": "tick", "thread": "tick-0", "cpu": 0,
                                    "t_us": START * 1_000_000, "dur_us": 120_000,
                                    "span_us": 120_000, "n": 1, "run_delay_us": 3,
                                    "cause": "stopped", "scope": "all"}) + "\n")
                (run / f"lab{lab}" / "lab.json").write_text(json.dumps(
                    {"lab": lab, "sessions": sessions}))
            with mock.patch.object(first_session, "BENCH_OUT", bench):
                result = first_session.analyse(run, with_null=False)
        self.assertEqual([entry["gate"] for entry in result["labs"]],
                         [{"lon-readers": True}, {"lon-readers": False}])
        journal = result["labs"][0]["sessions"]["first"]["journals"]["lon-edge-1"]
        self.assertEqual(journal["stops"]["stopped"]["over_100ms"]["all"], 1)
        text = first_session.format_report(result)
        self.assertIn("gate lon-readers: first within 0.3 ms of warm and 0 missing: NOT met", text)


if __name__ == "__main__":
    unittest.main()
