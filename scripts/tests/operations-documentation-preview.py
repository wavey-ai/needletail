#!/usr/bin/env python3
"""Focused fixtures for the Operations documentation preview server."""

from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = (
    ROOT / "mission-control" / "scripts" / "serve-documentation-preview.py"
)
FIXTURE_PATH = (
    ROOT / "mission-control" / "fixtures" / "documentation-preview.json"
)
SPEC = importlib.util.spec_from_file_location(
    "needletail_operations_documentation_preview", SERVER_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load {SERVER_PATH}")
PREVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREVIEW)


class DocumentationPreviewTests(unittest.TestCase):
    def test_committed_fixture_is_canonical_and_never_claims_election(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        snapshot = PREVIEW.compose_documentation_fixture(
            fixture, now_unix_ms=1_785_388_800_000
        )

        self.assertEqual(snapshot["schema"], PREVIEW.OPERATIONS_SNAPSHOT_SCHEMA)
        self.assertEqual(snapshot["updated_unix_ms"], 1_785_388_800_000)
        self.assertEqual(snapshot["preview"]["kind"], "documentation_fixture")
        collector = snapshot["orchestration"]["collector"]
        self.assertEqual(collector["role"], "unreported")
        for field in (
            "leader_node_id",
            "term",
            "fencing_generation",
            "quorum_healthy",
            "voters_online",
            "voters_total",
            "lease_remaining_ms",
        ):
            self.assertIsNone(collector[field])
        self.assertGreaterEqual(len(snapshot["nodes"]), 10)
        self.assertGreaterEqual(len(snapshot["topology_links"]), 16)

    def test_qualification_adapter_strips_untrusted_collector_proof(self) -> None:
        edge = {
            "updated_unix_ms": 1_785_388_800_000,
            "node": {"node_id": "edge-london"},
            "orchestration": {
                "control_dispatch_ready": True,
                "collector": {
                    "authority": "needletail-controller",
                    "role": "leader",
                    "leader_node_id": "invented-leader",
                    "term": 99,
                    "fencing_generation": 98,
                    "quorum_healthy": True,
                    "voters_online": 3,
                    "voters_total": 3,
                    "lease_remaining_ms": 30000,
                },
            },
        }
        contributor = {"service": "av-contrib", "status": "active"}

        snapshot = PREVIEW.compose_qualification_snapshot(edge, contributor)

        self.assertEqual(snapshot["schema"], PREVIEW.OPERATIONS_SNAPSHOT_SCHEMA)
        self.assertEqual(snapshot["contributor"], contributor)
        self.assertTrue(snapshot["orchestration"]["control_dispatch_ready"])
        collector = snapshot["orchestration"]["collector"]
        self.assertEqual(collector["role"], "unreported")
        self.assertIsNone(collector["leader_node_id"])
        self.assertIsNone(collector["term"])
        self.assertIsNone(collector["fencing_generation"])
        self.assertIsNone(collector["quorum_healthy"])
        self.assertNotIn("delivery", snapshot)
        self.assertNotIn("publication", snapshot)
        self.assertNotIn("topology_links", snapshot)

    def test_server_exposes_same_origin_snapshot_and_static_assets(self) -> None:
        with TemporaryDirectory() as temporary:
            assets = Path(temporary)
            (assets / "index.html").write_text(
                "<!doctype html><title>Needletail Operations</title>",
                encoding="utf-8",
            )
            source = PREVIEW.SnapshotSource(
                fixture=FIXTURE_PATH,
                edge_source=None,
                contributor_source=None,
                timeout_seconds=1.0,
                insecure_https=False,
            )
            server = PREVIEW.PreviewServer(
                ("127.0.0.1", 0), assets=assets, snapshot_source=source
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base}/mesh", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(
                        b"Needletail Operations", response.read()
                    )
                with urlopen(f"{base}/api/mesh", timeout=2) as response:
                    self.assertEqual(
                        response.headers["X-Needletail-Preview"],
                        "documentation-fixture",
                    )
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    snapshot = json.load(response)
                self.assertEqual(
                    snapshot["schema"], PREVIEW.OPERATIONS_SNAPSHOT_SCHEMA
                )
                self.assertIsNone(
                    snapshot["orchestration"]["collector"]["term"]
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"{base}/../Cargo.toml", timeout=2)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
