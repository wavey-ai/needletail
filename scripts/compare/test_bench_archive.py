#!/usr/bin/env python3
"""Tests of latency-bench.py's pass in the run archive: every file the pass
wrote, the nodes' log slices taken before the lab goes down, the listener
steps; written however the pass ends; appended to a run that exists. The lab
is faked: nothing is started. A temporary archive root.
python3 -m unittest scripts/compare/test_bench_archive.py"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("latency_bench_archive", HERE / "latency-bench.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)
lab = bench.lab
ra = bench.archived_run.ra


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "data"
        (self.dir / "work" / "logs").mkdir(parents=True)
        (self.dir / "work" / "logs" / "lon-edge-1.log").write_text("an earlier pass\n")
        self.order: list = []

        @contextlib.contextmanager
        def hold(label):
            yield

        for patcher in (
                mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": str(self.root)}),
                mock.patch.object(bench.archived_run, "code_paths", lambda *_: []),
                mock.patch.object(lab, "WORK", self.dir / "work"),
                mock.patch.object(bench, "OUT_ROOT", self.dir / "out"),
                mock.patch.object(bench.guard, "require_frozen", lambda *a, **k: None),
                mock.patch.object(bench.guard, "hold", hold),
                mock.patch.object(lab, "up", lambda overrides: self.order.append("up")),
                mock.patch.object(lab, "down", lambda quiet=False: self.order.append(
                    ("down", self.logs_archived()))),
                mock.patch.object(bench, "prepare_media", lambda item: None),
                mock.patch.object(bench, "prepare_video", lambda item: item)):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.dir / "ntcmp").write_text("")
        out = self.dir / "out" / "p1"
        out.mkdir(parents=True)
        stale = out / "lon-readers.uc9.arrivals.csv"
        stale.write_text("from an earlier pass of the same name\n")
        os.utime(stale, (time.time() - 3600, time.time() - 3600))

    def logs_archived(self) -> bool:
        runs = [path for path in self.root.glob("*/manifest.json")] if self.root.is_dir() else []
        return any("node-logs/lon-edge-1.log" in entry["path"]
                   for path in runs for entry in json.loads(path.read_text())["files"])

    def write_pass(self, out: pathlib.Path) -> None:
        (out / "lon-readers.uc0.arrivals.csv").write_text("seq,arrival_unix_us\n1,5\n")
        (out / "lon-readers.uc0.uc.json").write_text("{}")
        (out / "load-london").mkdir(exist_ok=True)
        (out / "load-london" / "load.log").write_text("listeners 80\n")
        (out / "nodes.ndjson").write_text('{"node":"lon-edge-1","udp_drops":0}\n')
        with open(self.dir / "work" / "logs" / "lon-edge-1.log", "a") as handle:
            handle.write("during the pass\n")

    def main(self, *argv: str, measure=None) -> int:
        def measured(args, steps, out):
            self.write_pass(out)
            steps["load"] = [{"site": "london", "per_edge": 40, "edges": {"a": "lon-edge-1",
                                                                          "b": "lon-edge-2"}}]
            if measure:
                raise measure
            return {}, [], 0, 1, {}

        with mock.patch.object(bench, "measure", measured), \
                mock.patch.object(bench.hops, "read_sender_summary", lambda path: None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bench.main(["--name", "p1", "--ntcmp", str(self.dir / "ntcmp"),
                               "--udp-clients", "1", "--udp-client", str(self.dir / "ntcmp"),
                               *argv])

    def manifest(self, run_id: str) -> dict:
        return json.loads((self.root / run_id / "manifest.json").read_text())


class Pass(Base):
    def test_a_pass_keeps_its_files_logs_and_listener_steps(self):
        self.assertEqual(self.main("--run-id", "b1"), 0)
        manifest = self.manifest("b1")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["tool"], "latency-bench")
        self.assertEqual(manifest["deploy_settings"]["name"], "p1")
        self.assertEqual(manifest["deploy_settings"]["udp_clients"], 1)
        paths = {entry["path"] for entry in manifest["files"]}
        for rel in ("p1/lon-readers.uc0.arrivals.csv", "p1/lon-readers.uc0.uc.json",
                    "p1/load-london/load.log", "p1/nodes.ndjson", "p1/hops.raw.ndjson",
                    "p1/hops.json", "p1/summary.txt", "p1/plan.json", "p1/node-logs/lon-edge-1.log"):
            self.assertIn(rel, paths)
        self.assertNotIn("p1/lon-readers.uc9.arrivals.csv", paths, "an earlier pass's file")
        self.assertEqual((self.root / "b1" / "p1" / "node-logs" / "lon-edge-1.log").read_text(),
                         "during the pass\n")
        self.assertEqual(self.order, ["up", ("down", True)], "the logs are kept before the down")
        steps = {step["lane"]: step for step in manifest["listener_steps"]}
        self.assertEqual(steps["load"]["listeners_total"], 80)
        self.assertEqual(steps["udp"]["listeners_by_region"], {"london": 1, "newyork": 1})
        self.assertEqual(steps["hls-h2"]["listeners_total"], 2)

    def test_a_pass_that_fails_is_kept_and_its_run_failed(self):
        with self.assertRaises(RuntimeError):
            self.main("--run-id", "b2", measure=RuntimeError("the sender died"))
        manifest = self.manifest("b2")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["status_reason"], "RuntimeError: the sender died")
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertIn("p1/lon-readers.uc0.arrivals.csv", paths)
        self.assertIn("p1/node-logs/lon-edge-1.log", paths)
        self.assertEqual(self.order, ["up", ("down", True)])

    def test_a_run_that_exists_is_appended_to_under_the_pass_s_name(self):
        ra.RunArchive.create("fs1", "local-lab", tool="first-session", argv=["fs"], root=self.root)
        self.assertEqual(self.main("--run-id", "fs1"), 0)
        manifest = self.manifest("fs1")
        self.assertEqual(manifest["status"], "running")
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertIn("p1/lon-readers.uc0.arrivals.csv", paths)
        self.assertIn("settings.json", paths)
        outcome = json.loads((self.root / "fs1" / "outcomes.ndjson").read_text())
        self.assertEqual((outcome["tool"], outcome["status"]), ("latency-bench", "complete"))

    def test_a_dry_run_writes_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench.main(["--name", "p1", "--dry-run", "--run-id", "b3"]), 0)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
