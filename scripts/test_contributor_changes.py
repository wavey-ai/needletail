#!/usr/bin/env python3
"""Tests of workstream A's contributor and ingress changes
(scripts/contributor_changes.py, A 2.4, 2.7, 2.10), offline: the change
engine's simulated mesh (test_mesh_ops.Sim) with two contributors that
serve av-contrib's /api/node (A 2.6) and first forwarders that serve the
counters of /api/mesh/local, a fake clock, no lab, no network. The truth
table of K1 to K6, `classify` on the four classes, a kill without
redundancy refused before anything is touched, the unprotected label, a
quiet roll that stops at the second contributor, kill and return of a
contributor and of an ingress, freeze, readiness after a restart, and the
marks read by the referee's change-window.py.
python3 -m unittest scripts/test_contributor_changes.py"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import contributor_changes as cc  # noqa: E402
import mesh_ops  # noqa: E402
from test_mesh_ops import MARK, NEW, OLD, Clock, Sim  # noqa: E402

FIRSTS = ["lon-dist-a", "lon-dist-b"]
WORKSPACE = HERE.parent.parent
REFEREE = pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT",
                                      str(WORKSPACE / "needletail-compare"))) \
    / "scripts" / "compare" / "change-window.py"


def identity(fingerprint: str, numbering: str = "51c0", envelope: str = "0be2",
             wire: str = "77aa", generation: int = 1, subscription: int = 1) -> dict:
    """An `identity` object as av-contrib --print-fingerprint prints it."""
    return {"fingerprint": fingerprint, "object_version": (2 << 16) | int(fingerprint[-4:], 16),
            "numbering": numbering, "envelope": envelope, "wire": wire,
            "fields": {"fmt": 2, "base_stream_id": 2001, "audio_part": "epoch",
                       "segment_us": 1000000, "deadline_ms": 1000, "clock": "source:aep1",
                       "clock_error_ns": 5000000, "generation": generation,
                       "subscription": subscription, "symbol_size": 1316,
                       "max_datagram_bytes": 1472, "compact": True}}


SAME = identity("9f2c41aa7d10e6b3")


class ContribSim(Sim):
    """Sim with av-contrib's /api/node for the two contributors and the
    first forwarders' /api/mesh/local. `hears[name]`: a source reaches it
    (else it is standing_by and publishes nothing); `ids[git]`: the identity
    a build computes; `xor[name]`: a contributor whose parts differ;
    `plane[forwarder]`: its counters, `rising`: counters that grow at every
    read; `video`: contributors that publish stream 1."""

    def __init__(self, clock, tmp):
        super().__init__(clock, tmp)
        self.hears = {"contrib-lon": True, "contrib-lon-b": True}
        self.ids = {OLD[0]: SAME, NEW[0]: SAME}
        self.xor: dict[str, str] = {}
        self.contract = {"contrib-lon": 0, "contrib-lon-b": 0}
        self.video: set[str] = set()
        self.frozen: set[str] = set()
        self.plane = {name: {"duplicate_mismatch": 0, "not_admitted": 0, "stale_media": 0,
                             "canonical_conflicts": 0, "wide_hole_releases": 0,
                             "skipped_parts": 0} for name in FIRSTS}
        self.rising: set[tuple[str, str]] = set()

    def contributor(self, name: str) -> bool:
        return self.nodes_.get(name, {}).get("role") == "contributor"

    def feeds(self, parent, lease=True):
        if self.contributor(parent):
            node = self.nodes_[parent]
            return node["running"] and self.hears[parent] and parent not in self.frozen
        return super().feeds(parent, lease)

    def windows(self, name: str) -> list[dict]:
        """The last 8 windows of 256 parts closed since the node started
        (one part a millisecond)."""
        head = int(self.clock() * 1000)
        first = -(-int(self.nodes_[name]["start"] * 1000) // 256)
        closed = [w for w in range(head // 256 - 10, head // 256 - 1) if w >= first]
        return [{"first_part": w * 256, "parts": 256, "present": 256,
                 "xor": self.xor.get(name, f"{w:016x}")} for w in closed[-8:]]

    def doc(self, name):
        if not self.contributor(name):
            return super().doc(name)
        if not self.up(name) or name in self.frozen:
            return None
        node = self.nodes_[name]
        git, unix = node["build"]
        if not self.hears[name]:
            state = "standing_by"
        elif self.clock() - node["start"] < 1.0:
            state = "proving"
        else:
            state = "ready"
        streams = []
        if self.hears[name]:
            streams.append({"stream_id": 3001, "class": "audio", "publishing": True,
                            "numbers_reused": self.contract[name], "geometry_changed": 0,
                            "time_ahead": 0, "windows": self.windows(name)})
        if name in self.video:
            streams.append({"stream_id": 1, "class": "video", "publishing": True,
                            "numbers_reused": 0, "geometry_changed": 0, "time_ahead": 0,
                            "windows": []})
        down = [forwarder for forwarder in FIRSTS if not self.nodes_[forwarder]["running"]]
        document = {"schema": "needletail.node.v1", "node_id": name, "region": "london",
                    "role": "contributor",
                    "build": {"id": git, "git": git + "0" * 28, "dirty": False, "unix": unix},
                    "process": {"boot_id": node["boot"], "args_sha256": f"args-{git}"},
                    "state": state, "ready": state == "ready",
                    "not_ready": [] if state == "ready" else ["no source heard"],
                    "degraded": down, "identity": copy.deepcopy(self.ids[git]),
                    "streams": streams}
        if name in self.patch:
            document = self.patch[name](copy.deepcopy(document))
        return document

    def get(self, name, path):
        if path == "/api/mesh/local" and name in self.plane:
            if not self.up(name):
                return None, None
            counters = self.plane[name]
            for forwarder, key in self.rising:
                if forwarder == name:
                    counters[key] += 1
            return 200, {
                "relay_session": {
                    "fast_plane": {"parent_lanes": [
                        {"lane": "primary", "peer": "10.77.1.5:22301",
                         "duplicate_mismatch": counters["duplicate_mismatch"]},
                        {"lane": "secondary", "peer": "10.77.1.6:22301",
                         "duplicate_mismatch": 0}],
                        "not_admitted": counters["not_admitted"],
                        "media_clock": {"stale_epoch": 0,
                                        "stale_media": counters["stale_media"],
                                        "overflow": 0}},
                    "canonical_conflicts": counters["canonical_conflicts"],
                    "wide_hole_releases": counters["wide_hole_releases"]},
                "canonical_gap_skip": {"skipped_parts": counters["skipped_parts"]}}
        return super().get(name, path)

    # ------------------------------------------------- A's backend methods
    def hold_down(self, name):
        self.calls.append(("hold_down", name))

    def kill(self, name):
        self.calls.append(("kill", name))
        self.nodes_[name]["running"] = False

    def release(self, name):
        self.calls.append(("release", name))
        self.restart(name, self.nodes_[name]["build"])

    def freeze(self, name, frozen):
        self.calls.append(("freeze", name, frozen))
        (self.frozen.add if frozen else self.frozen.discard)(name)

    def staged_identity(self, name):
        return copy.deepcopy(self.ids[self.nodes_[name]["staged"][0]])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        self.sim = ContribSim(self.clock, self.tmp)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def change(self, verb: str, *arguments: str) -> int:
        parser = argparse.ArgumentParser()
        cc.add_options(parser, verb)
        args = parser.parse_args(list(arguments))
        return cc.run(self.sim, verb, args, out=self.out, err=self.err, clock=self.clock,
                      sleep=self.clock.sleep)

    def touched(self) -> list:
        return [call for call in self.sim.calls if call[0] in
                ("hold_down", "kill", "release", "freeze", "activate", "stop", "start")]

    def marks(self) -> list[str]:
        lines = self.out.getvalue().splitlines()
        for line in lines:
            self.assertRegex(line, MARK)
        return lines

    def statuses(self) -> dict:
        names = ["contrib-lon", "contrib-lon-b", *FIRSTS]
        return {name: mesh_ops.status_of(self.sim.doc(name)) for name in names}

    def planes(self) -> dict:
        return {name: cc.plane_counters(self.sim.get(name, "/api/mesh/local")[1])
                for name in FIRSTS}

    def lab(self) -> mesh_ops.Lab:
        return mesh_ops.Lab(self.sim.nodes())


# ----------------------------------------------------------------- classify

class Classify(Base):
    def test_the_four_classes(self):
        base = identity("9f2c41aa7d10e6b3")
        self.assertEqual(cc.classify(base, copy.deepcopy(base)), "same")
        self.assertEqual(cc.classify(base, identity("1111111111117777", envelope="ffff")),
                         "two-version")
        self.assertEqual(cc.classify(base, identity("1111111111117777", wire="ffff")),
                         "two-version")
        self.assertEqual(cc.classify(base, identity("2222222222228888", numbering="0000")),
                         "needs-idle")
        self.assertEqual(cc.classify(base, identity("3333333333339999", generation=2)),
                         "restart-bound")
        # The RelaySession identity comes first, whatever else changed.
        self.assertEqual(cc.classify(base, identity("4444444444441111", numbering="0000",
                                                    subscription=7)), "restart-bound")
        self.assertEqual(cc.CLASSES, ("same", "two-version", "needs-idle", "restart-bound"))

    def test_an_identity_without_its_digests_is_refused(self):
        with self.assertRaises(ValueError):
            cc.classify({"fingerprint": "9f2c41aa7d10e6b3"}, SAME)
        with self.assertRaises(ValueError):
            cc.classify(SAME, None)

    def test_identity_of_runs_print_fingerprint_and_reads_its_json(self):
        binary = self.tmp / "av-contrib"
        binary.write_text("#!/bin/sh\n"
                          'for last; do :; done\n'
                          '[ "$last" = --print-fingerprint ] || exit 8\n'
                          f"echo '{json.dumps(SAME)}'\n")
        binary.chmod(0o755)
        self.assertEqual(cc.identity_of(binary, ["/opt/x/av-contrib", "--node-id", "a"]), SAME)
        binary.write_text("#!/bin/sh\necho nothing >&2\nexit 3\n")
        with self.assertRaises(ValueError) as caught:
            cc.identity_of(binary, ["x"])
        self.assertIn("nothing", str(caught.exception))


# ----------------------------------------------------------------- seen

class Seen(Base):
    def test_redundancy_two_of_two_one_of_two_and_idle(self):
        lab = self.lab()
        self.assertEqual(cc.redundancy(lab, self.statuses()["lon-dist-a"]), "2 of 2")
        self.sim.row[("lon-dist-a", "contrib-lon-b")] = {"source_age_ms": 400}
        status = self.statuses()["lon-dist-a"]
        self.assertEqual(cc.redundancy(lab, status), "1 of 2")
        self.assertEqual(cc.seen(lab, status, "contrib-lon-b"), (False, [3001]))
        self.assertEqual(cc.seen(lab, status, "contrib-lon"), (True, []))
        self.sim.hears.update({"contrib-lon": False, "contrib-lon-b": False})
        self.assertEqual(cc.redundancy(lab, self.statuses()["lon-dist-a"]), "idle")
        # The source reaches one contributor only.
        self.sim.hears["contrib-lon"] = True
        self.assertEqual(cc.redundancy(lab, self.statuses()["lon-dist-b"]), "1 of 2")


# ----------------------------------------------------------------- K1 to K6

class GateTable(Base):
    """One poll of the gate for X = contrib-lon, Y = contrib-lon-b, each row
    of 2.10's table in turn, against a previous poll of the healthy mesh."""

    def check(self, staged=None, previous=True, **options):
        lab = self.lab()
        before = (self.statuses(), self.planes()) if previous is True else previous
        return cc.gate_check(lab, "contrib-lon", "contrib-lon-b", FIRSTS, self.statuses(),
                             self.planes(), before, staged, **options)

    def assert_passes(self, result):
        final, problems, _facts = result
        self.assertEqual((final, problems), ([], []))

    def test_a_healthy_pair_passes(self):
        self.assert_passes(self.check())
        self.assert_passes(self.check(staged=SAME))

    def test_k1_the_other_must_be_ready_or_an_idle_pair(self):
        self.sim.hears["contrib-lon-b"] = False
        final, problems, _ = self.check()
        self.assertEqual(final, [])
        self.assertTrue(any(item.startswith("K1: contrib-lon-b is standing_by") for item in problems))
        self.sim.nodes_["contrib-lon-b"]["running"] = False
        self.assertIn("K1: contrib-lon-b does not answer /api/node", self.check()[1])
        self.sim.nodes_["contrib-lon-b"]["running"] = True
        # Both standing_by on one build: an idle pair may lose either.
        self.sim.hears["contrib-lon"] = False
        self.assert_passes(self.check())
        # Both standing_by on two builds: only with --unproven.
        self.sim.nodes_["contrib-lon-b"]["build"] = NEW
        self.assertTrue(any("(--unproven)" in item for item in self.check()[1]))
        self.assert_passes(self.check(unproven=True))

    def test_k2_the_other_is_seen_at_both_first_forwarders(self):
        self.sim.row[("lon-dist-b", "contrib-lon-b")] = {"source_age_ms": 150}
        self.assertEqual(self.check()[1],
                         ["K2: contrib-lon-b is not seen at lon-dist-b for stream(s) [3001]"])
        self.sim.nodes_["lon-dist-a"]["running"] = False
        self.assertIn("K2: lon-dist-a does not answer /api/node", self.check()[1])

    def test_k3_equal_bytes(self):
        before = (self.statuses(), self.planes())
        self.sim.plane["lon-dist-a"]["duplicate_mismatch"] = 2
        self.assertEqual(self.check(previous=before)[1],
                         ["K3: duplicate_mismatch grew at lon-dist-a (0 to 2)"])
        # A counter that is not served is not read as 0.
        self.sim.patch["lon-dist-a"] = lambda doc: doc
        with mock.patch.object(self.sim, "get", lambda name, path: (200, {})):
            self.assertIn("K3: parity unknown: lon-dist-a's duplicate_mismatch",
                          self.check(previous=before)[1])
        self.sim.plane["lon-dist-a"]["duplicate_mismatch"] = 0
        # Equal fingerprints: windows with equal present must have equal xor.
        self.sim.xor["contrib-lon-b"] = "00000000deadbeef"
        problems = self.check()[1]
        self.assertTrue(problems and all(item.startswith("K3: stream 3001 window ")
                                         for item in problems), problems)
        # Different fingerprints (two-version): windows are not compared.
        self.sim.ids[NEW[0]] = identity("1111111111117777", envelope="ffff")
        self.sim.nodes_["contrib-lon-b"]["build"] = NEW
        self.assert_passes(self.check())

    def test_k4_the_contract_counters_of_the_other_stay_still(self):
        before = (self.statuses(), self.planes())
        self.sim.contract["contrib-lon-b"] = 3
        self.assertIn("K4: contrib-lon-b's contract counters grew "
                      "(numbers_reused, geometry_changed, time_ahead)",
                      self.check(previous=before)[1])
        # Non-zero and still is not a growth.
        self.assert_passes(self.check())
        self.sim.patch["contrib-lon-b"] = lambda doc: {
            **doc, "streams": [{key: value for key, value in row.items() if key != "time_ahead"}
                               for row in doc["streams"]]}
        self.assertIn("K4: parity unknown: contrib-lon-b's contract counters", self.check()[1])

    def test_k5_the_class_of_the_change(self):
        self.assert_passes(self.check(staged=identity("1111111111117777", wire="ffff")))
        final, _problems, facts = self.check(staged=identity("2222222222228888",
                                                             numbering="0000"))
        self.assertEqual(facts["class"], "needs-idle")
        self.assertTrue(final and "needs the source stopped" in final[0], final)
        # needs-idle goes on when both are standing_by.
        self.sim.hears.update({"contrib-lon": False, "contrib-lon-b": False})
        self.assert_passes(self.check(staged=identity("2222222222228888", numbering="0000")))
        # restart-bound never, idle or not.
        final, _problems, facts = self.check(staged=identity("3333333333339999", generation=2))
        self.assertEqual(facts["class"], "restart-bound")
        self.assertTrue(final and final[0].startswith("K5:"), final)
        self.assertTrue(self.check(staged=identity("3333333333339999", generation=2),
                                   unprotected=True)[0])

    def test_k6_video(self):
        self.sim.video.add("contrib-lon")
        final, _problems, _facts = self.check()
        self.assertTrue(final and final[0].startswith("K6: contrib-lon publishes video stream(s) [1]"),
                        final)
        self.assert_passes(self.check(video_gap="accept"))
        # K6 stands with --unprotected.
        self.assertTrue(self.check(unprotected=True)[0])

    def test_unprotected_skips_k1_to_k4(self):
        self.sim.hears["contrib-lon-b"] = False
        self.sim.plane["lon-dist-a"]["duplicate_mismatch"] = 9
        self.assert_passes(self.check(unprotected=True, previous=None))

    def test_a_lab_of_one_contributor_cannot_lose_it(self):
        final, _problems, _facts = cc.gate_check(self.lab(), "contrib-lon", None, FIRSTS,
                                                 self.statuses(), self.planes(), None, None)
        self.assertTrue(final and final[0].startswith("K1: contrib-lon is the lab's only"))

    def test_the_gate_passes_on_three_polls_in_a_row(self):
        ok, why, evidence = cc.contrib_gate(self.sim, "contrib-lon", clock=self.clock,
                                            sleep=self.clock.sleep)
        self.assertTrue(ok, why)
        # The first poll has nothing to compare with.
        self.assertEqual(evidence["polls"], 1 + cc.GATE_POLLS)
        self.sim.rising.add(("lon-dist-b", "duplicate_mismatch"))
        began = self.clock()
        ok, why, evidence = cc.contrib_gate(self.sim, "contrib-lon", timeout_s=3,
                                            clock=self.clock, sleep=self.clock.sleep)
        self.assertFalse(ok)
        self.assertIn("K3: duplicate_mismatch grew at lon-dist-b", why)
        self.assertGreaterEqual(self.clock() - began, 3)


# ----------------------------------------------------------------- kill

class Kill(Base):
    def test_a_kill_without_redundancy_is_refused_before_anything_is_touched(self):
        self.sim.hears["contrib-lon-b"] = False
        code = self.change("kill", "--node", "contrib-lon", "--gate-timeout-s", "2")
        self.assertEqual(code, mesh_ops.EXIT_REFUSED)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.touched(), [])
        self.assertIn("K1: contrib-lon-b is standing_by, not ready", self.err.getvalue())
        self.assertIsNone(cc.held_record(self.sim, "contrib-lon"))
        self.assertTrue(self.sim.nodes_["contrib-lon"]["running"])

    def test_unprotected_marks_its_label(self):
        self.sim.hears["contrib-lon-b"] = False
        code = self.change("kill", "--node", "contrib-lon", "--unprotected")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual([line.split()[::2] for line in self.marks()],
                         [["start", "kill.contrib-lon.unprotected"],
                          ["end", "kill.contrib-lon.unprotected"]])

    def test_a_contributor_is_held_down_then_killed_and_its_end_is_seen(self):
        code = self.change("kill", "--node", "contrib-lon", "--marks", str(self.tmp / "m.txt"))
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.touched(), [("hold_down", "contrib-lon"), ("kill", "contrib-lon")])
        lines = self.marks()
        self.assertEqual([line.split()[::2] for line in lines],
                         [["start", "kill.contrib-lon"], ["end", "kill.contrib-lon"]])
        self.assertEqual((self.tmp / "m.txt").read_text().splitlines(), lines)
        record = cc.held_record(self.sim, "contrib-lon")
        self.assertEqual((record["kind"], record["before"]["git"]), ("contributor", OLD[0]))
        # Held down: a second kill is refused; return brings it back.
        self.assertEqual(self.change("kill", "--node", "contrib-lon"), mesh_ops.EXIT_REFUSED)
        self.assertIn("held down by an earlier kill", self.err.getvalue())

    def test_only_a_contributor_or_a_first_forwarder(self):
        for name in ("lon-edge-1", "new-dist-a", "nobody"):
            self.assertEqual(self.change("kill", "--node", name), mesh_ops.EXIT_REFUSED, name)
        self.assertIn("neither a contributor nor a first forwarder", self.err.getvalue())
        self.assertEqual(self.touched(), [])
        self.assertEqual(self.change("return", "--node", "contrib-lon"), mesh_ops.EXIT_REFUSED)
        self.assertIn("is not held down by kill", self.err.getvalue())

    def test_a_video_contributor_needs_the_video_gap_accepted(self):
        self.sim.video.add("contrib-lon")
        self.assertEqual(self.change("kill", "--node", "contrib-lon"), mesh_ops.EXIT_REFUSED)
        self.assertEqual(self.touched(), [])
        self.assertEqual(self.change("kill", "--node", "contrib-lon", "--video-gap", "accept"), 0)

    def test_the_ops_lock_is_held(self):
        lock = mesh_ops.OpsLock(self.sim.lock_path).acquire()
        self.addCleanup(lock.release)
        with mock.patch.dict(os.environ, {mesh_ops.OpsLock.ENV: ""}):
            self.assertEqual(self.change("kill", "--node", "contrib-lon"), mesh_ops.EXIT_REFUSED)
        self.assertIn("holds", self.err.getvalue())
        self.assertEqual(self.touched(), [])

    def test_kill_and_return_of_a_contributor(self):
        self.assertEqual(self.change("kill", "--node", "contrib-lon-b"), 0, self.err.getvalue())
        self.clock.sleep(5)
        code = self.change("return", "--node", "contrib-lon-b")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.touched()[-1], ("release", "contrib-lon-b"))
        self.assertIsNone(cc.held_record(self.sim, "contrib-lon-b"))
        self.assertEqual([line.split()[::2] for line in self.marks()],
                         [["start", "kill.contrib-lon-b"], ["end", "kill.contrib-lon-b"],
                          ["start", "return.contrib-lon-b"], ["end", "return.contrib-lon-b"]])
        ready = json.loads(sorted(self.sim.evidence_dir.glob("return-*.json"))[-1].read_text())
        self.assertTrue(ready["ready"]["ok"])
        self.assertIsNotNone(ready["contributor_ready"]["last"]["windows"]["3001"])

    def test_a_returned_contributor_with_other_bytes_is_not_ready(self):
        self.assertEqual(self.change("kill", "--node", "contrib-lon-b"), 0)
        self.sim.xor["contrib-lon-b"] = "00000000deadbeef"
        self.assertEqual(self.change("return", "--node", "contrib-lon-b"), mesh_ops.EXIT_STOPPED)
        self.assertIn("xor", self.err.getvalue())
        # The end of the return is printed all the same.
        self.assertEqual(self.marks()[-1].split()[::2], ["end", "return.contrib-lon-b"])

    def test_kill_and_return_of_an_ingress(self):
        code = self.change("kill", "--node", "lon-dist-a")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertFalse(self.sim.nodes_["lon-dist-a"]["running"])
        record = cc.held_record(self.sim, "lon-dist-a")
        self.assertEqual(record["kind"], "ingress")
        self.assertTrue(record["pairs"], "the forwarder gate's pairs are kept for R4")
        self.clock.sleep(10)
        code = self.change("return", "--node", "lon-dist-a")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertTrue(self.sim.nodes_["lon-dist-a"]["running"])
        self.assertEqual([line.split()[2] for line in self.marks()],
                         ["kill.lon-dist-a", "kill.lon-dist-a", "return.lon-dist-a",
                          "return.lon-dist-a"])

    def test_an_ingress_whose_sibling_misses_a_contributor_is_not_killed(self):
        self.sim.row[("lon-dist-b", "contrib-lon-b")] = {"source_age_ms": 900}
        self.assertEqual(self.change("kill", "--node", "lon-dist-a", "--gate-timeout-s", "2"),
                         mesh_ops.EXIT_REFUSED)
        self.assertIn("lon-dist-b sees contributors 1 of 2", self.err.getvalue())
        self.assertEqual(self.touched(), [])

    def test_a_kill_whose_end_is_not_seen_says_so(self):
        # A forwarder that keeps showing the contributor's audio as fresh.
        def fresh(doc):
            for entry in doc["parents"]:
                if entry["id"] == "contrib-lon":
                    entry["audio_age_ms"] = 0
            return doc
        original = self.sim.parent_rows

        def rows(child):
            out = original(child)
            if child == "lon-dist-a" and not self.sim.nodes_["contrib-lon"]["running"]:
                out.append({"address": self.sim.nodes_["contrib-lon"]["vlan"], "id": "contrib-lon",
                            "path": "primary", "audio_age_ms": 20, "streams": []})
            return out
        self.sim.parent_rows = rows
        self.assertEqual(self.change("kill", "--node", "contrib-lon"), mesh_ops.EXIT_STOPPED)
        self.assertIn("lon-dist-a shows contrib-lon's audio 20 ms old", self.err.getvalue())
        self.assertEqual([line.split()[0] for line in self.marks()], ["start", "end"])


class Freeze(Base):
    def test_freeze_stops_the_service_and_thaws_it(self):
        code = self.change("freeze", "--node", "contrib-lon", "--ms", "5000")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.touched(), [("freeze", "contrib-lon", True),
                                          ("freeze", "contrib-lon", False)])
        start, end = (float(line.split()[1]) for line in self.marks())
        self.assertGreaterEqual(end - start, 5.0)
        self.assertEqual(self.change("freeze", "--node", "lon-edge-1", "--ms", "10"),
                         mesh_ops.EXIT_REFUSED)


# ----------------------------------------------------------------- readiness

class Ready(Base):
    def restart(self, name, build=OLD):
        self.sim.restart(name, build)

    def test_ready_once_seen_compared_and_still(self):
        self.restart("contrib-lon-b")
        ok, why, evidence = cc.contrib_ready(self.sim, "contrib-lon-b", clock=self.clock,
                                             sleep=self.clock.sleep)
        self.assertTrue(ok, why)
        self.assertEqual(evidence["last"]["state"], "same")
        self.assertGreaterEqual(evidence["settled_s"], cc.SETTLE_S)

    def test_other_bytes_fail_at_once(self):
        self.restart("contrib-lon-b")
        self.sim.xor["contrib-lon-b"] = "00000000deadbeef"
        began = self.clock()
        ok, why, _evidence = cc.contrib_ready(self.sim, "contrib-lon-b", clock=self.clock,
                                              sleep=self.clock.sleep)
        self.assertFalse(ok)
        self.assertIn("xor", why)
        self.assertLess(self.clock() - began, 10)

    def test_a_growing_mismatch_fails_and_a_growing_stale_count_waits(self):
        self.restart("contrib-lon-b")
        self.sim.rising.add(("lon-dist-a", "duplicate_mismatch"))
        ok, why, _ = cc.contrib_ready(self.sim, "contrib-lon-b", clock=self.clock,
                                      sleep=self.clock.sleep)
        self.assertFalse(ok)
        self.assertIn("duplicate_mismatch grew at lon-dist-a", why)
        self.sim.rising = {("lon-dist-b", "stale_media")}
        ok, why, _ = cc.contrib_ready(self.sim, "contrib-lon-b", timeout_s=6, clock=self.clock,
                                      sleep=self.clock.sleep)
        self.assertFalse(ok)
        self.assertIn("stale_media grew at lon-dist-b", why)

    def test_two_versions_may_conflict_and_must_not_leave_holes(self):
        self.sim.ids[NEW[0]] = identity("1111111111117777", envelope="ffff")
        self.restart("contrib-lon-b", NEW)
        self.sim.xor["contrib-lon-b"] = "00000000deadbeef"
        self.sim.rising = {("lon-dist-a", "canonical_conflicts")}
        ok, why, evidence = cc.contrib_ready(self.sim, "contrib-lon-b", clock=self.clock,
                                             sleep=self.clock.sleep)
        self.assertTrue(ok, why)
        self.assertEqual(evidence["last"]["state"], "two-version")
        self.sim.rising = {("lon-dist-a", "skipped_parts")}
        ok, why, _ = cc.contrib_ready(self.sim, "contrib-lon-b", timeout_s=5, clock=self.clock,
                                      sleep=self.clock.sleep)
        self.assertFalse(ok)
        self.assertIn("skipped_parts grew", why)

    def test_a_silent_contributor_is_not_ready_beside_a_publishing_one(self):
        self.sim.hears["contrib-lon-b"] = False
        self.restart("contrib-lon-b")
        ok, why, _ = cc.contrib_ready(self.sim, "contrib-lon-b", timeout_s=4, clock=self.clock,
                                      sleep=self.clock.sleep)
        self.assertFalse(ok)
        self.assertIn("contrib-lon-b is not seen at lon-dist-a for stream(s) [3001]", why)


# ----------------------------------------------------------------- the engine

class Engine(Base):
    def roll(self, **fields) -> int:
        return mesh_ops.roll(self.sim, "contributors", options=mesh_ops.Options(**fields),
                             out=self.out, err=self.err, clock=self.clock,
                             sleep=self.clock.sleep)

    def test_the_engine_calls_this_module(self):
        self.assertEqual(mesh_ops.contributor_rules(), (cc.contrib_gate, cc.contrib_ready))

    def test_a_roll_of_the_contributors_on_the_rules(self):
        self.assertEqual(self.roll(), 0, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("contrib-lon-b", False), ("contrib-lon", False)])
        for name in ("contrib-lon", "contrib-lon-b"):
            self.assertEqual(self.sim.nodes_[name]["build"], NEW)

    def test_a_restart_bound_change_is_stopped_at_the_first_gate(self):
        self.sim.ids[NEW[0]] = identity("3333333333339999", generation=2)
        self.assertEqual(self.roll(), mesh_ops.EXIT_STOPPED)
        self.assertEqual(self.sim.activations(), [])
        self.assertIn("restart-bound", self.err.getvalue())

    def test_a_quiet_roll_stops_at_the_second_contributor(self):
        self.sim.hears.update({"contrib-lon": False, "contrib-lon-b": False})
        self.sim.ids[NEW[0]] = identity("1111111111117777", envelope="ffff")
        code = self.roll(gate_timeout_s=3)
        self.assertEqual(code, mesh_ops.EXIT_STOPPED, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("contrib-lon-b", False)])
        self.assertEqual(self.sim.nodes_["contrib-lon-b"]["build"], NEW)
        self.assertEqual(self.sim.nodes_["contrib-lon"]["build"], OLD)
        self.assertIn("K1: contrib-lon-b is standing_by", self.err.getvalue())
        # With --unproven, the engine's gate would go on: the rule itself.
        statuses = self.statuses()
        final, problems, _ = cc.gate_check(self.lab(), "contrib-lon", "contrib-lon-b", FIRSTS,
                                           statuses, self.planes(), (statuses, self.planes()),
                                           None, unproven=True)
        self.assertEqual((final, problems), ([], []))


# ----------------------------------------------------------------- the referee

@unittest.skipUnless(REFEREE.is_file(), "needs the referee's change-window.py")
class Referee(Base):
    """The marks of kill and return, read by the referee's own windows()
    (change-window.py, read and run, never written)."""

    def test_the_referee_reads_the_marks(self):
        spec = importlib.util.spec_from_file_location("change_window", REFEREE)
        referee = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(referee)
        marks = self.tmp / "marks.txt"
        self.assertEqual(self.change("kill", "--node", "lon-dist-a", "--marks", str(marks)), 0)
        self.clock.sleep(10)
        self.assertEqual(self.change("return", "--node", "lon-dist-a", "--marks", str(marks)), 0)
        found = referee.windows(argparse.Namespace(window=None, marks=str(marks)))
        self.assertEqual(sorted(found), ["kill.lon-dist-a", "return.lon-dist-a"])
        for start, end in found.values():
            self.assertLessEqual(start, end)
        self.assertLess(found["kill.lon-dist-a"][1], found["return.lon-dist-a"][0])
        self.assertEqual(marks.read_text(), self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
