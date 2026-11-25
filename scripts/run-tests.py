#!/usr/bin/env python3
"""Run the script test suites so that they leave nothing in the temp root.

Each discovery (scripts/ and scripts/compare/) runs with TMPDIR set to a
fresh directory of its own inside the worktree's target/ (never /tmp: it
is a RAM tmpfs shared by every session on the dev box, and test runs that
left their directories filled it). After the run the directory must be
empty: every test removes what it made, pass or fail, and so does the code
it drives through `tempfile`. Exit status: the discoveries' own, or 3 when
either left anything (listed, with sizes; the directory is kept for a
look, under target/test-tmp/).

    scripts/run-tests.py                 # both discoveries
    scripts/run-tests.py scripts         # one
    scripts/run-tests.py -- -k Drain     # extra unittest arguments

NEEDLETAIL_AV_MESH_ROOT and NEEDLETAIL_COMPARE_ROOT pass through as given.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SUITES = ("scripts", "scripts/compare")
EXIT_LEFT = 3


def size(path: pathlib.Path) -> int:
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    return sum(item.lstat().st_size for item in path.rglob("*") if not item.is_dir())


def leftovers(directory: pathlib.Path) -> list[str]:
    """What a run left in its temp root, one line an entry."""
    return [f"{item.name} ({size(item) / 1e6:.1f} MB)" for item in sorted(directory.iterdir())]


def run(suite: str, extra: list[str]) -> tuple[int, list[str], pathlib.Path]:
    """One discovery with its own temp root: (exit status, what it left,
    the root)."""
    base = ROOT / "target" / "test-tmp"
    base.mkdir(parents=True, exist_ok=True)
    directory = base / f"{suite.replace('/', '-')}-{os.getpid()}-{int(time.time())}"
    directory.mkdir()
    environment = {**os.environ, "TMPDIR": str(directory), "TEMP": str(directory),
                   "TMP": str(directory)}
    code = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", suite,
                           "-p", "test_*.py", *extra], cwd=ROOT, env=environment).returncode
    left = leftovers(directory)
    if not left:
        directory.rmdir()
    return code, left, directory


def main(argv: list[str]) -> int:
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    suites = (argv[:argv.index("--")] if "--" in argv else argv) or list(SUITES)
    worst = 0
    for suite in suites:
        code, left, directory = run(suite, extra)
        if left:
            print(f"{suite}: the run left {len(left)} entr{'y' if len(left) == 1 else 'ies'} in "
                  f"{directory}:\n  " + "\n  ".join(left[:50])
                  + ("\n  ..." if len(left) > 50 else ""), file=sys.stderr)
            worst = max(worst, EXIT_LEFT if code == 0 else code)
        else:
            print(f"{suite}: nothing left in its temp root", file=sys.stderr)
            worst = max(worst, code)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
