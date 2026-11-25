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
Without NEEDLETAIL_AV_MESH_ROOT (integration 6) the first of these that
holds a built av-mesh is taken: the av-mesh worktree beside this one
(rm/av-mesh-<name> for rm/needletail-<name>), the workspace's
av-mesh-resilient, the workspace's av-mesh. NEEDLETAIL_AV_CONTRIB_ROOT the
same way. When the av-mesh found is too old for a flag the tests render
(REQUIRED_MESH_FLAGS), the run says so and sets
NEEDLETAIL_AV_MESH_TOO_OLD to the reason: the tests that render against
the real binary skip with it instead of failing.
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
# The flags of the deploy's default render that an av-mesh too old for them
# lacks (the newest first): such a binary makes the render's tests skip.
REQUIRED_MESH_FLAGS = ("--send-defer-ms", "--heavy-egress-max-mbps", "--relay-primary-promoted",
                       "--relay-part-repair", "--node-warm-file")


def workspace() -> pathlib.Path:
    """The directory of the repositories: the parent of this checkout, or
    of rm/ for a worktree under it."""
    parent = ROOT.parent
    return parent.parent if parent.name == "rm" else parent


def candidates(repo: str) -> list[pathlib.Path]:
    """Where `repo` ("av-mesh", "av-contrib") is looked for, in order."""
    out = []
    if ROOT.parent.name == "rm" and ROOT.name.startswith("needletail-"):
        out.append(ROOT.parent / f"{repo}-{ROOT.name[len('needletail-'):]}")
    out += [workspace() / f"{repo}-resilient", workspace() / repo]
    return out


def built(root: pathlib.Path, binary: str) -> pathlib.Path | None:
    path = root / "target" / "release" / binary
    return path if path.is_file() else None


def mesh_flags(binary: pathlib.Path) -> set[str] | None:
    try:
        result = subprocess.run([str(binary), "--help"], capture_output=True, text=True,
                                timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {word for word in result.stdout.split() if word.startswith("--")}


def too_old(binary: pathlib.Path) -> str | None:
    """Why `binary` is too old for the tests' render, or None."""
    flags = mesh_flags(binary)
    if flags is None:
        return f"{binary} --help does not run here"
    lacking = [flag for flag in REQUIRED_MESH_FLAGS if flag not in flags]
    return f"{binary} lacks {', '.join(lacking)}" if lacking else None


def default_roots(environment: dict) -> dict:
    """NEEDLETAIL_AV_MESH_ROOT and NEEDLETAIL_AV_CONTRIB_ROOT when not given,
    and NEEDLETAIL_AV_MESH_TOO_OLD when the av-mesh taken is too old."""
    out = {}
    if "NEEDLETAIL_AV_MESH_ROOT" not in environment:
        found = [root for root in candidates("av-mesh") if built(root, "av-mesh")]
        fresh = [root for root in found if too_old(built(root, "av-mesh")) is None]
        if fresh or found:
            out["NEEDLETAIL_AV_MESH_ROOT"] = str((fresh or found)[0])
    root = out.get("NEEDLETAIL_AV_MESH_ROOT", environment.get("NEEDLETAIL_AV_MESH_ROOT"))
    binary = built(pathlib.Path(root), "av-mesh") if root else None
    reason = too_old(binary) if binary else f"no built av-mesh under {root or 'any root'}"
    if reason:
        out["NEEDLETAIL_AV_MESH_TOO_OLD"] = reason
    if "NEEDLETAIL_AV_CONTRIB_ROOT" not in environment:
        found = [root for root in candidates("av-contrib") if built(root, "av-contrib")]
        if found:
            out["NEEDLETAIL_AV_CONTRIB_ROOT"] = str(found[0])
    return out


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
    environment.update(default_roots(environment))
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
    chosen = default_roots(dict(os.environ))
    for key in ("NEEDLETAIL_AV_MESH_ROOT", "NEEDLETAIL_AV_CONTRIB_ROOT"):
        print(f"{key}={os.environ.get(key) or chosen.get(key, '(none)')}"
              + ("" if key in os.environ else " (default)"), file=sys.stderr)
    if chosen.get("NEEDLETAIL_AV_MESH_TOO_OLD"):
        print("the tests that render against the real av-mesh skip: "
              + chosen["NEEDLETAIL_AV_MESH_TOO_OLD"], file=sys.stderr)
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
