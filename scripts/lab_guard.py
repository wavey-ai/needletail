#!/usr/bin/env python3
"""Guards of the local labs of one host (scripts/edge_lab.py).

  lab_guard.py busy                  whether a run holds this lab: prints the
                                     holder and exits 0 when one does, exits
                                     1 when the lab is free
  lab_guard.py outside -- COMMAND... run COMMAND (a build, the tests) on the
                                     host CPUs that no registered lab has

The lab is the one of NEEDLETAIL_EDGE_LAB_ID (or NEEDLETAIL_EDGE_LAB_WORK),
as edge_lab.py names it. A run (edge-campaign.py run, latency-bench.py) holds
`flock` on WORK/.lock for as long as it uses the lab, and waits while another
run holds it: a wrapper waits on its own lab only, never on another agent's
(`while lab_guard.py busy; do sleep 10; done`). A process that a holder
starts (a scenario's own tools) runs inside the holder's hold.

Runs take their binaries from a frozen set (scripts/freeze-binaries.py): a
run refuses a binary that is not in one unless NEEDLETAIL_ALLOW_LIVE=1, so a
build that finishes during a run cannot change what the run measures.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import edge_lab as lab  # noqa: E402

# A run's process tree inside the hold: the state directory it holds.
HELD_ENV = "NEEDLETAIL_EDGE_LAB_HELD"
ALLOW_LIVE_ENV = "NEEDLETAIL_ALLOW_LIVE"
# build.json of a frozen set (freeze-binaries.py).
BIN_SET_SCHEMA = "needletail.bin-set.v1"
# The player scenarios use fixed ports and browser profiles of the host: one
# lab at a time.
PLAYERS_LOCK = "players.lock"
# The frozen sets of this worktree (freeze-binaries.py).
BIN_SETS = lab.ROOT / "target" / "bin-sets"


def lock_path(work: pathlib.Path | None = None) -> pathlib.Path:
    return pathlib.Path(work or lab.WORK) / ".lock"


def holder(work: pathlib.Path | None = None) -> dict | None:
    """Who holds the lab of `work` now, as the holder wrote it, or None
    when no run holds it."""
    path = lock_path(work)
    try:
        handle = open(path, "r")
    except FileNotFoundError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            text = handle.read()
            try:
                return json.loads(text)
            except ValueError:
                # Taken a moment ago and not written yet.
                return {"pid": None, "command": "", "since_unix": None}
        fcntl.flock(handle, fcntl.LOCK_UN)
        return None


def describe(entry: dict) -> str:
    since = entry.get("since_unix")
    when = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since)) if since else "now")
    return (f"pid {entry.get('pid')} ({entry.get('command') or '?'}) of "
            f"{entry.get('owner') or '?'} since {when}")


@contextlib.contextmanager
def hold(label: str, work: pathlib.Path | None = None, out=sys.stderr):
    """Hold the lab of `work` for a run: wait while another run holds it,
    then write who holds it into the lock file. Inside a hold of the same
    lab (a process the holder started) it holds nothing more."""
    work = pathlib.Path(work or lab.WORK)
    resolved = str(work.resolve())
    if os.environ.get(HELD_ENV) == resolved:
        yield
        return
    work.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path(work), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            entry = holder(work) or {}
            print(f"lab {lab.lab_key()} is busy: {describe(entry)}; waiting", file=out, flush=True)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        entry = {"pid": os.getpid(), "command": label, "owner": lab.lab_owner(),
                 "worktree": str(lab.ROOT), "since_unix": time.time()}
        os.ftruncate(descriptor, 0)
        os.pwrite(descriptor, json.dumps(entry).encode(), 0)
        before = os.environ.get(HELD_ENV)
        os.environ[HELD_ENV] = resolved
        try:
            yield
        finally:
            if before is None:
                os.environ.pop(HELD_ENV, None)
            else:
                os.environ[HELD_ENV] = before
            os.ftruncate(descriptor, 0)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def players_lock(out=sys.stderr):
    """One lab of the host at a time runs a player scenario. The player
    scenarios of linode-edge-campaign.py run their players on this host
    through edge_campaign_core.run_scenario, so they take it too, and wait
    without limit while a local lab's player scenario holds it."""
    lab.REGISTRY.mkdir(parents=True, exist_ok=True)
    with open(lab.REGISTRY / PLAYERS_LOCK, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another lab runs a player scenario; waiting", file=out, flush=True)
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


# ------------------------------------------------------------------ CPUs outside the labs

def lab_scopes() -> list[str]:
    """Every lab scope of the user's systemd manager. Refused (LabError)
    when the manager cannot be asked (no user bus: a cron job, a
    non-interactive login), since a lab it runs could then be on any CPU."""
    result = subprocess.run(
        ["systemctl", "--user", "list-units", "--type=scope", "--state=active", "--no-legend",
         "--plain", "needletail-lab-*.scope"], capture_output=True, text=True)
    if result.returncode != 0:
        raise lab.LabError(
            "cannot list the labs of this host from the user's systemd manager "
            f"({result.stderr.strip()[:200] or f'exit {result.returncode}'}): run `outside` "
            "from a login session of the lab's user")
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


def outside_cpus() -> list[int]:
    """The host CPUs that no registered lab has. Refused (LabError) while a
    lab that the registry does not know runs (it may be on any CPU), and
    when the user's systemd manager cannot say which labs run."""
    with lab.registry_lock():
        entries = lab.live_entries()
        known = tuple(f"needletail-lab-{lab.work_tag(pathlib.Path(entry['work']))}-"
                      for entry in entries)
        unknown = [unit for unit in lab_scopes() if not unit.startswith(known)]
    if unknown:
        raise lab.LabError(
            f"a lab that is not registered runs on this host, on any of its CPUs: "
            f"{', '.join(unknown[:4])}{' ...' if len(unknown) > 4 else ''}")
    taken = {cpu for entry in entries for cpu in entry["cpus"]}
    free = [cpu for cpu in range(os.cpu_count() or 1) if cpu not in taken]
    if not free:
        owners = ", ".join(f"{entry['id']} ({lab.cpu_ranges(entry['cpus'])})" for entry in entries)
        raise lab.LabError(f"every CPU of the host is a lab's: {owners}")
    return free


def outside_command(command: list[str]) -> list[str]:
    if not command:
        raise lab.LabError("usage: lab_guard.py outside -- COMMAND...")
    return ["taskset", "-c", lab.cpu_list(outside_cpus()), *command]


# ------------------------------------------------------------------ frozen binaries

def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def frozen(path: pathlib.Path) -> bool:
    """Whether `path` is a binary of a frozen set, as it was frozen."""
    path = pathlib.Path(path).resolve()
    try:
        build = json.loads((path.parent / "build.json").read_text())
        recorded = build["binaries"][path.name]
        return (build.get("schema") == BIN_SET_SCHEMA
                and recorded["sha256"] == sha256(path))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def lab_binaries() -> dict[str, pathlib.Path]:
    """The binaries that edge_lab.py's `up` takes, by label (set b's only
    when one is given; the load tool only when it is built)."""
    out = {"av-mesh": lab.AV_MESH, "needletail-reader": lab.READER, "av-contrib": lab.AV_CONTRIB,
           "aep1-48k-probe": lab.PROBE}
    if lab.LOAD_BIN.is_file():
        out["needletail-load"] = lab.LOAD_BIN
    if lab.AV_MESH_NEXT:
        out["av-mesh (set b)"] = lab.AV_MESH_NEXT
    if lab.AV_CONTRIB_NEXT:
        out["av-contrib (set b)"] = lab.AV_CONTRIB_NEXT
    return out


def running_binaries() -> dict[str, pathlib.Path]:
    """The binaries the running lab runs: the copies that its `up` took
    into WORK/bin/<set> (edge_lab.snapshot_binaries), by label."""
    out = {}
    for build, paths in sorted(lab.load("binaries.json").items()):
        for label, path in paths.items():
            out[label if build == "a" else f"{label} (set {build})"] = pathlib.Path(path)
    return out


def recorded_sha256s() -> set[str]:
    """The sha256 of every binary of a frozen set: the sets of this
    worktree (target/bin-sets) and the sets that the environment's
    binaries are in."""
    directories = {*BIN_SETS.glob("*"),
                   *(pathlib.Path(path).resolve().parent for path in lab_binaries().values())}
    out = set()
    for directory in directories:
        try:
            build = json.loads((directory / "build.json").read_text())
            if build.get("schema") == BIN_SET_SCHEMA:
                out.update(str(item["sha256"]) for item in build["binaries"].values())
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return out


def require_frozen(binaries: dict[str, pathlib.Path], environ=os.environ,
                   running: dict[str, pathlib.Path] | None = None) -> None:
    """Refuse a run whose binaries are not all in frozen sets, unless
    NEEDLETAIL_ALLOW_LIVE=1. `binaries` are the paths a run takes (from
    the environment), each frozen where it lies; `running` are the copies a
    running lab runs (running_binaries(): a run that does not `up` the lab),
    each frozen when its bytes are a frozen set's."""
    if environ.get(ALLOW_LIVE_ENV) == "1":
        return
    live = [f"{label} {path}" for label, path in binaries.items() if not frozen(path)]
    if running:
        recorded = recorded_sha256s()
        for label, path in running.items():
            try:
                if sha256(path) in recorded:
                    continue
            except OSError:
                pass
            live.append(f"{label} {path} (the running lab's: `up` it again from a frozen set)")
    if live:
        raise SystemExit(
            "these binaries are not in a frozen set (a build could change them during the "
            "run): " + "; ".join(live) + ". Freeze them (`scripts/freeze-binaries.py LABEL`, "
            f"then its exports), or {ALLOW_LIVE_ENV}=1")


def main(argv: list[str]) -> int:
    if argv[:1] == ["busy"] and len(argv) == 1:
        entry = holder()
        if entry is None:
            print(f"lab {lab.lab_key()} is free")
            return 1
        print(f"lab {lab.lab_key()} is held: {describe(entry)}")
        return 0
    if argv[:1] == ["outside"]:
        command = argv[2:] if argv[1:2] == ["--"] else argv[1:]
        try:
            argv_out = outside_command(command)
        except lab.LabError as error:
            print(error, file=sys.stderr)
            return 2
        os.execvp(argv_out[0], argv_out)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
