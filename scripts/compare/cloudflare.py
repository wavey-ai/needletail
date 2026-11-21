#!/usr/bin/env python3
"""Create, list and delete what the comparison uses on Cloudflare.

It makes exactly one Realtime (SFU) app named `needletail-compare` and Stream
live inputs whose names begin `needletail-compare`, and it deletes only those.
Nothing else on the account is read or changed.

The account identifier and API token come from the environment or from
`~/wavey.ai/CLOUDFLARE_ACCOUNT.env`. They are never printed. The app secret
and the ingest keys that Cloudflare returns are kept in a state file under
`target/` (mode 600, outside git) and are never printed either.

  setup      create the app and the live inputs that do not exist yet
  status     list what exists under the comparison's names
  teardown   delete the live inputs, their recordings and the app; verify
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "target" / "compare" / "cloudflare-state.json"
ENV_FILE = ROOT.parent / "CLOUDFLARE_ACCOUNT.env"
PREFIX = "needletail-compare"
API = "https://api.cloudflare.com/client/v4"

LIVE_INPUTS = {
    # WHIP in, WHEP out. Cloudflare records nothing on this path.
    "needletail-compare-webrtc": {"recording": {"mode": "off"}},
    # RTMPS in, HLS and LL-HLS out. Playback needs recording mode automatic.
    "needletail-compare-hls": {
        "recording": {"mode": "automatic", "timeoutSeconds": 0},
        "preferLowLatency": True,
        "deleteRecordingAfterDays": 30,
    },
}


def credentials() -> tuple[str, str]:
    values = {key: os.environ.get(key, "") for key in
              ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")}
    if not all(values.values()) and ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            key, _, value = line.strip().removeprefix("export ").partition("=")
            if key in values:
                values[key] = value.strip().strip("\"'")
    if not all(values.values()):
        sys.exit("Cloudflare account identifier or token is missing")
    return values["CLOUDFLARE_ACCOUNT_ID"], values["CLOUDFLARE_API_TOKEN"]


def call(method: str, path: str, body: dict | None = None, allow: tuple[int, ...] = ()) -> dict:
    account, token = credentials()
    request = urllib.request.Request(
        f"{API}/accounts/{account}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "needletail-compare"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        if error.code in allow:
            return {"success": False, "status": error.code, "detail": detail}
        # Error bodies of these endpoints carry no secret.
        sys.exit(f"{method} {path.split('?')[0]} failed with HTTP {error.code}: {detail[:400]}")


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.is_file() else {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".next")
    temporary.touch(mode=0o600)
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(STATE)


def our_apps() -> list[dict]:
    return [app for app in call("GET", "/calls/apps").get("result") or []
            if app.get("name") == PREFIX]


def our_inputs() -> list[dict]:
    found = []
    for item in call("GET", "/stream/live_inputs").get("result") or []:
        if str((item.get("meta") or {}).get("name", "")).startswith(PREFIX):
            found.append(item)
    return found


def our_videos() -> list[dict]:
    """Recordings of our live inputs, and any video that carries our name."""
    found = {}
    for item in our_inputs():
        for video in call("GET", f"/stream/live_inputs/{item['uid']}/videos",
                          allow=(404,)).get("result") or []:
            found[video["uid"]] = video
    for video in call("GET", f"/stream?search={PREFIX}").get("result") or []:
        if str((video.get("meta") or {}).get("name", "")).startswith(PREFIX):
            found[video["uid"]] = video
    state = load_state()
    for uid in state.get("recordings", []):
        video = call("GET", f"/stream/{uid}", allow=(404,)).get("result")
        if video:
            found[uid] = video
    return list(found.values())


def setup(_: argparse.Namespace) -> None:
    state = load_state()
    if not our_apps() or "realtime" not in state:
        for app in our_apps():      # one we cannot use: its secret is shown once
            call("DELETE", f"/calls/apps/{app['uid']}")
        created = call("POST", "/calls/apps", {"name": PREFIX})["result"]
        state["realtime"] = {"app_id": created["uid"], "secret": created["secret"]}
        save_state(state)
        print(f"created Realtime app {PREFIX}")
    existing = {(item.get("meta") or {}).get("name"): item for item in our_inputs()}
    state.setdefault("live_inputs", {})
    for name, options in LIVE_INPUTS.items():
        if name in existing and name in state["live_inputs"]:
            continue
        if name in existing:
            call("DELETE", f"/stream/live_inputs/{existing[name]['uid']}")
        created = call("POST", "/stream/live_inputs", {"meta": {"name": name}, **options},
                       allow=(400, 402, 403))
        if not created.get("success", True) and "result" not in created:
            state["live_inputs"][name] = {"error": created.get("detail", "")[:600]}
            save_state(state)
            print(f"live input {name} was refused: HTTP {created.get('status')}: "
                  f"{created.get('detail', '')[:300]}")
            continue
        result = created["result"]
        state["live_inputs"][name] = {
            "uid": result["uid"],
            "rtmps_url": (result.get("rtmps") or {}).get("url"),
            "rtmps_key": (result.get("rtmps") or {}).get("streamKey"),
            "srt_url": (result.get("srt") or {}).get("url"),
            "srt_stream_id": (result.get("srt") or {}).get("streamId"),
            "srt_passphrase": (result.get("srt") or {}).get("passphrase"),
            "whip_url": (result.get("webRTC") or {}).get("url"),
            "whep_url": (result.get("webRTCPlayback") or {}).get("url"),
            "created": result.get("created"),
        }
        save_state(state)
        print(f"created live input {name}")
    status(_)


def status(_: argparse.Namespace) -> None:
    state = load_state()
    apps = our_apps()
    inputs = our_inputs()
    videos = our_videos()
    print(f"Realtime apps named {PREFIX}: {len(apps)}"
          + ("" if not apps else f" (created {apps[0].get('created')})"))
    print(f"Stream live inputs named {PREFIX}*: {len(inputs)}")
    for item in inputs:
        print(f"  {(item.get('meta') or {}).get('name')}  created {item.get('created')}  "
              f"recording {((item.get('recording') or {}).get('mode'))}")
    print(f"Stream recordings of those inputs: {len(videos)}")
    for video in videos:
        print(f"  {video.get('uid', '')[:8]}…  {video.get('duration')} s  "
              f"{(video.get('status') or {}).get('state')}")
    print(f"state file: {'present' if state else 'absent'}")


def teardown(_: argparse.Namespace) -> None:
    state = load_state()
    recordings = set(state.get("recordings", []))
    for video in our_videos():
        recordings.add(video["uid"])
    for item in our_inputs():
        call("DELETE", f"/stream/live_inputs/{item['uid']}", allow=(404,))
        print(f"deleted live input {(item.get('meta') or {}).get('name')}")
    for uid in sorted(recordings):
        call("DELETE", f"/stream/{uid}", allow=(404,))
        print(f"deleted recording {uid[:8]}…")
    for app in our_apps():
        call("DELETE", f"/calls/apps/{app['uid']}", allow=(404,))
        print(f"deleted Realtime app {PREFIX}")
    left = (len(our_apps()), len(our_inputs()),
            sum(1 for uid in recordings
                if call("GET", f"/stream/{uid}", allow=(404,)).get("result")))
    print(f"left on Cloudflare: {left[0]} apps, {left[1]} live inputs, {left[2]} recordings")
    if any(left):
        sys.exit("teardown is incomplete; run it again")
    if STATE.is_file():
        STATE.unlink()


def note_recordings(_: argparse.Namespace) -> None:
    """Remember the recordings that exist now, so that teardown deletes them
    even after their live input is gone."""
    state = load_state()
    known = set(state.get("recordings", []))
    known.update(video["uid"] for video in our_videos())
    state["recordings"] = sorted(known)
    save_state(state)
    print(f"{len(known)} recordings noted")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("setup", setup), ("status", status), ("teardown", teardown),
                          ("note-recordings", note_recordings)):
        sub.add_parser(name).set_defaults(handler=handler)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
