#!/usr/bin/env python3
"""Tests of the edge campaign's run in the run archive: one run per
invocation; each scenario's directory, its node log slice, every node's
hop and stop journals and its readers as listener steps go in as the
scenario ends, also when it is cut off; the node logs before each `up`;
the summary. The lab is faked: nothing is started. A temporary archive
root.
python3 -m unittest scripts/test_campaign_archive.py"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import shutil
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import archived_run  # noqa: E402
import edge_campaign_core as core  # noqa: E402
import edge_lab as lab  # noqa: E402

_spec = importlib.util.spec_from_file_location("edge_campaign_archive", HERE / "edge-campaign.py")
campaign = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(campaign)


class FakeHops:
    def __init__(self):
        self.asked = []

    def collect_edge_lab(self, since_us, stream):
        self.asked.append((since_us, stream))
        return ({"lon-edge-1": {"role": "edge", "http_status": 200}},
                [{"_node": "lon-edge-1", "stage": "udp_sent", "seq": 1, "t_us": since_us + 5}])

    @staticmethod
    def save_raw(path, nodes, events, epoch=None):
        path.write_text(json.dumps({"_meta": {"nodes": nodes}}) + "\n"
                        + "".join(json.dumps(event) + "\n" for event in events))


class Base(unittest.TestCase):
    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        self.root = self.work / "data"
        (self.work / "logs").mkdir()
        (self.work / "logs" / "lon-edge-1.log").write_text("from an earlier run\n")
        self.hops = FakeHops()
        for patcher in (mock.patch.object(lab, "WORK", self.work),
                        mock.patch.object(lab, "REGISTRY", self.work / "registry"),
                        mock.patch.object(core, "RUNS", self.work / "runs"),
                        mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": str(self.root),
                                                     "NEEDLETAIL_ALLOW_LIVE": "1"}),
                        mock.patch.object(archived_run, "code_paths", lambda *_: []),
                        mock.patch.object(core, "hops_module", lambda: self.hops),
                        mock.patch.object(core, "restore", lambda: None),
                        mock.patch.object(lab, "service_names",
                                          lambda role=None, site=None: ["contrib-lon", "lon-edge-1"]),
                        mock.patch.object(lab, "curl", self.curl)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.curls = []

    def curl(self, name, path, method="GET", body=None, timeout=5.0, **_kwargs):
        self.curls.append((name, path))
        if name == "contrib-lon":
            return 404, "", ""
        return 200, "", json.dumps({"stall_us": 1200}) + "\n"

    def evidence(self, item, directory, sample, fail=None):
        """A scenario: it writes its evidence and a reader's log, and the
        edge's log grows while it runs."""
        target = directory / item.name
        target.mkdir(parents=True, exist_ok=True)
        with open(self.work / "logs" / "lon-edge-1.log", "a") as handle:
            handle.write(f"during {item.name}\n")
        (target / "audio.reader.log").write_text("reader\n")
        if fail:
            raise fail
        evidence = {"scenario": item.name, "group": item.group, "result": "PASS", "checks": [],
                    "started_utc": "2026-11-24T00:00:00Z",
                    "readers": {"audio": {"host": "lon-readers", "stream": 2001, "sessions": 6,
                                          "seconds": 40}}}
        (target / "evidence.json").write_text(json.dumps(evidence))
        return evidence

    def run_campaign(self, *arguments: str, fail=None):
        def evidence(item, directory, sample):
            return self.evidence(item, directory, sample, fail)

        with mock.patch.object(core, "scenario_evidence", evidence), \
                mock.patch.object(sys, "argv", ["edge-campaign.py", *arguments]), \
                redirect_stdout(io.StringIO()):
            campaign.main()

    def manifest(self, run_id: str) -> dict:
        return json.loads((self.root / run_id / "manifest.json").read_text())


class CampaignRun(Base):
    def test_each_scenario_s_records_go_into_the_campaign_s_run(self):
        self.run_campaign("run", "--out", str(self.work / "r1"), "--run-id", "camp1",
                          "baseline", "reader-delay-20ms")
        manifest = self.manifest("camp1")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["tool"], "edge-campaign")
        self.assertEqual(manifest["kind"], "local-lab")
        self.assertEqual(manifest["deploy_settings"]["scenarios"], ["baseline", "reader-delay-20ms"])
        self.assertIn("gb-lon", manifest["regions"])
        self.assertTrue(any(node["name"] == "lon-edge-1" for node in manifest["topology"]["nodes"]))
        paths = {entry["path"] for entry in manifest["files"]}
        for name in ("baseline", "reader-delay-20ms"):
            for rel in ("evidence.json", "audio.reader.log", "hops.raw.ndjson",
                        "stalls/lon-edge-1.ndjson", "stalls/http-status.json",
                        "node-logs/lon-edge-1.log"):
                self.assertIn(f"scenarios/{name}/{rel}", paths)
        self.assertIn("summary.json", paths)
        run = self.root / "camp1"
        self.assertEqual((run / "scenarios/baseline/node-logs/lon-edge-1.log").read_text(),
                         "during baseline\n")
        self.assertEqual((run / "scenarios/reader-delay-20ms/node-logs/lon-edge-1.log").read_text(),
                         "during reader-delay-20ms\n")
        self.assertEqual(json.loads((run / "scenarios/baseline/stalls/http-status.json").read_text()),
                         {"contrib-lon": 404, "lon-edge-1": 200})
        self.assertTrue(all(path.startswith("/api/stalls?since_us=") for _name, path in self.curls))
        self.assertEqual([stream for _since, stream in self.hops.asked], [None, None])
        steps = manifest["listener_steps"]
        self.assertEqual([(step["step"], step["lane"], step["listeners_total"]) for step in steps],
                         [("baseline/audio", "stream-2001", 6),
                          ("reader-delay-20ms/audio", "stream-2001", 6)])
        self.assertEqual(steps[0]["listeners_by_region"], {"london": 6})
        rows = json.loads((run / "summary.json").read_text())
        self.assertEqual([row["scenario"] for row in rows], ["baseline", "reader-delay-20ms"])
        # The working copy stays where it was.
        self.assertTrue((self.work / "r1" / "baseline" / "evidence.json").is_file())

    def test_a_run_id_is_made_when_none_is_given(self):
        self.run_campaign("run", "--out", str(self.work / "r2"), "baseline")
        runs = [path.name for path in self.root.iterdir()]
        self.assertEqual(len(runs), 1)
        self.assertRegex(runs[0], r"^edge-campaign-\d{8}T\d{6}Z$")

    def test_a_campaign_cut_off_keeps_the_scenario_and_fails_the_run(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_campaign("run", "--out", str(self.work / "r3"), "--run-id", "camp3",
                              "baseline", fail=KeyboardInterrupt())
        manifest = self.manifest("camp3")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["status_reason"], "KeyboardInterrupt")
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertIn("scenarios/baseline/audio.reader.log", paths)
        self.assertIn("scenarios/baseline/node-logs/lon-edge-1.log", paths)
        self.assertIn("scenarios/baseline/hops.raw.ndjson", paths)

    def test_a_campaign_ended_by_sigterm_keeps_the_scenario_and_fails_the_run(self):
        def evidence(item, directory, sample):
            self.evidence(item, directory, sample)
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("SIGTERM did not stop the scenario")

        with mock.patch.object(core, "scenario_evidence", evidence), \
                mock.patch.object(sys, "argv", ["edge-campaign.py", "run", "--out",
                                                str(self.work / "r6"), "--run-id", "camp6",
                                                "baseline", "reader-delay-20ms"]), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stop:
            campaign.main()
        self.assertEqual(stop.exception.code, 128 + signal.SIGTERM)
        manifest = self.manifest("camp6")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["status_reason"], "Stopped: SIGTERM")
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertIn("scenarios/baseline/evidence.json", paths)
        self.assertIn("scenarios/baseline/node-logs/lon-edge-1.log", paths)
        self.assertIn("summary.json", paths)
        self.assertFalse(any(path.startswith("scenarios/reader-delay-20ms/") for path in paths))
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_a_scenario_s_archive_step_that_raises_never_replaces_its_result(self):
        def node(name):
            raise lab.LabError(f"no node {name}")

        with mock.patch.object(lab, "node", node), redirect_stdout(io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.run_campaign("run", "--out", str(self.work / "r7"), "--run-id", "camp7",
                              "baseline", "reader-delay-20ms")
        manifest = self.manifest("camp7")
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("keep scenario baseline: LabError", manifest["status_reason"])
        self.assertTrue((self.work / "r7" / "reader-delay-20ms" / "evidence.json").is_file(),
                        "the campaign went on to its next scenario")

    def test_the_run_records_the_five_worktrees_of_the_lab_s_binaries(self):
        asked = []
        trees = [self.work / name for name in ("needletail-x", "av-mesh-x", "av-contrib-x",
                                               "playlists-x", "daw-nexus-x")]

        def code_paths(mesh, contrib):
            asked.append((mesh, contrib))
            return trees

        with mock.patch.object(archived_run, "code_paths", code_paths):
            self.run_campaign("run", "--out", str(self.work / "r8"), "--run-id", "camp8",
                              "baseline")
        self.assertEqual(asked, [(lab.MESH_ROOT, lab.CONTRIB_ROOT)])
        self.assertEqual(sorted(self.manifest("camp8")["code_heads"]),
                         sorted(tree.name for tree in trees))

    def test_a_failed_journal_pull_fails_the_run_and_keeps_the_rest(self):
        def broken():
            raise lab.LabError("the lab is gone")

        with mock.patch.object(core, "hops_module", broken):
            self.run_campaign("run", "--out", str(self.work / "r4"), "--run-id", "camp4", "baseline")
        manifest = self.manifest("camp4")
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("hop and stop journals", manifest["status_reason"])
        self.assertIn("scenarios/baseline/evidence.json", {entry["path"] for entry in manifest["files"]})


class LabUp(Base):
    def test_the_logs_are_kept_before_the_lab_s_up_deletes_them(self):
        ups = []

        def up(overrides):
            ups.append(overrides)
            for log in (self.work / "logs").glob("*"):
                log.unlink()

        with mock.patch.object(lab, "up", up), \
                mock.patch.object(lab, "start_sources", lambda *a, **k: None), \
                mock.patch.object(lab, "wait_live", lambda *a, **k: None), \
                mock.patch.dict(core._current, {"overrides": None, "dirty": True}), \
                redirect_stdout(io.StringIO()):
            with core.archived("camp5", ["baseline"]):
                with open(self.work / "logs" / "lon-edge-1.log", "a") as handle:
                    handle.write("restore after a scenario\n")
                core.ensure_lab([])
        self.assertEqual(ups, [[]])
        run = self.root / "camp5"
        self.assertEqual((run / "node-logs/before-up-01/lon-edge-1.log").read_text(),
                         "restore after a scenario\n")
        self.assertIsNone(core.archive_run_id())


if __name__ == "__main__":
    unittest.main()
