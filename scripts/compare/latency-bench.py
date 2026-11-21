#!/usr/bin/env python3
"""Measure where the time of an audio part goes, on the local namespace lab.

One run: start the lab of scripts/edge_lab.py (tc netem links, a node has 2
CPUs and 4 GB, the contributor 4 and 8) with a given set of extra service
flags, so that each low-latency change can be switched off and on; send the
64-second loop with the AEP1 sender (aep1-48k-probe send, as
compare-audio.py does on lon-src) from the contributor's own host to its
ingest, as PCM, FLAC or Opus (--payload) in epochs of --epoch-samples (one
epoch is one part: 48 samples = 1 ms by default for PCM and FLAC, 120 =
2.5 ms for Opus); run N ntcmp HLS readers of each kind, over HTTP/2 with
5 ms of parts in flight (--h2-window-ms; one part at 5 ms, 5 at 1 ms, 20 at
0.25 ms) and over HTTP/3 with 40 parts in flight, on each reader host
(lon-readers and new-readers) for D seconds; then join the hop journals of every node with the readers'
records (hops.py), with the epoch that the sender reports. Linode is not
used. scripts/compare/part-sweep.py runs this for each part size.

Results: target/latency-bench/NAME/
  hops.json      hops.py's report, with the run's settings under "bench"
  summary.txt    the hop table of each region, and each reader's latency
  hops.raw.ndjson, HOST.LANE.probe*.csv|json, sender.log, plan.json

Flags reach the services through the lab's settings:
  --mesh-args "..."          every av-mesh node (backbone, distributor, edge)
  --backbone-args/--distributor-args/--edge-args "..."   one role
  --contrib-args "..."       av-contrib
  --sender-args "..."        aep1-48k-probe send (e.g. "--pace tokio")
  --set key=value            any setting of linode-small-mesh-deploy.py
Write a single flag with `=`: --mesh-args=--some-flag (argparse reads a
lone "--some-flag" as an option of its own).
e.g. the A/B of the event-driven cache-mesh sender:
  latency-bench.py --name p1-on
  latency-bench.py --name p1-off --mesh-args "--mesh-sync-on-publish false"

The journals are read only after the readers finish, so every service is
started with --hop-journal-capacity sized to the run and its part rate
(--journal-capacity overrides); summary.txt warns when a node's ring was
full or its journal starts late in the window.

--dry-run prints the plan (every command) and touches nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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
import edge_lab as lab  # noqa: E402

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
OPUS_EPOCH_SAMPLES = (120, 240, 480, 960)
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


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--name", required=True, help="results go to target/latency-bench/NAME")
    parser.add_argument("--seconds", type=int, default=60, help="D: how long the readers listen")
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
                             "opus 120 = 2.5 ms; pcm >= 12, flac >= 32, opus 120/240/480/960)")
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
    parser.add_argument("--no-up", action="store_true",
                        help="use the lab that is running (its flags are not changed)")
    parser.add_argument("--keep-up", action="store_true", help="leave the lab running afterwards")
    parser.add_argument("--ntcmp", default=str(ROOT / "target" / "ntcmp"))
    parser.add_argument("--pcm", default=str(lab.AUDIO_PCM),
                        help="stereo s24le PCM; its first 64 s are the loop")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; run nothing")
    return parser


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
    rate = JOURNAL_EVENTS_PER_PART * parts_per_second(epoch_samples(args))
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
    settings: dict[str, object] = {"daw_hls_formats": args.payload}
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


def sender_command(args, start: int, loop_file: str) -> list[str]:
    """The AEP1 sender. It sends epoch k when its last sample has been
    spoken: the session id is the loop start plus one epoch."""
    samples = epoch_samples(args)
    session_id = start * 1_000_000_000 + epoch_ns(samples)
    return [lab.binary("aep1-48k-probe"), "send", "--target", "127.0.0.1:27100",
            "--session-id", str(session_id), "--group-id", "0",
            # Long enough for the lab to go live first; it is stopped at the end.
            "--duration-seconds", str(args.seconds + max(0, start - int(time.time())) + 300),
            "--payload", args.payload, "--epoch-samples", str(samples),
            "--channels", "2", "--group-channels", "8",
            "--pcm-s24le-file", loop_file, "--repair-percent", "12", "--min-repair-symbols", "1",
            *split(args.sender_args)]


def reader_commands(args, out: pathlib.Path, index: str) -> list[dict]:
    """ntcmp hls readers: --h2-window-ms of parts in flight over HTTP/2 and
    --h3-window parts over HTTP/3 on each reader host, through the first
    distributor of the host's site."""
    readers = []
    stream = PAYLOAD_STREAM[args.payload]
    for host in [item for item in args.hosts.split(",") if item]:
        site = lab.node(host)["site"]
        entry = lab.distributors(site)[0]
        windows = reader_windows(args)
        for lane, window, http3 in (("h2", windows["h2"], False), ("h3", windows["h3"], True)):
            stem = out / f"{host}.{lane}"
            command = [args.ntcmp, "hls", "--lane", f"nt-{args.payload}-{lane}", "--mode", "chase",
                       "--window", str(window), *(["--http3"] if http3 else []),
                       "--url", f"{lab.base_url(entry)}/live/{stream}/master.m3u8"
                                f"?sid=bench-{args.name}-{host}-{lane}-{{i}}",
                       "--ca", lab.ca_file(), "--server-name", lab.SERVER_NAME,
                       "--audio-index", index, "--duration", str(args.seconds),
                       "--skip-seconds", str(SKIP_SECONDS), "--label", f"{args.name}-{host}-{lane}",
                       "--count", str(args.readers),
                       *(["--spread-ms", str(min(10_000, 400 * args.readers))] if args.readers > 1 else []),
                       "--report", f"{stem}.probe.json", "--frames", f"{stem}.probe.csv"]
            readers.append({"host": host, "site": site, "lane": lane, "window": window,
                            "entry": entry, "command": command})
    return readers


def reader_netem(args) -> str | None:
    if args.reader_netem:
        return args.reader_netem
    if args.reader_loss:
        return lab.netem_spec(loss=args.reader_loss)
    return None


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


def plan(args) -> dict:
    out = OUT_ROOT / args.name
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
        "sender": {"namespace": "contrib-lon", "command": sender_command(args, start, loop_file)},
        "readers": reader_commands(args, out, index),
        "reader_netem": reader_netem(args),
        "hops": {"stream": PAYLOAD_STREAM[args.payload], "source_start_ns": start * 1_000_000_000,
                 "skip_seconds": SKIP_SECONDS,
                 "journal_capacity": None if args.no_up else journal_capacity(args)},
    }


def prepare_media(item: dict) -> None:
    loop = pathlib.Path(item["loop"])
    if not loop.is_file() or loop.stat().st_size != LOOP_BYTES:
        MEDIA.mkdir(parents=True, exist_ok=True)
        with open(item["from"], "rb") as source:
            data = source.read(LOOP_BYTES)
        if len(data) != LOOP_BYTES:
            raise SystemExit(f"{item['from']} is shorter than {LOOP_SECONDS} s of stereo s24le")
        loop.write_bytes(data)
    if not pathlib.Path(item["index"]).is_file():
        subprocess.run(item["index_command"], check=True, capture_output=True)


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


def run(args, steps: dict) -> int:
    out = pathlib.Path(steps["out"])
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.probe*"):
        old.unlink()
    # The sender's summary of this run is read from its log.
    (out / "sender.log").unlink(missing_ok=True)
    (out / "plan.json").write_text(json.dumps(steps, indent=1) + "\n")
    if not pathlib.Path(args.ntcmp).is_file():
        raise SystemExit(f"{args.ntcmp} is missing: build it with scripts/compare/gobuild.sh")
    prepare_media(steps["media"])
    if steps["lab_up"] is None and not lab.load("services.json"):
        raise SystemExit("--no-up: the lab is not running")
    try:
        if steps["lab_up"] is not None:
            log(f"lab up: {' '.join(steps['lab_up'])}")
            lab.up(steps["lab_up"])
        nodes, events, since_us, ended_us = measure(args, steps, out)
    finally:
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
    result["bench"] = {**{key: value for key, value in steps.items() if key != "readers"},
                       "reader_commands": [item["command"] for item in steps["readers"]],
                       "readers": summaries, "window_us": [since_us, ended_us],
                       "journal_full": full, "sender": sender_digest(summary),
                       "epoch_note": epoch_note}
    (out / "hops.json").write_text(json.dumps(result, indent=1) + "\n")
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
             f"hop journal: {capacity if capacity is not None else 'as running'} events per node"
             + (f"; WARNING: full on {', '.join(full)}: the start of the window was overwritten"
                if full else ""), ""]
    for item in summaries:
        latency = item.get("latency_ms") or {}
        lines.append(f"{item['report']:<36} units {item.get('units')} gaps {item.get('gaps')} "
                     f"latency p50 {latency.get('p50')} p95 {latency.get('p95')} "
                     f"p99 {latency.get('p99')} max {latency.get('max')}"
                     + (f" error {item['error']}" if item.get("error") else ""))
    text = "\n".join(lines) + "\n\n" + hops.format_summary(result)
    (out / "summary.txt").write_text(text)
    print(text, end="")
    return 0


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


def measure(args, steps: dict, out: pathlib.Path) -> tuple[dict, list[dict], int, int]:
    """Send, listen and collect the journals. Returns the nodes, their
    events and the window of the readers' steady state."""
    # The loop start is fixed now that the lab is up.
    start = loop_start(time.time())
    steps["loop_start_unix"] = start
    steps["hops"]["source_start_ns"] = start * 1_000_000_000
    steps["sender"]["command"] = sender_command(args, start, steps["media"]["loop"])
    sender_log = open(out / "sender.log", "ab")
    if args.video:
        lab.start_sources(args.seconds + 120, video=True, audio=None)
    sender = subprocess.Popen(lab.enter("contrib-lon") + steps["sender"]["command"], stdout=sender_log,
                              stderr=sender_log, stdin=subprocess.DEVNULL, start_new_session=True)
    sources = lab.load("sources.json")
    sources["bench-sender"] = sender.pid
    lab.save("sources.json", sources)
    readers, shaped = [], []
    try:
        log(f"sender starts at {start} ({start - time.time():.1f} s)")
        time.sleep(max(0.0, start - time.time()))
        for site in sorted({item["site"] for item in steps["readers"]}):
            lab.wait_live(steps["hops"]["stream"], site, timeout=60)
        if steps["reader_netem"]:
            for host in sorted({item["host"] for item in steps["readers"]}):
                shaped.append(host)
                lab.set_net(host, "pub", "both", steps["reader_netem"])
        began_us = time.time_ns() // 1000
        for item in steps["readers"]:
            handle = open(out / f"{item['host']}.{item['lane']}.log", "ab")
            readers.append(subprocess.Popen(lab.enter(item["host"]) + item["command"], stdout=handle,
                                            stderr=handle, stdin=subprocess.DEVNULL,
                                            start_new_session=True))
        log(f"{len(readers)} reader processes for {args.seconds} s")
        for process in readers:
            try:
                process.wait(timeout=args.seconds + 90)
            except subprocess.TimeoutExpired:
                process.kill()
        ended_us = time.time_ns() // 1000
        since_us = began_us + SKIP_SECONDS * 1_000_000
        log("collecting the hop journals")
        nodes, events = hops.collect_edge_lab(since_us, steps["hops"]["stream"])
    finally:
        for host in shaped:
            lab.set_net(host, "pub", "both", None)
        for process in readers:
            if process.poll() is None:
                process.kill()
        stop_sender(sender)
        sources = lab.load("sources.json")
        sources.pop("bench-sender", None)
        lab.save("sources.json", sources)
    return nodes, events, since_us, ended_us


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
    steps = plan(args)
    if args.dry_run:
        print(json.dumps(steps, indent=1))
        return 0
    return run(args, steps)


if __name__ == "__main__":
    sys.exit(main())
