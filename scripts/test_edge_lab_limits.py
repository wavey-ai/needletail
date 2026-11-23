#!/usr/bin/env python3
"""Tests of the lab's service limits and of what keeps several labs apart
on one host, offline (no namespace, no service, no systemd): the contributor
has its own limits, and the campaign's restore puts it back on them, not on
a node's; a lab in a CPU range runs nothing outside it, with its readers and
its own commands on the tail of the range and a held slot without CPUs; the
registry that every worktree shares refuses another owner's lab, an id that
runs elsewhere and CPUs that another lab has, and drops a stale entry; two
state directories with one basename are two sets of units; the tools scope.
python3 -m unittest scripts/test_edge_lab_limits.py"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402
import edge_campaign_core as core  # noqa: E402

CONTRIBUTOR = lab.names("contributor")[0]
EDGE = lab.names("edge")[0]


class Limits(unittest.TestCase):
    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp())
        patcher = mock.patch.object(lab, "WORK", self.work)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_contributor_has_its_own_limits(self):
        self.assertEqual(lab.default_limits(CONTRIBUTOR), lab.CONTRIBUTOR_LIMITS)
        self.assertEqual(lab.default_limits(EDGE), lab.NANODE)
        if not lab.os.environ.get("NEEDLETAIL_EDGE_LAB_NANODE"):
            self.assertEqual(lab.default_limits(CONTRIBUTOR), {"cpu_percent": 400, "memory_mb": 8192})
            self.assertEqual(lab.default_limits(EDGE), {"cpu_percent": 200, "memory_mb": 4096})

    def test_a_replaced_nanode_is_followed(self):
        # linode-edge-campaign.py replaces both.
        with mock.patch.object(lab, "NANODE", {"cpu_percent": 100, "memory_mb": 1024}), \
                mock.patch.object(lab, "CONTRIBUTOR_LIMITS", {"cpu_percent": 100, "memory_mb": 1024}):
            self.assertEqual(lab.default_limits(CONTRIBUTOR)["cpu_percent"], 100)
            self.assertEqual(lab.default_limits(EDGE)["memory_mb"], 1024)

    def test_lab_overrides_win(self):
        lab.save("limits.json", {EDGE: {"cpu_percent": 50}})
        self.assertEqual(lab.service_limits(EDGE), {**lab.NANODE, "cpu_percent": 50})
        self.assertEqual(lab.service_limits(CONTRIBUTOR), lab.CONTRIBUTOR_LIMITS)

    def test_restore_gives_the_contributor_its_own_limits(self):
        lab.save("limits.json", {EDGE: {"memory_mb": 512}})
        # The legacy topology: every node runs a service.
        lab.save("rendered.json", {name: {"args": [], "rust_log": ""} for name in lab.names()})
        calls = {}

        def set_limits(name, cpu_percent=None, memory_mb=None):
            calls[name] = (cpu_percent, memory_mb)

        with mock.patch.multiple(lab, heal_all=mock.DEFAULT, freeze_service=mock.DEFAULT,
                                 service_pid=mock.Mock(return_value=1), wait_live=mock.DEFAULT,
                                 set_limits=set_limits), \
                mock.patch.object(core, "idle_edges"):
            core.restore()
        self.assertEqual(set(calls), set(lab.names()) - set(lab.DEFAULT_SLOTS))
        self.assertEqual(calls[CONTRIBUTOR], (lab.CONTRIBUTOR_LIMITS["cpu_percent"],
                                              lab.CONTRIBUTOR_LIMITS["memory_mb"]))
        self.assertEqual(calls[EDGE], (lab.NANODE["cpu_percent"], 512))
        other = next(name for name in lab.names("distributor"))
        self.assertEqual(calls[other], (lab.NANODE["cpu_percent"], lab.NANODE["memory_mb"]))


if __name__ == "__main__":
    unittest.main()


class CpuSets(unittest.TestCase):
    """Wave 9 (cold start): node_cpus gives each node its own host CPUs, as
    an instance has, and --relay-class-cpus then names a node's own."""

    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp())
        patcher = mock.patch.object(lab, "WORK", self.work)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_node_its_own_cpus_and_the_rest_for_the_readers(self):
        names = [name for name, role, *_ in lab.NODES if role != "backbone"]
        with mock.patch.object(lab.os, "cpu_count", return_value=32):
            sets = lab.node_cpu_sets(names, first=0)
        self.assertEqual(sets[CONTRIBUTOR], [0, 1, 2, 3])
        self.assertEqual(len(sets[EDGE]), 2)
        used = [cpu for name in names for cpu in sets[name]]
        self.assertEqual(len(used), len(set(used)), "no CPU is shared")
        self.assertEqual(sets["_rest"], [cpu for cpu in range(32) if cpu not in used])
        with mock.patch.object(lab.os, "cpu_count", return_value=8), \
                self.assertRaises(lab.LabError):
            lab.node_cpu_sets(names, first=0)
        # Without a layout: no sets, and every node shares the host.
        self.assertEqual(lab.node_cpu_sets(names), {})

    def test_pinned_class_threads_land_on_the_nodes_own_cpus(self):
        lab.save("cpus.json", {EDGE: [10, 11], "_rest": [30, 31]})
        self.assertEqual(lab.node_class_cpus(EDGE, "0,1"), "10,11")
        self.assertEqual(lab.node_class_cpus(EDGE, "1"), "11")
        # Without a set the rendered value stands.
        self.assertEqual(lab.node_class_cpus(CONTRIBUTOR, "0,1"), "0,1")


class SiteDelays(unittest.TestCase):
    def test_a_site_may_be_put_as_far_as_tokyo(self):
        base = {"london": 2.0, "amsterdam": 4.0, "newyork": 34.0}
        self.assertEqual(lab.site_delays(None, base), base)
        self.assertEqual(lab.site_delays("", base), base)
        far = lab.site_delays("newyork=101", base)
        self.assertEqual(far, {"london": 2.0, "amsterdam": 4.0, "newyork": 101.0})
        self.assertEqual(base["newyork"], 34.0, "the base is not changed")
        self.assertEqual(lab.site_delays(" london=3 , newyork=50", base)["london"], 3.0)
        with self.assertRaises(lab.LabError):
            lab.site_delays("tokyo=100", base)


# What `up` starts by default: every node but the backbone relays (the merged
# topology has none) and the slots that are held out.
STANDING = [name for name, role, *_ in lab.NODES
            if role != "backbone" and name not in lab.DEFAULT_SLOTS]


class Isolated(unittest.TestCase):
    """A lab with a state directory and a registry of the test's own, and
    neither systemd nor D-Bus: no scope is alive, and a process that would
    be moved into a scope or pinned is recorded."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = self.tmp / "tree" / "target" / "elab-b"
        self.work.mkdir(parents=True)
        self.registry = self.tmp / "labs"
        self.attached = []
        self.pinned = []
        self.killed = []
        for name, value in (
                ("WORK", self.work), ("REGISTRY", self.registry), ("LAB_ID", "b"),
                ("_joined", (threading.Lock(), set())),
                ("lab_scopes", lambda work: []),
                ("attach", lambda unit, pid: self.attached.append((unit, pid))),
                ("kill_lab", lambda work: self.killed.append(pathlib.Path(work)))):
            patcher = mock.patch.object(lab, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, name, value in (
                (lab.os, "cpu_count", lambda: 32),
                (lab.os, "sched_setaffinity", lambda task, cpus: self.pinned.append(
                    (task, list(cpus))))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(lab.os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("NEEDLETAIL_EDGE_LAB_OWNER", "NEEDLETAIL_EDGE_LAB_CPUS"):
            lab.os.environ.pop(key, None)

    def entry(self, ident: str, cpus, work: str | None = None, owner: str | None = None,
              pid: int | None = None) -> dict:
        """A lab of another worktree in the registry, alive unless `pid`
        says otherwise."""
        entry = {"id": ident, "owner": owner or ident,
                 "pid": os.getpid() if pid is None else pid,
                 "worktree": "/trees/one", "work": work or f"/trees/one/target/elab-{ident}",
                 "cpus": list(cpus), "since_unix": 1.0}
        self.registry.mkdir(parents=True, exist_ok=True)
        (self.registry / f"{ident}.json").write_text(json.dumps(entry))
        return entry

    def registered(self) -> dict:
        return {path.stem: json.loads(path.read_text())
                for path in self.registry.glob("*.json")}

    def a_lab_that_is_up(self, sets: dict, names=()) -> None:
        lab.save("cpus.json", sets)
        lab.save("namespaces.json", {"lab": 100, **{name: 101 + index
                                                    for index, name in enumerate(names)}})
        lab.save("tools.json", {"pid": 7, "unit": lab.unit_name("tools")})


def dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


class CpuRange(Isolated):
    """`up cpus=A-B`: everything the lab runs stays in the range."""

    def test_nothing_of_the_lab_runs_outside_the_range(self):
        sets = lab.node_cpu_sets(STANDING, cpus=(2, 15))
        inside = set(range(2, 16))
        self.assertEqual(lab.lab_cpus(sets), sorted(inside))
        for name in STANDING:
            self.assertTrue(sets[name], name)
            self.assertLessEqual(set(sets[name]), inside, name)
        self.assertLessEqual(set(sets["_rest"]), inside)
        # A node's service: on its window, its class threads too.
        lab.save("rendered.json", {EDGE: {"rust_log": "info", "args": [
            "/bin/av-mesh", "--relay-class-cpus", "0,1"]}})
        self.a_lab_that_is_up(sets, [EDGE, CONTRIBUTOR, "lon-readers"])
        with mock.patch.object(lab.subprocess, "run"), \
                mock.patch.object(lab.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            self.assertEqual(lab.start_service(EDGE), 4242)
        command = popen.call_args.args[0]
        window = ",".join(str(cpu) for cpu in sets[EDGE])
        at = command.index("--")
        self.assertEqual(command[at + 1:], [
            "nsenter", "--preserve-credentials", "-U", "-n", "-t", "101",
            "taskset", "-c", window, "/bin/av-mesh", "--relay-class-cpus", window])
        self.assertEqual(command[command.index("--unit") + 1], lab.unit_name(EDGE))
        # Every other command of the lab, in whichever namespace: on the
        # CPUs that are not the nodes', and the process that starts them is
        # there too, every thread of it, in the lab's tools scope.
        for name in (None, "lon-readers", CONTRIBUTOR, EDGE):
            self.assertEqual(lab.enter(name)[-3:], ["taskset", "-c", "13,14,15"], name)
        self.assertEqual(self.attached, [(lab.unit_name("tools"), os.getpid())])
        self.assertEqual({tuple(cpus) for _task, cpus in self.pinned}, {(13, 14, 15)})
        self.assertEqual({task for task, _cpus in self.pinned},
                         {int(task) for task in os.listdir("/proc/self/task")})

    def test_the_readers_are_on_the_tail_of_the_range(self):
        for cpus, rest in (((2, 15), [13, 14, 15]), ((16, 29), [27, 28, 29]),
                           ((0, 31), list(range(24, 32))), ((8, 15), [14, 15]),
                           ((4, 6), [5, 6])):
            sets = lab.node_cpu_sets(STANDING, cpus=cpus)
            self.assertEqual(sets["_rest"], rest, cpus)
            self.assertEqual(sets["_node_cpus"], list(range(cpus[0], rest[0])), cpus)
            nodes = {cpu for name in STANDING for cpu in sets[name]}
            self.assertFalse(nodes & set(rest), "no node is on the readers' CPUs")
        self.assertEqual(lab.cpu_range("2-15"), (2, 15))
        self.assertEqual(lab.cpu_range(" 16 - 29 "), (16, 29))
        for text in ("5-6", "30-40", "15-2", "2", "a-b", "2-15,20"):
            with self.assertRaises(lab.LabError, msg=text):
                lab.cpu_range(text)

    def test_nodes_take_windows_round_robin_and_share_a_short_range(self):
        sets = lab.node_cpu_sets(STANDING, cpus=(2, 15))
        # Eleven node CPUs (2-12) for 26 CPUs of limits: in the order of
        # NODES, each window follows the last, around the ring.
        self.assertEqual(sets[CONTRIBUTOR], [2, 3, 4, 5])
        self.assertEqual([sets[name] for name in ("lon-dist-a", "lon-dist-b", "lon-edge-1",
                                                  "lon-edge-2", "lon-edge-3")],
                         [[6, 7], [8, 9], [10, 11], [12, 2], [3, 4]])
        load = {cpu: sum(cpu in sets[name] for name in STANDING) for cpu in range(2, 13)}
        self.assertEqual(sum(load.values()), 26)
        self.assertLessEqual(max(load.values()) - min(load.values()), 1,
                             "the node CPUs are shared evenly")
        # A node never has more CPUs than there are.
        small = lab.node_cpu_sets(STANDING, cpus=(4, 6))
        self.assertEqual(small[CONTRIBUTOR], [4])
        # A range that is long enough gives every node CPUs of its own.
        with mock.patch.object(lab.os, "cpu_count", lambda: 64):
            wide = lab.node_cpu_sets(STANDING, cpus=(0, 47))
        used = [cpu for name in STANDING for cpu in wide[name]]
        self.assertEqual(len(used), len(set(used)))

    def test_a_held_slot_takes_no_window_until_it_is_taken_in(self):
        sets = lab.node_cpu_sets(STANDING, cpus=(2, 15))
        for slot in lab.DEFAULT_SLOTS:
            self.assertNotIn(slot, sets)
        lab.save("cpus.json", sets)
        last = sets[STANDING[-1]][-1]
        ring = sets["_node_cpus"]
        after = [ring[(ring.index(last) + step) % len(ring)] for step in (1, 2, 3, 4)]
        # Its service is started: the next window of the ring, kept.
        self.assertEqual(lab.node_cpus("lon-dist-c"), after[:2])
        self.assertEqual(lab.node_cpus("lon-dist-c"), after[:2])
        self.assertEqual(lab.load("cpus.json")["lon-dist-c"], after[:2])
        self.assertEqual(lab.node_cpus("new-dist-c"), after[2:])
        self.assertEqual(lab.node_cpus(EDGE), sets[EDGE], "the others keep theirs")
        # With node_cpus the slot's CPUs are its own, and leave the readers'.
        lab.save("cpus.json", lab.node_cpu_sets(STANDING, first=0))
        self.assertEqual(lab.load("cpus.json")["_rest"], list(range(26, 32)))
        self.assertEqual(lab.node_cpus("lon-dist-c"), [26, 27])
        self.assertEqual(lab.load("cpus.json")["_rest"], [28, 29, 30, 31])
        # Without a layout there is nothing to take.
        lab.save("cpus.json", {})
        self.assertIsNone(lab.node_cpus("lon-dist-c"))
        self.assertEqual(lab.load("cpus.json"), {})

    def test_two_labs_on_the_standing_ranges_share_no_cpu(self):
        one = lab.node_cpu_sets(STANDING, cpus=(2, 15))
        two = lab.node_cpu_sets(STANDING, cpus=(16, 29))
        every = lambda sets: {cpu for name in STANDING + ["_rest"] for cpu in sets[name]}  # noqa: E731
        self.assertEqual(every(one), set(range(2, 16)))
        self.assertEqual(every(two), set(range(16, 30)))
        self.assertFalse(set(lab.lab_cpus(one)) & set(lab.lab_cpus(two)))
        # A lab without a range may run wherever its starter may; one with
        # node_cpus is on the whole host.
        self.assertEqual(lab.lab_cpus({}), sorted(os.sched_getaffinity(0)))
        self.assertEqual(lab.lab_cpus(lab.node_cpu_sets(STANDING, first=0)), list(range(32)))


class Registry(Isolated):
    """One registry for every worktree of the host."""

    def test_an_overlap_is_refused_across_worktrees(self):
        self.entry("a", range(2, 16), owner="alice")
        with self.assertRaises(lab.LabError) as caught:
            lab.claim(list(range(10, 20)))
        message = str(caught.exception)
        for part in ("CPUs 10-15 are lab a's", "alice", "/trees/one", "cpus_overlap=1"):
            self.assertIn(part, message)
        self.assertEqual(self.killed, [], "nothing was stopped")
        self.assertEqual(sorted(self.registered()), ["a"])
        # A lab without a range is on every CPU: refused beside any lab.
        with self.assertRaises(lab.LabError):
            lab.claim(lab.lab_cpus({}))
        # Outside the other lab's CPUs: registered, for every worktree to see.
        lab.claim(list(range(16, 30)))
        mine = self.registered()["b"]
        self.assertEqual({key: mine[key] for key in ("id", "owner", "pid", "worktree", "work",
                                                     "cpus")},
                         {"id": "b", "owner": "b", "pid": os.getpid(),
                          "worktree": str(lab.ROOT), "work": str(self.work.resolve()),
                          "cpus": list(range(16, 30))})
        self.assertGreater(mine["since_unix"], 1.0)
        self.assertEqual([entry["id"] for entry in lab.registered_labs()], ["a", "b"])
        # Asked for: the two labs share CPUs, and both stay registered.
        lab.claim(list(range(10, 20)), overlap=True)
        self.assertEqual(self.registered()["b"]["cpus"], list(range(10, 20)))
        self.assertIn("a", self.registered())
        self.assertNotIn(pathlib.Path("/trees/one/target/elab-a"), self.killed)

    def test_a_stale_entry_is_dropped(self):
        self.entry("a", range(2, 16), pid=dead_pid())
        (self.registry / "torn.json").write_text("{")
        # Its process is gone, but a scope of the lab is alive: it runs.
        with mock.patch.object(lab, "lab_scopes", lambda work: [
                "needletail-lab-elab-a-00000000-lon-edge-1.scope"]):
            self.assertEqual([entry["id"] for entry in lab.registered_labs()], ["a"])
            with self.assertRaises(lab.LabError):
                lab.claim(list(range(2, 16)))
        self.assertEqual(sorted(self.registered()), ["a"])
        # Nothing of it is alive: dropped, and its CPUs are free.
        self.assertEqual(lab.registered_labs(), [])
        self.assertEqual(self.registered(), {})
        self.entry("a", range(2, 16), pid=dead_pid())
        lab.claim(list(range(2, 16)))
        self.assertEqual(sorted(self.registered()), ["b"])

    def test_up_on_a_lab_of_another_owner_is_refused_unless_it_is_taken(self):
        work = str(self.work.resolve())
        self.entry("b", range(16, 30), work=work, owner="alice")
        lab.os.environ["NEEDLETAIL_EDGE_LAB_OWNER"] = "bob"
        with self.assertRaises(lab.LabError) as caught:
            lab.claim(list(range(16, 30)))
        self.assertIn("lab b of alice", str(caught.exception))
        self.assertIn("take=1", str(caught.exception))
        self.assertEqual(self.killed, [], "alice's lab runs on")
        self.assertEqual(self.registered()["b"]["owner"], "alice")
        lab.claim(list(range(16, 30)), take=True)
        self.assertEqual(self.killed, [self.work])
        self.assertEqual(self.registered()["b"]["owner"], "bob")
        # The owner's own `up` replaces the owner's lab, as it always did.
        self.killed.clear()
        lab.claim(list(range(16, 30)))
        self.assertEqual(self.killed, [self.work])

    def test_an_id_that_runs_from_another_worktree_is_refused(self):
        self.entry("b", range(16, 30))
        with self.assertRaises(lab.LabError) as caught:
            lab.claim(list(range(2, 16)))
        self.assertIn("/trees/one/target/elab-b", str(caught.exception))
        self.assertEqual(self.killed, [])
        self.assertEqual(self.registered()["b"]["worktree"], "/trees/one")
        # Taken: that lab is stopped first, and the id is this worktree's.
        lab.claim(list(range(2, 16)), take=True)
        self.assertEqual(self.killed, [pathlib.Path("/trees/one/target/elab-b"), self.work])
        self.assertEqual(self.registered()["b"]["work"], str(self.work.resolve()))

    def test_down_removes_its_own_entry_only(self):
        self.entry("a", range(2, 16))
        lab.claim(list(range(16, 30)))
        lab.register_holder(os.getpid())
        lab.down(quiet=True)
        self.assertEqual(self.killed[-1], self.work)
        self.assertEqual(sorted(self.registered()), ["a"])
        # The same id from this worktree, which runs nothing: lab a of the
        # other worktree stays registered.
        with mock.patch.object(lab, "LAB_ID", "a"):
            lab.down(quiet=True)
        self.assertEqual(sorted(self.registered()), ["a"])

    def test_the_entry_follows_the_labs_namespace_holder(self):
        lab.claim(list(range(16, 30)))
        self.assertEqual(self.registered()["b"]["pid"], os.getpid())
        gone = dead_pid()
        lab.register_holder(gone)
        self.assertEqual(self.registered()["b"]["pid"], gone)
        self.assertEqual(lab.registered_labs(), [], "the holder is gone and no scope is alive")

    def test_the_lab_id_names_the_state_directory_and_the_lab(self):
        self.assertEqual(lab.work_dir({}), lab.ROOT / "target" / "edge-lab")
        self.assertEqual(lab.work_dir({"NEEDLETAIL_EDGE_LAB_ID": "a"}),
                         lab.ROOT / "target" / "elab-a")
        self.assertEqual(lab.work_dir({"NEEDLETAIL_EDGE_LAB_ID": "run7",
                                       "NEEDLETAIL_EDGE_LAB_WORK": "/somewhere/else"}),
                         pathlib.Path("/somewhere/else"))
        for bad in ("A", "a-b", "a/b", "ninechars", "a b", ".."):
            with self.assertRaises(SystemExit, msg=bad):
                lab.work_dir({"NEEDLETAIL_EDGE_LAB_ID": bad})
        self.assertEqual(lab.lab_key(), "b")
        self.assertEqual(lab.lab_owner(), "b")
        lab.os.environ["NEEDLETAIL_EDGE_LAB_OWNER"] = "jo"
        self.assertEqual(lab.lab_owner(), "jo")
        # A lab without an id is known by its state directory, which no id
        # can spell.
        with mock.patch.object(lab, "LAB_ID", None):
            self.assertEqual(lab.lab_key(), lab.work_tag(self.work))
            self.assertRegex(lab.lab_key(), r"^elab-b-[0-9a-f]{8}$")


class Up(Isolated):
    """`up`, with the namespaces, the binaries and the render left out."""

    def setUp(self):
        super().setUp()
        self.calls = []
        binary = self.tmp / "binary"
        binary.write_text("")

        def networks():
            self.calls.append("build_networks")
            lab.save("namespaces.json", {"lab": 4242})
            lab.save("links.json", {EDGE: 0})

        def render(overrides, build="a"):
            self.calls.append(("render", build, list(overrides)))
            return {EDGE: {"args": [], "rust_log": ""}}

        for name, value in (
                ("AV_MESH", binary), ("READER", binary), ("AV_CONTRIB", binary),
                ("PROBE", binary), ("AV_MESH_NEXT", None), ("AV_CONTRIB_NEXT", None),
                ("kill_lab", lambda work: self.calls.append(("kill_lab", pathlib.Path(work)))),
                ("start_tools", lambda: (self.calls.append("start_tools"), lab.save(
                    "tools.json", {"pid": 7, "unit": lab.unit_name("tools")}))),
                ("build_networks", networks),
                ("snapshot_binaries", lambda: self.calls.append("snapshot_binaries")),
                ("build_clock_shim", lambda: None), ("render", render),
                ("apply_netem", lambda *args: None), ("sh_in", lambda *args, **kwargs: "")):
            patcher = mock.patch.object(lab, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_refused_up_stops_nothing(self):
        self.entry("b", range(16, 30), work=str(self.work.resolve()), owner="alice")
        with self.assertRaises(lab.LabError):
            lab.up(["cpus=16-29"], start=False)
        self.entry("a", range(2, 16))
        (self.registry / "b.json").unlink()
        with self.assertRaises(lab.LabError):
            lab.up(["cpus=10-20"], start=False)
        with self.assertRaises(lab.LabError):
            lab.up([], start=False)  # every CPU: lab a is on some of them
        self.assertEqual(self.calls, [])
        self.assertEqual(sorted(self.registered()), ["a"])

    def test_up_registers_the_range_before_it_stops_or_starts_anything(self):
        self.entry("a", range(2, 16))
        lab.up(["cpus=16-29", "slots=lon-edge-4"], start=False)
        self.assertEqual(self.calls, [("kill_lab", self.work), "start_tools", "build_networks",
                                      "snapshot_binaries", ("render", "a", [])])
        mine = self.registered()["b"]
        self.assertEqual(mine["cpus"], list(range(16, 30)))
        self.assertEqual(mine["pid"], 4242, "the namespace holder")
        sets = lab.load("cpus.json")
        self.assertEqual(sets["_range"], [16, 29])
        self.assertEqual(sets["_rest"], [27, 28, 29])
        for slot in ("lon-dist-c", "new-dist-c", "lon-edge-4"):
            self.assertNotIn(slot, sets, "a held slot has no CPUs")
        self.assertIn("new-edge-3", sets)
        self.assertEqual(lab.load("node-build.json"), {EDGE: "a"})
        # The process that brought it up is one of its tools, on its tail.
        self.assertEqual(self.attached, [(lab.unit_name("tools"), os.getpid())])
        self.assertEqual({tuple(cpus) for _task, cpus in self.pinned}, {(27, 28, 29)})

    def test_the_range_may_come_from_the_environment(self):
        lab.os.environ["NEEDLETAIL_EDGE_LAB_CPUS"] = "2-15"
        lab.up([], start=False)
        self.assertEqual(self.registered()["b"]["cpus"], list(range(2, 16)))
        lab.up(["cpus=16-29"], start=False)
        self.assertEqual(self.registered()["b"]["cpus"], list(range(16, 30)))
        del lab.os.environ["NEEDLETAIL_EDGE_LAB_CPUS"]
        # One lab alone on the host, each node its own CPUs.
        lab.up(["node_cpus=0"], start=False)
        self.assertEqual(self.registered()["b"]["cpus"], list(range(32)))
        self.assertEqual(lab.load("cpus.json")[CONTRIBUTOR], [0, 1, 2, 3])

    def test_lab_settings_that_cannot_be(self):
        for overrides in (["cpus=2-15", "node_cpus=0"], ["take=yes"], ["cpus_overlap=2"],
                          ["cpus=2"], ["b.rust_log_mesh=debug"], ["slots=lon-edge-1"]):
            with self.assertRaises(lab.LabError, msg=overrides):
                lab.up(overrides, start=False)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.registered(), {})

    def test_set_b_is_rendered_with_its_own_settings(self):
        with mock.patch.object(lab, "AV_MESH_NEXT", self.tmp / "binary"):
            lab.up(["cpus=16-29", "rust_log_mesh=debug", "b.relay_lease_children=false",
                    "b.tokio_worker_threads=1"], start=False)
            with self.assertRaises(lab.LabError):
                lab.up(["b.cpus=2-15"], start=False)
            with self.assertRaises(lab.LabError):
                lab.up(["b.limit.lon-edge-1.cpu_percent=50"], start=False)
        renders = [call for call in self.calls if call[0] == "render"]
        self.assertEqual(renders, [
            ("render", "b", ["rust_log_mesh=debug", "relay_lease_children=false",
                             "tokio_worker_threads=1"]),
            ("render", "a", ["rust_log_mesh=debug"])])
        self.assertEqual(lab.load("node-build.json"), {EDGE: "a"}, "every node starts on set a")


class Units(Isolated):
    def test_two_work_paths_with_one_basename_get_two_units(self):
        one = self.tmp / "one" / "target" / "elab-a"
        two = self.tmp / "two" / "target" / "elab-a"
        units = []
        for work in (one, two):
            with mock.patch.object(lab, "WORK", work):
                units.append([lab.unit_name(name) for name in (EDGE, "tools")])
        self.assertFalse(set(units[0]) & set(units[1]))
        digest = hashlib.sha1(str(one.resolve()).encode()).hexdigest()[:8]
        self.assertEqual(units[0], [f"needletail-lab-elab-a-{digest}-{EDGE}.scope",
                                    f"needletail-lab-elab-a-{digest}-tools.scope"])
        self.assertRegex(units[1][0], rf"^needletail-lab-elab-a-[0-9a-f]{{8}}-{EDGE}\.scope$")
        # However the directory is spelt, it is one lab.
        with mock.patch.object(lab, "WORK", one / ".." / "elab-a"):
            self.assertEqual(lab.unit_name(EDGE), units[0][0])


class Tools(Isolated):
    """The scope of everything the lab runs beside its nodes' services."""

    def test_the_tools_scope_is_made_once_and_lets_processes_join(self):
        unit = lab.unit_name("tools")
        for active, command in ((False, ["systemd-run", "--user", "--scope", "--quiet",
                                         "--collect", "--unit", unit, "-p", "Delegate=yes",
                                         "--", "sleep", "infinity"]),
                                (True, ["sleep", "infinity"])):
            self.attached.clear()
            with mock.patch.object(lab.subprocess, "run") as run, \
                    mock.patch.object(lab.subprocess, "Popen") as popen, \
                    mock.patch.object(lab, "cgroup_of",
                                      lambda pid: pathlib.Path("/sys/fs/cgroup/app.slice") / unit):
                run.return_value.returncode = 0 if active else 3
                popen.return_value.pid = 555
                lab.start_tools()
            self.assertEqual(popen.call_args.args[0], command)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            # A scope that a process of the lab still holds is joined.
            self.assertEqual(self.attached, [(unit, 555)] if active else [])
            self.assertEqual(lab.load("tools.json"), {"pid": 555, "unit": unit})

    def test_a_scope_that_does_not_start_fails_the_up(self):
        with mock.patch.object(lab.subprocess, "run") as run, \
                mock.patch.object(lab.subprocess, "Popen") as popen, \
                mock.patch.object(lab, "cgroup_of", lambda pid: None), \
                self.assertRaises(lab.LabError):
            run.return_value.returncode = 3
            popen.return_value.pid = 555
            popen.return_value.poll.return_value = 1
            lab.start_tools()
        self.assertEqual(lab.load("tools.json"), {})

    def test_a_process_joins_once_for_each_lab_instance(self):
        self.a_lab_that_is_up({}, ["lon-readers"])
        for _ in range(3):
            self.assertEqual(lab.enter("lon-readers"),
                             ["nsenter", "--preserve-credentials", "-U", "-n", "-t", "101"])
        self.assertEqual(self.attached, [(lab.unit_name("tools"), os.getpid())])
        self.assertEqual(self.pinned, [], "a lab without a layout pins nothing")
        # The lab is brought up again: a new holder, joined again.
        lab.save("tools.json", {"pid": 8, "unit": lab.unit_name("tools")})
        lab.enter(None)
        self.assertEqual(len(self.attached), 2)
        # A lab without its tools scope runs no command.
        (self.work / "tools.json").unlink()
        with self.assertRaises(lab.LabError):
            lab.enter("lon-readers")

    def test_a_process_that_cannot_run_on_the_tail_is_an_error(self):
        self.a_lab_that_is_up(lab.node_cpu_sets(STANDING, cpus=(2, 15)))

        def refuse(task, cpus):
            raise OSError(22, "Invalid argument")

        with mock.patch.object(lab.os, "sched_setaffinity", refuse), \
                self.assertRaises(lab.LabError):
            lab.join_tools()

    def test_the_labs_own_cpu_time_is_read_from_its_scopes(self):
        self.a_lab_that_is_up({})
        usage = {"/cg/tools": 500, f"/cg/{EDGE}": 1200}
        with mock.patch.object(lab, "cgroup_of", lambda pid: pathlib.Path("/cg/tools")), \
                mock.patch.object(lab, "cgroup_dir", lambda name: (
                    pathlib.Path(f"/cg/{name}") if name in (EDGE, CONTRIBUTOR) else None)), \
                mock.patch.object(lab, "read_cgroup", lambda directory: (
                    {"usage_usec": usage[str(directory)]} if str(directory) in usage else {})):
            self.assertEqual(lab.scope_cpu_usec(), {"tools": 500, EDGE: 1200})
