#!/usr/bin/env python3
"""Show that Needletail's three tiers scale separately, and how an SFU grows.

Needletail is built in three tiers:

  contribution   one contributor for each stream (alone on a g6-dedicated-4)
  distribution   backbone relays and the distributors of each region
  fan-out        the edges of each region, one at a time as the audience grows

The steps here load one tier at a time and record the processor time, the
bytes and the packets out of every node (a sampler on each node, every five
seconds):

  audience   1, 10, 100 and 1,000 listeners of one Opus stream in London, by
             LL-HLS (the standard player's way: 1 s segments) and by RaptorQ FEC
             over UDP from the edge (needletail-udp-client, branch client-udp).
             The contributor, relays and distributors should not move; the edges
             should grow with the audience.
  edges      1,000 low-latency LL-HLS listeners (one request for each 5 ms part)
             spread over 1, 2 and 4 London edges; then one edge loaded until it
             fails, which is the capacity of one edge.
  streams    1, 2 and 4 PCM streams with forty listeners in all: the
             contributor and distribution tiers grow, the edges do not.
  sfu        the same audiences on the self-hosted LiveKit SFU in London
             (lon-lk is sampled: its load grows with the audience) and on
             Cloudflare Realtime (managed: only what the subscribers see), with
             1, 10, 100 and 1,000 subscribers of 20 ms Opus.

Results: target/compare/runs/SESSION/tiers.STEP/HOST/... and
target/compare/runs/SESSION/samplers/.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("compare_run", HERE / "compare-run.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)
common = run.common

REMOTE = run.REMOTE
INDEX = f"{REMOTE}/index"
AUDIO = f"{REMOTE}/audio"
SECONDS = 45
AUDIENCE = [1, 10, 100, 1000]
UDP_AUDIENCE = [1, 10, 100, 1000]
UDP_CLIENT = "/usr/local/bin/needletail-udp-client"


def load_hosts(session: run.Session) -> list[str]:
    """Measurement hosts in London, largest first. lon-src is the source and is not used."""
    names = [name for name, node in session.nodes.items()
             if node["role"] == "load" and node["site"] == "london" and name != "lon-src"]
    order = {"g6-dedicated-8": 0, "g6-dedicated-4": 1, "g6-dedicated-2": 2}
    return sorted(names, key=lambda name: (order.get(session.nodes[name]["type"], 3), name))


def split(total: int, hosts: list[str], weights: dict[str, int]) -> dict[str, int]:
    """Spread `total` listeners over hosts in proportion to their vCPUs."""
    share = {host: weights[host] for host in hosts}
    whole = sum(share.values())
    out = {host: total * share[host] // whole for host in hosts}
    left = total - sum(out.values())
    for host in hosts:
        if left <= 0:
            break
        out[host] += 1
        left -= 1
    return {host: count for host, count in out.items() if count}


def vcpus(session: run.Session, host: str) -> int:
    return {"g6-dedicated-2": 2, "g6-dedicated-4": 4, "g6-dedicated-8": 8}.get(session.nodes[host]["type"], 1)


class Tiers:
    def __init__(self, session: run.Session):
        self.session = session
        self.hosts = load_hosts(session)
        self.weights = {host: vcpus(session, host) for host in self.hosts}
        self.entry = f"https://{session.ip('lon-dist-a')}:19444"

    # ---- sources ----
    def sources(self, streams: int = 1, daw: bool = False) -> int:
        """A fresh session on a boundary of the 64-second loop: the DAW source
        (Opus 3001, FLAC 2001) or AEP1 PCM senders, one group (stream 4001 +
        group) each."""
        s = self.session
        run.stop_sources(s)
        run.src(s, "pkill -x daw-test-source; pkill -x aep1-48k-probe; true", check=False)
        contributor = s.node("contrib-lon")
        settings = json.loads((common.ARTIFACT_DIR / "settings.json").read_text())
        if set(settings.get("daw_hls_formats", "").split(",")) != {"flac", "opus", "pcm"}:
            subprocess.run([sys.executable, str(HERE.parent / "linode-small-mesh-deploy.py"), "restart",
                            "--roles", "contributor", "--set", "daw_hls_formats=flac,opus,pcm"],
                           check=True, capture_output=True)
        else:
            common.ssh(contributor, "systemctl restart needletail-contrib.service; sleep 3; "
                                    "systemctl is-active needletail-contrib.service")
        now = time.time()
        start = (int(now) // 64 + 1) * 64
        if start - now < 8:
            start += 64
        if daw:
            run.src(s, run.background("tiers-daw", (
                f"env RUST_LOG=warn DAW_TEST_SOURCE_START_UNIX_NS={start}000000000 daw-test-source "
                f"--direct-contributor --prepared-pcm {contributor['vlan_ipv4']}:27100 1900 {REMOTE}/daw-fixture")))
            streams = 0
        for group in range(streams):
            run.src(s, run.background(f"tiers-pcm-{group}", (
                f"/usr/local/bin/aep1-48k-probe send --target {contributor['vlan_ipv4']}:27100 "
                f"--session-id {start * 1_000_000_000 + 5_000_000 + group * 1000} --group-id {group} "
                "--duration-seconds 3600 "
                f"--payload pcm --channels 2 --group-channels 8 --pcm-s24le-file {AUDIO}/audio.s24le "
                "--repair-percent 12 --min-repair-symbols 1")))
        time.sleep(max(0, start - time.time()) + 8)
        return start

    # ---- listeners ----
    def hls(self, count: int, stream: int, mode: str, tag: str, seconds: int, cache: str = "") -> str:
        index = f"{INDEX}/pcm-5ms.index.json"
        query = f"sid={tag}-{{i}}" + (f"&cache={cache}" if cache else "")
        return (f"ntcmp hls --lane nt-{mode} --mode {mode} --url '{self.entry}/live/{stream}/master.m3u8?"
                f"{query}' --ca {run.CA} --server-name {run.SERVER_NAME} "
                + (f"--audio-index {index} " if 4001 <= stream < 5000 else "")
                + f"--duration {seconds} --skip-seconds 12 --count {count} "
                f"--spread-ms {min(10000, 10 * count)} --report hls.json --label {tag}")

    def udp(self, count: int, stream: int, tag: str, seconds: int) -> str:
        """needletail-udp-client: each listener is assigned an edge and receives
        the stream's parts as RaptorQ objects over UDP."""
        lines = []
        for i in range(count):
            lines.append(
                f"( {UDP_CLIENT} --entry {self.entry} --stream {stream} --tls-ca {run.CA} "
                f"--server-name {run.SERVER_NAME} --duration-seconds {seconds} --session {tag}-{i} "
                f">uc-{i:04d}.json 2>uc-{i:04d}.err ) &")
        return "\n".join(lines) + "\nwait"

    def step(self, name: str, scripts: dict[str, str], seconds: int, extra: dict) -> None:
        step_name = f"tiers.{name}"
        run.log(f"{step_name}: {sum(1 for _ in scripts)} hosts, {seconds} s")
        # The load hosts' processor, so that a failing listener can be told from
        # a saturated load generator.
        scripts = {host: "S0=$(grep '^cpu ' /proc/stat)\n" + body
                   + "\necho \"$S0\" >cpu.txt; grep '^cpu ' /proc/stat >>cpu.txt"
                   for host, body in scripts.items()}
        result = run.run_hosts(self.session, step_name, scripts, seconds)
        meta = {"step": step_name, "phase": "tiers", "detail": name, **extra, **result}
        (self.session.dir / step_name / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
        if result["host_errors"]:
            run.log(f"  host errors: {json.dumps(result['host_errors'])[:300]}")

    # ---- phases ----
    def audience(self) -> None:
        # Opus (0.9 Mbit/s as the edges measure it): 1,000 listeners fit the
        # admission of the two London edges (850 Mbit/s each); PCM would not.
        start = self.sources(1, daw=True)
        for count in AUDIENCE:
            shares = split(count, self.hosts, self.weights)
            scripts = {host: self.hls(n, 3001, "segments", f"{self.session.name}-aud{count}-{host}", SECONDS)
                       for host, n in shares.items()}
            self.step(f"audience-hls-{count}", scripts, SECONDS, {"listeners": count, "mode": "hls-segments",
                                                                  "spread": shares})
        for count in UDP_AUDIENCE:
            shares = split(count, self.hosts, self.weights)
            scripts = {host: self.udp(n, 3001, f"{self.session.name}-uc{count}-{host}", SECONDS)
                       for host, n in shares.items()}
            self.step(f"audience-udp-{count}", scripts, SECONDS, {
                "listeners": count, "mode": "udp-client-at-edge", "spread": shares})

    def edges(self, counts: list[int], listeners: int) -> None:
        """Low-latency readers over the London edges that are deployed now. The
        deploy should have a small assignment spread target, so that readers
        spread over every edge."""
        deployed = sorted(n for n, v in self.session.nodes.items() if v["role"] == "edge" and v["site"] == "london")
        if counts != [len(deployed)]:
            raise SystemExit(f"--edges {counts} but {len(deployed)} London edges are deployed: {deployed}")
        self.sources(1, daw=True)
        for edges in counts:
            shares = split(listeners, self.hosts, self.weights)
            scripts = {host: self.hls(n, 3001, "chase", f"{self.session.name}-e{edges}-{host}", SECONDS)
                       for host, n in shares.items()}
            self.step(f"edges-{edges}-{listeners}", scripts, SECONDS, {"edges": edges, "listeners": listeners,
                                                                     "mode": "hls-chase", "spread": shares})

    def capacity(self, steps: list[int]) -> None:
        """One edge, pinned, loaded until it fails."""
        self.sources(1, daw=True)
        for count in steps:
            shares = split(count, self.hosts, self.weights)
            scripts = {host: self.hls(n, 3001, "chase", f"{self.session.name}-cap{count}-{host}", SECONDS,
                                      cache="lon-edge-1")
                       for host, n in shares.items()}
            self.step(f"capacity-{count}", scripts, SECONDS, {"listeners": count, "mode": "hls-chase",
                                                             "pinned": "lon-edge-1", "spread": shares})

    def streams(self) -> None:
        total = 40                         # the audience stays the same; only the streams change
        for streams in (1, 2, 4):
            self.sources(streams)
            scripts = {}
            for host, n in split(total // streams, self.hosts, self.weights).items():
                scripts[host] = run.together(*[
                    self.hls(n, 4001 + group, "segments", f"{self.session.name}-s{streams}-{group}-{host}",
                             SECONDS).replace("--report hls.json", f"--report hls-{group}.json")
                    for group in range(streams)])
            self.step(f"streams-{streams}", scripts, SECONDS, {"streams": streams, "listeners": total,
                                                              "listeners_each": total // streams})

    def sfu(self) -> None:
        s = self.session
        run.stop_sources(s)
        room = f"needletail-compare-{s.name}-tiers"
        keys = "LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET"
        run.src(s, run.background("tiers-lk", (
            f"{keys} ntcmp-lk pub --lane lk --url ws://{s.ip('lon-lk')}:7880 --room {room} "
            f"--audio {AUDIO}/opus-20.opus --status tiers-lk.status")))
        run.src(s, "rm -f cf.session; " + run.background(
            "tiers-cf", f"ntcmp pub --lane cf --audio {AUDIO}/opus-20.opus --session-out cf.session "
                        "--status tiers-cf.status"))
        cf_session = ""
        for _ in range(20):
            time.sleep(1)
            cf_session = run.src(s, "cat cf.session 2>/dev/null", check=False).strip()
            if cf_session:
                break
        if not cf_session:
            run.log("sfu: Cloudflare Realtime publisher has no session; its subscribers are skipped")
        for count in AUDIENCE:
            shares = split(count, self.hosts, self.weights)
            scripts = {}
            for host, n in shares.items():
                tail = (f"--duration {SECONDS} --skip-seconds 12 --count {n} --spread-ms {min(10000, 10 * n)} "
                        f"--audio-index {INDEX}/opus-20.index.json --no-video")
                scripts[host] = run.together(
                    f"{keys} ntcmp-lk sub --lane lk --url ws://{s.ip('lon-lk')}:7880 --room {room} "
                    f"--identity {s.name}-t{count}-{host} {tail.replace('--no-video', '')} --report lk.json",
                    f"ntcmp sub --lane cf --session {cf_session} {tail} --report cf.json" if cf_session else "")
            self.step(f"sfu-{count}", scripts, SECONDS, {"listeners": count, "spread": shares})
        run.stop_sources(s)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", required=True)
    parser.add_argument("--phases", default="audience,streams,sfu")
    parser.add_argument("--edges", default="", help="edge counts deployed now, e.g. 4 (one per call)")
    parser.add_argument("--edge-listeners", type=int, default=1000)
    parser.add_argument("--capacity", default="100,250,500,1000,1500")
    args = parser.parse_args()
    session = run.Session(args.session)
    sampled = run.start_samplers(session)
    tiers = Tiers(session)
    run.log(f"load hosts: {tiers.hosts}")
    try:
        for phase in args.phases.split(","):
            if phase == "audience":
                tiers.audience()
            elif phase == "streams":
                tiers.streams()
            elif phase == "sfu":
                tiers.sfu()
            elif phase == "edges":
                tiers.edges([int(n) for n in args.edges.split(",")], args.edge_listeners)
            elif phase == "capacity":
                tiers.capacity([int(n) for n in args.capacity.split(",")])
    finally:
        run.stop_sources(session)
        run.collect_samplers(session, sampled)


if __name__ == "__main__":
    main()
