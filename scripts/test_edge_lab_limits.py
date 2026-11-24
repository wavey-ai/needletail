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
import io
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
# The real halves that Isolated replaces, for the tests of their own.
ATTACH, KILL_LAB, LAB_SCOPES, ACTIVE_SCOPES = (lab.attach, lab.kill_lab, lab.lab_scopes,
                                              lab.active_scopes)


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
        # Two contributors and the source host fit beside the mesh: each
        # contributor 2 CPUs, the dist-c slots none, 4 for the readers.
        self.assertEqual(sets[CONTRIBUTOR], [0, 1])
        self.assertEqual(len(sets["contrib-lon-b"]), lab.PINNED_CONTRIBUTOR_CPUS)
        self.assertEqual(len(sets["lon-src"]), 2)
        self.assertEqual(len(sets[EDGE]), 2)
        for slot in lab.DEFAULT_SLOTS:
            self.assertNotIn(slot, sets)
        used = [cpu for name in names for cpu in sets.get(name, [])]
        self.assertEqual(len(used), len(set(used)), "no CPU is shared")
        self.assertEqual(sets["_rest"], [cpu for cpu in range(32) if cpu not in used])
        self.assertEqual(sets["_rest"], [28, 29, 30, 31])
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
        # The scopes of labs on the host (systemctl list-units).
        self.host_scopes = []
        for name, value in (
                ("WORK", self.work), ("REGISTRY", self.registry), ("LAB_ID", "b"),
                ("_joined", (threading.Lock(), set())),
                ("lab_scopes", lambda work: []),
                ("active_scopes", lambda pattern: list(self.host_scopes)),
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
        # Eleven node CPUs (2-12) for 32 CPUs of limits: in the order of
        # NODES, each window follows the last, around the ring.
        self.assertEqual(sets[CONTRIBUTOR], [2, 3, 4, 5])
        self.assertEqual([sets[name] for name in ("lon-dist-a", "lon-dist-b", "lon-edge-1",
                                                  "lon-edge-2", "lon-edge-3")],
                         [[6, 7], [8, 9], [10, 11], [12, 2], [3, 4]])
        load = {cpu: sum(cpu in sets[name] for name in STANDING) for cpu in range(2, 13)}
        self.assertEqual(sum(load.values()), 32)
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
        # Without a layout there is nothing to take.
        lab.save("cpus.json", {})
        self.assertIsNone(lab.node_cpus("lon-dist-c"))
        self.assertEqual(lab.load("cpus.json"), {})

    def test_with_node_cpus_a_slot_never_takes_the_readers_cpus(self):
        # The reader hosts, the load tool and the driving process are pinned
        # to "_rest" at `up` and never again: with node_cpus every node that
        # may run has its own CPUs from `up` on (an edge slot included), and
        # a slot that is taken in later takes none of "_rest": a dist-c,
        # which has none in this layout, is refused.
        every = [name for name, role, *_ in lab.NODES if role != "backbone"]
        with mock.patch.object(lab.os, "cpu_count", return_value=32):
            sets = lab.node_cpu_sets(every, first=0)
        lab.save("cpus.json", sets)
        rest = list(sets["_rest"])
        self.assertEqual(rest, [28, 29, 30, 31])
        for slot in lab.SLOT_CANDIDATES:
            if slot in lab.DEFAULT_SLOTS:
                with self.assertRaises(lab.LabError, msg=slot):
                    lab.node_cpus(slot)
                continue
            self.assertEqual(lab.node_cpus(slot), sets[slot])
            self.assertFalse(set(sets[slot]) & set(rest), slot)
        self.assertEqual(lab.load("cpus.json")["_rest"], rest)
        # A backbone relay of the legacy topology runs under its quota, as
        # it did before: no window out of the readers' CPUs.
        self.assertIsNone(lab.node_cpus("relay-primary"))
        self.assertEqual(lab.load("cpus.json"), sets)

    def test_two_slots_started_at_once_take_two_windows(self):
        sets = lab.node_cpu_sets(STANDING, cpus=(2, 15))
        lab.save("cpus.json", sets)
        take = lab.take_window
        barrier = threading.Barrier(2, timeout=5)

        def slow(sets, name):
            # Both have read cpus.json before either writes it, unless the
            # lock keeps the second out until the first has saved.
            try:
                barrier.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass
            return take(sets, name)

        found = {}
        with mock.patch.object(lab, "take_window", slow):
            threads = [threading.Thread(target=lambda slot=slot: found.__setitem__(
                slot, lab.node_cpus(slot))) for slot in lab.DEFAULT_SLOTS]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        one, two = (found[slot] for slot in lab.DEFAULT_SLOTS)
        self.assertNotEqual(one, two)
        saved = lab.load("cpus.json")
        self.assertEqual(sorted([saved["lon-dist-c"], saved["new-dist-c"]]), sorted([one, two]))
        self.assertEqual(saved["_next"], (sets["_next"] + 4) % len(sets["_node_cpus"]))

    def test_two_labs_on_the_standing_ranges_share_no_cpu(self):
        one = lab.node_cpu_sets(STANDING, cpus=(2, 15))
        two = lab.node_cpu_sets(STANDING, cpus=(16, 29))
        every = lambda sets: {cpu for name in STANDING + ["_rest"] for cpu in sets[name]}  # noqa: E731
        self.assertEqual(every(one), set(range(2, 16)))
        self.assertEqual(every(two), set(range(16, 30)))
        self.assertFalse(set(lab.lab_cpus(one)) & set(lab.lab_cpus(two)))
        # A lab without a range, or with node_cpus, is on the whole host,
        # whatever CPUs the process that brings it up may use: a node or a
        # command that a later shell starts may run on any of them.
        with mock.patch.object(lab.os, "sched_getaffinity", lambda pid: {2, 3, 4}):
            self.assertEqual(lab.lab_cpus({}), list(range(32)))
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

    def test_a_reused_pid_is_not_the_lab(self):
        # The entry's pid is this process, which started before the entry
        # was written: the lab runs.
        self.entry("a", range(2, 16))
        self.assertEqual([entry["id"] for entry in lab.registered_labs()], ["a"])
        # Written before this process started: the lab's process is gone
        # and its pid was given to another one.
        started = lab.process_started_unix(os.getpid())
        self.assertIsNotNone(started)
        os.utime(self.registry / "a.json", (started - 60, started - 60))
        self.assertEqual(lab.registered_labs(), [])
        self.assertEqual(self.registered(), {})
        self.assertFalse(lab.pid_alive(dead_pid()))
        self.assertTrue(lab.pid_alive(os.getpid()))
        self.assertTrue(lab.pid_alive(os.getpid(), started + 0.5))
        self.assertFalse(lab.pid_alive(os.getpid(), started - 5))

    def test_a_lab_the_registry_does_not_know_is_refused(self):
        # The lab of another worktree, registered, and this lab's own units:
        # known. A lab brought up before the registry existed: not known,
        # and it may be on any CPU.
        other = self.entry("a", range(2, 16))
        self.host_scopes = [
            f"needletail-lab-{lab.work_tag(pathlib.Path(other['work']))}-lon-edge-1.scope",
            f"needletail-lab-{lab.work_tag(self.work)}-tools.scope"]
        lab.claim(list(range(16, 30)))
        self.host_scopes.append("needletail-lab-edge-lab-lon-edge-1.scope")
        with self.assertRaises(lab.LabError) as caught:
            lab.claim(list(range(16, 30)))
        self.assertIn("needletail-lab-edge-lab-lon-edge-1.scope", str(caught.exception))
        self.assertIn("cpus_overlap=1", str(caught.exception))
        self.assertEqual(self.killed, [self.work], "the refused claim stopped nothing")
        lab.claim(list(range(16, 30)), overlap=True)

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
                # Each `up` has a holder of its own.
                ("start_tools", lambda: (self.calls.append("start_tools"), lab.save(
                    "tools.json", {"pid": 7 + self.calls.count("start_tools"),
                                   "unit": lab.unit_name("tools")}))),
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
        # One lab alone on the host, each node its own CPUs, the edge slots
        # included: the readers are pinned to what is left, once. The
        # contributors' limit is their two CPUs.
        lab.up(["node_cpus=0", "slots=lon-edge-4"], start=False)
        self.assertEqual(self.registered()["b"]["cpus"], list(range(32)))
        sets = lab.load("cpus.json")
        self.assertEqual(sets[CONTRIBUTOR], [0, 1])
        nodes = [cpu for name in lab.SLOT_CANDIDATES for cpu in sets.get(name, [])]
        self.assertEqual(len(nodes), 4)
        self.assertEqual(sets["_rest"], [28, 29, 30, 31])
        self.assertFalse(set(nodes) & set(sets["_rest"]))
        self.assertEqual({tuple(cpus) for _task, cpus in self.pinned[-1:]}, {(28, 29, 30, 31)})
        for name in ("contrib-lon", "contrib-lon-b"):
            self.assertEqual(lab.service_limits(name)["cpu_percent"], 200, name)
        lab.up(["node_cpus=0", "limit.contrib-lon.cpu_percent=150"], start=False)
        self.assertEqual(lab.service_limits("contrib-lon")["cpu_percent"], 150)
        # In a range the limits are the instances': 4 CPUs a contributor.
        lab.up(["cpus=2-15"], start=False)
        self.assertEqual(lab.service_limits("contrib-lon")["cpu_percent"], 400)

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
        for state, command in ((("inactive", []), [
                                   "systemd-run", "--user", "--scope", "--quiet", "--collect",
                                   "--unit", unit, "-p", "Delegate=yes", "--", "sleep",
                                   "infinity"]),
                               (("active", [os.getpid()]), ["sleep", "infinity"])):
            self.attached.clear()
            with mock.patch.object(lab.subprocess, "run"), \
                    mock.patch.object(lab, "scope_state", lambda unit: state), \
                    mock.patch.object(lab.subprocess, "Popen") as popen, \
                    mock.patch.object(lab, "cgroup_of",
                                      lambda pid: pathlib.Path("/sys/fs/cgroup/app.slice") / unit):
                popen.return_value.pid = 555
                lab.start_tools()
            self.assertEqual(popen.call_args.args[0], command)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            # A scope that a process of the lab still holds is joined.
            self.assertEqual(self.attached, [(unit, 555)] if state[0] == "active" else [])
            self.assertEqual(lab.load("tools.json"), {"pid": 555, "unit": unit})

    def test_the_scope_of_the_last_lab_is_let_end_first(self):
        # `up` killed the last holder a moment ago: its scope is still
        # active with nothing in it, then deactivating, then gone. The new
        # scope is made only then, under the same name.
        unit = lab.unit_name("tools")
        states = [("active", []), ("deactivating", []), ("inactive", [])]
        with mock.patch.object(lab.subprocess, "run"), \
                mock.patch.object(lab, "scope_state", lambda unit: states.pop(0)), \
                mock.patch.object(lab.time, "sleep"), \
                mock.patch.object(lab.subprocess, "Popen") as popen, \
                mock.patch.object(lab, "cgroup_of",
                                  lambda pid: pathlib.Path("/sys/fs/cgroup/app.slice") / unit):
            popen.return_value.pid = 556
            lab.start_tools()
        self.assertEqual(states, [])
        self.assertEqual(popen.call_args.args[0][0], "systemd-run")
        self.assertEqual(self.attached, [])
        # A scope that never ends is an error, and nothing is started.
        with mock.patch.object(lab, "scope_state", lambda unit: ("deactivating", [])), \
                mock.patch.object(lab.time, "time", side_effect=[0.0, 1.0, 6.0]), \
                mock.patch.object(lab.time, "sleep"), \
                mock.patch.object(lab.subprocess, "Popen") as popen, \
                self.assertRaises(lab.LabError):
            lab.start_tools()
        popen.assert_not_called()

    def test_a_scope_that_does_not_start_fails_the_up_and_leaves_no_holder(self):
        for state in (("inactive", []), ("active", [os.getpid()])):
            with mock.patch.object(lab.subprocess, "run"), \
                    mock.patch.object(lab, "scope_state", lambda unit: state), \
                    mock.patch.object(lab.subprocess, "Popen") as popen, \
                    mock.patch.object(lab, "cgroup_of", lambda pid: None), \
                    self.assertRaises(lab.LabError):
                popen.return_value.pid = 555
                popen.return_value.poll.return_value = 1
                lab.start_tools()
            popen.return_value.kill.assert_called_once_with()
            self.assertEqual(lab.load("tools.json"), {})
        # The holder could not be moved into the scope that exists.
        def refuse(unit, pid):
            raise lab.LabError("could not move")

        with mock.patch.object(lab, "scope_state", lambda unit: ("active", [os.getpid()])), \
                mock.patch.object(lab, "attach", refuse), \
                mock.patch.object(lab.subprocess, "Popen") as popen, \
                self.assertRaises(lab.LabError):
            lab.start_tools()
        popen.return_value.kill.assert_called_once_with()

    def test_the_state_of_a_scope_is_read_from_systemd_and_its_cgroup(self):
        cgroup = self.tmp / "cg" / "user.slice" / "x.scope"
        cgroup.mkdir(parents=True)
        (cgroup / "cgroup.procs").write_text("12\n34\n")
        answers = {"x.scope": "ActiveState=active\nControlGroup=/user.slice/x.scope\n",
                   "gone.scope": "ActiveState=inactive\nControlGroup=\n"}
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, answers[command[-1]], "")

        with mock.patch.object(lab.subprocess, "run", run), \
                mock.patch.object(lab, "CGROUP_ROOT", self.tmp / "cg"):
            self.assertEqual(lab.scope_state("x.scope"), ("active", [12, 34]))
            self.assertEqual(lab.scope_state("gone.scope"), ("inactive", []))
        self.assertEqual(calls[0], ["systemctl", "--user", "show", "--property=ActiveState",
                                    "--property=ControlGroup", "x.scope"])

    def test_a_process_is_moved_into_a_scope_by_the_users_manager(self):
        with mock.patch.object(lab.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            ATTACH("needletail-lab-elab-b-0123abcd-tools.scope", 4321)
            run.return_value = subprocess.CompletedProcess(
                [], 1, "", "Process migration not available on non-delegated units.")
            with self.assertRaises(lab.LabError) as caught:
                ATTACH("needletail-lab-elab-b-0123abcd-tools.scope", 4321)
        self.assertEqual(run.call_args_list[0].args[0], [
            "busctl", "--user", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "AttachProcessesToUnit", "ssau",
            "needletail-lab-elab-b-0123abcd-tools.scope", "", "1", "4321"])
        self.assertIn("non-delegated", str(caught.exception))
        self.assertIn("tmux", str(caught.exception))

    def test_the_scopes_of_a_lab_are_read_from_list_units(self):
        # systemctl --user list-units --plain --no-legend, as it prints.
        listed = ("needletail-lab-elab-b-0123abcd-lon-edge-1.scope loaded active running "
                  "[systemd-run] /usr/bin/nsenter ...\n"
                  "needletail-lab-elab-b-0123abcd-tools.scope        loaded active running "
                  "[systemd-run] /usr/bin/sleep infinity\n\n")
        with mock.patch.object(lab.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, listed, "")
            with mock.patch.object(lab, "work_tag", lambda work: "elab-b-0123abcd"), \
                    mock.patch.object(lab, "active_scopes", ACTIVE_SCOPES):
                found = LAB_SCOPES("/x/target/elab-b")
        self.assertEqual(found, ["needletail-lab-elab-b-0123abcd-lon-edge-1.scope",
                                 "needletail-lab-elab-b-0123abcd-tools.scope"])
        self.assertEqual(run.call_args.args[0], [
            "systemctl", "--user", "list-units", "--type=scope", "--state=active",
            "--no-legend", "--plain", "needletail-lab-elab-b-0123abcd-*.scope"])

    def test_kill_lab_stops_every_process_it_recorded_the_tools_holder_too(self):
        work = self.tmp / "other" / "target" / "elab-c"
        work.mkdir(parents=True)
        for name, value in (("services.json", {"lon-edge-1": {"pid": 301, "build": "a"},
                                               "contrib-lon": {"pid": 302}}),
                            ("sources.json", {"audio": 303}),
                            ("namespaces.json", {"lab": 304, "lon-edge-1": 305}),
                            ("tools.json", {"pid": 306, "unit": "x-tools.scope"}),
                            ("cpus.json", {"_rest": [30, 31]}), ("links.json", {}),
                            ("shaped.json", {}), ("qdisc.json", {})):
            (work / name).write_text(json.dumps(value))
        (work / "rendered.json").write_text("{}")
        killed = []
        with mock.patch.object(lab.os, "killpg", lambda pid, sig: killed.append((pid, sig))):
            KILL_LAB(work)
        self.assertEqual(sorted(killed), [(pid, lab.signal.SIGKILL) for pid in range(301, 307)])
        self.assertEqual(sorted(path.name for path in work.iterdir()), ["rendered.json"])

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


# The edges that an rt=1 lab in 16-31 starts: lon-edge-4 and new-edge-3 held
# as slots, so that five edges have CPUs of their own.
RT_STANDING = [name for name in STANDING if name not in ("lon-edge-4", "new-edge-3")]


class RealTime(Isolated):
    """`up rt=1`: real-time scheduling as a Linode unit has it, granted to
    the node's own process by its real-time limits, never by running it as
    root; two CPUs a node; class threads pinned as the deploy pins them."""

    def test_every_node_two_cpus_and_the_edges_their_own(self):
        sets = lab.node_cpu_sets(RT_STANDING, cpus=(16, 31), per_node=lab.RT_CPUS)
        for name in RT_STANDING:
            if name != lab.SOURCE_HOST:
                self.assertEqual(len(sets[name]), 2, name)
        # The sender's host has the last CPU of the tail alone.
        self.assertEqual(sets["_rest"], [28, 29, 30])
        self.assertEqual(sets["_source"], [31])
        self.assertEqual(sets[lab.SOURCE_HOST], [31])
        self.assertFalse(any(31 in sets[name] for name in RT_STANDING if name != lab.SOURCE_HOST))
        self.a_lab_that_is_up(sets, [lab.SOURCE_HOST, CONTRIBUTOR])
        self.assertEqual(lab.enter(CONTRIBUTOR, source=True)[-3:], ["taskset", "-c", "31"])
        self.assertEqual(lab.enter(CONTRIBUTOR)[-3:], ["taskset", "-c", "28,29,30"])
        # Without rt a sender is pinned as any command.
        plain = lab.node_cpu_sets(RT_STANDING, cpus=(16, 31))
        self.assertNotIn("_source", plain)
        edges = [name for name in RT_STANDING if lab.node(name)["role"] == "edge"]
        self.assertEqual(len(edges), 5)
        for edge in edges:
            others = [name for name in RT_STANDING if name != edge
                      and set(sets[name]) & set(sets[edge])]
            self.assertEqual(others, [], f"{edge} shares its CPUs")
        # The others share the rest of the ring, never the readers' tail.
        for name in RT_STANDING:
            if name != lab.SOURCE_HOST:
                self.assertLessEqual(set(sets[name]), set(range(16, 28)), name)
        # Seven edges do not fit beside the rest: the ring is shared as before.
        crowded = lab.node_cpu_sets(STANDING, cpus=(16, 31), per_node=lab.RT_CPUS)
        self.assertNotIn("_edge_cpus", crowded)
        self.assertEqual(len(crowded[CONTRIBUTOR]), 2)
        # A slot edge taken in later wraps on the edges' ring.
        lab.save("cpus.json", sets)
        self.assertLessEqual(set(lab.node_cpus("lon-edge-4")), set(sets["_edge_cpus"]))
        # Without rt the layout is as it was.
        self.assertEqual(lab.node_cpu_sets(RT_STANDING, cpus=(16, 31))[CONTRIBUTOR],
                         [16, 17, 18, 19])
        # node_cpus: own CPUs anyway, two each.
        first = lab.node_cpu_sets(STANDING, first=0, per_node=lab.RT_CPUS)
        self.assertEqual(first[CONTRIBUTOR], [0, 1])

    def test_a_video_feed_never_takes_the_senders_own_cpu(self):
        # Local proof 3: in the own-CPU layout the 4K feed ran on the
        # sender's CPU 31 and the AEP1 sender's lateness went to 3.6 ms.
        sets = lab.own_cpu_sets([name for name, role, *_ in lab.NODES if role != "backbone"])
        self.a_lab_that_is_up(sets, [lab.SOURCE_HOST])
        self.assertEqual(lab.source_prefix()[-3:], ["taskset", "-c", "31"])
        tools = lab.cpu_list(lab.rest_split(sets)["tools"])
        self.assertEqual(lab.source_prefix(video=True)[-3:], ["taskset", "-c", tools])
        self.assertNotIn("31", tools.split(","))
        # Without a CPU of the sender's own, a feed goes where the sender does.
        plain = lab.node_cpu_sets(RT_STANDING, cpus=(16, 31))
        self.a_lab_that_is_up(plain, [lab.SOURCE_HOST])
        self.assertEqual(lab.source_prefix(video=True), lab.source_prefix())

    def test_a_node_setting_that_does_not_take_is_named(self):
        lab.save("sysctls.json", {"net.core.rmem_max": "134217728",
                                  "net.ipv4.udp_mem": "1 2 3"})
        read = {"net.core.rmem_max": "4194304", "net.ipv4.udp_mem": "1\t2\t3"}
        with mock.patch.object(lab, "sh_in", lambda name, script, check=True, timeout=60:
                               read[script.split()[-1].strip("'")]):
            seen = lab.sysctl_values("lon-edge-1", ["net.core.rmem_max", "net.ipv4.udp_mem"])
        self.assertEqual(seen, {"net.core.rmem_max": "4194304", "net.ipv4.udp_mem": "1 2 3"})
        self.assertEqual(lab.sysctls_not_applied(lab.load("sysctls.json"), seen),
                         {"net.core.rmem_max": {"wanted": "134217728", "node_has": "4194304"}})
        # As up calls it: the rendered nodes are a dict of name -> render.
        with mock.patch.object(lab, "sh_in", lambda name, script, check=True, timeout=60:
                               read[script.split()[-1].strip("'")]), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            short = lab.report_sysctls({"lon-edge-1": {}, "lon-edge-2": {}},
                                       lab.load("sysctls.json"))
        self.assertEqual(list(short), ["net.core.rmem_max"])
        self.assertEqual(lab.load("sysctls-not-applied.json"), short)
        self.assertIn("sudo sysctl -w net.core.rmem_max=134217728", err.getvalue())

    def test_the_node_raises_its_own_limits_and_runs_as_the_user(self):
        sets = lab.node_cpu_sets(RT_STANDING, cpus=(16, 31), per_node=lab.RT_CPUS)
        lab.save("rendered.json", {EDGE: {"rust_log": "info", "args": [
            "/bin/av-mesh", "--relay-class-cpus", "0,1"]}})
        self.a_lab_that_is_up(sets, [EDGE, CONTRIBUTOR, "lon-readers"])
        window = ",".join(str(cpu) for cpu in sets[EDGE])
        for realtime in (False, True):
            lab.save("rt.json", {"rt": realtime})
            with mock.patch.object(lab.subprocess, "run"), \
                    mock.patch.object(lab.subprocess, "Popen") as popen:
                popen.return_value.pid = 4242
                lab.start_service(EDGE)
            command = popen.call_args.args[0]
            at = command.index("--")
            service = ["nsenter", "--preserve-credentials", "-U", "-n", "-t", "101",
                       "taskset", "-c", window, "/bin/av-mesh", "--relay-class-cpus", window]
            if not realtime:
                self.assertEqual(command[at + 1:], service)
                self.assertNotIn("--expand-environment=no", command)
                continue
            # systemd-run leaves $$ to the shell; the shell raises its own
            # limits (sudo runs prlimit, nothing else) and becomes the service.
            self.assertIn("--expand-environment=no", command[:at])
            self.assertEqual(command[at + 1:at + 5], [
                "sh", "-c", lab.RT_GRANT.format(prio=99, nice=40), "needletail-rt"])
            self.assertEqual(command[at + 5:], service)
            grant = command[at + 3]
            self.assertIn("sudo -n prlimit --pid $$ --rtprio=99:99 --nice=40:40", grant)
            self.assertTrue(grant.endswith('exec "$@"'))
            self.assertEqual(grant.count("sudo -n prlimit --pid"), 1, "sudo runs prlimit once")

    def test_the_grant_raises_the_shells_own_limits_then_execs(self):
        # A sudo of the test's own, on PATH: it records what it was asked.
        fake = self.tmp / "bin"
        fake.mkdir()
        record = self.tmp / "sudo.args"
        (fake / "sudo").write_text(f'#!/bin/sh\necho "$PPID $*" >{record}\nexit "${{FAKE_SUDO:-0}}"\n')
        (fake / "sudo").chmod(0o755)
        grant = lab.RT_GRANT.format(prio=99, nice=40)
        env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"}
        result = subprocess.run(["sh", "-c", grant, "needletail-rt", "sh", "-c", "echo $$"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        parent, *argv = record.read_text().split()
        # --pid is the shell itself, which the service then is (exec).
        self.assertEqual(argv, ["-n", "prlimit", "--pid", parent, "--rtprio=99:99", "--nice=40:40"])
        self.assertEqual(result.stdout.strip(), parent)
        refused = subprocess.run(["sh", "-c", grant, "needletail-rt", "echo", "ran"],
                                 capture_output=True, text=True, env={**env, "FAKE_SUDO": "1"})
        self.assertEqual(refused.returncode, 126)
        self.assertNotIn("ran", refused.stdout)
        self.assertIn("rt=1", refused.stderr)

    def test_sudo_is_only_asked_whether_it_may(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "/usr/bin/prlimit\n", "")
        lab.rt_preflight(run)
        self.assertEqual(calls, [["sudo", "-n", "-l", "prlimit"]])
        for failure in (subprocess.CompletedProcess([], 1, "", "sudo: a password is required"),
                        OSError(2, "No such file")):
            def refuse(argv, failure=failure, **kwargs):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            with self.assertRaises(lab.LabError) as caught:
                lab.rt_preflight(refuse)
            self.assertIn("rt=1", str(caught.exception))

    def test_the_deploy_pins_an_rt_node_as_a_two_cpu_instance(self):
        sys.path.insert(0, str(HERE))
        import importlib.util
        spec = importlib.util.spec_from_file_location("deploy_rt", HERE / "linode-small-mesh-deploy.py")
        deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy)
        plan = {"relay_class_cpus": "auto"}
        lab.save("rt.json", {"rt": False, "layout": False})
        self.assertEqual(deploy.relay_class_cpus(lab.lab_state()["nodes"][EDGE], plan), "")
        lab.save("rt.json", {"rt": True, "layout": True})
        state = lab.lab_state()
        self.assertEqual(state["nodes"][EDGE]["type"], "g6-dedicated-2")
        self.assertEqual(deploy.relay_class_cpus(state["nodes"][EDGE], plan), "0,1")
        self.assertEqual(state["slots"]["lon-edge-4"]["type"], "g6-dedicated-2")
        # "0,1" lands on the node's own two CPUs.
        lab.save("cpus.json", {EDGE: [16, 17], "_rest": [28, 29, 30, 31]})
        self.assertEqual(lab.node_class_cpus(EDGE, "0,1"), "16,17")

    def test_the_check_wants_the_audio_thread_fifo_on_its_one_cpu(self):
        lab.save("rendered.json", {EDGE: {"rust_log": "", "args": [
            "/bin/av-mesh", "--relay-class-cpus", "0,1"]}, CONTRIBUTOR: {"rust_log": "", "args": [
            "/bin/av-contrib"]}})
        lab.save("cpus.json", {EDGE: [16, 17], CONTRIBUTOR: [26, 27], "_rest": [28, 29]})
        lab.save("services.json", {EDGE: {"pid": os.getpid()}, CONTRIBUTOR: {"pid": os.getpid()}})
        limits = "Max realtime priority     99                   99\n"
        fast = {"relay_session": {"fast_plane": {"realtime_threads": 2, "pinned_threads": 2}},
                # Another node's telemetry never counts as this node's.
                "relay_nodes": [{"fast_plane": {"realtime_threads": 9}}]}
        audio = {"tid": 11, "comm": "needletail-fast", "cls": "FF", "rtprio": 10, "psr": 16,
                 "allowed": "16"}
        other = {"tid": 12, "comm": "tokio-runtime-w", "cls": "TS", "rtprio": None, "psr": 17,
                 "allowed": "16-17"}
        good = lab.rt_check(EDGE, fast, [audio, other], limits)
        self.assertTrue(good["ok"], good["why"])
        self.assertEqual(good["fast_plane"]["realtime_threads"], 2)
        self.assertEqual(good["rtprio_limit"], "99")
        for snapshot, threads in (
                (fast, [{**audio, "allowed": "16-17"}, other]),     # not pinned
                (fast, [{**audio, "cls": "TS", "rtprio": None}]),   # not FIFO
                ({"relay_session": {"fast_plane": {"realtime_threads": 0,
                                                   "pinned_threads": 2}}}, [audio])):
            self.assertFalse(lab.rt_check(EDGE, snapshot, threads, limits)["ok"])
        self.assertIsNone(lab.rt_check(CONTRIBUTOR, {}, [other], limits)["ok"])
        # rt=pin: pinned at the normal policy passes; unpinned does not.
        plain = {**audio, "cls": "TS", "rtprio": None}
        self.assertTrue(lab.rt_check(EDGE, fast, [plain], limits, realtime=False)["ok"])
        self.assertFalse(lab.rt_check(EDGE, fast, [{**plain, "allowed": "16-17"}], limits,
                                      realtime=False)["ok"])
        self.assertEqual(lab.expand_cpus("0-2,8"), [0, 1, 2, 8])


class RealTimeUp(Up):
    def test_rt_is_refused_without_a_layout_or_sudo_and_nothing_is_stopped(self):
        with mock.patch.object(lab, "rt_preflight", lambda: None):
            with self.assertRaises(lab.LabError):
                lab.up(["rt=1"], start=False)
            with self.assertRaises(lab.LabError):
                lab.up(["rt=yes", "cpus=16-31"], start=False)
            with self.assertRaises(lab.LabError):
                lab.up(["rt=pin"], start=False)

        def no_sudo():
            raise lab.LabError("rt=1: no sudo")
        with mock.patch.object(lab, "rt_preflight", no_sudo), self.assertRaises(lab.LabError):
            lab.up(["rt=1", "cpus=16-31"], start=False)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.registered(), {})

    def test_rt_up_records_it_and_gives_every_node_two_cpus(self):
        with mock.patch.object(lab, "rt_preflight", lambda: None):
            lab.up(["rt=1", "cpus=16-31", "slots=lon-edge-4,new-edge-3"], start=False)
        self.assertTrue(lab.load("rt.json")["rt"])
        sets = lab.load("cpus.json")
        self.assertEqual(sets["_per_node"], 2)
        self.assertEqual(sets[CONTRIBUTOR], [26, 27])
        self.assertEqual(lab.service_limits(CONTRIBUTOR)["cpu_percent"], 200)
        self.assertEqual(lab.service_limits(EDGE)["cpu_percent"], 200)
        lab.up(["cpus=16-31"], start=False)
        self.assertFalse(lab.load("rt.json")["rt"])
        self.assertEqual(lab.service_limits(CONTRIBUTOR)["cpu_percent"], 400)

    def test_rt_pin_is_the_layout_without_the_grant(self):
        def no_sudo():
            raise lab.LabError("rt=1: no sudo")
        # The control needs no sudo: nothing is granted.
        with mock.patch.object(lab, "rt_preflight", no_sudo):
            lab.up(["rt=pin", "cpus=16-31", "slots=lon-edge-4,new-edge-3"], start=False)
        self.assertEqual(lab.load("rt.json"), {"rt": False, "layout": True, "cpus_per_node": 2})
        self.assertEqual(lab.load("cpus.json")[CONTRIBUTOR], [26, 27])
        self.assertEqual(lab.lab_state()["nodes"][EDGE]["type"], "g6-dedicated-2")
        self.assertEqual(lab.rt_grant(), [])
