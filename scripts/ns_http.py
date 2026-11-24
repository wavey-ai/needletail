#!/usr/bin/env python3
"""Kept-open HTTPS for the local lab's tools (local proof 2, F3).

The lab's nodes live in network namespaces, so each request of its tools was
a process: `nsenter ... taskset ... curl`, with a TLS handshake on the node
for every one. A gate polling ten nodes five times a second made fifty
processes and fifty handshakes a second on the CPUs the probes and the
sender share. This is one process, started once in the lab's namespace
(under the same prefix the lab gives curl: `edge_lab.enter(None)`), that
keeps one connection per node open (mesh_ops.Http, as the Linode backend
does from the operator host) and answers requests given as JSON lines:

  stdin   {"id": 1, "host": "10.90.0.11", "port": 19444, "method": "GET",
           "path": "/api/node", "body": null, "timeout": 2.0}
  stdout  {"id": 1, "status": 200, "body": {...}, "bytes": 3612}

`status` and `body` are null when the node did not answer. Several requests
are answered at once (a poll asks every node together); answers come back
as they are ready, by id.

  ns_http.py [--ca FILE] [--server-name NAME]   (the lab: TLS, its CA)
  ns_http.py --plain                            (plain HTTP: the tests)

`Client` is the tools' side: `request(host, port, method, path, body,
timeout)` -> (status, JSON) and `HelperGone` when the process died (the lab
then falls back to curl).
"""

from __future__ import annotations

import argparse
import http.client
import itertools
import json
import pathlib
import socket
import ssl
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

HERE = pathlib.Path(__file__).resolve().parent
WORKERS = 16


class HelperGone(Exception):
    """The helper process is not running: nothing was asked."""


def tls_connect(ca: str | None, server_name: str | None):
    """mesh_ops.Http's `connect` for the lab: TLS to an address with the
    lab's server name (SNI) and, with `ca`, its certificate checked."""
    if ca:
        context = ssl.create_default_context(cafile=ca)
        # Python 3.13+ sets VERIFY_X509_STRICT by default, which refuses a
        # certificate without an Authority Key Identifier, as the lab's CA
        # makes them (curl takes them). With it every request of the helper
        # failed its handshake and the engine's gates read every node as
        # silent (local proof 3: the ingress kill refused, "lon-dist-a does
        # not answer /api/node"). The chain and the name are still checked.
        context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    class Connection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((self.host, self.port), self.timeout)
            self.sock = context.wrap_socket(sock, server_hostname=server_name or self.host)

    def connect(host: str, port: int, timeout: float):
        return Connection(host, port, timeout=timeout, context=context)
    return connect


def plain_connect(host: str, port: int, timeout: float):
    return http.client.HTTPConnection(host, port, timeout=timeout)


def serve(connect, stdin=None, stdout=None) -> None:
    """Answer the requests of `stdin` on `stdout` until stdin closes."""
    sys.path.insert(0, str(HERE))
    import mesh_ops
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    clients: dict[float, mesh_ops.Http] = {}
    guard = threading.Lock()

    def http_for(timeout: float) -> mesh_ops.Http:
        with guard:
            if timeout not in clients:
                clients[timeout] = mesh_ops.Http(timeout=timeout, connect=connect)
            return clients[timeout]

    def answer(item: dict) -> None:
        status, document, size = None, None, 0
        try:
            client = http_for(float(item.get("timeout") or 2.0))
            status, document = client.request(item["host"], int(item["port"]),
                                              item.get("method") or "GET", item["path"],
                                              item.get("body"))
            size = len(json.dumps(document)) if document is not None else 0
        except Exception as error:  # noqa: BLE001 - one request failing is its answer
            document = {"error": repr(error)} if status else None
        line = json.dumps({"id": item.get("id"), "status": status, "body": document,
                           "bytes": size})
        with guard:
            stdout.write(line + "\n")
            stdout.flush()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            pool.submit(answer, item)
    for client in clients.values():
        client.close()


class Client:
    """The tools' side of the helper: one process, requests from any
    thread, each answered by id."""

    def __init__(self, command: list[str]):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.ids = itertools.count(1)
        self.waiting: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.requests = 0
        self.bytes = 0
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self) -> None:
        for line in self.process.stdout:
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                slot = self.waiting.get(answer.get("id"))
            if slot is not None:
                slot["answer"] = answer
                slot["done"].set()
        with self.lock:
            for slot in self.waiting.values():
                slot["done"].set()

    def alive(self) -> bool:
        return self.process.poll() is None

    def request(self, host: str, port: int, method: str, path: str, body=None,
                timeout: float = 2.0) -> tuple:
        """(HTTP status, JSON) or (None, None) when the node did not answer;
        HelperGone when the helper is not running."""
        if not self.alive():
            raise HelperGone("the kept-open HTTP helper is not running")
        ident = next(self.ids)
        slot = {"done": threading.Event(), "answer": None}
        with self.lock:
            self.waiting[ident] = slot
        try:
            line = json.dumps({"id": ident, "host": host, "port": port, "method": method,
                               "path": path, "body": body, "timeout": timeout})
            try:
                with self.lock:
                    self.process.stdin.write(line + "\n")
                    self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                raise HelperGone("the kept-open HTTP helper is not running") from None
            # Its own timeout, a reconnect's and a margin.
            if not slot["done"].wait(2 * timeout + 5):
                return None, None
            answer = slot["answer"]
            if answer is None:
                raise HelperGone("the kept-open HTTP helper stopped")
            self.requests += 1
            self.bytes += int(answer.get("bytes") or 0)
            return answer.get("status"), answer.get("body")
        finally:
            with self.lock:
                self.waiting.pop(ident, None)

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ca", default=None, help="the lab's CA certificate (PEM)")
    parser.add_argument("--server-name", default=None, help="the name the nodes' certificate has")
    parser.add_argument("--plain", action="store_true", help="plain HTTP (tests)")
    args = parser.parse_args(argv)
    serve(plain_connect if args.plain else tls_connect(args.ca, args.server_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
