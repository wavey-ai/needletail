#!/usr/bin/env python3
"""The Needletail edge fault campaign.

Each scenario asks one question of the two-region lab of `edge_lab.py`,
states what must hold, injects one fault while readers play 4K video and
lossless audio, and writes one evidence file: what every reader saw (missing
parts, stalls and their length, time to the first part, delay from the
contributor), and what every node did (CPU, throttling, memory, descriptors,
sockets, drops).

  edge-campaign.py list [group ...]
  edge-campaign.py run [--out DIR] [--run-id ID] [--no-slow] [group or scenario ...]
  edge-campaign.py summary [DIR]
  edge-campaign.py down [--force]

A result is PASS, FAIL, or GAP: a failure that the scenario knows to be a
missing feature, with the name of the gap. Evidence is written under
target/edge-campaign/<run>/<scenario>/evidence.json, or for a lab with an id
(NEEDLETAIL_EDGE_LAB_ID) under target/edge-campaign-<id>/<run>/, with the
`latest` link of that lab's runs. A run holds its lab (scripts/lab_guard.py:
it waits while another run holds it) and refuses binaries outside a frozen
set (scripts/freeze-binaries.py) unless NEEDLETAIL_ALLOW_LIVE=1. `down`
does not wait for a run that holds the lab: it refuses at once and names
the holder, and --force tears the lab down anyway.

A run also keeps all its raw data in the run archive as each scenario ends
(~/needletail-data/<run id>/, NEEDLETAIL_DATA_ROOT overrides; run id
--run-id, else edge-campaign-<UTC time>): every scenario's directory, the
slice of each node's log it wrote, every node's hop and stop journals since
it began, its readers as listener steps; the node logs between scenarios;
the summary (scripts/edge_campaign_core.py, CampaignRecord).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import archived_run  # noqa: E402
import edge_lab as lab  # noqa: E402
import edge_campaign_core as core  # noqa: E402
import mesh_ops  # noqa: E402
from edge_campaign_core import AUDIO, HQ, VIDEO, scenario  # noqa: E402,F401

LON_READERS = "lon-readers"
NEW_READERS = "new-readers"


def play(ctx, seconds=40, video=3, audio=6, host=LON_READERS, **options):
    """Start the usual readers: 4K video and lossless audio."""
    jobs = []
    if video:
        jobs.append(ctx.readers("video", VIDEO, video, seconds, host=host, **options))
    if audio:
        jobs.append(ctx.readers("audio", AUDIO, audio, seconds, host=host, **options))
    return jobs


def clean(ctx, jobs):
    return all([ctx.expect_clean(job) for job in jobs])


def report_experience(ctx, jobs):
    for job in jobs:
        summary = job.summary
        ctx.measure(f"{job.key}_startup_ms_p50", summary.get("startup_ms_p50"))
        ctx.measure(f"{job.key}_startup_ms_max", summary.get("startup_ms_max"))
        ctx.measure(f"{job.key}_latency_p50_ms", summary.get("latency_p50_ms_median"))
        ctx.measure(f"{job.key}_latency_p99_ms_worst", summary.get("latency_p99_ms_worst"))


# =================================================================== baseline

@scenario("baseline", "baseline",
          "Do readers in both regions play 4K video and lossless audio on healthy links?",
          "Every reader is served, no part is missing, and no reader stalls with a 1 s buffer.")
def baseline(ctx):
    jobs = play(ctx, 40)
    far = [ctx.readers("video-ny", VIDEO, 2, 40, host=NEW_READERS),
           ctx.readers("audio-ny", AUDIO, 4, 40, host=NEW_READERS)]
    ctx.wait_readers()
    clean(ctx, jobs + far)
    report_experience(ctx, jobs + far)


# =================================================================== networks
# The link between a reader and its cache.

def reader_link(name, question, criterion, spec, video=3, audio=6, expect="clean",
                gap=None, seconds=45, options=None):
    @scenario(name, "network-reader", question, criterion)
    def run(ctx):
        netem = lab.netem_spec(**spec)
        ctx.conditions["reader link, both directions"] = netem
        if any(key in spec for key in ("loss", "burst", "reorder", "duplicate", "rate_kbit")):
            lab.set_tcp_per_packet(True, "london")
        lab.set_net(LON_READERS, "pub", "both", netem)
        ctx.event(f"reader link: {netem}")
        jobs = play(ctx, seconds, video, audio, **(options or {}))
        ctx.wait_readers()
        report_experience(ctx, jobs)
        if expect == "clean":
            clean(ctx, jobs)
        else:
            expect(ctx, jobs)
        if gap:
            ctx.gap = gap
    return run


for delay, jitter in ((20, 4), (80, 15), (150, 30)):
    reader_link(
        f"reader-delay-{delay}ms",
        f"Does a reader {delay} ms from its cache (jitter {jitter} ms) keep up with 5 ms "
        "audio parts and one-frame video parts?",
        "No part is missing and no reader stalls with a 1 s buffer.",
        {"delay_ms": delay, "jitter_ms": jitter})


def far_reader(ctx, jobs):
    video, audio = jobs
    ctx.expect_clean(audio)
    ctx.expect_served(video)
    ctx.expect_complete(video)
    ctx.expect_no_stall_after(video, 20)
    ctx.check("video: a reader 300 ms away starts without a stall",
              video.summary.get("stalls", 1) == 0,
              f"{video.summary.get('stalls')} stalls, {video.summary.get('stalled_ms')} ms, "
              f"first part after {video.summary.get('startup_ms_p50')} ms")
    ctx.gap = ("adaptive bit rate: a far reader has no lower rendition to start on while "
               "TCP opens its window")


reader_link(
    "reader-delay-300ms",
    "Does a reader 300 ms from its cache (a 600 ms round trip, jitter 50 ms) play 5 ms "
    "audio parts and 4K video?",
    "No part is missing. Audio never stalls. Video does not stall, at the start or later.",
    {"delay_ms": 300, "jitter_ms": 50}, expect=far_reader, seconds=60)

def lossy_reader(ctx, jobs):
    """A reader on a lossy link depends on TCP for every part. The edge must
    not make it worse: no part is lost, no reader is moved or refused, and
    what TCP costs stays small."""
    for job in jobs:
        ctx.expect_served(job)
        ctx.expect_complete(job)
        summary = job.summary
        played_ms = job.seconds * 1000 * job.count
        ctx.check(f"{job.key}: the readers stay on their cache",
                  summary.get("moves", 1) == 0 and summary.get("rejections", 1) == 0,
                  f"{summary.get('moves')} moves, {summary.get('rejections')} refusals")
        ctx.check(f"{job.key}: no stall longer than 1 s, and less than 2% of the time stalled",
                  summary.get("longest_stall_ms", 1 << 30) <= 1000
                  and summary.get("stalled_ms", 1 << 30) <= played_ms * 0.02,
                  f"{summary.get('stalls')} stalls in {summary.get('readers_stalled')} "
                  f"readers, longest {summary.get('longest_stall_ms')} ms, "
                  f"{summary.get('stalled_ms')} ms of {played_ms} ms")


LOSSY = ("No part is missing, no reader is moved or refused, no stall is longer than 1 s, "
         "and the readers stall for less than 2% of the time with a 1 s buffer.")

for loss in (0.5, 2, 5):
    reader_link(
        f"reader-loss-{loss}pct",
        f"What does {loss}% random packet loss between reader and cache (20 ms away) cost "
        "a reader?", LOSSY,
        {"delay_ms": 20, "jitter_ms": 2, "loss": loss}, expect=lossy_reader)

reader_link(
    "reader-burst-loss",
    "What does burst loss between reader and cache (Gilbert-Elliott: 1% to enter a bad "
    "state that loses 70%, 20% to leave it) cost a reader?", LOSSY,
    {"delay_ms": 20, "jitter_ms": 2, "burst": (1, 20, 70, 0.1)}, expect=lossy_reader)

reader_link(
    "reader-reorder-duplicate",
    "What do reordering (25% of packets) and duplication (1%) between reader and cache "
    "cost a reader?", LOSSY,
    {"delay_ms": 20, "reorder": 25, "duplicate": 1}, expect=lossy_reader,
    gap="4K over one TCP connection does not hold 16.5 Mbit/s when a quarter of the "
        "packets arrive 20 ms early. The viewer needs a lower rendition: see "
        "ladder-hlsjs-reorder. The reader model does not choose a rendition")


@scenario("reader-rate-cap", "network-reader",
          "What does a reader see on a link of 12 Mbit/s, below the 16.5 Mbit/s of the 4K "
          "playlist, and does it disturb a reader with a good link on the same cache?",
          "The capped reader is given a playlist that fits its link. The other readers of "
          "the cache play without a stall.")
def reader_rate_cap(ctx):
    netem = lab.netem_spec(delay_ms=20, rate_kbit=12000)
    ctx.conditions["link of lon-readers-2, both directions"] = netem
    lab.set_tcp_per_packet(True, "london")
    lab.set_net("lon-readers-2", "pub", "both", netem)
    good = play(ctx, 50, video=2, audio=4)
    capped_video = ctx.readers("video-capped", VIDEO, 1, 50, host="lon-readers-2")
    capped_audio = ctx.readers("audio-capped", AUDIO, 1, 50, host="lon-readers-2")
    ctx.wait_readers()
    clean(ctx, good)
    ctx.expect_clean(capped_audio, "audio on the capped link")
    summary = capped_video.summary
    ctx.check("the capped video reader is given a playlist that fits its link",
              summary.get("longest_stall_ms", 1) == 0 and summary.get("parts_missing", 1) == 0
              and summary.get("readers_ok") == 1,
              f"{summary.get('stalls')} stalls, {summary.get('stalled_ms')} ms stalled, "
              f"{summary.get('parts_missing')} parts missing, max behind "
              f"{summary.get('max_behind_ms')} ms")
    ctx.gap = "adaptive bit rate: a stream has one rendition"


@scenario("reader-asymmetric", "network-reader",
          "Does a reader on an asymmetric line play: 25 Mbit/s down and 1 Mbit/s up, 40 ms? "
          "And lossless audio in 5 ms parts on an uplink of 256 kbit/s?",
          "On 25/1 Mbit/s no part is missing and no reader stalls with a 1 s buffer. The "
          "256 kbit/s uplink is measured: one request for each 5 ms part costs about "
          "220 kbit/s of uplink.")
def reader_asymmetric(ctx):
    up = lab.netem_spec(delay_ms=40, rate_kbit=1000)
    down = lab.netem_spec(delay_ms=40, rate_kbit=25000)
    ctx.conditions["reader uplink"] = up
    ctx.conditions["reader downlink"] = down
    lab.set_tcp_per_packet(True, "london")
    lab.set_net(LON_READERS, "pub", "out", up)
    lab.set_net(LON_READERS, "pub", "in", down)
    jobs = play(ctx, 45, video=1, audio=2)
    ctx.wait_readers()
    report_experience(ctx, jobs)
    clean(ctx, jobs)
    narrow = lab.netem_spec(delay_ms=40, rate_kbit=256)
    ctx.conditions["reader uplink, second step"] = narrow
    lab.set_net(LON_READERS, "pub", "out", narrow)
    slow = ctx.readers("audio-256k-uplink", AUDIO, 1, 40)
    ctx.wait_readers()
    summary = slow.summary
    ctx.measure("uplink_256k_audio_stalled_ms", summary.get("stalled_ms"))
    ctx.measure("uplink_256k_audio_longest_stall_ms", summary.get("longest_stall_ms"))
    ctx.measure("uplink_256k_audio_latency_p50_ms", summary.get("latency_p50_ms_median"))
    ctx.check("lossless audio in 5 ms parts plays on an uplink of 256 kbit/s",
              summary.get("readers_ok") == 1 and summary.get("longest_stall_ms", 1 << 30) <= 1000
              and summary.get("parts_missing", 1) == 0,
              f"{summary.get('stalls')} stalls, {summary.get('stalled_ms')} ms stalled, longest "
              f"{summary.get('longest_stall_ms')} ms, {summary.get('parts_missing')} parts missing")
    ctx.gap = "one request for each 5 ms part: a narrow uplink needs grouped parts"


@scenario("reader-partition-heals", "network-reader",
          "A reader loses its network for 8 s and gets it back. Does it resume, and does "
          "the cache free what it held for the reader?",
          "The readers resume without a restart and lose only the parts of the outage. "
          "The cache counts no session of theirs 15 s after they have gone.")
def reader_partition(ctx):
    jobs = play(ctx, 50, video=2, audio=4, abandon_ms=2000)
    ctx.at(15)
    lab.set_link(LON_READERS, "pub", False)
    ctx.event("reader link cut")
    ctx.at(23)
    lab.set_link(LON_READERS, "pub", True)
    ctx.event("reader link joined")
    ctx.wait_readers()
    for job in jobs:
        ctx.expect_served(job)
        summary = job.summary
        ctx.check(f"{job.key}: the readers played again after the outage",
                  all(reader.get("parts_received", 0) > 0 and
                      (reader.get("stall_events") or [{}])[-1].get("at_ms", 0) < 35_000
                      for reader in job.readers),
                  f"longest stall {summary.get('longest_stall_ms')} ms, "
                  f"{summary.get('parts_missing')} parts missing")
        ctx.check(f"{job.key}: the stall is no longer than the outage and 3 s",
                  summary.get("longest_stall_ms", 1 << 30) <= 11_000,
                  f"{summary.get('longest_stall_ms')} ms")
    ctx.sleep(15)
    sessions = {name: lab.edge_view(name)["sessions"] for name in lab.edges()}
    ctx.check("no edge counts a session 15 s after the readers have gone",
              sum(sessions.values()) == 0, sessions)


# The private network of a region: the cache mesh between distributor and edge.

def edge_vlan(name, question, criterion, spec, direction="both", expect_ok=True, gap=None):
    @scenario(name, "network-cache-mesh", question, criterion)
    def run(ctx):
        netem = lab.netem_spec(**spec)
        ctx.conditions[f"private link of every London edge ({direction})"] = netem
        for edge in lab.edges():
            lab.set_net(edge, "vlan", direction, netem)
        ctx.event(f"private link of every edge: {netem}")
        jobs = play(ctx, 45)
        ctx.wait_readers()
        report_experience(ctx, jobs)
        clean(ctx, jobs)
        if gap:
            ctx.gap = gap
    return run


for loss in (0.5, 2, 5):
    edge_vlan(
        f"mesh-loss-{loss}pct",
        f"Does {loss}% random packet loss on the private network of a region reach a "
        "reader?",
        "No part is missing and no reader stalls with a 1 s buffer.",
        {"loss": loss})

edge_vlan(
    "mesh-burst-loss",
    "Does burst loss on the private network (1% to enter a state that loses 70%, 20% to "
    "leave it) reach a reader?",
    "No part is missing and no reader stalls with a 1 s buffer.",
    {"burst": (1, 20, 70, 0.1)})

edge_vlan(
    "mesh-delay-jitter",
    "Does 20 ms of delay with 8 ms of jitter on the private network reach a reader?",
    "No part is missing and no reader stalls with a 1 s buffer.",
    {"delay_ms": 20, "jitter_ms": 8})

edge_vlan(
    "mesh-reorder-duplicate",
    "Do reordering (25%) and duplication (2%) of datagrams on the private network reach "
    "a reader?",
    "No part is missing and no reader stalls with a 1 s buffer.",
    {"delay_ms": 2, "reorder": 25, "duplicate": 2})


def with_edge_fault(ctx, fault, heal, label, fault_at=14, heal_at=30, seconds=55,
                    video=3, audio=6, late=True, **options):
    """Readers play, the busiest edge suffers `fault(edge)`, then `heal(edge)`.
    Returns (jobs, edge)."""
    jobs = play(ctx, seconds, video, audio, **options)
    ctx.at(fault_at - 2)
    edge, counts = core.busiest_edge(VIDEO if video else AUDIO)
    ctx.measure("sessions_before_fault", counts)
    ctx.at(fault_at)
    fault(edge)
    ctx.event(f"{label}: {edge}")
    if late:
        # Readers that arrive during the fault.
        jobs.append(ctx.readers("late-audio", AUDIO, 3, seconds - fault_at - 8))
    if heal:
        ctx.at(heal_at)
        heal(edge)
        ctx.event(f"healed: {edge}")
    ctx.wait_readers()
    victims = sum(len(core.readers_on(job, edge)) for job in jobs[:2])
    ctx.check("the edge had readers when the fault began", victims > 0, f"{victims} readers")
    return jobs, edge


@scenario("mesh-partition-edge-heals", "network-cache-mesh",
          "An edge loses the private network for 15 s while readers still reach it. Do "
          "its readers move, and does the edge serve again after the network heals?",
          "No part is missing and no reader stalls with a 1 s buffer. After the heal the "
          "edge holds a moving playlist again.")
def mesh_partition_edge(ctx):
    jobs, edge = with_edge_fault(
        ctx, lambda edge: lab.set_link(edge, "vlan", False),
        lambda edge: lab.set_link(edge, "vlan", True), "private link cut")
    clean(ctx, jobs)
    after = ctx.readers("after-heal", AUDIO, 2, 12, cache=edge)
    after.wait()
    ctx.expect_clean(after, f"a reader pinned to {edge} after the heal")


@scenario("mesh-flap-edge", "network-cache-mesh",
          "The private link of an edge flaps: 2 s down, 2 s up, eight times. What do its "
          "readers see?",
          "No part is missing and no reader stalls with a 1 s buffer.")
def mesh_flap_edge(ctx):
    def flap(edge):
        def run():
            for _ in range(8):
                lab.set_link(edge, "vlan", False)
                time.sleep(2)
                lab.set_link(edge, "vlan", True)
                time.sleep(2)
        import threading
        threading.Thread(target=run, daemon=True).start()
    jobs, _edge = with_edge_fault(ctx, flap, None, "private link flaps for 32 s",
                                  seconds=65, late=False)
    clean(ctx, jobs)


@scenario("mesh-rate-cap-edge", "network-cache-mesh",
          "The private link of an edge carries 8 Mbit/s, less than the 16.5 Mbit/s of the "
          "4K playlist. What do the readers of the edge see?",
          "The readers move to a cache that can hold the playlist. No part is missing and "
          "no reader stalls with a 1 s buffer.")
def mesh_rate_cap_edge(ctx):
    netem = lab.netem_spec(rate_kbit=8000)
    ctx.conditions["private link of the edge, towards the edge"] = netem
    jobs, _edge = with_edge_fault(
        ctx, lambda edge: lab.set_net(edge, "vlan", "in", netem), None,
        "private link capped at 8 Mbit/s", late=False)
    clean(ctx, jobs)


@scenario("mesh-partition-distributor", "network-cache-mesh",
          "One distributor of a region loses the private network for 15 s. Do the edges "
          "carry on from the other?",
          "No part is missing and no reader stalls with a 1 s buffer.")
def mesh_partition_distributor(ctx):
    jobs = play(ctx, 50)
    ctx.at(14)
    lab.set_link("lon-dist-a", "vlan", False)
    ctx.event("private link of lon-dist-a cut")
    ctx.at(29)
    lab.set_link("lon-dist-a", "vlan", True)
    ctx.event("private link of lon-dist-a joined")
    ctx.wait_readers()
    clean(ctx, jobs)


# The wide-area path that carries a stream to a region.

def lost_upstream(ctx, jobs) -> None:
    """Name the gap when parts were lost before they reached the region."""
    missing = sum(job.summary.get("parts_missing", 0) for job in jobs)
    if missing:
        ctx.gap = (f"{missing} parts were lost before the region. A reader is not told "
                   "(no GAP=YES), so it waits for each of them, and the wide-area "
                   "protection is sized for a configured loss, not for the measured one")


def region_wan(name, question, criterion, spec, expect_clean=True):
    @scenario(name, "network-wide-area", question, criterion)
    def run(ctx):
        netem = lab.netem_spec(**spec)
        ctx.conditions["wide-area link of New York, both directions"] = netem
        lab.set_wan("newyork", "both", netem)
        ctx.event(f"wide-area link of New York: {netem}")
        ctx.sleep(3)
        jobs = play(ctx, 45, video=2, audio=4, host=NEW_READERS)
        ctx.wait_readers()
        report_experience(ctx, jobs)
        clean(ctx, jobs)
        lost_upstream(ctx, jobs)
    return run


for loss in (1, 3):
    region_wan(
        f"wan-loss-{loss}pct",
        f"Does {loss}% random loss on the path from the relays to a region reach the "
        "readers of that region?",
        "No part is missing and no reader stalls with a 1 s buffer.",
        {"loss": loss})

region_wan(
    "wan-burst-loss",
    "Does burst loss on the path to a region (1% to enter a state that loses 70%, 20% to "
    "leave it) reach its readers?",
    "No part is missing and no reader stalls with a 1 s buffer.",
    {"burst": (1, 20, 70, 0.1)})

region_wan(
    "wan-jitter",
    "Does a path with 30 ms of jitter on 34 ms of delay reach the readers of the region?",
    "No part is missing and no reader stalls with a 1 s buffer.",
    {"delay_ms": 34, "jitter_ms": 30})

region_wan(
    "wan-reorder-duplicate",
    "Does a path that reorders a quarter of its datagrams and duplicates 2% reach the "
    "readers of the region?",
    "No part is missing and no reader stalls with a 1 s buffer.",
    {"delay_ms": 34, "reorder": 25, "duplicate": 2})


@scenario("wan-partition-region-heals", "network-wide-area",
          "A region loses the wide-area network for 12 s. Its readers reach its edges, but "
          "no media arrives. What do the readers see, and do they resume?",
          "The readers resume without a restart within 3 s of the heal. They lose no more "
          "than the parts of the outage.")
def wan_partition_region(ctx):
    jobs = play(ctx, 60, video=2, audio=4, host=NEW_READERS, abandon_ms=3000)
    ctx.at(15)
    lab.set_wan_link("newyork", False)
    ctx.event("New York cut off the wide-area network")
    ctx.at(27)
    lab.set_wan_link("newyork", True)
    ctx.event("New York joined again")
    ctx.wait_readers()
    for job in jobs:
        ctx.expect_served(job)
        summary = job.summary
        last_stall = max((event["at_ms"] + event["ms"] for reader in job.readers
                          for event in reader.get("stall_events", [])), default=0)
        ctx.check(f"{job.key}: the readers play again within 3 s of the heal",
                  last_stall <= 30_500 and summary.get("parts_received", 0) > 0,
                  f"the last stall ended {last_stall / 1000:.1f} s after the readers "
                  f"started; outage from 15.0 s to 27.0 s")
        ctx.measure(f"{job.key}_parts_missing", summary.get("parts_missing"))
        ctx.measure(f"{job.key}_longest_stall_ms", summary.get("longest_stall_ms"))


@scenario("contributor-uplink-loss", "network-wide-area",
          "Does 3% loss on the uplink of the contributor reach readers in either region?",
          "No part is missing and no reader stalls with a 1 s buffer.")
def contributor_uplink_loss(ctx):
    netem = lab.netem_spec(loss=3)
    ctx.conditions["both links of the contributor, outwards"] = netem
    lab.set_net("contrib-lon", "pub", "out", netem)
    lab.set_net("contrib-lon", "vlan", "out", netem)
    ctx.sleep(3)
    jobs = play(ctx, 45)
    far = [ctx.readers("video-ny", VIDEO, 2, 45, host=NEW_READERS),
           ctx.readers("audio-ny", AUDIO, 4, 45, host=NEW_READERS)]
    ctx.wait_readers()
    clean(ctx, jobs + far)
    lost_upstream(ctx, jobs + far)


# =================================================================== nodes

@scenario("edge-kill", "node",
          "An edge with readers is killed (kill -9). What do its readers see?",
          "Its readers move. No part is missing and no reader stalls with a 1 s buffer. "
          "Readers that arrive afterwards are served.")
def edge_kill(ctx):
    jobs, _edge = with_edge_fault(ctx, lab.kill_service, None, "edge killed")
    clean(ctx, jobs)


@scenario("edge-freeze-resume", "node",
          "An edge with readers freezes for 12 s (SIGSTOP), then runs again with the state "
          "it had. What do its readers see, and what does it serve when it is back?",
          "Its readers move. No part is missing and no reader stalls. The edge serves "
          "current parts when it is back.")
def edge_freeze_resume(ctx):
    jobs, edge = with_edge_fault(
        ctx, lambda edge: lab.freeze_service(edge, True),
        lambda edge: lab.freeze_service(edge, False), "edge frozen",
        fault_at=14, heal_at=26, request_timeout_ms=600)
    clean(ctx, jobs)
    after = ctx.readers("after-resume", AUDIO, 2, 12, cache=edge)
    after.wait()
    ctx.expect_clean(after, f"a reader pinned to {edge} after it resumed")


@scenario("edge-rolling-restart", "node",
          "Every edge of a region is drained, stopped and started again, one after the "
          "other, as a deploy does. What do readers see?",
          "No part is missing and no reader stalls with a 1 s buffer.")
def edge_rolling_restart(ctx):
    jobs = play(ctx, 110, video=3, audio=8)
    ctx.at(12)
    for edge in lab.edges():
        status, _ = lab.http_json("lon-dist-a", "/api/control/drain-node", "POST",
                                  {"node_id": edge})
        ctx.event(f"drain {edge}: HTTP {status}")
        ctx.sleep(8)
        lab.terminate_service(edge)
        ctx.event(f"{edge} stopped")
        ctx.sleep(3)
        lab.start_service(edge)
        ctx.event(f"{edge} started")
        ctx.sleep(8)
        status, _ = lab.http_json("lon-dist-a", "/api/control/resume-node", "POST",
                                  {"node_id": edge})
    ctx.wait_readers()
    clean(ctx, jobs)
    ctx.measure("moves", {job.key: job.summary.get("moves") for job in jobs})


@scenario("edge-restart-no-drain", "node",
          "Every edge of a region is stopped with SIGTERM and started again, one after the "
          "other, with no drain. What do readers see?",
          "No part is missing and no reader stalls with a 1 s buffer.")
def edge_restart_no_drain(ctx):
    # Every cache that a reader was given at the start is restarted in turn.
    # A player reloads the steering manifest and learns of the others.
    jobs = play(ctx, 80, video=3, audio=8, steering=True)
    ctx.at(12)
    for edge in lab.edges():
        lab.terminate_service(edge)
        ctx.event(f"{edge} stopped with SIGTERM")
        ctx.sleep(3)
        lab.start_service(edge)
        ctx.event(f"{edge} started")
        ctx.sleep(10)
    ctx.wait_readers()
    clean(ctx, jobs)


def one_node_fault(name, target, kind, question, criterion, host=LON_READERS,
                   entries=None, hold=14, heal_after=12, seconds=55, expect="clean"):
    # The backbone relays run only in the legacy topology (the merged one,
    # the default, has none).
    needs = ["topology=legacy"] if lab.node(target)["role"] == "backbone" else None

    @scenario(name, "node", question, criterion, needs=needs)
    def run(ctx):
        jobs = play(ctx, seconds, host=host, entries=entries)
        ctx.at(hold)
        if kind == "kill":
            lab.kill_service(target)
            ctx.event(f"{target} killed")
        else:
            lab.freeze_service(target, True)
            ctx.event(f"{target} frozen")
        late = ctx.readers("late-audio", AUDIO, 3, seconds - hold - 8, host=host,
                           entries=entries)
        ctx.at(hold + heal_after)
        if kind == "kill":
            lab.start_service(target)
            ctx.event(f"{target} started again")
        else:
            lab.freeze_service(target, False)
            ctx.event(f"{target} runs again")
        ctx.wait_readers()
        if expect == "clean":
            clean(ctx, jobs + [late])
        else:
            expect(ctx, jobs + [late])
        if target.startswith("relay"):
            lost_upstream(ctx, jobs + [late])
    return run


one_node_fault("distributor-kill", "lon-dist-a", "kill",
               "One distributor of a region is killed and started again 12 s later. It is "
               "also the first entry point of the readers. What do readers see?",
               "No part is missing and no reader stalls. Readers that arrive while it is "
               "down are served by the other entry point.")
one_node_fault("distributor-freeze", "lon-dist-a", "freeze",
               "One distributor of a region freezes for 12 s, then runs again. What do "
               "readers see?",
               "No part is missing and no reader stalls. Readers that arrive while it is "
               "frozen are served.")
one_node_fault("relay-primary-kill", "relay-primary", "kill",
               "The primary relay is killed and started again 12 s later. Does the "
               "secondary relay carry both regions?",
               "No part is missing and no reader stalls with a 1 s buffer.")
one_node_fault("relay-primary-freeze", "relay-primary", "freeze",
               "The primary relay freezes for 12 s, then runs again. What do readers see?",
               "No part is missing and no reader stalls with a 1 s buffer.")
one_node_fault("relay-secondary-kill", "relay-secondary", "kill",
               "The secondary relay is killed and started again. Does anything change for "
               "readers?",
               "No part is missing and no reader stalls with a 1 s buffer.")
one_node_fault("relay-primary-kill-ny", "relay-primary", "kill",
               "The primary relay is killed. What do the readers of the far region see?",
               "No part is missing and no reader stalls with a 1 s buffer.",
               host=NEW_READERS)


def outage(ctx, jobs, begin_s, end_s, allow_after_s=3.0):
    """Checks for a fault that stops the stream: the readers resume by
    themselves soon after it ends."""
    for job in jobs:
        ctx.expect_served(job)
        summary = job.summary
        started = job.started_at - ctx.t0
        last_stall = max((started + (event["at_ms"] + event["ms"]) / 1000
                          for reader in job.readers
                          for event in reader.get("stall_events", [])), default=0.0)
        resumed = all(reader.get("parts_received", 0) > 0 for reader in job.readers)
        ctx.check(f"{job.key}: every reader plays again within {allow_after_s:.0f} s of the "
                  "end of the fault", resumed and last_stall <= end_s + allow_after_s,
                  f"the last stall ended at {last_stall:.1f} s; fault from {begin_s:.1f} s "
                  f"to {end_s:.1f} s; longest stall {summary.get('longest_stall_ms')} ms; "
                  f"{summary.get('parts_missing')} parts missing")


@scenario("distributors-both-kill", "node",
          "Both distributors of a region are killed and started again 10 s later. The "
          "edges have no source. What do readers see, and do they resume?",
          "Readers resume without a restart within 3 s of the return of the distributors.")
def distributors_both_kill(ctx):
    entries = lab.distributors() + lab.edges()[:2]
    jobs = play(ctx, 60, entries=entries, abandon_ms=3000)
    ctx.at(15)
    for name in lab.distributors():
        lab.kill_service(name)
    ctx.event("both London distributors killed")
    ctx.at(25)
    for name in lab.distributors():
        lab.start_service(name)
    ctx.event("both London distributors started again")
    ctx.wait_readers()
    outage(ctx, jobs, 15, 25)


@scenario("distributors-both-freeze", "node",
          "Both distributors of a region freeze for 10 s, then run again with the state "
          "they had. What do readers see?",
          "Readers resume without a restart within 3 s of the return of the distributors.")
def distributors_both_freeze(ctx):
    entries = lab.distributors() + lab.edges()[:2]
    jobs = play(ctx, 60, entries=entries, abandon_ms=3000)
    ctx.at(15)
    for name in lab.distributors():
        lab.freeze_service(name, True)
    ctx.event("both London distributors frozen")
    ctx.at(25)
    for name in lab.distributors():
        lab.freeze_service(name, False)
    ctx.event("both London distributors run again")
    ctx.wait_readers()
    outage(ctx, jobs, 15, 25)


@scenario("region-lost", "node",
          "Every edge and every distributor of a region is killed at once, entry points "
          "included. Do the readers of the region move to the neighbouring region, and "
          "what do they lose?",
          "Every reader plays on from a cache of the neighbouring region without a "
          "restart. No reader stalls for more than 5 s.")
def region_lost(ctx):
    region = lab.edges() + lab.distributors()
    home = {lab.node(name)["public_ipv4"] for name in region} | set(region)
    jobs = play(ctx, 70, video=2, audio=4, steering=True, abandon_ms=4000)
    ctx.at(20)
    for name in region:
        lab.kill_service(name)
    ctx.event("every edge and distributor of London killed")
    ctx.wait_readers()
    for job in jobs:
        ctx.expect_served(job)
        moved = [reader for reader in job.readers
                 if str(reader.get("final_cache", "")).split(":")[0] not in home]
        ctx.check(f"{job.key}: every reader ended on a cache of the neighbouring region",
                  len(moved) == job.count,
                  f"{len(moved)} of {job.count}; final caches "
                  f"{job.summary.get('readers_by_final_cache')}")
        ctx.check(f"{job.key}: no reader stalled for more than 5 s",
                  job.summary.get("longest_stall_ms", 1 << 30) <= 5000,
                  f"longest stall {job.summary.get('longest_stall_ms')} ms, "
                  f"{job.summary.get('parts_missing')} parts missing")
        ctx.measure(f"{job.key}_parts_missing", job.summary.get("parts_missing"))
        ctx.measure(f"{job.key}_longest_stall_ms", job.summary.get("longest_stall_ms"))
        ctx.measure(f"{job.key}_stalled_ms", job.summary.get("stalled_ms"))


@scenario("contributor-freeze", "node",
          "The contributor freezes for 5 s, then runs again. What do readers see?",
          "Readers resume without a restart within 3 s. The parts after the freeze follow "
          "the parts before it.")
def contributor_freeze(ctx):
    jobs = play(ctx, 50, abandon_ms=3000)
    ctx.at(15)
    lab.freeze_service("contrib-lon", True)
    ctx.event("contributor frozen")
    ctx.at(20)
    lab.freeze_service("contrib-lon", False)
    ctx.event("contributor runs again")
    ctx.wait_readers()
    outage(ctx, jobs, 15, 20)


@scenario("contributor-kill-restart", "node",
          "The contributor is killed and started again 5 s later, and the sources "
          "reconnect. What do readers that were playing see?",
          "Readers resume without a restart within 5 s of the return of the stream.")
def contributor_kill_restart(ctx):
    jobs = play(ctx, 70, abandon_ms=3000)
    ctx.at(15)
    lab.kill_service("contrib-lon")
    lab.stop_sources()
    ctx.event("contributor killed; its sources stop")
    ctx.at(20)
    lab.start_service("contrib-lon")
    ctx.sleep(1.5)
    lab.start_sources(6 * 3600, audio="daw")
    ctx.event("contributor and sources started again")
    lab.wait_live(VIDEO, "london", 30)
    back = ctx.elapsed()
    ctx.event("the stream is live again at the distributors")
    ctx.wait_readers()
    outage(ctx, jobs, 15, back, allow_after_s=5.0)
    after = [ctx.readers("video-after", VIDEO, 2, 12), ctx.readers("audio-after", AUDIO, 2, 12)]
    ctx.wait_readers(*after)
    clean(ctx, after)


@scenario("edge-cpu-starved", "node",
          "An edge with readers gets a fifth of a CPU for 20 s. What do its readers see, "
          "and does the region stop sending it new readers?",
          "No part is missing and no reader stalls: the edge sheds what it cannot serve, or "
          "its readers move.")
def edge_cpu_starved(ctx):
    jobs, edge = with_edge_fault(
        ctx, lambda edge: lab.set_limits(edge, cpu_percent=20),
        lambda edge: lab.set_limits(edge, cpu_percent=lab.default_limits(edge)["cpu_percent"]),
        "edge limited to 20% of a CPU",
        fault_at=14, heal_at=34, seconds=60, steering=True)
    clean(ctx, jobs)
    # The entry point takes a cache that falls behind out of the steering
    # manifest. A reader that follows the manifest, as a player does, moves.
    ctx.gap = ("a starved edge serves late until the entry point sees it fall behind: its "
               "readers stall for the seconds in between")


@scenario("distributor-cpu-starved", "node",
          "One distributor of a region gets a fifth of a CPU for 20 s. Do the edges take "
          "their parts from the other?",
          "No part is missing and no reader stalls with a 1 s buffer.")
def distributor_cpu_starved(ctx):
    jobs = play(ctx, 55)
    ctx.at(14)
    lab.set_limits("lon-dist-a", cpu_percent=20)
    ctx.event("lon-dist-a limited to 20% of a CPU")
    ctx.at(34)
    lab.set_limits("lon-dist-a", cpu_percent=lab.default_limits("lon-dist-a")["cpu_percent"])
    ctx.event("lon-dist-a has its CPU again")
    ctx.wait_readers()
    clean(ctx, jobs)


@scenario("edge-out-of-memory", "node",
          "An edge with 4K readers reaches its memory limit and the kernel kills it. A "
          "supervisor starts it again 2 s later. What do readers see?",
          "The kernel kills only that service. Its readers move. No part is missing and no "
          "reader stalls.")
def edge_out_of_memory(ctx):
    def squeeze(edge):
        stats = lab.read_cgroup(lab.cgroup_dir(edge))
        current = stats.get("memory_bytes", 0) // (1 << 20)
        # What the service cannot give back: its anonymous memory. A limit
        # just under the whole use only made the kernel drop file pages.
        anonymous = stats.get("memory_anon_bytes", stats.get("memory_bytes", 0)) // (1 << 20)
        ctx.measure("memory_mb_before", current)
        ctx.measure("anonymous_mb_before", anonymous)
        limit = max(12, anonymous - 16)
        # Leave less than the copy needs: the next seconds of parts do not fit.
        lab.set_limits(edge, memory_mb=limit)
        ctx.conditions["memory limit of the edge, MB"] = limit

    def revive(edge):
        ctx.measure("killed_by_kernel", lab.service_pid(edge) is None)
        if lab.service_pid(edge) is not None:
            lab.kill_service(edge)
        lab.start_service(edge)

    jobs, edge = with_edge_fault(ctx, squeeze, revive, "memory limit set below its use",
                                 fault_at=14, heal_at=22, seconds=55)
    ctx.check("the kernel killed the service at its memory limit",
              ctx.measurements.get("killed_by_kernel") is True)
    clean(ctx, jobs)


# =================================================================== capacity

def cpu_seconds(name: str) -> tuple[float, float]:
    """CPU seconds used and seconds throttled by a service so far."""
    stats = lab.read_cgroup(lab.cgroup_dir(name))
    return stats.get("usage_usec", 0) / 1e6, stats.get("throttled_usec", 0) / 1e6


def capacity_steps(ctx, stream: int, steps: list[int], edge: str = "lon-edge-1",
                   seconds: int = 24) -> list[dict]:
    """Pin more and more readers of one stream to one edge. Returns one row
    for each step: what the readers saw and what the edge used."""
    rows = []
    for count in steps:
        core.idle_edges(30)
        used, throttled = cpu_seconds(edge)
        began = time.time()
        job = ctx.readers(f"n{count}", stream, count, seconds, cache=edge,
                          arrival_spread_ms=2000)
        job.wait()
        elapsed = time.time() - began
        used_after, throttled_after = cpu_seconds(edge)
        summary = job.summary
        view = lab.edge_view(edge)
        row = {
            "readers": count,
            "served": summary.get("readers_ok"),
            "refused": sum(1 for reader in job.readers if reader.get("parts_received", 0) == 0),
            "readers_stalled": summary.get("readers_stalled"),
            "stalled_ms": summary.get("stalled_ms"),
            "longest_stall_ms": summary.get("longest_stall_ms"),
            "parts_missing": summary.get("parts_missing"),
            "latency_p50_ms": summary.get("latency_p50_ms_median"),
            "latency_p99_ms_worst": summary.get("latency_p99_ms_worst"),
            "startup_ms_p99": summary.get("startup_ms_p99"),
            "edge_cpu_percent": round((used_after - used) / elapsed * 100, 1),
            "edge_throttled_s": round(throttled_after - throttled, 2),
            "edge_memory_mb": round(lab.read_cgroup(lab.cgroup_dir(edge)).get(
                "memory_bytes", 0) / 1e6),
            "edge_rejected_total": view.get("rejected"),
        }
        rows.append(row)
        print(f"    {count:4} readers: served {row['served']}, stalled readers "
              f"{row['readers_stalled']}, latency p99 {row['latency_p99_ms_worst']} ms, "
              f"edge CPU {row['edge_cpu_percent']}%, throttled {row['edge_throttled_s']} s",
              flush=True)
        job.report_path.unlink(missing_ok=True)
        ctx.jobs.pop(job.key, None)
        time.sleep(10)
    return rows


def clean_step(row: dict) -> bool:
    return (row["served"] == row["readers"] and not row["readers_stalled"]
            and not row["parts_missing"])


def capacity(name, stream, steps, what, unlimited=True):
    # Admission is switched off: no limit on bits or on requests.
    needs = ["edge_egress_capacity_bps=10000000000",
             'extra_edge_args=["--egress-request-capacity", "100000000"]'] if unlimited else []

    @scenario(name, "capacity",
              f"How many readers of {what} does one Nanode edge serve, and what happens "
              "just past that number?",
              "The ceiling is the largest number of readers that all play without a stall. "
              "It is reported. Past it the scenario records what the readers see.",
              needs=needs)
    def run(ctx):
        ctx.conditions["edge"] = "lon-edge-1, admission by bit rate switched off" \
            if unlimited else "lon-edge-1 with its admission limit"
        rows = capacity_steps(ctx, stream, steps)
        ctx.measure("steps", rows)
        clean = [row["readers"] for row in rows if clean_step(row)]
        ceiling = max(clean, default=0)
        ctx.measure("ceiling_readers", ceiling)
        ctx.check("the edge serves the smallest step without a stall", bool(clean),
                  rows[0])
        past = [row for row in rows if row["readers"] > ceiling]
        if past:
            first = past[0]
            ctx.note(f"past the ceiling ({first['readers']} readers): "
                     f"{first['readers_stalled']} readers stalled, "
                     f"{first['stalled_ms']} ms in all, edge CPU {first['edge_cpu_percent']}%")
    return run


capacity("capacity-edge-4k", VIDEO, [4, 8, 16, 24, 32, 48, 64], "4K video (16.5 Mbit/s)")
capacity("capacity-edge-flac", AUDIO, [10, 20, 40, 60, 80, 120, 160],
         "lossless audio in 5 ms parts")
capacity("capacity-edge-opus", HQ, [10, 20, 40, 60, 80, 120, 160],
         "HQ Opus audio in 5 ms parts")


@scenario("admission-refuses-cleanly", "capacity",
          "Thirty 4K readers, and then 240 lossless audio readers, ask one edge of a "
          "region of four edges, each with its admission limit: half as many again as the "
          "region admits. A refused reader is given the other edges. Is the surplus "
          "refused, and do the admitted readers play?",
          "Every admitted reader plays without a stall. The surplus is refused with HTTP "
          "429. The edge is never throttled for more than a tenth of the time.")
def admission_refuses_cleanly(ctx):
    for label, stream, count in (("4k", VIDEO, 30), ("flac", AUDIO, 240)):
        core.idle_edges(30)
        used, throttled = cpu_seconds("lon-edge-1")
        began = time.time()
        job = ctx.readers(f"offered-{label}", stream, count, 30, cache="lon-edge-1",
                          arrival_spread_ms=3000)
        job.wait()
        elapsed = time.time() - began
        used_after, throttled_after = cpu_seconds("lon-edge-1")
        served = [reader for reader in job.readers if reader.get("parts_received", 0) > 0]
        refused = [reader for reader in job.readers if reader.get("parts_received", 0) == 0]
        stalled = [reader for reader in served if reader.get("stalls")]
        ctx.measure(f"{label}_offered", count)
        ctx.measure(f"{label}_served", len(served))
        ctx.measure(f"{label}_refused", len(refused))
        ctx.measure(f"{label}_edge_cpu_percent",
                    round((used_after - used) / elapsed * 100, 1))
        ctx.measure(f"{label}_edge_throttled_s", round(throttled_after - throttled, 2))
        ctx.measure(f"{label}_served_by_cache", job.summary.get("readers_by_final_cache"))
        ctx.check(f"{label}: some readers were admitted and the surplus was refused",
                  0 < len(served) < count, f"{len(served)} served, {len(refused)} refused")
        ctx.check(f"{label}: every admitted reader played without a stall",
                  not stalled and all(reader.get("parts_missing", 1) == 0 for reader in served),
                  f"{len(stalled)} of {len(served)} admitted readers stalled, "
                  f"{sum(reader.get('stalled_ms', 0) for reader in served)} ms in all")
        ctx.check(f"{label}: the edge was not throttled for more than a tenth of the time",
                  throttled_after - throttled <= elapsed / 10,
                  f"{throttled_after - throttled:.1f} s of {elapsed:.0f} s")
        time.sleep(12)


# =================================================================== players

PLAYERS = lab.ROOT / "scripts" / "players"


def hlsjs(ctx, key: str, url: str, seconds: int, host: str = LON_READERS,
          wait: bool = True, extra: list[str] | None = None):
    """Play a URL with hls.js in headless Chrome on a reader host. Returns
    the process and the path of its report."""
    import subprocess
    report = ctx.directory / f"{key}.hlsjs.json"
    report.unlink(missing_ok=True)
    command = lab.enter(host) + [
        "node", str(PLAYERS / "hlsjs-play.mjs"), "--url", url, "--seconds", str(seconds),
        "--report", str(report), "--quiet", "--session-id", f"{ctx.scenario.name}-{key}",
        *(extra or [])]
    log = open(ctx.directory / f"{key}.hlsjs.log", "ab")
    process = subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                               start_new_session=True)
    ctx.event(f"hls.js starts on {host} ({key})")
    if wait:
        try:
            process.wait(timeout=seconds + 60)
        except subprocess.TimeoutExpired:
            process.kill()
    return process, report


def player_summary(report: pathlib.Path) -> dict:
    """The numbers of a player report that the evidence of a scenario keeps."""
    try:
        data = json.loads(report.read_text())
    except (OSError, ValueError):
        return {"verdict": "no report"}
    out = {"verdict": (data.get("verdict") or {}).get("result"),
           "reason": (data.get("verdict") or {}).get("reason")}
    if "hlsjs" in data:
        playback = data.get("playback", {})
        stalls = data.get("stalls", {})
        latency = (data.get("latency") or {}).get("hls_latency_s") or {}
        frames = data.get("frames") or {}
        errors = (data.get("errors") or {}).get("list") or []
        out.update({
            "player": f"hls.js {data['hlsjs'].get('version')}",
            "browser": (data.get("browser") or {}).get("version"),
            "first_frame_ms": (data.get("startup") or {}).get("first_frame_ms"),
            "media_played_s": playback.get("media_played_s"),
            "wall_s": playback.get("wall_s"),
            "stalled_ms": stalls.get("total_stalled_ms"),
            "stalls": len(stalls.get("playhead") or []) + len(stalls.get("element_waiting") or []),
            "frames": frames.get("total"), "frames_dropped": frames.get("dropped"),
            "latency_s_p50": latency.get("p50"), "latency_s_max": latency.get("max"),
            "errors": [f"{error.get('details')}" + (" (fatal)" if error.get("fatal") else "")
                       for error in errors][:12],
            "hosts": [(round(move.get("t", 0)), move.get("to"))
                      for move in (data.get("steering") or {}).get("host_timeline", [])][:8],
        })
        if "failover" in data:
            failover = data["failover"]
            out["failover"] = {key: failover.get(key) for key in (
                "victim", "kill_ms", "media_gap_after_kill_ms", "moved_to_pathway",
                "first_error_after_kill_ms")}
    else:
        out.update({key: data.get(key) for key in (
            "first_frame_s", "speed", "stalls", "stalled_ms", "media_s", "wall_s",
            "start_attempts") if key in data})
        out["errors"] = (data.get("log") or {}).get("first_lines", [])[:6] \
            if isinstance(data.get("log"), dict) else None
    return out


def entry_url(stream: int, key: str, site: str = "london", extra: str = "") -> str:
    return f"{lab.base_url(lab.distributors(site)[0])}/live/{stream}/master.m3u8?sid={key}{extra}"


@scenario("spec-playlists", "players",
          "Do the playlists of an edge and of a distributor, and the HTTP behaviour behind "
          "them, follow the LL-HLS rules of draft-pantos-hls-rfc8216bis-22?",
          "No rule that the draft states as MUST is broken. What it states as SHOULD, and "
          "Apple's authoring rules, are reported.")
def spec_playlists(ctx):
    import hls_spec
    warm = [ctx.readers("warm-video", VIDEO, 1, 40, cache="lon-edge-1"),
            ctx.readers("warm-audio", AUDIO, 1, 40, cache="lon-edge-1")]
    ctx.at(8)
    results = {}
    for node in ("lon-edge-1", "lon-dist-a"):
        fetch = hls_spec.lab_fetch(node)
        for label, stream, video, fmp4 in (("video", VIDEO, True, True),
                                           ("audio", AUDIO, False, False)):
            path = f"/live/{stream}/stream.m3u8"
            watched = hls_spec.watch(fetch, path, 6, video, fmp4=fmp4)
            http = hls_spec.summarise(hls_spec.check_http(fetch, path, video=video))
            results[f"{node} {label}"] = {"playlist": watched, "http": http}
            fails = watched["fail"] + http["fail"]
            ctx.check(f"{node}, {label}: no MUST of the draft is broken", not fails,
                      "; ".join(f"#{item['rule']} {item['text'][:110]}" for item in fails[:4]))
    status, _head, master = lab.curl("lon-dist-a", "/live/1/master.m3u8?sid=spec")
    multivariant = hls_spec.summarise(hls_spec.check_multivariant(master))
    results["multivariant"] = multivariant
    ctx.check("the Multivariant Playlist breaks no MUST", status == 200
              and not multivariant["fail"], [item["text"][:90] for item in multivariant["fail"]])
    pathways = {line.split('PATHWAY-ID="')[1].split('"')[0]
                for line in master.splitlines() if "#EXT-X-STREAM-INF" in line
                and 'PATHWAY-ID="' in line}
    known = ",".join(sorted(pathways))
    status, head, steering = lab.curl(
        "lon-dist-a", f"/live/1/steering.json?sid=spec&p={known}")
    steering_findings = hls_spec.summarise(hls_spec.check_steering(steering, pathways))
    results["steering"] = steering_findings
    ctx.check("the steering manifest breaks no MUST", status == 200
              and not steering_findings["fail"]
              and "application/vnd.apple.steering-list" in head,
              [item["text"][:90] for item in steering_findings["fail"]])
    warnings = {}
    for where, item in results.items():
        groups = [item] if "warn" in item else list(item.values())
        for group in groups:
            for finding in group.get("warn", []):
                warnings[f"#{finding['rule']} {finding['text'][:100]}"] = where
    ctx.measure("findings", results)
    ctx.measure("should_rules_not_met", sorted(warnings))
    ctx.wait_readers(*warm)


@scenario("player-hlsjs-4k", "players",
          "Does hls.js in headless Chrome play the 4K stream through the entry point of a "
          "region for 45 s?",
          "The player plays without a stall and without a fatal error.")
def player_hlsjs_4k(ctx):
    _process, report = hlsjs(ctx, "video", entry_url(VIDEO, "hlsjs-4k"), 45)
    summary = player_summary(report)
    ctx.measure("hlsjs", summary)
    ctx.check("hls.js played without a stall", summary.get("verdict") == "played",
              f"{summary.get('reason')}; errors {summary.get('errors')}")
    ctx.check("no frame was dropped", summary.get("frames_dropped") == 0
              and (summary.get("frames") or 0) > 500,
              f"{summary.get('frames_dropped')} of {summary.get('frames')}")


@scenario("player-hlsjs-4k-second-region", "players",
          "Does hls.js play the 4K stream from the second region, 5,600 km from the "
          "contributor, with a viewer who is near that region?",
          "The player plays without a stall and without a fatal error, and drops no "
          "frame.")
def player_hlsjs_4k_second_region(ctx):
    _process, report = hlsjs(ctx, "video", entry_url(VIDEO, "hlsjs-4k-ny", site="newyork"), 45,
                             host=NEW_READERS)
    summary = player_summary(report)
    ctx.measure("hlsjs", summary)
    ctx.check("hls.js played without a stall", summary.get("verdict") == "played",
              f"{summary.get('reason')}; errors {summary.get('errors')}")
    ctx.check("no frame was dropped", summary.get("frames_dropped") == 0
              and (summary.get("frames") or 0) > 500,
              f"{summary.get('frames_dropped')} of {summary.get('frames')}")


@scenario("player-hlsjs-edge-kill", "players",
          "hls.js plays the 4K stream, and the cache that serves it is killed. Does the "
          "player move to another cache without a stall?",
          "The player plays on without a stall. It moves to another pathway.")
def player_hlsjs_edge_kill(ctx):
    import subprocess
    report = ctx.directory / "failover.hlsjs.json"
    result = subprocess.run(
        ["python3", str(PLAYERS / "failover-test.py"), "--stream", str(VIDEO),
         "--seconds", "55", "--kill-after", "22", "--report", str(report)],
        capture_output=True, text=True, timeout=240,
        env={**__import__("os").environ, "NEEDLETAIL_EDGE_LAB_WORK": str(lab.WORK)})
    (ctx.directory / "failover.log").write_text(result.stdout[-4000:] + result.stderr[-4000:])
    summary = player_summary(report)
    ctx.measure("hlsjs", summary)
    failover = summary.get("failover") or {}
    ctx.check("hls.js played on without a stall", summary.get("verdict") == "played",
              f"{summary.get('reason')}; errors {summary.get('errors')}")
    ctx.check("the player moved to another pathway",
              bool(failover.get("moved_to_pathway"))
              and failover.get("moved_to_pathway") != failover.get("victim"),
              f"{failover.get('victim')} to {failover.get('moved_to_pathway')}, media again "
              f"{failover.get('media_gap_after_kill_ms')} ms after the kill")


@scenario("player-hlsjs-link-cut", "players",
          "hls.js plays the 4K stream, and the network of the viewer is cut for 8 s. Does "
          "the player play again when the network returns?",
          "The player plays again within 5 s of the return of its network, without a "
          "fatal error and without a reload of the page.")
def player_hlsjs_link_cut(ctx):
    import subprocess
    process, report = hlsjs(ctx, "video", entry_url(VIDEO, "hlsjs-link-cut"), 70, wait=False)
    ctx.at(25)
    lab.set_link(LON_READERS, "pub", False)
    ctx.event("the link of the player is cut")
    ctx.at(33)
    lab.set_link(LON_READERS, "pub", True)
    ctx.event("the link of the player is back")
    try:
        process.wait(timeout=130)
    except subprocess.TimeoutExpired:
        process.kill()
    summary = player_summary(report)
    ctx.measure("hlsjs", summary)
    try:
        data = json.loads(report.read_text())
    except (OSError, ValueError):
        data = {}
    stalls = (data.get("stalls") or {})
    events = (stalls.get("playhead") or []) + (stalls.get("element_waiting") or [])
    ends = [event.get("end_ms") or (event.get("t", 0) + event.get("ms", 0))
            for event in events if isinstance(event, dict)]
    played = (data.get("playback") or {}).get("final") or {}
    ctx.measure("stall_events", events[:8])
    ctx.check("hls.js plays at the end, without a fatal error",
              played.get("paused") is False and (played.get("ready_state") or 0) >= 3
              and not any("(fatal)" in error for error in summary.get("errors") or []),
              f"{summary.get('verdict')}: {summary.get('reason')}; errors "
              f"{summary.get('errors')}")
    ctx.check("the stall is no longer than the outage and 5 s",
              (summary.get("stalled_ms") or 0) <= 13_000,
              f"{summary.get('stalled_ms')} ms stalled in all; stalls end at {ends[:6]}")


@scenario("player-hlsjs-cache-kill", "players",
          "hls.js plays the 4K stream, and the cache that serves it is killed. The same "
          "question as player-hlsjs-edge-kill, with the faults of the lab in hand, so "
          "that it also runs on real instances.",
          "The player plays on with less than 1 s of stall in all, and its media comes "
          "from another cache.")
def player_hlsjs_cache_kill(ctx):
    import subprocess
    process, report = hlsjs(ctx, "video", entry_url(VIDEO, "hlsjs-cache-kill"), 60,
                            wait=False)
    ctx.at(25)
    victim, counts = core.busiest_edge(VIDEO)
    lab.kill_service(victim)
    ctx.event(f"kill -9 {victim}; sessions {counts}")
    ctx.defer(lambda: lab.start_service(victim))
    try:
        process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
    summary = player_summary(report)
    ctx.measure("hlsjs", summary)
    ctx.check("hls.js played on, with less than 1 s of stall in all",
              summary.get("verdict") in ("played", "stalled")
              and (summary.get("stalled_ms") or 0) <= 1000
              and not any("(fatal)" in error for error in summary.get("errors") or []),
              f"{summary.get('verdict')}: {summary.get('stalled_ms')} ms stalled; "
              f"{summary.get('reason')}; errors {summary.get('errors')}")
    dead = lab.node(victim)["public_ipv4"]
    hosts = summary.get("hosts") or []
    ctx.check("the media came from another cache after the kill",
              any(when > 20_000 and dead not in str(host) for when, host in hosts),
              f"hosts over time: {hosts}")


@scenario("player-hlsjs-region-lost", "players",
          "hls.js plays the 4K stream, and every edge and distributor of its region is "
          "killed at once. Does the player go on from the neighbouring region?",
          "The player plays on without a fatal error, stalls for less than 6 s in all, "
          "and its media comes from a cache of the neighbouring region within 8 s.")
def player_hlsjs_region_lost(ctx):
    import subprocess
    process, report = hlsjs(ctx, "video", entry_url(VIDEO, "hlsjs-region-lost"), 70,
                            wait=False)
    ctx.at(25)
    region = lab.edges() + lab.distributors()
    for name in region:
        lab.kill_service(name)
    ctx.event("every edge and distributor of London killed")
    try:
        process.wait(timeout=130)
    except subprocess.TimeoutExpired:
        process.kill()
    summary = player_summary(report)
    ctx.measure("hlsjs", summary)
    ctx.check("hls.js played on, with less than 6 s of stall in all",
              summary.get("verdict") in ("played", "stalled")
              and (summary.get("stalled_ms") or 0) <= 6000
              and not any("(fatal)" in error for error in summary.get("errors") or []),
              f"{summary.get('verdict')}: {summary.get('stalled_ms')} ms stalled, "
              f"{summary.get('media_played_s')} s of media in {summary.get('wall_s')} s; "
              f"{summary.get('reason')}; errors {summary.get('errors')}")
    far = {lab.node(name)["public_ipv4"] for name in lab.edges("newyork")}
    hosts = summary.get("hosts") or []
    # The clock of the player starts about a second after that of the
    # scenario, so the loss is at about 24 s on it.
    moved = [when for when, host in hosts
             if when > 20_000 and str(host).split(":")[0] in far]
    ctx.check("the media came from the neighbouring region within 8 s of the loss",
              bool(moved) and moved[0] <= 33_000, f"hosts over time: {hosts}")


@scenario("player-hlsjs-drain", "players",
          "hls.js plays the 4K stream, and the cache that serves it is drained. Does the "
          "player move without a stall?",
          "The player plays on without a stall, and its media comes from another cache "
          "within 5 s of the drain.")
def player_hlsjs_drain(ctx):
    import subprocess
    process, report = hlsjs(ctx, "video", entry_url(VIDEO, "hlsjs-drain"), 55, wait=False)
    ctx.at(22)
    edge, counts = core.busiest_edge(VIDEO)
    status, _ = lab.http_json("lon-dist-a", "/api/control/drain-node", "POST",
                              {"node_id": edge})
    ctx.event(f"drain {edge}: HTTP {status}; sessions {counts}")
    ctx.defer(lambda: lab.http_json("lon-dist-a", "/api/control/resume-node", "POST",
                                    {"node_id": edge}))
    try:
        process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
    summary = player_summary(report)
    ctx.measure("hlsjs", summary)
    ctx.check("hls.js played on without a stall", summary.get("verdict") == "played",
              f"{summary.get('reason')}; errors {summary.get('errors')}")
    drained_host = lab.node(edge)["public_ipv4"]
    hosts = summary.get("hosts") or []
    # The clock of the player starts about a second after that of the scenario.
    moved = [item for item in hosts if item[0] > 18000 and drained_host not in str(item[1])]
    ctx.check("the media came from another cache within 5 s of the drain",
              bool(moved) and moved[0][0] <= 27500, f"hosts over time: {hosts}")


@scenario("player-ffmpeg-4k", "players",
          "Does ffmpeg play the 4K stream through the entry point at real time for 40 s?",
          "ffmpeg opens the stream at the first attempt and reads it at real time "
          "without a pause.")
def player_ffmpeg_4k(ctx):
    import subprocess
    report = ctx.directory / "video.ffmpeg.json"
    subprocess.run(
        lab.enter(LON_READERS) + [
            "python3", str(PLAYERS / "ffmpeg-play.py"), "--url", entry_url(VIDEO, "ffmpeg-4k"),
            "--seconds", "40", "--start-attempts", "3", "--report", str(report), "--quiet"],
        capture_output=True, timeout=200)
    try:
        data = json.loads(report.read_text())
    except (OSError, ValueError):
        data = {}
    verdict = (data.get("verdict") or {}).get("result")
    ctx.measure("ffmpeg", {"verdict": verdict,
                           "reason": (data.get("verdict") or {}).get("reason"),
                           "start_attempts": (data.get("startup") or {}).get("attempts"),
                           "speed": (data.get("playback") or {}).get("speed")})
    ctx.check("ffmpeg played at real time without a pause", verdict == "played",
              (data.get("verdict") or {}).get("reason"))


# =================================================================== audience

@scenario("flash-crowd", "audience",
          "Two thousand readers ask for one HQ audio playlist within one second, at the two "
          "entry points of a region of four Nanode edges, while eight readers already "
          "play it. What do the readers that were there see, how many of the crowd are "
          "served, and how long does each wait for an answer?",
          "The readers that were there do not stall. Every reader that the region admits "
          "plays without a stall. A reader that the neighbouring region takes loses no "
          "part. Every other reader is refused. No node is throttled for more than a "
          "tenth of the time.")
def flash_crowd(ctx):
    before = {name: cpu_seconds(name) for name in lab.edges() + lab.distributors()}
    present = ctx.readers("present", HQ, 8, 50)
    ctx.at(12)
    began = time.time()
    crowd = ctx.readers("crowd", HQ, 2000, 30, arrival_spread_ms=1000,
                        host="lon-readers-2", connect_timeout_ms=5000)
    ctx.wait_readers()
    elapsed = time.time() - began
    ctx.expect_clean(present, "the readers that were there")
    served = [reader for reader in crowd.readers if reader.get("parts_received", 0) > 0]
    refused = [reader for reader in crowd.readers if reader.get("parts_received", 0) == 0]
    stalled = [reader for reader in served if reader.get("stalls")]
    startups = sorted(reader.get("startup_ms", 0) for reader in served)
    ctx.measure("crowd", len(crowd.readers))
    ctx.measure("served", len(served))
    ctx.measure("refused", len(refused))
    ctx.measure("served_by_cache", crowd.summary.get("readers_by_final_cache"))
    ctx.measure("served_startup_ms", {
        "p50": startups[len(startups) // 2] if startups else None,
        "p99": startups[int(len(startups) * 0.99) - 1] if startups else None,
        "max": startups[-1] if startups else None})
    ctx.measure("stalled_ms_of_served", sum(reader.get("stalled_ms", 0) for reader in served))
    ctx.check("part of the crowd was admitted", len(served) > 0, f"{len(served)} of 2000")
    home = [reader for reader in served
            if str(reader.get("final_cache", "")).startswith(("lon-", "10.90.0."))]
    far = [reader for reader in served if reader not in home]
    ctx.measure("served_in_the_region", len(home))
    ctx.measure("served_by_the_neighbouring_region", len(far))
    ctx.check("every reader that the region admitted played without a stall",
              not any(reader.get("stalls") for reader in home)
              and all(reader.get("parts_missing", 1) == 0 for reader in home),
              f"{sum(1 for reader in home if reader.get('stalls'))} of {len(home)} stalled, "
              f"{sum(reader.get('stalled_ms', 0) for reader in home)} ms in all")
    far_stalled = sum(reader.get("stalled_ms", 0) for reader in far)
    ctx.check("the readers that the neighbouring region took lost no part and stalled for "
              "less than 2% of the time",
              all(reader.get("parts_missing", 1) == 0 for reader in far)
              and far_stalled <= len(far) * 30_000 * 0.02,
              f"{len(far)} readers, {far_stalled} ms stalled in all")
    ctx.check("the rest of the crowd was refused, not left to wait",
              len(served) + len(refused) == len(crowd.readers) == 2000,
              f"{len(refused)} refused, {2000 - len(crowd.readers)} without a report")
    for name, (used, throttled) in before.items():
        used_after, throttled_after = cpu_seconds(name)
        ctx.measure(f"{name}_cpu_seconds", round(used_after - used, 1))
        ctx.measure(f"{name}_throttled_s", round(throttled_after - throttled, 2))
    worst = max(cpu_seconds(name)[1] - before[name][1] for name in before)
    ctx.check("no edge or entry point was throttled for more than a tenth of the time",
              worst <= (elapsed + 12) / 10, f"worst {worst:.1f} s")


@scenario("slow-and-stuck-readers", "audience",
          "Among the readers of one edge, three read 4K video at 4 Mbit/s, a quarter of "
          "its bit rate, and three stop reading and keep their connections open. Do the "
          "other readers of the edge play, and does the edge free what it held for "
          "them?",
          "The other readers do not stall. The memory of the edge returns to within "
          "60 MB of its level before, 30 s after the slow and stuck readers have gone.")
def slow_and_stuck_readers(ctx):
    edge = "lon-edge-1"
    good = [ctx.readers("video", VIDEO, 2, 60, cache=edge),
            ctx.readers("audio", AUDIO, 6, 60, cache=edge)]
    ctx.at(8)
    memory_before = lab.read_cgroup(lab.cgroup_dir(edge)).get("memory_bytes", 0) / 1e6
    slow = ctx.readers("slow", VIDEO, 3, 35, cache=edge, read_kbps=4000,
                       request_timeout_ms=20000, abandon_ms=60000)
    stuck = ctx.readers("stuck", VIDEO, 3, 35, cache=edge, freeze_after_seconds=5)
    peak = 0.0
    while time.time() - ctx.t0 < 46:
        peak = max(peak, lab.read_cgroup(lab.cgroup_dir(edge)).get("memory_bytes", 0) / 1e6)
        time.sleep(1)
    ctx.wait_readers()
    clean(ctx, good)
    ctx.sleep(30)
    memory_after = lab.read_cgroup(lab.cgroup_dir(edge)).get("memory_bytes", 0) / 1e6
    ctx.measure("edge_memory_mb", {"before": round(memory_before), "peak": round(peak),
                                   "30_s_after": round(memory_after)})
    ctx.measure("slow_readers", {key: slow.summary.get(key) for key in
                                 ("readers_ok", "parts_received", "max_behind_ms")})
    ctx.check("the memory of the edge returned to its level before",
              memory_after <= memory_before + 60,
              f"{memory_before:.0f} MB before, peak {peak:.0f} MB, "
              f"{memory_after:.0f} MB 30 s after")
    ctx.check("the edge stayed far below its memory limit", peak < 700, f"{peak:.0f} MB")


@scenario("mixed-edge", "audience",
          "One edge serves four 4K readers and twenty lossless audio readers at once. Does "
          "either kind disturb the other?",
          "No part is missing and no reader stalls with a 1 s buffer.")
def mixed_edge(ctx):
    edge = "lon-edge-1"
    jobs = [ctx.readers("video", VIDEO, 4, 45, cache=edge),
            ctx.readers("audio", AUDIO, 20, 45, cache=edge, arrival_spread_ms=2000)]
    ctx.wait_readers()
    clean(ctx, jobs)
    report_experience(ctx, jobs)
    ctx.measure("served_by_cache", {job.key: job.summary.get("readers_by_final_cache")
                                    for job in jobs})


# =================================================================== endurance

def slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of y over x."""
    count = len(points)
    if count < 2:
        return 0.0
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    if spread == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / spread


# ================================================================== long tail

# The long tail is tested with a small album and few slots, so that the
# contributor is not the limit: 4K and two tracks in two codecs are five
# streams. The album of eight tracks (17 streams) is the capacity scenario.
SMALL_ALBUM = [VIDEO, 2001, 2002, 3001, 3002]
ALBUM = [VIDEO] + [2001 + track for track in range(8)] + [3001 + track for track in range(8)]


def album_readers(ctx, seconds: int, streams=None, prefix="s", **options) -> dict:
    return {stream: ctx.readers(f"{prefix}{stream}", stream, 1, seconds, **options)
            for stream in (streams or ALBUM)}


def slot_refusals(names) -> dict:
    """Streams that each node refused for want of a playlist slot, from its
    log."""
    import re
    out = {}
    for name in names:
        text = re.sub(r"\x1b\[[0-9;]*m", "", lab.node_log(name))
        refused = sorted({int(item) for item in re.findall(
            r"every playlist slot holds a live stream[^\n]*?stream_id=(\d+)", text)})
        if refused:
            out[name] = refused
    return out


def plays_clean(job) -> bool:
    summary = job.summary
    return (summary.get("readers_ok") == 1 and summary.get("parts_missing") == 0
            and summary.get("longest_stall_ms", 1) == 0)


@scenario("long-tail-one-stream-too-many", "audience",
          "A region carries five streams, and every node has four playlist slots. What do "
          "the readers of each stream see?",
          "Four streams play without a missing part and without a stall. The stream that "
          "does not fit is refused by name on the nodes that are full, and its readers are "
          "told at once that it is not there.",
          needs=["daw_album_tracks=2", "playlist_slots=4"])
def long_tail_full(ctx):
    time.sleep(8)
    jobs = album_readers(ctx, 40, SMALL_ALBUM)
    ctx.wait_readers()
    played = [stream for stream, job in jobs.items() if plays_clean(job)]
    absent = [stream for stream in SMALL_ALBUM if stream not in played]
    refused = slot_refusals(lab.names())
    ctx.measure("streams_played_clean", played)
    ctx.measure("streams_not_played", {
        stream: {key: jobs[stream].summary.get(key) for key in (
            "readers_ok", "parts_received", "parts_missing", "stalls", "longest_stall_ms",
            "request_errors")} for stream in absent})
    ctx.measure("refused_for_want_of_a_slot", refused)
    ctx.check("four streams play without a missing part or a stall", len(played) == 4,
              f"{len(played)} played clean; not clean: {absent}")
    named = {stream for streams in refused.values() for stream in streams}
    ctx.check("the stream that does not play is the one that the full nodes refuse by name",
              len(absent) == 1 and named == set(absent), f"not played {absent}; refused {refused}")
    for stream in absent[:2]:
        status, _head, _body = lab.curl("lon-dist-a", f"/live/{stream}/stream.m3u8", timeout=6)
        ctx.check(f"the entry point answers 404 for stream {stream} at once", status == 404,
                  f"HTTP {status}")
    ctx.gap = "a node holds as many streams as it has playlist slots (--playlist-slots)"


@scenario("long-tail-more-slots", "audience",
          "The same five streams with eight playlist slots on every node. Do they all play, "
          "and can one edge serve a reader of every stream?",
          "Every stream plays without a missing part and without a stall, through the "
          "entry point and on one edge that holds all five.",
          needs=["daw_album_tracks=2", "playlist_slots=8"])
def long_tail_slots(ctx):
    time.sleep(8)
    jobs = album_readers(ctx, 40, SMALL_ALBUM)
    ctx.wait_readers()
    for stream, job in jobs.items():
        ctx.expect_clean(job, f"stream {stream}")
    ctx.measure("refused_for_want_of_a_slot", slot_refusals(lab.names()))
    core.idle_edges(30)
    began = time.time()
    pinned = album_readers(ctx, 30, SMALL_ALBUM, prefix="one-edge-s", cache="lon-edge-1")
    time.sleep(max(0.0, began + 12 - time.time()))
    held = lab.edge_view("lon-edge-1")["playlists"]
    ctx.wait_readers(*pinned.values())
    ctx.check("one edge holds all five playlists", len(held) >= 5, sorted(held, key=int))
    clean = [stream for stream, job in pinned.items() if plays_clean(job)]
    ctx.check("every stream plays clean on the one edge", len(clean) == 5,
              f"{len(clean)} of 5; not clean: {[s for s in SMALL_ALBUM if s not in clean]}")


@scenario("capacity-seventeen-streams", "capacity",
          "A region carries 17 streams: 4K video and an album of eight tracks, lossless and "
          "HQ, all audio in 5 ms parts: 3,205 parts a second. Which role is the limit on "
          "one vCPU?",
          "Reported: the CPU of each role and the share of the parts that a reader of each "
          "stream receives. No relay, distributor or edge is the limit.",
          needs=["daw_album_tracks=8", "playlist_slots=24"])
def capacity_seventeen_streams(ctx):
    many_streams(ctx, 8)
    ctx.gap = ("a contributor on one vCPU does not carry sixteen audio streams of 5 ms "
               "parts beside 4K: the cost is for each part, and grouped parts are gap 9")


def album_of(tracks: int) -> list:
    return ([VIDEO] + [2001 + track for track in range(tracks)]
            + [3001 + track for track in range(tracks)])


def many_streams(ctx, tracks: int) -> None:
    """One reader of every stream of an album on one edge: the CPU of each
    role and what the readers received."""
    time.sleep(10)
    roles = {"contributor": "contrib-lon", "relay": "relay-primary",
             "distributor": "lon-dist-a", "edge": "lon-edge-1"}
    if "relay-primary" not in lab.service_names("backbone"):
        # The merged topology: lon-dist-a is the first relay.
        roles.pop("relay")
    before = {role: cpu_seconds(name) for role, name in roles.items()}
    began = time.time()
    jobs = album_readers(ctx, 40, album_of(tracks), cache="lon-edge-1")
    ctx.wait_readers()
    elapsed = time.time() - began
    cpu = {role: round((cpu_seconds(name)[0] - before[role][0]) / elapsed * 100, 1)
           for role, name in roles.items()}
    ctx.measure("cpu_percent_by_role", cpu)
    received = {}
    for stream, job in jobs.items():
        expected = 40 * (5 if stream == VIDEO else 200)
        received[stream] = round(job.summary.get("parts_received", 0) / expected, 2)
    ctx.measure("share_of_parts_received", received)
    ctx.measure("streams_played_clean", [stream for stream, job in jobs.items()
                                         if plays_clean(job)])
    ctx.measure("refused_for_want_of_a_slot", slot_refusals(lab.names()))
    ctx.check("no relay, distributor or edge is above 90% of its CPU",
              all(cpu[role] < 90 for role in ("relay", "distributor", "edge")
                  if role in cpu), cpu)
    ctx.check("every stream plays without a missing part or a stall",
              all(plays_clean(job) for job in jobs.values()),
              f"CPU {cpu}; share of parts received {received}")


@scenario("capacity-nine-streams", "capacity",
          "A region carries 9 streams: 4K video and an album of four tracks, lossless and "
          "HQ, all audio in 5 ms parts: 1,605 parts a second. Does every stream play, and "
          "what does each role use of its vCPU?",
          "Every stream plays without a missing part or a stall. No relay, distributor or "
          "edge is above 90% of its CPU.",
          needs=["daw_album_tracks=4", "playlist_slots=24"])
def capacity_nine_streams(ctx):
    many_streams(ctx, 4)
    ctx.gap = ("the cost of a stream of 5 ms parts is for each part: grouped parts are "
               "gap 9")


@scenario("capacity-thirteen-streams", "capacity",
          "A region carries 13 streams: 4K video and an album of six tracks, lossless and "
          "HQ, all audio in 5 ms parts: 2,405 parts a second. Does every stream play, and "
          "what does each role use of its vCPU?",
          "Every stream plays without a missing part or a stall. No relay, distributor or "
          "edge is above 90% of its CPU.",
          needs=["daw_album_tracks=6", "playlist_slots=24"])
def capacity_thirteen_streams(ctx):
    many_streams(ctx, 6)
    ctx.gap = ("the cost of a stream of 5 ms parts is for each part: grouped parts are "
               "gap 9")


@scenario("capacity-scales-with-edges", "capacity",
          "Does a region with four edges serve twice the readers of the same region with "
          "two edges?",
          "With lossless audio in 5 ms parts and default admission, four edges admit "
          "between 1.7 and 2.3 times the readers of two. No admitted reader stalls.")
def capacity_scales(ctx):
    for name in ("lon-edge-3", "lon-edge-4"):
        lab.kill_service(name)
    ctx.event("lon-edge-3 and lon-edge-4 stopped: the region has two edges")
    time.sleep(6)
    two = ctx.readers("two-edges", AUDIO, 120, 40, cache="lon-edge-1",
                      arrival_spread_ms=3000)
    ctx.wait_readers(two)
    for name in ("lon-edge-3", "lon-edge-4"):
        lab.start_service(name)
    ctx.event("the region has four edges again")
    time.sleep(12)
    core.idle_edges(30)
    four = ctx.readers("four-edges", AUDIO, 240, 40, cache="lon-edge-1",
                       arrival_spread_ms=3000)
    ctx.wait_readers(four)
    served = {}
    for job in (two, four):
        home = [reader for reader in job.readers if reader.get("parts_received", 0) > 0]
        served[job.key] = len(home)
        stalled = [reader for reader in home if reader.get("stalls")]
        ctx.check(f"{job.key}: no reader that London admitted stalls", not stalled,
                  f"{len(stalled)} of {len(home)} stalled")
        ctx.measure(f"{job.key}_served_in_region", len(home))
        ctx.measure(f"{job.key}_by_cache", job.summary.get("readers_by_final_cache"))
    ratio = served["four-edges"] / max(served["two-edges"], 1)
    ctx.measure("four_to_two_ratio", round(ratio, 2))
    ctx.check("four edges admit 1.7 to 2.3 times the readers of two", 1.7 <= ratio <= 2.3,
              f"{served['two-edges']} readers on two edges, {served['four-edges']} on four")


@scenario("capacity-entry-joins", "capacity",
          "How many new sessions a second does one entry point assign, on one vCPU? A "
          "join is a new TLS connection and a request for the Multivariant Playlist with "
          "a session of its own.",
          "The entry point answers every join at 100 a second with a 99th percentile "
          "under 500 ms, and the readers that play are not disturbed. The highest rate "
          "that it answers in that time is reported.")
def capacity_entry_joins(ctx):
    entry = "lon-dist-a"
    present = play(ctx, 150, video=1, audio=3)
    time.sleep(8)
    rows, failures = [], 0
    for rate in (50, 100, 200, 400, 800, 1600):
        used, throttled = cpu_seconds(entry)
        began = time.time()
        try:
            result = lab.join_load("lon-readers-2", entry, AUDIO, rate, 12, f"r{rate}")
        except (lab.LabError, ValueError, RuntimeError) as error:
            result = {"joins": 0, "answered": 0, "error": str(error)[:160]}
        elapsed = time.time() - began
        used_after, throttled_after = cpu_seconds(entry)
        row = {"offered_per_second": rate,
               "entry_cpu_percent": round((used_after - used) / elapsed * 100, 1),
               "entry_throttled_s": round(throttled_after - throttled, 2),
               **{key: result.get(key) for key in (
                   "joins", "answered", "answered_200", "answered_per_second", "by_answer",
                   "join_ms_p50", "join_ms_p99", "started_late_ms_p99", "error")}}
        row["in_time"] = bool(result.get("joins")) \
            and result.get("answered") == result.get("joins") \
            and (result.get("join_ms_p99") or 1e9) < 500 \
            and (result.get("started_late_ms_p99") or 0) < 250
        rows.append(row)
        ctx.event(f"{rate} joins a second: {row['answered']} of {row['joins']} answered, "
                  f"p99 {row['join_ms_p99']} ms, entry CPU {row['entry_cpu_percent']}%, "
                  f"{row['by_answer']}")
        failures = 0 if row["in_time"] else failures + 1
        if failures >= 2:
            break
        time.sleep(4)
    in_time = [row["offered_per_second"] for row in rows if row["in_time"]]
    ctx.measure("steps", rows)
    ctx.measure("joins_per_second_answered_in_time", max(in_time, default=0))
    ctx.check("every join at 100 a second is answered, with a 99th percentile under 500 ms",
              100 in in_time, [(row["offered_per_second"], row["answered"], row["joins"],
                                row["join_ms_p99"]) for row in rows])
    ctx.wait_readers()
    for job in present:
        ctx.expect_clean(job, f"{job.key} during the joins")


# =========================================================== real instances

@scenario("clock-skew-edge", "node",
          "The clock of one edge is 4 s ahead, and then 4 s behind. Do its readers play, "
          "and does the region still give it readers?",
          "Readers that are pinned to the edge play without a missing part and without a "
          "stall, and the entry points still list the edge as a cache.")
def clock_skew_edge(ctx):
    edge = "lon-edge-2"
    for offset in (4, -4):
        lab.set_clock(edge, offset)
        ctx.defer(lambda: lab.set_clock(edge, None))
        ctx.event(f"the clock of {edge} is {offset:+d} s from true time")
        time.sleep(8)
        job = ctx.readers(f"skew{offset:+d}", AUDIO, 2, 25, cache=edge)
        video = ctx.readers(f"skew{offset:+d}-video", VIDEO, 1, 25, cache=edge)
        _status, _head, master = lab.curl(
            "lon-dist-a", f"/live/{AUDIO}/master.m3u8?sid=skew{offset}", timeout=6)
        ctx.wait_readers(job, video)
        ctx.expect_clean(job, f"audio on the edge at {offset:+d} s")
        ctx.expect_clean(video, f"video on the edge at {offset:+d} s")
        candidates = 0
        for index in range(12):
            _status, _head, text = lab.curl(
                "lon-dist-a", f"/live/{AUDIO}/master.m3u8?sid=skew{offset}-{index}", timeout=6)
            candidates += f'PATHWAY-ID="{edge}"' in text
        ctx.check(f"the entry point lists the edge as a cache at {offset:+d} s", candidates > 0,
                  f"in {candidates} of 12 Multivariant Playlists")
        lab.set_clock(edge, None)
        time.sleep(6)


@scenario("clock-skew-distributor", "node",
          "The clock of one distributor, which is an entry point, is 4 s ahead. Do the "
          "readers of the region play?",
          "Every reader plays without a missing part and without a stall, through either "
          "entry point.")
def clock_skew_distributor(ctx):
    before = deadline_drops()
    lab.set_clock("lon-dist-a", 4)
    ctx.defer(lambda: lab.set_clock("lon-dist-a", None))
    time.sleep(8)
    jobs = [ctx.readers("via-skewed", AUDIO, 4, 30, entries=["lon-dist-a"]),
            ctx.readers("via-true", AUDIO, 4, 30, entries=["lon-dist-b"]),
            ctx.readers("video", VIDEO, 2, 30)]
    ctx.wait_readers()
    after = deadline_drops()
    ctx.measure("relay_objects_dropped_at_their_deadline",
                {name: after[name] - before.get(name, 0) for name in after})
    for job in jobs:
        ctx.expect_clean(job)
    report_experience(ctx, jobs)
    ctx.gap = RELAY_DEADLINE_GAP


RELAY_DEADLINE_GAP = (
    "a relay object carries its deadline as a time of day, one second after its "
    "publication on the clock of the contributor: a relay or a distributor whose clock is "
    "more than that ahead of the contributor drops every object as expired")


def deadline_drops() -> dict:
    """Relay symbols that each relay and distributor refused because their
    object was past its deadline."""
    out = {}
    for name in lab.names("backbone") + lab.names("distributor"):
        try:
            snapshot = lab.mesh_local(name)
        except (lab.LabError, subprocess.TimeoutExpired):
            continue
        out[name] = int((snapshot.get("relay_session") or {}).get("deadline_drops") or 0)
    return out


@scenario("clock-skew-contributor-behind", "node",
          "The clock of the contributor is 2 s behind true time for 20 s while readers "
          "play. What do they see?",
          "No part is missing and no reader stalls for more than 1 s.")
def clock_skew_contributor_behind(ctx):
    jobs = play(ctx, 60, video=2, audio=4)
    before = deadline_drops()
    ctx.at(15)
    lab.set_clock("contrib-lon", -2)
    ctx.defer(lambda: lab.set_clock("contrib-lon", None))
    ctx.event("the clock of the contributor is 2 s behind")
    ctx.at(35)
    lab.set_clock("contrib-lon", None)
    ctx.event("the clock of the contributor is true again")
    ctx.wait_readers()
    after = deadline_drops()
    ctx.measure("relay_objects_dropped_at_their_deadline",
                {name: after[name] - before.get(name, 0) for name in after})
    for job in jobs:
        ctx.expect_served(job)
        ctx.expect_complete(job)
        ctx.check(f"{job.key}: no stall of more than 1 s",
                  job.summary.get("longest_stall_ms", 1 << 30) <= 1000,
                  f"longest {job.summary.get('longest_stall_ms')} ms")
    report_experience(ctx, jobs)
    ctx.gap = RELAY_DEADLINE_GAP


@scenario("clock-skew-contributor", "node",
          "The clock of the contributor steps 5 s ahead and then back to true time while "
          "readers play. What do they see?",
          "No part is missing and no reader stalls for more than 1 s. The Media Sequence "
          "Number of a playlist never goes down.")
def clock_skew_contributor(ctx):
    import hls_spec
    jobs = play(ctx, 60, video=2, audio=4)
    fetch = hls_spec.lab_fetch("lon-dist-a")
    seen = []

    def sample():
        _status, _headers, body, _seconds = fetch(f"/live/{VIDEO}/stream.m3u8")
        seen.append(hls_spec.parse_media(body)["media_sequence"])
    ctx.at(15)
    sample()
    lab.set_clock("contrib-lon", 5)
    ctx.defer(lambda: lab.set_clock("contrib-lon", None))
    ctx.event("the clock of the contributor is 5 s ahead")
    ctx.at(30)
    sample()
    lab.set_clock("contrib-lon", None)
    ctx.event("the clock of the contributor is true again")
    ctx.at(45)
    sample()
    ctx.wait_readers()
    sample()
    for job in jobs:
        ctx.expect_served(job)
        ctx.expect_complete(job)
        ctx.check(f"{job.key}: no stall of more than 1 s",
                  job.summary.get("longest_stall_ms", 1 << 30) <= 1000,
                  f"longest {job.summary.get('longest_stall_ms')} ms")
    numbers = [item for item in seen if item is not None]
    ctx.check("the Media Sequence Number never goes down",
              numbers == sorted(numbers) and len(numbers) == 4, numbers)
    report_experience(ctx, jobs)


@scenario("edge-disk-full", "node",
          "The disk of an edge fills up. Do its readers still play, and does it come back "
          "when there is space again?",
          "Readers of the edge play without a missing part and without a stall while the "
          "disk is full, and the service has not restarted.")
def edge_disk_full(ctx):
    edge = "lon-edge-3"
    before = lab.service_pid(edge)
    job = ctx.readers("audio", AUDIO, 4, 50, cache=edge)
    video = ctx.readers("video", VIDEO, 1, 50, cache=edge)
    ctx.at(10)
    free_mb = lab.fill_disk(edge, True)
    ctx.defer(lambda: lab.fill_disk(edge, False))
    ctx.event(f"the disk of {edge} is full ({free_mb} MB were free)")
    ctx.at(40)
    lab.fill_disk(edge, False)
    ctx.event("the disk has space again")
    ctx.wait_readers(job, video)
    ctx.expect_clean(job, "audio on the edge with a full disk")
    ctx.expect_clean(video, "video on the edge with a full disk")
    ctx.check("the service of the edge did not restart", lab.service_pid(edge) == before,
              f"pid {before} then {lab.service_pid(edge)}")


# ===================================================================== ladder

LADDER = ["hls_renditions=" + json.dumps(lab.ladder_renditions())]
LADDER_STREAMS = [item[0] for item in lab.LADDER]
LADDER_HEIGHT = {item[0]: int(item[4].split("x")[1]) for item in lab.LADDER}


def level_timeline(report: pathlib.Path) -> list[tuple[int, int]]:
    """(milliseconds, frame height) for each rendition that hls.js switched
    to, from a player report."""
    try:
        data = json.loads(report.read_text())
    except (OSError, ValueError):
        return []
    steering = data.get("steering") or {}
    heights = {level.get("index"): level.get("height") for level in steering.get("levels", [])}
    return [(round(event.get("t", 0)), heights.get(event.get("level")))
            for event in steering.get("level_events", []) if event.get("event") == "switched"]


def height_at(timeline: list[tuple[int, int]], at_ms: int) -> int | None:
    height = None
    for when, value in timeline:
        if when <= at_ms:
            height = value
    return height


def ladder_player_checks(ctx, key: str, report: pathlib.Path, stall_budget_ms: int) -> dict:
    summary = player_summary(report)
    timeline = level_timeline(report)
    summary["renditions"] = timeline[:24]
    ctx.measure(f"hlsjs_{key}", summary)
    played = (summary.get("media_played_s") or 0) / max(summary.get("wall_s") or 1, 1)
    ctx.check(f"{key}: hls.js played, with at most {stall_budget_ms} ms of stall in all",
              summary.get("verdict") in ("played", "stalled")
              and (summary.get("stalled_ms") or 0) <= stall_budget_ms and played >= 0.93
              and not any("(fatal)" in error for error in summary.get("errors") or []),
              f"{summary.get('verdict')}: {summary.get('stalled_ms')} ms stalled, "
              f"{summary.get('media_played_s')} s of media in {summary.get('wall_s')} s; "
              f"errors {summary.get('errors')}")
    ctx.check(f"{key}: no frame was dropped", summary.get("frames_dropped") == 0,
              f"{summary.get('frames_dropped')} of {summary.get('frames')}")
    return summary


@scenario("ladder-playlists", "ladder",
          "A stream is a ladder of four renditions (2160p, 1080p, 720p, 360p). Does the "
          "entry point list them for every pathway, does an edge with one reader hold the "
          "whole ladder, do the renditions line up, and does each report the others?",
          "Every variant carries BANDWIDTH, AVERAGE-BANDWIDTH, CODECS, RESOLUTION and "
          "FRAME-RATE. The edge holds four renditions within 5 s of its first reader. "
          "The same Media Sequence Number starts within 100 ms in every rendition. Each "
          "playlist reports the three others within two parts of their newest part. The "
          "edge releases the whole ladder when its reader has gone.",
          needs=LADDER)
def ladder_playlists(ctx):
    import hls_spec
    status, _head, master = lab.curl("lon-dist-a", "/live/1/master.m3u8?sid=ladder-spec")
    variants = [line for line in master.splitlines() if line.startswith("#EXT-X-STREAM-INF")]
    pathways = {line.split('PATHWAY-ID="')[1].split('"')[0] for line in variants
                if 'PATHWAY-ID="' in line}
    complete = [line for line in variants if all(
        word in line for word in ("BANDWIDTH=", "AVERAGE-BANDWIDTH=", "CODECS=",
                                  "RESOLUTION=", "FRAME-RATE="))]
    ctx.check("the Multivariant Playlist lists four renditions for each pathway",
              status == 200 and len(variants) == 4 * len(pathways) and len(pathways) >= 2,
              f"{len(variants)} variants on {len(pathways)} pathways: {sorted(pathways)}")
    ctx.check("every variant carries its bit rates, codecs, frame and frame rate",
              len(complete) == len(variants) and variants, f"{len(complete)} of {len(variants)}")
    multivariant = hls_spec.summarise(hls_spec.check_multivariant(master))
    ctx.check("the Multivariant Playlist breaks no MUST", not multivariant["fail"],
              [item["text"][:90] for item in multivariant["fail"]])
    ctx.check("a neighbouring region is a pathway of the ladder",
              any(not name.startswith("lon-") for name in pathways), sorted(pathways))

    edge = "lon-edge-2"
    reader = ctx.readers("reader", 11, 1, 45, cache=edge)
    ctx.at(5)
    held = lab.edge_view(edge)["playlists"]
    ctx.check("the edge holds the whole ladder 5 s after its first reader",
              all(str(stream) in held for stream in LADDER_STREAMS), sorted(held))
    ctx.at(10)
    fetch = hls_spec.lab_fetch(edge)
    playlists = {}
    for stream in LADDER_STREAMS:
        _status, _headers, body, _seconds = fetch(f"/live/{stream}/stream.m3u8")
        playlists[stream] = hls_spec.parse_media(body)
    starts = {}
    for stream, playlist in playlists.items():
        clock = None
        for segment in playlist["segments"]:
            if segment["pdt"] is not None:
                clock = segment["pdt"]
            if clock is not None:
                starts.setdefault(segment["msn"], {})[stream] = clock
                clock += segment["duration"] or 0
    shared = {msn: values for msn, values in starts.items() if len(values) == 4}
    spread = max((max(values.values()) - min(values.values()) for values in shared.values()),
                 default=None)
    ctx.measure("segment_start_spread_ms",
                None if spread is None else round(spread * 1000, 1))
    ctx.check("the same segment starts within 100 ms in every rendition",
              spread is not None and spread <= 0.1,
              f"{len(shared)} shared segments, widest spread "
              f"{None if spread is None else round(spread * 1000, 1)} ms")
    targets = {stream: (playlist["target_duration"], playlist["part_target"])
               for stream, playlist in playlists.items()}
    ctx.check("every rendition declares the same target duration and part target",
              len(set(targets.values())) == 1, targets)
    reports_ok, detail = True, []
    for stream in LADDER_STREAMS:
        asked = time.time()
        _status, _headers, body, _seconds = fetch(f"/live/{stream}/stream.m3u8")
        mine = hls_spec.parse_media(body)
        reported = {item.get("URI"): item for item in mine["rendition_reports"]}
        part_s = mine["part_target"] or 0.2
        for other in LADDER_STREAMS:
            if other == stream:
                continue
            item = reported.get(f"../{other}/stream.m3u8")
            _status, _headers, text, _seconds = fetch(f"/live/{other}/stream.m3u8")
            real = hls_spec.last_position(hls_spec.parse_media(text))
            if item is None or real is None:
                reports_ok = False
                detail.append(f"{stream} has no report of {other}")
                continue
            behind = (real[0] - int(item["LAST-MSN"])) * 10 + real[1] - int(item["LAST-PART"])
            # The other playlist is read after the report, and it moves on
            # by one part for each part duration that passes in between.
            allowed = 2 + int((time.time() - asked) / part_s) + 1
            if not 0 <= behind <= allowed:
                reports_ok = False
                detail.append(f"{stream} reports {other} at {item['LAST-MSN']}.{item['LAST-PART']}, "
                              f"it is at {real[0]}.{real[1]}")
    ctx.check("each rendition reports the three others within two parts of their newest part",
              reports_ok, detail[:4])
    watched = hls_spec.watch(fetch, "/live/11/stream.m3u8", 6, True)
    ctx.check("the 1080p playlist breaks no MUST of the draft", not watched["fail"],
              "; ".join(f"#{item['rule']} {item['text'][:110]}" for item in watched["fail"][:4]))
    ctx.wait_readers(reader)
    ctx.expect_clean(reader, "the 1080p reader")
    time.sleep(22)
    held = lab.edge_view(edge)["playlists"]
    ctx.check("the edge holds no rendition 22 s after its reader has gone",
              not any(str(stream) in held for stream in LADDER_STREAMS), sorted(held))


@scenario("ladder-hlsjs-rate-cap", "ladder",
          "hls.js plays the ladder on a link of 8 Mbit/s, below the 16.5 Mbit/s of the 4K "
          "rendition, next to a player with a good link. Which rendition does each play?",
          "The capped player settles on 1080p and the other on 2160p. Neither stalls for "
          "more than 500 ms in all, and neither drops a frame.",
          needs=LADDER)
def ladder_hlsjs_rate_cap(ctx):
    import subprocess
    netem = lab.netem_spec(delay_ms=20, rate_kbit=8000)
    ctx.conditions["link of lon-readers-2, both directions"] = netem
    lab.set_tcp_per_packet(True, "london")
    lab.set_net("lon-readers-2", "pub", "both", netem)
    capped, capped_report = hlsjs(ctx, "capped", entry_url(VIDEO, "ladder-capped"), 60,
                                  host="lon-readers-2", wait=False)
    _free, free_report = hlsjs(ctx, "free", entry_url(VIDEO, "ladder-free"), 60)
    try:
        capped.wait(timeout=90)
    except subprocess.TimeoutExpired:
        capped.kill()
    for key, report, wanted in (("capped", capped_report, 1080), ("free", free_report, 2160)):
        ladder_player_checks(ctx, key, report, 500)
        timeline = level_timeline(report)
        ctx.check(f"{key}: the player is on {wanted}p from 20 s to the end",
                  height_at(timeline, 20_000) == wanted and height_at(timeline, 60_000) == wanted,
                  f"renditions over time: {timeline[:12]}")


@scenario("ladder-hlsjs-reorder", "ladder",
          "hls.js plays the ladder on a link that reorders a quarter of the packets by "
          "20 ms and duplicates 1%, where a 4K reader falls 20 s behind. What does the "
          "viewer see?",
          "The player settles on a rendition that the link carries and stalls for less "
          "than 1 s in all.",
          needs=LADDER)
def ladder_hlsjs_reorder(ctx):
    netem = lab.netem_spec(delay_ms=20, reorder=25, duplicate=1)
    ctx.conditions["link of lon-readers, both directions"] = netem
    lab.set_tcp_per_packet(True, "london")
    lab.set_net(LON_READERS, "pub", "both", netem)
    _process, report = hlsjs(ctx, "player", entry_url(VIDEO, "ladder-reorder"), 60)
    ladder_player_checks(ctx, "player", report, 1000)
    timeline = level_timeline(report)
    ctx.measure("rendition_at_the_end", height_at(timeline, 60_000))


@scenario("ladder-hlsjs-browser-cap", "ladder",
          "hls.js plays the ladder with the download rate of the browser limited to "
          "8 Mbit/s (DevTools network emulation, for a player on a network that the lab "
          "cannot shape). Which rendition does it play?",
          "The player settles on 1080p, stalls for less than 500 ms in all and drops no "
          "frame.",
          needs=LADDER)
def ladder_hlsjs_browser_cap(ctx):
    _process, report = hlsjs(ctx, "player", entry_url(VIDEO, "ladder-browser-cap"), 60,
                             extra=["--throttle", "0:8000"])
    ladder_player_checks(ctx, "player", report, 500)
    timeline = level_timeline(report)
    ctx.check("the player is on 1080p from 20 s to the end",
              height_at(timeline, 20_000) == 1080 and height_at(timeline, 60_000) == 1080,
              f"renditions over time: {timeline[:12]}")


@scenario("ladder-hlsjs-browser-steps", "ladder",
          "hls.js plays the ladder. The download rate of the browser drops to 3 Mbit/s "
          "after 20 s and is unlimited again after 45 s. Does the player follow?",
          "The player is on 2160p before the drop, on 720p or less within 15 s of it, and "
          "on 2160p again within 25 s of the recovery. It stalls for less than 2 s in all.",
          needs=LADDER)
def ladder_hlsjs_browser_steps(ctx):
    _process, report = hlsjs(ctx, "player", entry_url(VIDEO, "ladder-browser-steps"), 75,
                             extra=["--throttle", "0:0,20:3000,45:0"])
    ladder_player_checks(ctx, "player", report, 2000)
    timeline = level_timeline(report)
    before, low, after = (height_at(timeline, 19_500), height_at(timeline, 35_000),
                          height_at(timeline, 70_000))
    ctx.check("2160p before the drop", before == 2160, f"{before}p; {timeline[:14]}")
    ctx.check("720p or less within 15 s of the drop", low is not None and low <= 720,
              f"{low}p at 35 s")
    ctx.check("2160p again within 25 s of the recovery", after == 2160, f"{after}p at 70 s")


@scenario("ladder-hlsjs-rate-steps", "ladder",
          "hls.js plays the ladder. Its link drops from unlimited to 3 Mbit/s after 20 s "
          "and recovers after 45 s. Does the player follow, and what does the viewer see?",
          "The player is on 2160p before the drop, on 720p or less within 15 s of it, and "
          "on 2160p again within 25 s of the recovery. It stalls for less than 2 s in all.",
          needs=LADDER)
def ladder_hlsjs_rate_steps(ctx):
    import subprocess
    lab.set_tcp_per_packet(True, "london")
    process, report = hlsjs(ctx, "player", entry_url(VIDEO, "ladder-steps"), 75, wait=False)
    ctx.at(20)
    netem = lab.netem_spec(delay_ms=20, rate_kbit=3000)
    ctx.conditions["link of lon-readers from 20 s to 45 s"] = netem
    lab.set_net(LON_READERS, "pub", "both", netem)
    ctx.event("the link of the player is now 3 Mbit/s")
    ctx.at(45)
    lab.set_net(LON_READERS, "pub", "both", None)
    ctx.event("the link of the player is unlimited again")
    try:
        process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
    ladder_player_checks(ctx, "player", report, 2000)
    timeline = level_timeline(report)
    before, low, after = (height_at(timeline, 19_500), height_at(timeline, 35_000),
                          height_at(timeline, 70_000))
    ctx.check("2160p before the drop", before == 2160, f"{before}p; {timeline[:14]}")
    ctx.check("720p or less within 15 s of the drop", low is not None and low <= 720,
              f"{low}p at 35 s")
    ctx.check("2160p again within 25 s of the recovery", after == 2160, f"{after}p at 70 s")


@scenario("ladder-hlsjs-edge-kill", "ladder",
          "hls.js plays the ladder on an 8 Mbit/s link, and the cache that serves it is "
          "killed. Does the player move to another cache and keep its rendition?",
          "The player plays on with less than 1 s of stall in all and ends on 1080p from "
          "another cache.",
          needs=LADDER)
def ladder_hlsjs_edge_kill(ctx):
    import subprocess
    netem = lab.netem_spec(delay_ms=20, rate_kbit=8000)
    ctx.conditions["link of lon-readers, both directions"] = netem
    lab.set_tcp_per_packet(True, "london")
    lab.set_net(LON_READERS, "pub", "both", netem)
    process, report = hlsjs(ctx, "player", entry_url(VIDEO, "ladder-kill"), 60, wait=False)
    ctx.at(25)
    counts = {name: sum(item["sessions"] for stream, item in
                        lab.edge_view(name)["playlists"].items()
                        if int(stream) in LADDER_STREAMS) for name in lab.edges("london")}
    victim = max(counts, key=counts.get)
    lab.kill_service(victim)
    ctx.event(f"kill -9 {victim}; ladder sessions {counts}")
    ctx.defer(lambda: lab.start_service(victim))
    try:
        process.wait(timeout=100)
    except subprocess.TimeoutExpired:
        process.kill()
    summary = ladder_player_checks(ctx, "player", report, 1000)
    timeline = level_timeline(report)
    hosts = summary.get("hosts") or []
    dead = lab.node(victim)["public_ipv4"]
    ctx.check("the media came from another cache after the kill",
              any(when > 20_000 and dead not in str(host) for when, host in hosts),
              f"hosts over time: {hosts}")
    ctx.check("the player ends on 1080p", height_at(timeline, 60_000) == 1080,
              f"renditions over time: {timeline[:14]}")


@scenario("endurance-churn", "endurance",
          "Readers of 4K video, lossless audio and HQ audio come and go for hours, so that "
          "every edge takes and releases copies again and again. Does any node keep "
          "memory, descriptors, sockets or threads?",
          "After the first tenth of the run, no node grows by more than 20 MB an hour, and "
          "no node gains descriptors or threads. Every reader of every cycle is served "
          "without a missing part.",
          slow=True)
def endurance_churn(ctx):
    import os
    seconds = int(os.environ.get("NEEDLETAIL_ENDURANCE_SECONDS", "10800"))
    ctx.conditions["seconds"] = seconds
    ctx.conditions["cycle"] = ("12 s of readers (2 x 4K, 4 x FLAC, 4 x Opus in London; "
                               "1 x 4K, 2 x FLAC in New York), then 12 s without readers, "
                               "which is longer than the 8 s after which an edge releases a "
                               "copy")
    samples = []
    cycles = 0
    bad_cycles = []
    released = 0
    started = time.time()
    names = lab.names()

    def take(cycle):
        row = {"cycle": cycle, "at_s": round(time.time() - started, 1), "nodes": {}}
        for name in names:
            directory = lab.cgroup_dir(name)
            pid = lab.service_pid(name)
            if directory is None or pid is None:
                continue
            group = lab.read_cgroup(directory)
            process = lab.read_process(pid)
            row["nodes"][name] = {
                "memory_mb": round(group.get("memory_bytes", 0) / 1e6, 1),
                "anon_mb": round(group.get("memory_anon_bytes", 0) / 1e6, 1),
                "fds": process.get("fds"), "sockets": process.get("sockets"),
                "threads": process.get("threads"),
            }
        samples.append(row)

    while time.time() - started < seconds:
        cycles += 1
        jobs = [
            ctx.readers(f"c{cycles}-video", VIDEO, 2, 12),
            ctx.readers(f"c{cycles}-flac", AUDIO, 4, 12),
            ctx.readers(f"c{cycles}-opus", HQ, 4, 12),
            ctx.readers(f"c{cycles}-video-ny", VIDEO, 1, 12, host=NEW_READERS),
            ctx.readers(f"c{cycles}-flac-ny", AUDIO, 2, 12, host=NEW_READERS),
        ]
        ctx.wait_readers(*jobs)
        for job in jobs:
            summary = job.summary
            if summary.get("readers_ok") != job.count or summary.get("parts_missing", 1) != 0:
                bad_cycles.append({"cycle": cycles, "job": job.key,
                                   "readers_ok": summary.get("readers_ok"),
                                   "parts_missing": summary.get("parts_missing")})
            # Keep the evidence of the run small: the reports of a cycle go.
            job.report_path.unlink(missing_ok=True)
            (ctx.directory / f"{job.key}.reader.log").unlink(missing_ok=True)
            ctx.jobs.pop(job.key, None)
        time.sleep(12)
        take(cycles)
        if cycles % 20 == 0:
            (ctx.directory / "endurance-samples.json").write_text(json.dumps(samples))
            print(f"    cycle {cycles}: " + ", ".join(
                f"{name} {item['memory_mb']:.0f} MB" for name, item in
                samples[-1]["nodes"].items() if "edge" in name or "contrib" in name),
                flush=True)
    (ctx.directory / "endurance-samples.json").write_text(json.dumps(samples))
    for name in lab.edges() + lab.edges("newyork"):
        try:
            released += lab.mesh_local(name)["cache_replication"]["subscriptions_released"]
        except lab.LabError:
            pass
    ctx.measure("cycles", cycles)
    ctx.measure("copies_released_by_edges", released)
    ctx.measure("hours", round((time.time() - started) / 3600, 2))
    settled = [row for row in samples if row["cycle"] > cycles / 10]
    growth = {}
    for name in names:
        rows = [(row["at_s"] / 3600, row["nodes"][name]) for row in settled
                if name in row["nodes"]]
        if len(rows) < 4:
            continue
        growth[name] = {
            "memory_mb_per_hour": round(slope([(x, item["memory_mb"]) for x, item in rows]), 2),
            "anon_mb_per_hour": round(slope([(x, item["anon_mb"]) for x, item in rows]), 2),
            "memory_mb_first": rows[0][1]["memory_mb"],
            "memory_mb_last": rows[-1][1]["memory_mb"],
            "memory_mb_max": max(item["memory_mb"] for _, item in rows),
            "fds_first": rows[0][1]["fds"], "fds_last": rows[-1][1]["fds"],
            "fds_max": max(item["fds"] or 0 for _, item in rows),
            "threads_first": rows[0][1]["threads"], "threads_last": rows[-1][1]["threads"],
        }
    ctx.measure("growth", growth)
    ctx.check("every reader of every cycle was served without a missing part",
              not bad_cycles, f"{len(bad_cycles)} of {cycles * 5} reader groups: "
              f"{bad_cycles[:4]}")
    ctx.check("the edges released their copies in each cycle",
              released >= cycles * 4, f"{released} copies released in {cycles} cycles")
    worst = max(growth.items(), key=lambda item: item[1]["anon_mb_per_hour"], default=None)
    ctx.check("no node grows by more than 20 MB an hour after the first tenth of the run",
              all(item["anon_mb_per_hour"] <= 20 for item in growth.values()),
              {name: item["anon_mb_per_hour"] for name, item in growth.items()})
    ctx.check("no node gains file descriptors",
              all((item["fds_last"] or 0) <= (item["fds_first"] or 0) + 8
                  for item in growth.values()),
              {name: (item["fds_first"], item["fds_last"], item["fds_max"])
               for name, item in growth.items()})
    ctx.check("no node gains threads",
              all((item["threads_last"] or 0) <= (item["threads_first"] or 0) + 2
                  for item in growth.values()),
              {name: (item["threads_first"], item["threads_last"])
               for name, item in growth.items()})
    if worst:
        ctx.note(f"largest growth: {worst[0]} at {worst[1]['anon_mb_per_hour']} MB an hour")


# ============================================================ scale operations
# Failover, redistribution and autoscaling with UDP listeners
# (low-latency ops item 7, judged as the ops design's scenarios F1, F2,
# R1-R5, A1 and A2 as far as one host can carry them): needletail-load
# crowds (the real client session per listener) on a reader host, placed and
# moved through /api/assignment (Phase AM item 10) at the site's
# distributors, which no edge scenario faults (the scale run's entry points
# are never under fault). Local proofs of the deploy's slots (a join
# restarts nothing), of the scale run's faults, and of the autoscaler's
# edge_lab backend through a ramp up and down.

CLIENT_UDP_PORT = 19446
# The crowds listen to the DAW lane's 1 ms Opus Custom at 320 kb/s (HQ): the
# load of latency-bench and of the scale run's opus1 steps, ~0.78 Mbit/s and
# ~1,031 datagrams a second a listener.
OPS_STREAM = HQ
OPS_PART_MS = 1.0
# Survivors: CPU under 80% of a g6-dedicated-2's two CPUs (% of one CPU).
OPS_CPU_LIMIT = 160.0
# The lab of the join and autoscale scenarios: the slot lon-edge-4, an edge
# load that counts client datagrams (a full CPU reads as a full edge, as the
# scale run's autoscale phases need), and the scale run's warm-up of a new
# copy in ms (cache_mesh_warm_ms 4,500; the lab's default is the whole
# 12,000-part window).
SCALE_NEEDS = ["slots=lon-edge-4", "client_udp_datagram_capacity=40000",
               "cache_mesh_warm_ms=4500"]
UDP_COUNTERS = ("subscribers", "subscriptions", "refusals", "moves", "would_block",
                "send_queue_drops", "playout_skipped_parts", "nack_parts", "nack_refused")


class UdpJob:
    """One needletail-load process: `count` UDP listeners of a stream over
    a list of edges, its interval and summary files in `out`."""

    def __init__(self, key, host, out, process, started_at, seconds):
        self.key, self.host, self.out = key, host, out
        self.process, self.started_at, self.seconds = process, started_at, seconds

    def wait(self, grace: float = 30.0) -> None:
        try:
            self.process.wait(timeout=max(1.0, self.started_at + self.seconds + grace
                                          - time.time()))
        except subprocess.TimeoutExpired:
            self.process.kill()

    def summary(self) -> dict:
        try:
            return json.loads((self.out / "summary.json").read_text())
        except (OSError, ValueError):
            return {}

    def intervals(self) -> list[dict]:
        rows = []
        try:
            for line in (self.out / "intervals.jsonl").read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
        return rows


def udp_edges(site: str = "london") -> list[str]:
    return [f"{lab.node(name)['public_ipv4']}:{CLIENT_UDP_PORT}" for name in lab.edges(site)]


def udp_entries(site: str = "london") -> list[str]:
    """needletail-load's entry points (Phase AM item 10): the site's
    distributors in service."""
    return [lab.base_url(name) for name in lab.distributors(site)]


def udp_command(host: str, edges: list[str], stream: int, count: int, seconds: int,
                out: pathlib.Path, label: str, ramp: str | None = None,
                entries: list[str] | None = None) -> list[str]:
    """needletail-load in a reader host's namespace, as scale-run starts it
    on a listener host. With `entries` each listener asks /api/assignment
    where to subscribe and where to move (`edges` is then the fallback)."""
    command = lab.enter(host) + [
        lab.binary("needletail-load"), "--edges", ",".join(edges), "--stream", str(stream),
        "--listeners", str(count), "--ramp", ramp or f"{count}@0", "--duration", str(seconds),
        "--sample", str(min(5, count)), "--out", str(out), "--label", label, "--quiet"]
    if entries:
        command += ["--entry", ",".join(entries), "--tls-ca", lab.ca_file(),
                    "--server-name", lab.SERVER_NAME]
    # An A/B of the client session's flags (needletail-load options).
    return command + os.environ.get("NEEDLETAIL_LOAD_EXTRA_ARGS", "").split()


def udp_listeners(ctx, key: str, count: int, seconds: int, host: str = LON_READERS,
                  stream: int = OPS_STREAM, edges: list[str] | None = None,
                  ramp: str | None = None, assign: bool = True) -> UdpJob:
    if not pathlib.Path(lab.binary("needletail-load")).is_file():
        raise lab.LabError("needletail-load is not built (av-mesh low-latency: "
                           "cargo build --release --bin needletail-load)")
    out = ctx.directory / f"{key}.udp"
    site = lab.node(host)["site"]
    command = udp_command(host, edges or udp_edges(site), stream, count, seconds, out,
                          f"{ctx.scenario.name}-{key}", ramp,
                          udp_entries(site) if assign else None)
    log = open(ctx.directory / f"{key}.udp.log", "ab")
    process = subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                               start_new_session=True)
    ctx.event(f"{count} UDP listeners of stream {stream} start on {host} ({key}"
              + (", through /api/assignment" if assign else ", static edge list") + ")")
    return UdpJob(key, host, out, process, time.time(), seconds)


def edge_name_map() -> dict[str, str]:
    return {f"{lab.node(name)['public_ipv4']}:{CLIENT_UDP_PORT}": name for name, *_ in lab.NODES}


def quantiles(block) -> dict | None:
    if not isinstance(block, dict) or not block.get("count"):
        return None
    return {key: block.get(key) for key in ("count", "p50", "p99", "max")}


def udp_report(ctx, job: UdpJob, edge_names: dict[str, str] | None = None,
               faulted=(), since: float | None = None) -> dict:
    """What a UDP crowd saw (needletail-load's own books): parts and missing
    parts; moves per edge (`moves` on the edge moved to, `moves_out` on the
    edge left, by reason), the time to move (from a listener's last part on
    the old edge to its first on the new one) p50/p99/max; the parts lost in
    moves away from each edge (`left_lost`): those off a faulted edge are the
    fault's, every other missing part is an unaffected listener's; listeners
    stranded (no part for 5 s, needletail-load's gauge, the largest of any
    interval and at the end), never served, never resumed after a move.
    With `since` (Unix time: the end of the start-up, before any fault),
    the missing parts are those that settled from then on, as the scale
    run's before/during/after windows judge them; the start-up's are
    reported apart (`startup_missing`)."""
    summary = job.summary()
    total = summary.get("total") or summary
    per_edge = summary.get("per_edge") or summary.get("edges") or {}
    if isinstance(per_edge, list):
        per_edge = {item.get("edge"): item for item in per_edge if isinstance(item, dict)}
    names = edge_names or edge_name_map()

    def by_edge(key):
        return {names.get(edge, edge): (block or {}).get(key, 0) or 0
                for edge, block in per_edge.items() if (block or {}).get(key)}

    moves = {names.get(edge, edge): (block or {}).get("moves", 0)
             for edge, block in per_edge.items() if edge != "unassigned"}
    # Wave 9 review: the moves into each edge by the reason the listener
    # left its last edge (needletail-load's moves_in_by_reason); a fault's
    # spread is judged on the fault's own moves, not on the rebalances
    # that followed them. None from a load tool without the field.
    moves_in = {names.get(edge, edge): (block or {}).get("moves_in_by_reason")
                for edge, block in per_edge.items() if edge != "unassigned"}
    fault_moves = None
    if moves and all(isinstance(value, dict) for value in moves_in.values()):
        fault_moves = {name: sum(count for reason, count in value.items()
                                 if reason != "rebalance")
                       for name, value in moves_in.items()}
    rows = [row for row in job.intervals() if row.get("phase") != "final"]
    last = (rows[-1].get("total") or {}) if rows else {}
    stranded = None
    if last.get("active") is not None:
        stranded = max(0, last["active"] - (last.get("up") or 0))
    if last.get("stranded") is not None:
        stranded = last["stranded"]
    known = [(row.get("total") or {}).get("stranded") for row in rows]
    known = [value for value in known if isinstance(value, int)]
    left_lost = by_edge("left_lost")
    missing = total.get("missing")
    startup_missing = None
    if since is not None and missing is not None:
        startup_missing = sum((row.get("total") or {}).get("missing") or 0 for row in rows
                              if row.get("unix_ms", 0) < since * 1000)
        missing -= startup_missing
    fault_lost = sum(left_lost.get(name, 0) for name in faulted)
    report = {"parts": total.get("parts"), "missing": missing,
              "startup_missing": startup_missing,
              "moves_by_edge": moves, "moves": sum(moves.values()),
              "moves_in_by_reason_by_edge": moves_in,
              "fault_moves_by_edge": fault_moves,
              "moves_out_by_edge": by_edge("moves_out"),
              "moves_out_reasons_by_edge": {
                  names.get(edge, edge): (block or {}).get("moves_by_reason") or {}
                  for edge, block in per_edge.items() if edge != "unassigned"},
              "moves_by_reason": total.get("moves_by_reason") or {},
              "time_to_move_ms": quantiles(total.get("time_to_move_ms")),
              "left_lost_by_edge": left_lost, "move_lost": total.get("move_lost"),
              "fault_lost": fault_lost,
              "unaffected_missing": None if missing is None else missing - fault_lost,
              "missing_by_edge": by_edge("missing"),
              "never_first": total.get("never_first", summary.get("listeners_without_parts")),
              "never_resumed": total.get("never_resumed", summary.get("never_resumed")),
              "stranded_max": max(known) if known else None, "stranded_at_end": stranded,
              "latency_ms": quantiles(total.get("latency_ms")),
              "ttfp_ms": quantiles(total.get("ttfp_ms")),
              "assignment_errors": total.get("assignment_errors"),
              "fallbacks": total.get("fallbacks"),
              "load_host_saturated": summary.get("load_host_saturated")}
    ctx.measure(f"{job.key}_udp", report)
    return report


def move_spread(moves: dict[str, int], edges: list[str]) -> float | None:
    """The old (wave 8) spread figure: the most moves into one edge over
    the mean, as if every survivor should take an equal share."""
    counts = [moves.get(edge, 0) for edge in edges]
    if not counts or not sum(counts):
        return None
    return round(max(counts) / (sum(counts) / len(counts)), 3)


# Wave 9 C4: each survivor's share of the moves within this factor of its
# headroom-weighted share, and the listeners per survivor after the moves
# within this of their mean.
MOVE_SHARE_LIMIT = 1.3
BALANCE_AFTER_LIMIT = 1.15


# The rebalance target (av-mesh --rebalance-target-permille): movers fill the
# survivors up to it (wave 9 review).
MOVE_LEVEL_PERMILLE = 800


def fill_level(loads: list[float], amount: float) -> float:
    """The level at which `amount` permille poured over loads `loads` fills
    them evenly (av-mesh assignment::fill_level)."""
    below = 0.0
    ordered = sorted(loads)
    for index, load in enumerate(ordered):
        below += load
        level = (amount + below) / (index + 1)
        if index + 1 == len(ordered) or level <= ordered[index + 1]:
            return level
    return amount


def listener_cost(loads: dict, subscribers: dict) -> float | None:
    """What one UDP listener costs an edge (permille), as av-mesh reckons
    it: the median of load over subscribers among the edges with >= 4."""
    costs = sorted(loads[name] / subscribers[name] for name in loads
                   if isinstance(loads.get(name), (int, float)) and loads[name] > 0
                   and isinstance(subscribers.get(name), int) and subscribers[name] >= 4)
    return costs[len(costs) // 2] if costs else None


def headroom_shares(moves: dict[str, int], loads: dict[str, int | None],
                    cost: float | None = None,
                    level: int = MOVE_LEVEL_PERMILLE) -> dict | None:
    """Wave 9 C4: each survivor's moves against its headroom-weighted share:
    `ratio` = moves / (all moves x headroom / all headroom). Since the wave
    9 review the headroom is to the level the movers fill the survivors to,
    as av-mesh apportions them: the rebalance target, raised to where all
    the moves at `cost` permille each fit when they do not under it; the
    headroom to 1,000 (at least 1) when no survivor is under the level.
    (To 1,000 alone, a survivor at 776 permille by its LL-HLS readers' CPU
    was due a share that it then shed again as over its target.) None
    without moves or loads."""
    total = sum(moves.get(name, 0) for name in loads)
    known = {name: load for name, load in loads.items() if isinstance(load, (int, float))}
    if not total or not known or len(known) != len(loads):
        return None
    at = float(level)
    if cost:
        at = max(at, fill_level(list(known.values()), total * cost))
    headroom = {name: max(0.0, at - float(load)) for name, load in known.items()}
    if sum(headroom.values()) < 1:
        headroom = {name: max(1.0, 1000.0 - float(load)) for name, load in known.items()}
    whole = sum(headroom.values())
    out = {}
    for name, room in headroom.items():
        expected = total * room / whole
        got = moves.get(name, 0)
        out[name] = {"load_permille": known[name], "headroom": round(room, 1),
                     "level": round(at, 1), "expected": round(expected, 2), "moves": got,
                     "ratio": (round(got / expected, 3) if expected
                               else (None if got == 0 else float("inf")))}
    return out


def headroom_shares_to_full(moves: dict[str, int], loads: dict[str, int | None]) -> dict | None:
    """The wave 9 C4 figure before the review (headroom to 1,000), kept in
    the record."""
    return headroom_shares(moves, loads, level=1000)


def headroom_share_worst(shares: dict | None) -> float | None:
    """The worst survivor's factor off its headroom-weighted share, either
    way (1.25 = 25% over or 1/1.25 under)."""
    if not shares:
        return None
    worst = 1.0
    for item in shares.values():
        ratio = item["ratio"]
        if ratio is None:
            continue
        worst = max(worst, (1 / ratio) if ratio > 0 else float("inf"), ratio)
    return round(worst, 3) if worst != float("inf") else worst


def edge_p99(job: UdpJob, edges: list[str], begin: float, end: float) -> float | None:
    """The listeners' latency p99 on `edges` between two Unix times: each
    interval's p99 per edge, weighted by its parts (needletail-load keeps
    quantiles per interval, not their histogram)."""
    names = edge_name_map()
    weighted, count = 0.0, 0
    for row in job.intervals():
        if row.get("phase") == "final" or not begin * 1000 <= row.get("unix_ms", 0) < end * 1000:
            continue
        for edge, block in (row.get("edges") or {}).items():
            latency = (block or {}).get("latency_ms") or {}
            if names.get(edge, edge) in edges and latency.get("count") and \
                    latency.get("p99") is not None:
                weighted += latency["p99"] * latency["count"]
                count += latency["count"]
    return round(weighted / count, 3) if count else None


def first_parts(job: UdpJob, begin: float, end: float) -> dict[str, int]:
    """Listeners that had their first part on each edge between two Unix
    times (arrivals, not moves)."""
    names = edge_name_map()
    out: dict[str, int] = {}
    for row in job.intervals():
        if row.get("phase") == "final" or not begin * 1000 <= row.get("unix_ms", 0) < end * 1000:
            continue
        for edge, block in (row.get("edges") or {}).items():
            value = (block or {}).get("first_parts") or 0
            if value:
                out[names.get(edge, edge)] = out.get(names.get(edge, edge), 0) + value
    return out


def join_warm_up_s(pending: list[tuple[float, int]], joined: float) -> float:
    """Seconds from a join to the end of its warm-up: the first sample at
    0 after the first run of samples with warm-up parts pending (a later
    run is the warm-up of another playlist's copy, taken when its first
    listener came, not the join's)."""
    started = False
    for t, value in pending:
        if value > 0:
            started = True
        elif started:
            return round(t - joined, 1)
    return 0.0 if not started else round(pending[-1][0] - joined, 1)


class OpsSampler:
    """Once a second, every London edge and distributor in service: its
    client UDP gauges and counters, its own advertisement (load_permille,
    fresh, draining, free_bps), the cache mesh's warm-up counters and
    children, replication and hole-repair counters, the fast plane's lease
    children. Rows in <scenario>/ops.ndjson."""

    def __init__(self, ctx, stream: int = OPS_STREAM, site: str = "london",
                 interval: float = 1.0):
        self.path = ctx.directory / "ops.ndjson"
        self.stream, self.site = stream, site
        self.rows: list[dict] = []
        self.thread = core.Background(self.take, interval)

    def start(self) -> "OpsSampler":
        self.thread.start()
        return self

    def stop(self) -> list[dict]:
        self.thread.stop()
        return self.rows

    def take(self) -> None:
        row = {"t": round(time.time(), 3), "nodes": {}}
        for name in lab.names("edge", self.site) + lab.names("distributor", self.site):
            if lab.service_pid(name) is None:
                continue
            item: dict = {}
            try:
                status, local = lab.http_json(name, "/api/mesh/local", timeout=0.8)
            except Exception:  # noqa: BLE001 - a measurement only
                status, local = None, None
            if status == 200 and isinstance(local, dict):
                item["gauges"] = local.get("client_udp") or {}
                item["cache_mesh"] = {key: value for key, value in
                                      (local.get("cache_mesh") or {}).items()
                                      if key != "draining_peers"}
                replication = local.get("cache_replication") or {}
                item["replication"] = {key: replication.get(key) for key in (
                    "subscriptions_opened", "subscriptions_released", "subscriptions_refused")}
                item["hole_repair_requests"] = local.get("edge_hole_repair_requests")
                item["fast_plane"] = local.get("fast_plane")
                fec = local.get("mesh_fec") or {}
                item["mesh_fec"] = {key: fec.get(key) for key in (
                    "rx_presumed_lost", "rx_expired_objects", "rx_evicted_objects")}
            if lab.node(name)["role"] == "edge":
                try:
                    status, body = lab.http_json(name, f"/api/assignment?stream={self.stream}",
                                                 timeout=0.8)
                except Exception:  # noqa: BLE001
                    status, body = None, None
                if status == 200 and isinstance(body, dict):
                    udp = body.get("client_udp") or {}
                    item["udp"] = {key: udp.get(key) for key in UDP_COUNTERS}
                    own = next((cache for cache in body.get("caches") or []
                                if cache.get("node_id") == name), {})
                    item["advert"] = {key: own.get(key) for key in (
                        "fresh", "draining", "overloaded", "load_permille", "free_bps")}
            row["nodes"][name] = item
        self.rows.append(row)
        with open(self.path, "a") as handle:
            handle.write(json.dumps(row) + "\n")


def ops_value(row: dict, name: str, path: str):
    value = row.get("nodes", {}).get(name)
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def ops_delta(rows: list[dict], names, path: str, begin: float, end: float) -> int | None:
    """A counter's rise between two Unix times, summed over `names` (the
    last sample before `begin`, or the first after it, to the last sample
    before `end`)."""
    total, seen = 0, False
    for name in names:
        values = [(row["t"], ops_value(row, name, path)) for row in rows]
        values = [(t, value) for t, value in values if value is not None]
        before = [value for t, value in values if t <= begin] or \
                 [value for t, value in values if t > begin][:1]
        after = [value for t, value in values if t <= end]
        if before and after:
            total += max(0, after[-1] - before[-1])
            seen = True
    return total if seen else None


def ops_max(rows: list[dict], names, path: str, begin: float, end: float):
    values = [ops_value(row, name, path) for row in rows if begin <= row["t"] <= end
              for name in names]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def cpu_percent(ctx, names, begin: float, end: float) -> dict[str, dict]:
    """Each node's CPU (% of one CPU) from the scenario's node sampler, per
    second between two Unix times: mean, p95 and max."""
    path = ctx.directory / "nodes.ndjson"
    series: dict[str, list[tuple[float, int]]] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            sample = json.loads(line)
        except ValueError:
            continue
        for name in names:
            item = sample.get(name) or {}
            if item.get("running") and "usage_usec" in item:
                series.setdefault(name, []).append((item["at"], item["usage_usec"]))
    out = {}
    for name, points in series.items():
        cpu = sorted((b[1] - a[1]) / 1e4 / (b[0] - a[0])
                     for a, b in zip(points, points[1:])
                     if begin <= b[0] <= end and b[0] > a[0] and b[1] >= a[1])
        if cpu:
            out[name] = {"mean": round(sum(cpu) / len(cpu), 1),
                         "p95": round(cpu[max(0, int(len(cpu) * 0.95) - 1)], 1),
                         "max": round(cpu[-1], 1)}
    return out


class GaugeWatch(threading.Thread):
    """A drained edge's live UDP-subscriber gauge every 100 ms: how many it
    had when the drain was sent, when the first left and when it read 0."""

    def __init__(self, name: str, seconds: float = 20.0):
        super().__init__(daemon=True)
        self.name, self.seconds = name, seconds
        self.samples: list[tuple[float, int | None]] = []
        self.started = time.time()

    def run(self) -> None:
        while time.time() - self.started < self.seconds:
            try:
                status, body = lab.http_json(self.name, "/api/mesh/local", timeout=0.5)
                value = ((body or {}).get("client_udp") or {}).get("subscribers") \
                    if status == 200 and isinstance(body, dict) else None
            except Exception:  # noqa: BLE001
                value = None
            self.samples.append((time.time(), value))
            if value == 0 and len(self.samples) > 1:
                break
            time.sleep(0.1)

    def result(self) -> dict:
        known = [(t, value) for t, value in self.samples if isinstance(value, int)]
        first = known[0][1] if known else None
        left = next((t for t, value in known if first is not None and value < first), None)
        zero = next((t for t, value in known if value == 0), None)
        return {"node": self.name, "subscribers_at_drain": first,
                "first_left_ms": None if left is None else round((left - self.started) * 1000),
                "zero_ms": None if zero is None else round((zero - self.started) * 1000)}


def service_pids() -> dict[str, int | None]:
    return {name: lab.service_pid(name) for name in lab.standing_services()}


def drain(name: str) -> None:
    status, _ = lab.http_json(name, "/api/control/drain-node", "POST", {"node_id": name})
    # av-mesh answers a control command with 202 Accepted (queued).
    if not 200 <= status < 300:
        raise lab.LabError(f"drain {name}: HTTP {status}")


def resume(name: str) -> None:
    lab.http_json(name, "/api/control/resume-node", "POST", {"node_id": name})


def survivor_checks(ctx, rows: list[dict], survivors: list[str], begin: float, end: float,
                    before: tuple[float, float], refusals_expected: bool = False) -> dict:
    """The survivors' criteria of every scenario: CPU under 80% of two CPUs
    (p95 of 1 s samples; the largest second reported), and no rise in
    would_block, send-queue drops or refusals on the edges that stay."""
    distributors = [name for name in lab.distributors("london")]
    cpu = cpu_percent(ctx, survivors + distributors, begin, end)
    ctx.measure("survivors_cpu_percent", cpu)
    worst = max((item["p95"] for item in cpu.values()), default=None)
    ctx.check(f"survivors: CPU p95 under {OPS_CPU_LIMIT:.0f}% of one CPU (80% of 2)",
              worst is not None and worst < OPS_CPU_LIMIT,
              {name: (item["p95"], item["max"]) for name, item in cpu.items()})
    rises = {}
    span = max(1e-9, end - begin)
    base_span = max(1e-9, before[1] - before[0])
    for counter in ("would_block", "send_queue_drops", "refusals"):
        during = ops_delta(rows, survivors, f"udp.{counter}", begin, end)
        base = ops_delta(rows, survivors, f"udp.{counter}", *before)
        if during is None:
            continue
        rises[counter] = {"before_per_s": round((base or 0) / base_span, 3),
                          "during_per_s": round(during / span, 3), "during": during}
    ctx.measure("survivors_udp_counters", rises)
    for counter, item in rises.items():
        if counter == "refusals" and refusals_expected:
            continue
        ctx.check(f"survivors: no rise in {counter}",
                  item["during_per_s"] <= item["before_per_s"], item)
    return {"cpu": cpu, "rises": rises}


def listener_checks(ctx, report: dict, moved_from=()) -> None:
    ctx.check("UDP listeners: none stranded (needletail-load's 5 s gauge, every interval)",
              report["stranded_max"] in (0, None) and report["stranded_at_end"] in (0, None),
              {"max": report["stranded_max"], "at_end": report["stranded_at_end"]})
    ctx.check("UDP listeners: every one had a first part and every mover resumed",
              not report["never_first"] and not report["never_resumed"],
              {"never_first": report["never_first"], "never_resumed": report["never_resumed"]})
    ctx.check("unaffected UDP listeners: 0 missing (missing less the parts lost in moves "
              "off the faulted edges)", report["unaffected_missing"] == 0,
              {"missing": report["missing"], "fault_lost": report["fault_lost"],
               "left_lost_by_edge": report["left_lost_by_edge"]})


def udp_fault(ctx, fault, label: str, targets: list[str], count: int = 160,
              seconds: int = 70, fault_at: int = 25, heal=None, heal_at: int = 55,
              drained: bool = False) -> dict:
    """`count` UDP listeners (1 ms Opus, through /api/assignment) and 4
    steered LL-HLS readers; the fault on every target at once at `fault_at`;
    the heal at `heal_at`. Judged against the ops design's F1 (drain) or F2
    (kill) and R2/R3, with the survivors' criteria."""
    ops = OpsSampler(ctx).start()
    udp = udp_listeners(ctx, "udp", count, seconds)
    hls = ctx.readers("audio", AUDIO, 4, seconds, steering=True)
    ctx.at(fault_at)
    fault_unix = time.time()
    watches = [GaugeWatch(target) for target in targets] if drained else []
    for watch in watches:
        watch.start()
    threads = [threading.Thread(target=fault, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ctx.event(f"{label}: {', '.join(targets)}")
    heal_unix = None
    if heal:
        ctx.at(heal_at)
        heal_unix = time.time()
        for target in targets:
            heal(target)
        ctx.event(f"healed: {', '.join(targets)}")
    udp.wait()
    ctx.wait_readers()
    for watch in watches:
        watch.join(timeout=1)
    rows = ops.stop()
    end_unix = heal_unix or time.time()
    report = udp_report(ctx, udp, faulted=targets, since=ctx.t0 + 10)
    others = [edge for edge in lab.edges() if edge not in targets]
    spread = move_spread(report["moves_by_edge"], others)
    # The old equal-spread figure stays in the record (wave 9 C4).
    ctx.measure("moves_spread_max_over_mean", spread)
    # Each survivor's load just before the fault (its own gauge, which
    # counts its CPU, datagrams and bytes): its headroom-weighted share.
    before_fault = next((row for row in reversed(rows) if row["t"] <= fault_unix), None)
    loads = {name: (ops_value(before_fault, name, "gauges.load_permille")
                    if before_fault else None) for name in others}
    # What a listener costs, from every edge's load and subscribers before
    # the fault (the faulted ones too), as av-mesh reckons it.
    all_loads = {name: (ops_value(before_fault, name, "gauges.load_permille")
                        if before_fault else None) for name in lab.edges()}
    all_subs = {name: (ops_value(before_fault, name, "gauges.subscribers")
                       if before_fault else None) for name in lab.edges()}
    cost = listener_cost(all_loads, all_subs)
    # Wave 9 review: the fault's own moves (not the rebalances after them)
    # against the headroom to the level; moves of every reason and the
    # headroom to 1,000 stay in the record.
    judged = report.get("fault_moves_by_edge") or report["moves_by_edge"]
    shares = headroom_shares(judged, loads, cost)
    share_worst = headroom_share_worst(shares)
    ctx.measure("listener_cost_permille", round(cost, 2) if cost else None)
    ctx.measure("moves_judged", "fault reasons" if report.get("fault_moves_by_edge")
                else "every reason (load tool without moves_in_by_reason)")
    ctx.measure("moves_by_headroom", shares)
    ctx.measure("moves_headroom_share_worst", share_worst)
    full = headroom_shares_to_full(report["moves_by_edge"], loads)
    ctx.measure("moves_by_headroom_to_full_every_reason", full)
    ctx.measure("moves_headroom_to_full_share_worst", headroom_share_worst(full))
    # Net arrivals per survivor: the fault's moves in, the rebalances in
    # and out.
    reasons_in = report.get("moves_in_by_reason_by_edge") or {}
    ctx.measure("net_arrivals_per_survivor", {
        name: {"fault_in": (report.get("fault_moves_by_edge") or {}).get(name),
               "rebalance_in": (reasons_in.get(name) or {}).get("rebalance", 0),
               "rebalance_out": ((report.get("moves_out_reasons_by_edge") or {})
                                 .get(name) or {}).get("rebalance", 0)}
        for name in others})
    balance = None
    late = next((row for row in reversed(rows) if row["t"] <= (heal_unix or time.time()) - 1),
                None)
    if late:
        held = {name: ops_value(late, name, "gauges.subscribers") for name in others}
        known = [value for value in held.values() if isinstance(value, int)]
        balance = (round(max(known) / (sum(known) / len(known)), 3)
                   if known and sum(known) else None)
        ctx.measure("listeners_per_survivor_after", held)
        ctx.measure("listeners_per_survivor_max_over_mean", balance)
        # Their loads too: an edge whose load is its LL-HLS readers' CPU
        # takes fewer listeners by headroom (wave 9 C4).
        loads_after = {name: ops_value(late, name, "gauges.load_permille") for name in others}
        known_loads = [value for value in loads_after.values() if isinstance(value, (int, float))]
        ctx.measure("load_per_survivor_after", loads_after)
        ctx.measure("load_per_survivor_max_over_mean",
                    round(max(known_loads) / (sum(known_loads) / len(known_loads)), 3)
                    if known_loads and sum(known_loads) else None)
    if watches:
        ctx.measure("drain_gauges", [watch.result() for watch in watches])
    listener_checks(ctx, report)
    moved_out = sum(report["moves_out_by_edge"].get(target, 0) for target in targets)
    ctx.check("UDP listeners moved off the faulted edges", moved_out > 0,
              report["moves_out_by_edge"])
    ctx.check(f"moves spread over the other edges by headroom (each survivor's share within "
              f"{MOVE_SHARE_LIMIT}x of its headroom-weighted share)",
              share_worst is not None and share_worst <= MOVE_SHARE_LIMIT,
              {"worst": share_worst, "shares": shares, "equal_spread_max_over_mean": spread})
    ctx.check(f"listeners per survivor after the moves: max/mean <= {BALANCE_AFTER_LIMIT}",
              balance is not None and balance <= BALANCE_AFTER_LIMIT,
              {"max_over_mean": balance})
    ttm = report["time_to_move_ms"] or {}
    if drained:
        ctx.check("F1: 0 parts missing (every listener, the moved ones included)",
                  report["missing"] == 0, report["missing"])
        ctx.check("F1: time to move p99 <= 300 ms", (ttm.get("p99") or 1e9) <= 300, ttm)
        for watch in watches:
            result = watch.result()
            ctx.check(f"{watch.name}: its UDP subscriber gauge reached 0 after the drain",
                      result["zero_ms"] is not None, result)
    else:
        ctx.check("F2: time to move p99 <= 1.5 s and max <= 3 s",
                  (ttm.get("p99") or 1e9) <= 1500 and (ttm.get("max") or 1e9) <= 3000, ttm)
        per_move = (report["fault_lost"] / moved_out) if moved_out else None
        bound = (ttm.get("max") or 0) / OPS_PART_MS
        ctx.measure("fault_lost_per_moved_listener", None if per_move is None
                    else round(per_move, 1))
        ctx.check("F2: parts lost only by moved listeners, each at most time to move / part "
                  "length (mean per mover against the largest time to move)",
                  report["unaffected_missing"] == 0 and (per_move or 0) <= bound,
                  {"per_mover": per_move, "bound_parts": bound})
    survivor_checks(ctx, rows, others, fault_unix, end_unix,
                    (ctx.t0 + 8, fault_unix))
    # R2 / R3: what the distributors and the receiving edges did.
    dists = lab.distributors("london")
    redistribution = {
        "distributors_subscriptions_released": ops_delta(
            rows, dists, "replication.subscriptions_released", fault_unix, end_unix),
        "receiving_edges_hole_repair_requests": ops_delta(
            rows, others, "hole_repair_requests", fault_unix, end_unix),
        "distributors_warm_bytes_sent": ops_delta(rows, dists, "cache_mesh.warm_bytes_sent",
                                                  fault_unix, end_unix),
        "distributors_warm_parts_sent": ops_delta(rows, dists, "cache_mesh.warm_parts_sent",
                                                  fault_unix, end_unix),
        "distributors_warm_tick_max_bytes": ops_max(rows, dists, "cache_mesh.warm_tick_max_bytes",
                                                    fault_unix, end_unix),
        "distributors_warm_capped_ticks": ops_delta(rows, dists, "cache_mesh.warm_capped_ticks",
                                                    fault_unix, end_unix),
        "watermark_stall_max_us": ops_max(rows, dists + others,
                                          "cache_mesh.watermark_stall_max_us",
                                          fault_unix, end_unix),
        "watermark_stalls_over_250ms": ops_delta(rows, dists + others,
                                                 "cache_mesh.watermark_stalls_over_250ms",
                                                 fault_unix, end_unix),
        "rx_presumed_lost": ops_delta(rows, dists + others, "mesh_fec.rx_presumed_lost",
                                      fault_unix, end_unix),
        "rx_expired_objects": ops_delta(rows, dists + others, "mesh_fec.rx_expired_objects",
                                        fault_unix, end_unix)}
    ctx.measure("redistribution", redistribution)
    ctx.expect_served(hls)
    ctx.expect_complete(hls)
    hls_moves = sum(len(reader.get("moves") or []) for reader in hls.readers)
    ctx.measure("hls_moves", hls_moves)
    return {"report": report, "rows": rows, "redistribution": redistribution,
            "fault_unix": fault_unix, "end_unix": end_unix}


@scenario("udp-edge-drain", "scale-ops",
          "An edge with its share of 160 UDP listeners (1 ms Opus, placed through "
          "/api/assignment) and steered LL-HLS readers is drained. Where do its listeners "
          "go, how fast, and does anyone stay behind or lose a part?",
          "F1: nobody stranded, 0 parts missing, time to move p99 <= 300 ms, each other edge's "
          "share of the moves within 1.3x of its headroom-weighted share and the listeners per "
          "survivor after them max/mean <= 1.15, the drained edge's subscriber gauge reaches "
          "0; survivors' CPU under 80% and no rise in would-block, send-queue drops or "
          "refusals. R2: the distributors release its copies; warm-up bytes reported.")
def udp_edge_drain(ctx):
    result = udp_fault(ctx, drain, "drained", ["lon-edge-1"], heal=resume, drained=True)
    rows = result["rows"]
    released = ops_delta(rows, ["lon-edge-1"], "replication.subscriptions_released",
                         result["fault_unix"], result["end_unix"])
    children = [(round(row["t"] - result["fault_unix"], 1),
                 sum(ops_value(row, name, "cache_mesh.children") or 0
                     for name in lab.distributors("london")))
                for row in rows if row["t"] >= result["fault_unix"] - 2]
    ctx.measure("r2_drained_edge_released", released)
    ctx.measure("r2_distributor_cache_children", children[::2])
    ctx.check("R2: the drained edge released its cache-mesh copies (its parents lost it as "
              "a child)", (released or 0) > 0 and children and
              min(value for _, value in children) < children[0][1],
              {"released": released, "children_first_min": (children[0][1] if children else
                                                             None,
                                                             min((value for _, value in children),
                                                                 default=None))})


@scenario("udp-edge-kill", "scale-ops",
          "An edge with its share of 160 UDP listeners is killed (SIGKILL; nothing restarts it "
          "until the heal 30 s later: node loss). Where do its listeners go, and how fast?",
          "F2: nobody stranded, time to move p99 <= 1.5 s and max <= 3 s, parts lost only by "
          "the moved listeners (each at most time to move / part length), unaffected "
          "listeners 0 missing, moves by headroom (each survivor within 1.3x of its "
          "headroom-weighted share, listeners after max/mean <= 1.15); survivors' criteria.")
def udp_edge_kill(ctx):
    udp_fault(ctx, lab.kill_service, "killed", ["lon-edge-2"], heal=lab.start_service)


@scenario("udp-two-edges-drain", "scale-ops",
          "Two of four London edges are drained at once with 120 UDP listeners on them all "
          "(the catch-up storm of R5, 2 of the region's edges as the critique sizes it). Do "
          "the two survivors take them without loss, overload or a warm-up burst?",
          "F1 for each drained edge (0 missing, time to move p99 <= 300 ms, nobody stranded); "
          "R5: the largest warm-up burst per tick and capped ticks reported, rx_presumed_lost "
          "and rx_expired_objects 0, no watermark stall over 250 ms; survivors' criteria.")
def udp_two_edges_drain(ctx):
    result = udp_fault(ctx, drain, "drained", ["lon-edge-1", "lon-edge-2"], count=120,
                       heal=resume, drained=True)
    item = result["redistribution"]
    ctx.check("R5: rx_presumed_lost and rx_expired_objects stay 0",
              not item["rx_presumed_lost"] and not item["rx_expired_objects"],
              {key: item[key] for key in ("rx_presumed_lost", "rx_expired_objects")})
    ctx.check("R5: no watermark stall over 250 ms", not item["watermark_stalls_over_250ms"],
              {key: item[key] for key in ("watermark_stall_max_us",
                                          "watermark_stalls_over_250ms")})


@scenario("slot-edge-join", "scale-ops",
          "A slot edge (lon-edge-4, rendered into every peer list but not started) is taken "
          "into service under load: 90 UDP listeners on three edges, 60 more arriving 15 s "
          "after the join. Does anything else restart, does it serve, and does the join "
          "disturb the other edges?",
          "R1: no other service restarts (every pid the same); the new edge is advertised "
          "fresh within 20 s and serves its first part within 20 s; the distributors' warm-up "
          "backlog for it reaches 0 within 10 s; p99 at the other edges within +5% of before "
          "the join; new UDP arrivals and late LL-HLS readers reach it. The edges' load counts "
          "client datagrams (client_udp_datagram_capacity, as the scale run's autoscale phases "
          "need it): by bytes alone an edge at its CPU limit reads ~40% loaded, and the "
          "assignment keeps new arrivals on the edges that hold the playlist below 60%.",
          needs=SCALE_NEEDS)
def slot_edge_join(ctx):
    slot = "lon-edge-4"
    ctx.defer(lambda: (lab.terminate_service(slot), lab.set_slot_in_service(slot, False)))
    ctx.check("the slot is not in service before the join", lab.service_pid(slot) is None)
    ops = OpsSampler(ctx).start()
    udp = udp_listeners(ctx, "udp", 150, 70, ramp="90@0,150@35")
    jobs = play(ctx, 70, video=2, audio=4, steering=True)
    others = list(lab.edges())
    ctx.at(20)
    before = service_pids()
    lab.set_slot_in_service(slot, True)
    lab.start_service(slot)
    joined = time.time()
    ctx.event(f"{slot} started")

    def fresh():
        status, body = lab.http_json("lon-dist-a", f"/api/assignment?stream={AUDIO}")
        return status == 200 and any(cache.get("node_id") == slot and cache.get("fresh")
                                     for cache in (body or {}).get("caches") or [])

    try:
        lab.wait_for(f"{slot} advertised", fresh, 30, 0.25)
        ctx.measure("join_to_fresh_s", round(time.time() - joined, 2))
    except lab.LabError:
        ctx.measure("join_to_fresh_s", None)
    ctx.check("the joined edge is advertised fresh within 20 s",
              (ctx.measurements.get("join_to_fresh_s") or 99) <= 20,
              ctx.measurements.get("join_to_fresh_s"))
    late = ctx.readers("after-join", AUDIO, 6, 25, steering=True)
    ctx.at(60)
    after = service_pids()
    changed = {name: (before.get(name), after.get(name)) for name in before
               if before.get(name) != after.get(name)}
    ctx.check("no other service restarted (the join changed no other node's arguments)",
              not changed, changed)
    late.wait()
    udp.wait()
    ctx.wait_readers()
    rows = ops.stop()
    # A reader names its cache by node or by address.
    slot_names = {slot, lab.node(slot)["public_ipv4"],
                  f"{lab.node(slot)['public_ipv4']}:{lab.HTTP_PORT}"}
    on_slot = sum(1 for reader in late.readers if reader.get("final_cache") in slot_names)
    ctx.measure("late_readers_on_the_joined_edge", on_slot)
    ctx.check("the joined edge serves readers that arrive after it", on_slot > 0, on_slot)
    for job in jobs + [late]:
        ctx.expect_served(job)
    report = udp_report(ctx, udp, since=ctx.t0 + 10)
    listener_checks(ctx, report)
    ctx.check("UDP listeners: 0 missing", report["missing"] == 0, report["missing"])
    # The first part the new edge served: its first UDP subscriber, or the
    # first late reader's first part.
    served = next((row["t"] for row in rows
                   if (ops_value(row, slot, "gauges.subscribers") or 0) > 0
                   or (ops_value(row, slot, "gauges.hls_sessions") or 0) > 0), None)
    ctx.measure("join_to_first_served_s", None if served is None else round(served - joined, 2))
    ctx.check("R1: the joined edge served within 20 s of its start",
              served is not None and served - joined <= 20,
              ctx.measurements["join_to_first_served_s"])
    dists = lab.distributors("london")
    pending = [(row["t"], sum(ops_value(row, name, "cache_mesh.warm_parts_pending") or 0
                              for name in dists)) for row in rows if row["t"] >= joined]
    zero_after = join_warm_up_s(pending, joined)
    ctx.measure("warm_parts_pending", {"max": max((value for _, value in pending), default=None),
                                       "zero_after_join_s": round(zero_after, 1)})
    ctx.check("R1: the distributors' warm-up backlog reached 0 within 10 s of the join",
              zero_after <= 10, ctx.measurements["warm_parts_pending"])
    warm = {"bytes": ops_delta(rows, dists, "cache_mesh.warm_bytes_sent", joined, joined + 30),
            "parts": ops_delta(rows, dists, "cache_mesh.warm_parts_sent", joined, joined + 30),
            "tick_max_bytes": ops_max(rows, dists, "cache_mesh.warm_tick_max_bytes", joined,
                                      joined + 30),
            "capped_ticks": ops_delta(rows, dists, "cache_mesh.warm_capped_ticks", joined,
                                      joined + 30)}
    ctx.measure("warm_up_of_the_joined_edge", warm)
    # The other edges, the same listeners before the join and while it
    # warms (the second wave arrives 15 s after the join).
    p99_before = edge_p99(udp, others, ctx.t0 + 8, joined)
    p99_during = edge_p99(udp, others, joined, ctx.t0 + 35)
    ctx.measure("other_edges_p99_ms", {"before": p99_before, "join_and_warm_up": p99_during})
    ctx.check("R1: p99 at the other edges within +5% of before the join (or +0.5 ms)",
              p99_before is not None and p99_during is not None
              and p99_during <= max(p99_before * 1.05, p99_before + 0.5),
              ctx.measurements["other_edges_p99_ms"])
    arrivals = first_parts(udp, ctx.t0 + 34, ctx.t0 + 70)
    ctx.measure("second_wave_first_parts_by_edge", arrivals)
    free = {name: ops_value(next((row for row in rows if row["t"] >= ctx.t0 + 34), {}),
                            name, "advert.free_bps") for name in others + [slot]}
    ctx.measure("free_bps_at_second_wave", free)
    ctx.check("new UDP arrivals reach the joined edge", arrivals.get(slot, 0) > 0, arrivals)
    survivor_checks(ctx, rows, others, joined, ctx.t0 + 60, (ctx.t0 + 8, joined))


@scenario("dist-c-join-kill", "scale-ops",
          "The forwarder slot lon-dist-c joins under load (120 UDP listeners) and is killed "
          "30 s later (node loss). Do edges take it as a parent and lose nothing when it "
          "goes?",
          "R4: lon-dist-c gets lease and cache-mesh children; the count of (edge, playlist) "
          "pairs whose first parent changes is reported; 0 parts lost by the UDP listeners "
          "and the LL-HLS readers; nobody stranded; survivors' criteria.")
def dist_c_join_kill(ctx):
    slot = "lon-dist-c"
    ctx.defer(lambda: (lab.terminate_service(slot), lab.set_slot_in_service(slot, False)))
    ops = OpsSampler(ctx).start()
    udp = udp_listeners(ctx, "udp", 120, 75)
    hls = ctx.readers("audio", AUDIO, 4, 75, steering=True)
    ctx.at(20)
    lab.set_slot_in_service(slot, True)
    lab.start_service(slot)
    joined = time.time()
    ctx.event(f"{slot} started")
    ctx.at(50)
    lab.kill_service(slot)
    killed = time.time()
    ctx.event(f"{slot} killed")
    udp.wait()
    ctx.wait_readers()
    rows = ops.stop()
    report = udp_report(ctx, udp, since=ctx.t0 + 10)
    listener_checks(ctx, report)
    ctx.check("UDP listeners: 0 missing", report["missing"] == 0, report["missing"])
    ctx.expect_served(hls)
    ctx.expect_complete(hls)
    lease = ops_max(rows, [slot], "fast_plane.lease_children", joined, killed)
    cache = ops_max(rows, [slot], "cache_mesh.children", joined, killed)
    ctx.measure("dist_c_children", {"lease": lease, "cache_mesh": cache})
    ctx.check("lon-dist-c took lease and cache-mesh children", (lease or 0) > 0 and
              (cache or 0) > 0, {"lease": lease, "cache_mesh": cache})
    ctx.measure("first_parent_pairs", {
        name: [ops_value(row, name, "cache_mesh.first_parent_pairs") for row in rows][::5]
        for name in lab.names("distributor", "london")})
    edges = lab.edges()
    survivor_checks(ctx, rows, edges, joined, time.time(), (ctx.t0 + 8, joined))


# Wave 9 C3: the autoscale scenario ramps at SCALE.md's A1 rate (0.135 % of
# the capacity of the edges in service a second; ~100 listeners fill the
# lab's three) from 50 to 95 listeners, holds, and comes down to 25.
# NEEDLETAIL_AUTOSCALE_RAMP (a needletail-load ramp), with
# NEEDLETAIL_AUTOSCALE_SECONDS, replaces it (e.g. the wave 8 proof's
# "30@0,60@15,90@30,120@90,25@150" over 250 s, 13x SCALE.md's rate).
AUTOSCALE_RAMP = os.environ.get("NEEDLETAIL_AUTOSCALE_RAMP")
AUTOSCALE_SECONDS = int(os.environ.get("NEEDLETAIL_AUTOSCALE_SECONDS") or 600)
AUTOSCALE_PEAK = (max(int(step.split("@")[0]) for step in AUTOSCALE_RAMP.split(","))
                  if AUTOSCALE_RAMP else 95)


def autoscale_ramp() -> str:
    if AUTOSCALE_RAMP:
        return AUTOSCALE_RAMP
    steps, at = [], 0
    while at <= 330:
        steps.append(f"{min(AUTOSCALE_PEAK, round(50 + 0.135 * at))}@{at}")
        at += 10
    for at in range(370, 501, 10):
        steps.append(f"{max(25, round(AUTOSCALE_PEAK - 0.54 * (at - 370)))}@{at}")
    return ",".join(steps)


def rebalance_moves_out(job) -> dict[str, int]:
    """Moves off each edge for `rebalance` (needletail-load's per-edge
    reasons)."""
    summary = job.summary()
    per_edge = summary.get("per_edge") or summary.get("edges") or {}
    if isinstance(per_edge, list):
        per_edge = {item.get("edge"): item for item in per_edge if isinstance(item, dict)}
    names = edge_name_map()
    out = {}
    for edge, block in per_edge.items():
        count = ((block or {}).get("moves_by_reason") or {}).get("rebalance", 0)
        if count:
            out[names.get(edge, edge)] = count
    return out


@scenario("autoscale-edge-lab", "scale-ops",
          "Does the autoscaler, with the edge_lab backend and nothing but the mesh's own "
          "advertised load, take a slot edge in as UDP listeners ramp up, and drain it out "
          "without a stranded listener as they leave?",
          "A1/A2 at SCALE.md's ramp rate: the controller alone joins lon-edge-4 when the mean "
          "load passes 700 permille or an edge 850 with the mean over 600 (client datagrams "
          "and the CPU count), no edge passes 1,000 permille (target 900; an edge over 800 "
          "sends listeners to the joined one, MOVE rebalance, losing nothing), it serves within "
          "20 s, no other service restarts; new arrivals reach it; on the way down it drains "
          "an edge and deletes the slot only after its live gauges read 0; listeners on edges "
          "not drained see no move and no loss; drained edges meet F1.",
          needs=SCALE_NEEDS, slow=True)
def autoscale_edge_lab(ctx):
    slot = "lon-edge-4"
    events = ctx.directory / "autoscale-events.jsonl"
    state = ctx.directory / "autoscale-state.json"
    ctx.defer(lambda: (lab.terminate_service(slot), lab.set_slot_in_service(slot, False)))
    ops = OpsSampler(ctx).start()
    controller = subprocess.Popen(
        [sys.executable, str(pathlib.Path(__file__).resolve().parent / "linode-autoscale.py"),
         "run", "--backend", "edge_lab", "--stream", str(OPS_STREAM), "--sites", "london",
         "--events", str(events), "--controller-state", str(state), "--duration",
         str(AUTOSCALE_SECONDS + 20),
         "--poll-s", "1", "--up-samples", "3", "--cooldown-s", "20", "--down-hold-s", "40",
         "--no-flap-s", "60", "--join-timeout-s", "60", "--drain-timeout-s", "40",
         "--min-edges", "2", "--no-warm-pool",
         # Its events and state into the campaign's run in the archive.
         *(["--run-id", core.archive_run_id()] if core.archive_run_id() else [])],
        stdout=open(ctx.directory / "autoscale.log", "ab"), stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True)
    ctx.defer(lambda: controller.poll() is None and controller.terminate())
    before = service_pids()
    # 1 ms Opus listeners (~1,031 client datagrams a second each, ~30
    # permille of 40,000 with their repair: ~33 fill an edge, ~100 the
    # three edges in service). Wave 9 C3: the ramp of SCALE.md's A1 (30 to
    # 95 % of capacity over 480 s, 0.135 % of it a second), from 50 % to
    # 95 % (50 to 95 listeners in steps every 10 s), held, then down to 25
    # four times as fast as A2's.
    udp = udp_listeners(ctx, "udp", AUTOSCALE_PEAK, AUTOSCALE_SECONDS, ramp=autoscale_ramp())
    hls = ctx.readers("audio", AUDIO, 3, AUTOSCALE_SECONDS - 10, steering=True)
    udp.wait()
    ctx.wait_readers()
    controller.wait(timeout=120)
    rows = ops.stop()
    lines = [json.loads(line) for line in events.read_text().splitlines()] \
        if events.is_file() else []
    joins = [line for line in lines if line.get("kind") == "decision"
             and line.get("action") == "join"]
    serving = [line for line in lines if line.get("kind") == "serving"]
    drains = [line for line in lines if line.get("kind") == "drain_start"]
    ends = [line for line in lines if line.get("kind") == "drain_end"]
    stops = [line for line in lines if line.get("kind") == "stopped"]
    deletes = [line for line in lines if line.get("kind") == "deleted"]
    failures = [line for line in lines if line.get("kind") in ("failure", "error")]
    ctx.measure("controller_decisions", [{key: line.get(key) for key in
                                          ("unix_us", "action", "reason", "targets", "inputs")}
                                         for line in lines if line.get("kind") == "decision"
                                         and line.get("action") != "hold"][:20])
    ctx.measure("controller_actions", [{key: line.get(key) for key in
                                        ("unix_us", "kind", "node", "seconds",
                                         "trigger_to_fresh_s", "detail", "what")}
                                       for line in lines if line.get("kind") not in
                                       ("decision", "controller_start")])
    ctx.check("the controller alone joined the slot edge",
              any([slot, "create"] in line.get("targets", []) or [slot, "start"]
                  in line.get("targets", []) for line in joins),
              [line.get("targets") for line in joins])
    ctx.check("the joined edge served within 20 s of the trigger",
              any((line.get("trigger_to_fresh_s") or 99) <= 20 for line in serving),
              [line.get("trigger_to_fresh_s") for line in serving])
    ctx.check("the controller made no failed action", not failures,
              [(line.get("kind"), line.get("node"), line.get("what") or line.get("phase"))
               for line in failures])
    touched = {line.get("node") for line in drains + stops}
    after = {name: lab.service_pid(name) for name in before}
    ctx.check("no other service restarted (the nodes the controller drained aside)",
              all(before[name] == after[name] for name in before if name not in touched),
              {name: (before[name], after[name]) for name in before
               if before[name] != after[name]})
    ctx.check("the controller drained an edge on the way down", bool(drains),
              [line.get("node") for line in drains])
    order_ok = True
    for line in stops + deletes:
        end = next((item for item in ends if item.get("node") == line.get("node")
                    and item["unix_us"] <= line["unix_us"]), None)
        order_ok = order_ok and end is not None
    ctx.check("every stop and delete came after the drained edge's gauges read 0 (drain_end)",
              bool(stops) and order_ok,
              {"drain_end": [(item.get("node"), item.get("seconds")) for item in ends],
               "stopped": [item.get("node") for item in stops],
               "deleted": [item.get("node") for item in deletes]})
    drained = sorted(touched)
    report = udp_report(ctx, udp, faulted=drained, since=ctx.t0 + 10)
    listener_checks(ctx, report)
    # Wave 9 C1: an edge over its target sends listeners to one with room
    # (MOVE rebalance): those moves are counted, timed and must lose
    # nothing; any other move off an edge not drained is a fault.
    rebalanced = rebalance_moves_out(udp)
    ctx.measure("rebalance_moves_out_by_edge", rebalanced)
    ctx.measure("rebalance_moves", sum(rebalanced.values()))
    ctx.measure("rebalance_moves_by_edge_gauge",
                {name: ops_delta(rows, [name], "gauges.rebalance_moves", rows[0]["t"],
                                 rows[-1]["t"]) for name in lab.edges()} if rows else {})
    others_moved = {name: count - rebalanced.get(name, 0)
                    for name, count in report["moves_out_by_edge"].items()
                    if name not in drained and count - rebalanced.get(name, 0) > 0}
    ctx.check("listeners on edges not drained: 0 moves but rebalance", not others_moved,
              {"moves_out_by_edge": report["moves_out_by_edge"], "rebalance": rebalanced})
    ctx.check("drained edges meet F1: 0 missing, time to move p99 <= 300 ms",
              report["missing"] == 0 and ((report["time_to_move_ms"] or {}).get("p99")
                                          or 0) <= 300,
              {"missing": report["missing"], "time_to_move_ms": report["time_to_move_ms"]})
    if serving:
        fresh_unix = serving[0]["unix_us"] / 1e6
        arrivals = first_parts(udp, fresh_unix, fresh_unix + 60)
        ctx.measure("arrivals_within_60_s_of_the_join", arrivals)
        free = {}
        row = next((row for row in rows if row["t"] >= fresh_unix), None)
        if row:
            free = {name: ops_value(row, name, "advert.free_bps") for name in row["nodes"]
                    if lab.node(name)["role"] == "edge"}
        ctx.measure("free_bps_at_the_join", free)
        ctx.check("new arrivals reach the joined edge within 60 s", arrivals.get(slot, 0) > 0,
                  arrivals)
    loads = {name: [ops_value(row, name, "advert.load_permille") for row in rows][::5]
             for name in sorted({name for row in rows for name in row["nodes"]})
             if lab.node(name)["role"] == "edge"}
    ctx.measure("load_permille_every_5_s", loads)
    # Wave 9 C3: no edge over 1,000 permille in the ramp (overload), and
    # the target is 900: the largest load of each edge (its own gauge, every
    # second, CPU and datagrams counted) and the seconds over 900.
    peak = {}
    for name in sorted({name for row in rows for name in row["nodes"]}):
        if lab.node(name)["role"] != "edge":
            continue
        values = [value for value in (ops_value(row, name, "gauges.load_permille")
                                      for row in rows) if value is not None]
        cpu = [value for value in (ops_value(row, name, "gauges.cpu_permille")
                                   for row in rows) if value is not None]
        if values:
            peak[name] = {"max": max(values), "seconds_over_900": sum(v > 900 for v in values),
                          "seconds_over_1000": sum(v > 1000 for v in values),
                          "cpu_max": max(cpu) if cpu else None}
    ctx.measure("edge_load_peak", peak)
    worst = max((item["max"] for item in peak.values()), default=None)
    ctx.check("no edge over 1,000 permille in the ramp", worst is not None and worst <= 1000,
              peak)
    ctx.check("no edge over the 900 permille target in the ramp",
              worst is not None and worst <= 900, {"max": worst})
    ctx.expect_served(hls)


# =================================================================== change
#
# C CL5: the scored changes of C-operations 6.3. Each scenario is a run of
# scripts/compare/change-run.py on the campaign's lab (--no-up): the
# referee's probes on lon-readers and new-readers, the crowd and the steered
# readers, the change command at 45 s, then the referee's own ntcmp
# udp-ingest and change-window.py. The scenario's null runs first (the
# engine's --dry-marks, or `sleep` under the same label for a plain kind),
# then the change; each NEEDLETAIL_CHANGE_REPEAT times (default 1: "proved
# locally" is 10 of 10 of both, change-run.py's summary.json counts every
# run of the name). The checks are the referee's verdicts: every run passes
# by its rule on every probe and label, and the nodes that the change must
# restart did (their /api/node boot id) and no other did. Each scenario's
# probes are sized from its command's bound (the timeouts it is given, never
# its typical time, as scale-run sizes its own): long enough after it for
# the referee's 120 no-change windows of its length.

CHANGE_RUN = pathlib.Path(__file__).resolve().parent / "compare" / "change-run.py"
EDGE_LAB = [sys.executable, str(pathlib.Path(__file__).resolve().parent / "edge_lab.py")]
AUTOSCALE = [sys.executable,
             str(pathlib.Path(__file__).resolve().parent / "linode-autoscale.py")]
CHANGE_REPEAT = int(os.environ.get("NEEDLETAIL_CHANGE_REPEAT") or 1)
CHANGE_LANES = "opus-1,pcm-1"
# The crowd of the autoscale scenarios against the lab's 40,000-datagram
# edges (SCALE_NEEDS): 30 one-path 1 ms listeners an edge read about 790
# permille, over the controller's 700 trigger; 10 read about 260, under its
# 450 for a scale-down.
AUTOSCALE_UP_LISTENERS = 30
AUTOSCALE_DOWN_LISTENERS = 10
# The engine's timeouts the change commands are given, the proving run's
# (phases-proving.json), and what a node may take with them: its gate, drain
# and readiness, and its stop and start (scale-run's ENGINE_RESTART_S).
CHANGE_TIMEOUTS = ["--gate-timeout-s", "10", "--drain-timeout-s", "15", "--ready-timeout-s", "30"]
CHANGE_NODE_S = 10.0 + 15.0 + 30.0 + 15.0
# change-run.py's --at: the change starts this long after the probes.
CHANGE_AT_S = 45
# The bounds of the commands with no engine timeouts: a crash (the kill,
# 5 s at most for the process to go, the 1 s restart, then wait_back's 60 s);
# a replace's 30 s gap; the autoscaler's scale verbs (its join timeout, 120 s,
# and the rebalance's 60 s; down: its drain timeout, 60 s, and the stop) and
# the controller's runs below (3 samples a second apart, the join timeout
# they give, 60 s, the rebalance's 60 s; down: the 20 s hold, the 40 s drain,
# the stop).
CRASH_BOUND_S = 5.0 + 1.0 + 60.0
REPLACE_GAP_S = 30.0
SCALE_UP_BOUND_S = 120.0 + 60.0
SCALE_DOWN_BOUND_S = 60.0 + 30.0 + 30.0
AUTOSCALE_UP_BOUND_S = 3.0 + 60.0 + 60.0
AUTOSCALE_DOWN_BOUND_S = 3.0 + 20.0 + 40.0 + 30.0
_change_run = {}


def change_run():
    """scripts/compare/change-run.py as a module (its out_root, runs_of and
    lab_overrides), loaded once."""
    if "module" not in _change_run:
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("change_run", CHANGE_RUN)
        module = sys.modules.get("change_run") or importlib.util.module_from_spec(spec)
        if "change_run" not in sys.modules:
            sys.modules["change_run"] = module
            spec.loader.exec_module(module)
        _change_run["module"] = module
    return _change_run["module"]


def change_needs(lanes: str, extra=()) -> list[str]:
    """The lab settings of a change scenario: change-run.py's own `up`
    settings for its lanes (it runs with --no-up on the campaign's lab),
    then the scenario's."""
    import argparse  # noqa: PLC0415
    return change_run().lab_overrides(argparse.Namespace(lanes=lanes, set=[])) + list(extra)


def probe_seconds(bound_s: float) -> int:
    """change-run.py's --seconds for a change of at most `bound_s` at
    CHANGE_AT_S: change-run.py's own seconds_needed (the referee's 120
    no-change windows of the change's length after it), and its 360 at
    least."""
    tool = change_run()
    at_ns = int(CHANGE_AT_S * 1e9)
    return max(tool.MIN_SECONDS, tool.seconds_needed((at_ns, at_ns + int(bound_s * 1e9)), 0))


def engine_bound_s(nodes: list[str], sites: list[str]) -> float:
    """The bound of an engine command over `nodes`: CHANGE_NODE_S a node,
    and a surge edge in each of `sites` that holds an edge slot out."""
    surges = {lab.node(name)["site"] for name in lab.held_slots()
              if lab.node(name)["role"] == "edge"} & set(sites)
    return (len(nodes) + len(surges)) * CHANGE_NODE_S


def change_run_argv(name: str, command: list[str], lanes: str, options: list[str],
                    seconds: int, dry: bool = False) -> list[str]:
    """change-run.py's command line for one command of a scenario, its
    probes `seconds` long with the change at CHANGE_AT_S (`dry`: the
    engine's null, COMMAND with --dry-marks)."""
    return ([sys.executable, str(CHANGE_RUN), "run", "--name", name, "--no-up",
             "--lanes", lanes, "--repeat", str(CHANGE_REPEAT), "--seconds", str(seconds),
             "--at", str(CHANGE_AT_S), *options]
            + (["--dry"] if dry else []) + ["--", *command])


def boot_ids(path: pathlib.Path) -> dict[str, int | None]:
    """{node: /api/node boot id} of a change-run.py nodes-before.json or
    nodes-after.json."""
    try:
        documents = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for name, document in documents.items():
        status = mesh_ops.status_of(document or None)
        out[name] = status.boot_id if status else None
    return out


def restart_checks(ctx, run: dict, restarted: list[str] | None) -> None:
    """The nodes the change restarts got a new boot id, and no other node
    present before and after did (a node that joined or left, a slot, is
    not compared). `restarted` None: the scenario does not judge restarts."""
    if restarted is None:
        return
    directory = pathlib.Path(run["directory"])
    before, after = boot_ids(directory / "nodes-before.json"), boot_ids(directory / "nodes-after.json")
    both = sorted(set(before) & set(after))
    unknown = [name for name in both if before[name] is None or after[name] is None]
    changed = [name for name in both if name not in unknown and before[name] != after[name]]
    ctx.check(f"{directory.name}: every node /api/node answers with a boot id", not unknown,
              unknown)
    if restarted:
        ctx.check(f"{directory.name}: {', '.join(restarted)} restarted (new boot id)",
                  all(name in changed for name in restarted),
                  {name: (before.get(name), after.get(name)) for name in restarted})
    others = [name for name in changed if name not in restarted]
    ctx.check(f"{directory.name}: no other node restarted", not others, others)


def verdict_checks(ctx, run: dict) -> None:
    """A run of change-run.py as checks: its status (the referee's rule on
    every probe and label, the command's exit, the probe long enough), and
    for a change every label on every probe, with CW's figures."""
    name = pathlib.Path(run["directory"]).name
    ctx.check(f"{name}: pass by the referee's rule", run.get("status") == "pass",
              {"status": run.get("status"), "reasons": run.get("reasons"),
               "command_exit": run.get("command_exit"), "hints": run.get("hints")})
    figures = {}
    for label, per in (run.get("labels") or {}).items():
        for probe, result in per.items():
            # CW's false-fail rate only where it could measure it (never a
            # 0.0 by construction).
            rate, unmeasured = change_run().null_fail_rate(result)
            result = {**result, "null_fail_rate": rate,
                      **({"null_fail_rate_unmeasured": unmeasured} if unmeasured else {})}
            figures[f"{label} {probe}"] = result
            if run["kind"] == "change":
                ctx.check(f"{name}: {label} on {probe}", result.get("pass") is True
                          and "error" not in result,
                          {key: result.get(key) for key in (
                              "pass", "missing", "gap_ms", "gap_bar_ms", "p99_ms", "p99_bar_ms",
                              "null_fail_rate", "null_fail_rate_unmeasured", "note", "error")
                           if result.get(key) is not None})
    ctx.measure(f"cw_{name}", figures)


def change_scenario(ctx, command: list[str], null: list[str] | None, lanes: str,
                    options: list[str], restarted: list[str] | None, bound_s: float,
                    setup=None) -> None:
    """Run the scenario's null, then its change, through change-run.py, and
    take the referee's verdicts as the scenario's checks; both with probes
    sized from the change's bound. The campaign's own sources are stopped
    for the runs (change-run.py starts the senders of its lanes) and
    started again after."""
    name = ctx.scenario.name
    if setup is not None:
        setup(ctx)
    lab.stop_sources()
    ctx.defer(lambda: lab.start_sources(6 * 3600, audio="daw"))
    scenario_dir = change_run().out_root() / name
    seconds = probe_seconds(bound_s)
    ctx.measure("change_bound_s", bound_s)
    ctx.measure("probe_seconds", seconds)
    null_argv = (change_run_argv(name, command, lanes, options, seconds, dry=True)
                 if null is None else change_run_argv(name, null, lanes, options, seconds))
    for kind, argv in (("null", null_argv),
                       ("change", change_run_argv(name, command, lanes, options, seconds))):
        known = {run.get("directory") for run in change_run().runs_of(scenario_dir)}
        ctx.event(f"change-run.py {kind}: {' '.join(argv[2:])}")
        with open(ctx.directory / f"change-run-{kind}.log", "ab") as log:
            done = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL)
        runs = [run for run in change_run().runs_of(scenario_dir)
                if run.get("directory") not in known and run.get("kind") == kind]
        ctx.check(f"change-run.py made the {kind} runs ({CHANGE_REPEAT})",
                  len(runs) == CHANGE_REPEAT, {"exit": done.returncode, "runs": len(runs)})
        for run in runs:
            verdict_checks(ctx, run)
            if kind == "change":
                restart_checks(ctx, run, restarted)
    summary = scenario_dir / "summary.json"
    if summary.is_file():
        record = json.loads(summary.read_text())
        ctx.measure("change_run_tally", record.get("tally"))
        ctx.measure("proved_locally", record.get("proved"))


def start_slot(name: str):
    """A set-up: the slot taken into service before the change (a scenario
    that needs one edge more than the lab serves), held out again after."""
    def setup(ctx) -> None:
        ctx.defer(lambda: (lab.terminate_service(name), lab.set_slot_in_service(name, False)))
        lab.set_slot_in_service(name, True)
        if lab.service_pid(name) is None:
            lab.start_service(name)
        ctx.event(f"{name} taken into service")
    return setup


def hold_out_after(*names: str):
    """A set-up: slots that the change takes into service (a scale-up, the
    controller's join) are held out again after the scenario."""
    def setup(ctx) -> None:
        for name in names:
            ctx.defer(lambda name=name: (lab.terminate_service(name),
                                         lab.set_slot_in_service(name, False)))
    return setup


def needs_set_b(ctx) -> None:
    """A set-up: the lab runs a second build set (up with
    NEEDLETAIL_AV_MESH_BIN_NEXT), or an upgrade has nothing to go to."""
    if "b" not in lab.build_sets():
        raise lab.LabError("the lab has no build set b: export NEEDLETAIL_AV_MESH_BIN_NEXT "
                           "(and NEEDLETAIL_AV_CONTRIB_BIN_NEXT) before the campaign")


def register_change(name: str, question: str, command: list[str], null: list[str] | None,
                    restarted, bound, criterion: str = "", needs=(), lanes: str = CHANGE_LANES,
                    options=(), setup=None) -> None:
    """One scenario of the change group: `restarted` is the nodes the change
    must restart (a callable of the lab, evaluated when the scenario runs),
    [] for none, None when restarts are not judged; `bound` the most its
    command may take in seconds (or a callable of the lab, likewise)."""
    def run(ctx) -> None:
        change_scenario(ctx, command, null, lanes, list(options),
                        restarted() if callable(restarted) else restarted,
                        bound() if callable(bound) else bound, setup)
    scenario(name, "change", question,
             criterion or ("change-run.py: the null and the change pass by the referee's rule "
                           "(change-window.py: 0 missing, silence and p99 within the 97.5th "
                           "percentile of the probe's own no-change windows) on every probe"),
             needs=change_needs(lanes, needs), slow=True)(run)


def london_edges() -> list[str]:
    return [name for name in lab.edges("london") if name not in lab.held_slots()]


def distributors_in_service() -> list[str]:
    return [name for site in ("london", "newyork") for name in lab.distributors(site)
            if name not in lab.held_slots()]


def every_node() -> list[str]:
    return [name for name in lab.standing_services()
            if lab.node(name)["role"] in ("edge", "distributor", "contributor")]


ROLL_EDGES = EDGE_LAB + ["roll", "edges", "--sites", "london", *CHANGE_TIMEOUTS]
ROLL_FORWARDERS = EDGE_LAB + ["roll", "forwarders", *CHANGE_TIMEOUTS]
UPGRADE_ALL = EDGE_LAB + ["roll", "all", "--build", "b", *CHANGE_TIMEOUTS]
UPGRADE_BACK = EDGE_LAB + ["roll", "all", "--build", "a", *CHANGE_TIMEOUTS]
FORWARDER_CRASH = EDGE_LAB + ["crash", "new-dist-a"]
# The typical times of the commands that have no dry mode, for their
# `sleep` nulls (C 6.2): a crash and its return, a replace with its 30 s
# gap, a scale-up to the rebalance's end, a scale-down to the stop.
CRASH_NULL = ["sleep", "5"]
REPLACE_NULL = ["sleep", "45"]
SCALE_NULL = ["sleep", "30"]
AUTOSCALE_NULL = ["sleep", "60"]
CELLS = (("opus25", "on 2.5 ms Opus", {"lanes": "opus-2.5"}),
         ("client-loss", "with 1% loss on the probe hosts' links",
          {"options": ["--client-loss", "loss 1%"]}),
         ("wan-loss", "with 1% loss on New York's wide-area link",
          {"options": ["--wan-loss", "newyork=loss 1%"]}))
def roll_edges_bound_s() -> float:
    return engine_bound_s(london_edges(), ["london"])


def roll_forwarders_bound_s() -> float:
    return engine_bound_s(distributors_in_service(), [])


def every_node_bound_s() -> float:
    return engine_bound_s(every_node(), ["london", "newyork"])


for _name, _question, _command, _null, _restarted, _bound, _extra in (
        ("roll-edges", "Is a rolling restart of London's edges hitless for every probe?",
         ROLL_EDGES, None, london_edges, roll_edges_bound_s, {"needs": ["slots=lon-edge-4"]}),
        ("roll-forwarders", "Is a rolling restart of every forwarder hitless for every probe?",
         ROLL_FORWARDERS, None, distributors_in_service, roll_forwarders_bound_s, {}),
        ("upgrade-all", "Is a rolling upgrade of every node to build b hitless?",
         UPGRADE_ALL, None, every_node, every_node_bound_s, {"setup": needs_set_b}),
        ("forwarder-kill-return", "Is a forwarder crash and its return hitless?",
         FORWARDER_CRASH, CRASH_NULL, ["new-dist-a"], CRASH_BOUND_S, {})):
    register_change(_name, _question, _command, _null, _restarted, _bound, **_extra)
    for _cell, _words, _change in CELLS:
        _options = {**_extra, **_change}
        register_change(f"{_name}-{_cell}", f"{_question[:-1]} {_words}?", _command, _null,
                        _restarted, _bound, **_options)
register_change("upgrade-back", "Is a rolling upgrade of every node back to build a hitless?",
                UPGRADE_BACK, None, every_node, every_node_bound_s, setup=needs_set_b)
register_change("replace-edge", "Is a replace of lon-edge-1 hitless, and nothing else restarted?",
                EDGE_LAB + ["replace", "lon-edge-1", "--gap-s", f"{REPLACE_GAP_S:g}",
                            *CHANGE_TIMEOUTS], REPLACE_NULL,
                ["lon-edge-1"], CHANGE_NODE_S + REPLACE_GAP_S)
# A forwarder's replace: its slot started and made ready, then the node
# itself.
register_change("replace-forwarder",
                "Is a replace of lon-dist-a through lon-dist-c hitless, nothing else restarted?",
                EDGE_LAB + ["replace", "lon-dist-a", "--gap-s", f"{REPLACE_GAP_S:g}",
                            *CHANGE_TIMEOUTS], REPLACE_NULL,
                ["lon-dist-a"], 2 * CHANGE_NODE_S + REPLACE_GAP_S)
register_change("scale-up", "Is the scale-up verb hitless (it ends when the rebalance is still)?",
                AUTOSCALE + ["scale", "up", "--backend", "edge_lab", "--site", "london",
                             "--stream", str(OPS_STREAM)], SCALE_NULL, [], SCALE_UP_BOUND_S,
                needs=SCALE_NEEDS, setup=hold_out_after("lon-edge-4"))
register_change("scale-down", "Is the scale-down verb hitless (it ends at the stop)?",
                AUTOSCALE + ["scale", "down", "--backend", "edge_lab", "--site", "london",
                             "--stream", str(OPS_STREAM)], SCALE_NULL, [], SCALE_DOWN_BOUND_S,
                needs=SCALE_NEEDS, setup=start_slot("lon-edge-4"))
register_change("autoscale-edges-up",
                "Does the controller alone add an edge over its trigger, hitlessly?",
                AUTOSCALE + ["run", "--backend", "edge_lab", "--until", "join", "--sites",
                             "london", "--stream", str(OPS_STREAM), "--poll-s", "1",
                             "--up-samples", "3", "--join-timeout-s", "60"],
                AUTOSCALE_NULL, [], AUTOSCALE_UP_BOUND_S, needs=SCALE_NEEDS,
                setup=hold_out_after("lon-edge-4"),
                options=["--load-listeners", str(AUTOSCALE_UP_LISTENERS)])
register_change("autoscale-edges-down",
                "Does the controller alone drain and stop an edge under its trigger, hitlessly?",
                AUTOSCALE + ["run", "--backend", "edge_lab", "--until", "stopped", "--sites",
                             "london", "--stream", str(OPS_STREAM), "--poll-s", "1",
                             "--down-hold-s", "20", "--drain-timeout-s", "40",
                             "--no-warm-pool"],
                AUTOSCALE_NULL, [], AUTOSCALE_DOWN_BOUND_S, needs=SCALE_NEEDS,
                setup=start_slot("lon-edge-4"),
                options=["--load-listeners", str(AUTOSCALE_DOWN_LISTENERS)])
register_change("autoscale-forwarders",
                "Does the controller alone join dist-c, warm, in London and New York?",
                AUTOSCALE + ["run", "--backend", "edge_lab", "--until", "join", "--sites",
                             "london,newyork", "--stream", str(OPS_STREAM), "--forwarders",
                             "--lease-k", "2", "--poll-s", "1", "--up-samples", "3",
                             "--join-timeout-s", "60"],
                AUTOSCALE_NULL, [], AUTOSCALE_UP_BOUND_S, needs=SCALE_NEEDS,
                setup=hold_out_after("lon-dist-c", "new-dist-c"),
                options=["--load-listeners", str(AUTOSCALE_UP_LISTENERS)])


# =================================================================== driver

def select(arguments: list[str], slow: bool) -> list[core.Scenario]:
    if not arguments:
        chosen = list(core.REGISTRY.values())
    else:
        chosen = []
        for argument in arguments:
            matched = [item for item in core.REGISTRY.values()
                       if item.name == argument or item.group == argument
                       or item.group.startswith(argument + "-")]
            if not matched:
                sys.exit(f"no scenario or group named {argument}")
            chosen += [item for item in matched if item not in chosen]
    return [item for item in chosen if slow or not item.slow or item.name in arguments]


def print_summary(directory: pathlib.Path) -> None:
    rows = core.summary_rows(directory)
    width = max((len(row["scenario"]) for row in rows), default=10)
    for row in rows:
        detail = "; ".join(row["failed"])[:150]
        print(f"{row['scenario']:{width}}  {row['group']:20} {row['result']:5} {detail}")
    counts = {}
    for row in rows:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    print(f"\n{len(rows)} scenarios: " + ", ".join(f"{count} {result}"
                                                   for result, count in sorted(counts.items())))


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command, arguments = sys.argv[1], sys.argv[2:]
    if command == "list":
        for item in select(arguments, True):
            print(f"{item.name:32} {item.group:20} {item.question}")
    elif command == "run":
        out = None
        run_id = None
        slow = True
        rest = []
        index = 0
        while index < len(arguments):
            if arguments[index] == "--out":
                out = pathlib.Path(arguments[index + 1])
                index += 2
            elif arguments[index] == "--run-id":
                run_id = arguments[index + 1]
                index += 2
            elif arguments[index] == "--no-slow":
                slow = False
                index += 1
            else:
                rest.append(arguments[index])
                index += 1
        items = select(rest, slow)
        core.guard.require_frozen(core.guard.lab_binaries())
        # SIGTERM or SIGHUP (kill, timeout, a closed pane) unwinds the run
        # as Ctrl-C does: the scenario in hand and the run's end still go
        # into the archive.
        with archived_run.stopped_by_signals(), \
                core.guard.hold("edge-campaign.py run " + " ".join(arguments)):
            directory = out or core.RUNS / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            directory.mkdir(parents=True, exist_ok=True)
            core.RUNS.mkdir(parents=True, exist_ok=True)
            (core.RUNS / "latest").unlink(missing_ok=True)
            try:
                (core.RUNS / "latest").symlink_to(directory.resolve())
            except OSError:
                pass
            with core.archived(run_id or archived_run.default_run_id("edge-campaign"),
                               [item.name for item in items]) as record:
                record.directory = directory
                print(f"run archive: {record.run.path}", flush=True)
                for item in items:
                    core.run_scenario(item, directory)
                    try:
                        core.restore()
                    except lab.LabError as error:
                        print(f"  (restore failed: {error})")
        print()
        print_summary(directory)
        print(f"evidence: {directory}")
    elif command == "summary":
        print_summary(pathlib.Path(arguments[0]) if arguments else core.RUNS / "latest")
    elif command == "down":
        if arguments not in ([], ["--force"]):
            sys.exit(__doc__)
        entry = core.guard.holder()
        inside = os.environ.get(core.guard.HELD_ENV) == str(lab.WORK.resolve())
        if entry is not None and not inside and not arguments:
            sys.exit(f"lab {lab.lab_key()} is held by {core.guard.describe(entry)}: `down` "
                     "would pull it from under that run; --force tears it down anyway")
        lab.down()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
