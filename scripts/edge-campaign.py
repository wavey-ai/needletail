#!/usr/bin/env python3
"""The Needletail edge fault campaign.

Each scenario asks one question of the two-region lab of `edge_lab.py`,
states what must hold, injects one fault while readers play 4K video and
lossless audio, and writes one evidence file: what every reader saw (missing
parts, stalls and their length, time to the first part, delay from the
contributor), and what every node did (CPU, throttling, memory, descriptors,
sockets, drops).

  edge-campaign.py list [group ...]
  edge-campaign.py run [--out DIR] [--no-slow] [group or scenario ...]
  edge-campaign.py summary [DIR]
  edge-campaign.py down

A result is PASS, FAIL, or GAP: a failure that the scenario knows to be a
missing feature, with the name of the gap. Evidence is written under
target/edge-campaign/<run>/<scenario>/evidence.json.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import edge_lab as lab  # noqa: E402
import edge_campaign_core as core  # noqa: E402
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

for loss in (0.5, 2, 5):
    reader_link(
        f"reader-loss-{loss}pct",
        f"Does {loss}% random packet loss between reader and cache (20 ms away) stall a "
        "reader?",
        "No part is missing and no reader stalls with a 1 s buffer.",
        {"delay_ms": 20, "jitter_ms": 2, "loss": loss})

reader_link(
    "reader-burst-loss",
    "Does burst loss between reader and cache (Gilbert-Elliott: 1% to enter a bad state "
    "that loses 70%, 20% to leave it) stall a reader?",
    "No part is missing and no reader stalls with a 1 s buffer.",
    {"delay_ms": 20, "jitter_ms": 2, "burst": (1, 20, 70, 0.1)})

reader_link(
    "reader-reorder-duplicate",
    "Do reordering (25% of packets) and duplication (1%) between reader and cache stall a "
    "reader?",
    "No part is missing and no reader stalls with a 1 s buffer.",
    {"delay_ms": 20, "reorder": 25, "duplicate": 1})


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
          "Does a reader with a slow uplink (256 kbit/s up, unlimited down, 40 ms) play?",
          "No part is missing and no reader stalls with a 1 s buffer.")
def reader_asymmetric(ctx):
    up = lab.netem_spec(delay_ms=40, rate_kbit=256)
    down = lab.netem_spec(delay_ms=40)
    ctx.conditions["reader uplink"] = up
    ctx.conditions["reader downlink"] = down
    lab.set_tcp_per_packet(True, "london")
    lab.set_net(LON_READERS, "pub", "out", up)
    lab.set_net(LON_READERS, "pub", "in", down)
    jobs = play(ctx, 45, video=1, audio=2)
    ctx.wait_readers()
    report_experience(ctx, jobs)
    clean(ctx, jobs)


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
    jobs = play(ctx, 80, video=3, audio=8)
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
    @scenario(name, "node", question, criterion)
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
        lambda edge: lab.set_limits(edge, cpu_percent=100), "edge limited to 20% of a CPU",
        fault_at=14, heal_at=34, seconds=60)
    clean(ctx, jobs)


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
    lab.set_limits("lon-dist-a", cpu_percent=100)
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
        directory = lab.cgroup_dir(edge)
        current = int((directory / "memory.current").read_text()) // (1 << 20)
        ctx.measure("memory_mb_before", current)
        # Leave less than the copy needs: the next seconds of parts do not fit.
        lab.set_limits(edge, memory_mb=max(24, current - 20))
        ctx.conditions["memory limit of the edge, MB"] = max(24, current - 20)

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
        slow = True
        rest = []
        index = 0
        while index < len(arguments):
            if arguments[index] == "--out":
                out = pathlib.Path(arguments[index + 1])
                index += 2
            elif arguments[index] == "--no-slow":
                slow = False
                index += 1
            else:
                rest.append(arguments[index])
                index += 1
        directory = out or core.RUNS / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        directory.mkdir(parents=True, exist_ok=True)
        (core.RUNS / "latest").unlink(missing_ok=True)
        try:
            (core.RUNS / "latest").symlink_to(directory.resolve())
        except OSError:
            pass
        for item in select(rest, slow):
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
        lab.down()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
