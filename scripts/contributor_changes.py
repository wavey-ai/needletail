#!/usr/bin/env python3
"""Workstream A's contributor and ingress changes (A-contributor 2.4, 2.7,
2.10, 3.1, 3.5): the rules that say whether a contributor or a first
forwarder may stop, whether a contributor that came back is publishing the
same parts as the other, and the commands `kill`, `return` and (local lab
only) `freeze`. A module with no command line of its own: the deploy tool's
`kill` and `return`, the local lab's `kill`, `return` and `freeze` and the
change engine (mesh_ops) call it.

The engine's names. C's engine (mesh_ops.contributor_rules) calls
`contrib_gate` and `contrib_ready`; A 2.10 calls them `contributor_gate`
and `contributor_ready`. These are the engine's names: one name each, the
one that was already called.

The backend is the engine's `mesh_ops.Ops` (LinodeOps in the deploy tool,
EdgeLabOps in edge_lab.py), with five methods beside the engine's:
  hold_down(node) the node's service is not started again by its manager
                  (Linode: a drop-in /run/systemd/system/<unit>.d/hold.conf
                  with Restart=no, then daemon-reload)
  kill(node)      SIGKILL to the service (systemctl kill -s KILL)
  release(node)   the hold removed and the service started
  freeze(node, frozen)  SIGSTOP or SIGCONT (the local lab only)
  staged_identity(node) the `--print-fingerprint` identity of what is
                  staged for a contributor (binary and arguments), None
                  when nothing is staged for it

What a contributor serves is av-contrib's `GET /api/node` (A 2.6): `state`
(`ready` only when publishing is proven, `standing_by` when it hears no
source), `identity` (fingerprint and the digests `numbering`, `envelope`,
`wire`, and `fields`), `streams[]` with the contract counters and
`windows[]` ({first_part, parts, present, xor}), `degraded` (the ids of its
lanes that do not carry). What a first forwarder serves: av-mesh's
`/api/node` `parents[]` (C's ParentFeeds: per sending address and stream,
`source_age_ms`, a row of audio by its `class`, its `track` being the
object key's, "opaque-audio"; per address `audio_age_ms`) and
`/api/mesh/local`
`relay_session.fast_plane.parent_lanes[].duplicate_mismatch`,
`relay_session.fast_plane.not_admitted`,
`relay_session.fast_plane.media_clock.stale_media`,
`relay_session.canonical_conflicts`, `relay_session.wide_hole_releases` and
`canonical_gap_skip.skipped_parts`. A figure that is missing fails a check
("parity unknown"); it is never read as 0.

Exit status of the commands (C's): 0 done; 2 refused before anything was
changed; 3 stopped: the change was made and its end was not seen, or the
node did not come back ready. stdout carries the change's two marks and
nothing else.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mesh_ops  # noqa: E402

# A contributor is seen at a first forwarder when its entry has a source
# part of every stream within this (2.7).
SEEN_MS = 100
# The streams an entry shows: a source part within this.
SHOWN_MS = 1000
# A killed contributor is gone from a forwarder once its audio is this old.
GONE_MS = 1000
# The gate passes on this many polls in a row (2.10).
GATE_POLLS = 3
# A kill's end is seen within this.
END_S = 10.0
# After a restart the forwarders' counters must stay still this long.
SETTLE_S = 2.0
# The counters of the source contract (2.3), per stream.
CONTRACT = ("numbers_reused", "geometry_changed", "time_ahead")
# The classes of a contributor's change (2.4), most permissive first.
CLASSES = ("same", "two-version", "needs-idle", "restart-bound")
# Where a kill records the node it holds down, under the backend's evidence
# directory: `return` reads it.
HELD_DIR = "held"
VERBS = ("kill", "return", "freeze")


# ----------------------------------------------------------------- identity

def identity_of(binary, args: list[str]) -> dict:
    """The identity (A 2.4) that av-contrib computes for a contributor
    started with `args`: `BINARY ARGS[1:] --print-fingerprint`, which binds
    nothing and reads no file. ValueError when it prints none: an identity
    that cannot be read is not known to be equal."""
    command = [str(binary), *args[1:], "--print-fingerprint"]
    result = identity = None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                check=False)
        if result.returncode == 0:
            identity = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        identity = None
    if not isinstance(identity, dict) or not identity.get("fingerprint"):
        said = "" if result is None else (result.stderr or result.stdout).strip()[-400:]
        raise ValueError("av-contrib --print-fingerprint gave no identity"
                         + (f": {said}" if said else ""))
    return identity


def _field(identity: dict, key: str):
    fields = identity.get("fields")
    return fields.get(key) if isinstance(fields, dict) else None


def classify(running: dict, staged: dict) -> str:
    """The class of a change from the identity a contributor runs to the
    one staged for it (2.4): `restart-bound` when the RelaySession identity
    (generation, subscription) differs, `same` for equal fingerprints,
    `needs-idle` when the numbering differs, else `two-version`. ValueError
    for an identity without its fingerprint and digests."""
    for identity in (running, staged):
        if not isinstance(identity, dict) or not all(
                isinstance(identity.get(key), str) and identity.get(key)
                for key in ("fingerprint", "numbering", "envelope", "wire")) or \
                not isinstance(identity.get("fields"), dict):
            raise ValueError(f"not an identity: {json.dumps(identity)[:200]}")
    if any(_field(running, key) != _field(staged, key) for key in ("generation", "subscription")):
        return "restart-bound"
    if running["fingerprint"] == staged["fingerprint"]:
        return "same"
    if running["numbering"] != staged["numbering"]:
        return "needs-idle"
    return "two-version"


# ----------------------------------------------------------------- the lab

def contributors(lab: mesh_ops.Lab) -> list[str]:
    """The lab's contributors, running or not (a killed one is still one)."""
    return sorted((name for name, record in lab.records.items()
                   if record["role"] == "contributor" and not record.get("slot")),
                  key=mesh_ops.natural)


def ingresses(lab: mesh_ops.Lab) -> list[str]:
    """The first forwarders: the compiled distributors of the contributors'
    site (lon-dist-a, lon-dist-b)."""
    site = lab.contributor_site()
    return lab.first_distributors(site) if site else []


def other_contributor(lab: mesh_ops.Lab, name: str) -> str | None:
    return next((item for item in contributors(lab) if item != name), None)


def kind_of(lab: mesh_ops.Lab, name: str) -> str:
    """"contributor" or "ingress"; Refused for any other node: kill and
    return take a contributor or a first forwarder (2.10)."""
    if name in contributors(lab):
        return "contributor"
    if name in ingresses(lab):
        return "ingress"
    raise mesh_ops.Refused(f"{name} is neither a contributor nor a first forwarder of the lab "
                           f"(contributors {', '.join(contributors(lab)) or 'none'}; first "
                           f"forwarders {', '.join(ingresses(lab)) or 'none'})")


# ----------------------------------------------------------------- reading

def _age(value) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def entry_of(status: mesh_ops.Status | None, addresses) -> dict | None:
    """The `parents[]` entry (as the node sent it) of the parent at one of
    `addresses`, None when it is not a parent there."""
    if status is None or not isinstance(status.raw.get("parents"), list):
        return None
    for entry in status.raw["parents"]:
        if isinstance(entry, dict) and isinstance(entry.get("address"), str) and \
                mesh_ops.address_of(entry["address"]) in addresses:
            return entry
    return None


def audio_ages(status: mesh_ops.Status | None, addresses) -> dict[int, float | None]:
    """{stream: source_age_ms} of the audio rows of the parent at
    `addresses`; {} when it is not a parent there. A row is audio by its
    `class` (av-mesh's ParentStreamStatus), never by its `track`: the track
    is the RLS track of the object key, "opaque-audio" for every audio
    stream a contributor publishes. A stream with rows of several tracks
    takes its freshest."""
    if status is None or status.parents is None:
        return {}
    for parent in status.parents:
        if parent.address in addresses:
            ages: dict[int, float | None] = {}
            for (stream, _track), row in parent.rows.items():
                if row.get("class") != "audio":
                    continue
                age = _age(row.get("source_age_ms"))
                if stream not in ages or (age is not None and (ages[stream] is None
                                                               or age < ages[stream])):
                    ages[stream] = age
            return ages
    return {}


def shown(ages: dict) -> set[int]:
    return {stream for stream, age in ages.items() if age is not None and age <= SHOWN_MS}


def seen(lab: mesh_ops.Lab, forwarder: mesh_ops.Status | None, name: str,
         others=None) -> tuple[bool, list[int]]:
    """Whether contributor `name` is seen at a first forwarder (2.7): for
    every audio stream that the other contributors' entries show within
    1 s, its own entry has a source part within 100 ms. (seen, the streams
    it is late on)."""
    others = [item for item in (others or contributors(lab)) if item != name]
    streams = set()
    for item in others:
        streams |= shown(audio_ages(forwarder, lab.addresses(item)))
    own = audio_ages(forwarder, lab.addresses(name))
    late = sorted(stream for stream in streams
                  if own.get(stream) is None or own[stream] > SEEN_MS)
    return not late, late


def redundancy(lab: mesh_ops.Lab, forwarder: mesh_ops.Status | None) -> str:
    """"N of M" contributors seen at a first forwarder, or "idle" when no
    entry shows an audio stream within 1 s (2.7)."""
    names = contributors(lab)
    streams = set()
    for name in names:
        streams |= shown(audio_ages(forwarder, lab.addresses(name)))
    if not streams:
        return "idle"
    count = sum(1 for name in names if seen(lab, forwarder, name)[0])
    return f"{count} of {len(names)}"


def plane_counters(document) -> dict:
    """The counters A's rules read at a first forwarder, from its
    `/api/mesh/local`; each None when it is not there."""
    def at(path):
        value = document
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return _age(value)

    lanes = (((document or {}).get("relay_session") or {}).get("fast_plane") or {}) \
        .get("parent_lanes") if isinstance(document, dict) else None
    mismatch = None
    if isinstance(lanes, list) and lanes and all(
            isinstance(lane, dict) and _age(lane.get("duplicate_mismatch")) is not None
            for lane in lanes):
        mismatch = sum(lane["duplicate_mismatch"] for lane in lanes)
    return {"duplicate_mismatch": mismatch,
            "not_admitted": at("relay_session.fast_plane.not_admitted"),
            "stale_media": at("relay_session.fast_plane.media_clock.stale_media"),
            "canonical_conflicts": at("relay_session.canonical_conflicts"),
            "wide_hole_releases": at("relay_session.wide_hole_releases"),
            "skipped_parts": at("canonical_gap_skip.skipped_parts")}


def identity(status: mesh_ops.Status | None) -> dict | None:
    value = status.raw.get("identity") if status is not None else None
    return value if isinstance(value, dict) and value.get("fingerprint") else None


def contract_total(status: mesh_ops.Status | None) -> int | None:
    """The sum of the contract counters over a contributor's streams; None
    when one is missing."""
    if status is None or status.streams is None:
        return None
    total = 0
    for row in status.streams.values():
        for key in CONTRACT:
            value = _age(row.get(key))
            if value is None:
                return None
            total += value
    return total


def video_streams(status: mesh_ops.Status) -> list[int]:
    return sorted(stream for stream, row in (status.streams or {}).items()
                  if row.get("class") == "video" and row.get("publishing") is not False)


def publishing(status: mesh_ops.Status | None) -> set[int]:
    return {stream for stream, row in ((status.streams if status else None) or {}).items()
            if row.get("class") == "audio" and row.get("publishing") is True}


def windows_compared(x: mesh_ops.Status | None, y: mesh_ops.Status | None):
    """The window digests of two contributors (2.6): for each audio stream
    both publish, the windows with the same first part and equal `present`.
    (mismatches, {stream: newest equal window or None})."""
    mismatches, equal = [], {}
    for stream in sorted(publishing(x) & publishing(y)):
        mine = {item.get("first_part"): item for item in x.streams[stream].get("windows") or []
                if isinstance(item, dict)}
        theirs = {item.get("first_part"): item for item in y.streams[stream].get("windows") or []
                  if isinstance(item, dict)}
        common = sorted(part for part in mine if part in theirs and part is not None
                        and mine[part].get("present") == theirs[part].get("present"))
        bad = [part for part in common if mine[part].get("xor") != theirs[part].get("xor")]
        mismatches += [f"stream {stream} window {part}: xor {mine[part].get('xor')} and "
                       f"{theirs[part].get('xor')}" for part in bad]
        good = [part for part in common if part not in bad]
        equal[stream] = good[-1] if good else None
    return mismatches, equal


def _statuses(ops, names) -> dict:
    names = [name for name in dict.fromkeys(names) if name]
    return {name: mesh_ops.status_of(document) for name, document in ops.statuses(names).items()}


def _planes(ops, names) -> dict:
    out = {}
    for name in names:
        code, document = ops.get(name, "/api/mesh/local")
        out[name] = plane_counters(document) if code == 200 else None
    return out


def _grew(now, before) -> bool:
    return now is not None and before is not None and now > before


# ----------------------------------------------------------------- the gate

def gate_check(lab: mesh_ops.Lab, x: str, y: str | None, firsts: list[str], statuses: dict,
               planes: dict, previous: tuple | None, staged: dict | None, *,
               video_gap: str = "refuse", unproven: bool = False,
               unprotected: bool = False) -> tuple[list, list, dict]:
    """One poll of `contrib_gate`: (final, problems, facts). `final` are
    refusals that waiting cannot change (K5, K6, no other contributor);
    `problems` fail this poll. `previous` is the last poll's (statuses,
    planes): K3 and K4 compare with it."""
    final, problems, facts = [], [], {}
    sx = statuses.get(x)
    sy = statuses.get(y) if y else None
    if y is None and not unprotected:
        final.append(f"K1: {x} is the lab's only contributor; stopping it is an outage "
                     "(kill --unprotected stops it all the same)")
    # K6: video (C's rule).
    if video_gap != "accept":
        if sx is None:
            problems.append(f"K6: {x} does not answer /api/node: whether it publishes video "
                            "is unknown")
        elif video_streams(sx):
            final.append(f"K6: {x} publishes video stream(s) {video_streams(sx)}: its stop is a "
                         "4K gap (one contributor carries video); --video-gap accept goes on")
    # K5: the class of the change.
    both_idle = sx is not None and sy is not None and \
        sx.state == "standing_by" and sy.state == "standing_by"
    if staged is not None:
        running = identity(sx)
        if running is None:
            problems.append(f"K5: {x} serves no identity: the class of its change is unknown")
        else:
            try:
                change = classify(running, staged)
            except ValueError as error:
                change = None
                final.append(f"K5: {error}")
            facts["class"] = change
            if change == "restart-bound":
                final.append(f"K5: {x}'s change is restart-bound (generation or subscription "
                             "differ): the first forwarders would not admit it")
            elif change == "needs-idle" and not both_idle:
                final.append(f"K5: {x}'s change renumbers the parts (needs-idle): it needs the "
                             "source stopped (both contributors standing_by)")
    if unprotected or y is None:
        return final, problems, facts
    # K1: the other contributor is ready, or an idle pair.
    if sy is None:
        problems.append(f"K1: {y} does not answer /api/node")
    elif sy.state != "ready":
        same_build = sx is not None and (sx.build_git, sx.build_unix) == \
            (sy.build_git, sy.build_unix)
        if not (both_idle and (unproven or same_build)):
            problems.append(f"K1: {y} is {sy.state}, not ready"
                            + (f", and runs {sy.build_git} {sy.build_unix}, not {x}'s build "
                               f"(--unproven)" if both_idle else ""))
    # K2: the other is seen at both first forwarders.
    for forwarder in firsts:
        status = statuses.get(forwarder)
        if status is None:
            problems.append(f"K2: {forwarder} does not answer /api/node")
        elif status.parents is None:
            problems.append(f"K2: parity unknown: {forwarder}'s parents[]")
        else:
            ok, late = seen(lab, status, y, [x, y])
            if not ok:
                problems.append(f"K2: {y} is not seen at {forwarder} for stream(s) {late}")
    # K3: equal bytes.
    for forwarder in firsts:
        plane = planes.get(forwarder)
        now = plane.get("duplicate_mismatch") if plane else None
        if now is None:
            problems.append(f"K3: parity unknown: {forwarder}'s duplicate_mismatch")
        elif previous is not None:
            before = ((previous[1].get(forwarder) or {}).get("duplicate_mismatch"))
            if _grew(now, before):
                problems.append(f"K3: duplicate_mismatch grew at {forwarder} ({before} to {now})")
    ix, iy = identity(sx), identity(sy)
    if ix and iy and ix["fingerprint"] == iy["fingerprint"]:
        mismatches, equal = windows_compared(sx, sy)
        facts["windows"] = equal
        problems += [f"K3: {item}" for item in mismatches]
        missing = sorted(stream for stream, part in equal.items() if part is None)
        if missing and not mismatches:
            problems.append(f"K3: parity unknown: no window of stream(s) {missing} closed by "
                            "both contributors with equal present")
    # K4: the other's contract counters still.
    total = contract_total(sy)
    facts["contract"] = total
    if total is None:
        problems.append(f"K4: parity unknown: {y}'s contract counters")
    elif previous is not None and _grew(total, contract_total(previous[0].get(y))):
        problems.append(f"K4: {y}'s contract counters grew ({', '.join(CONTRACT)})")
    return final, problems, facts


def contrib_gate(ops, node: str, *, timeout_s: float = 30.0, poll_s: float = mesh_ops.POLL_S,
                 clock=time.time, sleep=time.sleep, video_gap: str = "refuse",
                 unproven: bool = False, unprotected: bool = False,
                 change: bool = True) -> tuple[bool, str, dict]:
    """May contributor `node` (X) stop now (2.10, K1 to K6): polled every
    `poll_s`, passes on three polls in a row (each compared with the one
    before it), fails after `timeout_s` with the last poll's problems, or
    at once on a refusal that waiting cannot change. `change`: X is to run
    what is staged for it (K5 from `ops.staged_identity`); a kill has none.
    `unprotected` skips K1 to K4. (ok, why, evidence)."""
    lab = mesh_ops.Lab(ops.nodes())
    y = other_contributor(lab, node)
    firsts = ingresses(lab)
    evidence = {"node": node, "other": y, "ingresses": firsts, "polls": 0,
                "unprotected": unprotected, "unproven": unproven}
    if y is None and not unprotected:
        return False, (f"K1: {node} is the lab's only contributor; stopping it is an outage "
                       "(kill --unprotected stops it all the same)"), evidence
    staged = None
    if change:
        try:
            staged = ops.staged_identity(node)
        except (ValueError, mesh_ops.Refused) as error:
            return False, f"K5: the identity of what is staged for {node}: {error}", evidence
        evidence["staged"] = staged
    deadline = clock() + timeout_s
    previous, streak = None, 0
    while True:
        began = clock()
        statuses = _statuses(ops, [node, y, *firsts])
        planes = _planes(ops, firsts)
        evidence["polls"] += 1
        final, problems, facts = gate_check(lab, node, y, firsts, statuses, planes, previous,
                                            staged, video_gap=video_gap, unproven=unproven,
                                            unprotected=unprotected)
        evidence["last"] = {"problems": final + problems, **facts}
        if final:
            return False, "; ".join(final), evidence
        streak = streak + 1 if not problems and previous is not None else 0
        if streak >= GATE_POLLS or (unprotected and not problems):
            return True, "", evidence
        previous = (statuses, planes)
        now = clock()
        if now >= deadline:
            return False, "; ".join(problems or ["the gate did not pass three polls in a row"]), \
                evidence
        sleep(max(0.0, poll_s - (now - began)))


# ----------------------------------------------------------------- readiness

def ready_check(lab: mesh_ops.Lab, x: str, y: str | None, firsts: list[str], statuses: dict,
                planes: dict, baseline: dict | None,
                alone: bool = False) -> tuple[list, list, list, dict]:
    """One poll of `contrib_ready`: (fatal, waiting, unsettled, facts).
    `fatal`: other bytes under the same key (windows or duplicate_mismatch);
    `waiting`: X not seen yet, no window compared yet; `unsettled`: a
    counter that must stay still grew since `baseline`. `alone`: Y is held
    down by a kill (both contributors down) or there is none, so there is
    nothing to compare X with: X must be seen at both first forwarders for
    every audio stream it publishes itself, and the counters must stay
    still."""
    fatal, waiting, unsettled, facts = [], [], [], {}
    sx = statuses.get(x)
    sy = statuses.get(y) if y else None
    alone = alone or y is None
    if sx is None:
        waiting.append(f"{x} does not answer /api/node")
    if not alone and sy is None:
        waiting.append(f"{y} does not answer /api/node: the parts cannot be compared")
    for forwarder in firsts:
        status = statuses.get(forwarder)
        if status is None or status.parents is None:
            waiting.append(f"{forwarder}'s parents[] are not known")
            continue
        if alone:
            own = audio_ages(status, lab.addresses(x))
            late = sorted(stream for stream in publishing(sx)
                          if own.get(stream) is None or own[stream] > SEEN_MS)
        else:
            _ok, late = seen(lab, status, x, [x, y])
        if late:
            waiting.append(f"{x} is not seen at {forwarder} for stream(s) {late}")
    ix, iy = identity(sx), identity(sy)
    if alone:
        state = "alone"
    elif sy is None:
        state = None
    elif ix is None or iy is None:
        waiting.append("an identity is not served: the parts cannot be compared")
        state = None
    else:
        state = "same" if ix["fingerprint"] == iy["fingerprint"] else "two-version"
    facts["state"] = state
    if state == "same":
        mismatches, equal = windows_compared(sx, sy)
        facts["windows"] = equal
        fatal += mismatches
        missing = sorted(stream for stream, part in equal.items() if part is None)
        if missing:
            waiting.append(f"no window of stream(s) {missing} compared yet")
    held = ["duplicate_mismatch", "not_admitted", "stale_media"]
    if state == "two-version":
        held += ["wide_hole_releases", "skipped_parts"]
    for forwarder in firsts:
        plane = planes.get(forwarder)
        for key in held:
            now = plane.get(key) if plane else None
            if now is None:
                waiting.append(f"parity unknown: {forwarder}'s {key}")
                continue
            before = ((baseline or {}).get(forwarder) or {}).get(key)
            if _grew(now, before):
                (fatal if key == "duplicate_mismatch" else unsettled).append(
                    f"{key} grew at {forwarder} ({before} to {now})")
    return fatal, waiting, unsettled, facts


def contrib_ready(ops, node: str, *, timeout_s: float = 60.0, poll_s: float = mesh_ops.POLL_S,
                  clock=time.time, sleep=time.sleep) -> tuple[bool, str, dict]:
    """Is contributor `node` (X), started again, publishing the parts the
    other publishes (2.10; after C's R1 and R2): seen at both first
    forwarders for every audio stream the other's entry shows; with equal
    fingerprints a window of each stream both publish compared equal; and
    for 2 s at both first forwarders duplicate_mismatch, not_admitted and
    stale_media still (in the two-version state wide_hole_releases and
    skipped_parts too; canonical_conflicts may grow). With the other held
    down by a kill (its record in `held/`), or none: seen for its own
    streams and the counters still (ready_check, `alone`).
    Other bytes under one key fail at once; anything else fails at
    `timeout_s`. (ok, why, evidence)."""
    lab = mesh_ops.Lab(ops.nodes())
    y = other_contributor(lab, node)
    firsts = ingresses(lab)
    alone = y is None or held_record(ops, y) is not None
    evidence = {"node": node, "other": y, "ingresses": firsts, "polls": 0, "alone": alone}
    deadline = clock() + timeout_s
    baseline, since = None, None
    while True:
        began = clock()
        statuses = _statuses(ops, [node, y, *firsts])
        planes = _planes(ops, firsts)
        evidence["polls"] += 1
        fatal, waiting, unsettled, facts = ready_check(lab, node, y, firsts, statuses, planes,
                                                       baseline, alone)
        evidence["last"] = {"fatal": fatal, "waiting": waiting, "unsettled": unsettled, **facts}
        if fatal:
            return False, "; ".join(fatal), evidence
        now = clock()
        if waiting or unsettled or baseline is None:
            baseline, since = (planes, now) if not waiting else (None, None)
        elif now - since >= SETTLE_S:
            evidence["settled_s"] = round(now - since, 3)
            return True, "", evidence
        if now >= deadline:
            return False, "; ".join(waiting + unsettled
                                    or [f"not {SETTLE_S:g} s still before the timeout"]), evidence
        sleep(max(0.0, poll_s - (now - began)))


# ----------------------------------------------------------------- the ingress

def ingress_gate(ops, node: str, *, timeout_s: float = 30.0, poll_s: float = mesh_ops.POLL_S,
                 parity_ms: float = 1.0, clock=time.time,
                 sleep=time.sleep) -> tuple[bool, str, dict, list]:
    """May first forwarder `node` stop now (2.10): the other first
    forwarder is ready and sees every contributor ("2 of 2", or "idle"),
    then C's forwarder gate for its compiled and lease children. (ok, why,
    evidence, the gate's pairs)."""
    lab = mesh_ops.Lab(ops.nodes())
    others = [name for name in ingresses(lab) if name != node]
    evidence = {"node": node, "other": others[0] if others else None, "polls": 0}
    if not others:
        return False, f"{node} is the only first forwarder", evidence, []
    sibling = others[0]
    deadline = clock() + timeout_s
    while True:
        began = clock()
        status = mesh_ops.status_of(ops.status(sibling))
        evidence["polls"] += 1
        problems = []
        if status is None:
            problems.append(f"{sibling} does not answer /api/node")
        else:
            seen_by = redundancy(lab, status)
            evidence["redundancy"] = seen_by
            if status.ready is not True:
                problems.append(f"{sibling} is not ready ({status.state})")
            if seen_by != "idle" and not seen_by.startswith(f"{len(contributors(lab))} of "):
                problems.append(f"{sibling} sees contributors {seen_by}")
        if not problems:
            break
        now = clock()
        if now >= deadline:
            return False, "; ".join(problems), evidence, []
        sleep(max(0.0, poll_s - (now - began)))
    gate = mesh_ops.forwarder_gate(ops, node, lab=lab, timeout_s=max(0.0, deadline - clock()),
                                   parity_ms=parity_ms, poll_s=poll_s, clock=clock, sleep=sleep)
    evidence["forwarder_gate"] = gate.evidence
    evidence["notes"] = gate.notes
    return gate.ok, "; ".join(gate.problems), evidence, gate.pairs


# ----------------------------------------------------------------- the commands

def add_options(parser: argparse.ArgumentParser, verb: str, positional: bool = False) -> None:
    """The options of `kill`, `return` and `freeze`: the node (`--node X`
    on the deploy tool, `NODE` in the local lab, as their other verbs)."""
    if positional:
        parser.add_argument("node")
    else:
        parser.add_argument("--node", required=True)
    if verb == "kill":
        parser.add_argument("--unprotected", action="store_true",
                            help="skip K1 to K4 (the label gains .unprotected)")
        parser.add_argument("--video-gap", choices=("refuse", "accept"), default="refuse")
        parser.add_argument("--gate-timeout-s", type=float, default=30.0)
    if verb == "return":
        parser.add_argument("--ready-timeout-s", type=float, default=60.0)
    if verb == "freeze":
        parser.add_argument("--ms", type=int, required=True,
                            help="how long the service is stopped (SIGSTOP)")
    parser.add_argument("--marks", default=None, help="append the change's two marks here")


def clock_check(ops, lab: mesh_ops.Lab) -> None:
    """C 2.7 before the first mark: this host's clock against one serving
    node of each region, Refused (exit 2) above 50 ms, since the marks are
    scored against probes stamped on the view hosts. The engine's own check
    (Engine.clock_check), on this lab."""
    engine = mesh_ops.Engine(ops, "clock", mesh_ops.Options())
    engine.lab = lab
    engine.clock_check()


def held_path(ops, node: str) -> pathlib.Path:
    return pathlib.Path(ops.evidence_dir) / HELD_DIR / f"{node}.json"


def held_record(ops, node: str) -> dict | None:
    try:
        return json.loads(held_path(ops, node).read_text())
    except (OSError, ValueError):
        return None


def _write(path: pathlib.Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True, default=str))
    temporary.replace(path)


class Change:
    """One of `kill`, `return`, `freeze` on a backend: its marks on `out`,
    what it says on `err`, its evidence in the backend's evidence
    directory."""

    def __init__(self, ops, args: argparse.Namespace, out=None, err=None, clock=time.time,
                 sleep=time.sleep, poll_s: float = mesh_ops.POLL_S):
        self.ops = ops
        self.args = args
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.clock = clock
        self.sleep = sleep
        self.poll_s = poll_s
        clock_ns = time.time_ns if clock is time.time else (lambda: int(round(clock() * 1e9)))
        self.marks = mesh_ops.Marks(self.out, self.err, args.marks, None, clock_ns)
        self.evidence: dict = {"node": args.node}

    def say(self, text: str) -> None:
        self.err.write(text + "\n")
        self.err.flush()

    def keep(self, verb: str) -> None:
        _write(pathlib.Path(self.ops.evidence_dir)
               / f"{verb}-{self.args.node}-{int(self.clock())}.json", self.evidence)

    def poll_until(self, check, seconds: float, polls: int = 1) -> list[str]:
        """Call `check()` (problems) until it passes `polls` times in a row
        or `seconds` pass; the last problems ([] when it passed)."""
        deadline = self.clock() + seconds
        streak = 0
        while True:
            began = self.clock()
            problems = check()
            streak = streak + 1 if not problems else 0
            if streak >= polls:
                return []
            now = self.clock()
            if now >= deadline:
                return problems or ["it did not hold on consecutive polls"]
            self.sleep(max(0.0, self.poll_s - (now - began)))

    # ------------------------------------------------------------ kill
    def kill(self) -> int:
        node, args = self.args.node, self.args
        lab = mesh_ops.Lab(self.ops.nodes())
        kind = kind_of(lab, node)
        if held_record(self.ops, node) is not None:
            raise mesh_ops.Refused(f"{node} is held down by an earlier kill: return --node {node}")
        self.ops.check("kill", [node])
        pairs = []
        if kind == "contributor":
            ok, why, gate = contrib_gate(self.ops, node, timeout_s=args.gate_timeout_s,
                                         poll_s=self.poll_s, clock=self.clock, sleep=self.sleep,
                                         video_gap=args.video_gap, unprotected=args.unprotected,
                                         change=False)
        elif args.unprotected:
            ok, why, gate = True, "", {"unprotected": True}
        else:
            ok, why, gate, pairs = ingress_gate(self.ops, node, timeout_s=args.gate_timeout_s,
                                                poll_s=self.poll_s, clock=self.clock,
                                                sleep=self.sleep)
        self.evidence.update(kind=kind, gate=gate)
        for note in gate.get("notes") or []:
            self.say(f"kill --node {node}: gate note: {note}")
        if not ok:
            self.keep("kill")
            raise mesh_ops.Refused(f"kill --node {node}: the {kind} gate refuses: {why}")
        clock_check(self.ops, lab)
        status = mesh_ops.status_of(self.ops.status(node))
        before = None if status is None else {
            "boot_id": status.boot_id, "git": status.build_git, "unix": status.build_unix,
            "args_sha256": status.args_sha256, "caches_fresh": status.caches_fresh}
        _write(held_path(self.ops, node), {"node": node, "kind": kind, "before": before,
                                           "pairs": pairs, "killed_unix": self.clock()})
        try:
            self.ops.hold_down(node)
        except Exception as error:  # noqa: BLE001 - nothing was killed; say so
            self.say(f"kill --node {node}: the hold could not be put in place ({error!r}); "
                     f"nothing was killed; {self.unhold(node)}")
            return mesh_ops.EXIT_STOPPED
        label = f"kill.{node}" + (".unprotected" if args.unprotected else "")
        self.marks.start(label)
        failed = None
        try:
            try:
                self.ops.kill(node)
            except Exception as error:  # noqa: BLE001 - whether it was killed is read below
                failed = error
            if failed is None:
                check = self.contributor_gone if kind == "contributor" else self.ingress_gone
                problems = self.poll_until(lambda: check(lab, node), END_S,
                                           1 if kind == "contributor" else 3)
        finally:
            self.marks.close()
        if failed is not None:
            return self.kill_failed(node, before, failed)
        self.evidence["end"] = problems
        self.keep("kill")
        if problems:
            self.say(f"kill --node {node}: killed and held down; its end was not seen within "
                     f"{END_S:g} s: {'; '.join(problems)}")
            return mesh_ops.EXIT_STOPPED
        self.say(f"kill --node {node}: killed and held down (return --node {node} brings it "
                 "back)")
        return mesh_ops.EXIT_DONE

    def unhold(self, node: str) -> str:
        """Take the hold away from a node that was not killed (the drop-in
        removed, `daemon-reload`, `start`, which leaves a running unit as it
        is) and forget its record; what became of the hold, in words."""
        try:
            self.ops.release(node)
        except Exception as error:  # noqa: BLE001 - say where the hold stands
            return (f"the hold is still in place and could not be taken away ({error!r}): "
                    f"return --node {node} takes it away")
        held_path(self.ops, node).unlink(missing_ok=True)
        return "the hold was taken away"

    def kill_failed(self, node: str, before: dict | None, error: Exception) -> int:
        """The kill raised (ssh dropped, a timeout): a node that still
        answers on the boot it had is running, and its hold is taken away;
        otherwise it is held down, and the operator is told so."""
        self.sleep(1.0)
        status = mesh_ops.status_of(self.ops.status(node))
        running = status is not None and before is not None and \
            status.boot_id is not None and status.boot_id == before.get("boot_id")
        self.evidence["end"] = [f"the kill failed: {error!r}"]
        self.evidence["still_running"] = running
        self.keep("kill")
        if running:
            self.say(f"kill --node {node}: the kill failed ({error!r}); {node} still runs on the "
                     f"same boot; {self.unhold(node)}")
        else:
            self.say(f"kill --node {node}: the kill failed ({error!r}) and {node} no longer "
                     f"answers on the boot it had: it is held down (Restart=no); return --node "
                     f"{node} brings it back")
        return mesh_ops.EXIT_STOPPED

    def contributor_gone(self, lab: mesh_ops.Lab, node: str) -> list[str]:
        """Both first forwarders show the contributor's audio 1 s old or more
        (or no longer list it)."""
        problems = []
        statuses = _statuses(self.ops, ingresses(lab))
        for forwarder, status in statuses.items():
            if status is None:
                problems.append(f"{forwarder} does not answer /api/node")
                continue
            entry = entry_of(status, lab.addresses(node))
            age = _age(entry.get("audio_age_ms")) if entry else None
            if age is not None and age < GONE_MS:
                problems.append(f"{forwarder} shows {node}'s audio {age} ms old")
        return problems

    def ingress_gone(self, lab: mesh_ops.Lab, node: str) -> list[str]:
        """The forwarder does not answer, every contributor that is `ready`
        names its lane in `degraded`, and every child of it has the other
        first forwarder's source within 100 ms for each audio stream it
        hears. av-contrib fills `degraded` only while a lane carries
        (node_status.rs `evaluate`): a contributor that hears no source
        (`standing_by`) or carries nothing lists none, and one held down
        by a kill does not answer; neither is asked."""
        problems = []
        sibling = next((name for name in ingresses(lab) if name != node), None)
        children = sorted(set(lab.compiled_children(node)) | set(lab.edges(lab.site(node))),
                          key=mesh_ops.natural)
        statuses = _statuses(self.ops, [node, *contributors(lab), *children])
        if statuses.get(node) is not None:
            problems.append(f"{node} still answers /api/node")
        for name in contributors(lab):
            status = statuses.get(name)
            if status is None and held_record(self.ops, name) is not None:
                continue
            if status is None:
                problems.append(f"{name} does not answer /api/node")
                continue
            if status.state != "ready":
                continue
            degraded = status.raw.get("degraded")
            if not isinstance(degraded, list) or node not in degraded:
                problems.append(f"{name} does not list {node} in degraded")
        staying = lab.addresses(sibling) if sibling else set()
        for child in children:
            status = statuses.get(child)
            if status is None or status.parents is None:
                problems.append(f"{child}'s parents[] are not known")
                continue
            heard = set()
            for parent in status.parents:
                heard |= shown(audio_ages(status, {parent.address}))
            ages = audio_ages(status, staying)
            late = sorted(stream for stream in heard
                          if ages.get(stream) is None or ages[stream] > SEEN_MS)
            if late:
                problems.append(f"{child} has no part of stream(s) {late} from {sibling} "
                                f"within {SEEN_MS} ms")
        return problems

    # ------------------------------------------------------------ return
    def return_(self) -> int:
        node, args = self.args.node, self.args
        lab = mesh_ops.Lab(self.ops.nodes())
        kind = kind_of(lab, node)
        record = held_record(self.ops, node)
        if record is None:
            raise mesh_ops.Refused(f"{node} is not held down by kill (no {held_path(self.ops, node)})")
        clock_check(self.ops, lab)
        self.evidence.update(kind=kind, record=record)
        self.marks.start(f"return.{node}")
        try:
            began_us = int(self.clock() * 1e6)
            self.ops.release(node)
            held_path(self.ops, node).unlink(missing_ok=True)
            restarts = self.ops.unit_restarts(node)
            before = record.get("before")
            if before is None:
                # It did not answer when it was killed: what it reports now
                # is what it runs.
                found = []
                self.poll_until(lambda: [] if found.append(
                    mesh_ops.status_of(self.ops.status(node))) or found[-1] is not None
                    else [f"{node} does not answer"], args.ready_timeout_s)
                status = found[-1] if found else None
                before = {"boot_id": 0, "caches_fresh": None,
                          "git": status.build_git if status else None,
                          "unix": status.build_unix if status else None,
                          "args_sha256": status.args_sha256 if status else None}
            expected = {"git": before.get("git"), "unix": before.get("unix"),
                        "args_sha256": before.get("args_sha256")}
            result = mesh_ops.wait_ready(
                self.ops, node, expected=expected, before_boot=before.get("boot_id") or 0,
                activation_us=began_us, lab=lab, before=before,
                gate_pairs=[tuple(pair) for pair in record.get("pairs") or []],
                restarts_before=restarts, timeout_s=args.ready_timeout_s, poll_s=self.poll_s,
                clock=self.clock, sleep=self.sleep)
            ok, why = result.ok, "; ".join(result.problems)
            if ok and kind == "contributor":
                ok, why, ready = contrib_ready(self.ops, node, timeout_s=args.ready_timeout_s,
                                               poll_s=self.poll_s, clock=self.clock,
                                               sleep=self.sleep)
                self.evidence["contributor_ready"] = ready
        finally:
            self.marks.close()
        self.evidence["ready"] = {"ok": ok, "why": why}
        self.keep("return")
        if not ok:
            self.say(f"return --node {node}: started, and not ready: {why}")
            return mesh_ops.EXIT_STOPPED
        self.say(f"return --node {node}: back and ready")
        return mesh_ops.EXIT_DONE

    # ------------------------------------------------------------ freeze
    def freeze(self) -> int:
        node, args = self.args.node, self.args
        lab = mesh_ops.Lab(self.ops.nodes())
        kind_of(lab, node)
        if args.ms <= 0:
            raise mesh_ops.Refused("--ms must be positive")
        clock_check(self.ops, lab)
        self.marks.start(f"freeze.{node}")
        try:
            self.ops.freeze(node, True)
            try:
                self.sleep(args.ms / 1000)
            finally:
                self.ops.freeze(node, False)
            problems = self.poll_until(
                lambda: [] if self.ops.status(node) is not None
                else [f"{node} does not answer /api/node"], END_S)
        finally:
            self.marks.close()
        if problems:
            self.say(f"freeze --node {node}: thawed; {'; '.join(problems)}")
            return mesh_ops.EXIT_STOPPED
        return mesh_ops.EXIT_DONE


def run(ops, verb: str, args: argparse.Namespace, *, out=None, err=None, clock=time.time,
        sleep=time.sleep, poll_s: float = mesh_ops.POLL_S) -> int:
    """`kill`, `return` or `freeze` (args from a parser of `add_options`)
    under the ops lock; the exit status."""
    if verb not in VERBS:
        raise ValueError(f"no change {verb!r}: {', '.join(VERBS)}")
    change = Change(ops, args, out, err, clock, sleep, poll_s)
    lock = mesh_ops.OpsLock(ops.lock_path)
    try:
        lock.acquire()
    except mesh_ops.Refused as refused:
        change.say(str(refused))
        return mesh_ops.EXIT_REFUSED
    try:
        return {"kill": change.kill, "return": change.return_, "freeze": change.freeze}[verb]()
    except mesh_ops.Refused as refused:
        change.say(str(refused))
        return mesh_ops.EXIT_REFUSED
    except Exception as error:  # noqa: BLE001 - a backend failed during the change
        change.say(f"{verb} --node {args.node}: stopped by an error: {error!r}")
        return mesh_ops.EXIT_STOPPED
    finally:
        lock.release()
