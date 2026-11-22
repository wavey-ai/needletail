#!/usr/bin/env python3
"""Turn the records of audio comparison sessions into one evidence file.

  compare-audio-analyse.py --out evidence.json SESSION [SESSION ...]

Definitions, the same for every system:

unit latency       arrival of an audio unit (an Opus packet, a block of PCM, a
                   Needletail part) at the listener, minus the moment its last
                   sample was spoken at the source
mouth to ear       the age of a sample when a listener that plays every unit
                   on arrival would play it: the unit latency plus the time
                   the sample waited for the rest of its unit. Taken over
                   every sample, so a longer frame costs half its length at
                   the median and its whole length at the top
Chrome mouth to ear  measured, not derived: the 3 kHz marker that starts at
                   every whole second of the source, timed at the output of
                   Chrome's audio pipeline (jitter buffer and decoder included)
gap                consecutive units that never arrived
bit-exact          the unit at the listener is byte for byte the unit sent

Needletail's Opus and FLAC come from its DAW source, which hands audio to the
plug-in in host buffers, as a DAW does: 64 samples since 22 November 2026 (a
sample waits up to 1.3 ms), 512 before (up to 10.7 ms). The step's meta
(nt_daw) names the buffer and the source's epochs; a step without it is from
before, with 240-sample epochs and 512-sample buffers. The other systems are
fed without such a buffer. Both figures are given: `raw` as measured, and
`without_daw_buffer`, in which the known wait of each unit is taken off.

The first five seconds of each listener and the last three of a step are
left out.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
RUNS = ROOT / "target" / "compare" / "runs"
HEAD_MS = 5000.0
TAIL_MS = 3000.0
LOOP_SAMPLES = 64 * 48000
# The DAW source of the runs before 22 November 2026, whose steps have no
# nt_daw in their meta: 512-sample host buffers, 240-sample epochs.
DAW_BUFFER = 512
DAW_EPOCH_SAMPLES = 240
DAW_SAMPLES_KEY = {"nt-opus": "opus_frame_samples", "nt-flac": "lossless_epoch_samples"}
FRAME_MS = {"opus-2.5": 2.5, "opus-5": 5.0, "opus-10": 10.0, "opus-20": 20.0, "opus-max510": 10.0,
            "pcm": 2.5}
# A Needletail part is one producer frame; compare-audio.py records the frame
# of each lane in the step's meta (nt_frame_ms). Runs before that were 5 ms.
NT_FRAME_MS = 5.0
SITES = {"lon": "London", "new": "New York", "tok": "Tokyo", "syd": "Sydney"}


def dist(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    pick = lambda q: round(ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))], 2)
    return {"n": len(ordered), "min": round(ordered[0], 2), "p50": pick(0.5), "p95": pick(0.95),
            "p99": pick(0.99), "max": round(ordered[-1], 2)}


def load(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def nt_frame_ms(meta: dict, lane: str, default_ms: float | None = None) -> float:
    """The frame of a Needletail lane (`nt-pcm-h3` -> its base `nt-pcm`). A
    DAW source lane of a step without nt_daw is the first runs' 240-sample
    epoch, whatever nt_frame_ms says (some steps recorded 2.5 ms for it). A
    lane the step's meta does not name has `default_ms` (--nt-frame-ms,
    NT_FRAME_MS unless given)."""
    base = "-".join(lane.split("-")[:2])
    if base in DAW_SAMPLES_KEY and not meta.get("nt_daw"):
        return DAW_EPOCH_SAMPLES / 48.0
    fallback = NT_FRAME_MS if default_ms is None else default_ms
    return float((meta.get("nt_frame_ms") or {}).get(base) or fallback)


def source_to_publish_ms(csv_path: pathlib.Path) -> list[float]:
    """Last sample spoken -> the contributor published the part, for every
    recognised part of an HLS probe record (complete - latency is the spoken
    time; published_unix_us is the part's own header)."""
    values = []
    if not csv_path.is_file():
        return values
    with csv_path.open() as handle:
        for row in csv.DictReader(handle):
            published = (row.get("published_unix_us") or "").strip()
            if row.get("kind") != "a" or int(row["index"]) < 0 or not published or int(published) <= 0:
                continue
            spoken = int(row["complete_unix_us"]) - int(row["latency_us"])
            values.append((int(published) - spoken) / 1000.0)
    return values


def analyse_udp(record: dict, host_dir: pathlib.Path, meta: dict) -> dict:
    """needletail-udp-client's report. It times each part from publication
    to arrival; the HLS probe of the same stream on the same host gives the
    source -> publish part, and their medians add up to an estimate of the
    UDP lane's latency from the source."""
    keys = ("received_parts", "gap_parts", "missing_parts", "recovered_parts", "duplicate_parts",
            "datagram_loss", "final_repair_permille", "final_min_repair", "time_to_first_part_ms",
            "publish_to_arrival_ms", "available_to_arrival_ms", "error")
    out = {key: record[key] for key in keys if key in record}
    bases = sorted({"-".join(lane.split("-")[:2]) for lane in meta.get("lanes") or []
                    if lane.startswith("nt-") and lane != "nt-udp"})
    if bases:
        spoken = dist(source_to_publish_ms(host_dir / f"{bases[0]}.probe.csv"))
        out["source_to_publish_ms"] = spoken
        arrival = record.get("publish_to_arrival_ms") or {}
        if spoken and isinstance(arrival.get("p50"), (int, float)):
            out["estimated_unit_latency_p50_ms"] = round(spoken["p50"] + arrival["p50"], 2)
            out["note"] = ("estimated_unit_latency_p50_ms is the sum of the medians of source -> "
                           f"publish ({bases[0]} HLS probe) and publish -> arrival (UDP)")
    return out


def daw_source(meta: dict, lane: str) -> tuple[int, int]:
    """(epoch samples, host buffer samples) of a DAW source lane
    (`nt-flac-h3` -> nt-flac) in a step: from its meta, else the first runs'."""
    base = "-".join(lane.split("-")[:2])
    daw = meta.get("nt_daw") or {}
    epoch = daw.get(DAW_SAMPLES_KEY.get(base, ""))
    buffer = daw.get("buffer_frames")
    if not epoch or not buffer:
        return DAW_EPOCH_SAMPLES, DAW_BUFFER
    return int(epoch), int(buffer)


def daw_wait_ms(index: int, epoch: int = DAW_EPOCH_SAMPLES, buffer: int = DAW_BUFFER) -> float:
    """Time between the last sample of Needletail epoch `index` and the
    delivery of the DAW host buffer that contains it. The source's audio is
    one buffer behind the loop (it plays one buffer of silence first)."""
    end = (epoch * (index + 1) - buffer) % LOOP_SAMPLES     # one past its last content sample
    if end == 0:
        end = LOOP_SAMPLES
    buffer_end = ((end - 1) // buffer + 1) * buffer
    return (buffer_end - end) / 48.0


def mouth_to_ear(latencies: list[float], frame_ms: float) -> list[float]:
    """Age of the samples of each unit at its arrival: four points across the unit."""
    return [value + frame_ms * (k + 0.5) / 4 for value in latencies for k in range(4)]


def analyse_probe(summary: dict, csv_path: pathlib.Path, lane: str, frame_ms: float,
                  end_ms: float | None, daw: tuple[int, int] = (DAW_EPOCH_SAMPLES, DAW_BUFFER)) -> dict:
    out = {
        "error": (summary.get("error") or None),
        "connect_ms": round(summary.get("connect_ms") or -1, 1),
        "first_audio_ms": round(summary.get("first_audio_ms") or -1, 1),
        "frame_ms": frame_ms, "units": summary.get("audio_packets"),
        "units_not_bit_exact": summary.get("audio_packets_unrecognised"),
        "sequence_gaps": summary.get("audio_sequence_gaps"),
        "duplicates": summary.get("audio_duplicates"), "late_units": summary.get("audio_late_packets"),
        "rfc3550_jitter_ms": summary.get("audio_rfc3550_jitter_ms"),
    }
    detail = summary.get("detail") or {}
    for key in ("livekit_server", "codec_audio", "counts"):
        if detail.get(key):
            out[key] = detail[key]
    if not csv_path.is_file():
        # A fan-out listener: the summary of the probe, after its own five-second skip.
        latency = summary.get("audio_latency_ms") or {}
        if latency:
            out["unit_latency_ms"] = {key: latency.get(key) for key in ("p50", "p95", "p99", "max", "min")}
            out["unit_latency_ms"]["n"] = latency.get("count")
        return out
    rows = []
    with csv_path.open() as handle:
        for row in csv.DictReader(handle):
            if row["kind"] == "a":
                rows.append((int(row["index"]), int(row["latency_us"]) / 1000.0,
                             int(row["complete_unix_us"]) / 1000.0, int(row["first_unix_us"]),
                             int(row["bytes"])))
    if len(rows) < 50:
        return out
    lo = rows[0][2] + HEAD_MS
    hi = (end_ms if end_ms else rows[-1][2]) - TAIL_MS
    window = [row for row in rows if lo <= row[2] <= hi]
    if len(window) < 50:
        return out
    known = [row for row in window if row[0] >= 0]
    out["window_s"] = round((hi - lo) / 1000.0, 1)
    out["units_in_window"] = len(window)
    out["bit_exact_percent"] = round(100.0 * len(known) / len(window), 4)
    out["kbit_per_second"] = round(sum(row[4] for row in window) * 8 / (hi - lo), 1)
    numbers = sorted(row[3] for row in window)
    expected = numbers[-1] - numbers[0] + 1
    missing = expected - len(set(numbers))
    gaps, longest, previous = 0, 0, None
    for number in sorted(set(numbers)):
        if previous is not None and number > previous + 1:
            gaps += 1
            longest = max(longest, number - previous - 1)
        previous = number
    out.update({"units_expected": expected, "units_missing": missing,
                "missing_percent": round(100.0 * missing / expected, 4),
                "gaps": gaps, "longest_gap_ms": round(longest * frame_ms, 1),
                "gap_ms_per_minute": round(missing * frame_ms * 60000.0 / (hi - lo), 1)})
    latencies = [row[1] for row in known]
    if not latencies:
        return out
    arrivals = sorted(row[2] for row in window)
    spacing = [b - a for a, b in zip(arrivals, arrivals[1:])]

    def figures(values: list[float]) -> dict:
        unit = dist(values)
        return {"unit_latency_ms": unit, "mouth_to_ear_ms": dist(mouth_to_ear(values, frame_ms)),
                "jitter_p99_minus_p50_ms": round(unit["p99"] - unit["p50"], 2),
                "jitter_max_minus_p50_ms": round(unit["max"] - unit["p50"], 2)}

    out.update(figures(latencies))
    out["longest_silence_between_arrivals_ms"] = round(max(spacing), 1) if spacing else None
    if lane.startswith(("nt-opus", "nt-flac")):
        out["raw"] = figures(latencies)
        out["daw_buffer_wait_ms"] = dist([daw_wait_ms(row[0], *daw) for row in known])
        out["daw_epoch_and_buffer_samples"] = list(daw)
        out.update(figures([row[1] - daw_wait_ms(row[0], *daw) for row in known]))
        out["note"] = "top-level figures are without_daw_buffer; raw has the measured values"
    return out


def analyse_player(record: dict, base_ms: float | None, end_ms: float | None) -> dict:
    out = {"error": (record.get("error") or "")[:200] or None, "chrome": record.get("chrome")}
    stats = record.get("stats") or []
    final = {}
    for sample in reversed(stats):
        if sample.get("audio"):
            final = sample
            break
    audio = final.get("audio") or {}
    if audio:
        received = audio.get("totalSamplesReceived") or 0
        out.update({
            "codec": final.get("audioCodec"),
            "jitter_buffer_ms": round(1000.0 * audio.get("jitterBufferDelay", 0)
                                      / max(1, audio.get("jitterBufferEmittedCount", 1)), 1),
            "jitter_buffer_target_ms": round(1000.0 * audio.get("jitterBufferTargetDelay", 0)
                                             / max(1, audio.get("jitterBufferEmittedCount", 1)), 1),
            "rfc3550_jitter_ms": round(1000.0 * audio.get("jitter", 0), 2),
            "packets_received": audio.get("packetsReceived"), "packets_lost": audio.get("packetsLost"),
            "packets_discarded": audio.get("packetsDiscarded"),
            "concealed_samples": audio.get("concealedSamples"),
            "concealed_percent": round(100.0 * (audio.get("concealedSamples") or 0) / received, 3)
            if received else None,
            "concealment_events": audio.get("concealmentEvents"),
            "silent_concealed_samples": audio.get("silentConcealedSamples"),
            "inserted_samples": audio.get("insertedSamplesForDeceleration"),
            "removed_samples": audio.get("removedSamplesForAcceleration"),
            "seconds_received": round(received / 48000.0, 1),
        })
    markers = record.get("audio_markers") or []
    if markers:
        first = markers[0][0]
        hi = (end_ms if end_ms else markers[-1][0])
        kept = [m for m in markers if m[0] >= record.get("started_unix_ms", first) + 3000 and m[0] <= hi]
        base = base_ms if base_ms is not None else 0.0
        values = []
        for wall, _length in kept:
            fraction = wall % 1000.0
            # The marker starts on a whole second, so its latency is known
            # modulo one second; the probe on the same host picks the second.
            turns = round((base + 60.0 - fraction) / 1000.0)
            values.append(fraction + 1000.0 * max(0, turns))
        # Music can look like a marker for a few milliseconds. The markers
        # agree with each other; a detection far from their median is dropped.
        if values:
            middle = statistics.median(values)
            values = [value for value in values if abs(value - middle) <= 60.0]
        out["marker_mouth_to_ear_ms"] = dist(values)
        out["markers"] = len(values)
    return out


ANALYSIS_VERSION = 4


def analyse_step(path: pathlib.Path) -> dict | None:
    """The analysis of a step is kept beside its records and reused."""
    cache = path / "analysis.json"
    if cache.is_file():
        kept = load(cache)
        if kept and kept.get("analysis_version") == ANALYSIS_VERSION:
            return kept
    step = analyse_step_now(path)
    if step is not None:
        step["analysis_version"] = ANALYSIS_VERSION
        cache.write_text(json.dumps(step) + "\n")
    return step


def analyse_step_now(path: pathlib.Path) -> dict | None:
    meta = load(path / "meta.json")
    if meta is None or "config" not in meta:
        return None
    config = meta["config"]
    step = {key: meta.get(key) for key in ("step", "config", "phase", "detail", "netem", "lanes",
                                            "listeners", "started_unix", "seconds")}
    source = meta.get("source") or {}
    step["source"] = {name.replace(".status", ""): {
        key: value.get(key) for key in ("audio_packets", "audio_worst_late_ms", "write_errors")}
        for name, value in source.items() if name != "logs" and isinstance(value, dict)}
    if meta.get("host_errors"):
        step["host_errors"] = {name: text[-160:] for name, text in meta["host_errors"].items()}
    hosts = {}
    for host_dir in sorted(p for p in path.iterdir() if p.is_dir()):
        records = {file.name[:-5]: load(file) for file in sorted(host_dir.glob("*.json"))}
        ends = [r["started_unix_ms"] + r["duration_seconds"] * 1000.0 for name, r in records.items()
                if r and name.endswith(".probe") and "duration_seconds" in r]
        end_ms = min(ends) if ends else None
        lanes: dict = {}
        many: dict = {}
        for name, record in records.items():
            if record is None or ".probe" not in name:
                continue
            lane = name.split(".probe")[0]
            frame = nt_frame_ms(meta, lane) if lane.startswith("nt-") else FRAME_MS[config]
            analysed = analyse_probe(record, host_dir / f"{lane}.probe.csv", lane, frame, end_ms,
                                     daw_source(meta, lane))
            if name.endswith(".probe"):
                lanes.setdefault(lane, {})["probe"] = analysed
            else:
                many.setdefault(lane, []).append(analysed)
        for lane, probes in many.items():
            good = [p for p in probes if not p.get("error") and (p.get("units") or 0) > 100
                    and p.get("unit_latency_ms")]
            summary = {"listeners": len(probes), "listeners_with_audio": len(good)}
            if good:
                units = sum(p["units"] for p in good)
                summary.update({
                    "unit_latency_p50_ms_median_listener": round(statistics.median(
                        p["unit_latency_ms"]["p50"] for p in good), 2),
                    "unit_latency_p50_ms_worst_listener": max(p["unit_latency_ms"]["p50"] for p in good),
                    "unit_latency_p99_ms_median_listener": round(statistics.median(
                        p["unit_latency_ms"]["p99"] for p in good), 2),
                    "unit_latency_p99_ms_worst_listener": max(p["unit_latency_ms"]["p99"] for p in good),
                    "units": units,
                    "missing_percent": round(100.0 * sum(p.get("sequence_gaps") or 0 for p in good)
                                             / max(1, units), 4),
                    "not_bit_exact": sum(p.get("units_not_bit_exact") or 0 for p in good),
                    "first_audio_ms_median": round(statistics.median(p["first_audio_ms"] for p in good), 1),
                })
            errors = sorted({(p.get("error") or "")[:120] for p in probes if p.get("error")})
            if errors:
                summary["errors"] = errors[:4]
            lanes.setdefault(lane, {})["listeners"] = summary
        for name, record in records.items():
            if record is not None and name.endswith(".udp") and isinstance(record, dict):
                lanes.setdefault(name[:-4], {})["udp"] = analyse_udp(record, host_dir, meta)
        for name, record in records.items():
            if record is None or not name.endswith(".player"):
                continue
            lane = name[:-7]
            probe = (lanes.get(lane) or {}).get("probe") or {}
            base = ((probe.get("unit_latency_ms") or {}).get("p50"))
            lanes.setdefault(lane, {})["chrome"] = analyse_player(record, base, None)
        netem = host_dir / "netem.log"
        host = {"lanes": lanes}
        if netem.is_file():
            host["netem"] = [line.strip() for line in netem.read_text().splitlines()
                             if "netem" in line or "Sent" in line][:6]
        hosts[host_dir.name] = host
    step["hosts"] = hosts
    return step


def load_samplers(directory: pathlib.Path) -> dict:
    """{node: [(unix, busy jiffies, total jiffies, steal jiffies, {process: (jiffies, rss kB)})]}"""
    out = {}
    for path in sorted((directory / "samplers").glob("*.log")):
        rows = []
        for line in path.read_text().splitlines():
            parts = line.split(" | ")
            head = parts[0].split()
            if len(head) < 10 or head[1] != "cpu":
                continue
            fields = list(map(int, head[2:10]))
            total = sum(fields)
            processes = {}
            for item in parts[1:]:
                bits = item.split()
                if len(bits) >= 4:
                    name = bits[0]
                    used, rss = processes.get(name, (0, 0))
                    processes[name] = (used + int(bits[2]), rss + int(bits[3]))
            rows.append((int(head[0]), total - fields[3] - fields[4], total, fields[7], processes))
        if rows:
            out[path.stem] = rows
    return out


def node_usage(samplers: dict, start: float, end: float) -> dict:
    """Processor use of the sampled services between two times, in percent of one vCPU."""
    usage = {}
    for name, rows in samplers.items():
        window = [row for row in rows if start <= row[0] <= end]
        if len(window) < 2:
            continue
        first, last = window[0], window[-1]
        seconds = last[0] - first[0]
        total = max(1, last[2] - first[2])
        entry = {"node_busy_percent": round(100.0 * (last[1] - first[1]) / total, 1),
                 "steal_percent": round(100.0 * (last[3] - first[3]) / total, 2)}
        for process, (jiffies, rss) in last[4].items():
            if process in first[4]:
                entry[process + "_cpu_percent"] = round((jiffies - first[4][process][0]) / seconds, 1)
                entry[process + "_rss_mb"] = round(rss / 1024.0)
        usage[name] = entry
    return usage


PROBE_KEEP = ("error", "frame_ms", "first_audio_ms", "units", "units_in_window", "units_missing", "missing_percent",
              "gaps", "longest_gap_ms", "bit_exact_percent", "kbit_per_second", "jitter_p99_minus_p50_ms",
              "rfc3550_jitter_ms", "longest_silence_between_arrivals_ms", "livekit_server")
CHROME_KEEP = ("error", "codec", "jitter_buffer_ms", "rfc3550_jitter_ms", "packets_lost", "concealed_percent",
               "concealment_events", "inserted_samples", "removed_samples", "seconds_received", "markers")


def short(distribution: dict | None, keys=("p50", "p95", "p99", "max")) -> dict | None:
    if not distribution:
        return None
    return {key: distribution.get(key) for key in keys if distribution.get(key) is not None}


def slim(step: dict) -> dict:
    """The versioned form of a step: the figures that the tables and the
    text use. Single probes are kept for the Nanode listener of each region;
    the viewer host keeps Chrome and the crowds."""
    out = {key: value for key, value in step.items() if key not in ("hosts", "source", "analysis_version")}
    hosts = {}
    for name, host in step["hosts"].items():
        lanes = {}
        for lane, entry in host["lanes"].items():
            kept = {}
            probe = entry.get("probe")
            if probe and (name.endswith("-nano") or step["phase"].startswith("fanout")):
                item = {key: probe[key] for key in PROBE_KEEP if probe.get(key) is not None}
                item["unit_latency_ms"] = short(probe.get("unit_latency_ms"))
                item["mouth_to_ear_ms"] = short(probe.get("mouth_to_ear_ms"), ("p50", "p95", "p99"))
                if probe.get("raw"):
                    item["raw"] = {"unit_latency_ms": short(probe["raw"].get("unit_latency_ms")),
                                   "mouth_to_ear_ms": short(probe["raw"].get("mouth_to_ear_ms"),
                                                            ("p50", "p95", "p99"))}
                    item["daw_buffer_wait_ms"] = short(probe.get("daw_buffer_wait_ms"), ("p50", "max"))
                kept["probe"] = item
            if entry.get("listeners"):
                kept["listeners"] = entry["listeners"]
            chrome = entry.get("chrome")
            if chrome:
                item = {key: chrome[key] for key in CHROME_KEEP if chrome.get(key) is not None}
                item["marker_mouth_to_ear_ms"] = short(chrome.get("marker_mouth_to_ear_ms"),
                                                       ("min", "p50", "max"))
                kept["chrome"] = item
            if kept:
                lanes[lane] = kept
        if lanes:
            hosts[name] = {"lanes": lanes}
    out["hosts"] = hosts
    return out


SCRUB = (
    (re.compile(r"[a-z0-9-]+\.livekit\.cloud"), "<project>.livekit.cloud"),
    (re.compile(r"customer-[a-z0-9]+\.cloudflarestream\.com"), "customer-<code>.cloudflarestream.com"),
    (re.compile(r"\b[0-9a-f]{32,64}\b"), "<id>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "<token>"),
)


def scrub(value):
    """An error message can quote a URL. Identifiers of the accounts (the
    Cloudflare app, a live input, the LiveKit Cloud project) and tokens are
    taken out of every string before it is written."""
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        for pattern, replacement in SCRUB:
            value = pattern.sub(replacement, value)
    return value


def main() -> None:
    global NT_FRAME_MS
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sessions", nargs="+")
    parser.add_argument("--slim", action="store_true",
                        help="the versioned form: only the figures that the tables use")
    parser.add_argument("--out", required=True)
    parser.add_argument("--runs", default=str(RUNS))
    parser.add_argument("--nt-frame-ms", type=float, default=NT_FRAME_MS,
                        help="the frame (one part) of a Needletail lane whose step meta does not "
                             "record it (nt_frame_ms); 5 ms, the producer's frame of those runs")
    args = parser.parse_args()
    NT_FRAME_MS = args.nt_frame_ms
    evidence = {"schema": "needletail.compare-audio.v1", "head_ms_left_out": HEAD_MS,
                "tail_ms_left_out": TAIL_MS, "daw_buffer_samples": DAW_BUFFER,
                "daw_buffer_note": "daw_buffer_samples is that of steps without nt_daw in their "
                                   "meta; each DAW lane names its own in "
                                   "daw_epoch_and_buffer_samples", "sessions": {}}
    for name in args.sessions:
        directory = pathlib.Path(args.runs) / name
        if not directory.is_dir():
            sys.exit(f"no such session: {directory}")
        steps = {}
        samplers = load_samplers(directory)
        for path in sorted(p for p in directory.iterdir() if p.is_dir() and p.name.startswith("audio.")):
            step = analyse_step(path)
            if step:
                step = dict(step)
                if samplers and step.get("started_unix"):
                    # The listeners of a step start a few seconds after it does.
                    step["nodes"] = node_usage(samplers, step["started_unix"] + 12,
                                               step["started_unix"] + max(20, (step.get("seconds") or 30) - 6))
                steps[step["step"]] = slim(step) if args.slim else step
        evidence["sessions"][name] = {
            "record": load(directory / "session.json"),
            "codec_attempts": load(directory / "codec-attempts.json"),
            "steps": steps,
        }
        print(f"{name}: {len(steps)} steps")
    evidence = scrub(evidence)
    if args.slim:
        # One line for each step: small, and still readable in a diff.
        lines = []
        for name, session in evidence["sessions"].items():
            steps = session.pop("steps")
            head = json.dumps(session, separators=(",", ":"))[:-1]
            body = ",\n".join(f"  {json.dumps(key)}:{json.dumps(value, separators=(',', ':'))}"
                              for key, value in steps.items())
            lines.append(f" {json.dumps(name)}:{head},\"steps\":{{\n{body}\n }}}}")
        top = {key: value for key, value in evidence.items() if key != "sessions"}
        text = json.dumps(top, separators=(",", ":"))[:-1] + ",\"sessions\":{\n" + ",\n".join(lines) + "\n}}\n"
        json.loads(text)
        pathlib.Path(args.out).write_text(text)
    else:
        pathlib.Path(args.out).write_text(json.dumps(evidence, indent=1) + "\n")


if __name__ == "__main__":
    main()
