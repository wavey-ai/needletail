"""Offline tests of the 4K isolation lab: its topology (dedicated plans,
cost, the shapes, the compiled first hop), the driver's plan and commands,
a whole dry run, and the report on synthetic cells.
python3 -m unittest scripts/test_4k_isolate.py"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


iso = load_module("iso_driver", HERE / "linode-4k-isolate.py")
report = load_module("iso_report_test", HERE / "compare" / "iso-report.py")
deploy = load_module("deploy_iso", HERE / "linode-small-mesh-deploy.py")
COMPILER = deploy.common.ROOT / "target" / "release" / "needletail-compile"
ROUTES = {"best_direct_rtt_us": 300, "primary_rtt_us": 300, "primary_jitter_us": 100,
          "secondary_rtt_us": 400, "secondary_jitter_us": 100}


def value_of(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def lab_state() -> dict:
    """The isolation topology as `up` leaves it in the lab state."""
    topology = iso.load_topology()
    nodes = {}
    for index, node in enumerate(topology["nodes"], 10):
        nodes[node["name"]] = {**node, "public_ipv4": f"198.51.100.{index}",
                               "vlan_ipv4": (node.get("vlan_ipv4") or "").split("/")[0] or None}
    return {"run_id": "isotest", "hourly_usd": 0.0, "nodes": nodes, "slots": {}}


class Topology(unittest.TestCase):
    def test_dedicated_plans_cost_and_ceilings(self):
        topology = iso.load_topology()
        for node in topology["nodes"]:
            self.assertTrue(node["type"].startswith("g6-dedicated-"), node["name"])
        figures = iso.cost(topology)
        self.assertEqual(figures["instances"], 7)
        self.assertAlmostEqual(figures["hourly_usd"], 0.648)
        self.assertLessEqual(figures["hourly_usd"], iso.MAX_HOURLY_USD)
        self.assertLessEqual(figures["planned_usd"], iso.LAB_CEILING_USD)
        self.assertLessEqual(figures["instances"], iso.MAX_NODES)
        # The contributor alone on its own g6-dedicated-4; the mesh's own
        # size for the forwarder and edge of shapes one and two.
        types = {node["name"]: node["type"] for node in topology["nodes"]}
        self.assertEqual(types["contrib-lon"], "g6-dedicated-4")
        self.assertEqual(types["lon-dist-a"], "g6-dedicated-2")
        self.assertEqual(types["lon-edge-1"], "g6-dedicated-2")
        self.assertEqual(types["lon-dist-c4"], "g6-dedicated-4")
        self.assertEqual(types["lon-edge-c4"], "g6-dedicated-4")
        # The watchdog holds the lab inside two billed hours.
        self.assertLess(iso.WATCHDOG_HOURS, iso.BILLED_HOURS)

    def test_shapes_name_the_lab_s_nodes(self):
        topology = iso.load_topology()
        roles = {node["name"]: node["role"] for node in topology["nodes"]}
        edges = {name for name, role in roles.items() if role == "edge"}
        for name, shape in topology["iso"]["shapes"].items():
            self.assertEqual(roles[shape["edge"]], "edge", name)
            self.assertEqual(roles[shape["forwarder"]], "distributor", name)
            self.assertNotIn(shape["forwarder"], shape["blind"], name)
            # Exactly one edge serves: the viewer's.
            self.assertEqual(edges - set(shape["held_out"]), {shape["edge"]}, name)
        self.assertEqual(topology["iso"]["shapes"]["two"]["blind"], [])


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Render(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for target, name, value in ((deploy, "SETTINGS_FILE", self.dir / "settings.json"),
                                    (deploy, "HUNT_FILE", self.dir / "ecmp-ports.json"),
                                    (deploy.common, "ARTIFACT_DIR", self.dir)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The flags the resilient mesh adds are rendered only to a binary
        # that lists them (binary_takes): the cross-built set when it is here.
        binaries = {"av-mesh": iso.mesh_binary("av-mesh"),
                    "av-contrib": iso.contrib_root() / "target" / "x86_64-unknown-linux-gnu"
                    / "release" / "av-contrib"}
        self.binaries = all(path.is_file() for path in binaries.values())
        patcher = mock.patch.dict(deploy.RENDER_BINARIES, binaries if self.binaries else {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, arm: str = "default", shape: str = "one") -> dict[str, list[str]]:
        (self.dir / "settings.json").unlink(missing_ok=True)
        held = iso.load_topology()["iso"]["shapes"][shape]["held_out"]
        with redirect_stderr(io.StringIO()):
            settings = deploy.load_settings(["daw_hls_formats=flac,opus,pcm"]
                                            + iso.arm_settings(arm, held))
        state = deploy.active_state(lab_state(), settings)
        program = deploy.build_program(state, settings, ROUTES)
        plan = deploy.compile_plan(program)
        return {name: item["args"] for name, item in deploy.render(state, settings, plan).items()}

    def test_the_first_hop_and_the_edges_parents(self):
        args = self.render()
        contrib = args["contrib-lon"]
        self.assertEqual(value_of(contrib, "--relay-primary-id"), "lon-dist-a")
        self.assertEqual(value_of(contrib, "--relay-primary-target"), "10.78.1.11:22001")
        self.assertEqual(value_of(contrib, "--relay-secondary-id"), "lon-dist-c4")
        self.assertEqual(value_of(contrib, "--relay-secondary-target"), "10.78.1.12:22002")
        # Video at the ports plus the class offset: inside the driver's drop.
        low, high = (int(port) for port in iso.load_topology()["iso"]["video_ports"].split("-"))
        for name, port in (("lon-dist-a", 22001), ("lon-dist-c4", 22002)):
            self.assertEqual(value_of(args[name], "--fec-bind"), f"0.0.0.0:{port}")
            self.assertEqual(value_of(args[name], "--relay-class-port-offset"), "1000")
            self.assertTrue(low <= port + 1000 <= high)
        self.assertEqual(value_of(args["lon-edge-1"], "--relay-primary-id"), "lon-dist-a")
        self.assertEqual(value_of(args["lon-edge-1"], "--relay-secondary-id"), "lon-dist-c4")
        # The held-out edge is not rendered (it is staged and stopped).
        self.assertNotIn("lon-edge-c4", args)
        d4 = self.render(shape="d4")
        self.assertEqual(value_of(d4["lon-edge-c4"], "--relay-primary-id"), "lon-dist-c4")
        self.assertEqual(value_of(d4["lon-edge-c4"], "--relay-secondary-id"), "lon-dist-a")
        self.assertNotIn("lon-edge-1", d4)
        # The class threads pinned 0,1 by default on every node with them.
        for name in ("lon-dist-a", "lon-edge-1"):
            self.assertEqual(value_of(args[name], "--relay-class-cpus"), "0,1")

    def test_each_arm_renders_its_one_change(self):
        unpinned = self.render("unpinned")
        for name in ("lon-dist-a", "lon-dist-c4", "lon-edge-1"):
            self.assertEqual(unpinned[name].count("--relay-class-cpus"), 1, name)
            self.assertEqual(value_of(unpinned[name], "--relay-class-cpus"), "0,", name)
        fixed = self.render("sockbuf16")
        for name in ("lon-dist-a", "lon-edge-1"):
            self.assertEqual(value_of(fixed[name], "--relay-video-socket-buffer-ms"), "0")
            self.assertEqual(value_of(fixed[name], "--udp-socket-buffer-bytes"), str(16 << 20))
        self.assertEqual(value_of(self.render("sockbuf128")["lon-dist-a"],
                                  "--udp-socket-buffer-bytes"), str(128 << 20))
        gso = self.render("gso-off")
        self.assertEqual(value_of(gso["lon-dist-a"], "--relay-video-gso"), "false")
        if self.binaries:
            self.assertEqual(value_of(gso["lon-edge-1"], "--client-udp-gso"), "false")
        self.assertEqual(value_of(self.render("gro-off")["lon-edge-1"], "--relay-video-gro"), "false")
        # The default renders none of the arms' flags.
        default = self.render()
        self.assertNotIn("--relay-video-socket-buffer-ms", default["lon-dist-a"])
        self.assertNotIn("--udp-socket-buffer-bytes", default["lon-dist-a"])
        self.assertEqual(value_of(default["lon-dist-a"], "--relay-video-gso"), "true")


class Plan(unittest.TestCase):
    def test_the_schedule_in_priority_order(self):
        names = [item["name"] for item in iso.schedule()]
        self.assertEqual(names[:10], ["W0", "B40", "B80", "B120", "B164", "B247", "B333",
                                      "T164", "T247", "T333"])
        self.assertEqual(names[-3:], ["A-d4-247", "A-d4-333", "S247"])
        arms = [item["arm"] for item in iso.schedule() if item["group"] == "ab"]
        self.assertEqual(arms[0], "sockbuf16")
        self.assertNotIn("repair", arms)
        # Every cell restarts its nodes; the high rungs are short.
        for item in iso.schedule():
            self.assertTrue(item["restart"])
            if item["mbit"] >= 164 and item["group"] != "soak":
                self.assertLessEqual(item["seconds"], 40, item["name"])
        self.assertTrue(all(item["shape"] == "d4" for item in iso.schedule()
                            if item["arm"] == "d4"))

    def test_the_ab_rungs_follow_the_base_verdicts(self):
        self.assertEqual(iso.ab_rungs({"40": True, "80": True, "120": True, "164": True,
                                       "247": False}), ("164", "247"))
        self.assertEqual(iso.ab_rungs({"40": True, "247": True, "333": True}), ("333", None))
        self.assertEqual(iso.ab_rungs({"40": False}), (None, "40"))
        cells = iso.ab_cells({"164": True, "247": False})
        rungs = {item["rung"] for item in cells if item["arm"] != "d4"}
        self.assertEqual(rungs, {"164", "247"})
        self.assertEqual([item["rung"] for item in cells if item["arm"] == "d4"], ["247", "333"])
        # Without a whole 247 the ramp has no 333.
        self.assertNotIn("B333", [item["name"] for item in iso.base_cells({"247": False})])
        self.assertIn("T333", [item["name"] for item in iso.two_cells({"247": True})])
        self.assertEqual(iso.ran_name(["B164", "B247"], "247"), "B247")
        self.assertIsNone(iso.ran_name(["B164"], "247"))

    def test_every_cell_gives_every_knob(self):
        for arm in iso.ARMS:
            settings = dict(item.split("=", 1) for item in iso.arm_settings(arm, ["lon-edge-c4"]))
            for key in list(iso.KNOBS) + ["extra_distributor_args", "extra_edge_args",
                                          "standby_edges"]:
                self.assertIn(key, settings, arm)
            self.assertEqual(json.loads(settings["standby_edges"]), ["lon-edge-c4"])
        self.assertEqual(dict(item.split("=", 1) for item in iso.arm_settings(
            "unpinned", []))["relay_class_cpus"], "")
        # The host settings are given back by every other arm.
        self.assertIn("busy_poll=50", iso.host_command("busy50"))
        self.assertIn("busy_poll=0", iso.host_command("default"))
        self.assertIn("rps_sock_flow_entries=32768", iso.host_command("rps-on"))
        self.assertIn("echo 0 >$q/rps_cpus", iso.host_command("gso-off"))

    def test_the_drop_and_the_sources(self):
        block = iso.block_command("10.78.1.5", "23000-23999")
        self.assertIn("-s 10.78.1.5 -p udp --dport 23000:23999 -j DROP", block)
        self.assertIn("iptables -F NTISO", iso.block_command(None, "23000-23999"))
        self.assertNotIn("DROP", iso.block_command(None, "23000-23999"))
        audio = iso.audio_sender_command("10.78.1.5", "/x")
        self.assertIn("--session-id ${START}001000000", audio)
        self.assertIn("START=$(( (NOW / 64 + 1) * 64 ))", audio)
        video = iso.video_sender_command("2160p240", "10.78.1.5", "/x")
        self.assertIn("/var/lib/compare/clips/2160p240", video)
        self.assertIn("rtmp://10.78.1.5:19350/live/needletail-lab", video)
        script = iso.listener_script("/r/B40", ["a", "b"])
        self.assertIn("touch done", script)
        self.assertIn("( a ) >/dev/null 2>>errors.log &", script)

    def test_the_time_check(self):
        runner = iso.Runner(False, pathlib.Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, runner.out, True)
        now = [1000.0]
        lab = iso.Iso(runner, iso.load_topology(), iso.TOPOLOGY, "t", clock=lambda: now[0])
        lab.up_started = 1000.0
        self.assertTrue(lab.fits(iso.cell("B40", "base", "one", "40")))
        now[0] = 1000.0 + iso.PLAN_END_MINUTE * 60 - 60
        self.assertFalse(lab.fits(iso.cell("B40", "base", "one", "40")))


COMPARE = pathlib.Path(os.environ.get("NEEDLETAIL_COMPARE_ROOT",
                                      str(deploy.common.WORKSPACE / "needletail-compare")))


@unittest.skipUnless((COMPARE / "scripts" / "compare" / "compare-run.py").is_file(),
                     "needs the referee's checkout (NEEDLETAIL_COMPARE_ROOT)")
class Dry(unittest.TestCase):
    def test_a_dry_run_prints_every_cell_and_ends_with_down(self):
        environment = {**os.environ, "NEEDLETAIL_COMPARE_ROOT": str(COMPARE),
                       "NEEDLETAIL_DATA_ROOT": tempfile.mkdtemp()}
        self.addCleanup(shutil.rmtree, environment["NEEDLETAIL_DATA_ROOT"], True)
        result = subprocess.run([sys.executable, str(HERE / "linode-4k-isolate.py"), "run", "--dry",
                                 "--run-id", "isotestdry"], capture_output=True, text=True,
                                env=environment, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        text = result.stdout
        for name in ("cell W0", "cell B333", "cell T164", "cell A-sockbuf16-247", "cell A-d4-333",
                     "cell S247"):
            self.assertIn(name, text)
        self.assertIn("push --remeasure --slow-host report", text)
        self.assertIn("needletail-udp-client --entry", text)
        self.assertIn("ntcmp hls --lane nt-h3", text)
        self.assertIn("--stream 4001", text)
        self.assertIn("restart --roles distributor,edge,contributor", text)
        self.assertLess(text.index("# collect"), text.index("# down"))
        self.assertTrue(text.rstrip().splitlines()[-1].endswith("--status complete"))


# ------------------------------------------------------------------ report

def node(core=50.0, softirq=5.0, drops=None, tee=None, rss=500, stop=None, thread="video", last=1):
    return {"drops": drops or {},
            "cpu": {"cpus": {"cpu0": {"busy_percent": 20.0, "softirq_percent": 2.0},
                             "cpu1": {"busy_percent": core, "softirq_percent": softirq}},
                    "threads": {f"{thread}/123": {"percent": core - softirq, "last_cpu": last}}},
            "rss": {"peak_mb": rss}, "tee": tee or {}, "stop_ms": stop}


def row(name, rung, mbit, whole, group="base", arm="default", shape="one", **edge):
    return {"name": name, "group": group, "arm": arm, "shape": shape, "rung": rung, "mbit": mbit,
            "scored": True, "whole": whole, "on_time_percent": 100.0 if whole else 90.0,
            "latency_p99_ms": 60.0 if whole else 900.0, "lateness_worst_ms": 30.0 if whole else 800.0,
            "late_parts": 0 if whole else 40,
            "late_by_hop": {} if whole else {"forwarder.relay_first->forwarder.relay_forward": 35,
                                             "edge.relay_first->edge.udp_sent": 5},
            "forwarder": "lon-dist-a", "edge": "lon-edge-1",
            "nodes": {"contrib-lon": node(), "lon-dist-a": node(**edge), "lon-edge-1": node()}}


class Report(unittest.TestCase):
    def test_the_limit_and_its_cause(self):
        table = [row("B164", "164", 163.8, True),
                 row("B247", "247", 247.4, False, core=99.0, softirq=45.0,
                     drops={"sock.10.78.1.11:23001": 1200.0, "UdpRcvbufErrors": 1200.0})]
        limits = report.limits(table)
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0]["first_not_whole"], "247")
        self.assertEqual(limits[0]["highest_whole"], "164")
        text = "; ".join(limits[0]["causes"])
        self.assertIn("socket buffer: lon-dist-a 10.78.1.11:23001 dropped 1200", text)
        self.assertIn("kernel: lon-dist-a UdpRcvbufErrors 1200", text)
        self.assertIn("core: lon-dist-a cpu1 99.0% busy (softirq 45.0%), its main thread video/123", text)
        self.assertIn("late at: forwarder.relay_first->forwarder.relay_forward (35 of 40", text)

    def test_memory_tee_and_stops_are_named(self):
        broken = row("B333", "333", 333.0, False, rss=3400,
                     tee={"tee_dropped": 10, "tee_parts_shed": 2, "tee_lag_max_ms": 900.0,
                          "tee_cpu_percent": 40.0}, stop=350.0)
        broken["cut"] = {"node": "lon-dist-a", "rss_mb": 3350}
        text = "; ".join(report.causes(broken))
        self.assertIn("memory: lon-dist-a RSS 3350 MB, the source stopped", text)
        self.assertIn("memory: lon-dist-a RSS peak 3400 MB", text)
        self.assertIn("tee: lon-dist-a dropped 10, parts shed 2", text)
        self.assertIn("stop: lon-dist-a heartbeat 350.0 ms late", text)
        # A whole cell with nothing short says nothing.
        self.assertEqual(report.causes(row("B40", "40", 40.2, True)), [])

    def test_ab_deltas_against_base_at_the_same_rung(self):
        table = [row("B247", "247", 247.4, False, core=99.0),
                 row("A-rps-on-247", "247", 247.4, True, group="ab", arm="rps-on", core=70.0),
                 row("A-gso-off-247", "247", 247.4, False, group="ab", arm="gso-off", core=99.0,
                     drops={"sock.x:23001": 5.0})]
        deltas = {item["arm"]: item for item in report.ab_deltas(table)}
        self.assertEqual(deltas["rps-on"]["better"], "better")
        self.assertEqual(deltas["rps-on"]["delta"]["worst_core"], -29.0)
        self.assertEqual(deltas["gso-off"]["better"], "worse")
        self.assertEqual(deltas["gso-off"]["delta"]["drops"], 5.0)
        # Each configuration has its own limit.
        configs = {item["config"] for item in report.limits(table)}
        self.assertEqual(configs, {"one/default", "one/rps-on", "one/gso-off"})
        self.assertIn("| A-rps-on-247 | one | rps-on |", report.markdown(table))

    def test_counters_of_a_snapshot(self):
        start = ("== nstat\nUdpRcvbufErrors 10 0.0\nUdpSndbufErrors 0 0.0\n"
                 "== softnet\n0000aa00 00000001 00000002\n0000bb00 00000000 00000000\n"
                 "== sockets\nUNCONN 0 0 0.0.0.0:23001 0.0.0.0:*\n\t skmem:(r0,rb1000,t0,tb100,f0,w0,o0,bl0,d7)\n"
                 "== ethtool eth1\n     rx_drops: 3\n     rx_packets: 99\n"
                 "== qdisc\nqdisc fq 8002: parent 1:2\n Sent 1 bytes 1 pkt (dropped 4, overlimits 0)\n"
                 "== iptables\nChain NTISO (1 references)\n pkts bytes target prot opt in out source destination\n"
                 "== rss\nav-mesh 123 512000 30\n")
        end = start.replace("UdpRcvbufErrors 10", "UdpRcvbufErrors 25").replace(
            "d7)", "d19)").replace("00000001 00000002", "00000005 00000002").replace(
            "rx_drops: 3", "rx_drops: 8").replace("dropped 4", "dropped 6")
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        (directory / "snapshot-start.txt").write_text(start)
        (directory / "snapshot-end.txt").write_text(end)
        moved = report.drops(directory)
        self.assertEqual(moved["UdpRcvbufErrors"], 15.0)
        self.assertEqual(moved["sock.0.0.0.0:23001"], 12.0)
        self.assertEqual(moved["softnet.cpu0.dropped"], 4.0)
        self.assertEqual(moved["ethtool.eth1.rx_drops"], 5.0)
        self.assertEqual(moved["qdisc.dropped"], 2.0)
        self.assertNotIn("ethtool.eth1.rx_packets", moved)
        self.assertEqual(report.snapshot_rss(start), {"av-mesh": 500.0})

    def test_cpu_per_vcpu_and_thread(self):
        stat0 = "100.0 S stat cpu1 100 0 100 700 0 0 100 0 0 0"
        stat1 = "110.0 S stat cpu1 300 0 200 800 0 0 600 0 0 0"
        fields = " ".join(["S"] + ["0"] * 10 + ["{u}", "{s}"] + ["0"] * 23 + ["1"])
        task0 = "100.0 S task 77 81 (rs-video) " + fields.format(u=100, s=50)
        task1 = "110.0 S task 77 81 (rs-video) " + fields.format(u=800, s=150)
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "compare-sys.log"
        path.write_text("\n".join([stat0, task0, stat1, task1]) + "\n")
        figures = report.cpu_figures(path, 100.0, 110.0)
        # 900 ticks: 800 busy, 500 of them softirq, 100 system.
        self.assertEqual(figures["cpus"]["cpu1"], {"busy_percent": 88.9, "softirq_percent": 55.6,
                                                   "system_percent": 11.1})
        self.assertEqual(figures["threads"]["rs-video/81"], {"percent": 80.0, "last_cpu": 1})

    def test_late_parts_go_on_the_hop_that_held_them(self):
        paths = {}
        for part in range(100):
            hold = 300_000 if part == 50 else 0
            times = [("published", 0), ("contrib.sent_first", 1_000), ("forwarder.relay_first", 2_000),
                     ("forwarder.relay_forward", 2_100 + hold), ("edge.relay_first", 3_000 + hold),
                     ("edge.udp_sent", 3_500 + hold), ("viewer.kernel", 4_500 + hold),
                     ("viewer.arrival", 5_000 + hold)]
            paths[part] = [(label, 1_795_000_000_000_000 + at + part * 41_666)
                           for label, at in times]
        # A part without a publication stamp, and one on another path, are
        # left out.
        paths[200] = [("published", 0), ("viewer.arrival", 1_795_000_000_000_000)]
        paths[201] = [("published", 1_795_000_000_000_000), ("edge.relay_first", 1_795_000_000_900_000),
                      ("viewer.arrival", 1_795_000_001_000_000)]
        figures = report.lateness(paths)
        self.assertEqual(figures["late_parts"], 1)
        self.assertEqual(figures["parts"], 100)
        self.assertEqual(figures["parts_other_path"], 1)
        self.assertEqual(figures["late_by_hop"],
                         {"forwarder.relay_first->forwarder.relay_forward": 1})
        self.assertEqual(figures["lateness_worst_ms"], 300.0)
        self.assertEqual(figures["hops_ms"]["contrib.sent_first->forwarder.relay_first"]["p50"], 1.0)

    def test_whole_is_the_referee_s_rule(self):
        self.assertTrue(report.whole({"on_time_percent": 99.5, "longest_stall_ms": 500}))
        self.assertFalse(report.whole({"on_time_percent": 99.4, "longest_stall_ms": 0}))
        self.assertFalse(report.whole({"on_time_percent": 100.0, "longest_stall_ms": 501}))
        self.assertFalse(report.whole({"error": "x", "on_time_percent": 100.0}))


if __name__ == "__main__":
    unittest.main()
