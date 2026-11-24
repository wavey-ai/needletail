"""A run tool's run in the run archive (scripts/run_archive.py,
~/needletail-data/<run id>/, NEEDLETAIL_DATA_ROOT overrides the root).

Every local and Linode run tool keeps its raw data there as it runs, not
only under target/ (owner, 23 Nov: "keep all the data"). This module is
the one way the tools do it:

- `default_run_id(tool)`: `<tool>-<UTC yyyymmddThhmmssZ>`, when the tool
  is given no --run-id.
- `code_paths(mesh_root, contrib_root)`: the worktrees of the five
  repositories a run uses, whose heads the manifest records.
- `Run(...)`: the run, made (RunArchive.create) or, when the id is a run
  already, opened to append to (RunArchive.open: another tool's run, such
  as a lab's or a campaign's). Its writes never raise: a write that fails
  is said on stderr and kept in `problems`, and a run that lost a write
  ends failed. As a context manager it runs its close steps (the copies
  that must be made however the run ends) and then ends the run: failed
  with the exception's name when one ends the block, else as the tool
  said (`fail`). A run this tool did not make, or one made with
  finish=False (a Linode lab's run, which its collect and down end), is
  not ended here: the tool's outcome is appended to `outcomes.ndjson`
  under its prefix instead.
- `LogSlices`: the local lab's node logs (edge_lab's WORK/logs) copied
  into the run as slices of what each gained since the last slice; the
  lab's `up` deletes its logs, so a tool takes a slice before each `up`
  and at the end of each piece of work.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import sys
import tempfile
import time

import run_archive as ra

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
# The environment that names a repository's worktree, as the tools read it.
ROOT_ENV = {"av-mesh": "NEEDLETAIL_AV_MESH_ROOT", "av-contrib": "NEEDLETAIL_AV_CONTRIB_ROOT",
            "playlists": "NEEDLETAIL_PLAYLISTS_ROOT", "daw-nexus": "NEEDLETAIL_DAW_NEXUS_ROOT"}


def default_run_id(tool: str, now: float | None = None) -> str:
    return f"{tool}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}"


def code_paths(mesh_root, contrib_root) -> list[pathlib.Path]:
    """This checkout, the av-mesh and av-contrib worktrees the tool takes
    its binaries from, and the playlists and daw-nexus worktrees beside
    them: NEEDLETAIL_PLAYLISTS_ROOT and NEEDLETAIL_DAW_NEXUS_ROOT, else the
    sibling of the av-mesh worktree with its suffix (av-mesh-features ->
    playlists-features, av-mesh -> playlists). A path that is not a
    worktree is recorded with an error by the archive, never guessed."""
    mesh = pathlib.Path(mesh_root)
    suffix = mesh.name[len("av-mesh"):] if mesh.name.startswith("av-mesh") else ""

    def beside(repo: str) -> pathlib.Path:
        given = os.environ.get(ROOT_ENV[repo])
        return pathlib.Path(given) if given else mesh.parent / f"{repo}{suffix}"

    return [ROOT, mesh, pathlib.Path(contrib_root), beside("playlists"), beside("daw-nexus")]


def local_topology(nodes) -> tuple[dict, list[str]]:
    """The local namespace lab's nodes (edge_lab.NODES: name, role, site,
    region, addresses) as a topology for the manifest, and its regions."""
    listed = [{"name": name, "role": role, "site": site, "region": region}
              for name, role, site, region, *_addresses in nodes]
    return {"nodes": listed}, sorted({item["region"] for item in listed if item["region"]})


def fresh_files(directory: pathlib.Path, since_s: float) -> list[pathlib.Path]:
    """The regular files under `directory` written at `since_s` or later
    (not what an earlier pass left there); symlinks are left out."""
    out = []
    for path in sorted(directory.rglob("*")):
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime >= since_s:
                out.append(path)
        except OSError:
            continue
    return out


class Run:
    """One tool's run in the archive (see the module's text)."""

    def __init__(self, run_id: str, kind: str, tool: str, *, code=(), settings: dict | None = None,
                 topology=None, topology_nodes: list[dict] | None = None, regions=None,
                 prefix: str = "", finish: bool = True, argv: list[str] | None = None,
                 root=None):
        self.root = pathlib.Path(root).resolve() if root else ra.data_root()
        self.tool = tool
        self.prefix = prefix
        self.problems: list[str] = []
        self.closers: list = []
        self.status = "complete"
        self.reason: str | None = None
        self.made = not (self.root / run_id / ra.MANIFEST).is_file()
        if self.made:
            self.archive = ra.RunArchive.create(
                run_id, kind, tool=tool, argv=argv, code_paths=code, deploy_settings=settings,
                topology=topology, topology_nodes=topology_nodes, regions=regions, root=self.root)
        else:
            self.archive = ra.RunArchive.open(run_id, tool=tool, argv=argv, root=self.root)
            if settings is not None:
                self.write_json("settings.json", ra.scrub_obj(settings), "settings",
                                f"{tool}'s settings in this run")
            if code:
                self.write_json("code-heads.json", ra.code_heads(code), "settings",
                                f"the code heads of {tool}'s worktrees")
        self.run_id = run_id
        self.finish_run = finish and self.made

    @property
    def path(self) -> pathlib.Path:
        return self.archive.path

    # -- writes: never raise; a failure is kept and fails the run

    def guarded(self, what: str, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ra.ArchiveError, OSError, ValueError, TypeError) as error:
            self.problems.append(f"{what}: {error}")
            print(f"run archive {self.run_id}: {what} failed: {error}", file=sys.stderr, flush=True)
            return None

    def rel(self, dest: str) -> str:
        return f"{self.prefix}{dest}"

    def add_file(self, src, dest: str, kind: str = "raw", description: str = "", units=None,
                 move: bool = False) -> str | None:
        """src into the run at dest (under the prefix); nothing when src
        does not exist (a step that never wrote it)."""
        if not pathlib.Path(src).is_file():
            return None
        return self.guarded(f"add {src}", self.archive.add_file, src, self.rel(dest), kind,
                            description, move=move, units=units)

    def add_tree(self, src, dest: str, kind: str = "raw", description: str = "", units=None,
                 since_s: float | None = None, move: bool = False) -> dict | None:
        """Every file under src into the run at dest, keeping the layout;
        with since_s, only those written since then (a working directory
        that a tool reuses holds an earlier pass's files too). Nothing when
        src is not a directory."""
        src = pathlib.Path(src)
        if not src.is_dir():
            return None
        if since_s is not None:
            added = [self.add_file(path, f"{dest}/{path.relative_to(src).as_posix()}", kind,
                                   description, units, move=move)
                     for path in fresh_files(src, since_s)]
            return {"added": [path for path in added if path], "skipped": [], "refused": []}
        result = self.guarded(f"add {src}/", self.archive.add_tree, src, self.rel(dest), kind,
                              description, units=units, move=move)
        if result and result["refused"]:
            self.problems.append(f"{src}: credential files refused: {', '.join(result['refused'])}")
        return result

    def write_json(self, dest: str, obj, kind: str = "result", description: str = "",
                   units=None) -> str | None:
        return self.guarded(f"write {dest}", self.archive.write_json, self.rel(dest), obj, kind,
                            description, units=units)

    def append_ndjson(self, dest: str, records, kind: str = "samples", description: str = "",
                      units=None) -> str | None:
        return self.guarded(f"append to {dest}", self.archive.append_ndjson, self.rel(dest),
                            records, kind, description, units=units)

    def record_step(self, *args, **kwargs) -> dict | None:
        return self.guarded("record a listener step", self.archive.record_step, *args, **kwargs)

    # -- the end

    def fail(self, reason: str) -> None:
        """The run ends failed, with this reason (the first one given)."""
        if self.status != "failed":
            self.status, self.reason = "failed", reason

    def at_close(self, function) -> None:
        """A copy made when the run closes, however it ends."""
        self.closers.append(function)

    def close(self, error: BaseException | None = None) -> None:
        for function in self.closers:
            self.guarded("a close step", function)
        self.closers.clear()
        if error is not None:
            self.fail(f"{type(error).__name__}" + (f": {error}" if str(error) else ""))
        if self.problems:
            self.fail("the archive copy is incomplete: " + "; ".join(self.problems)[-1500:])
        if self.finish_run:
            self.guarded("finish", self.archive.finish, self.status, self.reason)
        else:
            self.append_ndjson("outcomes.ndjson", {
                "tool": self.tool, "status": self.status, "reason": self.reason,
                "unix_us": time.time_ns() // 1000}, "result",
                "each tool's outcome in a run it did not make")

    def __enter__(self) -> "Run":
        return self

    def __exit__(self, kind, error, _trace) -> bool:
        self.close(error)
        return False


def head_digest(path: pathlib.Path, length: int) -> str | None:
    """sha256 of a file's first `length` bytes; None when it cannot be read
    or is shorter."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(length)
    except OSError:
        return None
    return hashlib.sha256(head).hexdigest() if len(head) == length else None


class LogSlices:
    """The local lab's node logs into a run, slice by slice: each slice is
    what a log gained since the last one. A log that was made again (the
    lab's `up` deletes its logs and the services make them anew) is copied
    from its start. It is known as the same log only when its inode, its
    length (no shorter) and its first HEAD_BYTES bytes (or all of them, when
    it was shorter) are what they were: ext4 hands a deleted log's inode
    number to the next new file, so the inode alone does not tell."""

    HEAD_BYTES = 4096

    def __init__(self, directory, pattern: str = "*"):
        self.directory = pathlib.Path(directory)
        self.pattern = pattern
        # name -> (inode, offset, sha256 of the first min(offset, HEAD_BYTES) bytes)
        self.seen: dict[str, tuple[int, int, str | None]] = {}

    def remember(self, path: pathlib.Path, inode: int, offset: int) -> None:
        self.seen[path.name] = (inode, offset,
                                head_digest(path, min(offset, self.HEAD_BYTES)))

    def start_of(self, path: pathlib.Path, stat: os.stat_result) -> int:
        """Where this log's next slice starts: the offset of the last one,
        or 0 when it is another log under the same name."""
        inode, offset, digest = self.seen.get(path.name, (None, 0, None))
        if inode != stat.st_ino or offset > stat.st_size:
            return 0
        return offset if head_digest(path, min(offset, self.HEAD_BYTES)) == digest else 0

    def mark(self) -> None:
        """Start the next slice at the logs as they are now."""
        for path in sorted(self.directory.glob(self.pattern)):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                self.remember(path, stat.st_ino, stat.st_size)

    def take(self, run: Run, dest: str) -> list[str]:
        """Copy each log's new bytes into the run at dest/<log name>."""
        added = []
        with tempfile.TemporaryDirectory(prefix="needletail-log-slice-") as staging:
            for path in sorted(self.directory.glob(self.pattern)):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file():
                    continue
                start = self.start_of(path, stat)
                if stat.st_size <= start:
                    continue
                piece = pathlib.Path(staging) / path.name
                with open(path, "rb") as source, open(piece, "wb") as out:
                    source.seek(start)
                    shutil.copyfileobj(source, out)
                    end = start + out.tell()
                self.remember(path, stat.st_ino, end)
                used = run.add_file(piece, f"{dest}/{path.name}", "node-logs",
                                    f"what the node's log gained in this part of the run "
                                    f"(bytes {start} to {end} of {path})", move=True)
                if used:
                    added.append(used)
        return added

