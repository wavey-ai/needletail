#!/usr/bin/env python3
"""The route survey of the stall hunt's lab: which routes a long-haul link
has at one instant, how they move, and what a second flow would gain.

    linode-route-survey.py plan    [--topology T]
    linode-route-survey.py start   [--out DIR] [--hours H] [--dry]
    linode-route-survey.py stop    [--dry]
    linode-route-survey.py collect [--out DIR] [--dry]
    linode-route-survey.py report  DIR [--nodes-dir DIR] [--json OUT]

Design: docs/plans/resilient-mesh/D-hunt.md, section 7.6. The survey runs on
the bare nodes of the hunt's lab (deploy/low-latency/topology-hunt.json,
`hunt.survey`): from each sender to each receiver, PROBE_SETS_PER_SECOND
times a second, one probe from each of 64 UDP source ports and from the
two default ports of a relay link. Routers hash the 5-tuple, so a source
port picks one of the parallel long-haul paths. The receiver stamps every
probe in the kernel (SO_TIMESTAMPNS) and keeps every one: a record of 16
bytes (source port, link, probe id, receive minus send in ns), about 19 MB
a link an hour. Each `start` writes into a directory of its own on the
nodes (named by its start second, which survey.json keeps), so a survey
started again after a replacement never mixes its records with the first
one's (probe ids count from 0 at each start); `collect --out DIR` fetches
the start that DIR/survey.json names. It creates nothing and calls no cloud
API: it reads the lab's state file (NEEDLETAIL_LINODE_LAB_STATE) for
addresses and reaches the nodes over ssh. `start`, `stop` and `collect` take `--dry`, which
prints every command and runs none; `plan` prints the links and the
commands from the topology alone.

`report` replays three policies on the record, offline:
  default    the link's default source port, as a mesh without the hunt
  kept       the best port of the record's first minute, kept (the deploy
             tool's `hunt` at bring-up)
  flows      base plus extra as designed (D-hunt.md 2.6): the base flow on
             the default port carries every part, an extra flow carries a
             copy on the port the chooser picks while a second route class
             (300 us or more apart) exists; first arrival wins
and prints, per link and policy: the median and p99 of the first-arrival
delay, its excess over the best port of the moment, the share of time
within 0.3 ms of it, the steps (a change of 1 ms or more that lasts 5 s)
and the longest time to recover; per link: how many route classes exist at
one instant and for how long, and how often every port steps together.
Two verdicts, fixed before the run (7.6):
  gain     on a link, flows is 1 ms or more ahead of the default port for
           10 % of the time or more. Flows stay on by default if any
           long-haul link shows it.
  chooser  its excess over the best port of the moment is 0.3 ms or less
           at p99 outside the 3 s after a step, and no step costs more
           than 3.0 s of excess.
Seconds in which an end's own `needletail-nodecheck watch` shows a stopped
tick are left out (`--nodes-dir`: a stopped receiver delays every port
alike and would read as a route step).

Delays are receive minus send on two clocks. Both ends run chrony; what
the verdicts use is the difference between ports of one link at one
instant, in which the offset cancels.

The chooser is the same as av-mesh's (fast_plane/flows.rs): the cases of
scripts/tests/fixtures/flows-chooser.json are kept byte for byte in both
repositories and both implementations pass them.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import small_mesh_common as common  # noqa: E402
import stall_records  # noqa: E402

ROOT = common.ROOT
TOPOLOGY = ROOT / "deploy" / "low-latency" / "topology-hunt.json"
OUT = ROOT / "target" / "stall-hunt"
REMOTE = "/var/lib/needletail-runs/survey"
# A relay child's receiver port and its parents' default sender ports (the
# deploy tool's ports of the first far child), and the port range of the
# deploy tool's hunt: the survey's 5-tuples are a relay link's.
RECEIVER_PORT = 22200
DEFAULT_PORTS = (22401, 22402)
FIRST_PORT = 24000
PORTS = 64
PROBE_SETS_PER_SECOND = 5
INTERVAL_MS = 1000 // PROBE_SETS_PER_SECOND
# A probe on the wire: source port, link, probe id, send time (Unix ns).
PROBE = struct.Struct("!HHIQ")
# A record on disk: source port, link, probe id, receive minus send (ns).
RECORD = struct.Struct("!HHIq")
# The chooser's settings (D-hunt.md 2.6: --relay-flow-class-us,
# --relay-flow-hold-reports, --relay-flow-idle-s, and the 150 us within
# which the current extra is kept).
CLASS_US = 300
KEEP_US = 150
HOLD_REPORTS = 3
IDLE_S = 60.0
REPORT_MS = 500
SILENT_S = 5.0
# The verdicts' numbers (7.6).
NEAR_BEST_US = 300
STEP_US = 1000
STEP_HOLD_S = 5
RECOVER_S = 3
GAIN_US = 1000
GAIN_SHARE = 0.10

# The probe agent of a survey end. It is started twice on a node that both
# sends and receives. It stops by itself at `stop_unix_ns` (the lab's
# deadline), so a survey that nobody stops ends with the lab.
AGENT = r"""
import json, socket, struct, sys, time
mode, config = sys.argv[1], json.load(open(sys.argv[2]))
PROBE, RECORD = struct.Struct("!HHIQ"), struct.Struct("!HHIq")
stop = config["stop_unix_ns"]
if mode == "send":
    interval, start = config["interval_ms"] * 1_000_000, config["start_unix_ns"]
    socks = []
    for port in config["ports"]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((config["bind"], port))
        except OSError as error:
            print(f"port {port}: {error}", file=sys.stderr, flush=True)
            continue
        sock.setblocking(False)
        socks.append((port, sock))
    targets = [(link["id"], (link["target"], config["receiver_port"])) for link in config["links"]]
    while True:
        now = time.time_ns()
        if now >= stop:
            break
        # The id of a set is its place on the clock, so a sender that fell
        # behind skips ids and a set's id still gives its time.
        index = 0 if now < start else (now - start) // interval + 1
        wait = (start + index * interval - time.time_ns()) / 1e9
        if wait > 0:
            time.sleep(wait)
        for port, sock in socks:
            for link, target in targets:
                try:
                    sock.sendto(PROBE.pack(port, link, index & 0xFFFFFFFF, time.time_ns()), target)
                except OSError:
                    pass
else:
    SO_TIMESTAMPNS = 35
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
    sock.bind(("0.0.0.0", config["receiver_port"]))
    sock.settimeout(1.0)
    files = {link["id"]: open(f"{config['dir']}/link-{link['id']}.rec", "wb")
             for link in config["links"]}
    flushed = time.time()
    while time.time_ns() < stop:
        try:
            data, ancillary, _flags, _peer = sock.recvmsg(64, 128)
        except OSError:
            data = b""
        if len(data) == PROBE.size:
            port, link, index, sent = PROBE.unpack(data)
            stamp = None
            for level, kind, payload in ancillary:
                if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS and len(payload) >= 16:
                    seconds, nanos = struct.unpack("@qq", payload[:16])
                    stamp = seconds * 1_000_000_000 + nanos
            if stamp is None:
                stamp = time.time_ns()
            if link in files:
                files[link].write(RECORD.pack(port, link, index, stamp - sent))
        if time.time() - flushed >= 1.0:
            for handle in files.values():
                handle.flush()
            flushed = time.time()
    for handle in files.values():
        handle.close()
"""


PYTHON = ("command -v python3 >/dev/null || { apt-get update -q >/dev/null && "
          "DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-minimal >/dev/null; }")


# ------------------------------------------------------------------ the lab

def survey_ports() -> list[int]:
    """The source ports probed on every link: the two default ports, then
    the 64 candidates."""
    return list(DEFAULT_PORTS) + list(range(FIRST_PORT, FIRST_PORT + PORTS))


def survey_links(topology: dict, present: set[str] | None = None) -> list[dict]:
    """The links of the survey, in a fixed order with ids from 0: every
    sender to every receiver but itself (`hunt.survey` of the topology).
    With `present`, only the ends that the lab has (a site left out)."""
    survey = (topology.get("hunt") or {}).get("survey") or {}
    names = {node["name"] for node in topology["nodes"]}
    for name in survey.get("senders", []) + survey.get("receivers", []):
        if name not in names:
            raise SystemExit(f"hunt.survey names {name}, which is not a node of the topology")
    links = []
    for sender in survey.get("senders", []):
        for receiver in survey.get("receivers", []):
            if sender == receiver:
                continue
            links.append({"id": len(links), "sender": sender, "receiver": receiver})
    if present is not None:
        links = [link for link in links
                 if link["sender"] in present and link["receiver"] in present]
    return links


def topology_of(state: dict | None, path: str | None) -> dict:
    file = pathlib.Path(path or (state or {}).get("topology_file") or TOPOLOGY)
    return json.loads(file.read_text())


def run_dir(start_ns: int | None) -> str:
    """The nodes' directory of one start of the survey."""
    return f"{REMOTE}/{'<start>' if start_ns is None else start_ns // 1_000_000_000}"


def agent_configs(links: list[dict], addresses: dict[str, str], start_ns: int,
                  stop_ns: int) -> dict[str, dict]:
    """{node: {"send": config, "recv": config}} of the survey's ends
    (`addresses`: a node's public IPv4)."""
    out: dict[str, dict] = {}
    for link in links:
        sender = out.setdefault(link["sender"], {})
        sender.setdefault("send", {
            "bind": addresses[link["sender"]], "ports": survey_ports(),
            "receiver_port": RECEIVER_PORT, "interval_ms": INTERVAL_MS,
            "start_unix_ns": start_ns, "stop_unix_ns": stop_ns, "links": []})
        sender["send"]["links"].append({"id": link["id"], "target": addresses[link["receiver"]]})
        receiver = out.setdefault(link["receiver"], {})
        receiver.setdefault("recv", {"receiver_port": RECEIVER_PORT, "dir": run_dir(start_ns),
                                     "stop_unix_ns": stop_ns, "links": []})
        receiver["recv"]["links"].append({"id": link["id"]})
    return out


def start_commands(configs: dict[str, dict], start_ns: int) -> list[tuple[str, str]]:
    """(node, shell command) in order: every end gets the agent and its
    configuration in this start's directory, receivers start before
    senders."""
    directory = run_dir(start_ns)
    out = []
    for name in sorted(configs):
        # The deploy tool installs nothing on a bare node: the agent's
        # interpreter is put there if the image lacks it.
        files = [PYTHON, f"mkdir -p {directory}",
                 f"cat >{REMOTE}/agent.py <<'PY'\n{AGENT}\nPY"]
        for mode, config in sorted(configs[name].items()):
            files.append(f"cat >{directory}/{mode}.json <<'JSON'\n{json.dumps(config)}\nJSON")
        out.append((name, "\n".join(files)))
    for mode in ("recv", "send"):
        for name in sorted(configs):
            if mode in configs[name]:
                out.append((name, (
                    f"pkill -f '^python3 {REMOTE}/agent.py {mode}' ; "
                    f"(setsid nohup python3 {REMOTE}/agent.py {mode} {directory}/{mode}.json "
                    f">{directory}/{mode}.log 2>&1 </dev/null &)")))
    return out


def stop_commands(names: list[str]) -> list[tuple[str, str]]:
    return [(name, f"pkill -f '^python3 {REMOTE}/agent.py' ; true") for name in sorted(names)]


def say(node: str, command: str) -> None:
    print(f"ssh {node} {shlex.quote(command)}")


def lab_addresses(state: dict) -> dict[str, str]:
    return {name: node["public_ipv4"] for name, node in state["nodes"].items()}


def out_dir(args: argparse.Namespace, state: dict | None) -> pathlib.Path:
    if getattr(args, "out", None):
        return pathlib.Path(args.out)
    run = (state or {}).get("run_id") or "dry"
    return OUT / run / "survey"


def stop_ns_for(state: dict | None, hours: float | None, now_ns: int) -> int:
    """When the agents stop by themselves: `hours` from now, else the lab's
    watchdog deadline, else two hours."""
    if hours:
        return now_ns + int(hours * 3600e9)
    deadline = (state or {}).get("watchdog_deadline_unix")
    return int(deadline * 1e9) if deadline else now_ns + int(2 * 3600e9)


def command_plan(args: argparse.Namespace) -> None:
    topology = topology_of(None, args.topology)
    links = survey_links(topology)
    addresses = {node["name"]: f"<public:{node['name']}>" for node in topology["nodes"]}
    print(f"{len(links)} links, {len(survey_ports())} source ports each, "
          f"{PROBE_SETS_PER_SECOND} probe sets a second: "
          f"{len(survey_ports()) * PROBE_SETS_PER_SECOND * RECORD.size * 3600 / 1e6:.0f} MB of "
          "records a link an hour")
    for link in links:
        print(f"  link {link['id']:>2}  {link['sender']} -> {link['receiver']}")
    for name, command in start_commands(agent_configs(links, addresses, 0, 0), 0):
        say(name, command)


def command_start(args: argparse.Namespace) -> None:
    state = None if args.dry and not common.state_path().is_file() else common.load_state()
    topology = topology_of(state, args.topology)
    present = set(state["nodes"]) if state else None
    links = survey_links(topology, present)
    addresses = (lab_addresses(state) if state else
                 {node["name"]: f"<public:{node['name']}>" for node in topology["nodes"]})
    now_ns = time.time_ns()
    start_ns = (now_ns // 1_000_000_000 + 5) * 1_000_000_000
    stop_ns = stop_ns_for(state, args.hours, now_ns)
    configs = agent_configs(links, addresses, start_ns, stop_ns)
    record = {"schema": "needletail.route-survey.v1", "start_unix_ns": start_ns,
              "stop_unix_ns": stop_ns, "interval_ms": INTERVAL_MS,
              "receiver_port": RECEIVER_PORT, "default_ports": list(DEFAULT_PORTS),
              "ports": survey_ports(),
              "links": [{**link, "sender_ipv4": addresses[link["sender"]],
                         "receiver_ipv4": addresses[link["receiver"]]} for link in links]}
    for name, command in start_commands(configs, start_ns):
        if args.dry:
            say(name, command)
        else:
            common.ssh(state["nodes"][name], command)
    if args.dry:
        return
    directory = out_dir(args, state)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "survey.json").write_text(json.dumps(record, indent=1) + "\n")
    print(f"survey started on {len(configs)} nodes, {len(links)} links; {directory}/survey.json")


def survey_nodes(state: dict | None, topology: dict) -> list[str]:
    links = survey_links(topology, set(state["nodes"]) if state else None)
    return sorted({link["sender"] for link in links} | {link["receiver"] for link in links})


def command_stop(args: argparse.Namespace) -> None:
    state = None if args.dry and not common.state_path().is_file() else common.load_state()
    topology = topology_of(state, args.topology)
    for name, command in stop_commands(survey_nodes(state, topology)):
        if args.dry:
            say(name, command)
        else:
            common.ssh(state["nodes"][name], command, check=False)


def command_collect(args: argparse.Namespace) -> None:
    state = None if args.dry and not common.state_path().is_file() else common.load_state()
    topology = topology_of(state, args.topology)
    directory = out_dir(args, state)
    started = directory / "survey.json"
    if not started.is_file() and not args.dry:
        sys.exit(f"{started} is missing: this directory's survey was never started")
    remote = run_dir(json.loads(started.read_text())["start_unix_ns"] if started.is_file()
                     else None)
    links = survey_links(topology, set(state["nodes"]) if state else None)
    receivers = sorted({link["receiver"] for link in links})
    failures = []
    for name in survey_nodes(state, topology):
        wanted = [f"{remote}/*.log"] + ([f"{remote}/link-*.rec"] if name in receivers else [])
        for source in wanted:
            target = directory / "logs" / name if source.endswith(".log") else directory
            if args.dry:
                print(f"rsync {name}:{source} {target}/")
                continue
            target.mkdir(parents=True, exist_ok=True)
            try:
                common.rsync_from(state["nodes"][name], source, str(target) + "/")
            except RuntimeError as error:
                failures.append(str(error))
    if failures:
        sys.exit("survey collect: " + "; ".join(failures))
    if not args.dry:
        print(f"records of {len(links)} links in {directory}")


# --------------------------------------------------------------- the record

def write_records(path, records) -> None:
    """Records ((source port, link, probe id, delta ns), ...) to a file."""
    with open(path, "wb") as handle:
        for record in records:
            handle.write(RECORD.pack(*record))


def read_records(path) -> list[tuple[int, int, int, int]]:
    """Every whole record of a file (a record cut off at the end is left)."""
    data = pathlib.Path(path).read_bytes()
    whole = len(data) - len(data) % RECORD.size
    return [RECORD.unpack_from(data, offset) for offset in range(0, whole, RECORD.size)]


def probe_sets(records) -> dict[int, dict[int, int]]:
    """{probe id: {source port: delta ns}}: the first arrival of each probe."""
    sets: dict[int, dict[int, int]] = {}
    for port, _link, index, delta in records:
        sets.setdefault(index, {}).setdefault(port, delta)
    return sets


def median(values) -> float | None:
    values = sorted(values)
    if not values:
        return None
    return values[(len(values) - 1) // 2]


def percentile(values, p: float) -> float | None:
    values = sorted(values)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


# -------------------------------------------------------------- the chooser

class Chooser:
    """Which port the extra flow of one path uses (D-hunt.md 2.6, Choice).

    From each report, `delays` is {port: relative delay in us} (the median
    of the port's last three probes minus the smallest such median) and
    `eligible` the ports seen in two of their last three probes. With b the
    base port's delay:
    - base slow (b >= class_us): wanted is the eligible port of smallest
      delay (the current extra while it is within keep_us of it);
    - base on the fastest class: wanted is the eligible port of smallest
      delay among those at b + class_us or more, the nearest other class
      (the current extra while it qualifies and is within keep_us of it);
      none when no port qualifies.
    A port becomes the extra when it was wanted in hold_reports reports in a
    row. "None" must hold for idle_s before the extra stops. A report
    without the base port's delay changes nothing. Ties go to the lower
    port. Before the first decision the extra carries nothing.

    `state`: "no-reports" before the first report; "diverse" while an extra
    carries; "single-class" from idle_s of "none" until an extra carries
    again; "probing" otherwise.
    """

    def __init__(self, base: int, class_us: int = CLASS_US, keep_us: int = KEEP_US,
                 hold_reports: int = HOLD_REPORTS, idle_s: float = IDLE_S) -> None:
        self.base = base
        self.class_us, self.keep_us = class_us, keep_us
        self.hold_reports, self.idle_s = hold_reports, idle_s
        self.reset()

    def reset(self) -> None:
        """The child went silent: no extra, as before the first report."""
        self.extra: int | None = None
        self.wanted: int | None = None
        self.count = 0
        self.none_since: float | None = None
        self.single = False
        self.state = "no-reports"

    def want(self, delays: dict[int, int], eligible) -> int | None:
        base = delays[self.base]
        pool = sorted(port for port in eligible
                      if port != self.base and delays.get(port) is not None)
        if base < self.class_us:
            pool = [port for port in pool if delays[port] >= base + self.class_us]
        if not pool:
            return None
        best = min(pool, key=lambda port: (delays[port], port))
        if self.extra in pool and delays[self.extra] <= delays[best] + self.keep_us:
            return self.extra
        return best

    def report(self, t_s: float, delays: dict[int, int], eligible) -> int | None:
        """Take one report; returns the extra port from now on (or None)."""
        if delays.get(self.base) is None:
            return self.extra
        want = self.want(delays, eligible)
        if want is None:
            self.wanted, self.count = None, 0
            if self.none_since is None:
                self.none_since = t_s
            if t_s - self.none_since >= self.idle_s:
                self.extra, self.single = None, True
        else:
            self.none_since = None
            if want == self.extra:
                self.wanted, self.count = None, 0
            else:
                if want == self.wanted:
                    self.count += 1
                else:
                    self.wanted, self.count = want, 1
                if self.count >= self.hold_reports:
                    self.extra, self.wanted, self.count = want, None, 0
        if self.extra is not None:
            self.single = False
            self.state = "diverse"
        else:
            self.state = "single-class" if self.single else "probing"
        return self.extra


def run_fixture(case: dict, settings: dict) -> list[dict]:
    """A fixture case through the chooser: [{"extra", "state"}] per report."""
    chooser = Chooser(case["base"], settings["class_us"], settings["keep_us"],
                      settings["hold_reports"], settings["idle_s"])
    out = []
    for report in case["reports"]:
        delays = {int(port): value for port, value in report["delays_us"].items()}
        extra = chooser.report(report["t_ms"] / 1000, delays, report["eligible"])
        out.append({"extra": extra, "state": chooser.state})
    return out


# --------------------------------------------------------------- the replay

def report_inputs(history: dict[int, list], ports: list[int]) -> tuple[dict[int, int], set[int]]:
    """A report as the child makes it (2.6, RLP2) from each port's last
    three probes (delta ns or None): ({port: relative delay us}, eligible)."""
    medians = {}
    eligible = set()
    for port in ports:
        seen = [value for value in history.get(port, [])[-3:] if value is not None]
        if len(seen) >= 2:
            eligible.add(port)
        if seen:
            medians[port] = median(seen)
    if not medians:
        return {}, eligible
    floor = min(medians.values())
    return {port: int((value - floor) // 1000) for port, value in medians.items()}, eligible


def replay_flows(sets: dict[int, dict[int, int]], ports: list[int], base: int,
                 interval_ms: int = INTERVAL_MS, chooser: Chooser | None = None) -> dict[int, int | None]:
    """{probe id: the extra port in force when that set was sent} for the
    chooser fed at two probes a second: of the record's sets it sees the
    one at or after each half second, makes a report from each port's last
    three, and its choice holds from the next set on. A hole of SILENT_S or
    more in the record is a silent child: the extra stops."""
    chooser = chooser or Chooser(base)
    history: dict[int, list] = {port: [] for port in ports}
    extra_at: dict[int, int | None] = {}
    last = None
    next_report_ms = None
    for index in sorted(sets):
        if last is not None and (index - last) * interval_ms >= SILENT_S * 1000:
            chooser.reset()
            history = {port: [] for port in ports}
            next_report_ms = None
        extra_at[index] = chooser.extra
        now_ms = index * interval_ms
        if next_report_ms is None:
            next_report_ms = now_ms
        if now_ms >= next_report_ms:
            for port in ports:
                history[port].append(sets[index].get(port))
            delays, eligible = report_inputs(history, ports)
            chooser.report(now_ms / 1000, delays, eligible)
            next_report_ms += REPORT_MS
            if next_report_ms <= now_ms:
                next_report_ms = now_ms + REPORT_MS
        last = index
    return extra_at


def per_second(sets: dict[int, dict[int, int]], interval_ms: int, value) -> dict[int, float]:
    """{second: the median over the second's sets of value(index, set)},
    sets for which it is None left out."""
    buckets: dict[int, list] = {}
    for index, by_port in sets.items():
        found = value(index, by_port)
        if found is not None:
            buckets.setdefault(index * interval_ms // 1000, []).append(found)
    return {second: median(values) for second, values in buckets.items()}


def steps_of(series: dict[int, float], step_ns: int = STEP_US * 1000,
             hold_s: int = STEP_HOLD_S) -> list[dict]:
    """The steps of a per-second series: seconds from which it stays
    `step_ns` or more away from the median of the `hold_s` seconds before,
    for `hold_s` seconds. [{"second", "from_ns", "to_ns"}]."""
    seconds = sorted(series)
    out = []
    index = hold_s
    while index + hold_s <= len(seconds):
        before = median(series[second] for second in seconds[index - hold_s:index])
        after = [series[second] for second in seconds[index:index + hold_s]]
        if all(value - before >= step_ns for value in after) \
                or all(before - value >= step_ns for value in after):
            out.append({"second": seconds[index], "from_ns": before, "to_ns": median(after)})
            index += hold_s
        else:
            index += 1
    return out


def route_classes(delays: list[float], class_ns: int = CLASS_US * 1000) -> int:
    """How many route classes a set of port delays holds: groups apart by
    `class_ns` or more."""
    ordered = sorted(delays)
    if not ordered:
        return 0
    return 1 + sum(1 for a, b in zip(ordered, ordered[1:]) if b - a >= class_ns)


def excess_runs(excess: dict[int, float], limit_ns: int) -> list[tuple[int, int]]:
    """Runs of consecutive seconds over `limit_ns`: [(first second, seconds)]."""
    runs = []
    for second in sorted(excess):
        if excess[second] <= limit_ns:
            continue
        if runs and runs[-1][0] + runs[-1][1] == second:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((second, 1))
    return runs


def step_groups(port_steps: dict[int, list[dict]]) -> list[dict]:
    """The route steps of a link from each port's own steps: steps of
    different ports within a second of each other are one.
    [{"second", "ports"}]."""
    groups: list[dict] = []
    marks = sorted((step["second"], port) for port, steps in port_steps.items() for step in steps)
    for second, port in marks:
        if groups and second - groups[-1]["last"] <= 1:
            groups[-1]["ports"].add(port)
            groups[-1]["last"] = second
        else:
            groups.append({"second": second, "last": second, "ports": {port}})
    return [{"second": group["second"], "ports": group["ports"]} for group in groups]


def analyse_link(records, ports: list[int], default_port: int, interval_ms: int = INTERVAL_MS,
                 left_out_seconds: set[int] = frozenset()) -> dict:
    """One link's record replayed under the three policies. Figures are per
    second: a port's delay in a second is the median of its probes in it,
    and the best port of the moment is the port whose median is lowest."""
    sets = probe_sets(records)
    if not sets:
        return {"error": "no records"}
    first = min(sets)
    minute = [index for index in sets if (index - first) * interval_ms < 60_000]

    def first_minute(port: int) -> float:
        seen = [sets[index][port] for index in minute if port in sets[index]]
        return median(seen) if seen else float("inf")

    kept_port = min(ports, key=lambda port: (first_minute(port), port))
    extra_at = replay_flows(sets, ports, default_port, interval_ms)

    def flows(index: int, by_port: dict[int, int]):
        seen = [by_port[port] for port in (default_port, extra_at.get(index)) if port in by_port]
        return min(seen) if seen else None

    def kept(series: dict[int, float]) -> dict[int, float]:
        return {second: value for second, value in series.items()
                if second not in left_out_seconds}

    by_port = {port: kept(per_second(sets, interval_ms, lambda _i, found, port=port: found.get(port)))
               for port in ports}
    seconds = sorted(set().union(*[set(series) for series in by_port.values()]))
    if not seconds:
        return {"error": "every second of the record is left out"}
    best = {second: min(series[second] for series in by_port.values() if second in series)
            for second in seconds}
    policies = {"default": by_port[default_port], "kept": by_port[kept_port],
                "flows": kept(per_second(sets, interval_ms, flows))}
    groups = step_groups({port: steps_of(series) for port, series in by_port.items()})
    after_step = {group["second"] + offset for group in groups for offset in range(RECOVER_S + 1)}
    out_policies = {}
    for name, series in policies.items():
        excess = {second: series[second] - best[second] for second in sorted(series)
                  if second in best}
        runs = excess_runs(excess, NEAR_BEST_US * 1000)
        settled = [value for second, value in excess.items() if second not in after_step]
        out_policies[name] = {
            "seconds": len(excess),
            "one_way_ms": {"p50": ms(median(series.values())),
                           "p99": ms(percentile(series.values(), 99))},
            "excess_ms": {"p50": ms(median(excess.values())),
                          "p99": ms(percentile(excess.values(), 99)),
                          "p99_outside_steps": ms(percentile(settled, 99))},
            "near_best_share": share(sum(1 for value in excess.values()
                                         if value <= NEAR_BEST_US * 1000), len(excess)),
            "steps": len(steps_of(series)),
            "longest_excess_s": max((length for _second, length in runs), default=0),
        }
    both = [second for second in policies["default"] if second in policies["flows"]]
    ahead = share(sum(1 for second in both if policies["default"][second]
                      - policies["flows"][second] >= GAIN_US * 1000), len(both))
    classes = [route_classes([series[second] for series in by_port.values() if second in series])
               for second in seconds]
    settled_excess = out_policies["flows"]["excess_ms"]["p99_outside_steps"]
    return {
        "seconds": len(seconds), "left_out_seconds": len(left_out_seconds),
        "default_port": default_port, "kept_port": kept_port,
        "policies": out_policies,
        "extra_carried_share": share(sum(1 for index in sets if extra_at.get(index) is not None),
                                     len(sets)),
        "extra_ports": sorted({port for port in extra_at.values() if port is not None}),
        "classes": {"max": max(classes),
                    "two_or_more_share": share(sum(1 for count in classes if count >= 2),
                                               len(classes))},
        "route_steps": len(groups),
        "steps_of_every_port_together": sum(1 for group in groups
                                            if len(group["ports"]) >= len(ports) - 1),
        "ahead_share": ahead,
        "gain": ahead is not None and ahead >= GAIN_SHARE,
        "chooser_ok": ((settled_excess is None or settled_excess <= NEAR_BEST_US / 1000)
                       and out_policies["flows"]["longest_excess_s"] <= RECOVER_S),
    }


def ms(value) -> float | None:
    return None if value is None else round(value / 1e6, 3)


def share(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


def stopped_seconds(nodes_dir: pathlib.Path, names: list[str], start_ns: int) -> set[int]:
    """The seconds of the survey's clock (from `start_ns`) in which a node
    of `names` shows a stopped tick in its watch record, with the second
    before and after."""
    out: set[int] = set()
    for name in names:
        events = stall_records.read_stalls(nodes_dir / name / "watch.ndjson")
        for event in stall_records.stopped_ticks(events, min_ms=0):
            begin, end = stall_records.interval(event)
            first = (begin * 1000 - start_ns) // 1_000_000_000 - 1
            last = (end * 1000 - start_ns) // 1_000_000_000 + 1
            out.update(range(int(first), int(last) + 1))
    return out


def analyse(directory: pathlib.Path, nodes_dir: pathlib.Path | None = None) -> dict:
    survey = json.loads((directory / "survey.json").read_text())
    ports = [int(port) for port in survey["ports"]]
    default_port = int(survey["default_ports"][0])
    links = {}
    for link in survey["links"]:
        path = directory / f"link-{link['id']}.rec"
        name = f"{link['sender']}->{link['receiver']}"
        if not path.is_file():
            links[name] = {"error": "no record file"}
            continue
        left_out = (stopped_seconds(nodes_dir, [link["sender"], link["receiver"]],
                                    survey["start_unix_ns"]) if nodes_dir else set())
        links[name] = analyse_link(read_records(path), ports, default_port,
                                   survey["interval_ms"], left_out)
    scored = [item for item in links.values() if "error" not in item]
    return {"schema": "needletail.route-survey-report.v1", "links": links,
            "gain_links": sorted(name for name, item in links.items() if item.get("gain")),
            "flows_stay_on": any(item["gain"] for item in scored) if scored else None,
            "chooser_ok": all(item["chooser_ok"] for item in scored) if scored else None}


def format_report(result: dict) -> str:
    lines = []
    for name, link in sorted(result["links"].items()):
        if "error" in link:
            lines.append(f"{name}: {link['error']}")
            continue
        lines.append(
            f"{name}: {link['seconds']} s ({link['left_out_seconds']} left out for a stopped end), "
            f"route classes at one instant up to {link['classes']['max']}, two or more for "
            f"{pct(link['classes']['two_or_more_share'])} of the time; {link['route_steps']} route "
            f"steps, {link['steps_of_every_port_together']} of every port together; the extra flow "
            f"carried {pct(link['extra_carried_share'])} of the time")
        lines.append(f"  {'policy':<8} {'port':>6} {'p50 ms':>9} {'p99 ms':>9} {'excess p50':>11} "
                     f"{'p99':>8} {'near best':>10} {'steps':>6} {'longest excess s':>17}")
        for policy, port in (("default", link["default_port"]), ("kept", link["kept_port"]),
                             ("flows", "+")):
            item = link["policies"][policy]
            lines.append(
                f"  {policy:<8} {port!s:>6} {num(item['one_way_ms']['p50']):>9} "
                f"{num(item['one_way_ms']['p99']):>9} {num(item['excess_ms']['p50']):>11} "
                f"{num(item['excess_ms']['p99']):>8} {pct(item['near_best_share']):>10} "
                f"{item['steps']:>6} {item['longest_excess_s']:>17}")
        lines.append(f"  gain: flows 1 ms or more ahead of the default port "
                     f"{pct(link['ahead_share'])} of the time: {'yes' if link['gain'] else 'no'}; "
                     f"chooser within 0.3 ms at p99 outside steps and no step over 3 s: "
                     f"{'yes' if link['chooser_ok'] else 'no'}")
    if result["flows_stay_on"] is None:
        lines.append("no link has a record: nothing is decided")
        return "\n".join(lines)
    if result["flows_stay_on"]:
        lines.append("flows stay on by default: the gain shows on "
                     + ", ".join(result["gain_links"]))
    else:
        lines.append("no link shows the gain: relay_flows 1 in the scale run's settings (D13); "
                     "no diversity was found to use")
    lines.append("chooser criterion: " + ("met on every link" if result["chooser_ok"]
                                          else "NOT met on every link"))
    return "\n".join(lines)


def pct(value) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def num(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def command_report(args: argparse.Namespace) -> None:
    directory = pathlib.Path(args.dir)
    if not (directory / "survey.json").is_file():
        sys.exit(f"{directory}/survey.json is missing: not a survey's directory")
    result = analyse(directory, pathlib.Path(args.nodes_dir) if args.nodes_dir else None)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1) + "\n")
    print(format_report(result))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="the links and every command, from the topology alone")
    plan.add_argument("--topology", default=None)
    plan.set_defaults(handler=command_plan)
    for name, handler, text in (("start", command_start, "start the agents on the survey's ends"),
                                ("stop", command_stop, "stop them"),
                                ("collect", command_collect, "fetch the receivers' records")):
        item = sub.add_parser(name, help=text)
        item.add_argument("--topology", default=None,
                          help="default: the lab state's topology file")
        item.add_argument("--out", default=None,
                          help="default target/stall-hunt/<run id>/survey")
        item.add_argument("--dry", action="store_true",
                          help="print every command and run none")
        if name == "start":
            item.add_argument("--hours", type=float, default=None,
                              help="the agents stop by themselves after this long (default: at "
                                   "the lab's watchdog deadline)")
        item.set_defaults(handler=handler, hours=None)
    report = sub.add_parser("report", help="replay the three policies on a collected record")
    report.add_argument("dir")
    report.add_argument("--nodes-dir", default=None,
                        help="the hunt run's nodes directory: seconds in which an end's watch "
                             "record shows a stopped tick are left out")
    report.add_argument("--json", default=None)
    report.set_defaults(handler=command_report)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
