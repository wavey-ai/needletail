#!/usr/bin/env python3
"""Play an HLS playlist with the system ffmpeg for N seconds and write a JSON report.

ffmpeg reads the playlist at real time (``-re``), decodes it and writes to the
null muxer. The report has: ffprobe stream info, time to the first decoded
frame, media time against wall time sampled every second (from ``-progress``),
stalls (media time not advancing for more than 500 ms), the speed factor, and
the ffmpeg error/warning lines (counted, first 30 listed).

Start it inside the network namespace of a reader host, like the hls.js harness:

    python3 scripts/edge_lab.py sh lon-readers \\
        python3 scripts/players/ffmpeg-play.py \\
        --url 'https://10.90.0.11:19444/live/1/master.m3u8?sid=demo' --seconds 40 --report /tmp/ff.json

Exit status: 0 played, 2 stalled, 3 failed, 1 harness error.
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse

HARNESS_VERSION = "0.1.0"
STATS_PERIOD_S = 0.1
LEVELS = ("panic", "fatal", "error", "warning")
LINE = re.compile(r"^(?:\[(?P<component>[^\]@]+?)(?: @ 0x[0-9a-f]+)?\] )?\[(?P<level>[a-z]+)\] (?P<text>.*)$")
OPENING = re.compile(r"Opening '([^']+)' for reading")
HTTP_ERROR = re.compile(r"HTTP error (\d{3})|Server returned (\d{3})")


def classify_component(component: str | None, text: str) -> str:
    c = (component or "").lower()
    if c in ("http", "https", "tls", "tcp", "crypto") or "http error" in text.lower() or "server returned" in text.lower():
        return "http"
    if c in ("hls", "mov,mp4,m4a,3gp,3g2,mj2", "mpegts", "matroska,webm", "in#0", "dmx", "aist#0", "vist#0") or c.startswith("in#"):
        return "demux"
    if c.startswith("dec") or c in ("h264", "hevc", "flac", "opus", "aac", "libopus", "libdav1d", "av1"):
        return "decoder"
    if c.startswith("out#") or c.startswith("enc") or c.startswith("vost") or c.startswith("aost") or c == "null":
        return "output"
    return "other"


def url_kind(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".m3u8"):
        return "multivariant_playlist" if name.startswith("master") else "media_playlist"
    if name.startswith("init"):
        return "init_segment"
    if name.startswith("part"):
        return "part"
    return "segment"


PROXIED = re.compile(r"^/(https?)/([^/]+)(/.*)?$")


class TlsProxy:
    """Loopback HTTP/1.1 forwarder: http://127.0.0.1:<port>/<scheme>/<host:port>/<path> -> <scheme>://<host:port>/<path>.

    ffmpeg >= 8 verifies TLS certificates by default and its HLS demuxer does
    not hand -tls_verify / -ca_file down to the requests it makes for Media
    Playlists and segments, so a private CA or a certificate whose name does
    not match the URL host (the lab names caches by IP address) cannot be
    played directly. The forwarder connects upstream over TLS without
    verification and rewrites absolute URIs in playlists so that they come
    back through it. Nothing else is changed. Every upstream request is logged.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                self.upstreams: dict[tuple[str, str], http.client.HTTPConnection] = {}

            def finish(self) -> None:
                for connection in self.upstreams.values():
                    try:
                        connection.close()
                    except OSError:
                        pass
                super().finish()

            def log_message(self, *_args) -> None:
                return

            def connect(self, scheme: str, netloc: str) -> http.client.HTTPConnection:
                if scheme == "https":
                    context = ssl.create_default_context()
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    return http.client.HTTPSConnection(netloc, timeout=30, context=context)
                return http.client.HTTPConnection(netloc, timeout=30)

            def do_GET(self) -> None:
                match = PROXIED.match(self.path)
                if not match:
                    self.send_error(404, "not a proxied URL")
                    return
                scheme, netloc, rest = match.group(1), match.group(2), match.group(3) or "/"
                url = f"{scheme}://{netloc}{rest}"
                started = time.monotonic()
                entry = {"t_ms": round((started - proxy.t0) * 1000.0, 1), "url": url, "kind": url_kind(url), "host": netloc,
                         "status": None, "bytes": 0, "ttfb_ms": None, "total_ms": None, "error": None, "range": self.headers.get("Range")}
                headers = {"Host": netloc, "Accept": self.headers.get("Accept", "*/*"), "User-Agent": self.headers.get("User-Agent", "Lavf")}
                is_playlist = entry["kind"] in ("multivariant_playlist", "media_playlist")
                # ffmpeg sends "Range: bytes=0-" on everything. A playlist is
                # rewritten (its length changes), so it is fetched whole.
                if self.headers.get("Range") and not is_playlist:
                    headers["Range"] = self.headers["Range"]
                response = None
                for attempt in (0, 1):
                    key = (scheme, netloc)
                    connection = self.upstreams.get(key)
                    fresh = connection is None
                    if fresh:
                        connection = self.connect(scheme, netloc)
                        self.upstreams[key] = connection
                    try:
                        connection.request("GET", rest, headers=headers)
                        response = connection.getresponse()
                        break
                    except (OSError, http.client.HTTPException) as error:
                        connection.close()
                        self.upstreams.pop(key, None)
                        if fresh or attempt == 1:
                            entry["error"] = f"{type(error).__name__}: {error}"
                            break
                if response is None:
                    entry["total_ms"] = round((time.monotonic() - started) * 1000.0, 1)
                    with proxy.lock:
                        proxy.requests.append(entry)
                    body = (entry["error"] or "upstream error").encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                entry["status"] = response.status
                entry["ttfb_ms"] = round((time.monotonic() - started) * 1000.0, 1)
                entry["content_type"] = response.getheader("Content-Type")
                try:
                    if is_playlist or response.getheader("Content-Length") is None:
                        data = response.read()
                        entry["bytes"] = len(data)
                        if is_playlist and response.status == 200:
                            host, port = self.server.server_address[:2]
                            data = re.sub(rb"(https?)://", lambda m: f"http://{host}:{port}/".encode() + m.group(1) + b"/", data)
                        self.send_response(response.status)
                        for name in ("Content-Type", "Content-Range", "Accept-Ranges"):
                            if response.getheader(name):
                                self.send_header(name, response.getheader(name))
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        self.send_response(response.status)
                        for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                            if response.getheader(name):
                                self.send_header(name, response.getheader(name))
                        self.end_headers()
                        while True:
                            chunk = response.read(65536)
                            if not chunk:
                                break
                            entry["bytes"] += len(chunk)
                            self.wfile.write(chunk)
                except (OSError, http.client.HTTPException) as error:
                    entry["error"] = f"{type(error).__name__}: {error}"
                    self.close_connection = True
                entry["total_ms"] = round((time.monotonic() - started) * 1000.0, 1)
                with proxy.lock:
                    proxy.requests.append(entry)

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address) -> None:
                # ffmpeg drops its keep-alive connections when it stops.
                return

        self.server = Server(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def wrap(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        rest = parts.path + (f"?{parts.query}" if parts.query else "")
        return f"{self.base}/{parts.scheme}/{parts.netloc}{rest}"

    def unwrap(self, url: str) -> str:
        if not url.startswith(self.base + "/"):
            return url
        match = PROXIED.match(url[len(self.base):])
        return f"{match.group(1)}://{match.group(2)}{match.group(3) or '/'}" if match else url

    def unwrap_text(self, text: str) -> str:
        return re.sub(re.escape(self.base) + r"/(https?)/", r"\1://", text)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def summary(self) -> dict:
        with self.lock:
            requests = list(self.requests)
        by_status: dict[str, int] = {}
        by_kind: dict[str, dict[str, int]] = {}
        by_host: dict[str, int] = {}
        for request in requests:
            status = str(request["status"]) if request["status"] is not None else "error"
            by_status[status] = by_status.get(status, 0) + 1
            by_kind.setdefault(request["kind"], {}).setdefault(status, 0)
            by_kind[request["kind"]][status] += 1
            by_host[request["host"]] = by_host.get(request["host"], 0) + 1
        failed = [r for r in requests if r["status"] is None or not 200 <= r["status"] < 300]
        return {"requests": len(requests), "by_status": by_status, "by_kind": by_kind, "by_host": by_host,
                "bytes": sum(r["bytes"] for r in requests), "non_2xx_or_failed": failed[:100], "list": requests[:600]}


def run_ffprobe(ffprobe: str, url: str, input_args: list[str], timeout: float) -> dict:
    command = [ffprobe, "-hide_banner", "-loglevel", "level+warning", *input_args,
               "-print_format", "json", "-show_format", "-show_streams", "-show_programs", url]
    started = time.monotonic()
    result = {"command": shlex.join(command), "timed_out": False}
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        result["exit_code"] = done.returncode
        stdout, stderr = done.stdout, done.stderr
    except subprocess.TimeoutExpired as error:
        result["timed_out"] = True
        result["exit_code"] = None
        stdout = (error.stdout or b"").decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = (error.stderr or b"").decode("utf-8", "replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
    result["wall_s"] = round(time.monotonic() - started, 3)
    lines = [line for line in stderr.splitlines() if line.strip()]
    result["stderr_lines"] = len(lines)
    result["stderr_first"] = lines[:30]
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}
    keep = ("index", "codec_name", "codec_long_name", "profile", "codec_type", "codec_tag_string", "width", "height",
            "pix_fmt", "level", "field_order", "has_b_frames", "r_frame_rate", "avg_frame_rate", "time_base", "start_time",
            "sample_fmt", "sample_rate", "channels", "channel_layout", "bits_per_raw_sample", "bit_rate", "color_space",
            "color_transfer", "color_primaries", "color_range")
    streams = []
    for stream in data.get("streams", []):
        entry = {key: stream[key] for key in keep if key in stream}
        tags = stream.get("tags") or {}
        if "variant_bitrate" in tags:
            entry["variant_bitrate"] = tags["variant_bitrate"]
        streams.append(entry)
    result["streams"] = streams
    fmt = data.get("format") or {}
    result["format"] = {key: fmt[key] for key in ("format_name", "format_long_name", "nb_streams", "nb_programs", "start_time", "duration", "bit_rate", "probe_score") if key in fmt}
    result["programs"] = [
        {"program_id": program.get("program_id"), "nb_streams": program.get("nb_streams"),
         "variant_bitrate": (program.get("tags") or {}).get("variant_bitrate"),
         "streams": [s.get("index") for s in program.get("streams", [])]}
        for program in data.get("programs", [])
    ]
    result["opened"] = bool(streams)
    return result


def tool_version(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Multivariant or Media Playlist URL")
    parser.add_argument("--seconds", type=float, default=40.0, help="wall-clock play time (default 40)")
    parser.add_argument("--report", help="JSON report path (default: stdout)")
    parser.add_argument("--label", help="free-form label copied into the report")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe")
    parser.add_argument("--input-args", default="", help="extra ffmpeg/ffprobe input options, e.g. '-live_start_index -1'")
    parser.add_argument("--no-realtime", action="store_true", help="do not pass -re (stall figures then show the segment cadence, not player stalls)")
    parser.add_argument("--no-probe", action="store_true", help="skip the ffprobe step")
    parser.add_argument("--probe-timeout", type=float, default=30.0)
    parser.add_argument("--stall-threshold-ms", type=float, default=500.0)
    parser.add_argument("--loglevel", default="info", help="ffmpeg log level (default info; verbose/debug give more HLS detail)")
    parser.add_argument("--quiet", action="store_true", help="no per-second status line on stderr")
    parser.add_argument("--start-attempts", type=int, default=1,
                        help="start ffmpeg again when it exits on its own without decoded output (default 1 = no retry)")
    parser.add_argument("--start-retry-delay", type=float, default=1.0, help="seconds between start attempts (default 1)")
    parser.add_argument("--tls-verify", choices=("0", "1"), default="0",
                        help="value of ffmpeg -tls_verify for the first URL (default 0). ffmpeg does not pass it to the Media "
                             "Playlist and segment requests of the HLS demuxer, see --tls-proxy")
    parser.add_argument("--tls-proxy", choices=("auto", "on", "off"), default="auto",
                        help="reach https URLs through a loopback forwarder in this process that does not verify certificates "
                             "(default auto: on for https). 'off' lets ffmpeg connect directly")
    args = parser.parse_args()

    input_args = shlex.split(args.input_args)
    is_https = args.url.lower().startswith("https://")
    proxy = TlsProxy() if (args.tls_proxy == "on" or (args.tls_proxy == "auto" and is_https)) else None
    play_url = proxy.wrap(args.url) if proxy else args.url
    if is_https and not proxy:
        input_args = ["-tls_verify", args.tls_verify, *input_args]
    clean = (lambda text: proxy.unwrap_text(text)) if proxy else (lambda text: text)
    started_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    report: dict = {
        "harness": {
            "name": "ffmpeg-play", "version": HARNESS_VERSION, "label": args.label, "url": args.url,
            "seconds_requested": args.seconds, "started_utc": started_utc, "realtime_read": not args.no_realtime,
            "ffmpeg_version": tool_version(args.ffmpeg), "ffprobe_version": tool_version(args.ffprobe),
            "timestamps": "milliseconds since the ffmpeg process was started, unless a field name says otherwise",
            "note": "the ffmpeg HLS demuxer ignores EXT-X-PART / preload hints and content steering; it plays whole segments of one variant",
        },
    }

    report["harness"]["transport"] = {
        "tls_proxy": bool(proxy),
        "tls_verify_flag": None if proxy or not is_https else args.tls_verify,
        "note": ("ffmpeg talks plain HTTP/1.1 to a loopback forwarder in this process, which talks HTTP/1.1 over TLS (no certificate "
                 "verification) to the servers and rewrites absolute playlist URIs; URLs in this report are the real ones")
        if proxy else "ffmpeg connects to the servers directly",
    }
    probe_requests = 0

    command = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", f"level+{args.loglevel}", "-nostats",
               "-stats_period", str(STATS_PERIOD_S), "-progress", "pipe:1"]
    if not args.no_realtime:
        command.append("-re")
    command += [*input_args, "-i", play_url, "-f", "null", "-"]
    report["harness"]["command"] = clean(shlex.join(command))
    interrupted = {"signal": None}

    def on_signal(signum, _frame):
        interrupted["signal"] = signal.Signals(signum).name

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, on_signal)

    def media_us(block: dict) -> int | None:
        value = block.get("out_time_us") or block.get("out_time_ms")
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    # A cold cache answers with a playlist that has no complete segment yet;
    # ffmpeg then exits at once ("Empty segment"). --start-attempts starts it
    # again, as a person would. Every attempt is recorded.
    attempts: list[dict] = []
    first_attempt_at = time.monotonic()
    for attempt in range(1, max(1, args.start_attempts) + 1):
        if proxy:
            proxy.t0 = time.monotonic()

        blocks: list[dict] = []
        log_lines: list[tuple[float, str]] = []
        lock = threading.Lock()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, errors="replace", bufsize=1, start_new_session=True)
        t0 = time.monotonic()

        def now_ms() -> float:
            return round((time.monotonic() - t0) * 1000.0, 1)

        def read_progress() -> None:
            current: dict = {}
            for raw in process.stdout:
                key, _, value = raw.strip().partition("=")
                if not key:
                    continue
                current[key] = value.strip()
                if key == "progress":
                    current["t_ms"] = now_ms()
                    with lock:
                        blocks.append(current)
                    current = {}

        def read_log() -> None:
            for raw in process.stderr:
                line = clean(raw.rstrip("\n"))
                if line.strip():
                    with lock:
                        log_lines.append((now_ms(), line))

        threads = [threading.Thread(target=read_progress, daemon=True), threading.Thread(target=read_log, daemon=True)]
        for thread in threads:
            thread.start()

        how_stopped = "exited on its own"
        deadline = t0 + args.seconds
        next_status = t0 + 1.0
        try:
            while process.poll() is None:
                now = time.monotonic()
                if interrupted["signal"]:
                    how_stopped = f"harness received {interrupted['signal']}"
                    break
                if now >= deadline:
                    how_stopped = f"stopped by the harness after {args.seconds:g} s"
                    break
                if now >= next_status:
                    next_status += 1.0
                    if not args.quiet:
                        with lock:
                            last = blocks[-1] if blocks else None
                            errors = sum(1 for _, line in log_lines if "[error]" in line or "[fatal]" in line)
                        media = media_us(last) if last else None
                        sys.stderr.write(
                            f"t={now - t0:.1f}s media={media / 1e6:.2f}s frame={last.get('frame', '-')} speed={last.get('speed', '-')} errors={errors}\n"
                            if media is not None else f"t={now - t0:.1f}s no decoded output yet errors={errors}\n")
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                # SIGINT lets ffmpeg finish the null muxer and print its summary.
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                    how_stopped += " (needed SIGKILL)"
        end_ms = now_ms()
        for thread in threads:
            thread.join(timeout=2)
        exit_code = process.returncode
        produced = any((media_us(b) or 0) > 0 for b in blocks)
        attempts.append({
            "attempt": attempt,
            "started_ms_after_first_attempt": round((t0 - first_attempt_at) * 1000.0, 1),
            "ran_ms": end_ms, "exit_code": exit_code, "how_stopped": how_stopped, "produced_output": produced,
            "first_problem_lines": [line for _, line in log_lines if any(f"[{level}]" in line for level in LEVELS)][:6],
        })
        if produced or how_stopped != "exited on its own" or attempt >= args.start_attempts:
            break
        if not args.quiet:
            sys.stderr.write(f"# attempt {attempt}: ffmpeg exited with code {exit_code} after {end_ms:.0f} ms without output; starting again in {args.start_retry_delay:g} s\n")
        time.sleep(args.start_retry_delay)
        if proxy:
            with proxy.lock:
                del proxy.requests[probe_requests:]

    # ---- progress, startup, stalls -----------------------------------------
    with lock:
        blocks_copy = list(blocks)
        lines_copy = list(log_lines)
    points = []
    for block in blocks_copy:
        media = media_us(block)
        frame = block.get("frame")
        points.append({
            "t_ms": block["t_ms"], "media_us": media,
            "frame": int(frame) if frame and frame.isdigit() else None,
            "fps": block.get("fps"), "speed": block.get("speed"), "bitrate": block.get("bitrate"),
            "dup": block.get("dup_frames"), "drop": block.get("drop_frames"), "total_size": block.get("total_size"),
        })
    first_output = next((p for p in points if (p["media_us"] or 0) > 0 or (p["frame"] or 0) > 0), None)
    first_frame = next((p for p in points if (p["frame"] or 0) > 0), None)
    last_started = attempts[-1]["started_ms_after_first_attempt"]
    report["startup"] = {
        "attempts": attempts,
        "attempts_made": len(attempts),
        "time_to_first_decoded_frame_including_failed_attempts_ms": round(last_started + first_output["t_ms"], 1) if first_output else None,
        "time_to_first_decoded_frame_ms": first_output["t_ms"] if first_output else None,
        "time_to_first_video_frame_ms": first_frame["t_ms"] if first_frame else None,
        "resolution_ms": STATS_PERIOD_S * 1000,
        "note": "time from ffmpeg process start to the first -progress block that reports decoded output (video frame count > 0 or output time > 0); "
                "blocks are emitted every 100 ms. With --start-attempts > 1 "
                "all other figures in this report are those of the last attempt.",
    }

    stalls = []
    if first_output:
        last_advance_t = first_output["t_ms"]
        last_media = first_output["media_us"] or 0
        for point in points[points.index(first_output) + 1:]:
            media = point["media_us"]
            if media is None:
                continue
            if media > last_media:
                gap = point["t_ms"] - last_advance_t
                if gap > args.stall_threshold_ms:
                    stalls.append({"start_ms": last_advance_t, "duration_ms": round(gap, 1), "media_s": round(last_media / 1e6, 3), "resolved": True})
                last_media = media
                last_advance_t = point["t_ms"]
        tail = end_ms - last_advance_t
        if tail > args.stall_threshold_ms:
            stalls.append({"start_ms": last_advance_t, "duration_ms": round(tail, 1), "media_s": round(last_media / 1e6, 3), "resolved": False})
    total_stalled = round(sum(s["duration_ms"] for s in stalls), 1)
    report["stalls"] = {
        "threshold_ms": args.stall_threshold_ms, "count": len(stalls), "total_stalled_ms": total_stalled, "list": stalls,
        "note": "a stall is a period after the first decoded frame in which the output media time did not advance; resolution 100 ms"
                + ("" if not args.no_realtime else "; without -re this shows the segment arrival cadence, not player stalls"),
    }

    samples = []
    next_second = 1000.0
    for point in points:
        if point["t_ms"] >= next_second:
            samples.append({
                "t_ms": point["t_ms"], "media_s": round(point["media_us"] / 1e6, 3) if point["media_us"] is not None else None,
                "frame": point["frame"], "fps": point["fps"], "speed": point["speed"], "bitrate": point["bitrate"],
                "dup_frames": point["dup"], "drop_frames": point["drop"],
            })
            while next_second <= point["t_ms"]:
                next_second += 1000.0
    last_point = next((p for p in reversed(points) if p["media_us"] is not None), None)
    media_s = round(last_point["media_us"] / 1e6, 3) if last_point else 0.0
    wall_after_first = round((end_ms - first_output["t_ms"]) / 1000.0, 3) if first_output else None
    media_after_first = round(media_s - (first_output["media_us"] or 0) / 1e6, 3) if first_output else 0.0
    speed_factor = round(media_after_first / wall_after_first, 3) if wall_after_first else None
    report["progress"] = {
        "wall_s": round(end_ms / 1000.0, 3), "media_s": media_s,
        "wall_after_first_frame_s": wall_after_first, "media_after_first_frame_s": media_after_first,
        "speed_factor": speed_factor,
        "ffmpeg_reported_speed": last_point["speed"] if last_point else None,
        "frames": last_point["frame"] if last_point else None,
        "dup_frames": last_point["dup"] if last_point else None,
        "drop_frames": last_point["drop"] if last_point else None,
        "progress_blocks": len(points),
        "samples": samples,
        "note": "speed_factor = media seconds output / wall seconds, both counted from the first decoded frame; 1.0 is real time",
    }

    # ---- log lines ----------------------------------------------------------
    counts = {level: 0 for level in LEVELS}
    by_category: dict[str, dict[str, int]] = {}
    by_message: dict[str, int] = {}
    listed = []
    opens = []
    open_by_host: dict[str, int] = {}
    open_by_kind: dict[str, int] = {}
    http_codes: dict[str, int] = {}
    input_lines = []
    for t_ms, line in lines_copy:
        match = LINE.match(line)
        level = match.group("level") if match else None
        component = match.group("component") if match else None
        text = match.group("text") if match else line
        opening = OPENING.search(line)
        if opening:
            url = opening.group(1)
            host = urllib.parse.urlsplit(url).netloc
            kind = url_kind(url)
            open_by_host[host] = open_by_host.get(host, 0) + 1
            open_by_kind[kind] = open_by_kind.get(kind, 0) + 1
            if len(opens) < 400:
                opens.append({"t_ms": t_ms, "kind": kind, "host": host, "file": urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]})
        if level in ("info",) and (text.startswith("Input #") or text.lstrip().startswith("Stream #") or text.lstrip().startswith("Duration:") or text.lstrip().startswith("Program ")):
            if len(input_lines) < 40:
                input_lines.append(text.rstrip())
        if level in LEVELS:
            counts[level] += 1
            category = classify_component(component, text)
            by_category.setdefault(category, {}).setdefault(level, 0)
            by_category[category][level] += 1
            normalized = re.sub(r"0x[0-9a-f]+", "0x..", re.sub(r"\d+", "N", f"[{component or '-'}] {text}"))[:200]
            by_message[normalized] = by_message.get(normalized, 0) + 1
            for code in HTTP_ERROR.findall(text):
                value = code[0] or code[1]
                http_codes[value] = http_codes.get(value, 0) + 1
            if len(listed) < 30:
                listed.append({"t_ms": t_ms, "level": level, "category": category, "component": component, "text": text[:500]})
    report["log"] = {
        "counts": counts, "by_category": by_category, "http_error_codes": http_codes,
        "by_message": dict(sorted(by_message.items(), key=lambda item: -item[1])[:40]),
        "first": listed, "total_lines": len(lines_copy), "input_description": input_lines,
        "last_lines": [line for _, line in lines_copy[-8:]],
    }
    report["opens"] = {"count": sum(open_by_kind.values()), "by_host": open_by_host, "by_kind": open_by_kind, "list": opens,
                       "note": "from ffmpeg 'Opening ... for reading' lines; reused keep-alive connections are included"}
    report["exit"] = {"code": exit_code, "how_stopped": how_stopped, "end_ms": end_ms}
    if proxy:
        report["http"] = proxy.summary()
        report["http"]["note"] = ("upstream requests made for ffmpeg by the loopback forwarder during the last play attempt "
                                  "(the ffprobe step is excluded); t_ms is since ffmpeg start")
    else:
        report["http"] = None

    # ffprobe runs after the play step, so that a cache that was cold at the
    # start (see startup.attempts) has a playlist with complete segments now.
    if args.no_probe or interrupted["signal"]:
        report["ffprobe"] = None
    else:
        probe_attempts = []
        for attempt in range(1, max(1, args.start_attempts) + 1):
            probe = run_ffprobe(args.ffprobe, play_url, input_args, args.probe_timeout)
            probe["command"] = clean(probe["command"])
            probe["stderr_first"] = [clean(line) for line in probe["stderr_first"]]
            probe_attempts.append({"attempt": attempt, "opened": probe["opened"], "exit_code": probe["exit_code"],
                                   "wall_s": probe["wall_s"], "stderr_first": probe["stderr_first"][:4]})
            if probe["opened"] or attempt >= args.start_attempts:
                break
            time.sleep(args.start_retry_delay)
        probe["attempts"] = probe_attempts
        probe["note"] = "run after the play step; with --start-attempts > 1 it is repeated until it opens the playlist"
        report["ffprobe"] = probe
    if proxy:
        proxy.close()

    # ---- verdict ------------------------------------------------------------
    first_error = next((entry for entry in listed if entry["level"] in ("error", "fatal", "panic")), None)
    notes = []
    if first_output is None:
        result = "failed"
        reason = f"ffmpeg produced no decoded output in {end_ms / 1000.0:.1f} s (exit code {exit_code}, {how_stopped})"
        if first_error:
            reason += f"; first error: [{first_error['component']}] {first_error['text']}"
    elif speed_factor is not None and speed_factor < 0.5:
        result = "failed"
        reason = f"only {media_after_first:.2f} s of media in {wall_after_first:.2f} s after the first frame (speed {speed_factor})"
    elif stalls or (speed_factor is not None and speed_factor < 0.97) or how_stopped == "exited on its own":
        result = "stalled" if how_stopped != "exited on its own" else "failed"
        reason = (f"{len(stalls)} stall(s), {total_stalled} ms in total, longest {max((s['duration_ms'] for s in stalls), default=0)} ms; "
                  f"{media_after_first:.2f} s of media in {wall_after_first:.2f} s after the first frame (speed {speed_factor})")
        if how_stopped == "exited on its own":
            reason = f"ffmpeg exited on its own with code {exit_code} after {end_ms / 1000.0:.1f} s; " + reason
    else:
        result = "played"
        reason = f"{media_after_first:.2f} s of media in {wall_after_first:.2f} s after the first frame (speed {speed_factor}), no stall over {args.stall_threshold_ms:g} ms"
    if len(attempts) > 1:
        failed_first = attempts[0]["first_problem_lines"][:1]
        notes.append(f"needed {len(attempts)} start attempts; attempt 1 exited with code {attempts[0]['exit_code']} after {attempts[0]['ran_ms']:.0f} ms"
                     + (f": {failed_first[0]}" if failed_first else ""))
    if counts["error"] or counts["fatal"] or counts["panic"]:
        notes.append(f"{counts['error'] + counts['fatal'] + counts['panic']} error line(s), {counts['warning']} warning line(s)")
    elif counts["warning"]:
        notes.append(f"{counts['warning']} warning line(s)")
    if report["ffprobe"] is not None and not report["ffprobe"]["opened"]:
        notes.append("ffprobe could not open the playlist: " + "; ".join(report["ffprobe"]["stderr_first"][:2]))
    report["verdict"] = {"result": result, "reason": reason, "notes": notes}

    ordered = {key: report[key] for key in ("harness", "verdict", "ffprobe", "startup", "progress", "stalls", "log", "opens", "http", "exit")}
    text = json.dumps(ordered, indent=1)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(text)
        sys.stderr.write(f"# verdict: {result} - {reason}\n")
        for note in notes:
            sys.stderr.write(f"#   note: {note}\n")
        sys.stderr.write(f"# report: {args.report}\n")
    else:
        sys.stdout.write(text + "\n")
    return {"played": 0, "stalled": 2, "failed": 3}[result]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
