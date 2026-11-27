#!/usr/bin/env python3
"""The own-CPU layout of the whole 32-CPU host for the change proofs (local
proof 2: on shared CPUs half the nulls failed): the arithmetic of
`up own_cpus=1 [leave_out=...]` for each scenario group of C 6.3, offline
(no namespace, no service, no lab).
python3 -m unittest scripts/test_own_layout.py"""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402

MESH = [name for name, role, *_ in lab.NODES if role != "backbone"]
TOTAL = 32

# C 6.3's scenario groups: the `up` settings of each (docs: C-operations
# 6.3.1) and the nodes that run with their own CPUs.
GROUPS = {
    # roll-edges and the London edge rows, scale and autoscale of edges:
    # London alone, lon-edge-4 the surge slot.
    "london-edges": (["own_cpus=1", "slots=lon-edge-4", "leave_out=newyork,lon-dist-c"],
                     ["contrib-lon", "lon-dist-a", "lon-dist-b", "lon-edge-1", "lon-edge-2",
                      "lon-edge-3", "lon-edge-4", "contrib-lon-b"]),
    # roll-forwarders, forwarder-kill-return, the gate refusals,
    # upgrade-bad-forwarder, sibling-dies; A's contributor and ingress rows.
    "forwarders": (["own_cpus=1", "leave_out=lon-edge-4,new-edge-3,lon-dist-c,new-dist-c"],
                   ["contrib-lon", "lon-dist-a", "lon-dist-b", "lon-edge-1", "lon-edge-2",
                    "lon-edge-3", "new-dist-a", "new-dist-b", "new-edge-1", "new-edge-2",
                    "contrib-lon-b"]),
    # replace-forwarder (through lon-dist-c) and autoscale-forwarders.
    "forwarder-slots": (["own_cpus=1", "leave_out=lon-edge-4,new-edge-3"],
                        ["contrib-lon", "lon-dist-a", "lon-dist-b", "lon-edge-1", "lon-edge-2",
                         "lon-edge-3", "lon-dist-c", "new-dist-a", "new-dist-b", "new-edge-1",
                         "new-edge-2", "new-dist-c", "contrib-lon-b"]),
    # upgrade-all and upgrade-back (roll all): every tier, the London surge
    # slot, New York's three edges.
    "all": (["own_cpus=1", "slots=lon-edge-4"],
            ["contrib-lon", "lon-dist-a", "lon-dist-b", "lon-edge-1", "lon-edge-2",
             "lon-edge-3", "lon-edge-4", "new-dist-a", "new-dist-b", "new-edge-1", "new-edge-2",
             "new-edge-3", "contrib-lon-b"]),
}


class OwnLayout(unittest.TestCase):
    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        for patcher in (mock.patch.object(lab, "WORK", self.work),
                        mock.patch.object(lab.os, "cpu_count", return_value=TOTAL)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def check_layout(self, sets: dict, nodes: list[str]) -> dict:
        """Every node its own two CPUs from 0, nothing shared, the sender
        the last CPU alone, the probes and the tools apart."""
        owned = [sets[name] for name in nodes]
        self.assertTrue(all(len(cpus) == lab.OWN_CPUS for cpus in owned))
        flat = [cpu for cpus in owned for cpu in cpus]
        self.assertEqual(flat, list(range(lab.OWN_CPUS * len(nodes))))
        roles = lab.rest_split(sets)
        self.assertEqual(roles["sender"], [TOTAL - 1])
        self.assertEqual(sets[lab.SOURCE_HOST], [TOTAL - 1])
        everything = flat + roles["probes"] + roles["tools"] + roles["sender"]
        self.assertEqual(sorted(everything), list(range(TOTAL)), "every CPU, once")
        self.assertTrue(roles["probes"] and roles["tools"])
        self.assertFalse(set(roles["probes"]) & set(roles["tools"]))
        return roles

    def test_each_scenario_group_fits_the_host(self):
        expected_rest = {"london-edges": ([16, 17, 18, 19, 20, 21, 22],
                                          [23, 24, 25, 26, 27, 28, 29, 30]),
                         "forwarders": ([22, 23, 24, 25], [26, 27, 28, 29, 30]),
                         "forwarder-slots": ([26, 27], [28, 29, 30]),
                         "all": ([26, 27], [28, 29, 30])}
        for group, (settings, nodes) in GROUPS.items():
            sets = lab.layout_of(settings, TOTAL)
            ran = [name for name in MESH if isinstance(sets.get(name), list)
                   and name != lab.SOURCE_HOST]
            self.assertEqual(ran, sorted(nodes, key=MESH.index), group)
            roles = self.check_layout(sets, ran)
            self.assertEqual((roles["probes"], roles["tools"]), expected_rest[group], group)

    def test_the_proof_topology_of_fourteen_with_both_slots_does_not_fit(self):
        # Every mesh node and both dist-c: 15 nodes, 30 CPUs, 2 left: not
        # the sender's one and a probe's and a tool's. Refused with what to
        # leave out.
        with self.assertRaises(lab.LabError) as caught:
            lab.layout_of(["own_cpus=1", "leave_out="], TOTAL)
        text = str(caught.exception)
        self.assertIn("15 nodes take 30 CPUs", text)
        self.assertIn("33 of the host's 32", text)
        self.assertIn("leave_out=", text)
        # Its suggestion fits.
        suggestion = text.rsplit("leave_out=", 1)[1]
        self.assertTrue(lab.layout_of(["own_cpus=1", f"leave_out={suggestion}"], TOTAL))

    def test_the_default_leaves_the_dist_c_slots_out(self):
        sets = lab.layout_of(["own_cpus=1"], TOTAL)
        self.assertEqual(sets["_left_out"], sorted(lab.DEFAULT_SLOTS))
        for slot in lab.DEFAULT_SLOTS:
            self.assertNotIn(slot, sets)
        self.check_layout(sets, [name for name in MESH if isinstance(sets.get(name), list)
                                 and name != lab.SOURCE_HOST])

    def test_a_site_leaves_out_every_node_of_it_and_the_core_stays(self):
        self.assertEqual(lab.leave_out_names("newyork"),
                         {"new-dist-a", "new-dist-b", "new-edge-1", "new-edge-2", "new-edge-3",
                          "new-dist-c"})
        for core in ("lon-src", "contrib-lon", "lon-dist-a", "lon-dist-b"):
            with self.assertRaises(lab.LabError):
                lab.leave_out_names(core)
        with self.assertRaises(lab.LabError):
            lab.leave_out_names("tokyo")

    def test_a_left_out_node_has_no_service_no_cpus_and_no_peer_names_it(self):
        lab.save("cpus.json", lab.layout_of(["own_cpus=1", "leave_out=new-edge-2,lon-dist-c,"
                                                           "new-dist-c"], TOTAL))
        lab.save("left-out.json", ["lon-dist-c", "new-dist-c", "new-edge-2"])
        with self.assertRaises(lab.LabError):
            lab.node_cpus("new-edge-2")
        with self.assertRaises(lab.LabError):
            lab.set_slot_in_service("lon-dist-c", True)
        self.assertNotIn("new-edge-2", lab.edges("newyork"))
        state = lab.lab_state()
        self.assertNotIn("new-edge-2", state["nodes"])
        self.assertNotIn("lon-dist-c", state["slots"])
        self.assertEqual(lab.node_cpus("new-edge-1"), lab.load("cpus.json")["new-edge-1"])

    def test_a_london_only_lab_renders_no_compiled_child(self):
        # The deploy's relay tree of the state without New York: the first
        # forwarders have no long-haul children, so no gate waits for one.
        lab.save("left-out.json", sorted(lab.leave_out_names("newyork") | {"lon-dist-c"}))
        tree = lab.deploy_module().relay_tree(lab.lab_state(), {"topology": "merged"})
        self.assertEqual([node["name"] for node in tree["first"]], ["lon-dist-a", "lon-dist-b"])
        self.assertEqual(tree["children"], [])

    def test_own_cpus_takes_no_other_layout_and_rt_keeps_two_a_node(self):
        sets = lab.layout_of(["own_cpus=1", "rt=1"], TOTAL)
        self.assertEqual(sets["_per_node"], lab.RT_CPUS)
        self.assertEqual(lab.pinned_limits(sets, {})["contrib-lon"]["cpu_percent"], 200)

    def test_the_layout_prints_its_map(self):
        lines = lab.layout_lines(lab.layout_of(GROUPS["forwarders"][0], TOTAL))
        self.assertIn("contrib-lon      cpus 0-1", lines)
        self.assertIn("sender           cpus 31 (lon-src)", lines)
        self.assertIn("probes           cpus 22-25", lines)
        self.assertTrue(lines[-1].startswith("left out: "))


class RestSplit(unittest.TestCase):
    def test_the_rest_of_each_layout_is_split_for_the_runner(self):
        # node_cpus=0: lon-src's own window is the sender's.
        self.assertEqual(lab.rest_split({"_first": 0, "lon-src": [26, 27],
                                         "_rest": [28, 29, 30, 31]}),
                         {"probes": [28, 29], "tools": [30, 31], "sender": [26, 27]})
        # A range under rt=1: "_source".
        self.assertEqual(lab.rest_split({"_range": [2, 15], "_source": [15],
                                         "_rest": [11, 12, 13, 14]}),
                         {"probes": [11, 12], "tools": [13, 14], "sender": [15]})
        # A range without: the last CPU of the rest when three or more.
        self.assertEqual(lab.rest_split({"_range": [2, 15], "_rest": [12, 13, 14, 15]}),
                         {"probes": [12], "tools": [13, 14], "sender": [15]})
        # Two CPUs: the sender shares the probes'.
        self.assertEqual(lab.rest_split({"_rest": [14, 15]}),
                         {"probes": [14], "tools": [15], "sender": [14]})
        self.assertEqual(lab.rest_split({}), {})


if __name__ == "__main__":
    unittest.main()
