#!/usr/bin/env python3
"""Tests of the change engine (scripts/mesh_ops.py), offline: a simulated
mesh that serves `/api/node` as C 2.3 and A 2.6 write it, a fake clock, no
Linode, no lab, no network. The truth table of the forwarder gate (P1 to
P4, LG1 to LG4, CG1 to CG4, the entry), the capacity gate in legs, the
readiness checks and their causes, the roll's order, gates, rollbacks,
interrupts, journal and resume, the dry mode, the marks, the clock check,
the ops lock, the surge edge, restart and replace.
python3 -m unittest scripts/test_mesh_ops.py"""

from __future__ import annotations

import copy
import io
import json
import os
import pathlib
import re
import shutil
import signal
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mesh_ops  # noqa: E402

OLD = ("aaaaaaaaaaaa", 1_000)
NEW = ("bbbbbbbbbbbb", 2_000)
MARK = re.compile(r"^(start|end) [0-9]+\.[0-9]{9} \S+$")


class Clock:
    def __init__(self, now: float = 1_790_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


def good_row(stream_id: int = 3001, **fields) -> dict:
    row = {"stream_id": stream_id, "track": "audio", "class": "audio", "source_age_ms": 0,
           "repair_age_ms": 1, "head_object": 5_123_456, "window_s": 10, "objects_1s": 1000,
           "objects_10s": 10_000, "skipped_10s": 0, "resets_10s": 0, "multi_symbol_10s": 0,
           "slack_mean_us_1s": 993_100, "slack_mean_us_10s": 993_000, "slack_min_us_10s": 990_800}
    row.update(fields)
    return row


class Sim(mesh_ops.Ops):
    """London: contrib-lon and contrib-lon-b; lon-dist-a and -b (the
    ingresses), the slot lon-dist-c; lon-edge-1 to -3 serving, lon-edge-4 on
    standby. New York: new-dist-a (primary lon-dist-a, repair lane
    lon-dist-b) and new-dist-b (the other way round); new-edge-1 to -3
    serving, new-edge-4 on standby. Stream 3001 is audio. Every node runs
    OLD; `stage` makes NEW the target. A node warms 1 s after its start.
    `broken[(node, git)]`: "silent" (no HTTP), "cold" (never warm),
    "threads" (a class thread short), "mute" (forwards nothing)."""

    def __init__(self, clock: Clock, tmp: pathlib.Path):
        self.clock = clock
        self.lock_path = tmp / "lab.json.ops.lock"
        self.evidence_dir = tmp / "rolls"
        self.calls: list[tuple] = []
        self.nodes_: dict[str, dict] = {}
        self.row: dict[tuple[str, str], dict] = {}
        self.patch: dict[str, callable] = {}
        self.broken: dict[tuple[str, str], str] = {}
        self.offsets: dict[str, float] = {}
        self.hooks: dict[tuple[str, str], callable] = {}
        self.activation_error: dict[str, str] = {}
        self.fail: dict[tuple[str, str], Exception] = {}
        self.drops_rise: set[str] = set()
        started = clock() - 3600

        def add(name, role, site, number, **extra):
            octet = {"london": 1, "newyork": 2}[site]
            self.nodes_[name] = {
                "role": role, "site": site, "vlan": f"10.77.{octet}.{number}",
                "public": f"192.0.{octet}.{number}", "serving": True, "running": True,
                "slot": False, "standby": False, "primary": None, "secondary": None,
                "build": OLD, "prev": OLD, "staged": OLD, "boot": int(started * 1e6),
                "start": started, "drained": False, "legs": 0, "single": 0, "drops": 0,
                "restarts": 0, **extra}

        add("contrib-lon", "contributor", "london", 5)
        add("contrib-lon-b", "contributor", "london", 6)
        add("lon-dist-a", "distributor", "london", 11)
        add("lon-dist-b", "distributor", "london", 12)
        add("lon-dist-c", "distributor", "london", 13, slot=True, serving=False, running=False)
        for index in range(1, 5):
            add(f"lon-edge-{index}", "edge", "london", 20 + index, legs=60 if index < 4 else 0,
                serving=index < 4, running=index < 4, standby=index == 4)
        add("new-dist-a", "distributor", "newyork", 11, primary="lon-dist-a",
            secondary="lon-dist-b")
        add("new-dist-b", "distributor", "newyork", 12, primary="lon-dist-b",
            secondary="lon-dist-a")
        for index in range(1, 5):
            add(f"new-edge-{index}", "edge", "newyork", 20 + index, legs=60 if index < 4 else 0,
                serving=index < 4, running=index < 4, standby=index == 4)

    # ---------------------------------------------------------------- model
    def up(self, name: str) -> bool:
        node = self.nodes_[name]
        return node["running"] and self.broken.get((name, node["build"][0])) != "silent"

    def warm(self, name: str) -> bool:
        node = self.nodes_[name]
        return self.clock() - node["start"] >= 1.0 and \
            self.broken.get((name, node["build"][0])) != "cold"

    def threads_ok(self, name: str) -> bool:
        return self.broken.get((name, self.nodes_[name]["build"][0])) != "threads"

    def feeds(self, parent: str, lease: bool = True) -> bool:
        """A drained forwarder loses its lease children and keeps feeding
        its compiled ones."""
        node = self.nodes_[parent]
        return node["running"] and node["serving"] and not (
            lease and node["role"] == "distributor" and node["drained"])

    def parent_rows(self, child: str) -> list[dict]:
        node = self.nodes_[child]
        if node["role"] == "contributor":
            return []
        if node["role"] == "edge":
            parents = [name for name, item in self.nodes_.items()
                       if item["role"] == "distributor" and item["site"] == node["site"]
                       and self.feeds(name)]
            via = "vlan"
        elif node["site"] == "london":
            parents = [name for name in ("contrib-lon", "contrib-lon-b") if self.feeds(name)]
            via = "vlan"
        else:
            parents = [name for name in (node["primary"], node["secondary"])
                       if self.feeds(name, lease=False)]
            via = "public"
        out = []
        for parent in sorted(parents):
            age = self.clock() - self.nodes_[parent]["start"]
            window = int(min(10, max(0, age)))
            row = good_row(window_s=window, objects_10s=1000 * window)
            if self.broken.get((parent, self.nodes_[parent]["build"][0])) == "mute":
                # A build that keeps its leases and forwards nothing.
                row.update(objects_1s=0, objects_10s=0, source_age_ms=9000)
            row.update(self.row.get((child, parent), {}))
            path = "secondary" if node.get("secondary") == parent else "primary"
            out.append({"address": self.nodes_[parent][via], "id": parent, "path": path,
                        "audio_age_ms": 0, "video_age_ms": None, "streams": [row]})
        return out

    def state(self, name: str) -> tuple[str, list[str]]:
        node = self.nodes_[name]
        reasons = []
        if not self.threads_ok(name):
            reasons.append("class_threads 1/2")
        if not self.warm(name):
            reasons.append("window 3001 1200/4500")
        if node["drained"]:
            reasons.append("draining: operator")
        if not self.warm(name):
            return "starting", reasons
        if node["drained"]:
            return "draining", reasons
        if not self.threads_ok(name):
            return "degraded", reasons
        return "ready", reasons

    def edge_udp(self, name: str) -> dict:
        node = self.nodes_[name]
        legs = 0 if node["drained"] else node["legs"]
        others = [other for other in self.edges(node["site"]) if other != name]
        by_other = {other: legs // len(others) for other in others} if others and legs else {}
        return {"subscribers": legs, "hls_sessions": 0, "hls_moved_active": 0,
                "hls_requests_in_flight": 0, "refusals": 0, "load_permille": legs * 5,
                "rebalance_moves": 0, "parents_lost": False, "single_legs": node["single"],
                "legs_by_other": by_other}

    def move_legs(self, name: str) -> None:
        """A drained edge's legs move to the other serving edges of its
        region that are not drained (B's make-before-break moves)."""
        node = self.nodes_[name]
        targets = [other for other in self.edges(node["site"])
                   if other != name and not self.nodes_[other]["drained"]]
        if node["role"] != "edge" or not targets or not node["legs"]:
            return
        share, rest = divmod(node["legs"], len(targets))
        for index, other in enumerate(targets):
            self.nodes_[other]["legs"] += share + (1 if index < rest else 0)
        node["legs"] = 0

    def edges(self, site: str) -> list[str]:
        return sorted(name for name, node in self.nodes_.items()
                      if node["role"] == "edge" and node["site"] == site and node["serving"]
                      and node["running"])

    def doc(self, name: str) -> dict | None:
        if not self.up(name):
            return None
        node = self.nodes_[name]
        if name in self.drops_rise:
            node["drops"] += 1
        state, reasons = self.state(name)
        git, unix = node["build"]
        if node["role"] == "contributor":
            document = {"schema": "needletail.node.v1", "node_id": name, "region": node["site"],
                        "role": "contributor",
                        "build": {"id": git, "git": git + "0" * 28, "dirty": False, "unix": unix},
                        "process": {"boot_id": node["boot"], "args_sha256": f"args-{git}"},
                        "state": state, "ready": state == "ready", "not_ready": reasons,
                        "streams": [{"stream_id": 3001, "class": "audio", "publishing": True}]}
        else:
            document = {
                "schema": "needletail.node.v1", "node_id": name, "region": node["site"],
                "role": node["role"],
                "build": {"git": git, "git_full": git + "0" * 28, "dirty": False,
                          "built_unix": unix, "version": "0.1.0"},
                "process": {"pid": 1, "boot_id": node["boot"], "args_sha256": f"args-{git}"},
                "state": state, "ready": state == "ready", "warm": self.warm(name),
                "ready_forced": False, "not_ready": reasons,
                "drain_reasons": ["operator"] if node["drained"] else [],
                "hello_draining": state != "ready",
                "fast_plane": {"threads": 2 if self.threads_ok(name) else 1, "threads_expected": 2,
                               "received_1s": 1000, "admitted_1s": 1000},
                "ingest": {"audio_deadline_drops": node["drops"], "video_deadline_drops": 0},
                "parents": self.parent_rows(name),
                "streams": [{"stream_id": 3001, "class": "audio", "seen_age_ms": 0,
                             "decoded": True}],
                "children": {"compiled": [], "lease": {"children": 0, "pairs": 0,
                                                       "pairs_list": [], "streams": []}},
                "cache_mesh": {"warm_parts_pending": 0},
                "client_udp": self.edge_udp(name) if node["role"] == "edge" else None,
                "entry": {"caches_fresh": sum(1 for edge in self.edges(node["site"])
                                              if not self.nodes_[edge]["drained"]),
                          "caches_draining": 0}}
            if node["role"] == "distributor":
                children = [] if node["drained"] else self.edges(node["site"])
                document["children"]["lease"] = {
                    "children": len(children), "pairs": len(children),
                    "pairs_list": [{"child": self.nodes_[edge]["vlan"], "stream_id": 3001,
                                    "primary": True, "rank": 0, "warm_parts_pending": 0}
                                   for edge in children],
                    "streams": [{"stream_id": 3001, "children": len(children), "fed_age_ms": 0}]}
                document["children"]["compiled"] = [
                    {"target": f"{self.nodes_[child]['public']}:22200", "lane": "source",
                     "class": "audio", "promoted": False}
                    for child, item in sorted(self.nodes_.items())
                    if name in (item["primary"], item["secondary"])]
        if name in self.patch:
            document = self.patch[name](copy.deepcopy(document))
        return document

    def hook(self, verb: str, name: str) -> None:
        action = self.hooks.pop((verb, name), None)
        if action:
            action()

    # ---------------------------------------------------------------- Ops
    def nodes(self):
        return [{"name": name, "role": node["role"], "site": node["site"], "vlan": node["vlan"],
                 "public": node["public"], "serving": node["serving"] and node["running"],
                 "slot": node["slot"], "standby": node["standby"],
                 "created": node.get("created", False), "primary": node["primary"],
                 "secondary": node["secondary"]} for name, node in self.nodes_.items()]

    def status(self, name):
        return self.doc(name)

    def get(self, name, path):
        if not self.up(name):
            return None, None
        if path.startswith("/api/assignment"):
            site = self.nodes_[name]["site"]
            return 200, {"caches": [{"node_id": edge, "fresh": self.warm(edge),
                                     "draining": self.nodes_[edge]["drained"]}
                                    for edge in self.edges(site)]}
        return 404, None

    def post(self, name, path, body):
        self.calls.append(("post", name, path.rsplit("/", 1)[1]))
        assert body == {"node_id": name}
        if not self.up(name):
            return None
        if path.endswith("drain-node"):
            self.move_legs(name)
            self.nodes_[name]["drained"] = True
        elif path.endswith("resume-node"):
            self.nodes_[name]["drained"] = False
        self.hook("post", name)
        return 202

    def check(self, command, names):
        self.calls.append(("check", command, tuple(names)))

    def target(self, tier):
        return {"git": NEW[0], "unix": NEW[1], "dirty": False}

    def expected(self, name, rollback=False):
        git, unix = self.nodes_[name]["prev" if rollback else "staged"]
        return {"git": git, "unix": unix, "args_sha256": f"args-{git}"}

    def stage(self, names, build):
        self.calls.append(("stage", tuple(names)))
        for name in names:
            self.nodes_[name]["staged"] = (build["git"], build["unix"]) if build \
                else self.nodes_[name]["build"]

    def restart(self, name, build):
        node = self.nodes_[name]
        node["build"] = build
        node["start"] = self.clock()
        node["boot"] = int(self.clock() * 1e6) + 1
        node["running"] = True

    def activate(self, name, not_after_ms, rollback=False):
        self.calls.append(("activate", name, rollback))
        if self.clock() * 1000 > not_after_ms:
            return {"ok": False, "why": "late"}
        error = self.activation_error.pop(name, None)
        if error == "before":
            raise OSError("ssh: connection reset before the script ran")
        node = self.nodes_[name]
        if rollback:
            # The script copies .args.prev to .args and keeps .args.prev.
            target = node["prev"]
        else:
            target = node["staged"]
            node["prev"] = node["build"]
        self.restart(name, target)
        self.hook("activate", name)
        if error == "after":
            raise OSError("ssh: connection reset after the script ran")
        return {"ok": True, "why": ""}

    def boot_changed(self, name, since_ms):
        return self.nodes_[name]["start"] * 1000 > since_ms

    def unit_restarts(self, name):
        return self.nodes_[name]["restarts"]

    def clock_offset(self, name):
        return self.offsets.get(name, 0.002)

    def surge_candidates(self, site):
        return sorted(name for name, node in self.nodes_.items()
                      if node["role"] == "edge" and node["site"] == site and node["standby"]
                      and not node["running"])

    def start_node(self, name):
        """Starts on what is staged (put in force as an activation does)."""
        self.calls.append(("start", name))
        self._maybe_fail("start", name)
        node = self.nodes_[name]
        if node["staged"] != node["build"]:
            node["prev"] = node["build"]
        node.update(serving=True, standby=False)
        self.restart(name, node["staged"])

    def stop_node(self, name, not_after_ms=None):
        self.calls.append(("stop", name))
        if not_after_ms is not None and self.clock() * 1000 > not_after_ms:
            raise mesh_ops.Refused("late")
        node = self.nodes_[name]
        node["running"] = False
        if node["role"] == "edge":
            node.update(serving=False, standby=True)

    def _maybe_fail(self, verb, name):
        error = self.fail.pop((verb, name), None)
        if error:
            raise error

    def rebuild(self, name, mode):
        self.calls.append(("rebuild", name, mode))
        self._maybe_fail("rebuild", name)
        self.nodes_[name].update(running=False, drained=False)
        if mode == "new":
            self.nodes_[name]["public"] = "192.0.9." + self.nodes_[name]["public"].rsplit(".", 1)[1]

    def push(self, name):
        self.calls.append(("push", name))
        self._maybe_fail("push", name)
        node = self.nodes_[name]
        node["staged"] = node["build"]
        if node["role"] == "edge":
            node.update(serving=True, standby=False)
        self.restart(name, node["build"])

    def add_slot(self, name):
        self.calls.append(("add_slot", name))
        self._maybe_fail("add_slot", name)
        self.nodes_[name]["created"] = True
        self._maybe_fail("add_slot_push", name)
        self.nodes_[name].update(serving=True)
        self.restart(name, OLD)

    def remove_slot(self, name):
        self.calls.append(("remove_slot", name))
        self.nodes_[name].update(serving=False, running=False, created=False)

    # ---------------------------------------------------------------- views
    def activations(self):
        return [(call[1], call[2]) for call in self.calls if call[0] == "activate"]

    def changing(self):
        return [call for call in self.calls if call[0] not in ("check",)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        self.sim = Sim(self.clock, self.tmp)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def options(self, **fields) -> mesh_ops.Options:
        return mesh_ops.Options(**fields)

    def roll(self, tier, **kwargs) -> int:
        options = kwargs.pop("options", None) or self.options()
        return mesh_ops.roll(self.sim, tier, options=options, out=self.out, err=self.err,
                             clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def stdout_marks(self) -> list[str]:
        lines = self.out.getvalue().splitlines()
        for line in lines:
            self.assertRegex(line, MARK)
        return lines

    def status(self, name):
        return mesh_ops.status_of(self.sim.doc(name))

    def lab(self):
        return mesh_ops.Lab(self.sim.nodes())

    def journals(self):
        return [json.loads(path.read_text())
                for path in sorted(self.sim.evidence_dir.glob("*.state.json"))]

    def interrupt_after(self, method: str, node: str, signum=signal.SIGINT, when=None):
        """Send a signal to this process once, right after the Sim's
        `method` acted on `node` (and `when(args)` holds)."""
        original = getattr(self.sim, method)
        armed = {"on": True}

        def wrapper(name, *args, **kwargs):
            result = original(name, *args, **kwargs)
            if armed["on"] and name == node and (when is None or when(args)):
                armed["on"] = False
                os.kill(os.getpid(), signum)
            return result
        setattr(self.sim, method, wrapper)

    def unfinished(self, command=None):
        return mesh_ops.Journal.unfinished(self.sim.evidence_dir, command)


def fake_rules(calls: list | None = None, gate_ok=True, ready_ok=True):
    def contrib_gate(ops, node, **options):
        if calls is not None:
            calls.append(("contrib_gate", node))
        return gate_ok, "" if gate_ok else "K1: the other contributor is not ready", {"k": 1}

    def contrib_ready(ops, node, **options):
        if calls is not None:
            calls.append(("contrib_ready", node))
        return ready_ok, "" if ready_ok else "windows differ", {"r": 1}

    return lambda: (contrib_gate, contrib_ready)


# --------------------------------------------------------------------- parity

class Parity(unittest.TestCase):
    """feed_parity, P1 to P4 (3.3), on a child's own record."""

    LEAVING = {"10.77.1.11"}
    STAYING = {"10.77.1.12"}

    def child(self, leaving=None, staying=None):
        document = {"role": "edge", "parents": [
            {"address": "10.77.1.11", "id": "lon-dist-a", "path": "primary",
             "streams": [good_row(**(leaving or {}))]},
            {"address": "10.77.1.12", "id": "lon-dist-b", "path": "primary",
             "streams": [good_row(**(staying or {}))]}]}
        return mesh_ops.status_of(document)

    def parity(self, leaving=None, staying=None, **kwargs):
        return mesh_ops.feed_parity(self.child(leaving, staying), 3001, self.LEAVING,
                                    self.STAYING, **kwargs)

    def test_two_equal_parents_pass(self):
        self.assertEqual(self.parity(), [])

    def test_a_parent_that_is_alive_but_4_ms_late_is_refused(self):
        problems = self.parity(staying={"slack_mean_us_10s": 989_000, "slack_min_us_10s": 986_800})
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all(item.startswith("P3 on time") for item in problems))
        self.assertIn("989000", problems[0])
        self.assertIn("993000", problems[0])
        # Only its worst object late: refused all the same.
        self.assertEqual(len(self.parity(staying={"slack_min_us_10s": 986_800})), 1)
        # Within the 1 ms of the rule: passes.
        self.assertEqual(self.parity(staying={"slack_mean_us_10s": 992_100,
                                              "slack_min_us_10s": 989_900}), [])
        # --parity-ms widens it knowingly.
        self.assertEqual(self.parity(staying={"slack_mean_us_10s": 989_000,
                                              "slack_min_us_10s": 986_800}, parity_ms=4.5), [])

    def test_a_parent_that_skipped_an_object_is_refused(self):
        problems = self.parity(staying={"skipped_10s": 1})
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("P2 complete"))

    def test_a_parent_that_reset_or_has_less_than_ten_seconds_is_refused(self):
        self.assertTrue(self.parity(staying={"resets_10s": 1})[0].startswith("P2"))
        self.assertTrue(self.parity(staying={"window_s": 9})[0].startswith("P2"))

    def test_a_parent_behind_the_leaving_one_by_more_than_100_ms_is_refused(self):
        # 10,000 objects from the leaving parent at 1,000 a second: 9,900 is
        # 100 ms behind and passes; 9,899 does not.
        self.assertEqual(self.parity(staying={"objects_10s": 9_900}), [])
        self.assertTrue(self.parity(staying={"objects_10s": 9_899})[0].startswith("P2"))

    def test_a_stale_parent_is_refused(self):
        self.assertEqual(self.parity(staying={"source_age_ms": 100}), [])
        self.assertTrue(self.parity(staying={"source_age_ms": 101})[0].startswith("P1 live"))

    def test_a_missing_figure_fails_it(self):
        for field in mesh_ops.PARENT_FIELDS:
            staying = good_row()
            del staying[field]
            document = {"role": "edge", "parents": [
                {"address": "10.77.1.11", "streams": [good_row()]},
                {"address": "10.77.1.12", "streams": [staying]}]}
            problems = mesh_ops.feed_parity(mesh_ops.status_of(document), 3001, self.LEAVING,
                                            self.STAYING)
            self.assertEqual(len(problems), 1, field)
            self.assertIn("parity unknown", problems[0])
            self.assertIn(field, problems[0])
        for field in mesh_ops.LEAVING_FIELDS:
            leaving = good_row()
            leaving[field] = None
            document = {"role": "edge", "parents": [
                {"address": "10.77.1.11", "streams": [leaving]},
                {"address": "10.77.1.12", "streams": [good_row()]}]}
            problems = mesh_ops.feed_parity(mesh_ops.status_of(document), 3001, self.LEAVING,
                                            self.STAYING)
            self.assertIn("parity unknown", problems[0], field)
        # No row at all, no record at all, no child.
        only_x = mesh_ops.status_of({"role": "edge", "parents": [
            {"address": "10.77.1.11", "streams": [good_row()]}]})
        self.assertIn("parity unknown",
                      mesh_ops.feed_parity(only_x, 3001, self.LEAVING, self.STAYING)[0])
        self.assertIn("parity unknown", mesh_ops.feed_parity(
            mesh_ops.status_of({"role": "edge"}), 3001, self.LEAVING, self.STAYING)[0])
        self.assertIn("parity unknown",
                      mesh_ops.feed_parity(None, 3001, self.LEAVING, self.STAYING)[0])

    def test_multi_symbol_audio_on_a_repair_lane_is_refused(self):
        self.assertEqual(self.parity(leaving={"multi_symbol_10s": 3}), [])
        problems = self.parity(leaving={"multi_symbol_10s": 3}, repair_lane=True)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("P4 whole objects"))
        self.assertEqual(self.parity(repair_lane=True), [])


class StatusOf(unittest.TestCase):
    def test_the_mesh_node_json_of_the_specification(self):
        document = {
            "schema": "needletail.node.v1", "node_id": "tok-dist-a", "region": "tokyo",
            "role": "distributor",
            "build": {"git": "3f2a9c1d77e0", "git_full": "3f2a9c1d77e0b4c1", "dirty": False,
                      "built_unix": 1763900000, "version": "0.1.0"},
            "process": {"pid": 812, "boot_id": 1763924000123456, "started_unix_ms": 1763924000123,
                        "uptime_ms": 86400, "args_sha256": "9a1f", "cpus": 2, "cpu_permille": 180},
            "state": "ready", "ready": True, "warm": True, "ready_unix_ms": 1763924006400,
            "ready_forced": False, "not_ready": [], "drain_reasons": [], "hello_draining": False,
            "fast_plane": {"threads": 2, "threads_expected": 2, "realtime_threads": 1,
                           "pinned_threads": 2, "received_1s": 4120, "admitted_1s": 4120,
                           "not_admitted_1s": 0, "received_age_ms": 0},
            "ingest": {"audio_deadline_drops": 0, "video_deadline_drops": 0, "tee_dropped": 0},
            "parents": [{"address": "172.236.1.10", "id": "lon-dist-a", "path": "primary",
                         "audio_age_ms": 0, "video_age_ms": None, "streams": [good_row()]}],
            "streams": [{"stream_id": 3001, "class": "audio", "seen_age_ms": 0, "decoded": True,
                         "ingest_age_ms": 1, "head_part": 5123456, "first_part": 5111456,
                         "window_parts": 12001, "warm_parts": 4500}],
            "children": {
                "compiled": [{"target": "139.162.1.5:22200", "lane": "source", "class": "audio",
                              "promoted": False}],
                "lease": {"children": 3, "pairs": 6, "refused": 0,
                          "pairs_list": [{"child": "10.80.3.21", "stream_id": 3001,
                                          "primary": True, "rank": 0, "warm_parts_pending": 0}],
                          "streams": [{"stream_id": 3001, "children": 3, "fed_age_ms": 0}]}},
            "cache_mesh": {"parents_live": 2, "pending_release": 0, "peers": [], "children": 3,
                           "pairs": 6, "subscribed_playlists": [], "warm_parts_pending": 0,
                           "rx_unknown_kinds": 0, "rx_unknown_roles": 0, "rx_invalid_frames": 0},
            "client_udp": None, "entry": {"caches_fresh": 3, "caches_draining": 0},
            "node_warm": None}
        status = mesh_ops.status_of(document)
        self.assertEqual((status.build_git, status.build_unix, status.dirty),
                         ("3f2a9c1d77e0", 1763900000, False))
        self.assertEqual(status.boot_id, 1763924000123456)
        self.assertEqual((status.state, status.ready, status.warm), ("ready", True, True))
        self.assertEqual(status.parents[0].address, "172.236.1.10")
        self.assertEqual(status.parents[0].rows[(3001, "audio")]["slack_min_us_10s"], 990_800)
        self.assertEqual(status.compiled_targets, ["139.162.1.5:22200"])
        self.assertEqual(status.lease_pairs, [("10.80.3.21", 3001)])
        self.assertEqual((status.lease_pairs_total, status.lease_fed_age_ms), (6, {3001: 0}))
        self.assertEqual((status.warm_parts_pending, status.caches_fresh), (0, 3))
        self.assertEqual(status.stream_class(3001), "audio")
        self.assertIsNone(status.client_udp)

    def test_the_contributor_json_of_workstream_a(self):
        document = {"schema": "needletail.node.v1", "node_id": "contrib-lon-b", "region": "london",
                    "role": "contributor",
                    "build": {"id": "3f2a9c1d77e0", "git": "3f2a9c1d77e0b4c1", "dirty": False,
                              "unix": 1763900000, "crate": "0.1.0"},
                    "process": {"pid": 812, "boot_id": 1763924000123456, "args_sha256": "9a1f"},
                    "state": "standing_by", "ready": False, "not_ready": ["no source"],
                    "streams": [{"stream_id": 3001, "class": "audio", "publishing": True}]}
        status = mesh_ops.status_of(document)
        self.assertEqual((status.build_git, status.build_unix), ("3f2a9c1d77e0", 1763900000))
        self.assertEqual((status.state, status.ready), ("standing_by", False))
        self.assertIsNone(status.parents)

    def test_a_missing_field_is_none_never_zero(self):
        status = mesh_ops.status_of({"role": "edge", "client_udp": {"subscribers": True}})
        self.assertIsNone(status.boot_id)
        self.assertIsNone(status.udp("subscribers"))
        self.assertIsNone(status.audio_deadline_drops)
        self.assertIsNone(status.lease_pairs)
        self.assertIsNone(mesh_ops.status_of(None))
        self.assertIsNone(mesh_ops.status_of("<html>"))


# ------------------------------------------------------------------- gates

class ForwarderGate(Base):
    def gate(self, name, **kwargs):
        kwargs.setdefault("timeout_s", 30.0)
        return mesh_ops.forwarder_gate(self.sim, name, clock=self.clock, sleep=self.clock.sleep,
                                       **kwargs)

    def assert_refused(self, name, text, **kwargs):
        gate = self.gate(name, **kwargs)
        self.assertFalse(gate.ok)
        self.assertTrue(any(text in item for item in gate.problems), gate.problems)
        return gate

    def test_a_healthy_mesh_passes_on_two_polls(self):
        for name in ("new-dist-a", "new-dist-b", "lon-dist-a", "lon-dist-b"):
            gate = self.gate(name)
            self.assertTrue(gate.ok, (name, gate.problems))
            self.assertEqual(gate.polls, 2)
        gate = self.gate("new-dist-b")
        self.assertEqual(sorted(pair[0] for pair in gate.pairs),
                         ["new-edge-1", "new-edge-2", "new-edge-3"])
        self.assertEqual(gate.evidence["video_children"], 0)

    def test_lg2_the_child_s_record_of_the_other_parent(self):
        self.sim.row[("new-edge-2", "new-dist-a")] = {"slack_mean_us_10s": 989_000}
        gate = self.assert_refused("new-dist-b", "LG2 new-edge-2 <- new-dist-a 3001: P3 on time",
                                   timeout_s=2)
        self.assertEqual(len(gate.problems), 1)
        self.sim.row[("new-edge-2", "new-dist-a")] = {"skipped_10s": 2}
        self.assert_refused("new-dist-b", "P2 complete", timeout_s=2)

    def test_lg1_a_child_that_is_stopping_or_silent(self):
        self.sim.patch["new-edge-1"] = lambda doc: {**doc, "state": "stopping"}
        self.assert_refused("new-dist-b", "LG1: new-edge-1 is stopping", timeout_s=2)
        self.sim.patch.clear()
        # A child whose HTTP is silent while its lease is still listed.
        self.sim.broken[("new-edge-1", OLD[0])] = "silent"
        self.assert_refused("new-dist-b", "LG1: new-edge-1 does not answer", timeout_s=2)

    def test_lg3_the_other_parent_holds_and_feeds_the_lease(self):
        cases = {
            "not ready": lambda doc: {**doc, "ready": False, "state": "degraded"},
            "hellos say draining": lambda doc: {**doc, "hello_draining": True},
            "does not hold the lease": lambda doc: {
                **doc, "children": {**doc["children"], "lease": {
                    **doc["children"]["lease"], "pairs_list": [
                        row for row in doc["children"]["lease"]["pairs_list"]
                        if row["child"] != "10.77.2.21"]}}},
            "fed 3001 250 ms ago": lambda doc: {
                **doc, "children": {**doc["children"], "lease": {
                    **doc["children"]["lease"],
                    "streams": [{"stream_id": 3001, "children": 3, "fed_age_ms": 250}]}}},
        }
        for text, patch in cases.items():
            self.sim.patch = {"new-dist-a": patch}
            self.assert_refused("new-dist-b", text, timeout_s=2)

    def test_lg4_the_other_parent_is_itself_fed_and_not_late(self):
        self.sim.row[("new-dist-a", "lon-dist-a")] = {"source_age_ms": 500}
        self.sim.row[("new-dist-a", "lon-dist-b")] = {"skipped_10s": 1}
        self.assert_refused("new-dist-b", "LG4: new-dist-a is not itself fed", timeout_s=2)
        self.sim.row.clear()
        self.sim.drops_rise.add("new-dist-a")
        self.assert_refused("new-dist-b", "LG4: new-dist-a dropped audio past its deadline",
                            timeout_s=2)

    def test_cg_checks_of_the_long_haul_children(self):
        # CG2: new-dist-a's repair lane from lon-dist-b, 4 ms late.
        self.sim.row[("new-dist-a", "lon-dist-b")] = {"slack_mean_us_10s": 989_000}
        self.assert_refused("lon-dist-a", "CG2 new-dist-a <- lon-dist-b 3001: P3", timeout_s=2)
        self.sim.row.clear()
        # P4 on the repair lane: X's objects of several symbols.
        self.sim.row[("new-dist-a", "lon-dist-a")] = {"multi_symbol_10s": 5}
        self.assert_refused("lon-dist-a", "P4 whole objects", timeout_s=2)
        self.sim.row.clear()
        # CG1: the child is not ready.
        self.sim.patch["new-dist-b"] = lambda doc: {**doc, "ready": False, "state": "degraded"}
        self.assert_refused("lon-dist-a", "CG1: new-dist-b is not ready", timeout_s=2)
        self.sim.patch.clear()
        # CG3: the running node and the tree disagree.
        self.sim.patch["lon-dist-a"] = lambda doc: {**doc, "children": {
            **doc["children"], "compiled": doc["children"]["compiled"][:1]}}
        self.assert_refused("lon-dist-a", "CG3", timeout_s=2)
        self.sim.patch.clear()
        # CG4: the other ingress is behind.
        self.sim.drops_rise.add("lon-dist-b")
        self.assert_refused("lon-dist-a", "CG4: lon-dist-b dropped", timeout_s=2)
        self.sim.drops_rise.clear()
        self.sim.broken[("lon-dist-b", OLD[0])] = "threads"
        self.assert_refused("lon-dist-a", "CG4: lon-dist-b runs 1/2 class threads", timeout_s=2)

    def test_the_first_distributor_needs_its_sibling_as_entry(self):
        original = self.sim.get
        self.sim.get = lambda name, path: (503, None) if name == "lon-dist-b" \
            else original(name, path)
        self.assert_refused("lon-dist-a", "entry: lon-dist-b's /api/assignment answered 503",
                            timeout_s=2)
        # dist-b is not the entry: no such check for it.
        self.assertTrue(self.gate("lon-dist-b").ok)

    def test_an_idle_mesh_passes_on_presence_alone(self):
        for child in [name for name in self.sim.nodes_]:
            for parent in self.sim.nodes_:
                self.sim.row[(child, parent)] = {"objects_1s": 0, "objects_10s": 0,
                                                 "source_age_ms": 9000,
                                                 "slack_mean_us_10s": 0}
        gate = self.gate("lon-dist-a")
        self.assertTrue(gate.ok, gate.problems)
        self.assertTrue(gate.evidence["idle"])
        # Even idle, a child that does not answer fails LG1.
        self.sim.patch["lon-edge-1"] = lambda doc: None
        self.assert_refused("lon-dist-a", "LG1: lon-edge-1 does not answer", timeout_s=2)

    def test_the_sibling_s_ten_clean_seconds_pace_the_gate(self):
        self.sim.nodes_["new-dist-a"]["start"] = self.clock() - 3
        began = self.clock()
        gate = self.gate("new-dist-b")
        self.assertTrue(gate.ok, gate.problems)
        self.assertGreaterEqual(self.clock() - began, 7.0)
        self.assertLess(self.clock() - began, 8.0)

    def test_a_missing_figure_at_x_fails_the_gate(self):
        self.sim.patch["new-dist-b"] = lambda doc: {
            key: value for key, value in doc.items() if key != "parents"}
        self.assert_refused("new-dist-b", "parity unknown: new-dist-b's parents[]", timeout_s=2)
        self.sim.patch["new-dist-b"] = lambda doc: {**doc, "children": {
            "compiled": [], "lease": {"pairs": 3}}}
        self.assert_refused("new-dist-b", "pairs_list", timeout_s=2)

    def test_a_lease_pair_x_does_not_feed_is_listed_not_judged(self):
        # A reader holds FLAC 2001 at new-edge-1 with no source: new-dist-b
        # keeps the lease and no child has a row for it.
        def held(doc):
            doc["children"]["lease"]["pairs_list"].append(
                {"child": "10.77.2.21", "stream_id": 2001, "primary": True, "rank": 0})
            doc["streams"].append({"stream_id": 2001, "class": "audio", "seen_age_ms": 60000,
                                   "decoded": False})
            return doc
        self.sim.patch["new-dist-b"] = held
        gate = self.gate("new-dist-b", timeout_s=3)
        self.assertTrue(gate.ok, gate.problems)
        self.assertEqual(gate.evidence["not_fed"], [{"child": "new-edge-1", "stream_id": 2001}])
        self.assertIn(("new-edge-1", "10.77.2.21", 2001, False), gate.pairs)
        self.assertIn(("new-edge-1", "10.77.2.21", 3001, True), gate.pairs)
        # Not fed, the child still needs another parent that is ready.
        self.sim.patch["new-dist-a"] = lambda doc: {**doc, "ready": False, "state": "degraded"}
        self.assert_refused("new-dist-b", "LG3: new-dist-a is not ready", timeout_s=2)
        self.sim.patch.pop("new-dist-a")
        # A row for X without objects_1s is a missing figure, not "not fed".
        self.sim.row[("new-edge-2", "new-dist-b")] = {"objects_1s": None}
        self.assert_refused("new-dist-b", "parity unknown: new-edge-2's row for new-dist-b has "
                            "no objects_1s", timeout_s=2)

    def test_video_streams_are_listed_not_judged(self):
        def video(doc):
            doc["children"]["lease"]["pairs_list"].append(
                {"child": "10.77.2.21", "stream_id": 4100, "primary": True, "rank": 0})
            doc["streams"].append({"stream_id": 4100, "class": "video", "decoded": True})
            return doc
        self.sim.patch["new-dist-b"] = video
        gate = self.gate("new-dist-b")
        self.assertTrue(gate.ok, gate.problems)
        self.assertEqual(gate.evidence["video"], [{"child": "new-edge-1", "stream_id": 4100}])


class Capacity(Base):
    def check(self, cost=5.0):
        lab = self.lab()
        statuses = {name: self.status(name) for name in lab.edges("london")}
        return mesh_ops.capacity_check(lab, "lon-edge-1", statuses, cost)

    def test_the_capacity_gate_counts_legs_and_the_shared_edge(self):
        # X holds 120 legs, every one with its other leg on lon-edge-2: they
        # can go only to lon-edge-3.
        self.sim.nodes_["lon-edge-1"]["legs"] = 120
        self.sim.nodes_["lon-edge-2"]["legs"] = 120
        self.sim.nodes_["lon-edge-3"]["legs"] = 0
        self.sim.patch["lon-edge-1"] = lambda doc: {**doc, "client_udp": {
            **doc["client_udp"], "legs_by_other": {"lon-edge-2": 120}}}
        problems, evidence = self.check()
        self.assertEqual(problems, [])
        self.assertEqual(evidence["room"], {"lon-edge-2": 40.0, "lon-edge-3": 160.0})
        # lon-edge-3 at 140 legs: room 20, and the 120 shared with lon-edge-2
        # have nowhere to go, though the two together have room for 60.
        self.sim.nodes_["lon-edge-3"]["legs"] = 140
        problems, _ = self.check()
        self.assertTrue(any("120 legs of lon-edge-1 have their other leg on lon-edge-2" in item
                            for item in problems), problems)
        self.assertTrue(any("room for 60 legs" in item for item in problems), problems)
        # The cost is the median permille per leg of edges with 10 legs.
        lab = self.lab()
        statuses = {name: self.status(name) for name in lab.edges("london")}
        self.assertEqual(mesh_ops.leg_cost(lab, "london", statuses), 5.0)

    def test_every_other_edge_must_be_ready(self):
        self.sim.nodes_["lon-edge-3"]["drained"] = True
        problems, _ = self.check()
        self.assertTrue(any("lon-edge-3 is not ready" in item for item in problems))

    def test_a_missing_leg_count_refuses(self):
        self.sim.patch["lon-edge-1"] = lambda doc: {**doc, "client_udp": {
            key: value for key, value in doc["client_udp"].items() if key != "legs_by_other"}}
        problems, _ = self.check()
        self.assertIn("capacity unknown: lon-edge-1's client_udp.legs_by_other", problems)
        self.sim.patch["lon-edge-2"] = lambda doc: {**doc, "client_udp": {
            key: value for key, value in doc["client_udp"].items() if key != "single_legs"}}
        self.sim.patch.pop("lon-edge-1")
        problems, _ = self.check()
        self.assertIn("capacity unknown: client_udp.single_legs in london", problems)

    def test_a_light_region_passes_without_a_cost(self):
        for index in (1, 2, 3):
            self.sim.nodes_[f"lon-edge-{index}"]["legs"] = 4
        problems, evidence = self.check(cost=None)
        self.assertEqual(problems, [])
        self.assertIsNone(evidence["cost"])
        self.sim.nodes_["lon-edge-1"]["legs"] = 12
        problems, _ = self.check(cost=None)
        self.assertTrue(any("no edge gives a cost" in item for item in problems))


class Readiness(Base):
    def test_r1_to_r6_name_their_checks_and_causes(self):
        lab = self.lab()
        self.sim.stage(["new-dist-b"], {"git": NEW[0], "unix": NEW[1]})
        activation = int(self.clock() * 1e6)
        self.clock.sleep(0.5)
        self.sim.restart("new-dist-b", NEW)
        expected = self.sim.expected("new-dist-b")
        result = mesh_ops.wait_ready(self.sim, "new-dist-b", expected=expected,
                                     before_boot=0, activation_us=activation, lab=lab,
                                     restarts_before=0, timeout_s=10, clock=self.clock,
                                     sleep=self.clock.sleep)
        self.assertTrue(result.ok, result.problems)
        # The old build answering: R1, the build's.
        result = mesh_ops.wait_ready(self.sim, "new-dist-b",
                                     expected={"git": "cccccccccccc", "unix": 3,
                                               "args_sha256": "x"},
                                     before_boot=0, activation_us=activation, lab=lab,
                                     restarts_before=0, timeout_s=1, clock=self.clock,
                                     sleep=self.clock.sleep)
        self.assertEqual(result.cause, "build")
        self.assertTrue(any(item.startswith("R1: build") for item in result.problems))
        # A unit that restarted by itself: R1.
        self.sim.nodes_["new-dist-b"]["restarts"] = 1
        result = mesh_ops.wait_ready(self.sim, "new-dist-b", expected=expected, before_boot=0,
                                     activation_us=activation, lab=lab, restarts_before=0,
                                     timeout_s=1, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(result.cause, "build")
        self.assertIn("R1: the unit restarted 1 times", result.problems)
        # A cold edge: R6, not the build's.
        self.sim.broken[("lon-edge-1", NEW[0])] = "cold"
        self.sim.stage(["lon-edge-1"], {"git": NEW[0], "unix": NEW[1]})
        self.sim.restart("lon-edge-1", NEW)
        result = mesh_ops.wait_ready(self.sim, "lon-edge-1",
                                     expected=self.sim.expected("lon-edge-1"),
                                     before_boot=0, activation_us=activation, lab=lab,
                                     restarts_before=0, timeout_s=3, clock=self.clock,
                                     sleep=self.clock.sleep)
        self.assertEqual(result.cause, "other")
        self.assertTrue(all(item.startswith("R6") for item in result.problems), result.problems)


# --------------------------------------------------------------------- rolls

class Roll(Base):
    def test_a_roll_restarts_one_node_at_a_time_in_tier_order(self):
        calls = []
        with mock.patch.object(mesh_ops, "contributor_rules", fake_rules(calls)):
            code = self.roll("all")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual([name for name, _ in self.sim.activations()], [
            "lon-edge-1", "lon-edge-2", "lon-edge-3", "new-edge-1", "new-edge-2", "new-edge-3",
            "new-dist-b", "new-dist-a", "lon-dist-b", "lon-dist-a",
            "contrib-lon-b", "contrib-lon"])
        self.assertEqual(calls, [("contrib_gate", "contrib-lon-b"),
                                 ("contrib_ready", "contrib-lon-b"),
                                 ("contrib_gate", "contrib-lon"), ("contrib_ready", "contrib-lon")])
        for name in ("lon-edge-1", "new-dist-a", "contrib-lon"):
            self.assertEqual(self.sim.nodes_[name]["build"], NEW)
            self.assertFalse(self.sim.nodes_[name]["drained"])
        # One node at a time: each edge is drained, activated and resumed
        # before the next one is drained.
        edge_calls = [(call[0], call[1]) for call in self.sim.calls
                      if call[0] in ("post", "activate") and call[1].startswith("lon-edge")]
        self.assertEqual(edge_calls, [("post", "lon-edge-1"), ("activate", "lon-edge-1"),
                                      ("post", "lon-edge-1"), ("post", "lon-edge-2"),
                                      ("activate", "lon-edge-2"), ("post", "lon-edge-2"),
                                      ("post", "lon-edge-3"), ("activate", "lon-edge-3"),
                                      ("post", "lon-edge-3")])
        # Staging went out to each tier's nodes before the first activation.
        stages = [call for call in self.sim.calls if call[0] == "stage"]
        self.assertEqual(len(stages), 3)
        self.assertLess(self.sim.calls.index(stages[-1]),
                        self.sim.calls.index(("activate", "lon-edge-1", False)))
        journal = self.journals()[-1]
        self.assertTrue(journal["finished"])
        self.assertEqual(journal["exit"], 0)
        self.assertIsNone(journal["in_hand"])

    def test_the_contributor_tier_needs_workstream_a_s_module(self):
        code = self.roll("contributors")
        self.assertEqual(code, 2)
        self.assertIn("scripts/contributor_changes.py", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.sim.activations(), [])
        self.assertEqual(mesh_ops.restart_nodes(self.sim, ["contrib-lon"], out=self.out,
                                                err=self.err, clock=self.clock,
                                                sleep=self.clock.sleep), 2)

    def test_a_failed_gate_stops_before_the_node(self):
        self.sim.row[("new-edge-2", "new-dist-a")] = {"slack_mean_us_10s": 989_000}
        code = self.roll("forwarders", options=self.options(gate_timeout_s=3))
        self.assertEqual(code, 3)
        self.assertEqual(self.sim.activations(), [])
        self.assertIn("P3 on time", self.err.getvalue())
        self.assertIn("989000", self.err.getvalue())
        self.assertEqual([line.split()[0] for line in self.stdout_marks()], ["start", "end"])
        self.assertTrue(self.journals()[-1]["finished"])

    def test_a_node_that_fails_r1_is_rolled_back_through_the_gate(self):
        self.sim.broken[("new-dist-b", NEW[0])] = "threads"
        code = self.roll("forwarders", options=self.options(ready_timeout_s=5))
        self.assertEqual(code, 4, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", False), ("new-dist-b", True)])
        self.assertEqual(self.sim.nodes_["new-dist-b"]["build"], OLD)
        self.assertIn("rolled back to aaaaaaaaaaaa", self.err.getvalue())
        self.assertIn("class_threads 1/2", self.err.getvalue())
        evidence = json.loads(next(path for path in self.sim.evidence_dir.glob("roll-*.json")
                                   if not path.name.endswith(".state.json")).read_text())
        self.assertEqual(evidence["nodes"]["new-dist-b"]["result"], "rolled-back")

    def test_a_forwarder_that_keeps_its_leases_and_feeds_nothing_is_rolled_back(self):
        # upgrade-bad-forwarder: R4 fails, the rollback's gate passes (X
        # feeds no pair, so none is X's to judge) and the rollback is made.
        self.sim.broken[("new-dist-b", NEW[0])] = "mute"
        code = self.roll("forwarders", options=self.options(ready_timeout_s=5, gate_timeout_s=3))
        self.assertEqual(code, 4, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", False), ("new-dist-b", True)])
        self.assertEqual(self.sim.nodes_["new-dist-b"]["build"], OLD)
        self.assertIn("is not fed 3001 by new-dist-b", self.err.getvalue())

    def test_an_nrestarts_that_cannot_be_read_is_not_the_build_s(self):
        self.sim.unit_restarts = lambda name: None
        code = self.roll("forwarders", nodes=["new-dist-b"], options=self.options(ready_timeout_s=3))
        self.assertEqual(code, 3, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", False)])
        self.assertIn("unit: parity unknown: the unit's NRestarts", self.err.getvalue())

    def test_a_rollback_the_gate_refuses_is_not_made(self):
        self.sim.broken[("new-dist-b", NEW[0])] = "threads"
        self.sim.hooks[("activate", "new-dist-b")] = lambda: self.sim.row.update(
            {("new-edge-1", "new-dist-a"): {"skipped_10s": 4}})
        code = self.roll("forwarders", options=self.options(ready_timeout_s=5, gate_timeout_s=3))
        self.assertEqual(code, 3)
        self.assertEqual(self.sim.activations(), [("new-dist-b", False)])
        self.assertIn("the rollback was not made", self.err.getvalue())
        self.assertIn("new-dist-b is left as it is", self.err.getvalue())

    def test_a_condition_that_is_not_the_build_s_stops_without_a_rollback(self):
        self.sim.broken[("lon-edge-1", NEW[0])] = "cold"
        code = self.roll("edges", options=self.options(ready_timeout_s=5))
        self.assertEqual(code, 3)
        self.assertEqual(self.sim.activations(), [("lon-edge-1", False)])
        self.assertTrue(self.sim.nodes_["lon-edge-1"]["drained"])
        self.assertIn("not for the build's reasons", self.err.getvalue())
        self.assertIn("stays drained", self.err.getvalue())
        journal = self.journals()[-1]
        self.assertFalse(journal["finished"])
        self.assertEqual(journal["in_hand"]["node"], "lon-edge-1")
        unfinished = mesh_ops.Journal.unfinished(self.sim.evidence_dir)
        self.assertEqual(unfinished[0].data["in_hand"]["node"], "lon-edge-1")

    def test_rollback_all_goes_in_reverse(self):
        self.sim.broken[("lon-edge-3", NEW[0])] = "threads"
        code = self.roll("edges", sites=["london"],
                         options=self.options(on_failure="rollback-all", ready_timeout_s=5))
        self.assertEqual(code, 4, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [
            ("lon-edge-1", False), ("lon-edge-2", False), ("lon-edge-3", False),
            ("lon-edge-3", True), ("lon-edge-2", True), ("lon-edge-1", True)])
        for index in (1, 2, 3):
            self.assertEqual(self.sim.nodes_[f"lon-edge-{index}"]["build"], OLD)
            self.assertFalse(self.sim.nodes_[f"lon-edge-{index}"]["drained"])

    def test_a_node_that_answers_on_neither_build_is_exit_5(self):
        self.sim.broken[("lon-edge-1", NEW[0])] = "silent"
        self.sim.hooks[("activate", "lon-edge-1")] = lambda: self.sim.broken.update(
            {("lon-edge-1", OLD[0]): "silent"})
        code = self.roll("edges", options=self.options(ready_timeout_s=3))
        self.assertEqual(code, 5, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("lon-edge-1", False), ("lon-edge-1", True)])
        self.assertIn("answers /api/node on neither build", self.err.getvalue())

    def test_an_activation_error_reads_the_boot_id_before_anything(self):
        # The ssh dropped after the script restarted the node: one activation.
        self.sim.activation_error["new-dist-b"] = "after"
        self.assertEqual(self.roll("forwarders", sites=["newyork"]), 0, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", False), ("new-dist-a", False)])
        self.assertIn("reading its boot id", self.err.getvalue())
        # The ssh dropped before it ran: the boot id is unchanged after the
        # deadline and the unit's stop bound; gate again and once more.
        self.sim.calls.clear()
        self.sim.stage(["lon-edge-1"], {"git": "cccccccccccc", "unix": 3000})
        self.sim.activation_error["lon-edge-1"] = "before"
        began = self.clock()
        self.assertEqual(self.roll("edges", nodes=["lon-edge-1"], options=self.options(force=True)),
                         0, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("lon-edge-1", False), ("lon-edge-1", False)])
        self.assertGreaterEqual(self.clock() - began, mesh_ops.UNIT_TIMEOUT_STOP_S)

    def test_a_late_activation_is_gated_again_and_tried_once_more(self):
        original = self.sim.activate
        late = {"count": 0}

        def slow(name, not_after_ms, rollback=False):
            if late["count"] < 1:
                late["count"] += 1
                self.clock.sleep(2.0)
            return original(name, not_after_ms, rollback)
        self.sim.activate = slow
        self.assertEqual(self.roll("forwarders", nodes=["new-dist-b"]), 0, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", False), ("new-dist-b", False)])
        self.assertIn("after its deadline", self.err.getvalue())

    def test_sigint_resumes_the_drained_node_and_closes_the_mark(self):
        self.sim.hooks[("post", "lon-edge-2")] = lambda: os.kill(os.getpid(), signal.SIGINT)
        code = self.roll("edges", sites=["london"])
        self.assertEqual(code, 130)
        self.assertEqual([name for name, _ in self.sim.activations()], ["lon-edge-1"])
        self.assertFalse(self.sim.nodes_["lon-edge-2"]["drained"])
        self.assertEqual(self.sim.calls[-1], ("post", "lon-edge-2", "resume-node"))
        self.assertEqual([line.split()[0] for line in self.stdout_marks()], ["start", "end"])
        self.assertIn("lon-edge-2 was drained and not activated; resumed", self.err.getvalue())
        self.assertTrue(self.journals()[-1]["finished"])
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_sigterm_after_the_activation_waits_for_warm_then_resumes(self):
        self.sim.hooks[("activate", "lon-edge-1")] = lambda: os.kill(os.getpid(), signal.SIGTERM)
        code = self.roll("edges", sites=["london"])
        self.assertEqual(code, 130)
        self.assertEqual(self.sim.nodes_["lon-edge-1"]["build"], NEW)
        self.assertFalse(self.sim.nodes_["lon-edge-1"]["drained"])
        self.assertEqual(len(self.stdout_marks()), 2)

    def test_an_interrupted_forwarder_roll_is_left_to_resume(self):
        self.sim.hooks[("activate", "new-dist-b")] = lambda: os.kill(os.getpid(), signal.SIGHUP)
        self.assertEqual(self.roll("forwarders"), 130)
        self.assertIn("its readiness was not seen", self.err.getvalue())
        journal = self.journals()[-1]
        self.assertFalse(journal["finished"])
        self.assertEqual(journal["in_hand"]["node"], "new-dist-b")
        self.clock.sleep(30)
        self.assertEqual(self.roll("forwarders", resume=True), 0, self.err.getvalue())
        self.assertEqual([name for name, _ in self.sim.activations()],
                         ["new-dist-b", "new-dist-a", "lon-dist-b", "lon-dist-a"])

    def test_a_backend_error_closes_the_mark_and_names_the_node(self):
        def fail():
            raise OSError("ssh: no route to host")
        self.sim.hooks[("post", "lon-edge-2")] = fail
        self.assertEqual(self.roll("edges", sites=["london"]), 3)
        self.assertEqual(len(self.stdout_marks()), 2)
        self.assertIn("no route to host", self.err.getvalue())
        self.assertIn("lon-edge-2 is in phase drain", self.err.getvalue())
        self.assertFalse(self.journals()[-1]["finished"])

    def test_resume_finishes_a_killed_roll(self):
        class Killed(BaseException):
            pass

        def kill():
            raise Killed()
        self.sim.hooks[("activate", "lon-edge-2")] = kill
        with self.assertRaises(Killed):
            self.roll("edges", sites=["london"])
        self.assertTrue(self.sim.nodes_["lon-edge-2"]["drained"])
        hand = mesh_ops.Journal.unfinished(self.sim.evidence_dir, "roll")[0].data["in_hand"]
        self.assertEqual((hand["node"], hand["phase"]), ("lon-edge-2", "activating"))
        self.sim.calls.clear()
        self.out = io.StringIO()
        code = self.roll("edges", sites=["london"], resume=True)
        self.assertEqual(code, 0, self.err.getvalue())
        # lon-edge-2 finished (readiness, resume), lon-edge-3 rolled,
        # lon-edge-1 skipped: it is on the target build.
        self.assertEqual(self.sim.activations(), [("lon-edge-3", False)])
        self.assertFalse(self.sim.nodes_["lon-edge-2"]["drained"])
        self.assertIn("resume: finishing lon-edge-2", self.err.getvalue())
        self.assertEqual(len(self.stdout_marks()), 2)
        self.assertEqual(mesh_ops.Journal.unfinished(self.sim.evidence_dir, "roll"), [])

    def test_resume_undoes_a_drain_that_was_never_activated(self):
        class Killed(BaseException):
            pass

        def kill():
            raise Killed()
        self.sim.hooks[("post", "lon-edge-1")] = kill
        with self.assertRaises(Killed):
            self.roll("edges", sites=["london"])
        self.assertTrue(self.sim.nodes_["lon-edge-1"]["drained"])
        self.assertEqual(self.roll("edges", sites=["london"], resume=True), 0, self.err.getvalue())
        self.assertEqual([name for name, _ in self.sim.activations()],
                         ["lon-edge-1", "lon-edge-2", "lon-edge-3"])
        self.assertIn("was drained and not activated; resumed", self.err.getvalue())
        self.assertEqual(self.roll("edges", resume=True), 2)

    def test_dry_marks_restart_nothing(self):
        polls = []
        original = self.sim.statuses
        self.sim.statuses = lambda names: polls.append(tuple(names)) or original(names)
        began = self.clock()
        code = self.roll("forwarders", options=self.options(dry=True))
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.roll("edges", options=self.options(dry=True)), 0)
        self.assertEqual([call for call in self.sim.calls if call[0] != "check"], [])
        self.assertGreater(len(polls), 8)
        self.assertEqual(len(self.stdout_marks()), 4)
        # The typical times: 2 s and 6 s a forwarder, 4, 2 and 5 s an edge.
        self.assertGreaterEqual(self.clock() - began, 4 * 8 + 6 * 11)
        self.assertIn("dry: gates only", self.err.getvalue())

    def test_dry_marks_take_the_last_real_roll_s_times(self):
        self.assertEqual(self.roll("forwarders", sites=["newyork"]), 0)
        times = mesh_ops.typical_times(self.sim.evidence_dir, "forwarders")
        self.assertNotEqual(times, mesh_ops.DRY_TYPICAL_S["forwarders"])
        self.assertEqual(set(times), {"activate", "ready"})
        self.assertEqual(mesh_ops.typical_times(self.tmp / "none", "edges"),
                         mesh_ops.DRY_TYPICAL_S["edges"])

    def test_stdout_is_two_marks_and_node_marks_go_to_their_file(self):
        marks = self.tmp / "marks.txt"
        node_marks = self.tmp / "node-marks.txt"
        code = self.roll("edges", sites=["london"],
                         options=self.options(marks_file=str(marks),
                                              node_marks_file=str(node_marks)))
        self.assertEqual(code, 0, self.err.getvalue())
        lines = self.stdout_marks()
        self.assertEqual([(line.split()[0], line.split()[2]) for line in lines],
                         [("start", "roll.edges"), ("end", "roll.edges")])
        self.assertEqual(marks.read_text().splitlines(), lines)
        node_lines = node_marks.read_text().splitlines()
        self.assertEqual([(line.split()[0], line.split()[2]) for line in node_lines], [
            ("start", "roll.edges.lon-edge-1"), ("end", "roll.edges.lon-edge-1"),
            ("start", "roll.edges.lon-edge-2"), ("end", "roll.edges.lon-edge-2"),
            ("start", "roll.edges.lon-edge-3"), ("end", "roll.edges.lon-edge-3")])
        for line in node_lines:
            self.assertRegex(line, MARK)
            self.assertIn(f"mark {line}", self.err.getvalue())
        start, end = (float(line.split()[1]) for line in lines)
        self.assertLessEqual(start, float(node_lines[0].split()[1]))
        self.assertGreaterEqual(end, float(node_lines[-1].split()[1]))

    def test_a_node_on_the_target_build_is_skipped(self):
        self.sim.restart("lon-edge-2", NEW)
        self.clock.sleep(30)
        self.assertEqual(self.roll("edges", sites=["london"]), 0, self.err.getvalue())
        self.assertEqual([name for name, _ in self.sim.activations()], ["lon-edge-1", "lon-edge-3"])
        self.assertIn("lon-edge-2 skipped", self.err.getvalue())
        self.sim.calls.clear()
        self.assertEqual(self.roll("edges", sites=["london"], options=self.options(force=True)), 0)
        self.assertEqual([name for name, _ in self.sim.activations()],
                         ["lon-edge-1", "lon-edge-2", "lon-edge-3"])

    def test_the_clock_check_refuses_over_50_ms(self):
        self.sim.offsets["new-dist-a"] = 0.051
        self.assertEqual(self.roll("edges"), 2)
        self.assertIn("51 ms", self.err.getvalue())
        self.assertEqual(self.sim.changing(), [])
        self.assertEqual(self.out.getvalue(), "")
        self.sim.offsets["new-dist-a"] = -0.049
        self.assertEqual(self.roll("edges", sites=["london"]), 0, self.err.getvalue())

    def test_a_node_that_is_not_ready_refuses_the_roll(self):
        self.sim.nodes_["new-edge-3"]["drained"] = True
        self.assertEqual(self.roll("edges"), 2)
        self.assertIn("new-edge-3 (draining)", self.err.getvalue())
        self.assertEqual(self.sim.changing(), [])

    def test_an_interrupt_before_a_forwarder_s_rollback_names_it_and_resume_makes_it(self):
        self.sim.broken[("new-dist-b", NEW[0])] = "threads"
        original, calls = mesh_ops.forwarder_gate, []

        def gate(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 2:
                os.kill(os.getpid(), signal.SIGINT)
            return original(*args, **kwargs)
        with mock.patch.object(mesh_ops, "forwarder_gate", gate):
            code = self.roll("forwarders", options=self.options(ready_timeout_s=5))
        self.assertEqual(code, 130, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", False)])
        self.assertIn("new-dist-b runs the build that failed; it was not rolled back (roll "
                      "forwarders --resume makes the rollback)", self.err.getvalue())
        hand = self.unfinished("roll")[0].data["in_hand"]
        self.assertEqual((hand["node"], hand["phase"]), ("new-dist-b", "rollback-gate"))
        self.clock.sleep(30)
        self.assertEqual(self.roll("forwarders", resume=True), 4, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", False), ("new-dist-b", True)])
        self.assertEqual(self.sim.nodes_["new-dist-b"]["build"], OLD)
        self.assertEqual(self.unfinished(), [])

    def test_rollback_all_keeps_the_node_that_failed_in_the_journal(self):
        self.sim.broken[("lon-edge-2", NEW[0])] = "cold"
        code = self.roll("edges", sites=["london"],
                         options=self.options(on_failure="rollback-all", ready_timeout_s=5,
                                              gate_timeout_s=3))
        # lon-edge-2 is not warm (not the build's: no rollback) and stays
        # drained; rolling lon-edge-1 back is refused because lon-edge-2 is
        # not ready.
        self.assertEqual(code, 3, self.err.getvalue())
        self.assertTrue(self.sim.nodes_["lon-edge-2"]["drained"])
        journal = self.unfinished("roll")[0].data
        self.assertFalse(journal["finished"])
        self.assertEqual(journal["left"]["lon-edge-2"]["phase"], "activated")
        self.assertIn("left: lon-edge-2 is in phase activated and stays drained",
                      self.err.getvalue())
        # The operator's rollback of that one node works on it although it is
        # not ready, and closes the roll's journal.
        self.sim.calls.clear()
        self.assertEqual(mesh_ops.restart_nodes(
            self.sim, ["lon-edge-2"], options=self.options(rollback=True), out=self.out,
            err=self.err, clock=self.clock, sleep=self.clock.sleep), 0, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("lon-edge-2", True)])
        self.assertEqual(self.sim.nodes_["lon-edge-2"]["build"], OLD)
        self.assertFalse(self.sim.nodes_["lon-edge-2"]["drained"])
        self.assertEqual(self.unfinished(), [])

    def test_a_refusal_after_the_change_began_is_exit_3(self):
        self.sim.nodes_["new-edge-3"].update(serving=False, running=False, standby=True, legs=0)
        self.sim.fail[("start", "new-edge-3")] = mesh_ops.Refused("budget: 61.2 > 60")
        code = self.roll("edges")
        self.assertEqual(code, 3, self.err.getvalue())
        self.assertEqual([name for name, _ in self.sim.activations()],
                         ["lon-edge-1", "lon-edge-2", "lon-edge-3"])
        self.assertIn("the surge edge new-edge-3 was not started (budget: 61.2 > 60)",
                      self.err.getvalue())
        journal = self.journals()[-1]
        self.assertTrue(journal["finished"])
        self.assertEqual((journal["exit"], journal["surge"]), (3, {}))
        # Any other Refused once the change began: exit 3, not 2.
        self.sim.calls.clear()
        self.sim.hooks[("post", "lon-edge-2")] = lambda: (_ for _ in ()).throw(
            mesh_ops.Refused("a rule"))
        self.assertEqual(self.roll("edges", sites=["london"],
                                   options=self.options(force=True)), 3)
        self.assertIn("refused after the change began: a rule; lon-edge-2 is in phase drain",
                      self.err.getvalue())

    def test_two_rolls_in_a_row_through_the_surge_edge(self):
        self.sim.nodes_["new-edge-3"].update(serving=False, running=False, standby=True, legs=0)
        self.assertEqual(self.roll("edges", sites=["newyork"]), 0, self.err.getvalue())
        # It was staged and started on the target build, and its leave
        # drained it (the drain survives its stop).
        self.assertEqual(self.sim.nodes_["new-edge-3"]["build"], NEW)
        self.assertTrue(self.sim.nodes_["new-edge-3"]["drained"])
        self.assertIn(("stage", ("new-edge-3",)), self.sim.calls)
        self.clock.sleep(30)
        self.sim.calls.clear()
        self.assertEqual(self.roll("edges", sites=["newyork"], options=self.options(force=True)),
                         0, self.err.getvalue())
        posts = [call[1:] for call in self.sim.calls if call[0] == "post"]
        self.assertEqual(posts[0], ("new-edge-3", "resume-node"))
        self.assertTrue(self.sim.nodes_["new-edge-3"]["standby"])

    def test_the_surge_edge_leaves_after_the_single_legs_wait_and_its_gate(self):
        self.sim.nodes_["new-edge-3"].update(serving=False, running=False, standby=True, legs=0)
        seen = {}
        original = self.sim.post

        def post(name, path, body):
            code = original(name, path, body)
            if path.endswith("resume-node") and name == "new-edge-2":
                seen["resumed"] = self.clock()
                self.sim.nodes_["new-edge-1"]["single"] = 30
            if path.endswith("drain-node") and name == "new-edge-3":
                seen["leave"] = self.clock()
            return code
        self.sim.post = post
        original_doc = self.sim.doc

        def doc(name):
            if seen.get("resumed") and self.clock() - seen["resumed"] > 6:
                self.sim.nodes_["new-edge-1"]["single"] = 0
            return original_doc(name)
        self.sim.doc = doc
        self.assertEqual(self.roll("edges", sites=["newyork"]), 0, self.err.getvalue())
        self.assertGreater(seen["leave"] - seen["resumed"], 6)
        # Its gate refuses (another edge is not ready): it is left serving.
        self.sim.calls.clear()
        self.sim.doc = original_doc
        self.sim.hooks[("post", "new-edge-2")] = lambda: self.sim.hooks.update(
            {("post", "new-edge-2"): lambda: self.sim.broken.update(
                {("new-edge-1", NEW[0]): "threads"})})
        self.clock.sleep(30)
        self.assertEqual(self.roll("edges", sites=["newyork"],
                                   options=self.options(force=True, gate_timeout_s=2)), 3)
        self.assertIn("the surge edge new-edge-3 may not leave newyork", self.err.getvalue())
        self.assertNotIn(("post", "new-edge-3", "drain-node"), self.sim.calls)
        self.assertTrue(self.sim.nodes_["new-edge-3"]["running"])

    def test_an_edge_refused_as_late_twice_is_resumed(self):
        original = self.sim.activate

        def ahead(name, not_after_ms, rollback=False):
            self.clock.sleep(2.0)  # the node's clock runs ahead: always late
            return original(name, not_after_ms, rollback)
        self.sim.activate = ahead
        self.assertEqual(self.roll("edges", sites=["london"]), 3)
        self.assertIn("lon-edge-1 was not activated twice (late); it runs as before and was "
                      "resumed", self.err.getvalue())
        self.assertFalse(self.sim.nodes_["lon-edge-1"]["drained"])
        self.assertEqual(self.sim.nodes_["lon-edge-1"]["build"], OLD)
        self.assertTrue(self.journals()[-1]["finished"])

    def test_an_interrupt_resumes_an_edge_only_with_its_new_arguments_seen(self):
        def wrong_args():
            self.sim.patch["lon-edge-1"] = lambda doc: {
                **doc, "process": {**doc["process"], "args_sha256": "args-other"}}
            os.kill(os.getpid(), signal.SIGTERM)
        self.sim.hooks[("activate", "lon-edge-1")] = wrong_args
        self.assertEqual(self.roll("edges", sites=["london"]), 130)
        self.assertTrue(self.sim.nodes_["lon-edge-1"]["drained"])
        self.assertIn("args_sha256 differs", self.err.getvalue())
        self.assertEqual(self.unfinished("roll")[0].data["in_hand"]["node"], "lon-edge-1")

    def test_an_edge_that_will_not_empty_for_its_rollback_stays_drained(self):
        self.sim.broken[("lon-edge-1", NEW[0])] = "threads"
        # The failed build's client_udp has no gauges: it is never "empty".
        self.sim.hooks[("activate", "lon-edge-1")] = lambda: self.sim.patch.update(
            {"lon-edge-1": lambda doc: {**doc, "client_udp": {}}})
        code = self.roll("edges", sites=["london"],
                         options=self.options(ready_timeout_s=3, drain_timeout_s=3))
        self.assertEqual(code, 3, self.err.getvalue())
        self.assertIn("lon-edge-1 did not empty in 3 s", self.err.getvalue())
        self.assertIn("it stays drained", self.err.getvalue())
        self.assertNotIn(("post", "lon-edge-1", "resume-node"), self.sim.calls)
        self.assertTrue(self.sim.nodes_["lon-edge-1"]["drained"])
        self.assertEqual(self.unfinished("roll")[0].data["in_hand"]["phase"], "rollback-gate")

    def test_a_new_roll_closes_an_older_journal_whose_node_is_ready_again(self):
        class Killed(BaseException):
            pass

        def kill():
            raise Killed()
        self.sim.hooks[("post", "lon-edge-1")] = kill
        with self.assertRaises(Killed):
            self.roll("edges", sites=["london"])
        # The operator resumed lon-edge-1 by hand; a plain roll runs.
        self.sim.nodes_["lon-edge-1"]["drained"] = False
        self.clock.sleep(5)
        self.assertEqual(self.roll("edges", sites=["london"]), 0, self.err.getvalue())
        self.assertIn("lon-edge-1 ready now", self.err.getvalue())
        self.assertEqual(self.roll("edges", resume=True), 2)
        self.assertIn("no unfinished roll", self.err.getvalue())

    def test_a_contributor_with_video_refuses_unless_accepted(self):
        def video(doc):
            doc["streams"].append({"stream_id": 4100, "class": "video", "publishing": True})
            return doc
        self.sim.patch["contrib-lon"] = video
        with mock.patch.object(mesh_ops, "contributor_rules", fake_rules()):
            self.assertEqual(self.roll("contributors"), 2)
            self.assertIn("--video-gap accept", self.err.getvalue())
            self.assertEqual(self.roll("contributors", options=self.options(video_gap="accept")), 0)

    def test_a_contributor_that_fails_its_readiness_is_rolled_back(self):
        with mock.patch.object(mesh_ops, "contributor_rules", fake_rules(ready_ok=False)):
            self.assertEqual(self.roll("contributors", options=self.options(ready_timeout_s=3)), 4)
        self.assertEqual(self.sim.activations(),
                         [("contrib-lon-b", False), ("contrib-lon-b", True)])
        with mock.patch.object(mesh_ops, "contributor_rules", fake_rules(gate_ok=False)):
            self.sim.calls.clear()
            self.assertEqual(self.roll("contributors"), 3)
        self.assertEqual(self.sim.activations(), [])

    def test_the_ops_lock_refuses_a_second_command_and_admits_its_holder_s(self):
        lock = mesh_ops.OpsLock(self.sim.lock_path).acquire()
        try:
            self.assertEqual(self.roll("edges"), 2)
            self.assertIn("ops.lock", self.err.getvalue())
            self.assertEqual(self.sim.calls, [])
            with mock.patch.dict(os.environ, lock.env()):
                self.assertEqual(self.roll("edges", sites=["london"]), 0, self.err.getvalue())
            with mock.patch.dict(os.environ, {mesh_ops.OpsLock.ENV: "not-the-token"}):
                self.assertEqual(self.roll("edges"), 2)
        finally:
            lock.release()
        self.assertEqual(self.sim.lock_path.read_text(), "")
        self.assertEqual(self.roll("edges", sites=["london"]), 0)

    def test_a_surge_edge_joins_before_the_region_and_leaves_after(self):
        self.sim.nodes_["new-edge-3"].update(serving=False, running=False, standby=True, legs=0)
        node_marks = self.tmp / "node-marks.txt"
        code = self.roll("edges", sites=["newyork"],
                         options=self.options(node_marks_file=str(node_marks)))
        self.assertEqual(code, 0, self.err.getvalue())
        changes = [call[:2] for call in self.sim.calls if call[0] in ("start", "stop", "activate")]
        self.assertEqual(changes, [("start", "new-edge-3"), ("activate", "new-edge-1"),
                                   ("activate", "new-edge-2"), ("stop", "new-edge-3")])
        labels = [line.split()[2] for line in node_marks.read_text().splitlines()]
        self.assertEqual(labels[:2], ["roll.edges.join.new-edge-3"] * 2)
        self.assertEqual(labels[-2:], ["roll.edges.leave.new-edge-3"] * 2)
        self.assertTrue(self.sim.nodes_["new-edge-3"]["standby"])

    def test_without_a_surge_candidate_the_roll_refuses_and_names_surge_never(self):
        for name in ("new-edge-3", "new-edge-4"):
            self.sim.nodes_[name].update(serving=False, running=False, standby=False, legs=0)
        self.assertEqual(self.roll("edges", sites=["newyork"]), 2)
        self.assertIn("--surge never", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.roll("edges", sites=["newyork"],
                                   options=self.options(surge="never")), 0, self.err.getvalue())

    def test_a_failed_roll_leaves_the_surge_edge_serving_and_names_it(self):
        self.sim.nodes_["new-edge-3"].update(serving=False, running=False, standby=True, legs=0)
        self.sim.broken[("new-edge-2", NEW[0])] = "cold"
        self.assertEqual(self.roll("edges", sites=["newyork"],
                                   options=self.options(ready_timeout_s=3)), 3)
        self.assertTrue(self.sim.nodes_["new-edge-3"]["running"])
        self.assertIn("the surge edge new-edge-3 of newyork is left serving", self.err.getvalue())

    def test_the_next_drain_waits_for_the_region_s_single_legs(self):
        resumed = {}
        original = self.sim.post

        def post(name, path, body):
            code = original(name, path, body)
            if path.endswith("resume-node") and name == "lon-edge-1":
                resumed["at"] = self.clock()
                self.sim.nodes_["lon-edge-2"]["single"] = 30
            if path.endswith("drain-node") and name == "lon-edge-2":
                resumed["next"] = self.clock()
            return code
        self.sim.post = post
        original_doc = self.sim.doc

        def doc(name):
            if resumed.get("at") and self.clock() - resumed["at"] > 6:
                self.sim.nodes_["lon-edge-2"]["single"] = 0
            return original_doc(name)
        self.sim.doc = doc
        self.assertEqual(self.roll("edges", sites=["london"]), 0, self.err.getvalue())
        self.assertGreater(resumed["next"] - resumed["at"], 6)


class Restart(Base):
    def restart(self, names, **fields):
        return mesh_ops.restart_nodes(self.sim, names, options=self.options(**fields), out=self.out,
                                      err=self.err, clock=self.clock, sleep=self.clock.sleep)

    def test_restart_gates_each_node_and_keeps_its_build(self):
        self.assertEqual(self.restart(["new-dist-a", "lon-edge-2"]), 0, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-a", False), ("lon-edge-2", False)])
        self.assertEqual(self.sim.nodes_["new-dist-a"]["build"], OLD)
        self.assertEqual(self.stdout_marks()[0].split()[2], "restart.new-dist-a,lon-edge-2")

    def test_hold_keeps_the_node_stopped_that_long(self):
        stopped = {}
        original = self.sim.stop_node
        self.sim.stop_node = lambda name, **bound: (stopped.setdefault(name, self.clock()),
                                                    original(name, **bound))
        original_activate = self.sim.activate

        def activate(name, not_after_ms, rollback=False):
            stopped["activated"] = self.clock()
            return original_activate(name, not_after_ms, rollback)
        self.sim.activate = activate
        self.assertEqual(self.restart(["new-dist-b"], hold_s=20), 0, self.err.getvalue())
        self.assertGreaterEqual(stopped["activated"] - stopped["new-dist-b"], 20)

    def test_hold_with_a_late_first_activation_tries_the_activation_alone(self):
        original = self.sim.activate
        late = {"count": 0}

        def slow(name, not_after_ms, rollback=False):
            if late["count"] < 1:
                late["count"] += 1
                self.clock.sleep(2.0)
            return original(name, not_after_ms, rollback)
        self.sim.activate = slow
        gates = []
        original_gate = mesh_ops.forwarder_gate
        with mock.patch.object(mesh_ops, "forwarder_gate",
                               lambda *a, **k: gates.append(a[1]) or original_gate(*a, **k)):
            self.assertEqual(self.restart(["new-dist-b"], hold_s=5), 0, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", False), ("new-dist-b", False)])
        self.assertEqual(gates, ["new-dist-b"])

    def test_the_hold_s_stop_is_bound_by_its_gate(self):
        original = self.sim.stop_node

        def slow(name, not_after_ms=None):
            self.clock.sleep(2.0)  # ssh slow: the stop lands after its deadline
            return original(name, not_after_ms=not_after_ms)
        self.sim.stop_node = slow
        self.assertEqual(self.restart(["lon-edge-2"], hold_s=5), 3)
        self.assertIn("the hold's stop was refused (late); it runs as before; resumed",
                      self.err.getvalue())
        self.assertTrue(self.sim.nodes_["lon-edge-2"]["running"])
        self.assertFalse(self.sim.nodes_["lon-edge-2"]["drained"])
        self.assertEqual(self.sim.activations(), [])

    def test_an_interrupt_while_held_starts_the_node_and_resume_finishes_it(self):
        self.interrupt_after("stop_node", "new-dist-b")
        self.assertEqual(self.restart(["new-dist-b", "new-dist-a"], hold_s=20), 130)
        self.assertTrue(self.sim.nodes_["new-dist-b"]["running"])
        self.assertIn("new-dist-b was held stopped and is started again", self.err.getvalue())
        self.assertIn("restart --nodes new-dist-b,new-dist-a --resume", self.err.getvalue())
        hand = self.unfinished("restart")[0].data["in_hand"]
        self.assertEqual((hand["node"], hand["phase"]), ("new-dist-b", "activated"))
        self.sim.calls.clear()
        self.clock.sleep(30)
        self.assertEqual(mesh_ops.restart_nodes(
            self.sim, ["new-dist-b", "new-dist-a"], options=self.options(hold_s=1), resume=True,
            out=self.out, err=self.err, clock=self.clock, sleep=self.clock.sleep), 0,
            self.err.getvalue())
        # new-dist-b's readiness is seen, then new-dist-a goes through.
        self.assertEqual(self.sim.activations(), [("new-dist-a", False)])
        self.assertEqual(self.unfinished(), [])

    def test_an_edge_interrupted_while_held_is_started_warmed_and_resumed(self):
        self.interrupt_after("stop_node", "lon-edge-2")
        self.assertEqual(self.restart(["lon-edge-2"], hold_s=20), 130)
        self.assertTrue(self.sim.nodes_["lon-edge-2"]["running"])
        self.assertFalse(self.sim.nodes_["lon-edge-2"]["drained"])
        self.assertIn("lon-edge-2 is warm and resumed", self.err.getvalue())
        self.assertTrue(self.journals()[-1]["finished"])

    def test_a_restart_rollback_that_fails_is_not_rolled_back_again(self):
        self.sim.nodes_["new-dist-b"]["prev"] = ("cccccccccccc", 500)
        self.sim.broken[("new-dist-b", "cccccccccccc")] = "threads"
        self.assertEqual(self.restart(["new-dist-b"], rollback=True, ready_timeout_s=3), 4)
        self.assertEqual(self.sim.activations(), [("new-dist-b", True)])
        self.assertIn("new-dist-b is not ready on its previous arguments either",
                      self.err.getvalue())
        self.assertNotIn("rolled back to", self.err.getvalue())

    def test_restart_of_an_edge_brings_the_surge_edge(self):
        self.sim.nodes_["new-edge-3"].update(serving=False, running=False, standby=True, legs=0)
        self.assertEqual(self.restart(["new-edge-1"]), 0, self.err.getvalue())
        self.assertEqual([call[:2] for call in self.sim.calls if call[0] in
                          ("start", "stop", "activate")],
                         [("start", "new-edge-3"), ("activate", "new-edge-1"),
                          ("stop", "new-edge-3")])

    def test_rollback_puts_the_previous_arguments_in_force_without_staging(self):
        self.sim.nodes_["new-dist-b"]["prev"] = ("cccccccccccc", 500)
        self.assertEqual(self.restart(["new-dist-b"], rollback=True), 0, self.err.getvalue())
        self.assertEqual(self.sim.activations(), [("new-dist-b", True)])
        self.assertFalse(any(call[0] == "stage" for call in self.sim.calls))
        self.assertEqual(self.sim.nodes_["new-dist-b"]["build"], ("cccccccccccc", 500))


class Replace(Base):
    def replace(self, name, **kwargs):
        options = self.options(**kwargs.pop("options", {}))
        return mesh_ops.replace(self.sim, name, options=options, out=self.out, err=self.err,
                                clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def test_replace_of_an_edge_restarts_nothing_else(self):
        self.assertEqual(self.replace("lon-edge-2"), 0, self.err.getvalue())
        self.assertEqual([call for call in self.sim.calls if call[0] != "check"], [
            ("post", "lon-edge-2", "drain-node"), ("stop", "lon-edge-2"),
            ("rebuild", "lon-edge-2", "rebuild"), ("push", "lon-edge-2"),
            ("post", "lon-edge-2", "resume-node")])
        self.assertEqual([line.split()[2] for line in self.stdout_marks()],
                         ["replace.lon-edge-2"] * 2)

    def test_replace_of_a_forwarder_goes_through_its_slot(self):
        self.assertEqual(self.replace("lon-dist-b"), 0, self.err.getvalue())
        self.assertEqual([call for call in self.sim.calls if call[0] != "check"], [
            ("add_slot", "lon-dist-c"), ("post", "lon-dist-c", "resume-node"),
            ("post", "lon-dist-b", "drain-node"), ("stop", "lon-dist-b"), ("rebuild", "lon-dist-b", "rebuild"), ("push", "lon-dist-b"),
            ("post", "lon-dist-b", "resume-node"), ("post", "lon-dist-c", "drain-node"),
            ("stop", "lon-dist-c"), ("remove_slot", "lon-dist-c")])
        self.assertFalse(self.sim.nodes_["lon-dist-b"]["drained"])

    def test_a_refused_slot_leaves_the_forwarder_untouched(self):
        self.sim.fail[("add_slot", "lon-dist-c")] = mesh_ops.Refused("budget: 61.2 > 60")
        self.assertEqual(self.replace("lon-dist-b"), 3)
        self.assertEqual([call for call in self.sim.calls if call[0] != "check"],
                         [("add_slot", "lon-dist-c")])
        self.assertIn("lon-dist-b is untouched", self.err.getvalue())

    def test_an_interrupted_forwarder_replace_names_what_it_left_and_resume_finishes_it(self):
        cases = {
            "after the slot's create": ("add_slot", "lon-dist-c", None),
            "after the drain of X": ("post", "lon-dist-b",
                                     lambda args: args[0].endswith("drain-node")),
            "during X's push": ("push", "lon-dist-b", None),
            "after the slot's drain": ("post", "lon-dist-c",
                                       lambda args: args[0].endswith("drain-node")),
        }
        for case, (method, node, when) in cases.items():
            with self.subTest(case):
                self.setUp()
                self.interrupt_after(method, node, when=when)
                self.assertEqual(self.replace("lon-dist-b"), 130, self.err.getvalue())
                journal = self.unfinished("replace")
                self.assertEqual(len(journal), 1, self.err.getvalue())
                self.assertIn("replace --nodes lon-dist-b --resume", self.err.getvalue())
                self.assertTrue(self.sim.nodes_["lon-dist-c"]["running"])
                self.clock.sleep(30)
                self.assertEqual(self.replace("lon-dist-b", resume=True), 0, self.err.getvalue())
                self.assertEqual(self.unfinished(), [])
                self.assertFalse(self.sim.nodes_["lon-dist-b"]["drained"])
                self.assertTrue(self.sim.nodes_["lon-dist-b"]["running"])
                self.assertFalse(self.sim.nodes_["lon-dist-c"]["running"])
                self.assertFalse(self.sim.nodes_["lon-dist-c"]["created"])

    def test_a_slot_that_fails_after_its_create_is_named_and_resume_deletes_it(self):
        self.sim.fail[("add_slot_push", "lon-dist-c")] = OSError("apt: 503")
        self.assertEqual(self.replace("lon-dist-b"), 3)
        self.assertIn("the slot lon-dist-c failed part-way (apt: 503); lon-dist-b is untouched",
                      self.err.getvalue())
        self.assertIn("left: the slot lon-dist-c may exist and bill", self.err.getvalue())
        self.assertEqual(self.unfinished("replace")[0].data["left"]["lon-dist-c"]["phase"],
                         "slot-creating")
        self.sim.calls.clear()
        self.assertEqual(self.replace("lon-dist-b", resume=True), 0, self.err.getvalue())
        changes = [call[:2] for call in self.sim.calls if call[0] in ("add_slot", "remove_slot")]
        self.assertEqual(changes, [("remove_slot", "lon-dist-c"), ("add_slot", "lon-dist-c"),
                                   ("remove_slot", "lon-dist-c")])

    def test_a_kept_slot_is_started_by_the_next_replace(self):
        self.assertEqual(self.replace("lon-dist-b", keep_slot=True), 0, self.err.getvalue())
        self.assertTrue(self.sim.nodes_["lon-dist-c"]["created"])
        self.assertFalse(self.sim.nodes_["lon-dist-c"]["running"])
        self.sim.calls.clear()
        self.clock.sleep(30)
        self.assertEqual(self.replace("lon-dist-a"), 0, self.err.getvalue())
        self.assertEqual([call[:2] for call in self.sim.calls if call[0] in
                          ("add_slot", "start", "remove_slot")],
                         [("start", "lon-dist-c"), ("remove_slot", "lon-dist-c")])

    def test_replace_with_a_new_instance_takes_its_new_addresses(self):
        self.assertEqual(self.replace("lon-dist-a", mode="new"), 0, self.err.getvalue())
        self.assertEqual(self.sim.nodes_["lon-dist-a"]["public"], "192.0.9.11")

    def test_a_failed_push_is_named_and_replace_resume_goes_on(self):
        self.sim.fail[("push", "lon-edge-1")] = OSError("apt: 503")
        self.assertEqual(self.replace("lon-edge-1"), 5)
        self.assertIn("left stopped or half-installed", self.err.getvalue())
        self.assertIn("--resume", self.err.getvalue())
        self.sim.calls.clear()
        self.assertEqual(self.replace("lon-edge-1", resume=True), 0, self.err.getvalue())
        self.assertEqual([call for call in self.sim.calls if call[0] != "check"], [
            ("push", "lon-edge-1"), ("post", "lon-edge-1", "resume-node")])
        self.assertEqual(self.replace("lon-edge-1", resume=True), 2)


# ---------------------------------------------------------------- plumbing

class Plumbing(unittest.TestCase):
    def test_marks_are_unix_seconds_with_nine_decimals(self):
        out, err = io.StringIO(), io.StringIO()
        marks = mesh_ops.Marks(out, err, clock_ns=lambda: 1763924012_123456789)
        marks.start("roll.edges")
        marks.node_start("roll.edges.lon-edge-1")
        marks.close()
        self.assertEqual(out.getvalue().splitlines(), ["start 1763924012.123456789 roll.edges",
                                                       "end 1763924012.123456789 roll.edges"])
        self.assertEqual(err.getvalue().splitlines(),
                         ["mark start 1763924012.123456789 roll.edges.lon-edge-1",
                          "mark end 1763924012.123456789 roll.edges.lon-edge-1"])
        with self.assertRaises(ValueError):
            marks.start("roll edges")

    def test_the_forwarder_order_puts_siblings_apart(self):
        records = []
        for site in ("sydney", "london", "tokyo"):
            short = site[:3]
            for suffix in ("a", "b", "c"):
                records.append({"name": f"{short}-dist-{suffix}", "role": "distributor",
                                "site": site, "serving": suffix != "c" or site == "tokyo",
                                "slot": suffix == "c"})
        records.append({"name": "contrib-lon", "role": "contributor", "site": "london",
                        "serving": True})
        order = mesh_ops.plan_order(mesh_ops.Lab(records), "forwarders")
        self.assertEqual(order, ["syd-dist-b", "tok-dist-c", "tok-dist-b", "syd-dist-a",
                                 "tok-dist-a", "lon-dist-b", "lon-dist-a"])
        with self.assertRaises(mesh_ops.Refused):
            mesh_ops.plan_order(mesh_ops.Lab(records), "forwarders", nodes=["contrib-lon"])

    def test_all_takes_named_nodes_of_every_tier_in_tier_order(self):
        sim = Sim(Clock(), pathlib.Path(tempfile.gettempdir()))
        lab = mesh_ops.Lab(sim.nodes())
        self.assertEqual(mesh_ops.plan_order(lab, "all", nodes=["new-dist-a", "lon-edge-1"]),
                         ["lon-edge-1", "new-dist-a"])
        with self.assertRaises(mesh_ops.Refused):
            mesh_ops.plan_order(lab, "all", nodes=["lon-edge-4"])

    def test_http_keeps_one_connection_and_reconnects_once(self):
        made = []

        class Response:
            status = 200

            def read(self):
                return b'{"ok": true}'

        class Connection:
            def __init__(self, fail):
                self.fail = fail
                self.requests = 0
                self.closed = False

            def request(self, method, path, body=None, headers=None):
                self.requests += 1
                if self.fail:
                    raise ConnectionResetError("reset")

            def getresponse(self):
                return Response()

            def close(self):
                self.closed = True

        plan = [False, True, False]

        def connect(host, port, timeout):
            made.append(Connection(plan.pop(0) if plan else True))
            return made[-1]

        client = mesh_ops.Http(connect=connect)
        self.assertEqual(client.get("10.0.0.1", 19444, "/api/node"), (200, {"ok": True}))
        self.assertEqual(client.get("10.0.0.1", 19444, "/api/node"), (200, {"ok": True}))
        self.assertEqual(len(made), 1)
        made[0].fail = True
        self.assertEqual(client.get("10.0.0.1", 19444, "/api/node"), (None, None))
        self.assertEqual(len(made), 2)
        self.assertTrue(made[0].closed and made[1].closed)
        self.assertEqual(client.get("10.0.0.1", 19444, "/api/node"), (200, {"ok": True}))
        polled = client.poll({"a": ("10.0.0.1", 19444, "/api/node"),
                              "b": ("10.0.0.1", 19444, "/api/node")})
        self.assertEqual(polled, {"a": (200, {"ok": True}), "b": (200, {"ok": True})})

    def test_a_mark_and_its_label_are_never_split_by_a_signal(self):
        seen = []
        previous = signal.signal(signal.SIGHUP, lambda *_: seen.append("handled"))
        try:
            with mesh_ops.signals_held():
                os.kill(os.getpid(), signal.SIGHUP)
                seen.append("inside")
            seen.append("after")
        finally:
            signal.signal(signal.SIGHUP, previous)
        self.assertEqual(seen, ["inside", "handled", "after"])

    def test_a_control_request_must_name_its_node(self):
        with self.assertRaises(mesh_ops.Refused):
            mesh_ops.control_body("")
        self.assertTrue(mesh_ops.control_accepted(202))
        self.assertFalse(mesh_ops.control_accepted(400))
        self.assertEqual(mesh_ops.gauge({"a": {"b": 3}}, "x", "a.b"), 3)
        self.assertIsNone(mesh_ops.gauge({"a": True}, "a"))


if __name__ == "__main__":
    unittest.main()
