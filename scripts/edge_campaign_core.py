"""The machinery of the fault campaign: a scenario registry, the context that
a scenario runs in, readers, and the evidence file of a scenario.

A scenario states a question and a pass criterion, runs readers against the
lab while it injects a fault, and records what the readers saw and what each
node did. `edge-campaign.py` holds the scenarios.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import threading
import time
import traceback

import archived_run
import edge_lab as lab
import lab_guard as guard


def runs_dir(ident: str | None = lab.LAB_ID) -> pathlib.Path:
    """Where the campaign's runs and their `latest` link go: one directory
    for each lab id (NEEDLETAIL_EDGE_LAB_ID), so that two labs' runs never
    share a `latest`."""
    return lab.ROOT / "target" / (f"edge-campaign-{ident}" if ident else "edge-campaign")


RUNS = runs_dir()
# Groups whose scenarios run a browser or a player of the host (fixed ports
# and profiles): one lab of the host at a time.
PLAYER_GROUPS = ("players", "ladder")
SCHEMA = "needletail.edge-campaign.scenario.v1"
VIDEO = lab.VIDEO_STREAM
AUDIO = lab.AUDIO_STREAM
HQ = lab.HQ_STREAM

REGISTRY: dict[str, "Scenario"] = {}


class Scenario:
    def __init__(self, name, group, question, criterion, function, needs=None, slow=False):
        self.name = name
        self.group = group
        self.question = question
        self.criterion = criterion
        self.function = function
        # Lab settings that the scenario needs, as deploy overrides.
        self.needs = needs or []
        self.slow = slow


def scenario(name: str, group: str, question: str, criterion: str,
             needs: list[str] | None = None, slow: bool = False):
    def register(function):
        REGISTRY[name] = Scenario(name, group, question, criterion, function, needs, slow)
        return function
    return register


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


READER_KEYS = ("session", "ok", "error", "first_cache", "final_cache", "startup_ms",
               "playback_start_ms",
               "parts_received", "parts_missing", "stalls", "stalled_ms",
               "longest_stall_ms", "max_behind_ms", "max_gap_ms", "request_errors",
               "rejections", "pipeline_depth", "round_trip_ms", "latency_p50_ms",
               "latency_p99_ms", "latency_max_ms", "frozen_at_ms")


class ReaderJob:
    """One `needletail-reader` process: N sessions of one stream."""

    def __init__(self, key, host, stream, count, seconds, report, process, started_at):
        self.key = key
        self.host = host
        self.stream = stream
        self.count = count
        self.seconds = seconds
        self.report_path = report
        self.process = process
        self.started_at = started_at
        self._report = None

    def wait(self, grace: float = 25.0) -> None:
        try:
            self.process.wait(timeout=max(1.0, self.started_at + self.seconds + grace
                                          - time.time()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                pass

    def stop(self) -> None:
        """End the readers at once, as a player that is closed."""
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except OSError:
            pass

    def report(self) -> dict:
        if self._report is None:
            try:
                self._report = json.loads(self.report_path.read_text())
            except (OSError, ValueError):
                self._report = {"summary": {"readers": self.count, "readers_ok": 0,
                                            "no_report": True}, "readers": []}
        return self._report

    @property
    def summary(self) -> dict:
        return self.report()["summary"]

    @property
    def readers(self) -> list[dict]:
        return self.report()["readers"]

    def evidence(self) -> dict:
        readers = []
        for reader in self.readers:
            item = {key: reader.get(key) for key in READER_KEYS if reader.get(key) is not None}
            item["moves"] = [{"at_ms": move["at_ms"], "from": move["from"], "to": move["to"],
                              "reason": move["reason"]} for move in reader.get("moves", [])][:12]
            if reader.get("stall_events"):
                item["stall_events"] = reader["stall_events"][:12]
            readers.append(item)
        # Keep every reader of a small job, and the worst of a large one.
        if len(readers) > 40:
            readers.sort(key=lambda item: (-(item.get("stalled_ms") or 0),
                                           -(item.get("parts_missing") or 0),
                                           item.get("ok", False)))
            readers = readers[:20]
        return {"host": self.host, "stream": self.stream, "sessions": self.count,
                "seconds": self.seconds, "started_at_s": round(self.started_at, 3),
                "summary": self.summary, "readers": readers}


class Context:
    """What a scenario uses to act on the lab and to record what it saw."""

    def __init__(self, item: Scenario, directory: pathlib.Path):
        self.scenario = item
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.t0 = time.time()
        self.events: list[dict] = []
        self.checks: list[dict] = []
        self.jobs: dict[str, ReaderJob] = {}
        self.conditions: dict = {}
        self.measurements: dict = {}
        self.notes: list[str] = []
        self.gap: str | None = None
        self.cleanup: list = []

    # -- time and events
    def elapsed(self) -> float:
        return time.time() - self.t0

    def event(self, what: str) -> None:
        self.events.append({"at_s": round(self.elapsed(), 2), "what": what})
        print(f"    [{self.elapsed():6.1f}s] {what}", flush=True)

    def at(self, seconds: float) -> None:
        """Sleep until `seconds` after the start of the scenario."""
        remaining = self.t0 + seconds - time.time()
        if remaining > 0:
            time.sleep(remaining)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    # -- readers
    def readers(self, key: str, stream: int, count: int, seconds: int,
                host: str = "lon-readers", entries: list[str] | None = None,
                site: str | None = None, **options) -> ReaderJob:
        site = site or lab.node(host)["site"]
        entries = entries or lab.distributors(site)
        report = self.directory / f"{key}.reader.json"
        report.unlink(missing_ok=True)
        command = lab.enter(host) + [lab.binary("needletail-reader")]
        for entry in entries:
            command += ["--entry", lab.base_url(entry)]
        command += ["--stream", str(stream), "--session", f"{self.scenario.name}-{key}",
                    "--readers", str(count), "--duration-seconds", str(seconds),
                    "--tls-ca", lab.ca_file(), "--server-name", lab.SERVER_NAME,
                    "--report", str(report)]
        for name, value in options.items():
            flag = "--" + name.replace("_", "-")
            if value is True:
                command.append(flag)
            elif value is not None and value is not False:
                command += [flag, str(value)]
        log = open(self.directory / f"{key}.reader.log", "ab")
        process = subprocess.Popen(command, stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        job = ReaderJob(key, host, stream, count, seconds, report, process, time.time())
        self.jobs[key] = job
        self.event(f"{count} readers of stream {stream} start on {host} ({key})")
        return job

    def wait_readers(self, *jobs: ReaderJob) -> None:
        for job in jobs or self.jobs.values():
            job.wait()

    # -- checks
    def check(self, description: str, ok: bool, detail="") -> bool:
        self.checks.append({"check": description, "ok": bool(ok), "detail": detail})
        print(f"    {'PASS' if ok else 'FAIL'}  {description}"
              + (f"  [{detail}]" if detail != "" else ""), flush=True)
        return bool(ok)

    def expect_served(self, job: ReaderJob, label: str | None = None) -> bool:
        label = label or job.key
        summary = job.summary
        errors = sorted({reader.get("error") for reader in job.readers
                         if reader.get("error")})[:3]
        return self.check(
            f"{label}: every reader was served",
            summary.get("readers_ok") == job.count,
            f"{summary.get('readers_ok')} of {job.count}" + (f"; {errors}" if errors else ""))

    def expect_complete(self, job: ReaderJob, label: str | None = None) -> bool:
        label = label or job.key
        return self.check(f"{label}: no part is missing",
                          job.summary.get("parts_missing", 1) == 0
                          and job.summary.get("parts_received", 0) > 0,
                          f"{job.summary.get('parts_missing')} missing of "
                          f"{job.summary.get('parts_received')}")

    def expect_no_stall(self, job: ReaderJob, label: str | None = None,
                        allow_ms: int = 0) -> bool:
        label = label or job.key
        summary = job.summary
        text = (f"no stall with a {summary.get('buffer_ms', 1000)} ms buffer" if allow_ms == 0
                else f"no stall longer than {allow_ms} ms")
        return self.check(
            f"{label}: {text}", summary.get("longest_stall_ms", 1 << 30) <= allow_ms,
            f"{summary.get('stalls')} stalls in {summary.get('readers_stalled')} readers, "
            f"longest {summary.get('longest_stall_ms')} ms, total {summary.get('stalled_ms')} ms")

    def expect_no_stall_after(self, job: ReaderJob, seconds: float,
                              label: str | None = None) -> bool:
        """No stall later than `seconds` after the readers started. Stalls
        before that are reported as a measurement."""
        label = label or job.key
        early, late = [], []
        for reader in job.readers:
            for event in reader.get("stall_events", []):
                (late if event["at_ms"] > seconds * 1000 else early).append(event["ms"])
        self.measure(f"{label}_startup_stalls", {"count": len(early), "ms": sum(early)})
        return self.check(
            f"{label}: no stall later than {seconds:.0f} s after the start",
            not late, f"{len(late)} stalls after {seconds:.0f} s (longest "
            f"{max(late, default=0)} ms); {len(early)} before it ({sum(early)} ms)")

    def expect_clean(self, job: ReaderJob, label: str | None = None) -> bool:
        first = self.expect_served(job, label)
        second = self.expect_complete(job, label)
        third = self.expect_no_stall(job, label)
        return first and second and third

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"    note: {text}", flush=True)

    def measure(self, key: str, value) -> None:
        self.measurements[key] = value

    def defer(self, function) -> None:
        """Run `function` when the scenario ends, whatever happens."""
        self.cleanup.append(function)


# ------------------------------------------------------------------ lab state

_current = {"overrides": None, "dirty": True}


def ensure_lab(overrides: list[str], fresh: bool = False, audio: str = "daw") -> None:
    """A healthy lab with the given settings and live sources."""
    if (fresh or _current["dirty"] or _current["overrides"] != overrides
            or not lab.load("namespaces.json")):
        print(f"  (starting the lab: {overrides or 'default settings'})", flush=True)
        if _archive is not None:
            _archive.before_up()
        lab.up(overrides)
        lab.start_sources(6 * 3600, audio=audio)
        _current["overrides"] = list(overrides)
        _current["dirty"] = False
        lab.wait_live(VIDEO, "london", 60)
        lab.wait_live(AUDIO, "london", 60)
        lab.wait_live(VIDEO, "newyork", 60)
        return
    restore()


def restore() -> None:
    """Put the lab back to health after a scenario."""
    try:
        lab.heal_all()
        # Only the nodes that the topology runs: the merged topology (the
        # default) has no backbone relay to start or to limit.
        # A slot that is not in service stays down (edge_lab.held_slots).
        if not lab.service_names():
            raise lab.LabError("the lab has no rendered topology")
        services = lab.standing_services()
        for name in services:
            lab.freeze_service(name, False)
            if lab.service_pid(name) is None:
                lab.start_service(name)
        sources = lab.load("sources.json")
        dead = [name for name, pid in sources.items()
                if not pathlib.Path(f"/proc/{pid}").exists()]
        if dead:
            lab.stop_sources()
            lab.start_sources(6 * 3600, audio="daw")
        for name in services:
            # The contributor goes back to its own limits, not a node's.
            limits = lab.service_limits(name)
            lab.set_limits(name, limits["cpu_percent"], limits["memory_mb"])
        lab.wait_live(VIDEO, "london", 60)
        lab.wait_live(AUDIO, "london", 60)
        lab.wait_live(VIDEO, "newyork", 60)
        idle_edges()
    except (lab.LabError, subprocess.TimeoutExpired) as error:
        print(f"  (the lab did not recover: {str(error)[:200]}; it will be restarted)",
              flush=True)
        _current["dirty"] = True
        ensure_lab(_current["overrides"] or [])


def idle_edges(timeout: float = 40.0) -> None:
    """Wait until no edge holds a session of an earlier scenario."""
    def idle():
        return all(lab.edge_view(name)["sessions"] == 0
                   for site in ("london", "newyork") for name in lab.edges(site))
    try:
        lab.wait_for("idle edges", idle, timeout, 1.0)
    except lab.LabError:
        pass


def busiest_edge(stream: int, site: str = "london") -> tuple[str, dict]:
    """The edge with the most sessions of a stream, and the sessions of all."""
    counts = {}
    for name in lab.edges(site):
        try:
            counts[name] = lab.edge_view(name)["playlists"].get(str(stream), {}).get(
                "sessions", 0)
        except lab.LabError:
            counts[name] = -1
    return max(counts, key=counts.get), counts


def readers_on(job: ReaderJob, cache: str) -> list[dict]:
    return [reader for reader in job.readers if reader.get("first_cache") == cache]


# ------------------------------------------------------------------ running

def mesh_counters() -> dict:
    """Counters of the cache mesh and of repair at every cache."""
    out = {}
    for name in lab.names("distributor") + lab.names("edge"):
        try:
            snapshot = lab.mesh_local(name)
        except (lab.LabError, subprocess.TimeoutExpired):
            continue
        fec = snapshot.get("mesh_fec") or {}
        replication = snapshot.get("cache_replication") or {}
        gap = snapshot.get("canonical_gap_skip") or {}
        out[name] = {
            **{key: value for key, value in fec.items() if isinstance(value, (int, float))},
            "hole_repair_requests": snapshot.get("edge_hole_repair_requests", 0),
            "gap_skips": gap.get("skips", 0), "gap_skipped_parts": gap.get("skipped_parts", 0),
            "subscriptions_opened": replication.get("subscriptions_opened", 0),
            "subscriptions_released": replication.get("subscriptions_released", 0),
            "subscriptions_refused": replication.get("subscriptions_refused", 0),
        }
    return out


def counter_delta(before: dict, after: dict) -> dict:
    out = {}
    for name, values in after.items():
        earlier = before.get(name, {})
        changed = {key: value - earlier.get(key, 0) for key, value in values.items()
                   if value - earlier.get(key, 0)}
        out[name] = changed
    return out


def run_scenario(item: Scenario, directory: pathlib.Path, sample: bool = True) -> dict:
    """One scenario; inside a campaign's archived run (archived), its
    records go into the run as it ends, whatever ends it."""
    with (guard.players_lock() if item.group in PLAYER_GROUPS else contextlib.nullcontext()):
        if _archive is None:
            return scenario_evidence(item, directory, sample)
        since_us = time.time_ns() // 1000
        try:
            return scenario_evidence(item, directory, sample)
        finally:
            _archive.scenario(item, directory / item.name, since_us)


def scenario_evidence(item: Scenario, directory: pathlib.Path, sample: bool) -> dict:
    print(f"\n=== {item.name}  ({item.group})\n    Q: {item.question}", flush=True)
    ensure_lab(item.needs)
    context = Context(item, directory / item.name)
    sampler = lab.Sampler(context.directory / "nodes.ndjson").start() if sample else None
    counters_before = mesh_counters()
    error = None
    try:
        item.function(context)
    except Exception as failure:  # noqa: BLE001 - a scenario failure is a result
        error = f"{type(failure).__name__}: {failure}"
        traceback.print_exc()
        context.check("the scenario ran to its end", False, error[:300])
    finally:
        for job in context.jobs.values():
            job.stop()
        for function in reversed(context.cleanup):
            try:
                function()
            except Exception as failure:  # noqa: BLE001
                print(f"    cleanup failed: {failure}", flush=True)
        nodes = sampler.stop() if sampler else {}
        try:
            mesh = counter_delta(counters_before, mesh_counters())
        except Exception:  # noqa: BLE001
            mesh = {}
    failed = [check for check in context.checks if not check["ok"]]
    if failed:
        result = "GAP" if context.gap else "FAIL"
    else:
        result = "PASS"
    evidence = {
        "schema": SCHEMA,
        "scenario": item.name,
        "group": item.group,
        "question": item.question,
        "criterion": item.criterion,
        "started_utc": datetime.datetime.fromtimestamp(
            context.t0, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.time() - context.t0, 1),
        "lab": {"settings": item.needs, "limits_per_service": lab.NANODE,
                "limits_of_contributor": lab.CONTRIBUTOR_LIMITS,
                "binaries": binary_digests()},
        "conditions": context.conditions,
        "events": context.events,
        "readers": {key: job.evidence() for key, job in context.jobs.items()},
        "measurements": context.measurements,
        "nodes": nodes,
        "mesh": mesh,
        "checks": context.checks,
        "result": result,
        "gap": context.gap,
        "notes": context.notes,
    }
    (context.directory / "evidence.json").write_text(json.dumps(evidence, indent=1))
    print(f"    => {result}" + (f" ({context.gap})" if context.gap and failed else ""),
          flush=True)
    return evidence


# ------------------------------------------------------------------ the run archive

# The campaign's run in the archive while `archived` holds one.
_archive: "CampaignRecord | None" = None
MESH_ROLES = ("contributor", "backbone", "distributor", "edge")


def hops_module():
    """scripts/compare/hops.py: its collect_edge_lab and save_raw."""
    spec = importlib.util.spec_from_file_location(
        "hops", pathlib.Path(__file__).resolve().parent / "compare" / "hops.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CampaignRecord:
    """What a campaign keeps in its run: each scenario's directory (its
    evidence, readers' reports and logs, node samples, the controller's
    events), the slice of every node's log the scenario wrote, every
    node's hop journal and stop journal since the scenario began, and its
    readers as listener steps; the node logs written between scenarios,
    each time before the lab's `up` deletes them; the summary."""

    def __init__(self, run: archived_run.Run):
        self.run = run
        self.logs = archived_run.LogSlices(lab.WORK / "logs")
        self.logs.mark()
        self.ups = 0
        self.directory: pathlib.Path | None = None

    def before_up(self) -> None:
        self.ups += 1
        self.logs.take(self.run, f"node-logs/before-up-{self.ups:02d}")

    def journals(self, directory: pathlib.Path, since_us: int) -> None:
        """Every node's hop journal (hops.raw.ndjson, hops.py's format) and
        stop journal (stalls/<node>.ndjson) since since_us, into the
        scenario's directory."""
        hops = hops_module()
        nodes, events = hops.collect_edge_lab(since_us, None)
        hops.save_raw(directory / "hops.raw.ndjson", nodes, events)
        statuses = {}
        (directory / "stalls").mkdir(exist_ok=True)
        for name in lab.service_names():
            if lab.node(name)["role"] not in MESH_ROLES:
                continue
            status, _head, body = lab.curl(name, f"/api/stalls?since_us={since_us}", timeout=30)
            statuses[name] = status
            if status == 200:
                (directory / "stalls" / f"{name}.ndjson").write_text(body)
        (directory / "stalls" / "http-status.json").write_text(json.dumps(statuses, indent=1) + "\n")

    def scenario(self, item: Scenario, directory: pathlib.Path, since_us: int) -> None:
        """The scenario's records into the run. Never raises: it runs in
        run_scenario's `finally`, where an error would replace the
        scenario's own result and end the campaign; a failure is kept and
        fails the run."""
        self.run.guarded(f"keep scenario {item.name}", self.keep, item, directory, since_us)

    def keep(self, item: Scenario, directory: pathlib.Path, since_us: int) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            self.journals(directory, since_us)
        except Exception as error:  # noqa: BLE001 - the rest is kept; the run fails
            self.run.problems.append(f"{item.name}: hop and stop journals: "
                                     f"{type(error).__name__}: {error}")
        self.logs.take(self.run, f"scenarios/{item.name}/node-logs")
        self.run.add_tree(directory, f"scenarios/{item.name}", "raw",
                          f"scenario {item.name}: evidence.json, the readers' reports and logs, "
                          "nodes.ndjson (each node every second), hops.raw.ndjson, stalls/",
                          {"at_s": "s", "t_us": "us", "unix_us": "us"})
        evidence = read_json(directory / "evidence.json")
        for key, job in ((evidence or {}).get("readers") or {}).items():
            site = lab.node(job["host"])["site"] if job.get("host") else None
            self.run.record_step(f"{item.name}/{key}", f"stream-{job.get('stream')}",
                                 job.get("sessions") or 0, {site: job.get("sessions") or 0}
                                 if site else {}, None, seconds=job.get("seconds"))

    def close(self) -> None:
        self.logs.take(self.run, "node-logs/after-scenarios")
        if self.directory is not None and self.directory.is_dir():
            self.run.write_json("summary.json", summary_rows(self.directory), "result",
                                "each scenario's result and failed checks")


def read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def archive_run_id() -> str | None:
    """The id of the campaign's run in the archive, while one runs: a tool
    that a scenario starts (the autoscaler) appends to it."""
    return _archive.run.run_id if _archive is not None else None


@contextlib.contextmanager
def archived(run_id: str, names: list[str]):
    """A campaign's run in the archive (one per invocation): scenarios run
    inside it keep their records there as each ends (run_scenario), and the
    run ends failed when the campaign is cut off."""
    global _archive
    topology, regions = archived_run.local_topology(lab.NODES)
    with archived_run.Run(
            run_id, "local-lab", "edge-campaign",
            code=archived_run.code_paths(lab.MESH_ROOT, lab.CONTRIB_ROOT),
            settings={"scenarios": names, "lab_id": lab.LAB_ID, "lab_work": str(lab.WORK),
                      "limits_per_service": lab.NANODE,
                      "limits_of_contributor": lab.CONTRIBUTOR_LIMITS},
            topology=topology, regions=regions) as run:
        record = CampaignRecord(run)
        run.at_close(record.close)
        _archive = record
        try:
            yield record
        finally:
            _archive = None


_digests: dict = {}


def binary_digests() -> dict:
    if not _digests:
        for label, path in (("av_mesh", lab.binary("av-mesh")),
                            ("needletail_reader", lab.binary("needletail-reader")),
                            ("av_contrib", lab.binary("av-contrib"))):
            try:
                output = subprocess.run(["sha256sum", str(path)], capture_output=True,
                                        text=True).stdout.split()
                _digests[label] = output[0][:16]
            except (OSError, IndexError):
                _digests[label] = None
    return _digests


def summary_rows(directory: pathlib.Path) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*/evidence.json")):
        evidence = json.loads(path.read_text())
        failed = [check["check"] for check in evidence["checks"] if not check["ok"]]
        rows.append({"scenario": evidence["scenario"], "group": evidence["group"],
                     "result": evidence["result"], "failed": failed,
                     "gap": evidence.get("gap"), "started": evidence["started_utc"]})
    return rows


class Background(threading.Thread):
    """Runs a function every `interval` seconds until stopped."""

    def __init__(self, function, interval: float):
        super().__init__(daemon=True)
        self.function = function
        self.interval = interval
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                self.function()
            except Exception:  # noqa: BLE001
                pass
            self.stopped.wait(self.interval)

    def stop(self) -> None:
        self.stopped.set()
        self.join(timeout=10)
