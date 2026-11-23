#!/usr/bin/env python3
"""Tests of scripts/compare/scale-run.py, the scale run's driver: the steps
and their split over regions, hosts and edges (from topology-scale.json,
standby edges left out, Sydney optional), the capacity and cost of a plan,
the commands of the source, needletail-load and ntcmp (VLAN addresses
only), the sampler (run here for two seconds), the readers of
needletail-load's interval and summary files (exact histogram merge across
hosts, the fallback without one), the LL-HLS readers' figures, saturation
and which tier gave out first, a whole step's analysis from files and its
summary.md; the failover, redistribution and autoscale phases: the
committed phase plan (at most 2 of 5 edges at once, F9 and A3 only when
named), its timeline (a kill is node loss, every fault restored), entry
points and liveness away from the faults, steered readers, the autoscale
listener profile, faults left out of what gave out first, the stranded
detector, a scenario's before/during/after analysis from files, the runner's
order, Unix-µs events and owed restores, and the plan's phase cost.
python3 -m unittest scripts/compare/test_scale_run.py"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("scale_run", HERE / "scale-run.py")
scale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scale)

TOPOLOGY = scale.TOPOLOGY
# Edges 6 to 10 of every region: the second curve's.
STANDBY = [f"{short}-edge-{index}" for short in ("syd", "lon", "new", "tok")
           for index in range(6, 11)]


def lab(without=()) -> dict:
    return scale.topology_state(TOPOLOGY, list(without))


_lab_spec = importlib.util.spec_from_file_location("small_mesh_lab",
                                                   HERE.parent / "linode-small-mesh-lab.py")
lab_tool = importlib.util.module_from_spec(_lab_spec)
_lab_spec.loader.exec_module(lab_tool)


def missing_entry(node: dict, kind: str = "account") -> dict:
    """A `listeners_missing` entry as the lab tool records it."""
    return lab_tool.missing_entry(node, kind, "HTTP 400: Account Limit reached")


def lab_tool_batches(state: dict) -> list[list[dict]]:
    """The lab tool's listener batches (`up --listener-batches 2`) of a lab."""
    hosts = sorted((node for node in state["nodes"].values()
                    if lab_tool.is_listener_host(node)), key=lambda node: scale.natural(node["name"]))
    return lab_tool.listener_batches(hosts, 2)


class Steps(unittest.TestCase):
    def test_parse_steps(self):
        self.assertEqual(scale.parse_steps("100,1000,5k,10_000"), [100, 1000, 5000, 10000])
        for bad in ("", "100,50", "0", "x", "100,100"):
            with self.assertRaises(ValueError, msg=bad):
                scale.parse_steps(bad)

    def test_split_is_whole_and_proportional(self):
        self.assertEqual(scale.split(10, [1, 1, 1]), [4, 3, 3])
        self.assertEqual(scale.split(7, [8, 8, 4, 4]), [3, 2, 1, 1])
        self.assertEqual(sum(scale.split(12345, [4, 8, 8, 4, 4])), 12345)
        self.assertEqual(scale.split(0, [1, 2]), [0, 0])

    def test_regions_leave_out_standby_edges_and_take_listener_hosts(self):
        regions = scale.regions_of(lab(), STANDBY)
        self.assertEqual(sorted(regions), ["london", "newyork", "sydney", "tokyo"])
        self.assertEqual([node["name"] for node in regions["london"]["edges"]],
                         [f"lon-edge-{index}" for index in range(1, 6)])
        self.assertEqual([node["name"] for node in regions["sydney"]["listeners"]],
                         [f"syd-load-{index}" for index in range(1, 17)])
        self.assertEqual([node["name"] for node in regions["tokyo"]["view"]], ["tok-view"])
        # The second curve: ten edges a region.
        self.assertEqual([len(item["edges"]) for item in scale.regions_of(lab(), []).values()],
                         [10] * 4)
        # Sydney left out.
        self.assertNotIn("sydney", scale.regions_of(lab(["sydney"]), STANDBY))

    def test_allocation_by_edges_vcpus_and_share(self):
        regions = scale.regions_of(lab(), STANDBY)
        alloc = scale.allocate(10000, regions, 0.5)
        per_site = {site: sum(item["udp"] + item["hls"] for item in hosts.values())
                    for site, hosts in alloc.items()}
        self.assertEqual(per_site, {"london": 2500, "newyork": 2500, "sydney": 2500,
                                    "tokyo": 2500})
        # Eight g6-dedicated-8 in London share evenly, sixteen g6-dedicated-4
        # in Sydney too, at half a g6-dedicated-8's share each.
        self.assertEqual({(item["udp"], item["hls"]) for item in alloc["london"].values()},
                         {(156, 156), (157, 157)})
        self.assertEqual(sum(alloc["london"]["lon-load-1"]["hls_by_edge"].values()),
                         alloc["london"]["lon-load-1"]["hls"])
        self.assertLessEqual(max(item["udp"] for item in alloc["sydney"].values()), 79)
        self.assertEqual(sum(item["hls"] for item in alloc["sydney"].values()), 1250)
        # Every edge of a region gets the same readers, to one.
        by_edge: dict[str, int] = {}
        for item in alloc["london"].values():
            for edge, count in item["hls_by_edge"].items():
                by_edge[edge] = by_edge.get(edge, 0) + count
        self.assertEqual(sum(by_edge.values()), 1250)
        self.assertLessEqual(max(by_edge.values()) - min(by_edge.values()), 1)
        # Twice the edges in London: twice London's share.
        wide = scale.allocate(1000, scale.regions_of(lab(), STANDBY[:5] + STANDBY[10:]), 0.5)
        self.assertEqual(sum(item["udp"] + item["hls"] for item in wide["london"].values()), 400)

    def test_hls_cap_moves_readers_to_udp(self):
        regions = scale.regions_of(lab(), STANDBY)
        alloc = scale.allocate(10000, regions, 0.5, hls_cap_per_host=100)
        for hosts in alloc.values():
            for item in hosts.values():
                self.assertLessEqual(item["hls"], 100)
        self.assertEqual(sum(item["udp"] + item["hls"] for hosts in alloc.values()
                             for item in hosts.values()), 10000)

    def test_plan_capacity_duration_and_cost(self):
        plan = scale.plan_steps(lab(), [100, 1000, 20000], "pcm-1", standby=STANDBY)
        self.assertEqual([step["listeners"] for step in plan["steps"]], [100, 1000, 20000])
        self.assertTrue(plan["steps"][0]["drivable"])
        self.assertTrue(plan["steps"][1]["drivable"])
        # Half LL-HLS at 1 ms parts is more than the listener hosts drive.
        self.assertFalse(plan["steps"][2]["drivable"])
        capacity = plan["capacity"]["lon-load-1"]
        self.assertEqual(capacity["vcpus"], 8)
        # 1 ms PCM: the measured CPU a datagram bounds UDP (about 1,000 per
        # g6-dedicated-8 within 70%), before the host's inbound.
        self.assertEqual(capacity["udp"], capacity["udp_by_cpu"])
        self.assertEqual(capacity["udp"], 1018)
        cost = scale.cost_estimate(lab(), plan)
        self.assertAlmostEqual(cost["hourly_usd"], 10.422, places=3)
        self.assertGreater(cost["listener_traffic_gb"], 0)
        # A cost file replaces the planning figures.
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "cost.json").write_text(json.dumps({"udp_vcpu": 0.0005, "other": 1}))
        costs = scale.load_costs(str(tmp / "cost.json"))
        self.assertEqual(costs, {"udp_vcpu": 0.0005, "hls_vcpu": scale.COSTS["hls_vcpu"],
                                 "lk_vcpu": scale.COSTS["lk_vcpu"]})
        # 2.5 ms parts cost 40% of 1 ms ones per listener.
        fast = scale.host_capacity({"type": "g6-dedicated-8"}, "pcm-1")
        slow = scale.host_capacity({"type": "g6-dedicated-8"}, "opus-2.5")
        self.assertAlmostEqual(slow["hls"], fast["hls"] * 2.5, delta=2)

    def test_steps_fit_the_listener_hosts_that_exist(self):
        # The account refused Tokyo's last six listener hosts at `up`.
        state = lab(["sydney"])
        missing = [f"tok-load-{index}" for index in range(3, 9)]
        state["listeners_missing"] = [missing_entry(state["nodes"].pop(name)) for name in missing]
        regions = scale.regions_of(state, STANDBY)
        plain = scale.plan_steps(state, [1000, 12000, 15000, 20000], "pcm-1", hls_share=0,
                                 standby=STANDBY)
        self.assertFalse(plain["steps"][1]["drivable"])
        plan = scale.plan_steps(state, [1000, 12000, 30000, 40000], "pcm-1", hls_share=0,
                                standby=STANDBY, fit=True)
        caps = plan["fit"]["caps"]
        # Two g6-dedicated-8 in Tokyo drive about 2,000 UDP listeners at 1 ms;
        # London and New York, eight each, about 8,000.
        self.assertEqual(caps["tokyo"], 2 * plan["capacity"]["tok-load-1"]["udp"])
        self.assertEqual(caps["london"], 8 * plan["capacity"]["lon-load-1"]["udp"])
        self.assertTrue(all(step["drivable"] for step in plan["steps"]))
        # Tokyo is clipped, the others keep their share; a step past every
        # region's hosts adds nothing and is dropped.
        first, second, third = plan["steps"]
        self.assertEqual(first["listeners"], 1000)
        self.assertEqual(second["requested"], 12000)
        self.assertEqual(second["clipped_regions"], {"tokyo": caps["tokyo"]})
        self.assertEqual(second["sites"]["london"]["udp"], 4000)
        self.assertEqual(second["listeners"], 8000 + caps["tokyo"])
        self.assertEqual(third["requested"], 30000)
        self.assertEqual(third["listeners"], sum(caps.values()))
        self.assertEqual(plan["fit"]["dropped"], [40000])
        self.assertEqual(plan["fit"]["listeners_missing"], missing)
        self.assertIn("tokyo", scale.fit_line(plan))
        # A lab that misses listener hosts is fitted by default, with --force
        # too (it only lets over-headroom steps run); --no-fit turns it off.
        self.assertTrue(scale.fit_wanted(argparse.Namespace(force=False), state))
        self.assertTrue(scale.fit_wanted(argparse.Namespace(force=True), state))
        self.assertFalse(scale.fit_wanted(argparse.Namespace(force=True, no_fit=True), state))
        self.assertFalse(scale.fit_wanted(argparse.Namespace(force=False), lab()))
        self.assertTrue(scale.fit_wanted(argparse.Namespace(force=False, fit_hosts=True), lab()))
        for name, cap in caps.items():
            one = {name: regions[name]}
            self.assertTrue(scale.drivable(scale.allocate(cap, one, 0), plan["capacity"]))
            self.assertFalse(scale.drivable(scale.allocate(cap + 1, one, 0), plan["capacity"]))

    def test_a_region_without_listener_hosts_keeps_its_share_and_gives_it_to_nobody(self):
        # A capacity refusal of Tokyo's g6-dedicated-8 in batch 1 blocks its
        # plan in batch 2 too: Tokyo keeps its core and has no listener host.
        state = lab(["sydney"])
        state["listeners_missing"] = [
            missing_entry(state["nodes"].pop(f"tok-load-{index}"), "capacity")
            for index in range(1, 9)]
        self.assertEqual(scale.idle_regions(state, STANDBY), {"tokyo": 5})
        self.assertNotIn("tokyo", scale.regions_of(state, STANDBY))
        whole = scale.plan_steps(lab(["sydney"]), [6000], "pcm-1", standby=STANDBY,
                                 lk_per_sfu=450)
        for fit in (True, False):
            plan = scale.plan_steps(state, [6000], "pcm-1", standby=STANDBY, fit=fit,
                                    lk_per_sfu=450)
            step = plan["steps"][0]
            # London and New York carry what they carry on the whole lab,
            # not Tokyo's share on top (2,305 each before).
            self.assertEqual({site: item["udp"] + item["hls"] for site, item in step["sites"].items()},
                             {"london": 2000, "newyork": 2000})
            self.assertEqual({site: item["udp"] + item["hls"]
                              for site, item in step["sites"].items()},
                             {site: item["udp"] + item["hls"]
                              for site, item in whole["steps"][0]["sites"].items()
                              if site != "tokyo"})
            self.assertEqual(step["listeners"], 4000)
            self.assertEqual(step["idle_regions"], {"tokyo": 2000})
            self.assertEqual(plan["idle_regions"], {"tokyo": 5})
            self.assertIn("no listener host in tokyo", scale.idle_line(plan))
        self.assertIsNone(scale.idle_line(whole))

    def test_the_documented_forced_ramp_is_fitted_and_as_far_over_as_on_the_whole_lab(self):
        # Curve 2's pcm-1 UDP-only ramp, run with --force as SCALE.md says,
        # on a lab whose second listener batch the account refused (half of
        # every region's hosts missing).
        steps = "100,1000,10000,20000,24000,28000,32000"
        whole = lab()
        short = lab()
        short["listeners_missing"] = [
            missing_entry(short["nodes"].pop(node["name"]))
            for node in lab_tool_batches(whole)[1]]
        self.assertEqual(len(short["listeners_missing"]), 20)
        args = argparse.Namespace(steps=steps, config="pcm-1", hls_share=0.0,
                                  hls_cap_per_host=None, hold=60.0, ramp=None, cost=None,
                                  udp_mbps=None, inbound_gbps=scale.LOAD_HOST_INBOUND_GBPS,
                                  lk_per_sfu=450, joins=0, joins_from=0, force=True,
                                  fit_hosts=False, no_fit=False)
        full = scale.build_plan(args, whole, [])
        fitted = scale.build_plan(args, short, [])
        self.assertIn("fit", fitted)
        # The whole lab: 32,000 is the one step over the headroom (106%).
        self.assertEqual([step["drivable"] for step in full["steps"]], [True] * 6 + [False])
        over = max(full["steps"][-1]["host_load"].values())
        self.assertGreater(over, 1.0)
        # Half the hosts: no host is loaded past the headroom or what the
        # whole lab would load it, and the forced step stays over the
        # headroom as planned, not clipped to it nor doubled. 24,000 and
        # 28,000 add nobody past 20,000 (clipped to the headroom) and go.
        planned = {step["listeners"]: step for step in full["steps"]}
        self.assertEqual([step["requested"] for step in fitted["steps"]],
                         [100, 1000, 10000, 20000, 32000])
        self.assertEqual(fitted["fit"]["dropped"], [24000, 28000])
        for step in fitted["steps"]:
            self.assertLessEqual(max(step["host_load"].values()), max(
                1.0, max(planned[step["requested"]]["host_load"].values())) + 1e-9)
        last = fitted["steps"][-1]
        self.assertEqual(last["requested"], 32000)
        self.assertGreater(max(last["host_load"].values()), 1.0)
        self.assertAlmostEqual(max(last["host_load"].values()), over, delta=0.01)
        self.assertTrue(last["fit_load_limit"])
        self.assertIn("as far over it as on the whole lab", scale.fit_line(fitted))
        # Unfitted (--no-fit), the same step puts about twice the load on
        # the hosts that exist: what the fit prevents.
        loose = scale.build_plan(argparse.Namespace(**{**vars(args), "no_fit": True}), short, [])
        self.assertNotIn("fit", loose)
        self.assertGreater(max(loose["steps"][-1]["host_load"].values()), 1.8 * over)
        # Without --force the fit keeps every host within the headroom.
        within = scale.build_plan(argparse.Namespace(**{**vars(args), "force": False}), short, [])
        self.assertTrue(all(step["drivable"] for step in within["steps"]))
        # On a whole lab --fit-hosts --force changes nothing.
        same = scale.build_plan(argparse.Namespace(**{**vars(args), "fit_hosts": True}), whole, [])
        self.assertEqual([step["listeners"] for step in same["steps"]],
                         [step["listeners"] for step in full["steps"]])

    def test_plan_command_offline(self):
        args = argparse.Namespace(
            topology_only=True, topology=str(TOPOLOGY), without_sites="sydney",
            second_curve=False, edges_per_region=None, steps="100,1000", config="opus-1", hls_share=0.5,
            hls_cap_per_host=None, hold=30.0, ramp=None, cost=None, udp_mbps=None,
            inbound_gbps=10.0, json=False)
        with redirect_stdout(io.StringIO()) as out:
            plan = scale.command_plan(args)
        self.assertNotIn("sydney", plan["regions"])
        self.assertIn("step   1000", out.getvalue())
        self.assertAlmostEqual(plan["cost"]["hourly_usd"], 10.422 - 2.538, places=3)


class Commands(unittest.TestCase):
    def test_settings_hold_the_standby_edges_for_the_first_curve(self):
        first = scale.scale_settings(json.loads(TOPOLOGY.read_text())["standby_edges"])
        self.assertEqual(first["standby_edges"], STANDBY)
        self.assertEqual(first["playback_address"], "vlan")
        self.assertIn("pcm", first["daw_hls_formats"])
        # A copy that a listener moves onto is warmed for a time, not a
        # count of parts: silence detection + the move bound + margin.
        self.assertEqual(first["cache_mesh_warm_ms"], 4500)
        self.assertGreaterEqual(first["cache_mesh_warm_ms"], 1000 + 3000)
        with redirect_stdout(io.StringIO()) as out:
            scale.command_settings(argparse.Namespace(topology=str(TOPOLOGY),
                                                      second_curve=True, edges_per_region=None))
        self.assertIn("--set standby_edges=[]", out.getvalue())
        # Two edges per region: the rest of each region on standby.
        with redirect_stdout(io.StringIO()) as out:
            scale.command_settings(argparse.Namespace(topology=str(TOPOLOGY),
                                                      second_curve=False, edges_per_region=2))
        standby = json.loads(out.getvalue().split("--set standby_edges=")[1])
        self.assertEqual(len(standby), 4 * 8)
        self.assertNotIn("lon-edge-2", standby)
        self.assertIn("syd-edge-3", standby)
        self.assertEqual(scale.standby_for(lab()["nodes"].values(), ["x"], None), ["x"])
        # Every setting is one the deploy tool knows.
        spec = importlib.util.spec_from_file_location(
            "deploy_for_scale", HERE.parent / "linode-small-mesh-deploy.py")
        deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy)
        self.assertTrue(set(first) <= set(deploy.DEFAULT_SETTINGS))

    def test_sender_follows_compare_audio(self):
        command = scale.sender_command("opus-1", "10.80.1.5", 600, "/var/lib/compare/audio/a.s24le")
        self.assertIn("--target 10.80.1.5:27100", command)
        self.assertIn("--session-id ${START}001000000", command)
        self.assertIn("--payload opus --epoch-samples 48", command)
        self.assertIn("--opus-bitrate 320000", command)
        pcm = scale.sender_command("pcm-1", "10.80.1.5", 600, "/x")
        self.assertNotIn("--opus-bitrate", pcm)
        self.assertIn("--session-id ${START}001000000", pcm)
        self.assertIn("${START}002500000", scale.sender_command("opus-2.5", "h", 1, "/x"))

    def test_ramp_rises_in_stages_to_the_count(self):
        self.assertEqual(scale.ramp_spec(1000, 20, 4), "250@0,500@5,750@10,1000@15")
        self.assertEqual(scale.ramp_spec(3, 10), "1@0,2@3.33333,3@6.66667")
        self.assertTrue(scale.ramp_spec(5000, 50).endswith(",5000@45"))

    def test_load_command(self):
        alloc = {"udp": 700, "hls": 0, "hls_by_edge": {}}
        command = scale.load_command(alloc, ["10.80.1.21:19446", "10.80.1.22:19446"], "pcm-1",
                                     20, 80, 20, "/r/7", "run-1000-lon-load-1", "10.80.1.101",
                                     threads=8)
        for part in ("needletail-load ", "--edges 10.80.1.21:19446,10.80.1.22:19446",
                     "--stream 4001", "--listeners 700", "--duration 80", "--sample 20",
                     "--bind 10.80.1.101", "--threads 8", "--out /r/7/udp", "--quiet", "ulimit -n 1048576"):
            self.assertIn(part, command)
        self.assertEqual(scale.load_command({"udp": 0}, [], "pcm-1", 1, 1, 1, "/r"), "")
        self.assertIn("--sample 5 ", scale.load_command({"udp": 5}, ["a:1"], "pcm-1", 1, 1, 20,
                                                        "/r"))

    def test_hls_commands_pin_to_edges_or_follow_steering(self):
        alloc = {"udp": 0, "hls": 5, "hls_by_edge": {"lon-edge-1": 3, "lon-edge-2": 2}}
        spread = scale.hls_commands(alloc, "10.80.1.11", "pcm-1", 10, 70, "/r", "L")
        self.assertEqual(len(spread), 2)
        self.assertIn("&cache=lon-edge-1'", spread[0])
        self.assertIn("--count 3", spread[0])
        self.assertIn("https://10.80.1.11:19444/live/4001/master.m3u8?sid=L-lon-edge-1-{i}",
                      spread[0])
        self.assertIn("--audio-index /var/lib/compare/index/nt-pcm-48s.index.json", spread[0])
        self.assertIn("--http3", spread[0])
        steered = scale.hls_commands(alloc, "10.80.1.11", "opus-1", 10, 70, "/r", "L",
                                     http3=False, steering="steered")
        self.assertEqual(len(steered), 1)
        self.assertIn("--count 5", steered[0])
        self.assertNotIn("cache=", steered[0])
        self.assertNotIn("--http3", steered[0])
        self.assertEqual(scale.hls_commands({"hls": 0}, "e", "pcm-1", 1, 1, "/r", "L"), [])

    def test_step_commands_use_vlan_addresses_only(self):
        state = lab()
        plan = scale.plan_steps(state, [1000], "pcm-1", standby=STANDBY)
        args = argparse.Namespace(config="pcm-1", sample=10, load_args="", hls_window=40,
                                  hls_http2=False, hls_steering="spread")
        commands = scale.step_commands(state, plan, plan["steps"][0], "r1", args)
        self.assertEqual(len(commands), 3 * 8 + 16)
        public = {node["public_ipv4"] for node in state["nodes"].values()}
        for name, command in commands.items():
            self.assertFalse([address for address in re.findall(r"[0-9.]+", command)
                              if address in public], name)
            self.assertNotIn("lon-edge-6", command)
            # The shell takes it: a host runs needletail-load and several
            # ntcmp crowds, each sent to the background.
            checked = subprocess.run(["bash", "-n", "-c", command], capture_output=True,
                                     text=True)
            self.assertEqual(checked.returncode, 0, f"{name}: {checked.stderr}")
        self.assertIn("10.80.1.21:19446", commands["lon-load-1"])
        self.assertIn("ntcmp hls", commands["lon-load-1"])
        self.assertIn("needletail-load", commands["lon-load-1"])

    def test_vlan_guard(self):
        state = lab()
        regions = scale.regions_of(state, STANDBY)
        self.assertEqual(scale.check_vlan(state, regions, False), [])
        state["nodes"]["lon-load-1"]["vlan_ipv4"] = None
        with self.assertRaises(SystemExit):
            scale.check_vlan(state, scale.regions_of(state, STANDBY), False)
        self.assertEqual(len(scale.check_vlan(state, scale.regions_of(state, STANDBY), True)), 1)


class Sampler(unittest.TestCase):
    def test_the_sampler_runs_and_its_rows_summarize(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        script = tmp / "sampler.py"
        script.write_text(scale.SAMPLER)
        out = tmp / "s.jsonl"
        subprocess.run([sys.executable, "-I", str(script), str(out), "2.5", "127.0.0.1"],
                       check=True, timeout=30)
        header, rows = scale.sampler_rows(out.read_text())
        self.assertEqual(header["public_iface"], "lo")
        self.assertGreaterEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(0.0 <= row["cpu"] <= 1.0)
            self.assertIn("RcvbufErrors", row["udp"])
            self.assertIn("rss_mb", row)
        summary = scale.summarize_samples(header, rows, node_type="g6-dedicated-2")
        self.assertEqual(summary["samples"], len(rows))
        self.assertIsNone(summary["saturated_at"])

    def test_saturation_is_held_for_three_seconds(self):
        def row(t, cpu, drops=0, tx=0):
            return {"t": t, "cpu": cpu, "core_max": cpu, "rx": {}, "tx": {"eth0": tx},
                    "udp": {"RcvbufErrors": drops}}
        rows = [row(1, 0.95), row(2, 0.5), row(3, 0.95), row(4, 0.96), row(5, 0.97)]
        self.assertEqual(scale.first_held(rows, lambda r: r["cpu"] >= 0.9), 3)
        summary = scale.summarize_samples({"public_iface": "eth0"}, rows)
        self.assertEqual((summary["saturated_at"], summary["saturated_by"]), (3, "cpu"))
        self.assertEqual(scale.load_saturated(rows, []), "cpu 0.95 for 3 s")
        drops = [row(t, 0.2, 500) for t in range(1, 5)]
        self.assertIn("receive-buffer", scale.load_saturated(drops, []))
        self.assertEqual(scale.summarize_samples({}, drops)["saturated_by"], "udp_rcvbuf_drops")
        # 3.7 Gbit/s out of a g6-dedicated-2's 4 is its NIC.
        nic = [row(t, 0.3, tx=3_700_000_000 // 8) for t in range(1, 5)]
        self.assertEqual(scale.summarize_samples({}, nic, node_type="g6-dedicated-2")
                         ["saturated_by"], "nic_out")
        self.assertIsNone(scale.summarize_samples({}, nic)["saturated_at"])
        # The tool's own verdict, held for three intervals (its rows of one
        # interval, per edge and in total, are one verdict).
        def line(t, saturated, edge=None):
            return {"t": t, "edge": edge, "saturated": saturated,
                    "reasons": ["receive-lag"] if saturated else []}
        held = [line(t, True, edge) for t in (1, 2, 3) for edge in ("a", None)]
        self.assertEqual(scale.load_saturated([row(1, 0.1)], held),
                         "needletail-load: receive-lag for 3 intervals")
        self.assertIsNone(scale.load_saturated([row(1, 0.1)], [line(1, False)]))
        # One interval's verdict (listeners joining, a few drops) is not.
        once = [line(1, False), line(2, True), line(2, True, "a"), line(3, False)]
        self.assertIsNone(scale.load_saturated([row(1, 0.1)], once))
        self.assertIsNone(scale.load_saturated_at(once))
        # In the step's window only, the final line after the drain aside.
        late = [line(t, t >= 20) for t in range(10, 30)]
        self.assertEqual(scale.load_saturated_at(late, 10, 30), 20)
        self.assertIsNone(scale.load_saturated_at(late, 10, 21))
        self.assertIsNone(scale.load_saturated_at(
            [line(1, True), line(2, True), {**line(3, True), "phase": "final"}]))


def block(listeners, parts, missing, buckets, ttfp_buckets, p50, p99):
    return {"up": listeners, "parts": parts, "missing": missing, "recovered": 3,
            "nack_parts": 2,
            "latency": {"count": sum(c for _, c in buckets), "p50": p50, "p99": p99,
                        "max": buckets[-1][0], "buckets": buckets},
            "ttfp": {"count": sum(c for _, c in ttfp_buckets), "p50": 40000, "p99": 90000,
                     "max": ttfp_buckets[-1][0], "buckets": ttfp_buckets}}


class LoadFiles(unittest.TestCase):
    def test_histograms_merge_exactly_across_hosts(self):
        a = block(10, 900, 0, [[1000, 90], [2000, 9], [9000, 1]], [[40000, 10]], 1000, 2000)
        b = block(10, 900, 100, [[1500, 50], [3000, 50]], [[50000, 10]], 3000, 3000)
        views = [scale.block_view(a), scale.block_view(b)]
        self.assertEqual(views[0]["latency_ms"]["p50"], 1.0)  # µs block with buckets
        merged = scale.merge_views(views)
        self.assertEqual(merged["latency_ms"]["merge"], "histogram")
        self.assertEqual(merged["latency_ms"]["count"], 200)
        self.assertEqual(merged["latency_ms"]["p50"], 1.5)   # rank 100 of 200
        self.assertEqual(merged["latency_ms"]["p99"], 3.0)
        self.assertEqual(merged["latency_ms"]["max"], 9.0)
        self.assertEqual(merged["listeners"], 20)
        self.assertAlmostEqual(merged["missing_percent"], 100 * 100 / 1900, places=3)
        self.assertEqual(merged["ttfp_ms"]["p50"], 40.0)

    def test_needletail_load_summary_blocks(self):
        # As av_mesh::load writes them: percentiles in ms, bucket lows in µs.
        a = {"up": 5, "parts": 100, "missing": 0,
             "latency_ms": {"count": 100, "p50": 1.016, "p99": 2.032, "max": 2.05,
                            "buckets_us": [[100, 1], [1008, 98], [2016, 1]]},
             "ttfp_ms": {"count": 5, "p50": 30.0, "p99": 31.0, "max": 31.0,
                         "buckets_us": [[29696, 5]]}}
        view = scale.block_view(a)
        self.assertEqual(view["latency_ms"]["p50"], 1.016)
        # 1008 is in [512, 1024): buckets 8 µs wide, middle 1011; 100 is exact.
        self.assertEqual(view["histogram"], {100.0: 1, 1011.0: 98, 2023.0: 1})
        self.assertEqual(scale.load_bucket_middle(127), 127.0)
        self.assertEqual(scale.load_bucket_middle(128), 128.0)
        self.assertEqual(scale.load_bucket_middle(256), 257.0)
        merged = scale.merge_views([view, scale.block_view(a)])
        self.assertEqual(merged["latency_ms"]["p50"], 1.011)
        self.assertEqual(merged["latency_ms"]["count"], 200)
        self.assertEqual(merged["ttfp_ms"]["merge"], "histogram")

    def test_without_histograms_the_worst_host_bounds_the_tail(self):
        views = [scale.block_view({"up": 10, "parts": 10, "missing": 0,
                                   "latency_ms": {"p50": 1.0, "p99": 2.0, "max": 3.0}}),
                 scale.block_view({"up": 30, "parts": 10, "missing": 0,
                                   "latency_ms": {"p50": 2.0, "p99": 5.0, "max": 6.0}})]
        merged = scale.merge_views(views)
        self.assertEqual(merged["latency_ms"], {"merge": "worst-host", "p50": 1.75, "p99": 5.0,
                                                "max": 6.0})
        # Keys that name their unit.
        self.assertEqual(scale.block_view({"latency_us": {"p50": 2500}})["latency_ms"],
                         {"p50": 2.5})

    def test_interval_rows_of_the_load_tool(self):
        line = {"schema": "needletail.load.interval.v1", "unix_ms": 1_763_800_000_000,
                "t_s": 3, "phase": "run",
                "total": {"up": 20, "parts": 2000, "missing": 30},
                "edges": [{"edge": "10.80.1.21:19446", "up": 10, "parts": 1000, "missing": 0},
                          {"edge": "10.80.1.22:19446", "up": 10, "parts": 1000, "missing": 30}],
                "host": {"cpu_host_busy_percent": 55.0, "udp_rcvbuf_errors": 4},
                "load_host_saturated": True, "load_host_reasons": ["cpu"]}
        rows = scale.interval_rows([line])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["t"], 1_763_800_000.0)
        self.assertEqual(rows[1]["missing"], 30)
        self.assertEqual(rows[2]["edge"], None)
        self.assertEqual(rows[0]["host_cpu"], 55.0)
        self.assertTrue(rows[0]["saturated"])
        # The tool's shape now: edges by name, the host under "load_host".
        now = scale.interval_rows([{
            "schema": "needletail.load.interval.v1", "t": 4.0, "unix_ms": 1_763_800_004_000,
            "load_host_saturated": True, "total": {"up": 7, "parts": 70},
            "edges": {"10.80.1.21:19446": {"up": 7, "active": 7, "parts": 70, "missing": 1}},
            "load_host": {"host_cpu_busy": 0.93, "udp_rcvbuf_errors": 0,
                          "saturation": ["worker lag p99 8 ms"]}}])
        self.assertEqual([(row["edge"], row["listeners"]) for row in now],
                         [("10.80.1.21:19446", 7), (None, 7)])
        self.assertEqual(now[0]["t"], 1_763_800_004.0)
        self.assertEqual(now[0]["host_cpu"], 0.93)
        self.assertEqual(now[0]["reasons"], ["worker lag p99 8 ms"])
        # An earlier draft's shape: edges by name, counters under other names.
        old = scale.interval_rows([{"t": 5, "edges": {"e": {"listeners_up": 3,
                                                             "parts_received": 7}}}])
        self.assertEqual((old[0]["listeners"], old[0]["parts"]), (3, 7))

    def test_summary_blocks(self):
        total, edges = scale.summary_blocks({"listeners_peak": 40, "total": {"parts": 1},
                                             "per_edge": [{"edge": "a:1", "parts": 1}]})
        self.assertEqual(total["listeners"], 40)
        self.assertEqual(list(edges), ["a:1"])


class Hls(unittest.TestCase):
    def test_region_figures_of_the_readers(self):
        rows = [{"audio_packets": 990, "audio_sequence_gaps": 10, "first_audio_ms": 300,
                 "audio_latency_ms": {"count": 990, "p50": 20.0, "p99": 30.0, "max": 40.0},
                 "published_to_complete": {"count": 990, "p50": 5.0, "p99": 9.0, "max": 12.0},
                 "refused": 2},
                {"audio_packets": 1000, "audio_sequence_gaps": 0, "first_audio_ms": 500,
                 "audio_latency_ms": {"count": 1000, "p50": 22.0, "p99": 60.0, "max": 80.0}},
                {"error": "connect: refused", "audio_packets": 0, "first_audio_ms": -1}]
        region = scale.hls_region(rows)
        self.assertEqual(region["readers"], 3)
        self.assertEqual(region["errors"], 1)
        self.assertEqual(region["refused_requests"], 2)
        self.assertEqual(region["missing_percent"], 0.5)
        self.assertEqual(region["latency_ms"]["p50"], 22.0)
        self.assertEqual(region["latency_ms"]["p99"], 60.0)
        self.assertEqual(region["publish_to_arrival_ms"]["p50"], 5.0)
        self.assertEqual(region["ttfa_ms"], {"p50": 500.0, "p99": 500.0})
        self.assertIsNone(scale.hls_region([])["latency_ms"])

    def test_condense_reads_ntcmp_reports(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "hls").mkdir()
        (tmp / "hls" / "reader-lon-edge-1-000.json").write_text(json.dumps({
            "audio_packets": 5, "audio_latency_ms": {"count": 5, "p50": 1, "p99": 2, "max": 3},
            "detail": {"counts": {"part_error_503": 4, "rejoin": 1},
                       "server_timing_ms": {"published_to_complete": {"count": 5}}}}))
        (tmp / "hls" / "reader-lon-edge-1-001.json").write_text("{broken")
        script = tmp / "condense.py"
        script.write_text(scale.HLS_CONDENSE)
        subprocess.run([sys.executable, "-I", str(script), str(tmp)], check=True,
                       capture_output=True)
        rows = json.loads((tmp / "hls.condensed.json").read_text())
        self.assertEqual(rows[0]["refused"], 4)
        self.assertEqual(rows[0]["published_to_complete"], {"count": 5})
        self.assertEqual(rows[1]["error"], "unreadable")


class Analysis(unittest.TestCase):
    def test_gave_out_first_is_the_earliest(self):
        state = lab()
        nodes = {"lon-edge-1": {"saturated_at": 105.0, "saturated_by": "cpu"},
                 "lon-dist-a": {"saturated_at": 110.0, "saturated_by": "cpu"},
                 "lon-view": {"saturated_at": 100.0, "saturated_by": "cpu"}}
        first = scale.gave_out_first(state, nodes, {"new-edge-2": 104.0}, {"lon-load-1": None})
        self.assertEqual((first["tier"], first["node"], first["reason"]),
                         ("edge", "new-edge-2", "listeners_missing_parts"))
        self.assertEqual([item["node"] for item in first["others"]], ["lon-edge-1", "lon-dist-a"])
        first = scale.gave_out_first(state, nodes, {}, {"syd-load-3": 90.0})
        self.assertEqual((first["tier"], first["node"]), ("load", "syd-load-3"))
        self.assertIsNone(scale.gave_out_first(state, {}, {}, {}))

    def test_a_step_from_its_files(self):
        state = lab()
        plan = scale.plan_steps(state, [100], "pcm-1", standby=STANDBY)
        step = plan["steps"][0]
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        t0 = 1_763_800_000.0

        def sampler(name, cpu, tx=1000, procs=None, rss=None):
            directory = tmp / name
            directory.mkdir(exist_ok=True)
            lines = [json.dumps({"public_iface": "eth0", "cpus": 2})]
            lines += [json.dumps({"t": t0 + i, "cpu": cpu, "core_max": cpu,
                                  "rx": {"eth0": 10, "eth1": 10}, "tx": {"eth0": tx, "eth1": tx},
                                  "udp": {"RcvbufErrors": 0, "SndbufErrors": 0},
                                  "procs": procs or {}, "rss_mb": rss or {}})
                      for i in range(30)]
            (directory / "sampler.jsonl").write_text("\n".join(lines) + "\n")

        for name in state["nodes"]:
            sampler(name, 0.2)
        sampler("tok-edge-3", 0.97)
        lon1 = step["allocation"]["london"]["lon-load-1"]
        # 0.05 cores for the UDP listeners, 0.3 for the readers of lon-load-1.
        sampler("lon-load-1", 0.2, procs={"needletail-load": 0.005 * lon1["udp"],
                                          "ntcmp": 0.03 * lon1["hls"]},
                rss={"needletail-load": 0.5 * lon1["udp"], "ntcmp": 2.0 * lon1["hls"]})
        for site, hosts in step["allocation"].items():
            for host in hosts:
                udp = tmp / host / "udp"
                udp.mkdir(parents=True)
                edge = scale.regions_of(state, STANDBY)[site]["edges"][0]
                address = f"{edge['vlan_ipv4']}:19446"
                (udp / "summary.json").write_text(json.dumps({
                    "listeners_peak": 6,
                    "total": block(6, 6000, 6, [[1000, 98], [4000, 2]], [[30000, 6]], 1000, 4000),
                    "per_edge": [{"edge": address,
                                  **block(6, 6000, 6, [[1000, 98], [4000, 2]], [[30000, 6]],
                                          1000, 4000)}]}))
                (udp / "intervals.jsonl").write_text("\n".join(json.dumps({
                    "unix_ms": int((t0 + i) * 1000), "edges": [
                        {"edge": address, "up": 6, "parts": 6000, "missing": 0}],
                    "load_host_saturated": False}) for i in range(20))
                    # The line after the drain: the holes left at the end are
                    # not an interval of the edge's.
                    + "\n" + json.dumps({"unix_ms": int((t0 + 21) * 1000), "phase": "final",
                                         "edges": [{"edge": address, "up": 6, "parts": 1,
                                                    "missing": 5}]}) + "\n")
                (tmp / host / "hls.condensed.json").write_text(json.dumps([
                    {"audio_packets": 1000, "audio_sequence_gaps": 0, "first_audio_ms": 200,
                     "audio_latency_ms": {"count": 1000, "p50": 25.0, "p99": 31.0, "max": 40}}]))
        (tmp / "counters.json").write_text(json.dumps({"lon-edge-1": {"subscriptions": 12}}))
        result = scale.analyse_step(state, tmp, step, (t0 + 5, t0 + 25))
        london = result["regions"]["london"]
        self.assertEqual(london["udp"]["latency_ms"]["p50"], 1.0)
        self.assertEqual(london["udp"]["latency_ms"]["p99"], 4.0)
        self.assertEqual(london["udp"]["listeners"], 6 * len(step["allocation"]["london"]))
        self.assertEqual(london["hls"]["latency_ms"]["p50"], 25.0)
        self.assertEqual(result["edge_listeners"]["lon-edge-1"],
                         6 * len(step["allocation"]["london"]))
        self.assertEqual(result["tiers"]["edge"]["nodes"], 20 + 20)  # standby edges sampled too
        self.assertEqual(result["gave_out_first"]["node"], "tok-edge-3")
        self.assertNotIn("listeners_missing_parts",
                         [item["reason"] for item in result["gave_out_first"]["others"]])
        costs = result["measured_costs"]
        self.assertAlmostEqual(costs["udp_process_vcpu"], 0.005)
        self.assertAlmostEqual(costs["udp_vcpu"], 0.005 + scale.SOFTIRQ_VCPU)
        self.assertAlmostEqual(costs["hls_vcpu"], 0.03 + scale.SOFTIRQ_VCPU)
        self.assertAlmostEqual(costs["hls_mb"], 2.0)
        self.assertEqual(list(costs["hosts"]), ["lon-load-1"])
        self.assertEqual(scale.cost_file({"steps": [result]}),
                         {"udp_vcpu": costs["udp_vcpu"], "udp_vcpu_step": 100,
                          "hls_vcpu": costs["hls_vcpu"], "hls_vcpu_step": 100})
        self.assertEqual(result["edge_counters"]["lon-edge-1"]["subscriptions"], 12)
        self.assertGreater(result["public_transfer_gb"], 0)
        text = scale.format_summary({"run": "t", "config": "pcm-1", "hls_share": 0.5,
                                     "steps": [result]})
        self.assertIn("| 100 | london | 1 | 4 |", text)
        self.assertIn("edge (tok-edge-3, cpu)", text)
        self.assertIn("| 100 | 0.0065 | 0.5 | 0.0315 | 2 |", text)

    def test_a_saturated_load_host_is_not_an_edge_that_gave_out(self):
        state = lab()
        step = scale.plan_steps(state, [100], "pcm-1", standby=STANDBY)["steps"][0]
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        t0 = 1_763_800_000.0
        edge = scale.regions_of(state, STANDBY)["london"]["edges"][0]
        address = f"{edge['vlan_ipv4']}:19446"

        def write(host, saturated_from, missing):
            udp = tmp / host / "udp"
            udp.mkdir(parents=True)
            lines = []
            for i in range(30):
                saturated = saturated_from is not None and i >= saturated_from
                lines.append(json.dumps({
                    "schema": "needletail.load.interval.v1", "phase": "run", "t": i + 1.0,
                    "unix_ms": int((t0 + i) * 1000), "load_host_saturated": saturated,
                    "total": {"up": 10, "active": 10, "parts": 10_000,
                              "missing": missing if saturated else 0},
                    "edges": {address: {"up": 10, "active": 10, "parts": 10_000,
                                        "missing": missing if saturated else 0}},
                    "load_host": {"saturation": ["nic-drops"] if saturated else []}}))
            (udp / "intervals.jsonl").write_text("\n".join(lines) + "\n")
            # needletail-load's summary as it writes it.
            (udp / "summary.json").write_text(json.dumps({
                "schema": "needletail.load.v1", "peak_listeners": 10, "peak_subscribed": 10,
                "total": {"listeners_started": 10, "parts": 300_000, "missing": 0},
                "per_edge": {address: {"listeners_started": 10, "parts": 300_000,
                                       "missing": 0}}}))

        # lon-load-1 drops a fifth of its parts from t0 + 12 on (its NIC);
        # lon-load-2, on the same edge, misses nothing.
        write("lon-load-1", 12, 2_000)
        write("lon-load-2", None, 0)
        result = scale.analyse_step(state, tmp, step, (t0 + 5, t0 + 28))
        first = result["gave_out_first"]
        self.assertEqual((first["tier"], first["node"], first["reason"]),
                         ("load", "lon-load-1", "load_tool_saturated"))
        self.assertEqual(first["t"], t0 + 12)
        self.assertNotIn("listeners_missing_parts",
                         [item["reason"] for item in first["others"]])
        # The listeners of the region and of the edge, from the summaries.
        self.assertEqual(result["regions"]["london"]["udp"]["listeners"], 20)
        self.assertEqual(result["regions"]["london"]["edges"][edge["name"]]["listeners"], 20)
        # The same misses from a host that kept up are the edge's.
        shutil.rmtree(tmp / "lon-load-1")
        shutil.rmtree(tmp / "lon-load-2")
        write("lon-load-1", None, 0)
        write("lon-load-2", None, 0)
        rows = (tmp / "lon-load-2" / "udp" / "intervals.jsonl").read_text().splitlines()
        rows = [json.loads(line) for line in rows]
        for line in rows[12:]:
            line["edges"][address]["missing"] = 2_000
        (tmp / "lon-load-2" / "udp" / "intervals.jsonl").write_text(
            "\n".join(json.dumps(line) for line in rows) + "\n")
        first = scale.analyse_step(state, tmp, step, (t0 + 5, t0 + 28))["gave_out_first"]
        self.assertEqual((first["tier"], first["node"], first["reason"]),
                         ("edge", edge["name"], "listeners_missing_parts"))

    def test_the_monitor_outlives_a_host_that_does_not_answer(self):
        hosts = [{"name": f"lon-load-{index}"} for index in range(1, 4)]
        calls = []

        def fake(node, command, check=False, timeout=None):
            calls.append(node["name"])
            if node["name"] == "lon-load-2":
                raise subprocess.TimeoutExpired("ssh", timeout)
            return ""

        with mock.patch.object(scale.common, "ssh_text", fake):
            found = scale.monitor(hosts, "/r", scale.time.time() + 0.3, 0.1, lambda item: None)
        self.assertEqual(found, (None, None))
        self.assertIn("lon-load-2", calls)

    def test_counter_delta(self):
        self.assertEqual(scale.counter_delta({"e": {"parts": 5, "fast_lane": None}},
                                             {"e": {"parts": 9, "fast_lane": {"x": 1}},
                                              "f": None}),
                         {"e": {"parts": 4}, "f": None})


PHASES = scale.PHASES


class Phases(unittest.TestCase):
    """The failover, redistribution and autoscale phases (items 4 and 5)."""

    def test_the_committed_plan_loads_and_disabled_scenarios_run_only_by_name(self):
        plan = scale.load_phase_plan(PHASES, "F")
        names = [item["name"] for item in plan["scenarios"]]
        self.assertEqual(names, ["F1", "F2", "F2r", "F3", "F4", "F5", "F6", "F7", "F8"])
        self.assertNotIn("F9", names)  # waits for AM item 11
        self.assertEqual([item["name"] for item in scale.load_phase_plan(
            PHASES, names=["F9"])["scenarios"]], ["F9"])
        self.assertEqual([item["name"] for item in scale.load_phase_plan(PHASES, "R")[
            "scenarios"]], ["R1", "R2", "R3", "R5"])
        self.assertEqual([item["name"] for item in scale.load_phase_plan(PHASES, "A")[
            "scenarios"]], ["A1", "A2"])
        every = json.loads(PHASES.read_text())["scenarios"]
        # At most 2 of a region's 5 serving edges at once (D5), and never
        # the contributor.
        for item in every:
            if item["name"] == "F9":
                continue
            targets = [target for action in item["actions"] for target in action.get("targets", [])]
            for short in ("lon", "new", "tok", "syd"):
                edges = {target for target in targets if target.startswith(f"{short}-edge-")}
                self.assertLessEqual(len(edges), 2, item["name"])
            self.assertNotIn("contrib-lon", targets)
        sheds = [item["shed"] for item in every if item["shed"]]
        self.assertEqual(len(sheds), len(set(sheds)))
        self.assertEqual(min(every, key=lambda item: item["shed"] or 99)["name"], "F8")
        bad = json.loads(PHASES.read_text())
        bad["scenarios"][0]["actions"][0]["kind"] = "reboot"
        path = pathlib.Path(tempfile.mkdtemp()) / "bad.json"
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            scale.load_phase_plan(path, "F")

    def test_timeline_kill_is_node_loss_and_every_fault_is_restored(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "F"))
        rows = {row["name"]: row for row in timeline["scenarios"]}
        self.assertEqual((rows["F1"]["base_start"], rows["F1"]["fault"],
                          rows["F1"]["observe_end"], rows["F1"]["end"]), (0, 60, 150, 180))
        self.assertEqual(rows["F2"]["fault"], 240)
        self.assertEqual(timeline["hold_s"], 9 * 180)
        self.assertEqual([(item["kind"], item["targets"]) for item in rows["F2"]["restores"]],
                         [("start", ["new-edge-2"])])
        self.assertEqual([(item["kind"], item["t"]) for item in rows["F1"]["restores"]],
                         [("resume", 150)])
        self.assertEqual(rows["F2r"]["restores"], [])  # a crash restarts by itself
        self.assertEqual([item["kind"] for item in rows["F3"]["restores"]], ["start"])
        self.assertEqual(rows["F3"]["actions"][1]["t"], rows["F3"]["fault"] + 20)
        self.assertIn("systemctl stop", scale.NODE_COMMANDS["kill"])
        self.assertNotIn("systemctl stop", scale.NODE_COMMANDS["crash"])
        r4 = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["R4"]))["scenarios"][0]
        self.assertEqual(r4["observe_end"] - r4["fault"], 120 + 90)
        self.assertEqual([item["kind"] for item in r4["restores"]], ["leave"])

    def test_entries_and_liveness_avoid_the_nodes_under_fault(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "F"))
        avoid = scale.fault_nodes(timeline)
        regions = scale.regions_of(lab(), STANDBY)
        london = [node["name"] for node in scale.entry_nodes(regions["london"], avoid)]
        self.assertEqual(london, ["lon-edge-5"])
        self.assertEqual(scale.live_node(lab(), avoid)["name"], "new-dist-a")
        self.assertEqual([node["name"] for node in scale.entry_nodes(regions["tokyo"], avoid)][:1],
                         ["tok-edge-1"])
        with self.assertRaises(ValueError):
            scale.entry_nodes(regions["tokyo"], {node["name"] for node in
                                                 regions["tokyo"]["distributors"]
                                                 + regions["tokyo"]["edges"]})

    def test_phase_steps_use_steered_readers_at_entries_not_under_fault(self):
        state = lab()
        plan = scale.plan_steps(state, [8000], "pcm-1", 0.05, standby=STANDBY)
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "F"))
        args = argparse.Namespace(config="pcm-1", sample=20, load_args="", hls_window=40,
                                  hls_http2=False, hls_steering="spread")
        commands = scale.step_commands(state, plan, plan["steps"][0], "r", args,
                                       scale.fault_nodes(timeline), readers=True,
                                       timeline=timeline)
        text = "\n".join(commands.values())
        self.assertIn("needletail-reader", text)
        self.assertIn("--steering", text)
        self.assertNotIn("ntcmp hls", text)
        self.assertNotIn("cache=", text)
        london = "\n".join(command for name, command in commands.items()
                           if name.startswith("lon-"))
        lon_dist_a = state["nodes"]["lon-dist-a"]["vlan_ipv4"]
        self.assertNotIn(f"https://{lon_dist_a}:", london)
        self.assertIn(f"https://{state['nodes']['lon-edge-5']['vlan_ipv4']}:", london)
        for address in re.findall(r"https://([0-9.]+):", text):
            self.assertTrue(address.startswith("10.80."), address)

    def test_the_autoscale_profile_ramps_the_listeners_up_and_down(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "A"))
        spec = scale.profile_ramp(1000, 10.0, timeline)
        points = [(float(at), int(count)) for count, at in
                  (item.split("@") for item in spec.split(","))]
        self.assertEqual(points, sorted(points))
        by_time = dict((at, count) for at, count in points)
        self.assertEqual(by_time[10.0 + 60], 300)        # A1 starts at 30%
        self.assertEqual(by_time[10.0 + 540], 950)       # 95% after 8 min
        self.assertEqual(points[-1], (10.0 + 1110, 200))  # A2 ends at 20%
        self.assertTrue(max(count for _at, count in points) <= 950)
        self.assertEqual(scale.profile_ramp(100, 10.0, None), scale.ramp_spec(100, 10.0))

    def test_faults_do_not_count_as_a_tier_giving_out(self):
        state = lab()
        nodes = {"tok-dist-a": {"saturated_at": 1000.0, "saturated_by": "cpu"},
                 "lon-load-1": {"saturated_at": 1500.0, "saturated_by": "cpu"},
                 "new-dist-a": {"saturated_at": 1010.0, "saturated_by": "cpu"}}
        exclude = {"windows": [(990.0, 1100.0)],
                   "node_windows": {"new-edge-2": [(990.0, 1100.0)],
                                    "tok-dist-a": [(990.0, 1100.0)]}}
        first = scale.gave_out_first(state, nodes, {"new-edge-2": 995.0, "lon-edge-1": 1200.0},
                                     {}, exclude)
        # The killed node and anything inside the fault window are left
        # out; a listener host's saturation and a later edge are not.
        self.assertEqual((first["tier"], first["node"]), ("edge", "lon-edge-1"))
        self.assertEqual([item["node"] for item in first["others"]], ["lon-load-1"])
        first = scale.gave_out_first(state, nodes, {}, {})
        self.assertEqual(first["node"], "tok-dist-a")
        # A node faulted later in the step is judged like any other before
        # its own fault.
        first = scale.gave_out_first(state, nodes, {"new-edge-2": 500.0}, {}, exclude)
        self.assertEqual((first["node"], first["t"]), ("new-edge-2", 500.0))

    def test_only_faults_are_excluded_and_each_node_only_in_its_own(self):
        start = 10_000.0
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "A"))
        # An autoscale ramp faults nothing: an edge that saturates as A1
        # ramps to 95% is reported.
        exclude = scale.exclusion(timeline, start)
        self.assertEqual(exclude, {"windows": [], "node_windows": {}})
        first = scale.gave_out_first(lab(), {}, {"lon-edge-1": start + 500}, {}, exclude)
        self.assertEqual(first["node"], "lon-edge-1")
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "F"))
        rows = {row["name"]: row for row in timeline["scenarios"]}
        exclude = scale.exclusion(timeline, start)
        self.assertEqual(len(exclude["windows"]),
                         sum(1 for row in timeline["scenarios"] if row["targets"]))
        target = rows["F8"]["targets"][0]
        windows = exclude["node_windows"][target]
        self.assertIn([start + rows["F8"]["fault"], start + rows["F8"]["end"]],
                      [list(window) for window in windows])
        # Between scenarios (a settle's end to the next baseline's end is
        # not under any fault) the F8 target is judged.
        quiet = start + rows["F1"]["base_start"] + 10
        self.assertFalse(scale.excluded(exclude, target, quiet))
        self.assertTrue(scale.excluded(exclude, target, start + rows["F8"]["fault"] + 5))

    def test_stranded_listeners(self):
        rows = [{"t": 100.0 + i, "edge": None, "listeners": 90 if 20 <= i < 27 else 100,
                 "active": 100, "phase": None, "stranded": None} for i in range(60)]
        result = scale.stranded_listeners(rows, 100.0, 160.0)
        self.assertEqual((result["stranded"], result["at_end"], result["source"]),
                         (10, 0, "active - up"))
        short = [dict(row, listeners=90 if 20 <= row["t"] - 100 < 23 else 100) for row in rows]
        self.assertEqual(scale.stranded_listeners(short, 100.0, 160.0)["stranded"], 0)
        own = [dict(row, stranded=3 if row["t"] > 150 else 0) for row in rows]
        self.assertEqual(scale.stranded_listeners(own, 100.0, 160.0)["source"], "needletail-load")
        self.assertEqual(scale.stranded_listeners(own, 100.0, 160.0)["at_end"], 3)
        none = [dict(row, active=None) for row in rows]
        self.assertIsNone(scale.stranded_listeners(none, 100.0, 160.0)["stranded"])

    def write_phase_step(self, directory: pathlib.Path, start: float, timeline: dict,
                         am10: bool = False) -> None:
        """A step of F1 (drain lon-edge-1) from its files: lon-load-1 with
        listeners on lon-edge-1 and lon-edge-2, which move off lon-edge-1 at
        the fault onto lon-edge-2 (and two onto lon-edge-3), their missing
        parts booked on lon-edge-2 as needletail-load does; a survivor sampler; the mesh samplers;
        a reader report with one move."""
        state = lab()
        address = {name: f"{state['nodes'][name]['vlan_ipv4']}:19446"
                   for name in ("lon-edge-1", "lon-edge-2", "lon-edge-3")}
        row = timeline["scenarios"][0]
        fault = start + row["fault"]
        lines = []
        for second in range(int(row["end"]) + 1):
            t = start + second
            moving = fault <= t < fault + 2
            # needletail-load books a moved listener's missing parts on the
            # edge it moved to (src/load.rs mark(): the current edge's
            # counters), never on the edge it left.
            edges = {address["lon-edge-1"]: {"up": 0 if t >= fault + 2 else 50,
                                             "parts": 0 if t >= fault + 2 else 50_000,
                                             "missing": 0, "moves": 0},
                     address["lon-edge-2"]: {"up": 98 if t >= fault + 2 else 50, "parts": 50_000,
                                             "missing": 1 if moving else 0,
                                             "moves": 24 if moving else 0},
                     address["lon-edge-3"]: {"up": 2 if t >= fault + 2 else 0, "parts": 0,
                                             "missing": 0, "moves": 1 if moving else 0}}
            if am10:
                # needletail-load of Phase AM item 10: the moved listeners'
                # missing parts, and their time to move (~201 ms each).
                for edge in edges.values():
                    edge["missing_moved"] = edge["missing"]
                    edge["moves_out"] = 0
                edges[address["lon-edge-1"]]["moves_out"] = 25 if moving else 0
                if moving:
                    edges[address["lon-edge-2"]]["time_to_move_ms"] = {
                        "count": 24, "p99": 201.0, "buckets_us": [[200704, 24]]}
            lines.append({"unix_ms": int(t * 1000), "edges": edges,
                          "total": {"up": 100, "active": 100, "parts": 100_000, "missing": 0}})
        host = directory / "lon-load-1" / "udp"
        host.mkdir(parents=True)
        (host / "intervals.jsonl").write_text("\n".join(json.dumps(line) for line in lines))
        sampler = [{"host": "x", "public_iface": "eth0", "cpus": 2}] + [
            {"t": start + second, "cpu": 0.3 if second < 60 else 0.5, "core_max": 0.6,
             "steal": 0.0, "rx": {"eth0": 1000}, "tx": {"eth0": 2.5e8},
             "udp": {"RcvbufErrors": 0}, "procs": {}, "rss_mb": {}}
            for second in range(int(row["end"]) + 1)]
        for name in ("lon-edge-2", "lon-edge-1"):
            (directory / name).mkdir(exist_ok=True)
            (directory / name / "sampler.jsonl").write_text(
                "\n".join(json.dumps(item) for item in sampler))
            mesh = [{"t": start + second, "node": name,
                     "client_udp": {"would_block": 5, "send_queue_drops": 0, "refusals": 0,
                                    "send_errors": 0, "moves": second // 10},
                     "sessions": 0, "warm_parts_pending": None}
                    for second in range(int(row["end"]) + 1)]
            (directory / name / "mesh.jsonl").write_text(
                "\n".join(json.dumps(item) for item in mesh))
        report = {"summary": {}, "readers": [{"session": "s-0", "moves": [
            {"at_ms": int((fault - (start - 10)) * 1000) + 300, "from": "lon-edge-1",
             "to": "lon-edge-5", "reason": "drain-redirect"}]}]}
        (directory / "lon-load-1" / "hls").mkdir()
        (directory / "lon-load-1" / "hls" / "steered.json").write_text(json.dumps(report))

    def test_a_scenario_from_its_files_before_during_after(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        plan = scale.load_phase_plan(PHASES, names=["F1"])
        timeline = scale.phase_timeline(plan)
        start = 1_790_000_000.0
        self.write_phase_step(directory, start, timeline)
        events = [{"unix_us": int((start + 60) * 1e6), "event": "action", "node": "lon-edge-1"},
                  {"unix_us": int((start + 500) * 1e6), "event": "later"}]
        result = scale.analyse_phases(lab(), directory, timeline, start, plan, events,
                                      readers_launched=start - 10)[0]
        self.assertEqual(result["name"], "F1")
        self.assertEqual(result["targets"], ["lon-edge-1"])
        self.assertEqual(result["windows"]["during"], [start + 60, start + 150])
        self.assertEqual(result["udp"]["before"]["faulted"]["missing"], 0)
        # The moved listeners' losses land on the edge they moved to.
        self.assertEqual(result["udp"]["during"]["faulted"]["missing"], 0)
        self.assertEqual(result["udp"]["during"]["unaffected"]["missing"], 2)
        self.assertEqual(result["moves"]["udp"], 50)
        # 48 of 50 moves onto lon-edge-2: the pile-up of a static edge list.
        self.assertGreater(result["moves"]["udp_spread_max_over_mean"], 1.3)
        self.assertEqual(result["moves"]["hls_by_reason"], {"drain-redirect": 1})
        self.assertEqual(result["stranded"]["max"], 0)
        self.assertEqual([item["event"] for item in result["events"]], ["action"])
        criteria = {item["criterion"]: item for item in result["criteria"]}
        self.assertTrue(criteria["stranded = 0"]["ok"])
        # Judged on every listener; the unaffected ones alone wait for
        # per-listener moves (AM item 10), with the edge figure shown.
        self.assertFalse(criteria["all listeners (moved ones included): 0 missing"]["ok"])
        self.assertEqual(criteria["all listeners (moved ones included): 0 missing"]["value"], 2)
        self.assertIsNone(criteria["unaffected listeners alone: 0 missing"]["ok"])
        self.assertIn("AM item 10", criteria["unaffected listeners alone: 0 missing"]["needs"])
        self.assertFalse(criteria["missing = 0"]["ok"])
        self.assertFalse(criteria["moves spread max/mean <= 1.3"]["ok"])
        self.assertIsNone(criteria["time_to_move_p99_ms <= 300"]["ok"])
        self.assertIn("needletail-load", criteria["time_to_move_p99_ms <= 300"]["needs"])
        self.assertTrue(criteria["survivors: busiest core < 90%"]["ok"])
        self.assertTrue(criteria["survivors: no rise in client_udp.would_block"]["ok"])
        self.assertEqual(result["survivors"]["during"]["cpu_max"], 0.5)
        text = scale.format_phases([result])
        self.assertIn("| F1 | lon-edge-1 |", text)
        self.assertIn("FAIL missing = 0", text)

    def test_a_scenario_judges_moved_listeners_apart_with_the_am10_load(self):
        """needletail-load of Phase AM item 10 counts a moved listener's
        missing parts and times each move: the unaffected listeners' 0
        missing and the time to move are judged."""
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        plan = scale.load_phase_plan(PHASES, names=["F1"])
        timeline = scale.phase_timeline(plan)
        start = 1_790_000_000.0
        self.write_phase_step(directory, start, timeline, am10=True)
        result = scale.analyse_phases(lab(), directory, timeline, start, plan, [],
                                      readers_launched=start - 10)[0]
        during = result["udp"]["during"]
        self.assertEqual(during["unaffected"]["missing_moved"], 2)
        self.assertEqual(during["unaffected"]["time_to_move_ms"]["count"], 48)
        criteria = {item["criterion"]: item for item in result["criteria"]}
        self.assertTrue(criteria["unaffected listeners alone: 0 missing"]["ok"])
        self.assertEqual(criteria["unaffected listeners alone: 0 missing"]["value"], 0)
        self.assertFalse(criteria["all listeners (moved ones included): 0 missing"]["ok"])
        moved = criteria["time_to_move_p99_ms <= 300"]
        self.assertTrue(moved["ok"], moved)
        self.assertAlmostEqual(moved["value"], 201.727, places=2)
        self.assertNotIn("Phase AM items this scenario needs before it can pass", criteria)

    def test_the_load_command_asks_the_entry_points(self):
        alloc = {"udp": 700, "hls": 0, "hls_by_edge": {}}
        command = scale.load_command(alloc, ["10.80.1.21:19446"], "pcm-1", 20, 80, 20, "/r/7",
                                     entries=["https://10.80.1.11:19444",
                                              "https://10.80.1.12:19444"])
        self.assertIn("--entry https://10.80.1.11:19444,https://10.80.1.12:19444 ", command)
        self.assertIn(f"--tls-ca {scale.CA} --server-name {scale.SERVER_NAME} ", command)
        self.assertIn("--edges 10.80.1.21:19446 ", command)
        self.assertNotIn("--entry", scale.load_command(alloc, ["a:1"], "pcm-1", 1, 1, 1, "/r"))

    def test_the_runner_acts_in_order_logs_unix_us_and_restores_what_it_owes(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F2", "F3"]))
        state = lab()
        done, logged = [], []
        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=0.0)
        with mock.patch.object(scale, "ssh", lambda node, command, **k: done.append(
                (node["name"], command.split(";")[0])) or ""), \
                mock.patch.object(scale, "http_post", lambda node, path, body, **k: done.append(
                    (node["name"], path, body["node_id"])) or 200), \
                mock.patch.object(scale, "http_get", lambda node, path, **k: {}):
            runner = scale.PhaseRunner(state, timeline, time.time() - 10_000, logged.append,
                                       args)
            runner.run()
            runner.finish()
        self.assertEqual(done, [
            ("new-edge-2", "systemctl kill -s KILL needletail-mesh.service"),
            ("new-edge-2", "systemctl start needletail-mesh.service"),
            ("tok-dist-a", "/api/control/drain-node", "tok-dist-a"),
            ("tok-dist-a", "systemctl stop needletail-mesh.service"),
            ("tok-dist-a", "systemctl start needletail-mesh.service")])
        actions = [item for item in logged if item.get("event") == "action"]
        self.assertTrue(all(isinstance(item["unix_us"], int) and item["unix_us"] > 1.7e15
                            for item in actions))
        self.assertEqual(runner.owed, [])
        # Stopped early, after the kill: the kill's restore is owed and
        # carried out by finish.
        done.clear()
        with mock.patch.object(scale, "ssh", lambda node, command, **k: done.append(
                (node["name"], command.split(";")[0])) or ""):
            runner = scale.PhaseRunner(state, timeline, time.time(), logged.append, args)
            runner.act(timeline["scenarios"][0]["actions"][0])
            runner.owed.append(timeline["scenarios"][0]["actions"][0])
            runner.finish()
        self.assertEqual(done, [("new-edge-2", "systemctl kill -s KILL needletail-mesh.service"),
                                ("new-edge-2", "systemctl start needletail-mesh.service")])
        with self.assertRaises(ValueError):
            runner.node("contrib-lon")
        # A control request that names no node would be global in av-mesh.
        with self.assertRaises(ValueError):
            scale.http_post(state["nodes"]["lon-edge-1"], "/api/control/drain-node", {})

    def test_a_step_stopped_early_starts_no_new_fault_and_restores_the_one_under_way(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F2", "F3"]))
        state = lab()
        done, logged = [], []
        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=0.0)
        runner = scale.PhaseRunner(state, timeline, time.time() - 10_000, logged.append, args)

        def ssh(node, command, **kwargs):
            done.append((node["name"], command.split(";")[0]))
            if "KILL" in command:
                # The monitor stops the step while the kill is under way.
                runner.stop.set()
            return ""

        with mock.patch.object(scale, "ssh", ssh), \
                mock.patch.object(scale, "http_post", lambda node, path, body, **k: done.append(
                    (node["name"], path)) or 200), \
                mock.patch.object(scale, "http_get", lambda node, path, **k: {}):
            runner.begin()
            runner.thread.join(timeout=10)
            runner.finish()
        # The kill, then its restore; F3's drain and stop never happen.
        self.assertEqual(done, [("new-edge-2", "systemctl kill -s KILL needletail-mesh.service"),
                                ("new-edge-2", "systemctl start needletail-mesh.service")])
        self.assertEqual(runner.owed, [])

    def test_the_targets_of_one_action_are_faulted_at_once(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F8"]))
        action = timeline["scenarios"][0]["actions"][0]
        targets = action["targets"]
        self.assertGreater(len(targets), 2)
        barrier = threading.Barrier(len(targets), timeout=5)
        logged = []
        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=0.0)
        runner = scale.PhaseRunner(lab(), timeline, time.time(), logged.append, args)
        # Each kill waits for every other to begin: only concurrent kills pass.
        with mock.patch.object(scale, "ssh", lambda node, command, **k: barrier.wait() and ""), \
                mock.patch.object(scale, "http_post", lambda node, path, body, **k:
                                  barrier.wait() and 200), \
                mock.patch.object(scale, "http_get", lambda node, path, **k: {}):
            runner.act(action)
        self.assertFalse([item for item in logged if item.get("event") == "failed"], logged)
        self.assertEqual(sorted(item["node"] for item in logged if item.get("event") == "done"),
                         sorted(targets))
        spread = [item for item in logged if item.get("event") == "spread"]
        self.assertEqual(len(spread), 1)
        self.assertLess(spread[0]["spread_us"], 5_000_000)

    def test_an_added_slot_is_only_stopped_and_a_remove_needs_its_drain(self):
        plan = json.loads(PHASES.read_text())
        add = {"name": "X1", "group": "R", "actions": [
            {"kind": "add", "targets": ["lon-edge-11"]}]}
        plan["scenarios"] = [add]
        path = pathlib.Path(tempfile.mkdtemp()) / "plan.json"
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text(json.dumps(plan))
        row = scale.phase_timeline(scale.load_phase_plan(path, names=["X1"]))["scenarios"][0]
        self.assertEqual([(item["kind"], item["targets"]) for item in row["restores"]],
                         [("leave", ["lon-edge-11"])])
        # A remove with no drain of the slot before it is refused.
        plan["scenarios"] = [{"name": "X2", "group": "R", "actions": [
            {"kind": "remove", "targets": ["lon-edge-11"]}]}]
        path.write_text(json.dumps(plan))
        with self.assertRaises(ValueError):
            scale.load_phase_plan(path, names=["X2"])
        plan["scenarios"] = [{"name": "X3", "group": "R", "actions": [
            {"kind": "drain", "targets": ["lon-edge-11"]},
            {"kind": "remove", "targets": ["lon-edge-11"], "at_s": 60}]}]
        path.write_text(json.dumps(plan))
        timeline = scale.phase_timeline(scale.load_phase_plan(path, names=["X3"]))
        # At run time, a remove whose drain never ended deletes nothing.
        logged, called = [], []
        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=0.0)
        state = lab()
        state["nodes"]["lon-edge-11"] = {**state["nodes"]["lon-edge-10"], "name": "lon-edge-11"}
        runner = scale.PhaseRunner(state, timeline, time.time(), logged.append, args)
        with mock.patch.object(scale, "deploy_nodes", lambda verb, name: called.append(verb)), \
                mock.patch.object(scale, "lab_tool_slot", lambda verb, name, a: called.append(verb)):
            runner.act(timeline["scenarios"][0]["actions"][1])
        self.assertEqual(called, [])
        self.assertIn("did not end", [item for item in logged
                                      if item.get("event") == "failed"][0]["error"])
        runner.drained.add("lon-edge-11")
        with mock.patch.object(scale, "deploy_nodes", lambda verb, name: called.append(verb)), \
                mock.patch.object(scale, "lab_tool_slot", lambda verb, name, a: called.append(verb)):
            runner.act(timeline["scenarios"][0]["actions"][1])
        self.assertEqual(called, ["stop", "remove"])

    def test_one_controller_at_a_time_its_state_and_output_in_the_step(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "A"))
        items = [row["actions"][0] for row in timeline["scenarios"]]
        self.assertEqual([item["kind"] for item in items], ["autoscale", "autoscale"])
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        started = []

        class Process:
            def __init__(self, command, **kwargs):
                self.command = command
                self.signals = []
                self.exited = threading.Event()
                self.returncode = None
                started.append(self)

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if not self.exited.wait(timeout):
                    raise scale.subprocess.TimeoutExpired(self.command, timeout)
                return self.returncode

            def end(self, code):
                self.returncode = code
                self.exited.set()

            def send_signal(self, number):
                self.signals.append(number)
                self.end(130)

            def terminate(self):
                self.signals.append("TERM")
                self.end(-15)

            def kill(self):
                self.end(-9)

        logged = []
        args = argparse.Namespace(stream=4001, budget_usd=100.0, spent_before_usd=31.4)
        runner = scale.PhaseRunner(lab(), timeline, time.time(), logged.append, args,
                                   directory / "events.jsonl", directory=directory)
        with mock.patch.object(scale.subprocess, "Popen", Process), \
                mock.patch.object(scale, "CONTROLLER_WAIT_S", 0.2), \
                mock.patch.dict(scale.os.environ, {"NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS": "7"}):
            runner.act(items[0])
            # A1's controller is still in its last tick when A2 begins: it
            # is waited for, then stopped with SIGINT, before A2's starts.
            runner.act(items[1])
            self.assertEqual(started[0].signals, [scale.signal.SIGINT])
            self.assertEqual(len(started), 2)
            started[1].end(1)  # A2's controller refuses
            for thread in runner.threads:
                thread.join(timeout=5)
        for process in started:
            command = process.command
            self.assertEqual(command[command.index("--controller-state") + 1],
                             str(directory / "autoscale-state.json"))
            # The lab tool's ceiling on the watchdog's hours is the controller's.
            self.assertEqual(command[command.index("--max-watchdog-hours") + 1], "7")
        self.assertTrue((directory / "controller-A1.log").exists())
        exits = {item["scenario"]: item["returncode"] for item in logged
                 if item.get("event") == "controller_exit"}
        self.assertEqual(exits, {"A1": 130, "A2": 1})
        kinds = [item.get("event") for item in logged]
        self.assertLess(kinds.index("controller_wait"), kinds.index("controller_stop"))

    def test_plan_prints_the_phases_and_their_cost(self):
        args = argparse.Namespace(
            topology=str(TOPOLOGY), topology_only=True, without_sites="", second_curve=False,
            edges_per_region=None, json=False, config=None, steps=None, hls_share=None,
            hls_cap_per_host=None, hold=60.0, ramp=None, cost=None, udp_mbps=None,
            inbound_gbps=scale.LOAD_HOST_INBOUND_GBPS, phase_plan=str(PHASES), group="F",
            scenarios="")
        with redirect_stdout(io.StringIO()) as out:
            plan = scale.command_plan(args)
        self.assertEqual(plan["steps"][0]["listeners"], 8000)
        self.assertEqual(plan["steps"][0]["hold_s"], 9 * 180)
        self.assertIn("F8", out.getvalue())
        cost = plan["phases"]["cost"]
        self.assertAlmostEqual(cost["scenarios"]["F1"]["minutes"], 3.0)
        self.assertAlmostEqual(cost["minutes"], (plan["steps"][0]["ramp_s"] + 9 * 180 + 75) / 60,
                               places=1)


REFEREE = (scale.COMPARE_ROOT / "scripts" / "compare" / "compare-audio.py").is_file()


class LiveKitCrowd(unittest.TestCase):
    """The self-hosted LiveKit control beside Needletail (item 2)."""

    def test_the_crowd_is_capped_at_the_sfu_and_counted_on_the_hosts(self):
        state = lab(["sydney"])
        plan = scale.plan_steps(state, [100, 1000, 5000], "pcm-1", hls_share=0, standby=STANDBY,
                                lk_per_sfu=450)
        small, middle, large = plan["steps"]
        # The step's listeners of a region, up to 450 an SFU.
        self.assertEqual({item["livekit"] for item in small["sites"].values()}, {33, 34})
        self.assertEqual({item["livekit"] for item in middle["sites"].values()}, {333, 334})
        self.assertEqual({item["livekit"] for item in large["sites"].values()}, {450})
        self.assertEqual(large["livekit"], 3 * 450)
        # Over the region's listener hosts by vCPU, and in their load.
        london = large["allocation"]["london"]
        self.assertEqual(sum(item["lk"] for item in london.values()), 450)
        self.assertLessEqual(max(item["lk"] for item in london.values())
                             - min(item["lk"] for item in london.values()), 1)
        capacity = plan["capacity"]["lon-load-1"]
        self.assertEqual(capacity["lk"], int(8 * 0.7 / (0.02 * 0.4)))
        without = scale.plan_steps(state, [5000], "pcm-1", hls_share=0, standby=STANDBY)
        self.assertGreater(large["host_load"]["lon-load-1"],
                           without["steps"][0]["host_load"]["lon-load-1"])
        self.assertEqual(without["steps"][0]["livekit"], 0)
        # Its traffic is public (the SFUs' addresses) and priced.
        cost = scale.cost_estimate(state, plan)
        self.assertGreater(cost["livekit_public_gb"], 0)
        self.assertEqual(scale.cost_estimate(state, without)["livekit_public_gb"], 0)
        # A region without an SFU runs no crowd.
        bare = lab(["sydney"])
        del bare["nodes"]["tok-lk"]
        plan = scale.plan_steps(bare, [5000], "pcm-1", hls_share=0, standby=STANDBY,
                                lk_per_sfu=450)
        self.assertEqual(plan["steps"][0]["sites"]["tokyo"]["livekit"], 0)

    def test_the_cost_of_a_subscriber_is_measured_per_host(self):
        nodes = {"lon-load-1": {"procs_cores_mean": {"ntcmp-lk": 0.4, "needletail-load": 2.0},
                                "procs_rss_mb_max": {"ntcmp-lk": 50.0}}}
        allocation = {"london": {"lon-load-1": {"udp": 500, "hls": 0, "lk": 50}}}
        costs = scale.measured_costs(nodes, allocation, "pcm-1")
        # 0.4 cores for 50 subscribers of 400 units a second: 0.02 at 1,000.
        self.assertAlmostEqual(costs["lk_process_vcpu"], 0.02)
        self.assertAlmostEqual(costs["lk_vcpu"], 0.02 + scale.SOFTIRQ_VCPU)
        self.assertEqual(scale.cost_file({"steps": [{"listeners": 1, "measured_costs": costs}]})
                         ["lk_vcpu"], costs["lk_vcpu"])
        self.assertIn('"ntcmp-lk"', scale.SAMPLER)

    def test_the_keys_and_nothing_else_go_to_the_listener_hosts(self):
        text = ("export CF_REALTIME_APP_SECRET=x\nexport LK_SELF_KEY=APIabc\n"
                "export LK_SELF_SECRET=s3cret\n")
        self.assertEqual(scale.keys_lines(text), ["export LK_SELF_KEY=APIabc",
                                                  "export LK_SELF_SECRET=s3cret"])


@unittest.skipUnless(REFEREE, "the referee's harness (needletail-compare) is not checked out")
class Referee(unittest.TestCase):
    """The LiveKit crowd and the joins phase through the referee's own
    compare-audio.py, offline (its ssh calls replaced)."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = lab(["sydney"])
        self.ref = scale.referee()
        self.session = scale.referee_session(self.ref, "r1-5000", self.tmp, self.state)
        self.room = scale.livekit_room("r1", "pcm-1")
        self.audio = scale.referee_audio(self.ref, self.session, "pcm-1", self.room,
                                         ["lon", "new", "tok"])

    def test_the_referees_lanes_of_each_config(self):
        self.assertEqual(self.room, "needletail-scale-r1-pcm")
        self.assertEqual((self.audio.config, self.audio.lanes), ("pcm", ["lkregion"]))
        self.assertEqual(self.audio.nt, ["nt-pcm", "nt-pcm-w", "nt-pcm-h3", "nt-uc"])
        self.assertTrue(self.audio.index.endswith("/pcm-2.5ms.index.json"))
        opus = scale.referee_audio(self.ref, self.session, "opus-1", self.room, ["lon"])
        self.assertEqual(opus.config, "opus-2.5")
        self.assertEqual(opus.nt_frame_ms(), {"nt-popus": 1.0})
        self.assertIn("nt-popus-48s-la", opus.probe("nt-popus", "lon", 5, 1, "t"))
        standard = scale.referee_audio(self.ref, self.session, "opus-2.5", self.room, ["lon"])
        self.assertEqual(standard.nt_frame_ms(), {"nt-popus": 2.5})
        self.assertEqual(self.ref.SITES, ["lon"])

    def test_the_crowd_runs_the_referees_lkregion_probe_on_the_listener_hosts(self):
        plan = scale.plan_steps(self.state, [5000], "pcm-1", hls_share=0.05, standby=STANDBY,
                                lk_per_sfu=450)
        args = argparse.Namespace(config="pcm-1", sample=10, load_args="", hls_window=40,
                                  hls_http2=False, hls_steering="spread")
        commands = scale.step_commands(self.state, plan, plan["steps"][0], "r1", args,
                                       livekit=self.audio)
        sfu = self.state["nodes"]["lon-lk"]["public_ipv4"]
        london = commands["lon-load-1"]
        for part in (f"ntcmp-lk sub --lane lk --url ws://{sfu}:7880 --room {self.room}",
                     "--count 57 ", "--data ", "--identity r1-5000-lon-load-1-lkregion",
                     "--audio-index /var/lib/compare/index/pcm-2.5ms.index.json",
                     "--report lkregion.probe.json", ". /var/lib/compare/env",
                     "/var/lib/needletail-runs/scale/r1/5000/lk"):
            self.assertIn(part, london)
        self.assertIn("ws://" + self.state["nodes"]["tok-lk"]["public_ipv4"],
                      commands["tok-load-1"])
        sfus = {node["public_ipv4"] for node in self.state["nodes"].values()
                if node["role"] == "livekit"}
        public = {node["public_ipv4"] for node in self.state["nodes"].values()} - sfus
        for name, command in commands.items():
            # Needletail's listeners read the VLAN; only the SFUs are public.
            self.assertFalse([address for address in re.findall(r"[0-9.]+", command)
                              if address in public], name)
            checked = subprocess.run(["bash", "-n", "-c", command], capture_output=True,
                                     text=True)
            self.assertEqual(checked.returncode, 0, f"{name}: {checked.stderr}")
        self.assertEqual(sum(len(re.findall(r"--count ([0-9]+)", command)) and
                             int(re.search(r"ntcmp-lk sub.*?--count ([0-9]+)", command).group(1))
                             for command in commands.values()), 3 * 450)

    def test_the_publishers_are_the_referees_one_per_sfu(self):
        sent = []
        with mock.patch.object(self.ref.run, "src",
                               lambda session, command, **kw: sent.append(command) or ""):
            scale.start_livekit(self.ref, self.session, self.audio, ["lon", "new", "tok"])
            scale.stop_livekit(self.ref, self.session)
        self.assertEqual(sent[0], "pkill -x ntcmp-lk; true")
        self.assertEqual(len(sent), 5)
        sfu = self.state["nodes"]["new-lk"]["public_ipv4"]
        self.assertIn(f"ntcmp-lk pub --lane lk --url ws://{sfu}:7880 --room {self.room} "
                      "--audio /var/lib/compare/audio/audio.s24le --data --pcm-ms 2.5 "
                      "--status lkregion-new.status", sent[2])
        self.assertIn("LIVEKIT_API_KEY=$LK_SELF_KEY", sent[2])
        self.assertIn("setsid nohup", sent[2])
        self.assertEqual(sent[-1], "pkill -x ntcmp-lk; true")

    def write_crowd(self, step: dict) -> pathlib.Path:
        crowd = self.session.dir / scale.crowd_step_name("pcm-1", step["listeners"])
        for host, p50, gaps in (("lon-load-1", 41.0, 0), ("lon-load-2", 45.0, 24),
                                ("new-load-1", 60.0, 0)):
            (crowd / host).mkdir(parents=True)
            for index in range(3):
                (crowd / host / f"lkregion.probe-{index:03d}.json").write_text(json.dumps({
                    "audio_packets": 24000, "audio_sequence_gaps": gaps, "first_audio_ms": 400.0,
                    "audio_latency_ms": {"p50": p50, "p95": p50 + 5, "p99": p50 + 10,
                                         "max": p50 + 30, "min": p50 - 10, "count": 24000}}))
        (crowd / "meta.json").write_text(json.dumps(
            scale.crowd_meta(self.audio, "pcm-1", step, "r1", 1_000_000.0, 120.0)))
        return crowd

    def test_the_crowd_is_summarised_with_the_referees_analysis(self):
        plan = scale.plan_steps(self.state, [5000], "pcm-1", hls_share=0, standby=STANDBY,
                                lk_per_sfu=450)
        step = plan["steps"][0]
        crowd = self.write_crowd(step)
        meta = json.loads((crowd / "meta.json").read_text())
        self.assertEqual((meta["config"], meta["phase"], meta["detail"], meta["lanes"]),
                         ("pcm", "scale", "5000", ["lkregion"]))
        self.assertEqual(meta["sfu_pcm_ms"], 2.5)
        result = {"nodes": {"lon-lk": {"cpu_mean": 0.3, "cpu_max": 0.5, "saturated_at": None}},
                  "control": {"session": self.session.name, "crowd": crowd.name,
                              "livekit_planned": {site: item["livekit"]
                                                  for site, item in step["sites"].items()},
                              "joins_run": None}}
        figures = scale.control_results(self.state, self.tmp, result)
        self.assertNotIn("control_error", figures)
        london = figures["livekit"]["london"]
        self.assertEqual((london["planned"], london["listeners"], london["with_audio"]),
                         (450, 6, 6))
        self.assertEqual((london["p50_ms"], london["p99_ms"]), (43.0, 55.0))
        self.assertAlmostEqual(london["missing_percent"], 0.05)
        self.assertEqual(london["sfu"]["cpu_max"], 0.5)
        self.assertEqual(figures["livekit"]["tokyo"]["listeners"], 0)
        text = scale.format_summary({"run": "r1", "config": "pcm-1", "hls_share": 0,
                                     "steps": [{"listeners": 5000, **figures}]})
        self.assertIn("Self-hosted LiveKit beside Needletail", text)
        self.assertIn("| 5000 | london | 450 | 6 | 6 | 43 | 55 | 0.05 | 400 | 0.5 |", text)


@unittest.skipUnless(REFEREE, "the referee's harness (needletail-compare) is not checked out")
class Joins(unittest.TestCase):
    """The joins phase in every held step (item 3): the referee's
    Audio.joins on the view hosts, its records read by the referee's
    analysis and scoreboard."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = lab(["sydney"])
        self.ref = scale.referee()
        self.session = scale.referee_session(self.ref, "r1-5000", self.tmp, self.state)
        self.audio = scale.referee_audio(self.ref, self.session, "pcm-1",
                                         scale.livekit_room("r1", "pcm-1"), ["lon", "new", "tok"])

    def test_every_step_holds_long_enough_for_its_joins(self):
        self.assertEqual(scale.joins_seconds(20), 20 * 6.5 + 10)
        plan = scale.plan_steps(self.state, [1000, 5000], "pcm-1", standby=STANDBY, joins=20)
        self.assertEqual([step["joins"] for step in plan["steps"]], [20, 20])
        self.assertEqual([step["hold_s"] for step in plan["steps"]], [140.0, 140.0])
        late = scale.plan_steps(self.state, [1000, 5000], "pcm-1", standby=STANDBY, joins=20,
                                joins_from=5000)
        self.assertEqual([step["joins"] for step in late["steps"]], [0, 20])
        self.assertEqual([step["hold_s"] for step in late["steps"]], [60.0, 140.0])
        self.assertEqual(scale.plan_steps(self.state, [1000], "pcm-1", standby=STANDBY,
                                          joins=20, hold=300)["steps"][0]["hold_s"], 300)

    def test_a_phase_run_has_no_joins_unless_asked_and_never_with_dist_a_faulted(self):
        args = argparse.Namespace(phase_plan=str(PHASES), group="R", scenarios="", steps=None,
                                  hls_share=None, config=None, hold=60.0, joins=None,
                                  lk_per_sfu=None)
        scale.phase_setup(args)
        self.assertEqual((args.joins, args.lk_per_sfu), (0, 0))
        args = argparse.Namespace(phase_plan=str(PHASES), group="F", scenarios="", steps=None,
                                  hls_share=None, config=None, hold=60.0, joins=20,
                                  lk_per_sfu=None)
        with self.assertRaises(ValueError) as caught:
            scale.phase_setup(args)
        self.assertIn("lon-dist-a", str(caught.exception))
        ramp = argparse.Namespace(steps=None, hls_share=None, config=None)
        scale.resolve_defaults(ramp)
        self.assertEqual((ramp.joins, ramp.lk_per_sfu, ramp.joins_from), (20, 450, 0))

    def fake_hosts(self, scripts_seen):
        """compare-run.py's run_hosts without ssh: keep each view host's
        script and leave the records each join would leave."""
        def run_hosts(session, step, scripts, seconds):
            scripts_seen.update(scripts)
            for host in scripts:
                directory = session.dir / step / host
                directory.mkdir(parents=True, exist_ok=True)
                for lane, first in (("nt-uc", 120.0), ("lkregion", 900.0), ("nt-pcm-h3", 300.0)):
                    for index in range(1, 21):
                        (directory / f"{lane}.join-{index:02d}.json").write_text(json.dumps({
                            "first_audio_ms": first + index, "connect_ms": 50.0}))
                (directory / "nt-uc.join-01.uc.json").write_text("{}")
            return {"started_unix": time.time(), "seconds": 150.0, "host_errors": {}}
        return run_hosts

    def test_the_referees_joins_run_on_the_view_hosts_and_fill_the_scoreboard(self):
        scripts: dict = {}
        logged: list = []
        with mock.patch.object(self.ref.run, "run_hosts", self.fake_hosts(scripts)), \
                mock.patch.object(self.ref.run, "src", lambda *args, **kw: ""), \
                redirect_stdout(io.StringIO()):
            job = scale.JoinsJob(self.audio, 20, time.time(), time.time() + 600, logged.append,
                                 5000).start()
            job.wait()
        self.assertEqual(sorted(scripts), ["lon-view", "new-view", "tok-view"])
        london = scripts["lon-view"]
        # 20 new listeners of each lane, one after another, the lanes side by side.
        self.assertEqual(london.count("ntcmp-lk sub --lane lk"), 20)
        self.assertEqual(london.count("needletail-udp-client --entry"), 20)
        self.assertEqual(london.count("--lane nt-pcm-h3 "), 20)
        self.assertEqual(london.count("&\n"), 5)
        self.assertIn("mv -f lkregion.probe.json lkregion.join-20.json", london)
        self.assertIn("r1-5000-lon-join01", london)
        record = job.record()
        self.assertEqual(record["step"], "audio.pcm.joins.20")
        self.assertTrue(record["within_hold"])
        self.assertIsNone(record["error"])
        self.assertEqual([item["joins"] for item in logged], ["start", "end"])
        self.assertEqual(logged[0]["lanes"], ["lkregion", "nt-pcm", "nt-pcm-w", "nt-pcm-h3",
                                              "nt-uc"])
        meta = json.loads((self.session.dir / "audio.pcm.joins.20" / "meta.json").read_text())
        self.assertEqual((meta["phase"], meta["joins_per_lane"], meta["join_seconds"]),
                         ("joins", 20, 5))
        # The referee's analysis, then its scoreboard: the time to first
        # audio row, Needletail (nt-uc) against LiveKit (lkregion).
        result = {"nodes": {}, "control": {"session": self.session.name, "crowd": None,
                                           "joins_run": record}}
        figures = scale.control_results(self.state, self.tmp, result)
        self.assertEqual(figures["joins"]["lon-view"]["nt-uc"]["first_audio_ms"]["p50"], 131.0)
        self.assertEqual(figures["joins"]["lon-view"]["lkregion"]["with_audio"], 20)
        analyse = scale.referee("compare-audio-analyse.py")
        steps = {"audio.pcm.joins.20": analyse.analyse_step_now(
            self.session.dir / "audio.pcm.joins.20")}
        rows = scale.referee("scoreboard.py").board(
            {"sessions": {self.session.name: {"steps": steps}}}, [self.session.name])
        first = {row["region"]: row for row in rows if row["metric"] == "first_audio"}
        self.assertEqual(sorted(first), ["London", "New York", "Tokyo"])
        self.assertEqual((first["London"]["needletail"], first["London"]["livekit"],
                          first["London"]["result"]), (131.0, 911.0, "win"))
        text = scale.format_summary({"run": "r1", "config": "pcm-1", "hls_share": 0,
                                     "steps": [{"listeners": 5000, **figures}]})
        self.assertIn("| 5000 | lon-view | nt-uc | 20 | 20 | 131 | 139 | 140 |", text)

    def test_joins_past_the_hold_are_set_aside_and_not_scored(self):
        # The referee's own estimate for 20 joins is 190 s; the hold is
        # 140 s. Each join here starts 6.5 s after the one before, from the
        # end of the ramp, and the hold ends 100 s later: joins 1 to 14 ran
        # wholly inside it (14 x 6.5 + 5 = 96 s), 15 to 20 did not.
        base_ms = 1_800_000_000_000
        step = "audio.pcm.joins.20"
        for host in ("lon-view", "new-view"):
            directory = self.session.dir / step / host
            directory.mkdir(parents=True, exist_ok=True)
            for lane, first in (("nt-uc", 120.0), ("lkregion", 900.0)):
                for index in range(1, 21):
                    (directory / f"{lane}.join-{index:02d}.json").write_text(json.dumps({
                        "started_unix_ms": base_ms + index * 6500,
                        # The late ones met an emptying mesh: much faster.
                        "first_audio_ms": first + (index if index <= 14 else -100.0),
                        "connect_ms": 50.0}))
                    if lane == "nt-uc":
                        (directory / f"nt-uc.join-{index:02d}.uc.json").write_text("{}")
        (self.session.dir / step / "meta.json").write_text(json.dumps(
            {"step": step, "config": "pcm", "phase": "joins", "lanes": ["lkregion", "nt-uc"],
             "join_seconds": 5, "joins_per_lane": 20, "nt_uc_base": "nt-pcm"}))
        record = {"step": step, "per_lane": 20, "hold_end": base_ms / 1000 + 100.0,
                  "join_seconds": 5, "ran": True}
        result = {"nodes": {}, "control": {"session": self.session.name, "crowd": None,
                                           "joins_run": record}}
        figures = scale.control_results(self.state, self.tmp, result)
        london = figures["joins"]["lon-view"]
        self.assertEqual((london["nt-uc"]["joins"], london["nt-uc"]["late"]), (14, 6))
        self.assertEqual((london["lkregion"]["joins"], london["lkregion"]["late"]), (14, 6))
        # Only the joins inside the hold are scored: 121 to 134 ms (the
        # referee's p50 of 14 is the upper median).
        self.assertEqual(london["nt-uc"]["first_audio_ms"]["max"], 134.0)
        self.assertEqual(london["nt-uc"]["first_audio_ms"]["p50"], 128.0)
        aside = self.session.dir / step / "lon-view" / scale.LATE_JOINS
        self.assertTrue((aside / "nt-uc.join-15.json").is_file())
        self.assertTrue((aside / "nt-uc.join-15.uc.json").is_file())
        self.assertFalse((aside / "nt-uc.join-14.json").exists())
        # scale-run.py analyse reads it again: the same figures.
        again = scale.control_results(self.state, self.tmp, result)
        self.assertEqual(again["joins"], figures["joins"])
        text = scale.format_summary({"run": "r1", "config": "pcm-1", "hls_share": 0,
                                     "steps": [{"listeners": 5000, **figures}]})
        self.assertIn("| 5000 | lon-view | nt-uc | 14 | 14 | 128 |", text)
        self.assertIn("past the hold", text)
        # A step stopped when a host saturated: its joins count up to then.
        stopped = {**result, "control": {**result["control"],
                                         "listeners_stopped": base_ms / 1000 + 50.0}}
        early = scale.control_results(self.state, self.tmp, stopped)
        self.assertEqual((early["joins"]["new-view"]["nt-uc"]["joins"],
                          early["joins"]["new-view"]["nt-uc"]["late"]), (6, 14))

    def test_a_step_stopped_before_its_ramp_ends_runs_no_joins(self):
        logged: list = []
        job = scale.JoinsJob(self.audio, 20, time.time() + 60, time.time() + 120, logged.append,
                             5000).start()
        job.stop()
        job.wait()
        self.assertFalse(job.record()["ran"])
        self.assertEqual(logged, [])

    def test_joins_only_at_the_steps_named(self):
        plan = scale.plan_steps(self.state, [100, 1000, 10000, 16000], "pcm-1", hls_share=0.0,
                                standby=STANDBY, joins=20, joins_from=1000, joins_at=[100, 10000])
        self.assertEqual([step["joins"] for step in plan["steps"]], [20, 0, 20, 0])
        self.assertEqual([step["hold_s"] for step in plan["steps"]], [140.0, 60.0, 140.0, 60.0])
        self.assertEqual(plan["joins"]["at"], [100, 10000])
        args = scale.build_parser().parse_args(
            ["run", "--steps", "100,1000,10000", "--joins-at", "100,10000", "--hls-share", "0"])
        scale.resolve_defaults(args)
        built = scale.build_plan(args, self.state, STANDBY)
        self.assertEqual([step["joins"] for step in built["steps"]], [20, 0, 20])


class Schedule(unittest.TestCase):
    """The whole run in six billed hours without Sydney (agreed with the
    referee, 22 November): lab up to `down`, the two windows first, ending
    at least 10 minutes before the watchdog's `down` (6.0 h less 5 min)."""

    @classmethod
    def setUpClass(cls):
        with redirect_stdout(io.StringIO()) as out:
            scale.main(["plan", "--topology-only", "--json"])
        cls.plan = json.loads(out.getvalue())
        cls.schedule = cls.plan["schedule"]

    def test_plan_json_reports_a_total_within_the_watchdogs_limit(self):
        schedule = self.schedule
        self.assertEqual(schedule["watchdog_hours"], 6.0)
        self.assertEqual(schedule["down_at_min"], 6 * 60 - 5)
        self.assertEqual(schedule["limit_min"], 6 * 60 - 5 - 10)
        self.assertLessEqual(schedule["total_min"], schedule["limit_min"])
        self.assertTrue(schedule["fits"])
        self.assertAlmostEqual(schedule["total_min"],
                               sum(row["minutes"] for row in schedule["blocks"]), places=0)
        self.assertEqual(schedule["sites_left_out"], ["sydney"])
        self.assertEqual(schedule["nodes"], 69)
        self.assertAlmostEqual(schedule["hourly_usd"], 7.884, places=3)
        self.assertEqual(schedule["billed_hours"], 6)
        self.assertAlmostEqual(schedule["instances_usd"], 47.30, places=2)
        # The referee's budget: well under $67 with the LiveKit crowd's transfer.
        self.assertLess(schedule["instances_usd"] + schedule["livekit_public_usd_past_pool"], 67)

    def test_the_windows_come_first_and_f_r_a_and_the_livekit_control_are_kept(self):
        names = [row["name"] for row in self.schedule["blocks"]]
        self.assertEqual(names[:3], ["bring-up", "A-headline", "B-4k-gso"])
        self.assertEqual(names[-1], "down")
        for kept in ("ph-f", "ph-r", "ph-a", "c1-opus1-mixed", "c2-pcm-udp", "c2-opus25-udp",
                     "c2-opus25-mixed"):
            self.assertIn(kept, names)
        ramps = [row for row in self.schedule["blocks"] if row["kind"] == "ramp"]
        self.assertTrue(all(row["livekit_public_gb"] > 0 for row in ramps))
        phases = {row["name"]: row for row in self.schedule["blocks"] if row["kind"] == "phases"}
        self.assertEqual(sorted(phases), ["ph-a", "ph-f", "ph-r"])
        self.assertTrue(all(row["steps"] == [6000] for row in phases.values()))
        # Joins only at the representative steps (the referee's own sessions
        # of window A run theirs in full).
        joins = {row["name"]: row["joins_steps"] for row in self.schedule["blocks"]
                 if row.get("joins_steps")}
        self.assertEqual(joins, {"c1-pcm-udp": [100, 10000], "c1-opus25-udp": [40000]})
        self.assertEqual(self.schedule["joins_steps"], 3)

    def test_every_ramp_that_runs_a_step_over_the_listener_hosts_is_forced(self):
        # `run` refuses a step over the headroom without --force: a block
        # that would be refused on the day is not a block of the schedule.
        for row in self.schedule["blocks"]:
            if row.get("undrivable"):
                self.assertTrue(row["force"], row["name"])

    def test_a_schedule_past_the_limit_does_not_fit(self):
        spec = json.loads(PHASES.read_text())["schedule"]
        spec = {**spec, "blocks": spec["blocks"] + [{"name": "late", "minutes": 30}]}
        longer = scale.plan_schedule(spec, lab(["sydney"]), lab()["standby_edges"])
        self.assertFalse(longer["fits"])
        self.assertEqual(longer["total_min"], round(self.schedule["total_min"] + 30, 1))

    def test_scale_md_states_the_planned_total(self):
        text = (scale.ROOT / "deploy" / "low-latency" / "SCALE.md").read_text()
        total = re.search(r"\| \*\*Total\*\* \|[^|]*\| \*\*([\d.]+) min", text)
        self.assertIsNotNone(total, "SCALE.md's timeline has no total row")
        self.assertAlmostEqual(float(total.group(1)), self.schedule["total_min"], delta=0.05)


if __name__ == "__main__":
    unittest.main()
