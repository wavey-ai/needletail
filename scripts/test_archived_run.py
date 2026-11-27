#!/usr/bin/env python3
"""Tests of scripts/archived_run.py, the run tools' one way into the run
archive: a run made or opened, writes that never raise, a run ended failed
by an exception or a lost write, the five code heads, the local lab's log
slices. A temporary archive root.
python3 -m unittest scripts/test_archived_run.py"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import archived_run  # noqa: E402
import run_archive as ra  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "data"
        patcher = mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def manifest(self, run_id: str) -> dict:
        return json.loads((self.root / run_id / "manifest.json").read_text())


class Runs(Base):
    def test_a_default_run_id_is_the_tool_and_a_utc_time(self):
        self.assertEqual(archived_run.default_run_id("latency-bench", 0), "latency-bench-19700101T000000Z")

    def test_a_run_is_made_with_its_facts_and_ended_complete(self):
        with archived_run.Run("r1", "local-lab", "edge-campaign", argv=["edge-campaign.py", "run"],
                              settings={"x": 1, "password": "pw-value"}, regions=["london"]) as run:
            self.assertTrue(run.made)
            run.write_json("a/b.json", {"k": 1})
            run.append_ndjson("a/c.ndjson", [{"unix_us": 1}, {"unix_us": 2}])
        manifest = self.manifest("r1")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["tool"], "edge-campaign")
        self.assertEqual(manifest["deploy_settings"], {"x": 1, "password": ra.SCRUBBED})
        self.assertEqual(manifest["regions"], ["london"])
        self.assertEqual({item["path"] for item in manifest["files"]}, {"a/b.json", "a/c.ndjson"})

    def test_an_exception_ends_the_run_failed_and_is_raised(self):
        with self.assertRaises(KeyboardInterrupt):
            with archived_run.Run("r2", "local-lab", "change-run", argv=["x"]) as run:
                run.at_close(lambda: run.write_json("closed.json", {"ok": True}))
                raise KeyboardInterrupt
        manifest = self.manifest("r2")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["status_reason"], "KeyboardInterrupt")
        self.assertIn("closed.json", {item["path"] for item in manifest["files"]})

    def test_a_write_that_fails_does_not_raise_and_fails_the_run(self):
        credential = self.dir / "id_ed25519"
        credential.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        with redirect_stderr(StringIO()) as err:
            with archived_run.Run("r3", "local-lab", "latency-bench", argv=["x"]) as run:
                self.assertIsNone(run.add_file(credential, "key"))
                self.assertIsNone(run.add_file(self.dir / "never-written", "x"))
        manifest = self.manifest("r3")
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("the archive copy is incomplete", manifest["status_reason"])
        self.assertIn("credentials are never archived", err.getvalue())
        self.assertEqual(manifest["files"], [])

    def test_a_run_that_exists_is_opened_and_not_ended_by_the_opener(self):
        ra.RunArchive.create("lab1", "linode-lab", tool="linode-stall-hunt.py", argv=["h"],
                             root=self.root)
        with archived_run.Run("lab1", "local-lab", "linode-autoscale", argv=["a"],
                              prefix="autoscale/", settings={"poll_s": 1},
                              code=[archived_run.ROOT]) as run:
            self.assertFalse(run.made)
            run.append_ndjson("events.ndjson", {"unix_us": 5, "kind": "decision"})
            run.fail("the controller failed")
        manifest = self.manifest("lab1")
        self.assertEqual(manifest["status"], "running")
        self.assertEqual(manifest["kind"], "linode-lab")
        paths = {item["path"] for item in manifest["files"]}
        self.assertEqual(paths, {"autoscale/events.ndjson", "autoscale/settings.json",
                                 "autoscale/code-heads.json", "autoscale/outcomes.ndjson"})
        outcome = json.loads((self.root / "lab1" / "autoscale" / "outcomes.ndjson").read_text())
        self.assertEqual((outcome["tool"], outcome["status"], outcome["reason"]),
                         ("linode-autoscale", "failed", "the controller failed"))

    def test_an_opened_run_s_settings_are_scrubbed(self):
        ra.RunArchive.create("lab3", "linode-lab", tool="linode-stall-hunt.py", argv=["h"],
                             root=self.root)
        with archived_run.Run("lab3", "local-lab", "latency-bench", argv=["b"], prefix="p1/",
                              settings={"name": "p1", "livekit_api_secret": "s3cret-value",
                                        "args": ["--token", "tok-value-123"]}):
            pass
        text = (self.root / "lab3" / "p1" / "settings.json").read_text()
        self.assertNotIn("s3cret-value", text)
        self.assertNotIn("tok-value-123", text)
        self.assertEqual(json.loads(text)["name"], "p1")
        with archived_run.Run("r10", "local-lab", "change-run", argv=["c"],
                              settings={"command": ["roll", "--api-key", "k-value-456"]}):
            pass
        self.assertEqual(self.manifest("r10")["deploy_settings"]["command"],
                         ["roll", "--api-key", ra.SCRUBBED])

    def test_a_close_step_that_raises_anything_still_ends_the_run_failed(self):
        with redirect_stderr(StringIO()):
            with archived_run.Run("r8", "local-lab", "latency-bench", argv=["x"]) as run:
                run.at_close(lambda: {}["lane"])
                run.at_close(lambda: run.write_json("after.json", {}))
        manifest = self.manifest("r8")
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("KeyError", manifest["status_reason"])
        self.assertIn("after.json", {item["path"] for item in manifest["files"]})

    def test_an_opened_run_whose_settings_cannot_be_written_is_still_made(self):
        ra.RunArchive.create("lab4", "linode-lab", tool="linode-stall-hunt.py", argv=["h"],
                             root=self.root)
        with redirect_stderr(StringIO()) as err:
            with archived_run.Run("lab4", "local-lab", "change-run", argv=["c"],
                                  settings={"plan": {1, 2}}) as run:
                self.assertEqual(run.run_id, "lab4")
        self.assertIn("run archive lab4: write settings.json failed", err.getvalue())

    def test_finish_false_leaves_a_run_it_made_running(self):
        with archived_run.Run("lab2", "linode-lab", "linode-autoscale", argv=["a"],
                              finish=False) as run:
            self.assertTrue(run.made)
        self.assertEqual(self.manifest("lab2")["status"], "running")

    def test_a_tree_since_a_time_leaves_an_earlier_pass_s_files_out(self):
        work = self.dir / "work"
        (work / "sub").mkdir(parents=True)
        old = work / "plan.json"
        old.write_text("{}")
        (work / "sub" / "plan.json").write_text("{}")
        os.utime(old, (time.time() - 100, time.time() - 100))
        since = time.time() - 10
        (work / "sub" / "plan.json").touch()
        (work / "hops.json").write_text("{}")
        with archived_run.Run("r4", "local-lab", "latency-bench", argv=["x"]) as run:
            run.add_tree(work, "bench", "raw", "a pass", since_s=since)
        paths = {item["path"] for item in self.manifest("r4")["files"]}
        self.assertEqual(paths, {"bench/hops.json", "bench/sub/plan.json"})


class Signals(Base):
    def test_sigterm_unwinds_the_run_and_a_second_one_waits_for_its_copies(self):
        with redirect_stderr(StringIO()) as err:
            with self.assertRaises(SystemExit) as stop:
                with archived_run.stopped_by_signals():
                    with archived_run.Run("r9", "local-lab", "edge-campaign", argv=["x"]) as run:

                        def close_step():
                            os.kill(os.getpid(), signal.SIGTERM)
                            run.write_json("kept.json", {"ok": True})

                        run.at_close(close_step)
                        os.kill(os.getpid(), signal.SIGTERM)
                        time.sleep(5)
        self.assertEqual(stop.exception.code, 128 + signal.SIGTERM)
        manifest = self.manifest("r9")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["status_reason"], "Stopped: SIGTERM")
        self.assertIn("kept.json", {item["path"] for item in manifest["files"]})
        self.assertIn("still keeping the run's data", err.getvalue())
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class CodeHeads(Base):
    def test_the_five_worktrees_beside_the_av_mesh_one(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("NEEDLETAIL_PLAYLISTS_ROOT", "NEEDLETAIL_DAW_NEXUS_ROOT"):
                os.environ.pop(name, None)
            paths = archived_run.code_paths("/w/av-mesh-features", "/w/av-contrib-features")
        self.assertEqual([str(path) for path in paths],
                         [str(archived_run.ROOT), "/w/av-mesh-features", "/w/av-contrib-features",
                          "/w/playlists-features", "/w/daw-nexus-features"])

    def test_the_environment_names_playlists_and_daw_nexus(self):
        with mock.patch.dict(os.environ, {"NEEDLETAIL_PLAYLISTS_ROOT": "/p",
                                          "NEEDLETAIL_DAW_NEXUS_ROOT": "/d"}):
            paths = archived_run.code_paths("/w/av-mesh", "/w/av-contrib")
        self.assertEqual([str(path) for path in paths[3:]], ["/p", "/d"])


class Slices(Base):
    def test_each_slice_is_what_a_log_gained_and_a_new_log_starts_again(self):
        logs = self.dir / "logs"
        logs.mkdir()
        (logs / "lon-edge-1.log").write_text("before\n")
        slices = archived_run.LogSlices(logs)
        slices.mark()
        with archived_run.Run("r5", "local-lab", "edge-campaign", argv=["x"]) as run:
            with open(logs / "lon-edge-1.log", "a") as handle:
                handle.write("during one\n")
            (logs / "lon-dist-a.log").write_text("new log\n")
            slices.take(run, "scenarios/one/node-logs")
            self.assertEqual(slices.take(run, "scenarios/empty/node-logs"), [])
            (logs / "lon-edge-1.log").unlink()
            (logs / "lon-edge-1.log").write_text("after up\n")
            slices.take(run, "scenarios/two/node-logs")
        run_dir = self.root / "r5"
        self.assertEqual((run_dir / "scenarios/one/node-logs/lon-edge-1.log").read_text(), "during one\n")
        self.assertEqual((run_dir / "scenarios/one/node-logs/lon-dist-a.log").read_text(), "new log\n")
        self.assertEqual((run_dir / "scenarios/two/node-logs/lon-edge-1.log").read_text(), "after up\n")
        self.assertFalse((run_dir / "scenarios/two/node-logs/lon-dist-a.log").exists())
        self.assertEqual((logs / "lon-dist-a.log").read_text(), "new log\n", "the lab's log is kept")

    def test_a_log_made_again_under_the_same_inode_and_longer_is_copied_whole(self):
        # ext4 gives a log that `up` deleted and the service made again the
        # same inode number; written in place here, which keeps the inode
        # for certain. The new log is longer than the old offset.
        logs = self.dir / "logs"
        logs.mkdir()
        log = logs / "lon-edge-1.log"
        log.write_text("".join(f"old pass line {i}\n" for i in range(10)))
        slices = archived_run.LogSlices(logs)
        slices.mark()
        inode = log.stat().st_ino
        new = "".join(f"new pass line {i}\n" for i in range(40))
        with open(log, "w") as handle:
            handle.write(new)
        self.assertEqual(log.stat().st_ino, inode)
        with archived_run.Run("r6", "local-lab", "latency-bench", argv=["x"]) as run:
            slices.take(run, "p2/node-logs")
            with open(log, "a") as handle:
                handle.write("more\n")
            slices.take(run, "p3/node-logs")
        self.assertEqual((self.root / "r6/p2/node-logs/lon-edge-1.log").read_text(), new)
        self.assertEqual((self.root / "r6/p3/node-logs/lon-edge-1.log").read_text(), "more\n")

    def test_a_long_log_is_known_by_its_first_bytes(self):
        logs = self.dir / "logs"
        logs.mkdir()
        log = logs / "lon-edge-1.log"
        old = b"".join(b"2026-11-24T00:00:00Z line %05d\n" % i for i in range(400))
        self.assertGreater(len(old), archived_run.LogSlices.HEAD_BYTES)
        log.write_bytes(old)
        slices = archived_run.LogSlices(logs)
        slices.mark()
        with archived_run.Run("r7", "local-lab", "latency-bench", argv=["x"]) as run:
            with open(log, "ab") as handle:
                handle.write(b"tail line\n")
            slices.take(run, "one")
            with open(log, "r+b") as handle:
                handle.write(b"3")
            slices.take(run, "two")
        self.assertEqual((self.root / "r7/one/lon-edge-1.log").read_bytes(), b"tail line\n")
        self.assertEqual((self.root / "r7/two/lon-edge-1.log").read_bytes(),
                         b"3" + old[1:] + b"tail line\n")


if __name__ == "__main__":
    unittest.main()
