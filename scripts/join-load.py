#!/usr/bin/env python3
"""Open new playback sessions at an entry point at a fixed rate.

A join is what a new viewer does first: a new TLS connection, and one request
for the Multivariant Playlist with a session identifier of its own. The entry
point assigns the session to a cache for each join. The script measures how
long each join takes and prints one JSON object.

  join-load.py --url https://ENTRY:19444/live/2001/master.m3u8 --rate 200 \\
      --seconds 15 [--ca FILE] [--server-name NAME] [--processes 8]

Only the standard library is used, so the script runs on a bare load host.
"""

from __future__ import annotations

import argparse
import http.client
import json
import multiprocessing
import socket
import ssl
import threading
import time
import urllib.parse


def worker(index: int, args, start_at: float, queue) -> None:
    url = urllib.parse.urlsplit(args.url)
    context = ssl.create_default_context(cafile=args.ca) if args.ca else ssl.create_default_context()
    # A lab certificate authority does not carry every extension that the
    # strict mode of newer Python versions asks for.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    if not args.ca:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    rate = args.rate / args.processes
    total = max(1, int(rate * args.seconds))
    results = []
    lock = threading.Lock()
    slots = threading.Semaphore(args.concurrency)

    def join(number: int, due: float) -> None:
        began = time.time()
        status, error = 0, None
        try:
            raw = socket.create_connection((url.hostname, url.port or 443), timeout=args.timeout)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            tls = context.wrap_socket(raw, server_hostname=args.server_name or url.hostname)
            connection = http.client.HTTPSConnection(url.hostname, url.port or 443,
                                                     timeout=args.timeout)
            connection.sock = tls
            separator = "&" if url.query else "?"
            path = (f"{url.path}?{url.query}" if url.query else url.path) \
                + f"{separator}sid=join-{args.label}-{index}-{number}"
            connection.request("GET", path, headers={"Host": args.server_name or url.hostname})
            response = connection.getresponse()
            body = response.read()
            status = response.status
            if status == 200 and b"#EXTM3U" not in body:
                error = "no playlist in the answer"
            connection.close()
        except Exception as failure:  # noqa: BLE001 - every failure is a result
            error = type(failure).__name__
        done = time.time()
        with lock:
            results.append((round((done - began) * 1000, 1), round((began - due) * 1000, 1),
                            status, error))
        slots.release()

    threads = []
    for number in range(total):
        due = start_at + number / rate
        delay = due - time.time()
        if delay > 0:
            time.sleep(delay)
        slots.acquire()
        thread = threading.Thread(target=join, args=(number, due), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout=args.timeout + 5)
    queue.put(results)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--rate", type=float, required=True, help="joins a second")
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--ca")
    parser.add_argument("--server-name")
    parser.add_argument("--processes", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=400,
                        help="joins in flight for each process")
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--label", default=str(int(time.time())))
    args = parser.parse_args()
    queue = multiprocessing.Queue()
    start_at = time.time() + 1.0
    processes = [multiprocessing.Process(target=worker, args=(index, args, start_at, queue))
                 for index in range(args.processes)]
    for process in processes:
        process.start()
    results = []
    for _ in processes:
        results.extend(queue.get(timeout=args.seconds + args.timeout + 60))
    for process in processes:
        process.join(timeout=10)
    # An entry point that has no room answers a join with a refusal. That is
    # an answer: the entry point did its work.
    took = [item[0] for item in results if item[2] > 0 and item[3] is None]
    admitted = sum(1 for item in results if item[2] == 200 and item[3] is None)
    late = [item[1] for item in results]
    statuses: dict[str, int] = {}
    for _took, _late, status, error in results:
        key = error or str(status)
        statuses[key] = statuses.get(key, 0) + 1
    elapsed = time.time() - start_at
    print(json.dumps({
        "url": args.url, "offered_per_second": args.rate, "seconds": args.seconds,
        "joins": len(results), "answered": len(took), "answered_200": admitted,
        "answered_per_second": round(len(took) / max(elapsed, args.seconds, 0.001), 1),
        "by_answer": statuses,
        "join_ms_p50": percentile(took, 0.5), "join_ms_p99": percentile(took, 0.99),
        "join_ms_max": max(took) if took else None,
        # A join that starts late means that the load host could not keep the rate.
        "started_late_ms_p99": percentile(late, 0.99),
    }))


if __name__ == "__main__":
    main()
