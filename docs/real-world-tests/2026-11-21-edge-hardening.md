# 21 November 2026: edge hardening

This record covers a fault campaign against the regional LL-HLS cache: what
was tested, what broke, what was fixed, and what is still open. The brief was
that the earlier tests of the regional cache were far too gentle. Every
scenario here has a question, a pass criterion, and at the end a pass, a fix,
or a gap that is written down with its evidence.

The ranked list of what a production media edge has and this one lacks is in
[Media edge gaps](../media-edge-gaps.md). The design is in
[Regional LL-HLS edge cache fabric](../edge-cache-fabric.md).

## Result

| Lab | Scenarios | PASS | GAP | FAIL |
| --- | ---: | ---: | ---: | ---: |
| Local namespaces, run `local-20261121T0650Z-edge-campaign` | 83 | 60 | 14 | 9 |
| Linode, run `20261121T044850Z-edge-campaign` | 40 | 24 | 3 | 13 |

The campaign is not green, and the record does not call it green. A FAIL is a
scenario whose criterion did not hold and for which nothing was built; each is
named under [Still open](#still-open) with what was seen.

What the campaign found, in order of weight:

- **The earlier tests had not met a real player.** hls.js and ffmpeg could
  not play the audio streams at all, played 4K only from a warm cache, and
  did not survive the death of a cache. All of that is fixed: hls.js now
  plays 4K with no stall and no dropped frame in the local lab and from the
  nearby region on Linode, through the kill or the drain of its cache without
  a stall in the local lab, and through the loss of a whole region with 2.4
  to 2.8 s of stall in both. From an ocean away, with one rendition, it
  stalls 0.2 s at the start and 4 s when its cache is killed.
- **The cache network did not hold under loss, jitter or disorder.** 0.5%
  loss stalled a region, 2% stopped 4K, and 8 ms of jitter stopped 4K. Three
  causes: repair requests that cost more than the loss, the kernel's limits on
  IP fragments, and late packets counted as lost. With the fixes the local lab
  plays through 5% loss, jitter and disorder without a stall. On Linode 0.5%
  passes and 2% shows no 4K fault, with one incident on one edge in each of
  two runs.
- **A small node failed for everyone when it was asked for too much.** One
  stream too many took every stream down in turn; one audio reader too many
  stalled every reader of an edge; a crowd hurt the readers who were there.
  Each is now a refusal: by name for a stream, 429 with the other caches for
  a reader.
- **An edge that had lost its source, and a node with a wrong clock, each
  took readers and gave them nothing.** Both fixed.
- **Memory was not flat, and an edge would not have lasted a day.** Four
  causes, found by runs of hours and by reading the memory of the grown
  processes: a target duration that one long segment quadrupled, an
  allocator that kept freed 4K parts, a set of every part that an edge had
  ever received, and a packet sequence tracker that remembered every datagram
  after the first lost one. With all four fixed, 90 minutes of reader churn
  moves no node by more than 3.5 MB an hour. See [Endurance](#endurance).
- **5 ms parts are the limit of everything.** 60 audio readers for each vCPU
  of an edge, a contributor that carries 4K and four audio streams and not sixteen,
  220 kbit/s of uplink for each listener, no standard player. This is the
  largest open gap, and it is a design gap, not a fault.

## What was built, and how to use it

Ranked in [Media edge gaps](../media-edge-gaps.md), with what each is in
playlist terms. In short:

| Feature | How to use it |
| --- | --- |
| Playlists that every cache renders alike, from the stamps of the contributor: one Media Sequence Number, `PART-TARGET` and target duration on every cache | Always on. `av-contrib --fmp4-video-part-ms 200 --fmp4-video-segment-ms 2000` gives video its own part and segment duration beside 5 ms audio |
| Standby copy: a second edge holds each playlist that has readers | `av-mesh --playlist-copies 2` on distributors and edges (deploy setting `playlist_copies`) |
| Content steering: pathways for the alternates of a session, clones for new caches, order by standby copy | Always on for a session that was assigned (`?assign=1`) |
| Region failover: the last pathway of every reader is a cache of the neighbouring region | `av-mesh --failover-entry REGION=https://host:port` (deploy setting `region_failover`) |
| A ladder: renditions from the encoder, a Multivariant Playlist with all of them on every pathway, the whole ladder warm on an edge that has a reader of one, rendition reports, one date in all renditions | The encoder publishes each rendition as its own RTMP stream whose key is its stream number. `av-mesh --hls-rendition master:stream:peak:average:WIDTHxHEIGHT:millihertz` for each (deploy setting `hls_renditions`) |
| Admission by request cost: a session is charged for its requests as well as its bits | `av-mesh --egress-request-capacity` (requests a second for each worker) and `--http-worker-threads` |
| A full node refuses a new stream by name | `av-mesh --playlist-slots N` (default 16; deploy setting `playlist_slots`) |
| An edge that has heard no parent for 400 ms answers 503 with the other caches | Always on |
| The age of a cache advertisement is measured by receipt, not by two clocks | Always on |
| Loss-adaptive protection on the cache network; a late symbol is not a lost one | Always on |
| Kernel and allocator settings of a node | Deploy settings `node_sysctls` and `node_env`; the values are in `scripts/linode-small-mesh-deploy.py` and must be applied by any other deployment |
| The fault campaign, the LL-HLS rule checker, the player drivers, the joins tool | See [How to run it](#how-to-run-it) |

## How to run it

Everything runs from the `needletail` checkout with `av-mesh`, `av-contrib`
and `playlists` checked out beside it.

    # the lab: 13 nodes in namespaces, each with the limits of a Nanode
    scripts/edge_lab.py up
    scripts/edge_lab.py sources 3600
    scripts/edge_lab.py status
    scripts/edge_lab.py down

    # the campaign
    scripts/edge-campaign.py list
    scripts/edge-campaign.py run --out target/edge-campaign/run-1 --no-slow
    scripts/edge-campaign.py run --out target/edge-campaign/run-1 mesh-loss-2pct ladder
    scripts/edge-campaign.py summary target/edge-campaign/run-1

    # the same scenarios on real instances
    scripts/linode-small-mesh-lab.py up --topology deploy/linode-small-mesh/topology-regional-cache.json
    scripts/linode-small-mesh-lab.py watchdog --hours 3
    scripts/linode-small-mesh-deploy.py push $(scripts/linode-edge-campaign.py settings)
    scripts/linode-edge-campaign.py prepare
    scripts/linode-edge-campaign.py run
    scripts/linode-small-mesh-lab.py down

    # one playlist against the LL-HLS rules
    scripts/hls_spec.py lab lon-edge-1 1 --seconds 20

    # a real player (see scripts/players/FINDINGS.md)
    scripts/edge_lab.py sh lon-readers node scripts/players/hlsjs-play.mjs --url <master> --seconds 40

    # one versioned evidence file from a run
    scripts/edge-campaign-evidence.py --run-id ID --provider local --out FILE DIR

Each scenario writes `<out>/<scenario>/evidence.json`: the question, the
criterion, the conditions, the events with their times, every check with its
numbers, what each group of readers saw, and the CPU, memory, descriptors and
threads of every node.

## The lab

The local lab is one host. Each node is a network namespace with `eth0` on
the public network of its site and `eth1` on the private network of its
region, as a Linode has. The sites meet in a router namespace over links with
the delay of their distance (London 2 ms, Amsterdam 4 ms, New York 34 ms to
the router). Every link takes `tc netem` in each direction. Each service runs
in its own cgroup with one CPU, 1 GB and no swap, and with one runtime worker
thread, as on a Nanode. Nothing needs root.

Nodes: one contributor and one relay in London, one relay in Amsterdam, and
two distributors with four edges in London and two distributors with two
edges in New York. The sources are a 4K H.264 clip at 16.5 Mbit/s with AAC
(`tos24-16m.ts`) and one audio track as lossless FLAC and as HQ SoundKit Opus,
both in 5 ms parts. The ladder scenarios add 1080p, 720p and 360p encodes of
the clip with the same closed two-second group of pictures. The long-tail
scenarios use an album of two tracks, which makes five streams. The
many-stream capacity scenarios use albums of four, six and eight tracks: 9, 13
and 17 streams.

An edge is set to 100 Mbit/s of egress, so that admission is reached with a
few 4K readers. The capacity scenarios lift that limit to find the ceiling of
the node itself.

A reader is `needletail-reader`, a player model: it asks for parts ahead,
keeps a 1 s buffer, counts a stall when the buffer runs dry, follows the
steering manifest, and reports the delay from publication to arrival of every
part. Real players are hls.js 1.7.4 in headless Chrome for Testing 158 and
ffmpeg 9.0.2.

The Linode lab is `deploy/linode-small-mesh/topology-regional-cache.json`: 13
Nanodes (1 vCPU, 1 GB) in London, Amsterdam and Newark with the same roles, a
4-vCPU load host in London and a Nanode load host in Newark. Readers reach the
caches at their public addresses. The cache network is the private VLAN of
each region. Faults are injected on the instances with `systemctl` and `tc`.

## Method notes

These things made earlier passes of this campaign wrong. They are recorded so
that the next run does not repeat them.

- **`tc qdisc replace` keeps the netem options that it is not given.** A rate
  cap or a reorder setting of one scenario stayed on the link for every
  scenario after it. Passes 1 to 3 are not valid after their first rate-cap or
  reorder scenario. The lab now deletes and adds the qdisc and reads back what
  the link carries (`edge_lab.apply_netem`).
- **The test host has 16 cores with two threads each.** Three labs, a build
  and two browsers at once made every process about 60% more expensive, and
  the contributors of all three labs dropped parts for 2.5 minutes. A campaign
  pass now runs with nothing else on the host. Passes 4 to 8 ran beside
  other work or on earlier binaries and are not used for the table.
- **A shaped link needs the queue of a real link.** The lab gave every link
  a queue of 50,000 packets. On a link shaped to 8 Mbit/s that is more than a
  minute of traffic: a player saw delay grow and no loss. A link with a rate
  now has a queue of a quarter of a second. The ladder runs on shaped links
  before 09:25 UTC are not used.
- **Jitter in `netem` reorders packets** on this kernel, with or without a
  FIFO child. A scenario that asks for jitter also asks for disorder.
- **Browser scenarios need a quiet host.** hls.js decodes 4K in software.
  The player runs against Linode that were made during a local pass stalled
  for seconds and were made again on a quiet host.
- **On Linode the audio source must not share the vCPU of the contributor.**
  The first long-tail runs there measured the source, not the contributor.
- **The namespace lab had one clock.** The clock scenarios ran on Linode
  only until `scripts/clock_shift.c`: every service of the local lab is now
  started with that library preloaded, and `edge_lab.set_clock` steps the
  wall clock of one service while it runs. The monotonic clock is not moved.
  Local scenarios from 12:40 UTC ran with the library loaded.
- **A Linode has swap.** A service that is squeezed below its memory slows
  down; in the local lab, without swap, the kernel kills it. The
  out-of-memory scenario means different things in the two labs.
- **The load host can be the limit.** A reader of 5 ms audio parts sends 200
  requests a second. The Linode driver records how busy the load host was
  during each group of readers, and the table says where it was the limit.
- **The reader model was less patient and less clever than a player.** See
  [the reader model](#the-reader-model). Results for a failure upstream of the
  region were several times worse than what hls.js sees until this was fixed.

A scenario is PASS when every check holds, GAP when a check fails and the
scenario names the missing feature that explains it, and FAIL otherwise.

## What broke, and what was done

Each entry says what was seen, where, what was changed, and which test now
holds it. Commits are on the branches in [Revisions](#revisions).

### Playlists and players

1. **No standard player could play the audio streams, and the 4K stream only
   on a warm cache.** hls.js and ffmpeg, first run. The twelve defects are in
   [`scripts/players/FINDINGS.md`](../../scripts/players/FINDINGS.md): no
   complete segment in an audio playlist, a different Media Sequence Number,
   `PART-TARGET` and target duration on each cache, all three changing during
   the stream, a playlist of one part from a cold cache, a key frame in the
   middle of a part. Fix: the contributor stamps each part with its place in
   the playlist and every cache renders the playlist from the stamps; video
   has its own part duration; the reload rules of the draft; a cold cache
   holds the request until its copy is playable. `av-contrib` `fe8ef4f`,
   `av-mesh` `bb32fb8`, `32a819f`. Tests:
   `stamped_parts_give_every_cache_the_same_playlist`,
   `a_stamped_stream_keeps_six_whole_segments_whatever_one_part_lasts`,
   `a_reload_that_never_arrives_is_answered_503`, the `hls_stamp` tests.
2. **hls.js did not survive the death of its cache.** Two of three runs never
   recovered and the third stalled for 1.5 s: the next pathway had no copy.
   Fix: a standby copy on a second edge, pathway clones in the steering
   manifest. `av-mesh` `ef1e27e`. Test:
   `one_edge_without_readers_takes_the_standby_copy`; scenario
   `player-hlsjs-edge-kill`.
3. **The first alternate of a reader was not the edge with the standby
   copy.** Linode, `edge-freeze-resume`: three 4K readers of a frozen edge
   tried two cold edges and the neighbouring region and stalled for 2.4 to
   2.8 s. A player that has reloaded the steering manifest knows the standby
   edge; one that has not, or that does not use steering, went by the order of
   its session. Fix: the caches after the first are in the standby order of
   the playlist. `av-mesh` `097e02c`. Test:
   `the_first_alternate_of_every_reader_is_the_standby_edge`.
4. **A playlist ended with a short part in an open segment, and hinted a part
   that it then skipped.** Linode, `spec-playlists`, one reload in thirty. Fix:
   the segment of a short part is complete at once; the hint names the part
   after the last listed one. `av-mesh` `8dd7b80`. Tests:
   `a_short_part_completes_its_segment_at_once`,
   `a_part_is_listed_only_when_its_playlist_entry_is_known`.
5. **A stream that joins in the middle of a group of pictures declared a
   target duration of 4 s for good; one long first part widened `PART-TARGET`
   for good.** Linode and the ladder lab, over RTMP. Fix: the first segment
   ends at the first key frame; the declared part target and target duration
   follow a duration that recurs, not one outlier. `av-contrib` `9c84820`,
   `9a9021b`, `5f939a5`; `av-mesh` `bbd5568`. Tests:
   `a_stream_that_begins_inside_a_group_ends_its_first_segment_at_the_key_frame`,
   `one_long_part_does_not_widen_the_part_target`,
   `one_long_segment_does_not_widen_the_target_duration`,
   `one_long_segment_does_not_raise_the_target_duration_of_a_stamped_stream`.
6. **The renditions of a ladder had dates 0.14 to 0.26 s apart.** Each
   cache dated a playlist by the arrival of its first part. hls.js finds its
   place in a new rendition by date, landed a part away, and stalled for
   seconds at each switch. Fix: a rendition of a ladder carries the date of
   its Media Sequence Number times the segment duration, the same on every
   cache. `av-mesh` `725b4a3`. Test:
   `the_renditions_of_a_ladder_have_the_date_of_their_segment_number`.
   Verified in the local lab only: the Linode lab ran the commit before it.
7. **Audio over RTMP did not decode.** hls.js on the ladder lab:
   `PIPELINE_ERROR_DECODE` on the first audio packet. Every AAC frame had lost
   its first two bytes. The fix exists in `boxer` (`ee90c8f` on
   `fix-rtmp-aac-frame-start`, version 0.1.1, not published). The contributor
   build named it with a `[patch]`, and Cargo did not use it, because the lock
   file pins 0.1.0: `cargo update -p wavey-boxer --precise 0.1.1` with the
   patch makes it take effect. Open until `wavey-boxer` 0.1.1 is published: a
   plain build of `av-contrib` still has broken RTMP audio. The SRT path was
   never affected.

### The cache network

8. **0.5% packet loss on the private network of a region stalled every reader
   of the region.** Both parents answered a hole with everything from the hole
   to the head, which took the CPU of the distributor and starved its ingest.
   Fix, in `playlists` `cfdf904` and `av-mesh` `f16ec95`: each node measures
   and reports the loss of each peer, the sender protects an object by the
   measured loss, a hole is filled by one parent with the hole, an object is
   encoded once for all children. Tests: the mesh tests of `playlists`
   (`--all-features`); scenarios `mesh-loss-*`.
9. **2% loss on the cache network stopped 4K on every edge, and audio played
   on.** The kernel, not the mesh. A 4K part travels in symbols of up to
   16 kB, which IP fragments. A datagram that has lost a fragment waits 30 s
   for it, and when 4 MB of fragments wait, the kernel drops every new
   fragment. One edge counted 397,330 failed reassemblies against 602,259
   good ones, and 5,446 timeouts: the rest were dropped at the limit. Audio
   symbols fit one packet. Fix: `net.ipv4.ipfrag_time=1` and
   `net.ipv4.ipfrag_high_thresh=67108864` on every node (deploy setting
   `node_sysctls`). After it the same edge counted 11,122 failed
   reassemblies, and 2% and 5% loss play without a stall or a missing part
   in the local lab. Scenarios `mesh-loss-2pct`, `mesh-loss-5pct`,
   `mesh-burst-loss`. The dependence on IP fragmentation is itself a gap: see
   [Media edge gaps](../media-edge-gaps.md).
10. **8 ms of jitter on the cache network, with no loss, made the distributors
   send 3.6 repair symbols for each source symbol, and no edge could take a 4K
   copy.** Jitter reorders packets. A receiver counted a source symbol that
   was missing when its block decoded as lost, and the senders answered the
   reported loss with repair. Fix: the loss of a block that needed repair is
   counted 250 ms after it decoded. `playlists` `e8c3001`. Test:
   `a_source_symbol_that_arrives_late_is_not_counted_as_loss`. That was half
   of it. The kernel also gives up a datagram when more than 64 other
   fragments of the same sender arrive between two of its fragments, which
   any disorder does at 4K rates: `net.ipv4.ipfrag_max_dist=0` on every node.
   Scenarios `mesh-delay-jitter`, `mesh-reorder-duplicate`: both pass.
11. **One stream more than a node has slots took every stream down in turn.**
   A node has 16 playlist slots, and the chunk cache gave a new stream the slot
   of a live one. Fix: a full node refuses the new stream by name; a stream
   that has been idle for 10 s gives up its slot; `--playlist-slots` sets the
   number. `av-mesh` `29393d3`. Test:
   `a_node_holds_as_many_streams_as_it_has_playlist_slots`; scenarios
   `long-tail-*`. On Linode, five streams on nodes of four slots: four play
   without a missing part and the fifth is refused by name and answered 404
   at once; with eight slots all five play.
12. **An edge with no free slot gave the reader of one more playlist
   nothing.** Local lab, the same scenario: one edge was given the readers of
   four streams, holds three for readers with four slots, and took no copy of
   the fourth. The reader was not refused and found no playlist, although the
   other edges of its session had room. Fix: the edge answers 429 with the
   other caches and logs the stream. `av-mesh` `91a1659`. Test:
   `an_edge_without_a_free_playlist_slot_sends_the_reader_away`. After it the
   reader is turned away once and plays from the next edge without a missing
   part. Verified in the local lab; the Linode lab was already deleted.
13. **An edge that had lost its source held every request until the reader
   gave up.** `mesh-partition-edge-heals`, `distributors-both-kill`: a 4K
   reader waited 11 s and more before it tried another cache. Fix: the nodes
   of the cache network say hello every 100 ms, and an edge that has heard no
   parent for 400 ms answers 503 with the other caches. `playlists` `fc1631c`,
   `av-mesh` `edebdcb`. Tests:
   `an_edge_knows_how_long_its_parents_have_been_silent`,
   `an_edge_that_has_lost_its_parents_does_not_hold_a_request`. After it: no
   stall when the private link of an edge is cut, and readers play from the
   neighbouring region 1.4 s (audio) to 2.8 s (4K) after a region loses both
   distributors.
14. **A clock that was 4 s wrong on one node broke assignment.** Linode,
   `clock-skew-*`. An entry point that was 4 s ahead took every edge for
   stale and answered every reader 429. An edge that was 4 s behind was never
   given a reader. Fix: the age of an advertisement is the time since this
   node received a new one. `av-mesh` `ee545f4`. Test:
   `the_age_of_an_advertisement_does_not_depend_on_the_clock_of_its_sender`.
   After it both are served. The clock scenarios now run in the local lab
   too: a preloaded library gives one service its own wall clock
   (`scripts/clock_shift.c`). What is left is not assignment: a relay object
   carries its deadline as a time of day, one second after publication on the
   clock of the contributor. A distributor whose clock is 4 s ahead dropped
   200,732 symbols as expired in 30 s and held nothing; its edges had every
   4K part from the other distributor and missed a quarter of the 5 ms audio
   parts. A contributor whose clock is 2 s behind is dropped by both relays.
   See [Still open](#still-open).

### Admission and crowds

15. **Admission counted bit rate, and an edge took more audio readers than its
   CPU serves.** Lossless audio in 5 ms parts is 200 requests a second for
   each reader. Past the ceiling every reader of the edge stalls, not only the
   last ones. Fix: a session is charged the larger of its bit rate and its
   share of the request capacity of the node. `av-mesh` `a18076a`. Tests in
   `edge_admission.rs`; scenario `admission-refuses-cleanly`.
16. **A crowd of 2,000 readers in one second hurt the readers who were already
    there.** The entry point decoded the whole window for each Multivariant
    Playlist; cold copies were over-admitted; the HTTP work starved the cache
    network of the node. Fix: the init scan is remembered, an entry point
    counts what it has just assigned, HTTP runs on its own runtime at lower
    priority. `av-mesh` `be37600`. Scenario `flash-crowd`.

### The contributor

17. **The contributor grew by 2 MB/s and was killed at 1 GB after seven
    minutes** of a 16 Mbit/s SRT stream: an ingest ring of 32,768 slots that
    never wrapped. Fix: 256 slots. `av-contrib` `79dd43d`.
18. **A pause of 120 ms made the contributor drop parts for as long as the
    host stayed busy.** A publication worker had 32 places for 405 parts a
    second. Fix: 256 places. `av-contrib` `8d056af`. Seen on three
    contributors at once when the test host was oversubscribed: see
    [Method notes](#method-notes).
19. **One long segment raised the target duration from 2 s to 8 s for the
    life of the stream**, and every node then held 80 s of 4K instead of 20 s:
    190 MB more on each relay, distributor and edge. Found by the endurance
    run, as a step in the memory of every node. Fixed with entry 5.

### Endurance

20. **An edge under reader churn grew from 110 MB to 310 MB in 80 minutes.**
    `malloc_trim` on the live process gave 200 MB back: glibc raises its mmap
    threshold to the largest block that was freed, so freed 4K parts stay in
    the heap. Fix: a fixed mmap threshold of 64 kB and two arenas, in the
    environment of every service (deploy setting `node_env`). That was not
    all of it: a two-hour run with the settings still grew, and entries 21
    and 22 are why.
21. **An edge remembered every part that it had ever received.** Two-hour
    churn run, after entry 20: one edge went from 79 MB to 245 MB and held no
    playlist at the end. The allocator reported 179 MB in use, 4 MB of it in
    its arenas. The largest live block, read from the memory of the process,
    was a hash set of 71 MB whose entries were a stream number and a part
    number: the set of slots that the cache mesh has taken from a peer. Only
    the copy loop prunes it, and an edge has no copy loop. 17 bytes for each
    part received, without limit: about 25 MB an hour for one 4K stream and
    two audio streams. Fix: an edge keeps no such set, and a node whose copy
    loop has no child prunes it. `playlists` `483765e`. Tests:
    `an_edge_does_not_remember_the_slots_that_it_receives`,
    `remote_slots_that_the_cache_has_dropped_are_forgotten`.
22. **Every receiver of a UDP-FEC flow remembered every datagram after the
    first that was lost or out of order.** The same run: the edge held two
    more hash sets of 42 MB each, and so did the contributor, which had gone
    from 77 MB to 170 MB. Their entries were 24-bit numbers: packet sequence
    numbers. The sequence tracker of `raptorq-datagram-fec` 0.1.6 waits for
    the oldest missing number and keeps every later one until it comes. A
    datagram that is lost never comes. Nor does one in the order 10, 14, 12,
    11, 13: the tracker then waits for 12, which it has had. From then on it
    stored 5 bytes for each datagram of the flow, and did 4,096 set inserts
    for each datagram as well. Fix: the tracker holds the numbers that are
    missing within 4,096 of the newest and nothing else. `raptor-fec`
    `68a1840` on the branch `bounded-sequence-tracker`; `av-mesh` `6e657a7`
    and `av-contrib` `12abbef` name it by revision. Tests:
    `a_lost_datagram_does_not_make_the_tracker_remember_every_later_one`,
    `datagrams_that_arrive_out_of_order_leave_nothing_behind`,
    `a_long_outage_counts_what_was_lost_and_tracks_only_the_newest_gap`.
    After both, 90 minutes of the same churn: no node grows by more than
    3.5 MB an hour, where one edge had gone from 79 MB to 244 MB in that
    time. See [Endurance](#endurance).

### The reader model

`needletail-reader` stands for a player in most scenarios, so its faults are
test faults. It asked for one part at a time and fell behind 5 ms parts on a
2 ms round trip; it now asks ahead. It chose its next cache without the
steering manifest. It let a reload that it had sent to its old cache time out
against its new one, and left a warm cache for it. When parts were lost
upstream it waited for each in turn, 1.5 s on each cache and 6 s before it
gave one up: a 4K reader stalled for 35 s after an outage of 10 s. A player
plays what the playlist lists, and the reader now does. `av-mesh` `151eba7`,
`e532d37`, `097e02c`, `ed8435f`, `0eef454`.

That last fault hid a real gap. The playlist says nothing about a part that
is lost for good: see rank 8 of [Media edge gaps](../media-edge-gaps.md).

## Scenario tables

PASS: every check held. GAP: a check failed and the scenario names the
missing feature that explains it. FAIL: a check failed for another reason,
which the last two columns say. The full checks, measurements and node
figures of each scenario are in the evidence files.

### Local lab

Run `local-20261121T0650Z-edge-campaign`: pass 9 of the campaign, alone on
the host, with the reruns of 08:57, 09:25 and 12:40 UTC for the scenarios
that were changed or fixed after it began, and the churn runs. Where a scenario ran more than once the last
run counts, and the evidence file lists the earlier results.

| Scenario | Result | What the run showed | What was fixed | What is left |
| --- | --- | --- | --- | --- |
| `reader-delay-20ms` | PASS | 9 of 9 readers served, 0 of 53946 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Reader asks ahead, and no more than 2 s ahead | - |
| `reader-delay-80ms` | PASS | 9 of 9 readers served, 0 of 52827 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Reader asks ahead, and no more than 2 s ahead | - |
| `reader-delay-150ms` | PASS | 9 of 9 readers served, 0 of 52308 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Reader asks ahead, and no more than 2 s ahead | - |
| `reader-delay-300ms` | PASS | 9 of 9 readers served, 0 of 68267 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Reader asks ahead; stays on a live cache | A far viewer needs a lower rendition to start on: use a ladder |
| `reader-loss-0.5pct` | PASS | 9 of 9 readers served, 0 of 53840 parts missing, 1 readers stalled (longest 133 ms), 0 moves | - | - |
| `reader-loss-2pct` | PASS | 9 of 9 readers served, 0 of 53724 parts missing, 3 readers stalled (longest 253 ms), 0 moves | - | - |
| `reader-loss-5pct` | PASS | 9 of 9 readers served, 0 of 53564 parts missing, 3 readers stalled (longest 224 ms), 0 moves | - | - |
| `reader-burst-loss` | PASS | 9 of 9 readers served, 0 of 53791 parts missing, 2 readers stalled (longest 155 ms), 0 moves | - | - |
| `reader-reorder-duplicate` | GAP | video: no part is missing: 35 missing of 343; video: the readers stay on their cache: 1 moves, 0 refusals | - | 4K over one TCP connection under heavy reordering needs a lower rendition: see ladder-hlsjs-reorder |
| `reader-partition-heals` | PASS | 6 of 6 readers served, 1 of 40356 parts missing, 6 readers stalled (longest 10980 ms), 24 moves | Reader follows the playlist past lost parts | - |
| `reader-rate-cap` | GAP | the capped video reader is given a playlist that fits its link: 19 stalls, 18657 ms stalled, 0 parts missing, max behind 21749 ms | - | The reader model does not choose a rendition: see ladder-hlsjs-rate-cap |
| `reader-asymmetric` | GAP | audio: no stall with a 1000 ms buffer: 5 stalls in 2 readers, longest 3903 ms, total 7694 ms | - | One request for each 5 ms part costs about 220 kbit/s of uplink: grouped parts (gap 9) |
| `mesh-loss-0.5pct` | PASS | 9 of 9 readers served, 0 of 54438 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Loss-adaptive protection; one parent fills a hole | - |
| `mesh-loss-5pct` | PASS | 9 of 9 readers served, 0 of 54259 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Loss-adaptive protection; fragment reassembly settings | - |
| `mesh-partition-edge-heals` | PASS | 14 of 14 readers served, 0 of 91192 parts missing, 0 readers stalled (longest 0 ms), 4 moves | An edge that has lost its parents answers 503 at once | - |
| `mesh-rate-cap-edge` | PASS | 9 of 9 readers served, 0 of 66431 parts missing, 0 readers stalled (longest 0 ms), 3 moves | - | An edge whose private link is narrower than its playlists is not taken out of assignment |
| `mesh-partition-distributor` | PASS | 9 of 9 readers served, 0 of 60489 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | - |
| `mesh-burst-loss` | FAIL | video: no part is missing: 22 missing of 644 | Loss-adaptive protection; fragment reassembly settings | 2 to 3% of the 4K parts are not delivered when bursts lose 70% of the packets |
| `mesh-reorder-duplicate` | PASS | 9 of 9 readers served, 0 of 54114 parts missing, 0 readers stalled (longest 0 ms), 0 moves | A late source symbol is not a lost one; fragment disorder limit off | - |
| `mesh-flap-edge` | FAIL | video: no stall with a 1000 ms buffer: 2 stalls in 2 readers, longest 2103 ms, total 4204 ms | An edge that has lost its parents answers 503 at once | A 4K reader can stall once for 0.6 to 2.1 s: the next edge holds its request |
| `mesh-loss-2pct` | PASS | 9 of 9 readers served, 0 of 54421 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Loss-adaptive protection; fragment reassembly settings | Linode: one incident on one edge in each of two runs; local clean |
| `mesh-delay-jitter` | PASS | 9 of 9 readers served, 0 of 54347 parts missing, 0 readers stalled (longest 0 ms), 0 moves | A late source symbol is not a lost one; fragment disorder limit off | - |
| `wan-loss-3pct` | GAP | video: no part is missing: 122 missing of 300; video: no stall with a 1000 ms buffer: 2 stalls in 2 readers, longest 1198 ms, total 2392 ms | - | Wide-area protection is sized for a configured loss; lost parts are not signalled (gap 8) |
| `wan-burst-loss` | PASS | 6 of 6 readers served, 0 of 35942 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | Wide-area protection is sized for a configured loss; lost parts are not signalled (gap 8) |
| `wan-jitter` | PASS | 6 of 6 readers served, 0 of 36327 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | Wide-area protection is sized for a configured loss; lost parts are not signalled (gap 8) |
| `wan-reorder-duplicate` | PASS | 6 of 6 readers served, 0 of 35888 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | Wide-area protection is sized for a configured loss; lost parts are not signalled (gap 8) |
| `wan-partition-region-heals` | PASS | 6 of 6 readers served, 10428 of 48218 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Reader stays on a live cache whose stream has stopped | The region has no media for the length of the cut |
| `contributor-uplink-loss` | GAP | video: no part is missing: 306 missing of 339; video: no stall with a 1000 ms buffer: 10 stalls in 3 readers, longest 2441 ms, total 11787 ms | - | Wide-area protection is sized for a configured loss; lost parts are not signalled (gap 8) |
| `wan-loss-1pct` | PASS | 6 of 6 readers served, 0 of 36279 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | Wide-area protection is sized for a configured loss; lost parts are not signalled (gap 8) |
| `edge-freeze-resume` | PASS | 14 of 14 readers served, 0 of 91268 parts missing, 0 readers stalled (longest 0 ms), 9 moves | Alternates in standby order; reader ignores a reload to a cache that it has left | - |
| `edge-rolling-restart` | PASS | 11 of 11 readers served, 0 of 176571 parts missing, 0 readers stalled (longest 0 ms), 23 moves | - | A 4K reader that is drained to an edge that has just started can stall for under half a second |
| `distributor-freeze` | PASS | 12 of 12 readers served, 0 of 81172 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | - |
| `relay-primary-freeze` | GAP | video: no part is missing: 3 missing of 799; video: no stall with a 1000 ms buffer: 3 stalls in 3 readers, longest 1717 ms, total 4285 ms | Reader follows the playlist past lost parts | The parts in flight are lost and not signalled (gap 8) |
| `relay-secondary-kill` | PASS | 12 of 12 readers served, 0 of 85593 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Reader follows the playlist past lost parts | The parts in flight are lost and not signalled (gap 8) |
| `relay-primary-kill-ny` | GAP | video: no part is missing: 3 missing of 803; video: no stall with a 1000 ms buffer: 3 stalls in 3 readers, longest 1495 ms, total 4052 ms | Reader follows the playlist past lost parts | The parts in flight are lost and not signalled (gap 8) |
| `distributors-both-kill` | PASS | 9 of 9 readers served, 3479 of 71624 parts missing, 9 readers stalled (longest 3447 ms), 36 moves | Lost-parents answer; pathway in the neighbouring region | Readers lose the seconds until they play from the neighbouring region |
| `distributors-both-freeze` | PASS | 9 of 9 readers served, 2269 of 72334 parts missing, 9 readers stalled (longest 3384 ms), 36 moves | Lost-parents answer; pathway in the neighbouring region | Readers lose the seconds until they play from the neighbouring region |
| `region-lost` | PASS | 6 of 6 readers served, 779 of 56444 parts missing, 6 readers stalled (longest 1062 ms), 24 moves | Pathway in the neighbouring region | - |
| `contributor-freeze` | FAIL | video: every reader plays again within 3 s of the end of the fault: the last stall ended at 24.3 s; fault from 15.0 s to 20.0 s; longest stall 4101 ms; 6 parts missing; audio: every reader plays again within 3 s of the end of the fault: the | Reader stays on a live cache whose stream has stopped; larger publication queue | The contributor wakes with a backlog and needs about 4 s to be live again |
| `contributor-kill-restart` | PASS | 13 of 13 readers served, 0 of 21941 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Segment numbers continue above the earlier ones | One contributor is one point of failure; no EXT-X-DISCONTINUITY (gap 11) |
| `distributor-cpu-starved` | PASS | 9 of 9 readers served, 0 of 65376 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | - |
| `edge-out-of-memory` | PASS | 12 of 12 readers served, 0 of 85632 parts missing, 0 readers stalled (longest 0 ms), 4 moves | Allocator settings | On an instance with swap the squeezed edge slows instead of dying |
| `edge-restart-no-drain` | PASS | 11 of 11 readers served, 0 of 128609 parts missing, 0 readers stalled (longest 0 ms), 18 moves | Readers follow the steering manifest | - |
| `edge-cpu-starved` | GAP | audio: no stall with a 1000 ms buffer: 4 stalls in 1 readers, longest 69 ms, total 226 ms | - | A starved edge is not taken out of assignment until it falls behind |
| `edge-kill` | PASS | 12 of 12 readers served, 0 of 85840 parts missing, 0 readers stalled (longest 0 ms), 4 moves | Standby copy; alternates in standby order | - |
| `distributor-kill` | PASS | 12 of 12 readers served, 0 of 86286 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | - |
| `relay-primary-kill` | GAP | video: no part is missing: 30 missing of 758 | Reader follows the playlist past lost parts | The parts in flight are lost and not signalled (gap 8) |
| `clock-skew-edge` | PASS | 6 of 6 readers served, 0 of 20188 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Age of an advertisement by receipt | - |
| `clock-skew-distributor` | GAP | via-skewed: no part is missing: 7000 missing of 23447; via-true: no part is missing: 5250 missing of 23331 | Age of an advertisement by receipt: readers are assigned and served | The deadline of a relay object is a time of day: the skewed distributor drops every object |
| `clock-skew-contributor` | PASS | 6 of 6 readers served, 0 of 47804 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | - |
| `clock-skew-contributor-behind` | GAP | video: no part is missing: 204 missing of 364; audio: no part is missing: 16260 missing of 31688 | - | The deadline of a relay object is a time of day: the relays drop every object of a contributor whose clock is behind |
| `capacity-edge-4k` | PASS | no stall up to 64 readers of one edge (edge CPU 24.1%) | - | The instance's network, about 1 Gbit/s, is the limit, not the CPU |
| `capacity-edge-flac` | PASS | no stall up to 40 readers of one edge (edge CPU 67.1%); at 60 readers 33 stall | Admission by request cost | 5 ms parts cost one request each: about 60 audio readers for each vCPU (gap 9) |
| `capacity-edge-opus` | PASS | no stall up to 60 readers of one edge (edge CPU 83.0%); at 80 readers 80 stall | Admission by request cost | 5 ms parts cost one request each: about 60 audio readers for each vCPU (gap 9) |
| `admission-refuses-cleanly` | PASS | 142 of 270 readers served, 0 of 742723 parts missing, 0 readers stalled (longest 0 ms), 7064 moves | Admission by request cost | - |
| `capacity-scales-with-edges` | PASS | two edges admitted 64 readers, four admitted 120: ratio 1.88 | Admission by request cost | - |
| `capacity-entry-joins` | PASS | every join answered in time up to 800 a second (p99 26.7 ms, entry CPU 65.0%) | Entry point remembers the init scan | Ceiling not reached: the load tool falls behind first |
| `capacity-seventeen-streams` | GAP | CPU by role {'contributor': 85.5, 'relay': 19.6, 'distributor': 57.5, 'edge': 71.6}; streams that played clean: 0 of 17 | - | The contributor is the limit: one vCPU does not carry sixteen audio streams of 5 ms parts beside 4K (gap 9) |
| `capacity-nine-streams` | GAP | CPU by role {'contributor': 73.7, 'relay': 42.9, 'distributor': 83.7, 'edge': 27.7}; streams that played clean: 0 of 9 | - | The contributor falls behind between five and nine streams of 5 ms parts (gap 9) |
| `capacity-thirteen-streams` | GAP | CPU by role {'contributor': 78.0, 'relay': 30.5, 'distributor': 84.0, 'edge': 28.0}; streams that played clean: 0 of 13 | - | The contributor falls behind between five and nine streams of 5 ms parts (gap 9) |
| `player-hlsjs-edge-kill` | PASS | hls.js: 53.83 s of media in 55.018 s, 0 ms stalled, 0 of 1289 frames dropped | Standby copy; pathway clones | - |
| `player-hlsjs-link-cut` | PASS | hls.js: 55.834 s of media in 70.016 s, 12673.6 ms stalled, 0 of 1346 frames dropped | - | - |
| `player-hlsjs-region-lost` | PASS | hls.js: 64.261 s of media in 70.015 s, 2382.3 ms stalled, 0 of 1530 frames dropped | Pathway in the neighbouring region | - |
| `player-ffmpeg-4k` | PASS | 33.79 s of media in 33.59 s after the first frame (speed 1.006), no stall over 500 ms | Stamps; cold copy held until playable | - |
| `player-hlsjs-cache-kill` | PASS | hls.js: 58.691 s of media in 60.015 s, 0 ms stalled, 0 of 1404 frames dropped | Standby copy; pathway clones | From an ocean away the move to a new connection at 16.5 Mbit/s stalls for 4 s: a ladder is the remedy |
| `player-hlsjs-drain` | PASS | hls.js: 52.318 s of media in 55.016 s, 0 ms stalled, 0 of 1261 frames dropped | - | - |
| `player-hlsjs-4k-second-region` | PASS | hls.js: 43.213 s of media in 45.015 s, 0 ms stalled, 0 of 1041 frames dropped | - | - |
| `spec-playlists` | PASS | 2 of 2 readers served, 0 of 8171 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Stamps; reload rules; segment complete at its short last part; hint after the last listed part | No delta updates, no gap signalling |
| `player-hlsjs-4k` | PASS | hls.js: 40.507 s of media in 45.015 s, 0 ms stalled, 0 of 978 frames dropped | Playlists that a player accepts | A viewer far from the region needs a lower rendition |
| `flash-crowd` | FAIL | the readers that were there: no stall with a 1000 ms buffer: 18 stalls in 4 readers, longest 31 ms, total 181 ms; every reader that the region admitted played without a stall: 56 of 142 stalled, 96791 ms in all | HTTP on its own runtime; pending assignments; init scan | An edge is sent more readers than it admits through the alternates of full edges. On Linode the load host could not drive the admitted readers |
| `slow-and-stuck-readers` | PASS | 14 of 14 readers served, 0 of 73009 parts missing, 3 readers stalled (longest 292 ms), 6 moves | - | - |
| `mixed-edge` | PASS | 24 of 24 readers served, 0 of 178955 parts missing, 0 readers stalled (longest 0 ms), 16 moves | Admission by request cost | - |
| `long-tail-one-stream-too-many` | PASS | 4 of 5 readers served, 0 of 23817 parts missing, 0 readers stalled (longest 0 ms), 1 moves | A full node refuses a new stream by name; an edge with no free slot sends the reader to another edge | - |
| `long-tail-more-slots` | PASS | 10 of 10 readers served, 0 of 55352 parts missing, 0 readers stalled (longest 0 ms), 0 moves | --playlist-slots | - |
| `ladder-playlists` | PASS | 1 of 1 readers served, 0 of 224 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Ladder; rendition reports; renditions numbered from one instant; one date in every rendition | - |
| `ladder-hlsjs-browser-cap` | PASS | hls.js: 59.38 s of media in 60.014 s, 0 ms stalled, 0 of 1402 frames dropped | Ladder of four renditions; one date in every rendition | From an ocean away, on the commit before the date fix, it stalled 5.6 s |
| `ladder-hlsjs-reorder` | FAIL | player: hls.js played, with at most 1000 ms of stall in all: stalled: 7656.7 ms stalled, 51.436 s of media in 60.018 s; errors ['bufferSeekOverHole', 'bufferStalledError', 'bufferSeekOverHole', 'bufferStalledError', 'bufferNudgeOnStall', 'b | Ladder of four renditions; one date in every rendition | hls.js at one-second latency stalls on a shaped link with little room above the rendition (gap 3) |
| `ladder-hlsjs-browser-steps` | FAIL | 2160p again within 25 s of the recovery: 1080p at 70 s | Ladder of four renditions; one date in every rendition | Local: no stall, 2160p again after 28 s where the criterion is 25 s |
| `ladder-hlsjs-rate-cap` | FAIL | capped: hls.js played, with at most 500 ms of stall in all: stalled: 3139.9 ms stalled, 55.901 s of media in 60.02 s; errors ['bufferSeekOverHole', 'bufferStalledError', 'bufferNudgeOnStall', 'bufferSeekOverHole']; capped: no frame was drop | Ladder of four renditions; one date in every rendition | hls.js at one-second latency stalls on a shaped link with little room above the rendition (gap 3) |
| `ladder-hlsjs-rate-steps` | FAIL | player: hls.js played, with at most 2000 ms of stall in all: stalled: 10713.1 ms stalled, 64.001 s of media in 75.017 s; errors ['bufferStalledError', 'bufferNudgeOnStall', 'bufferSeekOverHole', 'bufferStalledError', 'bufferNudgeOnStall', ' | Ladder of four renditions; one date in every rendition | hls.js at one-second latency stalls on a shaped link with little room above the rendition (gap 3) |
| `ladder-hlsjs-edge-kill` | FAIL | player: hls.js played, with at most 1000 ms of stall in all: stalled: 8791.8 ms stalled, 50.242 s of media in 60.015 s; errors ['bufferSeekOverHole', 'bufferStalledError', 'bufferNudgeOnStall', 'bufferSeekOverHole', 'fragLoadError', 'levelL | Ladder of four renditions; one date in every rendition | hls.js at one-second latency stalls on a shaped link with little room above the rendition (gap 3) |
| `baseline` | PASS | 15 of 15 readers served, 0 of 80582 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Contributor ingest ring; playlists from stamps | - |
| `endurance-churn` | PASS | 0 of 1125 reader groups: []; 1143 copies released in 225 cycles | Allocator settings; target duration follows a duration that recurs; an edge keeps no set of received slots; bounded packet sequence tracker | - |

The full-disk scenario needs a real instance and is in the Linode table only.
The clock scenarios ran in both labs: locally a preloaded library gives one
service its own wall clock.

### Linode lab

Run `20261121T044850Z-edge-campaign`: 15 instances for five hours. The table
is the final set of 07:12 UTC on the final binaries, the reruns of 08:46 UTC,
and, for the three edge capacity scenarios and the flash crowd, the run of
06:05 UTC. A scenario that says "the load host" was limited by the 4-vCPU
host that plays the readers, not by the mesh.

| Scenario | Result | What the run showed | What was fixed | What is left |
| --- | --- | --- | --- | --- |
| `capacity-edge-flac` | PASS | no stall up to 60 readers of one edge (edge CPU 91.7%); at 80 readers 36 stall | Admission by request cost | 5 ms parts cost one request each: about 60 audio readers for each vCPU (gap 9) |
| `capacity-edge-opus` | PASS | no stall up to 60 readers of one edge (edge CPU 90.7%); at 80 readers 80 stall | Admission by request cost | 5 ms parts cost one request each: about 60 audio readers for each vCPU (gap 9) |
| `capacity-edge-4k` | PASS | no stall up to 64 readers of one edge (edge CPU 22.7%) | - | The instance's network, about 1 Gbit/s, is the limit, not the CPU |
| `admission-refuses-cleanly` | PASS | 136 of 270 readers served, 0 of 701932 parts missing, 0 readers stalled (longest 0 ms), 7478 moves | Admission by request cost | - |
| `capacity-scales-with-edges` | PASS | two edges admitted 60 readers, four admitted 120: ratio 2.0 | Admission by request cost | - |
| `capacity-entry-joins` | FAIL | every join answered in time up to 200 a second (p99 29.1 ms, entry CPU 39.2%); video during the joins: no stall with a 1000 ms buffer: 16 stalls in 1 readers, longest 195 ms, total 1029 ms | Entry point remembers the init scan | Ceiling not reached: the load tool falls behind first |
| `capacity-seventeen-streams` | GAP | CPU by role {'contributor': 97.6, 'relay': 32.8, 'distributor': 61.0, 'edge': 72.5}; streams that played clean: 0 of 17 | - | The contributor is the limit: one vCPU does not carry sixteen audio streams of 5 ms parts beside 4K (gap 9) |
| `flash-crowd` | FAIL | every reader that the region admitted played without a stall: 262 of 263 stalled, 3012319 ms in all; the readers that the neighbouring region took lost no part and stalled for less than 2% of the time: 153 readers, 425023 ms stalled in all | HTTP on its own runtime; pending assignments; init scan | An edge is sent more readers than it admits through the alternates of full edges. On Linode the load host could not drive the admitted readers |
| `mixed-edge` | PASS | 24 of 24 readers served, 0 of 177950 parts missing, 0 readers stalled (longest 0 ms), 15 moves | Admission by request cost | - |
| `slow-and-stuck-readers` | PASS | 14 of 14 readers served, 0 of 73038 parts missing, 3 readers stalled (longest 1114 ms), 5 moves | - | - |
| `long-tail-one-stream-too-many` | PASS | 4 of 5 readers served, 0 of 23837 parts missing, 0 readers stalled (longest 0 ms), 0 moves | A full node refuses a new stream by name; an edge with no free slot sends the reader to another edge | - |
| `long-tail-more-slots` | PASS | 10 of 10 readers served, 0 of 54873 parts missing, 0 readers stalled (longest 0 ms), 0 moves | --playlist-slots | - |
| `baseline` | PASS | 15 of 15 readers served, 0 of 79528 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Contributor ingest ring; playlists from stamps | - |
| `edge-kill` | PASS | 12 of 12 readers served, 0 of 85030 parts missing, 0 readers stalled (longest 0 ms), 4 moves | Standby copy; alternates in standby order | - |
| `edge-freeze-resume` | PASS | 14 of 14 readers served, 0 of 90481 parts missing, 0 readers stalled (longest 0 ms), 5 moves | Alternates in standby order; reader ignores a reload to a cache that it has left | - |
| `edge-rolling-restart` | FAIL | video: no stall with a 1000 ms buffer: 8 stalls in 2 readers, longest 421 ms, total 916 ms | - | A 4K reader that is drained to an edge that has just started can stall for under half a second |
| `distributor-kill` | PASS | 12 of 12 readers served, 0 of 85993 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | - |
| `distributors-both-kill` | PASS | 9 of 9 readers served, 3966 of 71789 parts missing, 9 readers stalled (longest 2762 ms), 36 moves | Lost-parents answer; pathway in the neighbouring region | Readers lose the seconds until they play from the neighbouring region |
| `relay-primary-kill` | GAP | video: no part is missing: 6 missing of 785; video: no stall with a 1000 ms buffer: 3 stalls in 3 readers, longest 1342 ms, total 3991 ms | Reader follows the playlist past lost parts | The parts in flight are lost and not signalled (gap 8) |
| `contributor-freeze` | FAIL | video: every reader plays again within 3 s of the end of the fault: the last stall ended at 24.2 s; fault from 15.0 s to 20.0 s; longest stall 3989 ms; 45 parts missing; audio: every reader plays again within 3 s of the end of the fault: th | Reader stays on a live cache whose stream has stopped; larger publication queue | The contributor wakes with a backlog and needs about 4 s to be live again |
| `region-lost` | PASS | 6 of 6 readers served, 1009 of 56408 parts missing, 6 readers stalled (longest 971 ms), 24 moves | Pathway in the neighbouring region | - |
| `clock-skew-edge` | PASS | 6 of 6 readers served, 0 of 20113 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Age of an advertisement by receipt | - |
| `clock-skew-distributor` | FAIL | via-skewed: no part is missing: 6724 missing of 23824; via-true: no part is missing: 5043 missing of 23606 | Age of an advertisement by receipt: readers are assigned and served | The deadline of a relay object is a time of day: the skewed distributor drops every object |
| `clock-skew-contributor` | PASS | 6 of 6 readers served, 0 of 48195 parts missing, 1 readers stalled (longest 83 ms), 1 moves | - | - |
| `edge-disk-full` | PASS | 5 of 5 readers served, 0 of 40133 parts missing, 0 readers stalled (longest 0 ms), 0 moves | - | - |
| `edge-cpu-starved` | GAP | video: no part is missing: 32 missing of 832; video: no stall with a 1000 ms buffer: 18 stalls in 2 readers, longest 2084 ms, total 8303 ms | - | A starved edge is not taken out of assignment until it falls behind |
| `edge-out-of-memory` | FAIL | the kernel killed the service at its memory limit: ; audio: no stall with a 1000 ms buffer: 6 stalls in 1 readers, longest 276 ms, total 635 ms | Allocator settings | On an instance with swap the squeezed edge slows instead of dying |
| `mesh-loss-0.5pct` | PASS | 9 of 9 readers served, 0 of 54346 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Loss-adaptive protection; one parent fills a hole | - |
| `mesh-burst-loss` | FAIL | video: no part is missing: 11 missing of 651; video: no stall with a 1000 ms buffer: 3 stalls in 1 readers, longest 1760 ms, total 2485 ms | Loss-adaptive protection; fragment reassembly settings | 2 to 3% of the 4K parts are not delivered when bursts lose 70% of the packets |
| `mesh-partition-edge-heals` | PASS | 14 of 14 readers served, 0 of 91208 parts missing, 0 readers stalled (longest 0 ms), 4 moves | An edge that has lost its parents answers 503 at once | - |
| `mesh-loss-2pct` | FAIL | audio: no part is missing: 444 missing of 53259; audio: no stall with a 1000 ms buffer: 3 stalls in 3 readers, longest 1452 ms, total 4350 ms | Loss-adaptive protection; fragment reassembly settings | Linode: one incident on one edge in each of two runs; local clean |
| `player-hlsjs-region-lost` | PASS | hls.js: 62.623 s of media in 70.016 s, 2826 ms stalled, 0 of 1514 frames dropped | Pathway in the neighbouring region | - |
| `player-hlsjs-4k` | FAIL | hls.js played without a stall: 1 playhead stall(s), 1 waiting->playing stall(s), 0 hls.js stall report(s); 220.6 ms stalled in total, longest 200 ms; played 41.87 s of media in 42.15 s after first "playing"; errors ['bufferSeekOverHole'] | Playlists that a player accepts | A viewer far from the region needs a lower rendition |
| `player-hlsjs-4k-second-region` | PASS | hls.js: 43.496 s of media in 45.018 s, 0 ms stalled, 0 of 1048 frames dropped | - | - |
| `player-hlsjs-cache-kill` | FAIL | hls.js played on, with less than 1 s of stall in all: stalled: 3954.5 ms stalled; 2 playhead stall(s), 3 waiting->playing stall(s), 3 hls.js stall report(s); 3954.5 ms stalled in total, longest 3312.9 ms; played 54.51 s of media in 57.84 s  | Standby copy; pathway clones | From an ocean away the move to a new connection at 16.5 Mbit/s stalls for 4 s: a ladder is the remedy |
| `spec-playlists` | PASS | 2 of 2 readers served, 0 of 8181 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Stamps; reload rules; segment complete at its short last part; hint after the last listed part | No delta updates, no gap signalling |
| `ladder-hlsjs-browser-cap` | FAIL | player: hls.js played, with at most 500 ms of stall in all: stalled: 5555.8 ms stalled, 53.263 s of media in 60.016 s; errors ['bufferSeekOverHole', 'bufferStalledError', 'bufferStalledError', 'aborted', 'bufferStalledError', 'bufferStalled | Ladder of four renditions; one date in every rendition | From an ocean away, on the commit before the date fix, it stalled 5.6 s |
| `ladder-hlsjs-browser-steps` | FAIL | player: hls.js played, with at most 2000 ms of stall in all: stalled: 13211.3 ms stalled, 57.496 s of media in 75.016 s; errors ['bufferStalledError', 'bufferStalledError', 'bufferNudgeOnStall', 'bufferSeekOverHole', 'bufferStalledError', ' | Ladder of four renditions; one date in every rendition | Local: no stall, 2160p again after 28 s where the criterion is 25 s |
| `ladder-playlists` | PASS | 1 of 1 readers served, 0 of 223 parts missing, 0 readers stalled (longest 0 ms), 0 moves | Ladder; rendition reports; renditions numbered from one instant; one date in every rendition | - |
| `endurance-churn` | FAIL | every reader of every cycle was served without a missing part: 1 of 320 reader groups: [{'cycle': 17, 'job': 'c17-opus', 'readers_ok': 4, 'parts_missing': 970}]; no node grows by more than 20 MB an hour after the first tenth of the run: {'c | Allocator settings; target duration follows a duration that recurs; an edge keeps no set of received slots; bounded packet sequence tracker | - |

## Capacity of each role on a Nanode

Measured on Linode Nanodes (1 shared vCPU, 1 GB, 1 Gbit/s out) unless a row
says otherwise. The ceiling is the last step at which no reader stalled with a
1 s buffer. "Local" is the namespace lab, where a service has one capped core
of the test host, which is slower than a Nanode vCPU.

### Edge: readers of one playlist

| Stream | Readers without a stall | Edge CPU there | One step more | What sets the limit |
| --- | ---: | ---: | --- | --- |
| Lossless FLAC, 5 ms parts, 1.4 Mbit/s | 60 | 92% | 80 readers: 36 stall | CPU: 200 requests a second for each reader |
| HQ SoundKit Opus, 5 ms parts | 60 | 91% | 80 readers: all stall | CPU: 200 requests a second for each reader |
| 4K H.264, 200 ms parts, 16.5 Mbit/s | 64 | 23% | not reached | The 1 Gbit/s of the instance: 64 readers are 1.06 Gbit/s |
| FLAC, local lab | 40 | 67% | 60 readers: 33 stall | CPU |
| 4K, local lab | 64 | 24% | not reached | - |

The audio ceiling is a request ceiling. An edge that is past it stalls every
reader that it has, not only the newest, which is why admission charges a
session for its requests: with the default of 8,000 requests a second for each
worker, an edge admits about 31 audio readers and refuses the rest with 429
and the other caches. In `admission-refuses-cleanly`, four edges were offered
240 lossless readers at one edge: 120 were served, 120 refused, and no
admitted reader stalled. Offered 30 4K readers with 100 Mbit/s for each edge,
16 were served and 14 refused.

Adding edges adds capacity in proportion. `capacity-scales-with-edges`: two
edges admitted 60 lossless readers, four admitted 120, and no admitted reader
stalled (62 and 120 in the run before).

### Entry point: new sessions

A join is the Multivariant Playlist with an assignment, the request that
makes a session. The tool offers a fixed number of new sessions a second for
twelve seconds, each over a new TLS connection.

| Where | Offered a second | Answered | p50 | p99 | Entry CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| Linode | 50 | all | 17 ms | 24 ms | 31% |
| Linode | 100 | all | 17 ms | 27 ms | 35% |
| Linode | 200 | all | 17 ms | 29 ms | 39% |
| Local | 200 | all | 17 ms | 24 ms | 33% |
| Local | 400 | all | 17 ms | 22 ms | 43% |
| Local | 800 | all | 18 ms | 27 ms | 65% |

Above these rates the load tool fell behind its own schedule (by 9 s at 400 a
second on the Linode load host, by 5 s at 1,600 a second locally), so the
ceiling of the entry point was not reached. An edge used as the entry point
answers at least 200 joins a second on a Nanode with 61% of its vCPU free,
and at least 800 a second in the local lab. Once the region is full the
answer is a 429 with the other caches, which costs the same as an assignment:
at 800 a second, 8,170 of 9,600 answers were refusals. The Linode scenario is
marked FAIL because readers that shared the saturated load host stalled.

### Contributor, relay and distributor: streams

Every stream beside the 4K stream is one audio track in 5 ms parts, 200 parts
a second. One reader of each stream on one edge.

| Streams | Lab | Contributor | Relay | Distributor | What the readers received |
| --- | --- | ---: | ---: | ---: | --- |
| 3 (4K and 2 audio) | Linode | 54% | 31% | 24 to 29% | Everything (every scenario of the campaign) |
| 5 (4K and 4 audio) | Linode | 76% | 41% | 41 to 53%, peaks of 98% | Everything (`long-tail-more-slots`) |
| 5 | local | 64% | 26% | 39 to 42% | Everything |
| 9 (4K and 8 audio) | local | 74% | 42% | 71% | 80 to 85% of the parts of each stream (`capacity-nine-streams`) |
| 13 (4K and 12 audio) | local | 78% | 30% | 71% | About 60% of the audio parts, 2% of the video parts (`capacity-thirteen-streams`) |
| 17 (4K and 16 audio) | Linode | 94% | 31% | 51%, peaks of 98% | About 30% of the audio parts, 1% of the video parts (`capacity-seventeen-streams`) |
| 17 | local | 86% | 20% | 58% | About 35% of the audio parts, 1% of the video parts |

CPU is the mean over the run, of one vCPU.

The contributor is the first role to fail, and it fails before its vCPU is
full. Between five and nine streams its publication worker falls behind:
objects pass their one-second deadline before they are sent
(`canonical media object expired`), and the distributors drop what arrives
late. So a Nanode contributor carries 4K and four audio streams of 5 ms
parts, and not eight. From three and five streams on Linode, an audio stream
costs a relay about 5% of its vCPU and a distributor about 10%: a relay would
carry about fifteen and a distributor about nine. Those two are extrapolated,
not measured, because the contributor fails first. The limit is parts a second, not streams: the
same sixteen audio streams in 200 ms parts would be 80 parts a second, not
3,200. That is rank 9 of [Media edge gaps](../media-edge-gaps.md).

Memory with many streams is not settled. In a 40-minute run that carried 17
streams by mistake, with the contributor starved, every relay and distributor
went from about 400 MB to about 830 MB of its 1 GB. Whether that is where it
stops was not measured.

### Shared CPU

A Nanode shares its CPU. In these runs the hypervisor took 20 to 57% of one
edge's CPU for some seconds, several times in an hour, on two different
instances in the same slot. Each time the readers of that edge stalled for 15
to 500 ms with a 1 s buffer, and nothing else in the region noticed. Most of
the Linode scenarios that fail on "no reader stalls" fail on this. An edge
does not report its steal, and assignment does not avoid an edge that has it.

## Endurance

`endurance-churn` repeats one cycle for the whole run: readers of every
stream join in both regions, play for some seconds and leave, so that the
edges take and release copies all the time. It samples every node every few
seconds.

### The three-hour run, before the fixes

Local lab, on the binaries of the start of the round: 449 cycles in 3.0
hours, 2,245 groups of readers, 2,387 copies released by the edges.

- File descriptors and threads did not grow on any node: 19 to 27
  descriptors at the start, at most 6 more at the end on a distributor, the
  same on every edge; 2 threads for a mesh node, 8 for the contributor.
- Four groups of readers of 2,245 missed two to four parts, all in cycles
  220 and 221, when other work ran on the host.
- Memory was not flat. Relays and distributors went from 160 to 365 MB in one
  step: one long segment had raised the target duration from 2 s to 8 s, and
  every node then kept four times the media (finding 19). Edges went from
  about 110 MB to between 340 and 650 MB, except the one edge that was never
  given a 4K copy, which stayed at 80 MB (findings 20 to 22).

### After the fixes

Three runs, in the local lab.

| Run | Binaries | Readers | Edges | Contributor | Relays and distributors |
| --- | --- | --- | --- | --- | --- |
| 30 minutes | allocator settings, target duration fix | 375 groups, none missed a part | 52 to 100 MB at the start, 91 to 150 MB at the end | 85 to 108 MB | flat: 6 to 16 MB an hour |
| 2 hours | the same and the slot answer of an edge | 1,500 groups, none missed a part | 53 to 90 MB after four minutes, 132 to 245 MB at the end; one edge that took no copy stayed at 43 MB | 84 to 170 MB | flat: under 3 MB an hour |
| 90 minutes | the same and both memory fixes | 1,125 groups, none missed a part | under 3.5 MB an hour: 52 to 120 MB throughout | 85 to 91 MB throughout | under 3 MB an hour |

The two-hour run is what showed that the allocator settings were not the
whole answer. At its end the edges held no playlist and had no reader. The
largest of them had 240 MB. `malloc_trim` returned 64 MB of that, and
`malloc_stats` reported 179 MB in use with 4 MB in the arenas: live blocks,
not free memory. Read from `/proc/<pid>/mem`, the largest were three hash
tables that double as they grow: 71 MB of stream and part numbers, and two of
42 MB of packet sequence numbers. The contributor held two of the second kind
as well. They are findings 21 and 22. An edge that received one 4K stream and
two audio streams all day would have run out of its 1 GB in about half a day;
no earlier test ran long enough to see it.

The 90-minute run is the same scenario on the binaries with both fixes.
Every check of the scenario holds: no node grows by more than 20 MB an hour
after the first tenth of the run (the most is 3.5), descriptors and threads
are constant, the edges released 1,143 copies in 225 cycles, and no reader
missed a part.

On Linode a 30-minute run on the binaries before the two fixes showed the
same rise as the local 30-minute run (edges from 45 to 110 MB at the start to
76 to 135 MB at the end), and one group of four readers of 320 missed 1.2 s of
audio parts. Descriptors and threads were not sampled there. The fixed
binaries were not run on Linode: the lab was deleted before the cause was
found.

What is not shown: a day. Ninety minutes is long enough to see the two sets
that grew, and nothing else grew in it, but a slower leak would not show.

## Real players and the LL-HLS rules

hls.js 1.7.4 in headless Chrome for Testing 158, software decoding. Local runs
are from the reader host of the lab, 2 ms from its region. Linode runs are
from the operator host in Newark: 0.5 ms from the Newark region and an ocean
from the London region. Only the 4K stream is played: the audio streams have
5 ms parts, which no standard player follows (gap 9).

| Scenario | Where | Media played | Stalled | Frames dropped | Latency p50 | Note |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 4K through the entry point | local | 42.3 s of 45 s | 0 | 0 of 1,021 | 1.11 s | |
| 4K, second region | local | 43.2 s of 45 s | 0 | 0 of 1,041 | 1.02 s | |
| 4K, nearby region | Linode | 43.5 s of 45 s | 0 | 0 of 1,048 | 1.10 s | |
| 4K, region an ocean away | Linode | 41.9 s of 45 s | 0.2 s at the start | 0 of 1,015 | 2.4 s | one rendition; a ladder is the remedy |
| Serving cache killed | local | 58.7 s of 60 s | 0 | 0 of 1,404 | 0.92 s | plays on from the standby cache; in `player-hlsjs-edge-kill`, media again 52 ms after the kill |
| Serving cache killed, far region | Linode | 54.5 s of 60 s | 4.0 s | 0 of 1,298 | 4.6 s | moved in 0.8 s; then 16.5 Mbit/s over a new transatlantic connection |
| Serving cache drained | local | 52.3 s of 55 s | 0 | 0 of 1,261 | 1.09 s | moved at the drain |
| Whole region killed | local | 64.3 s of 70 s | 2.4 s | 0 of 1,530 | 3.3 s | media from the neighbouring region 1 s after the loss |
| Whole region killed | Linode | 62.6 s of 70 s | 2.8 s | 0 of 1,514 | 0.88 s | media from the neighbouring region 2.7 s after the loss |
| Viewer's network cut for 8 s | local | 55.8 s of 70 s | 12.7 s | 0 of 1,346 | 0.92 s | one stall; plays again about 5 s after the network returns, without a reload of the page |

Before this round, 2 of 3 cache kills ended playback for good (the next
pathway had no copy), and hls.js needed 1.2 to 7.7 s for a first frame from a
cold cache.

ffmpeg 9.0.2 plays at the first attempt and reads 33.8 s of media in 33.6 s.
It still logs `Empty segment` once for the first playlist that it reads.
At the start of the round it could not play the audio streams and failed on
a cold cache (`scripts/players/FINDINGS.md`).

### The ladder

Four renditions from the encoder: 2160p at 16.6 Mbit/s, 1080p at 5.4, 720p at
2.5 and 360p at 0.95.

| Scenario | Rendition over time | Media played | Stalled |
| --- | --- | --- | ---: |
| Browser limited to 8 Mbit/s, local | 720p, then 1080p from 1 s | 59.4 s of 60 s | 0 |
| Browser rate unlimited, 3 Mbit/s from 20 s, unlimited from 45 s, local | 2160p from 4 s, 360p at 20 s, 720p at 46 s, 1080p at 55 s, 2160p at 73 s | 71.0 s of 75 s | 0 |
| Link shaped to 8 Mbit/s with a quarter-second queue, local | 720p, then 1080p from 3 s to the end | 55.9 s of 60 s | 3.1 s |
| Good link beside it | 1080p, then 2160p from 1.4 s | 59.3 s of 60 s | 0.15 s |
| Link shaped to 3 Mbit/s from 20 s to 45 s, local | 2160p, 360p at 21 s, 720p at 60 s | 64.0 s of 75 s | 10.7 s |
| Browser limited to 8 Mbit/s, region an ocean away, Linode | 720p, 1080p, 360p at 32 s, 720p at 38 s | 53.3 s of 60 s | 5.6 s |

The player chooses the rendition that fits in every run, and with the rate
limit in the browser it plays through a collapse of the rate without a stall.
On a link that is shaped in the network it stalls. At a latency of one second
a player holds 0.6 s of media, and a key-frame part of 1080p is several times
the average part: on a link with one and a half times the average rate the
queue of the link is longer than the buffer of the player. The first ladder
runs were worse than this for two reasons that are fixed: the dates of the
renditions were 0.14 to 0.26 s apart, and the lab gave a shaped link a queue
of 50,000 packets. What is left is in
[Media edge gaps](../media-edge-gaps.md), rank 3.

### The rules of the draft

`scripts/hls_spec.py` checks 77 rules of draft-pantos-hls-rfc8216bis-22 and of
Apple's authoring specification. `spec-playlists` passes in both labs: no rule
that the draft states as MUST is broken by the video or the audio playlist of
an edge or of a distributor, by the Multivariant Playlist, or by the steering
manifest, over six seconds of reloads each, and the HTTP checks of blocking
reload pass (400 for a request too far ahead, 503 after three target
durations, 404 at once for a part or a stream that does not exist).

What it reports as SHOULD or as Apple's rule and not met:

- a single-rendition Multivariant Playlist has no `AVERAGE-BANDWIDTH`,
  `RESOLUTION` or `FRAME-RATE` (a ladder has all three);
- `PART-TARGET` of the audio streams is 0.005 s, below any round trip;
- no delta updates (`CAN-SKIP-UNTIL`);
- a part has the media type `application/octet-stream`;
- a playlist holds exactly three target durations of segments, the minimum.

## Still open

Every FAIL of the two tables and the two clock gaps, with what explains each. The same list with more
on each is at the end of [Media edge gaps](../media-edge-gaps.md).

| Scenario | Lab | What was seen | Standing |
| --- | --- | --- | --- |
| `mesh-burst-loss` | both | Local: 22 of 644 4K parts not delivered, no stall. Linode: 11 of 651 and one reader with a 1.8 s stall | Open. A burst that loses 70% of the packets for some tens of packets is over before the measured-loss protection has grown |
| `mesh-flap-edge` | local | Two 4K readers of the flapping edge: one stall of 2.1 s each. They left on its 503, the next edge held their request 1.5 s, the third served them | Open, not diagnosed further |
| `mesh-loss-2pct` | Linode | Run 1: two 4K readers that began on a copy warming on the lossy link started 5 s behind live and jumped 27 parts. Run 2: 4K clean, three audio readers of one edge missed 0.7 s of parts together | Open. Local run is clean at 2% and 5% |
| `edge-rolling-restart` | Linode | 4K stalls of 15 to 421 ms, on the edge with 22% CPU steal and on a reader drained to an edge that had just restarted | Open |
| `contributor-freeze` | both | After a 5 s freeze readers play again 4.0 to 4.2 s after its end; the criterion is 3 s | Open: backlog of the contributor |
| `edge-out-of-memory` | Linode | The instance has swap: the squeezed edge slowed and three audio readers stalled up to 0.3 s. Local: killed, readers move, PASS | Scenario does not fit an instance with swap |
| `clock-skew-distributor` | both | Readers are assigned and served, 4K is whole, audio readers miss a quarter of their 5 ms parts. Local: the skewed distributor dropped 200,732 relay symbols as expired, no other node dropped one | GAP: the deadline of a relay object is a time of day on the clock of the contributor |
| `clock-skew-contributor-behind` | local | Contributor clock 2 s behind for 20 s: both relays drop its objects as expired, readers lose half of the minute | GAP: the same |
| `capacity-entry-joins` | Linode | Joins answered in time to 200 a second; readers on the saturated load host stalled | Load host |
| `flash-crowd` | both | Local: 199 admitted and 1,801 refused of 2,000 in a second, readers who were there stalled 31 ms at most; one edge was sent 51 readers through the alternates of full edges and 33 stalled. Linode: 416 admitted, readers who were there clean, the admitted crowd stalled with the load host 78% busy | Over-admission through alternates is open. The Linode number is the load host |
| `player-hlsjs-4k` | Linode | 0.2 s stall at the start, viewer an ocean from the region, one rendition | A ladder is the remedy |
| `player-hlsjs-cache-kill` | Linode | Viewer an ocean away: moved in 0.8 s, then 4.0 s of stall while 16.5 Mbit/s starts on a new connection | Same. Local: no stall |
| `ladder-hlsjs-rate-cap`, `-rate-steps`, `-edge-kill`, `-reorder` (links shaped or disordered in the network), and `-browser-cap`, `-browser-steps` from an ocean away | both | The right rendition is chosen on a shaped link, with 3 to 11 s of stall in 60 to 75 s. From an ocean away, with the commit before the date fix, the player fell to 360p and stalled 5.6 and 13.2 s | Open: rank 3 of the gap list |
| `ladder-hlsjs-browser-steps` | local | No stall; 2160p again 28 s after the rate returned, criterion 25 s | Criterion |
| `endurance-churn` | Linode | 30 minutes on the binaries before the two memory fixes: edges still rising, one group of readers of 320 missed 1.2 s of audio | Fixed and passed in the local lab after the Linode lab was deleted; not rerun on Linode |

## Revisions

| Repository | Branch | Commit | Against origin |
| --- | --- | --- | --- |
| `av-mesh` | `regional-llhls-cache` | `07e7d4a` | 26 commits ahead of `a9cb1dd`, not pushed |
| `playlists` | `regional-llhls-cache` | `483765e` | 4 commits ahead of `151eaee`, not pushed |
| `av-contrib` | `edge-hardening` | `12abbef` | new branch, 8 commits on `probe-soundkit-opus-plain` `412014d`, not pushed |
| `raptor-fec` | `bounded-sequence-tracker` | `68a1840` | new branch, 1 commit on `main` `b9c570d`, not pushed |
| `needletail` | `regional-llhls-cache` | this commit | ahead of `23dd320`, not pushed |
| `boxer` | `fix-rtmp-aac-frame-start` | `ee90c8f` | as pushed before this round |

None of it is merged to `main`. The SSH agent of the operator host held no
key during the round, so no branch could be pushed. `av-mesh` names
`playlists` and `raptor-fec` by commit, and `av-contrib` names `raptor-fec`
and `boxer` by commit: push `playlists` and `raptor-fec` first. Until then a
build needs them from the local checkouts:

    export CARGO_NET_GIT_FETCH_WITH_CLI=true GIT_CONFIG_COUNT=3
    export GIT_CONFIG_KEY_0=url.file://$HOME/wavey.ai/playlists/.git.insteadOf
    export GIT_CONFIG_VALUE_0=https://github.com/wavey-ai/playlists.git
    export GIT_CONFIG_KEY_1=url.file://$HOME/wavey.ai/raptor-fec/.git.insteadOf
    export GIT_CONFIG_VALUE_1=https://github.com/wavey-ai/raptor-fec.git
    export GIT_CONFIG_KEY_2=url.file://$HOME/wavey.ai/boxer/.git.insteadOf
    export GIT_CONFIG_VALUE_2=https://github.com/wavey-ai/boxer.git

Which binaries ran what. Local pass 9: `av-mesh` `edebdcb`. The reruns of
08:57 and 09:25 UTC: `725b4a3`, which adds the age of an advertisement by
receipt and the date of a ladder. The two slot scenarios and the two-hour
churn run: `91a1659`, which adds the answer of an edge that has no free slot.
The runs from 12:40 UTC (regression subset, clock scenarios, nine and
thirteen streams, the last churn run): `av-mesh` `6e657a7` and `av-contrib`
`12abbef`, which add the two memory fixes. The Linode final set ran `av-mesh`
`ee545f4`, which has the first of those four changes and not the others.
`av-contrib` was `5f939a5` in every run before 12:40 UTC.

The Linux contributor for Linode is built without the SRT feature, because
libsrt does not cross-compile with zig, and takes video over RTMP.

Tests. `av-mesh`: 192 unit tests and 43 library tests pass. `playlists`: 89
with `--all-features`. `raptorq-datagram-fec`: 77 unit tests pass; two tests
of `tests/video_loss_matrix.rs` time out, with and without the change of this
round. `av-contrib`: two tests of `audio_epoch_hls` fail
(`aep1_recovery_publishes_every_declared_format_as_opaque_bytes`,
`format_selection_publishes_flac_and_leaves_opus_on_the_wire`); they fail the
same way on `412014d`, before the round. They were not looked into.

## Evidence

- [`local-20261121T0650Z-edge-campaign.json`](evidence/local-20261121T0650Z-edge-campaign.json):
  every local scenario.
- [`20261121T044850Z-edge-campaign.json`](evidence/20261121T044850Z-edge-campaign.json):
  every Linode scenario.

`scripts/validate-real-world-evidence.sh` accepts both files when it is given
them alone. Run over the whole directory it stops earlier, at two GCP files
of July (`20260721T061329Z-h264-4k-viewer-capacity.json` and
`20260721T124900Z-h264-4k-loaded-player.json`) whose clean-up fields do not
meet their own rule. It does the same on `main`; those files were not
touched.

Raw reader reports, player reports, node samples and logs are under
`target/edge-campaign/` and
`target/linode-qualification/small-mesh-runs/20261121T044850Z-edge-campaign/`
and are not versioned.

## Clean-up

The Linode lab was deleted at 09:40 UTC after 4.92 hours: 15 instances and
one firewall, 0.213 USD an hour, about 1.05 USD of instance time. One edge
instance was replaced during the run; with billing by the started hour the
round costs about 1.10 USD. `linode-small-mesh-lab.py status` then reported
no active lab, the watchdog had exited, and the SSH key of the lab was
deleted. Nothing else on the account was touched.

The local labs were stopped with `edge_lab.py down`, and no lab process or
namespace is left.
