#!/usr/bin/env python3
"""Write one Linux host and process capacity sample each second."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--end-unix-ns", type=int, required=True)
    return parser.parse_args()


def read_number(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


def read_cpu() -> tuple[int, int]:
    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
    values = [int(value) for value in fields[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def read_process(pid: int) -> dict[str, int | None]:
    process_root = Path("/proc") / str(pid)
    stat_line = (process_root / "stat").read_text(encoding="utf-8")
    fields = stat_line[stat_line.rfind(")") + 2 :].split()
    if len(fields) < 22:
        raise ProcessLookupError(f"incomplete process stat for PID {pid}")
    status = {}
    for line in (process_root / "status").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            value_fields = value.strip().split()
            if value_fields:
                status[key] = value_fields[0]
    io_fields = {}
    try:
        io_lines = (process_root / "io").read_text(encoding="utf-8").splitlines()
    except PermissionError:
        io_lines = []
    for line in io_lines:
        key, separator, value = line.partition(":")
        if separator:
            io_fields[key] = int(value.strip())
    try:
        open_fds = len(list((process_root / "fd").iterdir()))
    except PermissionError:
        open_fds = None
    return {
        "cpu_ticks": int(fields[11]) + int(fields[12]),
        "minor_faults": int(fields[7]),
        "major_faults": int(fields[9]),
        "threads": int(fields[17]),
        "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
        "voluntary_context_switches": int(status.get("voluntary_ctxt_switches", 0)),
        "involuntary_context_switches": int(
            status.get("nonvoluntary_ctxt_switches", 0)
        ),
        "read_bytes": io_fields.get("read_bytes", 0),
        "write_bytes": io_fields.get("write_bytes", 0),
        "open_fds": open_fds,
    }


def read_host() -> dict[str, int | float]:
    load_fields = Path("/proc/loadavg").read_text(encoding="utf-8").split()
    runnable, total = load_fields[3].split("/", 1)
    memory = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            memory[key] = int(value.strip().split()[0]) * 1_024
    return {
        "cpu_count": os.cpu_count() or 1,
        "load_1": float(load_fields[0]),
        "load_5": float(load_fields[1]),
        "load_15": float(load_fields[2]),
        "runnable_tasks": int(runnable),
        "total_tasks": int(total),
        "memory_total_bytes": memory["MemTotal"],
        "memory_available_bytes": memory["MemAvailable"],
    }


def wait_for_pid(pid_file: Path, end_unix_ns: int) -> int | None:
    while time.time_ns() < end_unix_ns:
        try:
            pid = read_number(pid_file)
            if (Path("/proc") / str(pid)).exists():
                return pid
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.05)
    return None


def delta_rate(current: int, previous: int | None, seconds: float) -> float | None:
    if previous is None or seconds <= 0:
        return None
    return max(0, current - previous) / seconds


def main() -> int:
    args = parse_args()
    pid = wait_for_pid(args.pid_file, args.end_unix_ns)
    if pid is None:
        raise SystemExit("the source PID did not become available")

    previous_time = None
    previous_total_cpu = None
    previous_idle_cpu = None
    previous_process = None
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    next_sample = time.monotonic()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        while time.time_ns() <= args.end_unix_ns:
            try:
                total_cpu, idle_cpu = read_cpu()
                process = read_process(pid)
                host = read_host()
            except FileNotFoundError:
                break
            except ProcessLookupError:
                time.sleep(0.05)
                continue
            now_unix_ns = time.time_ns()
            now_monotonic = time.monotonic()
            elapsed = (
                None if previous_time is None else max(0.0, now_monotonic - previous_time)
            )
            total_delta = (
                None
                if previous_total_cpu is None
                else max(0, total_cpu - previous_total_cpu)
            )
            idle_delta = (
                None if previous_idle_cpu is None else max(0, idle_cpu - previous_idle_cpu)
            )
            host_cpu_percent = None
            if total_delta:
                host_cpu_percent = (
                    100.0 * max(0, total_delta - (idle_delta or 0)) / total_delta
                )
            process_cpu_percent = None
            if elapsed and previous_process is not None:
                process_cpu_percent = (
                    100.0
                    * max(0, process["cpu_ticks"] - previous_process["cpu_ticks"])
                    / ticks_per_second
                    / elapsed
                )
            sample = {
                "schema": "needletail.source-host.capacity-sample.v1",
                "timestamp_unix_ns": now_unix_ns,
                "pid": pid,
                **host,
                "host_cpu_percent": host_cpu_percent,
                "process_cpu_percent": process_cpu_percent,
                "process_cpu_capacity_percent": (
                    None
                    if process_cpu_percent is None
                    else process_cpu_percent / host["cpu_count"]
                ),
                **process,
                "minor_faults_per_second": (
                    None
                    if elapsed is None or previous_process is None
                    else delta_rate(
                        process["minor_faults"],
                        previous_process["minor_faults"],
                        elapsed,
                    )
                ),
                "major_faults_per_second": (
                    None
                    if elapsed is None or previous_process is None
                    else delta_rate(
                        process["major_faults"],
                        previous_process["major_faults"],
                        elapsed,
                    )
                ),
                "read_bytes_per_second": (
                    None
                    if elapsed is None or previous_process is None
                    else delta_rate(
                        process["read_bytes"], previous_process["read_bytes"], elapsed
                    )
                ),
                "write_bytes_per_second": (
                    None
                    if elapsed is None or previous_process is None
                    else delta_rate(
                        process["write_bytes"],
                        previous_process["write_bytes"],
                        elapsed,
                    )
                ),
            }
            output.write(json.dumps(sample, separators=(",", ":")) + "\n")
            output.flush()
            previous_time = now_monotonic
            previous_total_cpu = total_cpu
            previous_idle_cpu = idle_cpu
            previous_process = process
            next_sample += 1.0
            time.sleep(max(0.0, next_sample - time.monotonic()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
