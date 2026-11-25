#!/usr/bin/env python3
"""scripts/run-tests.py, the way to run the offline suites: each discovery
with a temp root of its own under target/test-tmp/, refused (exit 3) when
it leaves anything there. python3 -m unittest scripts/test_run_tests.py"""

from __future__ import annotations

import importlib.util
import io
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("run_tests", HERE / "run-tests.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.object(runner, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, leave: bool, code: int = 0):
        seen = {}

        def run(command, cwd=None, env=None):
            seen.update(command=command, env=env)
            if leave:
                (pathlib.Path(env["TMPDIR"]) / "tmpabc").mkdir()
            return subprocess.CompletedProcess(command, code)
        return seen, run

    def test_a_clean_run_leaves_no_root_and_keeps_its_status(self):
        seen, run = self.fake(leave=False, code=1)
        with mock.patch.object(runner.subprocess, "run", run), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(["scripts", "--", "-k", "Drain"]), 1)
        temp = pathlib.Path(seen["env"]["TMPDIR"])
        self.assertEqual(temp.parent, self.root / "target" / "test-tmp")
        self.assertEqual(seen["env"]["TMP"], seen["env"]["TEMP"])
        self.assertFalse(temp.exists())
        self.assertEqual(seen["command"][-2:], ["-k", "Drain"])
        self.assertIn("scripts", seen["command"])

    def test_a_run_that_leaves_anything_fails_and_names_it(self):
        _seen, run = self.fake(leave=True)
        err = io.StringIO()
        with mock.patch.object(runner.subprocess, "run", run), redirect_stderr(err):
            self.assertEqual(runner.main(["scripts/compare"]), runner.EXIT_LEFT)
        self.assertIn("scripts/compare: the run left 1 entry", err.getvalue())
        self.assertIn("tmpabc", err.getvalue())

    def test_both_suites_by_default(self):
        commands = []

        def run(command, cwd=None, env=None):
            commands.append(command[command.index("-s") + 1])
            return subprocess.CompletedProcess(command, 0)
        with mock.patch.object(runner.subprocess, "run", run), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main([]), 0)
        self.assertEqual(commands, ["scripts", "scripts/compare"])


if __name__ == "__main__":
    unittest.main()
