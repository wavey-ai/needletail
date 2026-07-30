#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import re
import subprocess
import time
from pathlib import Path


PART_PATTERN = re.compile(r'#EXT-X-PART:DURATION=([0-9.]+),URI="([^"]+)"')
PART_NUMBER_PATTERN = re.compile(r"part([0-9]+)\.mp4")


def parse_playlist(body: str, arrival_ns: int) -> dict:
    lines = body.splitlines()
    pdt_index = -1
    pdt_value = None
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt_index = index
            pdt_value = line.split(":", 1)[1]
    if pdt_value is None:
        raise ValueError("playlist has no program date and time")

    pdt = datetime.datetime.fromisoformat(pdt_value.replace("Z", "+00:00"))
    pdt_ns = int(pdt.timestamp() * 1_000_000_000)
    part_duration_seconds = 0.0
    part_count = 0
    latest_part_uri = None
    latest_part_duration_seconds = None
    for line in lines[pdt_index + 1 :]:
        match = PART_PATTERN.search(line)
        if match is None:
            continue
        duration_seconds = float(match.group(1))
        part_duration_seconds += duration_seconds
        part_count += 1
        latest_part_duration_seconds = duration_seconds
        latest_part_uri = match.group(2)
    if latest_part_uri is None:
        raise ValueError(
            "playlist has no media part after its latest program date and time"
        )

    number_match = PART_NUMBER_PATTERN.search(latest_part_uri)
    media_end_ns = pdt_ns + int(part_duration_seconds * 1_000_000_000)
    return {
        "program_date_time": pdt_value,
        "program_date_time_unix_ns": pdt_ns,
        "parts_after_program_date_time": part_count,
        "latest_part_uri": latest_part_uri,
        "latest_part_number": int(number_match.group(1)) if number_match else None,
        "latest_part_duration_ms": latest_part_duration_seconds * 1000.0,
        "latest_media_end_unix_ns": media_end_ns,
        "live_edge_latency_ms": (arrival_ns - media_end_ns) / 1_000_000.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--interval-ms", type=int, default=200)
    parser.add_argument("--port", type=int, default=19444)
    parser.add_argument("--path", default="/live/1/stream.m3u8")
    parser.add_argument("--ca", default="/etc/needletail/tls/fullchain.pem")
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()

    url = f"https://{args.server_name}:{args.port}{args.path}"
    curl = [
        "curl",
        "--silent",
        "--show-error",
        "--fail",
        "--max-time",
        "2",
        "--cacert",
        args.ca,
        "--resolve",
        f"{args.server_name}:{args.port}:127.0.0.1",
        "--write-out",
        "\n__NEEDLETAIL_CURL__%{time_connect},%{time_starttransfer},%{time_total},%{http_code}",
        url,
    ]
    started = time.monotonic()
    deadline = started + args.duration_seconds
    next_sample = started
    status = 1
    try:
        while time.monotonic() < deadline and not (
            args.stop_file is not None and args.stop_file.exists()
        ):
            request_start_ns = time.time_ns()
            completed = subprocess.run(
                curl, capture_output=True, text=True, check=False
            )
            arrival_ns = time.time_ns()
            sample = {
                "request_start_unix_ns": request_start_ns,
                "arrival_unix_ns": arrival_ns,
                "curl_exit_code": completed.returncode,
            }
            try:
                body, metadata = completed.stdout.rsplit("\n__NEEDLETAIL_CURL__", 1)
                connect, start_transfer, total, http_code = metadata.strip().split(",")
                sample.update(
                    {
                        "http_code": int(http_code),
                        "connect_ms": float(connect) * 1000.0,
                        "start_transfer_ms": float(start_transfer) * 1000.0,
                        "request_ms": float(total) * 1000.0,
                        "playlist_bytes": len(body.encode()),
                    }
                )
                if completed.returncode == 0:
                    sample.update(parse_playlist(body, arrival_ns))
            except Exception as error:
                sample["parse_error"] = str(error)
            if completed.stderr:
                sample["curl_error"] = completed.stderr.strip()
            print(json.dumps(sample, separators=(",", ":")), flush=True)
            next_sample += args.interval_ms / 1000.0
            time.sleep(max(0.0, next_sample - time.monotonic()))
        status = 0
        return status
    finally:
        temporary_status = args.status_file.with_name(
            f".{args.status_file.name}.{os.getpid()}.tmp"
        )
        temporary_status.write_text(f"{status}\n", encoding="ascii")
        os.replace(temporary_status, args.status_file)


if __name__ == "__main__":
    raise SystemExit(main())
