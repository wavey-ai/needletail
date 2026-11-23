#!/usr/bin/env python3
"""Tests of change-run.py with the lab faked: nothing is started.
python3 -m unittest scripts/compare/test_change_run.py"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("change_run", HERE / "change-run.py")
cr = importlib.util.module_from_spec(_spec)
sys.modules["change_run"] = cr
_spec.loader.exec_module(cr)
lab = cr.lab
bench = cr.bench

T0 = 1_763_900_000 * 1_000_000_000
# The real test of a command, before the fakes replace it.
RUNNABLE = cr.runnable


def parse(*argv: str):
    return cr.build_parser().parse_args(["run", "--name", "roll-edges", *argv])


class Clock:
    """Time that moves only when the runner sleeps or a faked command runs."""

    def __init__(self):
        self.t = T0

    def now_ns(self) -> int:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += int(seconds * 1e9)


class FakeLab:
    """The lab, the clients, the change command and the referee's tools, faked.

    `outcomes` is what CW says of each run in turn (True, False, None or
    "error"); `command` is how the change command behaves: its duration, the
    mark lines it prints (with {start} and {end} for its own times), its exit,
    and `behaviour`: None (it ends by itself), "hang" (it runs until it gets
    SIGINT, then prints its end and exits 130) or "sigterm" (the runner gets
    SIGTERM while it waits for it, then as "hang"). `hang` names the binaries
    of the probes and listeners that do not end by themselves. Every signal
    is recorded in `signals` and reaches no real process."""

    def __init__(self, case: unittest.TestCase, directory: pathlib.Path):
        self.dir = directory
        self.clock = Clock()
        self.store: dict = {"services.json": {"lon-edge-1": 1}}
        self.started: list = []
        self.ran: list = []
        self.outcomes: list = []
        self.command = {"seconds": 20.0, "lines": ["start {start} roll.edges",
                                                   "end {end} roll.edges"], "exit": 0}
        self.share = 1.0
        self.hang: set = set()
        self.signals: list = []
        self.by_pid: dict = {}
        tools = directory / "tools"
        tools.mkdir()
        for name in ("ntcmp", "change-window.py", "needletail-udp-client", "needletail-load"):
            (tools / name).write_text("")
        fake = self

        class Process:
            def __init__(self, command, stdout=None, stderr=None, **_kwargs):
                self.pid = 5000 + len(fake.by_pid)
                fake.by_pid[self.pid] = self
                self.returncode = 0
                self.signalled = False
                self.behaviour = None
                fake.started.append(command)
                # A command in a host's namespace is ["@host", ...] here.
                body = command[1:] if command[0].startswith("@") else command
                if body[:2] == ["bash", "-c"]:
                    fake.probe(body[2])
                    if any(name in body[2] for name in fake.hang):
                        self.returncode = None
                elif body[:1] == ["engine"] or body[:1] == ["sleep"]:
                    self.stdout = stdout
                    self.behaviour = fake.command.get("behaviour")
                    if self.behaviour:
                        self.returncode = None
                        stdout.write(f"start {cr.unix_ns_text(fake.clock.t)} roll.edges\n"
                                     .encode())
                        stdout.flush()
                    else:
                        self.returncode = fake.change(stdout, marks=body[0] == "engine")
                elif any(name in " ".join(body) for name in fake.hang):
                    self.returncode = None

            def wait(self, timeout=None):
                if self.behaviour == "sigterm" and not self.signalled:
                    self.signalled = True
                    # Never the default action: that would end the tests.
                    assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL,
                                                                    signal.SIG_IGN)
                    signal.raise_signal(signal.SIGTERM)
                if self.returncode is None:
                    fake.clock.sleep(timeout)
                    raise subprocess.TimeoutExpired("fake", timeout)
                return self.returncode

            def poll(self):
                return self.returncode

            def signal(self, signum):
                fake.signals.append((self.pid, signum))
                if self.returncode is not None:
                    return
                if signum == signal.SIGINT and self.behaviour:
                    # The engine closes what it opened.
                    self.stdout.write(f"end {cr.unix_ns_text(fake.clock.t)} roll.edges\n"
                                      .encode())
                    self.stdout.flush()
                    self.returncode = 130
                elif signum == signal.SIGKILL:
                    self.returncode = -9

        class Interference:
            def __init__(self, cpus):
                pass

            def start(self):
                return self

            def stop(self):
                return {"seconds": 360.0, "cpus": 14, "outside_share_pct": fake.share,
                        "steal_pct": 0.0}

        patches = [
            mock.patch.object(cr, "out_root", lambda: directory / "out"),
            mock.patch.object(cr, "ntcmp", lambda: tools / "ntcmp"),
            mock.patch.object(cr, "change_window", lambda: tools / "change-window.py"),
            mock.patch.object(cr, "now_ns", self.clock.now_ns),
            mock.patch.object(cr, "sleep", self.clock.sleep),
            mock.patch.object(cr, "Interference", Interference),
            mock.patch.object(cr, "FLOOR_FILE", directory / "floor.json"),
            mock.patch.object(cr.subprocess, "Popen", Process),
            mock.patch.object(cr.os, "killpg", lambda pid, signum: fake.by_pid[pid].signal(signum)),
            mock.patch.object(cr, "runnable", lambda program: program != "missing"),
            mock.patch.object(cr.subprocess, "run", self.run),
            mock.patch.object(bench, "UDP_CLIENT", tools / "needletail-udp-client"),
            mock.patch.object(bench, "LOAD_BIN", tools / "needletail-load"),
            mock.patch.object(bench, "loop_start", lambda now: int(now) + 8),
            mock.patch.object(bench, "stop_sender",
                              lambda process: fake.ran.append(("stop_sender", process.pid))),
            mock.patch.object(bench, "os_kill_group",
                              lambda pid: fake.by_pid[pid].signal(signal.SIGKILL)),
            mock.patch.object(lab, "up", lambda overrides: fake.ran.append(("up", overrides))),
            mock.patch.object(lab, "down", lambda quiet=False: fake.ran.append(("down",))),
            mock.patch.object(lab, "join_tools", lambda: fake.ran.append(("join_tools",))),
            mock.patch.object(lab, "wait_live",
                              lambda stream, site, timeout=40: fake.ran.append(("live", stream, site))),
            mock.patch.object(lab, "enter", lambda name: [] if name is None else [f"@{name}"]),
            mock.patch.object(lab, "load", lambda name, default=None:
                              json.loads(json.dumps(fake.store.get(name, {} if default is None
                                                                   else default)))),
            mock.patch.object(lab, "save", lambda name, value: fake.store.__setitem__(name, value)),
            mock.patch.object(lab, "binary", lambda label, build="a": f"/lab/bin/{label}"),
            mock.patch.object(lab, "ca_file", lambda: "/lab/ca.crt"),
            mock.patch.object(lab, "service_names", lambda role=None, site=None: ["lon-edge-1"]),
            mock.patch.object(lab, "service_pid", lambda name: 7),
            mock.patch.object(lab, "node_status", lambda name: {"state": "ready"}),
            mock.patch.object(lab, "set_net", lambda *a: fake.ran.append(("net", *a))),
            mock.patch.object(lab, "set_wan", lambda *a: fake.ran.append(("wan", *a))),
            mock.patch.object(lab, "set_tcp_per_packet",
                              lambda on, site=None: fake.ran.append(("per_packet", on))),
        ]
        for patcher in patches:
            patcher.start()
            case.addCleanup(patcher.stop)

    def probe(self, script: str) -> None:
        launch = shlex.split(re.match(r"date \+%s\.%N >(\S+);", script).group(1))[0]
        pathlib.Path(launch).write_text(cr.unix_ns_text(self.clock.t) + "\n")

    def change(self, stdout, marks: bool) -> int:
        """The change command: an engine prints its marks, `sleep` none."""
        start = self.clock.t
        self.clock.sleep(self.command["seconds"])
        values = {"start": cr.unix_ns_text(start + 1_000_000),
                  "end": cr.unix_ns_text(self.clock.t - 1_000_000)}
        lines = self.command["lines"] if marks else []
        stdout.write(("roll: gate ok\n" + "".join(line.format(**values) + "\n"
                                                  for line in lines)).encode())
        stdout.flush()
        return self.command["exit"]

    def run(self, command, stdout=None, stderr=None, **_kwargs):
        self.started.append(command)
        if command[1] == "udp-ingest":
            frames = pathlib.Path(command[command.index("--frames") + 1])
            frames.write_text("kind,index,latency_us,complete_unix_us,first_unix_us\n")
            return subprocess.CompletedProcess(command, 0)
        # change-window.py
        frames = [command[i + 1] for i, item in enumerate(command) if item == "--frames"]
        marks = pathlib.Path(command[command.index("--marks") + 1]).read_text().split("\n")
        labels = sorted({line.split()[2] for line in marks if line})
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if outcome == "error":
            result = {"error": "no audio units"}
        elif outcome is None:
            result = {"pass": None, "change": {"missing": 0, "gap_ms": 3.0, "p99_ms": 7.0},
                      "null_windows": 80,
                      "note": "null too short: 80 no-change windows of this length, 120 needed"}
        else:
            result = {"pass": outcome, "change": {"missing": 0, "gap_ms": 3.0, "p99_ms": 7.0},
                      "gap_bar_ms": 4.0, "p99_bar_ms": 8.0, "null_fail_rate": 0.07,
                      "null_windows": 250}
        verdict = {label: {path: dict(result) for path in frames} for label in labels}
        pathlib.Path(command[command.index("--json") + 1]).write_text(json.dumps(verdict))
        return subprocess.CompletedProcess(command, 0 if outcome is True else 1)

    def main(self, *argv: str) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            code = cr.main(list(argv))
        return code, out.getvalue()


class Commands(unittest.TestCase):
    """The commands of a run, as the referee runs them."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.fake = FakeLab(self, self.dir)

    def test_the_commands_of_a_run(self):
        args = parse("--set", "daw_hls_formats=flac,opus,pcm", "--", "engine", "roll")
        self.assertEqual(cr.lab_overrides(args),
                         ["daw_hls_formats=opus,pcm", "opus_epoch_samples=48",
                          "pcm_epoch_samples=48", "daw_hls_formats=flac,opus,pcm"])
        senders = cr.sender_commands(cr.lanes_of(args.lanes), 1_763_900_032, 1500)
        self.assertEqual([command[command.index("--payload") + 1] for command in senders],
                         ["opus", "pcm"])
        # The Opus sender's session is the loop start plus one epoch exactly:
        # ntcmp reads Opus parts by number.
        self.assertEqual(senders[0][senders[0].index("--session-id") + 1],
                         "1763900032001000000")
        self.assertEqual(senders[1][senders[1].index("--session-id") + 1],
                         "1763900032001000001")
        self.assertEqual(senders[0][senders[0].index("--pcm-s24le-file") + 1],
                         str(cr.compare_root() / "target/compare-media/audio/audio.s24le"))
        self.assertEqual(senders[0][senders[0].index("--duration-seconds") + 1], "1500")
        # The senders' bound: to START (8 s), 60 s of waiting to be live for
        # each lane and site, then each repeat's probes (360 s), their grace
        # (90 s), the command's stop (30 s) and the scoring (300 s).
        args.repeat = 2
        self.assertEqual(cr.sender_seconds(args, 2, int(cr.now()) + 8),
                         8 + 4 * 60 + 2 * (360 + 90 + 30 + 300))
        probes = cr.probe_items(args, "roll-edges", self.dir)
        self.assertEqual([item["key"] for item in probes],
                         ["opus-1@lon-readers", "pcm-1@lon-readers",
                          "opus-1@new-readers", "pcm-1@new-readers"])
        client = probes[3]["client"]
        self.assertEqual(client[:3], [str(bench.UDP_CLIENT), "--entry", "https://10.90.2.11:19444"])
        for flag, value in (("--stream", "4001"), ("--duration-seconds", "360"),
                            ("--session", "change-roll-edges-new-readers-pcm-1"),
                            ("--server-name", lab.SERVER_NAME)):
            self.assertEqual(client[client.index(flag) + 1], value)
        self.assertIn("--arrivals", client)
        self.assertIn("--write-parts", client)
        script = probes[3]["command"][2]
        self.assertTrue(script.startswith(f"date +%s.%N >{probes[3]['files']['launch']}; exec "))
        pathlib.Path(probes[0]["files"]["launch"]).write_text("1763900040.123456789\n")
        ingest = cr.ingest_command(probes[0], "roll-edges-opus-1@lon-readers")
        self.assertEqual(ingest[:2], [str(cr.ntcmp()), "udp-ingest"])
        for flag, value in (("--started", "1763900040.123456789"), ("--skip-seconds", "5"),
                            ("--lane", "nt-uc"), ("--frames", probes[0]["files"]["probe.csv"]),
                            ("--audio-index", str(cr.audio_media() / "nt-popus-48s-la.index.json"))):
            self.assertEqual(ingest[ingest.index(flag) + 1], value)
        scored = cr.score_command(probes, self.dir)
        self.assertEqual(scored[1], str(cr.change_window()))
        self.assertEqual(scored.count("--frames"), 4)
        self.assertEqual(scored[-4:], ["--marks", str(self.dir / "marks.txt"),
                                       "--json", str(self.dir / "verdict.json")])

    def test_the_crowd_the_readers_and_the_two_point_five_lane(self):
        args = parse("--lanes", "opus-2.5", "--hosts", "lon-readers", "--readers", "4",
                     "--load-listeners", "40", "--", "engine")
        probes = cr.probe_items(args, "x", self.dir)
        self.assertEqual(len(probes), 1)
        self.assertEqual(cr.LANES[probes[0]["lane"]]["index"], "nt-popus-120s-la")
        self.assertEqual(cr.lab_overrides(args), ["daw_hls_formats=opus", "opus_epoch_samples=120"])
        readers = cr.reader_items(args, "x", self.dir)
        command = readers[0]["command"]
        self.assertEqual(command[0], "/lab/bin/needletail-reader")
        self.assertEqual([command[i + 1] for i, item in enumerate(command) if item == "--entry"],
                         ["https://10.90.0.11:19444", "https://10.90.0.12:19444"])
        self.assertEqual(command[command.index("--readers") + 1], "4")
        # Steered: the readers follow the region's Content Steering.
        self.assertIn("--steering", command)
        crowds = cr.crowd_items(args, "x", self.dir)
        self.assertEqual([item["site"] for item in crowds], ["london"])
        self.assertEqual(crowds[0]["command"][crowds[0]["command"].index("--listeners") + 1],
                         str(40 * len(lab.edges("london"))))
        self.assertEqual(crowds[0]["command"][crowds[0]["command"].index("--stream") + 1], "3001")

    def test_losses_keep_the_links_delay(self):
        self.assertEqual(cr.wan_losses(["newyork=loss 1%"]),
                         {"newyork": f"{lab.netem_spec(delay_ms=lab.SITE_DELAY_MS['newyork'])} "
                                     "loss 1%"})
        for bad in (["mars=loss 1%"], ["newyork="], ["newyork=delay 3ms"]):
            with self.assertRaises(SystemExit):
                cr.wan_losses(bad)


class Arguments(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(cr, "runnable", lambda program: program != "missing")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_short_probes_and_early_changes_are_refused(self):
        for argv in (("--seconds", "300"), ("--at", "5"), ("--lanes", "opus-1,opus-2.5"),
                     ("--lanes", "flac-1"), ("--dry", "--", "sleep", "3"), ("--name", "a b"),
                     ("--clients", "0"), ("--no-up", "--set", "x=1"), ("--hosts", "nowhere"),
                     ("--", "missing", "roll")):
            command = [] if "sleep" in argv or "missing" in argv else ["--", "engine"]
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                cr.check(parse(*argv, *command))
        with self.assertRaises(SystemExit):
            cr.check(parse())
        cr.check(parse("--", "engine", "roll"))

    def test_dry_appends_dry_marks_and_is_the_null(self):
        args = parse("--dry", "--", "scripts/edge_lab.py", "roll", "edges")
        self.assertEqual(cr.change_command(args),
                         ["scripts/edge_lab.py", "roll", "edges", "--dry-marks"])
        self.assertEqual(cr.kind_of(args), "null")
        self.assertEqual(cr.kind_of(parse("--", "/usr/bin/sleep", "3")), "null")
        self.assertEqual(cr.kind_of(parse("--", "scripts/edge_lab.py", "crash", "x")), "change")
        self.assertEqual(cr.lanes_of("pcm-1,opus-1,pcm-1"), ["opus-1", "pcm-1"])
        # The engine's refusal scenarios name their exit; a sleep exits 0.
        self.assertEqual(cr.expected_exit(parse("--expect-exit", "3", "--dry", "--", "engine")), 3)
        self.assertEqual(cr.expected_exit(parse("--expect-exit", "3", "--", "sleep", "20")), 0)

    def test_a_command_that_is_not_executable_is_refused_before_anything_starts(self):
        script = pathlib.Path(tempfile.mkdtemp()) / "edge_lab.py"
        script.write_text("print('x')\n")
        script.chmod(0o644)
        with mock.patch.object(cr, "runnable", RUNNABLE):
            with self.assertRaises(SystemExit) as refused:
                cr.check(parse("--", str(script), "kill", "contrib-lon"))
            self.assertIn("python3 SCRIPT", str(refused.exception))
            cr.check(parse("--", sys.executable, str(script), "kill", "contrib-lon"))


class Marks(unittest.TestCase):
    def test_the_marks_filter(self):
        text = ("roll edges: gate ok\nstart 1763900045.000000001 roll.edges\n"
                "mark start 1763900046.5 roll.edges.lon-edge-1\n"
                "start 1763900046 no.fraction\nend 1763900070.25 roll.edges trailing\n"
                "  end 1763900080.123456789 roll.edges  \n")
        self.assertEqual(cr.marks_of(text),
                         [("start", 1_763_900_045_000_000_001, "roll.edges"),
                          ("end", 1_763_900_080_123_456_789, "roll.edges")])
        marks, events = cr.close_marks(cr.marks_of(text), 1, 2, "ignored")
        self.assertEqual(events, [])
        self.assertEqual(cr.marks_text(marks),
                         "start 1763900045.000000001 roll.edges\n"
                         "end 1763900080.123456789 roll.edges\n")

    def test_a_command_without_marks_is_wrapped_in_the_scenario_s_label(self):
        marks, events = cr.close_marks([], T0 + 5, T0 + 9_000_000_007, "crash-edge")
        self.assertEqual(events, [])
        self.assertEqual(cr.marks_text(marks), "start 1763900000.000000005 crash-edge\n"
                                               "end 1763900009.000000007 crash-edge\n")

    def test_a_dead_command_s_label_is_closed(self):
        marks, events = cr.close_marks([("start", T0, "roll.edges"), ("start", T0 + 1, "x"),
                                        ("end", T0 + 2, "x")], T0 - 5, T0 + 30_000_000_000,
                                       "roll-edges")
        self.assertEqual(marks[-1], ("end", T0 + 30_000_000_000, "roll.edges"))
        self.assertEqual(events, [{"event": "aborted", "label": "roll.edges",
                                   "unix_ns": T0 + 30_000_000_000}])

    def test_the_probe_must_outlive_the_window(self):
        marks = [("start", T0, "c"), ("end", T0 + 20_000_000_000, "c")]
        self.assertIsNone(cr.probe_too_short(marks, T0 + 26_000_000_000))
        self.assertIn("probe too short", cr.probe_too_short(marks, T0 + 25_999_999_999))


class OutsideShare(unittest.TestCase):
    STAT = ("cpu  9 9 9 9 9 9 9 9 0 0\n"
            "cpu2 100 10 50 900 0 0 0 4 0 0\n"
            "cpu3 200 0 100 700 0 0 0 0 0 0\n"
            "cpu4 1 1 1 1 1 1 1 1 0 0\nintr 5\n")

    def test_proc_stat_is_read_for_the_lab_s_cpus(self):
        self.assertEqual(cr.read_cpu_times([2, 3], self.STAT), {2: (160, 4), 3: (300, 0)})

    def test_the_outside_share_arithmetic(self):
        # Two CPUs for 10 s: 20 s of CPU time. Busy 100 ticks of 10 ms = 1 s
        # (5%), of which the lab's scopes used 0.6 s (3%): 0.4 s outside (2%);
        # steal 10 ticks = 0.1 s (0.5%).
        samples = [{"t": 0.0, "cpu": {2: (0, 0), 3: (0, 0)}, "scopes": {"lon-edge-1": 1000}},
                   {"t": 10.0, "cpu": {2: (60, 10), 3: (40, 0)},
                    "scopes": {"lon-edge-1": 601_000}}]
        share = cr.outside_share(samples, 100)
        self.assertEqual(share["cpus"], 2)
        self.assertEqual(share["busy_pct"], 5.0)
        self.assertEqual(share["lab_pct"], 3.0)
        self.assertEqual(share["outside_share_pct"], 2.0)
        self.assertEqual(share["steal_pct"], 0.5)

    def test_a_restarted_scope_counts_from_zero(self):
        samples = [{"t": 0.0, "cpu": {2: (0, 0)}, "scopes": {"a": 500_000, "b": 100}},
                   # a restarted (its count began again), b stopped, c started.
                   {"t": 1.0, "cpu": {2: (50, 0)}, "scopes": {"a": 200_000, "c": 50_000}},
                   {"t": 2.0, "cpu": {2: (100, 0)}, "scopes": {"a": 300_000, "c": 150_000}}]
        self.assertEqual(cr.scope_delta_usec(samples), 200_000 + 50_000 + 100_000 + 100_000)
        share = cr.outside_share(samples, 100)
        self.assertEqual(share["outside_share_pct"], 27.5)
        # The lab's own time above the CPUs' busy time is no negative share.
        samples[-1]["scopes"]["a"] = 5_000_000
        self.assertEqual(cr.outside_share(samples, 100)["outside_share_pct"], 0.0)
        self.assertIn("error", cr.outside_share(samples[:1], 100))

    def test_a_run_above_the_floor_is_void(self):
        self.assertIsNone(cr.void_reason({"outside_share_pct": 5.0}, None))
        self.assertIsNone(cr.void_reason({"outside_share_pct": 5.0}, 3.0))
        self.assertIn("void", cr.void_reason({"outside_share_pct": 5.01}, 3.0))


class Runs(unittest.TestCase):
    """Whole runs on the faked lab."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.fake = FakeLab(self, self.dir)
        self.scenario = self.dir / "out" / "roll-edges"

    def test_dry_and_repeat_report_passes_of_n_beside_the_null(self):
        self.fake.outcomes = [True, True]
        code, out = self.fake.main("run", "--name", "roll-edges", "--repeat", "2", "--dry",
                                   "--", "engine", "roll", "edges")
        self.assertEqual(code, 0)
        self.assertIn("roll-edges: null 2 of 2 pass", out)
        dry = [command for command in self.fake.started if command[:1] == ["engine"]]
        self.assertEqual(dry, [["engine", "roll", "edges", "--dry-marks"]] * 2)
        # The lab came up once with the lanes' settings and went down once;
        # every stream was live in both sites before the probes started.
        self.assertEqual([item[0] for item in self.fake.ran if item[0] in ("up", "down")],
                         ["up", "down"])
        self.assertEqual(sorted(item[1:] for item in self.fake.ran if item[0] == "live"),
                         [(3001, "london"), (3001, "newyork"), (4001, "london"),
                          (4001, "newyork")])
        self.fake.outcomes = [True, False, True]
        code, out = self.fake.main("run", "--name", "roll-edges", "--repeat", "3",
                                   "--", "engine", "roll", "edges")
        self.assertEqual(code, 1)
        self.assertIn("roll-edges: change 2 of 3 pass", out)
        self.assertIn("roll-edges (host): change 2 of 3 pass; null 2 of 2 pass", out)
        self.assertIn("roll.edges opus-1@lon-readers: FAIL missing 0 gap 3/4 ms p99 7/8 ms "
                      "null fail rate 0.07", out)
        record = json.loads((self.scenario / "summary.json").read_text())
        self.assertEqual(record["tally"], {"host": {
            "change": {"pass": 2, "fail": 1, "void": 0, "unscored": 0, "interrupted": 0},
            "null": {"pass": 2, "fail": 0, "void": 0, "unscored": 0, "interrupted": 0}}})
        run = json.loads((self.scenario / "change-02" / "run.json").read_text())
        self.assertEqual(run["status"], "fail")
        self.assertEqual(run["kind"], "change")
        self.assertEqual(sorted(run["labels"]["roll.edges"]),
                         ["opus-1@lon-readers", "opus-1@new-readers", "pcm-1@lon-readers",
                          "pcm-1@new-readers"])
        directory = self.scenario / "change-01"
        self.assertEqual((directory / "marks.txt").read_text().split()[0::3], ["start", "end"])
        for name in ("command.out", "command.err", "verdict.json", "nodes-before.json",
                     "nodes-after.json"):
            self.assertTrue((directory / name).is_file(), name)

    def test_a_null_that_fails_is_reported_before_any_change(self):
        self.fake.outcomes = [False]
        code, out = self.fake.main("run", "--name", "roll-edges", "--", "sleep", "20")
        self.assertEqual(code, 1)
        self.assertIn("WARNING: the null failed 1 of 1", out)
        # A sleep prints no marks: the runner wraps it in the scenario's label.
        marks = (self.scenario / "null-01" / "marks.txt").read_text().split()
        self.assertEqual(marks[2::3], ["roll-edges", "roll-edges"])

    def test_the_change_runs_at_its_time_and_the_probe_outlives_it(self):
        code, _out = self.fake.main("run", "--name", "roll-edges", "--at", "45",
                                    "--load-listeners", "0", "--readers", "0",
                                    "--", "engine", "roll", "edges")
        self.assertEqual(code, 0)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        launch = float(pathlib.Path(run["probes"][0]["files"]["launch"]).read_text())
        start = float((self.scenario / "change-01" / "marks.txt").read_text().split()[1])
        self.assertAlmostEqual(start - launch, 45.001, places=3)
        self.fake.command["seconds"] = 330.0
        code, _out = self.fake.main("run", "--name", "roll-edges", "--load-listeners", "0",
                                    "--readers", "0", "--", "engine", "roll", "edges")
        self.assertEqual(code, 1)
        run = json.loads((self.scenario / "change-02" / "run.json").read_text())
        self.assertEqual(run["status"], "fail")
        self.assertIn("probe too short", run["reasons"][0])

    def test_a_dead_command_is_scored_with_its_label_closed(self):
        self.fake.command = {"seconds": 5.0, "lines": ["start {start} roll.edges"], "exit": -9}
        code, out = self.fake.main("run", "--name", "roll-edges", "--", "engine", "roll")
        # CW scored the window (its label closed), and passed it; but the
        # change died: the run fails.
        self.assertEqual(code, 1)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        self.assertEqual(run["command_exit"], -9)
        self.assertEqual(run["status"], "fail")
        self.assertIn("the command exited -9; the scenario expects 0 (--expect-exit)",
                      run["reasons"])
        self.assertTrue(all(result["pass"] for result in run["labels"]["roll.edges"].values()))
        self.assertEqual(run["events"][0]["event"], "aborted")
        self.assertEqual((self.scenario / "change-01" / "marks.txt").read_text().split()[0::3],
                         ["start", "end"])
        self.assertIn("roll-edges: change 0 of 1 pass", out)

    def test_a_refusal_passes_on_its_own_exit_and_a_refused_change_fails(self):
        # A gate that refuses (exit 3) changes nothing, and CW passes the
        # window: as a change it is no proof.
        self.fake.command["exit"] = 3
        code, _out = self.fake.main("run", "--name", "roll-edges", "--", "engine", "roll")
        self.assertEqual(code, 1)
        self.assertIn("the command exited 3; the scenario expects 0 (--expect-exit)",
                      json.loads((self.scenario / "change-01" / "run.json").read_text())
                      ["reasons"])
        # gate-refuses-dead expects it, and its dry null refuses too.
        for dry in ((), ("--dry",)):
            code, _out = self.fake.main("run", "--name", "gate-refuses-dead", "--expect-exit", "3",
                                        *dry, "--", "engine", "roll", "forwarders")
            self.assertEqual(code, 0)
        self.fake.command["exit"] = 0
        code, _out = self.fake.main("run", "--name", "gate-refuses-dead", "--expect-exit", "3",
                                    "--", "engine", "roll", "forwarders")
        self.assertEqual(code, 1)
        # The expected exit is part of the scenario.
        with self.assertRaises(SystemExit):
            self.fake.main("run", "--name", "gate-refuses-dead", "--", "engine", "roll",
                           "forwarders")

    def test_an_error_of_the_referee_is_a_failure(self):
        self.fake.outcomes = ["error"]
        code, out = self.fake.main("run", "--name", "roll-edges", "--", "engine")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads((self.scenario / "change-01" / "run.json").read_text())
                         ["status"], "fail")
        self.assertIn("FAIL missing - gap -/- ms p99 -/- ms null fail rate - (no audio units)", out)

    def test_only_scored_runs_count_and_ten_of_each_prove(self):
        # Ten changes and ten nulls pass; a void change, an unscored null and
        # an interrupted run count neither way.
        self.fake.main("run", "--name", "roll-edges", "--repeat", "10", "--", "engine")
        code, out = self.fake.main("run", "--name", "roll-edges", "--repeat", "9", "--dry",
                                   "--", "engine")
        self.assertNotIn("proved locally", out)
        self.fake.outcomes = [True, None]
        self.fake.main("run", "--name", "roll-edges", "--repeat", "2", "--dry", "--", "engine")
        self.fake.share = 9.0
        (self.dir / "floor.json").write_text(json.dumps({"outside_share_pct": 1.0,
                                                          "layout": "host"}))
        self.fake.main("run", "--name", "roll-edges", "--", "engine")
        # A runner killed in its run leaves a run.json with no status.
        killed = json.loads((self.scenario / "change-01" / "run.json").read_text())
        for key in ("status", "complete"):
            killed.pop(key)
        (self.scenario / "change-12").mkdir()
        (self.scenario / "change-12" / "run.json").write_text(json.dumps(killed))
        record, lines = cr.summary(self.scenario)
        self.assertEqual(record["tally"]["host"]["change"],
                         {"pass": 10, "fail": 0, "void": 1, "unscored": 0, "interrupted": 1})
        self.assertEqual(record["tally"]["host"]["null"],
                         {"pass": 10, "fail": 0, "void": 0, "unscored": 1, "interrupted": 0})
        self.assertEqual(lines, ["roll-edges (host): change 10 of 10 pass (not scored: 1 void, "
                                 "1 interrupted); null 10 of 10 pass (not scored: 1 unscored); "
                                 "proved locally"])
        self.assertEqual(record["proved"], {"host": True})
        # A void or unscored null is no failed null: no warning. A failed one is.
        self.fake.share = 1.0
        self.fake.outcomes = [False]
        _code, out = self.fake.main("run", "--name", "roll-edges", "--dry", "--", "engine")
        self.assertIn("null 10 of 11 pass", out)
        self.assertIn("WARNING: the null failed 1 of 11", out)
        self.assertNotIn("proved locally", out)

    def test_a_name_holds_one_scenario(self):
        self.fake.main("run", "--name", "roll-edges", "--", "engine", "roll", "edges")
        with self.assertRaises(SystemExit):
            self.fake.main("run", "--name", "roll-edges", "--", "engine", "roll", "forwarders")
        with self.assertRaises(SystemExit):
            self.fake.main("run", "--name", "roll-edges", "--at", "60", "--", "engine", "roll",
                           "edges")
        # The null of a plain kind is a sleep under the same name.
        code, _out = self.fake.main("run", "--name", "roll-edges", "--", "sleep", "20")
        self.assertEqual(code, 0)

    def test_losses_are_put_on_and_taken_off(self):
        self.fake.main("run", "--name", "roll-edges", "--client-loss", "loss 1%",
                       "--wan-loss", "newyork=loss 1%", "--", "engine")
        nets = [item for item in self.fake.ran if item[0] in ("net", "wan", "per_packet")]
        self.assertEqual([item[:2] for item in nets],
                         [("per_packet", True), ("net", "lon-readers"), ("net", "new-readers"),
                          ("wan", "newyork"), ("net", "lon-readers"), ("net", "new-readers"),
                          ("wan", "newyork"), ("per_packet", False)])
        self.assertTrue(nets[1][4].endswith("loss 1%"))
        self.assertIsNone(nets[4][4])

    def test_no_up_uses_the_running_lab(self):
        self.fake.main("run", "--name", "roll-edges", "--no-up", "--", "engine")
        self.assertIn(("join_tools",), self.fake.ran)
        self.assertFalse([item for item in self.fake.ran if item[0] in ("up", "down")])
        self.fake.store.pop("services.json")
        with self.assertRaises(SystemExit):
            self.fake.main("run", "--name", "roll-edges", "--no-up", "--", "engine")

    def test_a_run_above_the_floor_is_void_and_score_applies_a_later_floor(self):
        self.fake.share = 6.0
        code, _out = self.fake.main("run", "--name", "roll-edges", "--", "engine")
        self.assertEqual(code, 0)
        # A floor measured on another CPU range does not apply.
        (self.dir / "floor.json").write_text(json.dumps({"outside_share_pct": 0.5,
                                                          "layout": "cpus=2-15"}))
        code, _out = self.fake.main("score", str(self.scenario / "change-01"))
        self.assertEqual(code, 0)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        self.assertIsNone(run["floor_pct"])
        self.assertIn("no floor for host (it was measured on cpus=2-15)", run["floor_note"])
        (self.dir / "floor.json").write_text(json.dumps({"outside_share_pct": 3.5,
                                                          "layout": "host"}))
        code, out = self.fake.main("score", str(self.scenario / "change-01"))
        self.assertEqual(code, 1)
        self.assertIn("change-01: void", out)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        self.assertEqual(run["status"], "void")
        (self.dir / "floor.json").write_text(json.dumps({"outside_share_pct": 4.0,
                                                          "layout": "host"}))
        code, _out = self.fake.main("score", str(self.scenario))
        self.assertEqual(code, 0)

    def test_score_ingests_and_scores_again(self):
        self.fake.outcomes = [None]
        self.fake.command["seconds"] = 100.0
        code, out = self.fake.main("run", "--name", "roll-edges", "--", "engine")
        self.assertEqual(code, 1)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        self.assertEqual(run["status"], "unscored")
        # The change ran from 45 s to 145 s of the probe: CW's windows after
        # it need 145 + 100 + 14 + 120 s, and 10 spare.
        self.assertEqual(run["hints"], [
            "roll.edges: the change took 100 s, too long for CW's 120 no-change windows: give "
            "--seconds 390 or more, under a new --name (the length is part of the scenario)"])
        self.assertIn("give --seconds 390", out)
        before = len(self.fake.started)
        code, _out = self.fake.main("score", str(self.scenario / "change-01"))
        self.assertEqual(code, 0)
        again = self.fake.started[before:]
        self.assertEqual(sum(1 for command in again if command[1] == "udp-ingest"), 4)
        self.assertEqual(sum(1 for command in again if command[1] == str(cr.change_window())), 1)
        self.assertEqual(json.loads((self.scenario / "change-01" / "run.json").read_text())
                         ["status"], "pass")

    def test_the_senders_run_in_the_contributor_s_host_and_stop(self):
        self.fake.main("run", "--name", "roll-edges", "--", "engine")
        senders = [command for command in self.fake.started if command[:1] == ["@contrib-lon"]]
        self.assertEqual(len(senders), 2)
        self.assertEqual(self.fake.store["sources.json"], {})
        self.assertEqual(len([item for item in self.fake.ran if item[0] == "stop_sender"]), 2)

    def test_sigterm_during_the_change_cleans_up(self):
        self.fake.command["behaviour"] = "sigterm"
        before = signal.getsignal(signal.SIGTERM)
        code, _out = self.fake.main("run", "--name", "roll-edges", "--wan-loss",
                                    "newyork=loss 1%", "--client-loss", "loss 1%",
                                    "--", "engine", "roll", "edges")
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        # The command got SIGINT and closed its own mark; the probes and
        # listeners were killed.
        engine = [process for process in self.fake.by_pid.values() if process.behaviour]
        self.assertEqual(self.fake.signals[0], (engine[0].pid, signal.SIGINT))
        self.assertTrue(all(process.returncode is not None
                            for process in self.fake.by_pid.values()))
        directory = self.scenario / "change-01"
        marks = (directory / "marks.txt").read_text().split()
        self.assertEqual(marks[0::3], ["start", "end"])
        run = json.loads((directory / "run.json").read_text())
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["command_exit"], 130)
        self.assertEqual(run["events"], [])
        self.assertEqual(run["reasons"], ["interrupted (Stopped: SIGTERM)"])
        # The shaping came off, the senders stopped and the lab went down.
        nets = [item for item in self.fake.ran if item[0] in ("net", "wan", "per_packet")]
        self.assertEqual([item[-1] for item in nets[-4:]], [None, None, None, False])
        self.assertEqual(len([item for item in self.fake.ran if item[0] == "stop_sender"]), 2)
        self.assertEqual(self.fake.ran[-1], ("down",))
        self.assertEqual(self.fake.store["sources.json"], {})
        # Scored again, it stays interrupted: no ingest, no CW.
        before = len(self.fake.started)
        code, out = self.fake.main("score", str(self.scenario))
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.started[before:], [])
        self.assertIn("change 0 of 0 pass (not scored: 1 interrupted)", out)

    def test_a_failing_clean_up_step_does_not_stop_the_next(self):
        def broken(site, direction, spec):
            self.fake.ran.append(("wan", site, direction, spec))
            if spec is None:
                raise lab.LabError("namespace gone")
        with mock.patch.object(lab, "set_wan", broken):
            code, out = self.fake.main("run", "--name", "roll-edges", "--wan-loss",
                                       "newyork=loss 1%", "--", "engine")
        self.assertEqual(code, 1)
        self.assertIn("clean-up failed: take the impairment off newyork's wide-area link: "
                      "namespace gone", out)
        self.assertEqual(len([item for item in self.fake.ran if item[0] == "stop_sender"]), 2)
        self.assertEqual(self.fake.ran[-1], ("down",))

    def test_a_command_that_outlives_the_probes_is_stopped(self):
        self.fake.command["behaviour"] = "hang"
        code, _out = self.fake.main("run", "--name", "roll-edges", "--", "engine")
        self.assertEqual(code, 1)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        self.assertEqual(run["command_exit"], 130)
        self.assertIn("the command was still running when the probes ended: it was stopped "
                      "(SIGINT, then killed)", run["reasons"])
        self.assertTrue(any("probe too short" in reason for reason in run["reasons"]))

    def test_a_listener_that_outlives_its_duration_is_killed(self):
        self.fake.hang = {"needletail-reader"}
        code, _out = self.fake.main("run", "--name", "roll-edges", "--", "engine")
        self.assertEqual(code, 1)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        readers = [pid for pid, process in self.fake.by_pid.items()
                   if (pid, signal.SIGKILL) in self.fake.signals]
        self.assertEqual(len(readers), 2)
        self.assertEqual(len([reason for reason in run["reasons"]
                              if "outlived its duration and was killed" in reason]), 2)

    def test_a_run_whose_runner_died_is_not_scored(self):
        self.fake.main("run", "--name", "roll-edges", "--", "engine")
        path = self.scenario / "change-01" / "run.json"
        run = json.loads(path.read_text())
        for key in ("complete", "status"):
            run.pop(key)
        path.write_text(json.dumps(run))
        before = len(self.fake.started)
        code, _out = self.fake.main("score", str(self.scenario))
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.started[before:], [])
        self.assertEqual(json.loads(path.read_text())["status"], "interrupted")


class Sampler(unittest.TestCase):
    def test_the_sampler_reads_the_cpus_and_scopes_while_the_probes_run(self):
        # Two CPUs half busy, the lab's scopes 20% of them: 30% from outside.
        clock = os.sysconf("SC_CLK_TCK")
        calls = []

        def cpu_times(cpus):
            calls.append(cpus)
            t = time.monotonic()
            return {cpu: (t * clock * 0.5, t * clock * 0.01) for cpu in cpus}
        with mock.patch.object(cr, "read_cpu_times", cpu_times), \
                mock.patch.object(cr, "SAMPLE_INTERVAL_S", 0.01), \
                mock.patch.object(lab, "scope_cpu_usec",
                                  lambda: {"lon-edge-1": time.monotonic() * 1e6 * 0.4}):
            sampler = cr.Interference([2, 3]).start()
            time.sleep(0.3)
            share = sampler.stop()
        self.assertFalse(sampler.thread.is_alive())
        self.assertGreater(len(sampler.samples), 5)
        self.assertEqual(calls[0], [2, 3])
        self.assertEqual(share["cpus"], 2)
        self.assertAlmostEqual(share["busy_pct"], 50.0, delta=1.0)
        self.assertAlmostEqual(share["lab_pct"], 20.0, delta=1.0)
        self.assertAlmostEqual(share["outside_share_pct"], 30.0, delta=1.5)
        self.assertAlmostEqual(share["steal_pct"], 1.0, delta=0.2)


@unittest.skipUnless(cr.change_window().is_file(), "needs the referee's change-window.py")
class Referee(unittest.TestCase):
    """The runner's marks and probe records, scored by the referee's own
    change-window.py (read and run, never written)."""

    def test_the_referee_scores_the_runner_s_marks(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        start_us = T0 // 1000
        frames = directory / "f.csv"
        with frames.open("w", newline="") as handle:
            out = csv.writer(handle)
            out.writerow(["kind", "index", "latency_us", "complete_unix_us", "first_unix_us"])
            # 200 s of a unit every 10 ms, 7 ms late, with no change in it.
            for n in range(20_000):
                source = start_us + n * 10_000
                out.writerow(["a", n, 7000 + (n * 37) % 500, source + 7000 + (n * 37) % 500, n])
        marks, _events = cr.close_marks([], T0 + 100 * 10**9, T0 + 108 * 10**9, "c")
        (directory / "marks.txt").write_text(cr.marks_text(marks))
        item = {"key": "opus-1@lon-readers", "files": {"probe.csv": str(frames)}}
        done = subprocess.run(cr.score_command([item], directory), capture_output=True)
        verdict = cr.verdict_of(json.loads((directory / "verdict.json").read_text()), [item])
        result = verdict["c"]["opus-1@lon-readers"]
        self.assertEqual(result["missing"], 0)
        self.assertIsNotNone(result["null_fail_rate"])
        self.assertGreaterEqual(result["null_windows"], 120)
        self.assertEqual(done.returncode == 0, result["pass"] is True)

    def test_the_seconds_a_long_change_needs_are_enough_for_the_referee(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        span = (T0 + 45 * 10**9, T0 + 145 * 10**9)
        needed = cr.seconds_needed(span, T0)
        self.assertEqual(needed, 390)
        marks, _events = cr.close_marks([("start", span[0], "c"), ("end", span[1], "c")],
                                        0, 0, "c")
        (directory / "marks.txt").write_text(cr.marks_text(marks))
        windows = {}
        for seconds in (needed, needed - 20):
            frames = directory / f"f{seconds}.csv"
            with frames.open("w", newline="") as handle:
                out = csv.writer(handle)
                out.writerow(["kind", "index", "latency_us", "complete_unix_us",
                              "first_unix_us"])
                for n in range(seconds * 100):
                    source = T0 // 1000 + n * 10_000
                    out.writerow(["a", n, 7000 + (n * 37) % 500, source + 7000 + (n * 37) % 500, n])
            item = {"key": "k", "files": {"probe.csv": str(frames)}}
            subprocess.run(cr.score_command([item], directory), capture_output=True)
            result = cr.verdict_of(json.loads((directory / "verdict.json").read_text()),
                                   [item])["c"]["k"]
            windows[seconds] = result["null_windows"]
        self.assertGreaterEqual(windows[needed], cr.MIN_NULL_WINDOWS)
        self.assertLess(windows[needed - 20], cr.MIN_NULL_WINDOWS)


if __name__ == "__main__":
    unittest.main()
