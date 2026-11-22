#!/usr/bin/env python3
"""List, and with --delete remove, the comparison's rooms on LiveKit Cloud:
only rooms whose names begin `needletail-compare`. The credentials are read
from ~/wavey.ai/LIVEKIT_CLOUD.env and never printed.

  livekit-cloud-rooms.py [--delete]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import pathlib
import time
import urllib.request

ENV = pathlib.Path.home() / "wavey.ai" / "LIVEKIT_CLOUD.env"
PREFIX = "needletail-compare"


def env() -> dict:
    out = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip().removeprefix("export ").strip()] = value.strip().strip("\"'")
    return out


def token(key: str, secret: str) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = b64(json.dumps({"iss": key, "nbf": now - 10, "exp": now + 300,
                             "video": {"roomList": True, "roomCreate": True}}).encode())
    signature = hmac.new(secret.encode(), f"{header}.{claims}".encode(), hashlib.sha256).digest()
    return f"{header}.{claims}.{b64(signature)}"


def call(base: str, jwt: str, method: str, body: dict) -> dict:
    request = urllib.request.Request(f"{base}/twirp/livekit.RoomService/{method}", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read() or b"{}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    values = env()
    base = values["LIVEKIT_URL"].replace("wss://", "https://").replace("ws://", "http://").rstrip("/")
    jwt = token(values["LIVEKIT_API_KEY"], values["LIVEKIT_API_SECRET"])
    rooms = [room.get("name", "") for room in call(base, jwt, "ListRooms", {}).get("rooms", [])]
    ours = [name for name in rooms if name.startswith(PREFIX)]
    print(f"rooms on the project: {len(rooms)}; of the comparison: {len(ours)}")
    for name in ours:
        print("  ", name)
        if args.delete:
            call(base, jwt, "DeleteRoom", {"room": name})
            print("     deleted")


if __name__ == "__main__":
    main()
