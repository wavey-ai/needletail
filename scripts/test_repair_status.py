#!/usr/bin/env python3
"""4k-repair's counters in status, the memory sampler and a run's usage:
what a mesh node serves of its part repair, decode pool, tees and repair
stores, and what the contributor serves of its answers, read with and
without the fields an older binary lacks.

python3 -m unittest scripts/test_repair_status.py"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_module("deploy_repair_status", "linode-small-mesh-deploy.py")
run = load_module("run_repair_status", "linode-small-mesh-run.py")

MB = 1048576

# A fast plane snapshot of a node with every field this round reads.
NEW_PLANE = {
    "enabled": True, "threads": 2, "tee_dropped": 3, "not_admitted": 7,
    "parent_loss_ppm": 120, "video_parent_loss_ppm": [10_000, 500],
    "repair_trimmed": 44, "audio_tee_cpu_us": 1_000, "video_tee_cpu_us": 90_000,
    "video_tee_lag_max_us": 12_400, "audio_tee_lag_max_us": 600,
    "video_tee_parts_shed": 2,
    "part_repair": {"requests": 30, "retries": 4, "symbols_asked": 300,
                    "whole_parts_asked": 1, "gap_parts": 1, "recovered_parts": 29,
                    "gave_up_parts": 0, "asks": 12, "answers": 11, "answer_symbols": 140,
                    "busy": 0, "stored_parts": 128, "link_repair_symbols": 900,
                    "regenerated_symbols": 55, "refused_not_admitted": 1,
                    "refused_limited": 2, "refused_stale": 3, "window_spent": 4,
                    "repair_panics": 0, "probes": 9},
    "decode_pool": {"workers": 1, "decodes": 40, "failures": 1, "decode_us": 5,
                    "decode_us_max": 31_000, "inline": 0, "queued_max": 3,
                    "inline_bytes_limited": 0, "queue_bytes": 1_000_000,
                    "queue_budget_bytes": 64 * MB},
    "parent_lanes": [{"lane": "primary", "received": 9, "not_admitted": 5},
                     {"lane": "secondary", "received": 9, "not_admitted": 2}],
}
NEW_MEMORY = {"state": "ready", "rss_bytes": 900 * MB, "part_store_bytes": 100 * MB,
              "part_store_budget_bytes": 160 * MB, "decode_queue_bytes": 2 * MB,
              "decode_queue_budget_bytes": 64 * MB, "repair_plans_bytes": 34 * MB,
              "repair_plans_budget_bytes": 48 * MB, "repair_shed_bytes": 0}
# The same node's binary before 4k-repair: no part_repair, decode_pool,
# per-lane loss, audio tee lag; memory without the repair stores.
OLD_PLANE = {"enabled": True, "tee_dropped": 3, "not_admitted": 7, "parent_loss_ppm": 120,
             "audio_tee_cpu_us": 1_000, "video_tee_cpu_us": 90_000,
             "video_tee_lag_max_us": 12_400, "video_tee_parts_shed": 2}
OLD_MEMORY = {"state": "ready", "rss_bytes": 900 * MB}


class MeshFields(unittest.TestCase):
    def test_every_field_is_read_where_av_mesh_serves_it(self):
        fields = deploy.repair_fields(NEW_PLANE, NEW_MEMORY)
        self.assertEqual(fields["part_repair.requests"], 30)
        self.assertEqual(fields["part_repair.refused_stale"], 3)
        self.assertEqual(fields["decode_pool.decode_us_max"], 31_000)
        self.assertEqual(fields["video_parent_loss_ppm"], [10_000, 500])
        self.assertEqual(fields["audio_tee_lag_max_us"], 600)
        self.assertEqual(fields["memory.part_store_budget_bytes"], 160 * MB)
        self.assertEqual(fields["parent_lanes.not_admitted"], [5, 2])
        self.assertEqual(fields["part_repair.window_spent"], 4)
        self.assertEqual(fields["decode_pool.queue_budget_bytes"], 64 * MB)
        # Only the listed fields: not the pool's sum, the asker's probes.
        self.assertNotIn("decode_pool.decode_us", fields)
        self.assertNotIn("part_repair.probes", fields)
        self.assertEqual(len(fields), len(deploy.PART_REPAIR_FIELDS)
                         + len(deploy.DECODE_POOL_FIELDS) + len(deploy.FAST_PLANE_REPAIR_FIELDS)
                         + len(deploy.MEMORY_REPAIR_FIELDS) + 1)

    def test_an_older_binary_s_fields_are_absent_not_zero(self):
        fields = deploy.repair_fields(OLD_PLANE, OLD_MEMORY)
        self.assertEqual(sorted(fields), sorted([
            "tee_dropped", "not_admitted", "parent_loss_ppm", "audio_tee_cpu_us",
            "video_tee_cpu_us", "video_tee_lag_max_us", "video_tee_parts_shed"]))
        # Without the refusals of this round only.
        newer = json.loads(json.dumps(NEW_PLANE))
        for kind in ("not_admitted", "limited", "stale"):
            del newer["part_repair"][f"refused_{kind}"]
        del newer["audio_tee_lag_max_us"]
        fields = deploy.repair_fields(newer, OLD_MEMORY)
        self.assertNotIn("part_repair.refused_limited", fields)
        self.assertIn("part_repair.answers", fields)
        self.assertNotIn("memory.part_store_bytes", fields)
        line = deploy.repair_summary(fields)
        self.assertIn(" refused=-/-/- ", line)
        self.assertIn(" tee lag a/v=-/12ms ", line)
        self.assertIn(" store=-/-MB ", line)

    def test_nothing_served_reads_as_nothing(self):
        for plane, memory in ((None, None), ({}, {}), ("x", 3), ({"part_repair": None}, None),
                              ({"part_repair": {"asks": None, "answers": True}}, None),
                              ({"video_parent_loss_ppm": [1, None]}, None)):
            self.assertEqual(deploy.repair_fields(plane, memory), {}, (plane, memory))
        self.assertEqual(deploy.repair_summary({}), "")

    def test_status_s_line(self):
        line = deploy.repair_summary(deploy.repair_fields(NEW_PLANE, NEW_MEMORY))
        self.assertEqual(
            line,
            " not_admitted=7(lanes 5/2) <-- NOT ADMITTED (must be 0)"
            " repair req=30/4 asks=12 answers=11 sym=140 link=900 regen=55 rec=29 gaveup=0"
            " refused=1/2/3 pool=40/0/1 tee lag a/v=1/12ms drop=3 shed=2"
            " loss p/s=10000/500ppm small=120ppm trimmed=44 spent=4"
            " store=100/160MB queue=2/64MB repair_shed=0MB")
        old = deploy.repair_summary(deploy.repair_fields(OLD_PLANE, OLD_MEMORY))
        self.assertTrue(old.startswith(" not_admitted=7 <-- NOT ADMITTED (must be 0) repair "
                                       "req=-/- asks=- "), old)
        self.assertIn(" loss p/s=- small=120ppm ", old)
        # A clean node: 0, no warning; a repair thread's panic is shouted.
        clean = json.loads(json.dumps(NEW_PLANE))
        clean["not_admitted"] = 0
        clean["parent_lanes"][0]["not_admitted"] = 0
        clean["parent_lanes"][1]["not_admitted"] = 0
        clean["part_repair"]["repair_panics"] = 1
        line = deploy.repair_summary(deploy.repair_fields(clean, NEW_MEMORY))
        self.assertTrue(line.startswith(" not_admitted=0(lanes 0/0) repair "), line)
        self.assertIn(" spent=4 PANICS=1 store=", line)


class Contributor(unittest.TestCase):
    METRICS = ("# HELP av_contrib_relay_asks_answered Asks answered.\n"
               "# TYPE av_contrib_relay_asks_answered gauge\n"
               "av_contrib_relay_asks_answered 17\n"
               "av_contrib_relay_answer_symbols 230\n"
               "av_contrib_relay_lane_reported_loss_primary_ppm 5\n"
               'av_contrib_relay_answer_symbols{lane="primary"} 999\n')

    def test_answers_from_metrics_and_the_store_where_served(self):
        fields = deploy.contributor_repair_fields(self.METRICS, {"state": "ready"})
        self.assertEqual(fields, {"asks_answered": 17, "answer_symbols": 230})
        self.assertEqual(deploy.contributor_repair_summary(fields),
                         " repair answered=17 sym=230 store=-/-MB")
        with_store = self.METRICS + ("av_contrib_part_store_bytes 52428800\n"
                                     "av_contrib_part_store_budget_bytes 167772160\n")
        fields = deploy.contributor_repair_fields(with_store)
        self.assertEqual(deploy.contributor_repair_summary(fields),
                         " repair answered=17 sym=230 store=50/160MB")
        # Or in its /api/node, under memory or at the top.
        for document in ({"memory": {"part_store_bytes": 10 * MB,
                                     "part_store_budget_bytes": 20 * MB}},
                         {"part_store_bytes": 10 * MB, "part_store_budget_bytes": 20 * MB}):
            fields = deploy.contributor_repair_fields(self.METRICS, document)
            self.assertEqual(fields["memory.part_store_budget_bytes"], 20 * MB)

    def test_an_older_contributor_shows_nothing(self):
        self.assertEqual(deploy.contributor_repair_fields(
            "av_contrib_relay_lane_reported_loss_primary_ppm 5\n", {"state": "ready"}), {})
        self.assertEqual(deploy.contributor_repair_fields(None, None), {})
        self.assertEqual(deploy.contributor_repair_summary({}), "")


class StatusProbe(unittest.TestCase):
    def test_the_repair_line_is_taken_out_of_the_probe(self):
        text = ("needletail-mesh active restarts=0 cpu_mb_s=1400\nTHP [never]\nWARM {}\n"
                "REPAIR " + json.dumps(NEW_PLANE))
        rest, plane = deploy.split_repair_probe(text)
        self.assertNotIn("REPAIR", rest)
        self.assertEqual(plane["part_repair"]["asks"], 12)
        line = deploy.status_repair("edge", plane, {"memory": NEW_MEMORY})
        self.assertIn(" store=100/160MB ", line)
        # The probe's line of an older node, a silent one, the contributor's.
        for body, expected in (("REPAIR null", None), ("REPAIR", None), ("REPAIR {bad", None),
                               ("", None)):
            self.assertEqual(deploy.split_repair_probe("svc\n" + body)[1], expected, body)
        self.assertEqual(deploy.status_repair("edge", None, None), "")
        self.assertEqual(deploy.status_repair("edge", None, {"memory": OLD_MEMORY}), "")
        _rest, metrics = deploy.split_repair_probe(
            "svc\nREPAIR av_contrib_relay_asks_answered 3;av_contrib_relay_answer_symbols 40;")
        self.assertEqual(deploy.status_repair("contributor", metrics, {"state": "ready"}),
                         " repair answered=3 sym=40 store=-/-MB")
        self.assertEqual(deploy.status_repair("contributor", None, {"state": "ready"}), "")

    def test_the_probe_reads_the_node_s_own_counters(self):
        probe = deploy.STATUS_REPAIR_PROBE
        self.assertIn(f"127.0.0.1:{deploy.HTTP_PORT}/api/mesh/local", probe)
        self.assertIn(".relay_session.fast_plane // null", probe)
        self.assertIn(f"127.0.0.1:{deploy.CONTRIB_HTTP_PORT}/metrics", probe)
        for name in deploy.CONTRIB_REPAIR_METRICS:
            self.assertIn(name, probe)


class Sampler(unittest.TestCase):
    def test_the_sampler_keeps_the_repair_fields(self):
        sampler = deploy.MEMORY_SAMPLER
        compile(sampler, "sampler.py", "exec")
        self.assertNotIn("@FAST_FIELDS@", sampler)
        listed = json.loads(re.search(r"^FAST_FIELDS = (.*)$", sampler, re.M).group(1))
        self.assertEqual(listed, list(deploy.FAST_PLANE_REPAIR_FIELDS))
        metrics = json.loads(re.search(r"^CONTRIB_METRICS = (.*)$", sampler, re.M).group(1))
        self.assertEqual(sorted(metrics), sorted(deploy.CONTRIB_REPAIR_METRICS))
        for text in ('"/api/mesh/local"', '"part_repair", "decode_pool", "parent_lanes"',
                     '"/metrics"',
                     'row["fast_plane"]', 'row["contrib_repair"]', 'row["memory"]'):
            self.assertIn(text, sampler)
        contrib = deploy.memory_sampler_start_script({"role": "contributor"}, "T", 1.0)
        self.assertIn(" 1.0 contributor ", contrib)
        edge = deploy.memory_sampler_start_script({"role": "edge"}, "T", 1.0)
        self.assertIn(" 1.0 mesh ", edge)


class RunUsage(unittest.TestCase):
    def snapshot(self, plane, memory, service="needletail-mesh"):
        return {"service": service, "relay_session": {"fast_plane": plane},
                "node_memory": memory}

    def test_counts_differ_and_levels_are_taken_at_the_end(self):
        before = json.loads(json.dumps(NEW_PLANE))
        before["part_repair"].update(asks=2, stored_parts=10)
        before["decode_pool"].update(decodes=4, decode_us_max=50_000)
        usage = run.repair_usage(self.snapshot(before, NEW_MEMORY),
                                 self.snapshot(NEW_PLANE, NEW_MEMORY))
        self.assertEqual(usage["part_repair.asks"], 10)
        self.assertEqual(usage["decode_pool.decodes"], 36)
        self.assertEqual(usage["part_repair.stored_parts"], 128)
        self.assertEqual(usage["decode_pool.decode_us_max"], 31_000)
        self.assertEqual(usage["video_parent_loss_ppm"], [10_000, 500])
        self.assertEqual(usage["memory.part_store_bytes"], 100 * MB)
        self.assertEqual(usage["tee_dropped"], 0)

    def test_an_older_node_or_a_restart_leaves_out_what_it_cannot_count(self):
        usage = run.repair_usage(self.snapshot(OLD_PLANE, OLD_MEMORY),
                                 self.snapshot(NEW_PLANE, NEW_MEMORY))
        self.assertNotIn("part_repair.asks", usage)
        self.assertEqual(usage["part_repair.stored_parts"], 128)
        self.assertEqual(run.repair_usage(self.snapshot(None, None),
                                          self.snapshot(None, None)), {})

    def test_the_contributor_s_answers(self):
        before = {"service": "needletail-contrib", "contrib_metrics":
                  "av_contrib_relay_asks_answered 5;av_contrib_relay_answer_symbols 50;",
                  "contrib_part_store": {"part_store_bytes": None,
                                         "part_store_budget_bytes": None}}
        after = dict(before, contrib_metrics="av_contrib_relay_asks_answered 9;"
                                              "av_contrib_relay_answer_symbols 80;")
        self.assertEqual(run.repair_usage(before, after),
                         {"asks_answered": 4, "answer_symbols": 30})
        self.assertIn("node_memory:($node.memory // null)", run.SNAPSHOT)
        self.assertIn("av_contrib_(relay_asks_answered", run.SNAPSHOT)


class AvMeshServes(unittest.TestCase):
    """Each field read is declared where av-mesh serves it (av-mesh rm/int6
    serves all of them; PENDING names any a newer round adds)."""

    PENDING: set = set()

    def test_against_the_av_mesh_checkout(self):
        import av_mesh_contract
        root = av_mesh_contract.root()
        if root is None:
            self.skipTest("NEEDLETAIL_AV_MESH_ROOT is not set")
        sources = av_mesh_contract.SOURCES + ("src/fast_plane/part_repair.rs",
                                              "src/fast_plane/decode_pool.rs",
                                              "src/mem_guard.rs")
        original = av_mesh_contract.SOURCES
        try:
            av_mesh_contract.SOURCES = sources
            declared = av_mesh_contract.structs(root)
        finally:
            av_mesh_contract.SOURCES = original
        plane = av_mesh_contract.paths(declared, "FastPlaneSnapshot")
        wanted = ([f"part_repair/{name}" for name in deploy.PART_REPAIR_FIELDS]
                  + [f"decode_pool/{name}" for name in deploy.DECODE_POOL_FIELDS]
                  + list(deploy.FAST_PLANE_REPAIR_FIELDS))
        self.assertEqual([path for path in wanted
                          if path not in plane and path not in self.PENDING], [])
        memory = declared.get("MemorySnapshot", {})
        self.assertEqual([name for name in deploy.MEMORY_REPAIR_FIELDS if name not in memory], [])
        # The fast plane is served at relay_session/fast_plane of /api/mesh/local.
        served = av_mesh_contract.mesh_local_paths(root)
        self.assertIn("relay_session/fast_plane/part_repair", served)
        self.assertIn("relay_session/fast_plane/parent_lanes[]/not_admitted", served)


if __name__ == "__main__":
    unittest.main()
