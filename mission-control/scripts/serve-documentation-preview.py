#!/usr/bin/env python3
"""Serve Needletail Operations with an explicitly non-authoritative snapshot.

This server exists for qualification previews and documentation captures.  It
does not elect a collector, accept collector proof from its inputs, or stand in
for the production snapshot assembler.
"""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import ssl
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


OPERATIONS_SNAPSHOT_SCHEMA = "needletail.operations-snapshot.v1"
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 4.0
SCRIPT_DIR = Path(__file__).resolve().parent
MISSION_CONTROL_DIR = SCRIPT_DIR.parent
DEFAULT_FIXTURE = MISSION_CONTROL_DIR / "fixtures" / "documentation-preview.json"
DEFAULT_ASSETS = MISSION_CONTROL_DIR / "dist"

UNREPORTED_COLLECTOR = {
    "authority": "",
    "role": "unreported",
    "leader_node_id": None,
    "leader_region": None,
    "term": None,
    "fencing_generation": None,
    "quorum_healthy": None,
    "voters_online": None,
    "voters_total": None,
    "lease_remaining_ms": None,
    "last_leadership_change_unix_ms": None,
    "last_change_reason": None,
    "public_endpoint": None,
    "nodes_current": None,
    "nodes_stale": None,
    "nodes_awaiting": None,
}


class PreviewError(RuntimeError):
    """An invalid preview input or request."""


def _read_limited(response: Any, limit: int = MAX_SNAPSHOT_BYTES) -> bytes:
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise PreviewError(f"snapshot exceeds the {limit}-byte preview limit")
    return payload


def load_json_source(
    source: Union[str, Path],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    insecure_https: bool = False,
) -> Dict[str, Any]:
    """Read one bounded JSON object from a local path or HTTP(S) URL."""

    source_text = str(source)
    parsed = urlsplit(source_text)
    if parsed.scheme in {"http", "https"}:
        if parsed.username is not None or parsed.password is not None:
            raise PreviewError("snapshot URLs must not contain credentials")
        context = None
        if parsed.scheme == "https" and insecure_https:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        request = Request(
            source_text,
            headers={
                "Accept": "application/json",
                "User-Agent": "needletail-operations-documentation-preview/1",
            },
        )
        try:
            with urlopen(request, timeout=timeout_seconds, context=context) as response:
                payload = _read_limited(response)
        except (OSError, TimeoutError) as error:
            raise PreviewError(f"could not read snapshot URL {source_text}: {error}") from error
    elif parsed.scheme:
        raise PreviewError("snapshot sources must be paths or HTTP(S) URLs")
    else:
        path = Path(source_text)
        try:
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                raise PreviewError(
                    f"snapshot exceeds the {MAX_SNAPSHOT_BYTES}-byte preview limit"
                )
            payload = path.read_bytes()
        except OSError as error:
            raise PreviewError(f"could not read snapshot file {path}: {error}") from error

    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreviewError(f"snapshot source is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise PreviewError("snapshot source must contain one JSON object")
    return document


def _unreported_orchestration(value: Any) -> Dict[str, Any]:
    orchestration = copy.deepcopy(value) if isinstance(value, dict) else {}
    orchestration["collector"] = copy.deepcopy(UNREPORTED_COLLECTOR)
    return orchestration


def _mark_fixture_times(snapshot: Dict[str, Any], now_unix_ms: int) -> None:
    """Keep the static documentation fixture fresh without changing counters."""

    snapshot["updated_unix_ms"] = now_unix_ms
    node = snapshot.get("node")
    if isinstance(node, dict):
        node["updated_unix_ms"] = now_unix_ms
    for candidate in snapshot.get("nodes", []):
        if isinstance(candidate, dict):
            candidate["updated_unix_ms"] = now_unix_ms
    for service in snapshot.get("edge_services", []):
        if isinstance(service, dict) and service.get("last_response_unix_ms") is not None:
            service["last_response_unix_ms"] = now_unix_ms - 180
    contributor = snapshot.get("contributor")
    if isinstance(contributor, dict):
        contributor["updated_unix_ms"] = now_unix_ms
        for activity in contributor.get("activity", []):
            if isinstance(activity, dict):
                activity["seen_unix_ms"] = now_unix_ms - 900
        for alert in contributor.get("alerts", []):
            if isinstance(alert, dict) and alert.get("last_seen_unix_ms") is not None:
                alert["last_seen_unix_ms"] = now_unix_ms - 1_500
    for activity in snapshot.get("activity", []):
        if isinstance(activity, dict):
            activity["seen_unix_ms"] = now_unix_ms - 600
    for alert in snapshot.get("alerts", []):
        if isinstance(alert, dict) and alert.get("last_seen_unix_ms") is not None:
            alert["last_seen_unix_ms"] = now_unix_ms - 1_200


def compose_documentation_fixture(
    fixture: Dict[str, Any], *, now_unix_ms: Optional[int] = None
) -> Dict[str, Any]:
    """Return a fresh, canonical and visibly documented fixture snapshot."""

    if fixture.get("schema") != OPERATIONS_SNAPSHOT_SCHEMA:
        raise PreviewError(
            "documentation fixture must declare "
            f"{OPERATIONS_SNAPSHOT_SCHEMA!r}"
        )
    snapshot = copy.deepcopy(fixture)
    _mark_fixture_times(
        snapshot,
        now_unix_ms if now_unix_ms is not None else time.time_ns() // 1_000_000,
    )
    snapshot["orchestration"] = _unreported_orchestration(
        snapshot.get("orchestration")
    )
    snapshot["preview"] = {
        "kind": "documentation_fixture",
        "label": "Documentation fixture — not live telemetry",
        "collector_election_proof": "unreported",
    }
    return snapshot


def compose_qualification_snapshot(
    edge: Dict[str, Any],
    contributor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Adapt current service snapshots without claiming global control state."""

    snapshot = copy.deepcopy(edge)
    snapshot["schema"] = OPERATIONS_SNAPSHOT_SCHEMA
    if contributor is not None:
        snapshot["contributor"] = copy.deepcopy(contributor)
    else:
        snapshot.pop("contributor", None)
    snapshot["orchestration"] = _unreported_orchestration(
        snapshot.get("orchestration")
    )
    snapshot["preview"] = {
        "kind": "qualification_adapter",
        "label": "Qualification telemetry adapter — not a global collector",
        "collector_election_proof": "unreported",
    }
    return snapshot


class SnapshotSource:
    """Reload preview inputs for each bounded dashboard poll."""

    def __init__(
        self,
        *,
        fixture: Path,
        edge_source: Optional[str],
        contributor_source: Optional[str],
        timeout_seconds: float,
        insecure_https: bool,
    ) -> None:
        self.fixture = fixture
        self.edge_source = edge_source
        self.contributor_source = contributor_source
        self.timeout_seconds = timeout_seconds
        self.insecure_https = insecure_https

    @property
    def mode(self) -> str:
        return "qualification-adapter" if self.edge_source else "documentation-fixture"

    def load(self) -> Dict[str, Any]:
        options = {
            "timeout_seconds": self.timeout_seconds,
            "insecure_https": self.insecure_https,
        }
        if self.edge_source:
            edge = load_json_source(self.edge_source, **options)
            contributor = (
                load_json_source(self.contributor_source, **options)
                if self.contributor_source
                else None
            )
            return compose_qualification_snapshot(edge, contributor)
        fixture = load_json_source(self.fixture, **options)
        return compose_documentation_fixture(fixture)


class PreviewRequestHandler(BaseHTTPRequestHandler):
    """Serve one static asset tree and the preview snapshot endpoint."""

    server_version = "NeedletailDocumentationPreview/1"

    @property
    def preview_server(self) -> "PreviewServer":
        return self.server  # type: ignore[return-value]

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(send_body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(send_body=True)

    def _handle(self, *, send_body: bool) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/api/mesh":
            self._serve_snapshot(send_body=send_body)
            return
        if parsed.path == "/healthz":
            self._send(
                HTTPStatus.OK,
                b"ok\n",
                "text/plain; charset=utf-8",
                send_body=send_body,
                cache_control="no-store",
            )
            return
        self._serve_asset(parsed.path, send_body=send_body)

    def _serve_snapshot(self, *, send_body: bool) -> None:
        try:
            snapshot = self.preview_server.snapshot_source.load()
            payload = json.dumps(
                snapshot, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except PreviewError as error:
            payload = json.dumps({"error": str(error)}).encode("utf-8")
            self._send(
                HTTPStatus.BAD_GATEWAY,
                payload,
                "application/json",
                send_body=send_body,
                cache_control="no-store",
            )
            return
        self._send(
            HTTPStatus.OK,
            payload,
            "application/json",
            send_body=send_body,
            cache_control="no-store",
            extra_headers={
                "X-Needletail-Preview": self.preview_server.snapshot_source.mode
            },
        )

    def _serve_asset(self, request_path: str, *, send_body: bool) -> None:
        if request_path in {"", "/", "/mesh", "/mesh/"}:
            relative = Path("index.html")
        else:
            relative = Path(unquote(request_path).lstrip("/"))
        try:
            candidate = (self.preview_server.assets / relative).resolve()
            candidate.relative_to(self.preview_server.assets)
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = candidate.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        content_type = mimetypes.guess_type(candidate.name)[0]
        if candidate.suffix == ".wasm":
            content_type = "application/wasm"
        self._send(
            HTTPStatus.OK,
            payload,
            content_type or "application/octet-stream",
            send_body=send_body,
            cache_control="no-cache",
        )

    def _send(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *,
        send_body: bool,
        cache_control: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(payload)

    def log_message(self, format_: str, *args: object) -> None:
        sys.stderr.write(
            f"{self.address_string()} [{self.log_date_time_string()}] "
            f"{format_ % args}\n"
        )


class PreviewServer(ThreadingHTTPServer):
    """HTTP server state made explicit for testability."""

    daemon_threads = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        *,
        assets: Path,
        snapshot_source: SnapshotSource,
    ) -> None:
        self.assets = assets.resolve()
        self.snapshot_source = snapshot_source
        super().__init__(server_address, PreviewRequestHandler)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Serve Needletail Operations for documentation or qualification "
            "preview. Collector election proof is always unreported."
        )
    )
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5188)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--edge-snapshot",
        help="service-local or canonical edge JSON path/URL, reloaded per poll",
    )
    parser.add_argument(
        "--contributor-snapshot",
        help="contributor status JSON path/URL to embed, reloaded per poll",
    )
    parser.add_argument(
        "--upstream-timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--insecure-upstream",
        action="store_true",
        help="disable TLS verification only for qualification snapshot inputs",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be from 1 through 65535")
    if args.upstream_timeout_seconds <= 0:
        parser.error("--upstream-timeout-seconds must be positive")
    if args.contributor_snapshot and not args.edge_snapshot:
        parser.error("--contributor-snapshot requires --edge-snapshot")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    assets = args.assets.resolve()
    if not (assets / "index.html").is_file():
        print(
            f"Operations assets are missing from {assets}; run "
            "`make -C mission-control build` first",
            file=sys.stderr,
        )
        return 2
    snapshot_source = SnapshotSource(
        fixture=args.fixture,
        edge_source=args.edge_snapshot,
        contributor_source=args.contributor_snapshot,
        timeout_seconds=args.upstream_timeout_seconds,
        insecure_https=args.insecure_upstream,
    )
    try:
        snapshot_source.load()
    except PreviewError as error:
        print(f"invalid preview snapshot: {error}", file=sys.stderr)
        return 2
    server = PreviewServer(
        (args.address, args.port),
        assets=assets,
        snapshot_source=snapshot_source,
    )
    host, port = server.server_address[:2]
    print(
        f"Needletail Operations {snapshot_source.mode} preview: "
        f"http://{host}:{port}/mesh",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
