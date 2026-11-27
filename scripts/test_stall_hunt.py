#!/usr/bin/env python3
"""Tests of the stall hunt's lab driver (scripts/linode-stall-hunt.py,
D-hunt.md 7, as revised after the wave 9 run), offline: no lab, no token,
no request, no ssh. The lab's cost is under the hunt's ceilings and every
plan is dedicated; the timeline puts the first session on the fresh deploy
first, with nothing before it but the referee's idle and burn and the
touched arm; a dry run prints every command and runs none; a real run
refuses before anything is billed when the lab tool has no watchdog and
budget on `up`, or av-mesh no switch for the arena warm-up; the edge
replaced is the one not run most; the touched memory is held until it is
given back; the sampler writes what the report reads; a replacement that
fails part-way still pushes and samples the new instance; the first command
on a new instance waits for its ssh.
python3 -m unittest scripts/test_stall_hunt.py"""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hunt_tool = load("stall_hunt", "linode-stall-hunt.py")
lab_tool = load("stall_hunt_lab", "linode-small-mesh-lab.py")

TOPOLOGY = hunt_tool.TOPOLOGY
# The local lab's sampler (compare/first-session.py); the paid hunt samples
# with the referee's samplers.
SAMPLER = HERE / "hunt-sampler.sh"
TYPES = {plan: {"id": plan, "class": "dedicated", "price": {"hourly": price},
                "region_prices": []}
         for plan, price in (("g6-dedicated-2", 0.054), ("g6-dedicated-4", 0.108))}
TYPES["g6-standard-2"] = {"id": "g6-standard-2", "class": "standard",
                          "price": {"hourly": 0.036}, "region_prices": []}
REGIONS = {region: {"id": region, "status": "ok", "capabilities": ["Vlans"]}
           for region in ("gb-lon", "us-east", "ap-northeast", "ap-southeast", "nl-ams")}


def dry_run(topology: dict | None = None) -> tuple[str, pathlib.Path]:
    """A whole dry run of the hunt: (what it printed, its output directory).
    Any subprocess or ssh would raise."""
    topology = topology or hunt_tool.load_topology()
    out = pathlib.Path(tempfile.mkdtemp()) / "run"
    printed = io.StringIO()

    def refused(*_args, **_kwargs):
        raise AssertionError("a dry run ran something")

    with mock.patch.object(hunt_tool.subprocess, "run", refused), \
            mock.patch.object(hunt_tool.common, "ssh", refused), \
            mock.patch.object(hunt_tool.common, "rsync_to", refused), \
            mock.patch.object(hunt_tool.common, "rsync_from", refused), \
            mock.patch.object(hunt_tool.stall_records, "fetch", refused), \
            contextlib.redirect_stdout(printed):
        hunt = hunt_tool.Hunt(hunt_tool.Runner(True, out), topology, TOPOLOGY, "hunttest",
                              budget_usd=300)
        status = hunt.run()
    assert status == 0, printed.getvalue()[-2000:]
    return printed.getvalue(), out


class Cost(unittest.TestCase):
    def test_cost_under_the_ceiling(self):
        topology = hunt_tool.load_topology()
        figures = hunt_tool.cost(topology)
        self.assertEqual(figures["instances"], 24)
        self.assertAlmostEqual(figures["hourly_usd"], 1.512)
        self.assertLessEqual(figures["hourly_usd"], hunt_tool.MAX_HOURLY_USD)
        self.assertLessEqual(figures["instances"], hunt_tool.MAX_NODES)
        # Near $3 to $4 and two billed hours (the lead, after the 4K run).
        self.assertEqual(figures["billed_hours"], 2)
        self.assertAlmostEqual(figures["planned_usd"], 3.024)
        self.assertAlmostEqual(figures["run_usd"], 3.132)
        self.assertTrue(3.0 <= figures["run_usd"] <= 4.0)
        self.assertAlmostEqual(figures["with_one_repeat_usd"], 6.264)
        # The lab tool prices it the same from the API's plans and finds
        # nothing against it: every plan dedicated, every VLAN address once.
        priced = json.loads(json.dumps(topology))
        hourly, problems = lab_tool.topology_cost(priced, TYPES, REGIONS)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(hourly, figures["hourly_usd"])
        self.assertTrue(all(node["type"].startswith("g6-dedicated-")
                            for node in topology["nodes"]))

    def test_a_shared_plan_is_refused(self):
        topology = hunt_tool.load_topology()
        topology["nodes"][0]["type"] = "g6-standard-2"
        with self.assertRaises(SystemExit):
            hunt_tool.hourly_usd(topology)
        _hourly, problems = lab_tool.topology_cost(topology, TYPES, REGIONS)
        self.assertTrue(any("not a dedicated-CPU plan" in line for line in problems))

    def test_every_tool_gets_the_hunt_s_own_state_and_ceilings_and_no_token(self):
        environment = hunt_tool.tool_environment()
        self.assertEqual(environment["NEEDLETAIL_LINODE_MAX_HOURLY_USD"], "1.60")
        self.assertEqual(environment["NEEDLETAIL_LINODE_MAX_NODES"], "26")
        self.assertTrue(environment["NEEDLETAIL_LINODE_LAB_STATE"].endswith("hunt-lab.json"))
        self.assertFalse(any("TOKEN" in key or "PAT" in key for key in environment))


class Timeline(unittest.TestCase):
    def test_the_first_session_is_the_first_act(self):
        topology = hunt_tool.load_topology()
        out = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        hunt = hunt_tool.Hunt(hunt_tool.Runner(True, out), topology, TOPOLOGY, "hunttest")
        steps = hunt_tool.timeline(hunt)
        names = [step["name"] for step in steps]
        matrix = [hunt_tool.describe(combination) for combination in hunt_tool.MATRIX]
        self.assertEqual(names, [
            "hosts", "watch", "push", "the referee's samplers", "survey", "media",
            "touch the touched edges", "burn", "give the touched memory back", "S1", "S2", "S3",
            "replace the edge not run most", *matrix, "collect", "down"])
        minutes = [step["minute"] for step in steps]
        self.assertEqual(minutes, sorted(minutes))
        at = dict(zip(names, minutes))
        self.assertEqual([at[f"S{index}"] for index in range(1, 4)], [20, 24, 28])
        # The matrix from minute 44, each combination as the one before
        # ends (no fixed slots: room for M5 in the usual case).
        self.assertEqual([at[name] for name in matrix], [44] * 5)
        self.assertEqual((at["touch the touched edges"], at["burn"],
                          at["give the touched memory back"]), (15, 18, 19.5))
        self.assertEqual((at["collect"], at["down"]), (96, 101))
        roles = {session["name"]: (session["config"], session["role"], session["seconds"])
                 for session in hunt_tool.SESSIONS}
        self.assertEqual(roles["S1"], ("opus-1", "first", 70))
        self.assertEqual(roles["S3"], ("opus-1", "churn", 50))
        self.assertEqual(hunt_tool.listening_seconds(hunt_tool.SESSIONS[2]), 3 * 50 + 2 * 12)
        # The matrix: the defaults first, then each of the two off, then
        # both; each 120 s of the first audio session and of 4K at 25 and
        # 80 Mbit/s. Then M5, the defaults with the class threads not
        # real-time, 4K only, if it fits.
        self.assertEqual([(item["gso"], item["gro"], item.get("realtime", True))
                          for item in hunt_tool.MATRIX],
                         [(True, True, True), (False, True, True), (True, False, True),
                          (False, False, True), (True, True, False)])
        self.assertEqual([item["name"] for item in hunt_tool.MATRIX if item.get("optional")],
                         ["M5"])
        self.assertEqual(hunt_tool.VIDEO_RUNGS, ("2160p25", "2160p80"))
        self.assertEqual(hunt_tool.MATRIX_SECONDS, 120)
        self.assertEqual(hunt_tool.matrix_audio(hunt_tool.MATRIX[2])["gro"], False)
        self.assertEqual(hunt_tool.combination_settings(hunt_tool.MATRIX[3]),
                         ["relay_video_gso=false", "client_udp_gso=false",
                          "relay_video_gro=false", "relay_class_realtime=true"])
        self.assertEqual(hunt_tool.combination_settings(hunt_tool.MATRIX[4]),
                         ["relay_video_gso=true", "client_udp_gso=true",
                          "relay_video_gro=true", "relay_class_realtime=false"])
        # At worst the sessions end before the matrix, the matrix before
        # collect, and down comes inside the second billed hour and before
        # the watchdog's deadline less the latest start.
        self.assertLess(hunt_tool.sessions_end_minute(), at[matrix[0]])
        self.assertLess(hunt_tool.matrix_end_minute(), at["collect"])
        self.assertLess(at["down"], 120 - 10)
        self.assertLess(at["down"] * 60, hunt_tool.WATCHDOG_HOURS * 3600
                        - hunt_tool.LATEST_START_BEFORE_DEADLINE_S)
        self.assertLessEqual(hunt_tool.WATCHDOG_HOURS, 2.0)
        # M5 runs only when it, collect and down end inside the second
        # billed hour and before the watchdog's bounded collect; at worst
        # (the matrix ending at its worst after an `up` of 15 min) it is
        # left out, and it never adds a billed hour.
        m5 = hunt_tool.MATRIX[4]
        self.assertLessEqual(hunt_tool.OPTIONAL_END_MINUTE,
                             min(120 - 5, hunt_tool.WATCHDOG_HOURS * 60
                                 - hunt_tool.COLLECT_TIMEOUT_S / 60))
        latest = hunt_tool.optional_latest_start_minute(m5)
        self.assertTrue(hunt_tool.optional_fits(m5, latest * 60))
        self.assertFalse(hunt_tool.optional_fits(m5, latest * 60 + 1))
        self.assertFalse(hunt_tool.optional_fits(m5, (hunt_tool.matrix_end_minute() + 15) * 60))
        self.assertTrue(hunt_tool.optional_fits(m5, (hunt_tool.matrix_end_minute() - 2) * 60))
        self.assertLess(hunt_tool.combination_seconds(m5), hunt_tool.combination_seconds(
            hunt_tool.MATRIX[0]))

    def test_the_arms_and_the_per_node_settings(self):
        topology = hunt_tool.load_topology()
        hunt = topology["hunt"]
        edges = sorted(node["name"] for node in topology["nodes"] if node["role"] == "edge")
        self.assertEqual(edges, ["lon-edge-1", "lon-edge-2", "lon-edge-3", "lon-edge-4",
                                 "new-edge-1", "tok-edge-1", "tok-edge-2"])
        # New York's forwarders and edge (the 4K run's stops), and a view
        # host there.
        sites = {node["name"]: node["site"] for node in topology["nodes"]}
        self.assertEqual(sorted(name for name, site in sites.items() if site == "newyork"),
                         ["new-bare-1", "new-dist-a", "new-dist-b", "new-edge-1", "new-view"])
        self.assertIn("new-view", hunt["listeners"])
        # Half the twins touched, one of each pair (New York's one edge
        # is no pair).
        self.assertEqual(len(hunt["touched"]), (len(edges) - 1) // 2)
        for pair in (("lon-edge-1", "lon-edge-2"), ("lon-edge-3", "lon-edge-4"),
                     ("tok-edge-1", "tok-edge-2")):
            self.assertEqual(len(set(pair) & set(hunt["touched"])), 1, pair)
        overrides = hunt_tool.node_overrides(topology)
        large = ["--hop-journal-capacity", str(hunt_tool.HOP_JOURNAL_LARGE)]
        for name in ("lon-edge-1", "lon-edge-2"):
            self.assertEqual(overrides[name]["extra_edge_args"], large)
        for name in ("lon-dist-a", "lon-dist-b"):
            self.assertEqual(overrides[name]["extra_distributor_args"], large)
        # One region's edges without the arena warm-up, the other's with it.
        for name in ("tok-edge-1", "tok-edge-2"):
            self.assertEqual(overrides[name]["extra_edge_args"], [hunt_tool.ARENA_FLAG, "0"])
        self.assertFalse(any(hunt_tool.ARENA_FLAG in json.dumps(overrides.get(name, {}))
                             for name in edges if name.startswith("lon-")))
        self.assertEqual(sorted(overrides), ["lon-dist-a", "lon-dist-b", "lon-edge-1",
                                             "lon-edge-2", "tok-edge-1", "tok-edge-2"])
        settings = hunt_tool.push_settings(topology)
        self.assertIn("stall_journal_capacity=65536", settings)


class DryRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.printed, cls.out = dry_run()
        cls.addClassCleanup(shutil.rmtree, cls.out.parent, True)
        cls.lines = cls.printed.splitlines()

    def commands(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle in line]

    def test_a_dry_run_prints_every_command(self):
        up = self.commands("linode-small-mesh-lab.py up ")
        self.assertEqual(len(up), 1)
        for flag in ("--watchdog-hours 1.95", "--collect-timeout-s 240",
                     "--optional-sites sydney", "--budget-usd 300",
                     "--run-id hunttest", "NEEDLETAIL_LINODE_MAX_HOURLY_USD=1.60",
                     "NEEDLETAIL_LINODE_MAX_NODES=26"):
            self.assertIn(flag, up[0])
        # The lab is created, changed and deleted by the lab tool alone:
        # hosts first, after the replacement, last.
        self.assertEqual(len(self.commands("linode-small-mesh-lab.py hosts")), 3)
        self.assertEqual(len(self.commands("linode-small-mesh-lab.py remove ")), 1)
        self.assertEqual(len(self.commands("linode-small-mesh-lab.py grow ")), 1)
        self.assertEqual(len(self.commands("linode-small-mesh-lab.py down")), 1)
        topology = hunt_tool.load_topology()
        names = [node["name"] for node in topology["nodes"]]
        mesh = [node["name"] for node in topology["nodes"]
                if node["role"] in hunt_tool.MESH_ROLES]
        watch = self.commands("needletail-nodecheck watch --out")
        for name in names:
            self.assertTrue(any(line.startswith(f"ssh {name} ") for line in watch), name)
        push = self.commands("linode-small-mesh-deploy.py push --remeasure")
        self.assertEqual(len(push), 1)
        self.assertIn("node_overrides=", push[0])
        self.assertIn(hunt_tool.ARENA_FLAG, push[0])
        self.assertEqual(len(self.commands("linode-small-mesh-deploy.py push --nodes")), 1)
        # Each combination of the matrix: a push of its settings (every
        # mesh node activated: new processes).
        applied = self.commands("linode-small-mesh-deploy.py push --skip-bootstrap "
                                "--skip-binaries")
        self.assertEqual(len(applied), 5)
        for line, combination in zip(applied, hunt_tool.MATRIX):
            for setting in hunt_tool.combination_settings(combination):
                self.assertIn(f"--set {setting}", line)
        self.assertFalse(self.commands("restart --roles"))
        self.assertIn("--set relay_class_realtime=false", applied[4])
        self.assertEqual(len(self.commands("runs only if it starts by minute")), 1)
        # Each push restarts every mesh node: the processes' ages after it.
        self.assertEqual(len(self.commands("the age of each mesh process")), 5)
        # The referee's samplers on every node they take (the mesh, the
        # source and view hosts; no bare node), and on the replaced edge;
        # collected from the replaced edge and from all at collect. Beside
        # them on the same nodes the hunt's extra sampler (what they do not
        # take: tc, Udp errors, kernel stacks on a late heartbeat, with
        # kernel.watchdog_thresh=1), and journalctl -k at collect.
        started = self.commands("start_samplers on ")
        self.assertEqual(len(started), 2)
        sampled = [node["name"] for node in topology["nodes"]
                   if node["role"] in hunt_tool.REFEREE_SAMPLED_ROLES]
        self.assertEqual(started[0].split("start_samplers on ")[1].split(" (")[0].split(", "),
                         sorted(sampled))
        self.assertTrue(set(mesh) <= set(sampled))
        self.assertFalse(any(name.endswith("-bare-1") for name in sampled))
        self.assertIn("compare-run.py", started[0])
        self.assertEqual(len(self.commands("collect_samplers on ")), 2)
        self.assertFalse(self.commands("sampler.sh"))
        extra = self.commands("the extra sampler of the hunt")
        self.assertEqual(len(extra), len(sampled) + 1)
        self.assertEqual(sorted({line.split()[1] for line in extra
                                 if not line.split()[1].startswith("<")}), sorted(sampled))
        self.assertTrue(all("kernel.watchdog_thresh=1" in line for line in extra))
        self.assertEqual(len(self.commands("journalctl -k >kernel.log")), len(names) + 1)
        self.assertEqual(len(self.commands("linode-route-survey.py start")), 1)
        touched = topology["hunt"]["touched"]
        touch = self.commands("needletail-nodecheck touch --mb $MB")
        self.assertEqual(sorted(line.split()[1] for line in touch), sorted(touched))
        self.assertTrue(all("--hold" in line for line in touch))
        release = self.commands('echo "released')
        self.assertEqual(sorted(line.split()[1] for line in release), sorted(touched))
        # S1 to S3, each combination's audio session and its two rungs.
        self.assertEqual(len(self.commands("systemctl restart needletail-contrib")),
                         3 + 4 * 3 + 2)
        senders = self.commands("aep1-48k-probe send")
        self.assertEqual(len(senders), 3 + 4)
        self.assertTrue(all("--lateness-journal" in line for line in senders))
        views = len(topology["hunt"]["listeners"])
        self.assertEqual(len(self.commands("cat >run.sh")), (3 + 4 + 5 * 2) * views)
        # 4K: the referee's clip over RTMP into the contributor, a probe a
        # view host through its site's entry, at each rung of each
        # combination.
        feeds = self.commands("ntcmp flv --clip")
        self.assertEqual(len(feeds), 5 * 2)
        self.assertTrue(all("rtmp://10.78.1.5:19350/live/needletail-lab" in line
                            for line in feeds))
        self.assertTrue(all("pkill -f" not in line for line in feeds))
        self.assertEqual(sum("2160p80" in line for line in feeds), 5)
        probes = self.commands("/live/1/master.m3u8")
        self.assertEqual(len(probes), 5 * 2 * views)
        self.assertTrue(any("<public:new-dist-a>" in line for line in probes))
        # Receiver GRO off: the UDP probes of M3 and M4 without it.
        self.assertEqual(len(self.commands("--gro false")), 2 * 8)
        # Once, after the first session: London's four large rings.
        hops = self.commands("/api/hops")
        self.assertEqual(sorted(line.split("https://<public:")[1].split(">")[0] for line in hops),
                         sorted(topology["hunt"]["hop_journal_large"]))
        self.assertTrue(all("/api/hops?since_us=<session start>&stream=3001" in line
                            for line in hops))
        for word in ("create hunttest", "add-tree hunttest", "collected hunttest",
                     "finish hunttest"):
            self.assertEqual(len(self.commands(f"run_archive.py --tool stall-hunt {word}")), 1,
                             word)
        self.assertTrue(any("tar -C /var/lib/needletail-runs/hunt" in line
                            for line in self.lines))
        # The first command on the new instances waits for their ssh: at
        # minute 0 and after the replacement.
        self.assertEqual(len(self.commands("# until ssh answers on")), 2)
        # The watches stop before their files are fetched: the replaced
        # edge's, then every node's at collect.
        self.assertEqual(len(self.commands("'stop the watch'")), 1 + len(names))
        # Nothing is polled from here: the nodes sample themselves.
        self.assertFalse(self.commands("GET /api/stalls"))
        self.assertFalse(self.commands("echo-send"))
        self.assertFalse(self.commands("needletail-nodecheck warm"))

    def test_the_sessions_are_the_referee_s_with_clients_at_every_edge(self):
        text = self.printed
        self.assertIn("--stream 3001", text)
        self.assertIn("--stream 4001", text)
        self.assertIn("ntcmp udp-ingest", text)
        self.assertIn("nt-popus-48s-la.index.json", text)
        self.assertIn("nt-pcm-48s.index.json", text)
        self.assertIn("nt-popus-120s-la.index.json", text)
        self.assertIn("--listeners 40", text)
        self.assertIn("--listeners 20", text)
        first = text[text.index("# minute 20: S1"):text.index("# minute 24: S2")]
        for edge in ("lon-edge-1", "lon-edge-2", "lon-edge-3", "lon-edge-4", "new-edge-1",
                     "tok-edge-1", "tok-edge-2"):
            # An LL-HLS probe at the edge itself, and the crowd over it.
            self.assertIn(f"https://<public:{edge}>:19444/live/3001/stream.m3u8", first)
            self.assertIn(f"<public:{edge}>:19446", first)
        crowds = [line for line in first.splitlines() if "needletail-load --edges" in line]
        self.assertEqual(len(crowds), 3)
        self.assertIn("<public:lon-edge-1>:19446,<public:lon-edge-2>:19446,"
                      "<public:lon-edge-3>:19446,<public:lon-edge-4>:19446", crowds[0])
        # No entry point for the crowd: listener i starts at edge i mod n,
        # so that every edge gets UDP clients (an assignment would put them
        # on the edges that hold the playlist already).
        for crowd in crowds:
            self.assertNotIn("--entry", crowd)
        churn = text[text.index("# minute 28: S3"):text.index("# minute 35")]
        for index in (1, 2, 3):
            self.assertIn(f"r{index}-uc-1", churn)
            self.assertIn(f"r{index}-hls-lon-edge-1-1", churn)
            self.assertIn(f'echo "{index} $(date +%s.%N)" >>rounds.txt', churn)
        self.assertEqual(churn.count(f"sleep {hunt_tool.CHURN_GAP_S}"),
                         2 * len(hunt_tool.load_topology()["hunt"]["listeners"]))

    def test_nothing_is_written_and_no_token_is_named(self):
        self.assertFalse(self.out.exists())
        for word in ("LINODE_TOKEN", "LINODE_PAT", "api.linode.com", "linode-cli"):
            self.assertNotIn(word, self.printed)

    def test_the_run_archive_gets_the_five_heads_and_the_deploy_tool_s_collect(self):
        create = self.commands("run_archive.py --tool stall-hunt create hunttest")[0]
        self.assertEqual(create.count("--code "), 5)
        for repo in ("av-mesh", "av-contrib", "playlists", "daw-nexus"):
            self.assertIn(f"/{repo}", create)
        collect = self.commands("linode-small-mesh-deploy.py collect --run-id hunttest")
        self.assertEqual(len(collect), 1)
        order = [index for index, line in enumerate(self.lines)
                 if "deploy.py collect --run-id" in line or "stall-hunt collected" in line
                 or "linode-small-mesh-lab.py down" in line]
        self.assertEqual(len(order), 3)
        self.assertIn("collect --run-id", self.lines[order[0]])
        self.assertIn("collected hunttest", self.lines[order[1]])
        self.assertIn("lab.py down", self.lines[order[2]])

    def test_collect_and_down_come_last(self):
        marks = [line for line in self.lines if line.startswith("# minute")]
        self.assertTrue(marks[-2].endswith("collect"))
        self.assertTrue(marks[-1].endswith("down"))


class Collected(unittest.TestCase):
    """The run is collected when the deploy tool's collect and the hunt's
    own records both are: what the deploy's collect recorded missing is
    kept in the hunt's mark."""

    def setUp(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, True))
        patcher = mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": str(directory / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hunt = hunt_tool.Hunt(hunt_tool.Runner(False, directory / "run"),
                                   hunt_tool.load_topology(), TOPOLOGY, "hunt1")

    def test_the_deploy_collect_s_missing_is_read_from_the_run(self):
        run = hunt_tool.run_archive.RunArchive.create("hunt1", "linode-lab", tool="stall-hunt",
                                                      argv=["hunt"])
        began = hunt_tool.run_archive.utc_now()
        run.mark_collected(False, ["tok-edge-2", "lon-dist-a/api-stalls"])
        self.assertEqual(self.hunt.deploy_missing(1, began),
                         ["tok-edge-2", "lon-dist-a/api-stalls"])
        run.mark_collected(True, [])
        self.assertEqual(self.hunt.deploy_missing(0, began), [])

    def test_a_refused_deploy_collect_is_itself_missing(self):
        began = hunt_tool.run_archive.utc_now()
        self.assertEqual(self.hunt.deploy_missing(2, began), ["deploy collect (exit 2)"])
        self.assertEqual(self.hunt.deploy_missing(70, began), ["deploy collect (exit 70)"])
        self.assertTrue(self.hunt.deploy_missing(0, began)[0].startswith("deploy collect ("),
                        "no run in the archive")

    def test_a_collect_that_crashed_before_marking_the_run_is_missing(self):
        # A Python crash, or a sys.exit with a message, exits 1 like an
        # incomplete collect, but leaves the run as it was: never marked,
        # or marked by an earlier collect.
        run = hunt_tool.run_archive.RunArchive.create("hunt1", "linode-lab", tool="stall-hunt",
                                                      argv=["hunt"])
        began = hunt_tool.run_archive.utc_now()
        self.assertEqual(self.hunt.deploy_missing(1, began),
                         ["deploy collect (exit 1, the run not marked by it)"])
        run.mark_collected(True, [])
        later = "9999-01-01T00:00:00.000Z"
        self.assertEqual(self.hunt.deploy_missing(1, later),
                         ["deploy collect (exit 1, the run not marked by it)"])


class Preflight(unittest.TestCase):
    def test_a_real_run_refuses_without_the_lab_tool_s_watchdog_and_budget(self):
        topology = hunt_tool.load_topology()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(hunt_tool, "STATE", pathlib.Path(directory) / "hunt-lab.json"), \
                mock.patch.object(hunt_tool, "help_text", lambda _command: "usage: up --run-id"):
            problems = hunt_tool.preflight(topology)
        text = "\n".join(problems)
        self.assertIn("--watchdog-hours", text)
        self.assertIn("--budget-usd", text)

    def test_a_real_run_refuses_a_watchdog_that_does_not_collect(self):
        """The lab tool's `up` must bound the watchdog's collect, and the
        deploy tool must have the `collect --run-id` it runs."""
        topology = hunt_tool.load_topology()

        def helps(up: str, collect: str):
            def text(command):
                return up if "up" in command else collect if "collect" in command else ""
            return text

        without = "up --watchdog-hours --budget-usd --optional-sites --run-id"
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(hunt_tool, "STATE", pathlib.Path(directory) / "hunt-lab.json"):
            with mock.patch.object(hunt_tool, "help_text", helps(without, "usage: deploy")):
                refused = hunt_tool.preflight(topology)
            with mock.patch.object(hunt_tool, "help_text",
                                   helps(without + " --collect-timeout-s", "collect --run-id")):
                passed = hunt_tool.preflight(topology)
        self.assertTrue(any("no --collect-timeout-s" in line for line in refused), refused)
        self.assertTrue(any("no `collect --run-id`" in line for line in refused), refused)
        self.assertFalse([line for line in passed if "collect" in line], passed)

    def help_of(self, directory: str, text: str) -> list[str]:
        binary = pathlib.Path(directory)
        for name in ("needletail-nodecheck", "av-mesh"):
            (binary / name).write_text("")
        with mock.patch.object(hunt_tool, "STATE", binary / "hunt-lab.json"), \
                mock.patch.object(hunt_tool, "mesh_binary", lambda name: binary / name), \
                mock.patch.object(hunt_tool, "help_text", lambda _command: text):
            return hunt_tool.preflight(hunt_tool.load_topology())

    def test_a_real_run_refuses_a_touch_that_does_not_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            problems = self.help_of(directory, "usage: touch --mb --rate-mb-s --thp --out")
        self.assertTrue(any("touch has no --hold" in line for line in problems), problems)

    def test_a_real_run_refuses_an_av_mesh_without_the_arena_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            problems = self.help_of(directory, "--stall-journal-capacity --hop-journal-capacity "
                                               "--hold")
        named = [line for line in problems if hunt_tool.ARENA_FLAG in line]
        self.assertEqual(len(named), 1, problems)
        self.assertIn("no_arena_warm", named[0])
        with tempfile.TemporaryDirectory() as directory:
            problems = self.help_of(directory, "--stall-journal-capacity --hop-journal-capacity "
                                               f"--hold {hunt_tool.ARENA_FLAG}")
        self.assertFalse([line for line in problems if "av-mesh has no" in line
                          or "touch has no" in line], problems)

    def test_the_default_run_id_is_new_each_run_of_a_day(self):
        self.assertRegex(hunt_tool.default_run_id(), r"^hunt\d{8}$")

    def test_the_run_stops_before_anything_is_billed(self):
        args = mock.Mock(topology=str(TOPOLOGY), dry=False, run_id="hunttest", budget_usd=300,
                         spent_before_usd=None)
        with mock.patch.object(hunt_tool, "preflight", lambda _topology: ["a problem"]), \
                mock.patch.object(hunt_tool.Runner, "tool",
                                  side_effect=AssertionError("a tool was called")):
            with self.assertRaises(SystemExit) as stopped:
                hunt_tool.command_run(args)
        self.assertIn("nothing was created", str(stopped.exception))


def heartbeat_text(lates_ms: list[float], at_s: float = 1.0) -> str:
    """The referee's heartbeat logs of a 2-vCPU node (cat of both CPUs'):
    a line a second on each vCPU, CPU 0's wakes from `at_s` on late by
    `lates_ms` (a wake of 5 ms late or less is no line but the second's)."""
    lines = []
    for index, late in enumerate(lates_ms):
        realtime = int((at_s + index) * 1e9)
        lines.append(f"HB 0 {realtime} {realtime} {realtime} {int(late * 1e6)} fifo99")
        lines.append(f"HB 1 {realtime} {realtime} {realtime} 0 fifo99")
    return "\n".join(lines)


def watch_text(stops_ms: list[float], at_us: int = 2_000_000) -> str:
    """A watch file of a 2-CPU node with stopped ticks of CPU 0 at `at_us`."""
    lines = [{"kind": "sec", "cpu": cpu, "t_us": 1_000_000, "span_us": 1_000_000}
             for cpu in (0, 1)]
    lines += [{"kind": "tick", "thread": "tick-0", "cpu": 0, "t_us": at_us + i,
               "dur_us": int(ms * 1000), "span_us": int(ms * 1000), "n": 1,
               "run_delay_us": 10} for i, ms in enumerate(stops_ms)]
    return "\n".join(json.dumps(line) for line in lines)


class Replacement(unittest.TestCase):
    def hunt(self):
        topology = hunt_tool.load_topology()
        out = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        hunt = hunt_tool.Hunt(hunt_tool.Runner(False, out), topology, TOPOLOGY, "hunttest")
        # The sessions so far: the first 10 s of the test's clock; the
        # touched arm's touch long before them.
        hunt.record["periods"] = [
            {"name": "touch", "kind": "touch", "from_unix": -100.0, "to_unix": -90.0},
            {"name": "S1", "kind": "session", "from_unix": 0.0, "to_unix": 10.0}]
        return hunt

    def test_the_edge_not_run_most_is_chosen(self):
        hunt = self.hunt()
        watch = watch_text

        records = {name: (watch([]), heartbeat_text([1.0] * 5)) for name in hunt.nodes}
        records["lon-edge-1"] = (watch([60.0]), heartbeat_text([1.0] * 5))
        # Its vCPU's heartbeat woke 300 ms late once (30 ms wakes do not
        # count).
        records["tok-edge-2"] = (watch([]), heartbeat_text([1.0, 300.0, 30.0, 30.0]))
        records["lon-dist-a"] = (watch([900.0]), heartbeat_text([1.0]))  # not an edge

        def ssh(name, command, **_kwargs):
            return records[name][0] if "watch.ndjson" in command else records[name][1]

        with mock.patch.object(hunt.runner, "ssh", ssh):
            worst, figures = hunt.worst()
        self.assertEqual(worst, "tok-edge-2")
        self.assertAlmostEqual(figures["tok-edge-2"], 300.0)
        self.assertAlmostEqual(figures["lon-edge-1"], 30.0)
        self.assertNotIn("lon-dist-a", figures)
        quiet = {name: (watch([]), heartbeat_text([1.0] * 3)) for name in hunt.nodes}
        with mock.patch.object(hunt.runner, "ssh",
                               lambda name, command, **_k: quiet[name][0]
                               if "watch.ndjson" in command else quiet[name][1]):
            self.assertIsNone(hunt.worst()[0])

    def test_a_stop_outside_the_sessions_does_not_choose_the_edge(self):
        """A touched edge stopped 400 ms during its touch (minutes before
        the first session) and not in the sessions is not chosen over an
        edge whose vCPU was not run 250 ms in a session (its heartbeat)."""
        hunt = self.hunt()
        records = {name: (watch_text([]), heartbeat_text([1.0] * 5)) for name in hunt.nodes}
        records["lon-edge-3"] = (watch_text([400.0, 400.0], at_us=-95_000_000),
                                 heartbeat_text([400.0, 400.0], at_s=-95.0))
        records["lon-edge-2"] = (watch_text([]), heartbeat_text([1.0, 250.0, 1.0]))

        def ssh(name, command, **_kwargs):
            return records[name][0] if "watch.ndjson" in command else records[name][1]

        with mock.patch.object(hunt.runner, "ssh", ssh):
            worst, figures = hunt.worst()
        self.assertEqual(worst, "lon-edge-2")
        self.assertEqual(figures["lon-edge-3"], 0.0)


# A stand-in for needletail-nodecheck touch: it writes its arguments, when
# it started and ended to lifetimes.txt, and touch_done to its --out; with
# --hold it ends only on SIGTERM.
FAKE_TOUCH = """
import signal, sys, time
args = sys.argv[1:]
out = args[args.index("--out") + 1]
def line(word):
    with open("lifetimes.txt", "a") as handle:
        handle.write(f"{word} {time.time():.6f} {' '.join(args)}\\n")
line("start")
with open(out, "w") as handle:
    handle.write('{"kind":"touch_done"}\\n')
def ended(*_):
    line("end")
    sys.exit(0)
signal.signal(signal.SIGTERM, ended)
if "--hold" in args:
    while True:
        signal.pause()
line("end")
"""


class TouchedArm(unittest.TestCase):
    def test_the_guest_s_free_memory_is_held_until_it_is_given_back(self):
        """The touch takes MemFree (not MemAvailable, which counts the page
        cache) less a margin of at least the reserve and above the zones'
        watermarks, and holds it; the release ends it and marks that it had
        finished. Run under sh with a fake nodecheck, in a directory of its
        own."""
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            fake = folder / "nodecheck"
            fake.write_text(f"#!{sys.executable}\n{FAKE_TOUCH}")
            fake.chmod(0o755)

            def local(command: str) -> str:
                return command.replace(hunt_tool.REMOTE, str(folder)).replace(
                    hunt_tool.NODECHECK, str(fake))

            free = int(next(line.split()[1] for line in
                            pathlib.Path("/proc/meminfo").read_text().splitlines()
                            if line.startswith("MemFree:"))) // 1024
            high_pages = sum(int(line.split()[1]) for line in
                             pathlib.Path("/proc/zoneinfo").read_text().splitlines()
                             if line.split()[:1] == ["high"])
            subprocess.run(["sh", "-c", local(hunt_tool.touch_command())], check=True, timeout=30)
            for _ in range(100):
                if (folder / "touch-guest.ndjson").is_file():
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            held = (folder / "lifetimes.txt").read_text()
            subprocess.run(["sh", "-c", local(hunt_tool.release_command())], check=True,
                           timeout=60)
            marks = (folder / "touch-marks.txt").read_text().split("\n")
            lives = (folder / "lifetimes.txt").read_text().splitlines()
        self.assertNotIn("end", held)
        self.assertEqual(len(lives), 2)
        args = lives[0].split()[2:]
        megabytes = int(args[args.index("--mb") + 1])
        _word, _unix, marked, free_then, leave = marks[0].split()
        self.assertEqual(int(marked), megabytes)
        self.assertEqual(megabytes, int(free_then) - int(leave))
        self.assertEqual(int(leave), max(hunt_tool.TOUCH_LEAVE_MB, int(2 * high_pages * 4 / 1024)))
        # MemFree as the command read it (this machine's moves while it runs).
        self.assertLessEqual(abs(int(free_then) - free), 1024)
        self.assertIn("--hold", args)
        self.assertEqual(args[args.index("--rate-mb-s") + 1], str(hunt_tool.TOUCH_RATE_MB_S))
        self.assertTrue(lives[1].startswith("end "))
        self.assertTrue(marks[0].startswith("touch "))
        released = marks[1].split()
        self.assertEqual((released[0], released[2]), ("released", "finished"))
        self.assertLessEqual(float(lives[1].split()[1]), float(released[1]))


class FakeService(http.server.BaseHTTPRequestHandler):
    """/api/mesh/local, /api/stalls and /api/pacing of a fake service; the
    stall journal's since_us asked for is kept."""
    asked: list[str] = []

    def do_GET(self):  # noqa: N802 - the handler's name
        path, _, query = self.path.partition("?")
        if path == "/api/mesh/local":
            # As av-mesh serves it (MeshApiSnapshot): the fan-out's counters
            # under client_udp.fast_lane, the class thread's under
            # relay_session.fast_plane, the node's status (ingest) beside.
            body = {"relay_session": {"fast_plane": {"audio_loop_busy_max_us": 812,
                                                     "audio_thread_minflt": 3,
                                                     "duplicates": 5}},
                    "client_udp": {"subscribers": 4,
                                   "fast_lane": {"fanout_started": 2, "fanout_policies": 1,
                                                 "class_arena_warm_bytes": 33554432}},
                    "status": {"ingest": {"audio_deadline_drops": 0, "tee_dropped": 9}},
                    "stall": {"stopped_ms": 0},
                    # Every peer's telemetry rides on /api/mesh/local too.
                    "relay_nodes": [{"relay_session": {"fast_plane": {
                        "audio_loop_busy_max_us": 99999}}}],
                    "nodes": [{"stall": {"stopped_ms": 7}}]}
            data = json.dumps(body).encode()
        elif path == "/api/stalls":
            FakeService.asked.append(query)
            since = int(query.split("=")[1])
            events = [{"t_us": at, "kind": "tick", "cpu": 0, "dur_us": 3000}
                      for at in (100, 200) if at >= since]
            data = "".join(json.dumps(event) + "\n" for event in events).encode()
        elif path == "/api/pacing":
            data = b'{"rx_to_publish_p50_us": 120}'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


FAKE_REFEREE = """
calls = []
def start_samplers(session):
    calls.append(("start", sorted(session.nodes), str(session.dir)))
    return list(session.nodes.values())
def collect_samplers(session, sampled):
    calls.append(("collect", sorted(session.nodes), [item["name"] for item in sampled]))
"""


class RefereeSamplers(unittest.TestCase):
    """The hunt samples with the referee's start_samplers and
    collect_samplers, its compare-run.py loaded by path."""

    def test_the_hunt_calls_the_referee_s_functions(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        (root / "scripts" / "compare").mkdir(parents=True)
        (root / "scripts" / "compare" / "compare-run.py").write_text(FAKE_REFEREE)
        out = root / "run"
        hunt = hunt_tool.Hunt(hunt_tool.Runner(False, out), hunt_tool.load_topology(), TOPOLOGY,
                              "hunttest")
        path = list(sys.path)
        loaded = []
        real = hunt_tool.referee_samplers

        def load(root_=None):
            loaded.append(real(root_))
            return loaded[-1]

        sent = []
        with mock.patch.dict(os.environ, {"NEEDLETAIL_COMPARE_ROOT": str(root)}), \
                mock.patch.object(hunt_tool, "referee_samplers", load), \
                mock.patch.object(hunt.runner, "ssh",
                                  lambda name, command, **_: sent.append((name, command))):
            hunt.sample()
            hunt.collect_samplers(["lon-edge-1", "lon-bare-3"])
        self.assertEqual(sys.path, path)
        calls = [call for module in loaded for call in module.calls]
        sampled = sorted(node["name"] for node in hunt_tool.load_topology()["nodes"]
                         if node["role"] not in ("bare",))
        self.assertEqual(calls[0], ("start", sampled, str(out)))
        self.assertEqual(calls[1], ("collect", ["lon-edge-1"], ["lon-edge-1"]))
        # The hunt's extra sampler on the same nodes.
        self.assertEqual(sorted(name for name, command in sent
                                if command == hunt_tool.EXTRA_SAMPLER), sampled)
        journal = [json.loads(line) for line in (out / "journal.ndjson").read_text().splitlines()]
        self.assertEqual([item["action"] for item in journal], ["start", "collect"])

    def test_a_referee_without_the_functions_is_refused(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        with self.assertRaises(RuntimeError):
            hunt_tool.referee_samplers(root)
        (root / "scripts" / "compare").mkdir(parents=True)
        (root / "scripts" / "compare" / "compare-run.py").write_text("def start_samplers(s): pass\n")
        with self.assertRaises(RuntimeError):
            hunt_tool.referee_samplers(root)

    def test_the_referee_s_own_file_loads(self):
        root = os.environ.get("NEEDLETAIL_COMPARE_ROOT")
        if not root or not (pathlib.Path(root) / hunt_tool.REFEREE_RUN).is_file():
            self.skipTest("NEEDLETAIL_COMPARE_ROOT does not name the referee's checkout")
        path = list(sys.path)
        module = hunt_tool.referee_samplers(pathlib.Path(root))
        self.assertEqual(sys.path, path)
        self.assertTrue(callable(module.start_samplers) and callable(module.collect_samplers))
        # Its common is this checkout's (the one already loaded).
        self.assertIs(module.common, hunt_tool.common)
        for text in ("compare-heartbeat", "SCHED_FIFO", "softnet_stat"):
            self.assertIn(text, module.SYS_SAMPLER + module.HEARTBEAT + module.HEARTBEAT_PY)
        self.assertIn("ethtool -k", module.NIC_INFO)


class MeshContract(unittest.TestCase):
    """The counters the hunt reads from /api/mesh/local: one list in the
    sampler and in stall_records, each path served by av-mesh, and those
    its tooling contract test names among them."""

    def test_the_sampler_and_the_reader_name_the_same_paths(self):
        text = SAMPLER.read_text()
        listed = json.loads(text.split("MESH_PATHS='", 1)[1].split("'", 1)[0])
        self.assertEqual(listed, list(hunt_tool.stall_records.MESH_PATHS.values()))
        self.assertEqual([path for path in listed if "edge_clients" in path], [])

    def test_av_mesh_serves_every_path(self):
        import av_mesh_contract
        root = av_mesh_contract.root()
        if root is None:
            self.skipTest("NEEDLETAIL_AV_MESH_ROOT is not set")
        served = av_mesh_contract.mesh_local_paths(root)
        paths = [path.replace(".", "/") for path in hunt_tool.stall_records.MESH_PATHS.values()]
        self.assertEqual([path for path in paths if path not in served], [])
        # Every counter av-mesh's contract test names for the hunt is read.
        named = av_mesh_contract.contract(root)["mesh_local"]
        self.assertEqual(sorted(named - set(paths)), [])


class Sampler(unittest.TestCase):
    def test_the_sampler_writes_the_class_thread_s_view_each_second(self):
        """scripts/hunt-sampler.sh for 11 s against a sleeping process of
        this machine (its own /proc) and a fake service: a line a second
        with the thread's schedstat, switches and CPU, vmstat, MemFree,
        every thread, the counters cut from /api/mesh/local at their paths; the
        stall journal read every tenth second from the last event + 1, and
        once more at the end; the slow path's lines."""
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sleeper = subprocess.Popen(["sleep", "60"])
        FakeService.asked = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                subprocess.run(["nice", "-n", "19", "sh", str(SAMPLER), directory,
                                str(sleeper.pid), "sleep",
                                f"http://127.0.0.1:{server.server_address[1]}", "/api/pacing",
                                "11"], check=True, timeout=60)
                folder = pathlib.Path(directory)
                lines = [json.loads(line) for line in
                         (folder / "sampler.ndjson").read_text().splitlines()]
                stalls = [json.loads(line) for line in
                          (folder / "stalls.ndjson").read_text().splitlines()]
                pacing = [json.loads(line) for line in
                          (folder / "pacing.ndjson").read_text().splitlines()]
        finally:
            sleeper.kill()
            sleeper.wait()
            server.shutdown()
            server.server_close()
        self.assertEqual(len(lines), 11)
        first = lines[0]
        self.assertEqual((first["pid"], first["tid"], first["tid_by"]),
                         (sleeper.pid, sleeper.pid, "lowest"))
        self.assertEqual(len(first["schedstat"]), 3)
        self.assertEqual(len(first["csw"]), 2)
        self.assertIsInstance(first["cpu"], int)
        self.assertIn("pgfault", first["vm"])
        self.assertIn("thp_fault_alloc", first["vm"])
        self.assertGreater(first["memfree_kb"], 0)
        self.assertEqual(first["threads"][0][0], sleeper.pid)
        self.assertEqual(first["threads"][0][4], "sleep")
        self.assertEqual(first["mesh"], {
            "relay_session.fast_plane.audio_loop_busy_max_us": 812,
            "relay_session.fast_plane.audio_thread_minflt": 3,
            "client_udp.fast_lane.fanout_started": 2,
            "client_udp.fast_lane.fanout_policies": 1,
            "client_udp.fast_lane.class_arena_warm_bytes": 33554432,
            "status.ingest.audio_deadline_drops": 0,
            "stall.stopped_ms": 0})
        gaps = [(b["t_us"] - a["t_us"]) / 1e6 for a, b in zip(lines, lines[1:])]
        self.assertTrue(all(0.8 < gap < 1.6 for gap in gaps), gaps)
        # At seconds 0 and 10: everything, then from the last event + 1;
        # and the last pull at the end.
        self.assertEqual(FakeService.asked, ["since_us=0", "since_us=201", "since_us=201"])
        self.assertEqual([event["t_us"] for event in stalls], [100, 200])
        self.assertEqual(len(pacing), 3)
        self.assertEqual(pacing[0]["body"], {"rx_to_publish_p50_us": 120})

    def test_the_sampler_follows_the_first_of_two_threads_of_one_name(self):
        """Two threads whose names /proc cuts to the same 15 characters
        (needletail-fast-audio and -video), neither real-time (the local
        lab): the one started first, the lower tid, is the audio class
        thread. A SIGTERM ends the sampler with one more pull of the stall
        journal (the last seconds are not lost)."""
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        program = (
            "import os, threading, time\n"
            "tids = []\n"
            "def run():\n"
            "    tid = threading.get_native_id()\n"
            "    with open(f'/proc/self/task/{tid}/comm', 'w') as handle:\n"
            "        handle.write('needletail-fast-class')\n"
            "    tids.append(tid)\n"
            "    time.sleep(60)\n"
            "for _ in range(2):\n"
            "    threading.Thread(target=run, daemon=True).start()\n"
            "    while len(tids) < 1 + _:\n"
            "        time.sleep(0.01)\n"
            "print(' '.join(map(str, tids)), flush=True)\n"
            "time.sleep(60)\n")
        named = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, text=True)
        FakeService.asked = []
        try:
            first, second = (int(tid) for tid in named.stdout.readline().split())
            with tempfile.TemporaryDirectory() as directory:
                sampler = subprocess.Popen(
                    ["nice", "-n", "19", "sh", str(SAMPLER), directory, str(named.pid),
                     "needletail-fast", f"http://127.0.0.1:{server.server_address[1]}"])
                folder = pathlib.Path(directory)
                for _ in range(100):
                    if (folder / "stalls.ndjson").is_file():
                        break
                    time.sleep(0.05)
                time.sleep(1.5)
                os.kill(sampler.pid, 15)
                sampler.wait(timeout=30)
                lines = [json.loads(line) for line in
                         (folder / "sampler.ndjson").read_text().splitlines()]
        finally:
            named.kill()
            named.wait()
            named.stdout.close()
            server.shutdown()
            server.server_close()
        self.assertEqual(sampler.returncode, 0)
        self.assertLess(first, second)
        self.assertTrue(lines)
        self.assertTrue(all((line["tid"], line["tid_by"]) == (first, "lowest") for line in lines))
        self.assertEqual(sorted(line[0] for line in lines[0]["threads"]
                                if line[4] == "needletail-fast"), [first, second])
        self.assertEqual(FakeService.asked, ["since_us=0", "since_us=201"])


def live_hunt(directory: str):
    """A hunt in its real mode whose lab, tools and nodes are fakes."""
    topology = hunt_tool.load_topology()
    hunt = hunt_tool.Hunt(hunt_tool.Runner(False, pathlib.Path(directory) / "run"), topology,
                          TOPOLOGY, "hunttest", clock=time.time, sleep=lambda _s: None)
    return hunt


class ReplaceRecovers(unittest.TestCase):
    def test_a_failed_watch_on_the_new_instance_still_pushes_and_samples_it(self):
        with tempfile.TemporaryDirectory() as directory:
            hunt = live_hunt(directory)
            calls = []

            def tool(script, *args, **_kwargs):
                calls.append(args[0])
                return 0, ""

            def watch(_names=None):
                raise RuntimeError("lon-edge-3: ssh: connect to host: Connection refused")

            with mock.patch.object(hunt.runner, "tool", tool), \
                    mock.patch.object(hunt, "stop_recorders", lambda names: None), \
                    mock.patch.object(hunt.runner, "fetch_directory",
                                      side_effect=subprocess.TimeoutExpired("ssh", 900)), \
                    mock.patch.object(hunt, "hosts", lambda _label: None), \
                    mock.patch.object(hunt, "watch", watch), \
                    mock.patch.object(hunt, "sample", lambda names: calls.append("sample")), \
                    mock.patch.object(hunt, "save", lambda: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as failed:
                    hunt.replace("lon-edge-3", not_run_ms=450.0)
        self.assertIn("Connection refused", str(failed.exception))
        # Replaced (its records' timeout did not stop it), pushed and
        # sampled all the same.
        self.assertEqual(calls, ["remove", "grow", "push", "sample"])
        entry = hunt.record["replaced"][0]
        self.assertEqual((entry["name"], entry["not_run_ms_before"]), ("lon-edge-3", 450.0))
        self.assertFalse(entry["watched"])
        self.assertTrue(entry["pushed"])
        self.assertTrue(entry["sampled"])

    def test_a_grow_refused_is_tried_again_and_nothing_is_pushed(self):
        with tempfile.TemporaryDirectory() as directory:
            hunt = live_hunt(directory)
            statuses = iter([0, 1, 1, 1, 1])
            calls = []

            def tool(script, *args, **_kwargs):
                calls.append(args[0])
                return (next(statuses) if args[0] in ("remove", "grow") else 0), ""

            with mock.patch.object(hunt.runner, "tool", tool), \
                    mock.patch.object(hunt, "stop_recorders", lambda names: None), \
                    mock.patch.object(hunt.runner, "fetch_directory", lambda *_a, **_k: None), \
                    mock.patch.object(hunt, "save", lambda: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as failed:
                    hunt.replace("lon-edge-1")
        self.assertIn("in 4 tries", str(failed.exception))
        self.assertEqual(calls, ["remove", "grow", "grow", "grow", "grow"])


class Ssh(unittest.TestCase):
    def test_the_first_command_waits_for_ssh_and_names_a_node_that_never_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            hunt = live_hunt(directory)
            tries: dict[str, int] = {}
            now = [0.0]
            hunt.clock = lambda: now[0]

            def sleep(seconds):
                now[0] += seconds

            def answers(name):
                tries[name] = tries.get(name, 0) + 1
                return name == "lon-edge-1" and tries[name] >= 3

            started = []
            hunt.sleep = sleep
            with mock.patch.object(hunt.runner, "answers", answers), \
                    mock.patch.object(hunt, "install_nodecheck", lambda names: None), \
                    mock.patch.object(hunt.runner, "ssh",
                                      lambda name, command, **_k: started.append(name)):
                with self.assertRaises(RuntimeError) as failed:
                    hunt.watch(["lon-edge-1", "lon-edge-2"])
        self.assertEqual(started, ["lon-edge-1"])
        self.assertIn("no watch on lon-edge-2", str(failed.exception))
        self.assertGreaterEqual(now[0], hunt_tool.SSH_WAIT_S)


class Hangup(unittest.TestCase):
    def test_after_a_hangup_the_output_goes_to_the_run_s_file(self):
        """A closed terminal: SIGHUP is taken as SIGTERM (collect and down
        still run), and what this process and its tools write goes to
        after-hangup.log, not to the terminal that is gone."""
        with tempfile.TemporaryDirectory() as directory:
            program = (
                "import importlib.util, pathlib, subprocess, sys\n"
                f"spec = importlib.util.spec_from_file_location('h', {str(HERE / 'linode-stall-hunt.py')!r})\n"
                "tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)\n"
                "try:\n"
                f"    tool.hung_up(pathlib.Path({directory!r}) / 'run')\n"
                "except KeyboardInterrupt:\n"
                "    print('interrupted', flush=True)\n"
                "    subprocess.run(['echo', 'a tool'])\n")
            result = subprocess.run([sys.executable, "-c", program], capture_output=True,
                                    timeout=60)
            log = (pathlib.Path(directory) / "run" / "after-hangup.log").read_text()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(log, "interrupted\na tool\n")


class Tools(unittest.TestCase):
    def test_tools_run_from_the_checkout_s_root(self):
        seen = {}

        def run(command, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(hunt_tool.subprocess, "run", run), \
                contextlib.redirect_stdout(io.StringIO()):
            runner = hunt_tool.Runner(False, pathlib.Path(directory))
            runner.tool(hunt_tool.SURVEY_TOOL, "start", "--out", "target/stall-hunt/x/survey")
        self.assertEqual(seen["cwd"], str(hunt_tool.ROOT))
        self.assertFalse(any("TOKEN" in key or key.endswith("_PAT") for key in seen["env"]
                             if key not in os.environ))


if __name__ == "__main__":
    unittest.main()
