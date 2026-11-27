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
import shutil
import subprocess
import sys
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

    def test_a_check_cut_off_keeps_what_it_got_and_ends_failed(self):
        ran = []

        def run(self, command, stdout=None, timeout=None):
            ran.append(command)
            if "send" in command:
                raise KeyboardInterrupt
            return 0

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(source_check, "OUT", pathlib.Path(directory)), \
                mock.patch.object(source_check, "preflight", lambda: []), \
                mock.patch.object(source_check.Steps, "run", run), \
                mock.patch.object(source_check.Steps, "background", lambda self, command: None), \
                self.assertRaises(KeyboardInterrupt):
            source_check.command_run(argparse.Namespace(
                dry=False, seconds=600, payload="opus", epoch_samples=48, name="sc-cut",
                no_up=True))
        archive = [command[4:] for command in ran if "--tool" in command]
        self.assertEqual([item[0] for item in archive], ["create", "add-tree", "finish"])
        self.assertEqual(archive[2][2:], ["--status", "failed", "--reason",
                                          "the check was cut off"])
        self.assertEqual(archive[0].count("--code"), 5)

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
        ups = [index for index, line in enumerate(commands) if "edge_lab.py up " in line]
        self.assertEqual(len(ups), 2)
        # The up gives what the bench's own would: the opus lane and hop
        # journals sized to the session (the first seconds are kept).
        self.assertIn("daw_hls_formats=opus", commands[ups[0]])
        self.assertIn("--hop-journal-capacity", commands[ups[0]])
        lab = commands[ups[0]:ups[1]]
        kinds = []
        for line in lab:
            for word, kind in (("edge_lab.py up", "up"), ("nodecheck watch", "watch"),
                               ("# idle", "idle"), ("edge_lab.py burn", "burn"),
                               ("edge_lab.py stop contrib-lon", "stop"),
                               ("edge_lab.py start contrib-lon", "start"),
                               ("hunt-sampler.sh", "sampler"),
                               ("-first --no-up", "first"),
                               ("-warm --no-up", "warm"), ("/api/stalls", "stalls"),
                               ("edge_lab.py down", "down")):
                if word in line and (not kinds or kinds[-1] != kind):
                    kinds.append(kind)
        self.assertEqual(kinds, ["up", "watch", "idle", "burn", "stop", "start", "sampler",
                                 "first", "stalls", "warm", "stalls", "down"])
        samplers = [line for line in lab if "hunt-sampler.sh" in line]
        self.assertEqual(len(samplers), len(first_session.mesh_nodes()))
        self.assertTrue(all(line.endswith(" &") for line in samplers))
        self.assertTrue(any("'<pid of contrib-lon>' audio-hot-0" in line and "/api/pacing" in line
                            for line in samplers))
        self.assertTrue(any("'<pid of lon-edge-1>' needletail-fast" in line for line in samplers))
        burns = [line for line in lab if "edge_lab.py burn" in line]
        self.assertEqual(len(burns), len(first_session.mesh_nodes()))
        self.assertNotIn("lon-dist-c", " ".join(burns))
        first = next(line for line in lab if "-first --no-up" in line)
        for flag in ("--keep-up", "--skip-seconds 0", "--udp-clients 2", "--load-listeners 40",
                     "--load-assign", "--payload opus", "--seconds 70"):
            self.assertIn(flag, first)
        self.assertIn("# idle, as the referee's lab before its first session: 990 s", lines)
        self.assertFalse((first_session.OUT / "fs-test").exists())

    def test_rt_and_the_first_session_alone(self):
        lines = dry(first_session, labs=1, idle_s=1.0, burn_s=1, seconds=70, payload="opus",
                    name="fs-test", up_args="rt=1 slots=lon-edge-4,new-edge-3",
                    bench_args="--sample-nodes", load_sites="london", no_warm=True)
        up = next(line for line in lines if "edge_lab.py up " in line)
        self.assertTrue(up.endswith("rt=1 slots=lon-edge-4,new-edge-3"))
        passes = [line for line in lines if "latency-bench.py --name fs-test-lab1-" in line]
        self.assertEqual(len(passes), 1)
        self.assertIn("-first --no-up", passes[0])
        self.assertTrue(passes[0].endswith("--load-sites london --sample-nodes"))

    def test_each_pass_writes_into_the_first_session_run(self):
        lines = dry(first_session, labs=1, idle_s=1.0, burn_s=1, seconds=70, payload="opus",
                    name="fs-test")
        passes = [line for line in lines if "latency-bench.py --name fs-test-lab1-" in line]
        self.assertEqual(len(passes), 2)
        self.assertTrue(all(line.endswith("--run-id fs-test") for line in passes))
        create = next(line for line in lines if "first-session create fs-test" in line)
        self.assertEqual(create.count("--code "), 5)
        self.assertFalse(any("add-tree fs-test" in line and "latency-bench" in line
                             for line in lines), "the passes are not copied a second time")

    def test_a_run_cut_off_keeps_the_lab_s_files_and_ends_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            ran = []

            def one_lab(steps, args, index):
                (pathlib.Path(directory) / "fs-cut" / f"lab{index}").mkdir(parents=True)
                if index == 2:
                    raise KeyboardInterrupt
                return {"lab": index}

            def run(self, command, stdout=None, timeout=None):
                ran.append(command)
                return 0

            with mock.patch.object(first_session, "OUT", pathlib.Path(directory)), \
                    mock.patch.object(first_session, "one_lab", one_lab), \
                    mock.patch.object(first_session, "preflight", lambda: []), \
                    mock.patch.object(first_session.Steps, "run", run), \
                    self.assertRaises(KeyboardInterrupt):
                first_session.command_run(argparse.Namespace(
                    dry=False, labs=3, idle_s=1.0, burn_s=1, seconds=70, payload="opus",
                    name="fs-cut"))
        archive = [command[4:] for command in ran if "--tool" in command]
        self.assertEqual([item[:2] for item in archive],
                         [["create", "fs-cut"], ["add-tree", "fs-cut"], ["add-tree", "fs-cut"],
                          ["finish", "fs-cut"]])
        self.assertTrue(archive[2][2].endswith("lab2"))
        self.assertEqual(archive[3][2:], ["--status", "failed", "--reason",
                                          "the run was cut off"])

    def test_a_step_past_its_timeout_gets_sigterm_before_sigkill(self):
        # A latency-bench pass then keeps its files in the run archive.
        with tempfile.TemporaryDirectory() as directory:
            said = pathlib.Path(directory) / "said"
            program = ("import pathlib, signal, sys, time\n"
                       f"def term(*_):\n    pathlib.Path({str(said)!r}).write_text('term')\n"
                       "    sys.exit(3)\n"
                       "signal.signal(signal.SIGTERM, term)\n"
                       "time.sleep(30)\n")
            steps = first_session.Steps(False)
            with contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(subprocess.TimeoutExpired):
                steps.run([sys.executable, "-c", program], timeout=1.0)
            self.assertEqual(said.read_text(), "term")

    def test_the_preflight_reads_the_lab_s_dispatch_not_its_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = pathlib.Path(directory) / "edge_lab.py"
            usage.write_text('"""edge_lab.py burn NODE SECONDS"""\nif command == "up":\n    pass\n')
            with mock.patch.object(first_session, "EDGE_LAB", usage):
                self.assertTrue(any("no `burn`" in line for line in first_session.preflight()))
            usage.write_text('if command == "up":\n    pass\nelif command == "burn":\n    pass\n')
            with mock.patch.object(first_session, "EDGE_LAB", usage):
                self.assertFalse(any("no `burn`" in line for line in first_session.preflight()))

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
                    sessions[session].update(
                        {"from_unix": START + (0 if session == "first" else 100),
                         "to_unix": START + (90 if session == "first" else 190)})
                (run / f"lab{lab}" / "lab.json").write_text(json.dumps(
                    {"lab": lab, "sessions": sessions}))
                # The sampler of lon-edge-1: in the first session its class
                # thread waits runnable 300 ms within one second.
                lines = []
                for second in range(0, 190):
                    delay = 300 if second >= 30 else 0
                    lines.append({"t_us": (START + second) * 1_000_000, "pid": 7, "tid": 9,
                                  "cpu": 0, "schedstat": [10**9, delay * 10**6 + second, 9],
                                  "csw": [second, 0], "vm": {}, "threads": [],
                                  "mesh": {"fast_plane.audio_loop_busy_max_us": 900}})
                samplers = run / f"lab{lab}" / "samplers" / "lon-edge-1"
                samplers.mkdir(parents=True)
                (samplers / "sampler.ndjson").write_text(
                    "".join(json.dumps(line) + "\n" for line in lines))
            with mock.patch.object(first_session, "BENCH_OUT", bench):
                result = first_session.analyse(run, with_null=False)
        self.assertEqual([entry["gate"] for entry in result["labs"]],
                         [{"lon-readers": True}, {"lon-readers": False}])
        journal = result["labs"][0]["sessions"]["first"]["journals"]["lon-edge-1"]
        self.assertEqual(journal["stops"]["stopped"]["over_100ms"]["all"], 1)
        stops = result["labs"][0]["sessions"]["first"]["class_stops"]["lon-edge-1"]
        self.assertEqual([(stop["ms"], stop["class"]) for stop in stops], [(300.0, "not run")])
        self.assertEqual(result["labs"][0]["sessions"]["warm"]["class_stops"]["lon-edge-1"], [])
        text = first_session.format_report(result)
        self.assertIn("lon-edge-1     class thread stopped 300 ms: not run", text)
        self.assertIn("gate lon-readers: first within 0.3 ms of warm and 0 missing: NOT met", text)


if __name__ == "__main__":
    unittest.main()


class CommitStops(unittest.TestCase):
    """A commit gap of 100 ms or more on a node of a pass's hop journals,
    with its kernel stamps and wakes over the same window and the 10 ms
    sampler's view of the class thread."""

    def test_a_stop_is_found_and_typed(self):
        import tempfile
        bench = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bench, True)
        lab_dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, lab_dir, True)
        events = [{"_meta": {}}]
        base = 1_000_000_000
        for seq in range(400):
            # Commits stop for 300 ms after part 200; the kernel stamps go on.
            at = base + seq * 1000
            commit = at + (300_000 if seq >= 200 else 0)
            for stage, t_us in (("relay_first", at), ("relay_woken", commit),
                                ("committed", commit)):
                events.append({"node": "lon-edge-1", "_node": "lon-edge-1", "stage": stage,
                               "stream": 3001, "seq": seq, "t_us": t_us})
            events.append({"node": "lon-edge-1", "_node": "lon-edge-1", "stage": "committed",
                           "stream": 4001, "seq": seq, "t_us": base})
        (bench / "hops.raw.ndjson").write_text("\n".join(json.dumps(item) for item in events) + "\n")
        fine = [{"meta": {}}]
        for step in range(60):
            t_us = base + 150_000 + step * 10_000
            # Run delay (ns) only grows while the thread waits runnable.
            delay = 5_000_000 + 1000 * max(0, min(t_us, base + 500_000) - (base + 200_000))
            state = ["R", "", 1_000_000, delay, 10, step, 16] \
                if base + 199_000 < t_us <= base + 500_000 else \
                ["S", "poll_schedule_timeout", 1_000_000, delay, 10, 0, 16]
            fine.append({"t_us": t_us, "n": {"lon-edge-1": state}})
        (lab_dir / "class-threads.ndjson").write_text("\n".join(json.dumps(item) for item in fine))
        found = first_session.session_stops(lab_dir, bench, 3001)["lon-edge-1"]
        self.assertEqual(found["commits"], 400)
        (stop,) = found["stops"]
        self.assertAlmostEqual(stop["ms"], 301.0)
        self.assertAlmostEqual(stop["relay_first_gap_ms"], 1.0, "the input did not stop")
        self.assertAlmostEqual(stop["relay_woken_gap_ms"], 301.0)
        self.assertEqual(set(stop["fine"]["states"]), {"R"})
        self.assertGreater(stop["fine"]["run_delay_ms"], 250, "runnable and not run")
        self.assertEqual(first_session.commit_stops(bench, 4001)["lon-edge-1"]["stops"], [])
        # A gap after the stream's end on another node (its tail) is no stop.
        with open(bench / "hops.raw.ndjson", "a") as handle:
            for seq, t_us in ((0, base), (1, base + 1000)):
                handle.write(json.dumps({"node": "new-edge-1", "_node": "new-edge-1",
                                         "stage": "committed", "stream": 3001, "seq": seq,
                                         "t_us": t_us}) + "\n")
        self.assertEqual(first_session.commit_stops(bench, 3001)["lon-edge-1"]["stops"], [])
