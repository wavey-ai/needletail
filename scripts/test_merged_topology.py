#!/usr/bin/env python3
"""Tests of the merged topology (low-latency item 4) in the deploy tool and
the lab: relay and distributor are one forwarder. The contributor feeds the
two distributors of its own site and nothing else (no direct lane to an
edge, no --relay-direct-target), every other distributor has both of them as
parents, crossed, the backbone relays get no service, the plan records the
shared parent region as a declared gap, the class ports do not collide, and
everything but the relay arguments renders as the legacy topology does.
With a standby contributor (resilient mesh, workstream A): two contributors
by the same two first forwarders, each forwarder taking the standby on its
secondary lane, both contributors rendered alike, and no other node changed.
python3 -m unittest scripts/test_merged_topology.py"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
import sys
from contextlib import redirect_stderr, redirect_stdout
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
        # The lab's state directory too: a running lab (`up slots=lon-edge-4`)
        # holds lon-edge-4 out of edges() and the peer lists, and the render
        # tests are of the default lab.
        for target, name, value in ((deploy, "SETTINGS_FILE", self.dir / "settings.json"),
                                    (deploy, "HUNT_FILE", self.dir / "ecmp-ports.json"),
                                    (deploy.common, "ARTIFACT_DIR", self.dir),
                                    (lab, "WORK", self.dir / "edge-lab")):
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
        # The contributor alone (the lab's standby has its own tests below).
        _program, _plan, args = self.render(state=one_contributor_state())
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
        _program, _plan, lane = self.render(["audio_epoch_lane=true"],
                                            state=one_contributor_state())
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

    def test_the_video_push_setting_reaches_every_forwarder_only_when_given(self):
        """Wave 8 stage 3: cache_mesh_fast_plane_video_push renders
        --cache-mesh-fast-plane-video-push on every forwarder (the slot
        included) and on nothing else, only when given."""
        flag = "--cache-mesh-fast-plane-video-push"
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            self.assertNotIn(flag, node_args, name)
        _program, _plan, args = self.render(['cache_mesh_fast_plane_video_push="all"'])
        for name in ("lon-dist-a", "lon-dist-b", "lon-dist-c", "new-dist-a"):
            self.assertEqual(value_of(args[name], flag), "all", name)
            self.assertEqual(args[name].count(flag), 1, name)
        for name in ("lon-edge-1", "contrib-lon"):
            self.assertNotIn(flag, args[name], name)

    def test_the_promotion_backoff_setting_reaches_the_failover_controllers(self):
        """Wave 8 stage 3 (S6): relay_failover_promotion_backoff renders
        --relay-failover-promotion-backoff on every node with a failover
        controller (the edges), only when given."""
        flag = "--relay-failover-promotion-backoff"
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            self.assertNotIn(flag, node_args, name)
        _program, _plan, args = self.render(["relay_failover_promotion_backoff=false"])
        controllers = [name for name, node_args in args.items()
                       if "--relay-failover-controller" in node_args]
        self.assertIn("lon-edge-1", controllers)
        for name, node_args in args.items():
            if name in controllers:
                self.assertEqual(value_of(node_args, flag), "false", name)
            else:
                self.assertNotIn(flag, node_args, name)

    def test_the_phase_am_mesh_settings_reach_every_mesh_node(self):
        """Wave 8 stage 5 (Phase AM 12, 13): each setting renders its av-mesh
        flag on every node with a cache mesh, only when given."""
        flags = ("--relay-lease-ranked", "--relay-ranked-parents",
                 "--cache-mesh-subscribe-rank", "--cache-mesh-release-unranked-parents",
                 "--cache-mesh-avoid-draining-parents", "--cache-mesh-warm-total-kbps")
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            for flag in flags:
                self.assertNotIn(flag, node_args, name)
        _program, _plan, args = self.render([
            "relay_lease_ranked=false", "relay_ranked_parents=false",
            "cache_mesh_subscribe_rank=false", "cache_mesh_release_unranked_parents=false",
            "cache_mesh_avoid_draining_parents=false", "cache_mesh_warm_total_kbps=0"])
        meshed = 0
        for name, node_args in args.items():
            if "--mesh-bind" not in node_args:
                for flag in flags:
                    self.assertNotIn(flag, node_args, name)
                continue
            meshed += 1
            for flag in flags[:-1]:
                self.assertEqual(value_of(node_args, flag), "false", (name, flag))
            self.assertEqual(value_of(node_args, "--cache-mesh-warm-total-kbps"), "0")
        self.assertGreater(meshed, 0)
        _program, _plan, args = self.render(["cache_mesh_warm_total_kbps=60000"])
        self.assertEqual(value_of(args["lon-dist-a"], "--cache-mesh-warm-total-kbps"), "60000")

    def test_the_review_settings_reach_their_nodes_only_when_given(self):
        """Wave 8 review fixes: keepalive, datagram capacity and the region
        head check on the nodes that serve UDP clients; the second parent's
        warm-up on every mesh node; RLD1 to lease parents and the fallback
        nice on relay nodes; the lease sockets' drain on forwarders."""
        flags = {"--client-udp-keepalive-ms": "--client-udp-bind",
                 "--client-udp-datagram-capacity": "--client-udp-bind",
                 "--client-udp-region-head-check": "--client-udp-bind",
                 "--cache-mesh-warm-second-parent": "--mesh-bind",
                 "--relay-demand-report-lease-parents": "--relay-controlled-local",
                 "--relay-class-fallback-nice": "--relay-controlled-local",
                 "--relay-lease-drain-inbound": "--relay-lease-bind",
                 "--assignment-udp-in-region": "--mesh-bind"}
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            for flag in flags:
                self.assertNotIn(flag, node_args, name)
        _program, _plan, args = self.render([
            "client_udp_keepalive_ms=0", "client_udp_datagram_capacity=400000",
            "client_udp_region_head_check=false", "cache_mesh_warm_second_parent=false",
            "relay_demand_report_lease_parents=true", "relay_class_fallback_nice=5",
            "relay_lease_drain_inbound=false", "assignment_udp_in_region=false"])
        want = {"--assignment-udp-in-region": "false","--client-udp-keepalive-ms": "0", "--client-udp-datagram-capacity": "400000",
                "--client-udp-region-head-check": "false",
                "--cache-mesh-warm-second-parent": "false",
                "--relay-demand-report-lease-parents": "true",
                "--relay-class-fallback-nice": "5", "--relay-lease-drain-inbound": "false"}
        seen = {flag: 0 for flag in flags}
        for name, node_args in args.items():
            for flag, where in flags.items():
                if where in node_args:
                    self.assertEqual(value_of(node_args, flag), want[flag], (name, flag))
                    seen[flag] += 1
                else:
                    self.assertNotIn(flag, node_args, (name, flag))
        for flag, count in seen.items():
            self.assertGreater(count, 0, flag)
        self.assertIn("--client-udp-datagram-capacity", args["lon-edge-1"])
        self.assertIn("--relay-lease-drain-inbound", args["lon-dist-a"])

    def test_the_phase_am_client_udp_settings_reach_the_nodes_that_serve_clients(self):
        """Wave 8 stage 4 (Phase AM 8, 9, 11): each setting renders its av-mesh
        flag on every node with --client-udp-bind, only when given."""
        flags = ("--client-udp-egress-load", "--client-udp-admit-by-load",
                 "--client-udp-drain-spread-ms", "--client-udp-region-hold-ms")
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            for flag in flags:
                self.assertNotIn(flag, node_args, name)
        _program, _plan, args = self.render([
            "client_udp_egress_load=false", "client_udp_admit_by_load=false",
            "client_udp_drain_spread_ms=0", "client_udp_region_hold_ms=5000"])
        served = 0
        for name, node_args in args.items():
            if "--client-udp-bind" not in node_args:
                for flag in flags:
                    self.assertNotIn(flag, node_args, name)
                continue
            served += 1
            self.assertEqual(value_of(node_args, "--client-udp-egress-load"), "false")
            self.assertEqual(value_of(node_args, "--client-udp-admit-by-load"), "false")
            self.assertEqual(value_of(node_args, "--client-udp-drain-spread-ms"), "0")
            self.assertEqual(value_of(node_args, "--client-udp-region-hold-ms"), "5000")
        self.assertGreater(served, 0)

    def test_the_wave8_client_udp_settings_reach_the_nodes_that_serve_clients(self):
        """Wave 8 stage 2: each setting renders its av-mesh flag on every node
        with --client-udp-bind, only when given."""
        flags = ("--client-udp-repair-thread", "--client-udp-batched-admission",
                 "--client-udp-resume-from-request", "--client-udp-fanout-inline-shard")
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            for flag in flags:
                self.assertNotIn(flag, node_args, name)
        _program, _plan, args = self.render([
            "client_udp_repair_thread=false", "client_udp_batched_admission=false",
            "client_udp_resume_from_request=false", "client_udp_fanout_inline_shard=true"])
        for name, node_args in args.items():
            if "--client-udp-bind" not in node_args:
                for flag in flags:
                    self.assertNotIn(flag, node_args, name)
                continue
            self.assertEqual(value_of(node_args, "--client-udp-repair-thread"), "false", name)
            self.assertEqual(value_of(node_args, "--client-udp-batched-admission"), "false")
            self.assertEqual(value_of(node_args, "--client-udp-resume-from-request"), "false")
            self.assertEqual(value_of(node_args, "--client-udp-fanout-inline-shard"), "true")
        self.assertIn("--client-udp-repair-thread", args["lon-edge-1"])

    def test_the_wave9_cold_start_settings_reach_their_nodes_only_when_given(self):
        """Wave 9 (cold start): malloc_tune and prefault render on every
        mesh node, client_udp_prompt_sender on the nodes that serve UDP
        clients, each only when given. --mlock-all is gone (D 2.3)."""
        mesh = ("--malloc-tune", "--prefault")
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            for flag in mesh + ("--client-udp-prompt-sender",):
                self.assertNotIn(flag, node_args, name)
        _program, _plan, args = self.render([
            "malloc_tune=false", "prefault=false",
            "client_udp_prompt_sender=false"])
        for name, node_args in args.items():
            self.assertNotIn("--mlock-all", node_args, name)
            if "--mesh-bind" in node_args:
                for flag in mesh:
                    self.assertEqual(value_of(node_args, flag), "false", (name, flag))
            if "--client-udp-bind" in node_args:
                self.assertEqual(value_of(node_args, "--client-udp-prompt-sender"), "false", name)
            else:
                self.assertNotIn("--client-udp-prompt-sender", node_args, name)
        self.assertEqual(value_of(args["lon-edge-1"], "--client-udp-prompt-sender"), "false")
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            self.assertNotIn("--relay-lease-descriptors", node_args, name)
        _program, _plan, args = self.render(["relay_lease_descriptors=false"])
        for name, node_args in args.items():
            if "--relay-lease-bind" in node_args:
                self.assertEqual(value_of(node_args, "--relay-lease-descriptors"), "false", name)
            else:
                self.assertNotIn("--relay-lease-descriptors", node_args, name)
        self.assertIn("--relay-lease-descriptors", args["lon-dist-a"])

    def test_the_wave9_udp_assignment_setting_reaches_client_udp_nodes_only_when_given(self):
        """Wave 9 B: client_udp_assign renders --client-udp-assign on the
        nodes that serve UDP clients (where needletail-udp-client asks its
        entry point over UDP), only when given."""
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            self.assertNotIn("--client-udp-assign", node_args, name)
        _program, _plan, args = self.render(["client_udp_assign=false"])
        for name, node_args in args.items():
            if "--client-udp-bind" in node_args:
                self.assertEqual(value_of(node_args, "--client-udp-assign"), "false", name)
            else:
                self.assertNotIn("--client-udp-assign", node_args, name)
        # The entry points (each region's first distributor) serve UDP
        # clients: their client UDP port answers ASSIGN.
        self.assertIn("--client-udp-bind", args["lon-dist-a"])

    def test_the_wave9_4k_settings_reach_the_class_threads(self):
        """Wave 9 D: every mesh node takes parts of up to 8 MiB (slot_kb
        8192: the first frame of a 4K encode at 160-300 Mbit/s is 4.6-5.9 MB),
        and relay_video_gro renders --relay-video-gro on the nodes with class
        threads, only when given."""
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            if "--slot-kb" in node_args:
                self.assertEqual(value_of(node_args, "--slot-kb"), "8192", name)
            self.assertNotIn("--relay-video-gro", node_args, name)
        _program, _plan, args = self.render(["relay_video_gro=false", "slot_kb=2048"])
        seen = 0
        for name, node_args in args.items():
            if "--slot-kb" in node_args:
                self.assertEqual(value_of(node_args, "--slot-kb"), "2048", name)
            if value_of(node_args, "--relay-class-threads") == "true":
                self.assertEqual(value_of(node_args, "--relay-video-gro"), "false", name)
                seen += 1
            else:
                self.assertNotIn("--relay-video-gro", node_args, name)
        self.assertGreater(seen, 0)
        for given, rendered in (("window", "window"), ("4", "4")):
            _program, _plan, args = self.render([f"cache_mesh_video_warm_parts={given}"])
            for name, node_args in args.items():
                if "--mesh-bind" in node_args:
                    self.assertEqual(value_of(node_args, "--cache-mesh-video-warm-parts"),
                                     rendered, name)
                else:
                    self.assertNotIn("--cache-mesh-video-warm-parts", node_args, name)
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            self.assertNotIn("--cache-mesh-video-warm-parts", node_args, name)

    def test_the_wave9_scale_settings_reach_their_nodes_only_when_given(self):
        """Wave 9 C: cpu_load, cpu_load_full_percent, assignment_move_headroom
        and rebalance_target_permille render on every mesh node, and
        client_udp_rebalance with its margin and interval on the nodes that
        serve UDP clients, only when given."""
        flags = ("--cpu-load", "--cpu-load-full-percent", "--assignment-move-headroom",
                 "--rebalance-target-permille", "--client-udp-rebalance",
                 "--client-udp-rebalance-margin-permille",
                 "--client-udp-rebalance-interval-ms")
        _program, _plan, args = self.render()
        for name, node_args in args.items():
            for flag in flags:
                self.assertNotIn(flag, node_args, (name, flag))
        _program, _plan, args = self.render([
            "cpu_load=false", "cpu_load_full_percent=70", "assignment_move_headroom=false",
            "rebalance_target_permille=750", "client_udp_rebalance=false",
            "client_udp_rebalance_margin_permille=100",
            "client_udp_rebalance_interval_ms=2000"])
        seen_udp = False
        for name, node_args in args.items():
            if "--mesh-bind" in node_args:
                self.assertEqual(value_of(node_args, "--cpu-load"), "false", name)
                self.assertEqual(value_of(node_args, "--cpu-load-full-percent"), "70", name)
                self.assertEqual(value_of(node_args, "--assignment-move-headroom"), "false", name)
                self.assertEqual(value_of(node_args, "--rebalance-target-permille"), "750", name)
            if "--client-udp-bind" in node_args:
                seen_udp = True
                self.assertEqual(value_of(node_args, "--client-udp-rebalance"), "false", name)
                self.assertEqual(
                    value_of(node_args, "--client-udp-rebalance-margin-permille"), "100", name)
                self.assertEqual(
                    value_of(node_args, "--client-udp-rebalance-interval-ms"), "2000", name)
            else:
                self.assertNotIn("--client-udp-rebalance", node_args, name)
        self.assertTrue(seen_udp)

    def test_the_two_path_settings_reach_their_nodes_when_the_binary_has_the_flags(self):
        """Workstream B W1: the move grace, the confirmations per source and
        the rebalance to the mean render on the nodes that serve UDP clients,
        the two-path bit rate on every node of the cache mesh, each with its
        default, and only for an av-mesh that has the flag."""
        udp = {"--client-udp-move-grace-ms": "10000",
               "--client-udp-assign-confirms-per-source": "64",
               "--client-udp-rebalance-to-mean": "true"}
        mesh = {"--assignment-two-path-max-bps": "8000000"}
        for key, value in (("client_udp_move_grace_ms", 10000),
                           ("client_udp_assign_confirms_per_source", 64),
                           ("client_udp_rebalance_to_mean", True),
                           ("assignment_two_path_max_bps", 8000000)):
            self.assertEqual(deploy.DEFAULT_SETTINGS[key], value, key)

        def check(args: dict, udp_want: dict, mesh_want: dict) -> None:
            served = meshed = 0
            for name, node_args in args.items():
                for flags, where, want in ((udp, "--client-udp-bind", udp_want),
                                           (mesh, "--mesh-bind", mesh_want)):
                    for flag in flags:
                        if where in node_args and flag in want:
                            self.assertEqual(value_of(node_args, flag), want[flag], (name, flag))
                            self.assertEqual(node_args.count(flag), 1, (name, flag))
                        else:
                            self.assertNotIn(flag, node_args, (name, flag))
                served += "--client-udp-bind" in node_args
                meshed += "--mesh-bind" in node_args
            self.assertGreater(served, 8)
            self.assertGreater(meshed, 8)
            self.assertNotIn("--mesh-bind", args["contrib-lon"])

        # A render that cannot see the binary, and one for an av-mesh
        # without the flags: nothing, whatever the settings say.
        check(self.render()[2], {}, {})
        old_mesh = fake_binary(self.dir, "old-mesh", ["--client-udp-bind", "--mesh-bind"])
        new_mesh = fake_binary(self.dir, "new-mesh", ["--client-udp-bind", *udp, *mesh])
        part_mesh = fake_binary(self.dir, "part-mesh", [
            "--client-udp-move-grace-ms", "--assignment-two-path-max-bps"])
        try:
            deploy.RENDER_BINARIES.update({"av-mesh": old_mesh})
            check(self.render(["client_udp_assign_confirms_per_source=4096"])[2], {}, {})
            deploy.RENDER_BINARIES.update({"av-mesh": new_mesh})
            _program, _plan, args = self.render()
            check(args, udp, mesh)
            # Edges, distributors and the forwarder slot serve UDP clients.
            for name in ("lon-edge-1", "lon-dist-a", "lon-dist-c", "new-edge-2"):
                self.assertIn("--client-udp-move-grace-ms", args[name], name)
            _program, _plan, args = self.render([
                "client_udp_move_grace_ms=0", "client_udp_assign_confirms_per_source=4096",
                "client_udp_rebalance_to_mean=false", "assignment_two_path_max_bps=3000000"])
            check(args, {"--client-udp-move-grace-ms": "0",
                         "--client-udp-assign-confirms-per-source": "4096",
                         "--client-udp-rebalance-to-mean": "false"},
                  {"--assignment-two-path-max-bps": "3000000"})
            # null renders nothing for that setting: the binary's default.
            _program, _plan, args = self.render(["client_udp_move_grace_ms=null",
                                                 "assignment_two_path_max_bps=null"])
            check(args, {key: value for key, value in udp.items()
                         if key != "--client-udp-move-grace-ms"}, {})
            # An extra argument of a role is how one run overrides it.
            _program, _plan, args = self.render([
                'extra_edge_args=["--client-udp-assign-confirms-per-source","128"]'])
            self.assertEqual(
                args["lon-edge-1"].count("--client-udp-assign-confirms-per-source"), 1)
            self.assertEqual(
                value_of(args["lon-edge-1"], "--client-udp-assign-confirms-per-source"), "128")
            self.assertEqual(
                value_of(args["lon-dist-a"], "--client-udp-assign-confirms-per-source"), "64")
            for bad in ("client_udp_assign_confirms_per_source=0",
                        "assignment_two_path_max_bps=0", "client_udp_move_grace_ms=soon"):
                with self.assertRaises(ValueError, msg=bad):
                    self.render([bad])
            # An av-mesh with some of the flags gets those.
            deploy.RENDER_BINARIES.update({"av-mesh": part_mesh})
            check(self.render()[2], {"--client-udp-move-grace-ms": "10000"}, mesh)
        finally:
            deploy.RENDER_BINARIES.clear()

    def test_the_audio_fan_out_s_gso_is_a_setting(self):
        """The stall hunt's GSO/GRO matrix: client_udp_gso renders av-mesh
        --client-udp-gso on the nodes that serve UDP clients, only when it
        is given (null: the binary's default) and the binary has the flag."""
        self.assertIsNone(deploy.DEFAULT_SETTINGS["client_udp_gso"])
        new_mesh = fake_binary(self.dir, "gso-mesh", ["--client-udp-bind", "--mesh-bind",
                                                      "--client-udp-gso"])
        old_mesh = fake_binary(self.dir, "nogso-mesh", ["--client-udp-bind", "--mesh-bind"])
        try:
            deploy.RENDER_BINARIES.update({"av-mesh": new_mesh})
            args = self.render()[2]
            self.assertFalse(any("--client-udp-gso" in items for items in args.values()))
            for value in ("false", "true"):
                args = self.render([f"client_udp_gso={value}"])[2]
                for name, items in args.items():
                    if "--client-udp-bind" in items:
                        self.assertEqual(value_of(items, "--client-udp-gso"), value, name)
                    else:
                        self.assertNotIn("--client-udp-gso", items, name)
            deploy.RENDER_BINARIES.update({"av-mesh": old_mesh})
            args = self.render(["client_udp_gso=false"])[2]
            self.assertFalse(any("--client-udp-gso" in items for items in args.values()))
        finally:
            deploy.RENDER_BINARIES.clear()

    def test_a_setting_the_binary_cannot_take_is_named_on_stderr(self):
        """Review of B W1: a setting not at its default whose flag the
        av-mesh of the render lacks is named once on stderr with why, and the
        render is unchanged; a setting at its default, or one an extra
        argument gives, is not named."""
        def render(overrides) -> tuple[str, dict]:
            err = io.StringIO()
            with redirect_stderr(err):
                args = self.render(overrides)[2]
            return err.getvalue(), args

        old_mesh = fake_binary(self.dir, "old-mesh", ["--client-udp-bind", "--mesh-bind"])
        new_mesh = fake_binary(self.dir, "new-mesh", [
            "--client-udp-assign-confirms-per-source", "--relay-media-deadline-ms"])
        try:
            deploy.RENDER_BINARIES.update({"av-mesh": old_mesh})
            quiet, before = render([])
            self.assertEqual(quiet, "")
            err, args = render(["client_udp_assign_confirms_per_source=4096"])
            # The av-mesh nodes (a contributor's path observations carry the
            # time of the render).
            meshed = lambda rendered: {name: node_args for name, node_args in rendered.items()
                                       if "--mesh-bind" in node_args}
            self.assertEqual(meshed(args), meshed(before))
            self.assertIn("client_udp_assign_confirms_per_source=4096", err)
            # media_deadline_ms reaches the contributors whatever the av-mesh.
            err, args = render(["client_udp_assign_confirms_per_source=4096",
                                "media_deadline_ms=250"])
            self.assertEqual(len(err.splitlines()), 1, err)
            self.assertIn("client_udp_assign_confirms_per_source=4096 (av-mesh at "
                          f"{old_mesh} lacks --client-udp-assign-confirms-per-source)", err)
            self.assertIn(f"media_deadline_ms=250 (av-mesh at {old_mesh} lacks "
                          "--relay-media-deadline-ms)", err)
            self.assertNotIn("move_grace", err)
            err, _args = render([
                "client_udp_assign_confirms_per_source=4096",
                'extra_edge_args=["--client-udp-assign-confirms-per-source","4096"]',
                'extra_distributor_args=["--client-udp-assign-confirms-per-source","4096"]'])
            self.assertEqual(err, "")
            # A binary that cannot be run is said so.
            broken = self.dir / "broken-mesh"
            broken.write_text("#!/bin/sh\nexit 3\n")
            broken.chmod(0o755)
            deploy.RENDER_BINARIES.update({"av-mesh": broken})
            err, _args = render(["client_udp_assign_confirms_per_source=4096"])
            self.assertIn(f"(cannot read {broken} --help)", err)
            deploy.RENDER_BINARIES.update({"av-mesh": new_mesh})
            err, _args = render(["client_udp_assign_confirms_per_source=4096",
                                 "media_deadline_ms=250"])
            self.assertEqual(err, "")
        finally:
            deploy.RENDER_BINARIES.clear()

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
        program, plan, _args = self.render(state=one_contributor_state())
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
        # Each distributor lists its site's edges; the slot lon-dist-c and its
        # lease parents lon-dist-a and lon-dist-b also list each other
        # (forwarder_slot_feeds_edges, AM 12), but never dist-a and dist-b.
        pairs = {"10.77.1.11:29101": ["10.77.1.13:29101"],
                 "10.77.1.12:29101": ["10.77.1.13:29101"],
                 "10.77.1.13:29101": ["10.77.1.11:29101", "10.77.1.12:29101"]}
        for name, bind in binds.items():
            if bind in distributors:
                self.assertEqual(sorted(values_of(args[name], "--peer")),
                                 sorted(edges + pairs[bind]), name)
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


STANDBY = {"name": "contrib-lon-b", "role": "contributor", "site": "london", "region": "gb-lon",
           "public_ipv4": "10.90.0.7", "vlan_ipv4": "10.77.1.7", "type": "local",
           "standby": True}
# The arguments in which two contributors differ: who each is, and its own
# end of each lane (the first forwarders' ports are crossed between them).
OWN_OF_A_CONTRIBUTOR = {"--relay-local-id", "--relay-primary-bind", "--relay-primary-target",
                        "--relay-secondary-bind", "--relay-secondary-target",
                        "--mesh-fec-target", "--mesh-media-fec-target"}


def one_contributor_state() -> dict:
    """The lab with its contributor alone."""
    state = lab.lab_state()
    state["nodes"] = {name: node for name, node in state["nodes"].items()
                      if not node.get("standby")}
    return state


def two_contributor_state() -> dict:
    """The lab with the standby contributor contrib-lon-b on the London
    VLAN beside contrib-lon."""
    state = one_contributor_state()
    state["nodes"][STANDBY["name"]] = dict(STANDBY)
    return state


def fake_binary(directory: pathlib.Path, name: str, flags: list[str], body: str = "") -> pathlib.Path:
    """An executable whose --help lists `flags` and that otherwise runs the
    shell lines of `body` (what `--print-fingerprint` prints)."""
    path = directory / name
    path.write_text("#!/bin/sh\nfor arg in \"$@\"; do\n  if [ \"$arg\" = --help ]; then\n"
                    "    cat <<'EOF'\nOptions:\n"
                    + "".join(f"      {flag} <X>\n" for flag in flags)
                    + "EOF\n    exit 0\n  fi\ndone\n" + body + "\n")
    path.chmod(0o755)
    return path


def flag_values(args: list[str]) -> dict[str, list[str]]:
    """{flag: its values} of an argument list; a switch has none."""
    out: dict[str, list[str]] = {}
    for index, arg in enumerate(args):
        if arg.startswith("--"):
            follows = args[index + 1] if index + 1 < len(args) else "--"
            out.setdefault(arg, []).extend([] if follows.startswith("--") else [follows])
    return out


class Contributors(Base):
    """One contributor, or one and its standby: what the tree, the program
    and the tools take, without the compiler."""

    def test_the_standby_has_an_id_of_its_own(self):
        self.assertEqual(deploy.relay_node_id(lab.node("contrib-lon")), "contrib")
        self.assertEqual(deploy.relay_node_id(STANDBY), "contrib-b")
        self.assertEqual(deploy.relay_node_id(lab.node("lon-dist-a")), "lon-dist-a")

    def test_the_tree_takes_one_contributor_or_one_and_its_standby(self):
        settings = self.settings()
        tree = deploy.relay_tree(one_contributor_state(), settings)
        self.assertEqual(tree["contrib"]["name"], "contrib-lon")
        self.assertIsNone(tree["standby"])
        for overrides in ((), ("topology=legacy",)):
            tree = deploy.relay_tree(two_contributor_state(), self.settings(overrides))
            self.assertEqual(tree["contrib"]["name"], "contrib-lon")
            self.assertEqual(tree["standby"]["name"], "contrib-lon-b")
        # The standby changes nothing else of the tree.
        self.assertEqual(
            {key: value for key, value in deploy.relay_tree(two_contributor_state(),
                                                            settings).items() if key != "standby"},
            {key: value for key, value in deploy.relay_tree(one_contributor_state(),
                                                            settings).items() if key != "standby"})

    def test_anything_but_one_contributor_and_at_most_one_standby_is_refused(self):
        settings = self.settings()

        def refused(change) -> str:
            state = two_contributor_state()
            change(state["nodes"])
            with self.assertRaises(SystemExit) as caught:
                deploy.relay_tree(state, settings)
            return str(caught.exception)

        # Two contributors, neither the standby.
        self.assertIn("one is the standby",
                      refused(lambda nodes: nodes["contrib-lon-b"].pop("standby")))
        # A standby and no contributor.
        self.assertIn("contrib-lon-b (standby)", refused(lambda nodes: nodes.pop("contrib-lon")))
        # Two standbys: the fan-out would be three contributors.
        self.assertIn("one is the standby", refused(lambda nodes: nodes.update(
            {"contrib-lon-c": {**STANDBY, "name": "contrib-lon-c", "vlan_ipv4": "10.77.1.8"}})))
        # No contributor at all.
        state = one_contributor_state()
        del state["nodes"]["contrib-lon"]
        with self.assertRaises(SystemExit) as caught:
            deploy.relay_tree(state, settings)
        self.assertIn("found none", str(caught.exception))
        # The standby in another site, off the VLAN, or on the contributor's
        # own address.
        for change in (lambda nodes: nodes["contrib-lon-b"].update(site="newyork",
                                                                    region="us-east"),
                       lambda nodes: nodes["contrib-lon-b"].update(vlan_ipv4=None),
                       lambda nodes: nodes["contrib-lon-b"].update(vlan_ipv4="10.77.1.5"),
                       lambda nodes: nodes["contrib-lon"].update(vlan_ipv4=None)):
            self.assertIn("on its VLAN", refused(change))

    def test_the_program_carries_the_standby_as_a_replica_of_the_origin(self):
        settings = self.settings()
        with mock.patch.object(deploy.time, "time", return_value=1_763_900_000.0):
            single = deploy.build_program(one_contributor_state(), settings, ROUTES)
            program = deploy.build_program(two_contributor_state(), settings, ROUTES)
        # As workstream A 2.9 writes it: the standby's own lanes, each to
        # the port that the contributor's other lane leaves free.
        self.assertEqual(program["origin_standby"], {
            "node_id": "contrib-b",
            "failure_domain": {"provider": "linode", "region": "gb-lon", "asn": 63949,
                               "zone": "gb-lon"},
            "carrier_links": [
                {"child_node_id": "lon-dist-a", "lane": "source",
                 "sender_bind": "10.77.1.7:22301", "sender_peer": "10.77.1.7:22301",
                 "receiver_bind": "0.0.0.0:22002", "receiver_target": "10.77.1.11:22002"},
                {"child_node_id": "lon-dist-b", "lane": "source_and_repair",
                 "sender_bind": "10.77.1.7:22302", "sender_peer": "10.77.1.7:22302",
                 "receiver_bind": "0.0.0.0:22001", "receiver_target": "10.77.1.12:22001"}]})
        # Still one origin with two children, and the standby is no node.
        origins = [node for node in program["topology"]["nodes"] if node["role"] == "origin"]
        self.assertEqual([node["node_id"] for node in origins], ["contrib"])
        self.assertNotIn("contrib-b", {node["node_id"] for node in program["topology"]["nodes"]})
        self.assertEqual(program["topology"]["limits"]["max_origin_children"], 2)
        # Nothing else of the program moves, and one contributor's program
        # has no such key.
        self.assertNotIn("origin_standby", single)
        self.assertEqual({key: value for key, value in program.items()
                          if key != "origin_standby"}, single)

    def test_the_sources_targets_are_every_contributor(self):
        self.assertEqual(deploy.common.contributor_targets(one_contributor_state()),
                         ["10.77.1.5:27100"])
        state = two_contributor_state()
        self.assertEqual(deploy.common.contributor_targets(state),
                         ["10.77.1.5:27100", "10.77.1.7:27100"])
        # The origin first whatever the names, and a public address where a
        # contributor has no VLAN.
        state["nodes"]["contrib-lon"]["name"] = "z-contrib"
        state["nodes"]["contrib-lon"]["vlan_ipv4"] = None
        self.assertEqual(deploy.common.contributor_targets(state),
                         ["10.90.0.5:27100", "10.77.1.7:27100"])
        self.assertEqual(deploy.DAW_MEDIA_PORT, 27100)

    def test_a_distributor_of_the_contributors_region_holds_no_long_haul_gap(self):
        settings = self.settings()
        for state in (one_contributor_state(), two_contributor_state()):
            held = {name: deploy.gap_hold_args(node, settings, state)
                    for name, node in state["nodes"].items()}
            self.assertEqual(held["lon-dist-a"], [])
            self.assertEqual(held["new-dist-a"], ["--canonical-gap-hold-ms", "400"])
        # No contributor: nothing to be far from.
        state = one_contributor_state()
        del state["nodes"]["contrib-lon"]
        self.assertEqual(deploy.gap_hold_args(state["nodes"]["new-dist-a"], settings, state), [])

    def test_the_lab_tool_records_the_standby_in_its_state(self):
        lab_tool = load_module("lab_tool_merged", "linode-small-mesh-lab.py")

        class Api:
            def call(self, method, path, body=None, allow_404=False):
                return {"id": 7, "label": body["label"], "ipv4": ["192.0.2.7"],
                        "created": "2026-11-23T20:00:00"}

        node = {"name": "contrib-lon-b", "role": "contributor", "site": "london",
                "region": "gb-lon", "type": "g6-dedicated-4", "vlan_ipv4": "10.80.1.6/24",
                "_hourly": 0.108, "standby": True,
                # C CT3: no instance is created without its watchdog deadline.
                "deadline_unix": 1_795_000_000}
        record = lab_tool.create_node(Api(), "t1", "needletail-small-mesh-t1", "image", "key",
                                      node)
        self.assertIs(record["standby"], True)
        self.assertEqual(deploy.relay_node_id(record), "contrib-b")
        record = lab_tool.create_node(Api(), "t1", "needletail-small-mesh-t1", "image", "key",
                                      {**node, "name": "contrib-lon", "standby": False})
        self.assertNotIn("standby", record)


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class TwoContributors(Base):
    """The render of the contributor and its standby (workstream A NT2)."""

    def render(self, overrides=(), state: dict | None = None) -> tuple[dict, dict, dict]:
        settings = self.settings(overrides)
        state = state or two_contributor_state()
        # One clock for every render of a test: the path observations carry
        # the time of the plan.
        with mock.patch.object(deploy.time, "time", return_value=1_763_900_000.0):
            program = deploy.build_program(state, settings, ROUTES)
        plan = deploy.compile_plan(program)
        rendered = deploy.render(state, settings, plan)
        return state, rendered, {name: item["args"] for name, item in rendered.items()}

    def test_the_ports_of_two_contributors_at_the_first_forwarders(self):
        """Workstream A 2.7: each first forwarder takes contrib-lon on its
        primary socket and contrib-lon-b on its secondary socket, crossed."""
        _state, _rendered, args = self.render()
        for name, local_id, address, to_a, to_b in (
                ("contrib-lon", "contrib", "10.77.1.5", "22001", "22002"),
                ("contrib-lon-b", "contrib-b", "10.77.1.7", "22002", "22001")):
            contrib = args[name]
            self.assertEqual(value_of(contrib, "--relay-local-id"), local_id, name)
            self.assertEqual(contrib.count("--relay-local-id"), 1, name)
            # Today's shape on both: the primary lane to lon-dist-a, the
            # secondary lane (sources and repair) to lon-dist-b.
            self.assertEqual(value_of(contrib, "--relay-primary-id"), "lon-dist-a", name)
            self.assertEqual(value_of(contrib, "--relay-primary-bind"), f"{address}:22301")
            self.assertEqual(value_of(contrib, "--relay-primary-target"), f"10.77.1.11:{to_a}")
            self.assertEqual(value_of(contrib, "--relay-secondary-id"), "lon-dist-b", name)
            self.assertEqual(value_of(contrib, "--relay-secondary-bind"), f"{address}:22302")
            self.assertEqual(value_of(contrib, "--relay-secondary-target"), f"10.77.1.12:{to_b}")
            self.assertIn("--relay-secondary-seed-source", contrib)
            self.assertEqual(value_of(contrib, "--mesh-fec-target"), f"10.77.1.11:{to_a}")
            self.assertEqual(value_of(contrib, "--daw-media-bind"), "0.0.0.0:27100")
        plan = deploy.class_plan(self.settings())
        for name, primary, secondary in (
                ("lon-dist-a", ("0.0.0.0:22001", "10.77.1.5:22301"),
                 ("0.0.0.0:22002", "10.77.1.7:22301")),
                ("lon-dist-b", ("0.0.0.0:22002", "10.77.1.5:22302"),
                 ("0.0.0.0:22001", "10.77.1.7:22302"))):
            node_args = args[name]
            self.assertEqual(value_of(node_args, "--fec-bind"), primary[0], name)
            self.assertEqual(value_of(node_args, "--relay-primary-bind"), primary[0], name)
            self.assertEqual(value_of(node_args, "--relay-primary-peer"), primary[1], name)
            self.assertEqual(value_of(node_args, "--relay-primary-id"), "contrib", name)
            self.assertEqual(value_of(node_args, "--relay-secondary-bind"), secondary[0], name)
            self.assertEqual(value_of(node_args, "--relay-secondary-peer"), secondary[1], name)
            self.assertEqual(value_of(node_args, "--relay-secondary-id"), "contrib-b", name)
            # Both sessions admitted in full, each flag once; nothing
            # promotes or demotes a contributor.
            for flag in ("--relay-primary-promoted", "--relay-secondary-promoted",
                         "--relay-secondary-bind", "--relay-secondary-peer",
                         "--relay-secondary-id"):
                self.assertEqual(node_args.count(flag), 1, (name, flag))
            self.assertNotIn("--relay-failover-controller", node_args, name)
            # Audio on the two ports, video on their twins (23001, 23002).
            ports = {port for _socket, port, _arg in deploy.udp_binds(node_args, plan)}
            self.assertTrue({22001, 22002, 23001, 23002} <= ports, (name, ports))

    def test_no_other_nodes_arguments_change_with_a_standby(self):
        _state, _rendered, single = self.render(state=one_contributor_state())
        _state, _rendered, pair = self.render()
        self.assertEqual(set(pair) - set(single), {"contrib-lon-b"})
        self.assertEqual(set(single) - set(pair), set())
        firsts = ("lon-dist-a", "lon-dist-b")
        for name in single:
            if name not in firsts:
                self.assertEqual(pair[name], single[name], name)
        # A first forwarder gains its secondary lane, in one place, and
        # nothing else of it moves.
        for name in firsts:
            at = pair[name].index("--relay-secondary-bind")
            self.assertEqual(pair[name][at:at + 7:2],
                             ["--relay-secondary-bind", "--relay-secondary-peer",
                              "--relay-secondary-id", "--relay-secondary-promoted"], name)
            self.assertEqual(pair[name][:at] + pair[name][at + 7:], single[name], name)

    def test_a_one_contributor_topology_renders_as_before(self):
        _state, _rendered, args = self.render(state=one_contributor_state())
        contrib = args["contrib-lon"]
        self.assertEqual(value_of(contrib, "--relay-local-id"), "contrib")
        self.assertEqual(value_of(contrib, "--wall-clock-estimated-error-ms"), "5")
        # Nothing of the resilient mesh without a binary that has it.
        for flag in ("--node-id", "--region", "--print-fingerprint"):
            self.assertNotIn(flag, contrib, flag)
        for name, node_args in args.items():
            self.assertNotIn("--relay-media-deadline-ms", node_args, name)
            self.assertNotIn("contrib-b", node_args, name)
        for name in ("lon-dist-a", "lon-dist-b"):
            self.assertNotIn("--relay-secondary-id", args[name], name)
        plan = json.loads((self.dir / "compiled-plan.json").read_text())
        self.assertEqual([service["node_id"] for service in plan["services"]
                          if service["service"] == "av_contrib"], ["contrib"])
        self.assertNotIn("standby", (self.dir / "compiled-plan.json").read_text())
        self.assertNotIn("standby", (self.dir / "relay-program.json").read_text())
        self.assertFalse((self.dir / "nodes" / "contrib-lon-b.args").exists())

    def test_both_contributors_render_equal_identity_arguments(self):
        for overrides in ((), ("audio_part=96samples", "media_deadline_ms=800",
                               "contributor_clock_error_ms=6", "relay_compact_audio=false",
                               "relay_min_repair_symbols=2", "daw_hls_formats=flac,opus,pcm",
                               "daw_hls_base_stream_id=5001",
                               'extra_contributor_args=["--relay-compact-whole-ms","50"]')):
            _state, _rendered, args = self.render(overrides)
            one, other = flag_values(args["contrib-lon"]), flag_values(args["contrib-lon-b"])
            self.assertEqual(list(one), list(other))
            for flag in deploy.CONTRIBUTOR_PAIR_FLAGS:
                self.assertEqual(one.get(flag), other.get(flag), flag)
            for flag in one:
                if flag.startswith(deploy.CONTRIBUTOR_PAIR_PREFIXES):
                    self.assertEqual(one[flag], other[flag], flag)
            # They differ in who they are and in their own end of each
            # lane, and in nothing else.
            self.assertEqual({flag for flag in one if one[flag] != other[flag]},
                             OWN_OF_A_CONTRIBUTOR, overrides)
        # The settings that are the identity reach both.
        self.assertEqual(one["--audio-part"], ["96samples"])
        self.assertEqual(one["--relay-deadline-ms"], ["800"])
        self.assertEqual(one["--wall-clock-estimated-error-ms"], ["6"])
        self.assertEqual(other["--relay-compact-whole-ms"], ["50"])
        with self.assertRaises(ValueError) as caught:
            self.render(["contributor_clock_error_ms=five"])
        self.assertIn("contributor_clock_error_ms", str(caught.exception))

    def test_two_contributors_that_would_not_publish_the_same_parts_are_refused(self):
        state, rendered, _args = self.render()
        deploy.check_contributor_pair(state, rendered)
        deploy.check_contributor_pair(one_contributor_state(), rendered)

        def doctored(flag: str, value: str | None) -> dict:
            """The render with `flag` given another value (None: taken
            away) on the standby alone."""
            args = list(rendered["contrib-lon-b"]["args"])
            if flag in args:
                at = args.index(flag)
                args[at:at + 2] = [] if value is None else [flag, value]
            elif value is not None:
                args += [flag, value]
            return {**rendered, "contrib-lon-b": {**rendered["contrib-lon-b"], "args": args}}

        for flag, value in (
                ("--daw-hls-formats", "flac"), ("--daw-hls-base-stream-id", "5001"),
                ("--audio-part", "96samples"), ("--fmp4-segment-ms", "2000"),
                ("--relay-deadline-ms", "800"), ("--wall-clock-estimated-error-ms", "6"),
                ("--relay-topology-generation", "2"), ("--relay-subscription-id", "2"),
                ("--relay-compact-audio", "false"), ("--relay-compact-whole-ms", "50"),
                ("--relay-min-repair-symbols", "1"), ("--relay-send-batch", "false"),
                ("--relay-source-first", "false"), ("--relay-primary-id", "lon-dist-b"),
                ("--relay-path-loss-fraction", "0.5"), ("--relay-path-rtt-ms", None),
                ("--relay-secondary-path-jitter-ms", "9.0")):
            with self.assertRaises(ValueError, msg=flag) as caught:
                deploy.check_contributor_pair(state, doctored(flag, value))
            self.assertIn(flag, str(caught.exception))
            self.assertIn("contrib-lon and contrib-lon-b", str(caught.exception))
        # fMP4 audio is numbered by each contributor for itself: refused on
        # both, whatever else is equal.
        with self.assertRaises(ValueError) as caught:
            self.render(["daw_hls_packaging=fmp4"])
        self.assertIn("--daw-hls-packaging opaque", str(caught.exception))
        self.render(["daw_hls_packaging=fmp4"], state=one_contributor_state())

    def test_a_first_forwarder_that_would_not_admit_a_contributor_is_refused(self):
        state, rendered, _args = self.render()
        deploy.check_merged(state, rendered)
        for name, flag, value in (("lon-dist-a", "--relay-secondary-peer", "10.77.1.9:22301"),
                                  ("lon-dist-b", "--relay-secondary-bind", "0.0.0.0:22005"),
                                  ("lon-dist-a", "--relay-secondary-id", "contrib"),
                                  ("lon-dist-b", "--relay-primary-peer", "10.77.1.7:22302")):
            args = list(rendered[name]["args"])
            args[args.index(flag) + 1] = value
            with self.assertRaises(ValueError, msg=(name, flag)) as caught:
                deploy.check_merged(state, {**rendered, name: {**rendered[name], "args": args}})
            self.assertIn(f"{name} does not admit", str(caught.exception))
        # A standby whose lanes end elsewhere than the contributor's.
        args = list(rendered["contrib-lon-b"]["args"])
        args[args.index("--relay-primary-id") + 1] = "lon-dist-b"
        with self.assertRaises(ValueError) as caught:
            deploy.check_merged(state, {**rendered, "contrib-lon-b": {
                **rendered["contrib-lon-b"], "args": args}})
        self.assertIn("the two lanes of the contributor contrib-lon-b", str(caught.exception))

    def test_a_plan_without_the_standbys_service_is_refused(self):
        """A compiled plan from a compiler that knows no standby (it drops
        the key it does not know) must not render one contributor's lanes
        twice."""
        settings = self.settings()
        state = two_contributor_state()
        plan = deploy.compile_plan(deploy.build_program(one_contributor_state(), settings, ROUTES))
        with self.assertRaises(ValueError) as caught:
            deploy.render(state, settings, plan)
        self.assertIn("no contributor service contrib-b", str(caught.exception))

    def test_the_legacy_topology_takes_the_standby_on_its_backbones(self):
        _state, _rendered, args = self.render(["topology=legacy"])
        self.assertEqual(value_of(args["relay-primary"], "--relay-primary-id"), "contrib")
        self.assertEqual(value_of(args["relay-primary"], "--relay-secondary-id"), "contrib-b")
        self.assertEqual(value_of(args["relay-primary"], "--relay-secondary-peer"),
                         "10.77.1.7:22301")
        self.assertEqual(value_of(args["relay-secondary"], "--relay-secondary-id"), "contrib-b")
        self.assertEqual(value_of(args["contrib-lon-b"], "--relay-primary-id"), "relay-primary")

    def test_the_new_flags_render_only_for_a_binary_that_has_them(self):
        old_contrib = fake_binary(self.dir, "old-contrib", ["--relay-exclusive"])
        new_contrib = fake_binary(self.dir, "new-contrib",
                                  ["--relay-exclusive", "--node-id", "--region"])
        old_mesh = fake_binary(self.dir, "old-mesh", ["--client-udp-bind", "--region"])
        new_mesh = fake_binary(self.dir, "new-mesh",
                               ["--client-udp-bind", "--region", "--relay-media-deadline-ms"])
        try:
            deploy.RENDER_BINARIES.update({"av-contrib": old_contrib, "av-mesh": old_mesh})
            _state, _rendered, args = self.render()
            for name, node_args in args.items():
                self.assertNotIn("--relay-media-deadline-ms", node_args, name)
                if name.startswith("contrib"):
                    self.assertNotIn("--node-id", node_args, name)
                    self.assertNotIn("--region", node_args, name)
            deploy.RENDER_BINARIES.update({"av-contrib": new_contrib, "av-mesh": new_mesh})
            _state, _rendered, args = self.render(["media_deadline_ms=800"])
            # What /api/node names each contributor: the node, not the id
            # that the relay sessions carry.
            for name in ("contrib-lon", "contrib-lon-b"):
                self.assertEqual(value_of(args[name], "--node-id"), name)
                self.assertEqual(value_of(args[name], "--region"), "gb-lon")
                self.assertEqual(value_of(args[name], "--relay-deadline-ms"), "800")
                self.assertNotIn("--relay-media-deadline-ms", args[name])
            # The same budget on every node with a relay session, edges and
            # the forwarder slot included, and on no other.
            relayed = 0
            for name, node_args in args.items():
                if "--relay-controlled-local" in node_args:
                    relayed += 1
                    self.assertEqual(value_of(node_args, "--relay-media-deadline-ms"), "800",
                                     name)
                    self.assertEqual(node_args.count("--relay-media-deadline-ms"), 1, name)
                else:
                    self.assertNotIn("--relay-media-deadline-ms", node_args, name)
            self.assertGreater(relayed, 8)
            for name in ("lon-dist-a", "new-dist-b", "lon-edge-1", "lon-dist-c"):
                self.assertIn("--relay-media-deadline-ms", args[name], name)
            # An extra argument of a role still wins.
            _state, _rendered, args = self.render(
                ['extra_distributor_args=["--relay-media-deadline-ms","700"]'])
            self.assertEqual(args["lon-dist-a"].count("--relay-media-deadline-ms"), 1)
            self.assertEqual(value_of(args["lon-dist-a"], "--relay-media-deadline-ms"), "700")
            self.assertEqual(value_of(args["lon-edge-1"], "--relay-media-deadline-ms"), "1000")
        finally:
            deploy.RENDER_BINARIES.clear()

    def test_the_fingerprints_of_both_renders_must_be_equal(self):
        flags = ["--relay-exclusive", "--print-fingerprint"]
        checks = ('case "$1" in /*) echo "argv[0] passed on" >&2; exit 9;; esac\n'
                  'for last in "$@"; do :; done\n'
                  '[ "$last" = --print-fingerprint ] || { echo "not asked" >&2; exit 8; }\n')
        same = fake_binary(self.dir, "same", flags, checks + (
            'echo \'{"fingerprint": "9f2c41aa7d10e6b3", "numbering": "51c0", '
            '"envelope": "0be2", "wire": "77aa"}\''))
        # A build whose envelope depends on who sends it.
        differs = fake_binary(self.dir, "differs", flags, checks + (
            'prev=""; for arg in "$@"; do [ "$prev" = --relay-local-id ] && id="$arg"; '
            'prev="$arg"; done\n'
            'echo "{\\"fingerprint\\": \\"f-$id\\", \\"numbering\\": \\"51c0\\", '
            '\\"envelope\\": \\"e-$id\\", \\"wire\\": \\"77aa\\"}"'))
        silent = fake_binary(self.dir, "silent", flags,
                             'echo "cannot read /etc/needletail/tls/privkey.pem" >&2; exit 1')
        garbled = fake_binary(self.dir, "garbled", flags, "echo ready")
        without = fake_binary(self.dir, "without", ["--relay-exclusive"], "exit 7")
        try:
            deploy.RENDER_BINARIES.update({"av-contrib": same})
            _state, _rendered, args = self.render()
            self.assertEqual(deploy.contributor_identity(args["contrib-lon-b"])["fingerprint"],
                             "9f2c41aa7d10e6b3")
            deploy.RENDER_BINARIES.update({"av-contrib": differs})
            with self.assertRaises(ValueError) as caught:
                self.render()
            self.assertIn("fingerprints are f-contrib and f-contrib-b", str(caught.exception))
            self.assertIn("(envelope differ)", str(caught.exception))
            # One contributor has nothing to be equal to: the binary is not
            # even asked.
            self.render(state=one_contributor_state())
            for binary, said in ((silent, "privkey.pem"), (garbled, "gave no identity")):
                deploy.RENDER_BINARIES.update({"av-contrib": binary})
                with self.assertRaises(ValueError, msg=binary.name) as caught:
                    self.render()
                self.assertIn(said, str(caught.exception))
            # A binary without the flag is not run for it.
            deploy.RENDER_BINARIES.update({"av-contrib": without})
            _state, _rendered, args = self.render()
            self.assertIsNone(deploy.contributor_identity(args["contrib-lon"]))
        finally:
            deploy.RENDER_BINARIES.clear()

    def test_plan_names_the_targets_of_the_source(self):
        state_file = self.dir / "lab.json"
        state = two_contributor_state()
        state_file.write_text(json.dumps(state))
        # routes.json is this lab's (C 2.6: a file of another lab is refused).
        (self.dir / "routes.json").write_text(json.dumps({**ROUTES, "topology": "merged",
                                                          "lab_run_id": state["run_id"]}))
        out = io.StringIO()
        try:
            with mock.patch.dict("os.environ", {"NEEDLETAIL_LINODE_LAB_STATE": str(state_file)}), \
                    mock.patch.object(deploy, "binaries_for",
                                      lambda role, node=None: [self.dir / "no-such-binary"]), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                deploy.command_plan(argparse.Namespace(set=[], remeasure=False))
        finally:
            deploy.RENDER_BINARIES.clear()
        self.assertIn("contributor targets: 10.77.1.5:27100 10.77.1.7:27100\n", out.getvalue())
        self.assertIn("compiled 6 relay services", out.getvalue())


class Lifecycle(Base):
    def test_push_and_restart_stop_an_unused_backbone(self):
        sent = []
        node = {**lab.node("relay-primary"), "type": "g6-dedicated-2"}
        with mock.patch.object(deploy.common, "ssh_text",
                               lambda node, script, **k: sent.append(script) or ""):
            deploy.stop_service(node)
        self.assertIn("systemctl disable --now needletail-mesh.service", sent[-1])

    @unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
    def test_install_stages_and_activates_a_backbone_only_in_the_legacy_topology(self):
        """push and restart --roles go through LinodeOps.install: a backbone
        the merged topology does not use is stopped and gets no files; in
        the legacy topology it is staged and activated as any node."""
        state = lab.lab_state()
        build = "aaaaaaaaaaaa-1000"
        state["deployed"] = {"tiers": {"edges": build, "forwarders": build,
                                       "contributors": build}, "nodes": {}}
        for binary in ("av-mesh", "av-contrib"):
            path = self.dir / "builds" / build / binary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        (self.dir / "routes.json").write_text(json.dumps({**ROUTES, "topology": "merged"}))
        for mode, staged in (("merged", False), ("legacy", True)):
            calls = []
            with mock.patch.object(deploy, "stage_node", lambda node, *a: calls.append(
                    ("stage", node["name"]))), \
                    mock.patch.object(deploy, "activate_node", lambda node, *a, **k: calls.append(
                        ("activate", node["name"])) or {"ok": True, "why": "", "qdisc": None}), \
                    mock.patch.object(deploy, "stop_service", lambda node, *a: calls.append(
                        ("stop", node["name"]))), \
                    mock.patch.object(deploy, "record_deployed", lambda *a, **k: None), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                ops = deploy.LinodeOps(state, self.settings([f"topology={mode}"]))
                ops.install(["relay-primary"], None)
            self.assertEqual(calls, [("stage", "relay-primary"), ("activate", "relay-primary")]
                             if staged else [("stop", "relay-primary")], mode)

    def test_the_lab_starts_no_service_for_an_unused_backbone(self):
        with mock.patch.object(lab, "load", lambda name, default=None: {}):
            with self.assertRaises(lab.LabError) as caught:
                lab.start_service("relay-primary")
        self.assertIn("topology=legacy", str(caught.exception))


class Unit(unittest.TestCase):
    def test_the_unit_restarts_at_once_without_a_start_limit_and_stops_within_b_s_bound(self):
        """C 2.5 with B 3.12's stop bound: no start limit (with RestartSec=1,
        five crashes in ten seconds would leave a media node failed for
        good), a restart a second after an exit, a stop bounded by 20 s
        (the edge's --stop-drain-ms 14000, its shutdown and a spare), and
        /var/lib/needletail for the operator's drain."""
        for service in ("needletail-mesh", "needletail-contrib"):
            unit = deploy.unit_text(service, "info", dict(deploy.DEFAULT_SETTINGS))
            head, _, body = unit.partition("[Service]")
            self.assertIn("\nStartLimitIntervalSec=0\n", head, service)
            for line in ("Restart=always", "RestartSec=1", "TimeoutStopSec=20",
                         "StateDirectory=needletail",
                         f"ExecStart=/usr/local/bin/needletail-run-args /etc/needletail/"
                         f"{service}.args"):
                self.assertIn(f"\n{line}\n", body, service)
            self.assertNotIn("RestartSec=2", body)
        self.assertEqual(deploy.mesh_ops.UNIT_TIMEOUT_STOP_S, 20)
        self.assertGreaterEqual(deploy.mesh_ops.UNIT_TIMEOUT_STOP_S
                                - deploy.EDGE_STOP_DRAIN_MS / 1000, 4)


class Activation(unittest.TestCase):
    """The activation script (C 2.5) run by bash on a directory tree that
    stands for a node, with systemctl, sysctl and modprobe stubbed: a late
    activation and a missing binary change nothing; an activation keeps
    the previous arguments beside the previous binary and a rollback puts
    them back; `.args` is never absent; old builds are pruned, never one
    that is named."""

    SERVICE = "needletail-mesh"

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.etc, self.run_dir = self.root / "etc", self.root / "run"
        self.bin, self.units = self.root / "bin", self.root / "units"
        self.sysctl = self.root / "sysctl.d" / "90-needletail.conf"
        for folder in (self.etc, self.bin, self.units, self.sysctl.parent):
            folder.mkdir(parents=True)
        self.log = self.root / "calls.log"
        self.args = self.etc / f"{self.SERVICE}.args"
        script = deploy.ACTIVATE_SCRIPT
        for line, path in (("ETC=/etc/needletail", self.etc), ("RUN=/run/needletail", self.run_dir),
                           ("BIN=/usr/local/bin", self.bin),
                           ("UNITS=/etc/systemd/system", self.units),
                           ("SYSCTL=/etc/sysctl.d/90-needletail.conf", self.sysctl),
                           ("BUILDS=/opt/needletail/builds", self.root / "builds")):
            self.assertIn(f"\n{line}\n", script)
            script = script.replace(f"\n{line}\n", f"\n{line.split('=')[0]}={path}\n")
        self.script = self.root / "activate"
        self.script.write_text(script)
        stubs = self.root / "stubs"
        stubs.mkdir()
        # systemctl, sysctl and modprobe log their arguments; cp and mv run
        # the real ones and log "absent" when .args is missing after them.
        for name in ("systemctl", "sysctl", "modprobe"):
            (stubs / name).write_text(f"#!/bin/sh\necho \"{name} $*\" >>{self.log}\n")
        for name in ("cp", "mv"):
            (stubs / name).write_text(
                f"#!/bin/sh\n{shutil.which(name)} \"$@\"; status=$?\n"
                f"if [ -f {self.args}.prev ] && [ ! -f {self.args} ]; then "
                f"echo absent >>{self.log}; fi\nexit $status\n")
        for path in stubs.iterdir():
            path.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
        for build in ("a", "b"):
            binary = self.root / "builds" / build / "av-mesh"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            (binary.parent / "aep1-48k-probe").write_text("probe\n")
        # Build a is in force, with its unit.
        self.args.write_text(self.text("a"))
        (self.units / f"{self.SERVICE}.service").write_text("unit a\n")

    def text(self, build: str) -> str:
        return f"{self.root}/builds/{build}/av-mesh\n--node-id\nlon-edge-1\n--build\n{build}\n"

    def stage(self, build: str, unit: str | None = None) -> None:
        (self.etc / f"{self.SERVICE}.args.next").write_text(self.text(build))
        (self.etc / f"{self.SERVICE}.unit.next").write_text(unit or f"unit {build}\n")

    def activate(self, *extra: str, not_after_s: float = 10.0) -> tuple[int, dict]:
        not_after = int((time.time() + not_after_s) * 1000)
        result = subprocess.run(["bash", str(self.script), self.SERVICE, "--not-after",
                                 str(not_after), *extra], env=self.env, capture_output=True,
                                text=True, timeout=60)
        outcome = json.loads((self.run_dir / f"activate-{self.SERVICE}.json").read_text())
        return result.returncode, outcome

    def calls(self) -> str:
        return self.log.read_text() if self.log.is_file() else ""

    def test_a_late_activation_changes_nothing(self):
        self.stage("b")
        code, outcome = self.activate(not_after_s=-1.0)
        self.assertEqual((code, outcome["ok"], outcome["why"]), (1, False, "late"))
        self.assertEqual(self.args.read_text(), self.text("a"))
        self.assertTrue((self.etc / f"{self.SERVICE}.args.next").is_file())
        self.assertFalse((self.etc / f"{self.SERVICE}.args.prev").exists())
        self.assertEqual(self.calls(), "")

    def test_a_missing_binary_is_refused(self):
        for build in ("c", "plain"):
            if build == "plain":
                (self.root / "builds" / "plain").mkdir()
                (self.root / "builds" / "plain" / "av-mesh").write_text("not executable\n")
            self.stage(build)
            code, outcome = self.activate()
            self.assertEqual((code, outcome["ok"]), (1, False), build)
            self.assertEqual(outcome["why"], f"not an executable on this node: "
                                             f"{self.root}/builds/{build}/av-mesh")
            self.assertEqual(self.args.read_text(), self.text("a"))
            self.assertEqual(self.calls(), "")
        (self.etc / f"{self.SERVICE}.args.next").unlink()
        code, outcome = self.activate()
        self.assertIn("nothing to put in force", outcome["why"])

    def test_the_previous_arguments_stay_beside_the_previous_binary_and_come_back(self):
        self.stage("b")
        (self.etc / f"{self.SERVICE}.sysctl.next").write_text("net.core.rmem_max = 1\n")
        code, outcome = self.activate()
        self.assertEqual((code, outcome["ok"], outcome["why"]), (0, True, ""))
        self.assertEqual((outcome["from"], outcome["to"]),
                         (f"{self.root}/builds/a/av-mesh", f"{self.root}/builds/b/av-mesh"))
        self.assertEqual(self.args.read_text(), self.text("b"))
        self.assertEqual((self.etc / f"{self.SERVICE}.args.prev").read_text(), self.text("a"))
        self.assertFalse((self.etc / f"{self.SERVICE}.args.next").exists())
        self.assertEqual(os.readlink(self.bin / "av-mesh"), f"{self.root}/builds/b/av-mesh")
        self.assertEqual(os.readlink(self.bin / "aep1-48k-probe"),
                         f"{self.root}/builds/b/aep1-48k-probe")
        self.assertEqual((self.units / f"{self.SERVICE}.service").read_text(), "unit b\n")
        self.assertEqual((self.etc / f"{self.SERVICE}.unit.prev").read_text(), "unit a\n")
        self.assertEqual(self.sysctl.read_text(), "net.core.rmem_max = 1\n")
        self.assertEqual(self.calls().splitlines(), [
            "modprobe tcp_bbr", f"sysctl -q -p {self.sysctl}", "systemctl daemon-reload",
            f"systemctl enable --quiet {self.SERVICE}.service",
            f"systemctl restart {self.SERVICE}.service"])
        # Staged again with nothing that differs: no kernel setting, no
        # daemon-reload.
        self.log.unlink()
        self.stage("b")
        self.assertEqual(self.activate()[1]["ok"], True)
        self.assertEqual(self.calls().splitlines(), [
            f"systemctl enable --quiet {self.SERVICE}.service",
            f"systemctl restart {self.SERVICE}.service"])
        self.assertEqual((self.etc / f"{self.SERVICE}.args.prev").read_text(), self.text("b"))
        # The rollback of a failed activation: a, then b, then back to a.
        self.stage("a")
        self.activate()
        self.stage("b")
        self.activate()
        self.log.unlink()
        code, outcome = self.activate("--rollback")
        self.assertEqual((code, outcome["ok"]), (0, True))
        self.assertEqual(self.args.read_text(), self.text("a"))
        self.assertEqual((self.etc / f"{self.SERVICE}.args.failed").read_text(), self.text("b"))
        self.assertEqual(os.readlink(self.bin / "av-mesh"), f"{self.root}/builds/a/av-mesh")
        self.assertEqual((self.units / f"{self.SERVICE}.service").read_text(), "unit a\n")
        self.assertIn(f"systemctl restart {self.SERVICE}.service", self.calls())

    def test_the_arguments_are_never_absent(self):
        for build in ("b", "a", "b"):
            self.stage(build)
            self.assertTrue(self.activate()[1]["ok"])
        self.assertTrue(self.activate("--rollback")[1]["ok"])
        self.assertTrue(self.activate("--rollback")[1]["ok"])
        self.assertNotIn("absent", self.calls())
        # The stub sees an absent file (the check itself is live).
        os.rename(self.args, self.root / "away")
        subprocess.run([str(self.root / "stubs" / "cp"), str(self.root / "away"),
                        str(self.root / "copy")], env=self.env, check=True)
        self.assertIn("absent", self.calls())

    def test_arguments_of_the_layout_before_builds_are_never_put_back(self):
        """A lab upgraded to builds: the first activation keeps the old
        `.args` (line 1 /usr/local/bin/av-mesh) as `.args.prev`. Its
        rollback is refused, and no file of /usr/local/bin is touched."""
        self.args.write_text(f"{self.bin}/av-mesh\n--node-id\nlon-edge-1\n")
        (self.bin / "av-mesh").write_text("#!/bin/sh\n")
        (self.bin / "av-mesh").chmod(0o755)
        (self.bin / "needletail-run-args").write_text("#!/bin/sh\n")
        self.stage("b")
        self.assertTrue(self.activate()[1]["ok"])
        self.log.unlink()
        code, outcome = self.activate("--rollback")
        self.assertEqual((code, outcome["ok"]), (1, False))
        self.assertEqual(outcome["why"],
                         f"not a binary of a build in {self.root}/builds: {self.bin}/av-mesh")
        self.assertEqual(self.args.read_text(), self.text("b"))
        self.assertFalse((self.bin / "needletail-run-args").is_symlink())
        self.assertEqual(self.calls(), "")

    def test_the_host_qdisc_is_applied_once_and_a_failed_one_puts_nothing_in_force(self):
        qdisc = self.etc / f"{self.SERVICE}.qdisc.next"
        applied, output = self.run_dir / f"{self.SERVICE}.qdisc", \
            self.run_dir / f"qdisc-{self.SERVICE}.txt"
        qdisc.write_text("echo applied\n")
        self.stage("b")
        code, outcome = self.activate()
        self.assertEqual((code, outcome["ok"], outcome["qdisc"]), (0, True, True))
        self.assertEqual((output.read_text(), applied.read_text()), ("applied\n", "echo applied\n"))
        # Staged again as it is: not run again (its copy in /run is the same).
        output.unlink()
        self.stage("a")
        code, outcome = self.activate()
        self.assertEqual((code, outcome["ok"], outcome["qdisc"]), (0, True, False))
        self.assertFalse(output.exists())
        # A qdisc that fails: its last line is the reason, nothing is put in
        # force and nothing restarted.
        qdisc.write_text('echo "starting"\necho "tc: \\"eth1\\" refused"\nexit 1\n')
        self.stage("b")
        self.log.unlink()
        code, outcome = self.activate()
        self.assertEqual((code, outcome["ok"], outcome["qdisc"]), (1, False, False))
        self.assertEqual(outcome["why"], "the host qdisc failed: tc: eth1 refused")
        self.assertEqual(self.args.read_text(), self.text("a"))
        self.assertTrue((self.etc / f"{self.SERVICE}.args.next").is_file())
        self.assertEqual(applied.read_text(), "echo applied\n")
        self.assertNotIn("systemctl", self.calls())

    def test_a_staged_check_unit_is_installed_before_the_unit_that_wants_it(self):
        """D 2.4: the check at boot's unit goes in with the service's unit,
        once; staged again as it is, nothing is reloaded for it."""
        check = self.units / "needletail-node-check.service"
        (self.etc / f"{self.SERVICE}.check.next").write_text("check 30\n")
        self.stage("b", unit="unit b\nWants=needletail-node-check.service\n")
        self.assertTrue(self.activate()[1]["ok"])
        self.assertEqual(check.read_text(), "check 30\n")
        self.assertIn("Wants=needletail-node-check.service",
                      (self.units / f"{self.SERVICE}.service").read_text())
        self.log.unlink()
        self.stage("a", unit="unit b\nWants=needletail-node-check.service\n")
        self.assertTrue(self.activate()[1]["ok"])
        self.assertNotIn("daemon-reload", self.calls())

    def test_the_staged_host_qdisc_checks_every_nic(self):
        """The qdisc the deploy stages (node_files) fails when a NIC does
        not show it, so the activation refuses before it puts anything in
        force; tc is stubbed, and so is /sys/class/net."""
        net = self.root / "net"
        for device in ("eth0", "eth1"):
            (net / device / "device").mkdir(parents=True)
        (net / "lo").mkdir()
        shown = self.root / "tc-show"
        tc = self.root / "stubs" / "tc"
        tc.write_text(f"#!/bin/sh\ncase \"$*\" in *\"qdisc show\"*) cat {shown} ;; esac\n")
        tc.chmod(0o755)
        settings = dict(deploy.DEFAULT_SETTINGS)
        text = deploy.node_files(["x"], "unit", settings)["qdisc"]
        self.assertEqual(deploy.class_plan(settings)["host_qdisc"], "prio")
        (self.etc / f"{self.SERVICE}.qdisc.next").write_text(
            text.replace("/sys/class/net", str(net)))
        prio = ("qdisc prio 1: root refcnt 2 bands 3 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
                "qdisc pfifo 10: parent 1:1 limit 10000p\n"
                "qdisc fq 20: parent 1:2 limit 65536p flow_limit 4096p\n")
        for content, why in (("qdisc fq_codel 0: root refcnt 2\n",
                              "the host qdisc prio is not the root of eth0"),
                             (prio.replace("qdisc fq 20", "qdisc sfq 20"),
                              "the host qdisc prio has no fq 20: on eth0")):
            shown.write_text(content)
            self.stage("b")
            code, outcome = self.activate()
            self.assertEqual((code, outcome["ok"]), (1, False), why)
            self.assertEqual(outcome["why"], f"the host qdisc failed: {why}")
            self.assertEqual(self.args.read_text(), self.text("a"))
        shown.write_text(prio)
        code, outcome = self.activate()
        self.assertEqual((code, outcome["ok"], outcome["qdisc"]), (0, True, True))
        output = (self.run_dir / f"qdisc-{self.SERVICE}.txt").read_text()
        self.assertEqual(deploy.check_host_qdisc(output, "prio"), [])
        # No NIC at all is no qdisc.
        shutil.rmtree(net)
        net.mkdir()
        (self.run_dir / f"{self.SERVICE}.qdisc").unlink()
        self.stage("a")
        code, outcome = self.activate()
        self.assertEqual(outcome["why"], "the host qdisc failed: the host qdisc: no NIC")

    def test_old_builds_are_pruned_but_never_one_that_is_named(self):
        builds = self.root / "opt-builds"
        for index in range(1, 7):
            folder = builds / f"b{index}"
            folder.mkdir(parents=True)
            os.utime(folder, (1_000 + index, 1_000 + index))
        (self.etc / "needletail-mesh.args").write_text(f"{builds}/b1/av-mesh\n")
        (self.etc / "needletail-mesh.args.prev").write_text(f"{builds}/b2/av-mesh\n")
        script = deploy.PRUNE_BUILDS.replace("/etc/needletail", str(self.etc)) \
            .replace("/opt/needletail/builds", str(builds))
        subprocess.run(["bash", "-e", "-c", script], check=True, timeout=30)
        self.assertEqual(sorted(path.name for path in builds.iterdir()),
                         ["b1", "b2", "b4", "b5", "b6"])

    def test_the_node_computes_the_render_digest_as_the_tool_does(self):
        args = ["/opt/needletail/builds/k/av-contrib", "--relay-path-observed-at-unix-ms",
                "1790000000123", "--relay-secondary-path-observed-at-unix-ms", "1790000000456",
                "--x", "1"]
        path = self.root / "contrib.args"
        path.write_text(deploy.args_text(args))
        result = subprocess.run(["bash", "-c", deploy.RENDER_SHA256.format(path=path)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), deploy.render_sha256(args))
        later = list(args)
        later[2] = "1790000999999"
        self.assertEqual(deploy.render_sha256(later), deploy.render_sha256(args))
        self.assertNotEqual(deploy.args_sha256(later), deploy.args_sha256(args))


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Rollable(Base):
    """Every key of ROLLABLE_SETTINGS (C 2.6) renders local flags only: a
    node that runs the new value next to one that runs the old needs no
    peer to agree. Each key, set alone, changes only the flags named here
    (and, for the unit's keys, the unit), never an address, a port, a peer,
    a part or the relay DAG; and it reaches only the roles and the units
    named in REACH. C 2.6 asks one role a key; twelve keys of C's own list
    reach the edges and the forwarders both (a departure): what keeps a
    roll safe is the scope check at run time (LinodeOps.changed_by_set),
    and REACH makes a key that silently reaches another role show."""

    LOCAL = "--test-local-flag"
    CASES = {
        "rust_log_mesh": ("av_mesh=debug", set()),
        "rust_log_contrib": ("av_contrib=debug", set()),
        "node_env": ({"MALLOC_ARENA_MAX": "4"}, set()),
        "tokio_worker_threads": (2, set()),
        "extra_edge_args": (["--test-local-flag", "1"], {LOCAL}),
        "extra_distributor_args": (["--test-local-flag", "1"], {LOCAL}),
        "extra_contributor_args": (["--test-local-flag", "1"], {LOCAL}),
        "mlock": ("rings", {"--mlock"}),
        "malloc_tune": (True, {"--malloc-tune"}),
        "prefault": (True, {"--prefault"}),
        "relay_class_cpus": ("2,3", {"--relay-class-cpus"}),
        "relay_class_realtime": (False, {"--relay-class-realtime"}),
        "audio_hot_cpu": (1, {"--audio-hot-cpu"}),
        "cpu_load": (True, {"--cpu-load"}),
        "cpu_load_full_percent": (80, {"--cpu-load-full-percent"}),
        "client_udp_datagram_capacity": (200000, {"--client-udp-datagram-capacity"}),
        "egress_capacity_bps": (2000000000, {"--egress-capacity-bps"}),
        "client_udp_drain_spread_ms": (3000, {"--client-udp-drain-spread-ms"}),
        "rebalance_target_permille": (700, {"--rebalance-target-permille"}),
    }

    MESH, CONTRIB = "needletail-mesh", "needletail-contrib"
    EDGE_AND_FORWARDER = ({"edge", "distributor"}, set())
    REACH = {
        "rust_log_mesh": (set(), {MESH}),
        "rust_log_contrib": (set(), {CONTRIB}),
        "node_env": (set(), {MESH, CONTRIB}),
        "tokio_worker_threads": (set(), {MESH, CONTRIB}),
        "extra_edge_args": ({"edge"}, set()),
        "extra_distributor_args": ({"distributor"}, set()),
        "extra_contributor_args": ({"contributor"}, set()),
        "mlock": ({"edge", "distributor", "contributor"}, set()),
        "malloc_tune": EDGE_AND_FORWARDER,
        "prefault": EDGE_AND_FORWARDER,
        "relay_class_cpus": EDGE_AND_FORWARDER,
        "relay_class_realtime": EDGE_AND_FORWARDER,
        "audio_hot_cpu": ({"contributor"}, set()),
        "cpu_load": EDGE_AND_FORWARDER,
        "cpu_load_full_percent": EDGE_AND_FORWARDER,
        "client_udp_datagram_capacity": EDGE_AND_FORWARDER,
        "egress_capacity_bps": EDGE_AND_FORWARDER,
        "client_udp_drain_spread_ms": EDGE_AND_FORWARDER,
        "rebalance_target_permille": EDGE_AND_FORWARDER,
    }

    @staticmethod
    def pairs(args: list[str]) -> list[tuple]:
        """(flag, value) of every argument, the render's time masked."""
        out = []
        for index, arg in enumerate(args):
            if not arg.startswith("--"):
                continue
            value = args[index + 1] if index + 1 < len(args) and \
                not args[index + 1].startswith("--") else None
            out.append((arg, "*" if arg.endswith(deploy.RENDER_TIME_FLAG) else value))
        return out

    render = Render.render

    def units(self, overrides=()) -> dict:
        settings = self.settings(overrides)
        return {service: deploy.unit_text(service, settings[log], settings)
                for service, log in (("needletail-mesh", "rust_log_mesh"),
                                     ("needletail-contrib", "rust_log_contrib"))}

    def test_each_rollable_setting_renders_local_flags_only(self):
        self.assertEqual(set(self.CASES), set(deploy.ROLLABLE_SETTINGS))
        self.assertEqual(set(self.REACH), set(deploy.ROLLABLE_SETTINGS))
        _program, _plan, base = self.render()
        base_units = self.units()
        roles = {name: node["role"] for name, node in lab.lab_state()["nodes"].items()}
        for key, (value, flags) in self.CASES.items():
            with self.subTest(key=key):
                override = [f"{key}={json.dumps(value)}"]
                _program, _plan, args = self.render(override)
                changed, reached = set(), set()
                for name in base:
                    before, after = self.pairs(base[name]), self.pairs(args[name])
                    changed |= {flag for flag, _value in set(before) ^ set(after)}
                    if before != after:
                        reached.add(roles[name])
                self.assertEqual(changed, flags)
                units = self.units(override)
                self.assertEqual((reached, {service for service in units
                                            if units[service] != base_units[service]}),
                                 self.REACH[key])


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
