#!/usr/bin/env python3
"""Tests of the lab tool's host separation (tools6, item 2), on a fake
Linode API: every group of a region's mesh nodes on one physical host is
reported; with --separate-hosts (the default) the later-created of two
forwarders of a region, or of an edge and either of its parents, is made
anew on another host, at most --separate-attempts times a node, its billed
hour printed and held to the lab's ceilings and budget, and recorded.
python3 -m unittest scripts/test_host_separation.py"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_lab_slots  # noqa: E402

lab_tool = test_lab_slots.lab_tool


class HostedApi(test_lab_slots.FakeApi):
    """The fake API with a physical host per instance: `hosts` {instance id:
    host}; a new instance takes the next of `next_hosts`, else one of its
    own."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.next_hosts: list[str] = []

    def call(self, method, path, body=None, allow_404=False):
        answer = super().call(method, path, body, allow_404)
        if method == "POST" and path == "/linode/instances":
            item = self.instances[answer["id"]]
            item["host_uuid"] = (self.next_hosts.pop(0) if self.next_hosts
                                 else f"host-new-{answer['id']}")
            return dict(item)
        return answer


class Separation(test_lab_slots.Base):
    def lab(self, shared: dict[str, str]) -> tuple[HostedApi, dict]:
        api = HostedApi()
        state = test_lab_slots.lab_state(self.tmp, api)
        for name, node in state["nodes"].items():
            api.instances[node["id"]]["host_uuid"] = shared.get(name, f"host-{node['id']}")
        return api, state

    def separate(self, api, verb="up", scope=None, **flags) -> tuple[str, str]:
        args = argparse.Namespace(**{**vars(self.args), "separate_hosts": True,
                                     "separate_attempts": 2, "budget_usd": None,
                                     "spent_before_usd": None, **flags})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            lab_tool.separate_after(args, api, pathlib.Path(self.args.state), verb, scope)
        return out.getvalue(), err.getvalue()

    def saved(self) -> dict:
        return json.loads(pathlib.Path(self.args.state).read_text())

    def test_cmpd8_s_new_york_host_is_reported_and_separated(self):
        """new-dist-a, new-dist-b and new-edge-1 on one host (cmpd8): the
        later-created forwarder and the edge are made anew; the report
        names the group; the new hosts differ, so it stops there."""
        api, state = self.lab({"new-dist-a": "h-ny", "new-dist-b": "h-ny", "new-edge-1": "h-ny"})
        old_ids = {name: state["nodes"][name]["id"] for name in ("new-dist-b", "new-edge-1")}
        out, _err = self.separate(api)
        self.assertIn("SHARED HOST in us-east: new-dist-a, new-dist-b, new-edge-1 on h-ny", out)
        self.assertIn("two forwarders of us-east share host h-ny: new-dist-b (made later)", out)
        self.assertIn("the edge new-edge-1 with its parent new-dist-a", out)
        self.assertIn("making new-dist-b, new-edge-1 anew on other hosts (each a new billed hour: "
                      "$0.108 in all)", out)
        saved = self.saved()
        for name, ident in old_ids.items():
            self.assertNotEqual(saved["nodes"][name]["id"], ident, name)
            self.assertTrue(saved["nodes"][name]["host_uuid"].startswith("host-new-"))
            self.assertNotIn(ident, api.instances)
        self.assertEqual(saved["nodes"]["new-dist-a"]["id"], state["nodes"]["new-dist-a"]["id"])
        record = saved["host_separation"]
        self.assertEqual(record["replaced"], {"new-dist-b": 1, "new-edge-1": 1})
        # One line a pair: the edge shared the host with both its parents.
        self.assertEqual(sorted((item["move"], item["keep"]) for item in record["log"]),
                         [("new-dist-b", "new-dist-a"), ("new-edge-1", "new-dist-a"),
                          ("new-edge-1", "new-dist-b")])
        self.assertTrue(all(item["verb"] == "up" for item in record["log"]))
        # Billing keeps the deleted instances (a whole hour each).
        self.assertEqual(sorted(item["name"] for item in saved["removed"]),
                         ["new-dist-b", "new-edge-1"])
        self.assertIn("every forwarder pair and every edge with its parents are on different "
                      "hosts", out)

    def test_the_bound_of_attempts(self):
        """Linode keeps putting the forwarder back on the same host: made
        anew twice, then left and named."""
        api, _state = self.lab({"lon-dist-a": "h-lon", "lon-dist-b": "h-lon"})
        api.next_hosts = ["h-lon"] * 5
        out, err = self.separate(api)
        self.assertEqual(self.saved()["host_separation"]["replaced"], {"lon-dist-b": 2})
        self.assertEqual(out.count("making lon-dist-b anew"), 2)
        self.assertIn("lon-dist-b made anew 2 time(s) already (--separate-attempts): left", err)

    def test_off_and_report_only_pairs(self):
        api, state = self.lab({"tok-dist-a": "h-t", "tok-dist-b": "h-t",
                               "lon-edge-1": "h-l", "lon-edge-2": "h-l"})
        out, err = self.separate(api, separate_hosts=False)
        self.assertIn("SHARED HOST in gb-lon: lon-edge-1, lon-edge-2 on h-l", out)
        self.assertIn("host separation off", err)
        self.assertNotIn("host_separation", self.saved())
        self.assertEqual(self.saved()["nodes"]["tok-dist-b"]["id"], state["nodes"]["tok-dist-b"]["id"])
        # Two edges on one host are reported, never moved.
        out, _err = self.separate(api)
        self.assertEqual(self.saved()["host_separation"]["replaced"], {"tok-dist-b": 1})

    def test_grow_and_replace_move_only_their_own_nodes(self):
        api, state = self.lab({"syd-dist-a": "h-s", "syd-dist-b": "h-s"})
        _out, err = self.separate(api, verb="replace", scope={"syd-edge-1"})
        self.assertIn("syd-dist-b not made by this replace: left where they are", err)
        self.assertEqual(self.saved()["nodes"]["syd-dist-b"]["id"],
                         state["nodes"]["syd-dist-b"]["id"])

    def test_the_lab_s_ceilings_and_budget_hold(self):
        api, state = self.lab({"new-dist-a": "h-ny", "new-dist-b": "h-ny"})
        saved = self.saved()
        saved["budget_usd"] = 0.01
        pathlib.Path(self.args.state).write_text(json.dumps(saved))
        with self.assertRaises(SystemExit) as caught, redirect_stdout(io.StringIO()):
            lab_tool.separate_after(argparse.Namespace(**{**vars(self.args),
                                                          "separate_hosts": True}),
                                    api, pathlib.Path(self.args.state), "up")
        self.assertIn("up (host separation) refused: over the budget", str(caught.exception))
        self.assertIn(state["nodes"]["new-dist-b"]["id"], api.instances)

    def test_up_and_grow_run_it_and_the_flags_parse(self):
        parser = lab_tool.build_parser()
        parsed = parser.parse_args(["up", "--watchdog-hours", "1", "--budget-usd", "5",
                                    "--no-separate-hosts", "--separate-attempts", "1"])
        self.assertFalse(parsed.separate_hosts)
        self.assertEqual(parsed.separate_attempts, 1)
        self.assertTrue(parser.parse_args(["grow", "--topology", "x"]).separate_hosts)
        self.assertTrue(parser.parse_args(["replace", "a", "--mode", "new"]).separate_hosts)
        source = pathlib.Path(lab_tool.__file__).read_text()
        self.assertIn('separate_after(args, api, state_path, "up")', source)
        self.assertIn('separate_after(args, api, state_path, "grow"', source)
        self.assertIn('separate_after(args, api, state_path, "replace"', source)


class Pairs(unittest.TestCase):
    def test_pairs_and_groups(self):
        nodes = {
            "lon-dist-a": {"name": "lon-dist-a", "role": "distributor", "site": "london",
                           "region": "gb-lon", "vlan_ipv4": "10.0.0.11", "id": 1,
                           "host_uuid": "h1"},
            "lon-dist-b": {"name": "lon-dist-b", "role": "distributor", "site": "london",
                           "region": "gb-lon", "vlan_ipv4": "10.0.0.12", "id": 2,
                           "host_uuid": "h2"},
            "lon-dist-c": {"name": "lon-dist-c", "role": "distributor", "site": "london",
                           "region": "gb-lon", "vlan_ipv4": "10.0.0.13", "id": 9, "slot": True,
                           "host_uuid": "h3"},
            "lon-edge-1": {"name": "lon-edge-1", "role": "edge", "site": "london",
                           "region": "gb-lon", "vlan_ipv4": "10.0.0.21", "id": 3,
                           "host_uuid": "h3"},
            "lon-view": {"name": "lon-view", "role": "load", "site": "london",
                         "region": "gb-lon", "id": 4, "host_uuid": "h2"}}
        # An edge with a slot forwarder (not its parent) and a load host
        # with a forwarder: reported as shared, not a pair to separate.
        self.assertEqual(lab_tool.separation_pairs(nodes), [])
        self.assertEqual(lab_tool.shared_host_groups(nodes),
                         [("gb-lon", "h3", ["lon-dist-c", "lon-edge-1"])])
        nodes["lon-edge-1"]["host_uuid"] = "h2"
        self.assertEqual(lab_tool.separation_pairs(nodes),
                         [{"keep": "lon-dist-b", "move": "lon-edge-1", "host": "h2",
                           "why": "the edge lon-edge-1 with its parent lon-dist-b"}])
        # The later-created moves, whatever the role.
        nodes["lon-dist-b"]["created_unix"] = time.time()
        nodes["lon-edge-1"]["created_unix"] = time.time() - 60
        self.assertEqual(lab_tool.separation_pairs(nodes)[0]["move"], "lon-dist-b")


if __name__ == "__main__":
    unittest.main()
