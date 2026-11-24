#!/usr/bin/env python3
"""Touch a fresh guest's free memory once, then give it back (run on a lab node,
as root, before any measured window; python3 only, no other dependency).

The lead's hunt lab (hunt11241537, 24 Nov): Linode guests are not run for up
to 2.3 s, steal not reported, while they first touch their memory (soft
lockups in clear_page_rep under do_huge_pmd_anonymous_page with THP
[always]); a guest whose free memory had been written once had no stop of
100 ms or more for the rest of the run. This writes one byte in every 4 KiB
page of MemAvailable minus --reserve-mb, in anonymous 64 MiB mappings (THP
eligible, as the services' own allocations are), timing every write, then
unmaps them. It prints one JSON line: what it touched, how long it took, and
every write that stalled --stall-ms or more.

    warm-guest.py [--reserve-mb 300] [--stall-ms 100]
"""
import argparse
import json
import mmap
import os
import socket
import time

PAGE = 4096
CHUNK = 64 << 20


def available_kb() -> int:
    with open("/proc/meminfo") as handle:
        for line in handle:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reserve-mb", type=int, default=300)
    parser.add_argument("--stall-ms", type=float, default=100.0)
    args = parser.parse_args()
    started = time.time()
    before_kb = available_kb()
    maps, stalls, touched = [], [], 0
    worst_ns = 0
    try:
        while available_kb() - (CHUNK >> 10) > args.reserve_mb * 1024:
            area = mmap.mmap(-1, CHUNK, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
            maps.append(area)
            for offset in range(0, CHUNK, PAGE):
                t0 = time.monotonic_ns()
                area[offset] = 1
                spent = time.monotonic_ns() - t0
                if spent > worst_ns:
                    worst_ns = spent
                if spent >= args.stall_ms * 1e6:
                    stalls.append({"at_unix": round(time.time(), 3), "ms": round(spent / 1e6, 1),
                                   "after_mb": (touched + offset) >> 20})
            touched += CHUNK
    finally:
        for area in maps:
            area.close()
    print(json.dumps({"host": socket.gethostname(), "pid": os.getpid(),
                      "mem_available_before_mb": before_kb >> 10, "touched_mb": touched >> 20,
                      "seconds": round(time.time() - started, 1), "worst_write_ms": round(worst_ns / 1e6, 1),
                      "stalls": stalls, "stall_ms": args.stall_ms,
                      "thp": open("/sys/kernel/mm/transparent_hugepage/enabled").read().strip()}))


if __name__ == "__main__":
    main()
