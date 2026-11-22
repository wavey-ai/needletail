#!/usr/bin/env python3
"""Run the audio comparison on the lab: Opus at the smallest frames each
system forwards, and lossless 48 kHz / 24-bit stereo PCM.

Every system receives the same 64-second loop from lon-src at the same time,
and the same listeners in London, New York, Tokyo and Sydney hear all of them
at once, so every system is measured under the same network conditions. The
loop is locked to the wall clock and every sender sends a unit when its last
sample has been spoken (tools/ntcmp/audio.go), so a listener knows the age of
every sample it receives.

Lanes
  cf        Cloudflare Realtime (SFU): an Opus track; PCM blocks on a data channel
  whep      Cloudflare Stream WHIP/WHEP: an Opus track (it has no data channel)
  lkregion  LiveKit self-hosted, one SFU in each region: Opus track; PCM data packets
  lkmulti   LiveKit self-hosted, multi-region set on one Redis: the same
  lkcloud   LiveKit Cloud: the same
  nt-popus  Needletail, Opus in 2.5 ms parts (120 samples, CELT) that its AEP1
            sender encodes from the same loop (stream 3001)
  nt-opus   Needletail, SoundKit Opus from its DAW source (stream 3001), one
            120-sample (2.5 ms) frame per part; --nt-opus-frame-samples 48
            (or 72) gives 1 ms (1.5 ms) Opus Custom frames instead
  nt-flac   Needletail, FLAC from its DAW source (stream 2001), one 48-sample
            (1 ms) epoch per part; the DAW source runs with 64-frame host
            buffers (NT_DAW, recorded in each step's meta)
  nt-pcm    Needletail, 24-bit PCM from its AEP1 sender, one 48-sample (1 ms)
            epoch per part (--nt-pcm-samples; stream 4001)
  nt-uc     Needletail's RaptorQ FEC UDP client mode (needletail-udp-client):
            assigned an edge by the site's distributor, it writes the arrival
            and the bytes of every part (--arrivals, --write-parts), and
            `ntcmp udp-ingest` measures them with the index of the lane, exactly
            as every other lane is measured
  nt-udp    the same client's own JSON report only (publish -> arrival)
  nt-reader the product's needletail-reader, 25 in each region (--product-reader)

Configurations: opus-2.5, opus-5, opus-10, opus-20 (the frame length of the
WebRTC senders), opus-max510 (510 kbit/s, the nearest that Opus comes to
lossless) and pcm. Needletail parts are one producer frame each, the smallest
by default: it publishes one rendition in each configuration, Opus 2.5 ms
from the AEP1 sender with opus-2.5 (or from its DAW source when nt-popus is
not among --lanes), DAW Opus with opus-5 and opus-10, FLAC with opus-20 and
opus-max510, PCM with pcm.

Phases
  baseline  60 s of probes on both listener hosts of each region, and
            headless Chrome on the viewer host for each WebRTC system in turn
  impair    the same netem on every listener host, for each impairment
  fanout    1, 10 and 100 listeners
  attempts  ask each WebRTC system for an L16 and an L24 audio track

Results: target/compare/runs/SESSION/audio.CONFIG.PHASE.DETAIL/HOST/LANE.*.
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
AUDIO = f"{REMOTE}/audio"
INDEX = f"{REMOTE}/index"
SITES = run.SITES
CONFIGS = ["opus-2.5", "opus-5", "opus-10", "opus-20", "opus-max510", "pcm"]
WEBRTC = ["cf", "whep", "lkregion", "lkmulti", "lkcloud"]
NEEDLETAIL = {"nt-popus": (3001, "nt-popus-120s-la"), "nt-opus": (3001, run.lab.daw_index("nt-opus")[0]),
              "nt-flac": (2001, run.lab.daw_index("nt-flac")[0]), "nt-pcm": (4001, "nt-pcm-48s")}
SAMPLE_RATE = 48_000
# Samples of one AEP1 epoch (one part) of the Needletail lanes that the AEP1
# sender feeds; --nt-pcm-samples changes PCM's (24, 12: the halvings).
NT_EPOCH_SAMPLES = {"nt-popus": 120, "nt-pcm": 48}
NT_PCM_SAMPLES = (48, 24, 12)
# The DAW source's epochs and host buffer, which compare-audio gives it on its
# command line and records in each step's meta (nt_daw); its lanes' parts
# are one Opus frame (nt-opus) or one lossless epoch (nt-flac).
NT_DAW = run.lab.NT_DAW
NT_DAW_SAMPLES = {"nt-opus": "opus_frame_samples", "nt-flac": "lossless_epoch_samples"}


def set_nt_opus_frame(samples: int) -> None:
    """Give the DAW source's Opus lane frames of `samples`: its command line,
    its index and the frame recorded in each step's meta follow."""
    NT_DAW["opus_frame_samples"] = samples
    NEEDLETAIL["nt-opus"] = (3001, run.lab.daw_index("nt-opus")[0])


def set_nt_popus(samples: int) -> None:
    """Give the AEP1 sender's Opus lane (nt-popus) frames of `samples`: 120
    (2.5 ms, standard Opus) or 48 (1 ms Opus Custom); its index follows."""
    NT_EPOCH_SAMPLES["nt-popus"] = samples
    NEEDLETAIL["nt-popus"] = (3001, f"nt-popus-{samples}s-la")


def daw_command(target: str) -> str:
    """The DAW source of the Needletail DAW lanes, with its epochs and host
    buffer given (not left to the defaults of the build that is installed)."""
    return ("env RUST_LOG=warn DAW_TEST_SOURCE_START_UNIX_NS=${START}000000000 daw-test-source "
            f"--direct-contributor --buffer-frames {NT_DAW['buffer_frames']} "
            f"--opus-frame-samples {NT_DAW['opus_frame_samples']} "
            f"--lossless-epoch-samples {NT_DAW['lossless_epoch_samples']} "
            f"--prepared-pcm {target} 1900 {REMOTE}/daw-fixture")
UDP_LANE = "nt-udp"
UC_LANE = "nt-uc"
SFU_PCM_MS = 2.5
UDP_CLIENT = "/usr/local/bin/needletail-udp-client"
# The renditions the contributor publishes. On its own g6-dedicated-4 it
# publishes all three at once (--nt-formats); give one format to publish one
# rendition at a time, as the first pass on a 1 vCPU Nanode had to.
NT_FORMATS = "flac,opus,pcm"
# The candidates of each configuration, in order: the first among --lanes.
NT_RENDITION = {"opus-2.5": ("nt-popus", "nt-opus"), "opus-5": ("nt-opus",), "opus-10": ("nt-opus",),
                "opus-20": ("nt-flac",), "opus-max510": ("nt-flac",), "pcm": ("nt-pcm",)}
NT_FORMAT = {"nt-popus": "opus", "nt-opus": "opus", "nt-flac": "flac", "nt-pcm": "pcm"}
# Three readers of each Needletail rendition:
#   (none)  5 ms of parts in flight, HTTP/2: the one request for each part of
#           the 5 ms parts before (2 parts at 2.5 ms, 5 at 1 ms, 20 at
#           0.25 ms). One request for each part reads one part per round
#           trip, so it falls behind parts shorter than the round trip and
#           measures its own backlog.
#   -w      40 requests in flight (100 ms of 2.5 ms parts, 40 ms of 1 ms), HTTP/2
#   -h3     40 requests in flight, HTTP/3
NT_SINGLE_WINDOW_MS = 5.0
NT_READERS = {"": None, "-w": "--window 40", "-h3": "--window 40 --http3"}


def reader_flags(suffix: str, frame_ms: float) -> str:
    """ntcmp hls window flags of a Needletail reader lane (`""`, `-w`,
    `-h3`) whose parts are frame_ms long."""
    if NT_READERS[suffix] is not None:
        return NT_READERS[suffix]
    parts = -(-round(NT_SINGLE_WINDOW_MS * 1000) // max(1, round(frame_ms * 1000)))
    return f"--window {max(1, min(400, parts))}"
FANOUT = {1: {"lon": 1}, 10: {"lon": 3, "new": 3, "tok": 2, "syd": 2},
          100: {"lon": 25, "new": 25, "tok": 25, "syd": 25}}
CLOUD_PER_HOST = 24     # LiveKit Cloud's free plan allows 100 connections in all


def epoch_ns(samples: int) -> int:
    return round(samples * 1_000_000_000 / SAMPLE_RATE)


def nt_index(base: str, pcm_samples: int) -> str:
    """The ntcmp index of a Needletail lane (compare_lab.py audio makes them)."""
    if base == "nt-pcm":
        return f"nt-pcm-{pcm_samples}s"
    return NEEDLETAIL[base][1]


def aep1_command(target: str, base: str, samples: int, audio: str) -> str:
    """The AEP1 sender of a Needletail lane. It sends epoch k when its last
    sample has been spoken: the session id is the loop START plus one epoch."""
    payload = NT_FORMAT[base]
    return (f"/usr/local/bin/aep1-48k-probe send --target {target} "
            f"--session-id ${{START}}{epoch_ns(samples):09d} --group-id 0 --duration-seconds 1900 "
            f"--payload {payload} --epoch-samples {samples} "
            f"--channels 2 --group-channels 8 --pcm-s24le-file {audio} "
            "--repair-percent 12 --min-repair-symbols 1")


def udp_command(entry: str, stream: int, seconds: int, tag: str) -> str:
    """needletail-udp-client of a Needletail stream: its JSON report."""
    return (f"needletail-udp-client --entry {entry} --stream {stream} --tls-ca {run.CA} "
            f"--server-name {run.SERVER_NAME} --duration-seconds {seconds} "
            f"--session {tag}-{UDP_LANE} >{UDP_LANE}.udp.json 2>{UDP_LANE}.udp.err")


class Audio:
    def __init__(self, session: run.Session, config: str, lanes: list[str], pcm_samples: int = 48):
        self.session = session
        self.config = config
        self.pcm_samples = pcm_samples
        self.pcm = config == "pcm"
        self.lanes = [lane for lane in lanes if lane in WEBRTC and not (self.pcm and lane == "whep")]
        if session.cloud is None and "lkcloud" in self.lanes:
            self.lanes.remove("lkcloud")
            run.log("lkcloud: ~/wavey.ai/LIVEKIT_CLOUD.env is not filled in; the lane is skipped")
        self.nt_base = [lane for lane in NT_RENDITION[config] if lane in lanes][:1]
        self.nt = [lane + suffix for lane in self.nt_base for suffix in NT_READERS]
        if self.nt_base and UDP_LANE in lanes:
            self.nt.append(UDP_LANE)
        if self.nt_base and UC_LANE in lanes:
            self.nt.append(UC_LANE)
        self.room = f"needletail-compare-{session.name}-{config.replace('.', '')}"
        self.cf_session = ""
        # The SFUs' PCM blocks (--sfu-pcm-ms): 2.5 ms by default, 1 ms to match
        # Needletail's smallest parts (the 1 ms index is Needletail's PCM one).
        sfu_index = "pcm-2.5ms" if SFU_PCM_MS == 2.5 else f"nt-pcm-{round(SFU_PCM_MS * 48)}s"
        self.index = f"{INDEX}/{sfu_index}.index.json" if self.pcm else f"{INDEX}/{config}.index.json"

    # ---- sources ----
    def start(self) -> None:
        s = self.session
        run.stop_sources(s)
        run.src(s, "pkill -x daw-test-source; pkill -x aep1-48k-probe; true", check=False)
        media = (f"--audio {AUDIO}/audio.s24le --data --pcm-ms {SFU_PCM_MS:g}" if self.pcm
                 else f"--audio {AUDIO}/{self.config}.opus")
        if self.nt:
            contributor = s.node("contrib-lon")
            wanted = NT_FORMATS if "," in NT_FORMATS else NT_FORMAT[self.nt_base[0]]
            settings = json.loads((common.ARTIFACT_DIR / "settings.json").read_text())
            if settings.get("daw_hls_formats") != wanted:
                subprocess.run([sys.executable, str(HERE.parent / "linode-small-mesh-deploy.py"), "restart",
                                "--roles", "contributor", "--set", f"daw_hls_formats={wanted}"],
                               check=True, capture_output=True)
            else:
                common.ssh(contributor, "systemctl restart needletail-contrib.service; sleep 3; "
                                        "systemctl is-active needletail-contrib.service")
            target = f"{contributor['vlan_ipv4']}:27100"
            # Both producers start on a boundary of the 64-second loop. The DAW
            # source delivers its buffers when they are complete; the AEP1
            # sender is started one epoch later so that it sends an epoch when
            # its last sample has been spoken.
            base = self.nt_base[0]
            daw = run.background("nt-daw", daw_command(target))
            producer = daw
            if base in NT_EPOCH_SAMPLES:
                producer = run.background(base, aep1_command(
                    target, base, self.epoch_samples(base), f"{AUDIO}/audio.s24le"))
            run.src(s, (
                "NOW=$(date +%s); START=$(( (NOW / 64 + 1) * 64 )); "
                "[ $(( START - NOW )) -lt 8 ] && START=$(( START + 64 )); export START; "
                "echo $START >nt.start; " + producer))
        if "cf" in self.lanes:
            run.src(s, "rm -f cf.session; " + run.background(
                "cf", f"ntcmp pub --lane cf {media} --session-out cf.session --status cf.status"))
        if "whep" in self.lanes:
            run.src(s, run.background(
                "whep", f"ntcmp pub --lane whip --url \"$CF_WHIP_URL\" {media} --status whep.status"))
        keys = "LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET"
        if "lkregion" in self.lanes:
            for site in SITES:
                run.src(s, run.background(f"lkregion-{site}", (
                    f"{keys} ntcmp-lk pub --lane lk --url ws://{s.ip(site + '-lk')}:7880 "
                    f"--room {self.room} {media} --status lkregion-{site}.status")))
        if "lkmulti" in self.lanes:
            run.src(s, run.background("lkmulti", (
                f"{keys} ntcmp-lk pub --lane lk --url ws://{s.ip('lon-lk')}:7890 "
                f"--room {self.room} {media} --status lkmulti.status")))
        if "lkcloud" in self.lanes:
            s.cloud_tokens(self.room)
            run.src(s, run.background("lkcloud", (
                "LIVEKIT_TOKEN=$LK_CLOUD_PUB_TOKEN ntcmp-lk pub --lane lk --url \"$LK_CLOUD_URL\" "
                f"--room {self.room} {media} --status lkcloud.status")))
        if "cf" in self.lanes:
            for _ in range(20):
                time.sleep(1)
                self.cf_session = run.src(s, "cat cf.session 2>/dev/null", check=False).strip()
                if self.cf_session:
                    break
            if not self.cf_session:
                run.log("cf: no publisher session: " + run.src(s, "tail -c 300 cf.log", check=False))
                self.lanes.remove("cf")
        if self.nt:
            wait = int(run.src(s, "echo $(( $(cat nt.start) - $(date +%s) ))").strip() or 0)
            time.sleep(max(0, wait) + 12)
        else:
            time.sleep(8)
        alive = run.src(s, "pgrep -c -x ntcmp; pgrep -c -x ntcmp-lk; pgrep -c -x daw-test-source; "
                           "pgrep -c -x aep1-48k-probe", check=False).split()
        run.log(f"{self.config}: sources running (ntcmp, ntcmp-lk, daw, aep1): {' '.join(alive)}")

    def epoch_samples(self, base: str) -> int:
        return self.pcm_samples if base == "nt-pcm" else NT_EPOCH_SAMPLES[base]

    def nt_frame_ms(self) -> dict:
        """The frame of each Needletail lane of this configuration, for the
        analysis (one part is one frame)."""
        out = {}
        for base in self.nt_base:
            if base in NT_EPOCH_SAMPLES:
                out[base] = self.epoch_samples(base) * 1000 / SAMPLE_RATE
            else:
                out[base] = NT_DAW[NT_DAW_SAMPLES[base]] * 1000 / SAMPLE_RATE
        return out

    def status(self) -> dict:
        return run.Lane(self.session, "audio").source_status()

    # ---- listeners ----
    def probe(self, lane: str, site: str, seconds: int, count: int, tag: str) -> str:
        s = self.session
        if lane == "lkcloud" and count > CLOUD_PER_HOST:
            count = CLOUD_PER_HOST
        # A crowd arrives over ten seconds at most: 2.5 listeners a second on each host.
        tail = (f"--duration {seconds} --skip-seconds {5 if count == 1 else 12} "
                f"--report {lane}.probe.json --label {tag} "
                f"--count {count} " + (f"--spread-ms {min(10000, 400 * count)} " if count > 1 else "")
                + (f"--frames {lane}.probe.csv " if count == 1 else ""))
        data = "--data " if self.pcm else ""
        if lane == UC_LANE:
            return self.udp_client(site, seconds, count, tag)
        if lane == UDP_LANE:
            # One client: it has no --count, and it is not in the fan-out.
            stream = NEEDLETAIL[self.nt_base[0]][0]
            return udp_command(f"https://{s.ip(site + '-dist-a')}:19444", stream, seconds, tag)
        if lane.startswith("nt-"):
            base = "-".join(lane.split("-")[:2])
            stream = NEEDLETAIL[base][0]
            index = nt_index(base, self.pcm_samples)
            entry = f"https://{s.ip(site + '-dist-a')}:19444"
            flags = reader_flags(lane[len(base):], self.nt_frame_ms().get(base, NT_SINGLE_WINDOW_MS))
            return (f"ntcmp hls --lane {lane} --mode chase {flags} "
                    f"--url '{entry}/live/{stream}/master.m3u8"
                    f"?sid={tag}-{lane}-{{i}}' --ca {run.CA} --server-name {run.SERVER_NAME} "
                    f"--audio-index {INDEX}/{index}.index.json " + tail)
        if lane == "cf":
            return (f"ntcmp sub --lane cf --session {self.cf_session} --no-video {data}"
                    f"--audio-index {self.index} " + tail)
        if lane == "whep":
            return (f"ntcmp sub --lane whep --url \"$CF_WHEP_URL\" --no-video "
                    f"--audio-index {self.index} " + tail)
        if lane in ("lkregion", "lkmulti"):
            port = 7880 if lane == "lkregion" else 7890
            return ("LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET "
                    f"ntcmp-lk sub --lane lk --url ws://{s.ip(site + '-lk')}:{port} --room {self.room} "
                    f"--identity {tag}-{lane} {data}--audio-index {self.index} " + tail)
        if lane == "lkcloud":
            return (f". {REMOTE}/lkcloud.env; LIVEKIT_TOKEN_FILE={REMOTE}/lkcloud.tokens "
                    f"ntcmp-lk sub --lane lk --url \"$LK_CLOUD_URL\" --room {self.room} {data}"
                    f"--audio-index {self.index} " + tail)
        raise ValueError(lane)

    def udp_client(self, site: str, seconds: int, count: int, tag: str) -> str:
        """needletail-udp-client: RaptorQ FEC over UDP from the assigned edge,
        measured by `ntcmp udp-ingest` with the index of the lane."""
        base = self.nt_base[0]
        stream = NEEDLETAIL[base][0]
        index = nt_index(base, self.pcm_samples)
        entry = f"https://{self.session.ip(site + '-dist-a')}:19444"
        out = []
        for i in range(count):
            name = UC_LANE if count == 1 else f"{UC_LANE}-{i:03d}"
            report = f"{UC_LANE}.probe" if count == 1 else f"{UC_LANE}.probe-{i:03d}"
            out.append(
                f"( date +%s.%N >{name}.launch; {UDP_CLIENT} --entry {entry} --stream {stream} "
                f"--tls-ca {run.CA} --server-name {run.SERVER_NAME} --duration-seconds {seconds} "
                f"--session {tag}-uc-{i} --arrivals {name}.arrivals.csv --write-parts {name}.parts.bin "
                f">{name}.uc.json 2>{name}.uc.err; "
                f"ntcmp udp-ingest --arrivals {name}.arrivals.csv --parts {name}.parts.bin "
                f"--started $(cat {name}.launch) --audio-index {INDEX}/{index}.index.json "
                f"--skip-seconds {5 if count == 1 else 12} --lane {UC_LANE} --label {tag} "
                f"--client {name}.uc.json --report {report}.json "
                + (f"--frames {report}.csv " if count == 1 else "")
                + f">>{name}.ingest.log 2>&1; rm -f {name}.parts.bin {name}.launch )")
        return " &\n".join(out) + (" &\nwait" if count > 1 else "")

    def product_reader(self, site: str, seconds: int, count: int, tag: str) -> str:
        """needletail-reader itself on the audio stream: the reader of the product."""
        s = self.session
        return (f"/usr/local/bin/needletail-reader --stream {NEEDLETAIL[self.nt_base[0]][0]} "
                f"--entry https://{s.ip(site + '-dist-a')}:19444 --session {tag}-product "
                f"--readers {count} --arrival-spread-ms {min(10000, 400 * count)} "
                f"--duration-seconds {seconds} --buffer-ms 1000 --tls-ca {run.CA} "
                f"--server-name {run.SERVER_NAME} --report nt-reader.reader.json")

    def player(self, lane: str, site: str, seconds: int, tag: str, line: int) -> str:
        s = self.session
        # The listener is tuned for latency too: the smallest jitter buffer
        # target that Chrome accepts.
        base = (f"node /opt/compare-player/run.mjs --duration {seconds} --out {lane}.player.json "
                f"--label {tag} --lane {lane} --audio --audio-only --jitter-target 0 ")
        if lane == "cf":
            return ("export CF_REALTIME_APP_ID CF_REALTIME_APP_SECRET; " + base
                    + f"--kind cf --session {self.cf_session}")
        if lane == "whep":
            return base + "--kind whep --url \"$CF_WHEP_URL\""
        if lane in ("lkregion", "lkmulti"):
            port = 7880 if lane == "lkregion" else 7890
            return ("TOKEN=$(LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET ntcmp-lk "
                    f"lk-token {self.room} {tag}-chrome-$RANDOM sub); " + base
                    + f"--kind livekit --url ws://{s.ip(site + '-lk')}:{port} --token \"$TOKEN\"")
        if lane == "lkcloud":
            return (f". {REMOTE}/lkcloud.env; TOKEN=$(sed -n {line}p {REMOTE}/lkcloud.player-tokens); "
                    + base + "--kind livekit --url \"$LK_CLOUD_URL\" --token \"$TOKEN\"")
        raise ValueError(lane)

    # ---- steps ----
    def step(self, phase: str, detail: str, seconds: int, chrome: int, netem: str | None,
             fan: dict | None = None, token_line: int = 1, only: list[str] | None = None) -> None:
        s = self.session
        scripts = {}
        lanes = [lane for lane in self.lanes + self.nt if only is None or lane in only]
        if only and "nt-reader" in only and self.nt_base:
            lanes.append("nt-reader")
        if not lanes:
            return
        for site in (fan or {name: 1 for name in SITES}):
            tag = f"{s.name}-{site}"
            count = (fan or {}).get(site, 1)
            hosts = [f"{site}-view"] if fan else [f"{site}-nano", f"{site}-view"]
            for host in hosts:
                probes = [self.probe(lane, site, seconds, count, f"{tag}-{host[4:]}") for lane in lanes
                          if lane != "nt-reader"]
                if "nt-reader" in lanes:
                    probes.append(self.product_reader(site, seconds, count, f"{tag}-{host[4:]}"))
                script = run.together(*probes) if not (chrome and host.endswith("-view")) else (
                    "\n".join(f"( {probe} ) >/dev/null 2>>errors.log &" for probe in probes))
                if chrome and host.endswith("-view"):
                    script += "\n" + "\n".join(
                        self.player(lane, site, chrome, tag, token_line) + " >/dev/null 2>>errors.log"
                        for lane in self.lanes if lane in lanes) + "\nwait"
                if netem:
                    script = (f"compare-netem apply '{netem}' >netem.log 2>&1\n" + script
                              + "\ncompare-netem show >>netem.log 2>&1; compare-netem clear >>netem.log 2>&1")
                scripts[host] = script
        name = f"audio.{self.config}.{phase}.{detail}"
        total = max(seconds, chrome * len(self.lanes) + 10 if chrome else 0)
        run.log(f"{name}: {len(scripts)} hosts, {total} s, lanes {' '.join(lanes)}")
        result = run.run_hosts(s, name, scripts, total)
        meta = {"step": name, "config": self.config, "phase": phase, "detail": detail, "netem": netem,
                "lanes": lanes, "nt_frame_ms": self.nt_frame_ms(),
                "nt_uc_base": self.nt_base[0] if self.nt_base else None,
                # ntcmp sends a WebRTC Opus unit when its encoder has the last
                # input sample (tools/ntcmp/audio.go, the live rule).
                "webrtc_opus_send": "live",
                "nt_pcm_epoch_samples": self.pcm_samples, "nt_formats": NT_FORMATS,
                "sfu_pcm_ms": SFU_PCM_MS if self.pcm else None,
                "nt_daw": {**NT_DAW, "opus_lookahead_samples":
                           run.lab.opus_lookahead_samples(int(NT_DAW["opus_frame_samples"]))},
                "nt_popus_samples": NT_EPOCH_SAMPLES["nt-popus"],
                "nt_reader_flags": {lane: reader_flags(lane[len(base):], frame)
                                    for base, frame in self.nt_frame_ms().items()
                                    for lane in lanes if lane.startswith(base)
                                    and lane[len(base):] in NT_READERS},
                "listeners": sum((fan or {}).values()) if fan else None,
                "session": s.name, "source": self.status(), **result}
        (s.dir / name / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
        s.steps.append({"step": name, "seconds": result["seconds"]})
        if result["host_errors"]:
            run.log(f"  host errors: {json.dumps(result['host_errors'])[:400]}")


def attempts(session: run.Session) -> None:
    """Ask each WebRTC system for an uncompressed audio track, and keep its answer."""
    out = []
    s = session
    room = f"needletail-compare-{s.name}-attempt"
    if s.cloud is not None:
        s.cloud_tokens(room)
    keys = "LIVEKIT_API_KEY=$LK_SELF_KEY LIVEKIT_API_SECRET=$LK_SELF_SECRET"
    for mime in ("audio/L16", "audio/L24"):
        commands = {
            "cf": f"ntcmp pub --lane cf --codec-attempt {mime} --duration 12",
            "whep": f"ntcmp pub --lane whip --url \"$CF_WHIP_URL\" --codec-attempt {mime} --duration 12",
            "lkself": (f"{keys} ntcmp-lk pub --lane lk --url ws://{s.ip('lon-lk')}:7880 --room {room} "
                       f"--codec-attempt {mime} --duration 12 2>lk-attempt.err"),
        }
        if s.cloud is not None:
            commands["lkcloud"] = ("LIVEKIT_TOKEN=$LK_CLOUD_PUB_TOKEN ntcmp-lk pub --lane lk "
                                   f"--url \"$LK_CLOUD_URL\" --room {room} --codec-attempt {mime} "
                                   "--duration 12 2>lk-attempt.err")
        for lane, command in commands.items():
            text = run.src(s, command + "; true", check=False, timeout=90)
            try:
                record = json.loads(text[text.index("{"):])
            except ValueError:
                record = {"raw": text[-600:]}
            if lane.startswith("lk"):
                record["client_log"] = run.src(
                    s, "grep -iE 'published track|codec|remote description' lk-attempt.err | "
                       "grep -v 'pion.ice' | cut -c1-260 | tail -4", check=False)[-900:]
            record["system"], record["mime"] = lane, mime
            out.append(record)
            run.log(f"attempt {lane} {mime}: {json.dumps(record)[:260]}")
    (s.dir / "codec-attempts.json").write_text(json.dumps(out, indent=1) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", required=True)
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--lanes", default=",".join(WEBRTC + list(NEEDLETAIL) + [UC_LANE]))
    parser.add_argument("--nt-pcm-samples", type=int, default=NT_EPOCH_SAMPLES["nt-pcm"],
                        choices=NT_PCM_SAMPLES, help="samples of one Needletail PCM part (one epoch)")
    parser.add_argument("--nt-opus-samples", type=int, default=120, choices=(48, 120),
                        help="the AEP1 sender's Opus frame (one nt-popus part): 120 (2.5 ms, "
                             "standard Opus) or 48 (1 ms Opus Custom)")
    parser.add_argument("--nt-opus-frame-samples", type=int, default=NT_DAW["opus_frame_samples"],
                        choices=run.lab.NT_OPUS_FRAME_SAMPLES,
                        help="the DAW source's Opus frame (one nt-opus part): 120 (2.5 ms, "
                             "standard Opus) by default, 48 or 72 for 1 or 1.5 ms Opus Custom")
    parser.add_argument("--phases", default="baseline,impair,fanout,attempts")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--chrome-seconds", type=int, default=25)
    parser.add_argument("--nt-formats", default=NT_FORMATS,
                        help="renditions the contributor publishes; one format publishes one at a time")
    parser.add_argument("--window", type=int, default=40,
                        help="part requests that the pipelined readers of Needletail keep in flight")
    parser.add_argument("--sfu-pcm-ms", type=float, default=2.5, choices=(2.5, 1.0),
                        help="PCM block length on the SFUs' data channels")
    parser.add_argument("--product-reader", action="store_true",
                        help="also run needletail-reader itself with 25 readers in each region")
    args = parser.parse_args()
    globals()["NT_FORMATS"] = args.nt_formats
    globals()["SFU_PCM_MS"] = args.sfu_pcm_ms
    NT_READERS["-w"] = f"--window {args.window}"
    NT_READERS["-h3"] = f"--window {args.window} --http3"
    set_nt_opus_frame(args.nt_opus_frame_samples)
    set_nt_popus(args.nt_opus_samples)
    session = run.Session(args.session)
    phases = args.phases.split(",")
    started = time.time()
    sampled = run.start_samplers(session)
    try:
        if "attempts" in phases:
            attempts(session)
        for config in args.configs.split(","):
            audio = Audio(session, config, args.lanes.split(","), args.nt_pcm_samples)
            full = config in ("opus-2.5", "pcm")       # the two lanes of the comparison
            try:
                audio.start()
                chrome = 0 if audio.pcm else args.chrome_seconds
                if "baseline" in phases:
                    audio.step("baseline", "clean", args.seconds, chrome, None)
                if "impair" in phases and full:
                    for label, spec in run.IMPAIRMENTS.items():
                        audio.step("impair", label, 35, 20 if chrome else 0, spec, token_line=2)
                if "fanout" in phases and full:
                    # Two passes, so that systems that share a node are not
                    # loaded together: the two self-hosted LiveKit lanes share
                    # the London Nanode, and the Needletail readers share the edges.
                    first = [lane for lane in audio.lanes if lane != "lkmulti"] + audio.nt_base
                    second = ["lkmulti", UC_LANE] + [lane + "-h3" for lane in audio.nt_base]
                    for total, spread in FANOUT.items():
                        audio.step("fanout", str(total), 40, 0, None, fan=spread, only=first)
                        audio.step("fanoutb", str(total), 40, 0, None, fan=spread, only=second)
                        if args.product_reader and total == 100:
                            audio.step("fanoutc", str(total), 40, 0, None, fan=spread, only=["nt-reader"])
            except Exception as error:  # noqa: BLE001 - one configuration must not end the session
                run.log(f"{config} failed: {type(error).__name__}: {str(error)[:500]}")
    finally:
        run.stop_sources(session)
        run.src(session, "pkill -x daw-test-source; pkill -x aep1-48k-probe; true", check=False)
        for item in run.lab.viewers():
            common.ssh(item, "compare-netem clear >/dev/null 2>&1; pkill -f run.mjs; pkill -x chrome; "
                             "pkill -x ntcmp; pkill -x ntcmp-lk; true", check=False)
    run.collect_samplers(session, sampled)
    record = {"session": args.session, "kind": "audio", "configs": args.configs, "lanes": args.lanes,
              "pipelined_window_parts": args.window, "nt_formats": args.nt_formats,
              "nt_pcm_epoch_samples": args.nt_pcm_samples,
              "phases": args.phases, "started_unix": started, "ended_unix": time.time(),
              "lab_run": session.state["run_id"], "steps": session.steps}
    path = session.dir / "session.json"
    history = json.loads(path.read_text()) if path.is_file() else []
    history.append(record)
    path.write_text(json.dumps(history, indent=1) + "\n")
    run.log(f"audio session {args.session}: {len(session.steps)} steps in "
            f"{(time.time() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
