"""compare-run.py and crowd.py against a topology's run plan (topo4k, 27 Nov):
each host's entry, the LL-HLS reader's place in a lean step, the deployed
settings a cell expects, the video sender's start, and the crowd's load sides."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = load("compare_run_plan", "compare-run.py")
crowd = load("crowd_plan", "crowd.py")

PLAN = {"udp_viewer_entries": {"london": ["lon-dist-a", "lon-dist-b"], "newyork": ["new-dist-a", "new-dist-b"],
                               "tokyo": ["tok-dist-a", "tok-dist-b"], "sydney": ["syd-dist-a", "syd-dist-b"]}}


def nodes() -> dict:
    out = {}
    for site in ("lon", "new", "tok", "syd"):
        for name in ("dist-a", "dist-b"):
            out[f"{site}-{name}"] = {"role": "distributor", "public_ipv4": f"10.{len(out)}.0.1"}
        for name in ("edge-1", "edge-2"):
            out[f"{site}-{name}"] = {"role": "edge", "public_ipv4": f"10.{len(out)}.0.1"}
    for i in range(1, 5):
        out[f"lon-lh-{i}"] = {"role": "distributor", "long_haul": True, "public_ipv4": f"10.9{i}.0.1"}
    return out


def session(plan: dict | None) -> object:
    s = run.Session.__new__(run.Session)
    s.name = "plan-test"
    s.nodes, s.run_plan, s.topology_file = nodes(), plan or {}, "topology-4k-lk2.json" if plan else None
    s.entries = s.viewer_entries()
    return s


class Entries(unittest.TestCase):
    def test_each_host_asks_its_own_entry_from_the_plan(self):
        s = session(PLAN)
        self.assertEqual(s.entries["lon"], ["lon-dist-a", "lon-dist-b"])
        self.assertEqual(s.entries["syd"], ["syd-dist-a", "syd-dist-b"])
        lane = run.Lane(s, "nt")
        view = lane.probe("tok", "2160p240", 60, "probe.uc", 1, True, "x-nt-tok-view", "uc")
        nano = lane.probe("tok", "2160p240", 60, "probe.uc", 1, True, "x-nt-tok-nano", "uc")
        self.assertIn(f"--entry https://{s.nodes['tok-dist-a']['public_ipv4']}:19444", view)
        self.assertIn(f"--entry https://{s.nodes['tok-dist-b']['public_ipv4']}:19444", nano)
        self.assertIn(s.nodes["new-dist-b"]["public_ipv4"], lane.audio_hls("new", 60, "x-nt-new-nano"))
        self.assertIn(s.nodes["new-dist-a"]["public_ipv4"], lane.audio_uc("new", 60, "x-nt-new-view"))

    def test_without_a_plan_both_hosts_ask_dist_a_as_before(self):
        s = session(None)
        self.assertEqual(s.entries["lon"], ["lon-dist-a", "lon-dist-a"])

    def test_a_relay_or_a_missing_pair_is_refused(self):
        with self.assertRaises(SystemExit):
            session({"udp_viewer_entries": dict(PLAN["udp_viewer_entries"], london=["lon-dist-a", "lon-lh-1"])})
        with self.assertRaises(SystemExit):
            session({"udp_viewer_entries": {k: v for k, v in PLAN["udp_viewer_entries"].items()
                                            if k != "tokyo"}})

    def test_a_fake_session_falls_back_to_dist_a(self):
        self.assertEqual(run.entry_node(object(), "lon", 1), "lon-dist-a")


class Reader(unittest.TestCase):
    def test_the_lean_readers_place(self):
        s = session(PLAN)
        s.hls_reader_sites, s.nano_client = ["new", "tok", "syd"], "uc"
        self.assertIsNone(run.lean_reader_host(s, True, "lon"))
        self.assertEqual(run.lean_reader_host(s, True, "new"), "view")
        self.assertEqual(run.lean_reader_host(s, False, "lon"), "view")
        s.nano_client = "hls"
        self.assertEqual(run.lean_reader_host(s, True, "lon"), "nano")
        s.hls_reader_sites, s.nano_client = None, "uc"
        self.assertEqual(run.lean_reader_host(s, True, "lon"), "view")


class Settings(unittest.TestCase):
    def test_expected_settings(self):
        deployed = {"video_smooth_fraction": 0.8, "send_pacing": 0, "late_primary": True,
                    "part_repair_group": "off"}
        self.assertEqual(run.settings_mismatch(deployed, "video_smooth_fraction=0.80,send_pacing=0,"
                                                          "late_primary=on,part_repair_group=off"), [])
        wrong = run.settings_mismatch(deployed, "send_pacing=1.3,video_smooth_tick_us=250")
        self.assertEqual(len(wrong), 2)
        self.assertIn("send_pacing is 0", wrong[0])
        self.assertIn("not set", wrong[1])


class Sender(unittest.TestCase):
    def test_realtime_start_turns_off_the_input_probe(self):
        s = session(PLAN)
        s.nodes["contrib-lon"] = {"vlan_ipv4": "192.168.1.2"}
        lane = run.Lane(s, "nt")
        self.assertIn("-probesize 32 -analyzeduration 0 -fflags nobuffer -f flv -i pipe:0",
                      lane.nt_video("2160p240"))
        s.sender_start = "probe"
        self.assertNotIn("-probesize", lane.nt_video("2160p240"))


class Crowd(unittest.TestCase):
    def test_shares(self):
        self.assertEqual(crowd.shares(21, 2), [11, 10])
        self.assertEqual(crowd.shares(20, 2), [10, 10])

    def test_judge_reads_loss_only_while_every_load_side_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            step = pathlib.Path(directory)
            base = 1_800_000_000_000

            def write(host, rows):
                (step / host / "crowd").mkdir(parents=True)
                (step / host / "crowd" / "intervals.jsonl").write_text(
                    "\n".join(json.dumps(r) for r in rows) + "\n")

            def row(t, saturated=False, missing=0, drops=0, target=10):
                return {"phase": "run", "t": float(t), "unix_ms": base + 1000 * t, "load_host_saturated": saturated,
                        "target": target, "total": {"missing": missing, "socket_drops": drops}}
            write("lon-nano", [row(1), row(2), row(3, missing=4), row(4, missing=4)])
            write("lon-view", [row(1), row(2), row(3), row(4, drops=9)])
            verdict = crowd.judge(step, ["lon-nano", "lon-view"])
            self.assertEqual(verdict["readable_until_s"], 4.0)
            self.assertEqual(verdict["first_missing_s"], 3.0)
            self.assertTrue(verdict["missing_readable"])
            self.assertEqual(verdict["hosts"]["lon-view"]["first_socket_drop_s"], 4.0)

            write("lon-x", [row(1, saturated=True)])
            verdict = crowd.judge(step, ["lon-nano", "lon-x"])
            self.assertEqual(verdict["readable_until_s"], 1.0)
            self.assertFalse(verdict["missing_readable"])


if __name__ == "__main__":
    unittest.main()
