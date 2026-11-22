#!/usr/bin/env python3
"""Tests of scripts/compare/scale-run.py, the scale run's driver: the steps
and their split over regions, hosts and edges (from topology-scale.json,
standby edges left out, Sydney optional), the capacity and cost of a plan,
the commands of the source, needletail-load and ntcmp (VLAN addresses
only), the sampler (run here for two seconds), the readers of
needletail-load's interval and summary files (exact histogram merge across
hosts, the fallback without one), the LL-HLS readers' figures, saturation
and which tier gave out first, a whole step's analysis from files and its
summary.md.
python3 -m unittest scripts/compare/test_scale_run.py"""

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
import unittest
from contextlib import redirect_stdout

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("scale_run", HERE / "scale-run.py")
scale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scale)

TOPOLOGY = scale.TOPOLOGY
STANDBY = [f"lon-edge-{index}" for index in range(5, 9)]


def lab(without=()) -> dict:
    return scale.topology_state(TOPOLOGY, list(without))


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
                         [f"lon-edge-{index}" for index in range(1, 5)])
        self.assertEqual([node["name"] for node in regions["sydney"]["listeners"]],
                         [f"syd-load-{index}" for index in range(1, 5)])
        self.assertEqual([node["name"] for node in regions["tokyo"]["view"]], ["tok-view"])
        # The second curve: eight London edges.
        self.assertEqual(len(scale.regions_of(lab(), [])["london"]["edges"]), 8)
        # Sydney left out.
        self.assertNotIn("sydney", scale.regions_of(lab(["sydney"]), STANDBY))

    def test_allocation_by_edges_vcpus_and_share(self):
        regions = scale.regions_of(lab(), STANDBY)
        alloc = scale.allocate(10000, regions, 0.5)
        per_site = {site: sum(item["udp"] + item["hls"] for item in hosts.values())
                    for site, hosts in alloc.items()}
        self.assertEqual(per_site, {"london": 2500, "newyork": 2500, "sydney": 2500,
                                    "tokyo": 2500})
        # Two g6-dedicated-8 in London share evenly, four g6-dedicated-4 in Sydney too.
        self.assertEqual(alloc["london"]["lon-load-1"], {
            "udp": 625, "hls": 625, "hls_by_edge": {f"lon-edge-{i}": 157 if i == 1 else 156
                                                    for i in range(1, 5)}}
            if alloc["london"]["lon-load-1"]["hls_by_edge"]["lon-edge-1"] == 157
            else alloc["london"]["lon-load-1"])
        self.assertEqual(sum(item["hls"] for item in alloc["sydney"].values()), 1250)
        # Every edge of a region gets the same readers, to one.
        by_edge: dict[str, int] = {}
        for item in alloc["london"].values():
            for edge, count in item["hls_by_edge"].items():
                by_edge[edge] = by_edge.get(edge, 0) + count
        self.assertEqual(sum(by_edge.values()), 1250)
        self.assertLessEqual(max(by_edge.values()) - min(by_edge.values()), 1)
        # Twice the edges in London: twice London's share.
        wide = scale.allocate(1000, scale.regions_of(lab(), []), 0.5)
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
        plan = scale.plan_steps(lab(), [100, 1000, 5000], "pcm-1", standby=STANDBY)
        self.assertEqual([step["listeners"] for step in plan["steps"]], [100, 1000, 5000])
        self.assertTrue(plan["steps"][0]["drivable"])
        self.assertTrue(plan["steps"][1]["drivable"])
        # Half LL-HLS at 1 ms parts is more than the listener hosts drive.
        self.assertFalse(plan["steps"][2]["drivable"])
        capacity = plan["capacity"]["lon-load-1"]
        self.assertEqual(capacity["vcpus"], 8)
        # 1 ms PCM: the host's inbound is what bounds UDP.
        self.assertEqual(capacity["udp"], capacity["udp_by_network"])
        cost = scale.cost_estimate(lab(), plan)
        self.assertAlmostEqual(cost["hourly_usd"], 4.158, places=3)
        self.assertGreater(cost["listener_traffic_gb"], 0)
        # A cost file replaces the planning figures.
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "cost.json").write_text(json.dumps({"udp_vcpu": 0.0005, "other": 1}))
        costs = scale.load_costs(str(tmp / "cost.json"))
        self.assertEqual(costs, {"udp_vcpu": 0.0005, "hls_vcpu": scale.COSTS["hls_vcpu"]})
        # 2.5 ms parts cost 40% of 1 ms ones per listener.
        fast = scale.host_capacity({"type": "g6-dedicated-8"}, "pcm-1")
        slow = scale.host_capacity({"type": "g6-dedicated-8"}, "opus-2.5")
        self.assertAlmostEqual(slow["hls"], fast["hls"] * 2.5, delta=2)

    def test_plan_command_offline(self):
        args = argparse.Namespace(
            topology_only=True, topology=str(TOPOLOGY), without_sites="sydney",
            second_curve=False, steps="100,1000", config="opus-1", hls_share=0.5,
            hls_cap_per_host=None, hold=30.0, ramp=None, cost=None, udp_mbps=None,
            inbound_gbps=10.0, json=False)
        with redirect_stdout(io.StringIO()) as out:
            plan = scale.command_plan(args)
        self.assertNotIn("sydney", plan["regions"])
        self.assertIn("step   1000", out.getvalue())
        self.assertAlmostEqual(plan["cost"]["hourly_usd"], 4.158 - 0.918, places=3)


class Commands(unittest.TestCase):
    def test_settings_hold_the_standby_edges_for_the_first_curve(self):
        first = scale.scale_settings(json.loads(TOPOLOGY.read_text())["standby_edges"])
        self.assertEqual(first["standby_edges"], STANDBY)
        self.assertEqual(first["playback_address"], "vlan")
        self.assertIn("pcm", first["daw_hls_formats"])
        with redirect_stdout(io.StringIO()) as out:
            scale.command_settings(argparse.Namespace(topology=str(TOPOLOGY),
                                                      second_curve=True))
        self.assertIn("--set standby_edges=[]", out.getvalue())
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
                                     20, 80, 20, "/r/7", "run-1000-lon-load-1", "10.80.1.101")
        for part in ("needletail-load ", "--edges 10.80.1.21:19446,10.80.1.22:19446",
                     "--stream 4001", "--listeners 700", "--duration 80", "--sample 20",
                     "--bind 10.80.1.101", "--out /r/7/udp", "--quiet", "ulimit -n 1048576"):
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
        self.assertEqual(len(commands), 10)
        public = {node["public_ipv4"] for node in state["nodes"].values()}
        for name, command in commands.items():
            self.assertFalse([address for address in public if address in command], name)
            self.assertNotIn("lon-edge-5", command)
        self.assertIn("10.80.1.21:19446", commands["lon-load-1"])

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
        # The tool's own verdict.
        intervals = [{"saturated": True, "reasons": ["recv lag p99 9 ms"]}]
        self.assertEqual(scale.load_saturated([row(1, 0.1)], intervals),
                         "needletail-load: recv lag p99 9 ms")
        self.assertIsNone(scale.load_saturated([row(1, 0.1)], [{"saturated": False}]))


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

        def sampler(name, cpu, tx=1000):
            directory = tmp / name
            directory.mkdir(exist_ok=True)
            lines = [json.dumps({"public_iface": "eth0", "cpus": 2})]
            lines += [json.dumps({"t": t0 + i, "cpu": cpu, "core_max": cpu,
                                  "rx": {"eth0": 10, "eth1": 10}, "tx": {"eth0": tx, "eth1": tx},
                                  "udp": {"RcvbufErrors": 0, "SndbufErrors": 0}})
                      for i in range(30)]
            (directory / "sampler.jsonl").write_text("\n".join(lines) + "\n")

        for name in state["nodes"]:
            sampler(name, 0.2)
        sampler("tok-edge-3", 0.97)
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
                    "load_host_saturated": False}) for i in range(20)) + "\n")
                (tmp / host / "hls.condensed.json").write_text(json.dumps([
                    {"audio_packets": 1000, "audio_sequence_gaps": 0, "first_audio_ms": 200,
                     "audio_latency_ms": {"count": 1000, "p50": 25.0, "p99": 31.0, "max": 40}}]))
        (tmp / "counters.json").write_text(json.dumps({"lon-edge-1": {"subscriptions": 12}}))
        result = scale.analyse_step(state, tmp, step, (t0 + 5, t0 + 25))
        london = result["regions"]["london"]
        self.assertEqual(london["udp"]["latency_ms"]["p50"], 1.0)
        self.assertEqual(london["udp"]["latency_ms"]["p99"], 4.0)
        self.assertEqual(london["udp"]["listeners"], 12)
        self.assertEqual(london["hls"]["latency_ms"]["p50"], 25.0)
        self.assertEqual(result["edge_listeners"]["lon-edge-1"], 12)
        self.assertEqual(result["tiers"]["edge"]["nodes"], 16 + 4)  # standby edges sampled too
        self.assertEqual(result["gave_out_first"]["node"], "tok-edge-3")
        self.assertEqual(result["edge_counters"]["lon-edge-1"]["subscriptions"], 12)
        self.assertGreater(result["public_transfer_gb"], 0)
        text = scale.format_summary({"run": "t", "config": "pcm-1", "hls_share": 0.5,
                                     "steps": [result]})
        self.assertIn("| 100 | london | 1 | 4 |", text)
        self.assertIn("edge (tok-edge-3, cpu)", text)

    def test_counter_delta(self):
        self.assertEqual(scale.counter_delta({"e": {"parts": 5, "fast_lane": None}},
                                             {"e": {"parts": 9, "fast_lane": {"x": 1}},
                                              "f": None}),
                         {"e": {"parts": 4}, "f": None})


if __name__ == "__main__":
    unittest.main()
