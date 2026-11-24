#!/usr/bin/env python3
"""Tests of the deploy tool's `collect` (the run archive): every node in
parallel within the time bound, its journal, its API, its samplers' files;
the lab's state, hosts map, deployed record, settings (scrubbed) and
topology; the run marked collected, or not, with every node and file that
could not be had; one node never fails the others. Offline: ssh is faked,
no Linode call, no token, a temporary archive root.
python3 -m unittest scripts/test_deploy_collect.py"""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_archive as ra  # noqa: E402

_spec = importlib.util.spec_from_file_location("deploy_collect", HERE / "linode-small-mesh-deploy.py")
deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy)
REAL_POPEN = subprocess.Popen
SECRET = "f" * 64


def state_of(directory: pathlib.Path, run_id: str = "col1") -> dict:
    topology = directory / "topology-small.json"
    topology.write_text(json.dumps({"nodes": [
        {"name": name, "role": role, "site": "london", "region": "gb-lon", "type": "g6-dedicated-2"}
        for name, role in (("contrib-lon", "contributor"), ("lon-dist-a", "distributor"),
                           ("lon-edge-1", "edge"), ("lon-load-1", "load"))]}))
    nodes = {}
    for index, (name, role) in enumerate((("contrib-lon", "contributor"),
                                          ("lon-dist-a", "distributor"),
                                          ("lon-edge-1", "edge"), ("lon-load-1", "load"))):
        nodes[name] = {"id": 100 + index, "name": name, "label": f"ntsm-{run_id}-{name}",
                       "role": role, "site": "london", "region": "gb-lon",
                       "type": "g6-dedicated-2", "hourly_usd": 0.054,
                       "public_ipv4": f"203.0.113.{10 + index}", "vlan_ipv4": f"10.0.0.{index + 2}",
                       "host_uuid": f"host-{index % 2}"}
    return {"run_id": run_id, "tag": f"needletail-small-mesh-{run_id}", "created_unix": 1_763_900_000,
            "topology_file": str(topology), "nodes": nodes,
            "deployed": {"settings": {"part_us": 1000, "api_token": "hunter2-not-a-real-token"},
                         "tiers": {"edges": "abc-1"},
                         "nodes": {"lon-edge-1": {"build": "abc-1", "args_sha256": SECRET}}}}


class FakeProcess:
    """ssh as Popen gives it: its stdout a pipe fed by a thread, which
    ends when the data is sent (or, blocked, when killed)."""

    def __init__(self, data: bytes, code: int, block: bool):
        read, self._write = os.pipe()
        self.stdout = os.fdopen(read, "rb")
        self.code = code
        self.returncode = None
        self.killed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._feed, args=(data, block), daemon=True)
        self._thread.start()

    def _close(self) -> None:
        with self._lock:
            if self._write is not None:
                os.close(self._write)
                self._write = None

    def _feed(self, data: bytes, block: bool) -> None:
        with self._lock:
            if self._write is not None:
                os.write(self._write, data)
        if block:
            self._stop.wait()
        self._close()

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._stop.set()
        self._close()

    def wait(self, timeout=None):
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired("ssh", timeout)
        self.returncode = -9 if self.killed else self.code
        return self.returncode


class FakeNodes:
    """ssh to each node, faked: `ok` (every file), `down` (ssh fails),
    `slow` (it sends part of its tar and then nothing until killed),
    `no-stalls` (its /api/stalls answers 404), `cut-hops` (curl cut its
    /api/hops at 200: exit 28) and `cut` (its tar ends part-way)."""

    def __init__(self, case: unittest.TestCase, state: dict, behaviour: dict | None = None):
        self.by_address = {node["public_ipv4"]: name for name, node in state["nodes"].items()}
        self.behaviour = behaviour or {}
        self.scripts: dict[str, str] = {}
        self.started: dict[str, float] = {}
        self.processes: dict[str, FakeProcess] = {}
        patcher = mock.patch.object(deploy.subprocess, "Popen", self.popen)
        patcher.start()
        case.addCleanup(patcher.stop)

    def popen(self, command, *args, **kwargs):
        if command[0] != "ssh":
            return REAL_POPEN(command, *args, **kwargs)
        name = self.by_address[command[-2].split("@", 1)[1]]
        script = command[-1]
        self.scripts[name] = script
        self.started[name] = time.monotonic()
        how = self.behaviour.get(name, "ok")
        if how == "down":
            kwargs["stderr"].write(b"ssh: connect: no route")
            process = FakeProcess(b"", 255, False)
        else:
            stamp = re.search(r"T=(\d{8}T\d{6}Z)", script).group(1)
            data = self.tarball(name, stamp, script, how)
            if how in ("cut", "slow"):
                data = data[: len(data) // 2]
            process = FakeProcess(data, 0, how == "slow")
        self.processes[name] = process
        return process

    @staticmethod
    def tarball(name: str, stamp: str, script: str, how: str) -> bytes:
        files = {f"./journal.{stamp}.txt.gz": gzip.compress(f"{name} boot line\n".encode() * 2000)}
        statuses = []
        for stem, path in re.findall(r'"\$D/(api-[a-z-]+)\.\$T\.\w+" -w \S+ \'?https://[^/]+(/[^\'\s]+)',
                                     script):
            code = "404" if (how == "no-stalls" and stem == "api-stalls") else "200"
            curl_exit = "28" if (how == "cut-hops" and stem == "api-hops") else "0"
            statuses.append(f"{stem} {code} {curl_exit}")
            extension = "ndjson" if stem == "api-hops" else "json"
            files[f"./{stem}.{stamp}.{extension}"] = json.dumps({"path": path, "node": name}).encode()
        files[f"./http-status.{stamp}.txt"] = ("\n".join(statuses) + "\n").encode()
        files["needletail-runs/scale/r1/cpu.ndjson"] = b'{"unix_s":1,"cpu_pct":3}\n' * 500
        files["compare-sampler.log"] = b"1763900000 cpu 1 2 3\n"
        # Last and hard to compress: the part of the tar a cut ends in.
        files["needletail-runs/scale/r1/big.log"] = "".join(
            f"1763900000 {os.urandom(16).hex()}\n" for _ in range(1200)).encode()
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for member, body in files.items():
                info = tarfile.TarInfo(member)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        return out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "data"
        self.state_file = self.dir / "small-mesh-lab.json"
        self.state = state_of(self.dir)
        self.state_file.write_text(json.dumps(self.state))
        (self.dir / "settings.json").write_text(json.dumps(
            {"lab_run_id": "col1", "part_us": 1000, "livekit_secret": "s3cret-value"}))
        (self.dir / "routes.json").write_text(json.dumps({"lab_run_id": "col1", "primary_rtt_us": 1}))
        for target, name, value in ((deploy, "SETTINGS_FILE", self.dir / "settings.json"),
                                    (deploy, "HUNT_FILE", self.dir / "ecmp-ports.json"),
                                    (deploy.common, "ARTIFACT_DIR", self.dir)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": str(self.root),
                                               "NEEDLETAIL_LINODE_LAB_STATE": str(self.state_file)})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(deploy, "collect_code_paths", lambda: [deploy.common.ROOT])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(deploy, "mesh_worktree", lambda: self.dir / "no-av-mesh")
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, *argv: str) -> tuple[int, str, str]:
        with mock.patch.object(sys, "argv", ["linode-small-mesh-deploy.py", "collect", *argv]), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                deploy.main()
                code = 0
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def manifest(self) -> dict:
        return json.loads((self.root / "col1" / "manifest.json").read_text())

    def files(self) -> set[str]:
        return {entry["path"] for entry in self.manifest()["files"]}


class Collect(Base):
    def staged(self) -> list[pathlib.Path]:
        """Files left in the collects' staging directory."""
        staging = self.dir / deploy.COLLECT_STAGING
        return [path for path in staging.rglob("*") if path.is_file()] if staging.is_dir() else []

    def test_every_node_s_journal_api_and_samplers_and_the_lab_s_files(self):
        FakeNodes(self, self.state)
        code, out, _err = self.collect("--run-id", "col1")
        self.assertEqual(code, 0, out)
        manifest = self.manifest()
        self.assertEqual(manifest["collected"]["complete"], True)
        self.assertEqual(manifest["collected"]["missing"], [])
        self.assertEqual(manifest["kind"], "linode-lab")
        self.assertEqual(manifest["tool"], "deploy-collect")
        self.assertEqual(manifest["status"], "running", "collect never ends the run")
        files = self.files()
        stamp = re.search(r"journal\.(\w+)\.txt\.gz", " ".join(files)).group(1)
        for name in self.state["nodes"]:
            self.assertIn(f"nodes/{name}/journal.{stamp}.txt.gz", files)
            self.assertIn(f"nodes/{name}/samplers/needletail-runs/scale/r1/cpu.ndjson", files)
            self.assertIn(f"nodes/{name}/samplers/needletail-runs/scale/r1/big.log", files)
            self.assertIn(f"nodes/{name}/samplers/var-tmp/compare-sampler.log", files)
        for stem, extension in (("api-node", "json"), ("api-mesh-local", "json"),
                                ("api-stats", "json"), ("api-stalls", "json"),
                                ("api-hops", "ndjson")):
            self.assertIn(f"nodes/lon-edge-1/{stem}.{stamp}.{extension}", files)
            self.assertIn(f"nodes/lon-dist-a/{stem}.{stamp}.{extension}", files)
        self.assertIn(f"nodes/contrib-lon/api-stalls.{stamp}.json", files)
        self.assertNotIn(f"nodes/contrib-lon/api-mesh-local.{stamp}.json", files)
        self.assertFalse(any(path.startswith("nodes/lon-load-1/api-") for path in files))
        self.assertFalse(any(path.endswith(deploy.COLLECT_PARTIAL) for path in files))
        for path in (f"lab/state.{stamp}.json", f"lab/hosts.{stamp}.json",
                     f"lab/deployed.{stamp}.json", f"lab/builds.{stamp}.json",
                     f"lab/settings.{stamp}.json", f"lab/routes.{stamp}.json",
                     "topology/topology-small.json", f"collect/collect.{stamp}.json",
                     f"collect/progress.{stamp}.ndjson"):
            self.assertIn(path, files)
        run = self.root / "col1"
        with gzip.open(run / "nodes" / "lon-edge-1" / f"journal.{stamp}.txt.gz", "rt") as handle:
            self.assertIn("lon-edge-1 boot line", handle.read())
        hosts = json.loads((run / "lab" / f"hosts.{stamp}.json").read_text())
        self.assertEqual(hosts["lon-dist-a"]["host_uuid"], "host-1")
        progress = [json.loads(line) for line in
                    (run / "collect" / f"progress.{stamp}.ndjson").read_text().splitlines()]
        self.assertEqual(sorted(item["node"] for item in progress), sorted(self.state["nodes"]))
        self.assertEqual(manifest["regions"], ["gb-lon"])
        self.assertEqual(manifest["deploy_settings"]["part_us"], 1000)
        self.assertEqual(len(manifest["lab"]["instances"]), 4)
        self.assertEqual(manifest["lab"]["run_tag"], "needletail-small-mesh-col1")
        self.assertEqual(ra.RunArchive.open("col1", root=self.root, record=False).verify(), [])
        self.assertIsNone(ra.require_collected("col1", root=self.root))
        self.assertEqual(self.staged(), [], "every pulled file is moved into the archive")

    def test_settings_written_are_scrubbed_and_the_record_s_digests_kept(self):
        FakeNodes(self, self.state)
        self.collect("--run-id", "col1")
        run = self.root / "col1"
        text = "".join(path.read_text() for path in (run / "lab").glob("*.json"))
        text += (run / "manifest.json").read_text()
        self.assertNotIn("hunter2-not-a-real-token", text)
        self.assertNotIn("s3cret-value", text)
        deployed = json.loads(next((run / "lab").glob("deployed.*.json")).read_text())
        self.assertEqual(deployed["nodes"]["lon-edge-1"]["args_sha256"], SECRET)
        self.assertEqual(deployed["settings"]["api_token"], ra.SCRUBBED)
        self.assertEqual(self.manifest()["deploy_settings"]["api_token"], ra.SCRUBBED)

    def test_one_node_down_slow_or_cut_is_named_and_the_others_are_kept(self):
        FakeNodes(self, self.state, {"lon-edge-1": "down", "lon-load-1": "slow",
                                     "lon-dist-a": "no-stalls", "contrib-lon": "cut"})
        began = time.monotonic()
        code, out, _err = self.collect("--run-id", "col1", "--timeout-s", "3")
        self.assertLess(time.monotonic() - began, 8)
        self.assertEqual(code, 1, out)
        missing = self.manifest()["collected"]["missing"]
        self.assertFalse(self.manifest()["collected"]["complete"])
        self.assertIn("lon-edge-1", missing)
        self.assertIn("lon-load-1", missing)
        self.assertIn("lon-dist-a/api-stalls", missing)
        self.assertIn("contrib-lon", missing)
        files = self.files()
        self.assertTrue(any(path.startswith("nodes/lon-dist-a/journal.") for path in files))
        self.assertTrue(any(path.startswith("nodes/lon-dist-a/api-node.") for path in files))
        # What a cut or slow tar gave before it ended is kept, the file it
        # ended in under <name>.partial and said to be cut.
        self.assertTrue(any(path.startswith("nodes/contrib-lon/journal.") for path in files))
        partial = "nodes/lon-load-1/samplers/needletail-runs/scale/r1/big.log.partial"
        self.assertIn(partial, files)
        self.assertIn("nodes/lon-load-1/samplers/needletail-runs/scale/r1/cpu.ndjson", files)
        record = json.loads(next((self.root / "col1" / "collect").glob("collect.*.json")).read_text())
        self.assertIn("timed out", record["nodes"]["lon-load-1"]["error"])
        self.assertEqual(record["nodes"]["lon-load-1"]["partial"],
                         "samplers/needletail-runs/scale/r1/big.log.partial")
        self.assertIn("ssh exited 255: ssh: connect: no route", record["nodes"]["lon-edge-1"]["error"])
        self.assertIn("tar ended early", record["nodes"]["contrib-lon"]["error"])
        self.assertIn("refusing", ra.require_collected("col1", root=self.root))
        self.assertEqual(self.staged(), [])

    def test_an_api_answer_cut_by_curl_is_missing(self):
        FakeNodes(self, self.state, {"lon-edge-1": "cut-hops"})
        code, _out, _err = self.collect("--run-id", "col1")
        self.assertEqual(code, 1)
        self.assertEqual(self.manifest()["collected"]["missing"], ["lon-edge-1/api-hops"])
        record = json.loads(next((self.root / "col1" / "collect").glob("collect.*.json")).read_text())
        self.assertEqual(record["nodes"]["lon-edge-1"]["http"]["api-hops"], "200")
        self.assertEqual(record["nodes"]["lon-edge-1"]["curl_exit"]["api-hops"], "28")

    def test_collect_again_of_the_missing_nodes_completes_the_run(self):
        FakeNodes(self, self.state, {"lon-edge-1": "down"})
        self.assertEqual(self.collect("--run-id", "col1")[0], 1)
        FakeNodes(self, self.state)
        code, out, _err = self.collect("--run-id", "col1", "--nodes", "lon-edge-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.manifest()["collected"], {**self.manifest()["collected"],
                                                        "complete": True, "missing": []})

    def test_a_node_left_out_keeps_what_its_last_collect_could_not_have(self):
        # contrib-lon's tar was cut after its journal: a later collect of
        # lon-edge-1 alone must not count it as had.
        FakeNodes(self, self.state, {"contrib-lon": "cut", "lon-edge-1": "down",
                                     "lon-dist-a": "no-stalls"})
        self.assertEqual(self.collect("--run-id", "col1")[0], 1)
        self.assertTrue(any(path.startswith("nodes/contrib-lon/journal.") for path in self.files()))
        FakeNodes(self, self.state)
        code, _out, _err = self.collect("--run-id", "col1", "--nodes", "lon-edge-1")
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.manifest()["collected"]["missing"]),
                         ["contrib-lon", "lon-dist-a/api-stalls"])
        self.assertIn("refusing", ra.require_collected("col1", root=self.root))
        # And what each later collect of a node finds is what counts.
        FakeNodes(self, self.state)
        self.assertEqual(self.collect("--run-id", "col1", "--nodes", "contrib-lon")[0], 1)
        self.assertEqual(self.manifest()["collected"]["missing"], ["lon-dist-a/api-stalls"])
        self.assertEqual(self.collect("--run-id", "col1", "--nodes", "lon-dist-a")[0], 0)

    def test_a_node_never_collected_counts_as_missing_when_others_are_named(self):
        FakeNodes(self, self.state)
        code, _out, _err = self.collect("--run-id", "col1", "--nodes", "lon-edge-1")
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.manifest()["collected"]["missing"]),
                         ["contrib-lon", "lon-dist-a", "lon-load-1"])

    def test_the_bound_is_shared_by_more_nodes_than_workers(self):
        fake = FakeNodes(self, self.state, {name: "slow" for name in self.state["nodes"]})
        began = time.monotonic()
        with mock.patch.object(deploy, "COLLECT_WORKERS", 2):
            code, _out, _err = self.collect("--run-id", "col1", "--timeout-s", "2.5")
        self.assertLess(time.monotonic() - began, 6)
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.manifest()["collected"]["missing"]), sorted(self.state["nodes"]))
        record = json.loads(next((self.root / "col1" / "collect").glob("collect.*.json")).read_text())
        errors = sorted(item["error"] for item in record["nodes"].values())
        self.assertEqual(sum("no time left" in error for error in errors), 2, errors)
        self.assertEqual(len(fake.started), 2, "the queued nodes are never asked")

    def test_the_bound_starts_with_the_collect_not_with_its_pulls(self):
        FakeNodes(self, self.state, {"lon-edge-1": "slow"})
        opened = deploy.collect_run

        def slow_open(state):
            time.sleep(1.5)
            return opened(state)

        with mock.patch.object(deploy, "collect_run", slow_open):
            self.collect("--run-id", "col1", "--timeout-s", "4")
        record = json.loads(next((self.root / "col1" / "collect").glob("collect.*.json")).read_text())
        self.assertIn("timed out", record["nodes"]["lon-edge-1"]["error"])
        self.assertLess(record["nodes"]["lon-edge-1"]["seconds"], 3.5)

    def test_what_a_killed_collect_left_staged_is_moved_in_by_the_next(self):
        staging = self.dir / deploy.COLLECT_STAGING / "col1"
        killed = staging / "20261124T000000Z-4242" / "lon-edge-1"
        (killed / "samplers" / "needletail-runs" / "hunt").mkdir(parents=True)
        (killed / "journal.20261124T000000Z.txt.gz").write_bytes(gzip.compress(b"early boot\n"))
        (killed / "samplers" / "needletail-runs" / "hunt" / "cpu.ndjson.partial").write_text(
            '{"unix_s":1}\n')
        (staging / "20261124T000000Z-4242" / ".lock").write_text("")
        # A collect that is still running holds its lock: left alone.
        live = staging / "20261124T000001Z-4343" / "lon-dist-a"
        live.mkdir(parents=True)
        (live / "journal.20261124T000001Z.txt.gz").write_bytes(gzip.compress(b"live\n"))
        holder = os.open(staging / "20261124T000001Z-4343" / ".lock", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, holder)
        deploy.fcntl.flock(holder, deploy.fcntl.LOCK_EX)
        FakeNodes(self, self.state)
        code, out, _err = self.collect("--run-id", "col1")
        self.assertEqual(code, 0, out)
        files = self.files()
        self.assertIn("nodes/lon-edge-1/journal.20261124T000000Z.txt.gz", files)
        self.assertIn("nodes/lon-edge-1/samplers/needletail-runs/hunt/cpu.ndjson.partial", files)
        self.assertFalse((staging / "20261124T000000Z-4242").exists())
        self.assertNotIn("nodes/lon-dist-a/journal.20261124T000001Z.txt.gz", files)
        self.assertTrue((live / "journal.20261124T000001Z.txt.gz").is_file())

    def test_a_collect_interrupted_deletes_nothing_it_pulled(self):
        FakeNodes(self, self.state)
        with mock.patch.object(deploy, "archive_node", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                with mock.patch.object(sys, "argv", ["linode-small-mesh-deploy.py", "collect",
                                                     "--run-id", "col1", "--nodes", "lon-edge-1"]), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    deploy.main()
        self.assertTrue(any(path.name.startswith("journal.") for path in self.staged()))
        code, out, _err = self.collect("--run-id", "col1", "--nodes", "lon-dist-a")
        self.assertEqual(self.staged(), [])
        self.assertTrue(any(path.startswith("nodes/lon-edge-1/journal.") for path in self.files()))

    def test_collect_s_own_failure_is_its_own_exit_and_the_run_is_not_collected(self):
        FakeNodes(self, self.state)
        with mock.patch.object(deploy, "collect_nodes", side_effect=KeyError("nodes")):
            code, _out, err = self.collect("--run-id", "col1")
        self.assertEqual(code, deploy.EXIT_COLLECT_FAILED)
        self.assertIn("KeyError", err)
        collected = self.manifest()["collected"]
        self.assertFalse(collected["complete"])
        self.assertEqual(collected["missing"], ["deploy collect failed: KeyError: 'nodes'"])

    def test_no_lab_state_or_an_unknown_node_is_refused(self):
        FakeNodes(self, self.state)
        code, _out, err = self.collect("--run-id", "col1", "--nodes", "lon-edge-9")
        self.assertEqual(code, 2)
        self.assertIn("lon-edge-9", err)
        self.state_file.unlink()
        code, _out, err = self.collect("--run-id", "col1")
        self.assertEqual(code, 2)
        self.assertIn("lab state missing", err)
        self.assertFalse(self.root.exists() and any(self.root.iterdir()))

    def test_another_lab_s_run_id_is_refused_and_nothing_is_written(self):
        FakeNodes(self, self.state)
        code, _out, err = self.collect("--run-id", "other")
        self.assertEqual(code, 2)
        self.assertIn("'col1'", err)
        self.assertFalse(self.root.exists() and any(self.root.iterdir()))

    def test_an_existing_run_of_another_tool_is_appended_to_with_the_deploy_settings(self):
        ra.RunArchive.create("col1", "linode-lab", tool="linode-stall-hunt.py", argv=["hunt"],
                             root=self.root)
        FakeNodes(self, self.state)
        code, _out, _err = self.collect("--run-id", "col1")
        self.assertEqual(code, 0)
        manifest = self.manifest()
        self.assertEqual(manifest["tool"], "linode-stall-hunt.py")
        self.assertEqual([item["via"] for item in manifest["invocations"]], ["create", "open"])
        self.assertTrue(manifest["collected"]["complete"])
        self.assertEqual(manifest["deploy_settings"],
                         {"part_us": 1000, "api_token": ra.SCRUBBED})
        self.assertNotIn("settings.json", self.files())

    def test_no_deployed_record_or_topology_is_missing(self):
        state = dict(self.state)
        state.pop("deployed")
        pathlib.Path(state["topology_file"]).unlink()
        self.state_file.write_text(json.dumps(state))
        (self.dir / "settings.json").unlink()
        FakeNodes(self, state)
        self.assertEqual(self.collect("--run-id", "col1")[0], 1)
        self.assertEqual(sorted(self.manifest()["collected"]["missing"]),
                         ["lab/deployed", "lab/settings", "topology"])

    def test_the_builds_are_the_deployed_record_s_with_av_mesh_s_playlists_pin(self):
        repo = self.dir / "av-mesh-x"
        repo.mkdir()
        (repo / "Cargo.lock").write_text(
            '[[package]]\nname = "playlists"\nversion = "0.1.6"\nsource = '
            '"git+https://github.com/wavey-ai/playlists.git?rev=7496029#7496029aa"\n')
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        for command in (["init", "-q"], ["add", "Cargo.lock"], ["commit", "-qm", "lock"]):
            subprocess.run(["git", "-C", str(repo), *command], check=True, env=env)
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
        state = dict(self.state)
        state["deployed"] = {**state["deployed"], "nodes": {
            "lon-edge-1": {"build": f"{commit}-1763900000"},
            "lon-dist-a": {"build": f"{commit}-dirty-1763900001"},
            "contrib-lon": {"build": "c0ffee1-1763900002"}}}
        with mock.patch.object(deploy, "mesh_worktree", lambda: repo):
            builds = deploy.deployed_builds(state)
        self.assertEqual(builds["av-mesh"], {f"{commit}-1763900000": ["lon-edge-1"],
                                             f"{commit}-dirty-1763900001": ["lon-dist-a"]})
        self.assertEqual(builds["av-contrib"], {"c0ffee1-1763900002": ["contrib-lon"]})
        self.assertEqual(builds["playlists"][f"{commit}-1763900000"],
                         ["git+https://github.com/wavey-ai/playlists.git?rev=7496029#7496029aa"])
        self.assertEqual(builds["playlists"][f"{commit}-dirty-1763900001"],
                         builds["playlists"][f"{commit}-1763900000"])


class Script(unittest.TestCase):
    """The script one ssh runs on a node, run here with journalctl and curl
    stubbed: it reads, it never removes what it reads."""

    def test_the_node_script_tars_the_journal_and_each_answer(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        stubs = directory / "bin"
        stubs.mkdir()
        (stubs / "journalctl").write_text("#!/bin/sh\necho \"journal $*\"\n")
        (stubs / "curl").write_text(
            "#!/bin/sh\nout=''\nwhile [ $# -gt 0 ]; do\n  case \"$1\" in -o) out=$2; shift;; esac\n"
            "  last=$1; shift\ndone\necho \"{\\\"url\\\": \\\"$last\\\"}\" >\"$out\"\nprintf 200\n"
            "case \"$last\" in *hops) exit 28;; esac\n")
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        node = {"name": "lon-edge-1", "role": "edge", "public_ipv4": "203.0.113.5"}
        script = deploy.collect_script(node, "20261124T000000Z")
        self.assertNotRegex(script.replace('rm -rf "$D"', ""), r"\brm\b")
        result = subprocess.run(["bash", "-c", script], capture_output=True, timeout=60,
                                env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"})
        self.assertEqual(result.returncode, 0, result.stderr)
        target = directory / "node"
        target.mkdir()
        files, partial, ended = deploy.unpack_stream(io.BytesIO(result.stdout), target)
        self.assertIsNone(ended)
        self.assertIsNone(partial)
        self.assertIn("journal.20261124T000000Z.txt.gz", files)
        with gzip.open(target / "journal.20261124T000000Z.txt.gz", "rt") as handle:
            self.assertIn("-b --no-pager -o short-iso-precise", handle.read())
        self.assertEqual((target / "http-status.20261124T000000Z.txt").read_text().split(),
                         ["api-node", "200", "0", "api-mesh-local", "200", "0", "api-stats", "200",
                          "0", "api-stalls", "200", "0", "api-hops", "200", "28"])
        self.assertIn("https://203.0.113.5:19444/api/hops",
                      (target / "api-hops.20261124T000000Z.ndjson").read_text())

    def test_a_tar_cut_part_way_keeps_its_last_member_as_partial(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        body = "".join(f"{os.urandom(16).hex()}\n" for _ in range(2000)).encode()
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for name, data in (("./api-node.T.json", b"{}"), ("needletail-runs/x/big.log", body)):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        cut = out.getvalue()[: len(out.getvalue()) // 2]
        files, partial, ended = deploy.unpack_stream(io.BytesIO(cut), directory)
        self.assertEqual(files, ["api-node.T.json"])
        self.assertEqual(partial, "samplers/needletail-runs/x/big.log.partial")
        self.assertIn("ended early", ended)
        self.assertTrue((directory / partial).is_file())
        self.assertFalse((directory / "samplers/needletail-runs/x/big.log").exists())

    def test_tar_members_map_into_the_node_s_directory_and_never_out_of_it(self):
        self.assertEqual(deploy.collect_member_path("./api-node.T.json"), "api-node.T.json")
        self.assertEqual(deploy.collect_member_path("needletail-runs/hunt/S1/x.csv"),
                         "samplers/needletail-runs/hunt/S1/x.csv")
        self.assertEqual(deploy.collect_member_path("compare-stats.log"),
                         "samplers/var-tmp/compare-stats.log")
        for bad in ("../etc/passwd", "/etc/passwd", "./../x", "needletail-runs/../../x", "./",
                    "needletail-runs/x.log.partial"):
            self.assertIsNone(deploy.collect_member_path(bad), bad)

    def test_the_contributor_is_asked_on_its_port_and_a_load_host_for_no_api(self):
        contributor = deploy.collect_script({"name": "c", "role": "contributor",
                                             "public_ipv4": "203.0.113.1"}, "T")
        self.assertIn("https://203.0.113.1:19443/api/node", contributor)
        self.assertNotIn("/api/mesh/local", contributor)
        load = deploy.collect_script({"name": "l", "role": "load", "public_ipv4": "203.0.113.2"},
                                     "T")
        self.assertNotIn("curl", load)
        self.assertIn("journalctl -b", load)


if __name__ == "__main__":
    unittest.main()
