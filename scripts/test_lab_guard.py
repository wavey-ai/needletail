#!/usr/bin/env python3
"""Tests of lab_guard.py (a run's hold of its lab, `busy`, `outside`, the
players lock, the frozen-binary check) and freeze-binaries.py. Nothing of a
lab is started.
python3 -m unittest scripts/test_lab_guard.py"""

from __future__ import annotations

import fcntl
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402
import lab_guard as guard  # noqa: E402

_spec = importlib.util.spec_from_file_location("freeze_binaries", HERE / "freeze-binaries.py")
freeze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(freeze)


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.work = self.dir / "elab-t"
        patchers = [mock.patch.object(lab, "WORK", self.work),
                    mock.patch.object(lab, "REGISTRY", self.dir / "registry"),
                    mock.patch.object(lab, "LAB_ID", "t")]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        held = os.environ.pop(guard.HELD_ENV, None)
        if held is not None:
            self.addCleanup(os.environ.__setitem__, guard.HELD_ENV, held)

    def child(self, code: str, env_extra: dict | None = None) -> subprocess.Popen:
        """A Python process that imports lab_guard for this test's lab."""
        env = {key: value for key, value in os.environ.items() if key != guard.HELD_ENV}
        env.update({"NEEDLETAIL_EDGE_LAB_WORK": str(self.work), "NEEDLETAIL_EDGE_LAB_ID": "t",
                    "XDG_RUNTIME_DIR": str(self.dir)}, **(env_extra or {}))
        program = f"import sys; sys.path.insert(0, {str(HERE)!r}); import lab_guard as guard\n"
        return subprocess.Popen([sys.executable, "-c", program + textwrap.dedent(code)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class Hold(Temp):
    def test_busy_names_the_holder_and_a_free_lab_is_free(self):
        self.assertIsNone(guard.holder(self.work))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(guard.main(["busy"]), 1)
        self.assertIn("lab t is free", out.getvalue())
        with guard.hold("latency-bench.py --name x"):
            entry = guard.holder(self.work)
            self.assertEqual(entry["pid"], os.getpid())
            self.assertEqual(entry["command"], "latency-bench.py --name x")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(guard.main(["busy"]), 0)
            self.assertIn(f"pid {os.getpid()} (latency-bench.py --name x)", out.getvalue())
        self.assertIsNone(guard.holder(self.work))

    def test_a_second_run_of_the_lab_waits_for_the_first(self):
        with guard.hold("first"):
            waiting = self.child("""
                with guard.hold("second"):
                    print("held")
            """)
            time.sleep(1.0)
            self.assertIsNone(waiting.poll(), "the second run went on while the first held the lab")
        out, err = waiting.communicate(timeout=20)
        self.assertEqual(out.strip(), "held")
        self.assertIn("lab t is busy: pid", err)
        self.assertIn("(first)", err)

    def test_a_process_the_holder_starts_runs_inside_the_hold(self):
        with guard.hold("first"):
            child = self.child("""
                with guard.hold("a tool of the run"):
                    print("inside")
            """, {guard.HELD_ENV: os.environ[guard.HELD_ENV]})
            out, _ = child.communicate(timeout=20)
        self.assertEqual(out.strip(), "inside")

    def test_another_lab_is_not_waited_for(self):
        with guard.hold("first"):
            other = self.child("""
                with guard.hold("other lab"):
                    print("held")
            """, {"NEEDLETAIL_EDGE_LAB_WORK": str(self.dir / "elab-u"),
                  "NEEDLETAIL_EDGE_LAB_ID": "u"})
            out, _ = other.communicate(timeout=20)
        self.assertEqual(out.strip(), "held")

    def test_one_lab_at_a_time_runs_the_players(self):
        with guard.players_lock():
            handle = open(lab.REGISTRY / guard.PLAYERS_LOCK, "a+")
            self.addCleanup(handle.close)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


class Outside(Temp):
    def register(self, ident: str, cpus: list[int]) -> None:
        lab.REGISTRY.mkdir(parents=True, exist_ok=True)
        (lab.REGISTRY / f"{ident}.json").write_text(json.dumps(
            {"id": ident, "owner": ident, "pid": os.getpid(), "worktree": "/w",
             "work": str(self.dir / f"elab-{ident}"), "cpus": cpus, "since_unix": time.time()}))

    @unittest.skipUnless((os.cpu_count() or 1) >= 4, "needs four CPUs")
    def test_outside_runs_on_no_registered_cpu(self):
        total = os.cpu_count()
        self.register("a", [0, 1])
        self.register("b", [total - 1])
        with mock.patch.object(lab, "active_scopes", return_value=[]):
            command = guard.outside_command(
                [sys.executable, "-c", "import os; print(sorted(os.sched_getaffinity(0)))"])
        self.assertEqual(command[:2], ["taskset", "-c"])
        ran = subprocess.run(command, capture_output=True, text=True, check=True)
        cpus = set(json.loads(ran.stdout))
        self.assertTrue(cpus)
        self.assertFalse(cpus & {0, 1, total - 1})
        self.assertEqual(cpus, set(range(2, total - 1)))

    def test_a_lab_the_registry_does_not_know_may_be_on_any_cpu(self):
        with mock.patch.object(lab, "active_scopes",
                               return_value=["needletail-lab-edge-lab-0123abcd-lon-edge-1.scope"]):
            with self.assertRaisesRegex(lab.LabError, "not registered"):
                guard.outside_cpus()
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(guard.main(["outside", "--", "true"]), 2)
            self.assertIn("not registered", err.getvalue())

    def test_a_registered_lab_s_own_scopes_are_known(self):
        self.register("a", [0])
        tag = lab.work_tag(self.dir / "elab-a")
        with mock.patch.object(lab, "active_scopes",
                               return_value=[f"needletail-lab-{tag}-lon-edge-1.scope"]):
            self.assertNotIn(0, guard.outside_cpus())

    def test_no_cpu_left_is_refused(self):
        self.register("a", list(range(os.cpu_count() or 1)))
        with mock.patch.object(lab, "active_scopes", return_value=[]):
            with self.assertRaisesRegex(lab.LabError, "every CPU of the host is a lab's"):
                guard.outside_cpus()


class Frozen(Temp):
    def binaries(self) -> tuple:
        built = self.dir / "built"
        built.mkdir(exist_ok=True)
        out = []
        for name, _source, variable in freeze.BINARIES:
            path = built / name
            path.write_text(f"#!/bin/sh\necho '{name} 1.0 (abc123)'\n")
            path.chmod(0o755)
            out.append((name, path, variable))
        return tuple(out)

    def test_freeze_copies_the_six_with_their_versions_and_prints_the_exports(self):
        binaries = self.binaries()
        target = freeze.freeze("w24", binaries, self.dir / "sets")
        self.assertEqual(target, self.dir / "sets" / "w24")
        build = json.loads((target / "build.json").read_text())
        self.assertEqual(build["schema"], guard.BIN_SET_SCHEMA)
        self.assertEqual(sorted(build["binaries"]), sorted(name for name, *_ in binaries))
        self.assertEqual(build["binaries"]["av-mesh"]["version"], "av-mesh 1.0 (abc123)")
        self.assertEqual(len(binaries), 6)
        lines = freeze.exports(target, binaries)
        self.assertIn(f"export NEEDLETAIL_AV_MESH_BIN={target / 'av-mesh'}", lines)
        self.assertIn(f"export NEEDLETAIL_UDP_CLIENT_BIN={target / 'needletail-udp-client'}", lines)
        self.assertEqual(freeze.exports(target, binaries, next_set=True),
                         [f"export NEEDLETAIL_AV_MESH_BIN_NEXT={target / 'av-mesh'}",
                          f"export NEEDLETAIL_AV_CONTRIB_BIN_NEXT={target / 'av-contrib'}"])
        self.assertEqual(sorted(path.name for path in (self.dir / "sets").iterdir()), ["w24"])

    def test_a_set_is_never_written_twice_and_needs_every_binary(self):
        binaries = self.binaries()
        freeze.freeze("w24", binaries, self.dir / "sets")
        with self.assertRaisesRegex(SystemExit, "never written again"):
            freeze.freeze("w24", binaries, self.dir / "sets")
        (self.dir / "built" / "needletail-load").unlink()
        with self.assertRaisesRegex(SystemExit, "not built: needletail-load"):
            freeze.freeze("w25", binaries, self.dir / "sets")
        self.assertFalse((self.dir / "sets" / "w25").exists())

    def test_a_run_refuses_a_binary_outside_a_frozen_set(self):
        binaries = self.binaries()
        target = freeze.freeze("w24", binaries, self.dir / "sets")
        guard.require_frozen({"av-mesh": target / "av-mesh"}, environ={})
        with self.assertRaisesRegex(SystemExit, "not in a frozen set.*av-mesh"):
            guard.require_frozen({"av-mesh": self.dir / "built" / "av-mesh"}, environ={})
        guard.require_frozen({"av-mesh": self.dir / "built" / "av-mesh"},
                             environ={guard.ALLOW_LIVE_ENV: "1"})
        # A frozen binary that changed since is not frozen any more.
        changed = target / "av-contrib"
        changed.chmod(0o755)
        changed.write_text("#!/bin/sh\necho other\n")
        with self.assertRaisesRegex(SystemExit, "av-contrib"):
            guard.require_frozen({"av-contrib": changed}, environ={})


if __name__ == "__main__":
    unittest.main()
