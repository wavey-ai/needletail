#!/usr/bin/env python3
"""Tests of the long-haul gap hold of the deploy tool (low-latency A5): a
distributor outside the contributor's region, fed over a long-haul path,
holds a canonical gap for canonical_gap_hold_long_haul_ms (400 ms) so that no
part waits behind a lost one for the 1 s av-mesh default (every 1 ms PCM part
did, under long-haul loss, in the referee's mesh-w test), and the
contributor's region keeps the default. A hold below media_deadline_ms gives
up replication of parts later than the hold, and the settings say so.
python3 -m unittest scripts/test_edge_churn.py"""

from __future__ import annotations

import importlib.util
import io
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402

_spec = importlib.util.spec_from_file_location("deploy_churn", HERE / "linode-small-mesh-deploy.py")
deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy)
COMPILER = deploy.common.ROOT / "target" / "release" / "needletail-compile"
ROUTES = {"best_direct_rtt_us": 74000, "primary_rtt_us": 74000, "primary_jitter_us": 1000,
          "secondary_rtt_us": 80000, "secondary_jitter_us": 1000}


def values_of(args: list[str], flag: str) -> list[str]:
    return [args[index + 1] for index, arg in enumerate(args) if arg == flag]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for target, name, value in ((deploy, "SETTINGS_FILE", self.dir / "settings.json"),
                                    (deploy, "HUNT_FILE", self.dir / "ecmp-ports.json"),
                                    (deploy.common, "ARTIFACT_DIR", self.dir)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self, overrides=()) -> dict:
        (self.dir / "settings.json").unlink(missing_ok=True)
        with redirect_stderr(io.StringIO()):
            return deploy.load_settings(list(overrides))


class GapHold(Base):
    def test_only_distributors_outside_the_contributors_region_hold_less(self):
        state = lab.lab_state()
        settings = self.settings()
        self.assertEqual(settings["canonical_gap_hold_long_haul_ms"], 400)
        held = {name: deploy.gap_hold_args(node, settings, state)
                for name, node in state["nodes"].items()}
        self.assertEqual(held["new-dist-a"], ["--canonical-gap-hold-ms", "400"])
        self.assertEqual(held["new-dist-b"], ["--canonical-gap-hold-ms", "400"])
        for name in ("lon-dist-a", "lon-dist-b", "lon-edge-1", "new-edge-1",
                     "relay-secondary", "contrib-lon"):
            self.assertEqual(held[name], [], name)

    def test_zero_or_none_renders_nothing(self):
        state = lab.lab_state()
        node = state["nodes"]["new-dist-a"]
        for value in ("0", "null"):
            settings = self.settings([f"canonical_gap_hold_long_haul_ms={value}"])
            self.assertEqual(deploy.gap_hold_args(node, settings, state), [], value)


class Tradeoff(Base):
    def test_a_hold_below_the_deadline_is_stated(self):
        settings = self.settings()
        note = deploy.gap_hold_tradeoff(settings)
        self.assertIsNotNone(note)
        for words in ("canonical_gap_hold_long_haul_ms=400", "media_deadline_ms=1000",
                      "400-1000 ms late", "not replicated"):
            self.assertIn(words, note)

    def test_load_settings_prints_it(self):
        (self.dir / "settings.json").unlink(missing_ok=True)
        err = io.StringIO()
        with redirect_stderr(err):
            deploy.load_settings([])
        self.assertIn("is below media_deadline_ms=1000", err.getvalue())

    def test_no_note_at_or_above_the_deadline_or_when_off(self):
        for overrides in (["canonical_gap_hold_long_haul_ms=1000"],
                          ["canonical_gap_hold_long_haul_ms=1500"],
                          ["canonical_gap_hold_long_haul_ms=0"],
                          ["canonical_gap_hold_long_haul_ms=null"],
                          ["canonical_gap_hold_long_haul_ms=300", "media_deadline_ms=300"]):
            settings = self.settings(overrides)
            self.assertIsNone(deploy.gap_hold_tradeoff(settings), overrides)
        settings = self.settings(["canonical_gap_hold_long_haul_ms=300",
                                  "media_deadline_ms=500"])
        self.assertIn("300-500 ms late", deploy.gap_hold_tradeoff(settings))


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Render(Base):
    def render(self, overrides=()) -> dict:
        settings = self.settings(overrides)
        state = lab.lab_state()
        plan = deploy.compile_plan(deploy.build_program(state, settings, ROUTES))
        return {name: item["args"] for name, item in deploy.render(state, settings, plan).items()}

    def test_the_render_holds_long_haul_gaps_for_400_ms(self):
        args = self.render()
        for name in ("new-dist-a", "new-dist-b"):
            self.assertEqual(values_of(args[name], "--canonical-gap-hold-ms"), ["400"], name)
        for name in ("lon-dist-a", "lon-dist-b", "lon-edge-1", "new-edge-1"):
            self.assertEqual(values_of(args[name], "--canonical-gap-hold-ms"), [], name)

    def test_an_extra_argument_overrides_it(self):
        args = self.render(['extra_distributor_args=["--canonical-gap-hold-ms","700"]'])
        self.assertEqual(values_of(args["new-dist-a"], "--canonical-gap-hold-ms"), ["700"])
        self.assertEqual(values_of(args["lon-dist-a"], "--canonical-gap-hold-ms"), ["700"])


if __name__ == "__main__":
    unittest.main()
