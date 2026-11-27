#!/usr/bin/env python3
"""Integration 6's deploy defaults and the flags probe (av-mesh rm/int7):
part repair off for the headline with one group to turn it on, the send
path's flags rendered explicitly, pacing with its fq, the edges' heavy
egress cap from the egress plan, no hop-journal override, the 4K
topologies' declared rate, and a render that refuses a binary without a
flag it renders. python3 -m unittest scripts/test_integrate6_deploy.py"""

from __future__ import annotations

import io
import json
import pathlib
import re
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_merged_topology as merged  # noqa: E402

deploy = merged.deploy
COMPARE_4K = deploy.common.ROOT / "deploy" / "compare-cloudflare" / "topology-4k-lk.json"


@unittest.skipUnless(merged.COMPILER.is_file(), "needs target/release/needletail-compile")
class Defaults(merged.Base):
    def render(self, overrides=(), state=None, binaries=None):
        settings = self.settings(overrides)
        state = state or merged.topology_state(COMPARE_4K)
        program = deploy.build_program(state, settings, merged.ROUTES)
        plan = deploy.compile_plan(program)
        with mock.patch.dict(deploy.RENDER_BINARIES, binaries or {}, clear=True), \
                redirect_stderr(io.StringIO()):
            rendered = deploy.render(state, settings, plan)
        return state, {name: item["args"] for name, item in rendered.items()}

    def mesh(self, state, args):
        return {name: item for name, item in args.items()
                if state["nodes"][name]["role"] != "contributor"}

    def test_part_repair_is_off_for_the_headline_and_one_group_turns_it_on(self):
        state, args = self.render()
        fast = {name: item for name, item in self.mesh(state, args).items()
                if "--relay-controlled-local" in item}
        self.assertTrue(fast)
        for name, item in fast.items():
            for flag in ("--relay-part-repair", "--relay-link-repair",
                         "--relay-trim-forwarded-repair"):
                self.assertEqual(merged.values_of(item, flag), ["false"], (name, flag))
        # integrate-5b's --relay-primary-promoted stays: the regional relays
        # (a source-lane primary) take it once.
        for name in ("new-dist-a", "new-dist-b"):
            self.assertEqual(args[name].count("--relay-primary-promoted"), 1, name)
        _state, on = self.render(["part_repair_group=on"])
        for name in fast:
            for flag in ("--relay-part-repair", "--relay-link-repair",
                         "--relay-trim-forwarded-repair"):
                self.assertEqual(merged.values_of(on[name], flag), ["true"], (name, flag))
        # A switch set on its own wins over the group.
        _state, mixed = self.render(["part_repair_group=on", "relay_link_repair=false"])
        some = next(iter(fast))
        self.assertEqual(merged.value_of(mixed[some], "--relay-link-repair"), "false")
        self.assertEqual(merged.value_of(mixed[some], "--relay-part-repair"), "true")
        # None: the binaries' defaults, nothing rendered.
        _state, none = self.render(["part_repair_group=null"])
        self.assertNotIn("--relay-part-repair", none[some])
        with self.assertRaises(ValueError):
            deploy.repair_switches({"part_repair_group": "maybe"})

    def test_the_send_path_is_rendered_explicitly_on_every_mesh_service(self):
        state, args = self.render()
        for name, item in self.mesh(state, args).items():
            self.assertEqual(merged.values_of(item, "--send-defer-ms"), ["200"], name)
            self.assertEqual(merged.values_of(item, "--send-defer-mb"), ["auto"], name)
            self.assertEqual(merged.values_of(item, "--send-pacing"), ["0"], name)
            self.assertEqual(merged.values_of(item, "--edge-pull-budget-mbit"), ["48"], name)
            # rm/cpuadmit's admission, explicit, and place240's busiest-CPU
            # distress off.
            for flag_name, value in (("--cpu-admit", "true"),
                                     ("--cpu-admit-cpus-full-percent", "80"),
                                     ("--cpu-admit-thread-full-percent", "70"),
                                     ("--cpu-admit-datagram-ns", "3000"),
                                     ("--cpu-admit-pending-max-permille", "100"),
                                     ("--cpu-admit-shed-after-ms", "2000"),
                                     ("--heavy-distress-core-permille", "0")):
                self.assertEqual(merged.values_of(item, flag_name), [value], (name, flag_name))
            # No hop-journal override (integrate-5a: +161.5 MB a node).
            self.assertNotIn("--hop-journal-capacity", item, name)
        for name, item in args.items():
            if state["nodes"][name]["role"] == "contributor":
                self.assertNotIn("--send-pacing", item, name)

    def test_an_edge_s_heavy_egress_cap_is_the_egress_plan_s_limit(self):
        state, args = self.render()
        edges = [name for name, node in state["nodes"].items() if node["role"] == "edge"]
        self.assertTrue(edges)
        for name in edges:
            limit = deploy.egress_plan.DEFAULT_LIMITS_MBIT[state["nodes"][name]["type"]]
            self.assertEqual(merged.value_of(args[name], "--heavy-egress-max-mbps"),
                             str(int(limit)), name)
        for name, node in state["nodes"].items():
            if node["role"] != "edge" and name in args:
                self.assertNotIn("--heavy-egress-max-mbps", args[name], name)
        _state, given = self.render(["heavy_egress_max_mbps=640"])
        self.assertEqual(merged.value_of(given[edges[0]], "--heavy-egress-max-mbps"), "640")

    def test_pacing_renders_k_and_puts_fq_where_the_host_qdisc_has_none(self):
        state, args = self.render(["send_pacing=4"])
        some = next(name for name in args if state["nodes"][name]["role"] == "edge")
        self.assertEqual(merged.value_of(args[some], "--send-pacing"), "4")
        # The prio root's video band is fq already.
        self.assertEqual(deploy.class_plan({**deploy.DEFAULT_SETTINGS, "send_pacing": 4})
                         ["host_qdisc"], "prio")
        paced = {**deploy.DEFAULT_SETTINGS, "send_pacing": 4, "host_qdisc": "none"}
        self.assertEqual(deploy.class_plan(paced)["host_qdisc"], "fq")
        files = deploy.node_files(["--x"], "unit", paced)
        self.assertIn("root handle 30: fq", files["qdisc"])
        off = deploy.node_files(["--x"], "unit", {**paced, "send_pacing": 0})
        self.assertIn("fq 30): root", off["qdisc"])
        self.assertNotIn("root handle 30: fq", off["qdisc"])
        shown = ("== eth0\nqdisc fq 30: root refcnt 2 limit 65536p flow_limit 4096p\n"
                 "== eth1\nqdisc fq 30: root refcnt 2\n")
        self.assertEqual(deploy.check_host_qdisc(shown, "fq"), [])
        self.assertEqual(deploy.check_host_qdisc(shown, "none"), ["eth0", "eth1"])
        self.assertEqual(deploy.check_host_qdisc("== eth0\nqdisc mq 0: root\n", "fq"), ["eth0"])

    def fake_binary(self, flags: set[str], name: str) -> pathlib.Path:
        path = self.dir / name
        path.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'av-mesh 0.1.0 "
                        "(abc123def456 1795600000)'; exit 0; fi\n"
                        f"echo 'Usage: av-mesh [OPTIONS]'; echo '{' '.join(sorted(flags))}'\n")
        path.chmod(0o755)
        return path

    def test_the_flags_probe_refuses_a_binary_without_a_flag_it_renders(self):
        # Every flag the deploy tool can render, as a binary that lists them
        # all: the render passes, and renders what binary_takes adds.
        source = (HERE / "linode-small-mesh-deploy.py").read_text()
        everything = set(re.findall(r"--[a-z0-9][a-z0-9-]*", source))
        # and the compiled plan's (needletail-compile's) flags.
        _state, plain = self.render()
        for item in plain.values():
            everything |= deploy.flag_names(item)
        full = self.fake_binary(everything, "av-mesh")
        contrib = self.fake_binary(everything, "av-contrib")
        state, args = self.render(binaries={"av-mesh": full, "av-contrib": contrib})
        # integrate-5a's memory budgets reach a binary that lists them.
        for name, node in state["nodes"].items():
            if node["role"] != "contributor" and node["type"] == "g6-dedicated-2" \
                    and name in args:
                self.assertEqual(merged.value_of(args[name], "--memory-soft-mb"), "1960", name)
                self.assertEqual(merged.value_of(args[name], "--video-cache-mb"), "392", name)
        old = self.fake_binary(everything - {"--send-pacing"}, "av-mesh-old")
        with self.assertRaises(ValueError) as refused:
            self.render(binaries={"av-mesh": old, "av-contrib": contrib})
        text = str(refused.exception)
        self.assertIn("does not know --send-pacing", text)
        self.assertIn("abc123def456 1795600000", text)
        self.assertIn("lon-edge-1", text)
        # A binary that is not there: a render does not probe it (it renders
        # none of the new flags for it); plan, push and restart refuse it.
        missing = self.dir / "gone"
        _state, rendered = self.render(binaries={"av-mesh": missing, "av-contrib": contrib})
        items = {name: {"service": "needletail-mesh", "args": item}
                 for name, item in rendered.items()
                 if state["nodes"][name]["role"] != "contributor"}
        with mock.patch.dict(deploy.RENDER_BINARIES, {"av-mesh": missing}, clear=True):
            with self.assertRaises(ValueError) as refused:
                deploy.check_rendered_flags(items, strict=True)
        self.assertIn("cannot be checked", str(refused.exception))


def real_binaries() -> dict:
    roots = {"av-mesh": __import__("os").environ.get("NEEDLETAIL_AV_MESH_ROOT"),
             "av-contrib": __import__("os").environ.get("NEEDLETAIL_AV_CONTRIB_ROOT")}
    out = {}
    for kind, root in roots.items():
        path = pathlib.Path(root or "/nonexistent") / "target" / "release" / kind
        if path.is_file():
            out[kind] = path
    return out


@unittest.skipUnless(merged.COMPILER.is_file(), "needs target/release/needletail-compile")
@unittest.skipIf(__import__("os").environ.get("NEEDLETAIL_AV_MESH_TOO_OLD"),
                 "the av-mesh found is too old: "
                 + (__import__("os").environ.get("NEEDLETAIL_AV_MESH_TOO_OLD") or ""))
@unittest.skipUnless(len(real_binaries()) == 2,
                     "needs NEEDLETAIL_AV_MESH_ROOT and NEEDLETAIL_AV_CONTRIB_ROOT with release builds")
class RealBinaries(Defaults):
    """The default render of the 4K lab passes the flags probe against the
    binaries of the roots given (scripts/run-tests.py: the int7 worktrees)."""

    def test_the_default_render_passes_the_flags_probe_of_the_real_binaries(self):
        state, args = self.render(binaries=real_binaries())
        edge = next(name for name in args if state["nodes"][name]["role"] == "edge")
        self.assertEqual(merged.value_of(args[edge], "--send-defer-ms"), "200")
        self.assertEqual(merged.value_of(args[edge], "--relay-part-repair"), "false")

    # The Defaults tests run in Defaults.
    test_part_repair_is_off_for_the_headline_and_one_group_turns_it_on = None
    test_the_send_path_is_rendered_explicitly_on_every_mesh_service = None
    test_an_edge_s_heavy_egress_cap_is_the_egress_plan_s_limit = None
    test_pacing_renders_k_and_puts_fq_where_the_host_qdisc_has_none = None
    test_the_flags_probe_refuses_a_binary_without_a_flag_it_renders = None


class Topologies(unittest.TestCase):
    def test_every_4k_topology_declares_its_planned_rate(self):
        """tools6's egress plan reads plan_stream_mbit: each 4K lab's
        topology declares it (247 Mbit/s), so its plan never falls back to
        a default for another rate."""
        root = deploy.common.ROOT / "deploy"
        found = [path for path in root.glob("*/topology-*.json")
                 if "4k" in path.name or path.name == "topology-proving.json"]
        self.assertGreaterEqual(len(found), 3, found)
        for path in found:
            topology = json.loads(path.read_text())
            self.assertEqual(topology.get("plan_stream_mbit"), 247, path.name)


if __name__ == "__main__":
    unittest.main()
