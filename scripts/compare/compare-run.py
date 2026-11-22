#!/usr/bin/env python3
"""Run the network comparison on the lab and collect what every viewer saw.

One invocation is one session: every chosen system (lane) in turn receives the
same clip from lon-src and is measured by the same viewers in London, New York,
Tokyo and Sydney.

Lanes
  nt        Needletail: RTMP to the contributor, LL-HLS from the edges
  cf        Cloudflare Realtime (SFU), HTTPS API
  whep      Cloudflare Stream Live, WHIP in and WHEP out
  cfhls     Cloudflare Stream Live, RTMPS in, LL-HLS and HLS out
  lkregion  LiveKit self-hosted: one SFU in each region, the source publishes to each
  lkmulti   LiveKit self-hosted: the multi-region set that shares one Redis
  lkcloud   LiveKit Cloud

Phases, for each lane
  baseline  the 8 Mbit/s 4K clip for 60 s: a probe on the Nanode and on the
            viewer host of each region, and headless Chrome on the viewer host
  ttff      repeated joins: eight by the probe, three by Chrome
  impair    the same netem on both viewer hosts of each region, 45 s each
  fanout    1, 10 and 100 probes on one stream
  ramp      1080p at 6 Mbit/s and 4K at 16, 25, 40 and 80 Mbit/s, 40 s each
  throughput  4K at 10, 16, 25, 40, 80, 122, 164, 248 and 333 Mbit/s, 40 s
            each, until two rungs in a row reach no region whole

Results are kept under target/compare/runs/SESSION/STEP/HOST/. A step is
`LANE.PHASE.DETAIL`. scripts/compare/compare-analyse.py reads them.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import small_mesh_common as common  # noqa: E402

_spec = importlib.util.spec_from_file_location("compare_lab", HERE / "compare_lab.py")
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)

ROOT = common.ROOT
RUNS = ROOT / "target" / "compare" / "runs"
REMOTE = "/var/lib/compare"
CA = "/usr/local/share/ca-certificates/needletail-qualification.crt"
SERVER_NAME = common.TLS_SERVER_NAME
# COMPARE_SITES drops a site whose lab nodes could not be created (22 Nov: no
# dedicated capacity in Sydney).
SITES = os.environ.get("COMPARE_SITES", "lon,new,tok,syd").split(",")
BASE_RUNG = "2160p5"     # the rung that every system carries whole
RAMP = ["1080p6", "2160p16", "2160p25", "2160p40", "2160p80"]
# Raw throughput: 4K at a rising bit rate until the system no longer carries it.
THROUGHPUT = ["2160p5", "2160p8", "2160p16", "2160p25", "2160p40", "2160p80", "2160p120", "2160p160",
              "2160p240", "2160p320"]
CHROME_UP_TO = ("2160p5", "2160p8", "2160p16", "2160p25")      # a browser decodes these in real time
SIZES = {"1080p6": (1920, 1080)}
IMPAIRMENTS = {
    "loss1": "loss 1%",
    "loss5": "loss 5%",
    "burst": "loss gemodel 1% 20%",
    "delay100": "delay 100ms",
}
FANOUT = {1: {"lon": 1}, 10: {"lon": 3, "new": 3, "tok": 2, "syd": 2},
          100: {"lon": 25, "new": 25, "tok": 25, "syd": 25}}
# cfhls is not in the default: Cloudflare refuses ingest on a recording live
# input for an account without a Stream subscription (see the write-up).
LANES = ["nt", "cf", "whep", "lkregion", "lkmulti", "lkcloud"]
SAMPLER = (
    "[ -f /var/tmp/compare-sampler.pid ] && kill $(cat /var/tmp/compare-sampler.pid) 2>/dev/null; "
    "cat >/var/tmp/compare-sampler.sh <<'EOS'\n"
    "echo $$ >/var/tmp/compare-sampler.pid\n"
    "while true; do\n"
    "  line=\"$(date +%s.%N) $(head -1 /proc/stat) | net $(awk 'NR>2{gsub(\":\",\" \"); printf \"%s:%s:%s \", $1, $10, $11}' /proc/net/dev)\"\n"
    "  for name in av-mesh av-contrib livekit-server; do for pid in $(pgrep -x $name); do\n"
    "    line=\"$line | $name $pid $(awk '{print $14+$15}' /proc/$pid/stat) $(awk '/VmRSS/{print $2}' /proc/$pid/status)\"\n"
    "  done; done\n"
    "  echo \"$line\"; sleep 5\n"
    "done\n"
    "EOS\n"
    "(setsid nohup sh /var/tmp/compare-sampler.sh >/var/tmp/compare-sampler.log 2>&1 </dev/null &)")


def start_samplers(session: "Session") -> list[dict]:
    """Record the processor time of the services on every Needletail and
    LiveKit node, every five seconds, for the length of a session."""
    sampled = [item for item in session.nodes.values()
               if item["role"] in ("contributor", "backbone", "distributor", "edge", "livekit")]
    common.parallel(sampled, lambda item: common.ssh(item, SAMPLER, check=False))
    return sampled


def collect_samplers(session: "Session", sampled: list[dict]) -> None:
    (session.dir / "samplers").mkdir(exist_ok=True)

    def collect(item: dict) -> None:
        text = common.ssh_text(item, "kill $(cat /var/tmp/compare-sampler.pid) 2>/dev/null; "
                                     "cat /var/tmp/compare-sampler.log", check=False)
        # One file for each invocation: a session can be run in several parts.
        (session.dir / "samplers" / f"{item['name']}.{int(time.time())}.log").write_text(text + "\n")

    common.parallel(sampled, collect)


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] {text}", flush=True)



def refuse_livekit_cloud(lanes: list[str]) -> None:
    """LiveKit Cloud is no longer measured (owner, 22 November: "we dont need
    to test with their cloud"); its wave 5 figures stand as its record."""
    if "lkcloud" in lanes:
        raise SystemExit("lkcloud: LiveKit Cloud is no longer measured (owner, 22 Nov); "
                         "self-hosted LiveKit (lkregion, lkmulti) is the control")

class Session:
    def __init__(self, name: str):
        self.name = name
        self.dir = RUNS / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = common.load_state()
        self.nodes = self.state["nodes"]
        self.source = self.nodes["lon-src"]
        self.cloud = self.load_cloud()
        self.steps: list[dict] = []
        self.rungs: list[str] | None = None      # the rungs of a throughput sweep, when not all
        self.pace = 2.5                          # pacing factor of the WebRTC senders

    def node(self, name: str) -> dict:
        return self.nodes[name]

    def ip(self, name: str) -> str:
        return self.nodes[name]["public_ipv4"]

    def load_cloud(self) -> dict | None:
        path = ROOT.parent / "LIVEKIT_CLOUD.env"
        if not path.is_file():
            return None
        values = {}
        for line in path.read_text().splitlines():
            key, _, value = line.strip().removeprefix("export ").partition("=")
            values[key] = value.strip().strip("\"'")
        if not all(values.get(key) for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")):
            return None
        return values

    def cloud_tokens(self, room: str) -> None:
        """Sign tokens for one room here and put them on the hosts. The API
        secret of LiveKit Cloud stays on this host."""
        env = dict(os.environ, LIVEKIT_API_KEY=self.cloud["LIVEKIT_API_KEY"],
                   LIVEKIT_API_SECRET=self.cloud["LIVEKIT_API_SECRET"])
        binary = str(ROOT / "target" / "ntcmp-lk")

        def sign(identity: str, kind: str, count: int = 1) -> str:
            command = [binary, "lk-token", room, identity, kind] + ([str(count)] if count > 1 else [])
            return subprocess.run(command, env=env, capture_output=True, text=True, check=True).stdout

        publisher = sign("needletail-compare-source", "pub").strip()
        common.ssh(self.source, f"umask 077; cat >{REMOTE}/lkcloud.env", stdin=(
            f"export LK_CLOUD_URL={shlex.quote(self.cloud['LIVEKIT_URL'])}\n"
            f"export LK_CLOUD_PUB_TOKEN={shlex.quote(publisher)}\n").encode())

        def give(item: dict) -> None:
            tokens = sign(f"needletail-compare-{item['name']}", "sub", 40)
            player = sign(f"needletail-compare-{item['name']}-chrome", "sub", 8)
            common.ssh(item, f"umask 077; cat >{REMOTE}/lkcloud.tokens", stdin=tokens.encode())
            common.ssh(item, f"umask 077; cat >{REMOTE}/lkcloud.player-tokens", stdin=player.encode())
            common.ssh(item, f"umask 077; cat >{REMOTE}/lkcloud.env", stdin=(
                f"export LK_CLOUD_URL={shlex.quote(self.cloud['LIVEKIT_URL'])}\n").encode())

        common.parallel(lab.viewers(), give)


# ---- sources ----

def src(session: Session, command: str, check: bool = True, timeout: int = 120) -> str:
    return common.ssh_text(session.source, f". {REMOTE}/env; [ -f {REMOTE}/lkcloud.env ] && . "
                           f"{REMOTE}/lkcloud.env; mkdir -p {REMOTE}/src; cd {REMOTE}/src; " + command,
                           check=check, timeout=timeout)


def background(name: str, command: str) -> str:
    return (f"rm -f {name}.log {name}.status; (setsid nohup sh -c {shlex.quote(command)} "
            f">{name}.log 2>&1 </dev/null &)")


def stop_sources(session: Session) -> None:
    # By exact process name: a pattern would also match the shell that runs this.
    src(session, "pkill -x ntcmp; pkill -x ntcmp-lk; pkill -x ffmpeg; pkill -x aep1-48k-probe; "
        "pkill -x daw-test-source; sleep 1; true", check=False)


class Lane:
    """What differs between the systems: how the source reaches it and how a
    viewer reads it."""

    def __init__(self, session: Session, name: str):
        self.session = session
        self.name = name
        self.room = f"needletail-compare-{session.name}-{name}"
        self.cf_session = ""
        self.warm = 8

    # -- source --
    def start(self, rung: str) -> None:
        s = self.session
        clip = f"{REMOTE}/clips/{rung}"
        width, height = SIZES.get(rung, (3840, 2160))
        stop_sources(s)
        if self.name == "nt":
            contributor = s.node("contrib-lon")
            common.ssh(contributor, "systemctl restart needletail-contrib.service; sleep 3; "
                                    "systemctl is-active needletail-contrib.service")
            target = f"rtmp://{contributor['vlan_ipv4']}:19350/live/needletail-lab"
            src(s, background("nt", (
                f"ntcmp flv --clip {clip} --no-audio | ffmpeg -hide_banner -loglevel warning "
                f"-nostdin -f flv -i pipe:0 -c copy -f flv {target}")))
            # Lossless audio: FLAC from the same 64-second PCM, started on a
            # loop boundary so that sample 0 is sent at a multiple of 64 s.
            src(s, background("nt-audio", (
                "NOW=$(date +%s); START=$(( (NOW / 64 + 1) * 64 )); "
                "[ $(( START - NOW )) -lt 5 ] && START=$(( START + 64 )); "
                f"/usr/local/bin/aep1-48k-probe send --target {contributor['vlan_ipv4']}:27100 "
                "--session-id ${START}000000000 --group-id 0 --duration-seconds 3600 "
                f"--payload flac --channels 2 --group-channels 8 --pcm-s24le-file {REMOTE}/clips/audio.s24le "
                "--repair-percent 12 --min-repair-symbols 1")))
            self.warm = 12
        elif self.name == "cf":
            src(s, "rm -f cf.session; " + background("cf", (
                f"ntcmp pub --lane cf --clip {clip} --pace {s.pace} --session-out cf.session --status cf.status")))
            for _ in range(20):
                time.sleep(1)
                self.cf_session = src(s, "cat cf.session 2>/dev/null", check=False).strip()
                if self.cf_session:
                    break
            if not self.cf_session:
                raise RuntimeError("Cloudflare Realtime publisher has no session: "
                                   + src(s, "tail -c 400 cf.log", check=False))
        elif self.name == "whep":
            src(s, background("whep", (
                f"ntcmp pub --lane whip --url \"$CF_WHIP_URL\" --clip {clip} --pace {s.pace} --status whep.status")))
        elif self.name == "cfhls":
            src(s, background("cfhls", (
                f"ntcmp flv --clip {clip} | ffmpeg -hide_banner -loglevel warning -nostdin "
                "-f flv -i pipe:0 -c copy -f flv \"${CF_RTMPS_URL}${CF_RTMPS_KEY}\"")))
            self.warm = 45
        elif self.name == "lkregion":
            for site in SITES:
                src(s, background(f"lkregion-{site}", (
                    "LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET "
                    f"ntcmp-lk pub --lane lk --url ws://{s.ip(site + '-lk')}:7880 --room {self.room} "
                    f"--clip {clip} --pace {s.pace} --width {width} --height {height} --status lkregion-{site}.status")))
        elif self.name == "lkmulti":
            src(s, background("lkmulti", (
                "LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET "
                f"ntcmp-lk pub --lane lk --url ws://{s.ip('lon-lk')}:7890 --room {self.room} "
                f"--clip {clip} --pace {s.pace} --width {width} --height {height} --status lkmulti.status")))
        elif self.name == "lkcloud":
            src(s, background("lkcloud", (
                "LIVEKIT_TOKEN=$LK_CLOUD_PUB_TOKEN "
                f"ntcmp-lk pub --lane lk --url \"$LK_CLOUD_URL\" --room {self.room} "
                f"--clip {clip} --pace {s.pace} --width {width} --height {height} --status lkcloud.status")))
        else:
            raise ValueError(self.name)
        time.sleep(self.warm)
        if self.name == "cfhls":
            self.wait_hls()
        alive = src(s, "echo $(( $(pgrep -c -x ntcmp) + $(pgrep -c -x ntcmp-lk) ))", check=False).strip()
        if alive in ("", "0"):
            raise RuntimeError(f"{self.name}: the source is not running: "
                               + src(s, "tail -c 600 *.log", check=False))

    def wait_hls(self) -> None:
        deadline = time.time() + 90
        while time.time() < deadline:
            out = src(self.session, (
                "curl -s -m 5 \"https://$(echo $CF_WHEP_URL | cut -d/ -f3)/$CF_HLS_UID/manifest/video.m3u8\""
                " | grep -c STREAM-INF"), check=False).strip()
            if out and out != "0":
                time.sleep(10)
                return
            time.sleep(4)
        raise RuntimeError("Cloudflare Stream manifest did not become live")

    def source_status(self) -> dict:
        out = src(self.session, "for f in *.status; do [ -f $f ] && echo \"$f $(tail -1 $f)\"; done; "
                                "for f in *.log; do echo \"LOG $f $(tail -c 300 $f | tr '\\n' ' ')\"; done",
                  check=False)
        status = {"logs": {}}
        for line in out.splitlines():
            if line.startswith("LOG "):
                _, name, *rest = line.split(" ", 2)
                status["logs"][name] = rest[0] if rest else ""
            elif " " in line:
                name, raw = line.split(" ", 1)
                try:
                    status[name] = json.loads(raw)
                except json.JSONDecodeError:
                    pass
        return status

    # -- viewers --
    def entry(self, site: str) -> str:
        """Needletail entry point of a region: its first distributor."""
        return f"https://{self.session.ip(site + '-dist-a')}:19444"

    def probe(self, site: str, rung: str, seconds: int, out: str, count: int, frames: bool,
              tag: str, variant: str = "") -> str:
        # variant "h3" is Needletail read with eight part requests in flight over HTTP/3.
        s = self.session
        index = f"{REMOTE}/index/{rung}.index.json"
        common_args = (f"--duration {seconds} --report {out}.json --label {tag} --count {count} "
                       + (f"--spread-ms {min(5000, 200 * count)} " if count > 1 else "")
                       + (f"--frames {out}.csv " if frames else ""))
        if self.name == "nt" and variant == "uc":
            # RaptorQ FEC over UDP from the assigned edge, measured by the same index.
            return (f"date +%s.%N >{out}.launch; /usr/local/bin/needletail-udp-client --entry {self.entry(site)} "
                    f"--stream 1 --tls-ca {CA} --server-name {SERVER_NAME} --duration-seconds {seconds} "
                    f"--session {tag}uc --arrivals {out}.arrivals.csv --write-parts {out}.parts.bin "
                    f">{out}.uc.json 2>{out}.uc.err; ntcmp udp-ingest --arrivals {out}.arrivals.csv "
                    f"--parts {out}.parts.bin --started $(cat {out}.launch) --index {index} --lane nt-uc "
                    f"--label {tag} --client {out}.uc.json --report {out}.json "
                    + (f"--frames {out}.csv " if frames else "")
                    + f">{out}.ingest.log 2>&1; rm -f {out}.parts.bin {out}.launch")
        if self.name == "nt":
            reader = "--window 8 --http3 " if variant == "h3" else ""
            return (f"ntcmp hls --lane nt{'-h3' if variant == 'h3' else ''} --mode chase {reader}"
                    f"--url '{self.entry(site)}/live/1/master.m3u8"
                    f"?sid={tag}{variant}-{{i}}' --ca {CA} --server-name {SERVER_NAME} --index {index} "
                    + common_args)
        if self.name == "cf":
            return (f"ntcmp sub --lane cf --session {self.cf_session} --index {index} " + common_args)
        if self.name == "whep":
            return f"ntcmp sub --lane whep --url \"$CF_WHEP_URL\" --index {index} " + common_args
        if self.name == "cfhls":
            query = "?protocol=llhls" if variant != "std" else ""
            return ("ntcmp hls --lane cfhls" + ("-std" if variant == "std" else "-ll") + " --mode auto "
                    "--url \"https://$(echo $CF_WHEP_URL | cut -d/ -f3)/$CF_HLS_UID/manifest/video.m3u8"
                    f"{query}\" --timeout 10s --save {out}.units " + common_args)
        if self.name in ("lkregion", "lkmulti"):
            port = 7880 if self.name == "lkregion" else 7890
            return ("LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET "
                    f"ntcmp-lk sub --lane lk --url ws://{s.ip(site + '-lk')}:{port} --room {self.room} "
                    f"--identity {tag} --index {index} " + common_args)
        if self.name == "lkcloud":
            return (f". {REMOTE}/lkcloud.env; LIVEKIT_TOKEN_FILE={REMOTE}/lkcloud.tokens "
                    f"ntcmp-lk sub --lane lk --url \"$LK_CLOUD_URL\" --room {self.room} --index {index} "
                    + common_args)
        raise ValueError(self.name)

    def player(self, site: str, seconds: int, out: str, tag: str, variant: str = "",
               token_line: int = 1) -> str:
        s = self.session
        base = (f"node /opt/compare-player/run.mjs --duration {seconds} --out {out}.json "
                f"--label {tag} --lane {self.name}{('-' + variant) if variant else ''} --audio ")
        if self.name == "nt":
            return base + f"--kind hls --url '{self.entry(site)}/live/1/master.m3u8?sid={tag}-chrome'"
        if self.name == "cf":
            return ("export CF_REALTIME_APP_ID CF_REALTIME_APP_SECRET; " + base
                    + f"--kind cf --session {self.cf_session}")
        if self.name == "whep":
            return base + "--kind whep --url \"$CF_WHEP_URL\""
        if self.name == "cfhls":
            query = "?protocol=llhls" if variant != "std" else ""
            return base + ("--kind hls --url \"https://$(echo $CF_WHEP_URL | cut -d/ -f3)/$CF_HLS_UID/"
                           f"manifest/video.m3u8{query}\"")
        if self.name in ("lkregion", "lkmulti"):
            port = 7880 if self.name == "lkregion" else 7890
            return ("TOKEN=$(LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET ntcmp-lk "
                    f"lk-token {self.room} {tag}-chrome-$RANDOM sub); " + base
                    + f"--kind livekit --url ws://{s.ip(site + '-lk')}:{port} --token \"$TOKEN\"")
        if self.name == "lkcloud":
            return (f". {REMOTE}/lkcloud.env; TOKEN=$(sed -n {token_line}p {REMOTE}/lkcloud.player-tokens); "
                    + base + "--kind livekit --url \"$LK_CLOUD_URL\" --token \"$TOKEN\"")
        raise ValueError(self.name)

    def reader(self, site: str, seconds: int, out: str, tag: str, readers: int = 1) -> str:
        """needletail-reader, the reader of the Needletail product."""
        return (f"/usr/local/bin/needletail-reader --stream 1 --entry {self.entry(site)} "
                f"--session {tag}-reader --readers {readers} --duration-seconds {seconds} "
                f"--buffer-ms 1000 --tls-ca {CA} --server-name {SERVER_NAME} --report {out}.json")


# ---- steps ----

def run_hosts(session: Session, step: str, scripts: dict[str, str], seconds: int) -> dict:
    """Run one script on each named host at the same time and fetch the results."""
    remote = f"{REMOTE}/runs/{session.name}/{step}"
    local = session.dir / step
    local.mkdir(parents=True, exist_ok=True)

    def one(name: str) -> str:
        item = session.node(name)
        script = (f"set +e\n. {REMOTE}/env\nrm -rf {remote}; mkdir -p {remote}; cd {remote}\n"
                  + scripts[name] + "\ntrue\n")
        (local / name).mkdir(exist_ok=True)
        note = ""
        try:
            result = common.ssh(item, "bash -s", check=False, timeout=seconds + 150,
                                stdin=script.encode())
            note = result.stderr.decode(errors="replace")[-400:]
        except subprocess.TimeoutExpired:
            # Keep what the host has: stop its probes, then copy the results.
            common.ssh(item, "for p in ntcmp ntcmp-lk node chrome needletail-udp-client aep1-48k-probe "
                             "needletail-reader; do pkill -x $p; done; compare-netem clear >/dev/null 2>&1; true",
                       check=False, timeout=60)
            note = f"timed out after {seconds + 150} s; its probes were stopped and the results copied"
        try:
            common.rsync_from(item, remote + "/", str(local / name) + "/", timeout=600)
        except RuntimeError as error:
            return f"{note}; rsync failed: {error}"
        return note

    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(scripts)) as pool:
        futures = {name: pool.submit(one, name) for name in scripts}
        errors = {}
        for name, future in futures.items():
            try:
                text = future.result()
            except Exception as error:  # noqa: BLE001
                text = f"{type(error).__name__}: {error}"
            if text.strip():
                errors[name] = text.strip()
    return {"started_unix": started, "seconds": round(time.time() - started, 1), "host_errors": errors}


def step(session: Session, lane: Lane, phase: str, detail: str, rung: str, scripts: dict[str, str],
         seconds: int, extra: dict | None = None) -> None:
    name = f"{lane.name}.{phase}.{detail}"
    log(f"{name}: {len(scripts)} hosts, {seconds} s")
    result = run_hosts(session, name, scripts, seconds)
    meta = {"step": name, "lane": lane.name, "phase": phase, "detail": detail, "rung": rung,
            "session": session.name, "source": lane.source_status(), **result, **(extra or {})}
    (session.dir / name / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    session.steps.append({"step": name, "seconds": result["seconds"],
                          "host_errors": len(result["host_errors"])})
    if result["host_errors"]:
        log(f"  host errors: {json.dumps(result['host_errors'])[:500]}")


def together(*commands: str) -> str:
    return "\n".join(f"( {command} ) >/dev/null 2>>errors.log &" for command in commands if command) + "\nwait"


def baseline(session: Session, lane: Lane, rung: str, seconds: int, phase: str = "baseline",
             detail: str | None = None, netem: str | None = None, with_player: bool = True) -> None:
    scripts = {}
    for site in SITES:
        tag = f"{session.name}-{lane.name}-{site}"
        variants = ["ll", "std"] if lane.name == "cfhls" and phase == "baseline" else [""]
        if lane.name == "cfhls" and phase != "baseline":
            variants = ["ll"]
        nano, view = [], []
        for variant in variants:
            suffix = f".{variant}" if variant else ""
            nano.append(lane.probe(site, rung, seconds, f"probe{suffix}", 1, True, tag + "-nano", variant))
            view.append(lane.probe(site, rung, seconds, f"probe{suffix}", 1, True, tag + "-view", variant))
        if with_player:
            # One Chrome at a time on a host: a second would share its cores.
            first = variants[0]
            view.append(lane.player(site, seconds, "player" + (f".{first}" if first else ""),
                                    tag, first))
        if lane.name == "nt":
            view.append(lane.reader(site, seconds, "reader", tag))
        pre = f"compare-netem apply '{netem}' >netem.log 2>&1\n" if netem else ""
        post = "\ncompare-netem show >>netem.log 2>&1; compare-netem clear >>netem.log 2>&1" if netem else ""
        scripts[f"{site}-nano"] = pre + together(*nano) + post
        scripts[f"{site}-view"] = pre + together(*view) + post
        if lane.name == "cfhls" and phase == "baseline" and with_player:
            # The second Chrome, for standard HLS, after the first.
            scripts[f"{site}-view"] += "\n" + together(
                lane.player(site, seconds, "player.std", tag, "std"))
    step(session, lane, phase, detail or rung, rung, scripts,
         seconds * (2 if lane.name == "cfhls" and phase == "baseline" else 1),
         {"netem": netem})


def ttff(session: Session, lane: Lane, rung: str) -> None:
    scripts = {}
    hold = 12 if lane.name == "cfhls" else 6
    for site in SITES:
        tag = f"{session.name}-{lane.name}-{site}"
        joins = []
        for i in range(8):
            joins.append(lane.probe(site, rung, hold, f"join{i}", 1, False, f"{tag}-j{i}", "")
                         + " >/dev/null 2>>errors.log")
        scripts[f"{site}-nano"] = "\n".join(joins)
        plays = []
        for i in range(3):
            plays.append(lane.player(site, hold + 6, f"play{i}", f"{tag}-p{i}", "", token_line=i + 2)
                         + " >/dev/null 2>>errors.log")
        scripts[f"{site}-view"] = "\n".join(plays)
    step(session, lane, "ttff", rung, rung, scripts, 8 * (hold + 3))


def fanout(session: Session, lane: Lane, rung: str, seconds: int) -> None:
    for total, spread in FANOUT.items():
        scripts = {}
        for site, count in spread.items():
            tag = f"{session.name}-{lane.name}-{site}-f{total}"
            commands = [lane.probe(site, rung, seconds, "probe", count, count == 1, tag, "")]
            if lane.name == "nt":
                commands.append(lane.reader(site, seconds, "reader", tag, readers=count))
            scripts[f"{site}-view"] = (
                "S0=$(grep '^cpu ' /proc/stat)\n" + together(*commands)
                + "\necho \"$S0\" >cpu.txt; grep '^cpu ' /proc/stat >>cpu.txt")
        step(session, lane, "fanout", str(total), rung, scripts, seconds, {"viewers": total})


def carried(session: Session, step_name: str, seconds: int) -> dict:
    """Which regions received the clip whole in a throughput step: at least
    97% of the frames that the time allows, at most two incomplete, and a
    latency tail within half a second of its median."""
    out = {}
    for site in SITES:
        out[site] = any(carried_by(session, step_name, seconds, site, label) for label in ("probe", "probe.uc"))
    return out


def carried_by(session: Session, step_name: str, seconds: int, site: str, label: str) -> bool:
    """One client mode (probe label) carried the step in a region: every listener whole."""
    if True:
        verdicts = []
        for host in (f"{site}-view", f"{site}-nano"):
            path = session.dir / step_name / host / f"{label}.json"
            if not path.is_file():
                continue
            try:
                record = json.loads(path.read_text())
            except json.JSONDecodeError:
                verdicts.append(False)
                continue
            first = max(0.0, (record.get("first_key_frame_ms") or 0.0) / 1000.0)
            expected = 24 * max(1.0, seconds - first - 1.5)
            latency = record.get("video_latency_ms") or {}
            # The tail is judged after the first five seconds, as the analysis
            # does: a reader that joins at the last key frame (up to two
            # seconds back) receives its first frames late, once.
            csv_path = path.parent / f"{label}.csv"
            if csv_path.is_file():
                rows = [line.split(",") for line in csv_path.read_text().splitlines()[1:] if line.startswith("v,")]
                if rows:
                    start = min(int(r[3]) for r in rows) + 5_000_000
                    kept = sorted(int(r[2]) / 1000.0 for r in rows if int(r[3]) >= start)
                    if kept:
                        latency = {"p50": kept[len(kept) // 2], "p99": kept[min(len(kept) - 1, int(0.99 * len(kept)))]}
            verdicts.append(bool(
                not record.get("error") and (record.get("first_key_frame_ms") or -1) >= 0
                and record.get("video_frames", 0) >= 0.97 * expected
                and record.get("video_frames_incomplete", 0) <= 2
                and latency.get("p99", 1e9) - latency.get("p50", 0) < 500))
        return bool(verdicts) and all(verdicts)


def throughput(session: Session, lane: Lane, seconds: int, rungs: list[str] | None = None) -> None:
    failed = 0
    for rung in rungs or THROUGHPUT:
        try:
            lane.start(rung)
        except RuntimeError as error:
            log(f"{lane.name}.throughput.{rung}: source failed: {str(error)[:300]}")
            (session.dir / f"{lane.name}.throughput.{rung}").mkdir(exist_ok=True)
            (session.dir / f"{lane.name}.throughput.{rung}" / "meta.json").write_text(json.dumps(
                {"step": f"{lane.name}.throughput.{rung}", "lane": lane.name, "phase": "throughput",
                 "detail": rung, "rung": rung, "source_failed": str(error)[:2000]}) + "\n")
            failed += 1
            if failed >= 2:
                break
            continue
        scripts = {}
        for site in SITES:
            tag = f"{session.name}-{lane.name}-{site}"
            nano = [lane.probe(site, rung, seconds, "probe", 1, True, tag + "-nano")]
            view = [lane.probe(site, rung, seconds, "probe", 1, True, tag + "-view")]
            if lane.name == "nt":
                view.append(lane.probe(site, rung, seconds, "probe.h3", 1, True, tag + "-view", "h3"))
                view.append(lane.probe(site, rung, seconds, "probe.uc", 1, True, tag + "-view", "uc"))
                nano.append(lane.probe(site, rung, seconds, "probe.uc", 1, True, tag + "-nano", "uc"))
            if rung in CHROME_UP_TO:
                view.append(lane.player(site, seconds, "player", tag))
            if lane.name == "nt":
                view.append(lane.reader(site, seconds, "reader", tag))
            sample = ("S0=$(grep '^cpu ' /proc/stat)\n{body}\necho \"$S0\" >cpu.txt; "
                      "grep '^cpu ' /proc/stat >>cpu.txt")
            if lane.name != "lkcloud":       # LiveKit Cloud's free allowance: one viewer for each region
                scripts[f"{site}-nano"] = sample.format(body=together(*nano))
            scripts[f"{site}-view"] = sample.format(body=together(*view))
        step(session, lane, "throughput", rung, rung, scripts, seconds)
        verdict = carried(session, f"{lane.name}.throughput.{rung}", seconds)
        log(f"  carried whole: {' '.join(site for site, ok in verdict.items() if ok) or 'nowhere'}")
        failed = 0 if any(verdict.values()) else failed + 1
        if failed >= 2:
            log(f"{lane.name}: two rungs in a row reached no region whole; the sweep ends")
            break


def run_lane(session: Session, name: str, phases: list[str], seconds: int) -> None:
    lane = Lane(session, name)
    if name == "lkcloud":
        if session.cloud is None:
            log("lkcloud: ~/wavey.ai/LIVEKIT_CLOUD.env is not filled in; the lane is skipped")
            return
        session.cloud_tokens(lane.room)
    base_phases = [phase for phase in phases if phase in ("baseline", "ttff", "impair", "fanout")]
    try:
        if base_phases:
            lane.start(BASE_RUNG)
            if "baseline" in phases:
                baseline(session, lane, BASE_RUNG, seconds)
            if "ttff" in phases:
                ttff(session, lane, BASE_RUNG)
            if "impair" in phases:
                for label, spec in IMPAIRMENTS.items():
                    baseline(session, lane, BASE_RUNG, 45, phase="impair", detail=label, netem=spec)
            if "fanout" in phases:
                fanout(session, lane, BASE_RUNG, 40)
        if "throughput" in phases:
            throughput(session, lane, 30 if name == "lkcloud" else 40, session.rungs)
        if "ramp" in phases:
            rungs = RAMP if name != "cfhls" else ["1080p6", "2160p16"]
            for rung in rungs:
                try:
                    lane.start(rung)
                except RuntimeError as error:
                    log(f"{name}.ramp.{rung}: source failed: {str(error)[:300]}")
                    (session.dir / f"{name}.ramp.{rung}").mkdir(exist_ok=True)
                    (session.dir / f"{name}.ramp.{rung}" / "meta.json").write_text(json.dumps(
                        {"step": f"{name}.ramp.{rung}", "lane": name, "phase": "ramp", "rung": rung,
                         "source_failed": str(error)[:2000]}) + "\n")
                    continue
                baseline(session, lane, rung, 40, phase="ramp", with_player=True)
    finally:
        stop_sources(session)
        for item in lab.viewers():
            common.ssh(item, "compare-netem clear >/dev/null 2>&1; pkill -f run.mjs; pkill -x chrome; "
                             "pkill -x ntcmp; pkill -x ntcmp-lk; true", check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", required=True, help="name of the session, e.g. a1")
    parser.add_argument("--lanes", default=",".join(lane for lane in LANES if lane != "lkcloud"))
    parser.add_argument("--phases", default="baseline,ttff,impair,fanout,ramp")
    parser.add_argument("--seconds", type=int, default=60, help="length of the baseline")
    parser.add_argument("--rungs", default="", help="rungs of the throughput sweep (default all)")
    parser.add_argument("--pace", type=float, default=2.5,
                        help="a WebRTC sender spreads a frame at this multiple of the clip bit rate")
    args = parser.parse_args()
    refuse_livekit_cloud(args.lanes.split(","))
    session = Session(args.session)
    session.rungs = args.rungs.split(",") if args.rungs else None
    session.pace = args.pace
    started = time.time()
    sampled = start_samplers(session)
    for name in args.lanes.split(","):
        log(f"lane {name}")
        try:
            run_lane(session, name, args.phases.split(","), args.seconds)
        except Exception as error:  # noqa: BLE001 - one lane must not end the session
            log(f"lane {name} failed: {type(error).__name__}: {str(error)[:600]}")
    collect_samplers(session, sampled)
    record = {"session": args.session, "lanes": args.lanes, "phases": args.phases,
              "started_unix": started, "ended_unix": time.time(), "lab_run": session.state["run_id"],
              "steps": session.steps}
    existing = session.dir / "session.json"
    history = json.loads(existing.read_text()) if existing.is_file() else []
    history.append(record)
    existing.write_text(json.dumps(history, indent=1) + "\n")
    log(f"session {args.session}: {len(session.steps)} steps in {(time.time() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
