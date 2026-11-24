#!/usr/bin/env python3
"""find-session-tags.py against a fake entry: the client's ASSIGN on the
wire, the answer read, a tag with the 4K edge and one without, the viewer's
edge looked up by its tag, and nothing sent that would charge the entry (no
confirmation with its token, no SUB)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import pathlib
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("find_session_tags", HERE / "find-session-tags.py")
tags = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tags)

EDGES = ["lon-edge-1", "lon-edge-2", "lon-edge-3"]


class FakeEntry:
    """A UDP entry answering ASSIGN as av-mesh does: a padded request of 512
    bytes or more gets TOKEN then ASSIGNED (the region's edges ordered by a
    hash of the session, two legs and the spare when asked, the failover
    entry last; the 4K stream one leg, why=bitrate). Every datagram is
    kept."""

    def __init__(self, drop_first: int = 0, video_stream: int = 1):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.received: list[bytes] = []
        self.drop_first = drop_first
        self.video_stream = video_stream
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.running = True
        self.thread.start()

    def order(self, session: str) -> list[str]:
        return sorted(EDGES, key=lambda edge: hashlib.sha256(
            f"{session}/{edge}".encode()).hexdigest())

    def serve(self) -> None:
        self.sock.settimeout(0.1)
        while self.running:
            try:
                data, peer = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            self.received.append(data)
            if len(self.received) <= self.drop_first:
                continue
            words = data.decode().split()
            if words[1] != "ASSIGN" or len(data) < 512 or any(w.startswith("t=") for w in words):
                continue
            stream, nonce, session = int(words[2]), words[3], words[4]
            fields = dict(w.split("=", 1) for w in words[7:] if "=" in w)
            paths = int(fields.get("p", 1))
            spare = fields.get("s") == "1"
            why = None
            if stream == self.video_stream and paths > 1:
                paths, why = 1, "bitrate"
            order = self.order(session)
            caches = [f"{edge}|https://{edge}:19444|0" for edge in order]
            caches.append("new-dist-a|https://new-dist-a:19444|1")
            text = (f"NTU/1 ASSIGNED {stream} {nonce} 19446 " + " ".join(caches)
                    + (f" p={paths}" if paths != 1 else "") + (" s=1" if spare else "")
                    + (f" why={why}" if why else ""))
            self.sock.sendto(f"NTU/1 TOKEN {stream} 00000000deadbeef".encode(), peer)
            self.sock.sendto(text.encode(), peer)

    def close(self) -> None:
        self.running = False
        self.thread.join(timeout=2)
        self.sock.close()


class Wire(unittest.TestCase):
    def test_the_request_is_the_client_s_join(self):
        data = tags.assign_request(4001, 9, "w10-lon-7")
        self.assertEqual(len(data), tags.ASSIGN_REQUEST_BYTES)
        self.assertTrue(data.startswith(b"NTU/1 ASSIGN 4001 0000000000000009 w10-lon-7 - - "
                                        b"p=2 s=1 ...."), data[:80])
        self.assertTrue(data.endswith(b"."))
        one = tags.assign_request(4001, 9, "x", paths=1, spare=False)
        self.assertTrue(one.startswith(b"NTU/1 ASSIGN 4001 0000000000000009 x - - ...."))
        self.assertNotIn(b"t=", data)

    def test_the_answer_is_read_as_the_client_reads_it(self):
        text = (b"NTU/1 ASSIGNED 4001 0000000000000009 19446 lon-edge-2|https://a:19444|0 "
                b"lon-edge-3|https://b:19444|0 lon-edge-1|https://c:19444|0 "
                b"new-dist-a|https://d:19444|1 p=2 s=1")
        answer = tags.parse_assigned(text, 4001, 9)
        self.assertEqual(tags.legs_of(answer), ["lon-edge-2", "lon-edge-3"])
        self.assertTrue(answer["spare"])
        self.assertEqual(answer["port"], 19446)
        self.assertIsNone(tags.parse_assigned(text, 4001, 10))          # another nonce
        self.assertIsNone(tags.parse_assigned(text, 3001, 9))           # another stream
        self.assertIsNone(tags.parse_assigned(b"NTU/1 TOKEN 4001 00ff", 4001, 9))
        short = tags.parse_assigned(b"NTU/1 ASSIGNED 1 0000000000000009 - "
                                    b"lon-edge-1|u|0 why=bitrate", 1, 9)
        self.assertEqual((short["legs"], short["why"], short["port"]), (1, "bitrate", None))

    def test_the_entry_s_address(self):
        self.assertEqual(tags.entry_address("https://10.80.1.11:19444", None),
                         ("10.80.1.11", 19446))
        self.assertEqual(tags.entry_address("10.0.0.2", 29446), ("10.0.0.2", 29446))


class AgainstAFakeEntry(unittest.TestCase):
    def setUp(self):
        self.entry = FakeEntry()
        self.addCleanup(self.entry.close)

    def run_tool(self, *argv):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(out):
            path = pathlib.Path(directory) / "tags.json"
            code = tags.main(["--entry", "https://127.0.0.1:19444", "--udp-port",
                              str(self.entry.port), "--stream", "4001", "--region", "london",
                              "--json", str(path), *argv])
            result = json.loads(path.read_text())
        return code, out.getvalue(), result

    def test_one_tag_with_the_4k_edge_and_one_without(self):
        code, text, result = self.run_tool("--edge", "lon-edge-2", "--prefix", "w10-lon")
        self.assertEqual(code, 0, text)
        self.assertIn("lon-edge-2", result["with_edge"]["legs"])
        self.assertNotIn("lon-edge-2", result["without_edge"]["legs"])
        self.assertEqual(len(result["without_edge"]["legs"]), 2)
        # The fake orders by the tag as the entry's rendezvous does: asking
        # again gives the same legs.
        for kind in ("with_edge", "without_edge"):
            tag = result[kind]["tag"]
            self.assertTrue(tag.startswith("w10-lon-"))
            self.assertEqual(self.entry.order(tag)[:2], result[kind]["legs"])
        self.assertIn("with edge lon-edge-2: w10-lon-", text)
        self.assertIn("charged", result)

    def test_nothing_that_charges_the_entry_is_sent(self):
        self.run_tool("--edge", "lon-edge-1")
        self.assertTrue(self.entry.received)
        for data in self.entry.received:
            words = data.decode().split()
            self.assertEqual(words[:2], ["NTU/1", "ASSIGN"])
            self.assertEqual(len(data), tags.ASSIGN_REQUEST_BYTES)   # padded: unconfirmed
            self.assertFalse(any(w.startswith(("t=", "h=", "k=", "a=")) for w in words))
            self.assertEqual(words[5:7], ["-", "-"])                   # no skip, no current

    def test_the_viewer_s_edge_by_its_tag(self):
        viewer = "w10-nt-lon-viewuc"
        edge = self.entry.order(viewer)[0]
        code, text, result = self.run_tool("--viewer-tag", viewer)
        self.assertEqual(code, 0, text)
        self.assertEqual(result["viewer"]["edge"], edge)
        self.assertEqual(result["viewer"]["why"], "bitrate")
        self.assertEqual(result["edge"], edge)
        self.assertIn(edge, result["with_edge"]["legs"])
        self.assertNotIn(edge, result["without_edge"]["legs"])

    def test_a_lost_answer_is_asked_again(self):
        self.entry.drop_first = 1
        code, _text, result = self.run_tool("--edge", "lon-edge-3", "--tries", "50")
        self.assertEqual(code, 0)
        self.assertEqual(result["unanswered"], 0)

    def test_one_path_control(self):
        code, _text, result = self.run_tool("--edge", "lon-edge-3", "--paths", "1",
                                            "--no-spare")
        self.assertEqual(code, 0)
        self.assertEqual(result["with_edge"]["legs"], ["lon-edge-3"])
        self.assertEqual(len(result["without_edge"]["legs"]), 1)


class NoEntry(unittest.TestCase):
    def test_an_entry_that_never_answers(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        out = io.StringIO()
        with redirect_stdout(out):
            code = tags.main(["--entry", "127.0.0.1", "--udp-port", str(sock.getsockname()[1]),
                              "--stream", "4001", "--region", "london", "--edge", "lon-edge-1",
                              "--tries", "2", "--timeout-s", "0.05"])
        self.assertEqual(code, 1)
        self.assertIn("unanswered 2", out.getvalue())


if __name__ == "__main__":
    unittest.main()
