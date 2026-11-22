#!/usr/bin/env python3
"""Tests of the fast-plane feed of the edges in the deploy tool and the lab
(wave 5 W0, `edge_fast_plane`): every edge binds the two distributors of its
site as relay parents, alternating which is primary, with its failover
controller toward the secondary and a fast-lane rank of its own; every
distributor renders its lease children's bind, ports, primary children and
failover listener; the setting off (or the relay class split off) renders
the edges on the cache mesh alone; the render's check refuses a feed that
an extra argument broke; every rendered flag is one that the av-mesh binary
has; and the lab's edges keep one --fec-bind.
python3 -m unittest scripts/test_fast_plane_edges.py"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_module("deploy_fast_plane", "linode-small-mesh-deploy.py")
COMPILER = deploy.common.ROOT / "target" / "release" / "needletail-compile"
AV_MESH = pathlib.Path(os.environ.get(
    "NEEDLETAIL_AV_MESH_BIN", str(lab.MESH_ROOT / "target" / "release" / "av-mesh")))
ROUTES = {"best_direct_rtt_us": 74000, "primary_rtt_us": 74000, "primary_jitter_us": 1000,
          "secondary_rtt_us": 80000, "secondary_jitter_us": 1000}
MERGED_TOPOLOGY = deploy.common.ROOT / "deploy" / "low-latency" / "topology-merged.json"
EDGE_FLAGS = ("--relay-controlled-local", "--relay-primary-peer", "--relay-secondary-peer",
              "--relay-failover-controller", "--edge-fast-ingest", "--client-udp-edge-rank")
LEASE_FLAGS = ("--relay-lease-bind", "--relay-lease-failover-bind",
               "--relay-lease-primary-children", "--relay-lease-child-ports")


def value_of(args: list[str], flag: str) -> str | None:
    return deploy.flag_value(args, flag)


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


class Feed(Base):
    """fast_plane_feed: who feeds which edge."""

    def test_edges_alternate_their_primary_and_take_distinct_ranks(self):
        plan = deploy.class_plan(self.settings())
        self.assertTrue(plan["edge_fast_plane"])
        feed = deploy.fast_plane_feed(lab.lab_state(), plan)
        got = {name: (item["primary"]["name"], item["secondary"]["name"], item["rank"])
               for name, item in feed["edges"].items()}
        self.assertEqual(got, {
            "lon-edge-1": ("lon-dist-a", "lon-dist-b", 0),
            "lon-edge-2": ("lon-dist-b", "lon-dist-a", 1),
            "lon-edge-3": ("lon-dist-a", "lon-dist-b", 2),
            "lon-edge-4": ("lon-dist-b", "lon-dist-a", 3),
            "new-edge-1": ("new-dist-a", "new-dist-b", 0),
            "new-edge-2": ("new-dist-b", "new-dist-a", 1)})
        forwarders = {name: (item["primary_children"], item["children"])
                      for name, item in feed["forwarders"].items()}
        self.assertEqual(forwarders["lon-dist-a"],
                         (["10.77.1.21", "10.77.1.23"],
                          ["lon-edge-1", "lon-edge-2", "lon-edge-3", "lon-edge-4"]))
        self.assertEqual(forwarders["lon-dist-b"][0], ["10.77.1.22", "10.77.1.24"])
        self.assertEqual(forwarders["new-dist-a"][0], ["10.77.2.21"])
        # The backbone relays feed no edge.
        self.assertNotIn("relay-primary", forwarders)

    def test_off_with_the_setting_the_class_split_or_the_baseline(self):
        for overrides in (["edge_fast_plane=false"], ["class_isolation=false"],
                          ["relay_class_split=false"], ["low_latency_baseline=true"]):
            plan = deploy.class_plan(self.settings(overrides))
            self.assertFalse(plan["edge_fast_plane"], overrides)
            self.assertEqual(deploy.fast_plane_feed(lab.lab_state(), plan),
                             {"edges": {}, "forwarders": {}, "slot_forwarders": {}}, overrides)

    def test_a_site_of_one_distributor_feeds_its_edges_alone(self):
        state = lab.lab_state()
        del state["nodes"]["new-dist-b"]
        feed = deploy.fast_plane_feed(state, deploy.class_plan(self.settings()))
        for name in ("new-edge-1", "new-edge-2"):
            self.assertEqual(feed["edges"][name]["primary"]["name"], "new-dist-a")
            self.assertIsNone(feed["edges"][name]["secondary"])
        self.assertEqual(feed["forwarders"]["new-dist-a"]["primary_children"],
                         ["10.77.2.21", "10.77.2.22"])

    def test_an_edge_without_a_vlan_address_is_left_to_the_cache_mesh(self):
        state = lab.lab_state()
        state["nodes"]["lon-edge-4"]["vlan_ipv4"] = None
        feed = deploy.fast_plane_feed(state, deploy.class_plan(self.settings()))
        self.assertNotIn("lon-edge-4", feed["edges"])
        self.assertEqual(len(feed["edges"]), 5)


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Render(Base):
    def render(self, overrides=(), state: dict | None = None) -> dict[str, list[str]]:
        settings = self.settings(overrides)
        state = state or lab.lab_state()
        plan = deploy.compile_plan(deploy.build_program(state, settings, ROUTES))
        return {name: item["args"] for name, item in
                deploy.render(state, settings, plan).items()}

    def test_every_edge_is_fed_by_the_distributors_of_its_site(self):
        args = self.render()
        for name, primary, secondary, rank in (
                ("lon-edge-1", "10.77.1.11", "10.77.1.12", "0"),
                ("lon-edge-2", "10.77.1.12", "10.77.1.11", "1"),
                ("new-edge-2", "10.77.2.12", "10.77.2.11", "1")):
            edge = args[name]
            address = lab.node(name)["vlan_ipv4"]
            self.assertIn("--relay-controlled-local", edge)
            self.assertIn("--relay-primary-promoted", edge)
            self.assertIn("--relay-secondary-promoted", edge)
            self.assertEqual(value_of(edge, "--fec-bind"), "0.0.0.0:22001", name)
            self.assertEqual(value_of(edge, "--relay-primary-bind"), "0.0.0.0:22001", name)
            self.assertEqual(value_of(edge, "--relay-secondary-bind"), "0.0.0.0:22002", name)
            self.assertEqual(value_of(edge, "--relay-primary-peer"), f"{primary}:22901", name)
            self.assertEqual(value_of(edge, "--relay-secondary-peer"), f"{secondary}:22901", name)
            self.assertEqual(value_of(edge, "--relay-failover-controller"),
                             f"{address}:22903={secondary}:22902", name)
            self.assertEqual(value_of(edge, "--relay-topology-generation"), "1")
            self.assertEqual(value_of(edge, "--relay-subscription-id"), "1")
            self.assertEqual(value_of(edge, "--relay-primary-silence-ms"), "100")
            self.assertEqual(value_of(edge, "--edge-fast-ingest"), "true")
            self.assertEqual(value_of(edge, "--client-udp-edge-rank"), rank, name)
            self.assertEqual(value_of(edge, "--client-udp-fast-lane"), "true", name)
            # The class threads of the edge.
            self.assertEqual(value_of(edge, "--relay-class-threads"), "true", name)
            self.assertEqual(value_of(edge, "--relay-class-port-offset"), "1000", name)
        self.assertEqual(value_of(args["lon-edge-1"], "--relay-primary-id"), "lon-dist-a")
        self.assertEqual(value_of(args["lon-edge-1"], "--relay-secondary-id"), "lon-dist-b")

    def test_every_distributor_feeds_its_lease_children(self):
        args = self.render()
        for name, primaries in (("lon-dist-a", "10.77.1.21,10.77.1.23"),
                                ("lon-dist-b", "10.77.1.22,10.77.1.24"),
                                ("new-dist-a", "10.77.2.21"), ("new-dist-b", "10.77.2.22")):
            node_args = args[name]
            address = lab.node(name)["vlan_ipv4"]
            self.assertEqual(value_of(node_args, "--relay-lease-children"), "true", name)
            self.assertEqual(value_of(node_args, "--relay-lease-bind"), f"{address}:22901", name)
            self.assertEqual(value_of(node_args, "--relay-lease-child-ports"), "22001,22002")
            self.assertEqual(value_of(node_args, "--relay-lease-primary-children"), primaries)
            self.assertEqual(value_of(node_args, "--relay-lease-failover-bind"),
                             f"{address}:22902", name)
            self.assertEqual(value_of(node_args, "--relay-max-lease-children"), "16", name)

    def test_off_renders_the_edges_on_the_cache_mesh_alone(self):
        on = self.render()
        off = self.render(["edge_fast_plane=false"])
        for name in lab.edges("london") + lab.edges("newyork"):
            for flag in EDGE_FLAGS:
                self.assertNotIn(flag, off[name], (name, flag))
            self.assertNotIn("--relay-class-threads", off[name])
        for name in lab.distributors("london") + lab.distributors("newyork"):
            self.assertEqual(value_of(off[name], "--relay-lease-children"), "false", name)
            for flag in LEASE_FLAGS:
                self.assertNotIn(flag, off[name], (name, flag))
            # Nothing else of a distributor changes.
            strip = lambda items: [item for item in items if item not in  # noqa: E731
                                   ("--relay-lease-children", "true", "false")]
            on_args = list(on[name])
            for flag in LEASE_FLAGS + ("--relay-max-lease-children",):
                if flag in on_args:
                    index = on_args.index(flag)
                    del on_args[index:index + 2]
            self.assertEqual(strip(on_args), strip(off[name]), name)
        # The fast client lane is its own setting.
        self.assertEqual(value_of(off["lon-edge-1"], "--client-udp-fast-lane"), "true")
        lane_off = self.render(["client_udp_fast_lane=false"])
        for name in ("lon-dist-a", "lon-edge-1", "new-edge-2"):
            self.assertEqual(value_of(lane_off[name], "--client-udp-fast-lane"), "false", name)
        # Without the relay class split there are no class threads to feed.
        split_off = self.render(["class_isolation=false"])
        self.assertNotIn("--relay-controlled-local", split_off["lon-edge-1"])
        self.assertNotIn("--relay-lease-children", split_off["lon-dist-a"])

    def test_the_legacy_topology_feeds_the_edges_the_same_way(self):
        merged = self.render()
        legacy = self.render(["topology=legacy"])
        for name in lab.edges("london") + lab.edges("newyork"):
            self.assertEqual(merged[name], legacy[name], name)
        self.assertNotIn("--relay-lease-bind", legacy["relay-primary"])

    def test_the_fast_plane_ports_do_not_collide(self):
        args = self.render()
        plan = deploy.class_plan(self.settings())
        edge = {port for _s, port, _a in deploy.udp_binds(args["lon-edge-1"], plan)}
        self.assertTrue({22001, 23001, 22002, 23002, 22903, 29101, 30101, 19446} <= edge, edge)
        forwarder = {port for _s, port, _a in deploy.udp_binds(args["lon-dist-a"], plan)}
        self.assertTrue({22001, 23001, 22901, 22902} <= forwarder, forwarder)
        for name, node_args in args.items():
            flags = [arg for arg in node_args if arg.startswith("--")]
            repeated = {flag for flag in flags if flags.count(flag) > 1} - {
                "--peer", "--relay-forward", "--telemetry-peer", "--failover-entry",
                "--hls-rendition", "--relay-failover-listener"}
            self.assertFalse(repeated, name)

    def test_an_extra_argument_that_breaks_the_feed_is_refused(self):
        for overrides, words in (
                (['extra_edge_args=["--relay-primary-peer","10.77.1.11:22999"]'],
                 "not the --relay-lease-bind"),
                (['extra_distributor_args=["--relay-lease-child-ports","24001,24002"]'],
                 "sends its primary children's symbols to port 24001"),
                (['extra_distributor_args=["--relay-lease-primary-children","10.77.1.99"]'],
                 "does not name it"),
                (['extra_edge_args=["--client-udp-edge-rank","7"]'], "share --client-udp-edge-rank"),
                (['extra_distributor_args=["--relay-lease-failover-bind","10.77.1.11:22998"]'],
                 "lease failover listener")):
            with self.subTest(overrides), self.assertRaises(ValueError) as caught:
                self.render(overrides)
            self.assertIn(words, str(caught.exception))
        # An edge may still be turned off on its own: its relay flags are then
        # ignored by av-mesh, and the render is consistent.
        args = self.render(['extra_edge_args=["--edge-fast-ingest","false"]'])
        self.assertEqual(value_of(args["lon-edge-1"], "--edge-fast-ingest"), "false")

    def test_the_merged_linode_topology(self):
        topology = json.loads(MERGED_TOPOLOGY.read_text())
        nodes = {}
        for index, node in enumerate(topology["nodes"], 1):
            nodes[node["name"]] = {
                "name": node["name"], "role": node["role"], "site": node["site"],
                "region": node["region"], "type": node["type"],
                "public_ipv4": f"192.0.2.{index}",
                "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
        state = {"run_id": "test", "hourly_usd": 0.0, "nodes": nodes}
        args = self.render(state=state)
        edges = [name for name, item in nodes.items() if item["role"] == "edge"]
        self.assertTrue(edges)
        for name in edges:
            parents = {value_of(args[name], "--relay-primary-id"),
                       value_of(args[name], "--relay-secondary-id")}
            site = nodes[name]["site"]
            self.assertEqual(parents, {other for other, item in nodes.items()
                                       if item["role"] == "distributor" and item["site"] == site},
                             name)
            # Dedicated 2-vCPU edges pin the class threads as the forwarders do.
            self.assertEqual(value_of(args[name], "--relay-class-cpus"), "0,1", name)

    @unittest.skipUnless(AV_MESH.is_file(), "needs the av-mesh release binary")
    def test_every_rendered_flag_is_an_av_mesh_flag(self):
        """The flags are read from av-mesh itself (its --help), so a renamed
        or missing flag fails here rather than at the start of a lab."""
        text = subprocess.run([str(AV_MESH), "--help"], capture_output=True, text=True,
                              timeout=30).stdout
        known = set(re.findall(r"(--[a-z0-9][a-z0-9-]*)", text))
        self.assertIn("--relay-lease-bind", known)
        args = self.render()
        for name, node_args in args.items():
            if lab.node(name)["role"] == "contributor":
                continue
            unknown = deploy.flag_names(node_args) - known
            self.assertFalse(unknown, name)


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Lab(Base):
    def test_the_lab_renders_one_fec_bind_per_edge(self):
        work = self.dir / "lab"
        with mock.patch.object(lab, "WORK", work), \
                redirect_stderr(io.StringIO()):
            fed = lab.render([])
            alone = lab.render(["edge_fast_plane=false"])
        for name in lab.edges("london") + lab.edges("newyork"):
            self.assertEqual(fed[name]["args"].count("--fec-bind"), 1, name)
            self.assertEqual(value_of(fed[name]["args"], "--fec-bind"), "0.0.0.0:22001", name)
            self.assertEqual(alone[name]["args"].count("--fec-bind"), 1, name)
            self.assertEqual(value_of(alone[name]["args"], "--fec-bind"), "0.0.0.0:12001", name)
        # The lab is not a Linode type: nothing is pinned.
        self.assertNotIn("--relay-class-cpus", fed["lon-edge-1"]["args"])


def _netns_works() -> bool:
    try:
        return subprocess.run(["unshare", "-rn", "true"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(COMPILER.is_file() and AV_MESH.is_file() and _netns_works(),
                     "needs needletail-compile, the av-mesh release binary and unshare -rn")
class Start(Base):
    """The rendered forwarder and edge start: av-mesh accepts the arguments,
    binds every socket and runs the fast plane (each alone in a private
    network namespace with its addresses; nothing is sent)."""

    def start(self, name: str, seconds: float = 3.0) -> str:
        settings = self.settings()
        state = lab.lab_state()
        deploy.ensure_tls()
        plan = deploy.compile_plan(deploy.build_program(state, settings, ROUTES))
        args = list(deploy.render(state, settings, plan)[name]["args"])
        args[0] = str(AV_MESH)
        for index, arg in enumerate(args):
            if arg in ("--cert", "--key"):
                args[index + 1] = str(self.dir / "tls" / pathlib.Path(args[index + 1]).name)
        node = lab.node(name)
        setup = ["ip link set lo up", "ip link add d0 type dummy"]
        setup += [f"ip addr add {ip}/24 dev d0" for ip in (node["public_ipv4"], node["vlan_ipv4"])]
        setup += ["ip link set d0 up"]
        script = ("; ".join(setup) + f"; RUST_LOG=av_mesh=info timeout {seconds} "
                  + " ".join(shlex.quote(arg) for arg in args) + "; echo exit=$?")
        out = subprocess.run(["unshare", "-rn", "bash", "-c", script], capture_output=True,
                             text=True, timeout=seconds + 30)
        return re.sub(r"\x1b\[[0-9;]*m", "", out.stdout + out.stderr)

    def test_a_fed_edge_starts_its_fast_plane(self):
        log = self.start("lon-edge-1")
        # timeout's 124: it ran until it was stopped.
        self.assertIn("exit=124", log, log[-2000:])
        for port in ("22001", "23001", "22002", "23002"):
            self.assertRegex(log, rf"fast-plane RelaySession ingest listening bind=0\.0\.0\.0:{port}")
        self.assertIn("failover controller ready bind=10.77.1.21:22903 target=10.77.1.12:22902", log)

    def test_a_forwarder_starts_its_lease_children(self):
        log = self.start("lon-dist-a")
        self.assertIn("exit=124", log, log[-2000:])
        self.assertIn("fast-plane lease children ready bind=10.77.1.11:22901", log)
        self.assertIn("primary_children=[10.77.1.21, 10.77.1.23]", log)
        self.assertIn("lease children's failover listener ready bind=10.77.1.11:22902", log)


if __name__ == "__main__":
    unittest.main()
