#!/usr/bin/env python3
"""The change tools' own cost on the nodes (local proof 2, F3), offline: a
fake backend that counts every HTTP request the gates and waits make, by
node and path, on the fake clock of test_mesh_ops. The budget: at most one
request to a node per poll interval (1 s by default), /api/node rather than
/api/mesh/local wherever /api/node carries the figure, and nothing between a
gate's decision and the change's end that no wait needs.

`budget(...)` is also what the report's before/after table was made with
(run against the engine of 2aea92b and of this branch).
python3 -m unittest scripts/test_request_budget.py"""

from __future__ import annotations

import collections
import io
import json
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import contributor_changes as cc  # noqa: E402
import mesh_ops  # noqa: E402
from test_contributor_changes import ContribSim  # noqa: E402
from test_mesh_ops import Clock  # noqa: E402

# What one answer weighs on the wire, from the archive: /api/node of the
# integrated build 2.8 to 4.0 kB (lp2c10-e-ingress nodes-before.json,
# compact); /api/mesh/local 52 to 53 kB on wave 6's smaller lab
# (compare-legacy-20261123 evidence-w6/nack), more with more peers and
# streams; /api/assignment an estimate.
BYTES = {"/api/node": 3_600, "/api/mesh/local": 53_000, "/api/assignment": 1_500}


class CountingOps:
    """Any Ops, every request it is asked for logged as (time, node, path)."""

    def __init__(self, inner, clock):
        self.inner = inner
        self.clock = clock
        self.log: list[tuple[float, str, str]] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def note(self, node: str, path: str) -> None:
        self.log.append((self.clock(), node, path.split("?", 1)[0]))

    def status(self, node):
        self.note(node, "/api/node")
        return self.inner.status(node)

    def statuses(self, nodes):
        return {node: self.status(node) for node in nodes}

    def get(self, node, path):
        self.note(node, path)
        return self.inner.get(node, path)

    def post(self, node, path, body):
        self.note(node, path)
        return self.inner.post(node, path, body)


def budget(log: list[tuple[float, str, str]], window_s: float = 1.0) -> dict:
    """The most reads one node got in any `window_s` (control requests,
    a drain or a resume, are actions and not counted there), the mean rate
    a node over the span, requests and bytes by path."""
    if not log:
        return {"requests": 0, "max_per_node_window": 0, "seconds": 0.0}
    times = collections.defaultdict(list)
    for at, node, path in log:
        if not path.startswith("/api/control/"):
            times[node].append(at)
    worst = 0
    for stamps in times.values():
        stamps.sort()
        low = 0
        for high, at in enumerate(stamps):
            while at - stamps[low] >= window_s - 1e-9:
                low += 1
            worst = max(worst, high - low + 1)
    span = max(1e-9, log[-1][0] - log[0][0])
    paths = collections.Counter(path for _at, _node, path in log)
    return {"requests": len(log), "seconds": round(span, 1),
            "max_per_node_window": worst,
            "mean_per_node_per_s": round(len(log) / len(times) / max(span, 1.0), 2),
            "by_path": dict(paths),
            "kbytes": round(sum(BYTES.get(path, 0) * count for path, count in paths.items())
                            / 1000, 1)}


def scenarios(tmp: pathlib.Path, poll_s: float | None = None) -> dict:
    """Each gate and change of C 6.3 that polls, on the fake mesh, with the
    engine's default poll interval unless `poll_s`."""
    poll = poll_s if poll_s is not None else mesh_ops.POLL_S
    out = {}

    def fresh(name: str):
        clock = Clock()
        sim = ContribSim(clock, tmp / name)
        return clock, sim, CountingOps(sim, clock)

    # e of local proof 2: an ingress kill whose gate refuses for 30 s.
    clock, sim, ops = fresh("ingress-refused")
    sim.row[("lon-edge-1", "lon-dist-b")] = {"skipped_10s": 50}
    ok, _why, _evidence, _pairs = cc.ingress_gate(ops, "lon-dist-a", timeout_s=30.0, poll_s=poll,
                                                  clock=clock, sleep=clock.sleep)
    assert not ok
    out["ingress gate, refusing 30 s"] = budget(ops.log)
    # The contributor gate that passes, and a contributor's readiness.
    clock, sim, ops = fresh("contrib-gate")
    ok, why, _evidence = cc.contrib_gate(ops, "contrib-lon", timeout_s=30.0, poll_s=poll,
                                         clock=clock, sleep=clock.sleep, change=False)
    assert ok, why
    out["contributor gate, passing"] = budget(ops.log)
    clock, sim, ops = fresh("contrib-ready")
    ok, why, _evidence = cc.contrib_ready(ops, "contrib-lon", timeout_s=60.0, poll_s=poll,
                                          clock=clock, sleep=clock.sleep)
    assert ok, why
    out["contributor readiness"] = budget(ops.log)
    # Whole rolls.
    for tier in ("edges", "forwarders"):
        clock, sim, ops = fresh(f"roll-{tier}")
        code = mesh_ops.roll(ops, tier, sites=["london"] if tier == "edges" else None,
                             options=mesh_ops.Options(poll_s=poll), out=io.StringIO(),
                             err=io.StringIO(), clock=clock, sleep=clock.sleep)
        assert code == 0, code
        out[f"roll {tier}"] = budget(ops.log)
    return out


class Budget(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_poll_interval_is_a_second_by_default(self):
        self.assertEqual(mesh_ops.POLL_S, 1.0)
        self.assertEqual(mesh_ops.Options().poll_s, 1.0)

    def test_every_gate_and_wait_asks_a_node_at_most_once_a_poll_interval(self):
        for name, figures in scenarios(self.tmp).items():
            self.assertLessEqual(figures["max_per_node_window"], 1, (name, figures))

    def test_mesh_local_is_read_only_where_node_status_lacks_the_counters(self):
        found = scenarios(self.tmp)
        # The forwarder gate and the rolls read /api/node only.
        for name in ("ingress gate, refusing 30 s", "roll edges", "roll forwarders"):
            self.assertEqual(set(found[name]["by_path"]) - {"/api/node", "/api/assignment",
                                                             "/api/control/drain-node",
                                                             "/api/control/resume-node"},
                             set(), name)
        self.assertNotIn("/api/assignment", found["ingress gate, refusing 30 s"]["by_path"])
        # The contributor gate: the two ingresses' plane counters on its
        # first and its deciding poll only, never beside their /api/node.
        gate = found["contributor gate, passing"]["by_path"]
        self.assertLessEqual(gate.get("/api/mesh/local", 0), 4, gate)
        ready = found["contributor readiness"]["by_path"]
        self.assertLessEqual(ready.get("/api/mesh/local", 0), 4, ready)

    def test_counters_on_node_status_need_no_mesh_local(self):
        # Once av-mesh serves `relay_counters` on /api/node (the field this
        # work asks for), the contributor tools read /api/node alone.
        clock = Clock()
        sim = ContribSim(clock, self.tmp / "counters")
        original = sim.doc

        def doc(name):
            document = original(name)
            if document is not None and name in cc.ingresses(mesh_ops.Lab(sim.nodes())):
                document["relay_counters"] = dict(sim.plane[name])
            return document
        sim.doc = doc
        ops = CountingOps(sim, clock)
        ok, why, _evidence = cc.contrib_gate(ops, "contrib-lon", timeout_s=30.0, clock=clock,
                                             sleep=clock.sleep, change=False)
        self.assertTrue(ok, why)
        # The first poll reads each ingress's /api/mesh/local once, which
        # shows that its /api/node carries the counters; never again.
        local = [(node, at) for at, node, path in ops.log if path == "/api/mesh/local"]
        self.assertEqual(sorted(node for node, _at in local), ["lon-dist-a", "lon-dist-b"])
        self.assertEqual({at for _node, at in local}, {ops.log[0][0]})
        def rising(name):
            document = doc(name)
            if document is not None and name == "lon-dist-a":
                document["relay_counters"]["duplicate_mismatch"] = int(clock() * 10)
            return document
        sim.doc = rising
        ok, why, _evidence = cc.contrib_gate(ops, "contrib-lon", timeout_s=5.0, clock=clock,
                                             sleep=clock.sleep, change=False)
        self.assertFalse(ok)
        self.assertIn("duplicate_mismatch grew at lon-dist-a", why)

    def test_a_refusing_gate_backs_off(self):
        clock = Clock()
        sim = ContribSim(clock, self.tmp / "backoff")
        sim.row[("lon-edge-1", "lon-dist-b")] = {"skipped_10s": 50}
        ops = CountingOps(sim, clock)
        gate = mesh_ops.forwarder_gate(ops, "lon-dist-a", timeout_s=30.0, clock=clock,
                                       sleep=clock.sleep)
        self.assertFalse(gate.ok)
        # 1 s, then longer, up to POLL_MAX_S: fewer than 30 polls in 30 s.
        self.assertLess(gate.polls, 15)
        self.assertGreaterEqual(gate.polls, 30 // mesh_ops.POLL_MAX_S)

    def test_nothing_is_asked_between_a_forwarder_gate_and_its_activation(self):
        clock = Clock()
        sim = ContribSim(clock, self.tmp / "quiet")
        ops = CountingOps(sim, clock)
        seen = {}
        gate = mesh_ops.forwarder_gate
        original = sim.activate

        def counted_gate(*args, **kwargs):
            result = gate(*args, **kwargs)
            seen["gate"] = len(ops.log)
            return result

        def activate(name, not_after_ms, rollback=False):
            seen.setdefault("activate", len(ops.log))
            return original(name, not_after_ms, rollback)
        sim.activate = activate
        with mock.patch.object(mesh_ops, "forwarder_gate", counted_gate):
            code = mesh_ops.roll(ops, "forwarders", nodes=["new-dist-b"], out=io.StringIO(),
                                 err=io.StringIO(), clock=clock, sleep=clock.sleep)
        self.assertEqual(code, 0)
        # Nothing: the activation takes the boot id from the gate's last
        # poll.
        self.assertEqual([(node, path) for _at, node, path in
                          ops.log[seen["gate"]:seen["activate"]]], [])


class Nice(unittest.TestCase):
    def test_renice_lowers_the_priority_once_and_never_raises_it(self):
        calls = []
        current = {"value": 0}

        def nice(delta):
            calls.append(delta)
            current["value"] += delta
            return current["value"]
        self.assertEqual(mesh_ops.renice(10, nice=nice), 10)
        self.assertEqual(mesh_ops.renice(10, nice=nice), 10)
        self.assertEqual(mesh_ops.renice(5, nice=nice), 10)
        self.assertEqual(mesh_ops.renice(0, nice=nice), 10)
        self.assertEqual([call for call in calls if call], [10])


# ------------------------------------------------- the kept-open helper

class Counting(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections: list = []

    def setup(self):
        super().setup()
        Counting.connections.append(self.client_address)

    def do_GET(self):  # noqa: N802 - http.server's name
        body = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class KeptOpen(unittest.TestCase):
    """scripts/ns_http.py: one helper process for the lab's tools, in the
    lab's namespace on Linode's terms (C 3.5: kept-open connections), in
    place of a curl process and a TLS handshake for every request."""

    def test_one_connection_carries_every_request_and_answers_come_back_in_order(self):
        import ns_http
        Counting.connections = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Counting)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        client = ns_http.Client([sys.executable, str(HERE / "ns_http.py"), "--plain"])
        self.addCleanup(client.close)
        answers = [client.request("127.0.0.1", port, "GET", f"/api/node?n={index}")
                   for index in range(20)]
        self.assertEqual([answer[0] for answer in answers], [200] * 20)
        self.assertEqual(answers[7][1], {"path": "/api/node?n=7"})
        self.assertEqual(len(Counting.connections), 1)
        self.assertEqual(client.requests, 20)

    def test_a_node_that_does_not_answer_is_none_and_a_dead_helper_is_said(self):
        import ns_http
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        client = ns_http.Client([sys.executable, str(HERE / "ns_http.py"), "--plain"])
        self.addCleanup(client.close)
        self.assertEqual(client.request("127.0.0.1", port, "GET", "/api/node", timeout=1.0),
                         (None, None))
        client.process.kill()
        client.process.wait()
        with self.assertRaises(ns_http.HelperGone):
            client.request("127.0.0.1", port, "GET", "/api/node", timeout=1.0)

    def test_the_helper_runs_under_the_given_prefix(self):
        import ns_http
        client = ns_http.Client(["env", "NS_HTTP_TEST=1", sys.executable,
                                 str(HERE / "ns_http.py"), "--plain"])
        self.addCleanup(client.close)
        self.assertIsNone(client.process.poll())
        self.assertIsInstance(client.process, subprocess.Popen)


if __name__ == "__main__":
    unittest.main()
