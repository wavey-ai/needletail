#!/usr/bin/env python3
"""Print the headline numbers of one viewer record (scripts/compare/player/run.mjs)."""
import json
import sys

LOOP_MS = 64000.0


def summarise(record: dict, fps: float = 24.0, skip_s: float = 5.0) -> dict:
    frames = [f for f in record["frames"] if f[0] >= 0]
    out = {"lane": record.get("lane"), "label": record.get("label"), "host": record.get("host"),
           "first_frame_ms": record.get("first_frame_ms"), "callbacks": record.get("callbacks"),
           "unreadable": record.get("unreadable_frames"), "sizes": record.get("sizes"),
           "chrome_cpu_cores": record.get("chrome_cpu_cores"), "error": record.get("error")}
    if not frames:
        return out
    start = record["started_unix_ms"]
    latencies, distinct, last_index = [], 0, None
    first_latency = None
    for index, wall, _media, _presented in frames:
        latency = (wall - index * 1000.0 / fps) % LOOP_MS
        if latency > LOOP_MS - 2000:
            latency -= LOOP_MS
        if first_latency is None:
            first_latency = latency
        if index != last_index:
            distinct += 1
            last_index = index
            if wall - start >= skip_s * 1000:
                latencies.append(latency)
    latencies.sort()
    pick = lambda q: round(latencies[min(len(latencies) - 1, int(q * (len(latencies) - 1) + 0.5))], 1)
    # A freeze is a gap between presented frames above three frame intervals.
    gaps = []
    previous = None
    for index, wall, _media, _presented in frames:
        if previous is not None and index != previous[0]:
            gap = wall - previous[1]
            if gap > 3 * 1000.0 / fps and previous[1] - start >= 1000:
                gaps.append(gap)
        if previous is None or index != previous[0]:
            previous = (index, wall)
    span = (frames[-1][1] - frames[0][1]) / 1000.0
    out.update({
        "distinct_frames": distinct, "seconds": round(span, 1),
        "fps": round(distinct / span, 2) if span else None,
        "first_frame_latency_ms": round(first_latency, 1),
        "latency_ms": {"p50": pick(0.5), "p95": pick(0.95), "p99": pick(0.99),
                       "min": round(latencies[0], 1), "max": round(latencies[-1], 1)} if latencies else None,
        "freezes": len(gaps), "freeze_ms": round(sum(gaps)), "longest_freeze_ms": round(max(gaps)) if gaps else 0,
        "events": [e for e in record.get("events", []) if not e[1].startswith("video-playing")][:14],
    })
    stats = record.get("stats") or []
    if stats:
        last = stats[-1]
        out["last_stats"] = {k: last[k] for k in last if k in (
            "kbps", "packetsLost", "nackCount", "pliCount", "framesDecoded", "framesDropped",
            "freezeCount", "totalFreezesDuration", "frameWidth", "frameHeight", "decoderImplementation",
            "jitterBufferDelay", "jitterBufferEmittedCount", "latency", "buffered", "dropped", "decoded", "rate")}
    return out


if __name__ == "__main__":
    for path in sys.argv[1:]:
        print(json.dumps(summarise(json.load(open(path)))))
