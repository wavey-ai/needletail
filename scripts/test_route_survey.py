#!/usr/bin/env python3
"""Tests of the route survey (scripts/linode-route-survey.py, D-hunt.md
7.6), offline: the record written and read back; the links and ports of
the hunt's topology; the three policies replayed on synthetic records (two
route classes and a step; one class); the chooser against the fixture
that av-mesh's chooser passes too, and the two copies of the fixture byte
for byte when NEEDLETAIL_AV_MESH_ROOT names an av-mesh checkout.
python3 -m unittest scripts/test_route_survey.py"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("route_survey", HERE / "linode-route-survey.py")
survey = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(survey)

FIXTURE = HERE / "tests" / "fixtures" / "flows-chooser.json"
# Where av-mesh keeps its copy of the fixture (D7's flows.rs tests read it).
AV_MESH_FIXTURE = pathlib.Path("tests") / "fixtures" / "flows-chooser.json"
MS = 1_000_000


def synthetic(seconds: int, delay_ns) -> list[tuple[int, int, int, int]]:
    """Records of one link: every probe set of `seconds`, each port's delay
    delay_ns(second, port)."""
    sets_per_second = 1000 // survey.INTERVAL_MS
    records = []
    for index in range(seconds * sets_per_second):
        second = index // sets_per_second
        for port in survey.survey_ports():
            records.append((port, 0, index, delay_ns(second, port)))
    return records


class Record(unittest.TestCase):
    def test_survey_record_round_trip(self):
        records = [(22401, 3, 7, 109_000_000), (24000, 3, 7, 105_250_000),
                   (24001, 3, 8, -12_345)]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "link-3.rec"
            survey.write_records(path, records)
            self.assertEqual(path.stat().st_size, 16 * len(records))
            with open(path, "ab") as handle:
                handle.write(b"\x00" * 9)          # a record cut off by the end of a run
            self.assertEqual(survey.read_records(path), records)
        self.assertEqual(survey.RECORD.size, 16)
        # The first arrival of a probe is kept.
        sets = survey.probe_sets([(22401, 0, 1, 5), (22401, 0, 1, 9), (24000, 0, 1, 4)])
        self.assertEqual(sets, {1: {22401: 5, 24000: 4}})

    def test_the_links_and_ports_of_the_hunt(self):
        topology = json.loads(survey.TOPOLOGY.read_text())
        links = survey.survey_links(topology)
        self.assertEqual(len(links), 11)
        self.assertEqual(sorted({link["sender"] for link in links}),
                         ["ams-bare-1", "lon-bare-3", "lon-bare-4"])
        self.assertNotIn(("ams-bare-1", "ams-bare-1"),
                         {(link["sender"], link["receiver"]) for link in links})
        ports = survey.survey_ports()
        self.assertEqual(len(ports), 66)
        self.assertEqual(ports[:2], [22401, 22402])
        self.assertEqual(ports[2:], list(range(24000, 24064)))
        # A site left out (Sydney had no capacity) leaves its links out.
        present = {node["name"] for node in topology["nodes"]} - {"syd-bare-1"}
        self.assertEqual(len(survey.survey_links(topology, present)), 8)
        addresses = {node["name"]: f"192.0.2.{index}" for index, node in
                     enumerate(topology["nodes"], 1)}
        commands = survey.start_commands(survey.agent_configs(links, addresses, 1, 2))
        started = [(name, "recv" if " recv " in command else "send")
                   for name, command in commands if "setsid" in command]
        modes = [mode for _name, mode in started]
        self.assertEqual(modes, sorted(modes))          # every receiver before any sender
        self.assertTrue(all("python3" in command for _name, command in commands[:len(addresses)]
                            if "agent.py <<" in command))


class Policies(unittest.TestCase):
    def test_two_classes_and_a_step(self):
        # The default port is on the 109 ms class; ports 24000-24015 on a
        # 105 ms class. At 120 s the default port's route steps 4 ms more.
        def delay(second: int, port: int) -> int:
            if 24000 <= port < 24016:
                return 105 * MS
            base = 109 * MS
            return base + 4 * MS if (port == 22401 and second >= 120) else base

        result = survey.analyse_link(synthetic(240, delay), survey.survey_ports(), 22401)
        policies = result["policies"]
        # 105 and 109, and after the step the default port alone at 113.
        self.assertEqual(result["classes"]["max"], 3)
        self.assertEqual(policies["default"]["one_way_ms"]["p50"], 109.0)
        self.assertEqual(result["kept_port"], 24000)
        self.assertEqual(policies["kept"]["excess_ms"]["p99"], 0.0)
        # The extra flow takes the fast class within three reports and keeps it.
        self.assertGreater(result["extra_carried_share"], 0.98)
        self.assertEqual(result["extra_ports"], [24000])
        self.assertGreater(policies["flows"]["near_best_share"], 0.98)
        self.assertEqual(policies["default"]["near_best_share"], 0.0)
        self.assertTrue(result["gain"])
        self.assertTrue(result["chooser_ok"])
        self.assertEqual(policies["default"]["steps"], 1)
        self.assertEqual(policies["flows"]["steps"], 0)
        self.assertEqual(result["route_steps"], 1)
        self.assertEqual(result["steps_of_every_port_together"], 0)

    def test_one_class_no_extra_and_no_gain(self):
        result = survey.analyse_link(synthetic(120, lambda _second, _port: 109 * MS),
                                     survey.survey_ports(), 22401)
        self.assertEqual(result["classes"]["max"], 1)
        self.assertEqual(result["extra_carried_share"], 0.0)
        self.assertFalse(result["gain"])
        self.assertTrue(result["chooser_ok"])

    def test_the_verdict_over_links_and_a_stopped_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            start_ns = 1_795_380_000 * 1_000_000_000
            record = {"start_unix_ns": start_ns, "interval_ms": survey.INTERVAL_MS,
                      "ports": survey.survey_ports(), "default_ports": [22401, 22402],
                      "links": [{"id": 0, "sender": "lon-bare-3", "receiver": "tok-bare-1"},
                                {"id": 1, "sender": "lon-bare-3", "receiver": "syd-bare-1"}]}
            (root / "survey.json").write_text(json.dumps(record))
            survey.write_records(root / "link-0.rec", synthetic(
                60, lambda _second, port: (105 if 24000 <= port < 24008 else 109) * MS))
            survey.write_records(root / "link-1.rec", synthetic(
                60, lambda _second, _port: 125 * MS))
            # tok-bare-1 was not run for 300 ms at 20 s into the survey.
            nodes = root / "nodes" / "tok-bare-1"
            nodes.mkdir(parents=True)
            stop_end_us = start_ns // 1000 + 20_300_000
            (nodes / "watch.ndjson").write_text(json.dumps(
                {"kind": "tick", "thread": "tick-0", "cpu": 0, "t_us": stop_end_us,
                 "dur_us": 300_000, "span_us": 300_000, "n": 1, "run_delay_us": 5,
                 "cause": "stopped", "scope": "all"}) + "\n")
            result = survey.analyse(root, root / "nodes")
        tokyo = result["links"]["lon-bare-3->tok-bare-1"]
        sydney = result["links"]["lon-bare-3->syd-bare-1"]
        self.assertEqual(tokyo["left_out_seconds"], 3)      # 19 to 21: the stop and a second each side
        self.assertEqual(sydney["left_out_seconds"], 0)
        self.assertTrue(tokyo["gain"])
        self.assertFalse(sydney["gain"])
        self.assertTrue(result["flows_stay_on"])
        self.assertEqual(result["gain_links"], ["lon-bare-3->tok-bare-1"])
        text = survey.format_report(result)
        self.assertIn("flows stay on by default", text)


class Chooser(unittest.TestCase):
    def test_the_chooser_passes_the_fixture(self):
        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(fixture["settings"], {"class_us": survey.CLASS_US,
                                               "keep_us": survey.KEEP_US,
                                               "hold_reports": survey.HOLD_REPORTS,
                                               "idle_s": survey.IDLE_S})
        self.assertGreaterEqual(len(fixture["cases"]), 10)
        for case in fixture["cases"]:
            got = survey.run_fixture(case, fixture["settings"])
            want = [{"extra": report["extra"], "state": report["state"]}
                    for report in case["reports"]]
            self.assertEqual(got, want, case["name"])

    def test_the_fixture_is_av_mesh_s_byte_for_byte(self):
        root = os.environ.get("NEEDLETAIL_AV_MESH_ROOT")
        if not root:
            self.skipTest("NEEDLETAIL_AV_MESH_ROOT is not set")
        other = pathlib.Path(root) / AV_MESH_FIXTURE
        if not other.is_file():
            self.skipTest(f"{other} does not exist (av-mesh without D7's flows)")
        self.assertEqual(other.read_bytes(), FIXTURE.read_bytes())


if __name__ == "__main__":
    unittest.main()
