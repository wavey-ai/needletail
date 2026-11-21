#!/usr/bin/env python3
"""Prepare the comparison lab: tools on the viewers, clips on the source,
and the self-hosted LiveKit servers.

The lab is the small-instance Linode lab of
deploy/compare-cloudflare/topology-compare.json, with Needletail already
deployed by scripts/linode-small-mesh-deploy.py.

  prepare    install the probes, the netem helper and the clip indexes on
             every viewer host; Chrome and the player on the *-view hosts;
             ffmpeg and the clips on lon-src
  livekit    install livekit-server on the *-lk Nanodes: one standalone SFU
             on each (port 7880) and one member of a multi-region set that
             shares the Redis of lk-redis (port 7890); the `lk` CLI on viewers
  audio      the audio files, their indexes and Needletail's DAW source
  secrets    copy what the source and the viewers need of the Cloudflare
             state (app secret, ingest URLs) to lon-src and the viewers, in
             files of mode 600; the account token never leaves this host
  status     versions and reachability
"""

from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import shlex
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import small_mesh_common as common  # noqa: E402

ROOT = common.ROOT
COMPARE = ROOT / "target" / "compare"
MEDIA = pathlib.Path(__import__("os").environ.get("COMPARE_MEDIA", str(ROOT / "target" / "compare-media")))
CLIPS = MEDIA / "clips2"       # the encodes whose video is close to the nominal rate
LIVEKIT_VERSION = "1.13.11"
LK_CLI_VERSION = "2.20.1"
REMOTE = "/var/lib/compare"
RUNGS = ["1080p6", "2160p5", "2160p8", "2160p16", "2160p25", "2160p40", "2160p80", "2160p120", "2160p160",
         "2160p240", "2160p320"]
SHORT = {"london": "lon", "newyork": "new", "tokyo": "tok", "sydney": "syd"}
PLACES = {"london": (51.5074, -0.1278), "newyork": (40.7128, -74.0060),
          "tokyo": (35.6762, 139.6503), "sydney": (-33.8688, 151.2093)}


def state() -> dict:
    return common.load_state()


def node(name: str) -> dict:
    return state()["nodes"][name]


def named(suffix: str) -> list[dict]:
    return sorted((item for item in state()["nodes"].values() if item["name"].endswith(suffix)),
                  key=lambda item: item["name"])


def viewers() -> list[dict]:
    return named("-view") + named("-nano")


def livekit_state() -> dict:
    path = COMPARE / "livekit-selfhost.json"
    if path.is_file():
        return json.loads(path.read_text())
    COMPARE.mkdir(parents=True, exist_ok=True)
    data = {"api_key": "API" + secrets.token_hex(6), "api_secret": secrets.token_urlsafe(36),
            "redis_password": secrets.token_urlsafe(24)}
    path.touch(mode=0o600)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


TOOLS = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
mkdir -p /var/lib/compare/index /var/lib/compare/runs
cat >/etc/sysctl.d/61-compare.conf <<'EOF'
net.core.rmem_max=67108864
net.core.wmem_max=67108864
net.core.rmem_default=8388608
net.core.wmem_default=8388608
net.core.netdev_max_backlog=8192
EOF
sysctl -q --system
modprobe sch_netem; modprobe ifb numifbs=1 2>/dev/null || true
chronyc -c tracking | awk -F, '{printf "clock offset %.3f ms\n", $5*1000}'
"""

PLAYER = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v google-chrome >/dev/null 2>&1 || ! command -v node >/dev/null 2>&1; then
  for attempt in 1 2 3; do
    apt-get update -q >/dev/null 2>&1 && apt-get install -y -q nodejs npm ffmpeg >/dev/null 2>&1 && break
    sleep 5
  done
  curl -sSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y -q /tmp/chrome.deb >/dev/null 2>&1
fi
cd /opt/compare-player && npm install --omit=dev --no-audit --no-fund >/dev/null 2>&1
google-chrome --version; node --version
"""


def prepare(_: argparse.Namespace) -> None:
    hosts = viewers() + [node("lon-src")]
    binaries = [str(ROOT / "target" / "ntcmp"), str(ROOT / "target" / "ntcmp-lk")]
    indexes = [str(CLIPS / f"{rung}.index.json") for rung in RUNGS]
    for path in binaries + indexes:
        if not pathlib.Path(path).is_file():
            sys.exit(f"missing: {path}")

    def tools(item: dict) -> str:
        out = common.ssh_text(item, TOOLS, timeout=300)
        common.rsync_to(item, binaries, "/usr/local/bin/")
        common.rsync_to(item, [str(ROOT / "scripts" / "compare" / "netem.sh")],
                        "/usr/local/bin/compare-netem")
        common.rsync_to(item, indexes, f"{REMOTE}/index/")
        return out

    for name, out in sorted(common.parallel(hosts, tools).items()):
        print(f"{name:<10} {out}")

    def player(item: dict) -> str:
        common.ssh(item, "mkdir -p /opt/compare-player")
        common.rsync_to(item, [str(ROOT / "scripts" / "compare" / "player") + "/"],
                        "/opt/compare-player/")
        return common.ssh_text(item, PLAYER, timeout=900).replace("\n", "  ")

    for name, out in sorted(common.parallel(named("-view"), player).items()):
        print(f"{name:<10} {out}")
    source = node("lon-src")
    print("lon-src   ", common.ssh_text(source, (
        "export DEBIAN_FRONTEND=noninteractive; command -v ffmpeg >/dev/null 2>&1 || "
        "(apt-get update -q >/dev/null 2>&1; apt-get install -y -q ffmpeg >/dev/null 2>&1); "
        f"mkdir -p {REMOTE}/clips; ffmpeg -version | head -1"), timeout=900))


def clips(args: argparse.Namespace) -> None:
    """Copy the clips from the host that made them to lon-src, through this host."""
    source = node("lon-src")
    base = ["ssh", *common.ssh_base()]
    wanted = " ".join(RUNGS) + " audio.opus audio.aac audio.s24le media.json"
    common.ssh(source, f"rm -rf {REMOTE}/clips")
    send = subprocess.Popen(
        base + [f"root@{args.from_host}", f"cd {args.from_dir} && tar cf - {wanted}"],
        stdout=subprocess.PIPE)
    receive = subprocess.run(
        base + [f"root@{source['public_ipv4']}", f"mkdir -p {REMOTE}/clips && tar xf - -C {REMOTE}/clips"],
        stdin=send.stdout)
    send.wait()
    if send.returncode or receive.returncode:
        sys.exit("copying the clips failed")
    print(common.ssh_text(source, f"du -sh {REMOTE}/clips; ls {REMOTE}/clips"))


LIVEKIT_INSTALL = r"""
set -e
if [ ! -x /usr/local/bin/livekit-server ]; then
  curl -sSL -o /tmp/livekit.tgz https://github.com/livekit/livekit/releases/download/v%(version)s/livekit_%(version)s_linux_amd64.tar.gz
  tar -xzf /tmp/livekit.tgz -C /usr/local/bin livekit-server
fi
mkdir -p /etc/livekit
for name in single multi; do
cat >/etc/systemd/system/livekit-$name.service <<EOF
[Unit]
Description=LiveKit SFU ($name) for the network comparison
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/livekit-server --config /etc/livekit/$name.yaml
Restart=always
RestartSec=2
LimitNOFILE=500000

[Install]
WantedBy=multi-user.target
EOF
done
systemctl daemon-reload
systemctl enable livekit-single.service livekit-multi.service >/dev/null 2>&1
systemctl restart livekit-single.service livekit-multi.service
sleep 3
systemctl is-active livekit-single.service livekit-multi.service | tr '\n' ' '
/usr/local/bin/livekit-server --version
"""


def livekit_config(item: dict, keys: dict, multi: bool, redis: dict) -> str:
    """LiveKit's documented production shape: UDP port range open, the public
    address advertised, TURN off (every host has a public address), and for the
    multi-region set a shared Redis with the region-aware node selector."""
    base = 7890 if multi else 7880
    ports = (55000, 59999) if multi else (50000, 54999)
    lines = [
        f"port: {base}",
        "rtc:",
        f"  tcp_port: {base + 1}",
        f"  port_range_start: {ports[0]}",
        f"  port_range_end: {ports[1]}",
        "  use_external_ip: false",
        f"  node_ip: {item['public_ipv4']}",
        "keys:",
        f"  {keys['api_key']}: {keys['api_secret']}",
        "turn:",
        "  enabled: false",
        "logging:",
        "  level: info",
        f"prometheus_port: {base + 9}",
        f"region: {item['site']}",
    ]
    if multi:
        lines += [
            "redis:",
            f"  address: {redis['public_ipv4']}:6379",
            f"  password: {keys['redis_password']}",
            "node_selector:",
            "  kind: regionaware",
            "  regions:",
        ]
        for name, (latitude, longitude) in PLACES.items():
            lines += [f"    - name: {name}", f"      lat: {latitude}", f"      lon: {longitude}"]
    return "\n".join(lines) + "\n"


def livekit(_: argparse.Namespace) -> None:
    keys = livekit_state()
    redis = node("lk-redis")
    print("lk-redis  ", common.ssh_text(redis, (
        "export DEBIAN_FRONTEND=noninteractive; command -v redis-server >/dev/null 2>&1 || "
        "(apt-get update -q >/dev/null 2>&1; apt-get install -y -q redis-server >/dev/null 2>&1); "
        "cat >/etc/redis/redis.conf <<'EOF'\n"
        "bind 0.0.0.0\nprotected-mode yes\nport 6379\n"
        f"requirepass {keys['redis_password']}\nsave \"\"\nappendonly no\ndir /var/lib/redis\nEOF\n"
        "chown redis:redis /etc/redis/redis.conf; chmod 640 /etc/redis/redis.conf; "
        "systemctl restart redis-server; sleep 1; systemctl is-active redis-server; "
        "redis-server --version | cut -d' ' -f3"), timeout=600).replace("\n", " "))

    def install(item: dict) -> str:
        common.ssh(item, "mkdir -p /etc/livekit")
        for name, multi in (("single", False), ("multi", True)):
            common.ssh(item, f"umask 077; cat >/etc/livekit/{name}.yaml",
                       stdin=livekit_config(item, keys, multi, redis).encode())
        return common.ssh_text(item, LIVEKIT_INSTALL % {"version": LIVEKIT_VERSION},
                               timeout=600).replace("\n", " ")

    for name, out in sorted(common.parallel(named("-lk"), install).items()):
        print(f"{name:<10} {out}")

    def cli(item: dict) -> str:
        return common.ssh_text(item, (
            "[ -x /usr/local/bin/lk ] || (curl -sSL -o /tmp/lk.tgz https://github.com/livekit/"
            f"livekit-cli/releases/download/v{LK_CLI_VERSION}/lk_{LK_CLI_VERSION}_linux_amd64.tar.gz"
            " && tar -xzf /tmp/lk.tgz -C /usr/local/bin lk); lk --version"), timeout=300)

    for name, out in sorted(common.parallel(named("-view") + [node("lon-src")], cli).items()):
        print(f"{name:<10} {out}")


def push_secrets(_: argparse.Namespace) -> None:
    cloudflare = json.loads((COMPARE / "cloudflare-state.json").read_text())
    keys = livekit_state()
    inputs = cloudflare["live_inputs"]
    viewer = {
        "CF_REALTIME_APP_ID": cloudflare["realtime"]["app_id"],
        "CF_REALTIME_APP_SECRET": cloudflare["realtime"]["secret"],
        "CF_WHEP_URL": inputs["needletail-compare-webrtc"]["whep_url"],
        "CF_HLS_UID": inputs["needletail-compare-hls"]["uid"],
        "LK_SELF_KEY": keys["api_key"], "LK_SELF_SECRET": keys["api_secret"],
    }
    source = dict(viewer)
    source.update({
        "CF_WHIP_URL": inputs["needletail-compare-webrtc"]["whip_url"],
        "CF_RTMPS_URL": inputs["needletail-compare-hls"]["rtmps_url"],
        "CF_RTMPS_KEY": inputs["needletail-compare-hls"]["rtmps_key"],
    })

    def write(item: dict, values: dict) -> None:
        text = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items())
        common.ssh(item, f"mkdir -p {REMOTE}; umask 077; cat >{REMOTE}/env", stdin=text.encode())

    common.parallel(viewers(), lambda item: write(item, viewer))
    write(node("lon-src"), source)
    print(f"wrote {REMOTE}/env on {len(viewers())} viewers and lon-src")


FIXTURE = r"""
import hashlib, json
src = open("/var/lib/compare/audio/audio.s24le", "rb").read()
loops = 30
frames = len(src) // 6 * loops
digest = hashlib.sha256()
with open("source-track-00.s24le", "wb") as out:
    for _ in range(loops):
        out.write(src)
        digest.update(src)
sha = digest.hexdigest()
meta = {"bits_per_sample": 24, "bytes_per_frame": 6, "canonical_source_track_count": 1, "channels": 2,
        "complete_project_cycles": loops, "duration_seconds": float(frames // 48000),
        "final_partial_cycle_frames": 0, "manifest_path": "manifest.sha256",
        "project_cycle_frames": len(src) // 6, "sample_format": "s24le", "sample_rate_hz": 48000,
        "schema": "needletail.prepared-pcm-fixtures.v2", "target_bytes_per_track": len(src) * loops,
        "target_frames": frames, "track_count": 1, "tracks": [{
            "canonical_source_index": 0, "index": 0, "output_bytes": len(src) * loops,
            "output_path": "source-track-00.s24le", "output_sha256": sha, "reuses_output_track": None,
            "silence_frames_per_cycle": 0, "source_frames": len(src) // 6,
            "source_name": "needletail-compare-audio.s24le", "source_pcm_bytes": len(src),
            "source_wav_bytes": len(src) + 44, "source_wav_sha256": hashlib.sha256(src).hexdigest()}]}
json.dump(meta, open("metadata.json", "w"), indent=2)
open("manifest.sha256", "w").write(sha + "  source-track-00.s24le\n")
print("DAW fixture of", frames // 48000, "s")
"""


# The ntcmp indexes of the Needletail lanes: PCM from the AEP1 sender by
# fingerprint in blocks of one epoch (48, 24, 12 samples); the Opus that the
# sender encodes (120 samples), and the DAW source's Opus and FLAC, by part
# number, part N being epoch N of the loop (offset as daw_index says). (name,
# ntcmp audio-index arguments after the output path.)
# The DAW source as compare-audio.py starts it (daw-test-source
# --buffer-frames, --opus-frame-samples, --lossless-epoch-samples): the
# low-latency defaults, given explicitly so that an older build is not
# measured with the wrong index. It plays one host buffer of silence before
# the loop, so its audio is `buffer_frames` behind the loop, and its Opus is
# the encoder's look-ahead (120 samples, 2.5 ms) later still. Its lanes'
# indexes number part N as epoch N of the loop, offset by that: the indexes
# of the first runs were 240-sample epochs, offsets 512 (FLAC) and 632 (Opus)
# behind 512-sample buffers.
NT_DAW = {"buffer_frames": 64, "opus_frame_samples": 120, "lossless_epoch_samples": 48}
OPUS_LOOKAHEAD_SAMPLES = 120


def daw_index(base: str, daw: dict | None = None) -> tuple[str, list[str]]:
    """(name, ntcmp audio-index arguments after the output path) of the
    index of a DAW source lane, nt-opus or nt-flac."""
    daw = daw or NT_DAW
    buffer = int(daw["buffer_frames"])
    if base == "nt-opus":
        samples, offset = int(daw["opus_frame_samples"]), buffer + OPUS_LOOKAHEAD_SAMPLES
    elif base == "nt-flac":
        samples, offset = int(daw["lossless_epoch_samples"]), buffer
    else:
        raise ValueError(f"{base} is not a DAW source lane")
    ms = repr(samples * 1000 / 48000)
    if int(float(ms) * 48000 / 1000) != samples:
        raise ValueError(f"{samples} samples cannot be named in ms for ntcmp")
    name = f"{base}-{samples}s-b{buffer}"
    return name, [f"numbered:{name}", ms, str(offset)]


NT_INDEXES = [
    ("nt-pcm-48s", ["audio.s24le", "1.0"]),
    ("nt-pcm-24s", ["audio.s24le", "0.5"]),
    ("nt-pcm-12s", ["audio.s24le", "0.25"]),
    # The AEP1 sender's Opus is libopus CELT low-delay, frame k coded from
    # samples [k*120, (k+1)*120): it decodes 120 samples (the look-ahead)
    # late, as every WebRTC lane's Opus does, whose indexes count it too.
    ("nt-popus-120s-la", ["numbered:nt-popus-120s-la", "2.5", str(OPUS_LOOKAHEAD_SAMPLES)]),
    daw_index("nt-opus"),
    daw_index("nt-flac"),
]


def needletail_indexes(media: pathlib.Path, ntcmp: pathlib.Path) -> list[pathlib.Path]:
    """Make the indexes of NT_INDEXES that are missing under media."""
    out = []
    for name, spec in NT_INDEXES:
        path = media / f"{name}.index.json"
        if not path.is_file():
            source = spec[0] if spec[0].startswith("numbered:") else str(media / spec[0])
            subprocess.run([str(ntcmp), "audio-index", source, str(path), *spec[1:]], check=True,
                           capture_output=True)
        out.append(path)
    return out


def udp_client() -> pathlib.Path | None:
    """needletail-udp-client of the av-mesh checkout (Linux build), if built."""
    mesh = pathlib.Path(__import__("os").environ.get(
        "NEEDLETAIL_AV_MESH_ROOT", str(common.WORKSPACE / "av-mesh")))
    path = mesh / "target" / "x86_64-unknown-linux-gnu" / "release" / "needletail-udp-client"
    return path if path.is_file() else None


def audio(_: argparse.Namespace) -> None:
    """Audio files on lon-src, audio indexes on the listeners, Needletail's
    DAW source on lon-src with the loop as its prepared PCM, and PCM enabled in
    the contributor's LL-HLS formats. The listeners also get
    needletail-udp-client for the nt-udp lane when it is built."""
    media = MEDIA / "audio"
    files = sorted(media.glob("*.opus")) + [media / "audio.s24le"]
    if (media / "audio.s24le").is_file():
        needletail_indexes(media, ROOT / "target" / "ntcmp")
    indexes = sorted(media.glob("*.index.json"))
    if not files or not indexes:
        sys.exit(f"no audio under {media}")
    binaries = [str(ROOT / "target" / "ntcmp"), str(ROOT / "target" / "ntcmp-lk")]
    client = udp_client()
    if client:
        binaries.append(str(client))
    else:
        print("needletail-udp-client is not built (av-mesh, x86_64-unknown-linux-gnu): "
              "the nt-udp lane will report errors")
    source = node("lon-src")

    def push(item: dict) -> None:
        common.rsync_to(item, binaries, "/usr/local/bin/")
        common.rsync_to(item, [str(path) for path in indexes], f"{REMOTE}/index/")
        if item["name"].endswith("-view"):
            common.rsync_to(item, [str(ROOT / "scripts" / "compare" / "player" / name)
                                   for name in ("player.js", "player.html", "run.mjs")],
                            "/opt/compare-player/")

    common.parallel(viewers() + [source], push)
    common.ssh(source, f"mkdir -p {REMOTE}/audio {REMOTE}/daw-fixture")
    common.rsync_to(source, [str(path) for path in files], f"{REMOTE}/audio/")
    common.rsync_to(source, [str(common.WORKSPACE / "daw-nexus" / "target" / "x86_64-unknown-linux-gnu"
                                 / "release" / "daw-test-source")], "/usr/local/bin/")
    print(common.ssh_text(source, f"cd {REMOTE}/daw-fixture && python3 -", stdin=FIXTURE.encode(),
                          timeout=600))
    print(f"audio on lon-src, {len(indexes)} indexes on {len(viewers())} listeners")


def status(_: argparse.Namespace) -> None:
    def probe(item: dict) -> str:
        return common.ssh_text(item, (
            "ntcmp 2>&1 | head -1 | cut -c1-40; (google-chrome --version 2>/dev/null || true); "
            "(systemctl is-active livekit-single livekit-multi 2>/dev/null | tr '\\n' ' ' || true); "
            "chronyc -c tracking | awk -F, '{printf \"offset %.3f ms\", $5*1000}'"),
            check=False, timeout=60).replace("\n", "  ")
    hosts = viewers() + named("-lk") + [node("lon-src")]
    for name, out in sorted(common.parallel(hosts, probe).items()):
        print(f"{name:<10} {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare").set_defaults(handler=prepare)
    copy = sub.add_parser("clips")
    copy.add_argument("--from-host", required=True)
    copy.add_argument("--from-dir", default="/root/media/clips2")
    copy.set_defaults(handler=clips)
    sub.add_parser("livekit").set_defaults(handler=livekit)
    sub.add_parser("audio").set_defaults(handler=audio)
    sub.add_parser("secrets").set_defaults(handler=push_secrets)
    sub.add_parser("status").set_defaults(handler=status)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
