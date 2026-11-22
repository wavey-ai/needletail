#!/usr/bin/env python3
"""Tests of the lab tool's autoscale slots, offline (a fake Linode API, no
token, nothing created): `up` creates every node but the slots and records
them; `add` creates only a slot of this lab, refuses a shared plan, a name
that is not a slot, a lab without an armed watchdog, the node and hourly
ceilings and a budget that the projection to the watchdog would pass;
`remove` deletes only a recorded node whose live instance carries the run's
tag and label, never the dev box or an untagged instance; `billed` counts
every instance a whole hour at least, removed ones included.
python3 -m unittest scripts/test_lab_slots.py"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import pathlib
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("lab_slots", HERE / "linode-small-mesh-lab.py")
lab_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab_tool)

SCALE = HERE.parent / "deploy" / "low-latency" / "topology-scale.json"
TYPES = {"g6-dedicated-2": {"id": "g6-dedicated-2", "class": "dedicated",
                            "price": {"hourly": 0.054}, "region_prices": []},
         "g6-dedicated-4": {"id": "g6-dedicated-4", "class": "dedicated",
                            "price": {"hourly": 0.108}, "region_prices": []},
         "g6-dedicated-8": {"id": "g6-dedicated-8", "class": "dedicated",
                            "price": {"hourly": 0.216}, "region_prices": []},
         "g6-standard-2": {"id": "g6-standard-2", "class": "standard",
                           "price": {"hourly": 0.036}, "region_prices": []},
         "g6-nanode-1": {"id": "g6-nanode-1", "class": "nanode",
                         "price": {"hourly": 0.0075}, "region_prices": []}}
REGIONS = {region: {"id": region, "status": "ok", "capabilities": ["Vlans"]}
           for region in ("gb-lon", "us-east", "ap-northeast", "ap-southeast")}
SLOTS = ["lon-dist-c", "lon-edge-11", "lon-edge-12", "new-dist-c"]


class FakeApi:
    """The Linode API as far as the lab tool uses it, in memory. The dev
    box (arch-us-east, no tags) is one of its instances."""

    def __init__(self, tag: str = "needletail-small-mesh-t1"):
        self.calls: list[tuple] = []
        self.next_id = 5000
        self.instances = {1: {"id": 1, "label": "arch-us-east", "tags": [],
                              "type": "g6-dedicated-32", "status": "running",
                              "created": "2026-11-18T09:00:00", "ipv4": ["198.74.56.161"]}}
        self.tag = tag

    def list_all(self, path, tag=None):
        if "types" in path:
            return list(TYPES.values())
        if "regions" in path:
            return list(REGIONS.values())
        if "instances" in path:
            items = list(self.instances.values())
            return [item for item in items if tag is None or tag in item.get("tags", [])]
        return []

    def call(self, method, path, body=None, allow_404=False):
        self.calls.append((method, path))
        if method == "POST" and path == "/linode/instances":
            self.next_id += 1
            item = {"id": self.next_id, "label": body["label"], "tags": body["tags"],
                    "type": body["type"], "status": "running",
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                    "ipv4": [f"192.0.2.{self.next_id % 250}"]}
            self.instances[item["id"]] = item
            return item
        if path.startswith("/linode/instances/"):
            ident = int(path.rsplit("/", 1)[1])
            if method == "DELETE":
                if ident == 1:
                    raise AssertionError("the dev box was deleted")
                self.instances.pop(ident, None)
                return {}
            return dict(self.instances.get(ident, {}))
        return {"id": 77}


def lab_state(tmp: pathlib.Path, api: FakeApi, watchdog: float | None = 3 * 3600) -> dict:
    """A lab of the scale topology as `up` leaves it: every node but the
    slots, created an hour and a half ago, tagged and labelled."""
    topology = json.loads(SCALE.read_text())
    created = time.time() - 1.5 * 3600
    state = {"run_id": "t1", "tag": api.tag, "image": "linode/debian13",
             "topology_file": str(SCALE), "created_unix": int(created), "hourly_usd": 0.0,
             "firewall_id": 9, "operator_ipv4": "203.0.113.9", "nodes": {},
             "slots": lab_tool.slot_specs(topology), "removed": [], "sites_left_out": []}
    for node in lab_tool.created_part(topology)["nodes"]:
        api.next_id += 1
        label = f"ntsm-t1-{node['name']}"
        api.instances[api.next_id] = {"id": api.next_id, "label": label,
                                      "tags": ["needletail-qualification", api.tag],
                                      "type": node["type"], "status": "running"}
        state["nodes"][node["name"]] = {
            "id": api.next_id, "name": node["name"], "label": label, "role": node["role"],
            "site": node["site"], "region": node["region"], "type": node["type"],
            "hourly_usd": TYPES[node["type"]]["price"]["hourly"],
            "public_ipv4": f"198.51.100.{api.next_id % 250}",
            "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None,
            "created_unix": int(created)}
    state["hourly_usd"] = round(sum(node["hourly_usd"] for node in state["nodes"].values()), 4)
    if watchdog is not None:
        state["watchdog_deadline_unix"] = int(time.time() + watchdog)
    path = tmp / "state.json"
    path.write_text(json.dumps(state))
    return state


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        key = self.tmp / "key.pub"
        key.write_text("ssh-ed25519 AAAA test")
        self.args = argparse.Namespace(state=str(self.tmp / "state.json"),
                                       ssh_public_key=str(key))
        for name, value in (("MAX_NODES", 103), ("MAX_HOURLY_USD", 10.7)):
            patcher = mock.patch.object(lab_tool, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(lab_tool.time, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, api, state, names, **kwargs):
        with redirect_stdout(io.StringIO()):
            return lab_tool.add_slots(self.args, api, state, names, **kwargs)

    def refused(self, function, *args, **kwargs) -> str:
        with self.assertRaises(SystemExit) as caught, redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            function(*args, **kwargs)
        return str(caught.exception)


class Up(Base):
    def test_up_creates_every_node_but_the_slots_and_records_them(self):
        api = FakeApi()
        created = []

        def create(api_, run_id, tag, image, key, node):
            created.append(node["name"])
            ident = 100 + len(created)
            api.instances[ident] = {"id": ident, "status": "running", "tags": [tag]}
            return {"id": ident, "name": node["name"], "label": node["name"],
                    "role": node["role"], "site": node["site"], "region": node["region"],
                    "type": node["type"], "hourly_usd": node["_hourly"],
                    "public_ipv4": "192.0.2.1", "vlan_ipv4": None, "created": "x"}

        args = argparse.Namespace(state=self.args.state, topology=str(SCALE), run_id="t1",
                                  ssh_public_key=self.args.ssh_public_key, without_sites="",
                                  optional_sites="", watchdog_hours=None)
        with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                mock.patch.object(lab_tool, "MAX_NODES", 99), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 10.5), \
                mock.patch.object(lab_tool, "create_node", create), \
                mock.patch.object(lab_tool, "operator_ipv4", lambda: "203.0.113.9"), \
                mock.patch.object(lab_tool, "command_status", lambda args: None), \
                redirect_stdout(io.StringIO()):
            lab_tool.command_up(args)
        self.assertEqual(len(created), 99)
        self.assertFalse(set(created) & set(SLOTS))
        state = json.loads(pathlib.Path(self.args.state).read_text())
        self.assertEqual(sorted(state["slots"]), SLOTS)
        self.assertEqual(state["slots"]["lon-edge-11"]["vlan_ipv4"], "10.80.1.32")
        self.assertEqual(state["removed"], [])


class Add(Base):
    def test_add_creates_a_slot_tagged_on_its_vlan_and_refreshes_the_firewall(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        self.add(api, state, ["lon-edge-11"])
        node = state["nodes"]["lon-edge-11"]
        live = api.instances[node["id"]]
        self.assertIn(api.tag, live["tags"])
        self.assertIn("needletail-qualification", live["tags"])
        self.assertEqual(node["vlan_ipv4"], "10.80.1.32")
        self.assertEqual(node["type"], "g6-dedicated-2")
        self.assertTrue(node["slot"])
        self.assertIn(("PUT", "/networking/firewalls/9/rules"), api.calls)
        self.assertIn(("POST", "/networking/firewalls/9/devices"), api.calls)
        on_disk = json.loads(pathlib.Path(self.args.state).read_text())
        self.assertIn("lon-edge-11", on_disk["nodes"])
        self.assertAlmostEqual(on_disk["hourly_usd"], 10.422 + 0.054, places=3)

    def test_add_refuses_what_is_not_a_slot_of_this_lab(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        for names in (["lon-edge-3"], ["arch-us-east"], ["lon-edge-13"], ["lon-load-1"],
                      ["lon-edge-11", "lon-edge-11"], []):
            before = len(api.instances)
            message = self.refused(self.add, api, state, names)
            self.assertIn("add", message, names)
            self.assertEqual(len(api.instances), before, names)
        self.add(api, state, ["lon-edge-12"])
        self.assertIn("exists already", self.refused(self.add, api, state, ["lon-edge-12"]))

    def test_add_refuses_a_shared_plan(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        for shared in ("g6-standard-2", "g6-nanode-1"):
            state["slots"]["lon-edge-11"]["type"] = shared
            message = self.refused(self.add, api, state, ["lon-edge-11"])
            self.assertIn("not a dedicated-CPU plan", message)
        self.assertNotIn(("POST", "/linode/instances"), api.calls)

    def test_add_refuses_without_a_watchdog(self):
        api = FakeApi()
        state = lab_state(self.tmp, api, watchdog=None)
        self.assertIn("watchdog", self.refused(self.add, api, state, ["lon-edge-11"]))
        state["watchdog_deadline_unix"] = int(time.time() - 10)
        self.assertIn("watchdog", self.refused(self.add, api, state, ["lon-edge-11"]))
        self.assertNotIn(("POST", "/linode/instances"), api.calls)

    def test_add_keeps_the_node_and_hourly_ceilings(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        with mock.patch.object(lab_tool, "MAX_NODES", 100):
            self.assertIn("ceiling is 100",
                          self.refused(self.add, api, state, ["lon-edge-11", "lon-edge-12"]))
        with mock.patch.object(lab_tool, "MAX_HOURLY_USD", 10.45):
            self.assertIn("NEEDLETAIL_LINODE_MAX_HOURLY_USD",
                          self.refused(self.add, api, state, ["lon-edge-11"]))
        self.assertNotIn(("POST", "/linode/instances"), api.calls)

    def test_add_refuses_over_the_budget(self):
        api = FakeApi()
        # 1.5 h old, 3 h of watchdog left: every live node billed 5 hours.
        state = lab_state(self.tmp, api, watchdog=3 * 3600)
        ok, figures = lab_tool.budget_check(state, [{"hourly_usd": 0.054}], 100.0)
        self.assertAlmostEqual(figures["lab_projected_usd"], 10.422 * 5 + 0.054 * 3, places=3)
        self.assertTrue(ok)
        message = self.refused(self.add, api, state, ["lon-edge-11"], budget_usd=100.0,
                               spent_before_usd=40.0, other_labs_usd=8.0)
        self.assertIn("over the budget", message)
        self.assertNotIn(("POST", "/linode/instances"), api.calls)
        self.add(api, state, ["lon-edge-11"], budget_usd=100.0, spent_before_usd=40.0)

    def test_a_slot_made_again_gets_a_new_label(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        self.add(api, state, ["lon-edge-11"])
        with redirect_stdout(io.StringIO()):
            lab_tool.remove_nodes(api, pathlib.Path(self.args.state), state, ["lon-edge-11"])
        self.add(api, state, ["lon-edge-11"])
        self.assertTrue(state["nodes"]["lon-edge-11"]["label"].endswith("lon-edge-11a1"))


class Remove(Base):
    def test_remove_deletes_by_recorded_id_and_billing_keeps_it(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        self.add(api, state, ["lon-dist-c"])
        ident = state["nodes"]["lon-dist-c"]["id"]
        with redirect_stdout(io.StringIO()):
            lab_tool.remove_nodes(api, pathlib.Path(self.args.state), state, ["lon-dist-c"])
        self.assertIn(("DELETE", f"/linode/instances/{ident}"), api.calls)
        self.assertNotIn("lon-dist-c", state["nodes"])
        self.assertEqual([item["name"] for item in state["removed"]], ["lon-dist-c"])
        self.assertAlmostEqual(state["hourly_usd"], 10.422, places=3)
        # Its hour is still billed.
        self.assertEqual(lab_tool.billed(state)["removed"], 1)
        self.assertIn(("PUT", "/networking/firewalls/9/rules"), api.calls)
        on_disk = json.loads(pathlib.Path(self.args.state).read_text())
        self.assertEqual(len(on_disk["removed"]), 1)

    def test_remove_refuses_anything_not_a_tagged_slot_of_this_lab(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        path = pathlib.Path(self.args.state)
        # Not in the state: the dev box, an unknown name.
        for names in (["arch-us-east"], ["nope"], []):
            self.refused(lab_tool.remove_nodes, api, path, state, names)
        # A lab node that is not a slot, unless asked for.
        self.assertIn("not an autoscale slot",
                      self.refused(lab_tool.remove_nodes, api, path, state, ["lon-edge-1"]))
        self.add(api, state, ["lon-edge-11"])
        ident = state["nodes"]["lon-edge-11"]["id"]
        # The recorded id now names an instance without the run's tag.
        api.instances[ident]["tags"] = []
        self.assertIn("does not carry",
                      self.refused(lab_tool.remove_nodes, api, path, state, ["lon-edge-11"]))
        api.instances[ident]["tags"] = ["needletail-qualification", api.tag]
        api.instances[ident]["label"] = "arch-us-east"
        self.assertIn("labelled",
                      self.refused(lab_tool.remove_nodes, api, path, state, ["lon-edge-11"]))
        # Nothing was deleted by any refusal, and a refusal of one name
        # deletes none of the others.
        self.assertFalse([call for call in api.calls if call[0] == "DELETE"])
        state["nodes"]["lon-edge-11"]["id"] = 1  # the dev box's id
        self.refused(lab_tool.remove_nodes, api, path, state, ["lon-edge-11"])
        self.assertIn(1, api.instances)

    def test_an_instance_already_gone_is_recorded_not_deleted_again(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        self.add(api, state, ["new-dist-c"])
        ident = state["nodes"]["new-dist-c"]["id"]
        api.instances.pop(ident)
        with redirect_stdout(io.StringIO()):
            lab_tool.remove_nodes(api, pathlib.Path(self.args.state), state, ["new-dist-c"])
        self.assertNotIn(("DELETE", f"/linode/instances/{ident}"), api.calls)
        self.assertEqual(state["removed"][0]["name"], "new-dist-c")


class Billing(unittest.TestCase):
    def test_every_instance_bills_a_whole_hour_at_least(self):
        self.assertEqual(lab_tool.billed_hours(0, 0), 1)
        self.assertEqual(lab_tool.billed_hours(0, 60), 1)
        self.assertEqual(lab_tool.billed_hours(0, 3600), 1)
        self.assertEqual(lab_tool.billed_hours(0, 3601), 2)
        self.assertEqual(lab_tool.billed_hours(0, 5.83 * 3600), 6)
        self.assertEqual(lab_tool.billed_hours(0, 6.0 * 3600), 6)
        self.assertEqual(lab_tool.billed_hours(0, 6.2 * 3600), 7)

    def test_billed_keeps_removed_instances_and_projects_to_a_deadline(self):
        now = 1_000_000.0
        state = {"created_unix": now - 7200, "nodes": {
            "a": {"hourly_usd": 0.054, "created_unix": now - 7200},
            "b": {"hourly_usd": 0.108, "created": "1970-01-12T13:26:40"}},  # now - 600 s
            "removed": [{"name": "c", "hourly_usd": 0.054, "created_unix": now - 3000,
                         "deleted_unix": now - 2900},
                        {"name": "d", "hourly_usd": 0.216, "created_unix": now - 9000,
                         "deleted_unix": now - 1000}]}
        result = lab_tool.billed(state, now)
        # a: 2 h, b: 1 h, c: 1 h (100 s), d: 3 h (8,000 s).
        self.assertEqual(result["instance_hours"], 2 + 1 + 1 + 3)
        self.assertAlmostEqual(result["usd"], 2 * 0.054 + 0.108 + 0.054 + 3 * 0.216)
        # Kept to a deadline 2.5 h on: a 4.5 h -> 5, b 2.67 h -> 3; and a
        # new one created now, 2.5 h -> 3.
        projected = lab_tool.billed(state, now, now + 2.5 * 3600, [{"hourly_usd": 0.054}])
        self.assertAlmostEqual(projected["usd"],
                               5 * 0.054 + 3 * 0.108 + 0.054 + 3 * 0.216 + 3 * 0.054)

    def test_whole_lab_under_hourly_rounding(self):
        # The critique's figures: 5.83 h bills 6 h for every instance;
        # 6.2 h bills 7.
        state = {"created_unix": 0, "nodes": {str(i): {"hourly_usd": 10.422 / 99,
                                                        "created_unix": 0} for i in range(99)}}
        self.assertAlmostEqual(lab_tool.billed(state, 5.83 * 3600)["usd"], 62.532, places=2)
        self.assertAlmostEqual(lab_tool.billed(state, 6.2 * 3600)["usd"], 72.954, places=2)

    def test_account_billed_counts_other_labs_never_the_dev_box(self):
        api = FakeApi()
        api.instances[2] = {"id": 2, "label": "ntsm-other-x", "type": "g6-dedicated-4",
                            "tags": ["needletail-qualification", "needletail-small-mesh-other"],
                            "created": "2026-11-22T00:00:00"}
        api.instances[3] = {"id": 3, "label": "ntsm-t1-y", "type": "g6-dedicated-2",
                            "tags": ["needletail-qualification", api.tag],
                            "created": "2026-11-22T00:00:00"}
        now = lab_tool.created_unix({"created": "2026-11-22T02:30:00"})
        result = lab_tool.account_billed(api, api.tag, now)
        self.assertEqual([row["label"] for row in result["instances"]], ["ntsm-other-x"])
        self.assertAlmostEqual(result["usd"], 3 * 0.108)


if __name__ == "__main__":
    unittest.main()
