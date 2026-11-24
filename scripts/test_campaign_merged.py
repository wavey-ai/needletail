#!/usr/bin/env python3
"""Tests of the edge campaign in the merged topology (the default), which
renders no backbone relay: restore() puts the lab back to health without
starting or limiting relay-primary and relay-secondary (and so without
restarting the whole lab), and many_streams measures and checks the roles
that run.
python3 -m unittest scripts/test_campaign_merged.py"""

from __future__ import annotations

import fcntl
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402
import edge_campaign_core as core  # noqa: E402

_spec = importlib.util.spec_from_file_location("edge_campaign_merged", HERE / "edge-campaign.py")
campaign = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(campaign)

BACKBONES = set(lab.names("backbone"))


def rendered(names) -> dict:
    return {name: {"args": [], "rust_log": ""} for name in names}


class Base(unittest.TestCase):
    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        patcher = mock.patch.object(lab, "WORK", self.work)
        patcher.start()
        self.addCleanup(patcher.stop)

    def merged(self) -> list[str]:
        names = [name for name in lab.names() if name not in BACKBONES]
        lab.save("rendered.json", rendered(names))
        return names


class ServiceNames(Base):
    def test_the_merged_topology_has_no_backbone_service(self):
        names = self.merged()
        self.assertEqual(lab.service_names(), names)
        self.assertEqual(lab.service_names("backbone"), [])
        self.assertEqual(lab.service_names("distributor", "london"),
                         lab.names("distributor", "london"))

    def test_the_legacy_topology_runs_every_node(self):
        lab.save("rendered.json", rendered(lab.names()))
        self.assertEqual(lab.service_names(), lab.names())

    def test_nothing_before_the_lab_is_rendered(self):
        self.assertEqual(lab.service_names(), [])


class Restore(Base):
    def run_restore(self, pid=None):
        """restore() with every service dead (pid None) or alive; the real
        start_service refuses a node that the topology does not render."""
        started, limited = [], []

        def start_service(name, **_kwargs):
            if name not in lab.load("rendered.json"):
                raise lab.LabError(f"{name} has no service in this lab's topology")
            started.append(name)
            return 1

        def set_limits(name, cpu_percent=None, memory_mb=None):
            limited.append(name)

        ensure = mock.Mock()
        with mock.patch.multiple(lab, heal_all=mock.DEFAULT, freeze_service=mock.DEFAULT,
                                 service_pid=mock.Mock(return_value=pid),
                                 start_service=start_service, set_limits=set_limits,
                                 wait_live=mock.DEFAULT), \
                mock.patch.object(core, "idle_edges"), \
                mock.patch.object(core, "ensure_lab", ensure), \
                redirect_stdout(io.StringIO()) as out:
            core.restore()
        return started, limited, ensure, out.getvalue()

    def test_merged_restore_recovers_without_restarting_the_lab(self):
        names = self.merged()
        started, limited, ensure, out = self.run_restore(pid=None)
        ensure.assert_not_called()
        self.assertNotIn("did not recover", out)
        # A slot that is not in service (lon-dist-c) stays down.
        standing = [name for name in names if name not in lab.DEFAULT_SLOTS]
        self.assertEqual(started, standing)
        self.assertEqual(limited, standing)
        lab.set_slot_in_service("lon-dist-c", True)
        started, _limited, _ensure, _out = self.run_restore(pid=None)
        self.assertIn("lon-dist-c", started)
        self.assertFalse(BACKBONES & set(started) or BACKBONES & set(limited))

    def test_legacy_restore_starts_the_relays(self):
        lab.save("rendered.json", rendered(lab.names()))
        started, limited, ensure, _ = self.run_restore(pid=None)
        ensure.assert_not_called()
        self.assertTrue(BACKBONES <= set(started))
        self.assertEqual(limited, [name for name in lab.names()
                                   if name not in lab.DEFAULT_SLOTS])

    def test_running_services_are_not_started_again(self):
        self.merged()
        started, _, ensure, _ = self.run_restore(pid=1)
        ensure.assert_not_called()
        self.assertEqual(started, [])

    def test_a_lab_with_no_rendered_topology_is_restarted(self):
        started, _, ensure, out = self.run_restore(pid=None)
        ensure.assert_called_once()
        self.assertIn("no rendered topology", out)
        self.assertEqual(started, [])


class Ctx:
    def __init__(self):
        self.measures, self.checks = {}, []

    def measure(self, key, value):
        self.measures[key] = value

    def check(self, label, ok, detail=None):
        self.checks.append((label, ok, detail))

    def wait_readers(self):
        pass


class Job:
    summary = {"parts_received": 40 * 200, "readers_ok": 1, "parts_missing": 0,
               "longest_stall_ms": 0}


class ManyStreams(Base):
    def run_many(self, cpu_by_node):
        ctx = Ctx()
        used = {}

        def cpu_seconds(name):
            # Each call adds the node's share of the elapsed second.
            used[name] = used.get(name, 0.0) + cpu_by_node[name]
            return used[name], 0.0

        clock = iter(range(1000, 2000))
        with mock.patch.object(campaign.time, "sleep"), \
                mock.patch.object(campaign.time, "time", side_effect=lambda: next(clock)), \
                mock.patch.object(campaign, "cpu_seconds", cpu_seconds), \
                mock.patch.object(campaign, "album_readers",
                                  lambda ctx, seconds, streams, **_: {
                                      stream: Job() for stream in streams}), \
                mock.patch.object(campaign, "slot_refusals", return_value={}):
            campaign.many_streams(ctx, 2)
        return ctx

    def test_merged_measures_no_relay_and_checks_the_rest(self):
        self.merged()
        ctx = self.run_many({"contrib-lon": 0.0, "lon-dist-a": 0.0, "lon-edge-1": 0.0})
        cpu = ctx.measures["cpu_percent_by_role"]
        self.assertEqual(set(cpu), {"contributor", "distributor", "edge"})
        self.assertTrue(all(ok for _, ok, _ in ctx.checks), ctx.checks)

    def test_merged_still_fails_a_busy_distributor(self):
        self.merged()
        # The distributor's CPU seconds rise by one per call over a one
        # second window: 100%.
        ctx = self.run_many({"contrib-lon": 0.0, "lon-dist-a": 1.0, "lon-edge-1": 0.0})
        cpu_check = ctx.checks[0]
        self.assertIn("90%", cpu_check[0])
        self.assertFalse(cpu_check[1])

    def test_legacy_measures_the_relay(self):
        lab.save("rendered.json", rendered(lab.names()))
        ctx = self.run_many({"contrib-lon": 0.0, "relay-primary": 0.0, "lon-dist-a": 0.0,
                             "lon-edge-1": 0.0})
        self.assertIn("relay", ctx.measures["cpu_percent_by_role"])
        self.assertTrue(ctx.checks[0][1])


class PerLab(Base):
    """Two labs of one host (NEEDLETAIL_EDGE_LAB_ID) keep their runs apart,
    a run holds its lab and takes frozen binaries, and one lab at a time
    runs the players."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(lab, "REGISTRY", self.work / "registry"),
                        mock.patch.object(core, "RUNS", self.work / "runs")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_runs_of_a_lab_with_an_id_are_its_own(self):
        """The campaign as a lab's shell starts it: RUNS (where `run` puts
        its runs and `latest`) follows NEEDLETAIL_EDGE_LAB_ID."""
        def runs(**extra) -> pathlib.Path:
            env = {key: value for key, value in os.environ.items()
                   if key not in ("NEEDLETAIL_EDGE_LAB_ID", "NEEDLETAIL_EDGE_LAB_WORK")}
            env.update(extra)
            program = (f"import sys; sys.path.insert(0, {str(HERE)!r}); "
                       "import edge_campaign_core as core; print(core.RUNS)")
            ran = subprocess.run([sys.executable, "-c", program], env=env, capture_output=True,
                                 text=True, check=True)
            return pathlib.Path(ran.stdout.strip())

        self.assertEqual(runs(NEEDLETAIL_EDGE_LAB_ID="a"), lab.ROOT / "target" / "edge-campaign-a")
        self.assertEqual(runs(), lab.ROOT / "target" / "edge-campaign")

    def run_campaign(self, *arguments: str, allow_live: bool = True) -> list:
        seen = []

        def scenario(item, directory, sample=True):
            seen.append((item.name, directory, core.guard.holder(lab.WORK)))
            return {}

        with mock.patch.object(core, "run_scenario", scenario), \
                mock.patch.object(core, "restore", lambda: None), \
                mock.patch.dict(os.environ), \
                mock.patch.object(sys, "argv", ["edge-campaign.py", *arguments]), \
                redirect_stdout(io.StringIO()):
            os.environ.pop("NEEDLETAIL_ALLOW_LIVE", None)
            if allow_live:
                os.environ["NEEDLETAIL_ALLOW_LIVE"] = "1"
            campaign.main()
        return seen

    def test_a_run_holds_its_lab_and_points_its_own_latest(self):
        seen = self.run_campaign("run", "--out", str(self.work / "r1"), "baseline")
        self.assertEqual([name for name, *_ in seen], ["baseline"])
        holder = seen[0][2]
        self.assertEqual(holder["pid"], os.getpid())
        self.assertIn("edge-campaign.py run", holder["command"])
        self.assertIsNone(core.guard.holder(lab.WORK))
        self.assertEqual((self.work / "runs" / "latest").resolve(), (self.work / "r1").resolve())

    def test_a_run_refuses_live_binaries(self):
        live = self.work / "av-mesh"
        live.write_text("")
        with mock.patch.object(core.guard, "lab_binaries", return_value={"av-mesh": live}):
            with self.assertRaisesRegex(SystemExit, "not in a frozen set"):
                self.run_campaign("run", "baseline", allow_live=False)
        self.assertFalse((self.work / "runs").exists())

    def test_down_refuses_at_once_while_another_run_holds_the_lab(self):
        downs = []
        with mock.patch.object(lab, "down", lambda: downs.append(True)):
            with core.guard.hold("edge-campaign.py run baseline"):
                # Inside the hold (a tool of the run): down goes ahead.
                self.run_campaign("down")
                self.assertEqual(len(downs), 1)
                held = os.environ.pop(core.guard.HELD_ENV)
                try:
                    with self.assertRaisesRegex(SystemExit, r"held by pid .*edge-campaign.py run "
                                                            r"baseline.*--force"):
                        self.run_campaign("down")
                    self.assertEqual(len(downs), 1)
                    self.run_campaign("down", "--force")
                    self.assertEqual(len(downs), 2)
                finally:
                    os.environ[core.guard.HELD_ENV] = held
            self.run_campaign("down")
            self.assertEqual(len(downs), 3)

    def test_player_scenarios_take_the_players_lock(self):
        held = {}

        def evidence(item, directory, sample):
            with open(lab.REGISTRY / core.guard.PLAYERS_LOCK, "a+") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held[item.name] = False
                except BlockingIOError:
                    held[item.name] = True
            return {}

        lab.REGISTRY.mkdir(parents=True)
        with mock.patch.object(core, "scenario_evidence", evidence):
            for name in ("player-hlsjs-4k", "ladder-hlsjs-reorder", "baseline"):
                core.run_scenario(core.REGISTRY[name], self.work)
        self.assertEqual(held, {"player-hlsjs-4k": True, "ladder-hlsjs-reorder": True,
                                "baseline": False})


if __name__ == "__main__":
    unittest.main()
