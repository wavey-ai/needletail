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
detector, a scenario's before/during/after analysis from files, `analyse`
on a phase run's directory, the serving edges as the only receiving edges,
missing parts booked when they were due (the load tool's settle time, its
final line, a clean baseline), the runner's order, Unix-µs events and owed
restores, and the plan's phase cost and default step.
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


# Edges 1 to 5 of every region: in service on the first curve.
SERVING = sorted((name for name, node in lab()["nodes"].items()
                  if node["role"] == "edge" and name not in STANDBY), key=scale.natural)


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
        command = scale.sender_command(["opus-1"], "10.80.1.5", 600,
                                       "/var/lib/compare/audio/a.s24le")
        self.assertIn("--target 10.80.1.5:27100", command)
        self.assertIn("--session-id ${START}001000000", command)
        self.assertIn("--payload opus --epoch-samples 48", command)
        self.assertIn("--opus-bitrate 320000", command)
        pcm = scale.sender_command(["pcm-1"], "10.80.1.5", 600, "/x")
        self.assertNotIn("--opus-bitrate", pcm)
        self.assertIn("--session-id ${START}001000000", pcm)
        self.assertIn("${START}002500000", scale.sender_command(["opus-2.5"], "h", 1, "/x"))
        # --probe-configs (C CL7): one sender a published config from one
        # START, the second session one ns on (as change-run.py sends its
        # lanes), each with files of its own.
        both = scale.sender_command(["opus-1", "pcm-1"], "h", 1, "/x")
        self.assertEqual(both.count("echo $START"), 1)
        self.assertEqual(both.count("aep1-48k-probe send"), 2)
        self.assertIn("--session-id ${START}001000000 --group-id 0 --duration-seconds 1 "
                      "--payload opus", both)
        self.assertIn("--session-id ${START}001000001 --group-id 0 --duration-seconds 1 "
                      "--payload pcm", both)
        self.assertIn(">/var/lib/needletail-runs/scale/source-pcm-1.json", both)
        self.assertEqual(scale.probe_configs("opus-1,pcm-1", "opus-1"), ["opus-1", "pcm-1"])
        self.assertEqual(scale.probe_configs(None, "pcm-1"), ["pcm-1"])
        with self.assertRaises(ValueError):
            scale.probe_configs("opus-2.5", "opus-1")  # both stream 3001
        with self.assertRaises(ValueError):
            scale.probe_configs("flac", "opus-1")

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


class FakeProcess:
    """A change command that has already exited with `code`."""

    def __init__(self, command, code):
        self.command = command
        self.returncode = code

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def send_signal(self, number):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass


class SlowProcess:
    """A change command that takes `seconds` to exit by itself, or ends at
    the first signal it does not ignore; every signal sent is recorded."""

    def __init__(self, command, seconds, ignore=()):
        self.command = command
        self.ends = time.time() + seconds
        self.ignore = set(ignore)
        self.signals = []
        self.returncode = None

    def poll(self):
        if self.returncode is None and time.time() >= self.ends:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        deadline = time.time() + (timeout if timeout is not None else 3600)
        while self.poll() is None:
            if time.time() >= deadline:
                raise subprocess.TimeoutExpired(self.command, timeout)
            time.sleep(0.02)
        return self.returncode

    def signal(self, name, code):
        self.signals.append(name)
        if name not in self.ignore and self.returncode is None:
            self.returncode = code

    def send_signal(self, number):
        self.signal("SIGINT", 130)

    def terminate(self):
        self.signal("SIGTERM", -15)

    def kill(self):
        self.signal("SIGKILL", -9)


ROLES = {name: node["role"] for name, node in lab()["nodes"].items()}


def write_scenario_marks(step_dir: pathlib.Path, start: float, timeline: dict) -> None:
    """Each scenario's marks as the runner writes them: one label from 2 s
    after its planned fault to 5 s before its planned restore, so that its
    window (widened as the referee widens it) is the planned one."""
    for row in timeline["scenarios"]:
        directory = step_dir / "scenarios" / row["name"]
        directory.mkdir(parents=True, exist_ok=True)
        first = int((start + row["fault"] + scale.CW_BEFORE_S) * 1e9)
        last = int((start + row["observe_end"] - scale.CW_AFTER_S) * 1e9)
        (directory / "marks.txt").write_text(
            scale.mark_line("start", first, f"{row['name']}.change")
            + scale.mark_line("end", last, f"{row['name']}.change"))


def change_figures(**overrides) -> dict:
    """The figures of C CL7 that a scenario's result carries (analyse_phases
    writes them from its files), quiet: no crowd figure (a load tool before
    B Y4), no reader, no probe, nothing restarted."""
    empty = scale.crowd_window([], 0.0, 0.0, 1.5, False)
    out = {"ran": True, "crowd": {key: dict(empty) for key in ("before", "during", "after")},
           "unit_ms": 1.0, "readers": {"readers": 0, "stalls": 0, "longest_stall_ms": 0,
                                       "parts_missing_run": 0},
           "probe": None, "restarts": {}, "restarted_expected": [], "changes": [],
           "builds": {"before": {}, "after": {}, "rx_invalid_frames_rise": None},
           "commands": [], "waits": [], "controller": {"decisions": [], "kinds": []}}
    out.update(overrides)
    return out


class Phases(unittest.TestCase):
    """The failover, redistribution and autoscale phases (items 4 and 5)."""

    def test_the_committed_plan_loads_and_disabled_scenarios_run_only_by_name(self):
        plan = scale.load_phase_plan(PHASES, "F")
        names = [item["name"] for item in plan["scenarios"]]
        self.assertEqual(names, ["F1", "F2", "F2r", "F3", "F4", "F5", "F6", "F7", "F8"])
        self.assertNotIn("F9", names)  # waits for AM item 11
        self.assertEqual([item["name"] for item in scale.load_phase_plan(
            PHASES, names=["F9"])["scenarios"]], ["F9"])
        # C CL7: R4 and A3 are enabled.
        self.assertEqual([item["name"] for item in scale.load_phase_plan(PHASES, "R")[
            "scenarios"]], ["R1", "R2", "R3", "R4", "R5"])
        self.assertEqual([item["name"] for item in scale.load_phase_plan(PHASES, "A")[
            "scenarios"]], ["A1", "A2", "A3"])
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
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "F"), lab())
        rows = {row["name"]: row for row in timeline["scenarios"]}
        # The probes start 45 s before the fault and hold 369 s: the 90 s of
        # the observation, and 234 s after it for the referee's 120
        # no-change windows of 97 s (and 10 more); the slot ends 30 s after.
        self.assertEqual((rows["F1"]["base_start"], rows["F1"]["probe_start"],
                          rows["F1"]["fault"], rows["F1"]["observe_end"], rows["F1"]["probe_s"],
                          rows["F1"]["end"]), (0, 15, 60, 150, 369, 414))
        self.assertEqual(rows["F2"]["fault"], 474)
        self.assertEqual(timeline["hold_s"], 9 * 414)
        self.assertEqual([(item["kind"], item["targets"]) for item in rows["F2"]["restores"]],
                         [("start", ["new-edge-2"])])
        self.assertEqual([(item["kind"], item["t"]) for item in rows["F1"]["restores"]],
                         [("resume", 150)])
        self.assertEqual(rows["F2r"]["restores"], [])  # a crash restarts by itself
        self.assertEqual([item["kind"] for item in rows["F3"]["restores"]], ["start"])
        self.assertEqual(rows["F3"]["actions"][1]["t"], rows["F3"]["fault"] + 20)
        self.assertIn("systemctl stop", scale.NODE_COMMANDS["kill"])
        self.assertNotIn("systemctl stop", scale.NODE_COMMANDS["crash"])
        r4 = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["R4"]), lab())["scenarios"][0]
        self.assertEqual(r4["observe_end"] - r4["fault"], 120 + 90)
        self.assertEqual([item["kind"] for item in r4["restores"]], ["leave"])

    def test_entries_and_liveness_avoid_the_nodes_under_fault(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "F"), lab())
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
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "F"), lab())
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
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["A1", "A2"]),
                                        lab())
        a2 = timeline["scenarios"][1]
        spec = scale.profile_ramp(1000, 10.0, timeline)
        points = [(float(at), int(count)) for count, at in
                  (item.split("@") for item in spec.split(","))]
        self.assertEqual(points, sorted(points))
        by_time = dict((at, count) for at, count in points)
        self.assertEqual(by_time[10.0 + 60], 300)        # A1 starts at 30%
        self.assertEqual(by_time[10.0 + 540], 950)       # 95% after 8 min
        # A1's probes hold on after its 9.5 minutes (1,374 s in all), the
        # crowd at 95% until A2 begins; A2 ends at 20%.
        self.assertEqual(a2["fault"], 1374 + 60)
        self.assertEqual(points[-1], (10.0 + a2["fault"] + 480, 200))
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
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "A"), lab())
        # An autoscale ramp faults nothing: an edge that saturates as A1
        # ramps to 95% is reported.
        exclude = scale.exclusion(timeline, start)
        self.assertEqual(exclude, {"windows": [], "node_windows": {}})
        first = scale.gave_out_first(lab(), {}, {"lon-edge-1": start + 500}, {}, exclude)
        self.assertEqual(first["node"], "lon-edge-1")
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, "F"), lab())
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
                         am10: bool = False, left_lost: bool = False) -> None:
        """A step of F1 (drain lon-edge-1) from its files: lon-load-1 with
        listeners on lon-edge-1 and lon-edge-2, which move off lon-edge-1 at
        the fault onto lon-edge-2 (and two onto lon-edge-3), their missing
        parts booked on lon-edge-2 as needletail-load does, in the intervals
        where it counts them (1.5 s after they were due); a survivor
        sampler; the mesh samplers; a reader report with one move."""
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
            # counters), never on the edge it left, and when the newest part
            # is 1,500 past them (settle()): two intervals after the moves.
            settled = fault + 2 <= t < fault + 4
            edges = {address["lon-edge-1"]: {"up": 0 if t >= fault + 2 else 50,
                                             "parts": 0 if t >= fault + 2 else 50_000,
                                             "missing": 0, "moves": 0},
                     address["lon-edge-2"]: {"up": 98 if t >= fault + 2 else 50, "parts": 50_000,
                                             "missing": 1 if settled else 0,
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
                if left_lost:
                    # Wave 8 review: the moves' losses booked on the edge
                    # left; and a listener of lon-edge-3 (not faulted) that
                    # stalls, moves and loses a part 5 s later: collateral.
                    for edge in edges.values():
                        edge["left_lost"] = 0
                    edges[address["lon-edge-1"]]["left_lost"] = 1 if settled else 0
                    if int(t - fault) == 5:
                        edges[address["lon-edge-3"]].update(
                            missing=1, missing_moved=1, left_lost=1)
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
        write_scenario_marks(directory, start, timeline)

    def test_a_scenario_from_its_files_before_during_after(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        plan = scale.load_phase_plan(PHASES, names=["F1"])
        timeline = scale.phase_timeline(plan, lab())
        start = 1_790_000_000.0
        self.write_phase_step(directory, start, timeline)
        events = [{"unix_us": int((start + 60) * 1e6), "event": "action", "node": "lon-edge-1"},
                  {"unix_us": int((start + 500) * 1e6), "event": "later"}]
        result = scale.analyse_phases(lab(), directory, timeline, start, plan, SERVING,
                                      "pcm-1", events, readers_launched=start - 10)[0]
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
        # Wave 9 C4: judged against each receiving edge's headroom, which
        # needs their loads before the fault (none sampled here: open); the
        # old equal-spread figure stays in the record.
        share = "each edge's moves within 1.3x of its headroom-weighted share"
        self.assertIsNone(criteria[share]["ok"])
        self.assertIn("load before the fault", criteria[share]["needs"])
        self.assertIn("listeners per receiving edge after the moves max/mean <= 1.15",
                      criteria)
        self.assertNotIn("moves spread max/mean <= 1.3", criteria)
        self.assertIsNone(criteria["time_to_move_p99_ms <= 300"]["ok"])
        self.assertIn("needletail-load", criteria["time_to_move_p99_ms <= 300"]["needs"])
        self.assertTrue(criteria["survivors: busiest core < 90%"]["ok"])
        self.assertTrue(criteria["survivors: no rise in client_udp.would_block"]["ok"])
        self.assertEqual(result["survivors"]["during"]["cpu_max"], 0.5)
        text = scale.format_phases([result])
        self.assertIn("| F1 | lon-edge-1 |", text)
        self.assertIn("FAIL missing = 0", text)

    def test_moves_are_judged_against_each_edges_headroom(self):
        """Wave 9 C4: the wave 8 local F1 (36 movers, 17/9/10 into survivors
        at 285/450/404 permille): 1.42x the equal share, but within 1.3x of
        each survivor's headroom-weighted share; the listeners after are
        judged by their own max/mean."""
        moves = {"lon-edge-2": 17, "lon-edge-3": 9, "lon-edge-4": 10}
        loads = {"lon-edge-2": 285, "lon-edge-3": 450, "lon-edge-4": 404}
        self.assertEqual(scale.spread(moves, list(loads)), 1.417)
        shares = scale.headroom_shares(moves, loads)
        # Wave 9 review: the headroom is to the rebalance target (800):
        # 515 of 1,261 for lon-edge-2 (to 1,000: 13.83 of 36).
        self.assertAlmostEqual(shares["lon-edge-2"]["expected"], 14.70, places=2)
        self.assertLess(scale.share_worst(shares), 1.3)
        full = scale.headroom_shares(moves, loads, level=1000)
        self.assertAlmostEqual(full["lon-edge-2"]["expected"], 13.83, places=2)
        # A survivor over the level is due none; with a listener's cost the
        # level rises to where the moves fit (36 x 40 over 285/450/404:
        # 859.7).
        busy = {"lon-edge-2": 285, "lon-edge-3": 820, "lon-edge-4": 404}
        shares = scale.headroom_shares({"lon-edge-2": 20, "lon-edge-4": 16}, busy)
        self.assertEqual(shares["lon-edge-3"]["expected"], 0)
        self.assertIsNone(shares["lon-edge-3"]["ratio"])
        raised = scale.headroom_shares(moves, loads, cost=40.0)
        self.assertAlmostEqual(raised["lon-edge-2"]["level"], 859.7, places=1)
        self.assertEqual(scale.listener_cost({"a": 600, "b": 300, "c": 100},
                                             {"a": 40, "b": 30, "c": 2}), 15.0)
        self.assertAlmostEqual(scale.fill_level([394, 553, 747], 658), 784.0)
        # All onto one edge: far off its share either way.
        piled = scale.headroom_shares({"lon-edge-2": 36}, loads)
        self.assertGreater(scale.share_worst(piled), 1.3)
        self.assertEqual(scale.share_worst(piled), float("inf"))
        self.assertIsNone(scale.headroom_shares(moves, {**loads, "lon-edge-4": None}))
        self.assertEqual(scale.balance({"a": 48, "b": 58, "c": 54}), 1.087)
        self.assertIsNone(scale.balance({"a": 48, "b": None}))

    def test_a_scenario_judges_moved_listeners_apart_with_the_am10_load(self):
        """needletail-load of Phase AM item 10 counts a moved listener's
        missing parts and times each move: the unaffected listeners' 0
        missing and the time to move are judged."""
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        plan = scale.load_phase_plan(PHASES, names=["F1"])
        timeline = scale.phase_timeline(plan, lab())
        start = 1_790_000_000.0
        self.write_phase_step(directory, start, timeline, am10=True)
        result = scale.analyse_phases(lab(), directory, timeline, start, plan, SERVING,
                                      "pcm-1", [], readers_launched=start - 10)[0]
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

    def test_collateral_losses_of_unaffected_listeners_are_not_hidden(self):
        """Wave 8 review: the fault's losses are the parts lost in moves away
        from the faulted node (left_lost); a listener of an edge not faulted
        that moves and loses parts counts as unaffected, where missing_moved
        (every loss of a listener that ever moved) hid it."""
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        plan = scale.load_phase_plan(PHASES, names=["F1"])
        timeline = scale.phase_timeline(plan, lab())
        start = 1_790_000_000.0
        self.write_phase_step(directory, start, timeline, am10=True, left_lost=True)
        result = scale.analyse_phases(lab(), directory, timeline, start, plan, SERVING,
                                      "pcm-1", [], readers_launched=start - 10)[0]
        during = result["udp"]["during"]
        self.assertEqual(during["faulted"]["left_lost"], 2)
        self.assertEqual(during["unaffected"]["left_lost"], 1)
        criteria = {item["criterion"]: item for item in result["criteria"]}
        unaffected = criteria["unaffected listeners alone: 0 missing"]
        self.assertFalse(unaffected["ok"], unaffected)
        self.assertEqual(unaffected["value"], 1)

    def test_analyse_reads_back_a_phase_run(self):
        """`analyse` on a phase run's directory as `run` leaves it (plan.json,
        phases.json, events.jsonl, scale.json, steps/N/HOST/...): the faults'
        windows, the serving edges and the lane come back from the files,
        and the step and its scenario read the same again."""
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        directory = tmp / "scale-ph-f"
        state = lab()
        phase_plan = scale.load_phase_plan(PHASES, names=["F1"])
        timeline = scale.phase_timeline(phase_plan, lab())
        plan = scale.plan_steps(state, [6000], "pcm-1", 0.05, standby=STANDBY,
                                hold=timeline["hold_s"])
        step = plan["steps"][0]
        start = 1_790_000_000.0  # the end of the ramp
        fault = start + timeline["scenarios"][0]["fault"]
        step_dir = directory / "steps" / str(step["listeners"])
        step_dir.mkdir(parents=True)
        self.write_phase_step(step_dir, start, timeline, am10=True, left_lost=True)
        # The drained edge's last listeners miss every part of one interval:
        # an edge that gave out, were it not under its own fault.
        intervals = step_dir / "lon-load-1" / "udp" / "intervals.jsonl"
        lines = [json.loads(line) for line in intervals.read_text().splitlines()]
        drained = f"{state['nodes']['lon-edge-1']['vlan_ipv4']}:19446"
        for line in lines:
            if line["unix_ms"] == int((fault + 3) * 1000):
                line["edges"][drained]["missing"] = 600
        intervals.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        window = (start, start + timeline["hold_s"])
        self.assertEqual(scale.analyse_step(state, step_dir, step, window)[
            "gave_out_first"]["node"], "lon-edge-1")
        events = [{"unix_us": int(fault * 1e6), "source": "scale-run", "event": "action",
                   "kind": "drain", "node": "lon-edge-1", "scenario": "F1"}]
        (directory / "events.jsonl").write_text(
            "\n".join(json.dumps(item) for item in events) + "\n")
        (directory / "plan.json").write_text(json.dumps(plan, indent=1) + "\n")
        (directory / "phases.json").write_text(json.dumps(
            {"plan": phase_plan, "timeline": timeline, "serving_edges": SERVING}, indent=1) + "\n")
        # The step's record as run_step leaves it.
        exclude = scale.exclusion(timeline, start)
        first = scale.analyse_step(state, step_dir, step, window, "pcm-1", exclude)
        first["stopped"] = None
        first["ramp_s"], first["hold_s"] = step["ramp_s"], step["hold_s"]
        first["phase_start"], first["readers_launched"] = start, start - 10
        first["exclude"] = exclude
        first["phases"] = scale.analyse_phases(state, step_dir, timeline, start, phase_plan,
                                               SERVING, "pcm-1", events, start - 10)
        scale.write_results(directory, {"run": "ph-f", "config": "pcm-1", "hls_share": 0.05,
                                        "standby_edges": STANDBY, "steps": [first]})
        written = json.loads((directory / "scale.json").read_text())
        (directory / "summary.md").unlink()
        args = argparse.Namespace(run=str(directory), topology=str(TOPOLOGY), topology_only=True)
        with redirect_stdout(io.StringIO()) as out:
            scale.command_analyse(args)
        again = json.loads((directory / "scale.json").read_text())
        self.assertEqual(again, written)
        outcome = again["steps"][0]
        # The fault's windows came back: the drained edge did not give out.
        self.assertIsNone(outcome["gave_out_first"])
        self.assertEqual(outcome["exclude"]["node_windows"]["lon-edge-1"],
                         [[fault, start + timeline["hold_s"]]])
        scenario = outcome["phases"][0]
        self.assertEqual((scenario["name"], scenario["targets"]), ("F1", ["lon-edge-1"]))
        self.assertEqual(scenario["udp"]["during"]["faulted"]["missing"], 600)
        self.assertEqual(sorted(scenario["moves"]["listeners_after"]),
                         ["lon-edge-2", "lon-edge-3", "lon-edge-4", "lon-edge-5"])
        self.assertEqual([item["event"] for item in scenario["events"]], ["action"])
        self.assertIn("| F1 | lon-edge-1 |", out.getvalue())
        self.assertEqual(out.getvalue(), (directory / "summary.md").read_text() + "\n")

    def write_f1_moves(self, state: dict, taken: dict[str, int]):
        """A step directory of F1: 50 listeners on each of London's five
        serving edges; lon-edge-1 drains and its listeners move in the
        second after the fault, `taken` {edge: count} of them to each other
        edge. The directory, the plan, the timeline and the start."""
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        plan = scale.load_phase_plan(PHASES, names=["F1"])
        timeline = scale.phase_timeline(plan, lab())
        row = timeline["scenarios"][0]
        start = 1_790_000_000.0
        fault = start + row["fault"]
        london = [f"lon-edge-{index}" for index in range(1, 11)]
        address = {name: f"{state['nodes'][name]['vlan_ipv4']}:19446" for name in london[:5]}
        lines = []
        for second in range(int(row["end"]) + 1):
            t = start + second
            moved, moving = t > fault, t == fault + 1
            edges = {address["lon-edge-1"]: {"up": 0 if moved else 50, "moves": 0,
                                             "parts": 0 if moved else 50_000, "missing": 0}}
            for name, count in taken.items():
                edges[address[name]] = {
                    "up": 50 + count if moved else 50, "parts": 50_000, "missing": 0,
                    "moves": count if moving else 0,
                    "moves_in_by_reason": {"drain": count} if moving else {}}
            lines.append({"unix_ms": int(t * 1000), "phase": "run", "edges": edges,
                          "total": {"up": 250, "active": 250, "parts": 250_000, "missing": 0}})
        host = directory / "lon-load-1" / "udp"
        host.mkdir(parents=True)
        (host / "intervals.jsonl").write_text("\n".join(json.dumps(line) for line in lines))
        for name in london:
            rows = []
            for second in range(int(row["end"]) + 1):
                t = start + second
                if name in STANDBY:
                    # The sampler of a stopped node: no endpoint answers.
                    rows.append({"t": t, "node": name})
                    continue
                listeners = 50 if t <= fault else 50 + taken.get(name, -50)
                rows.append({"t": t, "node": name, "load_permille": 8 * listeners,
                             "draining": False, "sessions": 0,
                             "client_udp": {"subscribers": listeners}})
            (directory / name).mkdir()
            (directory / name / "mesh.jsonl").write_text(
                "\n".join(json.dumps(item) for item in rows))
        write_scenario_marks(directory, start, timeline)
        return directory, plan, timeline, start

    def test_standby_edges_are_not_receiving_edges(self):
        """The edges that take a faulted edge's listeners are those in
        service when the phases began. An edge held out on standby is
        stopped: it has no load and no gauge, and counted as a receiving
        edge it left the share and the balance of F1 and F2 unjudged."""
        state = lab()
        asked = []

        def answer(node, path, timeout=5.0):
            asked.append((node["name"], path))
            if node["name"] == "lon-edge-2":
                return {"state": "draining", "ready": False, "not_ready": ["draining: operator"],
                        "drain_reasons": ["operator"]}
            if node["name"] == "new-edge-3":
                return {"error": "timed out"}
            return {"state": "ready", "ready": True, "not_ready": []}

        with mock.patch.object(scale, "http_get", answer), \
                mock.patch.object(scale.time, "sleep", lambda seconds: None):
            serving, left_out = scale.serving_edges(state, STANDBY)
        self.assertEqual(serving, [name for name in SERVING
                                   if name not in ("lon-edge-2", "new-edge-3")])
        self.assertEqual(left_out, {
            "lon-edge-2": {"state": "draining", "not_ready": ["draining: operator"],
                           "drain_reasons": ["operator"]},
            "new-edge-3": {"error": "timed out"}})
        # An edge on standby is not asked: it is out whatever it would say.
        # One that does not answer is asked again.
        self.assertEqual(sorted(asked), sorted(
            [(name, "/api/node") for name in SERVING]
            + [("new-edge-3", "/api/node")] * scale.NODE_RETRIES))

        london = [f"lon-edge-{index}" for index in range(1, 11)]
        # lon-edge-1 drains and its 50 listeners move to the other four
        # serving edges: 13, 12, 13 and 12.
        directory, plan, timeline, start = self.write_f1_moves(
            state, dict(zip(london[1:5], (13, 12, 13, 12))))
        share = "each edge's moves within 1.3x of its headroom-weighted share"
        after = "listeners per receiving edge after the moves max/mean <= 1.15"
        result = scale.analyse_phases(state, directory, timeline, start, plan, SERVING,
                                      "pcm-1")[0]
        criteria = {item["criterion"]: item for item in result["criteria"]}
        self.assertEqual(result["moves"]["listeners_after"],
                         {"lon-edge-2": 63, "lon-edge-3": 62, "lon-edge-4": 63, "lon-edge-5": 62})
        self.assertEqual(sorted(result["moves"]["udp_by_headroom"]), london[1:5])
        self.assertTrue(criteria[share]["ok"], criteria[share])
        self.assertEqual(criteria[share]["value"]["worst"], 1.042)  # 12 of a due 12.5
        self.assertTrue(criteria[after]["ok"], criteria[after])
        self.assertEqual(criteria[after]["value"], 1.008)
        self.assertEqual(result["moves"]["udp_spread_max_over_mean"], 1.04)
        # With every edge of the site counted, as before: nothing judged,
        # and the spread thinned over five edges that took nobody.
        every = [name for name, node in state["nodes"].items() if node["role"] == "edge"]
        result = scale.analyse_phases(state, directory, timeline, start, plan,
                                      sorted(every, key=scale.natural), "pcm-1")[0]
        criteria = {item["criterion"]: item for item in result["criteria"]}
        self.assertIsNone(criteria[share]["ok"])
        self.assertIsNone(criteria[after]["ok"])
        self.assertEqual(result["moves"]["udp_spread_max_over_mean"], 2.34)

    def test_moves_into_an_edge_left_out_fail_the_share_and_the_balance(self):
        """Review of CL6: an edge in service that serving_edges left out
        still takes listeners. Its moves are weighed, and the share and the
        balance fail naming it, never pass over the edges that were listed."""
        state = lab()
        london = [f"lon-edge-{index}" for index in range(2, 6)]
        # lon-edge-5 takes 40 of lon-edge-1's 50: the imbalance F1 catches.
        directory, plan, timeline, start = self.write_f1_moves(
            state, dict(zip(london, (3, 3, 4, 40))))
        share = "each edge's moves within 1.3x of its headroom-weighted share"
        after = "listeners per receiving edge after the moves max/mean <= 1.15"

        def criteria(serving: list[str]) -> tuple[dict, dict]:
            result = scale.analyse_phases(state, directory, timeline, start, plan, serving,
                                          "pcm-1")[0]
            return result, {item["criterion"]: item for item in result["criteria"]}

        result, judged = criteria(SERVING)
        self.assertEqual(result["moves"]["unlisted_receiving"], [])
        self.assertFalse(judged[share]["ok"])
        self.assertEqual(judged[share]["value"]["worst"], 4.167)
        self.assertFalse(judged[after]["ok"])
        self.assertEqual(judged[after]["value"], 1.44)
        result, judged = criteria([name for name in SERVING if name != "lon-edge-5"])
        self.assertEqual(result["moves"]["unlisted_receiving"], ["lon-edge-5"])
        self.assertEqual(sorted(result["moves"]["listeners_after"]), london)
        self.assertFalse(judged[share]["ok"], judged[share])
        self.assertEqual(judged[share]["value"]["not_serving"], ["lon-edge-5"])
        self.assertEqual(judged[share]["value"]["worst"], 4.167)
        self.assertFalse(judged[after]["ok"], judged[after])
        self.assertEqual(judged[after]["value"], {"max_over_mean": 1.44,
                                                  "not_serving": ["lon-edge-5"]})
        self.assertIn("not_serving", scale.format_phases([result]))

    def test_a_phase_run_records_its_serving_edges_or_does_not_start(self):
        """`run` asks every edge not on standby for /api/node as the phases
        begin, asks again an edge that did not answer, and writes the ready
        ones to phases.json, which `analyse` requires; an edge that never
        answers, a build without /api/node, or no ready edge stops the run
        before anything is started."""
        state = lab()
        phase_plan = scale.load_phase_plan(PHASES, names=["F1"])
        timeline = scale.phase_timeline(phase_plan, lab())
        plan = scale.plan_steps(state, [6000], "pcm-1", 0.05, standby=STANDBY,
                                hold=timeline["hold_s"])
        plan["cost"] = scale.cost_estimate(state, plan)
        asked: list[str] = []

        def begin(answer) -> tuple[pathlib.Path, dict, list[dict]]:
            directory = pathlib.Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, directory, True)
            phases, logged = {"plan": phase_plan, "timeline": timeline}, []
            asked.clear()

            def get(node, path, timeout=5.0):
                asked.append(node["name"])
                return answer(node["name"], asked.count(node["name"]))
            with mock.patch.object(scale, "http_get", get), \
                    mock.patch.object(scale.time, "sleep", lambda seconds: None):
                scale.begin_phases(state, STANDBY, phases, plan, directory, logged.append)
            return directory, phases, logged

        ready = {"state": "ready", "ready": True, "not_ready": []}
        # new-edge-3 times out once and answers the second time.
        directory, phases, logged = begin(
            lambda name, tries: {"error": "timed out"} if name == "new-edge-3" and tries == 1
            else ready)
        self.assertEqual(asked.count("new-edge-3"), 2)
        self.assertEqual(phases["serving_edges"], SERVING)
        written = json.loads((directory / "phases.json").read_text())
        self.assertEqual(written["serving_edges"], SERVING)
        self.assertEqual(written["timeline"], timeline)
        self.assertIn("pro_rata_usd", written["cost"])
        self.assertEqual(logged, [{"serving_edges": SERVING, "not_ready": {}}])
        # A draining edge is left out by its own word.
        directory, phases, logged = begin(
            lambda name, tries: {"state": "draining", "ready": False,
                                 "drain_reasons": ["operator"]}
            if name == "lon-edge-2" else ready)
        self.assertNotIn("lon-edge-2", phases["serving_edges"])
        self.assertEqual(logged[0]["not_ready"],
                         {"lon-edge-2": {"state": "draining", "drain_reasons": ["operator"]}})
        for answer, text in (
                (lambda name, tries: {"error": "timed out"} if name == "new-edge-3" else ready,
                 "new-edge-3 (timed out)"),
                (lambda name, tries: {"error": "HTTP Error 404: Not Found"},
                 "this build has no /api/node"),
                (lambda name, tries: {"state": "starting", "ready": False},
                 "no edge in service says it is ready")):
            with self.assertRaises(SystemExit) as caught:
                begin(answer)
            self.assertIn(text, str(caught.exception))
        self.assertEqual(asked.count("lon-edge-1"), 1)
        # An edge that never answers is asked three times in all.
        with self.assertRaises(SystemExit):
            begin(lambda name, tries: {"error": "timed out"} if name == "new-edge-3" else ready)
        self.assertEqual(asked.count("new-edge-3"), 1 + scale.NODE_RETRIES)

    def test_a_phase_run_keeps_the_load_tools_settle_time(self):
        """Review of CL6: the analysis books losses by the load tool's
        default settle time, so a phase run refuses another."""
        for extra in ("--settle-parts 500", "--quiet --settle-ms=3500"):
            args = argparse.Namespace(phase_plan=str(PHASES), group="R", scenarios="",
                                      steps=None, hls_share=None, config=None, joins=None,
                                      lk_per_sfu=None, load_args=extra)
            with self.assertRaises(ValueError) as caught:
                scale.phase_setup(args, lab())
            self.assertIn("settle", str(caught.exception))
        args = argparse.Namespace(phase_plan=str(PHASES), group="R", scenarios="", steps=None,
                                  hls_share=None, config=None, joins=None, lk_per_sfu=None,
                                  load_args="--quiet")
        self.assertIsNotNone(scale.phase_setup(args, lab()))

    def test_a_late_settle_is_booked_in_its_own_scenario(self):
        """needletail-load counts a part missing 1,500 parts after it was due
        and its final line carries what was still open when it stopped: a
        scenario's last losses are booked in that scenario, the final line
        in the last window, and a loss in a scenario's baseline voids it."""
        self.assertEqual(scale.settle_seconds("pcm-1"), 1.5)
        self.assertEqual(scale.settle_seconds("opus-2.5"), 3.75)
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        plan = scale.load_phase_plan(PHASES, names=["F1", "F2"])
        timeline = scale.phase_timeline(plan, lab())
        self.assertEqual([(row["base_start"], row["end"]) for row in timeline["scenarios"]],
                         [(0, 414), (414, 828)])
        state = lab()
        start = 1_790_000_000.0
        address = f"{state['nodes']['lon-edge-2']['vlan_ipv4']}:19446"
        (directory / "lon-load-1" / "udp").mkdir(parents=True)
        write_scenario_marks(directory, start, timeline)

        def analyse(missing_in: dict[int, int], final_missing: int = 0,
                    config: str = "pcm-1") -> dict:
            """The scenarios of a step whose load tool counted `missing_in`
            {second of the hold: parts} and `final_missing` as it stopped."""
            lines = [{"schema": "needletail.load.interval.v1", "phase": "run",
                      "t": 30.0 + second, "unix_ms": int((start + second) * 1000),
                      "total": {"up": 100, "active": 100, "parts": 100_000,
                                "missing": missing_in.get(second, 0)},
                      "edges": {address: {"up": 100, "active": 100, "parts": 100_000,
                                          "missing": missing_in.get(second, 0), "moves": 0}}}
                     for second in range(1, 829)]
            # The tool's last line, after its drain: seconds from its start
            # and no Unix time (src/load.rs).
            lines.append({"schema": "needletail.load.interval.v1", "phase": "final", "t": 390.6,
                          "total": {"parts": 600, "missing": final_missing},
                          "edges": {address: {"parts": 600, "missing": final_missing,
                                              "moves": 0}}})
            (directory / "lon-load-1" / "udp" / "intervals.jsonl").write_text(
                "\n".join(json.dumps(line) for line in lines) + "\n")
            return {item["name"]: item for item in scale.analyse_phases(
                state, directory, timeline, start, plan, SERVING, config)}

        def missing(result: dict, name: str, window: str) -> int:
            return result[name]["udp"][window]["unaffected"]["missing"]

        def criterion(result: dict, name: str, text: str) -> dict:
            return {item["criterion"]: item for item in result[name]["criteria"]}[text]

        every = "all listeners (moved ones included): 0 missing"
        # A part due 0.8 s before F1 ends is counted 1.5 s later, in the
        # first interval of F2's baseline; two more are open when the tool
        # stops.
        result = analyse({415: 1}, final_missing=2)
        self.assertEqual(result["F1"]["settle_s"], 1.5)
        self.assertEqual(missing(result, "F1", "after"), 1)
        self.assertEqual(criterion(result, "F1", every)["value"], 1)
        self.assertFalse(criterion(result, "F1", every)["ok"])
        self.assertEqual(missing(result, "F2", "before"), 0)
        self.assertTrue(criterion(result, "F2", "baseline: 0 missing")["ok"])
        self.assertEqual(missing(result, "F2", "after"), 2)
        # F2's after: from its restore (564 s) to the end of its slot (828 s).
        self.assertEqual(result["F2"]["udp"]["after"]["unaffected"]["parts"],
                         264 * 100_000 + 600)
        self.assertEqual(criterion(result, "F2", every)["value"], 2)
        self.assertEqual(missing(result, "F1", "before") + missing(result, "F1", "during")
                         + missing(result, "F2", "during"), 0)
        # A part lost in F2's own baseline: whatever F2 shows is void.
        result = analyse({415: 1, 437: 1})
        baseline = criterion(result, "F2", "baseline: 0 missing")
        self.assertFalse(baseline["ok"])
        self.assertEqual(baseline["value"], 1)
        self.assertTrue(criterion(result, "F2", every)["ok"])
        self.assertIn("FAIL baseline: 0 missing (1)", scale.format_phases(list(result.values())))
        # At 2.5 ms a part is counted 3.75 s after it was due.
        self.assertEqual(missing(analyse({417: 1}), "F2", "before"), 1)
        result = analyse({417: 1}, config="opus-2.5")
        self.assertEqual(result["F1"]["settle_s"], 3.75)
        self.assertEqual((missing(result, "F1", "after"), missing(result, "F2", "before")), (1, 0))
        # No listener intervals at all (a host's file not fetched): the
        # baseline is not known clean, so it is open, not passed.
        (directory / "lon-load-1" / "udp" / "intervals.jsonl").unlink()
        result = {item["name"]: item for item in scale.analyse_phases(
            state, directory, timeline, start, plan, SERVING, "pcm-1")}
        baseline = criterion(result, "F1", "baseline: 0 missing")
        self.assertIsNone(baseline["ok"])
        self.assertEqual(baseline["needs"], "no listener intervals in the baseline")

    def test_the_autoscale_phases_need_a_datagram_capacity(self):
        """Wave 8 review: A1-A3 need client_udp_datagram_capacity in the
        deploy (an edge load that means a full CPU), and wave 9 C2 the CPU
        term; scale-run settings renders both by default (wave 9 C3), and
        --datagram-capacity 0 leaves the capacity out."""
        plan = json.loads(PHASES.read_text())
        for scenario in plan["scenarios"]:
            needs = scenario.get("needs_settings") or {}
            for key in ("client_udp_datagram_capacity", "cpu_load"):
                self.assertEqual(key in needs, scenario["name"] in ("A1", "A2", "A3"),
                                 (scenario["name"], key))
        standby = json.loads(TOPOLOGY.read_text())["standby_edges"]
        settings = scale.scale_settings(standby)
        self.assertEqual(settings["client_udp_datagram_capacity"], 400_000)
        self.assertIs(settings["cpu_load"], True)
        self.assertNotIn("client_udp_datagram_capacity", scale.scale_settings(standby, 0))
        self.assertEqual(scale.scale_settings(standby, 300_000)["client_udp_datagram_capacity"],
                         300_000)
        spec = next(item for item in plan["scenarios"] if item["name"] == "A1")
        quiet = {"nodes": [], "cpu_max": 0.1, "core_max": 0.1, "nic_share_max": 0.1}
        result = {"stranded": {"max": 0},
                  "udp": {key: {side: {"missing": 0, "parts": 1000}
                                for side in ("unaffected", "faulted")}
                          for key in ("before", "during", "after")},
                  "survivors": {key: dict(quiet) for key in ("before", "during", "after")},
                  "moves": {"udp": 0, "udp_spread_max_over_mean": None},
                  "mesh": {key: {"survivors": {}} for key in ("before", "during", "after")},
                  **change_figures(changes=["autoscale"])}
        setting = "deploy setting client_udp_datagram_capacity"
        with mock.patch.object(scale, "deploy_settings", return_value={}):
            names = [item["criterion"] for item in scale.phase_criteria(result, spec, ROLES)]
        self.assertIn(setting, names)
        with mock.patch.object(scale, "deploy_settings",
                               return_value={"client_udp_datagram_capacity": 400_000,
                                             "cpu_load": True}):
            names = [item["criterion"] for item in scale.phase_criteria(result, spec, ROLES)]
        self.assertNotIn(setting, names)
        self.assertNotIn("deploy setting cpu_load", names)

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
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F2", "F3"]), lab())
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
        # Role-aware (C CL7): a contributor is a node like any other, its
        # service its role's unit.
        self.assertEqual(scale.node_command("kill", runner.node("contrib-lon")),
                         "systemctl kill -s KILL needletail-contrib.service; "
                         "systemctl stop needletail-contrib.service; true")
        with self.assertRaises(ValueError):
            runner.node("lon-edge-99")
        # A control request that names no node would be global in av-mesh.
        with self.assertRaises(ValueError):
            scale.http_post(state["nodes"]["lon-edge-1"], "/api/control/drain-node", {})

    def test_a_step_stopped_early_starts_no_new_fault_and_restores_the_one_under_way(self):
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F2", "F3"]), lab())
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
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F8"]), lab())
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
        row = scale.phase_timeline(scale.load_phase_plan(path, names=["X1"]), lab())["scenarios"][0]
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
        timeline = scale.phase_timeline(scale.load_phase_plan(path, names=["X3"]), lab())
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

    def test_the_controller_runs_until_its_action_and_takes_its_marks(self):
        """The autoscale kind (C 2.11): `run --until join|stopped` for the
        rest of the observation, blocking; its state in the step's
        directory, its output and marks in the scenario's, its exit
        logged; one at a time, so A2's starts only when A1's is done."""
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["A1", "A2"]),
                                        lab())
        items = [row["actions"][0] for row in timeline["scenarios"]]
        self.assertEqual([(item["kind"], item["until"]) for item in items],
                         [("autoscale", "join"), ("autoscale", "stopped")])
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        started = []
        outputs = iter([("start 1790000000.000000001 autoscale.up.london\n"
                         "end 1790000030.000000001 autoscale.up.london\n", 0), ("", 1)])

        def popen(command, **kwargs):
            text, code = next(outputs)
            kwargs["stdout"].write(text.encode())
            started.append(command)
            return FakeProcess(command, code)

        logged = []
        args = argparse.Namespace(stream=4001, budget_usd=100.0, spent_before_usd=31.4)
        runner = scale.PhaseRunner(lab(), timeline, time.time(), logged.append, args,
                                   directory / "events.jsonl", directory=directory)
        with mock.patch.object(scale.subprocess, "Popen", popen), \
                mock.patch.dict(scale.os.environ, {"NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS": "7"}):
            runner.act(items[0])
            self.assertEqual(len(started), 1)
            runner.act(items[1])
        for command in started:
            self.assertEqual(command[2:5], ["run", "--backend", "linode"])
            self.assertEqual(command[command.index("--controller-state") + 1],
                             str(directory / "autoscale-state.json"))
            self.assertEqual(command[command.index("--events") + 1],
                             str(directory / "events.jsonl"))
            self.assertEqual(command[command.index("--max-watchdog-hours") + 1], "7")
            self.assertEqual(command[command.index("--budget-usd") + 1], "100.0")
            self.assertEqual(command[command.index("--spent-before-usd") + 1], "31.4")
        self.assertEqual(started[0][started[0].index("--until") + 1], "join")
        self.assertEqual(started[1][started[1].index("--until") + 1], "stopped")
        # --duration: what is left of the scenario's observation.
        self.assertGreater(int(started[0][started[0].index("--duration") + 1]), 500)
        self.assertEqual((directory / "scenarios" / "A1" / "marks.txt").read_text(),
                         "start 1790000000.000000001 autoscale.up.london\n"
                         "end 1790000030.000000001 autoscale.up.london\n")
        self.assertTrue((directory / "scenarios" / "A1" / "command-1.out").exists())
        exits = {item["scenario"]: item["returncode"] for item in logged
                 if item.get("event") == "controller_exit"}
        self.assertEqual(exits, {"A1": 0, "A2": 1})
        # A2's controller printed no mark: its run is wrapped in its own label.
        a2 = scale.change_run().marks_of((directory / "scenarios" / "A2" / "marks.txt").read_text())
        self.assertEqual([(kind, label) for kind, _t, label in a2],
                         [("start", "A2.autoscale"), ("end", "A2.autoscale")])

    def test_each_kind_runs_its_one_command(self):
        """C 2.11: one blocking command a kind, by the deploy tool's and the
        autoscaler's command lines, with the engine's timeouts (the bound
        of its scenario) and the run's budget where it may create."""
        directory = pathlib.Path("/tmp/step")
        args = argparse.Namespace(stream=3001, budget_usd=60.0, spent_before_usd=None,
                                  next_av_mesh_root="/w/av-mesh-next", next_av_contrib_root=None)

        def command(action, duration=0.0):
            argv, env = scale.action_command(action, args, directory,
                                             directory / "events.jsonl", duration)
            return argv[1:], env

        deploy = str(scale.DEPLOY)
        autoscaler = str(scale.AUTOSCALER)
        timeouts = ["--gate-timeout-s", "30", "--drain-timeout-s", "30", "--ready-timeout-s", "60"]
        self.assertEqual(command({"kind": "roll", "tier": "edges", "sites": ["london"]})[0],
                         [deploy, "roll", "edges", "--force", "--sites", "london", *timeouts])
        self.assertEqual(command({"kind": "roll", "tier": "all", "dry": True})[0],
                         [deploy, "roll", "all", "--force", "--dry-marks", *timeouts])
        argv, env = command({"kind": "upgrade", "tier": "all", "build": "next"})
        self.assertEqual(argv, [deploy, "roll", "all", *timeouts])
        self.assertEqual(env["NEEDLETAIL_AV_MESH_ROOT"], "/w/av-mesh-next")
        with mock.patch.dict(scale.os.environ, {"NEEDLETAIL_AV_MESH_ROOT": "/w/av-mesh-first"}):
            _argv, env = command({"kind": "upgrade", "tier": "all", "build": "first"})
        self.assertEqual(env["NEEDLETAIL_AV_MESH_ROOT"], "/w/av-mesh-first")
        self.assertEqual(command({"kind": "restart", "targets": ["contrib-lon"], "hold_s": 20,
                                  "ready_timeout_s": 90})[0],
                         [deploy, "restart", "--nodes", "contrib-lon", "--hold-s", "20",
                          "--gate-timeout-s", "30", "--drain-timeout-s", "30",
                          "--ready-timeout-s", "90"])
        self.assertEqual(command({"kind": "replace", "targets": ["tok-dist-b"]})[0],
                         [deploy, "replace", "--nodes", "tok-dist-b", "--mode", "rebuild",
                          "--budget-usd", "60.0", *timeouts])
        self.assertEqual(command({"kind": "change", "change": "kill", "targets": ["contrib-lon"],
                                  "args": ["--unprotected"]})[0],
                         [deploy, "kill", "--node", "contrib-lon", "--unprotected"])
        self.assertEqual(command({"kind": "change", "change": "return",
                                  "targets": ["lon-dist-a"]})[0],
                         [deploy, "return", "--node", "lon-dist-a"])
        self.assertEqual(command({"kind": "scale", "direction": "up", "site": "london"})[0],
                         [autoscaler, "scale", "up", "--backend", "linode", "--site", "london",
                          "--tier", "edges", "--stream", "3001", "--controller-state",
                          "/tmp/step/autoscale-state.json", "--events",
                          "/tmp/step/events.jsonl", "--budget-usd", "60.0"])
        self.assertEqual(command({"kind": "autoscale", "until": "stopped",
                                  "controller_args": ["--down-hold-s", 60, "--no-warm-pool"]},
                                 840.5)[0],
                         [autoscaler, "run", "--backend", "linode", "--until", "stopped",
                          "--duration", "840", "--stream", "3001", "--controller-state",
                          "/tmp/step/autoscale-state.json", "--events",
                          "/tmp/step/events.jsonl", "--budget-usd", "60.0", "--down-hold-s",
                          "60", "--no-warm-pool"])
        args.budget_usd = None
        with self.assertRaises(RuntimeError):
            command({"kind": "replace", "targets": ["tok-dist-b"]})
        # Each kind's keys are checked when the plan loads.
        for bad in ({"kind": "roll"}, {"kind": "upgrade", "tier": "all"},
                    {"kind": "replace", "targets": ["a", "b"]},
                    {"kind": "scale", "direction": "sideways", "site": "london"},
                    {"kind": "change", "change": "handover", "targets": ["contrib-lon"]},
                    {"kind": "autoscale"}, {"kind": "kill", "targets": ["x"], "background": True},
                    {"kind": "kill", "targets": ["x"], "at_s": 5,
                     "after": {"event": "drain_end", "timeout_s": 5}}):
            with self.assertRaises(ValueError, msg=bad):
                scale.check_action("X", bad, False)
        with self.assertRaises(ValueError):
            scale.check_action("X", {"kind": "start", "targets": ["x"],
                                     "after": {"event": "e", "timeout_s": 1}}, True)
        # The bounds of the commands come from those timeouts.
        state = {**lab(), "standby_edges": STANDBY}
        per_node = 30 + 30 + 60 + scale.ENGINE_RESTART_S
        self.assertEqual(scale.action_bound_s({"kind": "roll", "tier": "edges",
                                               "sites": ["london"]}, state), 6 * per_node)
        self.assertEqual(scale.tier_nodes(state, "edges", sites=["london"]),
                         [f"lon-edge-{index}" for index in range(1, 6)])
        self.assertEqual(scale.action_bound_s({"kind": "replace", "targets": ["tok-dist-b"]},
                                              state),
                         scale.REPLACE_BOUND_S["rebuild"] + scale.SLOT_BOUND_S)
        self.assertEqual(scale.action_bound_s({"kind": "scale", "direction": "up",
                                               "site": "london"}, state),
                         scale.SCALE_JOIN_S + scale.SCALE_REBALANCE_S)
        self.assertEqual(scale.action_bound_s({"kind": "kill", "targets": ["x"]}, state), 0)

    def test_probes_hold_long_enough_for_the_referee_s_null(self):
        """A scenario's probes run PROBE_LEAD_S before its change and long
        enough after the bound of its change for change-window.py's 120
        no-change windows of its length (notes-stage2), 360 s at least."""
        self.assertEqual(scale.probe_tail_s(90), 234)
        self.assertGreaterEqual(scale.null_windows(45, 90, 234), 130)
        self.assertLess(scale.null_windows(45, 90, 233), 130)
        # A short change: the windows before it count too, and the probe is
        # 360 s whatever the arithmetic says.
        self.assertEqual(45 + 5 + scale.probe_tail_s(5), 360)
        self.assertGreaterEqual(scale.null_windows(45, 5, scale.probe_tail_s(5)), 130)
        # A roll's bound and its probe: the change, twice over, and more.
        span = 6 * 135.0
        self.assertGreaterEqual(scale.probe_tail_s(span), span + 7 + 130)

    def test_an_open_label_is_closed_when_its_command_dies(self):
        """C 2.7: a command that died with its label open is closed when it
        was reaped, with an `aborted` event; its exit status is logged."""
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F1"]), lab())
        item = {"kind": "roll", "tier": "edges", "scenario": "F1", "t": 60.0}
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        logged = []
        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=None,
                                  next_av_mesh_root=None, next_av_contrib_root=None)
        runner = scale.PhaseRunner(lab(), timeline, time.time(), logged.append, args,
                                   directory=directory)

        def popen(command, **kwargs):
            kwargs["stdout"].write(b"start 1790000000.000000001 roll.edges\n")
            kwargs["stderr"].write(b"killed\n")
            return FakeProcess(command, -9)

        with mock.patch.object(scale.subprocess, "Popen", popen):
            runner.act(item)
        marks = scale.change_run().marks_of(
            (directory / "scenarios" / "F1" / "marks.txt").read_text())
        self.assertEqual([(kind, label) for kind, _t, label in marks],
                         [("start", "roll.edges"), ("end", "roll.edges")])
        self.assertGreater(marks[1][1], marks[0][1])
        events = [item.get("event") for item in logged]
        self.assertIn("aborted", events)
        exit_ = next(item for item in logged if item.get("event") == "command_exit")
        self.assertEqual((exit_["returncode"], exit_["output_tail"]), (-9, "killed\n"))
        # The plain kinds: a drain's mark ends at drain_end; a kill's when
        # its restore has brought the node back and it says ready.
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F2"]), lab())
        row = timeline["scenarios"][0]
        runner = scale.PhaseRunner(lab(), timeline, time.time() - row["fault"], logged.append,
                                   args, directory=directory)
        with mock.patch.object(scale, "ssh", lambda node, command, **k: ""), \
                mock.patch.object(scale, "http_get", lambda node, path, **k: {"ready": True}):
            runner.act(row["actions"][0])
            self.assertEqual(list(runner.open_marks["F2"]), ["F2.kill.new-edge-2"])
            runner.act(row["restores"][0])
            for thread in runner.threads:
                thread.join(timeout=5)
        self.assertEqual(runner.open_marks["F2"], {})
        f2 = scale.change_run().marks_of((directory / "scenarios" / "F2" / "marks.txt").read_text())
        self.assertEqual([(kind, label) for kind, _t, label in f2],
                         [("start", "F2.kill.new-edge-2"), ("end", "F2.kill.new-edge-2")])

    def waits_plan(self, timeout_s: float) -> dict:
        plan = json.loads(PHASES.read_text())
        plan["scenarios"] = [{"name": "W1", "group": "U", "actions": [
            {"kind": "drain", "targets": ["lon-edge-1"]},
            {"kind": "restart", "targets": ["lon-edge-1"],
             "after": {"event": "drain_end", "node": "lon-edge-1", "timeout_s": timeout_s}}]}]
        path = pathlib.Path(tempfile.mkdtemp()) / "plan.json"
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text(json.dumps(plan))
        return scale.load_phase_plan(path, names=["W1"])

    def test_an_action_waits_for_its_event(self):
        """C 2.11: `after` an event, never a sleep: the restart starts once
        the drain has ended; on a timeout the scenario's later actions are
        skipped, its restores still run, and "event E within T s" fails."""
        timeline = scale.phase_timeline(self.waits_plan(30), lab())
        row = timeline["scenarios"][0]
        # The restart waits up to 30 s, then may take its bound: the
        # observation covers both.
        self.assertEqual(row["observe_end"] - row["fault"], 30 + row["actions"][1]["bound_s"])
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        logged, commands = [], []
        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=None,
                                  next_av_mesh_root=None, next_av_contrib_root=None)

        def popen(command, **kwargs):
            commands.append(command)
            kwargs["stdout"].write(b"start 1790000100.000000000 restart.lon-edge-1\n"
                                   b"end 1790000130.000000000 restart.lon-edge-1\n")
            return FakeProcess(command, 0)

        drained = threading.Event()

        def http_get(node, path, **kwargs):
            if drained.is_set():
                return {"client_udp": {"subscribers": 0}, "edge_services": [
                    {"node_id": node["name"], "active_playback_sessions": 0}]}
            return {"client_udp": {"subscribers": 5}, "edge_services": []}

        def run(drains: bool) -> list[dict]:
            logged.clear()
            commands.clear()
            runner = scale.PhaseRunner(lab(), timeline, time.time() - row["fault"],
                                       logged.append, args, directory=directory)
            with mock.patch.object(scale.subprocess, "Popen", popen), \
                    mock.patch.object(scale, "http_post", lambda *a, **k: 202), \
                    mock.patch.object(scale, "http_get", http_get):
                runner.begin()
                deadline = time.time() + 10
                while time.time() < deadline and not any(
                        item.get("event") in ("command_exit", "after_timeout")
                        for item in logged):
                    if drains and any(item.get("event") == "action" for item in logged):
                        drained.set()
                    time.sleep(0.05)
                runner.finish()
            return list(logged)

        events = run(True)
        names = [item.get("event") for item in events]
        self.assertLess(names.index("drain_end"), names.index("after_met"))
        self.assertLess(names.index("after_met"), names.index("command_start"))
        self.assertEqual(commands[0][2:5], ["restart", "--nodes", "lon-edge-1"])
        # The drain's restore (resume) is owed and put back by finish.
        self.assertIn(("resume", True), [(item.get("kind"), item.get("restore"))
                                         for item in events if item.get("event") == "done"])
        drained.clear()
        timeline = scale.phase_timeline(self.waits_plan(0.5), lab())
        row = timeline["scenarios"][0]
        events = run(False)
        self.assertIn("after_timeout", [item.get("event") for item in events])
        self.assertEqual(commands, [])
        self.assertIn(("resume", True), [(item.get("kind"), item.get("restore"))
                                         for item in events if item.get("event") == "done"])
        result = {"commands": [], "waits": [{"event": "after_timeout", "awaited": "drain_end",
                                             "timeout_s": 0.5}]}
        criteria = scale.change_criteria({**change_figures(), **result}, {"actions": []}, ROLES)
        wait = next(item for item in criteria if item["criterion"].startswith("event drain_end"))
        self.assertEqual((wait["criterion"], wait["ok"]), ("event drain_end within 0.5 s", False))

    def test_windows_come_from_the_marks(self):
        """A scenario's windows are its marks, widened as the referee widens
        them (2 s before, 5 s after), not its planned times; a scenario
        with no marks did not run and says so."""
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        plan = scale.load_phase_plan(PHASES, names=["F1", "F2"])
        timeline = scale.phase_timeline(plan, lab())
        start = 1_790_000_000.0
        self.write_phase_step(directory, start, timeline)
        marks = directory / "scenarios" / "F1" / "marks.txt"
        marks.write_text(scale.mark_line("start", int((start + 70) * 1e9), "F1.drain.lon-edge-1")
                         + scale.mark_line("end", int((start + 100) * 1e9),
                                           "F1.drain.lon-edge-1"))
        shutil.rmtree(directory / "scenarios" / "F2")
        results = scale.analyse_phases(lab(), directory, timeline, start, plan, SERVING, "pcm-1",
                                       [], readers_launched=start - 10)
        f1, f2 = results
        self.assertEqual(f1["windows"], {"before": [start, start + 68],
                                         "during": [start + 68, start + 105],
                                         "after": [start + 105, start + 414]})
        self.assertEqual(f1["marks"][0][2], "F1.drain.lon-edge-1")
        self.assertFalse(f2["ran"])
        self.assertEqual([(item["criterion"], item["ok"]) for item in f2["criteria"]],
                         [("the scenario ran (its marks)", False)])
        self.assertIn("| F2 | new-edge-2 | not run |", scale.format_phases(results))

    def test_each_scenario_is_scored_on_its_own_probes_and_marks(self):
        """One scenario, one verdict (C 2.11): the referee's probe on every
        view host and probed config, from the region's dist-a, for the
        scenario's probe length; ingested with the referee's ntcmp on the
        view host; change-window.py on that scenario's frames and its marks
        only, its verdict the probe criterion (pass may be null)."""
        state = lab()
        step_dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, step_dir, True)
        probes = scale.ScenarioProbes(state, ["opus-1", "pcm-1"], "/r/run/6000", step_dir,
                                      lambda item: None)
        self.assertEqual([(view["name"], entry["name"]) for view, entry in probes.hosts],
                         [("lon-view", "lon-dist-a"), ("new-view", "new-dist-a"),
                          ("syd-view", "syd-dist-a"), ("tok-view", "tok-dist-a")])
        start = probes.start_command("U1", "pcm-1", state["nodes"]["lon-dist-a"], 1200.4)
        self.assertIn(f"--entry https://{state['nodes']['lon-dist-a']['vlan_ipv4']}:19444 "
                      "--stream 4001", start)
        for part in ("--duration-seconds 1201", "--arrivals /r/run/6000/probes/U1/pcm-1/"
                     "arrivals.csv", "--write-parts /r/run/6000/probes/U1/pcm-1/parts.bin",
                     "date +%s.%N >/r/run/6000/probes/U1/pcm-1/launch"):
            self.assertIn(part, start)
        ingest = probes.ingest_command("U1", "opus-1", "lon-view")
        for part in ("ntcmp udp-ingest", "--audio-index /var/lib/compare/index/"
                     "nt-popus-48s-la.index.json", "--lane nt-uc", "--skip-seconds 5",
                     "--frames /r/run/6000/probes/U1/opus-1/probe.csv"):
            self.assertIn(part, ingest)
        frames = [step_dir / "scenarios" / "U1" / "probes" / "lon-view" / "opus-1" / "probe.csv"]
        self.assertEqual(scale.cw_command(frames, step_dir / "m.txt", step_dir / "v.json")[1:],
                         [str(scale.CHANGE_WINDOW), "--frames", str(frames[0]), "--marks",
                          str(step_dir / "m.txt"), "--json", str(step_dir / "v.json")])
        # The runner scores each scenario after its own probes, on its own
        # directory (its marks only).
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F1", "F2"]), state)
        collected, scored = [], []

        class Probes:
            hosts = probes.hosts

            def start(self, scenario, seconds):
                collected.append(("start", scenario, seconds))

            def collect(self, scenario):
                collected.append(("collect", scenario))
                return frames

        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=None)
        runner = scale.PhaseRunner(state, timeline, time.time() - 10_000, lambda item: None,
                                   args, directory=step_dir, probes=Probes())
        with mock.patch.object(scale, "ssh", lambda node, command, **k: ""), \
                mock.patch.object(scale, "http_post", lambda *a, **k: 202), \
                mock.patch.object(scale, "http_get", lambda node, path, **k: {}), \
                mock.patch.object(scale, "score_scenario",
                                  lambda directory, got: scored.append(directory.name) or 0):
            runner.run()
            runner.finish()
        self.assertEqual(collected, [("start", "F1", 369.0), ("collect", "F1"),
                                     ("start", "F2", 369.0), ("collect", "F2")])
        self.assertEqual(scored, ["F1", "F2"])
        for name in ("F1", "F2"):
            labels = {label for _k, _t, label in scale.change_run().marks_of(
                (step_dir / "scenarios" / name / "marks.txt").read_text())}
            self.assertTrue(labels and all(label.startswith(name + ".") for label in labels))
        # The verdict: one entry a label and probe; pass, fail or unscored.
        directory = step_dir / "scenarios" / "F1"
        verdict = {"F1.drain.lon-edge-1": {
            str(frames[0]): {"pass": True, "change": {"missing": 0, "gap_ms": 1.2, "p99_ms": 2.0},
                             "gap_bar_ms": 1.5, "p99_bar_ms": 2.1, "null_fail_rate": 0.07,
                             "null_windows": 250, "window_s": 20.0},
            str(frames[0]).replace("lon-view", "new-view"): {
                "pass": None, "note": "null too short: 80 no-change windows of this length, "
                                      "120 needed", "change": {"missing": 0}}}}
        (directory / "change-window.json").write_text(json.dumps(verdict))
        record = scale.probe_record(directory)
        self.assertEqual(sorted(record), ["F1.drain.lon-edge-1 lon-view/opus-1",
                                          "F1.drain.lon-edge-1 new-view/opus-1"])
        probe = scale.change_criteria(change_figures(probe=record), {"actions": []}, ROLES)[0]
        self.assertEqual((probe["criterion"], probe["ok"]),
                         ("probe: pass on every lane and view host", None))
        self.assertIn("null too short", probe["needs"])
        self.assertEqual(probe["value"]["null_fail_rate_max"], 0.07)
        # A 20 s window needs 163 null windows for CW to score each against
        # 120 others: with fewer its rate is not measured, never 0.0.
        self.assertEqual(scale.change_run().null_fail_rate(
            {"null_fail_rate": 0.07, "null_windows": 163, "window_s": 20.0}), (0.07, None))
        rate, why = scale.change_run().null_fail_rate(
            {"null_fail_rate": 0.0, "null_windows": 162, "window_s": 20.0})
        self.assertIsNone(rate)
        self.assertIn("not measured", why)
        record["F1.drain.lon-edge-1 new-view/opus-1"]["pass"] = False
        probe = scale.change_criteria(change_figures(probe=record), {"actions": []}, ROLES)[0]
        self.assertFalse(probe["ok"])
        probe = scale.change_criteria(change_figures(), {"actions": []}, ROLES)[0]
        self.assertIsNone(probe["ok"])
        # Review of CL7: every probe that was meant to run must have its
        # verdict. Two expected, both pass: a pass; one view host's probe
        # never came back: a failure naming it, not a pass on fewer hosts.
        record["F1.drain.lon-edge-1 new-view/opus-1"]["pass"] = True
        expected = ["lon-view/opus-1", "new-view/opus-1"]
        probe = scale.change_criteria(change_figures(probe=record, probe_expected=expected),
                                      {"actions": []}, ROLES)[0]
        self.assertTrue(probe["ok"])
        probe = scale.change_criteria(change_figures(
            probe=record, probe_expected=expected + ["tok-view/opus-1"]), {"actions": []},
            ROLES)[0]
        self.assertFalse(probe["ok"])
        self.assertEqual(probe["value"]["missing"], ["F1.drain.lon-edge-1 tok-view/opus-1"])
        probe = scale.change_criteria(change_figures(probe_expected=expected), {"actions": []},
                                      ROLES)[0]
        self.assertFalse(probe["ok"])
        # The probes write down which were meant to run before they start,
        # so a host whose ssh fails is still expected.
        def failing_ssh(node, command, **kwargs):
            if node["name"] == "tok-view":
                raise RuntimeError("ssh: connect to host tok-view: timed out")
            return ""

        with mock.patch.object(scale, "ssh", failing_ssh), self.assertRaises(RuntimeError):
            probes.start("F9", 400)
        self.assertEqual(json.loads((step_dir / "scenarios" / "F9" / scale.PROBES_EXPECTED)
                                    .read_text()), probes.expected())
        self.assertIn("tok-view/pcm-1", probes.expected())

    def test_the_controller_must_have_acted(self):
        """A scenario with a controller or a scale verb: it acted in the
        direction its action asks (up: a join; down: a drain_end and a stop;
        review of CL7: a join never passes a run until stopped), and its
        command exited 0."""
        spec = {"actions": [{"kind": "autoscale", "until": "join"}]}

        def judged(spec=spec, **figures):
            out = scale.change_criteria(change_figures(changes=["autoscale"], **figures), spec,
                                        ROLES)
            return {item["criterion"]: item["ok"] for item in out}

        acted = "the controller acted (up: a join)"
        self.assertFalse(judged()[acted])
        self.assertTrue(judged(controller={"decisions": ["join"], "kinds": ["decision"]})[acted])
        self.assertFalse(judged(controller={"decisions": [],
                                            "kinds": ["drain_end", "stopped"]})[acted])
        down = {"actions": [{"kind": "autoscale", "until": "stopped"}]}
        stopped = "the controller acted (down: a drain_end and a stop)"
        self.assertTrue(judged(down, controller={"decisions": [],
                                                 "kinds": ["drain_end", "stopped"]})[stopped])
        self.assertFalse(judged(down, controller={"decisions": [], "kinds": ["drain_end"]})[stopped])
        self.assertFalse(judged(down, controller={"decisions": ["join"],
                                                  "kinds": ["serving"]})[stopped])
        self.assertNotIn(acted, judged(down))
        scale_down = judged({"actions": [{"kind": "scale", "direction": "down",
                                          "site": "london"}]})
        self.assertIn(stopped, scale_down)
        # A3p: up, then down; each is judged.
        both = judged({"actions": [{"kind": "autoscale", "until": "join"},
                                   {"kind": "autoscale", "until": "stopped"}]},
                      controller={"decisions": ["join"], "kinds": ["serving"]})
        self.assertEqual((both[acted], both[stopped]), (True, False))
        criteria = judged(controller={"decisions": ["join"], "kinds": []},
                          commands=[{"event": "controller_exit", "kind": "autoscale",
                                     "returncode": 2}])
        self.assertFalse(criteria["autoscale: the command exited 0"])
        # A4p's events from inside the mesh.
        out = scale.change_criteria(change_figures(changes=["autoscale"], controller={
            "decisions": [], "kinds": ["lost", "replace_join"]}),
            {"actions": [], "expect": {"events": ["lost", "replace_join", "recovered"]}}, ROLES)
        self.assertEqual({item["criterion"]: item["ok"] for item in out
                          if item["criterion"].startswith("controller event")},
                         {"controller event lost": True, "controller event replace_join": True,
                          "controller event recovered": False})

    def test_the_crowd_is_judged_on_the_load_tool_s_new_fields_and_open_without_them(self):
        """B Y4's fields (needletail.load.interval.v2), read where present:
        listeners interrupted and with missing parts, gaps, resets, the
        longest silence against the baseline's plus a unit, the latency p99
        of the window's buckets against the baseline's plus 1 ms, the
        single-path spans. Without them each is open, never passed."""
        spec = {"actions": [{"kind": "drain", "targets": ["lon-edge-1"]}]}
        open_ = {item["criterion"]: item for item in scale.change_criteria(
            change_figures(), spec, ROLES)}
        for name in ("crowd: listeners_interrupted = 0", "crowd: listeners_with_missing = 0",
                     "crowd: gaps = 0", "crowd: resets = 0",
                     "crowd: longest_silence_ms inside <= outside + one unit",
                     "crowd: latency p99 inside <= outside + 1 ms", "crowd: single_path_ms <= 0"):
            self.assertIsNone(open_[name]["ok"], name)
            self.assertIn("B Y4", open_[name]["needs"])

        def line(t, interrupted=0, silence=1.0, p99_bucket=2000, single=None):
            total = {"up": 100, "parts": 100_000, "missing": 0, "listeners_interrupted":
                     interrupted, "listeners_with_missing": 0, "gaps": 0, "resets": 0,
                     "never_resumed": 0, "listeners_without_parts": 0,
                     "longest_silence_ms": silence,
                     "latency_ms": {"p50": 1.0, "buckets_us": [[1000, 98], [p99_bucket, 2]]},
                     "paths": {"two": 100, "one": 0, "none": 0}}
            if single is not None:
                total["single_path_ms"] = {"buckets_us": [[single, 1]]}
            return {"schema": "needletail.load.interval.v2",
                    "unix_ms": int((1_790_000_000 + t) * 1000), "total": total}

        rows = scale.interval_rows([line(t) for t in range(0, 60)]
                                   + [line(t, silence=1.9, p99_bucket=2800) for t in range(60, 70)]
                                   + [line(t) for t in range(70, 100)])
        windows = {key: (1_790_000_000 + a, 1_790_000_000 + b) for key, (a, b) in
                   {"before": (0, 60), "during": (60, 70), "after": (70, 100)}.items()}
        crowd = {key: scale.crowd_window(rows, a, b, 1.5, False) for key, (a, b) in windows.items()}
        self.assertEqual(crowd["during"]["longest_silence_ms"], 1.9)
        self.assertEqual(crowd["during"]["latency_ms"]["p99"], 2.8)
        judged = {item["criterion"]: item for item in scale.change_criteria(
            change_figures(crowd=crowd), spec, ROLES)}
        for name in ("crowd: listeners_interrupted = 0", "crowd: listeners_with_missing = 0",
                     "crowd: gaps = 0", "crowd: never_resumed = 0",
                     "crowd: longest_silence_ms inside <= outside + one unit",
                     "crowd: latency p99 inside <= outside + 1 ms"):
            self.assertTrue(judged[name]["ok"], name)
        # One interrupted listener, a silence of a unit and more, the p99
        # 1 ms and more over the baseline's, a single-path span on a planned
        # change: each fails.
        rows = scale.interval_rows([line(t) for t in range(0, 60)] + [
            line(t, interrupted=1, silence=2.1, p99_bucket=3100, single=4000)
            for t in range(60, 70)])
        crowd = {key: scale.crowd_window(rows, a, b, 1.5, False) for key, (a, b) in windows.items()}
        judged = {item["criterion"]: item for item in scale.change_criteria(
            change_figures(crowd=crowd), spec, ROLES)}
        for name in ("crowd: listeners_interrupted = 0",
                     "crowd: longest_silence_ms inside <= outside + one unit",
                     "crowd: latency p99 inside <= outside + 1 ms", "crowd: single_path_ms <= 0"):
            self.assertFalse(judged[name]["ok"], name)
        # The loss of an edge allows B's bound for its single-path spans.
        kill = {"actions": [{"kind": "kill", "targets": ["lon-edge-2"]}]}
        judged = {item["criterion"]: item for item in scale.change_criteria(
            change_figures(crowd=crowd), kill, ROLES)}
        self.assertTrue(judged["crowd: single_path_ms <= 100"]["ok"])

    def test_the_six_criteria_once_open_are_judged(self):
        """C 2.11: first_part_s from first_serving, warm_parts_pending_zero_s
        and largest_burst_per_tick from the forwarders, watermark_stall_ms,
        parent_change_share from the lease pairs before and after, and
        trigger_to_fresh_s from the controller's serving event."""
        spec = {"actions": [{"kind": "join", "targets": ["lon-edge-6"]}],
                "expect": {"first_part_s": 20, "warm_parts_pending_zero_s": 10,
                           "largest_burst_per_tick": 100000, "watermark_stall_ms": 250,
                           "parent_change_share_max": 0.77, "trigger_to_fresh_s": 20}}
        base = {"stranded": {"max": 0},
                "udp": {key: {side: {"missing": 0, "parts": 1000} for side in
                              ("unaffected", "faulted")} for key in ("before", "during", "after")},
                "survivors": {key: {"nodes": 0, "cpu_max": 0.1, "core_max": 0.1,
                                    "nic_share_max": 0.1} for key in ("before", "during", "after")},
                "moves": {"udp": 0, "udp_spread_max_over_mean": None},
                "mesh": {key: {"survivors": {}} for key in ("before", "during", "after")}}

        def judged(**figures):
            result = {**base, **change_figures(**figures)}
            with mock.patch.object(scale, "deploy_settings", return_value={}):
                out = scale.phase_criteria(result, spec, ROLES)
            return {item["criterion"]: item for item in out}

        good = judged(first_part_s=12.0, warm_parts_pending_zero_s=6.0,
                      largest_burst_per_tick=80_000, watermark_stall_ms=40.0,
                      watermark_stalls_over_250ms_rise=0, parent_change_share=0.66,
                      trigger_to_fresh_s=14.0)
        for name in ("first_part_s <= 20", "warm_parts_pending_zero_s <= 10",
                     "largest_burst_per_tick <= 100000", "watermark_stall_ms <= 250",
                     "parent_change_share_max <= 0.77", "trigger_to_fresh_s <= 20"):
            self.assertTrue(good[name]["ok"], name)
        bad = judged(first_part_s=None, warm_parts_pending_zero_s=False,
                     largest_burst_per_tick=120_000, watermark_stall_ms=40.0,
                     watermark_stalls_over_250ms_rise=1, parent_change_share=0.9,
                     trigger_to_fresh_s=None)
        for name in ("first_part_s <= 20", "warm_parts_pending_zero_s <= 10",
                     "largest_burst_per_tick <= 100000", "watermark_stall_ms <= 250",
                     "parent_change_share_max <= 0.77", "trigger_to_fresh_s <= 20"):
            self.assertFalse(bad[name]["ok"], name)
        blind = judged(largest_burst_per_tick=None)
        self.assertIsNone(blind["largest_burst_per_tick <= 100000"]["ok"])
        # The figures from the samplers: two forwarders' lease pairs before
        # a third joins and after (2 of 3 pairs change one parent), and the
        # backlog's way to 0.
        mesh = {name: [{"t": t, "node": name, "warm_parts_pending": pending,
                        "node": {"lease_pairs": pairs}} for t, pending, pairs in rows]
                for name, rows in {
                    "lon-dist-a": [(0, 0, [["e1", 3001], ["e2", 3001], ["e3", 3001]]),
                                   (20, 0, [["e1", 3001]])],
                    "lon-dist-b": [(0, 0, [["e1", 3001], ["e2", 3001], ["e3", 3001]]),
                                   (20, 0, [["e1", 3001], ["e2", 3001], ["e3", 3001]])],
                    "lon-dist-c": [(20, 0, [["e2", 3001], ["e3", 3001]])]}.items()}
        forwarders = ["lon-dist-a", "lon-dist-b", "lon-dist-c"]
        share = scale.parent_change_share(scale.lease_parents(mesh, forwarders, 5),
                                          scale.lease_parents(mesh, forwarders, 25))
        self.assertAlmostEqual(share, 0.667, places=3)
        backlog = {"lon-dist-a": [{"t": t, "node": "lon-dist-a", "warm_parts_pending": value}
                                  for t, value in ((11, 900), (12, 300), (13, 0))],
                   "lon-dist-b": [{"t": t, "node": "lon-dist-b", "warm_parts_pending": 0}
                                  for t in (11, 12, 13)]}
        self.assertEqual(scale.warm_zero_s(backlog, ["lon-dist-a", "lon-dist-b"], 10.0, 60.0), 3.0)
        self.assertFalse(scale.warm_zero_s(backlog, ["lon-dist-a", "lon-dist-b"], 10.0, 12.5))
        self.assertIsNone(scale.warm_zero_s({}, ["lon-dist-a"], 10.0, 60.0))

    def test_restarts_are_judged_from_the_nodes_boot_ids(self):
        """The nodes a scenario restarts (a roll's tier, a kill's target) get
        a new boot id, and no other node does; an upgrade leaves every node
        on one build, and no node reads a frame it cannot."""
        rows = {name: [{"t": t, "node": {"boot_id": boot, "build": build,
                                         "rx_invalid_frames": 0}}
                       for t, boot, build in series]
                for name, series in {
                    "lon-edge-1": [(0, 1, "aaa"), (100, 2, "bbb")],
                    "lon-edge-2": [(0, 3, "aaa"), (100, 3, "aaa")],
                    "lon-dist-a": [(0, 5, "aaa"), (100, 6, "bbb")]}.items()}
        restarts = scale.boot_changes(rows, 10, 90)
        self.assertEqual(restarts, {"lon-edge-1": [1, 2], "lon-edge-2": [3, 3],
                                    "lon-dist-a": [5, 6]})
        spec = {"actions": [{"kind": "upgrade", "tier": "all", "build": "next"}]}
        out = {item["criterion"]: item for item in scale.change_criteria(change_figures(
            restarts=restarts, restarted_expected=["lon-edge-1", "lon-edge-2"],
            changes=["upgrade"], builds=scale.builds_around(rows, lab(), (10, 90))), spec, ROLES)}
        restarted = out["restarts: the nodes it changes and no other"]
        self.assertFalse(restarted["ok"])
        self.assertEqual(restarted["value"], {"restarted": ["lon-dist-a", "lon-edge-1"],
                                              "not_restarted": ["lon-edge-2"], "not_back": [],
                                              "not_read_before": [], "others": ["lon-dist-a"]})
        self.assertFalse(out["every node on one build after the upgrade"]["ok"])
        self.assertTrue(out["rx_invalid_frames = 0 while the builds are mixed"]["ok"])

        def restarts_ok(restarts, expected):
            item = next(item for item in scale.change_criteria(change_figures(
                restarts=restarts, restarted_expected=expected), {"actions": []}, ROLES)
                if item["criterion"] == "restarts: the nodes it changes and no other")
            return item["ok"], item["value"]

        # Review of CL7: a node it must restart that does not answer after
        # the change is not back (a failure, never a pass); one not read
        # before cannot be judged (open).
        rows = {"lon-edge-1": rows["lon-edge-1"],
                "new-edge-1": [{"t": 0, "node": {"boot_id": 7}}]}
        restarts = scale.boot_changes(rows, 10, 90, ["lon-edge-1", "new-edge-1", "tok-dist-b"])
        self.assertEqual(restarts, {"lon-edge-1": [1, 2], "new-edge-1": [7, None],
                                    "tok-dist-b": [None, None]})
        ok, value = restarts_ok(restarts, ["lon-edge-1", "new-edge-1", "tok-dist-b"])
        self.assertFalse(ok)
        self.assertEqual((value["not_back"], value["not_read_before"]),
                         (["new-edge-1"], ["tok-dist-b"]))
        self.assertIsNone(restarts_ok({"lon-edge-1": [1, 2], "tok-dist-b": [None, None]},
                                      ["lon-edge-1", "tok-dist-b"])[0])
        # A rebuilt node (RP1) has no sampler file from before its replace:
        # the runner's own reads of its boot id stand in.
        events = [{"event": "boot_ids", "when": "before", "boot_ids": {"new-edge-1": 7}},
                  {"event": "boot_ids", "when": "after", "boot_ids": {"new-edge-1": 8}}]
        restarts = scale.boot_changes({"new-edge-1": [{"t": 95, "node": {"boot_id": 8}}]}, 10, 90,
                                      ["new-edge-1"], events)
        self.assertEqual(restarts, {"new-edge-1": [7, 8]})
        self.assertTrue(restarts_ok(restarts, ["new-edge-1"])[0])

    def test_the_proving_plan_runs_every_change_kind(self):
        """C 7: phases-proving.json, group by group, with probes on opus-1
        and pcm-1 beside an opus-1 crowd; every change kind of C CL7; A's
        changes run only when named; its targets are nodes of the proving
        lab's naming (topology-scale.json shares it)."""
        proving = scale.ROOT / "deploy" / "low-latency" / "phases-proving.json"
        groups = {group: [item["name"] for item in scale.load_phase_plan(proving, group)[
            "scenarios"]] for group in "NUFRA"}
        self.assertEqual(groups, {"N": ["N1"], "U": ["U1", "U2", "U4", "U5"], "F": ["F4", "F6"],
                                  "R": ["RP1", "RP2"],
                                  "A": ["S1", "S2", "A1p", "A2p", "A3p", "A4p"]})
        every = scale.load_phase_plan(proving, names=["U3", "C1", "C2"])["scenarios"]
        self.assertEqual([item["name"] for item in every], ["U3", "C1", "C2"])
        kinds = {action["kind"] for item in json.loads(proving.read_text())["scenarios"]
                 for action in item["actions"]}
        self.assertTrue(set(scale.CHANGE_KINDS) <= kinds, set(scale.CHANGE_KINDS) - kinds)
        state = lab()
        for item in json.loads(proving.read_text())["scenarios"]:
            for action in item["actions"]:
                for target in action.get("targets", []):
                    self.assertIn(target, state["nodes"], item["name"])
        args = argparse.Namespace(phase_plan=str(proving), group="A", scenarios="", steps=None,
                                  hls_share=None, config=None, probe_configs=None, load_args="")
        phases = scale.phase_setup(args, {**state, "standby_edges": STANDBY})
        self.assertEqual((args.steps, args.config, phases["probe_configs"]),
                         ("900", "opus-1", ["opus-1", "pcm-1"]))
        # An upgrade to `next` needs the roots it rolls to before the run,
        # and any roll or upgrade the run's own roots in the environment
        # (review of CL7: else the deploy tool takes ~/wavey.ai's checkouts).
        timeline = scale.phase_timeline(scale.load_phase_plan(proving, "U"),
                                        {**state, "standby_edges": STANDBY})
        roots = {"NEEDLETAIL_AV_MESH_ROOT": "/w/av-mesh", "NEEDLETAIL_AV_CONTRIB_ROOT": "/w/av-c"}
        with mock.patch.dict(scale.os.environ, roots):
            with self.assertRaises(ValueError):
                scale.check_build_roots(timeline, argparse.Namespace(
                    next_av_mesh_root=None, next_av_contrib_root=None))
            scale.check_build_roots(timeline, argparse.Namespace(
                next_av_mesh_root="/w/next", next_av_contrib_root=None))
        for unset in roots:
            with mock.patch.dict(scale.os.environ, {**roots, unset: ""}):
                with self.assertRaises(ValueError, msg=unset) as raised:
                    scale.check_build_roots(timeline, argparse.Namespace(
                        next_av_mesh_root="/w/next", next_av_contrib_root=None))
                self.assertIn(unset, str(raised.exception))
        n1 = scale.phase_timeline(scale.load_phase_plan(proving, "N"), state)
        with mock.patch.dict(scale.os.environ, {key: "" for key in roots}):
            with self.assertRaises(ValueError):
                scale.check_build_roots(n1, argparse.Namespace(next_av_mesh_root=None,
                                                               next_av_contrib_root=None))
            scale.check_build_roots(scale.phase_timeline(scale.load_phase_plan(proving, "F"),
                                                         state),
                                    argparse.Namespace(next_av_mesh_root=None,
                                                       next_av_contrib_root=None))
        # The rolls are planned from their engine timeouts (70 s a node).
        u1 = timeline["scenarios"][0]
        self.assertEqual(u1["actions"][0]["bound_s"], (20 + 1) * 70.0)
        # Review of CL7: A1p's controller runs twice, the standby's join and
        # then the first create on Linode (`run --until join` returns at its
        # first join), with the create's bound in its observation; A2p
        # expects the slot's drain, stop and delete; A4p holds its edge's
        # unit masked, and its restore unmasks it.
        plan = {item["name"]: item for item in json.loads(proving.read_text())["scenarios"]}
        a1 = plan["A1p"]
        self.assertEqual([(item["until"], (item.get("after") or {}).get("event"))
                          for item in a1["actions"]], [("join", None), ("join", "controller_exit")])
        self.assertIn("--create-timeout-s", a1["actions"][1]["controller_args"])
        create = a1["actions"][1]["controller_args"][1]
        self.assertGreaterEqual(a1["observe_s"], 360 + create)
        self.assertEqual(a1["expect"]["events"], ["join", "created", "serving"])
        self.assertEqual(plan["A2p"]["expect"]["events"], ["drain_end", "stopped", "deleted"])
        kill = plan["A4p"]["actions"][1]
        self.assertTrue(kill["mask"])
        self.assertEqual(scale.restore_of(kill), {"kind": "start", "targets": ["lon-edge-2"],
                                                  "unmask": True})
        edge = state["nodes"]["lon-edge-2"]
        self.assertEqual(scale.node_command("kill", edge, kill),
                         "systemctl mask --runtime needletail-mesh.service; "
                         "systemctl kill -s KILL needletail-mesh.service; "
                         "systemctl stop needletail-mesh.service; true")
        self.assertEqual(scale.node_command("start", edge, scale.restore_of(kill)),
                         "systemctl unmask --runtime needletail-mesh.service; "
                         "systemctl start needletail-mesh.service")
        with self.assertRaises(ValueError):
            scale.check_action("X", {"kind": "crash", "targets": ["x"], "mask": True}, False)
        # What section 7 expects and the run cannot judge yet is open, with
        # what it needs (review of CL7: never dropped).
        for name in ("U1", "U4", "U5", "F4", "F6", "RP1", "RP2", "A4p"):
            self.assertTrue(plan[name]["open"], name)
        criteria = scale.change_criteria(change_figures(), plan["RP2"], ROLES)
        opened = [item for item in criteria if item["criterion"].startswith("the slot created")]
        self.assertEqual(len(opened), 1)
        self.assertIsNone(opened[0]["ok"])
        self.assertIn("journal", opened[0]["needs"])

    def test_a_restore_is_never_stopped_and_a_blocking_change_is_never_cut(self):
        """Review of CL7. A's kill is owed its return: finish() plays it and
        lets it run to its end (it used to get SIGINT a second after it
        started, leaving the contributor held down). A blocking change that
        is under way when the step stops is waited for up to its bound, and
        only then sent SIGINT; one that SIGINT does not end owes its
        --resume, loudly. A restore past its own bound is stopped, loudly."""
        proving = scale.ROOT / "deploy" / "low-latency" / "phases-proving.json"
        state = lab()
        timeline = scale.phase_timeline(scale.load_phase_plan(proving, names=["C2", "U1"]),
                                        {**state, "standby_edges": STANDBY})
        rows = {row["name"]: row for row in timeline["scenarios"]}
        kill, roll = rows["C2"]["actions"][0], rows["U1"]["actions"][0]
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        args = argparse.Namespace(stream=3001, budget_usd=None, spent_before_usd=None,
                                  next_av_mesh_root=None, next_av_contrib_root=None)
        processes, logged = [], []

        def popen(seconds, ignore=()):
            def make(command, **kwargs):
                processes.append(SlowProcess(command, seconds, ignore))
                return processes[-1]
            return make

        # The owed return, 1.5 s long, under finish().
        runner = scale.PhaseRunner(state, timeline, time.time(), logged.append, args,
                                   directory=directory)
        runner.owed.append(kill)
        with mock.patch.object(scale.subprocess, "Popen", popen(1.5)):
            runner.finish()
        self.assertEqual([process.command[2:5] for process in processes],
                         [["return", "--node", "contrib-lon"]])
        self.assertEqual(processes[0].signals, [])
        exit_ = next(item for item in logged if item.get("event") == "command_exit")
        self.assertEqual((exit_["returncode"], exit_["restore"]), (0, True))
        # A roll under way when the step stops: never cut within its bound.
        processes.clear()
        logged.clear()
        runner = scale.PhaseRunner(state, timeline, time.time(), logged.append, args,
                                   directory=directory)
        base = {"source": "scale-run", "scenario": "U1", "kind": "roll", "restore": False}
        with mock.patch.object(scale.subprocess, "Popen", popen(1.5)):
            thread = threading.Thread(target=runner.change, args=(roll, base))
            thread.start()
            time.sleep(0.3)
            runner.stop.set()
            thread.join(timeout=10)
        self.assertEqual(processes[0].signals, [])
        events = [item.get("event") for item in logged]
        self.assertIn("command_waited", events)
        self.assertEqual(next(item for item in logged
                              if item.get("event") == "command_exit")["returncode"], 0)
        # Past its bound, once stopped: SIGINT; it ignores it, SIGTERM; its
        # --resume is owed.
        processes.clear()
        logged.clear()
        runner = scale.PhaseRunner(state, timeline, time.time(), logged.append, args,
                                   directory=directory)
        with mock.patch.object(scale.subprocess, "Popen", popen(60, ignore={"SIGINT"})), \
                mock.patch.object(scale, "action_bound_s", lambda action, lab: 0.0), \
                mock.patch.object(scale, "BOUND_GRACE_S", 0.0), \
                mock.patch.object(scale, "CONTROLLER_STOP_S", 0.2):
            thread = threading.Thread(target=runner.change, args=(roll, base))
            thread.start()
            time.sleep(0.2)
            runner.stop.set()
            thread.join(timeout=10)
        self.assertEqual(processes[0].signals, ["SIGINT", "SIGTERM"])
        owed = next(item for item in logged if item.get("event") == "resume_owed")
        self.assertEqual(owed["command"][-1], "--resume")
        self.assertEqual(owed["command"][1:3], ["roll", "edges"])
        # A restore past its own bound is stopped, and says so.
        processes.clear()
        logged.clear()
        runner = scale.PhaseRunner(state, timeline, time.time(), logged.append, args,
                                   directory=directory)
        restore = {**scale.restore_of(kill), "scenario": "C2", "restore": True, "t": 0}
        with mock.patch.object(scale.subprocess, "Popen", popen(60)), \
                mock.patch.object(scale, "action_bound_s", lambda action, lab: 0.0), \
                mock.patch.object(scale, "BOUND_GRACE_S", 0.5), \
                mock.patch.object(scale, "CONTROLLER_STOP_S", 0.2):
            runner.change(restore, {**base, "scenario": "C2", "kind": "change",
                                    "restore": True})
        self.assertEqual(processes[0].signals, ["SIGINT"])
        self.assertIn("restore_unfinished", [item.get("event") for item in logged])

    def test_the_runner_waits_for_two_paths_before_each_scenario(self):
        """B 5's seam with C: before a fault, every listener host's latest
        interval shows paths.one == 0. The runner waits for it (an event,
        never a sleep) before each scenario's probes, which then start late
        by the wait and end on time; a timeout fails the criterion, a load
        tool without `paths` leaves it open, a one-path crowd has none. And
        `assignment_confirms_limited` stays 0 on the edges."""
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F1"]), lab())
        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=None)
        started = []

        class Probes:
            hosts = [None]

            def start(self, scenario, seconds):
                started.append(seconds)

        def run(answers, paths=2, wait_s=5.0):
            logged = []
            answers = iter(answers)
            runner = scale.PhaseRunner(lab(), timeline, time.time(), logged.append, args,
                                       directory=pathlib.Path(tempfile.mkdtemp()),
                                       probes=Probes(), paths=paths,
                                       crowd_paths=lambda since: next(answers))
            self.addCleanup(shutil.rmtree, runner.directory, True)
            with mock.patch.object(scale, "PATHS_WAIT_S", wait_s), \
                    mock.patch.object(scale, "http_get", lambda node, path, **k: {}):
                waited = runner.wait_paths("F1")
                runner.start_probes("F1", waited)
            return [item for item in logged if item.get("event", "").startswith("paths_")], waited

        one = {"known": True, "two": 90, "one": 10, "none": 0, "hosts": 2}
        two = {"known": True, "two": 100, "one": 0, "none": 0, "hosts": 2}
        events, waited = run([None, one, two])
        self.assertEqual([item["event"] for item in events], ["paths_settled"])
        self.assertEqual(events[0]["paths"], two)
        self.assertGreaterEqual(waited, 2.0)
        self.assertAlmostEqual(started[-1], timeline["scenarios"][0]["probe_s"] - waited,
                               places=6)
        events, _waited = run(iter(lambda: one, None), wait_s=0.3)
        self.assertEqual([item["event"] for item in events], ["paths_timeout"])
        events, waited = run([{"known": False}])
        self.assertEqual(([item["event"] for item in events], waited < 1), (["paths_unknown"], True))
        events, waited = run([], paths=1)
        self.assertEqual(([item["event"] for item in events], waited), (["paths_not_waited"], 0.0))

        def judged(**figures):
            return {item["criterion"]: item for item in scale.change_criteria(
                change_figures(**figures), {"actions": []}, ROLES)}

        name = "crowd on two paths before the change (paths.one = 0)"
        self.assertTrue(judged(paths_wait={"event": "paths_settled"})[name]["ok"])
        self.assertFalse(judged(paths_wait={"event": "paths_timeout"})[name]["ok"])
        unknown = judged(paths_wait={"event": "paths_unknown"})[name]
        self.assertIsNone(unknown["ok"])
        self.assertIn("B Y3", unknown["needs"])
        self.assertNotIn(name, judged(paths_wait={"event": "paths_not_waited"}))
        self.assertIsNone(judged()[name]["ok"])
        # The crowd's paths, from each listener host's newest interval since
        # the wait began; stale lines and the final line do not count.
        base = 1_790_000_000

        def line(t, paths=None, phase=None):
            total = {"up": 100, **({"paths": paths} if paths else {})}
            return json.dumps({"unix_ms": (base + t) * 1000, "total": total,
                               **({"phase": phase} if phase else {})})

        texts = {"lon-load-1": "\n".join([line(1, {"two": 50, "one": 0, "none": 0}),
                                          line(9, {"two": 48, "one": 2, "none": 0})]),
                 "new-load-1": "\n".join([line(9, {"two": 50, "one": 1, "none": 0}),
                                          line(10, {"two": 50, "one": 0, "none": 0},
                                               phase="final")]),
                 "tok-load-1": line(2, {"two": 50, "one": 50, "none": 0})}
        hosts = [{"name": name} for name in texts]
        with mock.patch.object(scale, "ssh_quiet", lambda node, command, **k: texts[node["name"]]):
            self.assertEqual(scale.crowd_paths(hosts, "/r", base + 5),
                             {"known": True, "hosts": 2, "two": 98, "one": 3, "none": 0})
            self.assertIsNone(scale.crowd_paths(hosts, "/r", base + 20))
        texts = {"lon-load-1": line(9)}
        with mock.patch.object(scale, "ssh_quiet", lambda node, command, **k: texts[node["name"]]):
            self.assertEqual(scale.crowd_paths([{"name": "lon-load-1"}], "/r", base + 5),
                             {"known": False})
        self.assertEqual(scale.load_paths(argparse.Namespace(load_args="--paths 1 --spare false")),
                         1)
        self.assertEqual(scale.load_paths(argparse.Namespace(load_args="")), 2)
        # The edges' confirmations turned away (B Z3): 0, or open without
        # the counter.
        meshes = {"lon-edge-1": [{"t": t, "client_udp": {"assignment_confirms_limited": value}}
                                 for t, value in ((0, 4), (10, 4), (20, 6))]}
        self.assertEqual(scale.dotted_rise(meshes, "client_udp.assignment_confirms_limited",
                                           (5, 25)), 2)
        self.assertIsNone(scale.dotted_rise({"lon-edge-1": [{"t": 1}]},
                                            "client_udp.assignment_confirms_limited", (0, 5)))
        confirms = "edges: assignment_confirms_limited = 0"
        self.assertFalse(judged(assignment_confirms_limited_rise=2)[confirms]["ok"])
        self.assertTrue(judged(assignment_confirms_limited_rise=0)[confirms]["ok"])
        self.assertIn("B Z3", judged()[confirms]["needs"])

    def test_a_step_stopped_early_still_scores_the_probes_that_ran(self):
        """Review of CL7: the scenario under way when a step stops has its
        probes fetched and scored all the same; the nodes it restarts have
        their boot ids read at its probes' start and its scoring."""
        timeline = scale.phase_timeline(scale.load_phase_plan(PHASES, names=["F2"]), lab())
        collected, logged = [], []

        class Probes:
            hosts = [None]

            def start(self, scenario, seconds):
                pass

            def collect(self, scenario):
                collected.append(scenario)
                return []

        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=None)
        runner = scale.PhaseRunner(lab(), timeline, time.time() - 10_000, logged.append, args,
                                   directory=pathlib.Path(tempfile.mkdtemp()), probes=Probes())
        self.addCleanup(shutil.rmtree, runner.directory, True)
        with mock.patch.object(scale, "http_get", lambda node, path, **k:
                               {"process": {"boot_id": 41}}):
            runner.log_boot_ids("F2", "before")
            runner.start_probes("F2")
            runner.finish()
        self.assertEqual(collected, ["F2"])
        boots = [item for item in logged if item.get("event") == "boot_ids"]
        self.assertEqual([(item["when"], item["boot_ids"]) for item in boots],
                         [("before", {"new-edge-2": 41})])

    def test_a_scenario_that_no_longer_fits_is_skipped_never_started(self):
        """A change that ran past its bound: the next scenario, late, is
        skipped and logged, never started."""
        state = lab()
        timeline = {"hold_s": 4.0, "scenarios": [
            {"name": "S1", "probe_start": 0.0, "probe_s": 0.1, "fault": 0.0,
             "observe_end": 0.0, "end": 0.0,
             "actions": [{"kind": "kill", "targets": ["new-edge-2"], "t": 0.0,
                          "scenario": "S1"}], "restores": []},
            {"name": "S2", "probe_start": 0.3, "probe_s": 0.1, "fault": 0.3,
             "observe_end": 0.3, "end": 0.4,
             "actions": [{"kind": "kill", "targets": ["new-edge-3"], "t": 0.3,
                          "scenario": "S2"}], "restores": []}]}
        done, logged = [], []

        def ssh(node, command, **kwargs):
            done.append((node["name"], command.split(" ", 1)[1].split(" ", 1)[0]))
            if "KILL" in command:
                time.sleep(0.6)  # the kill of S1 runs long
            return ""

        args = argparse.Namespace(stream=4001, budget_usd=None, spent_before_usd=None)
        runner = scale.PhaseRunner(state, timeline, time.time(), logged.append, args,
                                   directory=pathlib.Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, runner.directory, True)
        with mock.patch.object(scale, "ssh", ssh), \
                mock.patch.object(scale, "SCENARIO_LATE_S", 0.1), \
                mock.patch.object(scale, "PROBE_GRACE_S", 0.0), \
                mock.patch.object(scale, "http_get", lambda node, path, **k: {}):
            runner.run()
            runner.finish(join_s=2)
        # S1's kill, and its restore (owed, put back by finish); nothing of S2.
        self.assertEqual(done, [("new-edge-2", "kill"), ("new-edge-2", "start")])
        skipped = [item for item in logged if item.get("event") == "scenario_skipped"]
        self.assertEqual([item["scenario"] for item in skipped], ["S2"])

    def test_a_slot_s_add_is_the_lab_tool_s_own_command_line(self):
        """The lab tool's add, as its own parser reads it (C CT3): the slot,
        the run's budget, and what earlier labs cost only when the run was
        given it (otherwise the lab tool takes the ledger's figure; unknown
        is never sent as 0)."""
        spec = importlib.util.spec_from_file_location(
            "lab_for_scale_run", HERE.parent / "linode-small-mesh-lab.py")
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(scale.subprocess, "run", run):
            for spent in (None, 31.4):
                scale.lab_tool_slot("add", "lon-edge-11", argparse.Namespace(
                    budget_usd=100.0, spent_before_usd=spent))
            scale.lab_tool_slot("remove", "lon-edge-11", argparse.Namespace(budget_usd=None))
        parsed = [lab.build_parser().parse_args(command[2:]) for command in commands]
        self.assertEqual([item.handler for item in parsed],
                         [lab.command_add, lab.command_add, lab.command_remove])
        self.assertEqual([(item.names, getattr(item, "budget_usd", None),
                           getattr(item, "spent_before_usd", None)) for item in parsed],
                         [(["lon-edge-11"], 100.0, None), (["lon-edge-11"], 100.0, 31.4),
                          (["lon-edge-11"], None, None)])

    def test_plan_prints_the_phases_and_their_cost(self):
        args = argparse.Namespace(
            topology=str(TOPOLOGY), topology_only=True, without_sites="", second_curve=False,
            edges_per_region=None, json=False, config=None, steps=None, hls_share=None,
            hls_cap_per_host=None, hold=60.0, ramp=None, cost=None, udp_mbps=None,
            inbound_gbps=scale.LOAD_HOST_INBOUND_GBPS, phase_plan=str(PHASES), group="F",
            scenarios="")
        with redirect_stdout(io.StringIO()) as out:
            plan = scale.command_plan(args)
        # The plan's own default, which is what its schedule runs, in
        # every group.
        self.assertEqual(plan["steps"][0]["listeners"], 6000)
        for group in ("F", "R", "A"):
            asked = argparse.Namespace(phase_plan=str(PHASES), group=group, scenarios="",
                                       steps=None, hls_share=None, config=None)
            scale.phase_setup(asked, lab())
            self.assertEqual(asked.steps, "6000", group)
        runs = [block["run"] for block in json.loads(PHASES.read_text())["schedule"]["blocks"]
                if "--phase-plan" in block.get("run", "")]
        self.assertEqual([re.search(r"--steps (\S+)", run).group(1) for run in runs],
                         ["6000"] * 3)
        # Each scenario holds its probes 369 s from 45 s before its fault,
        # and its slot ends 30 s after them (C CL7): 414 s.
        self.assertEqual(plan["steps"][0]["hold_s"], 9 * 414)
        self.assertIn("F8", out.getvalue())
        cost = plan["phases"]["cost"]
        self.assertAlmostEqual(cost["scenarios"]["F1"]["minutes"], 6.9)
        self.assertAlmostEqual(cost["minutes"], (plan["steps"][0]["ramp_s"] + 9 * 414 + 75) / 60,
                               places=1)


class Party(unittest.TestCase):
    """The global listening party (owner, 23 Nov): the schedule's
    arithmetic (shares over the regions, waves, churn, the fault step's
    hold, the joins after the faults), the controller through it all, the
    LiveKit Cloud hook, and every step's raw data in the run archive."""

    def test_the_committed_schedule_loads_and_is_checked(self):
        spec = scale.load_party(scale.PARTY)
        self.assertEqual(spec["steps"], [1000, 5000, 10000, 20000])
        self.assertEqual(sorted(spec["shares"]), ["london", "newyork", "sydney", "tokyo"])
        self.assertEqual([action["kind"] for scenario in spec["faults"]["scenarios"]
                          for action in scenario["actions"]], ["kill", "kill"])
        kinds = {scenario["name"]: scenario["actions"][0]["targets"][0]
                 for scenario in spec["faults"]["scenarios"]}
        roles = {name: lab()["nodes"][target]["role"] for name, target in kinds.items()}
        self.assertEqual(roles, {"P1": "edge", "P2": "distributor"})
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        for change, why in (({"steps": [5000, 1000]}, "rise"), ({"config": "flac"}, "config"),
                            ({"shares": {"london": -1}}, "shares"),
                            ({"waves": {"count": 0, "every_s": 20}}, "waves"),
                            ({"churn": {"share": 1.0, "leave_after_s": 1, "rejoin_after_s": 1}},
                             "churn"),
                            ({"faults": {**spec["faults"], "at_step": 7}}, "at_step")):
            path = directory / f"{why}.json"
            path.write_text(json.dumps({**spec, **change}))
            with self.assertRaises(ValueError, msg=why):
                scale.load_party(path)
        self.assertEqual(scale.parse_shares("london=0.4, newyork=0.4,tokyo=0.1,sydney=0.1"),
                         {"london": 0.4, "newyork": 0.4, "tokyo": 0.1, "sydney": 0.1})
        self.assertIsNone(scale.parse_shares(None))
        for bad in ("london", "london=-1", "london=0,tokyo=0", "london=x"):
            with self.assertRaises(ValueError, msg=bad):
                scale.parse_shares(bad)

    def test_waves_and_churn_are_the_load_tool_s_ramp(self):
        """Listeners join in equal waves; a share leaves and rejoins later;
        each COUNT@SECOND holds from that second on."""
        self.assertEqual(scale.party_ramp(1003, 5, 20.0, 0.1, 160.0, 30.0),
                         "201@0,402@20,603@40,803@60,1003@80,903@160,1003@190")
        self.assertEqual(scale.party_ramp(3, 5, 20.0, 0.1, 160.0, 30.0),
                         "1@0,2@20,3@40,3@60,3@80")
        times = scale.party_times(scale.load_party(scale.PARTY), 0.0, 20)
        self.assertEqual(times, {"ramp_s": 100.0, "leave_at_s": 160.0, "rejoin_at_s": 190.0,
                                 "joins_after_s": 0.0, "hold_s": 150.0})
        # The fault step: the faults' scenarios first, from the end of the
        # waves; then the churn and the joins.
        faulted = scale.party_times(scale.load_party(scale.PARTY), 828.0, 20)
        self.assertEqual((faulted["leave_at_s"], faulted["joins_after_s"], faulted["hold_s"]),
                         (988.0, 828.0, 978.0))
        # Joins that take longer than the churn hold the step longer.
        self.assertEqual(scale.party_times(scale.load_party(scale.PARTY), 0.0, 40)["hold_s"],
                         scale.joins_seconds(40))

    def test_the_party_s_steps_follow_the_shares_and_hold_its_faults(self):
        state = lab()
        spec = scale.load_party(scale.PARTY)
        shares = scale.parse_shares("london=0.4,newyork=0.3,tokyo=0.2,sydney=0.1")
        faults = scale.party_faults(spec, {**state, "standby_edges": STANDBY}, "opus-1")
        self.assertEqual([row["name"] for row in faults["timeline"]["scenarios"]], ["P1", "P2"])
        self.assertEqual(faults["timeline"]["hold_s"], 2 * 414)
        plan = scale.party_plan(spec, state, STANDBY, shares, [1000, 10000], "opus-1", faults)
        first, second = plan["steps"]
        self.assertEqual(first["party"]["by_region"],
                         {"london": 400, "newyork": 300, "tokyo": 200, "sydney": 100})
        self.assertEqual(sum(second["party"]["by_region"].values()), 10000)
        self.assertEqual((first["faults"], second["faults"]), (False, True))
        self.assertEqual(second["hold_s"], 978.0)
        self.assertEqual(second["joins_after_s"], 828.0)
        # Each host's ramp: its own listeners in the step's waves, its share
        # of the churn at the step's times.
        for step in plan["steps"]:
            for by_host in step["allocation"].values():
                for name, item in by_host.items():
                    if not item["udp"]:
                        continue
                    points = [entry.split("@") for entry in step["ramp_by_host"][name].split(",")]
                    self.assertEqual(int(points[4][0]), item["udp"])
                    self.assertEqual(points[-1], [str(item["udp"]),
                                                  f"{step['party']['rejoin_at_s']:g}"])
        self.assertEqual(plan["seconds"], sum(step["seconds"] for step in plan["steps"]))
        self.assertGreater(plan["cost"]["instances_usd"], 0)
        # The run's commands take each host's party ramp.
        args = argparse.Namespace(config="opus-1", sample=20, load_args="", hls_window=40,
                                  hls_http2=False, hls_steering="steered", udp_assignment="on")
        commands = scale.step_commands(state, plan, first, "party", args)
        host = next(name for by_host in first["allocation"].values() for name, item
                    in by_host.items() if item["udp"])
        self.assertIn(f"--ramp {first['ramp_by_host'][host]} ", commands[host])
        with self.assertRaises(ValueError):
            scale.party_plan(spec, state, STANDBY, {"atlantis": 1.0}, [1000], "opus-1", None)

    def test_the_party_is_sized_for_its_paths_and_runs_on_opus_2_5(self):
        """Review of CL7: a two-path listener is two legs, about twice the
        datagrams on its load host and twice the wire rate, so the party is
        sized for its schedule's paths (or a measured cost file as it is);
        and `--config opus-2.5` probes the faults on opus-2.5 (one stream
        with opus-1), not on both."""
        state = lab()
        spec = scale.load_party(scale.PARTY)
        self.assertEqual(spec["paths"], 2)
        one = scale.party_plan({**spec, "paths": 1}, state, STANDBY, spec["shares"],
                               [20000], "opus-1", None)
        two = scale.party_plan(spec, state, STANDBY, spec["shares"], [20000], "opus-1", None)
        self.assertEqual(two["costs"]["udp_vcpu"], 2 * scale.COSTS["udp_vcpu"])
        self.assertEqual(two["udp_mbps"], 2 * scale.CONFIGS["opus-1"]["udp_mbps"])
        for name, capacity in one["capacity"].items():
            self.assertLess(two["capacity"][name]["udp"], capacity["udp"], name)
            self.assertAlmostEqual(two["capacity"][name]["udp"], capacity["udp"] / 2, delta=1)
        load = max(one["steps"][0]["host_load"].values())
        self.assertGreater(max(two["steps"][0]["host_load"].values()), 1.5 * load)
        self.assertAlmostEqual(two["cost"]["listener_traffic_gb"],
                               2 * one["cost"]["listener_traffic_gb"], delta=1.0)
        # Over the hosts at two paths what one path would call drivable.
        hosts = scale.party_plan({**spec, "paths": 1}, state, STANDBY, spec["shares"],
                                 [1000, 5000, 10000, 20000, 40000, 80000, 160000], "opus-1", None)
        both = scale.party_plan(spec, state, STANDBY, spec["shares"],
                                [1000, 5000, 10000, 20000, 40000, 80000, 160000], "opus-1", None)
        self.assertLess(sum(step["drivable"] for step in both["steps"]),
                        sum(step["drivable"] for step in hosts["steps"]))
        # A measured cost file is taken as it is (it was measured at the
        # party's paths); the wire rate still counts both legs.
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        (directory / "cost.json").write_text(json.dumps({"udp_vcpu": 0.009}))
        costs, mbps = scale.party_costs(spec, "opus-1", str(directory / "cost.json"))
        self.assertEqual((costs["udp_vcpu"], mbps), (0.009, 2 * scale.CONFIGS["opus-1"]["udp_mbps"]))
        # The crowd is told its paths; a --load-args that says otherwise is
        # refused.
        self.assertEqual(scale.party_load_args(spec, "--sample-arrivals 5"),
                         "--sample-arrivals 5 --paths 2")
        self.assertEqual(scale.party_load_args(spec, "--paths 2"), "--paths 2")
        with self.assertRaises(ValueError):
            scale.party_load_args(spec, "--paths=1")
        answers = {"lon-load-1": "1\n", "new-load-1": "0\n", "tok-load-1": ""}
        with mock.patch.object(scale, "ssh_quiet", lambda node, command, **k:
                               answers[node["name"]]):
            self.assertEqual(scale.load_tool_has([{"name": name} for name in answers], "--paths"),
                             ["new-load-1", "tok-load-1"])
        # --config opus-2.5: the faults' probes on opus-2.5 and the
        # schedule's configs of other streams.
        faults = scale.party_faults({**spec, "probe_configs": "opus-1,pcm-1"},
                                    {**state, "standby_edges": STANDBY}, "opus-2.5")
        self.assertEqual(faults["probe_configs"], ["opus-2.5", "pcm-1"])
        self.assertEqual(scale.party_faults(spec, state, "opus-1")["probe_configs"], ["opus-1"])
        with redirect_stdout(io.StringIO()) as out:
            scale.main(["party-plan", "--topology-only", "--json", "--config", "opus-2.5"])
        self.assertEqual(json.loads(out.getvalue())["party"]["config"], "opus-2.5")

    def test_the_plan_command_prints_the_party(self):
        with redirect_stdout(io.StringIO()) as out:
            plan = scale.main(["party-plan", "--topology-only", "--json",
                               "--shares", "london=1,newyork=1,tokyo=1,sydney=1"]) or \
                json.loads(out.getvalue())
        self.assertEqual([step["listeners"] for step in plan["steps"]],
                         [1000, 5000, 10000, 20000])
        self.assertEqual(plan["party"]["faults_at"], 10000)

    def test_the_controller_runs_through_the_party_and_livekit_cloud_is_a_hook(self):
        args = argparse.Namespace(config="opus-1", budget_usd=200.0, spent_before_usd=None)
        command = scale.party_controller_command(args, pathlib.Path("/tmp/run"),
                                                 pathlib.Path("/tmp/run/events.jsonl"), 2130.2,
                                                 ["--up-samples", 3])
        self.assertEqual(command[1:], [str(scale.AUTOSCALER), "run", "--backend", "linode",
                                       "--duration", "2131", "--stream", "3001",
                                       "--controller-state", "/tmp/run/autoscale-state.json",
                                       "--events", "/tmp/run/events.jsonl", "--budget-usd",
                                       "200.0", "--up-samples", "3"])
        logged = []
        record = scale.livekit_cloud_hook(scale.load_party(scale.PARTY), {"listeners": 5000},
                                          logged.append)
        self.assertEqual(record["livekit_cloud"]["listeners"], 1000)
        self.assertFalse(record["livekit_cloud"]["run"])
        self.assertEqual(logged, [record])
        self.assertIsNone(scale.livekit_cloud_hook({}, {"listeners": 5000}, logged.append))

    def test_a_party_step_says_who_was_interrupted_and_what_the_controller_did(self):
        step_dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, step_dir, True)
        base = 1_790_000_000
        (step_dir / "lon-load-1" / "udp").mkdir(parents=True)

        def write(interrupted):
            lines = [{"unix_ms": (base + t) * 1000, "total": {
                "up": 100, "parts": 100_000, "missing": 0,
                **({"listeners_interrupted": interrupted if t == 50 else 0}
                   if interrupted is not None else {})}} for t in range(0, 100)]
            (step_dir / "lon-load-1" / "udp" / "intervals.jsonl").write_text(
                "\n".join(json.dumps(line) for line in lines))

        events = [{"unix_us": (base + 30) * 1_000_000, "kind": "decision", "action": "join"},
                  {"unix_us": (base + 45) * 1_000_000, "kind": "serving", "node": "lon-edge-6"},
                  {"unix_us": (base + 200) * 1_000_000, "kind": "serving", "node": "late"},
                  {"unix_us": (base + 40) * 1_000_000, "source": "scale-run", "kind": "kill"}]
        for interrupted, ok in ((0, True), (2, False), (None, None)):
            write(interrupted)
            figures = scale.party_step_figures(step_dir, (base + 10, base + 90), "opus-1", events,
                                               base + 10)
            self.assertEqual(figures["criteria"][0]["ok"], ok, interrupted)
        self.assertEqual(figures["controller"], {"joins": 1, "serving": ["lon-edge-6"],
                                                 "drains": []})
        self.assertIn("B Y4", figures["criteria"][0]["needs"])
        # Review of CL7: the step is judged over its waves too (from its
        # start), and the controller's acts from the end of the step before:
        # a listener interrupted in the 3rd wave and a join between steps
        # count.
        write(2)
        figures = scale.party_step_figures(step_dir, (base + 40, base + 99), "opus-1",
                                           events + [{"unix_us": (base + 5) * 1_000_000,
                                                      "kind": "decision", "action": "join"}],
                                           base + 2)
        self.assertFalse(figures["criteria"][0]["ok"])
        self.assertEqual(figures["controller"]["joins"], 2)

    def test_the_party_reads_the_lab_at_each_step_and_judges_the_whole_step(self):
        """Review of CL7: each step runs on the lab as it stands then (an
        edge the controller created is sampled and fetched), its crowd is
        judged from the step's start (its waves) to the end of its hold,
        and the controller's acts are counted from the end of the step
        before."""
        first = lab()
        grown = lab()
        grown["nodes"]["lon-edge-11"] = {**grown["nodes"]["lon-edge-10"], "name": "lon-edge-11"}
        labs = iter([first, first, grown])
        seen, figures = [], []
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        started = {1000: 1_790_000_000.0, 5000: 1_790_000_500.0}

        def run_step(state, run, plan, step, args, out, log, phases=None, control=None):
            seen.append((step["listeners"], "lon-edge-11" in state["nodes"], args.load_args))
            (out / "steps" / str(step["listeners"])).mkdir(parents=True, exist_ok=True)
            return {"started": started[step["listeners"]], "stopped": None,
                    "gave_out_first": None, "regions": {}, "window": [0, 0]}

        def step_figures(step_dir, window, config, events, since):
            figures.append((window, since))
            return {"criteria": []}

        args = scale.build_parser().parse_args(
            ["party", "--steps", "1000,5000", "--settle", "0", "--budget-usd", "50",
             "--name", "p"])
        source = {"name": "src", "public_ipv4": "1.2.3.4"}
        with mock.patch.object(scale, "state", lambda: next(labs)), \
                mock.patch.object(scale, "deploy_settings", return_value={
                    "playback_address": "vlan", "standby_edges": STANDBY}), \
                mock.patch.object(scale, "check_vlan", lambda *a: []), \
                mock.patch.object(scale, "load_tool_has", lambda hosts, flag: []), \
                mock.patch.object(scale, "OUT", directory), \
                mock.patch.object(scale, "open_archive", lambda *a: None), \
                mock.patch.object(scale, "begin_phases", lambda *a: None), \
                mock.patch.object(scale, "control_setup", lambda *a: None), \
                mock.patch.object(scale.common, "nodes_for", lambda state, kind: [source]), \
                mock.patch.object(scale.common, "nodes_by_role", lambda state, role: [
                    {"name": "contrib-lon", "public_ipv4": "1.2.3.5"}]), \
                mock.patch.object(scale, "ssh", lambda *a, **k: ""), \
                mock.patch.object(scale, "wait_live", lambda *a, **k: True), \
                mock.patch.object(scale.subprocess, "Popen", lambda command, **k:
                                  FakeProcess(command, 0)), \
                mock.patch.object(scale, "run_step", run_step), \
                mock.patch.object(scale, "party_step_figures", step_figures), \
                mock.patch.object(scale, "write_results", lambda *a: None), \
                mock.patch.object(scale, "stop_listeners", lambda *a: None), \
                mock.patch.object(scale.time, "sleep", lambda seconds: None), \
                redirect_stdout(io.StringIO()):
            scale.command_party(args)
        self.assertEqual([(step, node) for step, node, _args in seen],
                         [(1000, False), (5000, True)])
        self.assertTrue(all(load_args.endswith("--paths 2") for _s, _n, load_args in seen))
        plan = json.loads((directory / "scale-p" / "plan.json").read_text())
        ends = [started[step["listeners"]] + step["ramp_s"] + step["hold_s"]
                for step in plan["steps"]]
        self.assertEqual([window for window, _since in figures],
                         [(started[1000], ends[0]), (started[5000], ends[1])])
        self.assertEqual(figures[1][1], ends[0])
        self.assertLess(figures[0][1], started[1000] + 1e9)

    def test_every_step_s_raw_data_goes_to_the_run_archive(self):
        """Keep all the data: the step's directory whole and the step
        recorded (listeners by region, lane, paths), then the run's own
        files, into the run of the lab's id."""
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        directory = root / "target" / "scale-party"
        step_dir = directory / "steps" / "1000"
        (step_dir / "lon-load-1" / "udp").mkdir(parents=True)
        (step_dir / "lon-load-1" / "udp" / "intervals.jsonl").write_text('{"unix_ms": 1}\n')
        (step_dir / "lon-load-1" / "udp" / "summary.json").write_text('{"total": {}}\n')
        (step_dir / "lon-dist-a").mkdir()
        (step_dir / "lon-dist-a" / "sampler.jsonl").write_text('{"t": 1}\n')
        (step_dir / "step.json").write_text("{}\n")
        for name in ("plan.json", "party.json", "scale.json", "summary.md",
                     "controller-party.log", "autoscale-state.json"):
            (directory / name).write_text("{}\n")
        events = directory / "events.jsonl"
        events.write_text('{"unix_us": 1, "step": 1000}\n{"unix_us": 2, "kind": "join"}\n')
        logged = []
        state = {**lab(), "run_id": "party1"}
        step = {"listeners": 1000, "sites": {"london": {"udp": 600, "hls": 0},
                                             "tokyo": {"udp": 400, "hls": 0}}}
        outcome = {"stopped": "lon-load-1: cpu", "regions": {
            "london": {"udp": {"listeners": 412}}, "tokyo": {"udp": {"listeners": 400}}}}
        sent = {}
        with mock.patch.dict(scale.os.environ, {"NEEDLETAIL_DATA_ROOT": str(root / "data")}), \
                mock.patch.object(scale, "deploy_settings", return_value={"playback_address": "vlan"}):
            archive = scale.open_archive(state, "party", logged.append)
            scale.archive_step(archive, "party", directory, step, "opus-1", 2, logged.append,
                               outcome, party={"waves": {"count": 5}}, faults=False)
            # The events go to the archive after every step, as they come
            # (a run that dies keeps them there up to its last step); a line
            # still being written waits for the next time.
            scale.archive_events(archive, "party", events, sent, logged.append)
            with events.open("a") as handle:
                handle.write('{"unix_us": 3, "kind": "serving"}\n{"unix_us": 4, "par')
            scale.archive_events(archive, "party", events, sent, logged.append)
            scale.archive_run(archive, "party", directory, logged.append)
            manifest = json.loads((root / "data" / "party1" / "manifest.json").read_text())
            archived = [json.loads(line) for line in (root / "data" / "party1" / "scale-run"
                                                      / "party" / "events.jsonl").read_text()
                        .splitlines()]
            # Another tool's run of the lab's id is appended to, not refused.
            again = scale.open_archive(state, "party", logged.append)
        self.assertIsNotNone(again)
        self.assertEqual(manifest["kind"], "linode-lab")
        steps = manifest["listener_steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual((steps[0]["step"], steps[0]["lane"], steps[0]["listeners_total"],
                          steps[0]["listeners_by_region"], steps[0]["paths_per_listener"]),
                         (1000, "opus-1", 1000, {"london": 600, "tokyo": 400}, 2))
        self.assertEqual(steps[0]["party"], {"waves": {"count": 5}})
        # A step stopped early says so, beside what the load tool counted.
        self.assertEqual((steps[0]["stopped"], steps[0]["listeners_measured_by_region"]),
                         ("lon-load-1: cpu", {"london": 412, "tokyo": 400}))
        self.assertEqual([item["unix_us"] for item in archived], [1, 2, 3])
        files = {entry["path"] for entry in manifest["files"]}
        for path in ("scale-run/party/steps/1000/lon-load-1/udp/intervals.jsonl",
                     "scale-run/party/steps/1000/lon-load-1/udp/summary.json",
                     "scale-run/party/steps/1000/lon-dist-a/sampler.jsonl",
                     "scale-run/party/steps/1000/step.json", "scale-run/party/plan.json",
                     "scale-run/party/party.json", "scale-run/party/events.jsonl",
                     "scale-run/party/scale.json", "scale-run/party/summary.md",
                     "scale-run/party/controller-party.log",
                     "scale-run/party/autoscale-state.json"):
            self.assertIn(path, files)
        self.assertFalse([item for item in logged if "error" in item], logged)
        # A refusal is loud and leaves the run going (its files stay under
        # target/compare).
        logged.clear()
        with mock.patch.dict(scale.os.environ, {"NEEDLETAIL_DATA_ROOT": str(root / "data")}):
            refused = scale.open_archive({**state, "run_id": "../escape"}, "party",
                                         logged.append)
        self.assertIsNone(refused)
        self.assertEqual(logged[0]["archive"], "refused")
        scale.archive_step(None, "party", directory, step, "opus-1", 2, logged.append)


@unittest.skipUnless(scale.CHANGE_WINDOW.is_file(),
                     "needs the referee's change-window.py (NEEDLETAIL_COMPARE_ROOT)")
class RefereeScoring(unittest.TestCase):
    """The referee's own change-window.py on a scenario's probe of the
    planned length: it finds its 120 no-change windows and scores it; its
    null_fail_rate is kept only where it could measure it."""

    def score(self, span: float) -> dict:
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        seconds = scale.PROBE_LEAD_S + span + scale.probe_tail_s(span)
        launch_us = 1_790_000_000 * 1_000_000
        frames = directory / "probes" / "lon-view" / "opus-1" / "probe.csv"
        frames.parent.mkdir(parents=True)
        with frames.open("w") as handle:
            handle.write("kind,index,latency_us,complete_unix_us,first_unix_us\n")
            for n in range(int(seconds * 100)):
                latency = 7000 + (n * 37) % 500
                source = launch_us + n * 10_000
                handle.write(f"a,{n},{latency},{source + latency},{n}\n")
        first = (launch_us + int(scale.PROBE_LEAD_S * 1e6)) * 1000
        (directory / "marks.txt").write_text(
            scale.mark_line("start", first, "S.kill.lon-edge-2")
            + scale.mark_line("end", first + int(span * 1e9), "S.kill.lon-edge-2"))
        scale.score_scenario(directory, [frames])
        return scale.probe_record(directory)["S.kill.lon-edge-2 lon-view/opus-1"]

    def test_a_probe_of_the_planned_length_is_scored(self):
        result = self.score(90.0)
        self.assertGreaterEqual(result["null_windows"], scale.MIN_NULL_WINDOWS)
        self.assertIsNotNone(result["pass"])
        # Review of CL7: on 130-odd null windows of a 97 s window, CW scores
        # none against 120 others and its rate reads 0.0 by construction:
        # recorded as not measured.
        self.assertIsNone(result["null_fail_rate"])
        self.assertIn("not measured", result["null_fail_rate_unmeasured"])
        # A short change on a 360 s probe: every null window is scored, and
        # the rate is CW's own.
        result = self.score(10.0)
        self.assertIsNotNone(result["null_fail_rate"])
        self.assertNotIn("null_fail_rate_unmeasured", result)


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
        scale.phase_setup(args, lab())
        self.assertEqual((args.joins, args.lk_per_sfu), (0, 0))
        args = argparse.Namespace(phase_plan=str(PHASES), group="F", scenarios="", steps=None,
                                  hls_share=None, config=None, hold=60.0, joins=20,
                                  lk_per_sfu=None)
        with self.assertRaises(ValueError) as caught:
            scale.phase_setup(args, lab())
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
        for kept in ("ph-f", "ph-r", "ph-a", "c2-pcm-udp", "c2-opus25-udp", "c2-opus25-mixed"):
            self.assertIn(kept, names)
        # Shed in the agreed order when C CL7 gave every scenario probes of
        # its own: the joins, the reruns, c1-opus1-mixed, half of curve 2's
        # steps, then F8, R5, F2r, F7 and F5.
        self.assertNotIn("c1-opus1-mixed", names)
        self.assertNotIn("reruns", names)
        runs = {row["name"]: row["run"] for row in self.schedule["blocks"] if "run" in row}
        self.assertIn("--scenarios F1,F2,F3,F4,F6 ", runs["ph-f"])
        self.assertIn("--scenarios R1,R2,R3 ", runs["ph-r"])
        self.assertIn("--scenarios A1,A2 ", runs["ph-a"])
        ramps = [row for row in self.schedule["blocks"] if row["kind"] == "ramp"]
        self.assertTrue(all(row["livekit_public_gb"] > 0 for row in ramps))
        phases = {row["name"]: row for row in self.schedule["blocks"] if row["kind"] == "phases"}
        self.assertEqual(sorted(phases), ["ph-a", "ph-f", "ph-r"])
        self.assertTrue(all(row["steps"] == [6000] for row in phases.values()))
        # No joins in the ramps (shed first; the referee's own sessions of
        # window A run theirs in full).
        self.assertEqual(self.schedule["joins_steps"], 0)

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
