#!/usr/bin/env python3
"""Tests of change-run.py with the lab faked: nothing is started.
python3 -m unittest scripts/compare/test_change_run.py"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
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

    `outcomes` is what CW says of each run in turn (True, False or None);
    `command` is how the change command behaves: its duration and the mark
    lines it prints (with {start} and {end} for its own times)."""

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
        tools = directory / "tools"
        tools.mkdir()
        for name in ("ntcmp", "change-window.py", "needletail-udp-client", "needletail-load"):
            (tools / name).write_text("")
        fake = self

        class Process:
            def __init__(self, command, stdout=None, stderr=None, **_kwargs):
                self.pid = 4242
                self.returncode = 0
                fake.started.append(command)
                # A command in a host's namespace is ["@host", ...] here.
                body = command[1:] if command[0].startswith("@") else command
                if body[:2] == ["bash", "-c"]:
                    fake.probe(body[2])
                elif body[:1] == ["engine"] or body[:1] == ["sleep"]:
                    self.returncode = fake.change(stdout, marks=body[0] == "engine")

            def wait(self, timeout=None):
                return self.returncode

            def poll(self):
                return self.returncode

            def kill(self):
                pass

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
            mock.patch.object(cr.subprocess, "run", self.run),
            mock.patch.object(bench, "UDP_CLIENT", tools / "needletail-udp-client"),
            mock.patch.object(bench, "LOAD_BIN", tools / "needletail-load"),
            mock.patch.object(bench, "loop_start", lambda now: int(now) + 8),
            mock.patch.object(bench, "stop_sender", lambda process: None),
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
        verdict = {label: {path: {"pass": outcome, "change": {"missing": 0, "gap_ms": 3.0,
                                                               "p99_ms": 7.0},
                                  "gap_bar_ms": 4.0, "p99_bar_ms": 8.0, "null_fail_rate": 0.07,
                                  "null_windows": 250}
                           for path in frames} for label in labels}
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
        senders = cr.sender_commands(cr.lanes_of(args.lanes), 1_763_900_032)
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
    def test_short_probes_and_early_changes_are_refused(self):
        for argv in (("--seconds", "300"), ("--at", "5"), ("--lanes", "opus-1,opus-2.5"),
                     ("--lanes", "flac-1"), ("--dry", "--", "sleep", "3"), ("--name", "a b"),
                     ("--clients", "0"), ("--no-up", "--set", "x=1"), ("--hosts", "nowhere")):
            command = [] if "sleep" in argv else ["--", "engine"]
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
        self.assertEqual(record["tally"], {"host": {"change": [2, 3], "null": [2, 2]}})
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
        code, _out = self.fake.main("run", "--name", "roll-edges", "--", "engine", "roll")
        self.assertEqual(code, 0)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        self.assertEqual(run["command_exit"], -9)
        self.assertEqual(run["events"][0]["event"], "aborted")
        self.assertEqual((self.scenario / "change-01" / "marks.txt").read_text().split()[0::3],
                         ["start", "end"])

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
        (self.dir / "floor.json").write_text(json.dumps({"outside_share_pct": 3.5}))
        code, out = self.fake.main("score", str(self.scenario / "change-01"))
        self.assertEqual(code, 1)
        self.assertIn("change-01: void", out)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        self.assertEqual(run["status"], "void")
        (self.dir / "floor.json").write_text(json.dumps({"outside_share_pct": 4.0}))
        code, _out = self.fake.main("score", str(self.scenario))
        self.assertEqual(code, 0)

    def test_score_ingests_and_scores_again(self):
        self.fake.outcomes = [None]
        code, _out = self.fake.main("run", "--name", "roll-edges", "--", "engine")
        self.assertEqual(code, 1)
        run = json.loads((self.scenario / "change-01" / "run.json").read_text())
        self.assertEqual(run["status"], "unscored")
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


if __name__ == "__main__":
    unittest.main()
