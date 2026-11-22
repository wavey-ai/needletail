#!/usr/bin/env python3
"""Tests of the large audio mesh's deploy plan (spec-scale part M): the
topology files are what scripts/scale_topology.py generates, dedicated plans
only, Sydney first; the deploy tool renders topology-scale.json (the
referee's plan: eight London edges, four on standby for the first curve) in
the merged topology (every edge fed by its
site's two forwarders on the fast plane and the cache mesh, the cache mesh's
children raised to the site's edges, no class port collision, distinct
fast-lane ranks); standby edges are left out of the render and stopped;
listener load hosts get needletail-load, ntcmp and their kernel settings; the
port plan's limit on relay children is enforced; the lab tool leaves a site
out of `up` and `grow` and prices a topology from the API's plans.
python3 -m unittest scripts/test_scale_topology.py"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tempfile
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


def topology_state(topology: dict) -> dict:
    """A Linode state for a topology document: made-up public addresses, the
    fields that the lab tool keeps (purpose included)."""
    nodes = {}
    for index, node in enumerate(topology["nodes"], 1):
        nodes[node["name"]] = {
            "name": node["name"], "role": node["role"], "site": node["site"],
            "region": node["region"], "type": node["type"],
            **({"purpose": node["purpose"]} if node.get("purpose") else {}),
            "public_ipv4": f"198.51.{index // 250}.{index % 250 + 1}",
            "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
    return {"run_id": "test", "hourly_usd": 0.0, "nodes": nodes}


def flag_values(args: list[str], flag: str) -> list[str]:
    return [args[index + 1] for index, arg in enumerate(args) if arg == flag]


class Files(unittest.TestCase):
    def test_the_committed_files_are_what_the_generator_writes(self):
        for name, kwargs in scale_topology.VARIANTS.items():
            self.assertEqual((FILES / name).read_text(),
                             scale_topology.render(scale_topology.scale_topology(**kwargs)), name)

    def test_shape_of_the_scale_topology(self):
        topology = json.loads(SCALE.read_text())
        nodes = topology["nodes"]
        self.assertTrue(all(node["type"].startswith("g6-dedicated-") for node in nodes))
        names = [node["name"] for node in nodes]
        self.assertEqual(len(names), len(set(names)))
        vlans = [(node["region"], node["vlan_ipv4"]) for node in nodes if node.get("vlan_ipv4")]
        self.assertEqual(len(vlans), len(set(vlans)))
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
                 if node["role"] == "edge" and node["site"] == "london"]
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
        for bad in ({"sites": ("newyork", "tokyo")}, {"edges": {"mars": 2}},
                    {"edges": {"london": 0}}, {"load_type": "g6-standard-8"},
                    {"standby": {"london": 10}}):
            with self.assertRaises(ValueError, msg=bad):
                scale_topology.scale_topology(**bad)


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
        program = deploy.build_program(active, settings, ROUTES)
        plan = deploy.compile_plan(program)
        rendered = deploy.render(active, settings, plan)
        return full, plan, {name: item["args"] for name, item in rendered.items()}

    def check_site(self, state: dict, args: dict, site: str, edges: list[str]) -> None:
        nodes = state["nodes"]
        dists = sorted(name for name, node in nodes.items()
                       if node["role"] == "distributor" and node["site"] == site)
        for dist in dists:
            peers = flag_values(args[dist], "--peer")
            self.assertEqual(peers, [f"{nodes[edge]['vlan_ipv4']}:{deploy.MESH_PORT}"
                                     for edge in sorted(edges)], dist)
            children = int(deploy.flag_value(args[dist], "--cache-mesh-max-children"))
            self.assertGreaterEqual(children, len(edges), dist)
            lease = int(deploy.flag_value(args[dist], "--relay-max-lease-children"))
            self.assertGreaterEqual(lease, len(edges), dist)
        ranks = []
        for edge in edges:
            parents = {deploy.flag_value(args[edge], "--relay-primary-id"),
                       deploy.flag_value(args[edge], "--relay-secondary-id")}
            self.assertEqual(parents, set(dists), edge)
            ranks.append(deploy.flag_value(args[edge], "--client-udp-edge-rank"))
            self.assertEqual(sorted(flag_values(args[edge], "--peer")),
                             sorted(f"{nodes[dist]['vlan_ipv4']}:{deploy.MESH_PORT}"
                                    for dist in dists), edge)
        self.assertEqual(sorted(ranks, key=int), [str(rank) for rank in range(len(edges))])
        # Each forwarder is the primary of half of the edges.
        primaries = [deploy.flag_value(args[edge], "--relay-primary-id") for edge in edges]
        self.assertEqual(sorted(primaries.count(dist) for dist in dists),
                         [len(edges) // 2, len(edges) - len(edges) // 2])

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

    def test_standby_edges_are_left_out_and_stopped(self):
        standby = STANDBY
        state, _plan, args = self.render(SCALE, FIRST_CURVE)
        for name in standby:
            self.assertNotIn(name, args)
        self.check_site(state, args, "london", [f"lon-edge-{index}" for index in range(1, 6)])
        # Their mates render as on a file of five edges a region.
        five = scale_topology.scale_topology(edges={site: 5 for site in scale_topology.SITES},
                                             standby={})
        _s, _p, fewer = self.render(SCALE, state=topology_state(five))
        for name in ("lon-dist-a", "lon-dist-b", "lon-edge-1", "lon-edge-5", "tok-dist-b"):
            self.assertEqual(flag_values(args[name], "--peer"), flag_values(fewer[name], "--peer"))
        sent = []
        with mock.patch.object(deploy.common, "rsync_to", lambda *a, **k: None), \
                mock.patch.object(deploy.common, "ssh_text",
                                  lambda node, script, **k: sent.append(script) or ""):
            deploy.install_node(state["nodes"]["lon-edge-6"], {}, self.settings(), False)
        self.assertIn("systemctl disable --now needletail-mesh.service", sent[-1])
        with self.assertRaises(SystemExit):
            deploy.active_state(state, self.settings(['standby_edges=["lon-dist-a"]']))

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

    def test_client_udp_max_subscribers(self):
        _state, _plan, args = self.render(SCALE, ["client_udp_max_subscribers=20000"])
        self.assertEqual(deploy.flag_value(args["lon-edge-1"], "--client-udp-max-subscribers"),
                         "20000")
        _state, _plan, args = self.render(SCALE)
        self.assertNotIn("--client-udp-max-subscribers", args["lon-edge-1"])


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
            deploy.install_node(listener, {}, self.settings(), False)
            deploy.install_node(view, {}, self.settings(), False)
        self.assertIn("net.ipv4.ip_local_port_range = 1024 65000", sent[0])
        self.assertIn("91-needletail-listeners.conf", sent[0])
        self.assertNotIn("91-needletail-listeners.conf", sent[1])

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
            deploy.install_node({"name": "lon-load", "role": "load", "site": "london"}, {},
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
        hourly, problems = lab_tool.topology_cost(json.loads(SCALE.read_text()), self.TYPES,
                                                  self.REGIONS)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(hourly, 10.422, places=3)
        topology = lab_tool.without_sites(json.loads(SCALE.read_text()), ["sydney"])
        hourly, _ = lab_tool.topology_cost(topology, self.TYPES, self.REGIONS)
        self.assertAlmostEqual(hourly, 10.422 - 2.538, places=3)

    def test_capacity_and_shared_plans_are_named(self):
        topology = json.loads(SCALE.read_text())
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
        base = lab_tool.without_sites(json.loads(SCALE.read_text()), ["sydney"])
        base["nodes"] = [node for node in base["nodes"] if node["name"] not in STANDBY]
        state = {"run_id": "t", "tag": "t", "image": "linode/debian13", "hourly_usd": 0,
                 "firewall_id": 1, "sites_left_out": ["sydney"],
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
                mock.patch.object(lab_tool, "add_nodes",
                                  lambda args, api, state, nodes: added.extend(nodes)), \
                mock.patch.object(lab_tool, "command_status", lambda args: None), \
                redirect_stdout(io.StringIO()):
            lab_tool.command_grow(args)
        self.assertEqual(sorted(node["name"] for node in added),
                         sorted(name for name in STANDBY if not name.startswith("syd-")))

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

        def watchdog(args):
            events.append(("watchdog", args.hours, args.foreground))
            state = json.loads(pathlib.Path(args.state).read_text())
            state["watchdog_deadline_unix"] = 123
            pathlib.Path(args.state).write_text(json.dumps(state))

        def create(api, run_id, tag, image, key, node):
            events.append(("create", node["name"]))
            raise RuntimeError("no capacity")

        args = lab_tool.argparse.Namespace(
            state=str(tmp / "state.json"), topology=str(SCALE), run_id="t1",
            ssh_public_key=str(key), without_sites="", optional_sites="", watchdog_hours=5.5)
        with mock.patch.object(lab_tool, "Api", FakeApi), \
                mock.patch.object(lab_tool, "MAX_NODES", 200), \
                mock.patch.object(lab_tool, "MAX_HOURLY_USD", 20.0), \
                mock.patch.object(lab_tool, "command_watchdog", watchdog), \
                mock.patch.object(lab_tool, "create_node", create), \
                mock.patch.object(lab_tool, "command_down", lambda args: events.append(("down",))), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            lab_tool.command_up(args)
        self.assertEqual(events[0], ("watchdog", 5.5, False))
        self.assertEqual(events[1][0], "create")
        self.assertEqual(events[-1], ("down",))
        self.assertEqual(json.loads((tmp / "state.json").read_text())["watchdog_deadline_unix"],
                         123)


if __name__ == "__main__":
    unittest.main()
