#!/usr/bin/env python3
"""Tests of the lab tool, offline (a fake Linode API, a fake user systemd,
the ledger, the unit directory and known_hosts in a temporary directory: no
token is read, nothing is created, no unit is started).

`up` is refused without a watchdog and a budget, and the budget holds every
node and slot of the topology for the watchdog's whole hours, what the
ledger says was spent and the other live labs to their deadline tags; the
ceilings that `up` recorded hold from another shell. The watchdog is a user
unit that every command checks and arms again. The ledger is reconciled
from the account's events. `replace` rebuilds in place (the id and both
addresses kept, the interfaces put in place before one boot) or makes a
node anew. `add` creates only a slot of this lab, refuses a shared plan, a
name that is not a slot, a lab without an armed and active watchdog, the
node and hourly ceilings and a budget that the projection to the watchdog
would pass; `remove` deletes only a recorded node whose live instance
carries the run's tag and label, never the dev box or an untagged instance;
`billed` counts every instance a whole hour at least, removed ones included.
python3 -m unittest scripts/test_lab_slots.py"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import pathlib
import shutil
import subprocess
import sys
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
UNIT = "needletail-watchdog-t1.service"
# What the fake token file holds: it must appear in no file the tool writes.
TOKEN = "fake-token-never-written"


def stamp(unix: float) -> str:
    """A time as the API writes it (UTC, no zone)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(unix))


class FakeApi:
    """The Linode API as far as the lab tool uses it, in memory. The dev
    box (arch-us-east, no tags) is one of its instances. An instance has a
    config profile with its interfaces; a rebuild shows `rebuilding` for
    one read and then `offline` (`running`, with `rebuild_boots`: the API
    booted it all the same), and (with `rebuild_drops_vlan`) leaves the
    config with the public interface only."""

    def __init__(self, tag: str = "needletail-small-mesh-t1"):
        self.calls: list[tuple] = []
        self.bodies: list[tuple] = []
        self.next_id = 5000
        self.instances = {1: {"id": 1, "label": "arch-us-east", "tags": [],
                              "type": "g6-dedicated-32", "status": "running",
                              "created": "2026-11-18T09:00:00", "ipv4": ["198.74.56.161"]}}
        self.configs: dict[int, list[dict]] = {}
        self.devices: list[dict] = []
        self.firewalls: dict[int, dict] = {}
        self.event_pages: list[list[dict]] = [[]]
        self.rebuild_drops_vlan = False
        self.rebuild_boots = False
        self.tag = tag

    def list_all(self, path, tag=None):
        if "types" in path:
            return list(TYPES.values())
        if "regions" in path:
            return list(REGIONS.values())
        if path.endswith("/configs"):
            return [dict(item) for item in self.configs.get(int(path.split("/")[3]), [])]
        if path.endswith("/devices"):
            return list(self.devices)
        if path == "/networking/firewalls":
            return [item for item in self.firewalls.values()
                    if tag is None or tag in item.get("tags", [])]
        if "instances" in path:
            items = list(self.instances.values())
            return [item for item in items if tag is None or tag in item.get("tags", [])]
        return []

    def call(self, method, path, body=None, allow_404=False):
        self.calls.append((method, path))
        self.bodies.append((method, path, body))
        if path.startswith("/account/events"):
            page = int(path.split("page=")[1].split("&")[0])
            return {"data": self.event_pages[page - 1], "pages": len(self.event_pages)}
        if method == "POST" and path == "/linode/instances":
            self.next_id += 1
            item = {"id": self.next_id, "label": body["label"], "tags": body["tags"],
                    "type": body["type"], "status": "running", "created": stamp(time.time()),
                    "ipv4": [f"192.0.2.{self.next_id % 250}"]}
            self.instances[item["id"]] = item
            self.configs[item["id"]] = [{"id": 700 + item["id"],
                                         "interfaces": body["interfaces"]}]
            return item
        if method == "DELETE" and path.startswith("/networking/firewalls/"):
            self.firewalls.pop(int(path.split("/")[3]), None)
            return {}
        if method == "POST" and path.endswith("/devices"):
            self.devices.append({"id": len(self.devices) + 1,
                                 "entity": {"id": body["id"], "type": body["type"]}})
            return {}
        if path.startswith("/linode/instances/"):
            parts = path.split("/")
            ident = int(parts[3])
            item = self.instances.get(ident)
            if len(parts) == 4:
                if method == "DELETE":
                    if ident == 1:
                        raise AssertionError("the dev box was deleted")
                    self.instances.pop(ident, None)
                    return {}
                if method == "PUT":
                    item.update(body)
                    return dict(item)
                if item and item["status"] == "rebuilding":
                    item["status"] = "running" if self.rebuild_boots else "offline"
                    return {**item, "status": "rebuilding"}
                return dict(item or {})
            if ident == 1:
                raise AssertionError(f"the dev box was touched: {method} {path}")
            if parts[4] == "rebuild":
                item["status"] = "rebuilding"
                if self.rebuild_drops_vlan:
                    self.configs[ident] = [{"id": 700 + ident,
                                            "interfaces": [{"purpose": "public"}]}]
                return dict(item)
            if parts[4] == "configs":
                for config in self.configs[ident]:
                    if config["id"] == int(parts[5]):
                        config.update(body)
                return {}
            if parts[4] == "boot":
                item["status"] = "running"
                return {}
            if parts[4] == "shutdown":
                item["status"] = "offline"
                return {}
        return {"id": 77}


class FakeSystemd:
    """The user's systemd as run_user calls it: units that are enabled,
    started and stopped, and lingering."""

    def __init__(self):
        self.active: set[str] = set()
        self.enabled: set[str] = set()
        self.linger = "yes"
        self.commands: list[list[str]] = []

    def __call__(self, command):
        self.commands.append(list(command))
        if command[0] == "loginctl":
            return subprocess.CompletedProcess(command, 0, self.linger + "\n", "")
        verb, unit = command[2], (command[3] if len(command) > 3 else None)
        if verb == "is-active":
            active = unit in self.active
            return subprocess.CompletedProcess(command, 0 if active else 3,
                                               "active\n" if active else "inactive\n", "")
        if verb in ("restart", "start"):
            self.active.add(unit)
        elif verb == "stop":
            self.active.discard(unit)
        elif verb == "enable":
            self.enabled.add(unit)
        elif verb == "disable":
            self.enabled.discard(unit)
        return subprocess.CompletedProcess(command, 0, "", "")


def lab_state(tmp: pathlib.Path, api: FakeApi, watchdog: float | None = 3 * 3600,
              without: tuple[str, ...] = ()) -> dict:
    """A lab of the scale topology as `up` leaves it: every node but the
    slots (and the sites `without`), created an hour and a half ago, tagged,
    labelled and in the lab firewall, with the budget and the ceilings that
    `up` records."""
    topology = lab_tool.without_sites(json.loads(SCALE.read_text()), list(without))
    created = time.time() - 1.5 * 3600
    state = {"run_id": "t1", "tag": api.tag, "image": "linode/debian13",
             "topology_file": str(SCALE), "created_unix": int(created), "hourly_usd": 0.0,
             "firewall_id": 9, "operator_ipv4": "203.0.113.9", "nodes": {},
             "budget_usd": 300.0, "spent_before_usd": 0.0,
             "max_nodes": 103, "max_hourly_usd": 10.7,
             "slots": lab_tool.slot_specs(topology), "removed": [],
             "sites_left_out": list(without)}
    for node in lab_tool.created_part(topology)["nodes"]:
        api.next_id += 1
        label = f"ntsm-t1-{node['name']}"
        public = f"198.51.100.{api.next_id % 250}"
        api.instances[api.next_id] = {"id": api.next_id, "label": label,
                                      "tags": ["needletail-qualification", api.tag],
                                      "type": node["type"], "status": "running",
                                      "ipv4": [public]}
        api.configs[api.next_id] = [{"id": 700 + api.next_id,
                                     "interfaces": lab_tool.node_interfaces("t1", node)}]
        api.devices.append({"id": len(api.devices) + 1,
                            "entity": {"id": api.next_id, "type": "linode"}})
        state["nodes"][node["name"]] = {
            "id": api.next_id, "name": node["name"], "label": label, "role": node["role"],
            "site": node["site"], "region": node["region"], "type": node["type"],
            "hourly_usd": TYPES[node["type"]]["price"]["hourly"],
            "public_ipv4": public,
            "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None,
            "created_unix": int(created)}
    state["hourly_usd"] = round(sum(node["hourly_usd"] for node in state["nodes"].values()), 4)
    if watchdog is not None:
        state["watchdog_deadline_unix"] = int(time.time() + watchdog)
    path = tmp / "state.json"
    path.write_text(json.dumps(state))
    return state


def no_api(public=False):
    raise AssertionError("a test reached the real Linode API")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        key = self.tmp / "key.pub"
        key.write_text("ssh-ed25519 AAAA test")
        token = self.tmp / "LINODE_PAT"
        token.write_text(TOKEN)
        self.args = argparse.Namespace(state=str(self.tmp / "state.json"),
                                       ssh_public_key=str(key))
        self.systemd = FakeSystemd()
        # The lab of lab_state has its watchdog running.
        self.systemd.active.add(UNIT)
        for name, value in (("MAX_NODES", 103), ("MAX_HOURLY_USD", 10.7),
                            ("run_user", self.systemd), ("Api", no_api),
                            ("WATCHDOG_UNIT_DIR", self.tmp / "units"),
                            ("LEDGER", self.tmp / "ledger.jsonl"),
                            ("KNOWN_HOSTS", self.tmp / "known_hosts"),
                            ("TOKEN_FILE", token),
                            ("operator_ipv4", lambda: "203.0.113.9")):
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

    def up_args(self, **kwargs) -> argparse.Namespace:
        values = {"state": self.args.state, "topology": str(SCALE), "run_id": "t1",
                  "ssh_public_key": self.args.ssh_public_key, "without_sites": "",
                  "optional_sites": "", "watchdog_hours": 2.5, "budget_usd": 300.0,
                  "spent_before_usd": None, "listener_batches": 2}
        values.update(kwargs)
        return argparse.Namespace(**values)

    def up(self, api, **kwargs) -> dict:
        with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            lab_tool.command_up(self.up_args(**kwargs))
        return json.loads(pathlib.Path(self.args.state).read_text())

    def ledger(self, *lines: dict) -> None:
        lab_tool.LEDGER.write_text("".join(json.dumps(line) + "\n" for line in lines))

    def replace_args(self, names, mode="rebuild", **kwargs) -> argparse.Namespace:
        return argparse.Namespace(state=self.args.state, names=list(names), mode=mode,
                                  ssh_public_key=self.args.ssh_public_key,
                                  budget_usd=kwargs.get("budget_usd"),
                                  spent_before_usd=kwargs.get("spent_before_usd"))

    def replace(self, api, names, mode="rebuild", **kwargs) -> None:
        with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                redirect_stdout(io.StringIO()):
            lab_tool.command_replace(self.replace_args(names, mode, **kwargs))


class Up(Base):
    def test_up_creates_every_node_but_the_slots_and_records_them(self):
        api = FakeApi()
        self.systemd.active.clear()
        created = []

        def create(api_, run_id, tag, image, key, node):
            created.append(node["name"])
            ident = 100 + len(created)
            api.instances[ident] = {"id": ident, "status": "running", "tags": [tag]}
            return {"id": ident, "name": node["name"], "label": node["name"],
                    "role": node["role"], "site": node["site"], "region": node["region"],
                    "type": node["type"], "hourly_usd": node["_hourly"],
                    "public_ipv4": "192.0.2.1", "vlan_ipv4": None, "created": "x"}

        with mock.patch.object(lab_tool, "MAX_NODES", 99), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 10.5), \
                mock.patch.object(lab_tool, "create_node", create), \
                mock.patch.object(lab_tool, "command_status", lambda args: None):
            self.up(api, spent_before_usd=0.0)
        self.assertEqual(len(created), 99)
        self.assertFalse(set(created) & set(SLOTS))
        state = json.loads(pathlib.Path(self.args.state).read_text())
        self.assertEqual(sorted(state["slots"]), SLOTS)
        self.assertEqual(state["slots"]["lon-edge-11"]["vlan_ipv4"], "10.80.1.32")
        self.assertEqual(state["removed"], [])


class UpBudget(Base):
    """`up` refuses before anything exists unless a watchdog and a budget
    are given and the budget holds the whole work."""

    def test_up_refuses_without_a_watchdog_or_a_budget(self):
        api = FakeApi()
        self.systemd.active.clear()
        self.ledger({"run_id": "before-23-nov", "billed_usd": 38.0, "removed_unix": 1})
        # From the command line: both flags are required.
        for argv in (["up", "--budget-usd", "300"], ["up", "--watchdog-hours", "2"]):
            with mock.patch.object(sys, "argv", ["lab", "--state", self.args.state] + argv), \
                    redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                lab_tool.main()
            self.assertIn("required", err.getvalue())
        refusals = {
            "required": [{"watchdog_hours": None}, {"budget_usd": None}],
            "must be above 0 and at most 6": [{"watchdog_hours": 0}, {"watchdog_hours": 6.5}],
            "run id": [{"run_id": "T-1"}, {"run_id": "x" * 25}],
        }
        with mock.patch.object(lab_tool, "Api", lambda public=False: api):
            for why, cases in refusals.items():
                for case in cases:
                    self.assertIn(why, self.refused(lab_tool.command_up, self.up_args(**case)))
            # A host that cannot keep the unit: no token file for it to read,
            # or a user who does not linger.
            with mock.patch.object(lab_tool, "TOKEN_FILE", self.tmp / "missing"):
                self.assertIn("token", self.refused(lab_tool.command_up, self.up_args()))
            self.systemd.linger = "no"
            self.assertIn("linger", self.refused(lab_tool.command_up, self.up_args()))
            self.systemd.linger = "yes"
            # A run id whose tag a live instance carries.
            api.instances[2] = {"id": 2, "label": "ntsm-t1-x", "type": "g6-dedicated-2",
                                "tags": ["needletail-qualification", api.tag],
                                "created": stamp(time.time())}
            self.assertIn("already carry", self.refused(lab_tool.command_up, self.up_args()))
            api.instances.pop(2)
            # A run id whose watchdog unit another worktree's lab has (made
            # in the same minute, or with no instance yet): its unit is
            # neither taken nor removed.
            self.systemd.active.add(UNIT)
            self.assertIn("watchdog unit", self.refused(lab_tool.command_up, self.up_args()))
            self.assertEqual(self.systemd.active, {UNIT})
            self.systemd.active.clear()
            lab_tool.WATCHDOG_UNIT_DIR.mkdir()
            (lab_tool.WATCHDOG_UNIT_DIR / UNIT).write_text("another lab's")
            self.assertIn("watchdog unit", self.refused(lab_tool.command_up, self.up_args()))
            self.assertEqual((lab_tool.WATCHDOG_UNIT_DIR / UNIT).read_text(), "another lab's")
            (lab_tool.WATCHDOG_UNIT_DIR / UNIT).unlink()
            # Another up is creating a lab: its instances are not there yet
            # for this budget to count.
            lock = lab_tool.LEDGER.with_name(lab_tool.LEDGER.name + ".up.lock")
            with open(lock, "a") as held:
                lab_tool.fcntl.flock(held, lab_tool.fcntl.LOCK_EX)
                self.assertIn("another up", self.refused(lab_tool.command_up, self.up_args()))
        self.assertNotIn(("POST", "/linode/instances"), api.calls)
        self.assertFalse(pathlib.Path(self.args.state).exists())
        self.assertFalse(self.systemd.active)
        self.assertFalse(list(lab_tool.WATCHDOG_UNIT_DIR.iterdir()))

    def test_up_counts_the_slots_the_ledger_and_other_labs_to_their_deadlines(self):
        api = FakeApi()
        self.systemd.active.clear()
        now = time.time()
        self.ledger({"run_id": "before-23-nov", "billed_usd": 38.0,
                     "removed_unix": int(now - 86400)},
                    {"run_id": "a1", "tag": "needletail-small-mesh-a1", "created_unix":
                     int(now - 80000), "removed_unix": int(now - 70000), "billed_usd": 5.0,
                     "source": "down"})
        # Another live lab: two instances an hour old whose watchdog deletes
        # them at four hours (4 billed hours each), and one of another tool
        # with no deadline tag, half an hour old: priced to the longest
        # life a watchdog allows (6 billed hours), not to now (1).
        made = int(now) - 3600
        for ident in (2, 3):
            api.instances[ident] = {"id": ident, "label": f"ntsm-o1-n{ident}",
                                    "type": "g6-dedicated-4", "created": stamp(made),
                                    "tags": ["needletail-qualification",
                                             "needletail-small-mesh-o1",
                                             f"needletail-deadline:{made + 4 * 3600}"]}
        api.instances[4] = {"id": 4, "label": "ntsm-o2-n4", "type": "g6-dedicated-2",
                            "created": stamp(int(now) - 1800),
                            "tags": ["needletail-qualification", "needletail-small-mesh-o2"]}
        other = 2 * 4 * 0.108 + 6 * 0.054
        # Every node of the topology, its four slots included, 3 billed
        # hours each for a watchdog of 2.5 h.
        topology = json.loads(SCALE.read_text())
        lab = 3 * sum(TYPES[node["type"]]["price"]["hourly"] for node in topology["nodes"])
        total = lab + 43.0 + other
        message = self.refused(self.up, api, budget_usd=round(total - 0.01, 2))
        self.assertIn("over the budget", message)
        figures = json.loads(message[message.index("{"):])
        self.assertAlmostEqual(figures["lab_usd"], lab, places=3)
        self.assertEqual(figures["billed_hours_each"], 3)
        self.assertAlmostEqual(figures["spent_before_usd"], 43.0)
        self.assertAlmostEqual(figures["other_labs_usd"], other, places=3)
        self.assertNotIn(("POST", "/linode/instances"), api.calls)
        self.assertFalse(pathlib.Path(self.args.state).exists())
        self.assertFalse(self.systemd.active)

        armed = []
        real = api.call

        def call(method, path, body=None, allow_404=False):
            if (method, path) == ("POST", "/linode/instances"):
                armed.append(UNIT in self.systemd.active)
            return real(method, path, body, allow_404)

        api.call = call
        state = self.up(api, budget_usd=round(total + 0.01, 2))
        self.assertEqual(state["budget_usd"], round(total + 0.01, 2))
        self.assertAlmostEqual(state["spent_before_usd"], 43.0)
        self.assertEqual((state["max_nodes"], state["max_hourly_usd"]), (103, 10.7))
        deadline = state["watchdog_deadline_unix"]
        # Counted from the lab's creation, so its life is the hours given.
        self.assertEqual(deadline, int(state["created_unix"] + 2.5 * 3600))
        mine = [item for item in api.instances.values() if api.tag in item["tags"]]
        self.assertEqual(len(mine), 99)
        for item in mine:
            self.assertIn(f"needletail-deadline:{deadline}", item["tags"])
        # The watchdog is a unit, enabled and running, at that deadline,
        # armed before the first instance was created.
        self.assertEqual(self.systemd.active, {UNIT})
        self.assertIn(UNIT, self.systemd.enabled)
        text = (lab_tool.WATCHDOG_UNIT_DIR / UNIT).read_text()
        self.assertIn(f'"--deadline-unix" "{deadline}"', text)
        self.assertIn("Restart=always", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN, pathlib.Path(self.args.state).read_text())
        self.assertEqual(armed, [True] * 99)

    def test_the_ceilings_of_up_hold_in_another_shell(self):
        api = FakeApi()
        self.ledger({"run_id": "before-23-nov", "billed_usd": 38.0, "removed_unix": 1})
        self.systemd.active.clear()
        self.up(api)
        # Another shell: the environment's defaults, 40 nodes and $1.00 an
        # hour. The lab of 99 nodes at $10.422 an hour keeps the ceilings
        # that up recorded.
        with mock.patch.object(lab_tool, "MAX_NODES", 40), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 1.0):
            state = lab_tool.read_state(pathlib.Path(self.args.state))
            self.assertEqual(lab_tool.lab_ceilings(state), (103, 10.7))
            self.add(api, state, ["lon-edge-11"])
            grow = argparse.Namespace(state=self.args.state, topology=str(SCALE),
                                      without_sites="", ssh_public_key=self.args.ssh_public_key,
                                      budget_usd=None, spent_before_usd=None)
            with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                    redirect_stdout(io.StringIO()) as out:
                lab_tool.command_grow(grow)
            self.assertIn("already has every topology node", out.getvalue())
            self.replace(api, ["lon-edge-1"], mode="new")
            state = lab_tool.read_state(pathlib.Path(self.args.state))
            self.assertTrue(state["nodes"]["lon-edge-1"]["label"].endswith("lon-edge-1r1"))
        # And they bind: a lab recorded at $10.45 an hour gets no slot
        # from a shell whose environment allows $50.
        state["max_hourly_usd"] = 10.45
        pathlib.Path(self.args.state).write_text(json.dumps(state))
        with mock.patch.object(lab_tool, "MAX_HOURLY_USD", 50.0):
            self.assertIn("the ceiling is $10.45",
                          self.refused(self.add, api, state, ["lon-edge-12"]))


class Add(Base):
    def test_add_creates_a_slot_tagged_on_its_vlan_and_refreshes_the_firewall(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        self.add(api, state, ["lon-edge-11"])
        node = state["nodes"]["lon-edge-11"]
        live = api.instances[node["id"]]
        self.assertIn(api.tag, live["tags"])
        self.assertIn("needletail-qualification", live["tags"])
        self.assertIn(f"needletail-deadline:{state['watchdog_deadline_unix']}", live["tags"])
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
        state["max_nodes"] = 100
        self.assertIn("ceiling is 100",
                      self.refused(self.add, api, state, ["lon-edge-11", "lon-edge-12"]))
        state["max_nodes"], state["max_hourly_usd"] = 103, 10.45
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
        # The budget that up recorded, and the ledger's sum when it is more
        # than the spent figure that up recorded.
        state["budget_usd"] = 100.0
        self.ledger({"run_id": "before-23-nov", "billed_usd": 48.0, "removed_unix": 1})
        self.assertIn("over the budget", self.refused(self.add, api, state, ["lon-edge-11"]))
        del state["budget_usd"]
        self.assertIn("records no budget", self.refused(self.add, api, state, ["lon-edge-11"]))
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


class Watchdog(Base):
    """The watchdog is a user unit whose deadline is when the lab is gone:
    no instance bills a seventh hour of a 6 h lab, nothing is created that
    `down` misses, and a unit that died is a problem every command finds
    and arms again."""

    def test_a_dead_watchdog_is_a_problem_and_is_armed_again(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        path = pathlib.Path(self.args.state)
        deadline = state["watchdog_deadline_unix"]
        self.assertEqual(lab_tool.watchdog_problems(state, time.time()), [])
        # The dev box rebooted without the unit, or it was killed.
        self.systemd.active.clear()
        problems = lab_tool.watchdog_problems(state, time.time())
        self.assertEqual(len(problems), 1)
        self.assertIn("not active", problems[0])
        self.assertIn("not active", self.refused(self.add, api, state, ["lon-edge-11"]))
        self.assertNotIn(("POST", "/linode/instances"), api.calls)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(lab_tool.watchdog_ensure(state, path), [])
        self.assertIn("was not active", err.getvalue())
        self.assertEqual(self.systemd.active, {UNIT})
        self.assertIn(UNIT, self.systemd.enabled)
        # At the recorded deadline, not a new one.
        self.assertIn(f'"--deadline-unix" "{deadline}"',
                      (lab_tool.WATCHDOG_UNIT_DIR / UNIT).read_text())
        self.assertEqual(lab_tool.read_state(path)["watchdog_deadline_unix"], deadline)
        self.assertEqual(lab_tool.watchdog_problems(state, time.time()), [])
        # A live unit is left alone.
        before = len(self.systemd.commands)
        self.assertEqual(lab_tool.watchdog_ensure(state, path), [])
        self.assertEqual([command[2] for command in self.systemd.commands[before:]],
                         ["is-active"])
        # Every command on the lab arms it again: add creates its slot.
        self.systemd.active.clear()
        add = argparse.Namespace(state=self.args.state, names=["lon-edge-11"],
                                 ssh_public_key=self.args.ssh_public_key, budget_usd=None,
                                 spent_before_usd=None, max_watchdog_hours=None)
        with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            lab_tool.command_add(add)
        self.assertIn("lon-edge-11", lab_tool.read_state(path)["nodes"])
        self.assertEqual(self.systemd.active, {UNIT})
        # A host that cannot keep it: said, not raised.
        self.systemd.active.clear()
        self.systemd.linger = "no"
        with redirect_stderr(io.StringIO()):
            problems = lab_tool.watchdog_ensure(state, path)
        self.assertIn("linger", problems[0])
        self.assertFalse(self.systemd.active)
        self.assertIn("watchdog --hours",
                      lab_tool.watchdog_ensure({**state, "watchdog_deadline_unix": None}, path)[0])

    def test_add_refuses_near_the_deadline_and_past_the_lab_life(self):
        api = FakeApi()
        state = lab_state(self.tmp, api, watchdog=10 * 60)
        self.assertIn("within 15 min", self.refused(self.add, api, state, ["lon-edge-11"]))
        # A deadline moved later mid-run (`watchdog --hours 6` at 1.5 h):
        # 7.5 h of the lab's life.
        state["watchdog_deadline_unix"] = int(time.time() + 6 * 3600)
        self.assertIn("7.500 h of the lab's life",
                      self.refused(self.add, api, state, ["lon-edge-11"]))
        self.assertNotIn(("POST", "/linode/instances"), api.calls)
        # Exactly 6.0 h of life passes; a looser limit given passes 7.5 h.
        state["watchdog_deadline_unix"] = state["created_unix"] + 6 * 3600
        self.add(api, state, ["lon-edge-11"])
        state["watchdog_deadline_unix"] = int(time.time() + 6 * 3600)
        self.add(api, state, ["lon-edge-12"], max_life_hours=8.0, budget_usd=1000.0)

    def test_grow_is_held_to_the_watchdog_and_the_budget(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        path = pathlib.Path(self.args.state)
        # Two nodes the lab does not have yet: grow has something to create.
        for name in ("lon-edge-1", "lon-edge-2"):
            api.instances.pop(state["nodes"].pop(name)["id"])
        path.write_text(json.dumps(state))

        def grow(budget=None):
            args = argparse.Namespace(state=self.args.state, topology=str(SCALE),
                                      without_sites="", ssh_public_key=self.args.ssh_public_key,
                                      budget_usd=budget, spent_before_usd=None)
            with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                lab_tool.command_grow(args)

        def lab(**changes):
            path.write_text(json.dumps({**state, **changes}))

        # The unit is dead and cannot be started again (the user does not
        # linger): refused.
        self.systemd.active.clear()
        self.systemd.linger = "no"
        self.assertIn("not active", self.refused(grow))
        self.systemd.linger = "yes"
        # Within 15 minutes of the deadline.
        lab(watchdog_deadline_unix=int(time.time() + 10 * 60))
        self.assertIn("within 15 min", self.refused(grow))
        # Armed for more of the lab's life than a watchdog allows.
        lab(watchdog_deadline_unix=int(time.time() + 6 * 3600))
        self.assertIn("of the lab's life", self.refused(grow))
        # Over a budget given, and over the budget that up recorded.
        lab()
        self.assertIn("over the budget", self.refused(grow, 50.0))
        lab(budget_usd=50.0)
        self.assertIn("over the budget", self.refused(grow))
        self.assertNotIn(("POST", "/linode/instances"), api.calls)
        # Within them, grow creates the two.
        lab()
        grow()
        self.assertEqual(api.calls.count(("POST", "/linode/instances")), 2)
        self.assertIn("lon-edge-1", lab_tool.read_state(path)["nodes"])

    def test_watchdog_hours_moves_the_deadline_the_unit_and_the_tags(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        path = pathlib.Path(self.args.state)
        for node in state["nodes"].values():
            api.instances[node["id"]]["tags"].append(
                f"needletail-deadline:{state['watchdog_deadline_unix']}")

        def watchdog(hours, budget=None):
            args = argparse.Namespace(state=self.args.state, hours=hours, foreground=False,
                                      run_id=None, deadline_unix=None, budget_usd=budget,
                                      spent_before_usd=None)
            with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                    redirect_stdout(io.StringIO()):
                lab_tool.command_watchdog(args)

        # Later, over the budget that up recorded: refused, nothing moved.
        self.assertIn("over the budget", self.refused(watchdog, 4.0, 60.0))
        self.assertEqual(lab_tool.read_state(path)["watchdog_deadline_unix"],
                         state["watchdog_deadline_unix"])
        watchdog(4.0)
        deadline = lab_tool.read_state(path)["watchdog_deadline_unix"]
        self.assertAlmostEqual(deadline, time.time() + 4 * 3600, delta=5)
        self.assertIn(f'"--deadline-unix" "{deadline}"',
                      (lab_tool.WATCHDOG_UNIT_DIR / UNIT).read_text())
        for node in state["nodes"].values():
            tags = api.instances[node["id"]]["tags"]
            self.assertEqual([tag for tag in tags if tag.startswith("needletail-deadline:")],
                             [f"needletail-deadline:{deadline}"])
        self.assertEqual(api.instances[1]["tags"], [])  # the dev box: untouched

    def test_down_starts_before_the_deadline_and_is_retried(self):
        path = self.tmp / "state.json"
        deadline = 1_000_000 + 6 * 3600
        path.write_text(json.dumps({"run_id": "t1", "watchdog_deadline_unix": deadline}))
        now = [deadline - lab_tool.WATCHDOG_LEAD_S - 12.0]
        downs = []

        def down(args):
            downs.append(now[0])
            if len(downs) == 1:
                raise RuntimeError("HTTP 500")
            path.unlink()

        def sleep(seconds):
            self.assertLessEqual(seconds, 5)
            now[0] += seconds

        # Once `down` has removed the state, the account lists nothing with
        # the run's tag: the loop returns.
        with mock.patch.object(lab_tool, "Api", lambda public=False: FakeApi()), \
                redirect_stderr(io.StringIO()):
            lab_tool.watch_until_down(argparse.Namespace(state=str(path)), path, "t1",
                                      deadline, clock=lambda: now[0], sleep=sleep, down=down)
        self.assertEqual(len(downs), 2)
        self.assertGreaterEqual(downs[0], deadline - lab_tool.WATCHDOG_LEAD_S)
        self.assertLess(downs[-1], deadline - lab_tool.WATCHDOG_LEAD_S + 15)
        # The first instances, created 1 to 3 s after the lab, are gone by
        # the deadline: 6 billed hours, not 7.
        self.assertEqual(lab_tool.billed_hours(1_000_003, downs[-1] + 60), 6)

    def test_a_lost_state_keeps_the_watchdog_to_its_deadline_by_the_tag(self):
        """`cargo clean` (or a worktree removed) takes the state file two
        hours into a lab: the unit keeps watching the run's tag, deletes
        every tagged instance and firewall from WATCHDOG_LEAD_S before its
        deadline, writes the ledger, and only then removes itself. An
        unreadable state is the same, never a crash loop."""
        self.ledger({"run_id": "before-23-nov", "billed_usd": 38.0, "removed_unix": 1})
        for index, lose in enumerate(("removed", "unreadable", "another run's")):
            api = FakeApi()
            state = lab_state(self.tmp, api)
            path = pathlib.Path(self.args.state)
            # Three labs of one run id, one after another.
            state["created_unix"] -= index
            path.write_text(json.dumps(state))
            deadline = state["watchdog_deadline_unix"]
            api.firewalls[9] = {"id": 9, "tags": ["needletail-qualification", api.tag]}
            api.firewalls[10] = {"id": 10, "tags": ["needletail-qualification",
                                                    "needletail-small-mesh-o1"]}
            edge = state["nodes"]["lon-edge-1"]["public_ipv4"]
            lab_tool.KNOWN_HOSTS.write_text(f"{edge} ssh-ed25519 AAAAedge\n")
            self.systemd.active.add(UNIT)
            self.systemd.enabled.add(UNIT)
            lab_tool.WATCHDOG_UNIT_DIR.mkdir(parents=True, exist_ok=True)
            (lab_tool.WATCHDOG_UNIT_DIR / UNIT).write_text("unit")
            now = [time.time()]
            deletes = []
            sleeps = []
            real = api.call

            def call(method, call_path, body=None, allow_404=False):
                if method == "DELETE" and call_path.startswith("/linode/instances/"):
                    deletes.append(now[0])
                return real(method, call_path, body, allow_404)

            api.call = call

            def sleep(seconds):
                if not sleeps:
                    # The first poll read the lab; then its state is lost.
                    if lose == "removed":
                        path.unlink()
                    elif lose == "unreadable":
                        path.write_text("{")
                    else:
                        path.write_text(json.dumps({"run_id": "t2", "nodes": {}}))
                sleeps.append(seconds)
                # The unit is never removed while the lab lives.
                self.assertIn(UNIT, self.systemd.enabled)
                self.assertTrue((lab_tool.WATCHDOG_UNIT_DIR / UNIT).exists())
                now[0] += seconds

            watch = lab_tool.watch_until_down
            args = argparse.Namespace(state=self.args.state, foreground=True, run_id="t1",
                                      deadline_unix=deadline)
            with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                    mock.patch.object(lab_tool, "watch_until_down",
                                      lambda *a: watch(*a, clock=lambda: now[0], sleep=sleep)), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                lab_tool.command_watchdog(args)
            self.assertTrue(deletes, lose)
            self.assertGreaterEqual(min(deletes), deadline - lab_tool.WATCHDOG_LEAD_S, lose)
            self.assertLess(max(deletes), deadline, lose)
            # No poll of the account more often than the poll period, and
            # not every 5 s for hours.
            self.assertLess(len(sleeps), 3 * 3600 / 30, lose)
            self.assertEqual(list(api.instances), [1], lose)  # only the dev box is left
            self.assertEqual(list(api.firewalls), [10], lose)  # another lab's is kept
            self.assertFalse(self.systemd.active, lose)
            self.assertNotIn(UNIT, self.systemd.enabled)
            self.assertFalse((lab_tool.WATCHDOG_UNIT_DIR / UNIT).exists())
            line = lab_tool.read_ledger()[-1]
            self.assertEqual((line["run_id"], line["source"]), ("t1", "watchdog"), lose)
            # From the last state it read: 99 nodes, 1.5 h old, 2 hours each.
            self.assertEqual(line["instance_hours"], 99 * 2, lose)
            self.assertAlmostEqual(line["billed_usd"], 10.422 * 2, places=3)
            self.assertEqual(lab_tool.KNOWN_HOSTS.read_text(), "")
        self.assertEqual(len(lab_tool.read_ledger()), 4)  # one line for each lab

    def test_add_racing_down_leaves_no_instance_and_no_state(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        path = pathlib.Path(self.args.state)
        real = lab_tool.create_node

        def create_during_down(*args):
            created = real(*args)
            # `down` listed the lab, deleted it and removed the state while
            # this create was in flight.
            path.unlink()
            return created

        with mock.patch.object(lab_tool, "create_node", create_during_down):
            message = self.refused(self.add, api, state, ["lon-edge-11"])
        self.assertIn("down has run", message)
        self.assertFalse(path.exists())
        self.assertFalse([item for item in api.instances.values()
                          if "lon-edge-11" in item.get("label", "")])
        # `down` marked as begun: the same.
        state = lab_state(self.tmp, api)
        on_disk = json.loads(path.read_text())

        def create_marked(*args):
            created = real(*args)
            path.write_text(json.dumps({**on_disk, "down_started_unix": int(time.time())}))
            return created

        with mock.patch.object(lab_tool, "create_node", create_marked):
            self.assertIn("down has begun", self.refused(self.add, api, state, ["lon-edge-12"]))
        self.assertNotIn("lon-edge-12", json.loads(path.read_text())["nodes"])
        self.assertTrue(json.loads(path.read_text())["down_started_unix"])
        self.assertFalse([item for item in api.instances.values()
                          if "lon-edge-12" in item.get("label", "")])

    def test_a_slot_whose_firewall_fails_is_deleted_not_served(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        real = api.call

        def call(method, path, body=None, allow_404=False):
            if path.startswith("/networking/firewalls/9/devices") and method == "POST":
                raise RuntimeError("POST devices failed with HTTP 400")
            return real(method, path, body, allow_404)

        api.call = call
        message = self.refused(self.add, api, state, ["lon-edge-11"])
        self.assertIn("firewall could not be set", message)
        self.assertNotIn("lon-edge-11", state["nodes"])
        self.assertEqual([item["name"] for item in state["removed"]], ["lon-edge-11"])
        self.assertFalse([item for item in api.instances.values()
                          if "lon-edge-11" in item.get("label", "")])

    def test_firewall_attached(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        ident = state["nodes"]["lon-edge-1"]["id"]
        api.list_all = lambda path, tag=None: [{"id": 3, "entity": {"id": ident,
                                                                    "type": "linode"}}]
        self.assertTrue(lab_tool.firewall_attached(api, state, "lon-edge-1"))
        self.assertFalse(lab_tool.firewall_attached(api, state, "lon-edge-2"))
        self.assertFalse(lab_tool.firewall_attached(api, {**state, "firewall_id": None},
                                                    "lon-edge-1"))

    def test_down_deletes_in_parallel_and_sweeps_the_tag_again(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        path = pathlib.Path(self.args.state)
        late = {}

        def vlans(api_, run_id):
            # A create that was in flight lands while the VLANs go.
            late["id"] = 9999
            api.instances[9999] = {"id": 9999, "label": "ntsm-t1-lon-edge-11",
                                   "tags": ["needletail-qualification", api.tag]}
            return []

        with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                mock.patch.object(lab_tool, "delete_vlans", vlans), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            lab_tool.command_down(argparse.Namespace(state=str(path)))
        self.assertFalse(path.exists())
        self.assertEqual(list(api.instances), [1])  # only the dev box is left
        self.assertIn(("DELETE", "/linode/instances/9999"), api.calls)
        # The unit is gone with the lab: stopped, disabled, its file removed.
        self.assertFalse(self.systemd.active)
        self.assertNotIn(UNIT, self.systemd.enabled)
        self.assertFalse((lab_tool.WATCHDOG_UNIT_DIR / UNIT).exists())
        self.assertEqual(self.systemd.commands[-1][2:], ["stop", UNIT])


class Ledger(Base):
    """What the work has spent: `down` writes its lab, `up` adds the labs
    that the account's events show made and removed elsewhere."""

    def test_a_lab_made_elsewhere_is_reconciled_from_events(self):
        api = FakeApi()
        since = int(time.time()) - 10 * 86400
        self.ledger({"run_id": "before-23-nov", "billed_usd": 38.0, "removed_unix": since},
                    {"run_id": "y1", "tag": "needletail-small-mesh-y1", "created_unix":
                     since + 1000, "removed_unix": since + 4000, "billed_usd": 1.5,
                     "source": "down"})

        def event(action, ident, label, unix, status="finished"):
            return {"action": action, "status": status, "created": stamp(unix),
                    "entity": {"id": ident, "label": label, "type": "linode"}}

        x1 = since + 20000
        api.instances[300] = {"id": 300, "label": "ntsm-z1-lon-edge-1", "type": "g6-dedicated-2",
                              "tags": ["needletail-qualification", "needletail-small-mesh-z1"],
                              "created": stamp(since + 30000)}
        # Newest first, over two pages.
        api.event_pages = [
            [event("linode_create", 300, "ntsm-z1-lon-edge-1", since + 30000),
             event("linode_delete", 201, "ntsm-x1-lon-dist-a", x1 + 5400),
             event("linode_delete", 200, "ntsm-x1-lon-edge-1", x1 + 5400),
             event("linode_create", 202, "ntsm-x1-lon-edge-2", x1 + 60, status="failed"),
             event("linode_create", 201, "ntsm-x1-lon-dist-a", x1 + 30)],
            [event("linode_create", 200, "ntsm-x1-lon-edge-1", x1),
             event("linode_delete", 100, "ntsm-y1-lon-edge-1", since + 3990),
             event("linode_create", 100, "ntsm-y1-lon-edge-1", since + 1010),
             event("linode_create", 1, "arch-us-east", since + 500),
             event("linode_create", 50, "ntsm-old-lon-edge-1", since - 5000),
             event("linode_delete", 50, "ntsm-old-lon-edge-1", since - 1000)],
        ]
        plans = {"lon-edge-1": {"g6-dedicated-2"}, "lon-dist-a": {"g6-dedicated-2",
                                                                  "g6-dedicated-4"}}
        with mock.patch.object(lab_tool, "node_plans", lambda: plans), \
                redirect_stdout(io.StringIO()) as out:
            spent = lab_tool.spent_before(api, None)
        # x1: two instances, 1.5 h each (2 billed hours), lon-dist-a at the
        # dearest plan any topology gives it. y1 was written by its `down`;
        # z1 is live (account_billed counts it); the failed create, the dev
        # box and what came before the first line are not labs of the work.
        x1_usd = 2 * 0.054 + 2 * 0.108
        self.assertAlmostEqual(spent, 38.0 + 1.5 + x1_usd, places=4)
        lines = lab_tool.read_ledger()
        self.assertEqual([line["run_id"] for line in lines], ["before-23-nov", "y1", "x1"])
        self.assertEqual(lines[2]["source"], "events")
        self.assertEqual(lines[2]["instance_hours"], 4)
        self.assertEqual((lines[2]["created_unix"], lines[2]["removed_unix"]), (x1, x1 + 5400))
        self.assertIn("lab x1 was made and removed by another tool", out.getvalue())
        # Once only.
        with mock.patch.object(lab_tool, "node_plans", lambda: plans), \
                redirect_stdout(io.StringIO()):
            self.assertAlmostEqual(lab_tool.spent_before(api, None), spent, places=4)
        self.assertEqual(len(lab_tool.read_ledger()), 3)

    def test_the_first_up_writes_what_was_spent_before(self):
        api = FakeApi()
        # No ledger and no figure: refused, not counted as nothing spent.
        self.assertIn("--spent-before-usd", self.refused(lab_tool.spent_before, api, None))
        self.assertEqual(lab_tool.spent_before(api, 38.0), 38.0)
        first = lab_tool.read_ledger()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["billed_usd"], 38.0)
        self.assertAlmostEqual(first[0]["removed_unix"], time.time(), delta=5)
        # A first line without a time cannot be reconciled: a figure must
        # be given, and is used as given.
        self.ledger({"run_id": "before-23-nov", "billed_usd": 38.0})
        self.assertIn("could not be reconciled", self.refused(lab_tool.spent_before, api, None))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(lab_tool.spent_before(api, 40.0), 40.0)

    def test_down_writes_the_ledger_and_forgets_the_hosts(self):
        api = FakeApi()
        self.ledger({"run_id": "before-23-nov", "billed_usd": 38.0, "removed_unix": 1})
        state = lab_state(self.tmp, api)
        path = pathlib.Path(self.args.state)
        self.add(api, state, ["lon-edge-11"])
        with redirect_stdout(io.StringIO()):
            lab_tool.remove_nodes(api, path, state, ["lon-edge-11"])
        removed = state["removed"][0]["public_ipv4"]
        edge = state["nodes"]["lon-edge-1"]["public_ipv4"]
        known = [f"{edge} ssh-ed25519 AAAAedge", f"[{removed}]:22 ssh-ed25519 AAAAslot",
                 "203.0.113.200 ssh-ed25519 AAAAother", "# a comment"]
        lab_tool.KNOWN_HOSTS.write_text("\n".join(known) + "\n")
        with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                mock.patch.object(lab_tool, "delete_vlans", lambda api_, run_id: []), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            lab_tool.command_down(argparse.Namespace(state=str(path)))
        lines = lab_tool.read_ledger()
        self.assertEqual(len(lines), 2)
        line = lines[1]
        self.assertEqual((line["run_id"], line["tag"], line["source"]), ("t1", api.tag, "down"))
        self.assertEqual(line["topology"], str(SCALE))
        self.assertEqual(line["worktree"], str(lab_tool.ROOT))
        # 99 nodes for 2 billed hours (1.5 h old) and the removed slot 1.
        self.assertAlmostEqual(line["billed_usd"], 10.422 * 2 + 0.054, places=3)
        self.assertEqual(line["instance_hours"], 99 * 2 + 1)
        self.assertEqual(lab_tool.KNOWN_HOSTS.read_text().splitlines(), known[2:])
        archive = json.loads(path.with_name("small-mesh-lab-t1.removed.json").read_text())
        self.assertEqual(archive["billed_usd"], line["billed_usd"])
        # A state written back after down (a command that held an old copy)
        # is the same lab: its second down bills it once.
        archive.pop("removed_unix")
        path.write_text(json.dumps(archive))
        with mock.patch.object(lab_tool, "Api", lambda public=False: api), \
                mock.patch.object(lab_tool, "delete_vlans", lambda api_, run_id: []), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            lab_tool.command_down(argparse.Namespace(state=str(path)))
        self.assertEqual(len(lab_tool.read_ledger()), 2)


class Replace(Base):
    """`replace` rebuilds an instance in place (the default) or makes it
    anew, refused before anything is touched when the instance is not the
    lab's, the watchdog is near or the budget would pass."""

    def test_rebuild_keeps_the_id_and_both_addresses(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        before = dict(state["nodes"]["lon-edge-1"])
        live_tags = list(api.instances[before["id"]]["tags"])
        with redirect_stderr(io.StringIO()):
            self.replace(api, ["lon-edge-1"])
        state = lab_tool.read_state(pathlib.Path(self.args.state))
        after = state["nodes"]["lon-edge-1"]
        for key in ("id", "public_ipv4", "vlan_ipv4", "label", "created_unix"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(api.instances[before["id"]]["tags"], live_tags)
        self.assertEqual(api.instances[before["id"]]["status"], "running")
        self.assertEqual(state["replaced"][-1]["mode"], "rebuild")
        self.assertEqual(state["replaced"][-1]["id"], before["id"])
        # No instance deleted or created: no new billed hour.
        self.assertEqual(state["removed"], [])
        self.assertFalse([call for call in api.calls
                          if call[0] == "DELETE" or call == ("POST", "/linode/instances")])
        self.assertEqual(lab_tool.billed(state)["instance_hours"], 99 * 2)
        rebuild = [body for method, path, body in api.bodies if path.endswith("/rebuild")]
        self.assertEqual(len(rebuild), 1)
        self.assertEqual(rebuild[0]["image"], "linode/debian13")
        self.assertEqual(rebuild[0]["authorized_keys"], ["ssh-ed25519 AAAA test"])
        self.assertFalse(rebuild[0]["booted"])
        # The root password is random, long, and kept nowhere.
        self.assertGreaterEqual(len(rebuild[0]["root_pass"]), 32)
        self.assertNotIn(rebuild[0]["root_pass"], pathlib.Path(self.args.state).read_text())
        # A rebuild of an instance that is gone is refused (new makes it).
        api.instances.pop(after["id"])
        self.assertIn("nothing to rebuild",
                      self.refused(self.replace, api, ["lon-edge-1"]))

    def test_rebuild_puts_the_interfaces_in_place_before_one_boot(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        ident = state["nodes"]["lon-dist-a"]["id"]
        api.rebuild_drops_vlan = True
        api.devices = [item for item in api.devices if item["entity"]["id"] != ident]
        self.replace(api, ["lon-dist-a"])
        calls = api.calls
        rebuilt = calls.index(("POST", f"/linode/instances/{ident}/rebuild"))
        put = calls.index(("PUT", f"/linode/instances/{ident}/configs/{700 + ident}"))
        attach = calls.index(("POST", "/networking/firewalls/9/devices"))
        boots = [index for index, call in enumerate(calls)
                 if call == ("POST", f"/linode/instances/{ident}/boot")]
        self.assertEqual(len(boots), 1)
        self.assertLess(rebuilt, put)
        self.assertLess(put, boots[0])
        self.assertLess(attach, boots[0])
        self.assertEqual(api.configs[ident][0]["interfaces"], [
            {"purpose": "public"},
            {"purpose": "vlan", "label": "ntsm-t1-gb-lon", "ipam_address": "10.80.1.11/24"}])
        boot = [body for method, path, body in api.bodies if path.endswith("/boot")][0]
        self.assertEqual(boot, {"config_id": 700 + ident})
        # A rebuild that kept both interfaces: the config is not written.
        api.rebuild_drops_vlan = False
        api.calls.clear()
        self.replace(api, ["lon-dist-a"])
        self.assertFalse([call for call in api.calls if "/configs/" in call[1]])
        self.assertEqual(api.calls.count(("POST", f"/linode/instances/{ident}/boot")), 1)
        # A rebuild that boots all the same, without the VLAN: shut down,
        # the config put right, then one boot.
        api.rebuild_boots = True
        api.rebuild_drops_vlan = True
        api.calls.clear()
        self.replace(api, ["lon-dist-a"])
        calls = api.calls
        shutdown = calls.index(("POST", f"/linode/instances/{ident}/shutdown"))
        put = calls.index(("PUT", f"/linode/instances/{ident}/configs/{700 + ident}"))
        boots = [index for index, call in enumerate(calls)
                 if call == ("POST", f"/linode/instances/{ident}/boot")]
        self.assertEqual(len(boots), 1)
        self.assertLess(shutdown, put)
        self.assertLess(put, boots[0])
        self.assertEqual(api.instances[ident]["status"], "running")
        self.assertEqual(len(api.configs[ident][0]["interfaces"]), 2)
        # One that boots with both interfaces: left running, no boot.
        api.rebuild_drops_vlan = False
        api.calls.clear()
        self.replace(api, ["lon-dist-a"])
        self.assertFalse([call for call in api.calls if call[1].endswith(("/boot", "/shutdown"))
                          or "/configs/" in call[1]])
        self.assertEqual(api.instances[ident]["status"], "running")
        api.rebuild_boots = False
        # A public address that is not the recorded one: not booted.
        api.instances[ident]["ipv4"] = ["192.0.2.99"]
        api.calls.clear()
        self.assertIn("not the recorded", self.refused(self.replace, api, ["lon-dist-a"]))
        self.assertNotIn(("POST", f"/linode/instances/{ident}/boot"), api.calls)

    def test_replace_works_on_a_lab_without_sydney(self):
        api = FakeApi()
        # The ceilings that up recorded for the lab without Sydney: the
        # whole topology file would pass them.
        state = lab_state(self.tmp, api, without=("sydney",))
        state.update(max_nodes=73, max_hourly_usd=8.0)
        pathlib.Path(self.args.state).write_text(json.dumps(state))
        self.replace(api, ["lon-edge-1"])
        old = state["nodes"]["lon-edge-2"]
        self.replace(api, ["lon-edge-2"], mode="new")
        state = lab_tool.read_state(pathlib.Path(self.args.state))
        new = state["nodes"]["lon-edge-2"]
        self.assertNotEqual(new["id"], old["id"])
        self.assertNotIn(old["id"], api.instances)
        self.assertEqual(new["vlan_ipv4"], old["vlan_ipv4"])
        self.assertTrue(new["label"].endswith("lon-edge-2r2"))
        self.assertEqual([item["mode"] for item in state["replaced"]], ["rebuild", "new"])
        self.assertEqual([item["name"] for item in state["removed"]], ["lon-edge-2"])
        live = api.instances[new["id"]]
        self.assertIn(f"needletail-deadline:{state['watchdog_deadline_unix']}", live["tags"])
        self.assertIn({"id": len(api.devices), "entity": {"id": new["id"], "type": "linode"}},
                      api.devices)
        self.assertIn("not a lab node", self.refused(self.replace, api, ["syd-edge-1"]))
        # A plan changed in the topology past the recorded hourly ceiling.
        state["max_hourly_usd"] = state["hourly_usd"]
        pathlib.Path(self.args.state).write_text(json.dumps(state))
        topology = json.loads(SCALE.read_text())
        for node in topology["nodes"]:
            if node["name"] == "lon-edge-3":
                node["type"] = "g6-dedicated-4"
        changed = self.tmp / "topology.json"
        changed.write_text(json.dumps(topology))
        state["topology_file"] = str(changed)
        pathlib.Path(self.args.state).write_text(json.dumps(state))
        self.assertIn("ceiling", self.refused(self.replace, api, ["lon-edge-3"], mode="new"))

    def test_replace_refuses_an_instance_without_the_run_tag(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        ident = state["nodes"]["lon-edge-1"]["id"]
        api.instances[ident]["tags"] = ["needletail-qualification"]
        for mode in ("rebuild", "new"):
            self.assertIn("does not carry",
                          self.refused(self.replace, api, ["lon-edge-1"], mode=mode))
        api.instances[ident]["tags"] = ["needletail-qualification", api.tag]
        api.instances[ident]["label"] = "arch-us-east"
        self.assertIn("labelled", self.refused(self.replace, api, ["lon-edge-1"]))
        # One name refused: none of the others is touched either.
        self.assertIn("labelled",
                      self.refused(self.replace, api, ["lon-edge-2", "lon-edge-1"], mode="new"))
        state["nodes"]["lon-edge-1"]["id"] = 1  # the dev box's id
        pathlib.Path(self.args.state).write_text(json.dumps(state))
        self.assertIn("does not carry", self.refused(self.replace, api, ["lon-edge-1"]))
        self.assertFalse([call for call in api.calls if call[0] in ("DELETE", "PUT")
                          or call[0] == "POST" and "/linode/instances" in call[1]])
        self.assertIn(1, api.instances)

    def test_replace_refuses_near_the_deadline_and_over_the_budget(self):
        api = FakeApi()
        state = lab_state(self.tmp, api, watchdog=10 * 60)
        for mode in ("rebuild", "new"):
            self.assertIn("within 15 min",
                          self.refused(self.replace, api, ["lon-edge-1"], mode=mode))
        # The lab kept to its deadline, 4.5 h: 5 billed hours of $10.422,
        # less lon-edge-1's 3 hours from its delete now (it bills 2) and
        # with its new instance's 3: $52.11.
        state["watchdog_deadline_unix"] = int(time.time() + 3 * 3600)
        state["budget_usd"] = 52.10
        pathlib.Path(self.args.state).write_text(json.dumps(state))
        message = self.refused(self.replace, api, ["lon-edge-1"], mode="new")
        self.assertIn("over the budget", message)
        self.assertIn("over the budget",
                      self.refused(self.replace, api, ["lon-edge-1"], mode="new",
                                   budget_usd=100.0, spent_before_usd=48.0))
        self.assertFalse([call for call in api.calls if call[0] == "DELETE"
                          or call[1].endswith("/rebuild") or call == ("POST",
                                                                       "/linode/instances")])
        # A rebuild bills no new hour: the same budget allows it, and
        # `new` fits a budget given just above $52.11 (the old instance is
        # not also priced to the deadline).
        self.replace(api, ["lon-edge-1"])
        self.replace(api, ["lon-edge-1"], mode="new", budget_usd=52.12)


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


class StateLock(Base):
    def test_a_change_never_writes_back_what_another_command_wrote(self):
        api = FakeApi()
        state = lab_state(self.tmp, api)
        path = pathlib.Path(self.args.state)
        # Another command (the deploy's record) writes while this copy is
        # held; the add's record keeps it.
        on_disk = json.loads(path.read_text())
        on_disk["deployed"] = {"edge": "abc"}
        path.write_text(json.dumps(on_disk))
        self.add(api, state, ["lon-edge-11"])
        after = json.loads(path.read_text())
        self.assertEqual(after["deployed"], {"edge": "abc"})
        self.assertIn("lon-edge-11", after["nodes"])
        self.assertEqual(state["deployed"], {"edge": "abc"})
        self.assertTrue(path.with_name("state.json.lock").exists())
        # The lock is held for the change: another process's flock waits.
        held = []

        def change(current):
            probe = subprocess.run(
                [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1]); "
                 "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                 str(path.with_name("state.json.lock"))], capture_output=True)
            held.append(probe.returncode != 0)
            current["note"] = 1

        lab_tool.change_state(path, state, change)
        self.assertEqual(held, [True])
        self.assertEqual(json.loads(path.read_text())["note"], 1)


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
        # No deadline tag (the referee's tool writes none): to the longest
        # life a watchdog allows from its creation, 6 h, not to now (3 h).
        self.assertAlmostEqual(result["usd"], 6 * 0.108)
        self.assertEqual(result["without_deadline"], 1)
        # Past that life it is priced to now: 7 h.
        later = now + 4.5 * 3600
        self.assertAlmostEqual(lab_tool.account_billed(api, api.tag, later)["usd"], 7 * 0.108)
        # A region's own price, where the plan lists one.
        api.instances[2]["region"] = "ap-southeast"
        dearer = {**TYPES["g6-dedicated-4"],
                  "region_prices": [{"id": "ap-southeast", "hourly": 0.13}]}
        with mock.patch.dict(TYPES, {"g6-dedicated-4": dearer}):
            self.assertAlmostEqual(lab_tool.account_billed(api, api.tag, now)["usd"], 6 * 0.13)
        del api.instances[2]["region"]
        # To the deadline its tag carries, not to now (2.5 h): 5 hours.
        api.instances[2]["tags"].append(
            f"needletail-deadline:{int(lab_tool.created_unix({'created': '2026-11-22T05:00:00'}))}")
        result = lab_tool.account_billed(api, api.tag, now)
        self.assertAlmostEqual(result["usd"], 5 * 0.108)
        self.assertEqual(result["without_deadline"], 0)


if __name__ == "__main__":
    unittest.main()
