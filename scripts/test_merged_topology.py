#!/usr/bin/env python3
"""Tests of the merged topology (low-latency item 4) in the deploy tool and
the lab: relay and distributor are one forwarder. The contributor feeds the
two distributors of its own site and nothing else (no direct lane to an
edge, no --relay-direct-target), every other distributor has both of them as
parents, crossed, the backbone relays get no service, the plan records the
shared parent region as a declared gap, the class ports do not collide, and
everything but the relay arguments renders as the legacy topology does.
python3 -m unittest scripts/test_merged_topology.py"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import shutil
import tempfile
import unittest
import sys
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


deploy = load_module("deploy_merged", "linode-small-mesh-deploy.py")
COMPILER = deploy.common.ROOT / "target" / "release" / "needletail-compile"
ROUTES = {"best_direct_rtt_us": 74000, "primary_rtt_us": 74000, "primary_jitter_us": 1000,
          "secondary_rtt_us": 80000, "secondary_jitter_us": 1000}
MERGED_TOPOLOGY = deploy.common.ROOT / "deploy" / "low-latency" / "topology-merged.json"
# Arguments that carry the relay DAG; everything else renders the same in
# both topologies.
RELAY_ARGS = ("--fec-bind", "--mesh-fec-target", "--mesh-media-fec-target",
              "--audio-epoch-ingress-target", "--audio-epoch-redundant-ingress-target")


def value_of(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def values_of(args: list[str], flag: str) -> list[str]:
    return [args[index + 1] for index, arg in enumerate(args) if arg == flag]


def without_relay_args(args: list[str]) -> list[str]:
    """`args` without the relay DAG's flags and their values."""
    out = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in RELAY_ARGS or (arg.startswith("--relay-") and arg not in (
                "--relay-class-threads", "--relay-class-port-offset", "--relay-class-realtime",
                "--relay-class-cpus", "--relay-class-lanes", "--relay-audio-socket-priority",
                "--relay-video-pacing-mbps", "--relay-audio-inline", "--relay-repair-both-lanes",
                "--relay-local-id", "--relay-exclusive", "--relay-secondary-seed-source",
                "--relay-controlled-local", "--relay-min-repair-symbols",
                "--relay-deadline-ms", "--relay-topology-generation",
                "--relay-subscription-id")):
            # A flag without a value is followed by another flag.
            index += 1 if index + 1 < len(args) and args[index + 1].startswith("--") else 2
            continue
        out.append(arg)
        index += 1
    return out


def topology_state(path: pathlib.Path) -> dict:
    """A Linode state for a topology file: made-up public addresses."""
    topology = json.loads(path.read_text())
    nodes = {}
    for index, node in enumerate(topology["nodes"], 1):
        nodes[node["name"]] = {
            "name": node["name"], "role": node["role"], "site": node["site"],
            "region": node["region"], "type": node["type"],
            "public_ipv4": f"192.0.2.{index}",
            "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
    return {"run_id": "test", "hourly_usd": 0.0, "nodes": nodes}


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


class Settings(Base):
    def test_merged_is_the_default_and_legacy_a_setting(self):
        self.assertEqual(deploy.topology_mode(self.settings()), "merged")
        self.assertEqual(deploy.topology_mode(self.settings(["topology=legacy"])), "legacy")
        # A file saved before the setting existed is merged.
        self.assertEqual(deploy.topology_mode({}), "merged")
        for bad in (["topology=star"], ["max_edges_per_forwarder=0"]):
            with self.assertRaises(SystemExit, msg=bad):
                self.settings(bad)

    def test_contributor_direct_region_is_refused_in_the_merged_topology(self):
        with self.assertRaises(SystemExit) as caught:
            self.settings(["contributor_direct_region=true"])
        self.assertIn("merged topology", str(caught.exception))

    def test_the_tree_of_the_lab(self):
        state = lab.lab_state()
        tree = deploy.relay_tree(state, self.settings())
        self.assertEqual([node["name"] for node in tree["first"]], ["lon-dist-a", "lon-dist-b"])
        self.assertEqual([node["name"] for node in tree["unused"]],
                         ["relay-primary", "relay-secondary"])
        crossed = {child["node"]["name"]: (child["primary"]["name"], child["secondary"]["name"],
                                           child["index"]) for child in tree["children"]}
        self.assertEqual(crossed, {"new-dist-a": ("lon-dist-a", "lon-dist-b", 0),
                                   "new-dist-b": ("lon-dist-b", "lon-dist-a", 1)})
        legacy = deploy.relay_tree(state, self.settings(["topology=legacy"]))
        self.assertEqual([node["name"] for node in legacy["first"]],
                         ["relay-primary", "relay-secondary"])
        self.assertEqual(len(legacy["children"]), 4)
        self.assertEqual(legacy["unused"], [])

    def test_a_site_needs_two_distributors_for_the_merged_topology(self):
        state = lab.lab_state()
        del state["nodes"]["lon-dist-b"]
        with self.assertRaises(SystemExit) as caught:
            deploy.relay_tree(state, self.settings())
        self.assertIn("topology=legacy", str(caught.exception))

    def test_more_edges_than_a_forwarder_feeds_are_refused(self):
        # The lab has four London edges.
        with self.assertRaises(SystemExit) as caught:
            deploy.build_program(lab.lab_state(), self.settings(["max_edges_per_forwarder=3"]),
                                 ROUTES)
        self.assertIn("regional relays", str(caught.exception))
        deploy.build_program(lab.lab_state(), self.settings(["max_edges_per_forwarder=4"]), ROUTES)

    def test_routes_follow_the_tree(self):
        state = lab.lab_state()
        rtt = {"10.77.1.11": 300, "10.77.1.12": 400, "10.90.2.11": 74_000, "10.90.2.12": 75_000}

        def measure(source, target, count=7):
            # The leg from lon-dist-b is 1 ms longer than from lon-dist-a.
            extra = 1000 if source["name"] == "lon-dist-b" else 0
            return rtt.get(target, 70_000) + extra, 100

        with mock.patch.object(deploy, "measure_rtt_us", measure):
            routes = deploy.measure_routes(state, self.settings())
        self.assertEqual(routes["topology"], "merged")
        self.assertEqual(routes["contrib_primary_rtt_us"], 300)
        self.assertEqual(routes["contrib_secondary_rtt_us"], 400)
        # new-dist-b: primary lon-dist-b (400 + 76,000), secondary lon-dist-a.
        new_b = routes["distributors"]["new-dist-b"]
        self.assertEqual(new_b["primary_route_rtt_us"], 400 + 76_000)
        self.assertEqual(new_b["secondary_route_rtt_us"], 300 + 75_000)
        self.assertEqual(routes["primary_rtt_us"], 76_400)
        # A first forwarder is a route of one leg.
        self.assertEqual(routes["distributors"]["lon-dist-a"]["primary_route_rtt_us"], 300)


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Render(Base):
    def render(self, overrides=(), state: dict | None = None) -> tuple[dict, dict, dict]:
        settings = self.settings(overrides)
        state = state or lab.lab_state()
        program = deploy.build_program(state, settings, ROUTES)
        plan = deploy.compile_plan(program)
        rendered = deploy.render(state, settings, plan)
        return program, plan, {name: item["args"] for name, item in rendered.items()}

    def test_three_links_in_the_contributors_region(self):
        _program, _plan, args = self.render()
        contrib = args["contrib-lon"]
        # contributor -> lon-dist-a / lon-dist-b over the London VLAN.
        self.assertEqual(value_of(contrib, "--relay-primary-id"), "lon-dist-a")
        self.assertEqual(value_of(contrib, "--relay-primary-target"), "10.77.1.11:22001")
        self.assertEqual(value_of(contrib, "--relay-secondary-id"), "lon-dist-b")
        self.assertEqual(value_of(contrib, "--relay-secondary-target"), "10.77.1.12:22002")
        # No legacy AEP1 lane by default (wave 7 item 1): no target, and no
        # --audio-epoch-mesh-lane (an older av-contrib must still start);
        # with audio_epoch_lane the two first forwarders are its targets.
        self.assertNotIn("--audio-epoch-mesh-lane", contrib)
        self.assertNotIn("--audio-epoch-ingress-target", contrib)
        self.assertNotIn("--audio-epoch-redundant-ingress-target", contrib)
        _program, _plan, lane = self.render(["audio_epoch_lane=true"])
        self.assertNotIn("--audio-epoch-mesh-lane", lane["contrib-lon"])
        self.assertEqual(value_of(lane["contrib-lon"], "--audio-epoch-ingress-target"),
                         "10.77.1.11:22001")
        self.assertEqual(value_of(lane["contrib-lon"], "--audio-epoch-redundant-ingress-target"),
                         "10.77.1.12:22002")
        for name, port, source in (("lon-dist-a", "22001", "10.77.1.5:22301"),
                                   ("lon-dist-b", "22002", "10.77.1.5:22302")):
            self.assertEqual(value_of(args[name], "--relay-primary-id"), "contrib", name)
            self.assertEqual(value_of(args[name], "--relay-primary-peer"), source, name)
            self.assertEqual(value_of(args[name], "--fec-bind"), f"0.0.0.0:{port}", name)
            self.assertNotIn("--relay-secondary-id", args[name])
            # Repair on both contributor lanes is admitted on both.
            self.assertEqual(args[name].count("--relay-primary-promoted"), 1, name)
        # distributor -> edge: the cache mesh of the site, and the fast plane
        # from the same two distributors (edge_fast_plane).
        # The edges also peer the forwarder slot lon-dist-c (a peer that is
        # down until it is taken into service).
        for edge in lab.edges("london"):
            self.assertEqual(values_of(args[edge], "--peer"),
                             ["10.77.1.11:29101", "10.77.1.12:29101", "10.77.1.13:29101"], edge)
            self.assertEqual({value_of(args[edge], "--relay-primary-id"),
                              value_of(args[edge], "--relay-secondary-id")},
                             {"lon-dist-a", "lon-dist-b"}, edge)
        _program, _plan, args = self.render(["edge_fast_plane=false"])
        for edge in lab.edges("london"):
            self.assertNotIn("--relay-controlled-local", args[edge])

    def test_the_render_looks_at_the_flags_of_the_binaries_it_deploys(self):
        """Wave 7 review: an av-contrib older than --audio-epoch-mesh-lane
        keeps its AEP1 targets; a contributor that sends RLC1 in front of an
        av-mesh that cannot read it is rendered --relay-compact-audio false."""
        with tempfile.TemporaryDirectory() as tmp:
            def binary(name: str, flags: list[str]) -> pathlib.Path:
                path = pathlib.Path(tmp) / name
                path.write_text("#!/bin/sh\ncat <<'EOF'\nOptions:\n"
                                + "".join(f"      {flag} <X>\n" for flag in flags)
                                + "EOF\n")
                path.chmod(0o755)
                return path
            old_contrib = binary("old-contrib", ["--relay-exclusive"])
            new_contrib = binary("new-contrib", ["--relay-exclusive", "--audio-epoch-mesh-lane",
                                                 "--relay-compact-audio"])
            old_mesh = binary("old-mesh", ["--client-udp-bind"])
            new_mesh = binary("new-mesh", ["--client-udp-bind",
                                           "--client-udp-compact-short-header"])
            try:
                deploy.RENDER_BINARIES.update({"av-contrib": old_contrib, "av-mesh": old_mesh})
                contrib = self.render()[2]["contrib-lon"]
                self.assertEqual(value_of(contrib, "--audio-epoch-ingress-target"),
                                 "10.77.1.11:22001")
                self.assertNotIn("--relay-compact-audio", contrib)
                deploy.RENDER_BINARIES.update({"av-contrib": new_contrib})
                contrib = self.render()[2]["contrib-lon"]
                self.assertNotIn("--audio-epoch-ingress-target", contrib)
                self.assertEqual(value_of(contrib, "--relay-compact-audio"), "false")
                contrib = self.render(["relay_compact_audio=true"])[2]["contrib-lon"]
                self.assertEqual(value_of(contrib, "--relay-compact-audio"), "true")
                deploy.RENDER_BINARIES.update({"av-mesh": new_mesh})
                contrib = self.render()[2]["contrib-lon"]
                self.assertNotIn("--relay-compact-audio", contrib)
                self.assertNotIn("--audio-epoch-ingress-target", contrib)
            finally:
                deploy.RENDER_BINARIES.clear()

    def test_wave7_settings_render_only_when_given(self):
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            for flag in ("--relay-audio-copies", "--cache-mesh-fast-plane-push",
                         "--relay-demand-renditions", "--relay-compact-audio",
                         "--client-udp-compact-short-header"):
                self.assertNotIn(flag, node_args, name)
        _program, _plan, args = self.render([
            'relay_audio_copies="policy"', 'cache_mesh_fast_plane_push="all"',
            'relay_demand_renditions=""'])
        self.assertEqual(value_of(args["contrib-lon"], "--relay-audio-copies"), "policy")
        self.assertEqual(value_of(args["contrib-lon"], "--relay-demand-renditions"), "")
        self.assertEqual(value_of(args["lon-dist-a"], "--cache-mesh-fast-plane-push"), "all")
        self.assertEqual(value_of(args["lon-dist-a"], "--relay-demand-renditions"), "")
        self.assertNotIn("--cache-mesh-fast-plane-push", args["lon-edge-1"])
        _program, _plan, args = self.render([
            "relay_compact_audio=false", "client_udp_compact_short_header=false"])
        self.assertEqual(value_of(args["contrib-lon"], "--relay-compact-audio"), "false")
        self.assertEqual(
            value_of(args["lon-edge-1"], "--client-udp-compact-short-header"), "false")
        # Every node that serves UDP clients takes it, distributors too.
        self.assertEqual(
            value_of(args["lon-dist-a"], "--client-udp-compact-short-header"), "false")
        self.assertNotIn("--relay-compact-audio", args["lon-dist-a"])
        _program, _plan, args = self.render(["daw_hls_base_stream_id=5001",
                                             'relay_demand_renditions="auto"'])
        self.assertEqual(value_of(args["contrib-lon"], "--relay-demand-renditions"),
                         "5001,6001,7001")
        self.assertEqual(value_of(args["new-dist-a"], "--relay-demand-renditions"),
                         "5001,6001,7001")

    def test_a_slot_forwarder_renders_the_wave7_settings_like_the_others(self):
        """Wave 7 review C5: the forwarder slot lon-dist-c (a lease child of
        lon-dist-a/b, rendered on its own path) takes every wave 7 setting
        that the other forwarders take, with the same value, and none when
        none is given."""
        wave7 = ("--relay-audio-copies", "--cache-mesh-fast-plane-push",
                 "--relay-demand-renditions", "--relay-compact-audio",
                 "--client-udp-compact-short-header")
        _program, _plan, args = self.render()
        self.assertIn("lon-dist-c", args)
        for flag in wave7:
            self.assertNotIn(flag, args["lon-dist-c"], flag)
        _program, _plan, args = self.render([
            'relay_audio_copies="policy"', 'cache_mesh_fast_plane_push="secondary"',
            'relay_demand_renditions="auto"', "relay_compact_audio=false",
            "client_udp_compact_short_header=false"])
        given = {flag: value_of(args["lon-dist-a"], flag) for flag in wave7
                 if flag in args["lon-dist-a"]}
        self.assertEqual(sorted(given), ["--cache-mesh-fast-plane-push",
                                         "--client-udp-compact-short-header",
                                         "--relay-demand-renditions"])
        for name in ("lon-dist-b", "lon-dist-c", "new-dist-a"):
            self.assertEqual({flag: value_of(args[name], flag) for flag in wave7
                              if flag in args[name]}, given, name)
            for flag in given:
                self.assertEqual(args[name].count(flag), 1, (name, flag))

    def test_the_origin_has_two_children_both_first_forwarders_of_its_region(self):
        program, plan, _args = self.render()
        self.assertEqual(program["forwarding"], "merged")
        nodes = {node["node_id"]: node for node in program["topology"]["nodes"]}
        children = [link["child_node_id"] for link in program["topology"]["parent_links"]
                    if link["parent_node_id"] == "contrib"]
        self.assertEqual(sorted(children), ["lon-dist-a", "lon-dist-b"])
        for child in children:
            self.assertEqual(nodes[child]["level"], 1)
            self.assertEqual(nodes[child]["role"], "backbone")
            self.assertEqual(nodes[child]["failure_domain"]["region"], "gb-lon")
        self.assertEqual(program["topology"]["limits"]["max_origin_children"], 2)
        self.assertNotIn("relay-primary", nodes)
        self.assertNotIn("relay-secondary", nodes)
        for remote in ("new-dist-a", "new-dist-b"):
            self.assertEqual(nodes[remote]["role"], "regional_relay")
            self.assertEqual(nodes[remote]["level"], 2)
        services = {service["node_id"] for service in plan["services"]}
        self.assertEqual(services, {"contrib", "lon-dist-a", "lon-dist-b", "new-dist-a",
                                    "new-dist-b"})

    def test_no_direct_target_and_no_backbone_is_rendered(self):
        _program, _plan, args = self.render()
        self.assertNotIn("relay-primary", args)
        self.assertNotIn("relay-secondary", args)
        for name, node_args in args.items():
            self.assertNotIn("--relay-direct-target", node_args, name)
            self.assertNotIn("--relay-direct-peer", node_args, name)

    def test_each_remote_forwarder_has_two_distinct_parents_crossed(self):
        _program, _plan, args = self.render()
        for name, primary, secondary, controller in (
                ("new-dist-a", "lon-dist-a", "lon-dist-b", "10.90.2.11:22501=10.90.0.12:22502"),
                ("new-dist-b", "lon-dist-b", "lon-dist-a", "10.90.2.12:22511=10.90.0.11:22512")):
            node_args = args[name]
            self.assertEqual(value_of(node_args, "--relay-primary-id"), primary, name)
            self.assertEqual(value_of(node_args, "--relay-secondary-id"), secondary, name)
            self.assertIn("--relay-secondary-promoted", node_args)
            self.assertEqual(value_of(node_args, "--relay-failover-controller"), controller)
        # Each first forwarder: the source lane to one, repair to the other.
        self.assertEqual(values_of(args["lon-dist-a"], "--relay-forward"),
                         ["10.90.0.11:22401=10.90.2.11:22200,source",
                          "10.90.0.11:22412=10.90.2.12:22211,repair"])
        self.assertEqual(values_of(args["lon-dist-a"], "--relay-failover-listener"),
                         ["10.90.0.11:22512=10.90.2.12:22511,10.90.2.12:22211"])
        self.assertEqual(values_of(args["lon-dist-b"], "--relay-forward"),
                         ["10.90.0.12:22402=10.90.2.11:22201,repair",
                          "10.90.0.12:22411=10.90.2.12:22210,source"])

    def test_the_shared_parent_region_is_a_declared_gap(self):
        program, plan, _args = self.render()
        self.assertEqual(program["shared_domain_parents"], ["new-dist-a", "new-dist-b"])
        self.assertEqual(plan["purpose"], "single_provider_qualification")
        self.assertIn("shared_parent_failure_domain:new-dist-a", plan["production_readiness_gaps"])
        self.assertIn("shared_parent_failure_domain:new-dist-b", plan["production_readiness_gaps"])
        # Without the declaration the compiler reports the overlap.
        program = dict(program, shared_domain_parents=[])
        with self.assertRaises(SystemExit) as caught:
            deploy.compile_plan(program)
        self.assertIn("parent_failure_domain_overlap", str(caught.exception))

    def test_class_ports_do_not_collide(self):
        _program, _plan, args = self.render()
        plan = deploy.class_plan(self.settings())
        for name, node_args in args.items():
            ports = [port for _socket, port, _arg in deploy.udp_binds(node_args, plan)]
            # --fec-bind and --relay-primary-bind are one socket.
            sockets = {(socket, port) for socket, port, _arg in deploy.udp_binds(node_args, plan)}
            self.assertEqual(len({port for _s, port in sockets}), len(sockets), name)
            self.assertTrue(all(port <= 65535 for port in ports), name)
        for name in ("lon-dist-a", "new-dist-b"):
            self.assertEqual(value_of(args[name], "--relay-class-threads"), "true", name)
        ports = {port for _s, port, _a in deploy.udp_binds(args["lon-dist-a"], plan)}
        self.assertTrue({22001, 23001, 29101, 30101} <= ports, ports)

    def test_everything_but_the_relay_arguments_renders_as_the_legacy_topology(self):
        """The render dry run diffed against the legacy render."""
        _p, _q, merged = self.render()
        _p, _q, legacy = self.render(["topology=legacy"])
        self.assertEqual(set(legacy) - set(merged), {"relay-primary", "relay-secondary"})
        self.assertEqual(set(merged) - set(legacy), set())
        for name in merged:
            self.assertEqual(without_relay_args(merged[name]), without_relay_args(legacy[name]),
                             name)
        # Edges render identically: they are not DAG members in either.
        for edge in lab.edges("london") + lab.edges("newyork"):
            self.assertEqual(merged[edge], legacy[edge], edge)

    def test_an_extra_argument_that_breaks_the_shape_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.render(['extra_contributor_args=["--relay-direct-target","10.77.1.21:22001"]'])
        self.assertIn("--relay-direct-target", str(caught.exception))

    def test_every_cache_mesh_peer_is_listed_back_and_no_removed_flag_renders(self):
        # The merged playlists mesh takes datagrams only from a listed peer,
        # class socket included: every distributor lists its site's edges
        # and every edge its site's distributors.
        _program, _plan, args = self.render()
        binds = {name: value_of(item, "--mesh-bind") for name, item in args.items()}
        for name, item in args.items():
            self.assertFalse({"--mesh-sync-on-publish", "--mesh-liveness-on-receipt"}
                             & deploy.flag_names(item), name)
            for peer in values_of(item, "--peer"):
                other = next(other for other, bind in binds.items() if bind == peer)
                self.assertIn(binds[name], values_of(args[other], "--peer"), (name, other))
        edges = sorted(binds[edge] for edge in lab.edges("london"))
        distributors = sorted({peer for edge in lab.edges("london")
                               for peer in values_of(args[edge], "--peer")})
        # lon-dist-a, lon-dist-b and the forwarder slot lon-dist-c (the
        # scale work: a slot is in every peer list from the first push).
        self.assertEqual(distributors,
                         ["10.77.1.11:29101", "10.77.1.12:29101", "10.77.1.13:29101"])
        for name, bind in binds.items():
            if bind in distributors:
                self.assertEqual(sorted(values_of(args[name], "--peer")), edges, name)
        with self.assertRaises(ValueError) as caught:
            self.render(['extra_edge_args=["--mesh-sync-on-publish","false"]'])
        self.assertIn("--mesh-sync-on-publish", str(caught.exception))
        stranger = binds[lab.edges("newyork")[0]]
        with self.assertRaises(ValueError) as caught:
            self.render([f'extra_edge_args=["--peer","{stranger}"]'])
        self.assertIn("does not list", str(caught.exception))

    def test_the_merged_lab_topology_file(self):
        topology = json.loads(MERGED_TOPOLOGY.read_text())
        # Dedicated plans only, no backbone relay, two London distributors.
        self.assertTrue(all(node["type"].startswith("g6-dedicated-") for node in topology["nodes"]))
        self.assertFalse([node for node in topology["nodes"] if node["role"] == "backbone"])
        state = topology_state(MERGED_TOPOLOGY)
        _program, plan, args = self.render(state=state)
        self.assertEqual(value_of(args["contrib-lon"], "--relay-primary-id"), "lon-dist-a")
        self.assertEqual(value_of(args["contrib-lon"], "--relay-secondary-id"), "lon-dist-b")
        # Three regions of two forwarders each, crossed under London.
        remote = [name for name, item in state["nodes"].items()
                  if item["role"] == "distributor" and item["site"] != "london"]
        self.assertEqual(len(remote), 6)
        for name in remote:
            parents = {value_of(args[name], "--relay-primary-id"),
                       value_of(args[name], "--relay-secondary-id")}
            self.assertEqual(parents, {"lon-dist-a", "lon-dist-b"}, name)
        primaries = sorted(value_of(args[name], "--relay-primary-id") for name in remote)
        self.assertEqual(primaries, ["lon-dist-a"] * 3 + ["lon-dist-b"] * 3)
        self.assertEqual(len([gap for gap in plan["production_readiness_gaps"]
                              if gap.startswith("shared_parent_failure_domain:")]), 6)


class Lifecycle(Base):
    def test_push_and_restart_stop_an_unused_backbone(self):
        sent = []
        node = {**lab.node("relay-primary"), "type": "g6-dedicated-2"}
        with mock.patch.object(deploy.common, "rsync_to", lambda *a, **k: None), \
                mock.patch.object(deploy.common, "ssh_text",
                                  lambda node, script, **k: sent.append(script) or ""):
            deploy.install_node(node, {}, self.settings(), False)
        self.assertIn("systemctl disable --now needletail-mesh.service", sent[-1])
        # A rendered node is installed and restarted as before.
        sent.clear()
        with mock.patch.object(deploy.common, "rsync_to", lambda *a, **k: None), \
                mock.patch.object(deploy.common, "ssh_text",
                                  lambda node, script, **k: sent.append(script) or ""), \
                mock.patch.object(deploy, "apply_host_qdisc", lambda *a, **k: ""):
            deploy.install_node(node, {"relay-primary": {"service": "needletail-mesh",
                                                         "rust_log": "info"}},
                                self.settings(["topology=legacy"]), False)
        self.assertNotIn("disable --now", sent[-1])
        self.assertIn("systemctl restart needletail-mesh.service", sent[-1])

    def test_the_lab_starts_no_service_for_an_unused_backbone(self):
        with mock.patch.object(lab, "load", lambda name, default=None: {}):
            with self.assertRaises(lab.LabError) as caught:
                lab.start_service("relay-primary")
        self.assertIn("topology=legacy", str(caught.exception))



class CompilerAge(unittest.TestCase):
    """A compiler binary older than its sources is refused by name, not
    left to reject today's program by old rules."""

    def test_a_compiler_older_than_a_source_is_named(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            source = root / "src" / "relay_topology.rs"
            source.write_text("// rules\n")
            compiler = root / "needletail-compile"
            compiler.write_text("")
            os.utime(compiler, (1_000, 1_000))
            os.utime(source, (2_000, 2_000))
            saved = deploy.common.ROOT
            deploy.common.ROOT = root
            try:
                self.assertEqual(deploy.compiler_stale_by(compiler), "src/relay_topology.rs")
                os.utime(compiler, (3_000, 3_000))
                self.assertIsNone(deploy.compiler_stale_by(compiler))
            finally:
                deploy.common.ROOT = saved

if __name__ == "__main__":
    unittest.main()
