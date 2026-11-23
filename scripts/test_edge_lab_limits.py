#!/usr/bin/env python3
"""Tests of the lab's service limits: the contributor has its own, and the
campaign's restore puts it back on them, not on a node's.
python3 -m unittest scripts/test_edge_lab_limits.py"""

from __future__ import annotations

import pathlib
import sys
import tempfile
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
            sets = lab.node_cpu_sets(0, names)
        self.assertEqual(sets[CONTRIBUTOR], [0, 1, 2, 3])
        self.assertEqual(len(sets[EDGE]), 2)
        used = [cpu for name in names for cpu in sets[name]]
        self.assertEqual(len(used), len(set(used)), "no CPU is shared")
        self.assertEqual(sets["_rest"], [cpu for cpu in range(32) if cpu not in used])
        with mock.patch.object(lab.os, "cpu_count", return_value=8), \
                self.assertRaises(lab.LabError):
            lab.node_cpu_sets(0, names)

    def test_pinned_class_threads_land_on_the_nodes_own_cpus(self):
        lab.save("cpus.json", {EDGE: [10, 11], "_rest": [30, 31]})
        self.assertEqual(lab.node_class_cpus(EDGE, "0,1"), "10,11")
        self.assertEqual(lab.node_class_cpus(EDGE, "1"), "11")
        # Without a set the rendered value stands.
        self.assertEqual(lab.node_class_cpus(CONTRIBUTOR, "0,1"), "0,1")
