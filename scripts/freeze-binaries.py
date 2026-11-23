#!/usr/bin/env python3
"""Freeze the six binaries of the local lab into a set that no build changes.

  freeze-binaries.py LABEL           copy them to target/bin-sets/LABEL/
  freeze-binaries.py --next LABEL    the same, and print the exports of set b
                                     (the build that a roll moves nodes to)

The six: av-mesh, needletail-reader, needletail-load, needletail-udp-client
(from NEEDLETAIL_AV_MESH_ROOT, default ../av-mesh-features), av-contrib and
aep1-48k-probe (NEEDLETAIL_AV_CONTRIB_ROOT, default ../av-contrib-features),
each overridable by its own variable as edge_lab.py and latency-bench.py
read them. build.json records each binary's sha256, size, source and
`--version`. Stdout is only the exports, for
`eval "$(scripts/freeze-binaries.py w24)"`; a set is never written twice.
Runs refuse a binary outside a frozen set (lab_guard.require_frozen).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import edge_lab as lab  # noqa: E402
import lab_guard  # noqa: E402

SETS = lab_guard.BIN_SETS
UDP_CLIENT = pathlib.Path(os.environ.get(
    "NEEDLETAIL_UDP_CLIENT_BIN", str(lab.MESH_ROOT / "target" / "release" / "needletail-udp-client")))
# Label, where it is built, the variable that the lab and the bench read.
BINARIES = (
    ("av-mesh", lab.AV_MESH, "NEEDLETAIL_AV_MESH_BIN"),
    ("needletail-reader", lab.READER, "NEEDLETAIL_READER_BIN"),
    ("needletail-load", lab.LOAD_BIN, "NEEDLETAIL_LOAD_BIN"),
    ("needletail-udp-client", UDP_CLIENT, "NEEDLETAIL_UDP_CLIENT_BIN"),
    ("av-contrib", lab.AV_CONTRIB, "NEEDLETAIL_AV_CONTRIB_BIN"),
    ("aep1-48k-probe", lab.PROBE, "NEEDLETAIL_PROBE_BIN"),
)
# Set b of a lab: its two services' binaries.
NEXT = {"av-mesh": "NEEDLETAIL_AV_MESH_BIN_NEXT", "av-contrib": "NEEDLETAIL_AV_CONTRIB_BIN_NEXT"}


def version(path: pathlib.Path) -> dict:
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                                timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"version": None, "version_error": str(error)}
    if result.returncode != 0:
        return {"version": None,
                "version_error": f"exit {result.returncode}: {result.stderr.strip()[:200]}"}
    return {"version": result.stdout.strip()}


def freeze(label: str, binaries=BINARIES, sets: pathlib.Path = SETS) -> pathlib.Path:
    """Copy `binaries` into sets/label with build.json, read-only; the
    directory appears whole or not at all."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", label):
        raise SystemExit(f"{label!r}: a label is letters, digits, '.', '_' and '-'")
    target = sets / label
    if target.exists():
        raise SystemExit(f"{target} exists: a frozen set is never written again; take another label")
    missing = [f"{name} ({source})" for name, source, _ in binaries
               if not pathlib.Path(source).is_file()]
    if missing:
        raise SystemExit("not built: " + ", ".join(missing))
    sets.mkdir(parents=True, exist_ok=True)
    building = sets / f".{label}.{os.getpid()}"
    building.mkdir()
    try:
        record = {"schema": lab_guard.BIN_SET_SCHEMA, "label": label, "frozen_unix": time.time(),
                  "binaries": {}}
        for name, source, _ in binaries:
            copy = building / name
            shutil.copy2(source, copy)
            copy.chmod(0o555)
            record["binaries"][name] = {"source": str(pathlib.Path(source).resolve()),
                                        "sha256": lab_guard.sha256(copy),
                                        "bytes": copy.stat().st_size, **version(copy)}
        (building / "build.json").write_text(json.dumps(record, indent=1) + "\n")
        building.rename(target)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return target


def exports(target: pathlib.Path, binaries=BINARIES, next_set: bool = False) -> list[str]:
    if next_set:
        return [f"export {NEXT[name]}={target / name}" for name, _, _ in binaries if name in NEXT]
    return [f"export {variable}={target / name}" for name, _, variable in binaries]


def main(argv: list[str]) -> int:
    next_set = argv[:1] == ["--next"]
    rest = argv[1:] if next_set else argv
    if len(rest) != 1 or rest[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    target = freeze(rest[0])
    for line in exports(target, next_set=next_set):
        print(line)
    print(f"frozen in {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
