#!/usr/bin/env python3
"""Tests of the run archive, offline (a temporary root, temporary git repos,
nothing else touched): the manifest survives a crash between updates; a
different file is never overwritten; ids and destinations that escape are
refused; code heads are read from git (clean and dirty); credential files
are refused and command lines scrubbed; verify catches a changed file;
steps and lab cost round-trip; two tools (two processes) append to one run;
the CLI and require_collected.
python3 -m unittest scripts/test_run_archive.py"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("run_archive", HERE / "run_archive.py")
ra = importlib.util.module_from_spec(_spec)
sys.modules["run_archive"] = ra
_spec.loader.exec_module(ra)


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.root = self.tmp / "data"
        self.src = self.tmp / "src"
        self.src.mkdir()
        self.env = mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def make(self, run_id="r1", kind="local-lab", tool="tool-a", **kw):
        return ra.RunArchive.create(run_id, kind, tool=tool, argv=[tool, "--x"], **kw)

    def write(self, name, data):
        path = self.src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path


class RootAndIds(Base):
    def test_data_root_from_env_and_default(self):
        self.assertEqual(ra.data_root(), self.root.resolve())
        with mock.patch.dict(os.environ, {"NEEDLETAIL_DATA_ROOT": ""}):
            self.assertEqual(ra.data_root(), pathlib.Path("~/needletail-data").expanduser().resolve())

    def test_escaping_run_ids_refused(self):
        for bad in ("../x", "..", "a/b", "/etc", "", ".hidden", "a\nb", "abc\n", "x" * 200):
            with self.assertRaises(ra.ArchiveError, msg=bad):
                self.make(bad)
        self.assertFalse((self.tmp / "x").exists())

    def test_escaping_destinations_refused(self):
        archive = self.make()
        src = self.write("a.txt", "a")
        for bad in ("../a.txt", "/tmp/a.txt", "x/../../a.txt", "manifest.json", ".manifest.lock"):
            with self.assertRaises(ra.ArchiveError, msg=bad):
                archive.add_file(src, bad)
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, archive.path / "link")
        with self.assertRaises(ra.ArchiveError):
            archive.add_file(src, "link/a.txt")
        with self.assertRaises(ra.ArchiveError):
            archive.add_file(src, "link/deeper/a.txt")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(archive.add_file(src, "lead/manifest.json"), "lead/manifest.json")

    def test_collision_with_a_different_run_refused(self):
        self.make("r1", tool="tool-a")
        with self.assertRaises(ra.ArchiveError):
            self.make("r1", tool="tool-b")
        with self.assertRaises(ra.ArchiveError):
            self.make("r1", kind="linode-lab", tool="tool-a")
        (self.root / "notarun").mkdir()
        (self.root / "notarun" / "data.csv").write_text("x\n")
        with self.assertRaises(ra.ArchiveError):
            self.make("notarun")
        again = self.make("r1", tool="tool-a")  # same tool reopens and appends
        self.assertEqual(len(again.manifest["invocations"]), 2)

    def test_unknown_kind_refused(self):
        with self.assertRaises(ra.ArchiveError):
            self.make(kind="party")


class Manifest(Base):
    def test_fields_present_at_create(self):
        m = self.make().manifest
        for key in ("schema_version", "run_id", "kind", "started_at_utc", "ended_at_utc", "tool",
                    "command", "host", "code_heads", "deploy_settings", "topology", "regions",
                    "listener_steps", "lab", "collected", "status", "files"):
            self.assertIn(key, m)
        self.assertEqual(m["status"], "running")
        self.assertEqual(m["command"], ["tool-a", "--x"])
        self.assertTrue(m["started_at_utc"].endswith("Z"))

    def test_failed_rename_leaves_previous_manifest(self):
        archive = self.make()
        archive.record_step(1, "opus-1ms", 100)
        before = (archive.path / "manifest.json").read_text()
        with mock.patch.object(ra.os, "replace", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                archive.record_step(2, "opus-1ms", 200)
        self.assertEqual((archive.path / "manifest.json").read_text(), before)
        self.assertEqual(len(json.loads(before)["listener_steps"]), 1)
        archive.record_step(2, "opus-1ms", 200)
        self.assertEqual([s["step"] for s in archive.manifest["listener_steps"]], [1, 2])

    def test_process_killed_mid_update_leaves_valid_manifest(self):
        script = textwrap.dedent(f"""
            import importlib.util, os, sys
            spec = importlib.util.spec_from_file_location("run_archive", {str(HERE / "run_archive.py")!r})
            ra = importlib.util.module_from_spec(spec); sys.modules["run_archive"] = ra
            spec.loader.exec_module(ra)
            a = ra.RunArchive.create("crash", "local-lab", tool="t", argv=["t"])
            a.record_step(1, "lane", 10)
            os.replace = lambda *args: os._exit(9)   # die after the temp file, before the rename
            a.record_step(2, "lane", 20)
        """)
        proc = subprocess.run([sys.executable, "-c", script], env={**os.environ})
        self.assertEqual(proc.returncode, 9)
        archive = ra.RunArchive.open("crash", tool="t2", argv=["t2"])  # lock was released by the kill
        self.assertEqual([s["step"] for s in archive.manifest["listener_steps"]], [1])
        archive.record_step(2, "lane", 20)
        self.assertEqual(len(archive.manifest["listener_steps"]), 2)
        self.assertEqual(archive.verify(), [])

    def test_steps_and_lab_cost_round_trip(self):
        archive = self.make("lab1", kind="linode-lab")
        archive.record_step("ramp-1000", "opus-1ms-udp", 1000, {"gb-lon": 400, "us-east": 600}, 2,
                            joins_per_s=50)
        archive.set_lab(run_tag="needletail-small-mesh-lab1",
                        instances=[{"label": "ntsm-lab1-lon-edge-1", "id": 11, "plan": "g6-dedicated-2",
                                    "region": "gb-lon", "role": "edge", "hourly_usd": 0.054, "hours_billed": 2},
                                   {"label": "ntsm-lab1-contrib", "id": 12, "plan": "g6-dedicated-4",
                                    "region": "gb-lon", "role": "contributor", "hourly_usd": 0.108,
                                    "hours_billed": 2}],
                        created_at_utc="2026-11-23T10:00:00.000Z")
        archive.set_lab(deleted_at_utc="2026-11-23T11:30:00.000Z")
        reopened = ra.RunArchive.open("lab1", tool="other", argv=["other"]).manifest
        step = reopened["listener_steps"][0]
        self.assertEqual((step["step"], step["lane"], step["listeners_total"], step["paths_per_listener"]),
                         ("ramp-1000", "opus-1ms-udp", 1000, 2))
        self.assertEqual(step["listeners_by_region"], {"gb-lon": 400, "us-east": 600})
        self.assertEqual(step["joins_per_s"], 50)
        lab = reopened["lab"]
        self.assertAlmostEqual(lab["cost_usd"], 0.324)
        self.assertEqual(lab["hours_billed"], 4)
        self.assertEqual(lab["deleted_at_utc"], "2026-11-23T11:30:00.000Z")
        archive.set_lab(cost_usd=1.5)
        self.assertEqual(archive.manifest["lab"]["cost_usd"], 1.5)

    def test_topology_nodes_and_regions(self):
        topo = self.write("topology-x.json", json.dumps({"nodes": [
            {"name": "lon-edge-1", "role": "edge", "site": "london", "region": "gb-lon", "type": "g6-dedicated-2"},
            {"name": "tok-edge-1", "role": "edge", "site": "tokyo", "region": "ap-northeast",
             "type": "g6-dedicated-2"}]}))
        archive = self.make(topology=topo, regions=["us-east"])
        m = archive.manifest
        self.assertEqual(m["topology"]["file"], "topology/topology-x.json")
        self.assertEqual(m["topology"]["nodes"][0]["plan"], "g6-dedicated-2")
        self.assertEqual(m["regions"], ["ap-northeast", "gb-lon", "us-east"])

    def test_finish_and_uncollected_linode_lab_is_failed(self):
        archive = self.make("lab2", kind="linode-lab")
        self.assertIn("not collected", ra.require_collected("lab2"))
        archive.mark_collected(False, ["tok-edge-2"])
        self.assertIn("tok-edge-2", ra.require_collected("lab2"))
        self.assertEqual(archive.finish("complete"), "failed")
        m = archive.manifest
        self.assertIn("tok-edge-2", m["status_reason"])
        self.assertIsNotNone(m["ended_at_utc"])
        archive.mark_collected(True)
        self.assertIsNone(ra.require_collected("lab2"))
        self.assertEqual(archive.finish("complete", "all steps ran"), "complete")
        self.assertIn("no archive", ra.require_collected("nothing-here"))
        local = self.make("loc")
        self.assertEqual(local.finish("complete"), "complete")


class Files(Base):
    def test_no_overwrite_of_different_content(self):
        archive = self.make()
        first = self.write("a/log.txt", "one\n")
        second = self.write("b/log.txt", "two\n")
        self.assertEqual(archive.add_file(first, "logs/node.log.txt"), "logs/node.log.txt")
        self.assertEqual(archive.add_file(second, "logs/node.log.txt"), "logs/node.1.log.txt")
        third = self.write("c/log.txt", "three\n")
        self.assertEqual(archive.add_file(third, "logs/node.log.txt"), "logs/node.2.log.txt")
        self.assertEqual(archive.add_file(first, "logs/node.log.txt"), "logs/node.log.txt")  # same: reused
        self.assertEqual((archive.path / "logs/node.log.txt").read_text(), "one\n")
        self.assertEqual((archive.path / "logs/node.1.log.txt").read_text(), "two\n")
        self.assertEqual(len(archive.manifest["files"]), 3)
        name = archive.write_json("logs/node.log.txt", {"x": 1})
        self.assertEqual(name, "logs/node.3.log.txt")
        self.assertEqual((archive.path / "logs/node.log.txt").read_text(), "one\n")

    def test_move_removes_source_and_keeps_data(self):
        archive = self.make()
        src = self.write("big.csv", "t_ms,v\n1,2\n")
        rel = archive.add_file(src, "samples/big.csv", "samples", "cpu samples", move=True,
                               units={"t_ms": "ms"})
        self.assertFalse(src.exists())
        entry = archive.manifest["files"][0]
        self.assertEqual((entry["path"], entry["bytes"], entry["units"]), (rel, 11, {"t_ms": "ms"}))
        self.assertEqual(archive.verify(), [])

    def test_hash_limit_says_so(self):
        archive = self.make()
        with mock.patch.object(ra, "HASH_LIMIT_BYTES", 4):
            archive.add_file(self.write("x.bin", b"0123456789"), "x.bin")
        entry = archive.manifest["files"][0]
        self.assertIsNone(entry["sha256"])
        self.assertIn("hash limit", entry["sha256_skipped"])
        self.assertEqual(entry["bytes"], 10)

    def test_add_tree_with_excludes_and_refusals(self):
        archive = self.make()
        self.write("run/hops/lon-edge-1.ndjson", '{"t_us":1}\n')
        self.write("run/hops/lon-edge-2.ndjson", '{"t_us":2}\n')
        self.write("run/build/huge.o", "obj")
        self.write("run/LINODE_PAT", "a" * 64)
        os.symlink(self.src / "run/hops", self.src / "run/hops-link")
        result = archive.add_tree(self.src / "run", "lead/run", "hop-journal", "hop journals",
                                  exclude=["build/*"])
        self.assertEqual(sorted(result["added"]), ["lead/run/hops/lon-edge-1.ndjson",
                                                   "lead/run/hops/lon-edge-2.ndjson"])
        self.assertEqual(result["refused"], ["LINODE_PAT"])
        self.assertIn("build/huge.o", result["skipped"])
        m = archive.manifest
        self.assertEqual(len(m["files"]), 2)
        self.assertEqual(m["refused"][0]["path"], str(self.src / "run/LINODE_PAT"))
        self.assertFalse((archive.path / "lead/run/LINODE_PAT").exists())

    def test_verify_catches_changes(self):
        archive = self.make()
        archive.add_file(self.write("a.json", '{"a":1}'), "a.json")
        archive.add_file(self.write("b.json", '{"b":1}'), "b.json")
        archive.add_file(self.write("c.json", '{"c":1}'), "c.json")
        self.assertEqual(archive.verify(), [])
        (archive.path / "a.json").write_text('{"a":2}')  # same size, other content
        (archive.path / "b.json").write_text('{"b":12}')
        os.unlink(archive.path / "c.json")
        problems = archive.verify()
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("content changed: a.json" in p for p in problems))
        self.assertTrue(any("size changed: b.json" in p for p in problems))
        self.assertTrue(any("missing: c.json" in p for p in problems))

    def test_ndjson_append_and_finish_hash(self):
        archive = self.make()
        archive.append_ndjson("samples/arrivals.ndjson", [{"seq": 1, "lag_ms": 3.1}], "samples",
                              "per-unit arrivals", {"lag_ms": "ms"})
        archive.append_ndjson("samples/arrivals.ndjson", {"seq": 2, "lag_ms": 2.9})
        entry = archive.manifest["files"][0]
        self.assertIsNone(entry["sha256"])
        self.assertEqual(archive.verify(), [])
        archive.finish("complete")
        entry = archive.manifest["files"][0]
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertNotIn("sha256_skipped", entry)
        self.assertEqual(entry["units"], {"lag_ms": "ms"})
        lines = (archive.path / "samples/arrivals.ndjson").read_text().splitlines()
        self.assertEqual([json.loads(l)["seq"] for l in lines], [1, 2])


class CodeHeads(Base):
    def test_clean_and_dirty(self):
        repo = self.tmp / "needletail"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "rm/arch")
        (repo / "f.txt").write_text("1")
        git(repo, "add", "f.txt")
        git(repo, "commit", "-q", "-m", "one")
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                                stdout=subprocess.PIPE, check=True).stdout.strip()
        heads = ra.code_heads([repo, self.tmp / "nogit"])
        self.assertEqual(heads["needletail"], {"path": str(repo.resolve()), "branch": "rm/arch",
                                               "commit": commit, "dirty": False, "dirty_files": 0})
        self.assertIn("error", heads["nogit"])
        (repo / "f.txt").write_text("2")
        (repo / "new.txt").write_text("n")
        archive = self.make(code_paths=[repo / "."])
        head = archive.manifest["code_heads"]["needletail"]
        self.assertTrue(head["dirty"])
        self.assertEqual(head["dirty_files"], 2)


class Secrets(Base):
    def test_credential_files_refused(self):
        archive = self.make()
        cases = {
            "LINODE_PAT": "0123456789abcdef" * 4 + "\n",
            "CLOUDFLARE_ACCOUNT.env": "CLOUDFLARE_ACCOUNT_ID=abc\nCLOUDFLARE_API_TOKEN=xyz\n",
            "LIVEKIT_CLOUD.env": "export LIVEKIT_API_KEY=k\nexport LIVEKIT_API_SECRET=s\n",
            "deploy.conf": "# settings\nNEEDLETAIL_TOKEN=abc\n",
            "id_ed25519": "whatever",
            "server.pem": "x",
            "notes.txt": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
            "token.txt": "ghp_" + "A" * 36,
        }
        for name, content in cases.items():
            with self.assertRaises(ra.ArchiveError, msg=name):
                archive.add_file(self.write(name, content), name)
            self.assertFalse((archive.path / name).exists(), name)
        ok = {"edge.log": "2026-11-23 started; auth token refreshed\nLINODE_TOKEN is read from env\n",
              "id_ed25519.pub": "ssh-ed25519 AAAA", "settings.env": "PART_MS=1\nREGIONS=gb-lon\n",
              "results.json": '{"token_bucket_rate": 5}'}
        for name, content in ok.items():
            self.assertEqual(archive.add_file(self.write(name, content), name), name)
        self.assertEqual(archive.manifest["files"].__len__(), 4)

    def test_command_lines_and_settings_scrubbed(self):
        hexpat = "f" * 64
        argv = ["curl", "-H", "Authorization: Bearer abcdefghijklmnop", "--token", "s3cret",
                "--api-key=k3y", "LINODE_TOKEN=" + hexpat, "--token-file", "/home/x/LINODE_PAT",
                "https://user:pw@example.com/x?token=abc&n=1", hexpat, "--part-ms", "1",
                "--keyframe-interval=2"]
        archive = ra.RunArchive.create("s", "analysis", tool="t", argv=argv,
                                       deploy_settings={"part_ms": 1, "livekit_api_secret": "zz",
                                                        "token_file": "/x/LINODE_PAT",
                                                        "nested": {"auth": "Authorization: Basic Zm9v"}})
        text = (archive.path / "manifest.json").read_text()
        for secret in ("abcdefghijklmnop", "s3cret", "k3y", hexpat, ":pw@", "token=abc", "Zm9v", '"zz"'):
            self.assertNotIn(secret, text, secret)
        m = archive.manifest
        self.assertEqual(m["command"], [
            "curl", "-H", "Authorization: Bearer [scrubbed]", "--token", "[scrubbed]",
            "--api-key=[scrubbed]", "LINODE_TOKEN=[scrubbed]", "--token-file", "/home/x/LINODE_PAT",
            "https://user:[scrubbed]@example.com/x?token=[scrubbed]&n=1", "[scrubbed]", "--part-ms", "1",
            "--keyframe-interval=2"])
        self.assertEqual(m["deploy_settings"]["part_ms"], 1)
        self.assertEqual(m["deploy_settings"]["token_file"], "/x/LINODE_PAT")
        self.assertEqual(m["invocations"][0]["command"], m["command"])


class Appenders(Base):
    def test_two_tools_append_to_one_run(self):
        self.make("shared", kind="linode-lab", tool="referee")
        script = textwrap.dedent(f"""
            import importlib.util, sys
            spec = importlib.util.spec_from_file_location("run_archive", {str(HERE / "run_archive.py")!r})
            ra = importlib.util.module_from_spec(spec); sys.modules["run_archive"] = ra
            spec.loader.exec_module(ra)
            tool = sys.argv[1]
            a = ra.RunArchive.open("shared", tool=tool, argv=[tool])
            for i in range(40):
                a.append_ndjson("events.ndjson", {{"tool": tool, "i": i}})
                a.record_step(i, tool, i)
            a.write_json(f"{{tool}}/summary.json", {{"tool": tool}})
        """)
        procs = [subprocess.Popen([sys.executable, "-c", script, tool]) for tool in ("lead", "scale-run")]
        self.assertEqual([p.wait() for p in procs], [0, 0])
        archive = ra.RunArchive.open("shared", tool="check", argv=["check"])
        m = archive.manifest
        self.assertEqual(m["tool"], "referee")
        self.assertEqual(len(m["listener_steps"]), 80)
        lines = (archive.path / "events.ndjson").read_text().splitlines()
        self.assertEqual(len(lines), 80)
        self.assertEqual({json.loads(l)["tool"] for l in lines}, {"lead", "scale-run"})
        self.assertEqual(sorted(f["path"] for f in m["files"]),
                         ["events.ndjson", "lead/summary.json", "scale-run/summary.json"])
        self.assertEqual({i["tool"] for i in m["invocations"]}, {"referee", "lead", "scale-run", "check"})
        self.assertEqual(archive.verify(), [])


class Cli(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ra.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_cli_round_trip(self):
        code, out, _ = self.run_cli("--tool", "sh-tool", "create", "c1", "--kind", "linode-lab",
                                    "--command", "lab.sh up --token abc", "--region", "gb-lon",
                                    "--deploy-settings", '{"part_ms": 1}')
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.root.resolve() / "c1"))
        src = self.write("cpu.csv", "t_s,cpu_pct\n0,5\n")
        code, out, _ = self.run_cli("add", "c1", str(src), "--as", "nodes/lon-edge-1/cpu.csv",
                                    "--kind", "samples", "--description", "CPU", "--units", '{"t_s":"s"}')
        self.assertEqual((code, out.strip()), (0, "nodes/lon-edge-1/cpu.csv"))
        self.write("tree/a.log", "a")
        self.assertEqual(self.run_cli("add-tree", "c1", str(self.src / "tree"), "--as", "logs")[0], 0)
        self.assertEqual(self.run_cli("step", "c1", "--step", "1", "--lane", "opus-2.5ms",
                                      "--listeners", "300", "--by-region", "gb-lon=100,us-east=200",
                                      "--paths-per-listener", "2")[0], 0)
        self.assertEqual(self.run_cli("lab", "c1", "--run-tag", "t", "--cost-usd", "2.5")[0], 0)
        code, _, err = self.run_cli("check-collected", "c1")
        self.assertEqual(code, 1)
        self.assertIn("not collected", err)
        self.assertEqual(self.run_cli("collected", "c1", "--complete")[0], 0)
        self.assertEqual(self.run_cli("check-collected", "c1")[0], 0)
        self.assertEqual(self.run_cli("finish", "c1", "--status", "complete")[:2], (0, "complete\n"))
        code, out, _ = self.run_cli("show", "c1")
        m = json.loads(out)
        self.assertEqual(m["command"], ["lab.sh", "up", "--token", "[scrubbed]"])
        self.assertEqual(m["listener_steps"][0]["listeners_by_region"], {"gb-lon": 100, "us-east": 200})
        self.assertEqual(m["lab"]["cost_usd"], 2.5)
        code, out, _ = self.run_cli("list")
        self.assertEqual(out.split("\t")[:3], ["c1", "complete", "linode-lab"])
        self.assertIn("$2.50", out)
        self.assertEqual(self.run_cli("verify", "c1")[0], 0)
        (self.root / "c1" / "logs" / "a.log").write_text("b")
        code, out, _ = self.run_cli("verify", "c1")
        self.assertEqual(code, 1)
        self.assertIn("content changed: logs/a.log", out)
        code, _, err = self.run_cli("--tool", "other", "create", "c1", "--kind", "linode-lab")
        self.assertEqual(code, 2)
        self.assertIn("refused", err)


if __name__ == "__main__":
    unittest.main()
