#!/usr/bin/env python3
"""Tables of the tier test (tiers.py): the load of each tier of Needletail as
the audience, the edges and the streams change, and of an SFU's publishing
node as its audience grows.

  tiers-report.py SESSION [--json OUT]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import statistics

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("analyse", HERE / "compare-audio-analyse.py")
analyse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyse)
RUNS = HERE.parents[1] / "target" / "compare" / "runs"
NODES = [("contrib-lon", "av-contrib", "Contributor"), ("relay-primary", "av-mesh", "Primary relay"),
         ("relay-secondary", "av-mesh", "Secondary relay"), ("lon-dist-a", "av-mesh", "Distributor A, London"),
         ("lon-dist-b", "av-mesh", "Distributor B, London"), ("lon-edge-1", "av-mesh", "Edge 1, London"),
         ("lon-edge-2", "av-mesh", "Edge 2, London"), ("lon-lk", "livekit-server", "LiveKit SFU, London")]


def listeners(step_dir: pathlib.Path) -> dict:
    """What the listeners of a step got: HLS crowds (ntcmp hls --count),
    UDP clients (needletail-udp-client), SFU subscribers (ntcmp sub)."""
    out = {"started": 0, "with_media": 0, "latency_p50": [], "latency_p99": [], "missing": []}
    for host in sorted(p for p in step_dir.iterdir() if p.is_dir()):
        for path in host.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except ValueError:
                continue
            if path.name.startswith("uc-"):
                out["started"] += 1
                if data.get("received_parts"):
                    out["with_media"] += 1
                    lat = data.get("publish_to_arrival_ms") or {}
                    if lat.get("p50") is not None:
                        out["latency_p50"].append(lat["p50"])
                        out["latency_p99"].append(lat["p99"])
                    if data.get("expected_parts"):
                        out["missing"].append(100.0 * (data.get("missing_parts") or 0) / data["expected_parts"])
            elif path.name.startswith("hls") and isinstance(data, dict):
                # One listener of an HLS crowd (ntcmp hls --count): it has media when
                # it received the stream's rate for most of the step.
                out["started"] += 1
                rates = sorted(data.get("kbps_by_second") or [])
                if rates and rates[len(rates) // 2] > 50:
                    out["with_media"] += 1
                out.setdefault("kbps", []).append(rates[len(rates) // 2] if rates else 0)
            elif path.name.startswith(("lk", "cf")) and isinstance(data, dict):
                count = int(data.get("count") or 1)
                out["started"] += count
                ok = data.get("listeners_with_audio", data.get("readers_ok"))
                detail = data.get("detail") or {}
                ok = detail.get("listeners_ok", ok)
                out["with_media"] += int(ok) if ok is not None else (count if (data.get("audio_packets") or data.get("bytes")) else 0)
                lat = data.get("audio_latency_ms") or {}
                if lat.get("p50") is not None:
                    out["latency_p50"].append(lat["p50"])
                    out["latency_p99"].append(lat.get("p99"))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session")
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    session = RUNS / args.session
    samplers = analyse.load_samplers(session)
    rows, record = [], {}
    for step_dir in sorted(p for p in session.iterdir() if p.is_dir() and p.name.startswith("tiers.")):
        meta_path = step_dir / "meta.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text())
        usage = analyse.node_usage(samplers, meta["started_unix"], meta["started_unix"] + meta["seconds"])
        got = listeners(step_dir)
        record[step_dir.name] = {"meta": {k: meta.get(k) for k in ("detail", "listeners", "mode", "streams",
                                                                     "listeners_each", "spread", "pinned")},
                                 "listeners": {k: (v if not isinstance(v, list) else
                                                   (round(statistics.median(v), 2) if v else None))
                                               for k, v in got.items()},
                                 "nodes": {n: usage.get(n) for n, _, _ in NODES}}
        cells = [step_dir.name[len("tiers."):], f"{got['with_media']}/{got['started']}"]
        for node, process, _ in NODES:
            u = usage.get(node) or {}
            cpu = u.get(f"{process}_cpu_percent")
            mbit = (u.get("net_eth0_mbit_per_s") or 0) + (u.get("net_eth1_mbit_per_s") or 0)
            cells.append(f"{cpu:.0f}% {mbit:.0f}" if cpu is not None else "–")
        rows.append(cells)
    print("Processor (% of one vCPU) and bytes out (Mbit/s, all interfaces) of each node during each step; "
          "listeners with media of those started:\n")
    header = ["Step", "Listeners"] + [label for _, _, label in NODES]
    print("| " + " | ".join(header) + " |")
    print("| " + " | ".join("---" if i == 0 else "---:" for i in range(len(header))) + " |")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print()
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(record, indent=1) + "\n")


if __name__ == "__main__":
    main()
