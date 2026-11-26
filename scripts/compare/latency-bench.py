#!/usr/bin/env python3
"""Measure where the time of an audio part goes, on the local namespace lab.

One run: start the lab of scripts/edge_lab.py (tc netem links, a node has 2
CPUs and 4 GB, the contributor 4 and 8) with a given set of extra service
flags, so that each low-latency change can be switched off and on; send the
64-second loop with the AEP1 sender (aep1-48k-probe send, as
compare-audio.py does on lon-src) from the contributor's own host to its
ingest, as PCM, FLAC or Opus (--payload) in epochs of --epoch-samples (one
epoch is one part: 48 samples = 1 ms by default for PCM and FLAC, and for Opus
(1 ms Opus Custom at 320 kb/s, the lane default; 120 = 2.5 ms standard)); run N ntcmp HLS readers of each kind, over HTTP/2 with
5 ms of parts in flight (--h2-window-ms; one part at 5 ms, 5 at 1 ms, 20 at
0.25 ms) and over HTTP/3 with 40 parts in flight, on each reader host
(lon-readers and new-readers) for D seconds; then join the hop journals of every node with the readers'
records (hops.py), with the epoch that the sender reports. Linode is not
used. scripts/compare/part-sweep.py runs this for each part size.

Results: target/latency-bench/NAME/, or target/latency-bench/ID/NAME/ for the lab
of NEEDLETAIL_EDGE_LAB_ID. The loop, the indexes and the synthetic clips in
target/latency-bench/media are shared by every lab: each is made under a lock
(media/.lock), written beside and renamed. A pass holds its lab
(scripts/lab_guard.py: it waits while another run holds it) and refuses
binaries outside a frozen set (scripts/freeze-binaries.py) unless
NEEDLETAIL_ALLOW_LIVE=1.
  hops.json      hops.py's report, with the run's settings under "bench"
  summary.txt    the hop table of each region, and each reader's latency
  hops.raw.ndjson, HOST.LANE.probe*.csv|json, sender.log, plan.json
Each pass also keeps all of it in the run archive as it ends, whatever ends
it (~/needletail-data/<run id>/<NAME>/, NEEDLETAIL_DATA_ROOT overrides; run
id --run-id, else latency-bench-<UTC time>; a run id that exists is appended
to): every file the pass wrote in its results directory (arrivals,
hops.raw.ndjson, summaries, the per-socket drops in nodes.ndjson and
hops.json, the load tool's output) and NAME/node-logs/, what each node's
log gained in the pass, taken before the lab goes down.

Flags reach the services through the lab's settings:
  --mesh-args "..."          every av-mesh node (backbone, distributor, edge)
  --backbone-args/--distributor-args/--edge-args "..."   one role
  --contrib-args "..."       av-contrib
  --sender-args "..."        aep1-48k-probe send (e.g. "--pace tokio")
  --set key=value            any setting of linode-small-mesh-deploy.py
Write a single flag with `=`: --mesh-args=--some-flag (argparse reads a
lone "--some-flag" as an option of its own).
e.g. the A/B of the non-blocking relay forward:
  latency-bench.py --name p1-on
  latency-bench.py --name p1-off --mesh-args "--relay-forward-nonblocking false"

The journals are read only after the readers finish, so every service is
started with --hop-journal-capacity sized to the run and its part rate
(--journal-capacity overrides); summary.txt warns when a node's ring was
full or its journal starts late in the window.

4K at a chosen rate (wave 5 W3: how far past 164 Mbit/s a 4K stream crosses
contributor -> forwarder -> remote forwarder -> edge -> viewer with no frame
late). --video runs the lab's clip (16.5 Mbit/s) under the audio, as before.
  --video-clip NAME|PATH    a clip of ~/media/4k-encodes by name (bbb60-25m,
                            crowd50-25m, crowd50-40m, ...) or a path
  --video-mbps R            a synthetic 4K source at R Mbit/s (3840x2160 at
                            --video-fps, noise so that the encoder spends the
                            rate), encoded once into target/latency-bench/media
                            (--video-clip-seconds long, looped)
With either, each reader host also runs --video-readers ntcmp HLS readers of
the 4K stream (HTTP/3, --video-window parts in flight) and --video-udp-clients
needletail-udp-client viewers, every node's CPU, throttling and UDP drops are
sampled each second, and summary.txt and hops.json ("video") report per
region: frames expected (part numbers: one frame per part), received,
missing, late and on time, the publish-to-viewer latency, and the 4K hop
journal (stream 1). A frame is late when it completes more than
--frame-deadline-ms after its reader's fastest frame (a player's jitter
buffer of that much over the path). The UDP client reports no per-part
times: it is whole when it misses no part and its latency tail (max - p50)
is within the deadline.
e.g. latency-bench.py --name 4k-200 --video-mbps 200 --seconds 40

The UDP client lane (wave 6, the headline lane): --udp-clients N (default 1)
needletail-udp-client listeners of the audio stream on each reader host, each
recording every part's arrival (--arrivals). summary.txt and hops.json
("udp_clients") report per reader host the unit latency (the part's last
sample spoken -> the part at the client) p50/p95/p99/max, publish -> arrival,
the parts that came on the edge's fast lane and on its cache-fed lane, wire
bytes per part and the parts missed, and the hop table of the UDP clients'
parts (edge udp_sent -> client). --client-loss SPEC puts a netem impairment
("loss 5%", "loss gemodel 1% 20%") on the reader hosts' links, in and out,
over their access delay, as the referee does on its listener hosts.
e.g. latency-bench.py --name uc-burst --payload opus --client-loss "loss gemodel 1% 20%"

--dry-run prints the plan (every command) and touches nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import importlib.util
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import archived_run  # noqa: E402
import edge_lab as lab  # noqa: E402
import lab_guard as guard  # noqa: E402

_spec = importlib.util.spec_from_file_location("hops", HERE / "hops.py")
hops = importlib.util.module_from_spec(_spec)
sys.modules["hops"] = hops
_spec.loader.exec_module(hops)

OUT_ROOT = ROOT / "target" / "latency-bench"
MEDIA = OUT_ROOT / "media"
LOOP_SECONDS = 64                       # ntcmp's loop (tools/ntcmp/audio.go loopSecs)
LOOP_BYTES = LOOP_SECONDS * 48_000 * 6  # stereo 24-bit
SAMPLE_RATE = hops.SAMPLE_RATE
# daw_hls_base_stream_id 2001 + the format's offset (Opus 1000, PCM 2000).
PAYLOAD_STREAM = hops.PAYLOAD_STREAM
DEFAULT_EPOCH_SAMPLES = hops.DEFAULT_EPOCH_SAMPLES
OPUS_EPOCH_SAMPLES = (48, 64, 72, 120, 240, 480, 960)
MIN_EPOCH_SAMPLES = {"pcm": 12, "flac": 32}
READER_HOSTS = ["lon-readers", "new-readers"]
# Time in flight of the HTTP/2 readers. ntcmp's window 1 asks for part n + 1
# when part n has arrived: one part per round trip (about 2 ms on the lab's
# reader links), 500 parts a second, so it falls behind 1 ms parts and further
# behind smaller ones, and its backlog, not the part, is measured. 5 ms is the
# window 1 of the 5 ms parts of the first runs, at every part size.
H2_WINDOW_MS = 5.0
MAX_WINDOW = 400
SKIP_SECONDS = 5
# The hop journal is a ring that is read only after the readers have
# finished, so it must hold the whole window. The busiest node journals about
# 9 events of each part (the contributor: src_first..sent_secondary; a
# distributor: relay_first..watermark and a mesh_sent per edge). Rings are
# sized for 10 events of each part at the run's part rate (1,000 parts a
# second at 1 ms, 4,000 at 0.25 ms) over the run and a margin for the start
# of the readers and the collection, never below the services' default.
JOURNAL_DEFAULT = 262_144
JOURNAL_EVENTS_PER_PART = 10
JOURNAL_MARGIN_SECONDS = 60
JOURNAL_FLAG = "--hop-journal-capacity"
SENDER_STOP_SECONDS = 5
# 4K (wave 5 W3).
VIDEO_STREAM = lab.VIDEO_STREAM
CLIPS = lab.VIDEO_CLIP.parent
UDP_CLIENT = pathlib.Path(os.environ.get(
    "NEEDLETAIL_UDP_CLIENT_BIN", str(lab.MESH_ROOT / "target" / "release" / "needletail-udp-client")))
VIDEO_FPS = 50
VIDEO_CLIP_SECONDS = 10
VIDEO_WINDOW = 40
FRAME_DEADLINE_MS = 100.0
# The clip's rate and frame rate are measured on its first seconds.
PROBE_SECONDS = 10


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--name", required=True,
                        help="results go to target/latency-bench/[ID/]NAME")
    parser.add_argument("--seconds", type=int, default=60, help="D: how long the readers listen")
    parser.add_argument("--skip-seconds", type=int, default=None,
                        help=f"seconds at the start of the readers' window left out of every "
                             f"figure (default {SKIP_SECONDS}; 0: the start-up counts, as the "
                             "referee's first session after a deploy does)")
    parser.add_argument("--readers", type=int, default=1, help="N: readers of each kind on each host")
    parser.add_argument("--hosts", default=",".join(READER_HOSTS), help="reader namespaces")
    parser.add_argument("--video", action="store_true",
                        help="also run the lab's 4K source (SRT into the contributor), so the "
                             "audio crosses a mesh that carries 4K")
    parser.add_argument("--payload", choices=sorted(PAYLOAD_STREAM), default="pcm",
                        help="what the AEP1 sender sends (opus: CELT frames encoded by the probe "
                             "from the same loop); the stream follows (pcm 4001, flac 2001, opus 3001)")
    parser.add_argument("--epoch-samples", type=int, default=None,
                        help="samples of one AEP1 epoch, one part (default pcm and flac 48 = 1 ms, "
                             "opus 48 = 1 ms Opus Custom; pcm >= 12, flac >= 32, opus 48/64/72 (Opus Custom) or 120/240/480/960)")
    parser.add_argument("--h2-window", type=int, default=None,
                        help="parts in flight of the HTTP/2 readers (default: --h2-window-ms)")
    parser.add_argument("--h2-window-ms", type=float, default=H2_WINDOW_MS,
                        help="time in flight of the HTTP/2 readers, in parts of the run "
                             "(default %(default)g: the window 1 of the 5 ms parts before; a "
                             "reader that asks for one part per round trip cannot keep up "
                             "with parts shorter than the round trip)")
    parser.add_argument("--h3-window", type=int, default=40)
    for role in ("mesh", "backbone", "distributor", "edge", "contrib", "sender"):
        parser.add_argument(f"--{role}-args", default="", metavar="ARGS",
                            help=f"extra arguments of {role} (one string)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="a lab setting (value as JSON or a bare string); repeat")
    parser.add_argument("--journal-capacity", type=int, default=None, metavar="EVENTS",
                        help="hop journal ring of every node (default: sized to --seconds, at "
                             f"least {JOURNAL_DEFAULT}); a role whose arguments already set "
                             f"{JOURNAL_FLAG} keeps its own")
    parser.add_argument("--reader-loss", type=float, default=0.0,
                        help="random loss in percent on the reader hosts' links, both directions")
    parser.add_argument("--reader-netem", default="",
                        help="a netem spec for the reader hosts' links instead (edge_lab net)")
    parser.add_argument("--client-loss", default="", metavar="SPEC",
                        help="netem impairment of the reader hosts' links, in and out, on top of "
                             "their access delay, as the referee puts it on its listener hosts "
                             "(e.g. \"loss 5%%\", \"loss gemodel 1%% 20%%\")")
    parser.add_argument("--udp-clients", type=int, default=1, metavar="N",
                        help="needletail-udp-client listeners of the run's audio stream on each "
                             "reader host (default %(default)s; 0: none): the headline lane")
    parser.add_argument("--load-listeners", type=int, default=0, metavar="N",
                        help="needletail-load UDP listeners of the run's audio stream for each "
                             "serving edge of each --load-sites site, from that site's load host "
                             "(lon-readers-2, new-readers-2): the many listeners an edge carries "
                             "(wave 8: the 1 ms edge stalls appear with 64+ a edge); they "
                             "subscribe all at once with the UDP clients, a subscribe spike")
    parser.add_argument("--load-ramp", default="", metavar="SPEC",
                        help="needletail-load's --ramp in listeners per edge (e.g. "
                             "\"32@0,128@20\": a second subscribe spike 20 s in); default "
                             "--load-listeners from the start")
    parser.add_argument("--load-sites", default="london", metavar="LIST",
                        help="sites whose edges get --load-listeners (default %(default)s)")
    parser.add_argument("--load-args", default="", metavar="ARGS",
                        help="extra arguments of needletail-load (e.g. \"--silence-nack false\")")
    parser.add_argument("--stall-edge", default="", metavar="SECOND:MS",
                        help="SECOND s into the readers' window, stop the London edge that "
                             "serves the most UDP clients for MS ms (SIGSTOP, as a stalled "
                             "host: its sockets queue) and let it run again: the referee's edge "
                             "stall (wave 8 S2: the clients' move and the parts it costs)")
    parser.add_argument("--stall-node", default="", metavar="NAME:SECOND:MS[,...]",
                        help="SECOND s into the readers' window, stop node NAME for MS ms "
                             "(SIGSTOP) and let it run again: a forwarder's failure as its "
                             "children see it (wave 8 stage 3: an edge's 4K when the "
                             "distributor that feeds it the sources stops); a comma list "
                             "stops several (a stop past the run's end is a kill)")
    parser.add_argument("--drain-node", default="", metavar="NAME:SECOND",
                        help="SECOND s into the readers' window, drain node NAME (POST "
                             "/api/control/drain-node) and sample its live gauges every 100 ms "
                             "until its UDP subscribers reach 0 (wave 8 stage 4, Phase AM 9: "
                             "the paced drain); resumed after the run")
    parser.add_argument("--node-events", default="", metavar="ACTION:NAME:SECOND[,...]",
                        help="start, stop (SIGTERM), kill (SIGKILL, not restarted), drain or "
                             "resume a node SECOND s into the window; starting a slot "
                             "(lon-dist-c) takes it into service (Phase AM 12: a forwarder "
                             "slot joins and leaves under load)")
    parser.add_argument("--sample-forwarders", action="store_true",
                        help="every London distributor's fast-plane lease and cache-mesh "
                             "gauges and its served leases with their ranks, once a second "
                             "(Phase AM 12, 13)")
    parser.add_argument("--sender-host", choices=("contrib-lon", lab.SOURCE_HOST),
                        default="contrib-lon",
                        help="where the AEP1 senders run: the contributor's own host, sending "
                             "to itself (default), or the source host, sending every epoch to "
                             "each contributor (A 6, with the standby contributor)")
    parser.add_argument("--load-assign", action="store_true",
                        help="the default since integration 6 (kept for old command lines): "
                             "needletail-load listeners subscribe and move through the site's "
                             "/api/assignment (every distributor and edge of the site an entry "
                             "point; Phase AM 10), as real clients do")
    parser.add_argument("--split-client-cpus", action="store_true",
                        help="the readers, probes and 4K viewers on one half of the layout's "
                             "client CPUs and the crowd's load tool on the other "
                             "(edge_lab.rest_split), and each client's run-queue wait and its "
                             "namespace's UDP receive drops recorded (client-cpu.json): a 4K "
                             "viewer then does not wait behind the crowd (integration 6)")
    parser.add_argument("--load-direct", action="store_true",
                        help="needletail-load listener i starts at edge i mod n and never asks "
                             "an entry (before integration 6 the default: refused listeners "
                             "re-asked full edges 10-80k times)")
    parser.add_argument("--sample-gauges", action="store_true",
                        help="sample every serving edge's client UDP gauges (/api/assignment: "
                             "subscribers, load_permille, egress_observed_bps, draining) once a "
                             "second (Phase AM 8)")
    parser.add_argument("--sample-nodes", action="store_true",
                        help="sample every node's CPU, throttling and UDP drops each second "
                             "(always on with 4K)")
    parser.add_argument("--udp-client-args", default="", metavar="ARGS",
                        help="extra arguments of every audio UDP client (e.g. \"--fast-lane false\")")
    parser.add_argument("--video-udp-client-args", default="", metavar="ARGS",
                        help="extra arguments of every 4K UDP viewer (e.g. "
                             "\"--quality-moves-per-minute 0\": an A/B of place240's moves)")
    parser.add_argument("--unread-payloads", default="", metavar="LIST",
                        help="more renditions that nobody reads (e.g. \"pcm,flac\"): each has a "
                             "sender of its own and is published beside --payload (wave 7 item 5: "
                             "a rendition with no reader should not travel)")
    parser.add_argument("--no-up", action="store_true",
                        help="use the lab that is running (its flags are not changed)")
    parser.add_argument("--keep-up", action="store_true", help="leave the lab running afterwards")
    parser.add_argument("--ntcmp", default=str(ROOT / "target" / "ntcmp"))
    parser.add_argument("--pcm", default=str(lab.AUDIO_PCM),
                        help="stereo s24le PCM; its first 64 s are the loop")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; run nothing")
    parser.add_argument("--run-id", default=None,
                        help="the run in the archive (default latency-bench-<UTC time>; one that "
                             "exists is appended to, under NAME/)")
    video = parser.add_argument_group("4K at a chosen rate (implies --video)")
    source = video.add_mutually_exclusive_group()
    source.add_argument("--video-clip", metavar="NAME|PATH",
                        help=f"a clip of {CLIPS} by name (bbb60-25m, crowd50-40m, ...) or a path")
    source.add_argument("--video-mbps", type=float, metavar="R",
                        help="a synthetic 4K source at R Mbit/s, encoded once with ffmpeg")
    video.add_argument("--video-fps", type=int, default=VIDEO_FPS,
                       help="frame rate of the synthetic source (default %(default)s)")
    video.add_argument("--video-clip-seconds", type=int, default=VIDEO_CLIP_SECONDS,
                       help="length of the synthetic clip, looped (default %(default)s)")
    video.add_argument("--video-readers", type=int, default=None, metavar="N",
                       help="4K HLS readers (ntcmp, HTTP/3) on each reader host (default 1 with "
                            "--video-clip or --video-mbps, else 0)")
    video.add_argument("--video-udp-clients", type=int, default=None, metavar="N",
                       help="4K needletail-udp-client viewers on each reader host (default as "
                            "--video-readers)")
    video.add_argument("--video-window", type=int, default=VIDEO_WINDOW,
                       help="parts in flight of the 4K readers (default %(default)s)")
    video.add_argument("--frame-deadline-ms", type=float, default=FRAME_DEADLINE_MS,
                       help="a frame is late when it completes more than this after its "
                            "reader's fastest frame (default %(default)g)")
    video.add_argument("--udp-client", default=str(UDP_CLIENT), help="needletail-udp-client")
    return parser


def skip_seconds(args) -> int:
    """--skip-seconds, else the bench's default (read when the run starts)."""
    value = getattr(args, "skip_seconds", None)
    return SKIP_SECONDS if value is None else max(0, int(value))


def split(text: str) -> list[str]:
    return shlex.split(text) if text else []


def epoch_samples(args) -> int:
    """Samples of one epoch of this run, checked against what the payload
    allows."""
    samples = args.epoch_samples or DEFAULT_EPOCH_SAMPLES[args.payload]
    if args.payload == "opus" and samples not in OPUS_EPOCH_SAMPLES:
        raise SystemExit(f"an Opus epoch is {OPUS_EPOCH_SAMPLES} samples, not {samples}")
    if samples < MIN_EPOCH_SAMPLES.get(args.payload, 1):
        raise SystemExit(f"a {args.payload} epoch is at least {MIN_EPOCH_SAMPLES[args.payload]} samples")
    return samples


def epoch_ns(samples: int) -> int:
    """One epoch in ns, rounded (32 samples are 666,666.67 ns)."""
    return round(samples * 1_000_000_000 / SAMPLE_RATE)


def epoch_ms_text(samples: int) -> str:
    """One epoch in ms as ntcmp audio-index reads it (%g): exact for every
    size it computes back to `samples` (48 -> "1.0", 12 -> "0.25")."""
    text = repr(samples * 1000 / SAMPLE_RATE)
    if int(float(text) * SAMPLE_RATE / 1000) != samples:
        raise SystemExit(f"{samples} samples cannot be named in ms for ntcmp")
    return text


def parts_per_second(samples: int) -> float:
    return SAMPLE_RATE / samples


def window_for_ms(window_ms: float, samples: int) -> int:
    """Parts in flight that cover window_ms at this part size, 1 to 400."""
    parts = -(-round(window_ms * SAMPLE_RATE) // (samples * 1000))
    return max(1, min(MAX_WINDOW, parts))


def reader_windows(args) -> dict[str, int]:
    """Parts in flight of each reader lane of this run."""
    h2 = args.h2_window if args.h2_window is not None else window_for_ms(
        args.h2_window_ms, epoch_samples(args))
    return {"h2": h2, "h3": args.h3_window}


def journal_capacity(args) -> int:
    """Events that every node's hop journal keeps in this run."""
    if args.journal_capacity is not None:
        return args.journal_capacity
    parts = parts_per_second(epoch_samples(args))
    if video_measured(args):
        # The 4K stream's parts (one a frame) share the ring.
        parts += args.video_fps if args.video_mbps else 60
    rate = JOURNAL_EVENTS_PER_PART * parts
    return max(JOURNAL_DEFAULT, int(rate * (args.seconds + JOURNAL_MARGIN_SECONDS)))


def with_journal(extra: list[str], capacity: int) -> list[str]:
    """extra, with the journal capacity unless it already sets one."""
    if any(item == JOURNAL_FLAG or item.startswith(JOURNAL_FLAG + "=") for item in extra):
        return extra
    return extra + [JOURNAL_FLAG, str(capacity)]


def lab_overrides(args) -> list[str]:
    """The `key=value` settings of edge_lab.up for this run."""
    capacity = journal_capacity(args)
    samples = epoch_samples(args)
    settings: dict[str, object] = {"daw_hls_formats": ",".join([args.payload, *unread_payloads(args)])}
    # The lab records the epoch that its sender sends (the parts follow it:
    # audio_part is "epoch").
    settings["opus_epoch_samples" if args.payload == "opus" else "pcm_epoch_samples"] = samples
    plain = []
    for item in args.set:
        key, _, value = item.partition("=")
        if key.startswith("extra_") and key.endswith("_args"):
            settings[key] = json.loads(value)
        else:
            plain.append(item)
    mesh = split(args.mesh_args)
    for role in ("backbone", "distributor", "edge"):
        extra = list(settings.get(f"extra_{role}_args") or []) + mesh + split(getattr(args, f"{role}_args"))
        settings[f"extra_{role}_args"] = with_journal(extra, capacity)
    contrib = list(settings.get("extra_contributor_args") or []) + split(args.contrib_args)
    settings["extra_contributor_args"] = with_journal(contrib, capacity)
    out = [f"{key}={value if isinstance(value, str) else json.dumps(value)}"
           for key, value in settings.items()]
    return out + plain


def loop_start(now: float) -> int:
    """The next 64-second boundary at least 8 s away (compare-audio.py's)."""
    start = (int(now) // LOOP_SECONDS + 1) * LOOP_SECONDS
    if start - now < 8:
        start += LOOP_SECONDS
    return start


def unread_payloads(args) -> list[str]:
    """--unread-payloads: payloads besides --payload, in order, once each."""
    out = []
    for item in (args.unread_payloads or "").split(","):
        item = item.strip()
        if not item or item == args.payload or item in out:
            continue
        if item not in PAYLOAD_STREAM:
            raise SystemExit(f"--unread-payloads: unknown payload {item!r}")
        out.append(item)
    return out


def unread_sender_commands(args, start: int, loop_file: str) -> list[list[str]]:
    """A sender for each --unread-payloads rendition: a session of its own,
    the loop's own epochs (48 samples; 32 for FLAC)."""
    out = []
    for index, payload in enumerate(unread_payloads(args), start=1):
        samples = max(32, DEFAULT_EPOCH_SAMPLES[payload]) if payload == "flac" else DEFAULT_EPOCH_SAMPLES[payload]
        session_id = start * 1_000_000_000 + epoch_ns(samples) + index
        out.append([lab.binary("aep1-48k-probe"), "send", *sender_targets(args),
                    # Group 0, as the main sender: the contributor numbers a
                    # rendition base + format offset + group, so PCM is 4001
                    # and FLAC 2001, the DAW lane's renditions of one source.
                    "--session-id", str(session_id), "--group-id", "0",
                    "--duration-seconds", str(args.seconds + max(0, start - int(time.time())) + 300),
                    "--payload", payload, "--epoch-samples", str(samples),
                    "--channels", "2", "--group-channels", "8",
                    "--pcm-s24le-file", loop_file, "--repair-percent", "12",
                    "--min-repair-symbols", "1"])
    return out


def sender_place(args) -> tuple[str, list[str]]:
    """Where the senders run and where they send (--sender-host): the
    contributor's own host to itself (the default, the gate's baseline), or
    the source host to every contributor (A 6: each epoch to both)."""
    if getattr(args, "sender_host", "contrib-lon") == lab.SOURCE_HOST:
        return lab.SOURCE_HOST, lab.contributor_targets()
    return "contrib-lon", ["127.0.0.1:27100"]


def sender_targets(args) -> list[str]:
    return [item for target in sender_place(args)[1] for item in ("--target", target)]


def sender_command(args, start: int, loop_file: str) -> list[str]:
    """The AEP1 sender. It sends epoch k when its last sample has been
    spoken: the session id is the loop start plus one epoch."""
    samples = epoch_samples(args)
    session_id = start * 1_000_000_000 + epoch_ns(samples)
    return [lab.binary("aep1-48k-probe"), "send", *sender_targets(args),
            "--session-id", str(session_id), "--group-id", "0",
            # Long enough for the lab to go live first; it is stopped at the end.
            "--duration-seconds", str(args.seconds + max(0, start - int(time.time())) + 300),
            "--payload", args.payload, "--epoch-samples", str(samples),
            "--channels", "2", "--group-channels", "8",
            "--pcm-s24le-file", loop_file, "--repair-percent", "12", "--min-repair-symbols", "1",
            *split(args.sender_args)]


def planned_left_out(args) -> set[str]:
    """The nodes that the run's lab leaves out: the running lab's with
    --no-up, else those of its `up` settings (`leave_out=`, or own_cpus's
    default when own_cpus=1 names none)."""
    if args.no_up:
        return lab.left_out()
    sets = dict(item.partition("=")[::2] for item in args.set)
    if sets.get("leave_out"):
        return lab.leave_out_names(sets["leave_out"])
    if str(sets.get("own_cpus", "")).lower() in ("1", "true"):
        return set(lab.OWN_DEFAULT_LEFT_OUT)
    return set()


def client_prefix(args, host: str, kind: str) -> list[str]:
    """lab.enter for a client of `kind` ("load" or "client"): with
    --split-client-cpus the load tool on the layout's tool CPUs and every
    other client on its probe CPUs."""
    if not getattr(args, "split_client_cpus", False):
        return lab.enter(host)
    split = lab.rest_split(lab.load("cpus.json"))
    cpus = split.get("tools" if kind == "load" else "probes")
    if not cpus:
        return lab.enter(host)
    saved = os.environ.get(lab.TOOL_CPUS_ENV)
    os.environ[lab.TOOL_CPUS_ENV] = ",".join(str(cpu) for cpu in cpus)
    try:
        return lab.enter(host)
    finally:
        if saved is None:
            os.environ.pop(lab.TOOL_CPUS_ENV, None)
        else:
            os.environ[lab.TOOL_CPUS_ENV] = saved


def namespace_udp(host: str) -> dict:
    """A namespace's UDP counters (/proc/net/snmp): RcvbufErrors and others."""
    try:
        text = lab.run_in(host, ["cat", "/proc/net/snmp"], check=False, timeout=10)
    except Exception:  # noqa: BLE001 - a record, never a failure
        return {}
    rows = [line.split() for line in text.splitlines() if line.startswith("Udp:")]
    if len(rows) < 2:
        return {}
    return {key: int(value) for key, value in zip(rows[0][1:], rows[1][1:]) if value.isdigit()}


def schedstat(pid: int) -> tuple[int, int] | None:
    """A process's CPU time and run-queue wait so far (ns), its threads
    summed (/proc/PID/task/*/schedstat)."""
    run = wait = 0
    try:
        for task in pathlib.Path(f"/proc/{pid}/task").iterdir():
            fields = (task / "schedstat").read_text().split()
            run += int(fields[0])
            wait += int(fields[1])
    except (OSError, ValueError, IndexError):
        return None
    return run, wait


def client_kind(item: dict) -> str:
    """A client's kind in client-cpu.json: "4k-udp" (a 4K UDP viewer),
    "4k-hls" (a 4K LL-HLS reader), "udp" (an audio probe), "reader"."""
    if item.get("kind") == "udp":
        return "4k-udp" if ".4k." in pathlib.Path(str(item.get("report", ""))).name else "udp"
    return "4k-hls" if item.get("kind") == "hls" else "reader"


def socket_drops(pid: int) -> int | None:
    """The drops of a process's own UDP sockets so far (/proc/PID/net/udp
    and udp6, the `drops` column of the sockets its fds hold), so that a
    viewer's drops are told from its namespace's other clients'. None when
    the process is gone."""
    try:
        inodes = set()
        for fd in pathlib.Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
        total = 0
        for table in ("udp", "udp6"):
            for line in pathlib.Path(f"/proc/{pid}/net/{table}").read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 13 and fields[9] in inodes:
                    total += int(fields[12])
        return total
    except (OSError, ValueError):
        return None


def planned_nodes(args, role: str, site: str) -> list[str]:
    """A site's nodes of `role` in the run's lab: the running lab's with
    --no-up (slots taken in count), else the planned layout's (no slot)."""
    # A caller without a layout of its own (change-run's crowd on a running
    # lab) reads the running lab's.
    if getattr(args, "no_up", True) or not hasattr(args, "set"):
        return lab.edges(site) if role == "edge" else lab.distributors(site)
    out = planned_left_out(args)
    slots = set(lab.slot_nodes())
    return [name for name in lab.names(role, site) if name not in out and name not in slots]


def reader_entry(args, site: str) -> str:
    """The entry point of a site's readers and viewers: its first
    distributor in the run's layout (planned_left_out; never a slot). Not
    the lab's record of what it left out, which is the last lab's until
    this run's `up`."""
    out = planned_left_out(args)
    slots = set(lab.slot_nodes())
    return next(name for name in lab.names("distributor", site)
                if name not in out and name not in slots)


def reader_hosts(args) -> list[str]:
    """The reader hosts of the run: those of --hosts whose site has a
    distributor in the lab (not left out, not a slot), the entry point of
    the host's readers and viewers. With the default hosts a host of a site
    the layout leaves out is dropped (logged); a host named in
    --hosts whose site is left out is refused. (Integration 6: a London-only
    layout made every pass after the first fail on new-readers' entry.)"""
    named = [item for item in args.hosts.split(",") if item]
    out = planned_left_out(args)
    slots = set(lab.slot_nodes())
    kept, dropped = [], []
    for host in named:
        site = lab.node(host)["site"]
        entries = [name for name in lab.names("distributor", site)
                   if name not in out and name not in slots]
        (kept if entries else dropped).append(host)
    if dropped:
        if args.hosts != ",".join(READER_HOSTS):
            raise SystemExit(f"--hosts {','.join(dropped)}: the lab leaves out every distributor "
                             "of its site (leave_out); name hosts of the sites it runs")
        log(f"reader hosts {', '.join(dropped)} left out: their site has no distributor in "
            "this layout")
    if not kept:
        raise SystemExit("no reader host: the layout leaves out every site's distributors")
    return kept


def reader_commands(args, out: pathlib.Path, index: str) -> list[dict]:
    """ntcmp hls readers: --h2-window-ms of parts in flight over HTTP/2 and
    --h3-window parts over HTTP/3 on each reader host, through the first
    distributor of the host's site."""
    readers = []
    stream = PAYLOAD_STREAM[args.payload]
    for host in reader_hosts(args):
        site = lab.node(host)["site"]
        entry = reader_entry(args, site)
        windows = reader_windows(args)
        for lane, window, http3 in (("h2", windows["h2"], False), ("h3", windows["h3"], True)):
            stem = out / f"{host}.{lane}"
            command = [args.ntcmp, "hls", "--lane", f"nt-{args.payload}-{lane}", "--mode", "chase",
                       "--window", str(window), *(["--http3"] if http3 else []),
                       "--url", f"{lab.base_url(entry)}/live/{stream}/master.m3u8"
                                f"?sid=bench-{args.name}-{host}-{lane}-{{i}}",
                       "--ca", lab.ca_file(), "--server-name", lab.SERVER_NAME,
                       "--audio-index", index, "--duration", str(args.seconds),
                       "--skip-seconds", str(skip_seconds(args)), "--label", f"{args.name}-{host}-{lane}",
                       "--count", str(args.readers),
                       *(["--spread-ms", str(min(10_000, 400 * args.readers))] if args.readers > 1 else []),
                       "--report", f"{stem}.probe.json", "--frames", f"{stem}.probe.csv"]
            readers.append({"host": host, "site": site, "lane": lane, "window": window,
                            "entry": entry, "command": command})
    return readers


# ------------------------------------------------------------------ 4K

def video_measured(args) -> bool:
    """Whether this run measures 4K (a clip or a rate was chosen)."""
    return bool(args.video_clip or args.video_mbps)


def video_viewers(args) -> dict[str, int]:
    """4K viewers of each kind on each reader host."""
    default = 1 if video_measured(args) else 0
    readers = default if args.video_readers is None else args.video_readers
    clients = default if args.video_udp_clients is None else args.video_udp_clients
    return {"readers": readers, "udp_clients": clients}


def synthetic_clip(mbps: float, fps: int, seconds: int) -> pathlib.Path:
    return MEDIA / f"synthetic-4k-{mbps:g}m-{fps}fps-{seconds}s.ts"


def synthetic_command(path: pathlib.Path, mbps: float, fps: int, seconds: int) -> list[str]:
    """ffmpeg that encodes the synthetic 4K clip: a test pattern under
    temporal noise, so that H.264 needs the whole rate (constrained to it:
    VBV of one second), a two-second closed GOP of P frames as the lab
    encodes have, and the AAC track that the lab's source maps."""
    rate = f"{mbps:g}M"
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=3840x2160:rate={fps}",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
            "-t", str(seconds), "-vf", "noise=alls=40:allf=t+u",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-bf", "0",
            "-g", str(2 * fps), "-keyint_min", str(2 * fps), "-sc_threshold", "0",
            "-b:v", rate, "-maxrate", rate, "-bufsize", rate, "-x264-params", "nal-hrd=vbr",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
            "-f", "mpegts", str(path) + ".part"]


def video_source(args) -> dict | None:
    """The 4K source of the run: the lab's clip (--video), a named clip, or
    a synthetic one; None without video."""
    if args.video_mbps is not None:
        if args.video_mbps <= 0:
            raise SystemExit("--video-mbps is more than 0")
        path = synthetic_clip(args.video_mbps, args.video_fps, args.video_clip_seconds)
        return {"label": f"synthetic {args.video_mbps:g} Mbit/s {args.video_fps} fps",
                "path": str(path), "synthetic": True, "nominal_mbps": args.video_mbps,
                "fps": args.video_fps,
                "prepare": synthetic_command(path, args.video_mbps, args.video_fps,
                                             args.video_clip_seconds)}
    if args.video_clip:
        given = pathlib.Path(args.video_clip).expanduser()
        referee = lab.referee_clip(given)
        if referee:
            # A clip directory of the referee's (prepare-media): sent as the
            # referee sends it, ntcmp flv over RTMP (edge_lab.start_sources).
            return {"label": f"referee {referee['rung']}", "path": str(given), "synthetic": False,
                    "referee": True, "fps": referee["fps"],
                    "video_mbps": round(referee["kbps"] / 1000, 2) if referee.get("kbps") else None}
        path = given if given.suffix or given.is_file() else CLIPS / f"{args.video_clip}.ts"
        return {"label": path.stem, "path": str(path), "synthetic": False}
    if args.video:
        return {"label": lab.VIDEO_CLIP.stem, "path": str(lab.VIDEO_CLIP), "synthetic": False}
    return None


@contextlib.contextmanager
def media_lock():
    """The media directory is shared by the labs of every id: one of them
    makes a file at a time."""
    MEDIA.mkdir(parents=True, exist_ok=True)
    with open(MEDIA / ".lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def prepare_video(item: dict | None) -> dict | None:
    """Encode the synthetic clip if it is not there, check that the clip
    exists, and measure its video rate and frame rate (ffprobe, the first
    PROBE_SECONDS)."""
    if item is None:
        return None
    path = pathlib.Path(item["path"])
    if item.get("synthetic"):
        with media_lock():
            if not path.is_file():
                log(f"encoding the synthetic 4K clip {path.name}")
                subprocess.run(["nice", "-n", "10", *item["prepare"]], check=True)
                pathlib.Path(str(path) + ".part").replace(path)
    if item.get("referee"):
        return item
    if not path.is_file():
        raise SystemExit(f"no 4K clip at {path} (clips: {', '.join(sorted(p.stem for p in CLIPS.glob('*.ts')))})")
    item.update(probe_clip(path))
    return item


def probe_clip(path: pathlib.Path) -> dict:
    """Video Mbit/s and frames a second of a clip's first PROBE_SECONDS."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals",
             f"%+{PROBE_SECONDS}", "-show_entries", "packet=pts_time,size", "-of", "csv=p=0",
             str(path)], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return clip_rate(result.stdout)


def clip_rate(text: str) -> dict:
    """{"video_mbps", "fps"} from ffprobe's packet lines, `pts_time,size,`
    (ffprobe writes a packet's fields in its own order: time, then size)."""
    sizes, times = [], []
    for line in text.splitlines():
        fields = line.strip().split(",")
        try:
            time_s, size = float(fields[0]), int(fields[1])
        except (IndexError, ValueError):
            continue
        times.append(time_s)
        sizes.append(size)
    if len(times) < 2 or max(times) <= min(times):
        return {}
    span = (max(times) - min(times)) * len(times) / (len(times) - 1)
    return {"video_mbps": round(sum(sizes) * 8 / span / 1e6, 2), "fps": round(len(times) / span, 2)}


def video_commands(args, out: pathlib.Path) -> list[dict]:
    """The 4K viewers on each reader host, through the first distributor of
    the host's site: ntcmp HLS readers (HTTP/3, chase, --video-window parts
    in flight, every frame's record with the server timing headers) and
    needletail-udp-client viewers (its JSON report on standard output)."""
    viewers = video_viewers(args)
    items = []
    for host in reader_hosts(args):
        site = lab.node(host)["site"]
        entry = reader_entry(args, site)
        if viewers["readers"]:
            stem = out / f"{host}.4k.h3"
            items.append({"host": host, "site": site, "kind": "hls", "entry": entry,
                          "frames": f"{stem}.frames.csv", "report": f"{stem}.report.json",
                          "command": [
                              args.ntcmp, "hls", "--lane", "nt-4k-h3", "--mode", "chase",
                              "--window", str(args.video_window), "--http3",
                              "--url", f"{lab.base_url(entry)}/live/{VIDEO_STREAM}/master.m3u8"
                                       f"?sid=bench-{args.name}-{host}-4k-{{i}}",
                              "--ca", lab.ca_file(), "--server-name", lab.SERVER_NAME,
                              "--duration", str(args.seconds), "--skip-seconds", str(skip_seconds(args)),
                              "--label", f"{args.name}-{host}-4k", "--count", str(viewers["readers"]),
                              "--report", f"{stem}.report.json", "--frames", f"{stem}.frames.csv"]})
        for index in range(viewers["udp_clients"]):
            items.append({"host": host, "site": site, "kind": "udp", "entry": entry,
                          "report": str(out / f"{host}.4k.udp{index}.json"),
                          "command": [
                              args.udp_client, "--entry", lab.base_url(entry),
                              "--stream", str(VIDEO_STREAM), "--tls-ca", lab.ca_file(),
                              "--server-name", lab.SERVER_NAME,
                              "--duration-seconds", str(args.seconds),
                              "--session", f"bench-{args.name}-{host}-udp-{index}",
                              # Each 4K part's arrival, bytes and publication
                              # time (wave 9 D: the loop points, the tail).
                              "--arrivals", str(out / f"{host}.4k.udp{index}.arrivals.csv"),
                              *split(getattr(args, "video_udp_client_args", "") or "")]})
    return items


def read_frames(path: pathlib.Path, since_us: int | None = None,
                until_us: int | None = None) -> dict[int, tuple[int, int]]:
    """{part: (complete_us, published_us)} of the video rows of an ntcmp
    record (the earliest completion of each part), inside the window."""
    parts: dict[int, tuple[int, int]] = {}
    try:
        handle = path.open()
    except OSError:
        return parts
    with handle:
        for row in csv.DictReader(handle):
            if row.get("kind") != "v":
                continue
            try:
                part = int((row.get("part") or "").strip())
                complete = int(row["complete_unix_us"])
                published = int((row.get("published_unix_us") or "0").strip() or 0)
            except (KeyError, ValueError):
                continue
            if published <= 0:
                continue
            if (since_us is not None and complete < since_us) or \
                    (until_us is not None and complete > until_us):
                continue
            if part not in parts or complete < parts[part][0]:
                parts[part] = (complete, published)
    return parts


def frame_verdict(parts: dict[int, tuple[int, int]], deadline_ms: float) -> dict:
    """Frames of one 4K reader (one frame per part): expected from the part
    numbers it saw, received, missing, late (more than deadline_ms after
    its fastest frame's publish-to-viewer time) and on time."""
    if not parts:
        return {"expected": 0, "received": 0, "missing": 0, "late": 0, "on_time": 0,
                "on_time_fraction": None, "latency_ms": None}
    latency = {part: (complete - published) / 1000 for part, (complete, published) in parts.items()}
    floor = min(latency.values())
    expected = max(parts) - min(parts) + 1
    late = sum(1 for value in latency.values() if value > floor + deadline_ms)
    on_time = len(parts) - late
    return {"expected": expected, "received": len(parts), "missing": expected - len(parts),
            "late": late, "on_time": on_time, "on_time_fraction": round(on_time / expected, 4),
            "floor_ms": round(floor, 3), "latency_ms": hops.distribution(list(latency.values()))}


def udp_verdict(report: dict | None, deadline_ms: float) -> dict:
    """What a needletail-udp-client report says of its 4K parts: whole when
    it missed none and its publish-to-arrival tail (max - p50) is within
    deadline_ms. It records no per-part times, so late frames are not
    counted one by one."""
    if not report:
        return {"error": "no report"}
    latency = report.get("publish_to_arrival_ms") or {}
    tail = (latency.get("max", 0) - latency.get("p50", 0)) if latency.get("count") else None
    missing = report.get("missing_parts")
    return {"expected": report.get("expected_parts"), "received": report.get("received_parts"),
            "missing": missing, "fast_lane_parts": report.get("fast_lane_parts"),
            "cache_lane_parts": report.get("cache_lane_parts"),
            "moves": len(report.get("moves") or []),
            "latency_ms": {key: latency.get(key) for key in ("count", "p50", "p95", "p99", "max")},
            "tail_ms": None if tail is None else round(tail, 3),
            "whole": bool(missing == 0 and tail is not None and tail <= deadline_ms)}


def video_report(items: list[dict], deadline_ms: float, since_us: int | None,
                 until_us: int | None) -> dict:
    """Per region: every 4K viewer's verdict and the region's frames."""
    regions: dict[str, dict] = {}
    for item in items:
        region = regions.setdefault(item["site"], {"readers": [], "udp_clients": []})
        if item["kind"] == "hls":
            base = pathlib.Path(item["frames"])
            files = sorted(base.parent.glob(base.stem + "*" + base.suffix))
            for path in files:
                verdict = frame_verdict(read_frames(path, since_us, until_us), deadline_ms)
                region["readers"].append({"host": item["host"], "record": path.name, **verdict})
        else:
            try:
                report = json.loads(pathlib.Path(item["report"]).read_text())
            except (OSError, ValueError):
                report = None
            region["udp_clients"].append({"host": item["host"],
                                          "report": pathlib.Path(item["report"]).name,
                                          **udp_verdict(report, deadline_ms)})
    for region in regions.values():
        readers = region["readers"]
        expected = sum(item["expected"] for item in readers)
        on_time = sum(item["on_time"] for item in readers)
        region["frames"] = {
            "expected": expected, "received": sum(item["received"] for item in readers),
            "missing": sum(item["missing"] for item in readers),
            "late": sum(item["late"] for item in readers), "on_time": on_time,
            "on_time_fraction": round(on_time / expected, 4) if expected else None}
        region["all_on_time"] = bool(readers or region["udp_clients"]) and all(
            item["expected"] and item["on_time"] == item["expected"] for item in readers) and all(
            item.get("whole") for item in region["udp_clients"])
    return regions


def video_summary_lines(source: dict | None, regions: dict, deadline_ms: float) -> list[str]:
    if source is None:
        return []
    rate = (f"{source['video_mbps']:g} Mbit/s, {source['fps']:g} fps measured"
            if source.get("video_mbps") else "rate not measured")
    lines = ["", f"4K: {source['label']} ({rate}); late = more than {deadline_ms:g} ms after the "
                 "reader's fastest frame"]
    for site, region in sorted(regions.items()):
        frames = region["frames"]
        share = frames["on_time_fraction"]
        lines.append(f"  {site}: frames expected {frames['expected']} received {frames['received']} "
                     f"missing {frames['missing']} late {frames['late']} on time "
                     f"{'-' if share is None else f'{share * 100:.2f}%'}; all on time: "
                     f"{'yes' if region['all_on_time'] else 'NO'}")
        for item in region["readers"]:
            latency = item.get("latency_ms") or {}
            lines.append(f"    hls {item['record']:<34} {item['received']}/{item['expected']} "
                         f"late {item['late']}  publish->viewer p50 {latency.get('p50')} "
                         f"p99 {latency.get('p99')} max {latency.get('max')} ms")
        for item in region["udp_clients"]:
            latency = item.get("latency_ms") or {}
            lines.append(f"    udp {item['report']:<34} {item.get('received')}/{item.get('expected')} "
                         f"missing {item.get('missing')} fast-lane {item.get('fast_lane_parts')}  "
                         f"publish->arrival p50 {latency.get('p50')} p99 {latency.get('p99')} "
                         f"max {latency.get('max')} ms; whole: {'yes' if item.get('whole') else 'NO'}"
                         + (f" ({item['error']})" if item.get("error") else ""))
    return lines


def node_summary_lines(nodes: dict | None) -> list[str]:
    if not nodes:
        return []
    lines = ["", "nodes (CPU % of one CPU: mean p95 max; throttled ms; UDP receive-buffer drops)"]
    for name, item in sorted(nodes.items()):
        if item.get("cpu_percent_mean") is None:
            continue
        lines.append(f"  {name:<16} {item['cpu_percent_mean']:>6} {item['cpu_percent_p95']:>6} "
                     f"{item['cpu_percent_max']:>6}  throttled {item.get('throttled_ms')}  "
                     f"rcvbuf drops {item.get('udp_rcvbuf_errors')}")
        drops = item.get("udp_socket_drops")
        if drops:
            queues = item.get("udp_socket_rx_queue_max", {})
            lines.append("      socket drops by port: " + ", ".join(
                f":{port} {count}" + (f" (queue max {queues[port] // 1024} KiB)"
                                      if port in queues else "")
                for port, count in list(drops.items())[:8]))
    return lines


def node_traffic_lines(nodes: dict | None, parts_per_second: float) -> list[str]:
    """What each node sent and received on its public (eth0) and private
    (eth1) interface: datagrams a second, datagrams per part of the run's
    audio stream (at `parts_per_second`) and Mbit/s (Ethernet bytes, as the
    referee's samplers count them). Every packet counts: the readers' HTTP,
    the cache mesh, telemetry."""
    if not nodes or not any("eth0_tx_pps" in item for item in nodes.values()):
        return []
    lines = ["", f"node traffic (datagrams/s, datagrams per part at {parts_per_second:g} parts/s, "
                 "Mbit/s; eth0 public / long haul, eth1 VLAN)"]
    for name, item in sorted(nodes.items()):
        if "eth0_tx_pps" not in item:
            continue
        cells = []
        for device in ("eth0", "eth1"):
            for direction in ("tx", "rx"):
                pps = item.get(f"{device}_{direction}_pps")
                if pps is None:
                    continue
                per_part = pps / parts_per_second if parts_per_second else 0.0
                cells.append(f"{device} {direction} {pps:>8.0f} {per_part:>6.2f}/part "
                             f"{item.get(f'{device}_{direction}_mbps'):>7.2f}")
        lines.append(f"  {name:<16} " + " | ".join(cells))
    return lines


def reader_netem(args) -> str | None:
    if args.reader_netem:
        return args.reader_netem
    if args.client_loss:
        return client_loss_spec(args.client_loss)
    if args.reader_loss:
        return lab.netem_spec(loss=args.reader_loss)
    return None


def client_loss_spec(impairment: str) -> str:
    """The netem of a reader host's link with `impairment` (netem words:
    "loss 5%", "loss gemodel 1% 20%") on top of the link's own access delay,
    so that only the impairment changes."""
    words = impairment.split()
    if not words:
        raise SystemExit("--client-loss is a netem impairment, e.g. \"loss 5%\"")
    if "delay" in words or "limit" in words:
        raise SystemExit("--client-loss names the impairment only; the link keeps its delay "
                         "(give a whole spec with --reader-netem)")
    return f"{lab.netem_spec(delay_ms=lab.ACCESS_DELAY_MS)} {' '.join(words)}"


# ------------------------------------------------------------------ the UDP client lane

def audio_udp_commands(args, out: pathlib.Path) -> list[dict]:
    """--udp-clients needletail-udp-client listeners of the run's audio
    stream on each reader host, through the first distributor of the host's
    site (which assigns them an edge), each with its record of every part's
    arrival (--arrivals) and its JSON report on standard output."""
    stream = PAYLOAD_STREAM[args.payload]
    items = []
    for host in reader_hosts(args):
        site = lab.node(host)["site"]
        entry = reader_entry(args, site)
        for index in range(args.udp_clients):
            stem = out / f"{host}.uc{index}"
            items.append({"host": host, "site": site, "kind": "udp", "entry": entry,
                          "report": f"{stem}.uc.json", "arrivals": f"{stem}.arrivals.csv",
                          "command": [
                              args.udp_client, "--entry", lab.base_url(entry),
                              "--stream", str(stream), "--tls-ca", lab.ca_file(),
                              "--server-name", lab.SERVER_NAME,
                              "--duration-seconds", str(args.seconds),
                              "--session", f"bench-{args.name}-{host}-uc-{index}",
                              "--arrivals", f"{stem}.arrivals.csv",
                              *split(args.udp_client_args)]})
    return items


# ------------------------------------------------------------------ edge load (wave 8)

# needletail-load's host in each site: a reader namespace with no CPU limit.
LOAD_HOSTS = {"london": "lon-readers-2", "newyork": "new-readers-2"}
CLIENT_UDP_PORT = 19446
LOAD_BIN = pathlib.Path(os.environ.get(
    "NEEDLETAIL_LOAD_BIN", str(lab.MESH_ROOT / "target" / "release" / "needletail-load")))


def load_ramp(spec: str, edges: int) -> str:
    """--load-ramp (listeners per edge) as needletail-load's --ramp (all)."""
    steps = []
    for item in [item.strip() for item in spec.split(",") if item.strip()]:
        count, at, second = item.partition("@")
        if not at:
            raise SystemExit(f"--load-ramp: {item!r} is not COUNT@SECOND")
        try:
            steps.append(f"{int(count) * edges}@{int(second)}")
        except ValueError:
            raise SystemExit(f"--load-ramp: {item!r} is not COUNT@SECOND") from None
    return ",".join(steps)


def load_commands(args, out: pathlib.Path) -> list[dict]:
    """--load-listeners needletail-load listeners for each serving edge of
    each --load-sites site, from the site's load host, asking the site's
    entry points (/api/assignment) as real clients do; with --load-direct
    listener i starts at edge i mod n."""
    if args.load_listeners <= 0 and not args.load_ramp:
        return []
    items = []
    for site in [item for item in args.load_sites.split(",") if item]:
        if site not in LOAD_HOSTS:
            raise SystemExit(f"--load-sites: no load host in {site}")
        edges = planned_nodes(args, "edge", site)
        targets = [f"{lab.node(name)['public_ipv4']}:{CLIENT_UDP_PORT}" for name in edges]
        count = args.load_listeners * len(edges)
        ramp = load_ramp(args.load_ramp, len(edges)) if args.load_ramp else f"{count}@0"
        directory = out / f"load-{site}"
        # needletail-load takes --edges or --entry, never both: the edges
        # only without entry points.
        assign = ["--edges", ",".join(targets)]
        if not getattr(args, "load_direct", False):
            # Phase AM 10: every distributor and edge of the site answers
            # /api/assignment; the crowd asks it as real clients do, and a
            # refused listener is spilled by the entry (integration 6: the
            # direct crowd re-asked full edges).
            entries = [f"https://{lab.node(name)['public_ipv4']}:{lab.HTTP_PORT}"
                       for name in [*planned_nodes(args, "distributor", site), *edges]]
            assign = ["--entry", ",".join(entries), "--tls-ca", lab.ca_file(),
                      "--server-name", lab.SERVER_NAME]
        items.append({"site": site, "host": LOAD_HOSTS[site], "edges": dict(zip(targets, edges)),
                      "per_edge": args.load_listeners, "ramp": ramp, "out": str(directory),
                      "command": [str(LOAD_BIN), *assign,
                                  "--stream", str(PAYLOAD_STREAM[args.payload]),
                                  "--listeners", str(count), "--ramp", ramp,
                                  "--duration", str(args.seconds), "--out", str(directory),
                                  "--label", f"{args.name}-{site}", "--quiet",
                                  *split(args.load_args)]})
    return items


def edge_client_stats() -> dict[str, dict]:
    """Each serving edge's client UDP counters (/api/assignment), now."""
    out = {}
    for site in ("london", "newyork"):
        for name in lab.edges(site):
            try:
                status, body = lab.http_json(name, "/api/assignment")
            except Exception:  # a measurement only: the run goes on without it
                continue
            if status == 200 and isinstance(body, dict) and body.get("client_udp"):
                out[name] = body["client_udp"]
    return out


EDGE_COUNTERS = ("subscriptions", "refusals", "moves", "nack_parts", "nack_resent",
                 "nack_refused", "compact_window_repair", "would_block", "send_queue_drops",
                 "playout_skipped_parts")
FAST_COUNTERS = ("would_block", "given_back", "fanout_batches", "fanout_shed", "fanout_waited",
                 "compact_top_ups", "repair_handoffs", "repair_dropped", "class_repairs",
                 "class_repair_over_1ms", "fanout_lag_over_1ms", "fanout_inline_sends")
# Highest values since the edge started (wave 8 S1).
FAST_MAXIMA = ("class_repair_max_us", "repair_thread_max_us", "fanout_lag_max_us")


def edge_stats_delta(before: dict, after: dict) -> dict[str, dict]:
    """The edges' counters over the window (after - before)."""
    out = {}
    for name, end in after.items():
        start = before.get(name) or {}
        row = {key: (end.get(key) or 0) - (start.get(key) or 0) for key in EDGE_COUNTERS}
        fast_end, fast_start = end.get("fast_lane") or {}, start.get("fast_lane") or {}
        row["fast"] = {key: (fast_end.get(key) or 0) - (fast_start.get(key) or 0)
                       for key in FAST_COUNTERS}
        row["fast_clients"] = fast_end.get("clients")
        row["fast"].update({key: fast_end.get(key) for key in FAST_MAXIMA})
        out[name] = row
    return out


def edge_stats_lines(delta: dict[str, dict]) -> list[str]:
    if not delta:
        return []
    lines = ["", "edges' client UDP counters over the window (fast: the class thread's lane)"]
    for name, row in sorted(delta.items()):
        fast = row.get("fast") or {}
        lines.append(f"  {name:<11} clients {row.get('fast_clients')} subs {row['subscriptions']} "
                     f"moves {row['moves']} refusals {row['refusals']} nack parts "
                     f"{row['nack_parts']} resent {row['nack_resent']} refused "
                     f"{row['nack_refused']} window repair {row['compact_window_repair']} "
                     f"playout skipped {row['playout_skipped_parts']} | fast would_block "
                     f"{fast.get('would_block')} given_back {fast.get('given_back')} fan-out "
                     f"batches {fast.get('fanout_batches')} shed {fast.get('fanout_shed')} "
                     f"waited {fast.get('fanout_waited')} | window repair on the class thread "
                     f"{fast.get('class_repairs')} (max {fast.get('class_repair_max_us')} us, "
                     f"{fast.get('class_repair_over_1ms')} over 1 ms), handed off "
                     f"{fast.get('repair_handoffs')} (dropped {fast.get('repair_dropped')}, "
                     f"max {fast.get('repair_thread_max_us')} us) | fan-out lag max "
                     f"{fast.get('fanout_lag_max_us')} us, {fast.get('fanout_lag_over_1ms')} "
                     f"over 1 ms; inline sends {fast.get('fanout_inline_sends')}")
    return lines


def stall_edge(spec: str, began: float) -> dict:
    """--stall-edge: stop the London edge with the most UDP clients (the
    measuring clients' edge: the crowds spread evenly) for the stall."""
    second, _, length = spec.partition(":")
    time.sleep(max(0.0, began + float(second) - time.time()))
    counts = {}
    for name in lab.edges("london"):
        status, body = lab.http_json(name, "/api/assignment")
        if status == 200 and isinstance(body, dict):
            fast = ((body.get("client_udp") or {}).get("fast_lane") or {})
            counts[name] = fast.get("clients") or 0
    if not counts:
        return {"error": "no London edge answered"}
    edge = max(sorted(counts), key=lambda name: counts[name])
    started_us = time.time_ns() // 1000
    lab.freeze_service(edge, True)
    try:
        time.sleep(float(length) / 1000)
    finally:
        lab.freeze_service(edge, False)
    return {"edge": edge, "clients": counts, "from_us": started_us,
            "until_us": time.time_ns() // 1000}


def stall_nodes(specs: str, began: float) -> dict:
    """--stall-node: each NAME:SECOND:MS of the comma list in a thread of
    its own."""
    import threading
    items = [item for item in specs.split(",") if item]
    if len(items) == 1:
        return stall_node(items[0], began)
    out: dict = {}
    threads = [threading.Thread(target=lambda item=item: out.update(
        {item.split(":")[0]: stall_node(item, began)}), daemon=True) for item in items]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return out


def drain_node(spec: str, began: float, until: float) -> dict:
    """--drain-node: drain NAME at SECOND and sample its live gauges
    (/api/assignment client_udp: subscribers, draining; HLS sessions) every
    100 ms until its UDP subscribers reach 0 or the run ends (Phase AM 9)."""
    name, _, second = spec.partition(":")
    time.sleep(max(0.0, began + float(second) - time.time()))
    started_us = time.time_ns() // 1000
    status, body = lab.http_json(name, "/api/control/drain-node", "POST", {"node_id": name})
    samples = []
    while time.time() < until:
        state, found = lab.http_json(name, "/api/assignment")
        udp = (found or {}).get("client_udp") or {} if isinstance(found, dict) else {}
        samples.append({"t_ms": (time.time_ns() // 1000 - started_us) // 1000,
                        "subscribers": udp.get("subscribers"),
                        "hls_sessions": udp.get("hls_sessions"),
                        "draining": udp.get("draining"), "moves": udp.get("moves")})
        if udp.get("subscribers") == 0:
            break
        time.sleep(0.1)
    first = samples[0]["subscribers"] if samples else None
    left = [item["t_ms"] for item in samples
            if isinstance(item["subscribers"], int) and isinstance(first, int)
            and item["subscribers"] < first]
    zero = next((item["t_ms"] for item in samples if item["subscribers"] == 0), None)
    return {"node": name, "status": status, "from_us": started_us,
            "subscribers_at_drain": first, "first_left_ms": left[0] if left else None,
            "zero_ms": zero, "samples": samples}


def parse_node_events(spec: str) -> list[tuple[float, str, str]]:
    """--node-events: [(second, action, name)] in time order."""
    events = []
    for item in [item for item in spec.split(",") if item]:
        action, name, second = item.split(":")
        if action not in ("start", "stop", "kill", "drain", "resume"):
            raise SystemExit(f"--node-events: unknown action {action!r}")
        events.append((float(second), action, name))
    return sorted(events)


def node_events(spec: str, began: float) -> list[dict]:
    """--node-events: each event at its second of the window, in order."""
    done = []
    for second, action, name in parse_node_events(spec):
        time.sleep(max(0.0, began + second - time.time()))
        at_us = time.time_ns() // 1000
        detail = None
        if action == "start":
            if name in lab.SLOT_CANDIDATES:
                lab.set_slot_in_service(name, True)
            detail = lab.service_pid(name) or lab.start_service(name)
        elif action == "stop":
            lab.terminate_service(name)
        elif action == "kill":
            lab.kill_service(name)
        else:
            detail = lab.http_json(name, f"/api/control/{action}-node", "POST",
                                   {"node_id": name})[0]
        done.append({"action": action, "node": name, "second": second, "at_us": at_us,
                     "detail": detail})
    return done


def forwarder_sampler(stop, out: list) -> None:
    """--sample-forwarders: every London distributor's (the slot included)
    /api/mesh/local: fast_plane and cache_mesh gauges, its served leases
    (child, stream, rank) and its fast plane's received counts, once a
    second, until `stop` is set."""
    while not stop.is_set():
        row = {"t": round(time.time(), 3)}
        for name in lab.names("distributor", "london"):
            try:
                status, body = lab.http_json(name, "/api/mesh/local", timeout=0.8)
            except Exception:  # a measurement only
                continue
            if status != 200 or not isinstance(body, dict):
                continue
            served = ((body.get("cache_replication") or {}).get("served_subscriptions") or [])
            fast = ((body.get("relay_session") or {}).get("fast_plane") or {})
            row[name] = {
                "fast_plane": body.get("fast_plane"),
                "cache_mesh": {key: value for key, value in (body.get("cache_mesh") or {}).items()
                               if key != "draining_peers"},
                "served": [[item.get("child"), item.get("stream_id"), item.get("rank")]
                           for item in served],
                "audio_received": fast.get("audio_received"),
                "audio_forwarded": fast.get("audio_forwarded"),
            }
        out.append(row)
        stop.wait(1.0)


def forwarder_lines(rows: list[dict], events: list[dict], stream: int) -> tuple[list[str], dict]:
    """Per London distributor over the run: lease children and cache-mesh
    children (min/max), the warm-up counters at the end; and for the
    stream measured, each child's first parent per sample: how many
    (child, stream) pairs changed their first parent, and when."""
    if not rows:
        return ([""] + ["node events: " + "; ".join(
            f"{item['action']} {item['node']} at {item['second']} s" for item in events)]
            if events else []), {"events": events}
    names = sorted({key for row in rows for key in row if key != "t"})
    lines = ["", "forwarders (Phase AM 12, 13): node: lease children min/max, cache-mesh "
                 "children min/max, leases ranked first max; at the end: warm parts/bytes, "
                 "largest warm tick bytes, capped ticks, parents released, watermark stall "
                 "max ms"]
    report: dict = {"nodes": {}, "events": events}
    for name in names:
        items = [row[name] for row in rows if isinstance(row.get(name), dict)]
        lease = [((item.get("fast_plane") or {}).get("lease_children")) for item in items]
        lease = [value for value in lease if isinstance(value, int)]
        cache = [(item.get("cache_mesh") or {}).get("children") for item in items]
        cache = [value for value in cache if isinstance(value, int)]
        first = [(item.get("cache_mesh") or {}).get("first_parent_pairs") for item in items]
        first = [value for value in first if isinstance(value, int)]
        last = (items[-1].get("cache_mesh") or {}) if items else {}
        report["nodes"][name] = {"lease_children": lease, "cache_children": cache,
                                 "last": last, "samples": len(items)}
        stall = last.get("watermark_stall_max_us")
        lines.append(
            f"  {name}: {min(lease) if lease else '-'}/{max(lease) if lease else '-'}, "
            f"{min(cache) if cache else '-'}/{max(cache) if cache else '-'}, "
            f"{max(first) if first else '-'}; {last.get('warm_parts_sent')}/"
            f"{last.get('warm_bytes_sent')}, {last.get('warm_tick_max_bytes')}, "
            f"{last.get('warm_capped_ticks')}, {last.get('parents_released')}, "
            f"{round(stall / 1000, 1) if isinstance(stall, int) else '-'}")
    # The first parent of each (child, stream) pair, sample by sample.
    firsts: list[tuple[float, dict]] = []
    for row in rows:
        mapping = {}
        for name in names:
            for child, stream_id, rank in (row.get(name) or {}).get("served") or []:
                if stream_id == stream and rank == 0:
                    mapping[child.rsplit(":", 1)[0]] = name
        firsts.append((row["t"], mapping))
    changes = []
    for (_, before), (at, after) in zip(firsts, firsts[1:]):
        for child in sorted(set(before) | set(after)):
            if before.get(child) != after.get(child):
                changes.append({"t": at, "child": child, "from": before.get(child),
                                "to": after.get(child)})
    pairs = sorted({child for _, mapping in firsts for child in mapping})
    report["first_parent_changes"] = changes
    report["pairs"] = len(pairs)
    lines.append(f"  first parents of stream {stream}: {len(pairs)} (edge, stream) pairs; "
                 f"{len(changes)} changes: " + "; ".join(
                     f"{change['child']} {change['from']}->{change['to']} at "
                     f"{round(change['t'] - rows[0]['t'], 1)} s" for change in changes[:12]))
    if events:
        lines.append("  node events: " + "; ".join(
            f"{item['action']} {item['node']} at {item['second']} s" for item in events))
    return lines, report


def gauge_sampler(stop, out: list) -> None:
    """--sample-gauges: every serving edge's client UDP gauges once a
    second (Phase AM 8), until `stop` is set."""
    while not stop.is_set():
        row = {"t": round(time.time(), 3)}
        for site in ("london", "newyork"):
            for name in lab.edges(site):
                try:
                    status, body = lab.http_json(name, "/api/assignment")
                except Exception:  # a measurement only
                    continue
                udp = (body or {}).get("client_udp") if isinstance(body, dict) else None
                if status == 200 and isinstance(udp, dict):
                    caches = {item.get("node_id"): item for item in body.get("caches") or []}
                    row[name] = {key: udp.get(key) for key in (
                        "subscribers", "load_permille", "egress_observed_bps", "draining",
                        "hls_sessions", "load_refusals", "egress_wire_bytes")}
                    row[name]["advertised_free_bps"] = (caches.get(name) or {}).get("free_bps")
        out.append(row)
        stop.wait(1.0)


def gauge_lines(rows: list[dict]) -> list[str]:
    """Per edge over the run: the most subscribers, load_permille (max and
    mean of the samples with subscribers), measured egress."""
    if not rows:
        return []
    names = sorted({key for row in rows for key in row if key != "t"})
    lines = ["", "client UDP gauges (Phase AM 8): edge: subscribers max, load_permille "
                 "max/mean, egress Mbit/s max/mean, free Mbit/s min"]
    for name in names:
        items = [row[name] for row in rows if isinstance(row.get(name), dict)]
        loaded = [item for item in items if (item.get("subscribers") or 0) > 0] or items
        def numbers(key):
            return [item.get(key) for item in loaded if isinstance(item.get(key), (int, float))]
        load, egress = numbers("load_permille"), numbers("egress_observed_bps")
        free = numbers("advertised_free_bps")
        lines.append(
            f"  {name}: {max([item.get('subscribers') or 0 for item in items] or [0])}, "
            f"{max(load or [0])}/{round(sum(load) / len(load)) if load else '-'}, "
            f"{round(max(egress or [0]) / 1e6, 2)}/"
            f"{round(sum(egress) / len(egress) / 1e6, 2) if egress else '-'}, "
            f"{round(min(free) / 1e6, 2) if free else '-'}")
    return lines


def stall_node(spec: str, began: float) -> dict:
    """--stall-node: stop one named node for the stall."""
    name, second, length = spec.split(":")
    time.sleep(max(0.0, began + float(second) - time.time()))
    started_us = time.time_ns() // 1000
    lab.freeze_service(name, True)
    try:
        time.sleep(float(length) / 1000)
    finally:
        lab.freeze_service(name, False)
    return {"node": name, "from_us": started_us, "until_us": time.time_ns() // 1000}


def edge_wake_report(events: list[dict], since_us: int | None,
                     until_us: int | None) -> dict[str, dict]:
    """Per edge, the class thread's wake: relay_first (the kernel's receive
    time of a part's first symbol) -> relay_woken (when the thread read it),
    and relay_woken -> udp_sent (its send to the first client): a thread
    that waits on a lock shows here (wave 8 S1)."""
    first: dict[tuple, int] = {}
    woken: dict[tuple, int] = {}
    sent: dict[tuple, int] = {}
    for event in events:
        node = event.get("_node") or ""
        if "-edge-" not in node:
            continue
        key = (node, event["seq"])
        stage, at = event["stage"], event["t_us"]
        if stage == "relay_first":
            first[key] = min(first.get(key, at), at)
        elif stage == "relay_woken":
            woken[key] = min(woken.get(key, at), at)
        elif stage == "udp_sent":
            sent[key] = min(sent.get(key, at), at)
    per: dict[str, dict] = {}
    for key, rx in first.items():
        if (since_us is not None and rx < since_us) or (until_us is not None and rx > until_us):
            continue
        node = key[0]
        row = per.setdefault(node, {"wake": [], "send": []})
        if key in woken:
            row["wake"].append((woken[key] - rx) / 1000)
            if key in sent:
                row["send"].append((sent[key] - woken[key]) / 1000)
    out = {}
    for node, row in sorted(per.items()):
        out[node] = {"wake_ms": hops.distribution(row["wake"]),
                     "wake_over_1ms": sum(1 for value in row["wake"] if value > 1.0),
                     "woken_to_sent_ms": hops.distribution(row["send"])}
    return out


def edge_wake_lines(report: dict[str, dict]) -> list[str]:
    if not report:
        return []
    lines = ["", "edges' class thread (ms p50/p99/max; relay_first -> relay_woken, woken -> "
                 "udp_sent)"]
    for node, row in report.items():
        wake, send = row["wake_ms"] or {}, row["woken_to_sent_ms"] or {}
        lines.append(f"  {node:<11} wake {wake.get('p50')}/{wake.get('p99')}/{wake.get('max')} "
                     f"(n {wake.get('count')}, {row['wake_over_1ms']} over 1 ms)  "
                     f"woken->sent {send.get('p50')}/{send.get('p99')}/{send.get('max')}")
    return lines


def load_report(items: list[dict]) -> list[dict]:
    """What each needletail-load crowd saw (its summary.json)."""
    out = []
    for item in items:
        try:
            summary = json.loads((pathlib.Path(item["out"]) / "summary.json").read_text())
        except (OSError, ValueError):
            out.append({"site": item["site"], "error": "no summary"})
            continue
        total = summary.get("total") or {}
        keys = ("listeners_started", "listeners_with_parts", "parts", "missing", "gaps",
                "recovered", "duplicates", "nack_parts", "abandoned", "moves", "socket_drops",
                "compact_fallbacks", "latency_ms", "ttfp_ms", "moves_out", "moves_by_reason",
                "time_to_move_ms", "move_lost", "missing_moved", "never_resumed",
                "assignments", "assignment_errors", "assignment_ms", "fallbacks",
                # B Y4: the two-path figures (needletail-load at av-mesh 1a38d4a).
                "interruptions", "single_path_ms", "exposure_ms", "handover_ms", "hedges",
                "datagrams_per_leg_s", "wire_bytes_per_leg_s")
        out.append({"site": item["site"], "per_edge": item["per_edge"], "ramp": item["ramp"],
                    "total": {key: slim(total.get(key)) for key in keys if key in total},
                    "edges": {item["edges"].get(edge, edge): {
                        key: slim(row.get(key)) for key in ("listeners_started", "parts",
                                                            "missing", "nack_parts", "moves",
                                                            "moves_out", "time_to_move_ms",
                                                            "latency_ms")
                        if key in row}
                        for edge, row in (summary.get("per_edge") or {}).items()},
                    "saturated": summary.get("load_host_saturated"),
                    **busy_reasks(pathlib.Path(item["out"]) / "intervals.jsonl",
                                  total.get("listeners_started"), summary.get("duration_s")),
                    **{key: slim(summary.get(key)) for key in (
                        "paths", "spare", "listeners_interrupted", "listeners_with_missing",
                        "longest_silence_ms", "never_resumed", "bytes_per_listener_second")
                       if key in summary}})
    return out


def busy_reasks(path: pathlib.Path, listeners, duration_s) -> dict:
    """rm/cpuadmit 156fa2a: the crowd's re-asks put off by its back-off
    after edge-busy/edge-full, from needletail-load's interval lines (each
    a delta; summary.json does not carry them): {"busy_reasks": total,
    "busy_reasks_per_listener_10s": over the run, "_max": the worst
    interval's, per active listener, scaled to 10 s}. A refused crowd that
    backs off re-asks a few times per listener per 10 s, not hundreds. {}
    when the tool says nothing (a build before 156fa2a)."""
    total, worst, seen, last_t = 0, None, False, 0.0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        counts = row.get("total") if isinstance(row, dict) else None
        if not isinstance(counts, dict) or not isinstance(counts.get("busy_reasks"), int):
            continue
        seen = True
        value = counts["busy_reasks"]
        total += value
        t = row.get("t") if isinstance(row.get("t"), (int, float)) else None
        active = counts.get("active") or counts.get("up")
        if row.get("phase") != "final" and t is not None and t > last_t \
                and isinstance(active, int) and active > 0:
            rate = value / active * 10.0 / (t - last_t)
            worst = rate if worst is None else max(worst, rate)
        if t is not None:
            last_t = t
    if not seen:
        return {}
    out = {"busy_reasks": total}
    if isinstance(listeners, int) and listeners > 0 and isinstance(duration_s, (int, float)) \
            and duration_s > 0:
        out["busy_reasks_per_listener_10s"] = round(total / listeners * 10.0 / duration_s, 2)
    if worst is not None:
        out["busy_reasks_per_listener_10s_max"] = round(worst, 2)
    return out


def slim(value):
    """A histogram without its buckets."""
    if isinstance(value, dict):
        return {key: value.get(key) for key in ("count", "p50", "p95", "p99", "max")}
    return value


def load_lines(report: list[dict]) -> list[str]:
    if not report:
        return []

    def dist(value) -> str:
        if not isinstance(value, dict):
            return str(value)
        return "/".join(str(value.get(key)) for key in ("p50", "p99", "max"))
    lines = ["", "needletail-load crowds (edge load; latency ms p50/p99/max)"]
    for item in report:
        if item.get("error"):
            lines.append(f"  {item['site']}: {item['error']}")
            continue
        total = item["total"]
        lines.append(f"  {item['site']}: {item['per_edge']} a edge (ramp {item['ramp']}), "
                     f"saturated {item.get('saturated')}: "
                     + " ".join(f"{key} {dist(value)}" for key, value in total.items()))
        if "busy_reasks" in item:
            lines.append(f"    busy re-asks {item['busy_reasks']} (per listener per 10 s "
                         f"{item.get('busy_reasks_per_listener_10s')}, worst interval "
                         f"{item.get('busy_reasks_per_listener_10s_max')})")
        for edge, row in sorted(item["edges"].items()):
            lines.append(f"    {edge:<11} " + " ".join(f"{key} {dist(value)}"
                                                    for key, value in row.items()))
    return lines


def read_arrivals(path: pathlib.Path, since_us: int | None = None,
                  until_us: int | None = None) -> dict[int, dict]:
    """{part: {"arrival_us", "bytes", "published_us", "available_us"}} of a
    UDP client's --arrivals record, the parts that arrived in the window."""
    parts: dict[int, dict] = {}
    try:
        handle = path.open()
    except OSError:
        return parts
    with handle:
        for row in csv.DictReader(handle):
            try:
                part = int(row["part"])
                arrival = int(row["arrival_unix_us"])
            except (KeyError, TypeError, ValueError):
                continue
            if (since_us is not None and arrival < since_us) or \
                    (until_us is not None and arrival > until_us):
                continue

            def field(name: str) -> int | None:
                try:
                    value = int(row.get(name) or 0)
                except ValueError:
                    return None
                return value if value > 0 else None

            parts[part] = {"arrival_us": arrival, "bytes": field("bytes") or 0,
                           "published_us": field("published_unix_us"),
                           "available_us": field("available_unix_us"),
                           "kernel_us": field("kernel_unix_us")}
    return parts


def arrivals_probe(item: dict, parts: dict[int, dict], report: dict | None) -> dict:
    """A UDP client's parts as hops.analyse reads a listener's record: the
    part complete at its arrival, at the edge it was served by (the one
    cache of its report; a client that moved is placed by the journal)."""
    caches = sorted(set((report or {}).get("caches") or []))
    host_ip = None
    if len(caches) == 1:
        try:
            host_ip = lab.node(caches[0])["public_ipv4"]
        except lab.LabError:
            host_ip = None
    rows = {part: {"complete_us": row["arrival_us"], "spoken_us": None,
                   "published_us": row["published_us"],
                   "cache_available_us": row["available_us"], "edge_committed_us": None,
                   "reply_us": None}
            for part, row in parts.items()}
    return {"path": item["arrivals"], "site": item["site"], "host_ip": host_ip, "rows": rows}


def source_clock(nodes: dict, events: list[dict], stream: int, source_start_us: int | None,
                 epoch_us: float, since_us: int | None, until_us: int | None):
    """seq -> the instant its last sample was spoken (hops.py's rule: the
    epoch of the contributor's src_last; a part the journal does not hold
    takes its own number as its epoch when the journal shows seq = epoch)."""
    if source_start_us is None:
        return lambda seq: None
    journal = hops.Journal(nodes, events, stream, since_us, until_us)
    offsets: dict[int, int] = {}
    for (_stream, seq), slot in journal.epochs.items():
        first = slot.get("src_first", min(slot.values()))
        offsets[seq - first] = offsets.get(seq - first, 0) + 1
    checked = sum(offsets.values())
    equal = checked == 0 or offsets.get(0, 0) >= 0.99 * checked

    def source_of(seq: int) -> int | None:
        epoch = journal.last_epoch(stream, seq)
        if epoch is None and equal:
            epoch = seq
        if epoch is None:
            return None
        return source_start_us + round((epoch + 1) * epoch_us)
    return source_of


def udp_client_verdict(item: dict, report: dict | None, parts: dict[int, dict],
                       source_of) -> dict:
    """One audio UDP client: its unit latency (source spoken -> part
    arrived), publish -> arrival, the lane of its parts, its wire bytes per
    part and the parts it missed."""
    out: dict = {"host": item["host"], "site": item["site"],
                 "report": pathlib.Path(item["report"]).name}
    if not report:
        out["error"] = "no report"
    report = report or {}
    unit, published, kernel = [], [], []
    for part, row in parts.items():
        source = source_of(part)
        if source is not None:
            unit.append((row["arrival_us"] - source) / 1000)
            if row.get("kernel_us"):
                kernel.append((row["kernel_us"] - source) / 1000)
        if row["published_us"]:
            published.append((row["arrival_us"] - row["published_us"]) / 1000)
    received = report.get("received_parts") or 0
    compact = report.get("compact") or {}
    out.update({
        "caches": report.get("caches"), "moves": len(report.get("moves") or []),
        "expected": report.get("expected_parts"), "received": report.get("received_parts"),
        "missing": report.get("missing_parts"), "gaps": report.get("gap_parts"),
        "fast_lane_asked": report.get("fast_lane_asked"),
        "fast_lane_parts": report.get("fast_lane_parts"),
        "cache_lane_parts": report.get("cache_lane_parts"),
        "compact_plan": report.get("final_compact_plan"),
        "nack_parts": compact.get("nack_parts_sent"), "retransmits": compact.get("retransmits"),
        "recovered_by_window": compact.get("recovered_by_window"),
        "wire_bytes_per_part": round(report["wire_bytes"] / received, 1)
        if received and report.get("wire_bytes") is not None else None,
        "wire_per_payload": None if report.get("wire_per_payload") is None
        else round(report["wire_per_payload"], 3),
        "wire_kbps": None if report.get("wire_bits_per_second") is None
        else round(report["wire_bits_per_second"] / 1000, 1),
        "window_parts": len(parts),
        # Kernel receive -> the client read the datagram: the listener
        # host's own delay (clients from before wave 6 do not report it).
        "host_delay_ms": report.get("kernel_to_arrival_ms"),
        # Time to first audio (wave 7 item 8): from the client's start, and
        # each step of the start (clients from before item 8 give the total
        # alone).
        "time_to_first_part_ms": report.get("time_to_first_part_ms"),
        "startup": report.get("startup"),
        "unit_latency_ms": hops.distribution(unit),
        # The same at the kernel's receive time: without the listener host's
        # own delay (the local lab's listeners share the dev host).
        "kernel_unit_latency_ms": hops.distribution(kernel),
        "publish_to_arrival_ms": hops.distribution(published)})
    return out


def merge_counts(clients: list[dict], key: str) -> int | None:
    values = [item.get(key) for item in clients if item.get(key) is not None]
    return sum(values) if values else None


def udp_lane_report(items: list[dict], nodes: dict, events: list[dict], stream: int,
                    source_start_us: int | None, epoch_us: float, since_us: int | None,
                    until_us: int | None) -> tuple[dict, list[dict]]:
    """Per reader host: every audio UDP client's verdict and the host's
    distributions over all its clients' parts; and the clients' records as
    hops.analyse listener probes."""
    source_of = source_clock(nodes, events, stream, source_start_us, epoch_us, since_us,
                             until_us)
    hosts: dict[str, dict] = {}
    probes = []
    for item in items:
        try:
            report = json.loads(pathlib.Path(item["report"]).read_text())
        except (OSError, ValueError):
            report = None
        parts = read_arrivals(pathlib.Path(item["arrivals"]), since_us, until_us)
        verdict = udp_client_verdict(item, report, parts, source_of)
        host = hosts.setdefault(item["host"], {"site": item["site"], "clients": [],
                                                "_unit": [], "_published": [], "_kernel": []})
        host["clients"].append(verdict)
        for part, row in parts.items():
            source = source_of(part)
            if source is not None:
                host["_unit"].append((row["arrival_us"] - source) / 1000)
                if row.get("kernel_us"):
                    host["_kernel"].append((row["kernel_us"] - source) / 1000)
            if row["published_us"]:
                host["_published"].append((row["arrival_us"] - row["published_us"]) / 1000)
        if parts:
            probes.append(arrivals_probe(item, parts, report))
    for host in hosts.values():
        clients = host["clients"]
        received = merge_counts(clients, "received") or 0
        wire = sum((item.get("wire_bytes_per_part") or 0) * (item.get("received") or 0)
                   for item in clients)
        host.update({
            "unit_latency_ms": hops.distribution(host.pop("_unit")),
            "kernel_unit_latency_ms": hops.distribution(host.pop("_kernel")),
            "publish_to_arrival_ms": hops.distribution(host.pop("_published")),
            "expected": merge_counts(clients, "expected"), "received": received,
            "missing": merge_counts(clients, "missing"),
            "fast_lane_parts": merge_counts(clients, "fast_lane_parts"),
            "cache_lane_parts": merge_counts(clients, "cache_lane_parts"),
            "time_to_first_part_ms": hops.distribution(
                [item["time_to_first_part_ms"] for item in clients
                 if item.get("time_to_first_part_ms") is not None]),
            "wire_bytes_per_part": round(wire / received, 1) if received else None})
    return hosts, probes


def udp_hop_lines(regions: dict) -> list[str]:
    """The hop table of the UDP clients' parts in each region (as hops.py
    prints a region's listener parts)."""
    lines = []
    for region, kinds in sorted(regions.items()):
        report = kinds["listener_parts"]
        planes = report.get("planes") or {}
        lines.append(f"{region} / udp clients: {report['parts']} parts ({planes.get('fast', 0)} "
                     f"on the fast plane, {planes.get('mesh', 0)} over the cache mesh)"
                     "   (ms: n p50 p95 p99 max)")
        for item in report["hops"]:
            lines.append(f"  {item['hop']:<48} {item['count']:>7} {item['p50']:>8.3f} "
                         f"{item['p95']:>8.3f} {item['p99']:>8.3f} {item['max']:>9.3f}")
        for item in report["from_source"]:
            if item["stage"] in ("listener.complete", hops.UDP_SENT):
                lines.append(f"  source -> {item['stage']:<38} {item['count']:>7} "
                             f"{item['p50']:>8.3f} {item['p95']:>8.3f} {item['p99']:>8.3f} "
                             f"{item['max']:>9.3f}")
    return lines


def startup_text(item: dict) -> str:
    """A client's time to first audio and its steps (ms from its start):
    assignment, playlist read, first request, token, OK, first part."""
    first = item.get("time_to_first_part_ms")
    if first is None:
        return ""
    steps = item.get("startup") or {}
    keys = ("assignment_ms", "playlist_ms", "first_request_ms", "token_ms", "subscribed_ms")
    if not any(steps.get(key) is not None for key in keys):
        return f" first part {first:.1f} ms"

    def ms(key: str) -> str:
        value = steps.get(key)
        return "-" if value is None else f"{value:.1f}"
    return (f" first part {first:.1f} ms (assignment {ms('assignment_ms')}, playlist "
            f"{ms('playlist_ms')}, request {ms('first_request_ms')}, token {ms('token_ms')}, "
            f"OK {ms('subscribed_ms')})")


def udp_summary_lines(hosts: dict) -> list[str]:
    if not hosts:
        return []

    def dist(value: dict | None) -> str:
        if not value:
            return "-"
        return "/".join(f"{value.get(key)}" for key in ("p50", "p95", "p99", "max"))

    lines = ["", "UDP client lane (the headline lane; ms p50/p95/p99/max)"]
    for name, host in sorted(hosts.items()):
        lines.append(f"  {name} ({host['site']}): unit latency {dist(host['unit_latency_ms'])}  "
                     f"at kernel rx {dist(host.get('kernel_unit_latency_ms'))}  "
                     f"publish->arrival {dist(host['publish_to_arrival_ms'])}  "
                     f"fast lane {host['fast_lane_parts']} cache lane {host['cache_lane_parts']}  "
                     f"wire {host['wire_bytes_per_part']} B/part  missing {host['missing']} "
                     f"of {host['expected']}  first part {dist(host.get('time_to_first_part_ms'))} ms")
        for item in host["clients"]:
            lines.append(f"    {item['report']:<28} {item.get('received')}/{item.get('expected')} "
                         f"missing {item.get('missing')} gaps {item.get('gaps')} "
                         f"caches {','.join(item.get('caches') or []) or '-'} "
                         f"moves {item.get('moves')} plan {item.get('compact_plan')} "
                         f"nack {item.get('nack_parts')} wire {item.get('wire_bytes_per_part')} "
                         f"B/part ({item.get('wire_per_payload')}x, {item.get('wire_kbps')} kbit/s)"
                         + (f" host delay p99/max {(item.get('host_delay_ms') or {}).get('p99')}/"
                            f"{(item.get('host_delay_ms') or {}).get('max')}"
                            if item.get("host_delay_ms") else "")
                         + startup_text(item)
                         + (f" ({item['error']})" if item.get("error") else ""))
    return lines


def index_media(args, loop_file: str) -> tuple[str, list[str]]:
    """The ntcmp audio index of the run and the command that makes it. PCM
    parts are the loop's own bytes, recognised by fingerprint in blocks of
    one epoch; FLAC and Opus parts are recognised by number (part N is epoch
    N of the loop, no offset)."""
    samples = epoch_samples(args)
    ms = epoch_ms_text(samples)
    if args.payload == "pcm":
        index = str(MEDIA / f"pcm-{samples}s.index.json")
        return index, [args.ntcmp, "audio-index", loop_file, index, ms]
    name = f"nt-{args.payload}-{samples}s"
    index = str(MEDIA / f"{name}.index.json")
    return index, [args.ntcmp, "audio-index", f"numbered:{name}", index, ms, "0"]


def bench_dir(name: str, ident: str | None) -> pathlib.Path:
    """A pass's results: under the lab's id when it has one, so that two
    labs' passes of one name do not meet."""
    return OUT_ROOT / ident / name if ident else OUT_ROOT / name


def plan(args) -> dict:
    out = bench_dir(args.name, lab.LAB_ID)
    loop_file = str(MEDIA / "loop64.s24le")
    index, index_command = index_media(args, loop_file)
    start = loop_start(time.time())
    samples = epoch_samples(args)
    return {
        "name": args.name, "out": str(out), "seconds": args.seconds, "readers_per_kind": args.readers,
        "payload": args.payload, "epoch_samples": samples,
        "epoch_us": hops.epoch_us_of(samples), "parts_per_second": parts_per_second(samples),
        "reader_windows": reader_windows(args),
        "lab_up": None if args.no_up else lab_overrides(args),
        "media": {"loop": loop_file, "index": index, "from": args.pcm,
                  "index_command": index_command},
        "loop_start_unix": start,
        "sender": {"namespace": sender_place(args)[0],
                   "command": sender_command(args, start, loop_file)},
        "readers": reader_commands(args, out, index),
        "udp_clients": audio_udp_commands(args, out),
        "video": {"source": video_source(args), "viewers": video_commands(args, out),
                  "frame_deadline_ms": args.frame_deadline_ms,
                  "sample_nodes": video_measured(args) or args.sample_nodes},
        "reader_netem": reader_netem(args),
        "hops": {"stream": PAYLOAD_STREAM[args.payload], "source_start_ns": start * 1_000_000_000,
                 "skip_seconds": skip_seconds(args),
                 "journal_capacity": None if args.no_up else journal_capacity(args)},
    }


def prepare_media(item: dict) -> None:
    """The loop and the run's index, each made once under the media lock,
    written beside and renamed, so that a pass of another lab never reads
    one half written."""
    loop = pathlib.Path(item["loop"])
    index = pathlib.Path(item["index"])
    with media_lock():
        if not loop.is_file() or loop.stat().st_size != LOOP_BYTES:
            with open(item["from"], "rb") as source:
                data = source.read(LOOP_BYTES)
            if len(data) != LOOP_BYTES:
                raise SystemExit(f"{item['from']} is shorter than {LOOP_SECONDS} s of stereo s24le")
            beside = loop.with_name(loop.name + ".part")
            beside.write_bytes(data)
            beside.replace(loop)
        if not index.is_file():
            beside = index.with_name(index.name + ".part")
            command = [str(beside) if part == str(index) else part
                       for part in item["index_command"]]
            subprocess.run(command, check=True, capture_output=True)
            beside.replace(index)


def reader_summary(report: pathlib.Path) -> dict:
    try:
        data = json.loads(report.read_text())
    except (OSError, ValueError):
        return {"report": report.name, "error": "no report"}
    latency = data.get("audio_latency_ms") or {}
    detail = data.get("detail") or {}
    return {"report": report.name, "units": data.get("audio_packets"),
            "gaps": data.get("audio_sequence_gaps"), "error": data.get("error"),
            "latency_ms": {key: latency.get(key) for key in ("p50", "p95", "p99", "max")},
            "server_timing_ms": detail.get("server_timing_ms"), "counts": detail.get("counts")}


def journal_full(events: list[dict], capacity: int | None) -> list[str]:
    """Nodes that returned as many events as their ring holds: the ring was
    overwritten and the oldest events of the window are gone."""
    if not capacity:
        return []
    counts: dict[str, int] = {}
    for item in events:
        node = item.get("_node") or item.get("node")
        counts[node] = counts.get(node, 0) + 1
    return sorted(node for node, count in counts.items() if count >= capacity)


def run(args, steps: dict, record: "PassRecord") -> int:
    out = pathlib.Path(steps["out"])
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("*.probe*"), *out.glob("*.4k.*"), *out.glob("*.uc*"),
                out / "nodes.ndjson"]:
        old.unlink(missing_ok=True)
    # The sender's summary of this run is read from its log.
    (out / "sender.log").unlink(missing_ok=True)
    (out / "plan.json").write_text(json.dumps(steps, indent=1) + "\n")
    if not pathlib.Path(args.ntcmp).is_file():
        raise SystemExit(f"{args.ntcmp} is missing: build it with scripts/compare/gobuild.sh")
    prepare_media(steps["media"])
    video = steps["video"]
    video["source"] = prepare_video(video["source"])
    if (any(item["kind"] == "udp" for item in video["viewers"] + steps["udp_clients"])
            and not pathlib.Path(args.udp_client).is_file()):
        raise SystemExit(f"{args.udp_client} is missing: build av-mesh's needletail-udp-client "
                         "or give --udp-clients 0 --video-udp-clients 0")
    (out / "plan.json").write_text(json.dumps(steps, indent=1) + "\n")
    if steps["lab_up"] is None and not lab.load("services.json"):
        raise SystemExit("--no-up: the lab is not running")
    try:
        if steps["lab_up"] is not None:
            log(f"lab up: {' '.join(steps['lab_up'])}")
            lab.up(steps["lab_up"])
        nodes, events, since_us, ended_us, measured = measure(args, steps, out)
    finally:
        record.node_logs()
        if not args.keep_up and steps["lab_up"] is not None:
            lab.down(quiet=True)
    # The epoch of the run: what the sender reports it sent, else the plan's.
    summary = hops.read_sender_summary(out / "sender.log")
    epoch = run_epoch(steps, summary)
    hops.save_raw(out / "hops.raw.ndjson", nodes, events, epoch)
    probes = []
    for item in steps["readers"]:
        for path in sorted(out.glob(f"{item['host']}.{item['lane']}.probe*.csv")):
            probes.append(hops.read_probe(f"{item['site']}={path}"))
    epoch_note = sender_mismatch(steps, summary)
    result = hops.analyse(nodes, events, probes, steps["hops"]["stream"],
                          steps["hops"]["source_start_ns"] // 1000, epoch["epoch_us"],
                          since_us=since_us, until_us=ended_us)
    result["source"].update({"epoch_samples": epoch["epoch_samples"], "epoch_from": epoch["from"],
                             "payload": args.payload})
    summaries = [reader_summary(path) for path in sorted(out.glob("*.probe*.json"))]
    capacity = steps["hops"]["journal_capacity"]
    full = journal_full(events, capacity)
    udp_hosts, udp_probes = udp_lane_report(
        steps["udp_clients"], nodes, events, steps["hops"]["stream"],
        steps["hops"]["source_start_ns"] // 1000, epoch["epoch_us"], since_us, ended_us)
    if steps["udp_clients"]:
        # The hops of the UDP clients' parts, apart from the HTTP readers'.
        udp_hops = hops.analyse(nodes, events, udp_probes, steps["hops"]["stream"],
                                steps["hops"]["source_start_ns"] // 1000, epoch["epoch_us"],
                                since_us=since_us, until_us=ended_us)
        result["udp_clients"] = {"hosts": udp_hosts,
                                 "listeners": udp_hops["listeners"],
                                 "regions": {region: {"listener_parts": kinds["listener_parts"]}
                                             for region, kinds in udp_hops["regions"].items()
                                             if "listener_parts" in kinds}}
    result["bench"] = {**{key: value for key, value in steps.items() if key != "readers"},
                       "reader_commands": [item["command"] for item in steps["readers"]],
                       "readers": summaries, "window_us": [since_us, ended_us],
                       "journal_full": full, "sender": sender_digest(summary),
                       "epoch_note": epoch_note, "nodes": measured.get("nodes")}
    regions = {}
    if video["source"] is not None and video["viewers"]:
        regions = video_report(video["viewers"], video["frame_deadline_ms"], since_us, ended_us)
        video_nodes, video_events = measured.get("video_journal") or ({}, [])
        result["video"] = {
            "source": video["source"], "frame_deadline_ms": video["frame_deadline_ms"],
            "regions": regions,
            # The 4K stream's hops (no source clock: from the contributor on).
            "hops": hops.analyse(video_nodes, video_events, [], VIDEO_STREAM, None,
                                 since_us=since_us, until_us=ended_us)}
    lines = [f"latency bench {args.name}: {args.seconds} s, {args.readers} reader(s) of each kind "
             f"on {args.hosts}",
             f"lab settings: {' '.join(steps['lab_up'] or ['(running lab)'])}",
             f"sender: {args.payload}, {epoch['epoch_samples']}-sample epochs "
             f"({epoch['epoch_us'] / 1000:g} ms parts, from {epoch['from']}); "
             f"{' '.join(split(args.sender_args)) or '(defaults)'}; "
             f"reader links: {steps['reader_netem'] or 'clean'}",
             "reader windows (parts in flight): " + ", ".join(
                 f"{lane} {window}" for lane, window in (steps.get("reader_windows") or {}).items()),
             *([f"WARNING: {epoch_note}"] if epoch_note else []),
             qdisc_note(),
             f"hop journal: {capacity if capacity is not None else 'as running'} events per node"
             + (f"; WARNING: full on {', '.join(full)}: the start of the window was overwritten"
                if full else ""), ""]
    for item in summaries:
        latency = item.get("latency_ms") or {}
        lines.append(f"{item['report']:<36} units {item.get('units')} gaps {item.get('gaps')} "
                     f"latency p50 {latency.get('p50')} p95 {latency.get('p95')} "
                     f"p99 {latency.get('p99')} max {latency.get('max')}"
                     + (f" error {item['error']}" if item.get("error") else ""))
    lines += udp_summary_lines(udp_hosts)
    wake = edge_wake_report(events, since_us, ended_us)
    result["edges"] = {"class_thread": wake, "client_udp": measured.get("edge_stats"),
                       "load": measured.get("load")}
    lines += load_lines(measured.get("load") or [])
    lines += edge_stats_lines(measured.get("edge_stats") or {})
    lines += edge_wake_lines(wake)
    if measured.get("stall"):
        result["stall"] = measured["stall"]
        lines += ["", f"stall: {json.dumps(measured['stall'])}"]
    if measured.get("stall_node"):
        result["stall_node"] = measured["stall_node"]
        lines += ["", f"stalled node: {json.dumps(measured['stall_node'])}"]
    if measured.get("drain_node"):
        drained = measured["drain_node"]
        result["drain_node"] = drained
        lines += ["", "drained node: " + json.dumps(
            {key: value for key, value in drained.items() if key != "samples"})]
    if measured.get("gauges"):
        result["gauges"] = measured["gauges"]
        lines += gauge_lines(measured["gauges"])
    if measured.get("forwarders") or measured.get("node_events"):
        more, report = forwarder_lines(measured.get("forwarders") or [],
                                       measured.get("node_events") or [],
                                       steps["hops"]["stream"])
        result["forwarders"] = {**report, "rows": measured.get("forwarders") or []}
        lines += more
    lines += video_summary_lines(video["source"] if regions else None, regions,
                                 video["frame_deadline_ms"])
    lines += node_summary_lines(measured.get("nodes"))
    lines += node_traffic_lines(measured.get("nodes"), 1e6 / epoch["epoch_us"])
    text = "\n".join(lines) + "\n\n" + hops.format_summary(result)
    if result.get("udp_clients", {}).get("regions"):
        text += "\nUDP client hops (the UDP clients' parts):\n" + "\n".join(
            udp_hop_lines(result["udp_clients"]["regions"])) + "\n"
    if "video" in result:
        text += "\n4K hops (stream 1):\n" + hops.format_summary(result["video"]["hops"])
    (out / "hops.json").write_text(json.dumps(result, indent=1) + "\n")
    (out / "summary.txt").write_text(text)
    print(text, end="")
    return 0


def qdisc_note() -> str:
    """What the host qdisc did in this bench: in the namespace lab the prio
    is a child of each link's netem, which has no rate and so never holds a
    queue. The prio classifies but never reorders, and a --video gate run
    here measures the sockets, threads and ports of the class split, not the
    qdisc (scripts/test_class_isolation.py shows audio overtaking a queued
    burst; only the Linode lab measures it under load)."""
    qdisc = (lab.load("settings.json") or {}).get("host_qdisc") or "none"
    if qdisc == "none":
        return "host qdisc: none"
    return (f"host qdisc: {qdisc} under each link's netem, which holds no queue: this bench "
            "does not exercise it (audio first in a NIC queue is measured on Linode only)")


def run_epoch(steps: dict, summary: dict | None) -> dict:
    """The epoch of the run: what the sender reports it sent, else the
    plan's."""
    if hops.sender_epoch_samples(summary):
        return hops.resolve_epoch(summary=summary, payload=steps["payload"])
    return hops.resolve_epoch(epoch_samples=steps["epoch_samples"], payload=steps["payload"])


def sender_digest(summary: dict | None) -> dict | None:
    """What the sender says about itself: what it sent and how late."""
    if not summary:
        return None
    keys = ("payload", "epoch_samples", "frame_count", "sample_rate", "epochs", "pace",
            "stopped_by_signal", "send_lateness_ms", "send_complete_lateness_ms", "encode_ms")
    return {key: summary[key] for key in keys if key in summary}


def sender_mismatch(steps: dict, summary: dict | None) -> str | None:
    """A sender that sent another epoch or payload than the plan's."""
    if not summary:
        return "the sender left no summary: the planned epoch is used"
    samples = hops.sender_epoch_samples(summary)
    if samples is not None and samples != steps["epoch_samples"]:
        return f"the sender sent {samples}-sample epochs, the plan was {steps['epoch_samples']}"
    if summary.get("payload") not in (None, steps["payload"]):
        return f"the sender sent {summary['payload']}, the plan was {steps['payload']}"
    return None


def measure(args, steps: dict, out: pathlib.Path) -> tuple[dict, list[dict], int, int, dict]:
    """Send, listen and collect the journals. Returns the nodes, their
    events, the window of the readers' steady state, and what the 4K run
    measured besides ({"nodes": the sampler's summary, "video_journal":
    (nodes, events) of the 4K stream})."""
    # The loop start is fixed now that the lab is up.
    start = loop_start(time.time())
    steps["loop_start_unix"] = start
    steps["hops"]["source_start_ns"] = start * 1_000_000_000
    steps["sender"]["command"] = sender_command(args, start, steps["media"]["loop"])
    sender_log = open(out / "sender.log", "ab")
    video = steps["video"]
    measured: dict = {}
    if video["source"] is not None:
        lab.start_sources(args.seconds + 120, video=True, audio=None,
                          clip=pathlib.Path(video["source"]["path"]))
    sender_host = sender_place(args)[0]
    sender = subprocess.Popen(lab.enter(sender_host, source=True) + steps["sender"]["command"], stdout=sender_log,
                              stderr=sender_log, stdin=subprocess.DEVNULL, start_new_session=True)
    sources = lab.load("sources.json")
    sources["bench-sender"] = sender.pid
    unread_senders = []
    for index, command in enumerate(unread_sender_commands(args, start, steps["media"]["loop"])):
        unread_log = open(out / f"sender-unread-{index}.log", "ab")
        unread = subprocess.Popen(lab.enter(sender_host, source=True) + command, stdout=unread_log,
                                  stderr=unread_log, stdin=subprocess.DEVNULL,
                                  start_new_session=True)
        unread_log.close()
        unread_senders.append(unread)
        sources[f"bench-sender-unread-{index}"] = unread.pid
    lab.save("sources.json", sources)
    readers, shaped, handles = [], [], [sender_log]
    per_packet = False
    sampler = None
    try:
        log(f"sender starts at {start} ({start - time.time():.1f} s)")
        time.sleep(max(0.0, start - time.time()))
        for site in sorted({item["site"] for item in steps["readers"]}):
            lab.wait_live(steps["hops"]["stream"], site, timeout=60)
        if video["viewers"]:
            for site in sorted({item["site"] for item in video["viewers"]}):
                lab.wait_live(VIDEO_STREAM, site, timeout=60)
        if steps["reader_netem"]:
            # netem then loses TCP packets, not 64 KB segments, as a real
            # link does (edge_lab.set_tcp_per_packet), for the HTTP readers.
            lab.set_tcp_per_packet(True)
            per_packet = True
            for host in sorted({item["host"] for item in
                                steps["readers"] + steps["udp_clients"] + video["viewers"]}):
                shaped.append(host)
                lab.set_net(host, "pub", "both", steps["reader_netem"])
        if video["sample_nodes"]:
            sampler = lab.Sampler(out / "nodes.ndjson").start()
        loads = load_commands(args, out)
        steps["load"] = loads
        if loads and not LOAD_BIN.is_file():
            raise SystemExit(f"{LOAD_BIN} is missing: build av-mesh's needletail-load")
        stats_before = edge_client_stats()
        began_us = time.time_ns() // 1000
        load_processes = []
        for item in loads:
            pathlib.Path(item["out"]).mkdir(parents=True, exist_ok=True)
            handle = open(pathlib.Path(item["out"]) / "load.log", "ab")
            handles.append(handle)
            load_processes.append(subprocess.Popen(
                client_prefix(args, item["host"], "load") + item["command"], stdout=handle,
                stderr=handle, stdin=subprocess.DEVNULL, start_new_session=True))
        readers += load_processes
        for item in steps["readers"]:
            handle = open(out / f"{item['host']}.{item['lane']}.log", "ab")
            handles.append(handle)
            readers.append(subprocess.Popen(client_prefix(args, item["host"], "client")
                                            + item["command"], stdout=handle,
                                            stderr=handle, stdin=subprocess.DEVNULL,
                                            start_new_session=True))
        for item in steps["udp_clients"] + video["viewers"]:
            # A UDP client's report is its standard output; ntcmp's go to files.
            log_path = pathlib.Path(item["report"]).with_suffix(".log")
            handle = open(log_path, "ab")
            stdout = open(item["report"], "wb") if item["kind"] == "udp" else handle
            handles += [handle, stdout]
            readers.append(subprocess.Popen(client_prefix(args, item["host"], "client")
                                            + item["command"], stdout=stdout,
                                            stderr=handle, stdin=subprocess.DEVNULL,
                                            start_new_session=True))
        log(f"{len(readers)} reader processes for {args.seconds} s")
        if loads or steps["readers"] or steps["udp_clients"] or video["viewers"]:
            # Every pass (integration 6): each client's CPU and run-queue
            # wait, its own UDP sockets' drops, and each client namespace's
            # UDP drops over the window. A 4K viewer that waits behind the
            # other clients measures the host, not the mesh: a site whose
            # clients dropped is unreadable for that pass.
            import threading
            named = ([("load", item["host"], proc) for item, proc in zip(loads, load_processes)]
                     + [(client_kind(item), item["host"], proc)
                        for item, proc in zip(steps["readers"] + steps["udp_clients"]
                                              + video["viewers"],
                                              readers[len(load_processes):])])
            hosts = sorted({host for _, host, _ in named})
            before = {host: namespace_udp(host) for host in hosts}
            first = {proc.pid: schedstat(proc.pid) for _, _, proc in named}

            def record_clients() -> None:
                time.sleep(max(1.0, args.seconds - 2))
                last = {proc.pid: schedstat(proc.pid) for _, _, proc in named}
                after = {host: namespace_udp(host) for host in hosts}
                rows = []
                for kind, host, proc in named:
                    a, b = first.get(proc.pid), last.get(proc.pid)
                    row = {"kind": kind, "host": host, "pid": proc.pid,
                           "socket_drops": socket_drops(proc.pid)}
                    if a and b:
                        row.update({"cpu_s": round((b[0] - a[0]) / 1e9, 3),
                                    "wait_s": round((b[1] - a[1]) / 1e9, 3)})
                    rows.append(row)
                drops = {host: {key: after[host].get(key, 0) - before[host].get(key, 0)
                                for key in ("RcvbufErrors", "InErrors", "InDatagrams")}
                         for host in hosts if before.get(host) and after.get(host)}
                (out / "client-cpu.json").write_text(json.dumps(
                    {"window_s": args.seconds - 2, "split": lab.rest_split(lab.load("cpus.json")),
                     "processes": rows, "namespace_udp": drops}, indent=1) + "\n")

            threading.Thread(target=record_clients, daemon=True).start()
        if args.stall_edge:
            import threading
            began = began_us / 1e6
            box = {}
            stall = threading.Thread(target=lambda: box.update(stall_edge(args.stall_edge, began)),
                                     daemon=True)
            stall.start()
            measured["stall"] = box
        if args.stall_node:
            import threading
            began = began_us / 1e6
            node_box = {}
            node_stall = threading.Thread(
                target=lambda: node_box.update(stall_nodes(args.stall_node, began)), daemon=True)
            node_stall.start()
            measured["stall_node"] = node_box
        if args.drain_node:
            import threading
            began = began_us / 1e6
            drain_box = {}
            drain = threading.Thread(target=lambda: drain_box.update(
                drain_node(args.drain_node, began, began + args.seconds + 5)), daemon=True)
            drain.start()
            measured["drain_node"] = drain_box
        if args.node_events:
            import threading
            began = began_us / 1e6
            event_box: list = []
            threading.Thread(target=lambda: event_box.extend(
                node_events(args.node_events, began)), daemon=True).start()
            measured["node_events"] = event_box
        forwarder_stop = None
        if args.sample_forwarders:
            import threading
            forwarder_stop, forwarder_rows = threading.Event(), []
            threading.Thread(target=forwarder_sampler, args=(forwarder_stop, forwarder_rows),
                             daemon=True).start()
            measured["forwarders"] = forwarder_rows
        gauge_stop = None
        if args.sample_gauges:
            import threading
            gauge_stop, gauge_rows = threading.Event(), []
            threading.Thread(target=gauge_sampler, args=(gauge_stop, gauge_rows),
                             daemon=True).start()
            measured["gauges"] = gauge_rows
        for process in readers:
            try:
                process.wait(timeout=args.seconds + 90)
            except subprocess.TimeoutExpired:
                process.kill()
        ended_us = time.time_ns() // 1000
        if gauge_stop is not None:
            gauge_stop.set()
        if forwarder_stop is not None:
            forwarder_stop.set()
        if args.drain_node:
            name = args.drain_node.partition(":")[0]
            lab.http_json(name, "/api/control/resume-node", "POST", {"node_id": name})
        since_us = began_us + skip_seconds(args) * 1_000_000
        measured["edge_stats"] = edge_stats_delta(stats_before, edge_client_stats())
        measured["load"] = load_report(loads)
        if sampler is not None:
            measured["nodes"] = sampler.stop()
            sampler = None
        log("collecting the hop journals")
        nodes, events = hops.collect_edge_lab(since_us, steps["hops"]["stream"])
        if video["viewers"]:
            measured["video_journal"] = hops.collect_edge_lab(since_us, VIDEO_STREAM)
    finally:
        if sampler is not None:
            sampler.stop()
        for host in shaped:
            lab.set_net(host, "pub", "both", None)
        if per_packet:
            lab.set_tcp_per_packet(False)
        for process in readers:
            if process.poll() is None:
                process.kill()
        stop_sender(sender)
        for unread in unread_senders:
            stop_sender(unread)
        for handle in handles:
            handle.close()
        sources = lab.load("sources.json")
        sources.pop("bench-sender", None)
        for index in range(len(unread_senders)):
            sources.pop(f"bench-sender-unread-{index}", None)
        lab.save("sources.json", sources)
    return nodes, events, since_us, ended_us, measured


def stop_sender(process: subprocess.Popen) -> None:
    """Stop the sender with SIGTERM, so that it prints its summary (the epoch
    it sent, its lateness), and kill it if it does not stop."""
    import os
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        process.wait(timeout=SENDER_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        os_kill_group(process.pid)


def os_kill_group(pid: int) -> None:
    import os
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_up and any(getattr(args, f"{role}_args") for role in
                          ("mesh", "backbone", "distributor", "edge", "contrib")) or (args.no_up and args.set):
        raise SystemExit("--no-up keeps the running lab's flags: drop the service arguments or --no-up")
    epoch_samples(args)
    if args.h2_window_ms <= 0:
        raise SystemExit("--h2-window-ms is more than 0")
    for window in reader_windows(args).values():
        if not 1 <= window <= MAX_WINDOW:
            raise SystemExit("a reader window is 1 to 400 parts")
    if not 1 <= args.video_window <= MAX_WINDOW:
        raise SystemExit("--video-window is 1 to 400 parts")
    if args.video_fps <= 0 or args.video_clip_seconds <= 0 or args.frame_deadline_ms <= 0:
        raise SystemExit("--video-fps, --video-clip-seconds and --frame-deadline-ms are more than 0")
    if any(count < 0 for count in video_viewers(args).values()):
        raise SystemExit("--video-readers and --video-udp-clients are 0 or more")
    if args.udp_clients < 0:
        raise SystemExit("--udp-clients is 0 or more")
    if sum(bool(item) for item in (args.reader_netem, args.client_loss, args.reader_loss)) > 1:
        raise SystemExit("give one of --client-loss, --reader-netem and --reader-loss")
    if args.client_loss:
        client_loss_spec(args.client_loss)
    steps = plan(args)
    source = steps["video"]["source"]
    if source and not source["synthetic"] and not source.get("referee") \
            and not pathlib.Path(source["path"]).is_file():
        raise SystemExit(f"no 4K clip at {source['path']} (clips in {CLIPS}: "
                         f"{', '.join(sorted(path.stem for path in CLIPS.glob('*.ts'))) or 'none'})")
    loop = pathlib.Path(steps["media"]["loop"])
    if not pathlib.Path(steps["media"]["from"]).is_file() and \
            not (loop.is_file() and loop.stat().st_size == LOOP_BYTES):
        # Refused before the lab comes up, not a traceback in every pass
        # (local proof 2: a gate whose every pass crashed on it).
        raise SystemExit(f"--pcm {steps['media']['from']} is missing: give --pcm or export "
                         "NEEDLETAIL_AUDIO_PCM (docs/lab-environment.md)")
    if args.dry_run:
        print(json.dumps(steps, indent=1))
        return 0
    guard.require_frozen(run_binaries(args, steps))
    # SIGTERM or SIGHUP (first-session's timeout, kill) unwinds the pass as
    # Ctrl-C does: its files and node logs still go into the archive.
    with archived_run.stopped_by_signals(), guard.hold(f"latency-bench.py --name {args.name}"):
        if steps["lab_up"] is None:
            # The lab runs already: what it runs is what its `up` took,
            # checked once this pass holds it.
            guard.require_frozen({}, running=guard.running_binaries())
        with PassRecord(args, steps) as record:
            return run(args, steps, record)


class PassRecord(archived_run.Run):
    """A pass in the run archive: NAME/ holds every file the pass wrote in
    its results directory and NAME/node-logs/ what each node's log gained,
    copied however the pass ends; its readers, UDP clients and load
    listeners are the run's listener steps."""

    def __init__(self, args, steps: dict):
        topology, regions = archived_run.local_topology(lab.NODES)
        super().__init__(args.run_id or archived_run.default_run_id("latency-bench"), "local-lab",
                         "latency-bench",
                         code=archived_run.code_paths(lab.MESH_ROOT, lab.CONTRIB_ROOT),
                         settings={key: value for key, value in vars(args).items()
                                   if key != "run_id"},
                         topology=topology, regions=regions)
        self.name = args.name
        self.steps = steps
        self.began_s = time.time()
        self.slices = archived_run.LogSlices(lab.WORK / "logs")
        self.slices.mark()
        self.logs_taken = False
        self.at_close(self.copy)

    def node_logs(self) -> None:
        """The nodes' logs of the pass, before the lab goes down."""
        self.slices.take(self, f"{self.name}/node-logs")
        self.logs_taken = True

    def copy(self) -> None:
        if not self.logs_taken:
            self.node_logs()
        self.add_tree(self.steps["out"], self.name, "raw",
                      f"latency-bench pass {self.name}: arrivals, probes, hops.raw.ndjson, "
                      "hops.json, summary.txt, nodes.ndjson (per-socket drops), load output, "
                      "sender.log, plan.json", {"t_us": "us", "latency_us": "us"},
                      since_s=self.began_s)
        lanes: dict[str, dict[str, int]] = {}
        for item in self.steps.get("readers") or []:
            by_site = lanes.setdefault(f"hls-{item['lane']}", {})
            by_site[item["site"]] = by_site.get(item["site"], 0) + self.steps["readers_per_kind"]
        for item in self.steps.get("udp_clients") or []:
            by_site = lanes.setdefault("udp", {})
            by_site[item["site"]] = by_site.get(item["site"], 0) + 1
        for item in self.steps.get("load") or []:
            by_site = lanes.setdefault("load", {})
            by_site[item["site"]] = by_site.get(item["site"], 0) \
                + item["per_edge"] * len(item["edges"])
        for lane, by_site in sorted(lanes.items()):
            self.record_step(self.name, lane, sum(by_site.values()), by_site)


def run_binaries(args, steps: dict) -> dict:
    """The binaries a pass takes from the environment: the lab's when it
    brings the lab up, the UDP client when a pass has UDP listeners, the
    load tool when it has a crowd."""
    out = guard.lab_binaries() if steps["lab_up"] is not None else {}
    if any(item["kind"] == "udp" for item in steps["video"]["viewers"] + steps["udp_clients"]):
        out["needletail-udp-client"] = pathlib.Path(args.udp_client)
    if args.load_listeners > 0 or args.load_ramp:
        out["needletail-load"] = LOAD_BIN
    return out


if __name__ == "__main__":
    sys.exit(main())
