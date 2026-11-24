#!/usr/bin/env python3
"""Tests of the large audio mesh's deploy plan (spec-scale part M): the
topology files are what scripts/scale_topology.py generates, dedicated plans
only, Sydney first; the deploy tool renders topology-scale.json (the
referee's plan: eight London edges, four on standby for the first curve) in
the merged topology (every edge fed by its
site's two forwarders on the fast plane and the cache mesh, the cache mesh's
children raised to the site's edges, no class port collision, distinct
fast-lane ranks); standby edges have no service but, with the autoscale
slots (created or not), stay in every peer list, telemetry peer list and
fast-plane feed, so that starting one (or a slot forwarder, a lease child
that is never a compiled relay child) changes no other node's arguments;
push, start and stop --nodes touch the named nodes only;
listener load hosts get needletail-load, ntcmp and their kernel settings; the
port plan's limit on relay children is enforced; the lab tool leaves a site
out of `up` and `grow` and prices a topology from the API's plans. The
proving lab (C CT5): both contributors, three serving edges and one standby
a region, slots in every region, 40 nodes at $3.186 an hour, rendered and
compiled with the standby contributor.
python3 -m unittest scripts/test_scale_topology.py"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scale_topology  # noqa: E402


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_module("deploy_scale", "linode-small-mesh-deploy.py")
lab_tool = load_module("lab_scale", "linode-small-mesh-lab.py")
REAL_COMPILE = deploy.compile_plan
COMPILER = deploy.common.ROOT / "target" / "release" / "needletail-compile"
ROUTES = {"best_direct_rtt_us": 250000, "primary_rtt_us": 250000, "primary_jitter_us": 1000,
          "secondary_rtt_us": 260000, "secondary_jitter_us": 1000}
FILES = scale_topology.OUT
SCALE = FILES / "topology-scale.json"
LONDON_EDGES = [f"lon-edge-{index}" for index in range(1, 11)]
SHORT = ("syd", "lon", "new", "tok")
# Edges 6 to 10 of every region: the second curve's.
STANDBY = [f"{short}-edge-{index}" for short in SHORT for index in range(6, 11)]
FIRST_CURVE = [f"standby_edges={json.dumps(STANDBY)}"]
SCALE_SLOTS = ["syd-dist-c", "syd-edge-11", "lon-dist-c", "lon-edge-11", "lon-edge-12",
               "new-dist-c", "new-edge-11", "tok-dist-c", "tok-edge-11"]


def topology_state(topology: dict, created: tuple[str, ...] = ()) -> dict:
    """A Linode state for a topology document: made-up public addresses, the
    fields that the lab tool keeps (purpose included). The autoscale slots
    are left out, as `up` leaves them, but for those named in `created`
    (made by `add`); the state lists every slot under "slots", as `up`
    records them."""
    nodes = {}
    slots = lab_tool.slot_specs(topology)
    for index, node in enumerate(topology["nodes"], 1):
        if node["name"] in slots and node["name"] not in created:
            continue
        nodes[node["name"]] = {
            "name": node["name"], "role": node["role"], "site": node["site"],
            "region": node["region"], "type": node["type"],
            **({"purpose": node["purpose"]} if node.get("purpose") else {}),
            **({"standby": True} if node.get("standby") else {}),
            "public_ipv4": f"198.51.{index // 250}.{index % 250 + 1}",
            "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
    return {"run_id": "test", "hourly_usd": 0.0, "nodes": nodes, "slots": slots}


def flag_values(args: list[str], flag: str) -> list[str]:
    return [args[index + 1] for index, arg in enumerate(args) if arg == flag]


class Files(unittest.TestCase):
    def test_the_committed_files_are_what_the_generator_writes(self):
        for name, kwargs in scale_topology.VARIANTS.items():
            self.assertEqual((FILES / name).read_text(),
                             scale_topology.render(scale_topology.scale_topology(**kwargs)), name)

    def test_shape_of_the_scale_topology(self):
        topology = json.loads(SCALE.read_text())
        every = topology["nodes"]
        self.assertTrue(all(node["type"].startswith("g6-dedicated-") for node in every))
        names = [node["name"] for node in every]
        self.assertEqual(len(names), len(set(names)))
        vlans = [(node["region"], node["vlan_ipv4"]) for node in every if node.get("vlan_ipv4")]
        self.assertEqual(len(vlans), len(set(vlans)))
        # The autoscale slots: a third forwarder and an edge past its ten
        # in every region, two edges in London; g6-dedicated-2, on the VLAN.
        self.assertEqual(topology["autoscale_slots"], SCALE_SLOTS)
        slots = [node for node in every if node.get("slot")]
        self.assertEqual([node["name"] for node in slots], topology["autoscale_slots"])
        self.assertTrue(all(node["type"] == "g6-dedicated-2" and node.get("vlan_ipv4")
                            for node in slots))
        self.assertEqual({node["name"]: node["vlan_ipv4"] for node in slots},
                         {"syd-dist-c": "10.80.4.13/24", "syd-edge-11": "10.80.4.32/24",
                          "lon-dist-c": "10.80.1.13/24", "lon-edge-11": "10.80.1.32/24",
                          "lon-edge-12": "10.80.1.33/24", "new-dist-c": "10.80.2.13/24",
                          "new-edge-11": "10.80.2.32/24", "tok-dist-c": "10.80.3.13/24",
                          "tok-edge-11": "10.80.3.32/24"})
        nodes = [node for node in every if not node.get("slot")]
        # The scarce region is created first.
        self.assertEqual(nodes[0]["site"], "sydney")
        by_site: dict[str, dict[str, list[dict]]] = {}
        for node in nodes:
            by_site.setdefault(node["site"], {}).setdefault(node["role"], []).append(node)
        self.assertEqual(sorted(by_site), ["london", "newyork", "sydney", "tokyo"])
        # Ten edges a region, five of them on standby for the first curve.
        self.assertEqual(topology["standby_edges"], STANDBY)
        self.assertEqual(sum(1 for node in nodes if node["role"] == "edge"), 40)
        for site, roles in by_site.items():
            self.assertEqual(len(roles["distributor"]), 2, site)
            self.assertEqual(len(roles["edge"]), 10, site)
            self.assertTrue(all(node["type"] == "g6-dedicated-2"
                                for node in roles["distributor"] + roles["edge"]), site)
            listeners = [node for node in roles["load"] if node.get("purpose") == "listeners"]
            vcpus = sum(deploy.node_vcpus(node) for node in listeners)
            self.assertEqual(vcpus, 64, site)
            # More listener capacity than the region's ten edges send at
            # 1 ms PCM (about 700 listeners an edge at 4 Gbit/s; about 1,000
            # listeners of 1 ms parts a g6-dedicated-8 within 70% busy):
            # the edges give out first on the second curve.
            self.assertGreater(vcpus / 8 * 1_000, len(roles["edge"]) * 700, site)
            # Listener and view hosts read the edges over the VLAN (not
            # billed); every one has an address there.
            self.assertTrue(all(node.get("vlan_ipv4") for node in roles["load"]), site)
            self.assertEqual([node["name"] for node in roles["load"]
                              if node.get("purpose") == "view"], [f"{site[:3]}-view"
                                                                  if site != "newyork"
                                                                  else "new-view"])
            self.assertEqual(len([node for node in roles["livekit"]
                                  if node["name"].endswith("-lk")]), 1, site)
        contributor = by_site["london"]["contributor"]
        self.assertEqual([(node["name"], node["type"]) for node in contributor],
                         [("contrib-lon", "g6-dedicated-4")])
        self.assertIn("lk-redis", names)
        self.assertEqual([node["purpose"] for node in nodes if node["name"] == "lon-src"],
                         ["source"])

    def test_the_referees_plan_and_its_price(self):
        topology = json.loads(SCALE.read_text())
        price = {"g6-dedicated-2": 0.054, "g6-dedicated-4": 0.108, "g6-dedicated-8": 0.216}
        counts: dict[str, int] = {}
        # What `up` creates: the autoscale slots are left out.
        topology = lab_tool.created_part(topology)
        for node in topology["nodes"]:
            counts[node["type"]] = counts.get(node["type"], 0) + 1
        # Sized for the budget of about $60 to $70 with the watchdog's cap:
        # 99 nodes (Sydney's eight g6-dedicated-8 are sixteen g6-dedicated-4,
        # no capacity on 22 Nov), $10.422 an hour, $62.53 in the 6 h of the
        # watchdog.
        self.assertEqual(len(topology["nodes"]), 99)
        self.assertEqual(counts, {"g6-dedicated-2": 53, "g6-dedicated-4": 22,
                                  "g6-dedicated-8": 24})
        hourly = sum(price[node["type"]] for node in topology["nodes"])
        self.assertAlmostEqual(hourly, 10.422)
        self.assertLessEqual(hourly * 6, 70.0)

    def test_variants_and_refusals(self):
        lon8 = json.loads(SCALE.read_text())
        edges = [node["name"] for node in lon8["nodes"]
                 if node["role"] == "edge" and node["site"] == "london" and not node.get("slot")]
        self.assertEqual(edges, LONDON_EDGES)
        # The edge addresses skip the source host's .30.
        addresses = [node["vlan_ipv4"] for node in lon8["nodes"] if node["name"] in edges]
        self.assertNotIn("10.80.1.30/24", addresses)
        wide = scale_topology.scale_topology(edges={"london": 12})
        hosts = [node["vlan_ipv4"] for node in wide["nodes"]
                 if node["site"] == "london" and node.get("vlan_ipv4")]
        self.assertEqual(len(hosts), len(set(hosts)))
        three = scale_topology.scale_topology(sites=("london", "newyork", "tokyo"))
        self.assertNotIn("sydney", {node["site"] for node in three["nodes"]})
        none = scale_topology.scale_topology(standby={})
        self.assertEqual(none["standby_edges"], [])
        bare = scale_topology.scale_topology(slots={
            site: {"edges": 0, "forwarders": 0} for site in scale_topology.DEFAULT_SITES})
        self.assertEqual(bare["autoscale_slots"], [])
        self.assertFalse([node for node in bare["nodes"] if node.get("slot")])
        for bad in ({"sites": ("newyork", "tokyo")}, {"edges": {"mars": 2}},
                    {"edges": {"london": 0}}, {"load_type": "g6-standard-8"},
                    {"standby": {"london": 10}}, {"slots": {"mars": {"edges": 1}}},
                    {"slots": {"london": {"edges": 31}}},
                    {"slots": {"london": {"relays": 1}}},
                    # Listener and view hosts g6-dedicated-4 or -8 only (owner).
                    {"load_type": "g6-dedicated-2"},
                    {"load_by_site": {"sydney": (2, "g6-dedicated-2")}},
                    {"view_by_site": {"sydney": "g6-dedicated-2"}},
                    {"load_by_site": {"mars": (1, "g6-dedicated-8")}},
                    {"standby": {"mars": 1}}):
            with self.assertRaises(ValueError, msg=bad):
                scale_topology.scale_topology(**bad)

    def test_slots_in_every_region(self):
        """C 2.9: a forwarder slot (`<s>-dist-c`, VLAN .13) and an edge slot
        past the site's edges in every region of every committed file."""
        for name in scale_topology.VARIANTS:
            topology = json.loads((FILES / name).read_text())
            for site, (_region, short, net) in scale_topology.SITES.items():
                slots = {node["name"]: node["vlan_ipv4"] for node in topology["nodes"]
                         if node.get("slot") and node["site"] == site}
                self.assertEqual(slots.get(f"{short}-dist-c"), f"10.80.{net}.13/24",
                                 (name, site))
                edges = [node for node in slots if "-edge-" in node]
                self.assertEqual(len(edges), 2 if (name, site) == ("topology-scale.json",
                                                                   "london") else 1,
                                 (name, site))

    def test_the_proving_lab_and_its_price(self):
        """C 2.9 with the topology agreed with the referee on 23 Nov (three
        serving edges and one standby a region): 40 nodes at $3.186 an hour,
        the eight slots $0.432 more if all exist."""
        topology = json.loads((FILES / "topology-proving.json").read_text())
        self.assertEqual(topology["nodes"][0]["site"], "sydney")
        self.assertTrue(all(node["type"].startswith("g6-dedicated-") for node in topology["nodes"]))
        created = lab_tool.created_part(topology)["nodes"]
        roles: dict[tuple[str, str], int] = {}
        for node in created:
            key = (node["role"] if node["role"] != "load" else node["purpose"], node["type"])
            roles[key] = roles.get(key, 0) + 1
        self.assertEqual(roles, {("contributor", "g6-dedicated-4"): 2,
                                 ("source", "g6-dedicated-4"): 1,
                                 ("distributor", "g6-dedicated-2"): 8,
                                 ("edge", "g6-dedicated-2"): 16,
                                 ("listeners", "g6-dedicated-8"): 4,
                                 ("view", "g6-dedicated-4"): 4,
                                 ("livekit", "g6-dedicated-2"): 5})
        self.assertEqual(len(created), 40)
        hourly, problems = lab_tool.topology_cost({**topology, "nodes": created},
                                                  LabTool.TYPES, LabTool.REGIONS)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(hourly, 3.186, places=3)
        every, _ = lab_tool.topology_cost(topology, LabTool.TYPES, LabTool.REGIONS)
        self.assertAlmostEqual(every - hourly, 8 * 0.054, places=3)
        self.assertEqual(topology["standby_edges"],
                         [f"{short}-edge-4" for short in SHORT])
        self.assertEqual(topology["autoscale_slots"],
                         [name for short in SHORT for name in (f"{short}-dist-c",
                                                               f"{short}-edge-5")])
        # The standby contributor: A's render takes "standby": true at the
        # contributor's site on its VLAN, alone on its own g6-dedicated-4.
        contributors = [node for node in created if node["role"] == "contributor"]
        self.assertEqual([(node["name"], node.get("standby", False), node["vlan_ipv4"])
                          for node in contributors],
                         [("contrib-lon", False, "10.80.1.5/24"),
                          ("contrib-lon-b", True, "10.80.1.6/24")])
        london = [node["vlan_ipv4"] for node in topology["nodes"]
                  if node["site"] == "london" and node.get("vlan_ipv4")]
        self.assertEqual(len(london), len(set(london)))
        # The run's ceilings (C 7, with the agreed edges): every node and
        # slot, and their hourly price; `up` itself counts the 40 it creates.
        with mock.patch.object(lab_tool, "MAX_NODES", 48), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 3.65):
            _topology, checked = lab_tool.validate_topology(FakeTypesApi(), topology)
        self.assertAlmostEqual(checked, 3.186, places=3)
        with mock.patch.object(lab_tool, "MAX_NODES", 39), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 3.65), \
                self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            lab_tool.validate_topology(FakeTypesApi(), topology)
        # The file states its price and the ceilings it is run under
        # (review of CT5, finding 3).
        self.assertIn("Up creates 40 nodes at $3.186 an hour; with every slot 48 nodes at "
                      "$3.618, so the lab's ceilings are NEEDLETAIL_LINODE_MAX_NODES=48 and "
                      "NEEDLETAIL_LINODE_MAX_HOURLY_USD=3.65 or more.", topology["description"])
        # The variant without Sydney (lab up --without-sites sydney, or
        # --optional-sites sydney when its core is refused); no -syd2 file.
        self.assertEqual(sorted(scale_topology.VARIANTS),
                         ["topology-proving.json", "topology-scale.json"])
        self.assertFalse((FILES / "topology-proving-syd2.json").exists())
        three = lab_tool.created_part(lab_tool.without_sites(topology, ["sydney"]))
        hourly, _ = lab_tool.topology_cost(three, LabTool.TYPES, LabTool.REGIONS)
        self.assertEqual(len(three["nodes"]), 31)
        self.assertAlmostEqual(hourly, 2.484, places=3)

    def test_every_file_states_its_price_from_the_lab_tool(self):
        """Each committed file's description gives what `up` creates and
        every node with its slots, priced as the lab tool prices them."""
        for name in scale_topology.VARIANTS:
            topology = json.loads((FILES / name).read_text())
            created = lab_tool.created_part(topology)
            hourly, _ = lab_tool.topology_cost(created, LabTool.TYPES, LabTool.REGIONS)
            every, _ = lab_tool.topology_cost(topology, LabTool.TYPES, LabTool.REGIONS)
            self.assertIn(f"Up creates {len(created['nodes'])} nodes at ${hourly:.3f} an hour; "
                          f"with every slot {len(topology['nodes'])} nodes at ${every:.3f}",
                          topology["description"], name)

    def test_the_flags_change_the_variant(self):
        def printed(*argv: str) -> dict:
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["scale_topology.py", *argv]), \
                    redirect_stdout(out):
                scale_topology.main()
            return json.loads(out.getvalue())

        self.assertEqual(printed(), json.loads(SCALE.read_text()))
        proving = json.loads((FILES / "topology-proving.json").read_text())
        self.assertEqual(printed("--variant", "topology-proving.json"), proving)
        bigger = printed("--variant", "topology-proving.json", "--load",
                         "sydney=2:g6-dedicated-4", "--view", "sydney=g6-dedicated-8")
        self.assertEqual(sorted((node["name"], node["type"]) for node in bigger["nodes"]
                                if node["site"] == "sydney" and node["role"] == "load"),
                         [("syd-load-1", "g6-dedicated-4"), ("syd-load-2", "g6-dedicated-4"),
                          ("syd-view", "g6-dedicated-8")])
        changed = printed("--variant", "topology-proving.json", "--sites",
                          "london,newyork,tokyo", "--standby", "london=2", "--slots",
                          "tokyo=edges:2,forwarders:0", "--contributors", "1", "--no-livekit",
                          "--load-hosts", "2", "--load-type", "g6-dedicated-4")
        names = [node["name"] for node in changed["nodes"]]
        self.assertFalse([name for name in names if name.startswith("syd-")])
        self.assertEqual(changed["standby_edges"],
                         ["lon-edge-3", "lon-edge-4", "new-edge-4", "tok-edge-4"])
        self.assertEqual(changed["autoscale_slots"],
                         ["lon-dist-c", "lon-edge-5", "new-dist-c", "new-edge-5",
                          "tok-edge-5", "tok-edge-6"])
        self.assertNotIn("contrib-lon-b", names)
        self.assertFalse([node for node in changed["nodes"] if node["role"] == "livekit"])
        self.assertEqual({(node["name"], node["type"]) for node in changed["nodes"]
                          if node.get("purpose") == "listeners" and node["site"] == "tokyo"},
                         {("tok-load-1", "g6-dedicated-4"), ("tok-load-2", "g6-dedicated-4")})
        for argv in (("--variant", "topology-nope.json"), ("--contributors", "3"),
                     ("--view", "london=g6-standard-4"), ("--load", "london=two"),
                     ("--slots", "london=edges:x"), ("--load", "sydney=2:g6-dedicated-2"),
                     ("--view", "sydney=g6-dedicated-2")):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                printed(*argv)

    def test_a_flag_for_a_site_not_in_the_topology_is_refused(self):
        """Review of CT5, finding 1: a misspelt site, or one that --sites
        left out, is refused by every SITE=... flag, not dropped."""
        for variant in ("topology-scale.json", "topology-proving.json"):
            for argv in (("--edges-in", "sydeny=8"), ("--standby", "sydeny=1"),
                         ("--slots", "sydeny=edges:2"), ("--load", "londn=3:g6-dedicated-8"),
                         ("--view", "londn=g6-dedicated-8"),
                         ("--sites", "london,newyork,tokyo", "--edges-in", "sydney=3"),
                         ("--sites", "london,newyork,tokyo", "--load",
                          "sydney=1:g6-dedicated-8")):
                out, err = io.StringIO(), io.StringIO()
                with self.subTest(variant=variant, argv=argv), \
                        mock.patch.object(sys, "argv", ["scale_topology.py", "--variant",
                                                        variant, *argv]), \
                        self.assertRaises(SystemExit) as caught, redirect_stdout(out), \
                        redirect_stderr(err):
                    scale_topology.main()
                self.assertIn("not in the topology", str(caught.exception))
                self.assertEqual(out.getvalue(), "")

    def test_a_site_flag_keeps_the_other_sites_defaults(self):
        """Review of CT5, finding 2: on topology-scale.json, whose variant
        leaves the standby edges and the listener hosts to scale_topology's
        per-site defaults, a flag for London changes London only."""
        def printed(*argv: str) -> dict:
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["scale_topology.py", *argv]), \
                    redirect_stdout(out):
                scale_topology.main()
            return json.loads(out.getvalue())

        scale = json.loads(SCALE.read_text())
        standby = printed("--standby", "london=2")
        self.assertEqual(standby["standby_edges"],
                         [name for name in STANDBY if not name.startswith("lon-")
                          or name in ("lon-edge-9", "lon-edge-10")])
        load = printed("--load", "london=4:g6-dedicated-8")["scale"]["load_hosts"]
        self.assertEqual(load, {**scale["scale"]["load_hosts"],
                                "london": {"count": 4, "type": "g6-dedicated-8"}})
        self.assertEqual(load["sydney"], {"count": 16, "type": "g6-dedicated-4"})
        # --slots changes the counts it names and keeps the site's others.
        slots = printed("--slots", "london=forwarders:2")["autoscale_slots"]
        self.assertEqual([name for name in slots if name.startswith("lon-")],
                         ["lon-dist-c", "lon-dist-d", "lon-edge-11", "lon-edge-12"])
        # Fewer edges in a site than its default standby: the default
        # standby of that site goes, as scale_topology's own default does.
        fewer = printed("--edges-in", "london=4", "--standby", "tokyo=2")
        self.assertEqual(fewer["standby_edges"],
                         [name for name in STANDBY if name.startswith(("syd-", "new-"))]
                         + ["tok-edge-9", "tok-edge-10"])


class Party(unittest.TestCase):
    """The listening party (owner, 23 Nov): the plan's figures, each an
    argument and printed, whole-hour billing, dedicated plans only, the
    budget left after the proving run, and the committed files."""

    def test_the_committed_party_files_are_what_the_generator_writes(self):
        self.assertEqual((FILES / scale_topology.PARTY_FILE).read_text(),
                         scale_topology.render(scale_topology.party_document(
                             scale_topology.party_plan())))
        self.assertEqual((FILES / scale_topology.PARTY_MD).read_text(),
                         scale_topology.party_markdown())

    def test_the_default_party_plan(self):
        """20,000 listeners of 1 ms Opus Custom with two paths: 271 legs an
        edge (400,000 x 0.70 / 1,031), 37 serving edges a region; listener
        hosts for 10,000 legs and LiveKit's 450 a region within 70%."""
        plan = scale_topology.party_plan()
        self.assertEqual(plan["legs_per_edge"], 271)
        self.assertEqual(plan["listeners_per_edge"], 135)
        for site, region in plan["regions"].items():
            self.assertEqual((region["listeners"], region["legs"], region["serving_edges"],
                              region["standby_edges"], region["edge_slots"]),
                             (5000, 10000, 37, 1, 2), site)
            self.assertAlmostEqual(region["load_vcpu"], 10000 * 0.0055 + 450 * 0.008)
        self.assertEqual({site: (r["load_hosts"], r["load_type"])
                          for site, r in plan["regions"].items()},
                         {"sydney": (21, "g6-dedicated-4"), "london": (11, "g6-dedicated-8"),
                          "newyork": (11, "g6-dedicated-8"), "tokyo": (11, "g6-dedicated-8")})
        document = scale_topology.party_document(plan)
        created = lab_tool.created_part(document)
        self.assertEqual(len(created["nodes"]), plan["instances"])
        self.assertEqual(plan["instances"], 226)
        self.assertEqual(plan["vcpus"], 706)
        hourly, problems = lab_tool.topology_cost(created, LabTool.TYPES, LabTool.REGIONS)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(hourly, plan["hourly_usd"], places=6)
        self.assertAlmostEqual(plan["hourly_usd"], 19.062, places=3)
        # Edges and forwarders g6-dedicated-2, the contributors alone on a
        # g6-dedicated-4 each, listener hosts -4 or -8.
        for node in document["nodes"]:
            if node["role"] in ("edge", "distributor"):
                self.assertEqual(node["type"], "g6-dedicated-2", node["name"])
            if node["role"] == "contributor":
                self.assertEqual(node["type"], "g6-dedicated-4", node["name"])
            if node.get("purpose") == "listeners":
                self.assertIn(node["type"], ("g6-dedicated-4", "g6-dedicated-8"), node["name"])
        self.assertEqual([node["name"] for node in document["nodes"]
                          if node["role"] == "contributor"], ["contrib-lon", "contrib-lon-b"])
        self.assertEqual(document["standby_edges"], [f"{short}-edge-38" for short in SHORT])
        self.assertEqual([name for name in document["autoscale_slots"] if name.startswith("lon-")],
                         ["lon-dist-c", "lon-edge-39", "lon-edge-40"])
        # Four whole hours of every instance; the slots and the overage at
        # worst; the budget left after the spend, the held and the proving
        # run's 4 h of its 48 nodes and slots.
        self.assertEqual(plan["billed_hours"], 4)
        self.assertAlmostEqual(plan["run_usd"], 4 * 19.062, places=3)
        self.assertAlmostEqual(plan["proving_usd"], 4 * (3.186 + 0.432), places=3)
        self.assertAlmostEqual(plan["left_usd"], 300 - 41 - 45.62 - 4 * 3.618, places=3)
        self.assertTrue(plan["fits_budget"])
        self.assertEqual(plan["max_edges_per_forwarder"], 40)
        self.assertEqual(plan["max_nodes"], 226 + 12)
        self.assertGreaterEqual(plan["max_hourly_usd"],
                                plan["hourly_usd"] + plan["slots_hourly_usd"])
        # Traffic: the listener hosts over the VLAN, the long haul and the
        # LiveKit crowd over the public network.
        self.assertAlmostEqual(plan["vlan_mbps"]["tokyo"], 10000 * 1.2 + 2 * 38 * 1.2)
        self.assertAlmostEqual(plan["public_mbps"]["tokyo"], 4 * 1.2 + 450 * 0.42)
        self.assertAlmostEqual(plan["public_mbps"]["london"], 450 * 0.42)

    def test_every_figure_is_an_argument_and_printed(self):
        base = scale_topology.party_plan(listeners=scale_topology.even_split(5000))
        text = scale_topology.plan_text(base)
        for figure in ("400,000", "0.70", "1.031", "1.2 Mbit/s", "0.0055", "271 legs",
                       "g6-dedicated-8", "$7.182 an hour", "266 vCPUs", "Gbit/s",
                       "GB an hour", "$198.91 left", "NEEDLETAIL_LINODE_MAX_NODES=95"):
            self.assertIn(figure, text)
        for key, value, moved in (("edge_datagrams_per_s", 200_000, "serving_edges"),
                                  ("edge_fill", 0.35, "serving_edges"),
                                  ("datagrams_per_part", 2.0, "serving_edges"),
                                  ("load_vcpu_per_listener", 0.011, "load_hosts"),
                                  ("load_fill", 0.35, "load_hosts"),
                                  ("paths", 1, "legs")):
            plan = scale_topology.party_plan(listeners=scale_topology.even_split(5000),
                                             **{key: value})
            self.assertNotEqual(plan["regions"]["london"][moved],
                                base["regions"]["london"][moved], key)
        slower = scale_topology.party_plan(listeners=scale_topology.even_split(5000),
                                           lane="opus-2.5")
        self.assertEqual(slower["legs_per_edge"], 678)
        self.assertIn("0.6 Mbit/s", scale_topology.plan_text(slower))
        # A run of 2.5 h bills 3 whole hours of every instance.
        short = scale_topology.party_plan(listeners=scale_topology.even_split(5000), hours=2.5)
        self.assertEqual(short["billed_hours"], 3)
        self.assertAlmostEqual(short["run_usd"], 3 * short["hourly_usd"])
        quiet = scale_topology.party_plan(listeners=scale_topology.even_split(5000),
                                          livekit=False)
        self.assertFalse([node for node in quiet["document"]["nodes"]
                          if node["role"] == "livekit"])
        self.assertAlmostEqual(quiet["public_mbps"]["london"], 0.0)
        # LiveKit's crowd is no larger than the region's listeners.
        small = scale_topology.party_plan(listeners=scale_topology.even_split(1000))
        self.assertEqual(small["regions"]["london"]["lk_listeners"], 250)
        over = scale_topology.party_plan(budget_usd=100.0)
        self.assertFalse(over["fits_budget"])
        self.assertIn("**does not fit**", scale_topology.plan_text(over))

    def test_the_plan_names_the_capacity_setting_and_the_trigger(self):
        """Review of CT5, finding 6: the deploy line gives the datagram
        capacity the plan assumes, and the plan says where its top step
        sits against the autoscaler's trigger."""
        plan = scale_topology.party_plan()
        text = scale_topology.plan_text(plan)
        self.assertIn("`--set client_udp_datagram_capacity=400000`", text)
        # 10,000 legs on 37 edges: 270.3 x 1,031 / 400,000.
        self.assertEqual(plan["edge_permille"], 697)
        self.assertIn("mean load is up to 697 permille; the autoscaler adds an edge at a mean "
                      "of 700", text)
        self.assertIn("The top step runs at the trigger", text)
        slack = scale_topology.party_plan(listeners=scale_topology.even_split(5000),
                                          edge_fill=0.5, edge_datagrams_per_s=300_000)
        text = scale_topology.plan_text(slack)
        self.assertIn("client_udp_datagram_capacity=300000", text)
        self.assertLess(slack["edge_permille"], 600)
        self.assertIn("The top step runs below the trigger.", text)

    def test_the_plan_prints_the_lab_without_sydney(self):
        """Review of CT5, finding 4: what `up --optional-sites sydney`
        leaves when Sydney refuses, priced as the lab tool prices it; the
        recorded capacity stated as recorded."""
        plan = scale_topology.party_plan()
        document = scale_topology.party_document(plan)
        rest = lab_tool.created_part(lab_tool.without_sites(document, ["sydney"]))
        hourly, problems = lab_tool.topology_cost(rest, LabTool.TYPES, LabTool.REGIONS)
        self.assertEqual(problems, [])
        without = plan["without_sydney"]
        self.assertEqual((without["listeners"], without["instances"]),
                         (15000, len(rest["nodes"])))
        self.assertAlmostEqual(without["hourly_usd"], hourly, places=6)
        self.assertAlmostEqual(without["run_usd"], 4 * hourly, places=6)
        text = scale_topology.plan_text(plan)
        self.assertIn(f"- Without Sydney (`up --optional-sites sydney`", text)
        self.assertIn(f"15,000 listeners, {len(rest['nodes'])} instances", text)
        self.assertIn("every slot made once", text)
        three = scale_topology.party_plan(sites=("london", "newyork", "tokyo"),
                                          listeners=scale_topology.even_split(
                                              3000, ("london", "newyork", "tokyo")))
        self.assertIsNone(three["without_sydney"])
        self.assertNotIn("Without Sydney", scale_topology.plan_text(three))
        markdown = scale_topology.party_markdown()
        self.assertIn("no g6-dedicated-4 or larger capacity on 22 November", markdown)
        self.assertIn(f"| 163, {without['hourly_usd']:.3f} |", markdown)

    def test_refusals(self):
        for bad in ({"load_type": "g6-standard-8"}, {"load_type": "g6-dedicated-2"},
                    {"load_type_by_site": {"sydney": "g6-nanode-1"}}, {"paths": 3},
                    {"lane": "pcm-1"}, {"sites": ("newyork", "tokyo")},
                    {"listeners": {"mars": 10}}, {"edge_fill": 0},
                    {"listeners": {"london": 100_000}}, {"edge_slots": -1}, {"nodes": 3},
                    {"load_type_by_site": {"sydny": "g6-dedicated-8"}},
                    {"sites": ("london", "newyork", "tokyo"),
                     "listeners": {"london": 10, "newyork": 10, "tokyo": 10},
                     "load_type_by_site": {"sydney": "g6-dedicated-8"}}):
            with self.assertRaises(ValueError, msg=bad):
                scale_topology.party_plan(**bad)

    def test_the_party_and_plan_commands(self):
        def run(*argv: str) -> str:
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["scale_topology.py", *argv]), \
                    redirect_stdout(out):
                scale_topology.main()
            return out.getvalue()

        self.assertEqual(json.loads(run("party")),
                         json.loads((FILES / scale_topology.PARTY_FILE).read_text()))
        text = run("plan", "--listeners", "10000", "--lane", "opus-2.5", "--paths", "1",
                   "--hours", "3", "--edge-fill", "0.5", "--load-type-in", "sydney=g6-dedicated-8",
                   "--listeners-per-region", "tokyo=4000", "--lk-listeners-per-region", "100")
        self.assertIn("### 11,500 listeners, opus-2.5 (standard 2.5 ms Opus), 1 path", text)
        self.assertIn("x 0.50 = 484 legs", text)
        self.assertIn("| tokyo | 4,000 | 4,000 | 9 |", text)
        self.assertIn("x g6-dedicated-8 | 2,545 |", text.split("| sydney |")[1])
        self.assertIn("bills 3 whole hours", text)
        three = json.loads(run("party", "--sites", "london,newyork,tokyo", "--listeners", "300"))
        self.assertEqual(three["scale"]["sites"], ["london", "newyork", "tokyo"])
        self.assertEqual(three["party"]["regions"]["london"]["listeners"], 100)
        for argv in (("plan", "--paths", "3"), ("plan", "--load-type", "g6-standard-8"),
                     ("plan", "--listeners-per-region", "london=many"),
                     ("plan", "--load-type-in", "sydny=g6-dedicated-8"),
                     ("plan", "--listeners-per-region", "londn=100"),
                     ("party", "--listeners", "1000000")):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                run(*argv)


class FakeTypesApi:
    """The plans and regions of LabTool, as the API lists them."""

    def list_all(self, endpoint, tag=None):
        return list((LabTool.TYPES if "types" in endpoint else LabTool.REGIONS).values())


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


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Render(Base):
    def render(self, path: pathlib.Path, overrides=(), state: dict | None = None):
        settings = self.settings(overrides)
        full = state or topology_state(json.loads(path.read_text()))
        active = deploy.active_state(full, settings)
        self.active = active
        program = deploy.build_program(active, settings, ROUTES)
        plan = deploy.compile_plan(program)
        rendered = deploy.render(active, settings, plan)
        return full, plan, {name: item["args"] for name, item in rendered.items()}

    def check_site(self, state: dict, args: dict, site: str, edges: list[str]) -> None:
        """The site as rendered: `edges` are the edges in service. Every peer
        list names the roster (the edges held out and the autoscale slots,
        created or not, included: low-latency ops D3), so that starting one
        of them changes no other node's arguments."""
        nodes = self.active["roster"]
        slots = set(self.active["slot_names"])
        roster_edges = sorted(name for name, node in nodes.items()
                              if node["role"] == "edge" and node["site"] == site)
        all_dists = sorted(name for name, node in nodes.items()
                           if node["role"] == "distributor" and node["site"] == site)
        dists = [name for name in all_dists if name not in slots]
        slot_dists = [name for name in all_dists if name in slots]
        for dist in dists:
            peers = flag_values(args[dist], "--peer")
            # Its edges, then (AM 12, forwarder_slot_feeds_edges) the site's
            # forwarder slots, which subscribe at it.
            self.assertEqual(peers, [f"{nodes[edge]['vlan_ipv4']}:{deploy.MESH_PORT}"
                                     for edge in roster_edges]
                             + [f"{nodes[slot]['vlan_ipv4']}:{deploy.MESH_PORT}"
                                for slot in slot_dists], dist)
            children = int(deploy.flag_value(args[dist], "--cache-mesh-max-children"))
            self.assertGreaterEqual(children, len(roster_edges), dist)
            lease = int(deploy.flag_value(args[dist], "--relay-max-lease-children"))
            self.assertGreaterEqual(lease, len(roster_edges) + len(all_dists) - len(dists), dist)
        ranks = []
        for edge in edges:
            parents = {deploy.flag_value(args[edge], "--relay-primary-id"),
                       deploy.flag_value(args[edge], "--relay-secondary-id")}
            self.assertEqual(parents, set(dists), edge)
            ranks.append(deploy.flag_value(args[edge], "--client-udp-edge-rank"))
            self.assertEqual(sorted(flag_values(args[edge], "--peer")),
                             sorted(f"{nodes[dist]['vlan_ipv4']}:{deploy.MESH_PORT}"
                                    for dist in all_dists), edge)
        # Distinct ranks, from the roster's places.
        self.assertEqual(len(set(ranks)), len(ranks))
        self.assertTrue(all(0 <= int(rank) < len(roster_edges) for rank in ranks))
        # Each forwarder is the primary of half of the roster's edges.
        feed = deploy.fast_plane_feed(self.active, deploy.class_plan(self.settings()))
        primaries = [feed["edges"][edge]["primary"]["name"] for edge in roster_edges]
        self.assertEqual(sorted(primaries.count(dist) for dist in dists),
                         [len(roster_edges) // 2, len(roster_edges) - len(roster_edges) // 2])

    def test_the_scale_topology_renders_in_the_merged_shape(self):
        state, plan, args = self.render(SCALE, FIRST_CURVE)
        nodes = state["nodes"]
        self.assertEqual(deploy.flag_value(args["contrib-lon"], "--relay-primary-id"), "lon-dist-a")
        self.assertEqual(deploy.flag_value(args["contrib-lon"], "--relay-secondary-id"),
                         "lon-dist-b")
        remote = sorted(name for name, node in nodes.items()
                        if node["role"] == "distributor" and node["site"] != "london")
        self.assertEqual(len(remote), 6)
        for name in remote:
            self.assertEqual({deploy.flag_value(args[name], "--relay-primary-id"),
                              deploy.flag_value(args[name], "--relay-secondary-id")},
                             {"lon-dist-a", "lon-dist-b"}, name)
        for site in ("london", "newyork", "tokyo", "sydney"):
            edges = sorted(name for name, node in nodes.items()
                           if node["role"] == "edge" and node["site"] == site)
            edges = [name for name in edges if name not in STANDBY]
            self.assertEqual(len(edges), 5)
            self.check_site(state, args, site, edges)
        # Only the mesh services are rendered: load hosts and LiveKit are not.
        self.assertEqual({nodes[name]["role"] for name in args},
                         {"contributor", "distributor", "edge"})
        self.assertEqual(len(args), 1 + 8 + 20)
        classes = deploy.class_plan(self.settings())
        for name, node_args in args.items():
            sockets = {(socket, port) for socket, port, _ in deploy.udp_binds(node_args, classes)}
            self.assertEqual(len({port for _, port in sockets}), len(sockets), name)

    def test_ten_edges_a_region_on_the_second_curve(self):
        # The second curve: no standby edge.
        state, _plan, args = self.render(SCALE)
        for short, site in zip(SHORT, ("sydney", "london", "newyork", "tokyo")):
            self.check_site(state, args, site,
                            [f"{short}-edge-{index}" for index in range(1, 11)])

    def test_a_site_of_more_edges_than_the_cache_mesh_default(self):
        topology = scale_topology.scale_topology(edges={"london": 12})
        state, _plan, args = self.render(SCALE, state=topology_state(topology))
        self.assertGreater(12, deploy.DEFAULT_SETTINGS["cache_mesh_max_children"])
        self.check_site(state, args, "london", [f"lon-edge-{index}" for index in range(1, 13)])

    def test_standby_edges_have_no_service_and_stay_in_every_peer_list(self):
        standby = STANDBY
        state, _plan, args = self.render(SCALE, FIRST_CURVE)
        for name in standby:
            self.assertNotIn(name, args)
        self.check_site(state, args, "london", [f"lon-edge-{index}" for index in range(1, 6)])
        # Every peer list names the standby edges and the slots (a peer that
        # is down): the distributors' cache-mesh peers, the edges'
        # telemetry peers, the forwarders' fast-plane children.
        roster = self.active["roster"]
        london_edges = sorted(name for name, node in roster.items()
                              if node["role"] == "edge" and node["site"] == "london")
        self.assertEqual(len(london_edges), 12)
        for name in ("lon-edge-6", "lon-edge-10", "lon-edge-11", "lon-edge-12"):
            address = roster[name]["vlan_ipv4"]
            self.assertIn(f"{address}:{deploy.MESH_PORT}", flag_values(args["lon-dist-a"], "--peer"))
            self.assertIn(f"{address}:{deploy.TELEMETRY_PORT}",
                          flag_values(args["lon-edge-1"], "--telemetry-peer"))
            self.assertIn(f"{address}:{deploy.TELEMETRY_PORT}",
                          flag_values(args["lon-dist-b"], "--telemetry-peer"))
        for name in ("lon-dist-c",):
            address = roster[name]["vlan_ipv4"]
            self.assertIn(f"{address}:{deploy.MESH_PORT}", flag_values(args["lon-edge-1"], "--peer"))
            self.assertIn(f"{address}:{deploy.TELEMETRY_PORT}",
                          flag_values(args["lon-edge-2"], "--telemetry-peer"))
        primaries = (deploy.flag_value(args["lon-dist-a"], "--relay-lease-primary-children")
                     + "," + deploy.flag_value(args["lon-dist-b"],
                                               "--relay-lease-primary-children")).split(",")
        for name in ("lon-edge-6", "lon-edge-11", "lon-dist-c"):
            self.assertIn(roster[name]["vlan_ipv4"], primaries)
        # The forwarders' lease children fit under the cap of 16 (lease.rs).
        self.assertLessEqual(int(deploy.flag_value(args["lon-dist-a"],
                                                   "--relay-max-lease-children")), 16)
        sent = []
        with mock.patch.object(deploy.common, "rsync_to", lambda *a, **k: None), \
                mock.patch.object(deploy.common, "ssh_text",
                                  lambda node, script, **k: sent.append(script) or ""):
            deploy.stop_service(state["nodes"]["lon-edge-6"])
        self.assertIn("systemctl disable --now needletail-mesh.service", sent[-1])
        with self.assertRaises(SystemExit):
            deploy.active_state(state, self.settings(['standby_edges=["lon-dist-a"]']))

    def test_starting_a_held_out_node_changes_no_other_nodes_arguments(self):
        """The join restarts nothing: a standby edge, an edge slot and both
        forwarder slots, created and taken into service, leave the
        arguments of every other node as they were."""
        # The compiled plan stamps its route observations with the time.
        frozen = mock.patch.object(deploy.time, "time", lambda: 1_790_000_000.0)
        frozen.start()
        self.addCleanup(frozen.stop)
        _state, plan, before = self.render(SCALE, FIRST_CURVE)
        created = ("lon-edge-11", "lon-dist-c", "new-dist-c")
        full = topology_state(json.loads(SCALE.read_text()), created=created)
        held = [name for name in STANDBY if name != "lon-edge-6"]
        _state, plan_after, after = self.render(
            SCALE, [f"standby_edges={json.dumps(held)}"], state=full)
        started = {"lon-edge-6", *created}
        self.assertEqual(set(after) - set(before), started)
        for name in before:
            self.assertEqual(before[name], after[name], name)
        # The compiled relay DAG is the same: no slot is a relay child, so
        # no other forwarder's ports moved.
        self.assertEqual(sorted((item["service"], item["node_id"]) for item in plan["services"]),
                         sorted((item["service"], item["node_id"])
                                for item in plan_after["services"]))
        self.assertNotIn("lon-dist-c", {item["node_id"] for item in plan_after["services"]})

    def test_a_forwarder_slot_is_a_lease_child_and_a_cache_mesh_parent(self):
        full = topology_state(json.loads(SCALE.read_text()), created=("lon-dist-c", "new-dist-c"))
        _state, _plan, args = self.render(SCALE, FIRST_CURVE, state=full)
        roster = self.active["roster"]
        for name, site, short in (("lon-dist-c", "london", "lon"), ("new-dist-c", "newyork", "new")):
            dist_c = args[name]
            self.assertEqual(deploy.flag_value(dist_c, "--cache-mesh-role"), "distributor")
            self.assertEqual(flag_values(dist_c, "--peer"), [
                f"{roster[edge]['vlan_ipv4']}:{deploy.MESH_PORT}" for edge in sorted(
                    item for item, node in roster.items()
                    if node["role"] == "edge" and node["site"] == site)] + [
                f"{roster[parent]['vlan_ipv4']}:{deploy.MESH_PORT}"
                for parent in (f"{short}-dist-a", f"{short}-dist-b")])
            self.assertEqual(deploy.flag_value(dist_c, "--relay-primary-id"), f"{short}-dist-a")
            self.assertEqual(deploy.flag_value(dist_c, "--relay-secondary-id"), f"{short}-dist-b")
            self.assertEqual(deploy.flag_value(dist_c, "--relay-primary-peer"),
                             f"{roster[short + '-dist-a']['vlan_ipv4']}:{deploy.LEASE_PORT}")
            # AM 12: a lease parent of its site's edges too (their ranks
            # decide), subscribing at its forwarders for their leases.
            self.assertEqual(deploy.flag_value(dist_c, "--relay-lease-children"), "true")
            self.assertEqual(deploy.flag_value(dist_c, "--relay-lease-bind"),
                             f"{roster[name]['vlan_ipv4']}:{deploy.LEASE_PORT}")
            self.assertEqual(deploy.flag_value(dist_c, "--cache-mesh-upstream-demand"), "true")
            self.assertNotIn("--relay-forward", dist_c)
            self.assertIn(roster[name]["vlan_ipv4"], deploy.flag_value(
                args[f"{short}-dist-a"], "--relay-lease-primary-children").split(","))
        # The contributor's fan-out is the same two.
        self.assertEqual({deploy.flag_value(args["contrib-lon"], "--relay-primary-id"),
                          deploy.flag_value(args["contrib-lon"], "--relay-secondary-id")},
                         {"lon-dist-a", "lon-dist-b"})

    def test_the_peer_check_takes_held_nodes_by_the_rule_that_lists_them_back(self):
        """The merged playlists mesh refuses a peer that does not list it
        back (low-latency check_cache_mesh_peers). A standby edge or a slot
        not in service is not rendered: a distributor of its site that lists
        it passes (it lists the distributor when it starts); an edge that
        lists another edge would not be listed back, and is refused. The slot
        forwarders render the wave 7 settings like the other forwarders."""
        full, _plan, args = self.render(SCALE, FIRST_CURVE + [
            'cache_mesh_fast_plane_push="secondary"', "client_udp_compact_short_header=false"])
        roster = self.active["roster"]
        held = f"{roster['lon-edge-7']['vlan_ipv4']}:{deploy.MESH_PORT}"
        self.assertNotIn("lon-edge-7", args)
        self.assertIn(held, flag_values(args["lon-dist-a"], "--peer"))
        with self.assertRaises(ValueError) as caught:
            self.render(SCALE, FIRST_CURVE + [f'extra_edge_args=["--peer","{held}"]'])
        self.assertIn("would not list", str(caught.exception))
        full = topology_state(json.loads(SCALE.read_text()), created=("lon-dist-c",))
        _state, _plan, args = self.render(SCALE, FIRST_CURVE + [
            'cache_mesh_fast_plane_push="secondary"', "client_udp_compact_short_header=false",
            'relay_demand_renditions="auto"'], state=full)
        for flag in ("--cache-mesh-fast-plane-push", "--client-udp-compact-short-header",
                     "--relay-demand-renditions"):
            self.assertEqual(deploy.flag_value(args["lon-dist-c"], flag),
                             deploy.flag_value(args["lon-dist-a"], flag), flag)

    def test_warm_up_in_milliseconds(self):
        _state, _plan, args = self.render(SCALE, FIRST_CURVE + ["cache_mesh_warm_ms=4500"])
        # 4.5 s of the shortest audio part (1 ms at the defaults).
        self.assertEqual(deploy.flag_value(args["lon-edge-1"], "--cache-mesh-warm-parts"), "4500")
        self.assertEqual(deploy.flag_value(args["lon-dist-a"], "--cache-mesh-warm-parts"), "4500")
        _state, _plan, args = self.render(SCALE, FIRST_CURVE + ["cache_mesh_warm_ms=4500",
                                                                "pcm_epoch_samples=120",
                                                                "opus_epoch_samples=120"])
        # The DAW source's 1 ms lossless epoch is still the shortest part.
        self.assertEqual(deploy.flag_value(args["lon-edge-1"], "--cache-mesh-warm-parts"), "4500")
        _state, _plan, args = self.render(SCALE, FIRST_CURVE + ["cache_mesh_warm_parts=300"])
        self.assertEqual(deploy.flag_value(args["lon-edge-1"], "--cache-mesh-warm-parts"), "300")


    def test_without_sydney(self):
        topology = json.loads(SCALE.read_text())
        topology = lab_tool.without_sites(topology, ["sydney"])
        state, _plan, args = self.render(SCALE, state=topology_state(topology))
        self.assertFalse([name for name in args if name.startswith("syd-")])
        remote = [name for name, node in state["nodes"].items()
                  if node["role"] == "distributor" and node["site"] != "london"]
        self.assertEqual(len(remote), 4)
        # No region failover entry names a Sydney node.
        for node_args in args.values():
            for entry in flag_values(node_args, "--failover-entry"):
                self.assertFalse(entry.startswith("ap-southeast="), entry)

    def test_the_proving_lab_renders_both_contributors_and_compiles(self):
        """topology-proving.json through the deploy's offline render and the
        compiler: both contributors on the London forwarders (workstream A's
        render), three serving edges a region on their two forwarders, the
        standby edges and every slot in the peer lists; the same without
        Sydney, and after `up` left Sydney out part way (its slots still
        recorded)."""
        proving = FILES / "topology-proving.json"
        topology = json.loads(proving.read_text())
        held = [f"standby_edges={json.dumps(topology['standby_edges'])}"]
        state, plan, args = self.render(proving, held)
        for name in ("contrib-lon", "contrib-lon-b"):
            self.assertEqual({deploy.flag_value(args[name], "--relay-primary-id"),
                              deploy.flag_value(args[name], "--relay-secondary-id")},
                             {"lon-dist-a", "lon-dist-b"}, name)
        self.assertEqual(deploy.flag_value(args["contrib-lon-b"], "--relay-local-id"),
                         "contrib-b")
        self.assertEqual(deploy.flag_value(args["contrib-lon-b"], "--relay-primary-bind"),
                         "10.80.1.6:22301")
        self.assertEqual(len(args), 2 + 8 + 12)
        for short, site in zip(SHORT, ("sydney", "london", "newyork", "tokyo")):
            self.assertNotIn(f"{short}-edge-4", args)
            self.check_site(state, args, site, [f"{short}-edge-{index}" for index in (1, 2, 3)])
            for name in (f"{short}-edge-4", f"{short}-edge-5", f"{short}-dist-c"):
                self.assertIn(f"{self.active['roster'][name]['vlan_ipv4']}:{deploy.MESH_PORT}",
                              flag_values(args[f"{short}-dist-a"], "--peer"), name)
        classes = deploy.class_plan(self.settings())
        for name, node_args in args.items():
            sockets = {(socket, port) for socket, port, _ in deploy.udp_binds(node_args, classes)}
            self.assertEqual(len({port for _, port in sockets}), len(sockets), name)
        without = [f"standby_edges={json.dumps(topology['standby_edges'][1:])}"]
        _state, _plan, args = self.render(proving, without, state=topology_state(
            lab_tool.without_sites(topology, ["sydney"])))
        self.assertEqual(len(args), 22 - 5)
        dropped = topology_state(topology)
        dropped["nodes"] = {name: node for name, node in dropped["nodes"].items()
                            if node["site"] != "sydney"}
        self.assertIn("syd-dist-c", dropped["slots"])
        _state, _plan, args = self.render(proving, without, state=dropped)
        self.assertFalse([name for name in args if name.startswith("syd-")])

    def test_the_party_renders_with_its_forwarder_setting(self):
        """topology-party.json (40 edges in a region's peer lists) renders and
        compiles with the max_edges_per_forwarder its plan prints, and is
        refused without it."""
        path = FILES / scale_topology.PARTY_FILE
        topology = json.loads(path.read_text())
        plan = scale_topology.party_plan()
        held = [f"standby_edges={json.dumps(topology['standby_edges'])}"]
        state, _plan, args = self.render(path, held + [
            f"max_edges_per_forwarder={plan['max_edges_per_forwarder']}"])
        self.assertEqual(len(args), 2 + 8 + 4 * 37)
        self.check_site(state, args, "tokyo", [f"tok-edge-{index}" for index in range(1, 38)])
        self.assertEqual(deploy.flag_value(args["tok-dist-a"], "--relay-max-lease-children"),
                         "41")
        with self.assertRaises(SystemExit) as caught:
            self.render(path, held)
        self.assertIn("max_edges_per_forwarder", str(caught.exception))

    def test_client_udp_max_subscribers(self):
        _state, _plan, args = self.render(SCALE, ["client_udp_max_subscribers=20000"])
        self.assertEqual(deploy.flag_value(args["lon-edge-1"], "--client-udp-max-subscribers"),
                         "20000")
        _state, _plan, args = self.render(SCALE)
        self.assertNotIn("--client-udp-max-subscribers", args["lon-edge-1"])


OLD = ("aaaaaaaaaaaa", 1000)
NEW = ("bbbbbbbbbbbb", 2000)
CONTRIB = ("cccccccccccc", 1000)
LIFECYCLE_FLAGS = "--drain-state-file --stop-drain-ms"


def fake_binary(path: pathlib.Path, name: str, build: tuple, flags: str = "") -> None:
    """An executable that answers --version as the builds do (C 2.2) and
    lists `flags` in --help."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ncase \"$1\" in\n  --version) echo \"{name} 0.1.0 "
                    f"({build[0]} {build[1]})\" ;;\n  --help) echo \"{flags}\" ;;\nesac\n")
    path.chmod(0o755)


class FakeHosts:
    """The lab's hosts as files and services: rsync and ssh land here. A
    node's activation is the script's model (the script itself is proved
    in test_merged_topology's Activation); `/api/node` answers with the
    build and the args_sha256 of what each node runs."""

    ETC = "/etc/needletail"

    def __init__(self):
        self.files: dict[str, dict[str, bytes]] = {}
        self.running: dict[str, bool] = {}
        self.boots: dict[str, int] = {}
        self.scripts: dict[str, list[str]] = {}
        self.touched: list[str] = []
        self.udp: dict[str, dict] = {}
        self.inflight = 0
        self.most_inflight = 0
        self.guard = threading.Lock()
        self.by_address: dict[str, dict] = {}
        self.drained: set[str] = set()
        # The engine's clock: a node's boot id is the activation's time on it.
        self.clock = time.time

    def fs(self, name: str) -> dict[str, bytes]:
        return self.files.setdefault(name, {})

    def rsync_to(self, node, sources, destination, timeout=900):
        self.touched.append(node["name"])
        with self.guard:
            self.inflight += 1
            self.most_inflight = max(self.most_inflight, self.inflight)
        try:
            time.sleep(0.01)
            for source in sources:
                path = pathlib.Path(source)
                target = destination + path.name if destination.endswith("/") else destination
                self.fs(node["name"])[target] = path.read_bytes() if path.is_file() else b""
        finally:
            with self.guard:
                self.inflight -= 1

    def rsync_from(self, node, source, destination, timeout=900):
        self.touched.append(node["name"])
        target = pathlib.Path(destination)
        target.mkdir(parents=True, exist_ok=True)
        for path, data in self.fs(node["name"]).items():
            if path.startswith(source):
                (target / path[len(source):]).write_bytes(data)
                (target / path[len(source):]).chmod(0o755)

    def ssh_text(self, node, script, **kwargs):
        name = node["name"]
        self.touched.append(name)
        self.scripts.setdefault(name, []).append(script)
        files = self.fs(name)
        if script == deploy.BOOTSTRAP:
            return f"eth1={node.get('vlan_ipv4')}/24"
        if "systemd-run" in script:
            return self.activate(name, script)
        if script.startswith("cat "):
            return files.get(script.split()[1], b"").decode()
        if "systemctl disable --now" in script:
            self.running[name] = False
            return ""
        if "NRestarts" in script:
            return "0"
        if script.startswith("date +%s.%N"):
            return f"{time.time():.9f}"
        last = script.rstrip().splitlines()[-1] if script.strip() else ""
        if last.startswith("sha256sum "):
            return "\n".join(f"{hashlib.sha256(files[path]).hexdigest()}  {path}"
                             for path in last.split()[1:] if path in files)
        return ""

    def activate(self, name: str, script: str) -> str:
        service = re.search(r"needletail-activate-(\S+)", script).group(1)
        not_after = int(re.search(r"--not-after ([0-9]+)", script).group(1))
        rollback = "--rollback" in script.split("\n", 2)[1]
        files = self.fs(name)
        args = f"{self.ETC}/{service}.args"
        unit = f"/etc/systemd/system/{service}.service"

        def result(ok: bool, why: str = "") -> str:
            lines = [json.dumps({"ok": ok, "why": why, "qdisc": False})]
            if args in files:
                text = files[args].decode()
                lines += [f"args_sha256 {hashlib.sha256(text[:-1].encode()).hexdigest()}",
                          f"render_sha256 {deploy.render_sha256(text.splitlines())}",
                          f"binary {text.splitlines()[0]}"]
            if unit in files:
                lines.append(f"unit_sha256 {hashlib.sha256(files[unit]).hexdigest()}")
            return "\n".join(lines)

        if self.clock() * 1000 > not_after:
            return result(False, "late")
        source = args + (".prev" if rollback else ".next")
        if source not in files:
            return result(False, f"nothing to put in force: no {source}")
        binary = files[source].decode().splitlines()[0]
        if binary not in files:
            return result(False, f"not an executable on this node: {binary}")
        if rollback:
            files[args + ".failed"] = files[args]
            files[args] = files[source]
        else:
            if f"{self.ETC}/{service}.unit.next" in files:
                if unit in files:
                    files[f"{self.ETC}/{service}.unit.prev"] = files[unit]
                files[unit] = files[f"{self.ETC}/{service}.unit.next"]
            if args in files:
                files[args + ".prev"] = files[args]
            files[args] = files.pop(source)
        self.running[name] = True
        self.boots[name] = int(self.clock() * 1_000_000)
        return result(True)

    def document(self, name: str, role: str) -> dict | None:
        service = "needletail-contrib" if role == "contributor" else "needletail-mesh"
        text = self.fs(name).get(f"{self.ETC}/{service}.args")
        if not self.running.get(name) or text is None:
            return None
        text = text.decode()
        git, _, unix = text.splitlines()[0].split("/")[-2].rpartition("-")
        build = ({"id": git, "unix": int(unix)} if role == "contributor"
                 else {"git": git, "built_unix": int(unix), "dirty": False})
        drained = name in self.drained
        return {"schema": "needletail.node.v1", "node_id": name, "role": role, "build": build,
                "process": {"boot_id": self.boots.get(name, 1),
                            "args_sha256": hashlib.sha256(text[:-1].encode()).hexdigest()},
                "state": "draining" if drained else "ready", "ready": not drained, "warm": True,
                "ready_forced": False, "not_ready": ["draining: operator"] if drained else [],
                "fast_plane": {"threads": 2, "threads_expected": 2},
                "client_udp": self.udp.get(name, {
                    "subscribers": 0, "hls_sessions": 0, "hls_moved_active": 0,
                    "hls_requests_in_flight": 0, "single_legs": 0, "legs_by_other": {},
                    "load_permille": 0})}


class FakeHttp:
    """mesh_ops.Http on FakeHosts: `/api/node` of each node by its public
    address."""

    def __init__(self, hosts: FakeHosts):
        self.hosts = hosts

    def get(self, host, port, path):
        node = self.hosts.by_address.get(host)
        document = self.hosts.document(node["name"], node["role"]) if node else None
        if document is not None and path == "/api/assignment":
            # An entry lists its region's edges that run, fresh unless drained.
            caches = [{"node_id": name, "fresh": True, "draining": name in self.hosts.drained}
                      for name, other in self.hosts.by_address_name.items()
                      if other["site"] == node["site"] and other["role"] == "edge"
                      and self.hosts.running.get(name)]
            return 200, {"caches": caches}
        return (200, document) if document is not None and path == "/api/node" else (None, None)

    def post(self, host, port, path, body):
        node = self.hosts.by_address.get(host)
        if node is None or body.get("node_id") != node["name"]:
            return 400, None
        if path.endswith("/drain-node"):
            self.hosts.drained.add(node["name"])
        elif path.endswith("/resume-node"):
            self.hosts.drained.discard(node["name"])
        return 202, None

    def poll(self, targets):
        return {name: self.get(*target) for name, target in targets.items()}

    def close(self):
        pass


class FakeLab(Base):
    """A Linode lab of the scale topology on FakeHosts: its state file, this
    worktree's settings and routes (of lab "test"), roots that hold build
    OLD of av-mesh and CONTRIB of av-contrib, and the lab tool's watchdog
    and state lock as fakes. No ssh, rsync or HTTP leaves the process."""

    def setUp(self):
        super().setUp()
        self.state_file = self.dir / "lab.json"
        full = topology_state(json.loads(SCALE.read_text()), created=("lon-edge-11",))
        self.state_file.write_text(json.dumps(full))
        self.hosts = FakeHosts()
        self.hosts.by_address = {node["public_ipv4"]: node for node in full["nodes"].values()}
        self.hosts.by_address_name = dict(full["nodes"])
        self.roots = self.dir / "roots"
        self.set_root("av-mesh", OLD)
        fake_binary(self.roots / "contrib" / "av-contrib", "av-contrib", CONTRIB)
        for tool in ("aep1-48k-probe", "daw-test-source", "soundkit-part-check",
                     "needletail-reader", "needletail-udp-client", "needletail-load",
                     "h3-static-capacity", "ntcmp"):
            fake_binary(self.roots / "tools" / tool, tool, CONTRIB)
        tls = self.dir / "tls"
        tls.mkdir()
        for name in ("fullchain.pem", "privkey.pem", "ca.crt"):
            (tls / name).write_text(name)
        lab = types.SimpleNamespace(change_state=self.change_state,
                                    watchdog_ensure=lambda state, path: [])
        patcher = mock.patch.dict("os.environ", {"NEEDLETAIL_LINODE_LAB_STATE":
                                                 str(self.state_file)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for target, name, value in (
                (deploy.common, "ssh_text", self.hosts.ssh_text),
                (deploy.common, "rsync_to", self.hosts.rsync_to),
                (deploy.common, "rsync_from", self.hosts.rsync_from),
                (deploy, "binaries_for", self.binaries_for),
                (deploy, "lab_tool", lambda: lab),
                (deploy.mesh_ops, "Http", lambda *a, **k: FakeHttp(self.hosts)),
                (deploy, "ensure_tls", lambda: None),
                (deploy, "measure_routes", lambda state, settings=None: dict(ROUTES)),
                (deploy, "compile_plan", self.fake_compile)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The first curve deploys from this worktree, of lab "test".
        with redirect_stderr(io.StringIO()):
            deploy.load_settings(FIRST_CURVE, lab_run_id="test")
        # The whole lab was pushed once: its routes are measured.
        (self.dir / "routes.json").write_text(json.dumps({**ROUTES, "topology": "merged",
                                                          "lab_run_id": "test"}))

    def set_root(self, binary: str, build: tuple, flags: str = LIFECYCLE_FLAGS) -> None:
        folder = "mesh" if binary == "av-mesh" else "contrib"
        fake_binary(self.roots / folder / binary, binary, build, flags)

    def binaries_for(self, role, node=None):
        tools = self.roots / "tools"
        if role == "contributor":
            return [self.roots / "contrib" / "av-contrib", tools / "aep1-48k-probe",
                    tools / "daw-test-source"]
        if role == "load":
            return [tools / name for name in ("aep1-48k-probe", "needletail-reader",
                                              "needletail-udp-client", "daw-test-source")]
        return [self.roots / "mesh" / "av-mesh", tools / "aep1-48k-probe",
                tools / "soundkit-part-check"]

    @staticmethod
    def change_state(path, state, change):
        current = json.loads(pathlib.Path(path).read_text())
        change(current)
        pathlib.Path(path).write_text(json.dumps(current))
        state.clear()
        state.update(current)

    @staticmethod
    def fake_compile(program):
        if not COMPILER.is_file():
            raise unittest.SkipTest("needs target/release/needletail-compile")
        return REAL_COMPILE(program)

    def namespace(self, **kwargs):
        base = {"set": [], "remeasure": False, "skip_bootstrap": False,
                "skip_binaries": False, "nodes": None, "roles": "", "sites": None,
                "force": False, "allow_dirty": False, "marks": None, "node_marks": None,
                "dry_marks": False, "resume": False, "adopt_settings": False,
                "gate_timeout_s": 30.0, "drain_timeout_s": 30.0, "ready_timeout_s": 60.0,
                "on_failure": "stop", "surge": "auto", "parity_ms": 1.0, "video_gap": "refuse",
                "hold_s": 0.0, "rollback": False, "outage": False}
        return argparse.Namespace(**{**base, **kwargs})

    def quietly(self, function, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            try:
                return function(*args)
            except SystemExit as caught:
                self.stderr = err.getvalue()
                raise
            finally:
                self.stderr = err.getvalue()

    def push(self) -> None:
        self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True))
        self.hosts.touched.clear()
        self.hosts.scripts.clear()

    def state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def saved_standby(self) -> list[str]:
        return json.loads((self.dir / "settings.json").read_text())["standby_edges"]

    def ops(self, **kwargs) -> "deploy.LinodeOps":
        with redirect_stderr(io.StringIO()):
            return deploy.linode_ops(self.namespace(**kwargs))

    def args_of(self, name: str, suffix: str = "") -> list[str]:
        service = "needletail-contrib" if name.startswith("contrib") else "needletail-mesh"
        return self.hosts.fs(name)[f"/etc/needletail/{service}.args{suffix}"].decode().splitlines()

    def edges(self, site: str = "london") -> list[str]:
        standby = set(self.saved_standby())
        return sorted(name for name, node in self.state()["nodes"].items()
                      if node["role"] == "edge" and node["site"] == site and name not in standby)


def key(build: tuple) -> str:
    return f"{build[0]}-{build[1]}"


class NodesByName(FakeLab):
    """push, start and stop --nodes act on the named nodes and on nothing
    else: no ssh or rsync reaches another node."""

    def test_push_nodes_touches_only_the_named_node(self):
        self.push()
        self.quietly(deploy.command_push, self.namespace(nodes="lon-edge-11"))
        self.assertEqual(set(self.hosts.touched), {"lon-edge-11"})
        self.assertIn("/opt/needletail/activate needletail-mesh",
                      self.hosts.scripts["lon-edge-11"][-1])

    def test_start_takes_a_standby_edge_in_and_touches_nothing_else(self):
        self.push()
        self.quietly(deploy.command_start, self.namespace(nodes="lon-edge-6"))
        self.assertEqual(set(self.hosts.touched), {"lon-edge-6"})
        self.assertIn("/opt/needletail/activate needletail-mesh",
                      self.hosts.scripts["lon-edge-6"][-1])
        self.assertNotIn("lon-edge-6", self.saved_standby())
        self.assertIn("lon-edge-7", self.saved_standby())

    def test_stop_holds_a_node_out_and_touches_nothing_else(self):
        self.push()
        self.quietly(deploy.command_stop, self.namespace(nodes="lon-edge-11"))
        self.assertEqual(set(self.hosts.touched), {"lon-edge-11"})
        self.assertIn("systemctl disable --now", self.hosts.scripts["lon-edge-11"][-1])
        self.assertIn("lon-edge-11", self.saved_standby())

    def test_start_and_stop_refuse_what_they_must_not_touch(self):
        for verb, nodes in ((deploy.command_stop, "lon-dist-a"),
                            (deploy.command_stop, "contrib-lon"),
                            (deploy.command_start, "lon-edge-12"),   # a slot not created
                            (deploy.command_start, "arch-us-east"),
                            (deploy.command_stop, "lon-load-1")):
            with self.assertRaises(SystemExit, msg=nodes):
                self.quietly(verb, self.namespace(nodes=nodes))
        self.assertEqual(self.hosts.touched, [])


class Deployed(FakeLab):
    """What is deployed is recorded and later commands are held to it (C
    2.5, 2.6): a roll stages files only, in parallel, on the nodes it rolls;
    the previous arguments stay beside the previous binary; settings that
    do not reproduce the record, a `--set` outside ROLLABLE_SETTINGS or one
    that changes a node outside the command, and the files of another lab
    are refused; start runs the deployed build, not the root's; hunt is
    refused while edges serve."""

    def test_push_records_what_each_node_runs(self):
        self.push()
        deployed = self.state()["deployed"]
        self.assertEqual(deployed["tiers"], {"contributors": key(CONTRIB), "edges": key(OLD),
                                             "forwarders": key(OLD)})
        entry = deployed["nodes"]["lon-edge-1"]
        self.assertEqual(entry["build"], key(OLD))
        self.assertEqual(entry["args_sha256"],
                         hashlib.sha256("\n".join(self.args_of("lon-edge-1")).encode()).hexdigest())
        self.assertEqual(self.args_of("lon-edge-1")[0], f"/opt/needletail/builds/{key(OLD)}/av-mesh")
        self.assertEqual(self.args_of("contrib-lon")[0],
                         f"/opt/needletail/builds/{key(CONTRIB)}/av-contrib")
        # The lifecycle flags of a build that has them (C 2.2, B 3.12).
        args = self.args_of("lon-edge-1")
        self.assertEqual(deploy.flag_value(args, "--stop-drain-ms"), "14000")
        self.assertEqual(deploy.flag_value(args, "--drain-state-file"),
                         "/var/lib/needletail/needletail-mesh.drained")
        self.assertIsNone(deploy.flag_value(self.args_of("lon-dist-a"), "--stop-drain-ms"))
        # A standby edge is staged and left stopped; nothing of the record.
        self.assertIn("/etc/needletail/needletail-mesh.args.next", self.hosts.fs("lon-edge-6"))
        self.assertNotIn("lon-edge-6", deployed["nodes"])
        self.assertEqual(deployed["settings"]["standby_edges"], self.saved_standby())
        self.assertEqual(deployed["routes_sha256"],
                         deploy.sha256_file(self.dir / "routes.json"))
        self.assertEqual(json.loads((self.dir / "settings.json").read_text())["lab_run_id"], "test")

    def test_roll_stages_files_only_and_in_parallel(self):
        self.push()
        self.set_root("av-mesh", NEW)
        ops = self.ops()
        edges = self.edges("london") + self.edges("tokyo")
        before = {name: self.args_of(name) for name in edges}
        self.hosts.most_inflight = 0
        ops.stage(edges, ops.target("edges"))
        # Files only: no restart, no kernel setting, no qdisc, no activation.
        for name in edges:
            for script in self.hosts.scripts[name]:
                for word in ("systemctl restart", "systemd-run", "sysctl -q -p", "tc qdisc"):
                    self.assertNotIn(word, script.replace(deploy.PRUNE_BUILDS, ""), name)
            self.assertEqual(self.args_of(name), before[name])
            staged = self.args_of(name, ".next")
            self.assertEqual(staged[0], f"/opt/needletail/builds/{key(NEW)}/av-mesh")
            for suffix in ("unit", "sysctl", "qdisc"):
                self.assertIn(f"/etc/needletail/needletail-mesh.{suffix}.next",
                              self.hosts.fs(name))
            self.assertIn(f"/opt/needletail/builds/{key(NEW)}/av-mesh", self.hosts.fs(name))
        # Every edge at once, not one after another.
        self.assertGreater(self.hosts.most_inflight, 4)
        self.assertEqual(ops.expected(edges[0])["git"], NEW[0])

    def test_a_roll_of_a_region_s_edges_runs_on_the_engine(self):
        """LinodeOps under mesh_ops.roll: the reproduce check, the staging,
        then each London edge gated, drained, activated, ready on the new
        build, resumed and listed again; two marks on stdout; the record
        follows each node."""
        self.push()
        self.set_root("av-mesh", NEW)
        clock = types.SimpleNamespace(now=time.time())
        clock_now = lambda: clock.now  # noqa: E731

        def sleep(seconds):
            clock.now += max(seconds, 0.001)

        self.hosts.clock = clock_now
        out, err = io.StringIO(), io.StringIO()
        code = deploy.mesh_ops.roll(self.ops(), "edges", sites=["london"], out=out, err=err,
                                    clock=clock_now, sleep=sleep)
        self.assertEqual(code, 0, err.getvalue())
        marks = out.getvalue().splitlines()
        self.assertEqual([line.split()[0] + " " + line.split()[2] for line in marks],
                         ["start roll.edges", "end roll.edges"])
        london = self.edges("london")
        deployed = self.state()["deployed"]
        for name in london:
            self.assertEqual(self.args_of(name)[0], f"/opt/needletail/builds/{key(NEW)}/av-mesh")
            self.assertEqual(deployed["nodes"][name]["build"], key(NEW), name)
            self.assertIn(f"/opt/needletail/builds/{key(OLD)}/av-mesh",
                          self.args_of(name, ".prev")[0])
        self.assertEqual(deployed["nodes"]["new-edge-1"]["build"], key(OLD))
        self.assertEqual(self.hosts.drained, set())
        # Every other node is only asked the time (the clock check, one node
        # a region).
        for name, scripts in self.hosts.scripts.items():
            if name not in london:
                self.assertEqual(set(scripts), {"date +%s.%N"}, name)
        self.assertEqual(len(set(self.hosts.scripts) - set(london)), 4)

    def engine(self):
        """A fake clock shared by the engine and the hosts: (clock, sleep);
        it goes on from where the last command's left it."""
        clock = types.SimpleNamespace(now=max(time.time(), getattr(self, "last_now", 0.0)))
        clock_now = lambda: clock.now  # noqa: E731

        def sleep(seconds):
            clock.now += max(seconds, 0.001)
            self.last_now = clock.now

        self.hosts.clock = clock_now
        return {"out": io.StringIO(), "err": io.StringIO(), "clock": clock_now, "sleep": sleep}

    def test_restart_nodes_keeps_the_recorded_build_and_its_rollback_puts_back_the_last(self):
        self.push()
        self.set_root("av-mesh", NEW)
        run = self.engine()
        self.assertEqual(deploy.mesh_ops.roll(self.ops(), "edges", nodes=["lon-edge-1"], **run),
                         0, run["err"].getvalue())
        old_two = self.args_of("lon-edge-2")
        boots = dict(self.hosts.boots)
        run = self.engine()
        code = deploy.mesh_ops.restart_nodes(self.ops(), ["lon-edge-1", "lon-edge-2"], **run)
        self.assertEqual(code, 0, run["err"].getvalue())
        # Each on the build it runs, never the root's: lon-edge-1 stays on
        # NEW, lon-edge-2 on OLD with the arguments it had.
        self.assertEqual(self.args_of("lon-edge-1")[0],
                         f"/opt/needletail/builds/{key(NEW)}/av-mesh")
        self.assertEqual(self.args_of("lon-edge-2"), old_two)
        self.assertNotEqual(self.hosts.boots["lon-edge-2"], boots["lon-edge-2"])
        # The operator's rollback of lon-edge-3 after its roll: the
        # arguments before its last activation, on the build it ran then.
        run = self.engine()
        self.assertEqual(deploy.mesh_ops.roll(self.ops(), "edges", nodes=["lon-edge-3"], **run),
                         0, run["err"].getvalue())
        run = self.engine()
        code = deploy.mesh_ops.restart_nodes(
            self.ops(), ["lon-edge-3"], options=deploy.mesh_ops.Options(rollback=True), **run)
        self.assertEqual(code, 0, run["err"].getvalue())
        self.assertEqual(self.args_of("lon-edge-3")[0],
                         f"/opt/needletail/builds/{key(OLD)}/av-mesh")
        self.assertEqual(self.state()["deployed"]["nodes"]["lon-edge-3"]["build"], key(OLD))

    def test_push_nodes_installs_the_tier_s_recorded_build(self):
        self.push()
        self.set_root("av-mesh", NEW)
        self.quietly(deploy.command_push, self.namespace(nodes="lon-edge-11"))
        self.assertEqual(self.args_of("lon-edge-11")[0],
                         f"/opt/needletail/builds/{key(OLD)}/av-mesh")
        # A whole push moves the tiers to the roots' builds.
        self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True))
        self.assertEqual(self.state()["deployed"]["tiers"]["edges"], key(NEW))
        self.assertEqual(self.args_of("lon-edge-11")[0],
                         f"/opt/needletail/builds/{key(NEW)}/av-mesh")

    def test_kill_and_return_are_workstream_a_s_under_the_ops_lock(self):
        self.push()
        seen = []

        def run(ops, verb, args):
            seen.append((type(ops).__name__, verb, args.node,
                         os.environ.get(deploy.mesh_ops.OpsLock.ENV) is not None))
            return 0

        with mock.patch.object(deploy.contributor_changes, "run", run):
            for verb in ("kill", "return"):
                with self.assertRaises(SystemExit) as code:
                    self.quietly(deploy.command_change,
                                 self.namespace(command=verb, node="contrib-lon"))
                self.assertEqual(code.exception.code, 0)
        self.assertEqual(seen, [("LinodeOps", "kill", "contrib-lon", True),
                                ("LinodeOps", "return", "contrib-lon", True)])
        # This lab has one contributor: A's gate refuses its kill (exit 2),
        # before any host is reached, and its restart by name (the engine's
        # gate after its staging, exit 3): nothing is activated or killed.
        with self.assertRaises(SystemExit) as code:
            self.quietly(deploy.command_change, self.namespace(
                command="kill", node="contrib-lon", unprotected=False, gate_timeout_s=1.0))
        self.assertEqual(code.exception.code, 2)
        self.assertIn("K1: contrib-lon is the lab's only contributor", self.stderr)
        self.assertEqual(self.hosts.touched, [])
        with self.assertRaises(SystemExit) as code:
            self.quietly(deploy.command_restart, self.namespace(nodes="contrib-lon"))
        self.assertEqual(code.exception.code, 3)
        self.assertIn("K1: contrib-lon is the lab's only contributor", self.stderr)
        self.assertEqual([script for scripts in self.hosts.scripts.values() for script in scripts
                          if "systemd-run" in script or "systemctl" in script], [])

    def test_status_names_the_build_the_record_and_what_a_cut_off_command_left(self):
        self.push()
        state = self.state()
        document = self.hosts.document("lon-edge-1", "edge")
        line = deploy.node_summary(document, state["deployed"]["nodes"]["lon-edge-1"])
        self.assertIn(f"build={key(OLD)} state=ready drain=- boot=", line)
        self.assertTrue(line.endswith("record=same"))
        self.assertIn("record=DIFFERS", deploy.node_summary(
            document, {**state["deployed"]["nodes"]["lon-edge-1"], "args_sha256": "x"}))
        self.assertEqual(deploy.node_summary(None, None), " node=silent")
        journal = deploy.mesh_ops.Journal.create(self.dir / "rolls", "roll", 1_790_000_000,
                                                 label="roll.edges")
        journal.update(in_hand={"node": "lon-edge-2", "phase": "drained", "tier": "edges"})
        lines = deploy.deployed_summary(state)
        self.assertIn("this worktree's settings are the deployed ones", lines[1])
        self.assertIn("unfinished: roll.edges", lines[2])
        self.assertIn("lon-edge-2 (drained)", lines[2])

    def test_roll_changes_no_other_node_s_files(self):
        self.push()
        self.set_root("av-mesh", NEW)
        snapshot = {name: dict(files) for name, files in self.hosts.files.items()}
        ops = self.ops()
        edges = self.edges("london")
        ops.stage(edges, ops.target("edges"))
        ops.activate(edges[0], int((time.time() + 5) * 1000))
        self.assertEqual(set(self.hosts.touched), set(edges))
        for name, files in self.hosts.files.items():
            if name not in edges:
                self.assertEqual(files, snapshot.get(name, {}), name)
        # The record moves for the activated node only; the tier keeps the
        # build most of its nodes run.
        deployed = self.state()["deployed"]
        self.assertEqual(deployed["nodes"][edges[0]]["build"], key(NEW))
        self.assertEqual(deployed["nodes"][edges[1]]["build"], key(OLD))
        self.assertEqual(deployed["tiers"]["edges"], key(OLD))

    def test_the_previous_arguments_are_kept_with_the_previous_binary(self):
        self.push()
        self.set_root("av-mesh", NEW)
        ops = self.ops()
        name = "lon-edge-1"
        old_args = self.args_of(name)
        ops.stage([name], ops.target("edges"))
        self.assertTrue(ops.activate(name, int((time.time() + 5) * 1000))["ok"])
        self.assertEqual(self.args_of(name, ".prev"), old_args)
        self.assertEqual(self.args_of(name)[0], f"/opt/needletail/builds/{key(NEW)}/av-mesh")
        # The previous binary is still on the node, where .args.prev names it.
        self.assertIn(old_args[0], self.hosts.fs(name))
        self.assertEqual(ops.expected(name, rollback=True),
                         {"git": OLD[0], "unix": OLD[1],
                          "args_sha256": hashlib.sha256("\n".join(old_args).encode()).hexdigest()})
        self.assertTrue(ops.activate(name, int((time.time() + 5) * 1000), rollback=True)["ok"])
        self.assertEqual(self.args_of(name), old_args)
        self.assertEqual(self.state()["deployed"]["nodes"][name]["build"], key(OLD))
        # A late activation changes nothing.
        ops.stage([name], ops.target("edges"))
        self.assertEqual(ops.activate(name, int((time.time() - 5) * 1000)),
                         {"ok": False, "why": "late"})
        self.assertEqual(self.args_of(name), old_args)

    def test_a_roll_from_stale_settings_is_refused(self):
        self.push()
        # Another value saved in this worktree since the push (the trap of
        # an earlier run's --set): refused, by name, exit 2.
        with redirect_stderr(io.StringIO()):
            deploy.load_settings(["mesh_sync_interval_ms=40"], lab_run_id="test")
        with self.assertRaises(deploy.mesh_ops.Refused) as caught:
            self.ops().check("roll", self.edges())
        self.assertIn("mesh_sync_interval_ms=40 (deployed 20)", str(caught.exception))
        with self.assertRaises(SystemExit) as code:
            self.quietly(deploy.command_roll, self.namespace(tier="edges"))
        self.assertEqual(code.exception.code, 2)
        self.assertIn("do not reproduce what is deployed", self.stderr)
        self.assertEqual(self.hosts.touched, [])
        # --adopt-settings goes on, and says what it adopts.
        with redirect_stderr(io.StringIO()) as err:
            self.ops(adopt_settings=True).check("roll", self.edges())
        self.assertIn("adopting", err.getvalue())
        # A node that runs other arguments than the record renders is named
        # even when no setting differs.
        with redirect_stderr(io.StringIO()):
            deploy.load_settings(["mesh_sync_interval_ms=20"], lab_run_id="test")
        self.ops().check("roll", self.edges())
        # The contributor's arguments carry the time of their render
        # (--relay-path-observed-at-unix-ms): a later render reproduces
        # them all the same.
        self.assertIn("--relay-path-observed-at-unix-ms", self.args_of("contrib-lon"))
        self.ops().check("roll", ["contrib-lon", "lon-dist-a"])
        files = self.hosts.fs("lon-edge-2")
        files["/etc/needletail/needletail-mesh.args"] += b"--extra\n"
        with self.assertRaises(deploy.mesh_ops.Refused) as caught:
            self.ops().check("roll", self.edges())
        self.assertIn("lon-edge-2", str(caught.exception))
        self.assertNotIn("lon-edge-1,", str(caught.exception))

    def test_a_setting_outside_the_allow_list_cannot_roll(self):
        self.push()
        with self.assertRaises(deploy.mesh_ops.Refused) as caught:
            self.ops(set=["mesh_sync_interval_ms=40"]).check("roll", self.edges())
        self.assertIn("mesh_sync_interval_ms", str(caught.exception))
        self.assertIn("needs push", str(caught.exception))
        with self.assertRaises(SystemExit) as code:
            self.quietly(deploy.command_roll,
                         self.namespace(tier="edges", set=["mesh_sync_interval_ms=40"]))
        self.assertEqual(code.exception.code, 2)
        self.assertEqual(self.hosts.touched, [])
        # Nothing of a refused --set stays in this worktree's settings.
        self.assertEqual(json.loads((self.dir / "settings.json").read_text())
                         ["mesh_sync_interval_ms"], 20)

    def test_a_set_that_changes_a_node_outside_the_tier_is_refused(self):
        self.push()
        # malloc_tune renders on every mesh node: the forwarders are outside
        # a roll of the edges.
        with self.assertRaises(deploy.mesh_ops.Refused) as caught:
            self.ops(set=["malloc_tune=false"]).check("roll", self.edges())
        self.assertIn("lon-dist-a", str(caught.exception))
        self.assertNotIn("lon-edge-1", str(caught.exception))
        # The edges' own extra arguments stay inside it.
        edges = [name for site in ("sydney", "london", "newyork", "tokyo")
                 for name in self.edges(site)]
        ops = self.ops(set=['extra_edge_args=["--egress-request-capacity","100000000"]'])
        ops.check("roll", edges)
        self.assertEqual(sorted(ops.changed_by_set()), sorted(edges))

    def test_start_uses_the_deployed_build_not_the_root(self):
        self.push()
        self.set_root("av-mesh", NEW)  # the root was rebuilt since the push
        self.quietly(deploy.command_start, self.namespace(nodes="lon-edge-6"))
        self.assertEqual(self.args_of("lon-edge-6")[0],
                         f"/opt/needletail/builds/{key(OLD)}/av-mesh")
        self.assertNotIn(f"/opt/needletail/builds/{key(NEW)}/av-mesh",
                         self.hosts.fs("lon-edge-6"))
        self.assertEqual(self.state()["deployed"]["nodes"]["lon-edge-6"]["build"], key(OLD))
        self.assertNotIn("lon-edge-6", self.state()["deployed"]["settings"]["standby_edges"])
        # A roll takes the root's build: the tier's target.
        self.assertEqual(self.ops().target("edges")["git"], NEW[0])

    def test_settings_of_another_lab_are_refused(self):
        settings = self.dir / "settings.json"
        document = json.loads(settings.read_text())
        for owner in ("other", None):
            with self.subTest(owner=owner):
                document.pop("lab_run_id", None)
                if owner:
                    document["lab_run_id"] = owner
                settings.write_text(json.dumps(document))
                for command, namespace in (
                        (deploy.command_push, self.namespace(skip_bootstrap=True)),
                        (deploy.command_roll, self.namespace(tier="edges")),
                        (deploy.command_start, self.namespace(nodes="lon-edge-6")),
                        (deploy.command_stop, self.namespace(nodes="lon-edge-11"))):
                    with self.assertRaises(SystemExit) as code:
                        self.quietly(command, namespace)
                    self.assertEqual(code.exception.code, 2, command.__name__)
                    self.assertIn("not this lab's (test)", self.stderr)
                self.assertEqual(json.loads(settings.read_text()).get("lab_run_id"), owner)
        self.assertEqual(self.hosts.touched, [])
        # The routes of another lab: refused unless measured again.
        document["lab_run_id"] = "test"
        settings.write_text(json.dumps(document))
        routes = self.dir / "routes.json"
        routes.write_text(json.dumps({**ROUTES, "topology": "merged", "lab_run_id": "other"}))
        with self.assertRaises(SystemExit) as code:
            self.quietly(deploy.command_plan, self.namespace())
        self.assertEqual(code.exception.code, 2)
        self.quietly(deploy.command_plan, self.namespace(remeasure=True))
        self.assertEqual(json.loads(routes.read_text())["lab_run_id"], "test")
        (self.dir / "ecmp-ports.json").write_text(json.dumps({"lab_run_id": "other"}))
        with self.assertRaises(SystemExit) as code:
            self.quietly(deploy.command_plan, self.namespace())
        self.assertEqual(code.exception.code, 2)

    def test_hunt_refuses_under_listeners(self):
        self.push()
        hunted = []
        with mock.patch.object(deploy, "hunt_ports", lambda state, settings: hunted.append(1)):
            self.hosts.udp["new-edge-2"] = {"subscribers": 3, "hls_sessions": 0}
            with self.assertRaises(SystemExit) as code:
                self.quietly(deploy.command_hunt, self.namespace())
            self.assertEqual(code.exception.code, 2)
            self.assertIn("new-edge-2: 3 subscribers", self.stderr)
            self.hosts.udp["new-edge-2"] = {"subscribers": 0, "hls_sessions": 0}
            self.hosts.running["tok-edge-1"] = False   # does not say
            with self.assertRaises(SystemExit):
                self.quietly(deploy.command_hunt, self.namespace())
            self.assertIn("tok-edge-1: no answer", self.stderr)
            self.assertEqual(hunted, [])
            self.quietly(deploy.command_hunt, self.namespace(outage=True))
            self.assertEqual(hunted, [1])
            self.hosts.running["tok-edge-1"] = True
            self.quietly(deploy.command_hunt, self.namespace())
            self.assertEqual(hunted, [1, 1])
            # Under another command's ops lock: refused.
            lock = deploy.mesh_ops.OpsLock(self.dir / "lab.json.ops.lock").acquire()
            try:
                with mock.patch.dict("os.environ", {}, clear=False):
                    os.environ.pop(lock.ENV, None)
                    with self.assertRaises(SystemExit) as code:
                        self.quietly(deploy.command_hunt, self.namespace())
                self.assertEqual(code.exception.code, 2)
            finally:
                lock.release()
            self.assertEqual(hunted, [1, 1])



class Recorded(FakeLab):
    """The deployed record stays what the lab runs (CT2 review): an
    activation whose ssh dropped, a command killed during one and a push
    that failed part-way are settled by what each node runs; a `--set`
    stopped part-way is held per node until every node runs it; a
    contributor whose arguments carry the time of their render is on its
    recorded build; a node that runs nothing leaves the record; the build
    cache holds one binary a build; a change without the lab tool of CT3
    is refused before it begins."""

    engine = Deployed.engine
    GIVEN = ['extra_edge_args=["--egress-request-capacity","100000000"]']

    def setUp(self):
        super().setUp()
        # What LinodeOps says on stderr (a settled activation, a fold).
        quiet = redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def serving_edges(self) -> list[str]:
        return [name for site in ("sydney", "london", "newyork", "tokyo")
                for name in self.edges(site)]

    def digest(self, name: str) -> str:
        return hashlib.sha256("\n".join(self.args_of(name)).encode()).hexdigest()

    def test_an_activation_whose_ssh_dropped_is_recorded_by_what_the_node_runs(self):
        self.push()
        self.set_root("av-mesh", NEW)
        real = deploy.activate_node

        def dropped(node, *args, **kwargs):
            result = real(node, *args, **kwargs)
            if node["name"] == "lon-edge-1":
                raise RuntimeError("ssh: connection reset")
            return result

        run = self.engine()
        with mock.patch.object(deploy, "activate_node", dropped):
            code = deploy.mesh_ops.roll(self.ops(), "edges", sites=["london"], **run)
        self.assertEqual(code, 0, run["err"].getvalue())
        self.assertEqual(self.args_of("lon-edge-1")[0], f"/opt/needletail/builds/{key(NEW)}/av-mesh")
        entry = self.state()["deployed"]["nodes"]["lon-edge-1"]
        self.assertEqual((entry["build"], entry["activating"]["build"]), (key(OLD), key(NEW)))
        # The next command settles it by what the node runs, and goes on.
        with redirect_stderr(io.StringIO()):
            self.ops().check("roll", self.serving_edges())
        entry = self.state()["deployed"]["nodes"]["lon-edge-1"]
        self.assertNotIn("activating", entry)
        self.assertEqual((entry["build"], entry["args_sha256"], entry["prev"]["build"]),
                         (key(NEW), self.digest("lon-edge-1"), key(OLD)))

    def test_a_roll_killed_during_an_activation_is_resumed(self):
        self.push()
        self.set_root("av-mesh", NEW)
        real = deploy.activate_node

        class Killed(BaseException):
            pass

        def killed(node, *args, **kwargs):
            real(node, *args, **kwargs)
            raise Killed()

        run = self.engine()
        with mock.patch.object(deploy, "activate_node", killed), self.assertRaises(Killed):
            deploy.mesh_ops.roll(self.ops(), "edges", sites=["london"], **run)
        self.assertEqual(self.hosts.drained, {"lon-edge-1"})
        run = self.engine()
        code = deploy.mesh_ops.roll(self.ops(), "edges", sites=["london"], resume=True, **run)
        self.assertEqual(code, 0, run["err"].getvalue())
        nodes = self.state()["deployed"]["nodes"]
        for name in self.edges("london"):
            self.assertEqual(nodes[name]["build"], key(NEW), name)
            self.assertNotIn("activating", nodes[name])
            self.assertEqual(nodes[name]["args_sha256"], self.digest(name), name)
        self.assertEqual(self.hosts.drained, set())

    def test_a_push_that_fails_on_one_node_records_every_node_that_activated(self):
        self.push()
        self.set_root("av-mesh", NEW)
        real = deploy.activate_node

        def refused(node, *args, **kwargs):
            if node["name"] == "lon-edge-2":
                raise RuntimeError("ssh: connection reset")
            return real(node, *args, **kwargs)

        with mock.patch.object(deploy, "activate_node", refused), \
                self.assertRaises(RuntimeError):
            self.quietly(deploy.command_push, self.namespace(skip_bootstrap=True))
        nodes = self.state()["deployed"]["nodes"]
        for name in ("lon-edge-1", "new-edge-1", "lon-dist-a"):
            self.assertEqual(nodes[name]["build"], key(NEW), name)
            self.assertEqual(nodes[name]["args_sha256"], self.digest(name), name)
        self.assertEqual(nodes["lon-edge-2"]["build"], key(OLD))
        self.assertEqual(nodes["lon-edge-2"]["activating"]["build"], key(NEW))
        # It never ran: the next command drops what was begun.
        with redirect_stderr(io.StringIO()):
            self.ops().check("restart", ["lon-edge-2"])
        entry = self.state()["deployed"]["nodes"]["lon-edge-2"]
        self.assertEqual((entry["build"], "activating" in entry), (key(OLD), False))

    def test_a_roll_with_a_set_stopped_part_way_goes_on_when_run_again(self):
        self.push()
        edges = self.serving_edges()
        ops = self.ops(set=self.GIVEN)
        order = deploy.mesh_ops.plan_order(deploy.mesh_ops.Lab(ops.nodes()), "edges")
        busy = {"subscribers": 3, "hls_sessions": 0, "hls_moved_active": 0,
                "hls_requests_in_flight": 0, "single_legs": 0, "legs_by_other": {},
                "load_permille": 100}
        self.hosts.udp[order[1]] = busy
        run = self.engine()
        options = deploy.mesh_ops.Options(drain_timeout_s=2.0, gate_timeout_s=2.0)
        self.assertEqual(deploy.mesh_ops.roll(ops, "edges", options=options, **run), 3,
                         run["err"].getvalue())
        self.assertIn("--egress-request-capacity", self.args_of(order[0]))
        self.assertNotIn("--egress-request-capacity", self.args_of(order[1]))
        deployed = self.state()["deployed"]
        self.assertEqual(deployed["nodes"][order[0]]["set"],
                         {"extra_edge_args": ["--egress-request-capacity", "100000000"]})
        self.assertEqual(deployed["settings"]["extra_edge_args"],
                         deploy.DEFAULT_SETTINGS["extra_edge_args"])
        self.assertEqual(json.loads((self.dir / "settings.json").read_text())["extra_edge_args"],
                         deploy.DEFAULT_SETTINGS["extra_edge_args"])
        # The same command again, without --adopt-settings: it goes on from
        # the first edge that does not run it. Meanwhile another command
        # held an edge out in this worktree's settings.json: the fold keeps
        # that and writes only the keys of the --set.
        del self.hosts.udp[order[1]]
        ops = self.ops(set=self.GIVEN)
        document = json.loads((self.dir / "settings.json").read_text())
        document["standby_edges"] = sorted(document["standby_edges"] + ["tok-edge-5"])
        (self.dir / "settings.json").write_text(json.dumps(document))
        run = self.engine()
        self.assertEqual(deploy.mesh_ops.roll(ops, "edges", **run), 0, run["err"].getvalue())
        self.assertIn(f"plan: {order[0]} skipped", run["err"].getvalue())
        for name in edges:
            self.assertIn("--egress-request-capacity", self.args_of(name), name)
        deployed = self.state()["deployed"]
        self.assertEqual(deployed["settings"]["extra_edge_args"],
                         ["--egress-request-capacity", "100000000"])
        self.assertEqual([name for name, entry in deployed["nodes"].items() if "set" in entry], [])
        saved = json.loads((self.dir / "settings.json").read_text())
        self.assertEqual(saved["extra_edge_args"], ["--egress-request-capacity", "100000000"])
        self.assertIn("tok-edge-5", saved["standby_edges"])
        # The next command reproduces the lab from the worktree, without
        # --set; the same roll again restarts nothing.
        with redirect_stderr(io.StringIO()):
            self.ops().check("roll", self.serving_edges())
        self.hosts.scripts.clear()
        run = self.engine()
        self.assertEqual(deploy.mesh_ops.roll(self.ops(set=self.GIVEN), "edges", **run), 0,
                         run["err"].getvalue())
        self.assertFalse(any("systemd-run" in script for scripts in self.hosts.scripts.values()
                             for script in scripts))

    def test_a_rollback_after_the_fold_runs_what_the_node_ran_before(self):
        """The last edge to take a --set folds it into the deployed
        settings; when its rollback follows, the record still says what it
        runs (the setting before, as its own)."""
        self.push()
        run = self.engine()
        self.assertEqual(deploy.mesh_ops.roll(self.ops(set=self.GIVEN), "edges", **run), 0,
                         run["err"].getvalue())
        name = self.serving_edges()[0]
        run = self.engine()
        code = deploy.mesh_ops.restart_nodes(
            self.ops(), [name], options=deploy.mesh_ops.Options(rollback=True), **run)
        self.assertEqual(code, 0, run["err"].getvalue())
        self.assertNotIn("--egress-request-capacity", self.args_of(name))
        entry = self.state()["deployed"]["nodes"][name]
        self.assertEqual(entry["set"], {"extra_edge_args": deploy.DEFAULT_SETTINGS["extra_edge_args"]})
        with redirect_stderr(io.StringIO()):
            self.ops().check("restart", self.serving_edges())

    def test_a_contributor_on_its_recorded_build_is_on_the_target(self):
        self.push()
        running = self.args_of("contrib-lon")
        time.sleep(0.002)
        ops = self.ops()
        fresh, _unit, _service = ops.node_args("contrib-lon", ops.base, key(CONTRIB))
        self.assertNotEqual(deploy.args_sha256(fresh), deploy.args_sha256(running))
        ops.stage(["contrib-lon"], None)
        self.assertEqual(self.args_of("contrib-lon", ".next"), running)
        self.assertEqual(ops.expected("contrib-lon")["args_sha256"], self.digest("contrib-lon"))
        # Through the engine: skipped, nothing activated.
        passes = lambda ops, node, **kwargs: (True, "", {})  # noqa: E731
        self.hosts.scripts.clear()
        with mock.patch.object(deploy.mesh_ops, "contributor_rules", lambda: (passes, passes)):
            run = self.engine()
            code = deploy.mesh_ops.roll(self.ops(), "contributors", **run)
        self.assertEqual(code, 0, run["err"].getvalue())
        self.assertIn("plan: contrib-lon skipped", run["err"].getvalue())
        self.assertFalse(any("systemd-run" in script
                             for script in self.hosts.scripts.get("contrib-lon", [])))

    def test_a_node_that_runs_nothing_leaves_the_record(self):
        self.push()
        state = self.state()
        del state["nodes"]["lon-edge-11"]  # a slot deleted
        self.state_file.write_text(json.dumps(state))
        self.ops().hold("lon-edge-5", out=True)
        nodes = self.state()["deployed"]["nodes"]
        self.assertNotIn("lon-edge-11", nodes)
        self.assertNotIn("lon-edge-5", nodes)
        self.assertIn("lon-edge-5", self.state()["deployed"]["settings"]["standby_edges"])

    def test_a_cached_build_is_never_written_again_and_a_cut_off_fetch_is_no_build(self):
        self.push()
        cached = self.dir / "builds" / key(OLD) / "av-mesh"
        before = cached.read_bytes()
        # Another binary that names the same build: refused, the cache as it was.
        fake_binary(self.roots / "mesh" / "av-mesh", "av-mesh", OLD, "--other-flags")
        with self.assertRaises(deploy.mesh_ops.Refused) as caught:
            deploy.cache_build("edge")
        self.assertIn("a build id names one binary", str(caught.exception))
        self.assertEqual(cached.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in (self.dir / "builds").iterdir()),
                         sorted([key(OLD), key(CONTRIB)]))
        # A fetch cut off left the directory without its binary: the build
        # comes whole from a node that runs it.
        shutil.rmtree(cached.parent)
        cached.parent.mkdir()
        (cached.parent / "aep1-48k-probe").write_text("part of a fetch")
        self.quietly(deploy.command_start, self.namespace(nodes="lon-edge-6"))
        self.assertEqual(cached.read_bytes(), before)
        args = self.args_of("lon-edge-6")
        self.assertEqual(args[0], f"/opt/needletail/builds/{key(OLD)}/av-mesh")
        self.assertEqual(deploy.flag_value(args, "--stop-drain-ms"), "14000")

    def test_without_the_lab_tool_of_ct3_a_change_is_refused_before_it_begins(self):
        self.push()
        lab = types.SimpleNamespace(change_state=self.change_state)
        with mock.patch.object(deploy, "lab_tool", lambda: lab):
            for command, namespace in (
                    (deploy.command_roll, self.namespace(tier="edges")),
                    (deploy.command_push, self.namespace(skip_bootstrap=True)),
                    (deploy.command_restart, self.namespace(nodes="lon-edge-1")),
                    (deploy.command_restart, self.namespace(roles="edge")),
                    (deploy.command_start, self.namespace(nodes="lon-edge-6"))):
                with self.assertRaises(SystemExit) as code:
                    self.quietly(command, namespace)
                self.assertEqual(code.exception.code, 2, command.__name__)
                self.assertIn("watchdog_ensure", self.stderr)
                self.assertIn("C CT3", self.stderr)
        self.assertEqual(self.hosts.touched, [])

    def test_the_service_s_start_is_read_to_the_microsecond(self):
        self.push()
        ops = self.ops()
        sent = []
        with mock.patch.object(deploy.common, "ssh_text",
                               lambda node, script, **kwargs: sent.append(script)
                               or "1795223802527845"):
            self.assertTrue(ops.boot_changed("lon-edge-1", 1795223802527))
            self.assertFalse(ops.boot_changed("lon-edge-1", 1795223802528))
        self.assertIn("--timestamp=us+utc", sent[0])
        self.assertIn("+%s%6N", sent[0])

    def test_the_node_in_hand_gets_a_fresh_ssh_connection(self):
        self.push()
        ops = self.ops()
        ops.status("lon-edge-1")
        ops.status("lon-edge-1")
        self.assertEqual(self.hosts.scripts["lon-edge-1"], ["true"])
        ops._ssh_at["lon-edge-1"] -= ops.SSH_REFRESH_S
        ops.status("lon-edge-1")
        self.assertEqual(self.hosts.scripts["lon-edge-1"], ["true", "true"])
        # Staging reached it: no refresh.
        ops.stage(["lon-edge-2"], None)
        count = len(self.hosts.scripts["lon-edge-2"])
        ops.status("lon-edge-2")
        self.assertEqual(len(self.hosts.scripts["lon-edge-2"]), count)
        # A node that does not answer is not reached by ssh.
        self.hosts.running["lon-edge-3"] = False
        self.assertIsNone(ops.status("lon-edge-3"))
        self.assertNotIn("lon-edge-3", self.hosts.scripts)

    def test_arguments_of_the_layout_before_builds_are_not_rolled_back_to(self):
        self.push()
        files = self.hosts.fs("lon-edge-1")
        files["/etc/needletail/needletail-mesh.args.prev"] = b"/usr/local/bin/av-mesh\n--x\n"
        with self.assertRaises(deploy.mesh_ops.Refused) as caught:
            self.ops().expected("lon-edge-1", rollback=True)
        self.assertIn("not a build of /opt/needletail/builds", str(caught.exception))


class Replace(FakeLab):
    """`replace --nodes X` (C 3.7, CT4) on the engine: an edge is emptied,
    stopped, rebuilt by the lab tool with the command's budget, its host key
    forgotten, pushed on its recorded build and resumed, and nothing else
    restarts; a forwarder goes through its region's slot, and a slot that
    the lab tool refuses leaves it untouched; what the lab tool would refuse
    after the node was stopped (its watchdog, the instance's tag, the
    budget) and a new instance whose public address other nodes name are
    refused before anything; a new instance whose create fails is named
    with its way back."""

    engine = Deployed.engine
    TYPES = {plan: {"id": plan, "class": "dedicated", "price": {"hourly": price},
                    "region_prices": []}
             for plan, price in (("g6-dedicated-2", 0.054), ("g6-dedicated-4", 0.108),
                                 ("g6-dedicated-8", 0.216))}
    REGIONS = {region: {"id": region, "status": "ok", "capabilities": ["Vlans"]}
               for region in ("gb-lon", "us-east", "ap-northeast", "ap-southeast")}

    def setUp(self):
        super().setUp()
        self.lab_calls: list[list[str]] = []
        self.refuse: set[str] = set()
        self.fail_create = False
        self.forgotten: list[tuple] = []
        self.watchdog: list[str] = []
        self.foreign: set[str] = set()
        self.over_budget = False
        self.budgets: list[dict] = []
        self.argv_errors: list[str] = []
        self.addCleanup(lambda: self.assertEqual(self.argv_errors, []))
        state = self.state()
        for index, node in enumerate(state["nodes"].values(), 1):
            node.update(id=1000 + index, label=f"ntsm-test-{node['name']}",
                        hourly_usd=self.TYPES[node["type"]]["price"]["hourly"])
        state.update(tag="needletail-small-mesh-test", topology_file=str(SCALE),
                     watchdog_deadline_unix=int(time.time()) + 3 * 3600,
                     created_unix=int(time.time()) - 600, max_nodes=200, max_hourly_usd=20.0)
        self.state_file.write_text(json.dumps(state))
        api = types.SimpleNamespace(list_all=lambda path: list(
            (self.TYPES if path == "/linode/types" else self.REGIONS).values()))
        lab = types.SimpleNamespace(
            change_state=self.change_state, watchdog_ensure=lambda state, path: [],
            watchdog_problems=lambda state, now, life=None: list(self.watchdog),
            MAX_LIFE_HOURS=6.0, ADD_CUTOFF_S=900, without_sites=lab_tool.without_sites,
            Api=lambda: api, owned_instance=self.owned_instance,
            topology_cost=lab_tool.topology_cost,
            lab_ceilings=lambda state: (state["max_nodes"], state["max_hourly_usd"]),
            lab_hourly=lambda state: sum(node.get("hourly_usd", 0.0)
                                         for node in state["nodes"].values()),
            removed_record=lab_tool.removed_record, held_to_budget=self.held_to_budget)
        for target, name, value in (
                (deploy, "run_lab", self.fake_lab),
                (deploy, "lab_tool", lambda: lab),
                (deploy.common, "forget_host", lambda *addresses: self.forgotten.append(addresses))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def owned_instance(self, api, state, name, verb):
        if name in self.foreign:
            raise SystemExit(f"{verb} refused: instance {state['nodes'][name]['id']} does not "
                             f"carry the run's tag {state['tag']}")
        return {"id": state["nodes"][name]["id"]}

    def held_to_budget(self, verb, api, state, new, budget_usd=None, spent_before_usd=None):
        self.budgets.append({"nodes": sorted(state["nodes"]), "new": new, "budget": budget_usd,
                             "spent": spent_before_usd})
        if self.over_budget:
            raise SystemExit(f"{verb} refused: over the budget: {{\"budget_usd\": {budget_usd}}}")

    def create(self, state: dict, name: str, spec: dict, suffix: str) -> None:
        """A new instance of `name` in the state, with an address of its own."""
        address = f"203.0.113.{len(self.lab_calls) + 10}"
        state["nodes"][name] = {**spec, "name": name, "id": 5000 + len(self.lab_calls),
                                "label": f"ntsm-test-{name}{suffix}", "public_ipv4": address,
                                "vlan_ipv4": (spec.get("vlan_ipv4") or "").split("/")[0],
                                "hourly_usd": self.TYPES[spec["type"]]["price"]["hourly"]}
        self.hosts.by_address[address] = state["nodes"][name]
        self.hosts.by_address_name[name] = state["nodes"][name]

    def parsed_by_the_lab_tool(self, arguments) -> None:
        """The seam with C CT3: what the deploy hands the lab tool's command
        line (lab_argv) must parse with the lab tool's own parser, select
        the verb's handler and carry the node, the mode and the budget as
        given. A mismatch is recorded and fails the test at its end (the
        engine would turn an exception here into an exit code)."""
        try:
            with redirect_stderr(io.StringIO()):
                parsed = lab_tool.build_parser().parse_args(deploy.lab_argv(*arguments))
        except SystemExit:
            self.argv_errors.append(f"the lab tool's parser refuses {list(arguments)}")
            return
        verb = arguments[0]
        handlers = {"replace": lab_tool.command_replace, "add": lab_tool.command_add,
                    "remove": lab_tool.command_remove}
        problems = []
        if parsed.handler is not handlers.get(verb):
            problems.append(f"handler {parsed.handler.__name__}")
        if parsed.state != str(deploy.common.state_path()):
            problems.append(f"state {parsed.state}")
        if parsed.names != [arguments[1]]:
            problems.append(f"names {parsed.names}")
        if verb == "replace" and parsed.mode != arguments[arguments.index("--mode") + 1]:
            problems.append(f"mode {parsed.mode}")
        for flag, field in (("--budget-usd", "budget_usd"),
                            ("--spent-before-usd", "spent_before_usd")):
            if flag in arguments:
                if getattr(parsed, field, None) != float(arguments[arguments.index(flag) + 1]):
                    problems.append(f"{field} {getattr(parsed, field, None)}")
        if problems:
            self.argv_errors.append(f"{list(arguments)}: the lab tool reads {problems}")

    def fake_lab(self, *arguments):
        """The lab tool's command line as rm/ct3 behaves: `replace --mode
        rebuild` gives the node a new disk (nothing of ours on it, not
        running), `--mode new` deletes it, records it in `replaced`, and
        creates it again with a new id, label and public address (with
        `fail_create`, the create fails after the delete); `add` creates a
        slot and `remove` deletes one; a verb in `refuse` changes nothing
        and fails, as a refusal does."""
        self.lab_calls.append(list(arguments))
        self.parsed_by_the_lab_tool(arguments)
        verb, node = arguments[0], arguments[1]
        if verb in self.refuse:
            return deploy.subprocess.CompletedProcess(list(arguments), 1, "refused\n")
        state = self.state()
        code = 0
        if verb == "replace":
            self.hosts.files.pop(node, None)
            self.hosts.running[node] = False
            if arguments[arguments.index("--mode") + 1] == "new":
                old = state["nodes"].pop(node)
                state.setdefault("replaced", []).append(
                    {"name": node, "id": old["id"], "unix": int(time.time()), "mode": "new"})
                state.setdefault("removed", []).append(
                    lab_tool.removed_record(state, old, time.time()))
                if self.fail_create:
                    code = 1
                else:
                    self.create(state, node, old, f"r{len(state['replaced'])}")
        elif verb == "add":
            self.create(state, node, state["slots"][node], "")
        elif verb == "remove":
            state["nodes"].pop(node, None)
        self.state_file.write_text(json.dumps(state))
        return deploy.subprocess.CompletedProcess(list(arguments), code, "")

    def replace(self, **kwargs) -> int:
        """command_replace on the fake clock; its exit status."""
        run = self.engine()
        real = deploy.mesh_ops.replace

        def on_fake_clock(*args, **more):
            return real(*args, **more, clock=run["clock"], sleep=run["sleep"])

        namespace = self.namespace(**{"mode": "rebuild", "no_slot": False, "keep_slot": False,
                                      "budget_usd": 60.0, "spent_before_usd": None, **kwargs})
        with mock.patch.object(deploy.mesh_ops, "replace", on_fake_clock):
            with self.assertRaises(SystemExit) as code:
                self.quietly(deploy.command_replace, namespace)
        return code.exception.code

    def untouched(self) -> None:
        """No node was drained, stopped, restarted or given a file: each was
        at most asked the time or had its ssh connection opened."""
        for name, scripts in self.hosts.scripts.items():
            self.assertLessEqual(set(scripts), {"date +%s.%N", "true"}, name)
        self.assertEqual(self.hosts.drained, set())

    def test_replace_of_an_edge_restarts_nothing_else(self):
        for mode in ("rebuild", "new"):
            with self.subTest(mode=mode):
                self.push()
                address = self.state()["nodes"]["lon-edge-1"]["public_ipv4"]
                boots = dict(self.hosts.boots)
                self.lab_calls.clear()
                self.forgotten.clear()
                self.budgets.clear()
                code = self.replace(nodes="lon-edge-1", mode=mode, spent_before_usd=38.0)
                self.assertEqual(code, 0, self.stderr)
                self.assertEqual(self.lab_calls, [["replace", "lon-edge-1", "--mode", mode,
                                                   "--budget-usd", "60.0",
                                                   "--spent-before-usd", "38.0"]])
                new = self.state()["nodes"]["lon-edge-1"]["public_ipv4"]
                self.assertEqual(new != address, mode == "new")
                self.assertEqual([set(item) for item in self.forgotten], [{address, new}])
                # A new instance was held to the budget before anything, with
                # the old one deleted; a rebuild creates nothing.
                if mode == "new":
                    self.assertEqual(len(self.budgets), 1)
                    self.assertNotIn("lon-edge-1", self.budgets[0]["nodes"])
                    self.assertEqual([item["name"] for item in self.budgets[0]["new"]],
                                     ["lon-edge-1"])
                    self.assertEqual((self.budgets[0]["budget"], self.budgets[0]["spent"]),
                                     (60.0, 38.0))
                else:
                    self.assertEqual(self.budgets, [])
                # Pushed again on its recorded build, in service, recorded.
                self.assertEqual(self.args_of("lon-edge-1")[0],
                                 f"/opt/needletail/builds/{key(OLD)}/av-mesh")
                self.assertNotEqual(self.hosts.boots["lon-edge-1"], boots["lon-edge-1"])
                self.assertNotIn("lon-edge-1", self.saved_standby())
                self.assertEqual(self.hosts.drained, set())
                self.assertEqual(self.state()["deployed"]["nodes"]["lon-edge-1"]["build"],
                                 key(OLD))
                # No other node restarted: each is only asked the time.
                for name, scripts in self.hosts.scripts.items():
                    if name != "lon-edge-1":
                        self.assertEqual(set(scripts), {"date +%s.%N"}, name)
                self.assertEqual({name: boot for name, boot in self.hosts.boots.items()
                                  if name != "lon-edge-1"},
                                 {name: boot for name, boot in boots.items()
                                  if name != "lon-edge-1"})

    def forwarder_fakes(self) -> None:
        """The forwarder gate and a forwarder's readiness pass (CT1's, tested
        there on its own Sim: the FakeHosts documents carry no parents), and
        every forwarder reports its lease pairs, 0."""
        passed = deploy.mesh_ops.Gate(True, [], {}, [], 0.0, 2)
        document = self.hosts.document

        def with_pairs(name, role):
            found = document(name, role)
            if found is not None and role == "distributor":
                found["children"] = {"lease": {"pairs": 0}}
            return found

        for target, name, value in (
                (deploy.mesh_ops.Engine, "gate_forwarder", lambda engine, node: passed),
                (deploy.mesh_ops, "readiness_check", lambda *args, **kwargs: {}),
                (self.hosts, "document", with_pairs)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_forwarder_is_replaced_through_its_region_s_slot(self):
        self.forwarder_fakes()
        for keep in (False, True):
            with self.subTest(keep_slot=keep):
                self.push()
                boots = dict(self.hosts.boots)
                self.lab_calls.clear()
                self.forgotten.clear()
                self.budgets.clear()
                code = self.replace(nodes="lon-dist-a", budget_usd=45.5, keep_slot=keep)
                self.assertEqual(code, 0, self.stderr)
                # The slot held to the budget before anything, then created
                # with it, X rebuilt with it, and the slot deleted (or kept).
                self.assertEqual([[item["name"] for item in each["new"]]
                                  for each in self.budgets], [["lon-dist-c"]])
                self.assertEqual(self.lab_calls,
                                 [["add", "lon-dist-c", "--budget-usd", "45.5"],
                                  ["replace", "lon-dist-a", "--mode", "rebuild",
                                   "--budget-usd", "45.5"]]
                                 + ([] if keep else [["remove", "lon-dist-c"]]))
                # The slot was pushed on the forwarders' recorded build.
                self.assertEqual(self.args_of("lon-dist-c")[0],
                                 f"/opt/needletail/builds/{key(OLD)}/av-mesh")
                slot_address = self.hosts.by_address_name["lon-dist-c"]["public_ipv4"]
                address = self.state()["nodes"]["lon-dist-a"]["public_ipv4"]
                self.assertEqual([set(item) - {None} for item in self.forgotten],
                                 [{slot_address}, {address}]
                                 + ([] if keep else [{slot_address}]))
                self.assertEqual("lon-dist-c" in self.state()["nodes"], keep)
                # X pushed again on its build and running; drained by nobody.
                self.assertEqual(self.args_of("lon-dist-a")[0],
                                 f"/opt/needletail/builds/{key(OLD)}/av-mesh")
                self.assertNotEqual(self.hosts.boots["lon-dist-a"], boots["lon-dist-a"])
                self.assertTrue(self.hosts.running["lon-dist-a"])
                self.assertEqual(self.hosts.running["lon-dist-c"], False)
                self.assertEqual(self.hosts.drained - {"lon-dist-c"}, set())
                # No third node restarted.
                self.assertEqual({name: boot for name, boot in self.hosts.boots.items()
                                  if name not in ("lon-dist-a", "lon-dist-c")},
                                 {name: boot for name, boot in boots.items()
                                  if name not in ("lon-dist-a", "lon-dist-c")})
                for name, scripts in self.hosts.scripts.items():
                    if name not in ("lon-dist-a", "lon-dist-c"):
                        self.assertLessEqual(set(scripts), {"date +%s.%N", "true"}, name)
                self.hosts.scripts.clear()

    def test_a_refused_slot_leaves_the_forwarder_untouched(self):
        self.push()
        files = dict(self.hosts.fs("lon-dist-a"))
        boots = dict(self.hosts.boots)
        self.refuse.add("add")
        passed = deploy.mesh_ops.Gate(True, [], {}, [], 0.0, 2)
        with mock.patch.object(deploy.mesh_ops.Engine, "gate_forwarder",
                               lambda engine, node: passed):
            code = self.replace(nodes="lon-dist-a", budget_usd=45.5)
        self.assertEqual(code, 3, self.stderr)
        self.assertIn("the slot lon-dist-c was refused", self.stderr)
        self.assertIn("lon-dist-a is untouched", self.stderr)
        self.assertEqual(self.lab_calls, [["add", "lon-dist-c", "--budget-usd", "45.5"]])
        self.assertEqual(self.hosts.fs("lon-dist-a"), files)
        self.assertEqual(self.hosts.boots, boots)
        self.untouched()
        # --no-slot where the region has none is the operator's choice; a
        # region without a slot (a lab whose topology gave Tokyo none)
        # refuses before anything.
        self.lab_calls.clear()
        state = self.state()
        del state["slots"]["tok-dist-c"]
        self.state_file.write_text(json.dumps(state))
        self.push()
        self.assertEqual(self.replace(nodes="tok-dist-b"), 2)
        self.assertIn("tokyo has no forwarder slot (--no-slot)", self.stderr)
        self.assertEqual(self.lab_calls, [])

    def test_what_the_lab_tool_would_refuse_is_refused_before_anything(self):
        self.push()
        boots = dict(self.hosts.boots)
        state = self.state()
        cases = (
            # 25 min to the deadline: the lab tool's 15 min cutoff would
            # come while the edge is stopped.
            ("deadline", {"nodes": "lon-edge-1"}, "the watchdog deletes the lab in"),
            ("watchdog", {"nodes": "lon-edge-1"}, "the watchdog unit is not active"),
            ("tag", {"nodes": "lon-edge-1"}, "does not carry the run's tag"),
            ("budget", {"nodes": "lon-edge-1", "mode": "new"}, "over the budget"),
            ("slot budget", {"nodes": "lon-dist-a"}, "over the budget"),
            ("ceiling", {"nodes": "lon-dist-a"}, "the ceiling that up recorded is $0.10"),
        )
        for case, arguments, why in cases:
            with self.subTest(case=case):
                current = json.loads(json.dumps(state))
                self.watchdog = ["the watchdog unit is not active"] if case == "watchdog" else []
                self.foreign = {"lon-edge-1"} if case == "tag" else set()
                self.over_budget = case.endswith("budget")
                if case == "deadline":
                    current["watchdog_deadline_unix"] = int(time.time()) + 25 * 60
                if case == "ceiling":
                    current["max_hourly_usd"] = 0.1
                self.state_file.write_text(json.dumps(current))
                self.lab_calls.clear()
                self.hosts.scripts.clear()
                self.assertEqual(self.replace(**arguments), 2, self.stderr)
                self.assertIn("the lab tool would refuse this replace after "
                              f"{arguments['nodes']} was emptied and stopped", self.stderr)
                self.assertIn(why, self.stderr)
                self.assertEqual(self.lab_calls, [])
                self.assertEqual(self.hosts.boots, boots)
                self.untouched()
                self.assertNotIn("start ", self.stderr.replace("mark start", ""))

    def test_the_lab_tool_s_own_functions_take_what_replace_gives_them(self):
        """The seam with C CT3, in-process: LinodeOps.lab_refusals runs on
        the real lab tool's functions (only its Linode API, its user
        systemd and the other labs' bill are faked), so a signature or a
        state field that differs from what CT3 built fails here. Within
        the budget a rebuild and a new instance go ahead; over it, a new
        instance is refused by the lab tool's own held_to_budget, and an
        instance without the run's tag by its owned_instance."""
        self.push()
        state = self.state()
        tags = {node["id"]: [state["tag"]] for node in state["nodes"].values()}
        test = self

        class Api:
            def list_all(self, path):
                return list((test.TYPES if path == "/linode/types" else test.REGIONS).values())

            def call(self, method, path, body=None, allow_404=False):
                test.assertEqual(method, "GET", path)
                ident = int(path.rsplit("/", 1)[1])
                node = next((item for item in test.state()["nodes"].values()
                             if item.get("id") == ident), None)
                return node and {"id": ident, "label": node["label"], "tags": tags.get(
                    ident, [test.state()["tag"]]), "status": "running"}

        with mock.patch.object(deploy, "lab_tool", lambda: lab_tool), \
                mock.patch.multiple(lab_tool, Api=Api, watchdog_active=lambda run_id: True,
                                    account_billed=lambda api, tag, now=None: {"usd": 0.0},
                                    read_ledger=lambda: []):
            self.assertEqual(self.replace(nodes="lon-edge-1", mode="rebuild"), 0, self.stderr)
            self.assertEqual(self.replace(nodes="lon-edge-2", mode="new", budget_usd=60.0), 0,
                             self.stderr)
            self.lab_calls.clear()
            self.assertEqual(self.replace(nodes="lon-edge-3", mode="new", budget_usd=0.5), 2,
                             self.stderr)
            self.assertIn("replace refused: over the budget", self.stderr)
            tags[self.state()["nodes"]["lon-edge-3"]["id"]] = ["another-lab"]
            self.assertEqual(self.replace(nodes="lon-edge-3", mode="rebuild"), 2, self.stderr)
            self.assertIn("does not carry the run's tag", self.stderr)
            self.assertEqual(self.lab_calls, [])

    def test_a_new_instance_whose_create_fails_is_named_with_its_way_back(self):
        self.push()
        self.fail_create = True
        code = self.replace(nodes="lon-edge-1", mode="new")
        self.assertEqual(code, 5, self.stderr)
        self.assertNotIn("lon-edge-1", self.state()["nodes"])
        self.assertIn("the lab tool deleted lon-edge-1 and did not create it again", self.stderr)
        self.assertIn(f"grow --topology {SCALE}", self.stderr)
        self.assertIn("replace --nodes lon-edge-1 --mode rebuild --resume", self.stderr)
        # The lab tool's grow creates it again (at its VLAN address, a new
        # public one); the resume rebuilds that instance and finishes.
        state = self.state()
        self.create(state, "lon-edge-1", next(node for node in json.loads(SCALE.read_text())
                                              ["nodes"] if node["name"] == "lon-edge-1"), "")
        self.state_file.write_text(json.dumps(state))
        self.fail_create = False
        self.lab_calls.clear()
        code = self.replace(nodes="lon-edge-1", mode="rebuild", resume=True)
        self.assertEqual(code, 0, self.stderr)
        self.assertEqual([call[:4] for call in self.lab_calls],
                         [["replace", "lon-edge-1", "--mode", "rebuild"]])
        self.assertTrue(self.hosts.running["lon-edge-1"])
        self.assertEqual(self.hosts.drained, set())

    def test_a_resume_after_the_rebuild_is_not_held_to_the_deadline(self):
        """A node left stopped and rebuilt (its push failed) is pushed back by
        the resume even within the lab tool's cutoff: nothing is left for
        the lab tool to create or rebuild."""
        self.push()
        real = deploy.LinodeOps.push
        failures = ["ssh dropped"]

        def push(ops, node):
            if failures:
                raise RuntimeError(failures.pop())
            real(ops, node)

        with mock.patch.object(deploy.LinodeOps, "push", push):
            self.assertEqual(self.replace(nodes="lon-edge-1"), 5, self.stderr)
            self.assertIn("lon-edge-1's push failed (ssh dropped)", self.stderr)
            state = self.state()
            state["watchdog_deadline_unix"] = int(time.time()) + 25 * 60
            self.state_file.write_text(json.dumps(state))
            self.assertEqual(self.replace(nodes="lon-edge-1"), 2, self.stderr)
            self.assertIn("the watchdog deletes the lab in", self.stderr)
            self.lab_calls.clear()
            self.assertEqual(self.replace(nodes="lon-edge-1", resume=True), 0, self.stderr)
        self.assertEqual(self.lab_calls, [])
        self.assertTrue(self.hosts.running["lon-edge-1"])
        self.assertEqual(self.hosts.drained, set())

    def test_an_interrupt_during_the_lab_tool_still_reads_the_lab_again(self):
        """run_lab raises the interrupt only after the lab tool ended; the
        state it wrote is read and both addresses forgotten all the same."""
        self.push()
        ops = self.ops()
        address = ops.full["nodes"]["lon-edge-1"]["public_ipv4"]

        def interrupted(*arguments):
            self.fake_lab(*arguments)
            raise deploy.mesh_ops.Interrupted("SIGINT")

        with mock.patch.object(deploy, "run_lab", interrupted), \
                self.assertRaises(deploy.mesh_ops.Interrupted):
            ops.rebuild("lon-edge-1", "new")
        new = ops.full["nodes"]["lon-edge-1"]["public_ipv4"]
        self.assertNotEqual(new, address)
        self.assertEqual([set(item) for item in self.forgotten], [{address, new}])

    def test_a_new_forwarder_address_names_its_dependents(self):
        self.push()
        dependents = self.ops().address_dependents("tok-dist-a")
        # Its parents forward to it, and the other regions' nodes take it as
        # a failover entry: by its public address.
        for name in ("lon-dist-a", "lon-dist-b"):
            self.assertIn(name, dependents)
        # Its own region names it by its VLAN address.
        self.assertFalse([name for name in dependents if name.startswith("tok-")], dependents)
        self.hosts.scripts.clear()
        code = self.replace(nodes="tok-dist-a", mode="new")
        self.assertEqual(code, 2, self.stderr)
        self.assertIn(f"the arguments of {', '.join(dependents)} name tok-dist-a's public "
                      "address", self.stderr)
        self.assertIn("in this round that is an outage", self.stderr)
        self.assertEqual(self.lab_calls, [])
        self.assertEqual(self.budgets, [])
        self.untouched()
        # An edge is named by its VLAN address: nothing depends on its public one.
        self.assertEqual(self.ops().address_dependents("lon-edge-1"), [])

    def test_a_contributor_goes_through_workstream_a_s_gate_and_one_node_at_a_time(self):
        self.push()
        # One contributor in this lab: A's gate refuses before the node.
        self.assertEqual(self.replace(nodes="contrib-lon"), 3)
        self.assertIn("K1: contrib-lon is the lab's only contributor", self.stderr)
        self.assertEqual(self.replace(nodes="lon-edge-1,lon-edge-2"), 2)
        self.assertIn("one node", self.stderr)
        self.assertEqual(self.lab_calls, [])
        # The clock reads, and the ssh refresh of the node in hand.
        self.assertEqual([name for name, scripts in self.hosts.scripts.items()
                          if set(scripts) - {"date +%s.%N", "true"}], [])

    def test_without_the_lab_tool_of_ct3_replace_is_refused(self):
        lab = types.SimpleNamespace(change_state=self.change_state,
                                    watchdog_ensure=lambda state, path: [])
        with mock.patch.object(deploy, "lab_tool", lambda: lab):
            self.assertEqual(self.replace(nodes="lon-edge-1"), 2)
        self.assertIn("has no watchdog_problems", self.stderr)
        self.assertEqual(self.lab_calls, [])


class RunLab(unittest.TestCase):
    def test_an_interrupt_waits_for_the_lab_tool_and_reads_its_result(self):
        """The lab tool runs in a session of its own and is never cut: an
        interrupt of the deploy (the engine's handler raises Interrupted)
        waits for it, its output is read, and the interrupt comes after."""
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        (directory / "scripts").mkdir()
        done = directory / "done"
        (directory / "scripts" / "linode-small-mesh-lab.py").write_text(
            "import os, pathlib, sys, time\n"
            "print('session', os.getsid(0) == os.getpid(), flush=True)\n"
            "time.sleep(0.6)\n"
            f"pathlib.Path({str(done)!r}).write_text(' '.join(sys.argv[1:]))\n"
            "print('rebuilt', flush=True)\n")

        def handler(signum, _frame):
            raise deploy.mesh_ops.Interrupted(deploy.signal.Signals(signum).name)

        previous = deploy.signal.signal(deploy.signal.SIGINT, handler)
        self.addCleanup(deploy.signal.signal, deploy.signal.SIGINT, previous)
        timer = threading.Timer(0.2, lambda: os.kill(os.getpid(), deploy.signal.SIGINT))
        timer.start()
        self.addCleanup(timer.cancel)
        err = io.StringIO()
        with mock.patch.object(deploy.common, "ROOT", directory), redirect_stderr(err):
            with self.assertRaises(deploy.mesh_ops.Interrupted):
                deploy.run_lab("replace", "lon-edge-1", "--mode", "rebuild")
        self.assertEqual(done.read_text(), f"--state {deploy.common.state_path()} replace "
                                           "lon-edge-1 --mode rebuild")
        self.assertIn("runs to its end first", err.getvalue())
        self.assertIn("session True", err.getvalue())
        self.assertIn("rebuilt", err.getvalue())

    def test_the_exit_status_and_output_are_returned(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        (directory / "scripts").mkdir()
        (directory / "scripts" / "linode-small-mesh-lab.py").write_text(
            "import sys\nprint('add refused: over the budget')\nsys.exit(1)\n")
        err = io.StringIO()
        with mock.patch.object(deploy.common, "ROOT", directory), redirect_stderr(err):
            result = deploy.run_lab("add", "lon-dist-c")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "add refused: over the budget\n")
        self.assertEqual(err.getvalue(), "add refused: over the budget\n")


class ForgetHost(unittest.TestCase):
    def test_a_host_is_forgotten_in_known_hosts_and_its_control_connection_closed(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        known = directory / "known_hosts"
        key = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl")
        known.write_text(f"198.51.100.7 {key}\n198.51.100.8 {key}\n")
        calls = []
        real = deploy.common.subprocess.run

        def run(command, **kwargs):
            calls.append(command)
            if command[0] == "ssh":
                return deploy.subprocess.CompletedProcess(command, 255, b"", b"")
            return real(command, **kwargs)

        with mock.patch.object(deploy.common, "KNOWN_HOSTS", known), \
                mock.patch.object(deploy.common, "CONTROL_DIR", directory / "control"), \
                mock.patch.object(deploy.common.subprocess, "run", run):
            deploy.common.forget_host("198.51.100.7", None, "198.51.100.7")
        self.assertEqual(known.read_text().split(), ["198.51.100.8", *key.split()])
        self.assertFalse((directory / "known_hosts.old").exists())
        exits = [command for command in calls if command[0] == "ssh"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0][-3:], ["-O", "exit", "root@198.51.100.7"])
        self.assertIn(f"ControlPath={directory / 'control'}/%C", exits[0])


class Limits(Base):
    def test_more_relay_children_than_the_port_plan_holds_are_refused(self):
        nodes = {"contrib-lon": {"name": "contrib-lon", "role": "contributor", "site": "london",
                                 "region": "gb-lon", "type": "g6-dedicated-4",
                                 "public_ipv4": "192.0.2.1", "vlan_ipv4": "10.80.1.5"}}
        for index in range(deploy.MAX_RELAY_CHILDREN + 3):  # two first forwarders
            site = "london" if index < 2 else "newyork"
            name = f"dist-{index:02d}"
            nodes[name] = {"name": name, "role": "distributor", "site": site,
                           "region": "gb-lon" if index < 2 else "us-east",
                           "type": "g6-dedicated-2", "public_ipv4": f"192.0.2.{index + 10}",
                           "vlan_ipv4": f"10.80.1.{index + 11}" if index < 2 else None}
        state = {"nodes": nodes}
        with self.assertRaises(SystemExit) as caught:
            deploy.build_program(state, self.settings(["max_edges_per_forwarder=16"]), ROUTES)
        self.assertIn("port plan", str(caught.exception))


class LoadHosts(Base):
    def test_listener_hosts_get_the_load_tools_and_kernel_settings(self):
        state = topology_state(json.loads(SCALE.read_text()))
        listener = state["nodes"]["lon-load-1"]
        view = state["nodes"]["lon-view"]
        source = state["nodes"]["lon-src"]
        names = lambda node: {path.name for path in deploy.binaries_for(node["role"], node)}  # noqa: E731
        self.assertTrue({"needletail-load", "ntcmp"} <= names(listener))
        self.assertFalse({"needletail-load", "ntcmp"} & names(view))
        self.assertFalse({"needletail-load", "ntcmp"} & names(source))
        # A state saved before `purpose` existed is read from the names.
        bare = {key: value for key, value in listener.items() if key != "purpose"}
        self.assertEqual(deploy.common.load_purpose(bare), "listeners")
        self.assertEqual(deploy.common.load_purpose({"name": "syd-view", "role": "load"}), "view")
        self.assertEqual(deploy.common.load_purpose({"name": "lon-src", "role": "load"}), "source")
        sent = []
        with mock.patch.object(deploy.common, "rsync_to", lambda *a, **k: None), \
                mock.patch.object(deploy.common, "ssh_text",
                                  lambda node, script, **k: sent.append(script) or ""):
            deploy.install_tools(listener, self.settings(), False)
            deploy.install_tools(view, self.settings(), False)
        self.assertIn("net.ipv4.ip_local_port_range = 1024 65000", sent[0])
        self.assertIn("91-needletail-listeners.conf", sent[0])
        self.assertNotIn("91-needletail-listeners.conf", sent[1])

    def test_the_source_and_capacity_tool_come_from_their_variables(self):
        state = topology_state(json.loads(SCALE.read_text()))
        with mock.patch.dict("os.environ", {"NEEDLETAIL_DAW_NEXUS_ROOT": "/w/daw-nexus-x",
                                            "NEEDLETAIL_H3_CAPACITY_BIN": "/w/h3-cap"}):
            listener = [str(path) for path in deploy.binaries_for("load", state["nodes"]["lon-load-1"])]
            contributor = [str(path) for path in deploy.binaries_for("contributor")]
        daw = "/w/daw-nexus-x/target/x86_64-unknown-linux-gnu/release/daw-test-source"
        self.assertIn(daw, listener)
        self.assertIn("/w/h3-cap", listener)
        self.assertEqual(contributor[-1], daw)
        self.assertTrue(contributor[0].endswith("/av-contrib"))

    def test_the_older_labs_load_hosts_are_not_listener_hosts(self):
        # topology-core.json and its kin name their one source and probe
        # host per region lon-load, new-load: push must not ask for
        # needletail-load and ntcmp there, nor change its ports.
        for path in sorted((deploy.common.ROOT / "deploy").glob("*/topology*.json")):
            topology = json.loads(path.read_text())
            for node in topology["nodes"]:
                if node["role"] != "load" or node.get("purpose"):
                    continue
                with self.subTest(topology=path.name, node=node["name"]):
                    self.assertNotEqual(deploy.common.load_purpose(node), "listeners")
                    names = {item.name for item in deploy.binaries_for("load", node)}
                    self.assertFalse({"needletail-load", "ntcmp"} & names)
        self.assertEqual(deploy.common.load_purpose({"name": "lon-load", "role": "load"}),
                         "view")
        self.assertEqual(deploy.common.load_purpose({"name": "tok-load-12", "role": "load"}),
                         "listeners")
        sent = []
        with mock.patch.object(deploy.common, "rsync_to", lambda *a, **k: None), \
                mock.patch.object(deploy.common, "ssh_text",
                                  lambda node, script, **k: sent.append(script) or ""):
            deploy.install_tools({"name": "lon-load", "role": "load", "site": "london"},
                                 self.settings(), False)
        self.assertNotIn("91-needletail-listeners.conf", sent[0])


class LabTool(unittest.TestCase):
    TYPES = {"g6-dedicated-2": {"id": "g6-dedicated-2", "class": "dedicated",
                                "price": {"hourly": 0.054}, "region_prices": []},
             "g6-dedicated-4": {"id": "g6-dedicated-4", "class": "dedicated",
                                "price": {"hourly": 0.108}, "region_prices": []},
             "g6-dedicated-8": {"id": "g6-dedicated-8", "class": "dedicated",
                                "price": {"hourly": 0.216}, "region_prices": []},
             "g6-standard-2": {"id": "g6-standard-2", "class": "standard",
                               "price": {"hourly": 0.036}, "region_prices": []}}
    REGIONS = {region: {"id": region, "status": "ok", "capabilities": ["Vlans"]}
               for region in ("gb-lon", "us-east", "ap-northeast", "ap-southeast")}

    def test_price_of_the_scale_topologies(self):
        every = json.loads(SCALE.read_text())
        hourly, problems = lab_tool.topology_cost(lab_tool.created_part(every), self.TYPES,
                                                  self.REGIONS)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(hourly, 10.422, places=3)
        # The nine slots are priced and checked too (dedicated only).
        hourly, problems = lab_tool.topology_cost(every, self.TYPES, self.REGIONS)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(hourly, 10.422 + 9 * 0.054, places=3)
        topology = lab_tool.created_part(lab_tool.without_sites(every, ["sydney"]))
        hourly, _ = lab_tool.topology_cost(topology, self.TYPES, self.REGIONS)
        self.assertAlmostEqual(hourly, 10.422 - 2.538, places=3)

    def test_validate_refuses_a_shared_slot_and_caps_what_up_creates(self):
        class FakeApi:
            def list_all(self, endpoint, tag=None):
                return list((LabTool.TYPES if "types" in endpoint else LabTool.REGIONS).values())

        every = json.loads(SCALE.read_text())
        with mock.patch.object(lab_tool, "MAX_NODES", 99), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 10.5):
            _topology, hourly = lab_tool.validate_topology(FakeApi(), every)
            self.assertAlmostEqual(hourly, 10.422, places=3)
            shared = json.loads(SCALE.read_text())
            for node in shared["nodes"]:
                if node["name"] == "lon-edge-11":
                    node["type"] = "g6-standard-2"
            with self.assertRaises(SystemExit) as caught, redirect_stdout(io.StringIO()):
                lab_tool.validate_topology(FakeApi(), shared)
            self.assertIn("not a dedicated-CPU plan", str(caught.exception))
        with mock.patch.object(lab_tool, "MAX_NODES", 98), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 10.5), \
                self.assertRaises(SystemExit):
            lab_tool.validate_topology(FakeApi(), every)

    def test_capacity_and_shared_plans_are_named(self):
        topology = lab_tool.created_part(json.loads(SCALE.read_text()))
        availability = {("ap-southeast", "g6-dedicated-4"): False}
        _hourly, problems = lab_tool.topology_cost(topology, self.TYPES, self.REGIONS,
                                                   availability)
        self.assertEqual(len(problems), 17)  # sixteen load hosts and the view host
        self.assertTrue(all("ap-southeast" in problem for problem in problems))
        shared = {**topology, "nodes": [{**topology["nodes"][0], "type": "g6-standard-2"}]}
        _hourly, problems = lab_tool.topology_cost(shared, self.TYPES, self.REGIONS)
        self.assertIn("not a dedicated-CPU plan", problems[0])

    def test_without_sites_refuses_the_contributors_site(self):
        topology = json.loads(SCALE.read_text())
        with self.assertRaises(SystemExit):
            lab_tool.without_sites(topology, ["london"])
        with self.assertRaises(SystemExit):
            lab_tool.without_sites(topology, ["mars"])
        first, rest = lab_tool.split_optional(topology["nodes"], ["sydney"])
        self.assertEqual(list(first), ["sydney"])
        self.assertFalse([node for node in rest if node["site"] == "sydney"])

    def test_grow_leaves_out_the_sites_that_up_left_out(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        base = lab_tool.created_part(
            lab_tool.without_sites(json.loads(SCALE.read_text()), ["sydney"]))
        base["nodes"] = [node for node in base["nodes"] if node["name"] not in STANDBY]
        now = lab_tool.time.time()
        state = {"run_id": "t", "tag": "t", "image": "linode/debian13", "hourly_usd": 0,
                 "firewall_id": 1, "sites_left_out": ["sydney"], "created_unix": now - 60,
                 "watchdog_deadline_unix": now + 3600, "budget_usd": 300.0,
                 "spent_before_usd": 0.0,
                 "nodes": {node["name"]: {**node, "hourly_usd": 0.054}
                           for node in base["nodes"]}}
        path = tmp / "state.json"
        path.write_text(json.dumps(state))
        added = []

        class FakeApi:
            def list_all(self, endpoint, tag=None):
                return list((LabTool.TYPES if "types" in endpoint else LabTool.REGIONS).values())

        args = argparse.Namespace(state=str(path), topology=str(SCALE), without_sites="",
                                  ssh_public_key="unused")
        with mock.patch.object(lab_tool, "Api", FakeApi), \
                mock.patch.object(lab_tool, "MAX_NODES", 200), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 20.0), \
                mock.patch.object(lab_tool, "watchdog_active", lambda run_id: True), \
                mock.patch.object(lab_tool, "LEDGER", tmp / "ledger.jsonl"), \
                mock.patch.object(lab_tool, "add_nodes",
                                  lambda args, api, state, nodes: added.extend(nodes)), \
                mock.patch.object(lab_tool, "command_status", lambda args: None), \
                redirect_stdout(io.StringIO()):
            lab_tool.command_grow(args)
        # Never an autoscale slot: only `add` creates one.
        self.assertEqual(sorted(node["name"] for node in added),
                         sorted(name for name in STANDBY if not name.startswith("syd-")))
        self.assertEqual(sorted(json.loads(path.read_text())["slots"]),
                         sorted(name for name in SCALE_SLOTS if not name.startswith("syd-")))

    def test_down_deletes_the_runs_vlans_once_they_are_empty(self):
        calls = []

        class FakeApi:
            attached = {"ntsm-r1-gb-lon": 1}

            def list_all(self, endpoint, tag=None):
                self.endpoint = endpoint
                return [{"label": "ntsm-r1-gb-lon", "region": "gb-lon"},
                        {"label": "ntsm-r1-ap-southeast", "region": "ap-southeast"},
                        {"label": "ntsm-r10-gb-lon", "region": "gb-lon"},
                        {"label": "cmpd1-gb-lon", "region": "gb-lon"}]

            def call(self, method, path, body=None, allow_404=False):
                calls.append((method, path))
                label = path.rsplit("/", 1)[-1]
                if self.attached.get(label, 0) > 0:
                    self.attached[label] -= 1
                    raise RuntimeError("HTTP 400: VLAN is attached")
                return {}

        with mock.patch.object(lab_tool.time, "sleep", lambda seconds: None):
            left = lab_tool.delete_vlans(FakeApi(), "r1")
        self.assertEqual(left, [])
        # Only this run's VLANs (an optional site's too); the second try
        # after the attached one detached.
        self.assertEqual(sorted(set(calls)), [
            ("DELETE", "/networking/vlans/ap-southeast/ntsm-r1-ap-southeast"),
            ("DELETE", "/networking/vlans/gb-lon/ntsm-r1-gb-lon")])
        self.assertEqual(calls.count(("DELETE", "/networking/vlans/gb-lon/ntsm-r1-gb-lon")), 2)
        FakeApi.attached = {"ntsm-r1-gb-lon": 99}
        with mock.patch.object(lab_tool.time, "sleep", lambda seconds: None):
            self.assertEqual(lab_tool.delete_vlans(FakeApi(), "r1", attempts=3),
                             ["ntsm-r1-gb-lon"])

    def test_up_arms_the_watchdog_before_it_creates_an_instance(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        key = tmp / "key.pub"
        key.write_text("ssh-ed25519 AAAA test")
        events = []

        class FakeApi:
            def list_all(self, endpoint, tag=None):
                return list((LabTool.TYPES if "types" in endpoint else LabTool.REGIONS).values())

        def watchdog(state_path, state, deadline):
            events.append(("watchdog", deadline - state["created_unix"]))
            lab_tool.change_state(state_path, state, lambda current: current.update(
                watchdog_deadline_unix=deadline))

        def create(api, run_id, tag, image, key, node):
            events.append(("create", node["name"]))
            raise RuntimeError("no capacity")

        args = lab_tool.argparse.Namespace(
            state=str(tmp / "state.json"), topology=str(SCALE), run_id="t1",
            ssh_public_key=str(key), without_sites="", optional_sites="", watchdog_hours=5.5,
            budget_usd=300.0, spent_before_usd=0.0)
        with mock.patch.object(lab_tool, "Api", FakeApi), \
                mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": str(tmp / "data")}), \
                mock.patch.object(lab_tool, "MAX_NODES", 200), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 20.0), \
                mock.patch.object(lab_tool, "LEDGER", tmp / "ledger.jsonl"), \
                mock.patch.object(lab_tool, "WATCHDOG_UNIT_DIR", tmp / "units"), \
                mock.patch.object(lab_tool, "watchdog_active", lambda run_id: False), \
                mock.patch.object(lab_tool, "watchdog_host_problems", lambda: []), \
                mock.patch.object(lab_tool, "watchdog_arm", watchdog), \
                mock.patch.object(lab_tool, "create_node", create), \
                mock.patch.object(lab_tool, "command_down", lambda args: events.append(("down",))), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            lab_tool.command_up(args)
        self.assertEqual(events[0], ("watchdog", 5.5 * 3600))
        self.assertEqual(events[1][0], "create")
        self.assertEqual(events[-1], ("down",))
        # The core stops at its first refusal: only the creates already under
        # way (CREATE_WORKERS at most), not 69.
        self.assertLessEqual(sum(1 for item in events if item[0] == "create"),
                             lab_tool.CREATE_WORKERS)
        state = json.loads((tmp / "state.json").read_text())
        self.assertEqual(state["watchdog_deadline_unix"], state["created_unix"] + 5.5 * 3600)


ACCOUNT = ("POST /linode/instances failed with HTTP 400: {\"errors\": [{\"reason\": \"Account "
           "Limit reached. Please open a support ticket with our support team to request an "
           "increase.\"}]}")
TRANSIENT = "POST /linode/instances failed with HTTP 503: service busy, try again"
CAPACITY = ("POST /linode/instances failed with HTTP 400: {\"errors\": [{\"reason\": \"No "
            "capacity available in this region for the requested plan.\"}]}")


class StagedUp(unittest.TestCase):
    """`up` in stages (offline: create_node and the API are fakes)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "state.json"
        self.state = {"run_id": "t", "tag": "needletail-small-mesh-t", "image": "linode/debian13",
                      "created_unix": 1_000_000, "hourly_usd": 0.0, "nodes": {},
                      "sites_left_out": [], "removed": []}
        self.path.write_text(json.dumps(self.state))
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.downs = 0
        test = self

        class FakeApi:
            def call(self, method, path, body=None, allow_404=False):
                test.deleted.append(path)
                return {}

        self.api = FakeApi()
        topology = lab_tool.created_part(json.loads(SCALE.read_text()))
        for node in topology["nodes"]:
            node["_hourly"] = {"g6-dedicated-2": 0.054, "g6-dedicated-4": 0.108,
                               "g6-dedicated-8": 0.216}[node["type"]]
        self.nodes = topology["nodes"]

    def run_up(self, refuse, optional=("sydney",), batches=2):
        test = self

        def create(api, run_id, tag, image, key, node):
            why = refuse(node, len(test.created))
            if why:
                raise RuntimeError(why)
            test.created.append(node["name"])
            return {"id": len(test.created), "name": node["name"], "label": f"ntsm-t-{node['name']}",
                    "role": node["role"], "site": node["site"], "region": node["region"],
                    "type": node["type"], "hourly_usd": node["_hourly"],
                    "public_ipv4": "192.0.2.1", "vlan_ipv4": None,
                    "created": "2026-11-22T10:00:00", "created_unix": 1_000_000}

        def down(args):
            test.downs += 1

        args = argparse.Namespace(state=str(self.path), listener_batches=batches)
        with mock.patch.object(lab_tool, "create_node", create), \
                mock.patch.object(lab_tool, "CREATE_WORKERS", 1), \
                mock.patch.object(lab_tool, "TRANSIENT_RETRY_PAUSE_S", 0.0), \
                mock.patch.object(lab_tool, "command_down", down), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            try:
                lab_tool.create_staged(args, self.api, self.state, self.path, self.nodes,
                                       list(optional), "ssh-ed25519 AAAA")
                code = None
            except SystemExit as exit_:
                code = exit_.code
        return code, err.getvalue(), json.loads(self.path.read_text())

    def test_refusals_are_told_apart(self):
        self.assertEqual(lab_tool.refusal_kind(ACCOUNT), "account")
        self.assertEqual(lab_tool.refusal_kind("You have reached the maximum number of "
                                               "Linodes allowed on your account"), "account")
        self.assertEqual(lab_tool.refusal_kind(CAPACITY), "capacity")
        self.assertEqual(lab_tool.refusal_kind("g6-dedicated-4 is not available in this region"),
                         "capacity")
        self.assertEqual(lab_tool.refusal_kind("POST /linode/instances did not succeed after "
                                               "retries"), "transient")
        self.assertEqual(lab_tool.refusal_kind("failed with HTTP 503: busy"), "transient")
        self.assertEqual(lab_tool.refusal_kind("HTTP 400: image not found"), "other")

    def test_a_capacity_message_that_names_support_is_capacity(self):
        # A region or plan with nothing to give, however it suggests a ticket.
        for text in ("g6-dedicated-8 is not available in this region at this time. Please open a "
                     "support ticket to request an increase.",
                     "No capacity in ap-southeast for this plan: open a support ticket with your "
                     "account manager",
                     "Out of stock: please try again later"):
            self.assertEqual(lab_tool.refusal_kind(text), "capacity", text)
        # The account's own limit wins when the message names it.
        self.assertEqual(lab_tool.refusal_kind("Account Limit reached: no capacity left on "
                                               "your account"), "account")
        # A hint alone is not enough: it needs "limit" or "account".
        self.assertEqual(lab_tool.refusal_kind("Account quota exceeded"), "account")
        self.assertEqual(lab_tool.refusal_kind("vCPU quota limit"), "account")
        self.assertEqual(lab_tool.refusal_kind("quota service unavailable"), "other")

    def test_a_capacity_refusal_naming_support_stops_only_its_plan(self):
        # Tokyo's first listener host refused with a capacity message that
        # names a ticket: it was taken for the account's limit before, and
        # every later listener create in every region stopped.
        text = ("HTTP 400: g6-dedicated-8 is not available in this region. Please open a "
                "support ticket to request an increase.")
        code, _err, state = self.run_up(
            lambda node, made: text if node["name"] == "tok-load-1" else None)
        self.assertIsNone(code)
        self.assertEqual([item["kind"] for item in state["refusals"]], ["capacity"])
        # Only Tokyo's g6-dedicated-8 is stopped; the other regions' hosts exist.
        self.assertEqual({item["site"] for item in state["listeners_missing"]}, {"tokyo"})
        self.assertEqual(sum(1 for name in state["nodes"] if "-load-" in name), 32)

    def test_listener_batches_take_a_share_of_every_site(self):
        listeners = [node for node in self.nodes if lab_tool.is_listener_host(node)]
        self.assertEqual(len(listeners), 40)
        core = [node for node in self.nodes if not lab_tool.is_listener_host(node)]
        self.assertEqual(len(core), 59)
        self.assertEqual(len([node for node in core if node["site"] != "sydney"]), 45)
        first, second = lab_tool.listener_batches(listeners, 2)
        self.assertEqual(len(first), 20)
        self.assertEqual([node["name"] for node in first if node["site"] == "london"],
                         [f"lon-load-{index}" for index in range(1, 5)])
        self.assertEqual(sum(1 for node in second if node["site"] == "sydney"), 8)
        self.assertEqual(len(lab_tool.listener_batches(listeners, 1)), 1)
        # The older labs' hosts by name; view and source hosts are core.
        self.assertTrue(lab_tool.is_listener_host({"role": "load", "name": "lon-load-3"}))
        self.assertFalse(lab_tool.is_listener_host({"role": "load", "name": "lon-view"}))
        self.assertFalse(lab_tool.is_listener_host({"role": "load", "name": "lon-src",
                                                    "purpose": "source"}))

    def test_the_core_first_then_the_listener_hosts(self):
        code, _err, state = self.run_up(lambda node, made: None)
        self.assertIsNone(code)
        self.assertEqual(len(state["nodes"]), 99)
        self.assertEqual(state["listeners_missing"], [])
        # Sydney's core, then the rest of the core, then the listener hosts.
        listener_at = [index for index, name in enumerate(self.created) if "-load-" in name]
        self.assertEqual(min(listener_at), 59)
        self.assertTrue(all(name.startswith("syd-") for name in self.created[:14]))
        self.assertIn("contrib-lon", self.created[14:59])

    def test_an_account_refusal_among_the_listener_hosts_keeps_the_lab(self):
        # The account allows 80 instances: the core and 21 listener hosts.
        code, err, state = self.run_up(lambda node, made: ACCOUNT if made >= 80 else None)
        self.assertIsNone(code)
        self.assertEqual(self.downs, 0)
        self.assertEqual(len(state["nodes"]), 80)
        missing = state["listeners_missing"]
        self.assertEqual(len(missing), 19)
        self.assertEqual(missing[0]["kind"], "account")
        # Stopped at the first refusal: the rest never tried.
        self.assertEqual({item["kind"] for item in missing[1:]}, {"not attempted"})
        self.assertTrue(all("account refusal" in item["why"] for item in missing[1:]))
        self.assertEqual(sum(1 for name in state["nodes"] if "-load-" in name), 21)
        self.assertEqual(state["refusals"][0]["stage"], "listener batch 2")
        self.assertIn("missing", err)
        self.assertAlmostEqual(state["hourly_usd"],
                               sum(node["hourly_usd"] for node in state["nodes"].values()))

    def test_a_capacity_refusal_stops_only_its_plan_and_a_site_without_listeners_goes(self):
        def refuse(node, made):
            if node["region"] == "ap-southeast" and node["type"] == "g6-dedicated-4" \
                    and node["role"] == "load" and "-load-" in node["name"]:
                return CAPACITY
            return None
        code, err, state = self.run_up(refuse)
        self.assertIsNone(code)
        # Every other region's listener hosts were created; Sydney had none,
        # so its core was deleted too and the lab goes on without it.
        self.assertEqual(state["sites_left_out"], ["sydney"])
        self.assertFalse([name for name in state["nodes"] if name.startswith("syd-")])
        self.assertEqual(sum(1 for name in state["nodes"] if "-load-" in name), 24)
        self.assertEqual(state["listeners_missing"], [])
        self.assertEqual(len([item for item in state["removed"]
                              if item["name"].startswith("syd-")]), 14)
        self.assertIn("left out", err)

    def test_an_optional_site_planned_without_listener_hosts_is_kept(self):
        """The referee's cmpd5 (23 Nov): a topology with no listener hosts
        at all (topology-dedicated-syd2.json) and `up --optional-sites
        sydney`. Every Sydney node was created and then deleted as "none of
        its listener hosts was created", a rule that could not be met. The
        rule now holds only where the topology gives the site listener
        hosts: nothing created is deleted, and nothing is missing."""
        self.nodes = [node for node in self.nodes if not lab_tool.is_listener_host(node)]
        sydney = sorted(node["name"] for node in self.nodes if node["site"] == "sydney")
        self.assertTrue(sydney)
        code, err, state = self.run_up(lambda node, made: None)
        self.assertIsNone(code)
        self.assertEqual(state["sites_left_out"], [])
        self.assertEqual(sorted(name for name in state["nodes"] if name.startswith("syd-")),
                         sydney)
        self.assertEqual(state["removed"], [])
        self.assertEqual(self.deleted, [])
        self.assertEqual(state["listeners_missing"], [])
        self.assertNotIn("left out", err)
        # Where the topology does give the site listener hosts, a site left
        # with none of them still goes (test_a_capacity_refusal_stops_...).

    def test_a_transient_refusal_in_the_core_is_tried_again_not_rolled_back(self):
        tried: list[str] = []

        def refuse(node, made):
            tried.append(node["name"])
            return TRANSIENT if node["name"] == "lon-edge-3" and tried.count("lon-edge-3") == 1 \
                else None
        code, err, state = self.run_up(refuse)
        self.assertIsNone(code)
        self.assertEqual(self.downs, 0)
        self.assertEqual(len(state["nodes"]), 99)
        self.assertEqual(tried.count("lon-edge-3"), 2)
        # The rest of the core went on meanwhile; the retry came at its end.
        self.assertEqual([name for name in tried if "-load-" not in name][-1], "lon-edge-3")
        self.assertEqual([(item["name"], item["kind"], item.get("retried"))
                          for item in state["refusals"]], [("lon-edge-3", "transient", True)])
        self.assertIn("trying them once more", err)

    def test_a_transient_refusal_twice_in_the_core_deletes_the_lab(self):
        code, err, state = self.run_up(
            lambda node, made: TRANSIENT if node["name"] == "lon-edge-3" else None, optional=())
        self.assertEqual(code, 1)
        self.assertEqual(self.downs, 1)
        self.assertEqual(self.created.count("lon-edge-3"), 0)
        self.assertEqual(len(self.created), 58)  # every other node of the core
        self.assertEqual([item["kind"] for item in state["refusals"]], ["transient", "transient"])
        self.assertIn("the core was refused at lon-edge-3 (transient", err)

    def test_a_transient_refusal_among_the_listener_hosts_misses_that_host_only(self):
        code, _err, state = self.run_up(
            lambda node, made: TRANSIENT if node["name"] == "tok-load-2" else None)
        self.assertIsNone(code)
        self.assertEqual(sum(1 for name in state["nodes"] if "-load-" in name), 39)
        self.assertEqual([(item["name"], item["kind"]) for item in state["listeners_missing"]],
                         [("tok-load-2", "transient")])

    def test_a_region_left_without_listener_hosts_is_named(self):
        # Tokyo's g6-dedicated-8 refused in batch 1: batch 2 does not try
        # Tokyo's plan, and Tokyo (not optional) keeps its core.
        def refuse(node, made):
            if node["region"] == "ap-northeast" and "-load-" in node["name"]:
                return CAPACITY
            return None
        code, err, state = self.run_up(refuse, optional=())
        self.assertIsNone(code)
        self.assertEqual(sorted(item["name"] for item in state["listeners_missing"]),
                         sorted(f"tok-load-{index}" for index in range(1, 9)))
        self.assertEqual(sum(1 for item in state["listeners_missing"]
                             if item["kind"] == "not attempted"), 7)
        self.assertIn("tok-edge-1", state["nodes"])
        self.assertIn("no listener host in tokyo", err)
        self.assertIn("--without-sites tokyo", err)

    def test_a_refusal_in_the_core_deletes_the_lab_and_says_what_it_cost(self):
        code, err, state = self.run_up(
            lambda node, made: ACCOUNT if made >= 20 else None, optional=())
        self.assertEqual(code, 1)
        self.assertEqual(self.downs, 1)
        # The first refusal stopped the core: 20 created, the 21st refused.
        self.assertEqual(len(self.created), 20)
        self.assertIn("the core was refused", err)
        self.assertIn("$", err)
        self.assertEqual(state["refusals"][0]["kind"], "account")

    def test_an_account_refusal_in_the_core_gives_up_the_optional_site_first(self):
        # 50 instances allowed: Sydney's core (14) and 36 of the 45 others.
        def refuse(node, made):
            live = made - len([path for path in self.deleted if path.startswith("/linode")])
            return ACCOUNT if live >= 50 else None
        code, err, state = self.run_up(refuse)
        self.assertIsNone(code)
        self.assertEqual(self.downs, 0)
        self.assertEqual(state["sites_left_out"], ["sydney"])
        core = [node["name"] for node in self.nodes
                if not lab_tool.is_listener_host(node) and node["site"] != "sydney"]
        self.assertTrue(set(core) <= set(state["nodes"]))
        # Then 5 listener hosts fit, and the rest are recorded as missing.
        self.assertEqual(sum(1 for name in state["nodes"] if "-load-" in name), 5)
        self.assertEqual(len(state["listeners_missing"]), 19)
        self.assertEqual(len(state["removed"]), 14)
        self.assertIn("given back", err)

    def test_grow_clears_the_listener_hosts_it_created(self):
        self.state["nodes"] = {"tok-load-3": {"site": "tokyo"}}
        self.state["listeners_missing"] = [{"name": "tok-load-3", "kind": "account"},
                                           {"name": "tok-load-4", "kind": "account"}]
        self.path.write_text(json.dumps(self.state))
        lab_tool.refresh_listeners_missing(self.path, "HTTP 400: Account Limit reached")
        missing = json.loads(self.path.read_text())["listeners_missing"]
        self.assertEqual([item["name"] for item in missing], ["tok-load-4"])
        self.assertIn("grow: HTTP 400", missing[0]["why"])


if __name__ == "__main__":
    unittest.main()
