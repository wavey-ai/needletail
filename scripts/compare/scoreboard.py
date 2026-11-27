#!/usr/bin/env python3
"""The scoreboard of the standing goal (owner, 22 November): Needletail
against self-hosted LiveKit on every audio metric, in every region measured,
from the slim evidence of compare-audio-analyse.py.

Needletail is its UDP lane from the edge (nt-uc); LiveKit is an SFU in each
region (lkregion), measured in the same session. Each system runs at the
smallest frame it forwards, as in the records: the rendition row names both.

Metrics, per region, lower is better:
  mouth to ear, median and p99, clean network (baseline.clean)
  units missing at 1%, 5% and burst loss, and p99 mouth to ear at 5% loss
  time to first audio of a new listener on a running stream (joins), median
Figures are the 4-vCPU view host's probe, or the Nanode's where a step has no
view-host probe (older evidence); the host is named in the JSON.

python3 scripts/compare/scoreboard.py EVIDENCE --sessions waudio7,waudio7-std \
    --out target/compare/scoreboard-wave7.json  >scoreboard.md

--host-stops FILE (host-stops.py's JSON) marks each row whose step had a
heartbeat stop on a measurement host that the row depends on: the source host
(lon-src) or the host of either listener. Both systems' listeners run on the
same hosts at the same time, so such a stop voids the row for both. The rows
stay in the board, marked, and the tally is given with and without them.
"""

from __future__ import annotations

import argparse
import json
import pathlib

NT, LK = "nt-uc", "lkregion"
# --against: the LiveKit lane on the other side of the board.
AGAINST = {"lkregion": "self-hosted LiveKit (an SFU in each region)",
           "lkcloud": "LiveKit Cloud (LiveKit's own SFU mesh)"}
SITES = [("lon", "London"), ("new", "New York"), ("tok", "Tokyo"), ("syd", "Sydney")]
# (key, label, step detail, field path, digits)
METRICS = [
    ("m2e_p50", "Mouth to ear, median (ms)", "baseline.clean", ("mouth_to_ear_ms", "p50"), 1),
    ("m2e_p99", "Mouth to ear, p99 (ms)", "baseline.clean", ("mouth_to_ear_ms", "p99"), 1),
    ("missing_loss1", "Units missing at 1% loss (%)", "impair.loss1", ("missing_percent",), 2),
    ("missing_loss5", "Units missing at 5% loss (%)", "impair.loss5", ("missing_percent",), 2),
    ("missing_burst", "Units missing under burst loss (%)", "impair.burst", ("missing_percent",), 2),
    ("m2e_p99_loss5", "Mouth to ear, p99 at 5% loss (ms)", "impair.loss5", ("mouth_to_ear_ms", "p99"), 1),
    ("first_audio", "Time to first audio, new listener, median (ms)", "joins", ("first_audio_ms", "p50"), 0),
]


def dig(value, path):
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def rendition(steps: dict, config: str) -> str:
    """The rendition row: what each system ran in this configuration."""
    step = next((s for name, s in steps.items() if name.startswith(f"audio.{config}.")), {}) or {}
    if config == "pcm":
        nt = round((step.get("nt_pcm_epoch_samples") or 48) / 48, 2)
        sfu = step.get("sfu_pcm_ms") or 2.5
        return f"PCM 48 kHz/24-bit (Needletail {nt:g} ms parts; LiveKit {sfu:g} ms data packets)"
    samples = step.get("nt_popus_samples")
    if samples == 48:
        return "Opus, smallest frame (Needletail 1 ms Opus Custom; LiveKit 2.5 ms)"
    if samples == 120:
        return "Opus 2.5 ms, standard (both)"
    return f"Opus ({config}; Needletail frame not recorded)"


def figure(steps: dict, config: str, detail: str, site: str, lane: str, path, joins: bool):
    """(value, host, step name) of a lane in a region, preferring the view host."""
    if joins:
        name = next((n for n in steps if n.startswith(f"audio.{config}.joins.")), None)
    else:
        name = f"audio.{config}.{detail}"
    step = steps.get(name) if name else None
    if not step:
        return None, None, None
    for host in (f"{site}-view", f"{site}-nano"):
        entry = (step.get("hosts", {}).get(host, {}).get("lanes", {}) or {}).get(lane) or {}
        record = entry.get("joins") if joins else entry.get("probe")
        if record and not (not joins and record.get("error")):
            value = dig(record, path)
            if value is not None:
                return value, host, name
    return None, None, name


def stopped_hosts(host_stops: dict | None, session: str, step: str | None, hosts) -> dict:
    """{measurement host: worst stop ms} among the source host and the row's
    listener hosts, for this step; {} without host-stops data."""
    if not host_stops or not step:
        return {}
    info = (((host_stops.get("sessions") or {}).get(session) or {}).get("steps") or {}).get(step) or {}
    wanted = {"lon-src", *[h for h in hosts if h]}
    return {h: v["worst_ms"] for h, v in (info.get("hosts") or {}).items()
            if h in wanted and v.get("class") == "measurement"}


def verdict(nt, lk, digits: int) -> str:
    if nt is None or lk is None:
        return "not measured"
    a, b = round(max(0.0, nt), digits), round(max(0.0, lk), digits)
    return "win" if a < b else "tie" if a == b else "loss"


def board(evidence: dict, sessions: list[str], host_stops: dict | None = None) -> list[dict]:
    rows = []
    for session in sessions:
        steps = evidence["sessions"][session]["steps"]
        configs = sorted({name[len("audio."):].split(".baseline")[0].split(".impair")[0].split(".joins")[0]
                          .split(".fanout")[0] for name in steps if name.startswith("audio.")})
        for config in configs:
            if config not in ("opus-2.5", "pcm"):
                continue
            label = rendition(steps, config)
            for key, name, detail, path, digits in METRICS:
                for site, region in SITES:
                    joins = detail == "joins"
                    nt, nt_host, step_name = figure(steps, config, detail, site, NT, path, joins)
                    lk, lk_host, _ = figure(steps, config, detail, site, LK, path, joins)
                    if nt is None and lk is None:
                        continue
                    row = {"session": session, "config": config, "rendition": label, "metric": key,
                           "label": name, "region": region, "needletail": nt, "livekit": lk,
                           "hosts": [nt_host, lk_host], "result": verdict(nt, lk, digits),
                           "digits": digits}
                    stopped = stopped_hosts(host_stops, session, step_name, (nt_host, lk_host))
                    if host_stops is not None:
                        row["measurement_host_stopped"] = stopped or None
                    rows.append(row)
    return rows


def fmt(value, digits):
    return "–" if value is None else f"{max(0.0, value):.{digits}f}"


def markdown(rows: list[dict]) -> str:
    out = []
    counts = {}
    for row in rows:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    total = len(rows)
    out.append(f"Needletail (UDP from the edge) against {AGAINST[LK]}, "
               "same session; lower is better. "
               + ", ".join(f"{counts.get(k, 0)} {k}" for k in ("win", "tie", "loss", "not measured"))
               + f" of {total}.\n")
    if any("measurement_host_stopped" in row for row in rows):
        clean = [row for row in rows if not row.get("measurement_host_stopped")]
        kept = {}
        for row in clean:
            kept[row["result"]] = kept.get(row["result"], 0) + 1
        out.append(f"{total - len(clean)} rows are marked ‡: the source host or a listener's host stopped "
                   "(heartbeat) during the step, for both systems alike. Without them: "
                   + ", ".join(f"{kept.get(k, 0)} {k}" for k in ("win", "tie", "loss", "not measured"))
                   + f" of {len(clean)}.\n")
    seen = []
    for row in rows:
        group = (row["session"], row["rendition"])
        if group not in seen:
            seen.append(group)
    for session, label in seen:
        out.append(f"**{label}** (session {session}):\n")
        out.append("| Metric | Region | Needletail | LiveKit | Result |")
        out.append("| --- | --- | ---: | ---: | --- |")
        for row in rows:
            if (row["session"], row["rendition"]) == (session, label):
                stopped = row.get("measurement_host_stopped")
                mark = (" ‡ (" + ", ".join(f"{h} {ms:.0f} ms" for h, ms in sorted(stopped.items())) + ")"
                        if stopped else "")
                out.append(f"| {row['label']} | {row['region']} | {fmt(row['needletail'], row['digits'])} | "
                           f"{fmt(row['livekit'], row['digits'])} | {row['result']}{mark} |")
        out.append("")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("evidence")
    parser.add_argument("--sessions", required=True, help="comma-separated sessions of the evidence")
    parser.add_argument("--out", help="the scoreboard as JSON (target/compare/scoreboard-*.json)")
    parser.add_argument("--against", choices=sorted(AGAINST), default="lkregion",
                        help="the LiveKit lane: self-hosted (default) or LiveKit Cloud (audio sessions only)")
    parser.add_argument("--host-stops", help="host-stops.py's JSON: mark rows whose measurement hosts stopped")
    args = parser.parse_args()
    global LK
    LK = args.against
    evidence = json.loads(pathlib.Path(args.evidence).read_text())
    host_stops = json.loads(pathlib.Path(args.host_stops).read_text()) if args.host_stops else None
    rows = board(evidence, args.sessions.split(","), host_stops)
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps({"evidence": args.evidence, "sessions": args.sessions,
                                                      "needletail_lane": NT, "livekit_lane": LK,
                                                      "rows": rows}, indent=1) + "\n")
    print(markdown(rows))


if __name__ == "__main__":
    main()
