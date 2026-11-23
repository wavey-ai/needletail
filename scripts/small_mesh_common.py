"""Shared helpers for the small-instance Linode mesh lab tools."""

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent
QUALIFICATION_ROOT = ROOT / "target" / "linode-qualification"
DEFAULT_STATE = QUALIFICATION_ROOT / "small-mesh-lab.json"
ARTIFACT_DIR = QUALIFICATION_ROOT / "small-mesh-artifacts"
RUNS_DIR = QUALIFICATION_ROOT / "small-mesh-runs"
SSH_KEY = pathlib.Path(
    os.environ.get("NEEDLETAIL_LINODE_SSH_KEY", "~/.ssh/id_ed25519")
).expanduser()
KNOWN_HOSTS = QUALIFICATION_ROOT / "small-mesh-known_hosts"
CONTROL_DIR = pathlib.Path("/tmp") / f"ntsm-ssh-{os.getuid()}"
TLS_SERVER_NAME = os.environ.get("NEEDLETAIL_TLS_SERVER_NAME", "mesh.needletail.test")


def state_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("NEEDLETAIL_LINODE_LAB_STATE", str(DEFAULT_STATE)))


def load_state() -> dict:
    path = state_path()
    if not path.is_file():
        sys.exit("lab state missing; run scripts/linode-small-mesh-lab.py up first")
    return json.loads(path.read_text())


def nodes_by_role(state: dict, role: str) -> list[dict]:
    return sorted(
        (node for node in state["nodes"].values() if node["role"] == role),
        key=lambda node: node["name"],
    )


LOAD_PURPOSES = ("source", "view", "listeners")


def load_purpose(node: dict) -> str | None:
    """What a node of role load is for: "source" (sends the media into the
    systems: lon-src), "view" (the full probes and players of a region:
    *-view, *-nano, and the single *-load host of the older labs, such as
    topology-core.json's lon-load, which is their source and probe host) or
    "listeners" (simulated listeners, needletail-load and ntcmp crowds:
    *-load-N). The topology names it (`purpose`); a state saved before it
    did is read from the name. None for any other role."""
    if node.get("role") != "load":
        return None
    purpose = node.get("purpose")
    if purpose:
        if purpose not in LOAD_PURPOSES:
            raise ValueError(f"{node.get('name')}: purpose {purpose!r} is not one of "
                             f"{', '.join(LOAD_PURPOSES)}")
        return purpose
    name = str(node.get("name") or "")
    if name.endswith("-src"):
        return "source"
    if re.search(r"-load-[0-9]+$", name):
        return "listeners"
    return "view"


def nodes_for(state: dict, purpose: str) -> list[dict]:
    """The load nodes of one purpose, by name."""
    return [node for node in nodes_by_role(state, "load") if load_purpose(node) == purpose]


# The port on which a contributor takes its source's AEP1 epochs (av-contrib
# --daw-media-bind).
DAW_MEDIA_PORT = 27100


def contributors(state: dict) -> list[dict]:
    """The contributors of a lab: the origin first, then the standby (the
    node its topology marks `"standby": true`), which publishes the same
    parts beside it."""
    return sorted(nodes_by_role(state, "contributor"),
                  key=lambda node: bool(node.get("standby")))


def contributor_targets(state: dict) -> list[str]:
    """Where a source sends every AEP1 epoch: `address:27100` of each
    contributor, the origin first (one `--target` each for aep1-48k-probe
    send). A contributor's VLAN address where it has one: the source host is
    on its site's VLAN."""
    return [f"{node.get('vlan_ipv4') or node['public_ipv4']}:{DAW_MEDIA_PORT}"
            for node in contributors(state)]


def ssh_base() -> list[str]:
    CONTROL_DIR.mkdir(mode=0o700, exist_ok=True)
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    return [
        "-i", str(SSH_KEY), "-o", "BatchMode=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
        "-o", "ControlMaster=auto", "-o", f"ControlPath={CONTROL_DIR}/%C",
        "-o", "ControlPersist=900", "-o", "LogLevel=ERROR",
    ]


def ssh(node: dict, command: str, check: bool = True, timeout: int = 600,
        stdin: bytes | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["ssh", *ssh_base(), f"root@{node['public_ipv4']}", command],
        input=stdin, capture_output=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{node['name']}: remote command failed ({result.returncode}): "
            f"{result.stderr.decode(errors='replace')[-2000:]}"
        )
    return result


def ssh_text(node: dict, command: str, **kwargs) -> str:
    return ssh(node, command, **kwargs).stdout.decode(errors="replace").strip()


def rsync_to(node: dict, sources: list[str], destination: str,
             timeout: int = 900) -> None:
    command = [
        "rsync", "-az", "--chmod=F755", "-e", "ssh " + " ".join(ssh_base()),
        *sources, f"root@{node['public_ipv4']}:{destination}",
    ]
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"{node['name']}: rsync failed: {result.stderr.decode(errors='replace')[-1500:]}"
        )


def rsync_from(node: dict, source: str, destination: str, timeout: int = 900) -> None:
    command = [
        "rsync", "-az", "-e", "ssh " + " ".join(ssh_base()),
        f"root@{node['public_ipv4']}:{source}", destination,
    ]
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"{node['name']}: rsync failed: {result.stderr.decode(errors='replace')[-1500:]}"
        )


def parallel(nodes: list[dict], function, workers: int = 16) -> dict:
    """Run function(node) for every node; return {name: result}; raise on any failure."""
    results: dict = {}
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(function, node): node for node in nodes}
        for future in concurrent.futures.as_completed(futures):
            node = futures[future]
            try:
                results[node["name"]] = future.result()
            except Exception as error:  # noqa: BLE001 - report every node failure
                failures.append(f"{node['name']}: {error}")
    if failures:
        raise RuntimeError("\n".join(sorted(failures)))
    return results
