#!/usr/bin/env python3
"""Every 10 ms, what each node's audio class thread is doing: the finer
half of the stall hunt's sampler (hunt-sampler.sh reads once a second).

    class-thread-sampler.py OUT NODE=PID [NODE=PID ...] [--period-ms 10]
                            [--stack] [--seconds 0]

For each service PID, the class thread is av-mesh's "needletail-fast" with
a real-time policy, else the lowest tid of that name (av-mesh starts the
audio class thread before the video one), or av-contrib's "audio-hot-0".
OUT gets one NDJSON line a period:
  {"t_us": CLOCK_REALTIME, "n": {NODE: [state, wchan, run_ns, delay_ns,
                                      voluntary, involuntary, cpu]}}
state is /proc's (R running or runnable, S sleeping, D uninterruptible);
wchan is where a sleeping thread waits (poll_schedule_timeout: the class
loop's own poll for input; futex_*: a lock; a page-fault or folio wait:
memory). With --stack (the process then needs to read other processes'
/proc/PID/task/TID/stack, i.e. root: run it as `sudo -n` for read-only
diagnostics), a sample whose thread is not asleep in its poll also carries
the kernel stack ("stack": {NODE: [frames]}).
Ends on SIGTERM or SIGINT, or after --seconds."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

IDLE_WCHAN = ("poll_schedule_timeout", "do_sys_poll", "ep_poll")


def class_thread(pid: int) -> int | None:
    best = None
    try:
        tasks = sorted(int(task) for task in os.listdir(f"/proc/{pid}/task"))
    except OSError:
        return None
    for tid in tasks:
        try:
            with open(f"/proc/{pid}/task/{tid}/comm") as handle:
                comm = handle.read().strip()
            with open(f"/proc/{pid}/task/{tid}/sched") as handle:
                policy = next((line.split(":")[1].strip() for line in handle
                               if line.startswith("policy")), "0")
        except OSError:
            continue
        if comm in ("needletail-fast", "audio-hot-0"):
            if policy in ("1", "2"):
                return tid
            best = best or tid
    return best


def read(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return None


def sample(pid: int, tid: int, stack: bool) -> tuple[list, list | None]:
    base = f"/proc/{pid}/task/{tid}"
    stat = read(base + "/stat")
    if stat is None:
        return [None] * 7, None
    fields = stat.rpartition(")")[2].split()
    state, cpu = fields[0], int(fields[36])
    wchan = (read(base + "/wchan") or "").strip()
    sched = (read(base + "/schedstat") or "0 0 0").split()
    status = read(base + "/status") or ""
    vol = invol = None
    for line in status.splitlines():
        if line.startswith("voluntary_ctxt_switches"):
            vol = int(line.split()[1])
        elif line.startswith("nonvoluntary_ctxt_switches"):
            invol = int(line.split()[1])
    frames = None
    if stack and not (state == "S" and wchan.startswith(IDLE_WCHAN)):
        text = read(base + "/stack")
        frames = [line.split("] ", 1)[-1] for line in (text or "").splitlines()][:16]
    return [state, wchan, int(sched[0]), int(sched[1]), vol, invol, cpu], frames


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out")
    parser.add_argument("nodes", nargs="+", metavar="NODE=PID")
    parser.add_argument("--period-ms", type=float, default=10.0)
    parser.add_argument("--stack", action="store_true")
    parser.add_argument("--seconds", type=float, default=0.0)
    args = parser.parse_args()
    pids = {item.split("=")[0]: int(item.split("=")[1]) for item in args.nodes}
    tids = {name: class_thread(pid) for name, pid in pids.items()}
    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.append(1))
    period = args.period_ms / 1000
    end = time.monotonic() + args.seconds if args.seconds else None
    due = time.monotonic()
    with open(args.out, "a") as out:
        out.write(json.dumps({"meta": {"pids": pids, "tids": tids, "period_ms": args.period_ms,
                                       "stack": args.stack}}) + "\n")
        while not stop and (end is None or time.monotonic() < end):
            line, stacks = {"t_us": time.time_ns() // 1000, "n": {}}, {}
            for name, pid in pids.items():
                if tids.get(name) is None:
                    tids[name] = class_thread(pid)
                    if tids[name] is None:
                        continue
                values, frames = sample(pid, tids[name], args.stack)
                line["n"][name] = values
                if frames:
                    stacks[name] = frames
            if stacks:
                line["stack"] = stacks
            out.write(json.dumps(line, separators=(",", ":")) + "\n")
            due += period
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                due = time.monotonic()
        out.flush()


if __name__ == "__main__":
    sys.exit(main())
