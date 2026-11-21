# Media edge gaps

21 November 2026. A ranked list of what a production live media edge has and
this one lacked or lacks, written during the edge hardening round. The test
record of the round is
[2026-11-21 edge hardening](real-world-tests/2026-11-21-edge-hardening.md). The
design is in [Regional LL-HLS edge cache fabric](edge-cache-fabric.md).

The comparison is against the LL-HLS specification
(draft-pantos-hls-rfc8216bis-22), Apple's HLS authoring specification, and
what the large live edges and CDNs document: origin shielding and request
collapsing, token authorisation, origin failover and redundant ingest, DVR,
ad and caption signalling, CMCD and CMSD, purge, drain and autoscaling.

The rank is by what a viewer loses, how often it happens, and whether the
design of small nodes that hold whole playlists can work around it. Each gap
says what it is, why it matters, and what it takes here in terms of playlists:
the playlist is the unit that every node holds, copies and serves.

## Summary

| Rank | Gap | State |
| ---: | --- | --- |
| 1 | Playlists that a standard player accepts: segment stamp, a part duration for each track, reload rules, cold caches | Built and proven |
| 2 | Content Steering that survives a cache failure: pathway clones, a standby copy | Built and proven |
| 3 | A rendition for the link of the viewer: ladder, rendition reports, aligned renditions | Built for video with its audio. hls.js chooses the rendition that fits its link; it still stalls on a link with little headroom. Audio as its own rendition is open (rank 9) |
| 4 | Region failure: a pathway in the neighbouring region | Built and proven |
| 5 | Admission that knows what a reader costs; a crowd must not degrade the readers who are there | Built and proven |
| 6 | A cache network that keeps its promise under packet loss, jitter and reordering | Built and proven to 5% random loss in the lab and 2% on Linode. A copy that warms on a lossy link is open |
| 7 | More streams than a node has slots must be refused, not shared out | Built: clean refusal, and `--playlist-slots` |
| 7a | A node with a wrong clock must not break assignment | Built and proven on Linode |
| 7b | An edge that has lost its source must say so | Built and proven |
| 8 | Signalling of a part that is lost for good (`EXT-X-GAP`, `GAP=YES`) | Open |
| 9 | Audio that standard players and narrow uplinks can use: grouped parts, audio rendition groups, AAC | Open |
| 10 | Viewer authorisation and protection of the control paths on the playback port | Open |
| 11 | Contributor redundancy and continuity over a contributor restart | Open |
| 12 | Names for caches (certificates) and routing a reader to a region | Open |
| 13 | Delta updates, `Priority` headers, `Alt-Svc` for HTTP/3 | Open |
| 14 | Viewer-side quality data: CMCD, CMSD, per-session records | Open |
| 15 | Autoscaling with demand replication; deploy by drain | Partly built, not run |
| 16 | DVR, start-over, live to VOD | Open |
| 17 | Metadata: `EXT-X-DATERANGE`, SCTE-35, captions, I-frame playlists | Open |
| 18 | Encryption and DRM pass-through | Open |
| 19 | An external CDN in front of the mesh | Open |

Smaller faults that the campaign found and did not fix are listed at the end,
under [Open items from the campaign](#open-items-from-the-campaign).

## Built in this round

### 1. Playlists that a standard player accepts

What it was. hls.js and ffmpeg could not play the audio streams at all, and
played the 4K stream only on a cache that had been warm for some seconds. The
caches each built their own playlist from the parts that they held, so the
same stream had a different Media Sequence Number, a different `PART-TARGET`
and a different target duration on each cache, and all three changed during
the stream. A cache with a new copy answered with a playlist of one part and
no segment. The twelve defects are listed in
[`scripts/players/FINDINGS.md`](../scripts/players/FINDINGS.md).

Why it matters. A cache move must be invisible to a player. A player that
moves carries `_HLS_msn` and `_HLS_part` from one cache to the next.

What was built, in playlist terms.

- The contributor stamps each part with its place in the playlist: Media
  Sequence Number, part index, declared part target and declared target
  duration (`hls-msn`, `hls-part`, `hls-part-target-ms`,
  `hls-target-duration-ms` on the media object). Every cache renders the same
  playlist from the stamps.
- Video and audio have their own part durations: `--fmp4-video-part-ms` (200)
  and `--fmp4-video-segment-ms` (2000) next to the 5 ms audio parts. A key
  frame starts a part, and a segment is a whole number of groups of pictures.
- The declared part target follows a duration that recurs, so one long first
  part does not widen it for the life of the stream.
- A cache holds the playlist back until its copy is playable, keeps at least
  seven target durations of segments, never lets the Media Sequence Number go
  down, keeps a part or a segment for three target durations after it leaves
  the playlist, answers 400 and 503 for a blocking reload as the draft says,
  and answers 404 at once for a stream or a part that does not exist.

How it is checked. `scripts/hls_spec.py` checks 77 rules of the draft and of
Apple's authoring specification against a live playlist and the HTTP
behaviour behind it. Scenario `spec-playlists` runs it on an edge and on a
distributor.

### 2. Content Steering that survives a cache failure

What it was. Each cache of the assignment was a pathway. When the serving
cache died, hls.js moved to the next pathway, which had no copy of the
playlist: 2 of 3 runs never recovered, and the third stalled for 1.5 s. A
cache that started after the session began could not be named at all.

What was built.

- A standby copy: `--playlist-copies 2` (the default). A playlist that has
  readers is held by two edges of the region. The second is chosen by a rank
  of the stream and the edge names, so every node agrees without a message.
- Pathway clones: the steering request carries the pathways that the playlist
  named (`p=`). The steering manifest defines any other cache as a
  `PATHWAY-CLONES` entry with `URI-REPLACEMENT.HOST`.
- The manifest has the media type `application/vnd.apple.steering-list`.

Proof. Scenario `player-hlsjs-edge-kill`: hls.js plays 4K, the serving cache
is killed, the first part from the standby cache arrives 51 to 64 ms later,
no stall, no dropped frame.

### 3. A rendition for the link of the viewer

What it was. A stream had one rendition. A viewer whose link is below
16.5 Mbit/s could not play the 4K stream (scenario `reader-rate-cap`).

What was built. The nodes are too small to transcode, so the ladder comes
from the encoder, as it does for most live contribution.

- The contributor takes each rendition as its own RTMP stream. A stream key
  that is a number names the output stream (`rtmp://…/live/11`).
- Renditions that start within a second of each other number their segments
  from the same instant, so the same group of pictures has the same Media
  Sequence Number in every rendition.
- `--hls-rendition master:stream:peak:average:WIDTHxHEIGHT:rate` on the caches
  (deploy setting `hls_renditions`). The Multivariant Playlist lists every
  rendition for every pathway, with `BANDWIDTH`, `AVERAGE-BANDWIDTH`,
  `CODECS`, `RESOLUTION` and `FRAME-RATE`, and again for the neighbouring
  region.
- An edge holds the whole ladder while it has a reader of one rendition. A
  player changes rendition when its network gets worse, and that is the wrong
  moment to wait for a copy.
- Each rendition playlist carries `EXT-X-RENDITION-REPORT` for the others.

What it costs. An edge with one reader of a 720p rendition also receives the
4K rendition: 25 Mbit/s for the ladder of the test against 2.5. Admission
still charges a session for the playlist that it reads.

A rendition of a ladder carries the date of its Media Sequence Number times
the segment duration as `EXT-X-PROGRAM-DATE-TIME`, so the same segment has the
same date in every rendition and on every cache. Without that the dates were
0.14 to 0.26 s apart, and hls.js looked for its place in the new rendition by
a date that was off by a part.

Proof. `ladder-playlists`: 16 variants on 4 pathways, the edge holds four
renditions 5 s after its first reader and none 22 s after its last, the same
segment starts at the same instant in every rendition, and each rendition
reports the three others. `ladder-hlsjs-rate-cap`: on an 8 Mbit/s link hls.js
settles on 1080p after 2.7 s and stays there, and the player beside it with a
good link settles on 2160p.

`ladder-hlsjs-browser-cap`: with the browser limited to 8 Mbit/s hls.js plays
59.4 of 60 s on 1080p with no stall. `ladder-hlsjs-browser-steps`: through a
fall to 3 Mbit/s and back it plays 71 of 75 s with no stall, down to 360p and
up again to 2160p.

What is left. hls.js at a one-second latency stalls on a link that is shaped
in the network and has little room above the rendition: 3.1 s in 60 s on a
link of 8 Mbit/s with a quarter-second queue, where 1080p needs 5.4 Mbit/s on
average and several times that for the part with a key frame. It is slow to
step down when such a link collapses (10.7 s of stall in 75 s). The date of
the renditions was verified in the local lab only. Start-up is on the first variant, the
highest. A cheaper first variant, `EXT-X-START`, and CMSD hints are the
server's levers. Audio is inside each video rendition: audio as its own
rendition group is rank 9. Admission by the entry point assumes the bit rate
of the top rendition.

### 4. Region failure

What it was. Assignment stayed inside a region. When every entry point of a
region was gone, its readers had nowhere to go.

What was built. `--failover-entry NAME=URL` (deploy setting
`region_failover`). The Multivariant Playlist ends with one pathway for each
neighbouring region. Its URI is the entry point of that region with
`assign=1`, and the entry point redirects the reader to the cache that it
assigns. Readers that a full region refuses use the same pathway.

Proof. Scenario `region-lost`: every edge and distributor of London is killed
while readers play.

### 5. Admission that knows what a reader costs

What it was. Admission counted bit rate. A reader of lossless audio in 5 ms
parts sends 200 requests a second, and an edge on one vCPU serves about 60
such readers. With 80, every reader of the edge stalls, and the bit rate is
under 2% of the limit.

What was built. A session is charged the larger of its bit rate and its share
of the request capacity of the node (`--egress-request-capacity`, default
8,000 requests a second for each worker). The HTTP server runs on its own
runtime at lower priority, so a crowd at the door does not starve the cache
network of the node. An entry point remembers what it has assigned in the
last second.

Proof. Scenarios `admission-refuses-cleanly` and `flash-crowd`.

### 6. A cache network under packet loss

What it was. With 0.5% loss on the private network of a region, every reader
of the region stalled. Both parents answered each hole by sending everything
from the hole to the head.

What was built, in the playlists crate. Each node measures the loss of each
peer and reports it (`LinkReport`). The sender protects an object by the
measured loss of the path. A hole is filled by one parent, with the hole
(`request_parts`). An object is encoded once for all children. A source
symbol that arrives late, on a path that reorders packets, is not counted as
lost.

Two kernel limits had to move as well. A 4K part travels in symbols of up to
16 kB, which IP fragments. The kernel keeps a datagram that has lost a
fragment for 30 s and drops every new fragment when 4 MB of them wait; with
2% loss that stopped 4K on every edge. It also drops a datagram when more
than 64 other fragments of the same sender arrive between two of its
fragments, which any reordering does at these rates. The deploy tool sets
`net.ipv4.ipfrag_time=1`, `net.ipv4.ipfrag_high_thresh=67108864` and
`net.ipv4.ipfrag_max_dist=0` on every node.

Proof. Scenarios `mesh-loss-0.5pct`, `mesh-loss-2pct`, `mesh-loss-5pct`,
`mesh-burst-loss`, `mesh-delay-jitter`, `mesh-reorder-duplicate`.

What is left. The cache network depends on IP fragmentation and on those
settings. Symbols that fit one packet would remove the dependence; the first
attempt cost too much CPU on one vCPU and needs batched sends.

### 7. More streams than slots

What it was. A node has 16 playlist slots. With 17 live streams, each part of
the stream without a slot took the slot of a live stream. That stream took
the slot of the next one with its next part. Every stream of the node lost
its window again and again.

What was built. A full node keeps what it holds and refuses the new stream by
name. A stream that has had no part for 10 s gives up its slot.
`--playlist-slots` (deploy setting `playlist_slots`) sets the number. A slot
costs under 1 MB until it holds media.

An edge keeps one slot for a standby copy. An edge whose other slots all
hold a playlist with readers answers the reader of one more playlist with 429
and the other caches. Before, that reader got nothing.

Proof. Scenarios `long-tail-one-stream-too-many` and `long-tail-more-slots`.

What is left. Slots are not the only limit. With audio in 5 ms parts a
Nanode contributor is at 98% of its vCPU with 17 streams and delivers a third
of the parts. In a 40-minute run with 17 streams every relay and distributor
went from about 400 MB to about 830 MB of its 1 GB; whether it stops there
was not measured. See rank 9.

### 7a. A node with a wrong clock

What it was. An entry point compared the time in the advertisement of a
cache, from the clock of the cache, with its own clock. An entry point whose
clock was 4 s ahead took every edge for stale and refused every reader. An
edge whose clock was 4 s behind was never given a reader.

What was built. The age of an advertisement is the time since this node
received a new one from that node.

Proof. Scenario `clock-skew-edge` in both labs; `clock-skew-distributor`
for assignment: readers through the skewed entry point are served.

What is left. Assignment no longer compares clocks. The relay lanes still
do: see the open items at the end.

### 7b. An edge that has lost its source

What it was. An edge whose private network was cut, or whose distributors
had both died, held every request until the reader gave up.

What was built. The nodes of the cache network say hello every 100 ms. An
edge that has heard no parent for 400 ms answers 503 with the other caches.

Proof. `mesh-partition-edge-heals`: no reader stalls. `distributors-both-kill`:
readers play from the neighbouring region 1.4 s (audio) to 3.4 s (4K) after
the region loses both distributors, where a 4K reader took 11 s before.

## Open, in rank order

### 8. Signalling of a part that is lost for good

What it is. When a part cannot be recovered within its deadline, a
distributor moves its watermark past it. The playlist then has a hole in its
part numbers and says nothing about it. The draft has `EXT-X-GAP` for a
segment and `GAP=YES` for a part.

Why it matters. The reader of this project hunts through every cache for the
part. A standard player sees a timeline that is shorter than the media and
stalls or drifts. Seen in every scenario that loses a part upstream of the
region: `relay-primary-kill`, `contributor-kill-restart`, `wan-partition`.

What it takes. The node that gives up a part publishes a small object in its
place that says "gap" with the stamp of the part. Caches copy it like any
part. The playlist lists the part with `GAP=YES`, and a segment whose every
part is a gap with `EXT-X-GAP`. Two to three weeks with the tests.

### 9. Audio for standard players and narrow uplinks

What it is. Audio parts are 5 ms. A reader sends one request for each part.
hls.js and AVPlayer do not follow a part target of 5 ms: Apple's authoring
specification asks for parts no shorter than the round trip. Lossless FLAC
and SoundKit Opus are not what Apple devices and many televisions decode.

Why it matters. Three measured costs. A reader of one audio stream sends
about 220 kbit/s of requests upstream, which does not fit a 256 kbit/s uplink
next to anything else. An edge serves 60 readers on one vCPU where the bit
rate would allow thousands. A browser cannot play the audio streams at the
latency that they are made for.

What it takes. A second view of the same playlist with the parts in groups:
the cache serves `part_g{n}` as the concatenation of forty 5 ms parts, in a
playlist with a 200 ms part target. Nothing changes upstream of the edge.
The existing tail-bundle route already returns several parts in one
response. Then an `EXT-X-MEDIA TYPE=AUDIO` group, so that audio is one
rendition next to the video ladder, and an AAC rendition from the
contributor for devices that need it. Three to five weeks.

### 10. Viewer authorisation and the control paths

What it is. Anyone with a URL can read. The session id and the cache pin are
taken from the query without proof. `/api/control/*`, `/api/assignment` and
`/mesh/local` answer on the playback port.

Why it matters. No rights holder accepts open URLs. A reader can open any
number of sessions and blocking requests, and admission is keyed on an id
that the reader chooses.

What it takes. A signed token on the Multivariant Playlist request (expiry,
stream, optional address), verified by the entry point, and carried into
every pathway URI and into the steering `PARAMS`, so that every cache checks
the same token without a lookup. The control paths move to the control
listener. Two to three weeks.

### 11. Contributor redundancy and restart continuity

What it is. One contributor is one process on one node. The relay fabric
behind it has two parents at every hop.

Why it matters. Scenario `contributor-kill-restart`: readers lose the stream
for the length of the outage and for the start-up of the stream after it.
The restarted stream continues above the earlier Media Sequence Numbers, but
nothing tells a player that the timeline broke (`EXT-X-DISCONTINUITY`).

What it takes. Two contributors that receive the same source and stamp the
same numbers. The stamp comes from the wall clock and the key frames, so two
contributors with the same input already agree on the Media Sequence Number;
the part index and the canonical sequence must be made to agree too. The
relays then take a part from whichever contributor delivers it first. Plus
`EXT-X-DISCONTINUITY` on a real restart. Four to six weeks.

### 12. Names for caches, and routing a reader to a region

What it is. Playlists name caches by IP address. The certificate names a
host, so a browser needs certificate errors turned off, and ffmpeg needs a
forwarder. A reader reaches a region because it was given the address of an
entry point of that region.

What it takes. A DNS name for each cache under a wildcard certificate (the
name can encode the address), and certificate issue and rotation in the node
agent. For routing: a DNS name for each region, and one name that answers
with the nearest region. The failover pathway of rank 4 already covers a
reader that lands in the wrong or a dead region. One to two weeks for names;
routing depends on the DNS provider.

### 13. Delta updates, Priority, Alt-Svc

What it is. No `CAN-SKIP-UNTIL` and `EXT-X-SKIP`, so every reload carries the
whole playlist (about 4 kB gzipped for video; more for 5 ms audio). No RFC
9218 `Priority` header. No `Alt-Svc`, so a browser never moves to HTTP/3
although the edges serve it.

What it takes. Delta updates are a second rendering of the stamped playlist
and are cheap now that the stamps exist. The headers are a few lines each.
One week.

### 14. Viewer-side quality data

What it is. The nodes report what they sent. They do not know what the
viewer saw. Players send CMCD on every request (`bl`, `mtp`, `bs`, `su`), and
the caches ignore it. There is no per-session record.

What it takes. Parse CMCD on playlist requests, keep the last values with
the session, and export counts of starving sessions for each cache and
stream. CMSD `rd` on blocking responses, so that a player does not take the
hold time for a slow network. Two weeks.

### 15. Autoscaling and deploy by drain

Drain is built and tested (`edge-rolling-restart`, `player-hlsjs-drain`).
What is missing is the controller loop that adds an edge when admission
refuses readers, and a deploy that drains each node first. Scenario
`capacity-scales-with-edges` shows that capacity follows the number of edges.

### 16 to 19

DVR and live to VOD: the window is seven target durations by design. A longer
window is storage on a node class that has little, so it belongs on a
separate recording role that subscribes like an edge.

Metadata: `EXT-X-DATERANGE` and SCTE-35 need a stamp field from the
contributor and pass through the caches unchanged. Captions in the video
elementary stream already pass through and need only `CLOSED-CAPTIONS` in the
Multivariant Playlist.

Encryption: `EXT-X-KEY` and cbcs are a contributor and playlist matter. The
caches carry media without reading it.

An external CDN in front: possible once playlists are stable across caches,
which rank 1 delivered. Needs cache keys that ignore `sid` and honour
`_HLS_msn` and `_HLS_part`, and short TTLs.

## Open items from the campaign

Faults that the campaign found, with their scenario, that are not fixed.

- **A copy that warms on a lossy link can list a playlist that ends seconds
  behind live.** Linode, `mesh-loss-2pct`: two 4K readers that started on such
  a copy began 5 s behind and then jumped 27 parts. A reader on a warm edge
  missed nothing.
- **An edge takes more readers than it admits when they arrive through the
  alternates of full edges.** `flash-crowd`: one edge of four was sent 51
  audio readers where it admits about 31, and 33 of them stalled.
- **Shared CPU.** A Nanode loses 20 to 57% of its CPU to the hypervisor for
  seconds at a time. Its readers stall for 15 to 500 ms. An edge does not
  report steal, and assignment does not avoid it.
- **The wide-area protection is sized for a configured loss of 2%.** At 3%
  one 4K part in eleven is lost before the region (`wan-loss-3pct`,
  `contributor-uplink-loss`). The cache network adapts to measured loss; the
  relay lanes do not.
- **A relay that dies loses the 4K parts in flight** (`relay-primary-kill`):
  two or three parts, one stall of about a second for each reader.
- **A contributor that wakes from a 5 s freeze needs 4 s more** to shed its
  backlog (`contributor-freeze`).
- **A step of the contributor clock loses one 4K part**
  (`clock-skew-contributor`).
- **The deadline of a relay object is a time of day.** The contributor stamps
  each object with its publication time plus one second
  (`--relay-deadline-ms`), and each relay and distributor compares that with
  its own clock. A distributor whose clock is 4 s ahead drops every object:
  its edges get 4K whole from the other distributor and miss a quarter of
  the 5 ms audio parts (`clock-skew-distributor`: 200,732 symbols dropped at
  the deadline on that node, none elsewhere). A contributor whose clock is
  2 s behind is dropped by both relays, and the region has nothing
  (`clock-skew-contributor-behind`). Until the deadline is carried as an age,
  the nodes of the relay lanes need clocks within a fraction of a second of
  each other, and a node should refuse to start without a synchronised
  clock.
- **A flapping private link** costs a 4K reader one stall of 0.6 to 2.1 s
  (`mesh-flap-edge`): it leaves the edge on its 503, and the next edge holds
  its request for 1.5 s.
- **Bursts of loss on the cache network** (`mesh-burst-loss`: bursts that
  lose 70% of the packets, 3.4% on average): 2 to 3% of the 4K parts are not
  delivered.
- **A 4K reader that is drained to an edge that has just restarted** stalls
  for under half a second while the copy warms (`edge-rolling-restart`).
- **A starved edge serves late until the entry point sees it fall behind**
  (`edge-cpu-starved`).
- **RTMP audio needs `wavey-boxer` 0.1.1**, which is not published. A plain
  build of the contributor drops the first two bytes of every AAC frame.

