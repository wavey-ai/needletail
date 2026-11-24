#!/usr/bin/env python3
"""Session tags for audio listeners placed against the 4K edge of a region.

The referee runs audio listeners inside each 4K step (compare-run.py
--audio pcm) and wants one whose two legs include the edge that serves the
4K viewer and one whose legs avoid it (notes-stage2.md, "Owed to the
referee"). A two-path listener holds 2 of the region's 3 serving edges; which
two is the entry's assignment for its session tag. This asks the entry, as
the client does, for the assignment of candidate tags until both kinds come
out, and prints them. It subscribes to nothing.

    find-session-tags.py --entry https://ENTRY:19444 --stream 4001 --region london
                         (--edge lon-edge-2 | --viewer-tag TAG [--viewer-stream 1])
                         [--prefix w10-4k-lon] [--tries 200] [--json OUT]

The request is the client's own (av-mesh src/client_udp.rs, `ASSIGN`): one
datagram to the entry's client UDP port (19446 unless --udp-port), padded to
1,200 bytes, asking two legs and a spare (p=2 s=1, the client's defaults;
--paths 1 for the one-path control) for the stream and the tag; the entry
answers TOKEN and ASSIGNED, whose first p caches are the legs granted.

What it leaves on the entry: nothing that places anyone. An `ASSIGN`
without the entry's token is unproven: the entry answers it with the
assignment that /api/assignment gives but charges no cache and remembers no
session (av-mesh main.rs `udp_assignment` calls `client_assignment` with
remember = false; `assign_reader` charges only when `remember`). The charge
and the memory come only from the client's confirmation, an `ASSIGN` with
the token (`confirm_udp_assignment`), or from an HTTPS /api/assignment that
names `sid`; this tool sends neither (it reads the TOKEN and drops it). The
entry counts the answer in its client_udp `assignments` and `tokens_sent`
counters, and a mover (a request with `skip`) would be logged for
apportionment: this tool never sends `skip`.

The answer is the assignment at the moment it is asked: the entry orders
the region's edges by rendezvous on the tag and by their loads. Ask just
before the step, start the listener with the tag at once, and read the legs
it really held from its own report (the record says which; video-audio-score
reads them from the reports' caches and legs). A tag is test-only: the
record states that the listeners' tags were chosen.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import socket
import sys
import time
import urllib.parse

PREFIX = "NTU/1 "
ASSIGN_REQUEST_BYTES = 1_200
CLIENT_UDP_PORT = 19_446
SHORT = {"london": "lon", "newyork": "new", "tokyo": "tok", "sydney": "syd"}


def word(text: str) -> str:
    """A word of a control datagram: graphic ASCII, '-' for the rest."""
    out = "".join(c if "!" <= c <= "~" else "-" for c in text)
    return out or "-"


def assign_request(stream: int, nonce: int, session: str, paths: int = 2,
                   spare: bool = True) -> bytes:
    """An unconfirmed ASSIGN, as the client writes it for a join (no skip,
    no current, no hold, no token), padded to ASSIGN_REQUEST_BYTES."""
    text = f"{PREFIX}ASSIGN {stream} {nonce:016x} {word(session)} - -"
    if paths != 1:
        text += f" p={min(paths, 2)}"
    if spare:
        text += " s=1"
    pad = max(1, ASSIGN_REQUEST_BYTES - (len(text) + 1))
    return (text + " " + "." * pad).encode()


def is_field(item: str) -> bool:
    key, sep, _value = item.partition("=")
    return bool(sep) and 1 <= len(key) <= 8 and key.isascii() and key.isalpha() and key.islower()


def parse_assigned(data: bytes, stream: int, nonce: int) -> dict | None:
    """An ASSIGNED datagram for this stream and nonce: {"port", "caches":
    [{"node_id", "url", "failover"}], "legs", "spare", "why"}; None for any
    other datagram (a TOKEN, another request's answer)."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not text.startswith(PREFIX + "ASSIGNED "):
        return None
    words = text[len(PREFIX):].split()
    if len(words) < 4 or words[1] != str(stream):
        return None
    try:
        if int(words[2], 16) != nonce:
            return None
    except ValueError:
        return None
    port = None if words[3] == "-" else int(words[3])
    caches, rest = [], words[4:]
    while rest and not is_field(rest[0]):
        parts = rest.pop(0).split("|")
        if len(parts) != 3 or parts[2] not in ("0", "1"):
            return None
        caches.append({"node_id": parts[0], "url": parts[1], "failover": parts[2] == "1"})
    fields = dict(item.split("=", 1) for item in rest if is_field(item))
    return {"port": port, "caches": caches, "legs": int(fields.get("p", 1)),
            "spare": fields.get("s") == "1", "why": fields.get("why")}


def entry_address(entry: str, udp_port: int | None) -> tuple[str, int]:
    """The entry's host from its URL and its client UDP port."""
    parsed = urllib.parse.urlparse(entry if "://" in entry else f"https://{entry}")
    if not parsed.hostname:
        raise ValueError(f"no host in the entry {entry!r}")
    return parsed.hostname, udp_port or CLIENT_UDP_PORT


class Entry:
    """One UDP socket to the entry; ask() is one request and its answer."""

    def __init__(self, host: str, port: int, timeout_s: float = 0.5, attempts: int = 3,
                 pace_s: float = 0.02):
        self.address = (host, port)
        self.timeout_s, self.attempts, self.pace_s = timeout_s, attempts, pace_s
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sent = 0

    def close(self) -> None:
        self.sock.close()

    def ask(self, stream: int, session: str, paths: int = 2, spare: bool = True) -> dict | None:
        for _attempt in range(self.attempts):
            nonce = secrets.randbits(64)
            self.sock.sendto(assign_request(stream, nonce, session, paths, spare), self.address)
            self.sent += 1
            deadline = time.monotonic() + self.timeout_s
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.sock.settimeout(left)
                try:
                    data, _peer = self.sock.recvfrom(2048)
                except socket.timeout:
                    break
                answer = parse_assigned(data, stream, nonce)
                if answer is not None:
                    time.sleep(self.pace_s)
                    return answer
        return None


def legs_of(answer: dict) -> list[str]:
    """The legs granted: the first `legs` caches that are this region's."""
    return [cache["node_id"] for cache in answer["caches"][:answer["legs"]]
            if not cache["failover"]]


def find(entry: Entry, stream: int, edge: str, prefix: str, tries: int, paths: int = 2,
         spare: bool = True, start: int = 0) -> dict:
    """Ask the assignment of `prefix`-N for N from `start` until one tag's
    legs include `edge` and another's avoid it, or `tries` are asked."""
    out = {"with_edge": None, "without_edge": None, "asked": 0, "unanswered": 0, "short": 0,
           "seen": {}}
    for index in range(start, start + tries):
        tag = f"{prefix}-{index}"
        answer = entry.ask(stream, tag, paths, spare)
        out["asked"] += 1
        if answer is None:
            out["unanswered"] += 1
            continue
        legs = legs_of(answer)
        if len(legs) < paths:
            out["short"] += 1
        key = ",".join(sorted(legs))
        out["seen"][key] = out["seen"].get(key, 0) + 1
        found = {"tag": tag, "legs": legs, "spare": (answer["caches"][answer["legs"]]["node_id"]
                                                   if answer["spare"] and len(answer["caches"])
                                                   > answer["legs"] else None),
                 "why": answer["why"]}
        if edge in legs and out["with_edge"] is None:
            out["with_edge"] = found
        elif edge not in legs and legs and out["without_edge"] is None:
            out["without_edge"] = found
        if out["with_edge"] and out["without_edge"]:
            break
    return out


def viewer_edge(entry: Entry, stream: int, tag: str) -> dict | None:
    """The edge of a viewer by its tag: the first leg the entry gives it (a
    4K stream is over the two-path rate: one leg, why=bitrate). For a
    viewer that is running, the entry's memory of its confirmed legs comes
    first, so this is the edge it holds."""
    answer = entry.ask(stream, tag, 2, True)
    if answer is None:
        return None
    legs = legs_of(answer)
    return {"tag": tag, "edge": legs[0] if legs else None, "legs": legs, "why": answer["why"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--entry", required=True, help="the region's entry, https://HOST:19444")
    parser.add_argument("--udp-port", type=int, default=None,
                        help=f"the entry's client UDP port (default {CLIENT_UDP_PORT})")
    parser.add_argument("--stream", type=int, required=True,
                        help="the audio stream of the listeners (4001 PCM, 3001 Opus)")
    parser.add_argument("--region", required=True, help="london, newyork, tokyo or sydney")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--edge", help="the edge that serves the 4K viewer")
    which.add_argument("--viewer-tag", help="the 4K viewer's session tag, to look its edge up")
    parser.add_argument("--viewer-stream", type=int, default=1, help="the 4K stream (1)")
    parser.add_argument("--prefix", default=None,
                        help="candidate tags are PREFIX-0, PREFIX-1, ... (default ft-<region>)")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--tries", type=int, default=200)
    parser.add_argument("--paths", type=int, default=2, choices=(1, 2),
                        help="legs the listener asks (2, the client's default)")
    parser.add_argument("--no-spare", action="store_true", help="as the client's --spare false")
    parser.add_argument("--timeout-s", type=float, default=0.5)
    parser.add_argument("--json", default=None, help="write the result here too")
    args = parser.parse_args(argv)
    host, port = entry_address(args.entry, args.udp_port)
    short = SHORT.get(args.region, args.region)
    entry = Entry(host, port, timeout_s=args.timeout_s)
    try:
        viewer = None
        edge = args.edge
        if args.viewer_tag:
            viewer = viewer_edge(entry, args.viewer_stream, args.viewer_tag)
            if not viewer or not viewer["edge"]:
                print(f"no assignment for the viewer tag {args.viewer_tag} on stream "
                      f"{args.viewer_stream} from {host}:{port}", file=sys.stderr)
                return 2
            edge = viewer["edge"]
        found = find(entry, args.stream, edge, args.prefix or f"ft-{short}", args.tries,
                     args.paths, not args.no_spare, args.start)
    finally:
        entry.close()
    foreign = sorted({leg for item in (found["with_edge"], found["without_edge"]) if item
                      for leg in item["legs"] if not leg.startswith(f"{short}-")})
    result = {"schema": "needletail.find-session-tags.v1", "entry": f"{host}:{port}",
              "region": args.region, "stream": args.stream, "edge": edge, "viewer": viewer,
              "paths": args.paths, **found,
              "charged": "nothing: unconfirmed ASSIGN requests only (no token sent back, no "
                         "sid, no skip)",
              "note": "the assignment when asked; start the listener at once and read its "
                      "legs from its own report"}
    if foreign:
        result["warning"] = f"legs outside {args.region}: {', '.join(foreign)}"
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1) + "\n")
    for kind in ("with_edge", "without_edge"):
        item = found[kind]
        print(f"{kind.replace('_', ' ')} {edge}: "
              + (f"{item['tag']} (legs {', '.join(item['legs'])}"
                 + (f", spare {item['spare']}" if item["spare"] else "") + ")" if item
                 else f"none in {found['asked']} tags"))
    print(f"asked {found['asked']}, unanswered {found['unanswered']}, short of legs "
          f"{found['short']}; leg pairs seen: "
          + ", ".join(f"{key or '-'} x{count}" for key, count in sorted(found["seen"].items())))
    if result.get("warning"):
        print("warning: " + result["warning"], file=sys.stderr)
    return 0 if found["with_edge"] and found["without_edge"] else 1


if __name__ == "__main__":
    sys.exit(main())
