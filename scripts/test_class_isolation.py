#!/usr/bin/env python3
"""Tests of the class isolation and audio hot path rendering of the deploy
tool and the lab: every flag rendered on by default and off with
class_isolation=false, the pieces on their own, the class ports checked for
collisions, the primary backbone admitting the contributor's repair, the
host qdisc and its check, the unit's capabilities, the loss injection on
both class ports, and the lab's qdisc inside the namespaces.
python3 -m unittest scripts/test_class_isolation.py"""

from __future__ import annotations

import importlib.util
import io
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_lab as lab  # noqa: E402


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_module("deploy", "linode-small-mesh-deploy.py")
mesh_run = load_module("mesh_run", "linode-small-mesh-run.py")
COMPILER = deploy.common.ROOT / "target" / "release" / "needletail-compile"
ROUTES = {"best_direct_rtt_us": 74000, "primary_rtt_us": 74000, "primary_jitter_us": 1000,
          "secondary_rtt_us": 80000, "secondary_jitter_us": 1000}


def value_of(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


@unittest.skipUnless(COMPILER.is_file(), "needs target/release/needletail-compile")
class Render(unittest.TestCase):
    """The lab's topology (edge_lab.NODES) rendered as the deploy renders it."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for target, name, value in ((deploy, "SETTINGS_FILE", self.dir / "settings.json"),
                                    (deploy, "HUNT_FILE", self.dir / "ecmp-ports.json"),
                                    (deploy.common, "ARTIFACT_DIR", self.dir)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def render(self, overrides=(), types: dict | None = None) -> dict[str, list[str]]:
        (self.dir / "settings.json").unlink(missing_ok=True)
        with redirect_stderr(io.StringIO()):
            settings = deploy.load_settings(list(overrides))
        state = lab.lab_state()
        for name, kind in (types or {}).items():
            state["nodes"][name]["type"] = kind
        plan = deploy.compile_plan(deploy.build_program(state, settings, ROUTES))
        return {name: item["args"] for name, item in
                deploy.render(state, settings, plan).items()}

    def test_every_piece_is_on_by_default(self):
        args = self.render()
        contrib = args["contrib-lon"]
        for flag, value in (("--relay-class-lanes", "true"), ("--relay-class-port-offset", "1000"),
                            ("--relay-audio-socket-priority", "6"),
                            ("--relay-video-pacing-mbps", "0"),
                            ("--relay-audio-inline", "true"), ("--audio-hot-thread", "true"),
                            ("--relay-repair-both-lanes", "true")):
            self.assertEqual(value_of(contrib, flag), value, flag)
        for name in ("relay-primary", "relay-secondary", "lon-dist-a", "new-dist-b"):
            self.assertEqual(value_of(args[name], "--relay-class-threads"), "true", name)
            self.assertEqual(value_of(args[name], "--relay-class-port-offset"), "1000", name)
            self.assertEqual(value_of(args[name], "--relay-class-realtime"), "true", name)
            # The lab shares one host: never pinned.
            self.assertNotIn("--relay-class-cpus", args[name])
        for name, node_args in args.items():
            if name == "contrib-lon":
                continue
            # Every node of a mesh uses the same offset.
            self.assertEqual(value_of(node_args, "--cache-mesh-class-port-offset"), "1000", name)
        for name in ("lon-dist-a", "lon-edge-1", "new-edge-2"):
            self.assertEqual(value_of(args[name], "--client-udp-class-sockets"), "true", name)
        # A backbone serves no UDP clients; an edge runs no relay ingest.
        self.assertNotIn("--client-udp-class-sockets", args["relay-primary"])
        self.assertNotIn("--relay-class-threads", args["lon-edge-1"])
        # Each flag is rendered once: clap refuses a flag given twice.
        for name, node_args in args.items():
            flags = [arg for arg in node_args if arg.startswith("--")]
            repeated = {flag for flag in flags if flags.count(flag) > 1} - {
                "--peer", "--relay-forward", "--telemetry-peer", "--failover-entry",
                "--hls-rendition", "--relay-failover-listener", "--mesh-bind"}
            self.assertFalse(repeated, name)

    def test_the_primary_backbone_admits_repair_from_the_contributor(self):
        args = self.render()
        # The contributor's primary lane (a `source` carrier) now carries
        # repair: its backbone takes the lane as promoted, as the warm
        # secondary's already was.
        self.assertIn("--relay-primary-promoted", args["relay-primary"])
        self.assertEqual(args["relay-secondary"].count("--relay-primary-promoted"), 1)
        # A distributor's primary parent is a backbone, not the contributor.
        self.assertNotIn("--relay-primary-promoted", args["lon-dist-a"])
        args = self.render(["relay_repair_both_lanes=false"])
        self.assertNotIn("--relay-primary-promoted", args["relay-primary"])
        self.assertEqual(value_of(args["contrib-lon"], "--relay-repair-both-lanes"), "false")

    def test_class_isolation_false_renders_every_piece_off(self):
        args = self.render(["class_isolation=false"])
        contrib = args["contrib-lon"]
        self.assertEqual(value_of(contrib, "--relay-class-lanes"), "false")
        self.assertEqual(value_of(contrib, "--relay-audio-socket-priority"), "0")
        for name in ("relay-primary", "lon-dist-b"):
            self.assertEqual(value_of(args[name], "--relay-class-threads"), "false")
            self.assertNotIn("--relay-class-realtime", args[name])
        for name, node_args in args.items():
            if name != "contrib-lon":
                self.assertEqual(value_of(node_args, "--cache-mesh-class-port-offset"), "0")
        self.assertEqual(value_of(args["lon-edge-3"], "--client-udp-class-sockets"), "false")
        # The audio hot path is item 3, a setting of its own.
        self.assertEqual(value_of(contrib, "--relay-audio-inline"), "true")
        args = self.render(["audio_hot_path=false"])
        self.assertEqual(value_of(args["contrib-lon"], "--relay-audio-inline"), "false")
        self.assertEqual(value_of(args["contrib-lon"], "--audio-hot-thread"), "false")

    def test_a_piece_set_on_its_own_wins_over_class_isolation(self):
        args = self.render(["cache_mesh_class_split=false"])
        self.assertEqual(value_of(args["lon-edge-1"], "--cache-mesh-class-port-offset"), "0")
        self.assertEqual(value_of(args["lon-dist-a"], "--relay-class-threads"), "true")
        args = self.render(["class_isolation=false", "client_udp_class_sockets=true"])
        self.assertEqual(value_of(args["lon-edge-1"], "--client-udp-class-sockets"), "true")
        self.assertEqual(value_of(args["lon-dist-a"], "--relay-class-threads"), "false")
        # The contributor's lanes and the relays' threads share the port
        # convention: one setting renders both.
        args = self.render(["relay_class_split=false"])
        self.assertEqual(value_of(args["contrib-lon"], "--relay-class-lanes"), "false")
        self.assertEqual(value_of(args["relay-primary"], "--relay-class-threads"), "false")
        self.assertEqual(value_of(args["lon-edge-1"], "--cache-mesh-class-port-offset"), "1000")

    def test_pinning_follows_the_instance(self):
        args = self.render(types={"relay-primary": "g6-dedicated-2",
                                  "lon-dist-a": "g6-nanode-1",
                                  "contrib-lon": "g6-dedicated-4"})
        self.assertEqual(value_of(args["relay-primary"], "--relay-class-cpus"), "0,1")
        self.assertNotIn("--relay-class-cpus", args["lon-dist-a"])
        self.assertEqual(value_of(args["contrib-lon"], "--audio-hot-cpu"), "2")
        args = self.render(["relay_class_cpus=", "audio_hot_cpu=3"],
                           types={"relay-primary": "g6-dedicated-2"})
        self.assertNotIn("--relay-class-cpus", args["relay-primary"])
        self.assertEqual(value_of(args["contrib-lon"], "--audio-hot-cpu"), "3")
        self.assertEqual(value_of(self.render(types={"contrib-lon": "g6-dedicated-2"})
                                  ["contrib-lon"], "--audio-hot-cpu"), "-1")

    def test_an_extra_argument_overrides_the_rendered_one(self):
        args = self.render(['extra_edge_args=["--client-udp-class-sockets","false"]',
                            'extra_contributor_args=["--relay-audio-inline","false"]'])
        edge = args["lon-edge-2"]
        self.assertEqual(edge.count("--client-udp-class-sockets"), 1)
        self.assertEqual(value_of(edge, "--client-udp-class-sockets"), "false")
        contrib = args["contrib-lon"]
        self.assertEqual(contrib.count("--relay-audio-inline"), 1)
        self.assertEqual(value_of(contrib, "--relay-audio-inline"), "false")

    def test_the_class_ports(self):
        plan = deploy.class_plan({**deploy.DEFAULT_SETTINGS})
        args = self.render()
        binds = {name: deploy.udp_binds(node_args, plan) for name, node_args in args.items()}
        ports = {name: {port for _socket, port, _arg in items} for name, items in binds.items()}
        self.assertTrue({22001, 23001, 29101, 30101} <= ports["relay-primary"])
        self.assertTrue({22002, 23002} <= ports["relay-secondary"])
        self.assertTrue({22200, 23200, 22201, 23201, 29101, 30101} <= ports["lon-dist-a"])
        self.assertTrue({29101, 30101} <= ports["lon-edge-1"])
        # The contributor's class pair shares each lane's endpoint.
        self.assertTrue({22301, 22302} <= ports["contrib-lon"])
        self.assertFalse({23301, 23302} & ports["contrib-lon"])
        # Without the split there is no twin.
        args = self.render(["class_isolation=false"])
        off = deploy.class_plan({**deploy.DEFAULT_SETTINGS, "class_isolation": False})
        self.assertNotIn(23001, {port for _s, port, _a in
                                 deploy.udp_binds(args["relay-primary"], off)})

    def test_a_class_port_that_collides_is_refused(self):
        # 22001 + 100 is the backbone's media ingest port, 22101.
        with self.assertRaises(ValueError) as caught:
            self.render(["class_port_offset=100"])
        self.assertIn("22101", str(caught.exception))
        # Off, the offset is never used.
        self.render(["class_port_offset=100", "class_isolation=false"])

    def test_settings_that_cannot_be_rendered_are_refused(self):
        for bad in ("class_port_offset=0", "class_port_offset=40000", "host_qdisc=fq",
                    "relay_class_cpus=0", "audio_hot_cpu=x", "relay_video_pacing_mbps=x"):
            with self.subTest(bad), self.assertRaises(SystemExit):
                with redirect_stderr(io.StringIO()):
                    deploy.load_settings([bad])


class Host(unittest.TestCase):
    def test_the_unit_has_the_capabilities_of_the_fast_threads(self):
        unit = deploy.unit_text("needletail-mesh", "info", dict(deploy.DEFAULT_SETTINGS))
        self.assertIn("AmbientCapabilities=CAP_SYS_NICE CAP_NET_ADMIN\n", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        unit = deploy.unit_text("needletail-mesh", "info",
                                {**deploy.DEFAULT_SETTINGS, "service_capabilities": False})
        self.assertNotIn("AmbientCapabilities", unit)

    PRIO = """== eth0
qdisc prio 1: root refcnt 3 bands 3 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1
 Sent 1 bytes 1 pkt (dropped 0, overlimits 0 requeues 0)
qdisc pfifo 10: parent 1:1 limit 10000p
 Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)
qdisc fq 20: parent 1:2 limit 65536p flow_limit 4096p
 Sent 1 bytes 1 pkt (dropped 0, overlimits 0 requeues 0)
"""

    def test_the_qdisc_check(self):
        self.assertEqual(deploy.check_host_qdisc(self.PRIO, "prio"), [])
        two = self.PRIO + self.PRIO.replace("eth0", "eth1").replace("qdisc fq 20", "qdisc sfq 20")
        self.assertEqual(deploy.check_host_qdisc(two, "prio"), ["eth1"])
        default = "== eth0\nqdisc mq 0: root\nqdisc fq_codel 0: parent :1 limit 10240p\n"
        self.assertEqual(deploy.check_host_qdisc(default, "prio"), ["eth0"])
        self.assertEqual(deploy.check_host_qdisc(default, "none"), [])
        self.assertEqual(deploy.check_host_qdisc(self.PRIO, "none"), ["eth0"])
        self.assertEqual(deploy.check_host_qdisc("", "prio"), ["no NIC"])

    def test_the_qdisc_records_and_refuses(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        node = {"name": "lon-dist-a"}
        with mock.patch.object(deploy.common, "ARTIFACT_DIR", directory), \
                mock.patch.object(deploy.common, "ssh_text", return_value=self.PRIO) as ssh:
            deploy.apply_host_qdisc(node, dict(deploy.DEFAULT_SETTINGS))
            self.assertIn("prio bands 3 priomap " + deploy.PRIOMAP, ssh.call_args[0][1])
            self.assertEqual((directory / "qdisc" / "lon-dist-a.txt").read_text(), self.PRIO)
            with self.assertRaises(RuntimeError):
                deploy.apply_host_qdisc(node, {**deploy.DEFAULT_SETTINGS, "host_qdisc": "none"})

    @unittest.skipUnless(shutil.which("unshare") and shutil.which("tc"), "needs unshare and tc")
    def test_the_qdisc_commands_run_and_audio_takes_band_0(self):
        """The deploy's tc commands on a veth of a rootless namespace: the
        qdisc is accepted, the check passes, and a datagram of an
        SO_PRIORITY 6 socket leaves through band 0 and one of priority 0
        through the fq band."""
        body = deploy.HOST_QDISC_SCRIPT["prio"].split("for path", 1)[1]
        commands = [line.strip() for line in body.splitlines()
                    if line.strip().startswith(("tc ", "echo "))]
        script = "\n".join([
            "set -e",
            "ip link add qa type veth peer name qb",
            "sysctl -qw net.ipv6.conf.qa.disable_ipv6=1 || true",
            "ip addr add 10.211.0.1/24 dev qa",
            "ip link set qa up; ip link set qb up",
            # No ARP: the datagrams leave at once.
            "ip neigh replace 10.211.0.2 lladdr 02:00:00:00:00:02 dev qa",
            "dev=qa",
            *commands[:-2],
            "python3 -c \"import socket\n"
            "for prio in (6, 0):\n"
            "    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            "    s.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, prio)\n"
            "    s.sendto(bytes(prio + 100), ('10.211.0.2', 9))\"",
            *commands[-2:],
        ])
        result = subprocess.run(["unshare", "-Urn", "bash", "-c", script],
                                capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and "unshare" in result.stderr:
            self.skipTest(f"no user namespace: {result.stderr.strip()}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(deploy.check_host_qdisc(result.stdout, "prio"), [], result.stdout)
        sent = {}
        lines = result.stdout.splitlines()
        for line, following in zip(lines, lines[1:]):
            if line.startswith("qdisc "):
                sent[line.split()[2]] = following.split()
        # One datagram each: the audio one (106 bytes of payload) in band 0,
        # the video one (100) in the fq band.
        self.assertEqual(sent["10:"][3:5], ["1", "pkt"], result.stdout)
        self.assertEqual(sent["20:"][3:5], ["1", "pkt"], result.stdout)
        self.assertEqual(int(sent["10:"][1]) - int(sent["20:"][1]), 6, result.stdout)


class LossInjection(unittest.TestCase):
    def test_a_fault_covers_both_class_ports(self):
        settings = dict(deploy.DEFAULT_SETTINGS)
        self.assertEqual(mesh_run.class_split_ports(22200, settings, "relay"), [22200, 23200])
        self.assertEqual(mesh_run.class_split_ports(29101, settings, "mesh"), [29101, 30101])
        self.assertEqual(mesh_run.class_split_ports(
            29101, {**settings, "cache_mesh_class_split": False}, "mesh"), [29101])
        # A settings file saved before the class settings existed.
        self.assertEqual(mesh_run.class_split_ports(22200, {"class_isolation": False}, "relay"),
                         [22200])
        apply, remove = mesh_run.loss_commands([22200, 23200], 2.0, "NTSMRELAY")
        for port in (22200, 23200):
            self.assertIn(f"iptables -I INPUT 1 ! -i lo -p udp --dport {port} -j NTSMRELAY",
                          apply)
            self.assertIn(f"iptables -D INPUT ! -i lo -p udp --dport {port} -j NTSMRELAY",
                          remove)
        self.assertEqual(apply.count("--probability 0.02 -j DROP"), 1)
        self.assertTrue(remove.endswith("echo ${dropped:-0}"))
        # One port, as before.
        apply, _ = mesh_run.loss_commands(29101, 1.0, "NTSMMESH")
        self.assertIn("--dport 29101", apply)


class LabQdisc(unittest.TestCase):
    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        patcher = mock.patch.object(lab, "WORK", self.work)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.batches = []

        def run_in(name, command, check=True, timeout=60, text_input=None):
            if text_input is not None:
                self.batches.append((name, text_input))
                return ""
            shown = "qdisc netem 1: root refcnt 2 limit 50000 delay 500us\n"
            if any("handle 10: prio" in line for _name, batch in self.batches[-1:]
                   for line in batch.splitlines()):
                shown += "qdisc prio 10: parent 1:1 bands 3\n"
            return shown

        patcher = mock.patch.object(lab, "run_in", side_effect=run_in)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_node_sends_through_the_host_qdisc(self):
        lab.save("settings.json", {"host_qdisc": "prio"})
        lab.apply_netem("lon-edge-1", "eth0", lab.netem_spec(delay_ms=0.5))
        name, batch = self.batches[-1]
        self.assertEqual(name, "lon-edge-1")
        self.assertIn(f"qdisc add dev eth0 parent 1:1 handle 10: prio bands 3 priomap "
                      f"{deploy.PRIOMAP}", batch)
        self.assertIn("parent 10:1 handle 11: pfifo", batch)
        self.assertEqual(lab.load("qdisc.json"), {"lon-edge-1/eth0": "prio"})
        # The network side of a link and a reader host are no node.
        lab.apply_netem(None, "p5", lab.netem_spec(delay_ms=0.5))
        lab.apply_netem("lon-readers", "eth0", lab.netem_spec(delay_ms=0.5))
        for _name, batch in self.batches[-2:]:
            self.assertNotIn("prio", batch)

    def test_without_the_host_qdisc_the_link_is_as_before(self):
        lab.save("settings.json", {"host_qdisc": "none"})
        lab.apply_netem("lon-edge-1", "eth0", lab.netem_spec(delay_ms=0.5, jitter_ms=0.2))
        _name, batch = self.batches[-1]
        self.assertNotIn("prio", batch)
        self.assertIn("parent 1:1 pfifo limit 50000", batch)
        # With it, the prio's FIFO bands stand in for the `ordered` pfifo.
        lab.save("settings.json", {"host_qdisc": "prio"})
        lab.apply_netem("lon-edge-1", "eth0", lab.netem_spec(delay_ms=0.5, jitter_ms=0.2))
        _name, batch = self.batches[-1]
        self.assertNotIn("parent 1:1 pfifo", batch)
        self.assertIn("parent 1:1 handle 10: prio", batch)


if __name__ == "__main__":
    unittest.main()
