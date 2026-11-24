#!/usr/bin/env python3
"""The run archive: every local and Linode run keeps all its raw data in
~/needletail-data/<run id>/ (NEEDLETAIL_DATA_ROOT overrides the root),
outside any repo or target directory, with a manifest.json that says what
the run was and what every file holds. docs/run-archive.md has the fields.

Rules this module keeps:
- Nothing here deletes or truncates a file inside the root. Files are only
  created (never over an existing one: a different file at the same name is
  written as name.1.ext, name.2.ext) or appended to (NDJSON). The manifest is
  the one file that is rewritten, atomically (temp file, then rename), at
  every update, so a crashed run still has a valid one.
- A run id is a single path component; it is refused if it would escape the
  root, or if it names a run made by a different tool or of another kind.
  The same tool reopening its run, and any tool opening it with open(),
  appends to it.
- Credential files are refused, and secrets are scrubbed from the command
  lines and deploy settings recorded in the manifest.

Library: RunArchive.create(...) / RunArchive.open(...), then add_file,
add_tree, write_json, append_ndjson, set_topology, record_step, set_lab,
mark_collected, finish; require_collected(run_id) for a lab's `down`.
CLI: run_archive.py create|add|add-tree|step|lab|collected|finish|show|list|
verify|check-collected (see --help). Standard library only."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import time

SCHEMA_VERSION = 1
MANIFEST = "manifest.json"
LOCK = ".manifest.lock"
KINDS = ("local-lab", "linode-lab", "analysis", "legacy")
STATUSES = ("running", "complete", "failed")
HASH_LIMIT_BYTES = int(os.environ.get("NEEDLETAIL_DATA_HASH_LIMIT_BYTES", str(4 << 30)))
TREE_BATCH_FILES = 500
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SCRUBBED = "[scrubbed]"


class ArchiveError(Exception):
    """A refusal: the archive was left as it was."""


def data_root() -> pathlib.Path:
    """The archive root: NEEDLETAIL_DATA_ROOT, else ~/needletail-data."""
    raw = os.environ.get("NEEDLETAIL_DATA_ROOT") or "~/needletail-data"
    return pathlib.Path(raw).expanduser().resolve()


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- secrets

# Upper-case variable names that hold secrets: *TOKEN*, *SECRET*, *PASSWORD*,
# *KEY*, *_PAT*, PAT.
_SECRET_NAME = r"(?:[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|_PAT)[A-Z0-9_]*|PAT)"
_SECRET_ASSIGN = re.compile(r"(?m)^\s*(?:export\s+)?(" + _SECRET_NAME + r")\s*=\s*\S")
_SECRET_KEY_RE = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|credential|^pat$|_pat$|authorization)")
_PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|PuTTY-User-Key-File")
_CREDENTIAL_NAMES = {"linode_pat", "linode_token", ".netrc", ".git-credentials", "credentials",
                     "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk"}
_CREDENTIAL_GLOBS = ("*_pat", "*.pem", "*.key", "*.p12", "*.pfx", "id_*_wavey", "*credentials*.json")
_TOKEN_SHAPES = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9a-fA-F]{64,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)"
    r"(?![A-Za-z0-9])")


def credential_reason(path: pathlib.Path, head: bytes | None = None) -> str | None:
    """Why the file looks like a credential file, or None. Reads only the
    first kilobyte."""
    name = path.name.lower()
    if name.endswith(".pub"):
        return None
    if name in _CREDENTIAL_NAMES or any(fnmatch.fnmatch(name, g) for g in _CREDENTIAL_GLOBS):
        return f"its name ({path.name}) is a credential file's"
    if head is None:
        try:
            with open(path, "rb") as handle:
                head = handle.read(1024)
        except OSError:
            head = b""
    if _PRIVATE_KEY.search(head):
        return "it holds a private key"
    text = head.decode("utf-8", "replace")
    if (name.endswith(".env") or name.startswith(".env") or ".env." in name) and _SECRET_ASSIGN.search(text):
        return "it is an env file that sets a key, secret or token"
    stripped = text.strip()
    if stripped and "\n" not in stripped and _TOKEN_SHAPES.fullmatch(stripped):
        return "it holds a single token-shaped value"
    lines = text.splitlines()[:-1] if len(head) == 1024 else text.splitlines()
    if _SECRET_ASSIGN.search(text) and lines and all(
            re.match(r"^\s*(#.*|(export\s+)?[A-Za-z_][A-Za-z0-9_]*=.*|)$", line) for line in lines):
        return "it is an env-style file (only NAME=value lines) that sets a key, secret or token"
    return None


def scrub_text(text: str) -> str:
    text = re.sub(r"(?i)(authorization\s*:\s*)(?:(bearer|basic|token)\s+)?\S+",
                  lambda m: m.group(1) + (m.group(2) + " " if m.group(2) else "") + SCRUBBED, text)
    text = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 " + SCRUBBED, text)
    text = re.sub(r"(?i)(://[^/\s:@]+:)[^/\s@]+@", r"\1" + SCRUBBED + "@", text)
    text = re.sub(r"(?<![A-Za-z0-9_])(" + _SECRET_NAME + r"\s*=\s*)[^\s&;]+", r"\1" + SCRUBBED, text)
    text = re.sub(r"(?i)([?&](?:token|key|secret|password|access_token|sig)=)[^&\s]+", r"\1" + SCRUBBED, text)
    return _TOKEN_SHAPES.sub(SCRUBBED, text)


def scrub_argv(argv: list[str]) -> list[str]:
    """The command line with secret values replaced by [scrubbed]: the
    value after a flag named like a secret (--token X, --token=X), header
    values (Authorization: ...), NAME=value with a secret-like name, URL
    passwords and token-shaped strings."""
    out: list[str] = []
    hide_next = False
    for arg in argv:
        arg = str(arg)
        if hide_next:
            out.append(SCRUBBED)
            hide_next = False
            continue
        flag = re.match(r"^(--?[A-Za-z0-9_-]+)(=)?", arg)
        if flag and _SECRET_KEY_RE.search(flag.group(1).lstrip("-")) and "file" not in flag.group(1).lower():
            if flag.group(2):
                out.append(flag.group(0) + SCRUBBED)
            else:
                out.append(arg)
                hide_next = True
            continue
        out.append(scrub_text(arg))
    return out


def scrub_obj(value, key: str = ""):
    """Settings with secret-named keys' values and token-shaped strings scrubbed."""
    if isinstance(value, dict):
        return {k: scrub_obj(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_obj(v, key) for v in value]
    if isinstance(value, str):
        if key and _SECRET_KEY_RE.search(key) and not key.lower().endswith(("_file", "_path")):
            return SCRUBBED
        return scrub_text(value)
    return value


# ------------------------------------------------------------- code heads

def _git(path: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()


def code_heads(paths) -> dict:
    """{repo: {path, branch, commit, dirty, dirty_files}} for each worktree
    path, read with git; repo is the worktree directory's name (a second
    worktree of the same name gets #2). A path git cannot read is kept with
    an error."""
    heads: dict = {}
    for raw in paths or ():
        path = pathlib.Path(raw).expanduser().resolve()
        try:
            top = pathlib.Path(_git(path, "rev-parse", "--show-toplevel"))
            commit = _git(top, "rev-parse", "HEAD")
            branch = _git(top, "rev-parse", "--abbrev-ref", "HEAD")
            status = _git(top, "status", "--porcelain", "--untracked-files=normal")
            entry = {"path": str(top), "branch": None if branch == "HEAD" else branch,
                     "commit": commit, "dirty": bool(status),
                     "dirty_files": len(status.splitlines()) if status else 0}
            name = top.name
        except (subprocess.CalledProcessError, OSError):
            entry = {"path": str(path), "branch": None, "commit": None, "dirty": None,
                     "error": "not a git worktree, or git failed"}
            name = path.name or str(path)
        key, n = name, 2
        while key in heads and heads[key]["path"] != entry["path"]:
            key, n = f"{name}#{n}", n + 1
        heads[key] = entry
    return heads


# ---------------------------------------------------------------- helpers

def _check_run_id(run_id: str, root: pathlib.Path) -> pathlib.Path:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise ArchiveError(f"run id {run_id!r} refused: use letters, digits, '.', '_' and '-' "
                           "(one path component, no '..', at most 128 characters)")
    path = (root / run_id).resolve()
    if path.parent != root:
        raise ArchiveError(f"run id {run_id!r} refused: it would escape {root}")
    return path


def _hash_file(path: pathlib.Path, limit: int) -> tuple[int, str | None]:
    size = path.stat().st_size
    if size > limit:
        return size, None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return size, digest.hexdigest()


def _numbered(rel: pathlib.PurePosixPath, n: int) -> pathlib.PurePosixPath:
    """name.ext -> name.N.ext (name.tar.gz -> name.N.tar.gz; name -> name.N)."""
    name = rel.name
    stem, dot, ext = name.partition(".") if not name.startswith(".") else (name, "", "")
    return rel.with_name(f"{stem}.{n}.{ext}" if dot else f"{name}.{n}")


def _manifest_skeleton(run_id: str, kind: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "kind": kind,
        "started_at_utc": None,
        "ended_at_utc": None,
        "tool": None,
        "command": None,
        "host": None,
        "invocations": [],
        "code_heads": {},
        "deploy_settings": {},
        "topology": {"file": None, "nodes": []},
        "regions": [],
        "listener_steps": [],
        "lab": {"run_tag": None, "instances": [], "hours_billed": None, "cost_usd": None,
                "created_at_utc": None, "deleted_at_utc": None},
        "collected": {"complete": False, "at_utc": None, "missing": []},
        "status": "running",
        "status_reason": None,
        "hash_limit_bytes": HASH_LIMIT_BYTES,
        "files": [],
        "refused": [],
    }


# ---------------------------------------------------------------- archive

class RunArchive:
    """One run's directory and manifest. Every update reads the manifest
    under a lock, changes it and rewrites it atomically, so several tools
    (and processes) can append to one run."""

    def __init__(self, run_id: str, root: pathlib.Path | None = None):
        self.root = pathlib.Path(root).resolve() if root else data_root()
        self.run_id = run_id
        self.path = _check_run_id(run_id, self.root)
        self.tool = pathlib.Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "python"

    # -- opening

    @classmethod
    def create(cls, run_id: str, kind: str, *, tool: str | None = None, argv: list[str] | None = None,
               code_paths=(), deploy_settings: dict | None = None, topology=None,
               topology_nodes: list[dict] | None = None, regions=None,
               root: pathlib.Path | None = None) -> "RunArchive":
        """Make the run, or reopen it when the same tool made it with the
        same kind (the invocation is appended). Refused if the id escapes
        the root or names another tool's run, another kind's, or a
        directory that is not a run."""
        if kind not in KINDS:
            raise ArchiveError(f"kind {kind!r} is not one of {', '.join(KINDS)}")
        archive = cls(run_id, root)
        if tool:
            archive.tool = tool
        argv = scrub_argv(list(sys.argv if argv is None else argv))
        archive.root.mkdir(parents=True, exist_ok=True)
        try:
            archive.path.mkdir()
        except FileExistsError:
            if not archive.path.is_dir() or archive.path.is_symlink():
                raise ArchiveError(f"{archive.path} exists and is not a run directory")
        with archive._locked():
            manifest = archive._read(missing_ok=True)
            now = utc_now()
            if manifest is None:
                if any(p.name not in (LOCK,) for p in archive.path.iterdir()):
                    raise ArchiveError(f"run {run_id!r} refused: {archive.path} holds files but no "
                                       f"{MANIFEST}; it is not a run this tool made")
                manifest = _manifest_skeleton(run_id, kind)
                manifest.update(started_at_utc=now, tool=archive.tool, command=argv,
                                host=socket.gethostname())
            elif manifest.get("kind") != kind or manifest.get("tool") != archive.tool:
                raise ArchiveError(
                    f"run {run_id!r} refused: it exists as a {manifest.get('kind')} run made by "
                    f"{manifest.get('tool')} (this is {archive.tool}, {kind}); pick another id, or "
                    "RunArchive.open() it to append")
            manifest["invocations"].append({"tool": archive.tool, "command": argv,
                                            "host": socket.gethostname(), "pid": os.getpid(),
                                            "at_utc": now, "via": "create"})
            if code_paths:
                manifest["code_heads"].update(code_heads(code_paths))
            if deploy_settings:
                manifest["deploy_settings"].update(scrub_obj(deploy_settings))
            if regions:
                manifest["regions"] = sorted(set(manifest["regions"]) | set(regions))
            archive._write(manifest)
        if topology is not None:
            archive.set_topology(topology, topology_nodes)
        return archive

    @classmethod
    def open(cls, run_id: str, *, tool: str | None = None, argv: list[str] | None = None,
             root: pathlib.Path | None = None, record: bool = True) -> "RunArchive":
        """An existing run, to append to (any tool). record=False reads only."""
        archive = cls(run_id, root)
        if tool:
            archive.tool = tool
        if not (archive.path / MANIFEST).is_file():
            raise ArchiveError(f"run {run_id!r} has no manifest under {archive.root}")
        if record:
            with archive._update() as manifest:
                manifest["invocations"].append({
                    "tool": archive.tool, "command": scrub_argv(list(sys.argv if argv is None else argv)),
                    "host": socket.gethostname(), "pid": os.getpid(), "at_utc": utc_now(), "via": "open"})
        return archive

    # -- manifest I/O

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(self.path / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, missing_ok: bool = False) -> dict | None:
        try:
            return json.loads((self.path / MANIFEST).read_text())
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ArchiveError(f"run {self.run_id!r} has no manifest")

    def _write(self, manifest: dict) -> None:
        manifest["updated_at_utc"] = utc_now()
        data = (json.dumps(manifest, indent=2, sort_keys=False) + "\n").encode()
        tmp = self.path / f".{MANIFEST}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path / MANIFEST)
        with contextlib.suppress(OSError):
            dfd = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    @contextlib.contextmanager
    def _update(self):
        with self._locked():
            manifest = self._read()
            yield manifest
            self._write(manifest)

    @property
    def manifest(self) -> dict:
        return self._read()

    # -- paths

    def _dest(self, dest_rel) -> pathlib.PurePosixPath:
        rel = pathlib.PurePosixPath(str(dest_rel).replace(os.sep, "/"))
        if rel.is_absolute() or not rel.parts or any(p in ("..", "") for p in rel.parts):
            raise ArchiveError(f"destination {dest_rel!r} refused: it must be a relative path inside the run")
        if (len(rel.parts) == 1 and rel.name in (MANIFEST, LOCK)) or rel.name.startswith(f".{MANIFEST}"):
            raise ArchiveError(f"destination {dest_rel!r} refused: the name is the archive's own")
        cur = self.path
        for part in rel.parts[:-1]:
            cur = cur / part
            if cur.is_symlink() or (cur.exists() and not cur.is_dir()):
                raise ArchiveError(f"destination {dest_rel!r} refused: {part} is a symlink or a file")
        full = (self.path / rel)
        parent = full.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.resolve() != self.path and self.path not in parent.resolve().parents:
            raise ArchiveError(f"destination {dest_rel!r} refused: it would escape the run directory")
        if full.is_symlink():
            raise ArchiveError(f"destination {dest_rel!r} refused: it is a symlink")
        return rel

    def _inside_root(self, path: pathlib.Path) -> bool:
        resolved = path.resolve()
        return resolved == self.root or self.root in resolved.parents

    # -- files

    def _place(self, src: pathlib.Path, rel: pathlib.PurePosixPath, move: bool) -> tuple[pathlib.PurePosixPath, int, str | None, bool]:
        """Put src at rel or rel.N; never over another file. Returns
        (rel used, bytes, sha256 or None, reused) where reused means an
        identical file was already there."""
        size = src.stat().st_size
        src_hash = None
        n = 0
        candidate = rel
        while True:
            target = self.path / candidate
            if target.exists() or target.is_symlink():
                if target.is_file() and not target.is_symlink() and target.stat().st_size == size:
                    if src_hash is None:
                        src_hash = _hash_file(src, HASH_LIMIT_BYTES)[1]
                    if src_hash is not None and _hash_file(target, HASH_LIMIT_BYTES)[1] == src_hash:
                        if move and not self._inside_root(src):
                            os.unlink(src)
                        return candidate, size, src_hash, True
                n += 1
                candidate = _numbered(rel, n)
                continue
            if move:
                try:
                    os.link(src, target)
                except FileExistsError:
                    continue
                except OSError as exc:
                    if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP):
                        raise
                else:
                    os.unlink(src)  # the source, outside the root
                    size, digest = _hash_file(target, HASH_LIMIT_BYTES)
                    return candidate, size, digest, False
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as out, open(src, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    out.write(block)
                    if total <= HASH_LIMIT_BYTES:
                        digest.update(block)
                    total += len(block)
                out.flush()
                os.fsync(out.fileno())
            with contextlib.suppress(OSError):
                st = src.stat()
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            if move:
                os.unlink(src)  # the source, outside the root
            return candidate, total, digest.hexdigest() if total <= HASH_LIMIT_BYTES else None, False

    @staticmethod
    def _entry(rel, size, digest, kind, description, units, source, tool) -> dict:
        entry = {"path": str(rel), "bytes": size, "sha256": digest, "kind": kind,
                 "description": description, "units": units, "source": source,
                 "added_at_utc": utc_now(), "added_by": tool}
        if digest is None:
            entry["sha256_skipped"] = f"over the hash limit of {HASH_LIMIT_BYTES} bytes; bytes only"
        return entry

    @staticmethod
    def _register(manifest: dict, entry: dict) -> None:
        for i, old in enumerate(manifest["files"]):
            if old["path"] == entry["path"]:
                merged = {**old, **{k: v for k, v in entry.items() if v is not None},
                          "sha256": entry["sha256"]}
                if entry["sha256"] is not None:
                    merged.pop("sha256_skipped", None)
                manifest["files"][i] = merged
                return
        manifest["files"].append(entry)

    def _source_ok(self, src: pathlib.Path, move: bool) -> None:
        if not src.is_file():
            raise ArchiveError(f"{src} is not a regular file")
        reason = credential_reason(src)
        if reason:
            raise ArchiveError(f"{src} refused: {reason}; credentials are never archived")
        if move and self._inside_root(src):
            raise ArchiveError(f"{src} refused for move: it is inside the archive root")

    def add_file(self, src, dest_rel=None, kind: str = "raw", description: str = "",
                 move: bool = False, units=None) -> str:
        """Copy (or move) src into the run at dest_rel (default: its name).
        A different file already there is kept and this one written as
        name.N.ext; an identical one is reused. Returns the path used,
        relative to the run directory."""
        src = pathlib.Path(src).expanduser()
        self._source_ok(src, move)
        rel = self._dest(dest_rel if dest_rel is not None else src.name)
        used, size, digest, _ = self._place(src, rel, move)
        with self._update() as manifest:
            self._register(manifest, self._entry(used, size, digest, kind, description, units,
                                                 str(src.resolve()) if src.exists() else str(src), self.tool))
        return str(used)

    def add_tree(self, src_dir, dest_rel=None, kind: str = "raw", description: str = "",
                 exclude=(), units=None, move: bool = False) -> dict:
        """Copy every regular file under src_dir to dest_rel/ (default: the
        directory's name), keeping the layout. Files matching an exclude
        glob (against the relative path or the name) are left out;
        credential files and symlinks are skipped and recorded under
        `refused`. Returns {"added": [...], "skipped": [...], "refused": [...]}."""
        src_dir = pathlib.Path(src_dir).expanduser()
        if not src_dir.is_dir():
            raise ArchiveError(f"{src_dir} is not a directory")
        if self._inside_root(src_dir):
            raise ArchiveError(f"{src_dir} refused: it is inside the archive root")
        base = self._dest(dest_rel if dest_rel is not None else src_dir.resolve().name)
        result = {"added": [], "skipped": [], "refused": []}
        pending: list[dict] = []
        refused: list[dict] = []

        def flush():
            if pending or refused:
                with self._update() as manifest:
                    for entry in pending:
                        self._register(manifest, entry)
                    manifest["refused"].extend(refused)
                pending.clear()
                refused.clear()

        for dirpath, dirnames, filenames in os.walk(src_dir, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                path = pathlib.Path(dirpath) / name
                relsrc = path.relative_to(src_dir).as_posix()
                if any(fnmatch.fnmatch(relsrc, g) or fnmatch.fnmatch(name, g) for g in exclude):
                    result["skipped"].append(relsrc)
                    continue
                if path.is_symlink() or not path.is_file():
                    result["skipped"].append(relsrc)
                    continue
                reason = credential_reason(path)
                if reason:
                    result["refused"].append(relsrc)
                    refused.append({"path": str(path), "reason": reason, "at_utc": utc_now()})
                    continue
                rel = self._dest(base / relsrc)
                used, size, digest, _ = self._place(path, rel, move)
                pending.append(self._entry(used, size, digest, kind, description, units,
                                           str(path), self.tool))
                result["added"].append(str(used))
                if len(pending) >= TREE_BATCH_FILES:
                    flush()
        flush()
        return result

    def write_json(self, dest_rel, obj, kind: str = "result", description: str = "", units=None) -> str:
        """Write obj as a new JSON file (never over another). Returns the path used."""
        data = (json.dumps(obj, indent=2) + "\n").encode()
        rel = self._dest(dest_rel)
        n, candidate = 0, rel
        while True:
            try:
                fd = os.open(self.path / candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                break
            except FileExistsError:
                n += 1
                candidate = _numbered(rel, n)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        with self._update() as manifest:
            self._register(manifest, self._entry(candidate, len(data), hashlib.sha256(data).hexdigest(),
                                                 kind, description, units, None, self.tool))
        return str(candidate)

    def append_ndjson(self, dest_rel, records, kind: str = "samples", description: str = "",
                      units=None) -> str:
        """Append records (dicts) as lines to an NDJSON file, creating it
        if need be. Its sha256 is left null while it grows and computed by
        finish(); verify checks its bytes meanwhile."""
        rel = self._dest(dest_rel)
        if isinstance(records, dict):
            records = [records]
        data = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records).encode()
        with self._update() as manifest:
            fd = os.open(self.path / rel, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            size = (self.path / rel).stat().st_size
            existing = next((f for f in manifest["files"] if f["path"] == str(rel)), None)
            if existing:
                existing["bytes"] = size
                existing["sha256"] = None
                existing["sha256_skipped"] = "appended to; computed at finish"
            else:
                entry = self._entry(rel, size, None, kind, description, units, None, self.tool)
                entry["sha256_skipped"] = "appended to; computed at finish"
                manifest["files"].append(entry)
        return str(rel)

    # -- run facts

    def set_code_heads(self, paths) -> dict:
        heads = code_heads(paths)
        with self._update() as manifest:
            manifest["code_heads"].update(heads)
        return heads

    def set_deploy_settings(self, settings: dict) -> None:
        with self._update() as manifest:
            manifest["deploy_settings"].update(scrub_obj(settings))

    def set_topology(self, topology, nodes: list[dict] | None = None) -> None:
        """Copy the topology JSON in (topology/<name>) and record its nodes
        [{name, plan, region, role, site}] and their regions. topology may
        be a path or an already-loaded dict."""
        if isinstance(topology, dict):
            data, rel = topology, self.write_json("topology/topology.json", topology, "topology",
                                                  "the lab's topology")
        else:
            path = pathlib.Path(topology).expanduser()
            rel = self.add_file(path, f"topology/{path.name}", "topology", "the lab's topology")
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                data = {}
        if nodes is None:
            raw = data.get("nodes", []) if isinstance(data, dict) else []
            if isinstance(raw, dict):
                raw = [{"name": k, **v} for k, v in raw.items()]
            nodes = [{"name": n.get("name"), "plan": n.get("plan") or n.get("type"),
                      "region": n.get("region"), "role": n.get("role"), "site": n.get("site")}
                     for n in raw if isinstance(n, dict)]
        with self._update() as manifest:
            manifest["topology"] = {"file": rel, "nodes": nodes}
            regions = {n["region"] for n in nodes if n.get("region")}
            manifest["regions"] = sorted(set(manifest["regions"]) | regions)

    def set_regions(self, regions) -> None:
        with self._update() as manifest:
            manifest["regions"] = sorted(set(manifest["regions"]) | set(regions))

    def record_step(self, step, lane: str, listeners_total: int, listeners_by_region: dict | None = None,
                    paths_per_listener: int | None = None, at_utc: str | None = None, **extra) -> dict:
        """Append a listener step: {step, lane, listeners_total,
        listeners_by_region, paths_per_listener, at_utc, ...extra}."""
        entry = {"step": step, "lane": lane, "listeners_total": int(listeners_total),
                 "listeners_by_region": dict(listeners_by_region or {}),
                 "paths_per_listener": paths_per_listener, "at_utc": at_utc or utc_now(), **extra}
        with self._update() as manifest:
            manifest["listener_steps"].append(entry)
        return entry

    def set_lab(self, run_tag: str | None = None, instances: list[dict] | None = None,
                hours_billed: float | None = None, cost_usd: float | None = None,
                created_at_utc: str | None = None, deleted_at_utc: str | None = None) -> dict:
        """Merge the lab's facts. instances: [{label, id, plan, region,
        role, hourly_usd, hours_billed}]. Without cost_usd, the cost is the
        sum of hourly_usd * hours_billed over the instances when all carry
        both."""
        with self._update() as manifest:
            lab = manifest["lab"]
            for key, value in (("run_tag", run_tag), ("instances", instances),
                               ("hours_billed", hours_billed), ("cost_usd", cost_usd),
                               ("created_at_utc", created_at_utc), ("deleted_at_utc", deleted_at_utc)):
                if value is not None:
                    lab[key] = value
            insts = lab.get("instances") or []
            if cost_usd is None and insts and all(
                    i.get("hourly_usd") is not None and i.get("hours_billed") is not None for i in insts):
                lab["cost_usd"] = round(sum(float(i["hourly_usd"]) * float(i["hours_billed"])
                                            for i in insts), 4)
                if hours_billed is None:
                    lab["hours_billed"] = round(sum(float(i["hours_billed"]) for i in insts), 4)
            return dict(lab)

    def mark_collected(self, complete: bool, missing=()) -> None:
        """Say whether every node's data is in the archive; missing names
        the nodes or files that are not."""
        with self._update() as manifest:
            manifest["collected"] = {"complete": bool(complete), "at_utc": utc_now(),
                                     "missing": list(missing)}

    def finish(self, status: str = "complete", reason: str | None = None) -> str:
        """End the run: hash files whose sha256 was deferred, set
        ended_at_utc and the status. A linode-lab run whose data was not
        collected is recorded failed whatever was asked. Returns the status."""
        if status not in ("complete", "failed"):
            raise ArchiveError(f"status {status!r} is not complete or failed")
        with self._update() as manifest:
            for entry in manifest["files"]:
                if entry.get("sha256") is None:
                    path = self.path / entry["path"]
                    if path.is_file():
                        size, digest = _hash_file(path, HASH_LIMIT_BYTES)
                        entry["bytes"] = size
                        entry["sha256"] = digest
                        if digest is not None:
                            entry.pop("sha256_skipped", None)
                        else:
                            entry["sha256_skipped"] = f"over the hash limit of {HASH_LIMIT_BYTES} bytes; bytes only"
            if status == "complete" and manifest["kind"] == "linode-lab" and not manifest["collected"]["complete"]:
                status = "failed"
                missing = manifest["collected"].get("missing") or []
                reason = "lab data not collected" + (f" (missing: {', '.join(map(str, missing))})" if missing else "") \
                    + (f"; asked: {reason}" if reason else "")
            manifest["status"] = status
            manifest["status_reason"] = reason
            manifest["ended_at_utc"] = utc_now()
        return status

    # -- checks

    def verify(self) -> list[str]:
        """Differences between the manifest and the files: missing files,
        changed sizes, changed hashes. Empty when they agree."""
        problems = []
        for entry in self.manifest["files"]:
            path = self.path / entry["path"]
            if not path.is_file():
                problems.append(f"missing: {entry['path']}")
                continue
            size = path.stat().st_size
            if size != entry.get("bytes"):
                problems.append(f"size changed: {entry['path']} ({entry.get('bytes')} -> {size} bytes)")
                continue
            if entry.get("sha256"):
                if _hash_file(path, 1 << 62)[1] != entry["sha256"]:
                    problems.append(f"content changed: {entry['path']} (sha256 differs)")
        return problems

    def unlisted(self) -> list[str]:
        listed = {e["path"] for e in self.manifest["files"]}
        out = []
        for path in sorted(self.path.rglob("*")):
            rel = path.relative_to(self.path).as_posix()
            if path.is_file() and rel not in listed and rel != MANIFEST and rel != LOCK \
                    and not path.name.startswith(f".{MANIFEST}"):
                out.append(rel)
        return out


def require_collected(run_id: str, root: pathlib.Path | None = None) -> str | None:
    """None when the run's manifest says collected.complete; otherwise the
    refusal a lab tool's `down` prints."""
    try:
        manifest = RunArchive.open(run_id, root=root, record=False).manifest
    except ArchiveError as exc:
        return (f"refusing: run {run_id!r} has no archive ({exc}); collect the lab's data into "
                f"{(pathlib.Path(root) if root else data_root()) / run_id} first")
    collected = manifest.get("collected") or {}
    if collected.get("complete"):
        return None
    missing = collected.get("missing") or []
    return (f"refusing: run {run_id!r} is not collected"
            + (f" (missing: {', '.join(map(str, missing))})" if missing else
               " (no collection recorded)")
            + "; pull every node's logs, journals and samples into the archive and mark it "
            "collected before the lab is deleted. A lab deleted uncollected is a failed run.")


def list_runs(root: pathlib.Path | None = None) -> list[dict]:
    root = pathlib.Path(root).resolve() if root else data_root()
    runs = []
    if not root.is_dir():
        return runs
    for path in sorted(root.iterdir()):
        manifest_path = path / MANIFEST
        if not manifest_path.is_file():
            continue
        try:
            m = json.loads(manifest_path.read_text())
        except ValueError:
            runs.append({"run_id": path.name, "status": "unreadable", "kind": None,
                         "bytes": None, "cost_usd": None, "started_at_utc": None})
            continue
        if not isinstance(m, dict) or not m.get("schema_version") or not isinstance(m.get("files"), list):
            runs.append({"run_id": path.name, "status": "foreign-manifest", "kind": None,
                         "bytes": None, "cost_usd": None, "started_at_utc": None})
            continue
        runs.append({"run_id": m.get("run_id", path.name), "status": m.get("status"),
                     "kind": m.get("kind"), "bytes": sum(f.get("bytes") or 0 for f in m.get("files", [])),
                     "files": len(m.get("files", [])), "cost_usd": (m.get("lab") or {}).get("cost_usd"),
                     "started_at_utc": m.get("started_at_utc")})
    return runs


# -------------------------------------------------------------------- CLI

def _json_arg(value: str | None):
    if value is None:
        return None
    if value.startswith("@"):
        return json.loads(pathlib.Path(value[1:]).expanduser().read_text())
    return json.loads(value)


def _by_region(value: str | None) -> dict:
    if not value:
        return {}
    if value.lstrip().startswith("{"):
        return {k: int(v) for k, v in json.loads(value).items()}
    out = {}
    for part in value.split(","):
        key, _, count = part.partition("=")
        out[key.strip()] = int(count)
    return out


def _human(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", help="archive root (default NEEDLETAIL_DATA_ROOT or ~/needletail-data)")
    parser.add_argument("--tool", help="the calling tool's name, recorded in the manifest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create", help="make a run (or reopen the same tool's run)")
    p.add_argument("run_id")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--command", help="the calling tool's command line (scrubbed before it is recorded)")
    p.add_argument("--code", action="append", default=[], help="a worktree path whose git head to record")
    p.add_argument("--deploy-settings", help="JSON object, or @file")
    p.add_argument("--topology", help="topology JSON file to copy in")
    p.add_argument("--region", action="append", default=[])

    p = sub.add_parser("add", help="copy (or move) a file into a run")
    p.add_argument("run_id")
    p.add_argument("src")
    p.add_argument("--as", dest="dest")
    p.add_argument("--kind", default="raw")
    p.add_argument("--description", default="")
    p.add_argument("--units", help="JSON: a unit string or {field: unit}")
    p.add_argument("--move", action="store_true")

    p = sub.add_parser("add-tree", help="copy a directory into a run")
    p.add_argument("run_id")
    p.add_argument("src")
    p.add_argument("--as", dest="dest")
    p.add_argument("--kind", default="raw")
    p.add_argument("--description", default="")
    p.add_argument("--units")
    p.add_argument("--exclude", action="append", default=[])

    p = sub.add_parser("step", help="record a listener step")
    p.add_argument("run_id")
    p.add_argument("--step", required=True)
    p.add_argument("--lane", required=True)
    p.add_argument("--listeners", type=int, required=True)
    p.add_argument("--by-region", help="lon=100,new=200 or a JSON object")
    p.add_argument("--paths-per-listener", type=int)

    p = sub.add_parser("lab", help="record the lab's tag, instances and cost")
    p.add_argument("run_id")
    p.add_argument("--run-tag")
    p.add_argument("--instances", help="JSON list, or @file")
    p.add_argument("--hours-billed", type=float)
    p.add_argument("--cost-usd", type=float)
    p.add_argument("--created-at-utc")
    p.add_argument("--deleted-at-utc")

    p = sub.add_parser("collected", help="record whether every node's data is in")
    p.add_argument("run_id")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--complete", action="store_true")
    group.add_argument("--incomplete", action="store_true")
    p.add_argument("--missing", action="append", default=[])

    p = sub.add_parser("finish", help="end a run")
    p.add_argument("run_id")
    p.add_argument("--status", required=True, choices=("complete", "failed"))
    p.add_argument("--reason")

    p = sub.add_parser("show", help="print a run's manifest")
    p.add_argument("run_id")
    sub.add_parser("list", help="list runs with status, kind, size and cost")
    p = sub.add_parser("verify", help="check files against the manifest (exit 1 on a difference)")
    p.add_argument("run_id")
    p = sub.add_parser("check-collected", help="exit 1 with the refusal unless the run is collected")
    p.add_argument("run_id")

    args = parser.parse_args(argv)
    root = pathlib.Path(args.root).expanduser() if args.root else None
    try:
        if args.cmd == "create":
            command = shlex.split(args.command) if args.command else (argv if argv is not None else sys.argv)
            archive = RunArchive.create(args.run_id, args.kind, tool=args.tool, argv=command,
                                        code_paths=args.code, root=root,
                                        deploy_settings=_json_arg(args.deploy_settings),
                                        topology=args.topology, regions=args.region)
            print(archive.path)
        elif args.cmd == "list":
            for run in list_runs(root):
                cost = "-" if run["cost_usd"] is None else f"${run['cost_usd']:.2f}"
                print(f"{run['run_id']}\t{run['status']}\t{run['kind']}\t{_human(run['bytes'])}\t{cost}")
        elif args.cmd == "check-collected":
            refusal = require_collected(args.run_id, root)
            if refusal:
                print(refusal, file=sys.stderr)
                return 1
            print(f"{args.run_id}: collected")
        else:
            read_only = args.cmd in ("show", "verify")
            archive = RunArchive.open(args.run_id, tool=args.tool, root=root, record=not read_only,
                                      argv=argv if argv is not None else sys.argv)
            if args.cmd == "add":
                print(archive.add_file(args.src, args.dest, args.kind, args.description,
                                       move=args.move, units=_json_arg(args.units)))
            elif args.cmd == "add-tree":
                result = archive.add_tree(args.src, args.dest, args.kind, args.description,
                                          exclude=args.exclude, units=_json_arg(args.units))
                print(json.dumps({k: len(v) for k, v in result.items()}))
                for path in result["refused"]:
                    print(f"refused (credential): {path}", file=sys.stderr)
            elif args.cmd == "step":
                archive.record_step(args.step, args.lane, args.listeners, _by_region(args.by_region),
                                    args.paths_per_listener)
            elif args.cmd == "lab":
                print(json.dumps(archive.set_lab(args.run_tag, _json_arg(args.instances), args.hours_billed,
                                                 args.cost_usd, args.created_at_utc, args.deleted_at_utc)))
            elif args.cmd == "collected":
                archive.mark_collected(args.complete, args.missing)
            elif args.cmd == "finish":
                status = archive.finish(args.status, args.reason)
                print(status)
                if status != args.status:
                    print(f"recorded {status}: {archive.manifest['status_reason']}", file=sys.stderr)
            elif args.cmd == "show":
                print(json.dumps(archive.manifest, indent=2))
            elif args.cmd == "verify":
                problems = archive.verify()
                for line in problems:
                    print(line)
                for rel in archive.unlisted():
                    print(f"unlisted (not in the manifest): {rel}", file=sys.stderr)
                if problems:
                    return 1
                print(f"{args.run_id}: {len(archive.manifest['files'])} files agree with the manifest")
    except ArchiveError as exc:
        print(f"run_archive: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
