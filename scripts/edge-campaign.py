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
            "stalls": (stalls.get("playhead") or 0) + (stalls.get("element_waiting") or 0),
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
              any(when > 25_000 and dead not in str(host) for when, host in hosts),
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
    moved = [when for when, host in hosts
             if when > 25_000 and str(host).split(":")[0] in far]
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
    moved = [item for item in hosts if item[0] > 22000 and drained_host not in str(item[1])]
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

ALBUM = [VIDEO] + [2001 + track for track in range(8)] + [3001 + track for track in range(8)]


def album_readers(ctx, seconds: int, **options) -> dict:
    return {stream: ctx.readers(f"s{stream}", stream, 1, seconds, **options)
            for stream in ALBUM}


def slot_refusals(names) -> dict:
    """Streams that each node refused for want of a playlist slot, from its
    log."""
    import re
    out = {}
    for name in names:
        text = lab.node_log(name)
        refused = sorted({int(item) for item in re.findall(
            r"stream_id\S*?=\S*?(\d+)[^\n]*every playlist slot holds a live stream", text)})
        if refused:
            out[name] = refused
    return out


@scenario("long-tail-one-stream-too-many", "audience",
          "A region carries 17 streams: 4K video and an album of eight tracks, lossless and "
          "HQ. A node has 16 playlist slots. What do the readers of each stream see?",
          "Sixteen streams play without a missing part and without a stall. The stream "
          "that does not fit is refused by name on the nodes that are full, and its "
          "readers are told at once that it is not there.",
          needs=["daw_album_tracks=8"])
def long_tail_full(ctx):
    time.sleep(8)
    jobs = album_readers(ctx, 40)
    ctx.wait_readers()
    played = [stream for stream, job in jobs.items()
              if job.summary.get("readers_ok") == 1 and job.summary.get("parts_missing") == 0
              and job.summary.get("longest_stall_ms", 1) == 0]
    absent = [stream for stream in ALBUM if stream not in played]
    refused = slot_refusals(lab.names())
    ctx.measure("streams_played_clean", played)
    ctx.measure("streams_not_played", {
        stream: {key: jobs[stream].summary.get(key) for key in (
            "readers_ok", "parts_received", "parts_missing", "stalls", "longest_stall_ms",
            "request_errors")} for stream in absent})
    ctx.measure("refused_for_want_of_a_slot", refused)
    ctx.check("sixteen streams play without a missing part or a stall", len(played) == 16,
              f"{len(played)} played clean; not clean: {absent}")
    named = {stream for streams in refused.values() for stream in streams}
    ctx.check("the stream that does not play is the one that the full nodes refuse by name",
              len(absent) == 1 and named == set(absent), f"not played {absent}; refused {refused}")
    for stream in absent[:2]:
        status, _head, _body = lab.curl("lon-dist-a", f"/live/{stream}/stream.m3u8", timeout=6)
        ctx.check(f"the entry point answers 404 for stream {stream} at once", status == 404,
                  f"HTTP {status}")
    ctx.gap = "a node holds 16 streams unless --playlist-slots is raised"


@scenario("long-tail-more-slots", "audience",
          "The same 17 streams with 24 playlist slots on every node. Do they all play, and "
          "can one edge serve a reader of every stream?",
          "Every stream plays without a missing part and without a stall, through the "
          "entry point and on one edge that holds all 17.",
          needs=["daw_album_tracks=8", "playlist_slots=24"])
def long_tail_slots(ctx):
    time.sleep(8)
    jobs = album_readers(ctx, 40)
    ctx.wait_readers()
    for stream, job in jobs.items():
        ctx.expect_clean(job, f"stream {stream}")
    ctx.measure("refused_for_want_of_a_slot", slot_refusals(lab.names()))
    core.idle_edges(30)
    pinned = {stream: ctx.readers(f"one-edge-s{stream}", stream, 1, 30, cache="lon-edge-1")
              for stream in ALBUM}
    ctx.at(60)
    held = lab.edge_view("lon-edge-1")["playlists"]
    ctx.wait_readers(*pinned.values())
    ctx.check("one edge holds all 17 playlists", len(held) >= 17, sorted(held, key=int))
    clean = [stream for stream, job in pinned.items()
             if job.summary.get("readers_ok") == 1 and job.summary.get("parts_missing") == 0
             and job.summary.get("longest_stall_ms", 1) == 0]
    ctx.check("every stream plays clean on the one edge", len(clean) == 17,
              f"{len(clean)} of 17; not clean: {[s for s in ALBUM if s not in clean]}")
    node = lab.read_cgroup(lab.cgroup_dir("lon-dist-a"))
    ctx.measure("distributor_memory_mb", round(node.get("memory_bytes", 0) / 1e6))
    ctx.measure("edge_memory_mb", round(lab.read_cgroup(
        lab.cgroup_dir("lon-edge-1")).get("memory_bytes", 0) / 1e6))


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
    lab.set_clock("lon-dist-a", 4)
    ctx.defer(lambda: lab.set_clock("lon-dist-a", None))
    time.sleep(8)
    jobs = [ctx.readers("via-skewed", AUDIO, 4, 30, entries=["lon-dist-a"]),
            ctx.readers("via-true", AUDIO, 4, 30, entries=["lon-dist-b"]),
            ctx.readers("video", VIDEO, 2, 30)]
    ctx.wait_readers()
    for job in jobs:
        ctx.expect_clean(job)
    report_experience(ctx, jobs)


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
        _status, _headers, body, _seconds = fetch(f"/live/{stream}/stream.m3u8")
        mine = hls_spec.parse_media(body)
        reported = {item.get("URI"): item for item in mine["rendition_reports"]}
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
            if not 0 <= behind <= 2:
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
              any(when > 25_000 and dead not in str(host) for when, host in hosts),
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
