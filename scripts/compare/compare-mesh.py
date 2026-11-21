#!/usr/bin/env python3
"""The long haul on its own: impair the links between regions, not the listener.

Needletail's long haul is its UDP mesh with RaptorQ FEC: the backbone relays
send every stream to the distributors of each region. LiveKit with an SFU in
each region has a long haul too: the source publishes to the SFU of each region
over the public Internet. Here the same `tc netem` is put on those links only
(NETEM_PEERS in netem.sh), and the listeners' own links stay clean:

  Needletail   every distributor's public interface, for traffic to and from
               the two backbone relays (in London only the secondary relay's
               repair lane crosses it; the primary lane is on the VLAN)
  LiveKit      the New York, Tokyo and Sydney SFUs' interface, for traffic to
               and from the source host

Cloudflare's and LiveKit Cloud's long haul is inside their networks and cannot
be impaired; they are measured clean only.

During each step, scripts/compare/mesh-hops.py fetches the parts of the stream
from every mesh node and gives the one-way time of each hop, and the RaptorQ
counters of every node before and after.

  compare-mesh.py --session mesh-1 [--configs pcm,opus-2.5]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("compare_audio", HERE / "compare-audio.py")
audio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audio)
run = audio.run
common = run.common

PROFILES = {"clean": None, **run.IMPAIRMENTS}
LANES = {"pcm": ["lkregion", "nt-pcm"], "opus-2.5": ["lkregion", "nt-opus"]}
STREAM = {"pcm": 4001, "opus-2.5": 3001}
SECONDS = 50


def install_netem(session: run.Session) -> None:
    nodes = [node for node in session.nodes.values()
             if node["role"] in ("distributor", "livekit") and node["name"] != "lk-redis"]
    script = str(HERE / "netem.sh")
    common.parallel(nodes, lambda node: common.rsync_to(node, [script], "/usr/local/bin/compare-netem"))


def long_haul(session: run.Session, spec: str | None) -> dict[str, str]:
    """The netem command of each impaired node, or the clear command."""
    relays = f"{session.ip('relay-primary')} {session.ip('relay-secondary')}"
    commands = {}
    for name, node in session.nodes.items():
        if node["role"] == "distributor":
            peers = relays
        elif node["role"] == "livekit" and name not in ("lk-redis", "lon-lk"):
            peers = session.ip("lon-src")
        else:
            continue
        commands[name] = (f"NETEM_DEV=eth0 NETEM_PEERS='{peers}' compare-netem apply '{spec}'"
                          if spec else "compare-netem clear")
    return commands


def apply(session: run.Session, commands: dict[str, str]) -> dict[str, str]:
    results = common.parallel([session.node(name) for name in commands],
                              lambda node: common.ssh_text(node, commands[node["name"]] + " 2>&1 | tail -1",
                                                           check=False, timeout=60))
    return results


def show(session: run.Session, names: list[str]) -> dict[str, str]:
    return common.parallel([session.node(name) for name in names],
                           lambda node: common.ssh_text(node, "compare-netem show 2>&1 | grep -E 'netem|Sent' | "
                                                              "head -4", check=False, timeout=60))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", required=True)
    parser.add_argument("--configs", default="pcm,opus-2.5")
    parser.add_argument("--profiles", default=",".join(PROFILES))
    args = parser.parse_args()
    session = run.Session(args.session)
    install_netem(session)
    sampled = run.start_samplers(session)
    try:
        for config in args.configs.split(","):
            lanes = LANES[config]
            media = audio.Audio(session, config, lanes)
            media.lanes = [lane for lane in lanes if lane in audio.WEBRTC]
            media.nt_base = [lane for lane in lanes if lane in audio.NEEDLETAIL]
            media.nt = [lane + "-h3" for lane in media.nt_base]
            media.start()
            for profile in args.profiles.split(","):
                spec = PROFILES[profile]
                commands = long_haul(session, spec)
                applied = apply(session, commands) if spec else {}
                hops_out = session.dir / f"mesh.{config}.{profile}.hops.json"
                outcome: dict = {}

                def sample_hops(out=hops_out, label=f"{config} {profile}", stream=STREAM[config], box=outcome):
                    try:
                        done = subprocess.run(
                            [sys.executable, str(HERE / "mesh-hops.py"), "run", "--streams", str(stream),
                             "--seconds", "12", "--samples", "300", "--max-age", "9", "--out", str(out),
                             "--label", label], capture_output=True, timeout=300)
                        box["returncode"] = done.returncode
                        box["stderr"] = done.stderr.decode(errors="replace")[-600:]
                    except Exception as error:  # noqa: BLE001 - recorded in meta
                        box["error"] = repr(error)[:600]
                hops = threading.Thread(target=sample_hops)
                # Start the hop sampler 20 s into the step, so that the parts it
                # takes are from the impaired period.
                timer = threading.Timer(20, hops.start)
                timer.start()
                try:
                    media.step("mesh", profile, SECONDS, 0, None)
                finally:
                    timer.join()
                    if hops.is_alive() or hops.ident:
                        hops.join(timeout=320)
                    state = show(session, list(commands)) if spec else {}
                    apply(session, long_haul(session, None))
                meta_path = session.dir / f"audio.{config}.mesh.{profile}" / "meta.json"
                if meta_path.is_file():
                    meta = json.loads(meta_path.read_text())
                    meta.update({"long_haul_netem": spec, "netem_applied": applied, "netem_state": state,
                                 "hops_file": hops_out.name if hops_out.is_file() else None,
                                 "hops_run": outcome})
                    meta_path.write_text(json.dumps(meta, indent=1) + "\n")
    finally:
        apply(session, long_haul(session, None))
        run.stop_sources(session)
        run.src(session, "pkill -x daw-test-source; pkill -x aep1-48k-probe; true", check=False)
        run.collect_samplers(session, sampled)


if __name__ == "__main__":
    main()
