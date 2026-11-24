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
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_archive as ra  # noqa: E402

_spec = importlib.util.spec_from_file_location("deploy_collect", HERE / "linode-small-mesh-deploy.py")
deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy)
REAL_RUN = subprocess.run
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


class FakeNodes:
    """ssh to each node, faked: `ok` (every file), `down` (ssh fails),
    `slow` (it does not answer within the bound), `no-stalls` (its
    /api/stalls answers 404) and `cut` (its tar ends part-way)."""

    def __init__(self, case: unittest.TestCase, state: dict, behaviour: dict | None = None):
        self.by_address = {node["public_ipv4"]: name for name, node in state["nodes"].items()}
        self.behaviour = behaviour or {}
        self.scripts: dict[str, str] = {}
        self.timeouts: dict[str, float] = {}
        patcher = mock.patch.object(deploy.subprocess, "run", self.run)
        patcher.start()
        case.addCleanup(patcher.stop)

    def run(self, command, **kwargs):
        if command[0] != "ssh":
            return REAL_RUN(command, **kwargs)
        name = self.by_address[command[-2].split("@", 1)[1]]
        script = command[-1]
        self.scripts[name] = script
        self.timeouts[name] = kwargs["timeout"]
        how = self.behaviour.get(name, "ok")
        if how == "down":
            return subprocess.CompletedProcess(command, 255, b"", b"ssh: connect: no route")
        if how == "slow":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        stamp = re.search(r"T=(\d{8}T\d{6}Z)", script).group(1)
        data = self.tarball(name, stamp, script, how)
        if how == "cut":
            data = data[: len(data) // 2]
        kwargs["stdout"].write(data)
        return subprocess.CompletedProcess(command, 0, None, b"")

    @staticmethod
    def tarball(name: str, stamp: str, script: str, how: str) -> bytes:
        files = {f"./journal.{stamp}.txt.gz": gzip.compress(f"{name} boot line\n".encode() * 2000)}
        statuses = []
        for stem, path in re.findall(r'"\$D/(api-[a-z-]+)\.\$T\.\w+" -w \S+ \'?https://[^/]+(/[^\'\s]+)',
                                     script):
            code = "404" if (how == "no-stalls" and stem == "api-stalls") else "200"
            statuses.append(f"{stem} {code}")
            extension = "ndjson" if stem == "api-hops" else "json"
            files[f"./{stem}.{stamp}.{extension}"] = json.dumps({"path": path, "node": name}).encode()
        files[f"./http-status.{stamp}.txt"] = ("\n".join(statuses) + "\n").encode()
        files["needletail-runs/scale/r1/cpu.ndjson"] = b'{"unix_s":1,"cpu_pct":3}\n' * 500
        files["compare-sampler.log"] = b"1763900000 cpu 1 2 3\n"
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
    def test_every_node_s_journal_api_and_samplers_and_the_lab_s_files(self):
        fake = FakeNodes(self, self.state)
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
            self.assertIn(f"nodes/{name}/samplers/var-tmp/compare-sampler.log", files)
        for stem, extension in (("api-node", "json"), ("api-mesh-local", "json"),
                                ("api-stats", "json"), ("api-stalls", "json"),
                                ("api-hops", "ndjson")):
            self.assertIn(f"nodes/lon-edge-1/{stem}.{stamp}.{extension}", files)
            self.assertIn(f"nodes/lon-dist-a/{stem}.{stamp}.{extension}", files)
        self.assertIn(f"nodes/contrib-lon/api-stalls.{stamp}.json", files)
        self.assertNotIn(f"nodes/contrib-lon/api-mesh-local.{stamp}.json", files)
        self.assertFalse(any(path.startswith("nodes/lon-load-1/api-") for path in files))
        for path in (f"lab/state.{stamp}.json", f"lab/hosts.{stamp}.json",
                     f"lab/deployed.{stamp}.json", f"lab/settings.{stamp}.json",
                     f"lab/routes.{stamp}.json", "topology/topology-small.json",
                     f"collect/collect.{stamp}.json"):
            self.assertIn(path, files)
        run = self.root / "col1"
        with gzip.open(run / "nodes" / "lon-edge-1" / f"journal.{stamp}.txt.gz", "rt") as handle:
            self.assertIn("lon-edge-1 boot line", handle.read())
        hosts = json.loads((run / "lab" / f"hosts.{stamp}.json").read_text())
        self.assertEqual(hosts["lon-dist-a"]["host_uuid"], "host-1")
        self.assertEqual(manifest["regions"], ["gb-lon"])
        self.assertEqual(len(manifest["lab"]["instances"]), 4)
        self.assertEqual(manifest["lab"]["run_tag"], "needletail-small-mesh-col1")
        self.assertTrue(all(timeout <= deploy.COLLECT_TIMEOUT_S for timeout in fake.timeouts.values()))
        self.assertEqual(ra.RunArchive.open("col1", root=self.root, record=False).verify(), [])
        self.assertIsNone(ra.require_collected("col1", root=self.root))

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

    def test_one_node_down_slow_or_cut_is_named_and_the_others_are_kept(self):
        FakeNodes(self, self.state, {"lon-edge-1": "down", "lon-load-1": "slow",
                                     "lon-dist-a": "no-stalls", "contrib-lon": "cut"})
        code, out, _err = self.collect("--run-id", "col1", "--timeout-s", "40")
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
        # What a cut tar gave before it ended is kept.
        self.assertTrue(any(path.startswith("nodes/contrib-lon/") for path in files))
        record = json.loads(next((self.root / "col1" / "collect").glob("collect.*.json")).read_text())
        self.assertIn("timed out", record["nodes"]["lon-load-1"]["error"])
        self.assertIn("ssh exited 255", record["nodes"]["lon-edge-1"]["error"])
        self.assertIn("refusing", ra.require_collected("col1", root=self.root))

    def test_collect_again_of_the_missing_nodes_completes_the_run(self):
        FakeNodes(self, self.state, {"lon-edge-1": "down"})
        self.assertEqual(self.collect("--run-id", "col1")[0], 1)
        FakeNodes(self, self.state)
        code, out, _err = self.collect("--run-id", "col1", "--nodes", "lon-edge-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.manifest()["collected"], {**self.manifest()["collected"],
                                                        "complete": True, "missing": []})

    def test_a_node_never_collected_counts_as_missing_when_others_are_named(self):
        FakeNodes(self, self.state)
        code, _out, _err = self.collect("--run-id", "col1", "--nodes", "lon-edge-1")
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.manifest()["collected"]["missing"]),
                         ["contrib-lon", "lon-dist-a", "lon-load-1"])

    def test_the_time_bound_is_shared_by_the_nodes(self):
        fake = FakeNodes(self, self.state)
        self.collect("--run-id", "col1", "--timeout-s", "25")
        self.assertEqual(sorted(fake.timeouts), sorted(self.state["nodes"]))
        self.assertTrue(all(0 < timeout <= 25 for timeout in fake.timeouts.values()))

    def test_another_lab_s_run_id_is_refused_and_nothing_is_written(self):
        FakeNodes(self, self.state)
        code, _out, err = self.collect("--run-id", "other")
        self.assertEqual(code, 2)
        self.assertIn("'col1'", err)
        self.assertFalse(self.root.exists() and any(self.root.iterdir()))

    def test_an_existing_run_of_another_tool_is_appended_to(self):
        ra.RunArchive.create("col1", "linode-lab", tool="linode-stall-hunt.py", argv=["hunt"],
                             root=self.root)
        FakeNodes(self, self.state)
        code, _out, _err = self.collect("--run-id", "col1")
        self.assertEqual(code, 0)
        manifest = self.manifest()
        self.assertEqual(manifest["tool"], "linode-stall-hunt.py")
        self.assertEqual([item["via"] for item in manifest["invocations"]], ["create", "open"])
        self.assertTrue(manifest["collected"]["complete"])

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
            "  last=$1; shift\ndone\necho \"{\\\"url\\\": \\\"$last\\\"}\" >\"$out\"\nprintf 200\n")
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
        tarball = directory / "node.tar.gz"
        tarball.write_bytes(result.stdout)
        files, ended = deploy.unpack_node(tarball, target)
        self.assertIsNone(ended)
        self.assertIn("journal.20261124T000000Z.txt.gz", files)
        with gzip.open(target / "journal.20261124T000000Z.txt.gz", "rt") as handle:
            self.assertIn("-b --no-pager -o short-iso-precise", handle.read())
        self.assertEqual((target / "http-status.20261124T000000Z.txt").read_text().split(),
                         ["api-node", "200", "api-mesh-local", "200", "api-stats", "200",
                          "api-stalls", "200", "api-hops", "200"])
        self.assertIn("https://203.0.113.5:19444/api/hops",
                      (target / "api-hops.20261124T000000Z.ndjson").read_text())

    def test_tar_members_map_into_the_node_s_directory_and_never_out_of_it(self):
        self.assertEqual(deploy.collect_member_path("./api-node.T.json"), "api-node.T.json")
        self.assertEqual(deploy.collect_member_path("needletail-runs/hunt/S1/x.csv"),
                         "samplers/needletail-runs/hunt/S1/x.csv")
        self.assertEqual(deploy.collect_member_path("compare-stats.log"),
                         "samplers/var-tmp/compare-stats.log")
        for bad in ("../etc/passwd", "/etc/passwd", "./../x", "needletail-runs/../../x", "./"):
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
