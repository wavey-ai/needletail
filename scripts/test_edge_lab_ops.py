#!/usr/bin/env python3
"""Tests of EdgeLabOps and the lab's engine verbs (edge_lab.py roll,
restart, replace; C 2.12, 2.13, Unit L-E CL1), offline, on a faked lab: the
lab's primitives (start and stop of a service, its build set, the HTTP API
from the lab's namespace) are replaced by FakeLab, whose nodes answer
`/api/node` as the engine tests' simulated mesh does (test_mesh_ops.Sim),
with the build of the set each node was started on and the sha256 of the
argv it was started with. No namespace, service or network; a fake clock.
python3 -m unittest scripts/test_edge_lab_ops.py"""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab  # noqa: E402
import mesh_ops  # noqa: E402
import test_mesh_ops as engine_tests  # noqa: E402

SETS = {"a": engine_tests.OLD, "b": engine_tests.NEW}
MARK = re.compile(r"^(start|end) [0-9]+\.[0-9]{9} \S+$")


class FakeLab:
    """The lab's primitives over a simulated mesh. Every Sim node is a node
    of the lab; lon-edge-4 and new-edge-4 are slot edges out of service
    (the lab's `up slots=lon-edge-4,new-edge-4`), lon-dist-c the dist-c
    slot. A service started on set S runs S's build; a drain-node writes
    the node's drain file and a start reads it back (--drain-state-file)."""

    LabError = edge_lab.LabError
    SLOT_CANDIDATES = ("lon-edge-4", "new-edge-4", "lon-dist-c")

    def __init__(self, sim: engine_tests.Sim, work: pathlib.Path):
        self.sim = sim
        self.WORK = work
        self.NODES = [(name, node["role"], node["site"], node["site"], node["public"],
                       node["vlan"]) for name, node in sim.nodes_.items()]
        self.calls: list[tuple] = []
        self.args_sha: dict[str, str] = {}
        self.save("slots.json", list(self.SLOT_CANDIDATES))
        self.save("namespaces.json", {"lab": 1})
        self.save("binaries.json", {"a": {}, "b": {}})
        services = {}
        for name, node in sim.nodes_.items():
            self.save_build(name, "a")
            if node["running"]:
                self.args_sha[name] = self.sha(self.service_args(name, "a"))
                services[name] = {"pid": 100 + len(services), "started_unix": node["start"],
                                  "build": "a", "starts": 1, "crashes": 0, "limits": None,
                                  "env": None, "extra_args": None}
        self.save("services.json", services)
        self.sync()

    # ------------------------------------------------------------ state files
    def load(self, name, default=None):
        try:
            return json.loads((self.WORK / name).read_text())
        except (OSError, ValueError):
            return {} if default is None else default

    def save(self, name, value):
        self.WORK.mkdir(parents=True, exist_ok=True)
        (self.WORK / name).write_text(json.dumps(value))

    def save_build(self, name, build):
        builds = self.load("node-build.json")
        builds[name] = build
        self.save("node-build.json", builds)

    @staticmethod
    def sha(args):
        return hashlib.sha256("\n".join(args).encode()).hexdigest()

    # ------------------------------------------------------------ the lab's functions
    def slot_nodes(self):
        return list(self.SLOT_CANDIDATES)

    def slots_in_service(self):
        return set(self.load("slots-created.json", []))

    def set_slot_in_service(self, name, in_service):
        made = self.slots_in_service()
        made = made | {name} if in_service else made - {name}
        self.save("slots-created.json", sorted(made))
        self.sync()

    def sync(self):
        """The simulated mesh's view of who is in service (its entry lists
        and leg moves follow it)."""
        held = set(self.load(edge_lab.HELD_EDGES, []))
        made = self.slots_in_service()
        for name, node in self.sim.nodes_.items():
            taken = (name not in self.SLOT_CANDIDATES or name in made) and name not in held
            node["serving"] = taken
            if node["role"] == "edge":
                node["standby"] = not taken

    def relay_parents(self):
        return {name: (node["primary"], node["secondary"])
                for name, node in self.sim.nodes_.items() if node["primary"]}

    def build_sets(self):
        return ["a", "b"]

    def build_id(self, label, build):
        git, unix = SETS[build]
        return {"git": git, "unix": unix, "dirty": False}

    def node_build(self, name):
        return self.load("node-build.json").get(name, "a")

    def set_node_build(self, name, build):
        self.calls.append(("set_node_build", name, build))
        self.save_build(name, build)

    def rendered_service(self, name, build):
        if name not in self.sim.nodes_:
            raise self.LabError(f"{name} has no service")
        return {"args": self.service_args(name, build), "rust_log": "info"}

    def service_args(self, name, build=None):
        return [f"{self.WORK}/bin/{build or self.node_build(name)}/av-mesh", "--node-id", name]

    def service_pid(self, name):
        return (self.load("services.json").get(name) or {}).get("pid") \
            if self.sim.nodes_[name]["running"] else None

    def start_service(self, name, limits=None, env=None, extra_args=None, build=None):
        self.calls.append(("start", name))
        if build is not None:
            self.set_node_build(name, build)
        build = self.node_build(name)
        node = self.sim.nodes_[name]
        self.sim.restart(name, SETS[build])
        node["drained"] = (self.WORK / "state" / f"{name}.drained").is_file()
        self.args_sha[name] = self.sha(self.service_args(name, build) + list(extra_args or []))
        services = self.load("services.json")
        old = services.get(name, {})
        services[name] = {"pid": 500 + old.get("starts", 0), "started_unix": self.sim.clock(),
                          "build": build, "starts": old.get("starts", 0) + 1,
                          "crashes": old.get("crashes", 0), "limits": limits, "env": env,
                          "extra_args": extra_args}
        self.save("services.json", services)
        self.sync()
        return services[name]["pid"]

    def terminate_service(self, name, wait=15.0):
        if self.sim.nodes_[name]["running"]:
            self.calls.append(("stop", name))
        self.sim.nodes_[name]["running"] = False

    def http_json(self, name, path, method="GET", body=None, timeout=5.0):
        self.sync()
        if method == "POST":
            code = self.sim.post(name, path, body)
            drain = self.WORK / "state" / f"{name}.drained"
            if code and path.endswith("drain-node"):
                drain.parent.mkdir(parents=True, exist_ok=True)
                drain.write_text("operator\n")
            elif code and path.endswith("resume-node"):
                drain.unlink(missing_ok=True)
            return code or 0, {}
        if path == "/api/node":
            document = self.sim.doc(name)
            if document is None:
                return 0, None
            document["process"]["args_sha256"] = self.args_sha.get(name)
            return 200, document
        code, answer = self.sim.get(name, path)
        return code or 0, answer


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = engine_tests.Clock()
        self.sim = engine_tests.Sim(self.clock, self.tmp)
        self.lab = FakeLab(self.sim, self.tmp / "work")
        self.out = io.StringIO()
        self.err = io.StringIO()

    def ops(self, build=None, gap_s=30.0) -> edge_lab.EdgeLabOps:
        return edge_lab.EdgeLabOps(build=build, gap_s=gap_s, lab=self.lab, clock=self.clock,
                                   sleep=self.clock.sleep)

    def verb(self, command, *arguments, build=None, gap_s=30.0) -> int:
        """The CLI verb, as `edge_lab.py COMMAND ARGUMENTS` parses it, on the
        faked lab and the fake clock."""
        ops = self.ops(build=build, gap_s=gap_s)
        return edge_lab.engine_verb(command, list(arguments), ops=ops, out=self.out,
                                    err=self.err, clock=self.clock, sleep=self.clock.sleep)

    def two_edges(self) -> None:
        """London serves two edges (lon-edge-3 held out, standby): a roll
        or a replace of its edges needs the surge edge (C 3.5)."""
        self.lab.terminate_service("lon-edge-3")
        self.lab.save(edge_lab.HELD_EDGES, ["lon-edge-3"])
        self.sim.nodes_["lon-edge-3"]["legs"] = 0
        self.lab.calls.clear()

    def marks(self) -> list[str]:
        lines = self.out.getvalue().splitlines()
        for line in lines:
            self.assertRegex(line, MARK)
        return [line.split()[0] + " " + line.split()[2] for line in lines]

    def starts(self) -> list[str]:
        return [call[1] for call in self.lab.calls if call[0] == "start"]

    def build_of(self, name) -> tuple:
        return self.sim.nodes_[name]["build"]


class Nodes(Base):
    def test_the_lab_as_the_engine_sees_it(self):
        records = {record["name"]: record for record in self.ops().nodes()}
        self.assertTrue(records["lon-edge-1"]["serving"])
        self.assertEqual((records["lon-edge-4"]["serving"], records["lon-edge-4"]["standby"],
                          records["lon-edge-4"]["slot"], records["lon-edge-4"]["created"]),
                         (False, True, True, True))
        # A dist-c out of service is a slot not created: add_slot takes it in.
        self.assertEqual((records["lon-dist-c"]["serving"], records["lon-dist-c"]["created"]),
                         (False, False))
        self.assertEqual((records["new-dist-a"]["primary"], records["new-dist-a"]["secondary"]),
                         ("lon-dist-a", "lon-dist-b"))
        self.assertEqual(self.ops().surge_candidates("london"), ["lon-edge-4"])
        self.assertEqual(self.ops().lock_path, self.lab.WORK / "ops.lock")

    def test_expected_is_the_set_s_build_and_the_argv_s_sha256(self):
        ops = self.ops(build="b")
        ops.stage(["lon-edge-1"], ops.target("edges"))
        expected = ops.expected("lon-edge-1")
        self.assertEqual((expected["git"], expected["unix"]), engine_tests.NEW)
        self.assertEqual(expected["args_sha256"],
                         FakeLab.sha(self.lab.service_args("lon-edge-1", "b")))
        self.assertIsNone(self.ops().target("edges"))

    def test_a_late_activation_and_a_late_hold_change_nothing(self):
        ops = self.ops(build="b")
        ops.stage(["lon-edge-1"], ops.target("edges"))
        late = int(self.clock() * 1000) - 1
        self.assertEqual(ops.activate("lon-edge-1", late), {"ok": False, "why": "late"})
        with self.assertRaises(mesh_ops.Refused):
            ops.stop_node("lon-edge-1", not_after_ms=late)
        self.assertEqual(self.starts(), [])
        self.assertTrue(self.sim.nodes_["lon-edge-1"]["running"])

    def test_a_refusal_before_anything_when_the_lab_is_down_or_lacks_the_set(self):
        self.lab.save("namespaces.json", {})
        self.assertEqual(self.verb("roll", "edges", "--build", "b", build="b"), 2)
        self.lab.save("namespaces.json", {"lab": 1})
        self.lab.build_sets = lambda: ["a"]
        self.assertEqual(self.verb("roll", "edges", "--build", "b", build="b"), 2)
        self.assertIn("no build set b", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.starts(), [])


class Roll(Base):
    def test_a_roll_of_london_s_edges_to_set_b_through_the_surge_edge(self):
        self.two_edges()
        code = self.verb("roll", "edges", "--build", "b", "--sites", "london", build="b")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.marks(), ["start roll.edges", "end roll.edges"])
        for name in ("lon-edge-1", "lon-edge-2"):
            self.assertEqual(self.build_of(name), engine_tests.NEW, name)
            self.assertEqual(self.lab.node_build(name), "b")
            self.assertTrue(self.sim.nodes_[name]["serving"])
        # The surge edge (the held edge before the slot) joined on the
        # target and left: stopped and held out again.
        self.assertEqual(self.build_of("lon-edge-3"), engine_tests.NEW)
        self.assertFalse(self.sim.nodes_["lon-edge-3"]["running"])
        self.assertIn("lon-edge-3", self.lab.load(edge_lab.HELD_EDGES, []))
        self.assertIn("roll.edges.join.lon-edge-3", self.err.getvalue())
        self.assertIn("roll.edges.leave.lon-edge-3", self.err.getvalue())
        # One node at a time, nothing outside London's edges restarted.
        self.assertEqual(self.starts(), ["lon-edge-3", "lon-edge-1", "lon-edge-2"])
        self.assertEqual(self.build_of("new-edge-1"), engine_tests.OLD)
        self.assertEqual(self.lab.load(edge_lab.PREV_BUILDS)["lon-edge-1"], "a")

    def test_three_serving_edges_need_no_surge_edge(self):
        code = self.verb("roll", "edges", "--build", "b", "--sites", "london", build="b")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.starts(), ["lon-edge-1", "lon-edge-2", "lon-edge-3"])
        self.assertFalse(self.sim.nodes_["lon-edge-4"]["running"])

    def test_without_a_build_set_a_roll_restarts_every_node_on_its_own(self):
        """6.3 roll-edges: `edge_lab.py roll edges --sites london` changes
        every edge's boot id on the set it runs."""
        boots = {name: self.sim.nodes_[name]["boot"] for name in ("lon-edge-1", "lon-edge-2",
                                                                  "lon-edge-3", "new-edge-1")}
        code = self.verb("roll", "edges", "--sites", "london")
        self.assertEqual(code, 0, self.err.getvalue())
        for name in ("lon-edge-1", "lon-edge-2", "lon-edge-3"):
            self.assertNotEqual(self.sim.nodes_[name]["boot"], boots[name], name)
            self.assertEqual(self.build_of(name), engine_tests.OLD)
        self.assertEqual(self.sim.nodes_["new-edge-1"]["boot"], boots["new-edge-1"])

    def test_a_forwarder_roll_and_its_dry_null(self):
        code = self.verb("roll", "forwarders", "--build", "b", "--dry-marks", build="b")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.marks(), ["start roll.forwarders", "end roll.forwarders"])
        self.assertEqual(self.starts(), [])
        self.out.truncate(0)
        self.out.seek(0)
        code = self.verb("roll", "forwarders", "--build", "b", build="b")
        self.assertEqual(code, 0, self.err.getvalue())
        for name in ("lon-dist-a", "lon-dist-b", "new-dist-a", "new-dist-b"):
            self.assertEqual(self.build_of(name), engine_tests.NEW, name)
        self.assertNotIn("lon-dist-c", self.starts())

    def test_a_build_that_fails_its_readiness_is_rolled_back_to_its_set(self):
        self.sim.broken[("lon-edge-1", engine_tests.NEW[0])] = "silent"
        code = self.verb("roll", "edges", "--build", "b", "--sites", "london", build="b")
        self.assertEqual(code, mesh_ops.EXIT_ROLLED_BACK, self.err.getvalue())
        self.assertEqual(self.build_of("lon-edge-1"), engine_tests.OLD)
        self.assertEqual(self.lab.node_build("lon-edge-1"), "a")
        self.assertEqual(self.build_of("lon-edge-2"), engine_tests.OLD)


class Restart(Base):
    def test_restart_holds_the_node_stopped_and_brings_it_back_on_its_set(self):
        boot = self.sim.nodes_["lon-dist-a"]["boot"]
        code = self.verb("restart", "lon-dist-a", "--hold-s", "2")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.marks(), ["start restart.lon-dist-a", "end restart.lon-dist-a"])
        self.assertNotEqual(self.sim.nodes_["lon-dist-a"]["boot"], boot)
        self.assertEqual(self.starts(), ["lon-dist-a"])
        self.assertEqual(self.build_of("lon-dist-a"), engine_tests.OLD)

    def test_restart_rollback_puts_the_previous_set_back(self):
        self.assertEqual(self.verb("roll", "edges", "--build", "b", "--nodes", "new-edge-1",
                                   build="b"), 0, self.err.getvalue())
        self.assertEqual(self.build_of("new-edge-1"), engine_tests.NEW)
        code = self.verb("restart", "new-edge-1", "--rollback")
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.build_of("new-edge-1"), engine_tests.OLD)
        self.assertEqual(self.lab.node_build("new-edge-1"), "a")


class Replace(Base):
    def test_an_edge_is_drained_stopped_rebuilt_and_resumed(self):
        self.two_edges()
        began = self.clock()
        code = self.verb("replace", "lon-edge-1", "--gap-s", "30", gap_s=30.0)
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.marks(), ["start replace.lon-edge-1", "end replace.lon-edge-1"])
        self.assertGreaterEqual(self.clock() - began, 30)
        node = self.sim.nodes_["lon-edge-1"]
        self.assertTrue(node["running"] and node["serving"] and not node["drained"])
        self.assertFalse((self.lab.WORK / "state" / "lon-edge-1.drained").exists())
        self.assertNotIn("lon-edge-1", self.lab.load(edge_lab.HELD_EDGES, []))
        # The surge edge came and went; nothing else restarted.
        self.assertEqual(self.starts(), ["lon-edge-3", "lon-edge-1"])
        self.assertFalse(self.sim.nodes_["lon-edge-3"]["running"])
        self.assertIn("lon-edge-3", self.lab.load(edge_lab.HELD_EDGES, []))

    def test_a_forwarder_is_replaced_through_its_site_s_dist_c(self):
        code = self.verb("replace", "lon-dist-a", "--gap-s", "5", gap_s=5.0)
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(self.starts(), ["lon-dist-c", "lon-dist-a"])
        self.assertTrue(self.sim.nodes_["lon-dist-a"]["running"])
        # The slot served while lon-dist-a was away, then left the lab.
        self.assertFalse(self.sim.nodes_["lon-dist-c"]["running"])
        self.assertNotIn("lon-dist-c", self.lab.slots_in_service())

    def test_a_new_instance_is_refused(self):
        with self.assertRaises(mesh_ops.Refused):
            self.ops().rebuild("lon-edge-1", "new")


class Cli(unittest.TestCase):
    def test_the_verbs_options(self):
        for command, arguments, wanted in (
                ("roll", ["edges", "--build", "b", "--sites", "london", "--dry-marks"],
                 {"tier": "edges", "build": "b", "sites": "london", "dry_marks": True}),
                ("restart", ["lon-edge-1", "--hold-s", "3"], {"node": "lon-edge-1", "hold_s": 3.0}),
                ("replace", ["lon-dist-a", "--gap-s", "10", "--no-slot"],
                 {"node": "lon-dist-a", "gap_s": 10.0, "no_slot": True})):
            parsed = vars(edge_lab.engine_parser(command).parse_args(arguments))
            self.assertEqual({key: parsed[key] for key in wanted}, wanted, command)
        self.assertIn("roll", edge_lab.ENGINE_VERBS)


if __name__ == "__main__":
    unittest.main()
