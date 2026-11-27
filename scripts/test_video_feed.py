#!/usr/bin/env python3
"""Tests of the 4K feed loop (scripts/video-feed.sh, A-contributor 3.6) and
its unit (deploy/linode-small-mesh/needletail-video-feed.service), offline:
ffmpeg is a fake that records its arguments and exits as told. The feed goes
to the first contributor, moves to the next when ffmpeg fails, stays where it
works, ends when ffmpeg ends cleanly or its time is up, puts -rw_timeout (and
the time left) before every output, and stops its ffmpeg on SIGTERM.
python3 -m unittest scripts/test_video_feed.py"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "video-feed.sh"
UNIT_DIR = HERE.parent / "deploy" / "linode-small-mesh"
SRT = "srt://{host}:27001?mode=caller&transtype=live&latency=80"
FAKE = """#!/usr/bin/env python3
import json, os, pathlib, sys, time
log = pathlib.Path(os.environ["FAKE_LOG"])
plan = json.loads(pathlib.Path(os.environ["FAKE_PLAN"]).read_text())
runs = log.read_text().splitlines() if log.exists() else []
with log.open("a") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid()}) + "\\n")
step = plan[min(len(runs), len(plan) - 1)]
if step == "sleep":
    time.sleep(60)
    sys.exit(0)
sys.exit(step)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fake = self.tmp / "ffmpeg"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.env = {**os.environ, "FFMPEG": str(fake), "FAKE_LOG": str(self.tmp / "log"),
                    "FAKE_PLAN": str(self.tmp / "plan")}

    def plan(self, *steps) -> None:
        (self.tmp / "plan").write_text(json.dumps(list(steps)))

    def feed(self, *arguments, timeout=20) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), "--pause-ms", "0", *arguments],
                              env=self.env, capture_output=True, text=True, timeout=timeout)

    def runs(self) -> list[list[str]]:
        path = self.tmp / "log"
        return [json.loads(line)["argv"] for line in path.read_text().splitlines()] \
            if path.exists() else []

    @staticmethod
    def outputs(argv: list[str]) -> list[str]:
        return [item for item in argv if item.startswith(("srt://", "rtmp://"))]


class Feed(Base):
    def test_it_fails_over_and_stays_where_it_works(self):
        self.plan(1, 0)
        done = self.feed("10.77.1.5", "10.77.1.7", "--", "-i", "clip.ts", "-f", "mpegts", SRT)
        self.assertEqual(done.returncode, 0, done.stderr)
        runs = self.runs()
        self.assertEqual([self.outputs(argv) for argv in runs], [
            ["srt://10.77.1.5:27001?mode=caller&transtype=live&latency=80"],
            ["srt://10.77.1.7:27001?mode=caller&transtype=live&latency=80"]])
        for argv in runs:
            at = argv.index(self.outputs(argv)[0])
            self.assertEqual(argv[at - 2:at], ["-rw_timeout", "2000000"])
            self.assertEqual(argv[:4], ["-i", "clip.ts", "-f", "mpegts"])
        self.assertIn("video-feed: 10.77.1.5 exited 1 after", done.stderr)
        self.assertIn("to 10.77.1.7", done.stderr)

    def test_no_fail_back_while_the_second_works(self):
        # The feed on B ends cleanly: it never went back to A.
        self.plan(1, 0)
        self.feed("a", "b", "--", "-i", "x", SRT)
        self.assertEqual([self.outputs(argv)[0].split(":")[1] for argv in self.runs()],
                         ["//a", "//b"])
        # When B fails too, the next is A again (round robin over the list).
        (self.tmp / "log").unlink()
        self.plan(1, 1, 0)
        self.feed("a", "b", "--", "-i", "x", SRT)
        self.assertEqual([self.outputs(argv)[0].split(":")[1] for argv in self.runs()],
                         ["//a", "//b", "//a"])

    def test_every_output_of_a_ladder_names_the_contributor(self):
        self.plan(0)
        ladder = ["-i", "one.ts", "-i", "two.ts", "-map", "0:v:0", "-f", "flv",
                  "rtmp://{host}:19350/live/1", "-map", "1:v:0", "-f", "flv",
                  "rtmp://{host}:19350/live/2"]
        self.assertEqual(self.feed("--rw-timeout-us", "500000", "10.77.1.5", "10.77.1.7", "--",
                                   *ladder).returncode, 0)
        argv = self.runs()[0]
        self.assertEqual(self.outputs(argv), ["rtmp://10.77.1.5:19350/live/1",
                                              "rtmp://10.77.1.5:19350/live/2"])
        for output in self.outputs(argv):
            at = argv.index(output)
            self.assertEqual(argv[at - 2:at], ["-rw_timeout", "500000"])

    def test_a_feed_with_seconds_ends_when_its_time_is_up(self):
        self.plan(1, 0)
        done = self.feed("--seconds", "30", "a", "b", "--", "-i", "x", SRT)
        self.assertEqual(done.returncode, 0)
        lefts = []
        for argv in self.runs():
            at = argv.index(self.outputs(argv)[0])
            self.assertEqual(argv[at - 4:at - 1], ["-rw_timeout", "2000000", "-t"])
            lefts.append(float(argv[at - 1]))
        self.assertTrue(30.0 >= lefts[0] >= lefts[1] > 25.0, lefts)

    def test_sigterm_stops_ffmpeg_and_ends_cleanly(self):
        self.plan("sleep")
        process = subprocess.Popen(["bash", str(SCRIPT), "a", "b", "--", "-i", "x", SRT],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while not self.runs() and time.time() < deadline:
            time.sleep(0.05)
        child = json.loads((self.tmp / "log").read_text().splitlines()[0])["pid"]
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 0)
        process.stdout.close()
        process.stderr.close()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("ffmpeg was left running")
        self.assertEqual(len(self.runs()), 1, "no other contributor was tried")

    def test_usage(self):
        self.plan(0)
        for arguments in ((), ("a", "--", "-i", "x", "srt://b:1"), ("--", "-i", "x", SRT),
                          ("--seconds", "x", "a", "--", SRT), ("--bogus", "a", "--", SRT)):
            self.assertEqual(self.feed(*arguments).returncode, 2, arguments)
        self.assertEqual(self.runs(), [])


class Unit(Base):
    def test_the_unit_runs_the_loop_on_its_environment(self):
        unit = (UNIT_DIR / "needletail-video-feed.service").read_text()
        lines = dict(line.split("=", 1) for line in unit.splitlines()
                     if "=" in line and not line.startswith("#"))
        self.assertEqual(lines["EnvironmentFile"], "/etc/needletail/video-feed.env")
        self.assertEqual(lines["ExecStart"], "/usr/local/bin/needletail-video-feed "
                                             "$FEED_CONTRIBUTORS -- $FEED_ARGUMENTS")
        self.assertEqual(lines["Restart"], "on-failure")
        # The example environment, split on blanks as systemd splits an
        # unbraced $VARIABLE, feeds the origin first.
        example = dict(line.split("=", 1) for line in
                       (UNIT_DIR / "video-feed.env.example").read_text().splitlines()
                       if line and not line.startswith("#"))
        self.plan(0)
        command = lines["ExecStart"].split()[1:]
        argv = []
        for item in command:
            argv += example[item[1:]].split() if item.startswith("$") else [item]
        done = subprocess.run(["bash", str(SCRIPT), *argv], env=self.env, capture_output=True,
                              text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs(self.runs()[0]),
                         ["srt://10.77.1.5:27001?mode=caller&transtype=live&latency=80"])


if __name__ == "__main__":
    unittest.main()
