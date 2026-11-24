# 21–22 November 2026: 4K video against Cloudflare and LiveKit

The owner's order after audio: "then 4k video for raw throughput". This
record measures the highest bit rate of 4K video that each system carries
whole to each region, and then the latency, first frame, stalls, loss and
crowds of what it carries. Needletail, Cloudflare Realtime, Cloudflare Stream
WHIP/WHEP, self-hosted LiveKit and LiveKit Cloud carried the same 4K clip
from the same host in London to the same viewers in London, New York, Tokyo
and Sydney, on a lab of dedicated-CPU instances only. The same lab measured
the mesh itself hop by hop and the three tiers of Needletail's design.

Needletail was measured on eight builds of `low-latency`; the latest four,
waves 9, 7, 6 and 5, are first below (wave 8 had no 4K run). The earlier four: wave 1 (21 November,
evening), wave 2 (night, the smallest-frame defaults), wave 3 (a new relay
receive loop and class-isolated lanes, measured on 22 November at 02:38) and
wave 4 (edge and forwarder changes and UDP GSO for 4K, measured at 03:18, with
the relay topology of the earlier waves). The other systems were swept twice,
in the same sessions as waves 1 and 2; waves 3 and 4 were swept alone on the
same lab. The audio comparison is a separate
record, [2026-11-21 audio against Cloudflare and LiveKit](2026-11-21-audio-against-cloudflare-and-livekit.md).
A first pass on shared Nanodes is superseded and is not reported.

## Latest: wave 9 (24 November, morning)

**Wave 9 carried 25 Mbit/s whole to Sydney only and 40 Mbit/s to Tokyo and
Sydney; no region was whole at 80 or 120 Mbit/s, and the sweep ended there.**
Wave 5 carried 247 Mbit/s to every region on the same plans. Every frame
arrived at 25 and 40 Mbit/s. The regions failed the tail rule: a stall of
0.8 to 2.4 s within the step, where the rule allows 0.5 s over the median.

What changed since wave 7:
- **The processor regression is gone.** At 25 Mbit/s the contributor used 13%
  of one vCPU and the London forwarders 14% each (wave 6: 72% and 108%; wave 5:
  12% and 14%).
- **The socket drops are gone at 25 Mbit/s.** There were none except 24 on one
  New York forwarder; wave 7 already dropped about 900 a step there. At 40
  Mbit/s the New York edge dropped 4,859 datagrams. At 80 every edge dropped
  (6,100 to 46,000 in the step), and at 120 the London and New York edges did
  (34,000 and 71,000). The New York forwarders dropped 7,900 to 20,000 a step
  at 80 and 120.
- **The stalls come from stops of whole guests.** The 1 s samplers of a node
  stopped together for 2 to 8 s at a time. On the two New York forwarders that
  happened 32 and 30 times in the 5-minute sweep, about 114 s and 94 s in all.
  Tokyo's and Sydney's forwarders carried the same stream and had none.
  - The New York forwarders were on different physical hosts.
  - One stop, timed by the node's own hop journal: new-dist-a logged no event
    for 2,680 ms and never committed 41 parts (1.8 s of video). Its twin
    committed every one 35 ms after the contributor packaged it.
  - The New York edge's relay lane hangs off new-dist-a alone. It received
    those parts from the twin's mesh feed 2.2 to 2.65 s late, which is the
    viewers' stall.
- **The stops are below the process's packet handling.** During a stop no
  userspace ran on the guest, including a loop that runs only `date` and a
  0.5 s curl. Guest memory showed no compaction, reclaim or major faults, and
  1.6 GB was free. Steal grew by 0 to 2 ticks. The service was charged 4 to 6
  times its normal processor time inside the stops while the guest's tick
  counts did not grow. Two explanations fit, and this run cannot separate
  them:
  - the host did not run the guest and did not report steal;
  - a thread of the service spent seconds in the kernel with interrupts off.

  The stops did not start after large frames any more often than chance (12
  of 19 and 12 of 17, against 12.7 and 11.1 expected).
- **One measurement host stopped too.** Tokyo failed 25 Mbit/s only on
  tok-nano, whose own samplers stopped for 2.4 s. tok-view, beside it, was
  whole at 25.

Two extra steps at 25 Mbit/s, asked for by the lead, used the minutes already
billed. Both ran with the FLAC audio source off and with the hop journal of
every mesh node.
- GSO on, 120 s: whole in London, Tokyo and Sydney. New York failed on the
  2,680 ms stop above.
- GSO off (`relay_video_gso=false`, UDP GRO still on), 60 s: whole in all four
  regions, with no sampler gap on any node.

This is one short pair, not a controlled comparison.

CPU of one vCPU over each whole step (1 s sampler), the service's:

| Node | 25 Mbit/s | 40 | 80 | 120 |
| --- | ---: | ---: | ---: | ---: |
| Contributor | 13% | 22% | 41% | 60% |
| London forwarders | 14%, 14% | 30%, 22% | 60%, 49% | 79%, 75% |
| London edge | 26% | 30% | 51% | 60% |
| New York forwarders | 20%, 9% | 25%, 23% | 94%, 91% | 130%, 102% |
| New York edge | 23% | 30% | 23% | 30% |
| Tokyo forwarders | 11%, 9% | 23%, 17% | 43%, 39% | 62%, 57% |
| Tokyo edge | 23% | 29% | 63% | 65% |
| Sydney forwarders | 9%, 9% | 16%, 20% | 34%, 37% | 52%, 60% |
| Sydney edge | 17% | 21% | 35% | 49% |

The New York forwarders' figures include the time charged to them inside
their stops.

Build, lab and cost:
- Build: the wave 9 binaries, byte for byte those of the audio run (`av-mesh`
  `cdfeb5c`, `playlists` `c01662d`, `av-contrib` `219c6b5`, `needletail`
  `e641b51`), with the deploy defaults (merged topology, GSO on, GRO on).
- Lab `cmpd6`, 24 November 06:39 to 07:12 UTC: 22 dedicated instances in
  London, New York, Tokyo and Sydney. It had one edge and two forwarders a
  region, no LiveKit and no backbone relays
  (`deploy/compare-cloudflare/topology-4k-small.json`).
- Sweep `video-w9` at 07:00, 40 s a rung, Needletail alone, the 4K method
  unchanged. Extra steps `video-w9-hops25` and `video-w9-hops25-nogso`.
- Cost: $1.46 (one billed hour each).
- Evidence: `evidence/20261124-4k-wave9-analysis.json` and
  `evidence/20261124-4k-wave9-holds.txt`. All raw data is in the archive,
  run `cmpd6`.

## Wave 7 (22 November, evening)

**Wave 7 recovered part of wave 6's 4K regression, but stays far below wave
5.**
- With GSO on (the default), sweep `video-w7` at 21:28 UTC, on the same
  lab as the wave 7 audio, carried 25 Mbit/s whole to all four regions over
  UDP (Sydney 99.6% of frames on time).
- At 40 Mbit/s 86% to 99% of frames were on time; at 80 Mbit/s 81% to 91%;
  at 120 Mbit/s 40% to 70%, and the sweep ended.
- Wave 5 carried 247 Mbit/s whole to every region on the same plans
  (g6-dedicated-2 forwarders and edges).
- The median of what arrived at 25 Mbit/s: 11 ms London, 41 New York, 119
  Tokyo, 140 Sydney.

What changed, and where the loss is:
- **The contributor is no longer the problem.** Its output held steady at
  about 2.7 times the media rate through each step (69 Mbit/s at 25, 300 at
  120), with none of wave 6's mid-step surge.
- **The forwarders and edges drop at their sockets.** Their receive queues
  (16 MB each; the system allows 64 MB) overflowed. Each socket's own drop
  counter, cumulative since the deploy, read just after the sweep:
  - the second distributor's relay socket in London, Sydney and Tokyo:
    210,000 to 244,000 dropped datagrams;
  - New York's second distributor: 15,000;
  - the first distributors: 1,700 to 34,000 (their relay sockets summed);
  - the edges' mesh feed: 14,000 to 440,000.

  At the second distributors the drops grew from about 900 per step at
  25 Mbit/s to 127,000 to 152,000 at 120.
- So the 2-vCPU nodes can no longer read 4K as fast as it arrives. The
  latency session is bisecting the receive path between waves 5, 6 and 8.

## Wave 6 (22 November, afternoon)

**Wave 6 regressed on 4K.** With GSO on (its default) and the same
three-region lab, it carried 25 Mbit/s whole to New York only; at 25 Mbit/s
London had 92% to 94% of frames on time, and at 40 and 81 Mbit/s no region
was whole (London 61% to 88%, New York 22% to 72%, Tokyo 72% to 99%). The medians of what arrived stayed near wave 5's (London 8 to 32 ms, New
York 40 to 101, one HLS viewer 557 at 81 Mbit/s).
Its contributor used 72% of one vCPU at 25 Mbit/s and its London distributor
108% (wave 5: 12% and 14%), with no other source running and the audio-only
load unchanged from wave 5. Sweep `video-w6`, 15:10 UTC, Needletail alone;
the clips were encoded again from the same source with the same script. The
cause is with the product session.

## Wave 5 (22 November, morning)

**On wave 5, with its UDP GSO on (the deploy default), Needletail carried
247 Mbit/s of 4K whole to London, New York and Tokyo, more than any other
system has carried in this comparison: LiveKit Cloud carried 81 Mbit/s that
morning (164 in sweep 2), self-hosted LiveKit with an SFU in each region 40
to 81, Cloudflare 10 to 16. Its frames arrived earlier than LiveKit's SFU's at 40 Mbit/s and within
3 ms of them at 81: at 40 Mbit/s 12, 48 and 122 ms after they were due against 17,
53 and 122; at 81 Mbit/s 20, 55 and 131 against 19, 53 and 128; at 122
Mbit/s Needletail had every frame on time at 29, 65 and 140 ms where
LiveKit's SFU had 68%, 92% and 22% on time and LiveKit Cloud about 90%.
With its GSO off, the same build carried only 16 to 81 Mbit/s and failed
intermittently in London; Sydney was not measured.**

Build and lab as in the audio record's wave 5 section: `av-mesh` `05780d0`
(+ `ae77591`), `av-contrib` `33da3c1` on the contributor (paced GSO sends,
pushed during the round), `playlists` `c14d921`, deploy tool `a35ce1b` with
its defaults (merged topology, fast-plane edges); 26 dedicated instances,
London, New York, Tokyo. The 4K clips were encoded again from the same source
with the same script (the workbench holding the first encode was gone), so
their frame sizes differ slightly from sweeps 1 to 4.

| Sweep | Time (UTC) | Systems | Needletail settings |
| --- | --- | --- | --- |
| 6 (`video-w5`) | 08:42 | Needletail, Cloudflare Realtime and Stream, LiveKit SFU per region and multi-region | `relay_video_gso=false` |
| 6c (`video-w5c`) | 09:15 | LiveKit Cloud, to 160 Mbit/s | – |
| 7 (`video-w5-gso`) | 09:28 | Needletail alone, from 25 Mbit/s | `relay_video_gso=true` (the default) |
| 8 (`video-w5-legacy`) | 09:38 | Needletail alone | legacy topology, GSO off |
| 9 (`video-w5-nofast`) | 09:59 | Needletail alone, from 16 Mbit/s | `edge_fast_plane=false`, GSO off |

Highest bit rate carried whole (Mbit/s), UDP / LL-HLS; in brackets the
highest of an unbroken run from the bottom where it is lower:

| System | London | New York | Tokyo |
| --- | ---: | ---: | ---: |
| **Needletail wave 5, GSO on** (sweep 7) | **333 (164) / 247** | **247 / 247** | **247 / 164** |
| Needletail wave 5, GSO off, fast plane off (sweep 9) | 122 / 164 | 81 / 164 | 122 / 122 |
| Needletail wave 5, GSO off (sweep 6) | 16 / 81 (16) | 81 / 81 | 81 / 81 |
| Needletail wave 5, legacy topology, GSO off (sweep 8) | 81 / 40 | 40 / 40 (10) | 10 (none) / 10 (none) |
| LiveKit Cloud (sweep 6c) | 81 | 81 | 81 |
| LiveKit, SFU in each region (sweep 6) | 81 | 81 | 40 |
| LiveKit, multi-region set (sweep 6) | 40 | 25 | 25 |
| Cloudflare Realtime (sweep 6) | 16 | 10 | 16 |

Frames on time and median delivery after their due time (ms), the better of
each region's two viewers:

| System | 40 Mbit/s | 81 Mbit/s | 122 Mbit/s |
| --- | --- | --- | --- |
| **Needletail UDP, GSO on** | 100%: 12 / 48 / 122 | 100%: 20 / 55 / 131 | 100%: 29 / 65 / 140 |
| Needletail UDP, GSO off, fast plane off | 100%: 19 / 62 / 131 | 100%: 36 / 85 / 150 | 100% / 21% / 100%: 53 / 244 / 172 |
| LiveKit, SFU in each region | 100%: 17 / 53 / 122 | 100% / 100% / 87%: 19 / 53 / 128 | 68% / 92% / 22%: 23 / 49 / 135 |
| LiveKit Cloud | 91%: 30 / 73 / 146 | 100%: 32 / 74 / 143 | 90% / 91% / 91%: 31 / 74 / 149 |

The GSO-on sweep ran alone, 46 minutes after LiveKit's in the same lab; the
two were not in one sweep. Processor at 40, 81 and 122 Mbit/s with GSO on:
contributor 76%, 96% and 107% of one vCPU, London distributor 110%, 126% and
141%; with GSO off 21%, 50%, 86% and 26%, 71%, 152%. With GSO off and the
fast plane on, London failed at 16, 25 and 81 Mbit/s (87% to 96% of frames on
time) though its nodes were not busy, and a later 25 Mbit/s run (sweep
`video-w5-ev`, with the nodes' journals and sockets captured for the latency
session) carried it whole.

**Open on wave 5.** The 4K UDP clients received 97% to 99.9% of their parts
from the edges' fast lane (0% with the fast plane off, as set), (the audio clients' count of it is not reliable on wave 5; see the audio
record); with GSO off the fast-plane wiring cost London its low rates (with the fast
plane off it carried 122 to 164 Mbit/s); 247 Mbit/s, not 333, in New York
and Tokyo; Sydney, latency phases, crowds and loss for 4K on wave 5.

LiveKit Cloud is not measured after wave 5: the owner, 22 November, "we
already know the livekit numbers for now so lets just keep improving
needletail we dont need to test with their cloud". Its wave 5 figures stand
as its record; self-hosted LiveKit stays as one control in each headline run
of a new Needletail build.

The answer below is that of waves 1 to 4.

## Answer for waves 1 to 4

**On raw throughput LiveKit Cloud, LiveKit's own SFU mesh, leads: it
carried 164 Mbit/s of 4K to every region in sweep 2 (to all but Tokyo, 122,
in sweep 1). Needletail's latest build, wave 4, carried 122 Mbit/s whole to
every region over LL-HLS (and over UDP too with its 4K GSO switched off),
above self-hosted LiveKit with an SFU in each region (40 to 81 Mbit/s) and
far above Cloudflare (5); wave 3 carried 81. The earlier builds carried 16
to 25 Mbit/s; wave 2's backbone relays ran out of processor. At 40 Mbit/s
and above Needletail's frames arrived later than LiveKit's SFU's: 14 to
47 ms on wave 3 and 24 to 56 ms at 81 Mbit/s on wave 4. At 5 and 10 Mbit/s they arrived 4
to 13 ms before them and 15 to 39 ms before LiveKit Cloud's, but the WebRTC
senders pace each frame over up to 17 ms and Needletail's RTMP ingest does
not, so part or all of that lead is the sender; at 16 Mbit/s the two were
level. A viewer joining a warm Needletail edge had a key frame in 13 to
55 ms, and its contributor and distribution tiers did not grow with the
audience. Needletail loses under listener-side loss and delay with its HTTP
reader, and the time to first frame at a cold edge.**

Highest 4K bit rate carried whole, Mbit/s (the better of a region's two
viewers at least 99.5% of the frames on time, no stall over half a second;
LiveKit Cloud had one viewer in each region); sweep 1 / sweep 2:

| System | Uploads by the source, hops | London | New York | Tokyo | Sydney |
| --- | --- | ---: | ---: | ---: | ---: |
| **LiveKit Cloud (mesh)** | 1 upload; SFUs chosen by LiveKit Cloud (not observed) | **164 / 164** | **164 / 164** | **122 / 164** | **164 / 164** |
| LiveKit, SFU in each region | 4 uploads; 1 SFU | 40 / 81 | 81 / 40 | 81 / 81 | 40 / 40 |
| LiveKit, multi-region set | 1 upload; 1 SFU in London | 25 / 40 | 25 / 25 | 25 / 16 | 25 / 16 |
| **Needletail (mesh), UDP and LL-HLS** | 1 upload; 4 nodes | 25 / 16 | 25 / 16 | 16 / 16 | 16 / 16 |
| Needletail wave 3, LL-HLS / UDP (sweep 3, alone) | 1 upload; 4 nodes | 81 / 81 | 81 / 81 | 81 / 81 | 81 / 40 |
| **Needletail wave 4, LL-HLS / UDP** (sweep 4, alone) | 1 upload; 4 nodes | **122 / 81** | **122 / 122** | **122 / 122** | **122 / 40** |
| Needletail wave 4, 4K GSO off, LL-HLS / UDP (sweep 5, from 25 Mbit/s) | 1 upload; 4 nodes | 122 / 122 | 122 / 122 | 122 / 122 | 122 / 122 |
| Cloudflare Realtime | 1 upload; Cloudflare's network | 5 / 5 | 16 / 5 | 5 / 5 | 5 / none |
| Cloudflare Stream WHIP/WHEP | 1 upload; Cloudflare's network | 5 / 5 | 5 / 5 | 5 / 16 | 5 / none |

Sweep 1 ran Needletail's wave 1, sweep 2 its wave 2. A cell is the highest
rung carried; the rungs below it are in the tables, a few of them not carried
for no known cause (LiveKit Cloud's 5 Mbit/s in Sydney in sweep 2 had 90% on
time and every rung above it 100%). Cloudflare Stream's 16 in Tokyo, sweep 2,
rests on a short window and is not a carried rung in practice.

Delivery at 5 Mbit/s, median time from the moment a frame is due at the
source to its last byte at the viewer (ms), sweep 2. The WebRTC senders spread
each frame at 2.5 times the clip's rate, as libwebrtc does, so a frame's last
packet leaves up to about 17 ms after it is due; Needletail's RTMP ingest
sends each frame at once:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| **Needletail, RaptorQ FEC over UDP** | **6** | **37** | **112** | **132** |
| Needletail, LL-HLS | 7 | 38 | 113 | 132 |
| LiveKit, SFU in each region | 14 | 50 | 123 | 138 |
| LiveKit, multi-region set | 20 | 51 | 129 | 145 |
| Cloudflare Stream WHIP/WHEP | 24 | 58 | 132 | – |
| Cloudflare Realtime | 29 | 64 | 141 | – |
| **LiveKit Cloud** | 34 | 76 | 151 | – |

Where Needletail loses:

- **Throughput, against LiveKit Cloud.** Wave 4 carried 122 Mbit/s where
  LiveKit Cloud carried 164; at 164 Mbit/s wave 4 had 2% to 21% of the
  frames on time. (Wave 3: 81, with its primary relay at 132% of one vCPU at
  122.) The
  earlier builds carried 16 to 25 Mbit/s.
- **Latency at high rates.** On wave 3 at 40 Mbit/s Needletail's frames
  arrived 31, 64, 143 and 153 ms after they were due against 17, 48 to 53,
  122 to 126 and 141 to 143 for LiveKit's SFU in each region; at 81 Mbit/s
  61, 98, 174 and 178 against 19, 51, 128 and 143; wave 4 at 81 Mbit/s 43
  to 55, 94 to 100, 174 to 180 and 175 to 199. A whole 4K frame at that
  rate is about 420 kB; the likely cause, not measured hop by hop for 4K, is
  that Needletail moves it as one part through four nodes where an SFU
  forwards each packet as it comes.
- **The earlier builds' relays.** 16 to 25 Mbit/s against
  self-hosted LiveKit's 40 to 81. On wave 1 the backbone relays were the
  busiest nodes (66% to 71% of one vCPU at 40 Mbit/s, where delivery broke,
  112% to 123% at 81) but what limited wave 1 at 25 to 40 Mbit/s is not
  established. On wave 2 the secondary relay used about 120% of a vCPU
  whenever it carried a stream, at any rate, and the primary relay passed
  100% at 25 Mbit/s: a regression, reported to the product session.
- **Loss and delay at the viewer, over HTTP.** With 5% loss Needletail's
  LL-HLS reader had 92% to 94% of the frames on time with stalls of 0.5 to
  1.2 s; with bursts 69% to 83% and stalls of 1.4 to 4.4 s; with 100 ms of
  delay it fell 10 seconds behind. LiveKit's SFU in each region kept 95% to
  100% in every case (with one 2 s stall under bursts in London) and LiveKit
  Cloud 100%; the self-hosted multi-region set fell to 69% to 85% in
  Sydney. (The UDP mode, which recovers loss without a request, was not
  in these steps; it is in the audio record.)
- **The start of a crowd viewer.** A Needletail viewer starts from the
  latest key frame its edge holds, up to 2 s old, and catches up; a crowd
  viewer's 99th percentile, which includes its start, was 1.2 to 1.7 s.
  A LiveKit viewer waits for a fresh key frame instead (0.4 to 0.6 s on
  its SFU), and the worst LiveKit viewer's 99th percentile was 63 to 192 ms
  on its SFU and 78 to 204 ms on LiveKit Cloud.
- **First frame at a cold edge.** The first viewer at an edge that did not
  hold the stream waited 0.9 to 3.4 s (median) for a key frame; LiveKit's SFU
  0.4 to 0.6 s, LiveKit Cloud 0.5 to 2.4 s, Cloudflare 1.6 to 5.3 s.
- **A browser.** Needletail plays in Chrome through `hls.js`: 0.27 to 1.26 s
  glass to glass in the sweeps, and in the baseline step it barely played
  (0.4 to 1.7 frames a second). At 5 Mbit/s every WebRTC system played at 81
  to 409 ms.

Where Needletail wins:

- **Latency of what it carries.** At 5 Mbit/s (the 60-second baseline) its
  frames arrived 10 to 12 ms before those of LiveKit's SFU in each region,
  and its 99th percentile was 19, 48, 124 and 145 ms against 63, 93, 170
  and 190 (LiveKit Cloud: 78, 120, 197 and 202). The WebRTC senders' pacing
  (up to 17 ms a frame at this rate) is part or all of that lead; with the
  same pacing the two would be about level.
- **First frame at a warm edge.** 13 to 55 ms in every region: the edge
  answers with the latest key frame it holds (up to 2 s old; the viewer
  then catches up, see above). LiveKit's SFU took 256 to 791 ms, LiveKit
  Cloud 151 to 619 ms, Cloudflare 0.7 to 5.2 s.
- **A crowd at the latency of one.** 100 viewers, 25 in each region: every
  one had video, the median viewer within 2 ms of one viewer's latency, at 9,
  40, 115 and 135 ms (LiveKit's SFU 19, 49, 125 and 146, also within a few ms
  of one viewer), first key frame in 14 to 17 ms.
- **Three tiers.** From 10 to 100 viewers the contributor (85% to 87% of
  one vCPU), the primary relay (66% to 69%) and a London distributor (22% to
  23%) did not move; the London edges went from 5% to 15%. LiveKit's London
  SFU went from 10% to 41%, its multi-region node from 22% to 151%.
- **Cloudflare.** Needletail carried three to five times Cloudflare's rate in
  most regions.

What Needletail should fix to win, in order:

1. **Large frames through the mesh.** Wave 3 carries 81 Mbit/s but each
   4K frame waits to be whole at every hop: forward a part's symbols as they
   arrive (cut-through) to close the 14 to 47 ms behind LiveKit at 40 and
   81 Mbit/s.
2. **The primary relay above 81 Mbit/s** (132% of one vCPU at 122 Mbit/s on
   wave 3): forward on more than one core, batch sends, to reach LiveKit
   Cloud's 164.
3. **The UDP mode for 4K under loss**, measured beside the HTTP reader: its
   FEC should hold where the HTTP reader stalls.
4. **The start of a viewer.** Start at the latest key frame but play from
   the live edge, or send the viewer a fresh key frame, so that its first
   second is not up to 2 s behind.
5. **The distributor's hold after a lost part.** With loss on the long haul
   it held every 1 ms audio part about a second (see
   [The mesh hop by hop](#the-mesh-hop-by-hop)).
6. **A cold edge.** Subscribe an edge before its first viewer.
7. **A browser player** for the low-latency mode.

## What was compared

| System | How the video went in | How a viewer got it |
| --- | --- | --- |
| Needletail | RTMP from `lon-src` to the contributor in London (the frames are copied, not encoded again). The contributor alone on a g6-dedicated-4; two backbone relays (London and Amsterdam), and two distributors and two edges in each region, each a g6-dedicated-2. | Two client modes from an edge of the viewer's region, one frame in each part: RaptorQ FEC over UDP (`needletail-udp-client`), and LL-HLS over HTTP. |
| Cloudflare Realtime | One H.264 track and one Opus track to the SFU through its HTTPS API. | Pull of the tracks through the same API. |
| Cloudflare Stream WHIP/WHEP | The same tracks by WHIP. | WHEP. |
| LiveKit, SFU in each region | `livekit-server` 1.13.11 on one g6-dedicated-2 in each region. The source publishes to all four. | The SFU of the viewer's region. |
| LiveKit, multi-region set | The same nodes as one set on one Redis. The room is on the London node. | Signal through the node of the viewer's region, media from London. |
| LiveKit Cloud | The owner's project. | LiveKit Cloud chose the edge. |

The lab (dedicated-CPU instances only), the listeners, the clocks, the
impairments and the steal check are those of the
[audio record](2026-11-21-audio-against-cloudflare-and-livekit.md).

## Method

### The clip is a clock

The source is 64 seconds of Tears of Steel (Blender Foundation, CC BY 3.0),
3840x2160 at 24 frames a second, with the frame number burnt into the top 192
lines of every frame as two rows of 40 black and white cells
(`scripts/compare/prepare-media.py`). The senders play it in a loop that is
locked to the wall clock: frame `i` leaves when the Unix time is `i / 24`
seconds past a multiple of 64. A viewer that knows which frame it has knows
when that frame left the source.

Each rung is one H.264 elementary stream: High profile, no B-frames, a key
frame every two seconds, constant bit rate with a buffer of an eighth of a
second, so that no frame is much larger than the others. The same bytes go
into every system.

| Rung | Video that every system carries (Mbit/s) |
| --- | ---: |
| 2160p5 | 5.1 |
| 2160p8 | 9.9 |
| 2160p16 | 16.1 |
| 2160p25 | 25.1 |
| 2160p40 | 40.2 |
| 2160p80 | 80.6 |
| 2160p120 | 122.1 |
| 2160p160 | 163.9 |
| 2160p240 | 247.9 |
| 2160p320 | 332.8 |

x264 fills a constant-rate stream with filler data when the buffer is that
small. Needletail's packager drops filler, and so do WebRTC packetizers, so
every sender here drops it. The rate in the table is the video that remains.
The encoder targets were raised so that it is close to the name of the rung.

### What a viewer records

`ntcmp sub` (WebRTC), `ntcmp hls` (Needletail over LL-HLS) and `ntcmp
udp-ingest` (what `needletail-udp-client` received over UDP, with the arrival
time of each part) do not decode. They rebuild
every frame from its packets or parts and recognise it by a hash of its first
slice, so a recognised frame is the frame that was sent, and its number is
known. For each frame they record when its last byte arrived.

- **Delivery latency** is that time minus the time the frame was due at the
  source. It has no decoder and no display in it.
- A frame is **on time** when it arrived whole, can be decoded (no frame
  before it since the last key frame is missing) and was not more than 250 ms
  later than the median of the step.
- A region has **carried** a rung when the better of its two viewers (a
  g6-dedicated-2 and a g6-dedicated-4) had at least 99.5% of the frames on
  time and no stall longer than half a second. LiveKit Cloud had one viewer in each region, to stay
  inside its free allowance.
- The first five seconds of a viewer and the last three of a step are left
  out. A step is 40 seconds (30 for LiveKit Cloud).

Headless Chrome 158 plays the rungs up to 25 Mbit/s, with `hls.js` 1.7.4 for
Needletail and its own WebRTC stack for the others, and reads the burnt-in
frame number of every frame it presents. That gives glass-to-glass latency
and freezes. Chrome decodes 4K H.264 in software on these hosts, one thread
for WebRTC, so it is not asked for the higher rungs.

### A key frame on request

A WebRTC viewer that loses a picture asks the sender for a key frame, and a
live encoder answers at once. The clip cannot. Each rung up to 80 Mbit/s
therefore has a second encode with a key frame every 8 frames. On a request
the sender changes to it at its next key frame, at most a third of a second
away, and returns to the clip at the clip's next key frame. Without this a
lost picture would cost a WebRTC viewer up to two seconds, which a live
source would not.

### Pacing

A WebRTC sender spreads the packets of a frame instead of sending them at
once. The senders here spread a frame at 2.5 times the bit rate of the clip,
as libwebrtc does. Cloudflare was also swept at 1.2 times in the superseded
first pass on shared instances; it carried no more there. Needletail's RTMP
ingest sends each frame at once, so at a given rate a WebRTC frame's last
packet leaves up to 1/2.5 of a frame interval later than Needletail's.

### Sweeps

A sweep takes one system at a time from the lowest rung up, and stops when
two rungs in a row fail the harness's quick test in every region (every
viewer with at least 97% of the frames, at most two incomplete, 99th
percentile within 0.5 s of the median). That test is looser than the
"carried" rule of the tables, so some sweeps ran past their last carried
rung.

| Sweep | Start (UTC) | Needletail build | Systems |
| --- | --- | --- | --- |
| 1 | 21 Nov 21:30 | wave 1: `av-mesh` `c5d75c8`, `av-contrib` `7a41cea` | all; LiveKit Cloud 22:03 |
| 4, 5 | 22 Nov 03:18, 03:41 | wave 4: `av-mesh` `d532114` (+ `cad68c1`), `av-contrib` `996f723`, `playlists` `c14d921`, deploy tool `fb7c98e` with `topology=legacy` (its merged default failed the relay compiler's failure-domain check on this lab); sweep 5 with `relay_video_gso=false` | Needletail only |
| 3 | 22 Nov 02:38 | wave 3: `av-mesh` `8c4196d` (+ `89ecc5f`), `av-contrib` `bbb69dc`, `playlists` `89964dc`, deploy tool `ae17535` | Needletail only |
| 2 | 22 Nov 00:00 | wave 2: `av-mesh` `f378b12` (+ `34d260c`), `av-contrib` `f86c995`, smallest-part defaults | all; LiveKit Cloud 00:25, to 164 Mbit/s |

Latency, first frame, loss, delay and crowds were measured at 5 Mbit/s on
wave 2 (22 Nov 00:31 to 01:18), the long haul hop by hop (01:19 to 01:43),
and the tiers (from 01:43). LiveKit Cloud had one viewer in each region in
the sweeps, to stay inside its free allowance.

## Results

### Throughput

Each system was swept from 5 Mbit/s up, 40 seconds a rung (30 for LiveKit
Cloud), until two rungs in a row reached no region whole. The tables under
[Tables](#tables) give every rung, both sweeps.

**LiveKit Cloud** carried 164 Mbit/s whole to every region in sweep 2 and to
every region but Tokyo in sweep 1 (98.1% at 164 there, so 122), and
248 Mbit/s nowhere (62% to 79% of the frames on time). Sweep 2 had one bad
rung each in New York (81 Mbit/s, 93.6% on time) and Sydney (5 Mbit/s,
89.8%), with every rung above them whole; the cause was not found, but it was
not a rate limit.

**Self-hosted LiveKit, an SFU in each region,** carried 81 Mbit/s whole in
Tokyo in both sweeps and in London and New York in one each (95% to 99.5% of
the frames on time otherwise), and broke at 122 Mbit/s (48% to 92% on time). The SFU in London used 55% of one vCPU at 81 Mbit/s and
82% at 164; the publisher, with four uploads, had no write errors. Neither
the publisher nor the SFU instances were saturated; what limited the rate was
not measured.
The **multi-region set**, where every region's media comes from the London
node, carried 16 to 40 Mbit/s; its London node used 120% of one vCPU at
81 Mbit/s.

**Needletail** carried 25 Mbit/s whole to London and New York and 16 to Tokyo
and Sydney on wave 1, and 16 everywhere on wave 2. At 40 Mbit/s wave 1 had
69% to 98% of the frames on time, wave 2 25% to 33%. On wave 1 the backbone
relays were the busiest nodes: each used 66% to 71% of one vCPU at 40 Mbit/s,
where delivery first broke, and 112% to 123% at 81, while the contributor used 21% and 38% and the distributors
and edges less than 70%; the averages do not show why 40 Mbit/s already
failed. On wave 2 the secondary relay used about 120% of one vCPU whenever it
carried a stream, at any rate, and the primary relay 107% at 25 Mbit/s. Both client
modes carried the same rungs: the limit is upstream of the edge. Wave 3 of
`low-latency` replaced the relay's receive loop with per-class threads, and
its sweep (sweep 3, Needletail alone, 22 November 02:38) carried 81 Mbit/s to
every region over LL-HLS and to London, New York and Tokyo over UDP (99.2% in
Sydney). At 81 Mbit/s its primary relay used 86% of one vCPU, the secondary
24%, the contributor 33%; at 122 Mbit/s the primary relay reached 132% and
56% to 87% of the frames were on time. Wave 4 (sweep 4, 03:18) carried
122 Mbit/s whole to every region over LL-HLS, and over UDP to New York and
Tokyo (London 97.6%, Sydney 22% at 122); at 164 Mbit/s 2% to 21% of the
frames were on time. With its 4K UDP GSO switched off (sweep 5, from
25 Mbit/s up) it carried 122 everywhere in both client modes. The samplers of
sweep 4 missed the primary relay and the London distributor.

**Cloudflare** Realtime and Stream WHIP/WHEP carried 5 Mbit/s (not to Sydney
in sweep 2), and Realtime 16 Mbit/s once (New York, sweep 1); at 10 Mbit/s the
publishing session fell apart within seconds (a few frames, then NACK
storms). Pacing the sender at 1.2 times the rate instead of 2.5 did not help
in the superseded first pass on shared instances; it was not repeated here.

### Latency, first frame, loss and crowds

At 5 Mbit/s (wave 2; Needletail's LL-HLS reader asks for one part, one frame,
at a time; the UDP mode ran in the sweeps only):

- **Delivery.** Needletail's frames reached the viewer 10 to 12 ms before
  those of LiveKit's SFU in each region, and its 99th percentile was 44 to
  45 ms lower. LiveKit Cloud was 24 to 37 ms behind Needletail, Cloudflare 14
  to 29 ms. The WebRTC senders spread each frame over up to 17 ms at this
  rate (2.5 times the clip's rate), while the RTMP ingest does not, so part or
  all of the lead is the sender rather than the network.
- **Glass to glass.** In headless Chrome the WebRTC systems played at 81 to
  409 ms. Needletail through `hls.js` barely played in this step: 0.4 to 1.7
  frames a second, frozen 51 to 56 s of each minute; the frames it showed were
  0.25 s (London, Tokyo) to 2.5 s (New York, Sydney) old. In the sweeps at
  5 Mbit/s it played smoothly at 0.27 to 1.26 s.
- **First frame.** A viewer joining a running stream had Needletail's latest
  stored key frame (up to 2 s old, then caught up) in 13 to 55 ms, a fresh
  key frame from LiveKit's SFU in 256 to 791 ms, from LiveKit Cloud in 151 to
  619 ms, from Cloudflare in 0.7 to 5.2 s. A viewer at an edge that did not
  hold the stream (the sweeps) waited 0.7 to 3.7 s (LiveKit's SFU 0.3 to
  0.7 s).
- **Loss at the viewer.** With 1% loss LiveKit's SFU in each region,
  LiveKit Cloud and Needletail kept 100% of the frames on time; the
  multi-region set fell to 82% in Sydney and Cloudflare Realtime collapsed
  (11% to 12%). With 5% loss Needletail's HTTP reader stalled (92% to 94% on
  time) while LiveKit's SFU kept 100%; with bursts Needletail fell to 69% to
  83%, LiveKit's SFU to 94.5% to 100% with a 2 s stall in London. With 100 ms of delay Needletail's HTTP reader
  fell ten seconds behind; every LiveKit setup and Cloudflare Realtime were
  100 ms later and nothing else.
- **Crowds.** 100 viewers, 25 in each region (21 in Tokyo for the SFU in
  each region): Needletail and every LiveKit setup gave every viewer video.
  The worst viewer's 99th percentile was 63 to 204 ms on LiveKit's SFU and
  LiveKit Cloud, 1.0 to 1.2 s on the multi-region set and 1.6 to 1.7 s on
  Needletail (its start from a stored key frame). Cloudflare Realtime and
  Stream ran 0.1 to 0.7 seconds late, with their worst viewers 8 to 26
  seconds behind.

## The mesh hop by hop

The owner: "we are testing the wrong thing the mesh is UDP". Needletail's
mesh carries every part from the contributor to the backbone relays, on to
the distributors of each region and over the region's private network to its
edges, as RaptorQ FEC over UDP; only the last hop to a listener is UDP or
HTTP. From wave 2 on, every node journals the time each part passes each
stage (`GET /api/hops`), and `scripts/compare/hops.py` joins the journals on
(stream, part) with the listener's own record, so each millisecond is placed.
The clocks are chrony's; the largest offset was 0.18 ms across the ten
runs. In the 1 ms PCM runs the journal rings overflowed (the distributors'
journals start about 11 s into the 53 s window), so the hop times cover the
parts still journalled, about 80% of those the listener received.

`scripts/compare/compare-mesh.py` put the same `tc netem` on the long haul
only: on every distributor's traffic with the two backbone relays, and, for
self-hosted LiveKit, on each region's SFU's traffic with the source. The
listeners' own links stayed clean. London's distributors receive their
source over the private network from the primary relay, so London's long
haul is not impaired. The stream is wave 2's lossless PCM in 1 ms parts and
its Opus in 2.5 ms parts; the 4K stream's hops were not journalled.

1 ms PCM, no impairment, one-way time of each hop, median / 99th percentile
(ms), for the parts that a listener received over HTTP/3:

| Hop | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| contributor (spoken to sent) | 0.45 / 0.85 | 0.45 / 0.85 | 0.45 / 0.85 | 0.45 / 0.85 |
| contributor to primary relay | 0.58 / 1.73 | 0.58 / 1.74 | 0.58 / 1.73 | 0.58 / 1.73 |
| primary relay | 0.05 / 0.24 | 0.05 / 0.24 | 0.05 / 0.24 | 0.05 / 0.24 |
| long haul: relay to distributor | 0.01 / 0.10 | 31.02 / 31.24 | 106.19 / 106.43 | 125.70 / 125.95 |
| distributor | 0.12 / 0.20 | 0.09 / 0.47 | 0.09 / 0.48 | 0.10 / 0.46 |
| distributor to edge (private network) | 0.60 / 0.96 | −0.06 / 0.21 | 0.20 / 1.19 | 0.23 / 2.78 |
| edge | 0.20 / 4.99 | 0.06 / 5.15 | 0.24 / 9.94 | 0.16 / 13.52 |
| edge to listener (HTTP/3) | 0.12 / 2.37 | 0.44 / 3.14 | 0.26 / 3.55 | 0.10 / 3.71 |
| **source to listener** | **2.3 / 8.8** | **32.8 / 39.9** | **108.3 / 119.2** | **127.5 / 176.5** |

Everything but the long haul costs about 2 ms at the median. The long haul is
the Internet path from London: Linode's, the same that self-hosted LiveKit's
source uses to reach its SFUs (LiveKit's unit latency here was 1.4, 32.1,
110.4 and 127.9 ms against Needletail UDP's 1.9, 32.6, 107.8 and 127.2). A
negative median is the clock offset between two nodes.

With loss on the long haul, unit latency at the listener, median / 99th
percentile (ms), and units lost:

Needletail: the source uploads once; contributor, relay, distributor, edge.
LiveKit: one SFU in each region, the source uploads to each; the long haul is
the source's upload to the far SFUs. LiveKit Cloud's own long haul is inside
its network and cannot be impaired from outside, so it is not in this table.

| Long haul | System | New York | Tokyo | Sydney |
| --- | --- | ---: | ---: | ---: |
| 1% loss | Needletail UDP, 2.5 ms Opus | 34.9 / 40.1, 0% | 110.2 / 113.4, 0% | 129.4 / 132.0, 0% |
| 1% loss | LiveKit SFU, Opus | 34.3 / 35.0, 0.98% | 112.6 / 113.7, 1.03% | 130.2 / 130.9, 0.93% |
| 5% loss | Needletail UDP, Opus | 34.9 / 44.0, 0% | 110.2 / 117.8, 0% | 129.4 / 133.6, 0% |
| 5% loss | LiveKit SFU, Opus | 34.3 / 35.0, 4.92% | 112.6 / 113.4, 5.12% | 130.2 / 131.0, 4.86% |
| bursts | Needletail UDP, Opus | 40.0 / 918.2, 0% | 113.2 / 962.4, 0% | 396.4 / 1,083.5, 0% |
| bursts | LiveKit SFU, Opus | 34.3 / 35.1, 5.27% | 112.6 / 113.5, 4.64% | 130.2 / 130.9, 4.63% |
| 1% loss | Needletail UDP, 1 ms PCM | 940.6 / 1,033.0, 0% | 1,030.0 / 1,104.6, 0.01% | 1,043.0 / 1,126.2, 0.02% |
| 1% loss | LiveKit SFU, PCM data channel | 8,059 / 17,101, 0.08% | 19,187 / 36,813, 0.23% | 20,813 / 38,072, 0.10% |
| 5% loss | Needletail UDP, 1 ms PCM | 1,018.4 / 1,036.2, 0.25% | 1,094.4 / 1,109.4, 0.46% | 1,111.5 / 1,128.4, 0.29% |
| 5% loss | LiveKit SFU, PCM data channel | 16,332 / 31,273, 0.49% | 22,998 / 42,237, 0.19% | 17,414 / 61,666, 0.68% |
| 100 ms delay | Needletail UDP, Opus | 134.9 / 135.8, 0% | 210.3 / 211.5, 0% | 229.5 / 231.1, 0% |
| 100 ms delay | LiveKit SFU, Opus | 134.3 / 135.0, 0% | 212.7 / 213.4, 0% | 230.3 / 231.0, 0% |

The journal says where the time went. With random loss the RaptorQ repair of
the long haul recovered every Opus part, adding 0.3 to 8 ms at the 99th
percentile, where LiveKit's Opus lost what the long haul lost but kept its
99th percentile within 1 ms of clean. With bursts,
and with any loss for 1 ms PCM, the parts arrived at the distributor on time
but waited there: 0.85 to 1 s at the 99th percentile for Opus with bursts, and
about 0.9 to 1 s at the median for every PCM part. The distributor releases
parts in order and holds a hole for up to a second; with 1 ms parts a lost
object is frequent, so the whole stream runs a second late. LiveKit's PCM on a
data channel fared worse: with loss on its source-to-SFU path, its far
listeners ran 8 to 23 seconds behind at the median.

All five steps of both configurations are in
`docs/real-world-tests/evidence/20261122-compare-mesh.json`.

## Three tiers that scale separately

The owner: "the point is we want to scale distribution separately to
contributor", "and separate to distribution as we fan out". The design has
three tiers: the contributor (one for each stream), the relays and
distributors (for each stream and region), and the edges (for the audience).
`scripts/compare/tiers.py` loaded one at a time in London, on wave 2, with an
Opus stream (the DAW source's 2.5 ms Opus) or 1 ms PCM, and sampled every node
every five seconds. Listeners ran on the two London measurement hosts (6 vCPUs
in all); their own processor is given where it limited a step.

Processor of each node (% of one vCPU) and bytes out of the node (Mbit/s):

| Step | Listeners with audio | Contributor | Primary relay | Secondary relay | Distributor A, London | Distributor B, London | Edge 1, London | Edge 2, London |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 HLS listener | 1/1 | 115%, 94 | 111%, 126 | 120%, 142 | 24%, 6 | 24%, 5 | 1%, 0 | 4%, 1 |
| 10 HLS listeners | 10/10 | 114%, 94 | 115%, 126 | 119%, 141 | 23%, 6 | 24%, 6 | 1%, 0 | 13%, 2 |
| 100 HLS listeners | 100/100 | 114%, 94 | 114%, 125 | 119%, 136 | 24%, 11 | 24%, 11 | 24%, 7 | 39%, 13 |
| 1,000 HLS listeners | 486/1,000 | 116%, 94 | 115%, 123 | 119%, 129 | 26%, 11 | 24%, 11 | 141%, 42 | 101%, 43 |
| 1 UDP listener | 1/1 | 115%, 94 | 115%, 125 | 119%, 142 | 24%, 8 | 24%, 8 | 11%, 11 | 2%, 0 |
| 10 UDP listeners | 10/10 | 114%, 94 | 115%, 126 | 119%, 131 | 23%, 8 | 23%, 7 | 23%, 111 | 1%, 0 |
| 100 UDP listeners | 100/100 | 116%, 94 | 115%, 125 | 119%, 126 | 25%, 11 | 23%, 11 | 118%, 875 | 64%, 433 |
| 1,000 UDP listeners | 191/1,000 | 115%, 94 | 114%, 125 | 119%, 136 | 95%, 45 | 24%, 11 | 166%, 1,079 | 167%, 1,009 |
| 1 PCM stream, 40 listeners | 26/40 | 80%, 74 | 59%, 104 | 120%, 161 | 30%, 50 | 30%, 47 | 51%, 32 | 45%, 32 |
| 2 PCM streams, 20 listeners each | 40/40 | 168%, 149 | 115%, 129 | 122%, 146 | 31%, 69 | 30%, 59 | 35%, 18 | 41%, 24 |
| 4 PCM streams, 10 listeners each | 0/40 | 265%, 167 | 113%, 45 | 122%, 121 | 11%, 0 | 12%, 0 | 2%, 1 | 2%, 2 |

What it shows:

- **The contributor and distribution tiers are flat with the audience.**
  From 1 to 1,000 HLS listeners the contributor stayed at 114% to 116% of one
  vCPU, the relays at 111% to 120% and the London distributors at 23% to 26%
  (their bytes out rose from about 6 to 11 Mbit/s); from 1 to 100 UDP
  listeners the same. At 1,000 UDP listeners distributor A rose to 95%. (The relays'
  level is wave 2's busy relay loop; see the throughput section.) Only the
  edges grew.
- **The edges carry the audience, and run out.** Two g6-dedicated-2 edges
  served 100 HLS listeners at 24% and 39% of one vCPU. At 1,000 they
  admitted 486 (at 101% and 141% of one vCPU, the instances 52% and 72%
  busy) and refused the rest (`429`), while the listeners' hosts were 23% to
  28% busy; of the 486, 253 kept the full rate. The edges' admission, not
  their processor, set the about 250 listeners an edge took here. A test of one edge loaded to its limit was not valid
  (the harness's readers could not follow 2.5 ms parts without an index); the
  edge count was not varied.
- **The UDP mode costs the edge far more than the stream.** For each 2.5 ms
  Opus part of 46 bytes a client received 4 to 9 datagrams; the two edges
  sent 875 and 433 Mbit/s to 66 and 34 clients, about 13 Mbit/s a client for
  a 147 kbit/s stream. The UDP listeners of this test were also 1.7 to 1.9 s
  late at the median and missed 5% to 7% of the parts even alone, because
  the DAW source's stream itself lacked parts (see Open); so the UDP rows
  measure node load, not a working service. With 1,000 UDP clients
  the edges were at 166% and 167% with about 1 Gbit/s out each, and the two
  listener hosts at 94% and 96%, so that step was limited on both sides; 191
  clients had audio.
- **Streams load the contributor.** One, two and four 1 ms PCM streams took
  the contributor from 80% to 168% to 265% of one vCPU, and four were too
  many: nothing reached the listeners, and the distributors fell idle. With
  one stream only 26 of 40 listeners kept up (1 ms PCM over HTTP, as in the
  audio record's crowds).
- **An SFU's publishing node grows with the audience.** The self-hosted
  LiveKit SFU in London, with 1, 10, 100 and 1,000 subscribers of 20 ms Opus,
  used 1%, 3%, 16% and 98% of one vCPU and sent 0, 3, 29 and 114 Mbit/s; at
  1,000, 465 subscribers had audio (median 6.5 ms), about what Needletail's
  two edges admitted (486). The listener hosts, which also carried 1,000
  Cloudflare Realtime subscribers in this step, were 30% to 47% busy. To grow, LiveKit adds SFU nodes and the source publishes to each, or
  it relays between nodes (LiveKit Cloud). Cloudflare Realtime served 320 of
  1,000 in the same step; its servers cannot be sampled.

## Cost per viewer-hour

At list prices of 21 November 2026, for a viewer of 4K at 5 Mbit/s
(2.25 GB an hour) and of 2.5 ms Opus at 256 kbit/s (0.12 GB an hour). These
are prices, not bills.

| System | How it is priced | 4K at 5 Mbit/s | Opus at 256 kbit/s |
| --- | --- | ---: | ---: |
| Cloudflare Realtime | $0.05 per GB of egress after 1,000 GB a month | $0.11 | $0.006 |
| Cloudflare Stream WHIP/WHEP | $1 per 1,000 minutes delivered | $0.06 | $0.06 |
| LiveKit Cloud (Ship) | $0.0005 per connection-minute and $0.12 per GB | $0.30 | $0.04 |
| LiveKit, self-hosted, SFU in each region | four g6-dedicated-2 ($0.054 an hour each) and transfer beyond the pooled allowance at $0.005 per GB | about $0.011 at 1,000 viewers | under $0.001 at 1,000 viewers |
| Needletail | per stream: the contributor (g6-dedicated-4, $0.108), two relays and eight distributors (g6-dedicated-2), $0.65 an hour for four regions; per audience: edges (g6-dedicated-2) and transfer | about $0.012 at 1,000 viewers | about $0.002 at 1,000 viewers over LL-HLS; about $0.02 with the UDP mode as measured (13 Mbit/s a client) |

For the self-hosted systems the instance cost is shared by the audience and
the transfer dominates once it is large. Linode's transfer pool comes with
the instances (4 TB a month for a g6-dedicated-2), so a small audience pays
only the instances. The tier test below gives the audience one edge carries.

## What the comparison cost

| Account | Used | List price |
| --- | --- | ---: |
| Linode | the dedicated labs (about $2.11 an hour with Sydney, $1.65 without), build and encode hosts, a first lab of shared instances (superseded), a workbench, and about $10 to $12 of lab creations refused part-way on 22 November (each instance created is billed an hour) | about $45 in all |
| Cloudflare Realtime | about 30 GB of egress in all sessions | $0 (inside 1,000 GB a month) |
| Cloudflare Stream | about 1,630 viewer-minutes of WHEP | about $1.63 |
| LiveKit Cloud | about 75 GB of egress (33 GB of it in the superseded first pass) and about 2,900 viewer connection-minutes | above the free plan's 50 GB a month: about $3 at $0.12 a GB if the overage is billed |

The usage is estimated from the listeners' own records
(`scripts/compare/usage.py`), not read from the providers' bills.

## Other providers: published figures, not measured

These need accounts that the owner does not have, and none was opened. The
figures are what each provider publishes, not measurements.

| Provider | Published latency |
| --- | --- |
| AWS IVS | low-latency HLS under 5 s; IVS Real-Time (WebRTC) under 300 ms |
| Mux | low-latency HLS about 5 s; standard 25 to 30 s |
| Agora | 400 to 800 ms |
| Dolby OptiView (Millicast) | under 0.5 s |
| Ant Media Server | about 0.5 s over WebRTC; 3 to 5 s over LL-HLS |
| Phenix | under 0.5 s |
| Akamai (MSL5) | 5 to 6 s |
| Cloudflare's own figures | WHEP under 0.5 s; LL-HLS about 3 s; HLS under 10 s |
| LiveKit Cloud's own figure | under 100 ms |

## Received quality

Every complete frame a probe rebuilt, in every system, matched a sent frame
by the first 512 bytes and the length of its first slice (no unrecognised
frames). The probes do not compare whole frames, but no system re-encodes. So the quality at a viewer is the quality of the
rung, the same for every system: 2160p5 had a PSNR (luma) of 41.3 dB and an
SSIM of 0.968 against the composed source (`scripts/compare/quality.py`; the
figure was noted from the run and is not in the evidence). The
other rungs' measurement was lost with the workbench that held the reference
(its watchdog removed it before the results were copied); they were not
measured again. A system that drops frames or stalls loses quality by time,
not by picture, and that is in the on-time and stall figures.

## Not measured

- **Needletail's UDP mode under loss and in crowds for 4K.** It ran in the
  throughput sweeps; the latency, loss and crowd phases measured the LL-HLS
  reader only.
- **Waves 3 and 4 beside the other systems.** They were swept alone
  (throughput only); their latency phases, crowds and loss were not
  measured. Wave 4's merged topology (no backbone relays) did not pass the
  relay compiler's failure-domain check on this lab and was not measured.
- **One edge to its limit, and more edges.** The capacity steps of the tier
  test were not valid (see the tiers section) and the number of edges was
  not varied.
- **The 4K stream's hops.** The hop journal was read for the audio streams
  only.
- **Needletail in a browser at low latency.** Chrome played Needletail only
  through `hls.js` (LL-HLS); the UDP mode has no browser player.
- **Cloudflare Stream Live with RTMPS or SRT in and LL-HLS out.** Cloudflare
  reset every ingest to a live input with recording on, which HLS playback
  needs; the account has no Stream subscription, and none was bought.

## Open

- Wave 2's secondary relay used about 120% of a vCPU whenever it carried a
  stream, at any rate (about 1% with none).
- The UDP mode sends 4 to 9 datagrams for each 46-byte Opus part, about
  13 Mbit/s per client for a 147 kbit/s stream.
- The wave 2 DAW source's 2.5 ms Opus stream had playlist segments of 0.87
  to 0.93 s where 1 s was expected, so about 7% to 13% fewer parts than the
  source's time; the tier test's UDP listeners of it were late and missed
  parts (see the tiers section).
- With loss on the long haul, the distributor holds parts up to a second
  after a hole.
- LiveKit Cloud's single bad rungs in sweep 2 (5 Mbit/s in Sydney, 81 in New
  York) were not explained; every rung above them was whole.

## Evidence and harness

- [`20261122-compare-4k.json`](evidence/20261122-compare-4k.json): the
  three throughput sweeps (the third Needletail wave 3 alone, session
  `video-w3n`), every rung, system, region and viewer (frames on time,
  latency, stalls, bit rate, first frame), the Chrome players and the
  processor of every node; and the latency, loss, delay and crowd phases at
  5 Mbit/s.
- [`20261122-compare-mesh.json`](evidence/20261122-compare-mesh.json): the
  long-haul session, the hop journal of every part joined by `hops.py`, with
  the listeners' figures.
- [`20261122-compare-tiers.json`](evidence/20261122-compare-tiers.json): the
  tier test, node by node.
- Raw records are under `target/compare/runs/{video-d1b,video-d1bc,video-w2,video-w2c,video-wp,mesh-w,tiers-w}/`
  and are not versioned.
- Harness on branch `compare-cloudflare`: `scripts/compare/compare-run.py`
  (sweeps and phases), `compare-mesh.py` with `hops.py` and `mesh-report.py`
  (the long haul), `tiers.py` and `tiers-report.py`, `compare-analyse.py`,
  `compare-video-report.py`, `video-phases-report.py`, `usage.py`,
  `tools/ntcmp` (the paced sender, the WebRTC and HLS probes, `udp-ingest`).

## Tables

### Sweep 7: Needletail wave 5, GSO on, alone

Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest with every rung below it carried too. Each session is one sweep.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 333 (164) | 247 | 247 | none |
| Needletail, LL-HLS | 247 | 247 | 164 | none |

#### Every rung

Frames on time (%) at the better of the two listeners of each region, and in brackets the bit rate that listener received (Mbit/s).

Needletail, RaptorQ FEC over UDP from the edge:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | – |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | – |
| 80 | 100.0 (82) | 100.0 (82) | 100.0 (82) | – |
| 122 | 100.0 (124) | 100.0 (124) | 100.0 (124) | – |
| 164 | 100.0 (164) | 100.0 (164) | 100.0 (164) | – |
| 247 | 78.5 (243) | 100.0 (247) | 100.0 (246) | – |
| 333 | 100.0 (340) | 98.3 (335) | 0 (338) | – |

Needletail, LL-HLS:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | – |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | – |
| 80 | 100.0 (82) | 100.0 (82) | 100.0 (81) | – |
| 122 | 100.0 (124) | 100.0 (123) | 100.0 (124) | – |
| 164 | 100.0 (163) | 100.0 (162) | 100.0 (163) | – |
| 247 | 100.0 (248) | 100.0 (247) | 98.7 (244) | – |
| 333 | 19.3 (0) | 71.3 (273) | 0 (0) | – |

#### Latency of what was carried

Delivery latency at the listener's socket, median (ms): from the moment a frame is due at the source to its last byte at the listener:

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 9 | 46 | 119 | – |
| Needletail, LL-HLS | 9 | 45 | 120 | – |

Glass to glass in headless Chrome, median (ms):

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 2162 | 2202 | 2233 | – |

Chrome: time frozen (ms in each minute):

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | – |

#### Time to the first frame

At 5 Mbit/s, from the listener's start to its first complete key frame (ms). The clip has a key frame every two seconds; a WebRTC sender answers a request for one within a third of a second.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | – | – | – | – |
| Needletail, LL-HLS | – | – | – | – |
| Cloudflare Realtime | – | – | – | – |
| Cloudflare Stream WHIP/WHEP | – | – | – | – |
| LiveKit, SFU in each region | – | – | – | – |
| LiveKit, multi-region set | – | – | – | – |
| LiveKit Cloud | – | – | – | – |

#### Processor of the nodes

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 25 | 40 | 80 | 122 | 164 | 247 | 333 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 12 | 76 | 96 | 107 | 67 | 140 | 179 |
| Needletail primary relay | – | – | – | – | – | – | – |
| Needletail secondary relay | – | – | – | – | – | – | – |
| Needletail distributor, London | 14 | 110 | 126 | 141 | 67 | -385 | 189 |
| Needletail edge 1, London | 22 | 26 | 43 | 63 | 83 | 87 | -320 |
| Needletail distributor, Sydney | – | – | – | – | – | – | – |

### Sweep 6: wave 5 (GSO off) beside the other systems

Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest with every rung below it carried too. Each session is one sweep.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 16 | 80 | 80 | none |
| Needletail, LL-HLS | 80 (16) | 80 | 80 | none |
| Cloudflare Realtime | 16 | 10 | 16 | none |
| Cloudflare Stream WHIP/WHEP | 16 (5) | none | 16 (5) | none |
| LiveKit, SFU in each region | 80 | 80 | 40 | none |
| LiveKit, multi-region set | 40 | 25 | 25 | none |

#### Every rung

Frames on time (%) at the better of the two listeners of each region, and in brackets the bit rate that listener received (Mbit/s).

Needletail, RaptorQ FEC over UDP from the edge:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) | 100.0 (6) | 100.0 (6) | – |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | – |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | – |
| 25 | 94.4 (25) | 100.0 (25) | 100.0 (25) | – |
| 40 | 96.6 (40) | 100.0 (40) | 100.0 (40) | – |
| 80 | 90.5 (81) | 100.0 (81) | 100.0 (81) | – |
| 122 | 50.8 (106) | 75.8 (124) | 22.4 (73) | – |
| 164 | 17.0 (106) | 76.1 (168) | 25.3 (156) | – |

Needletail, LL-HLS:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) | 100.0 (6) | 100.0 (6) | – |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | – |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | – |
| 25 | 96.3 (25) | 100.0 (25) | 100.0 (25) | – |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (41) | – |
| 80 | 100.0 (81) | 100.0 (81) | 100.0 (81) | – |
| 122 | 42.7 (25) | 40.6 (0) | 39.3 (0) | – |
| 164 | 22.9 (0) | 31.9 (0) | 18.3 (0) | – |

Cloudflare Realtime:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (5) | 100.0 (5) | 100.0 (5) | – |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | – |
| 16 | 100.0 (17) | 93.5 (17) | 100.0 (16) | – |
| 25 | 0.4 (2) | 0.0 (2) | 0.8 (2) | – |
| 40 | 62.7 (5) | 0 (3) | 0 (3) | – |

Cloudflare Stream WHIP/WHEP:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) | 98.7 (6) | 100.0 (6) | – |
| 10 | 0 (0) | 0 (0) | 0 (0) | – |
| 16 | 100.0 (16) | 98.8 (16) | 100.0 (16) | – |
| 25 | 0 (1) | 0 (1) | 0 (1) | – |
| 40 | 0 (3) | 0 (3) | 0 (3) | – |

LiveKit, SFU in each region:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) | 100.0 (6) | 100.0 (6) | – |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | – |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | – |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | – |
| 40 | 100.0 (40) | 100.0 (41) | 100.0 (41) | – |
| 80 | 100.0 (81) | 100.0 (81) | 87.3 (81) | – |
| 122 | 67.9 (122) | 91.8 (122) | 21.9 (121) | – |
| 164 | 1.2 (159) | 33.6 (163) | 0.8 (160) | – |
| 247 | 0.2 (187) | 0.1 (225) | 0 (168) | – |

LiveKit, multi-region set:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (5) | 100.0 (5) | 100.0 (4) | – |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | – |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | – |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | – |
| 40 | 100.0 (40) | 89.0 (40) | 88.0 (40) | – |
| 80 | 36.2 (80) | 22.9 (80) | 23.0 (80) | – |
| 122 | 0.4 (99) | 0.3 (98) | 0.3 (98) | – |

#### Latency of what was carried

Delivery latency at the listener's socket, median (ms): from the moment a frame is due at the source to its last byte at the listener:

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 6 | 42 | 114 | – |
| Needletail, LL-HLS | 5 | 41 | 113 | – |
| Cloudflare Realtime | 20 | 55 | 130 | – |
| Cloudflare Stream WHIP/WHEP | 22 | – | 136 | – |
| LiveKit, SFU in each region | 19 | 50 | 124 | – |
| LiveKit, multi-region set | 13 | 47 | 120 | – |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 7 | 43 | 115 | – |
| Needletail, LL-HLS | 6 | 42 | 114 | – |
| Cloudflare Realtime | 21 | 56 | 129 | – |
| LiveKit, SFU in each region | 17 | 48 | 122 | – |
| LiveKit, multi-region set | 18 | 51 | 126 | – |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 9 | 44 | 117 | – |
| Needletail, LL-HLS | 8 | 43 | 116 | – |
| Cloudflare Realtime | 22 | – | 132 | – |
| Cloudflare Stream WHIP/WHEP | 24 | – | 136 | – |
| LiveKit, SFU in each region | 17 | 48 | 124 | – |
| LiveKit, multi-region set | 18 | 49 | 126 | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | – | 47 | 120 | – |
| Needletail, LL-HLS | – | 46 | 120 | – |
| LiveKit, SFU in each region | 17 | 48 | 123 | – |
| LiveKit, multi-region set | 23 | 60 | 130 | – |

Glass to glass in headless Chrome, median (ms):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 2331 | 2578 | 2525 | – |
| Cloudflare Realtime | 84 | 123 | 195 | – |
| Cloudflare Stream WHIP/WHEP | 97 | 128 | 210 | – |
| LiveKit, SFU in each region | 62 | 126 | 190 | – |
| LiveKit, multi-region set | 102 | 140 | 205 | – |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 811 | 668 | 923 | – |
| Cloudflare Realtime | 98 | 137 | 210 | – |
| LiveKit, SFU in each region | 86 | 116 | 197 | – |
| LiveKit, multi-region set | 99 | 138 | 203 | – |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 967 | 715 | 979 | – |
| Cloudflare Realtime | 121 | 152 | 225 | – |
| Cloudflare Stream WHIP/WHEP | 127 | 174 | 238 | – |
| LiveKit, SFU in each region | 99 | 130 | 211 | – |
| LiveKit, multi-region set | 106 | 145 | 226 | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1532 | 2204 | 1702 | – |
| LiveKit, SFU in each region | 106 | 136 | 209 | – |
| LiveKit, multi-region set | 137 | 201 | 241 | – |

Chrome: time frozen (ms in each minute):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 35486 | 45153 | 44110 | – |
| Cloudflare Realtime | 0 | 0 | 0 | – |
| Cloudflare Stream WHIP/WHEP | 1427 | 0 | 3536 | – |
| LiveKit, SFU in each region | 0 | 0 | 0 | – |
| LiveKit, multi-region set | 0 | 0 | 0 | – |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | – |
| Cloudflare Realtime | 2207 | 0 | 859 | – |
| LiveKit, SFU in each region | 0 | 0 | 0 | – |
| LiveKit, multi-region set | 0 | 0 | 0 | – |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 2592 | 311 | 1830 | – |
| Cloudflare Realtime | 2613 | 0 | 391 | – |
| Cloudflare Stream WHIP/WHEP | 2835 | 10187 | 1432 | – |
| LiveKit, SFU in each region | 0 | 0 | 0 | – |
| LiveKit, multi-region set | 0 | 0 | 0 | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 6666 | 3596 | 1462 | – |
| LiveKit, SFU in each region | 0 | 0 | 0 | – |
| LiveKit, multi-region set | 0 | 0 | 0 | – |

#### Time to the first frame

At 5 Mbit/s, from the listener's start to its first complete key frame (ms). The clip has a key frame every two seconds; a WebRTC sender answers a request for one within a third of a second.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 2069 (2059–2080) | 2256 (2252–2260) | 1962 (1961–1963) | – |
| Needletail, LL-HLS | 459 | 408 (404–412) | 508 (490–526) | – |
| Cloudflare Realtime | 1953 (1951–1956) | 2152 (2151–2154) | 4867 (3870–5864) | – |
| Cloudflare Stream WHIP/WHEP | 2191 (2190–2191) | 2879 (2391–3368) | 6088 (6084–6092) | – |
| LiveKit, SFU in each region | 308 (305–311) | 510 (505–515) | 560 (558–562) | – |
| LiveKit, multi-region set | 438 (435–440) | 1472 (1311–1633) | 4178 (4009–4346) | – |
| LiveKit Cloud | – | – | – | – |

#### Processor of the nodes

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 5 | 10 | 16 | 25 | 40 | 80 | 122 | 164 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 8 | 7 | 9 | 14 | 21 | 50 | 86 | 107 |
| Needletail primary relay | – | – | – | – | – | – | – | – |
| Needletail secondary relay | – | – | – | – | – | – | – | – |
| Needletail distributor, London | 8 | 9 | 12 | 18 | 26 | 71 | 152 | 190 |
| Needletail edge 1, London | 9 | 13 | 17 | 29 | 35 | 44 | 66 | 70 |
| Needletail distributor, Sydney | – | – | – | – | – | – | – | – |

LiveKit, SFU in each region:

| Node | 5 | 10 | 16 | 25 | 40 | 80 | 122 | 164 | 247 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LiveKit node, London | 11 | 17 | 23 | 35 | 33 | 58 | 72 | 82 | 104 |
| LiveKit node, Sydney | – | – | – | – | – | – | – | – | – |

LiveKit, multi-region set:

| Node | 5 | 10 | 16 | 25 | 40 | 80 | 122 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LiveKit node, London (serves every region) | 20 | 33 | 49 | 69 | 66 | 113 | 121 |

### Sweep 9: wave 5, GSO off, the edges' fast plane off

Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest with every rung below it carried too. Each session is one sweep.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 122 | 80 | 122 | none |
| Needletail, LL-HLS | 164 | 164 | 122 | none |

#### Every rung

Frames on time (%) at the better of the two listeners of each region, and in brackets the bit rate that listener received (Mbit/s).

Needletail, RaptorQ FEC over UDP from the edge:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | – |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | – |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | – |
| 80 | 100.0 (81) | 100.0 (81) | 100.0 (81) | – |
| 122 | 100.0 (123) | 21.4 (84) | 100.0 (121) | – |
| 164 | 95.6 (162) | 12.4 (70) | 4.3 (104) | – |

Needletail, LL-HLS:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | – |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | – |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | – |
| 80 | 100.0 (79) | 100.0 (81) | 100.0 (80) | – |
| 122 | 100.0 (123) | 100.0 (122) | 100.0 (122) | – |
| 164 | 100.0 (161) | 100.0 (164) | 4.9 (0) | – |

#### Latency of what was carried

Delivery latency at the listener's socket, median (ms): from the moment a frame is due at the source to its last byte at the listener:

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 10 | 48 | 119 | – |
| Needletail, LL-HLS | 12 | 47 | 121 | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 13 | 53 | 123 | – |
| Needletail, LL-HLS | 15 | 51 | 126 | – |

Glass to glass in headless Chrome, median (ms):

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 296 | 1210 | 466 | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1743 | 1690 | 447 | – |

Chrome: time frozen (ms in each minute):

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | – |

#### Time to the first frame

At 5 Mbit/s, from the listener's start to its first complete key frame (ms). The clip has a key frame every two seconds; a WebRTC sender answers a request for one within a third of a second.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | – | – | – | – |
| Needletail, LL-HLS | – | – | – | – |
| Cloudflare Realtime | – | – | – | – |
| Cloudflare Stream WHIP/WHEP | – | – | – | – |
| LiveKit, SFU in each region | – | – | – | – |
| LiveKit, multi-region set | – | – | – | – |
| LiveKit Cloud | – | – | – | – |

#### Processor of the nodes

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 16 | 25 | 40 | 80 | 122 | 164 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 21 | 14 | 18 | 32 | 42 | 62 |
| Needletail primary relay | – | – | – | – | – | – |
| Needletail secondary relay | – | – | – | – | – | – |
| Needletail distributor, London | 22 | 18 | 23 | 42 | 63 | 85 |
| Needletail edge 1, London | 16 | 24 | 35 | 62 | 98 | 118 |
| Needletail distributor, Sydney | – | – | – | – | – | – |

### Sweep 8: wave 5, legacy topology, GSO off

Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest with every rung below it carried too. Each session is one sweep.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 80 | 40 | 10 (none) | none |
| Needletail, LL-HLS | 40 | 40 (10) | 10 (none) | none |

#### Every rung

Frames on time (%) at the better of the two listeners of each region, and in brackets the bit rate that listener received (Mbit/s).

Needletail, RaptorQ FEC over UDP from the edge:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (5) | 100.0 (5) | 99.5 (5) | – |
| 10 | 100.0 (10) | 100.0 (10) | 99.7 (10) | – |
| 16 | 100.0 (16) | 99.7 (16) | 99.5 (16) | – |
| 25 | 100.0 (25) | 100.0 (25) | 99.5 (25) | – |
| 40 | 100.0 (40) | 99.9 (40) | 99.5 (40) | – |
| 80 | 99.9 (80) | 99.4 (81) | 97.7 (81) | – |
| 122 | 99.2 (122) | 98.0 (122) | 97.5 (121) | – |
| 164 | 22.2 (141) | 21.8 (142) | 22.3 (142) | – |
| 247 | 0.7 (94) | 2.0 (97) | 2.8 (94) | – |

Needletail, LL-HLS:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (5) | 100.0 (5) | 81.3 (3) | – |
| 10 | 100.0 (10) | 100.0 (10) | 99.7 (10) | – |
| 16 | 100.0 (16) | 98.6 (16) | 81.2 (16) | – |
| 25 | 100.0 (25) | 100.0 (25) | 99.1 (25) | – |
| 40 | 100.0 (40) | 99.6 (40) | 99.5 (40) | – |
| 80 | 98.2 (79) | 97.7 (81) | 90.2 (80) | – |
| 122 | 95.5 (122) | 77.9 (118) | 74.2 (113) | – |
| 164 | 12.7 (0) | 13.6 (0) | 11.6 (0) | – |
| 247 | 0 (0) | 0 (0) | 0 (0) | – |

#### Latency of what was carried

Delivery latency at the listener's socket, median (ms): from the moment a frame is due at the source to its last byte at the listener:

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 4 | 36 | – | – |
| Needletail, LL-HLS | 4 | 34 | – | – |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 7 | 38 | 117 | – |
| Needletail, LL-HLS | 6 | 36 | 117 | – |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 8 | 43 | – | – |
| Needletail, LL-HLS | 9 | – | – | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 11 | 46 | – | – |
| Needletail, LL-HLS | 13 | 42 | – | – |

Glass to glass in headless Chrome, median (ms):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 2238 | 2244 | 7825 | – |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 968 | 1184 | 1082 | – |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1024 | 1198 | 1279 | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1889 | 1928 | 2051 | – |

Chrome: time frozen (ms in each minute):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 55822 | 51378 | 8908 | – |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 361 | 0 | – |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 16545 | 25731 | – |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | – |

#### Time to the first frame

At 5 Mbit/s, from the listener's start to its first complete key frame (ms). The clip has a key frame every two seconds; a WebRTC sender answers a request for one within a third of a second.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 1216 (1214–1218) | 1413 (1412–1415) | 1119 (1118–1121) | – |
| Needletail, LL-HLS | 719 (717–720) | 696 (692–700) | 756 (740–773) | – |
| Cloudflare Realtime | – | – | – | – |
| Cloudflare Stream WHIP/WHEP | – | – | – | – |
| LiveKit, SFU in each region | – | – | – | – |
| LiveKit, multi-region set | – | – | – | – |
| LiveKit Cloud | – | – | – | – |

#### Processor of the nodes

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 5 | 10 | 16 | 25 | 40 | 80 | 122 | 164 | 247 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 76 | 64 | 61 | 45 | 37 | 44 | 45 | 62 | 86 |
| Needletail primary relay | 86 | 72 | 58 | 68 | 68 | 114 | 150 | 188 | 151 |
| Needletail secondary relay | 114 | 101 | 78 | 60 | 46 | 54 | 64 | 62 | 87 |
| Needletail distributor, London | 46 | 44 | 40 | 37 | 37 | 55 | 80 | 110 | 76 |
| Needletail edge 1, London | 11 | 16 | 22 | 31 | 43 | 89 | 130 | 156 | 104 |
| Needletail distributor, Sydney | – | – | – | – | – | – | – | – | – |

### Sweeps 1 and 2

Each cell of these tables holds sweep 1 / sweep 2.

### Highest bit rate carried whole

Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest with every rung below it carried too. Each session is one sweep.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 25 / 16 | 25 / 16 | 16 / 16 | 16 / 16 (5) |
| Needletail, LL-HLS | 25 / 16 | 25 / 16 | 16 / 16 | 16 / 16 |
| Cloudflare Realtime | 5 / 5 | 16 (5) / 5 | 5 / 5 | 5 / none |
| Cloudflare Stream WHIP/WHEP | 5 / 5 | 5 / 5 | 5 / 16 (5) | 5 / none |
| LiveKit, SFU in each region | 40 / 81 | 81 / 40 | 81 / 81 | 40 / 40 |
| LiveKit, multi-region set | 25 / 40 | 25 / 25 | 25 / 16 | 25 / 16 |
| LiveKit Cloud | 164 / 164 | 164 / 164 (40) | 122 / 164 | 164 / 164 (none) |

### Every rung

Frames on time (%) at the better of the two listeners of each region, and in brackets the bit rate that listener received (Mbit/s).

Needletail, RaptorQ FEC over UDP from the edge:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) / 100.0 (4) | 100.0 (6) / 100.0 (4) | 100.0 (6) / 100.0 (5) | 100.0 (6) / 100.0 (5) |
| 10 | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 99.6 (10) / 99.3 (10) |
| 16 | 99.7 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) | 99.9 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) |
| 25 | 100.0 (25) / 68.2 (24) | 100.0 (25) / 68.3 (24) | 98.6 (25) / 68.0 (24) | 98.8 (25) / 68.3 (24) |
| 40 | 98.1 (40) / 29.4 (29) | 91.3 (40) / 29.7 (31) | 69.0 (40) / 24.8 (29) | 81.1 (40) / 29.7 (30) |
| 81 | 9.7 (54) | 6.3 (54) | 9.7 (47) | 2.1 (53) |
| 122 | 0.7 (54) | 0.6 (52) | 0.5 (46) | 0.4 (42) |
| 164 | 0.9 (53) | 0.6 (45) | 0.1 (25) | 0.0 (21) |
| 248 | 0.2 (20) | 0.2 (19) | 0 (0) | 0 (0) |

Needletail, LL-HLS:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) / 100.0 (4) | 100.0 (6) / 100.0 (5) | 100.0 (6) / 100.0 (5) | 100.0 (6) / 100.0 (5) |
| 10 | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 99.6 (10) / 100.0 (10) |
| 16 | 99.7 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) | 99.9 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) |
| 25 | 100.0 (25) / 70.7 (23) | 100.0 (25) / 70.5 (23) | 98.6 (25) / 70.7 (24) | 97.6 (25) / 70.9 (24) |
| 40 | 98.0 (40) / 33.0 (0) | 96.8 (40) / 33.0 (0) | 51.5 (33) / 29.3 (0) | 64.2 (40) / 33.2 (0) |
| 81 | 5.4 (0) | 0 (0) | 0 (0) | 0 (0) |
| 122 | 0 (0) | 0 (0) | 0 (0) | 0 (0) |
| 164 | 0 (0) | 0 (0) | 0 (0) | 0 (0) |
| 248 | 0 (0) | 0 (0) | 0 (0) | 0 (0) |

Cloudflare Realtime:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (5) / 100.0 (5) | 100.0 (5) / 100.0 (5) | 100.0 (5) / 100.0 (5) | 100.0 (5) / 92.8 (5) |
| 10 | 0 (0) / 57.7 (1) | 0 (0) / 55.8 (4) | 0 (0) / 51.8 (5) | 0 (0) / 12.9 (0) |
| 16 | 96.7 (16) / 14.8 (5) | 100.0 (16) / 13.3 (10) | 96.7 (16) / 5.1 (6) | 96.6 (16) / 2.3 (10) |
| 25 | 0 (7) | 0 (8) | 0 (8) | 0 (8) |
| 40 | 0.2 (14) | 0.2 (14) | 0.0 (16) | 0.2 (13) |

Cloudflare Stream WHIP/WHEP:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) / 100.0 (6) | 100.0 (6) / 100.0 (6) | 100.0 (6) / 100.0 (6) | 100.0 (6) / 68.5 (33) |
| 10 | 5.3 (6) / 67.3 (8) | 1.6 (7) / 98.6 (7) | 0.2 (2) / 78.9 (7) | 0.0 (4) / 6.1 (1) |
| 16 | 0 (1) / 94.0 (0) | 0 (1) / 92.7 (5) | 0 (1) / 100.0 (2) | 0 (1) / 0 (0) |

LiveKit, SFU in each region:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (5) / 100.0 (5) | 100.0 (5) / 100.0 (5) | 100.0 (5) / 100.0 (5) | 100.0 (5) / 100.0 (5) |
| 10 | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) |
| 16 | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) |
| 25 | 100.0 (25) / 100.0 (25) | 100.0 (25) / 100.0 (25) | 100.0 (25) / 100.0 (25) | 100.0 (25) / 100.0 (25) |
| 40 | 100.0 (41) / 100.0 (41) | 100.0 (40) / 100.0 (40) | 100.0 (40) / 100.0 (40) | 100.0 (40) / 100.0 (40) |
| 81 | 99.5 (82) / 100.0 (82) | 100.0 (81) / 97.9 (82) | 100.0 (82) / 100.0 (82) | 95.2 (82) / 97.9 (82) |
| 122 | 69.3 (124) / 48.5 (125) | 55.1 (123) / 59.1 (124) | 54.8 (124) / 78.2 (125) | 91.7 (124) / 83.2 (125) |
| 164 | 2.6 (164) / 8.6 (163) | 0.5 (160) / 2.8 (159) | 3.6 (164) / 0.3 (160) | 10.5 (166) / 11.7 (165) |

LiveKit, multi-region set:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) / 100.0 (6) | 100.0 (6) / 100.0 (6) | 100.0 (5) / 100.0 (6) | 100.0 (6) / 100.0 (6) |
| 10 | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) |
| 16 | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) |
| 25 | 100.0 (25) / 100.0 (25) | 100.0 (25) / 100.0 (25) | 100.0 (25) / 92.8 (25) | 100.0 (25) / 92.5 (25) |
| 40 | 93.7 (41) / 100.0 (41) | 93.5 (41) / 96.3 (41) | 92.8 (41) / 92.0 (41) | 92.8 (41) / 91.8 (41) |
| 81 | 0.5 (70) / 4.8 (71) | 1.0 (68) / 1.6 (70) | 1.4 (67) / 2.0 (70) | 1.3 (68) / 2.8 (70) |
| 122 | 0.6 (71) / 0.1 (72) | 0.2 (73) / 0.1 (72) | 0.2 (70) / 0.2 (69) | 0.2 (70) / 0.2 (69) |

LiveKit Cloud:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) / 100.0 (6) | 100.0 (6) / 100.0 (6) | 100.0 (6) / 100.0 (6) | 100.0 (6) / 89.8 (6) |
| 10 | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) | 100.0 (10) / 100.0 (10) |
| 16 | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) | 100.0 (16) / 100.0 (16) |
| 25 | 100.0 (25) / 100.0 (25) | 100.0 (26) / 100.0 (25) | 100.0 (25) / 100.0 (25) | 100.0 (25) / 100.0 (25) |
| 40 | 100.0 (40) / 100.0 (41) | 100.0 (40) / 100.0 (41) | 100.0 (40) / 100.0 (41) | 100.0 (40) / 100.0 (41) |
| 81 | 100.0 (83) / 100.0 (81) | 100.0 (83) / 93.6 (82) | 100.0 (82) / 100.0 (82) | 100.0 (82) / 100.0 (82) |
| 122 | 100.0 (122) / 100.0 (123) | 100.0 (123) / 100.0 (123) | 100.0 (123) / 100.0 (122) | 100.0 (123) / 100.0 (122) |
| 164 | 100.0 (163) / 100.0 (169) | 100.0 (162) / 100.0 (168) | 98.1 (161) / 100.0 (168) | 100.0 (161) / 100.0 (168) |
| 248 | 61.6 (256) | 69.6 (255) | 78.7 (255) | 68.2 (255) |

### Latency of what was carried

Delivery latency at the listener's socket, median (ms): from the moment a frame is due at the source to its last byte at the listener:

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 7 (6–8) | 38 (37–38) | 114 (112–115) | 133 (132–133) |
| Needletail, LL-HLS | 8 (7–9) | 38 (38–39) | 114 (113–116) | 133 (132–134) |
| Cloudflare Realtime | 25 (20–29) | 59 (54–64) | 135 (128–141) | 186 |
| Cloudflare Stream WHIP/WHEP | 24 (23–24) | 58 | 133 (132–134) | 146 |
| LiveKit, SFU in each region | 14 (13–14) | 47 (44–50) | 121 (119–123) | 138 |
| LiveKit, multi-region set | 19 | 51 (51–52) | 127 (126–129) | 145 |
| LiveKit Cloud | 33 (31–34) | 75 (74–76) | 149 (146–151) | 156 |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 11 | 41 | 118 (117–119) | 136 |
| Needletail, LL-HLS | 12 | 42 | 119 (118–120) | 137 |
| LiveKit, SFU in each region | 18 | 53 | 127 | 143 |
| LiveKit, multi-region set | 18 | 54 | 126 (125–126) | 143 |
| LiveKit Cloud | 31 | 73 | 147 (146–148) | 154 (152–156) |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 16 (15–17) | 46 (46–47) | 123 | 142 (141–144) |
| Needletail, LL-HLS | 17 (16–18) | 47 (46–48) | 124 | 141 (141–142) |
| Cloudflare Realtime | – | 58 | – | – |
| Cloudflare Stream WHIP/WHEP | – | – | 192 | – |
| LiveKit, SFU in each region | 17 | 48 | 125 (123–126) | 143 |
| LiveKit, multi-region set | 20 | 52 (51–54) | 129 (128–130) | 146 |
| LiveKit Cloud | 31 | 73 | 147 (146–148) | 155 |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 21 | 52 | – | – |
| Needletail, LL-HLS | 23 | 53 | – | – |
| LiveKit, SFU in each region | 17 | 51 (48–54) | 126 | 143 |
| LiveKit, multi-region set | 31 | 64 (62–66) | 140 | 157 |
| LiveKit Cloud | 31 | 73 | 147 (146–148) | 154 (152–156) |

Glass to glass in headless Chrome, median (ms):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1070 (980–1160) | 924 (844–1005) | 766 (267–1264) | 1149 (1101–1198) |
| Cloudflare Realtime | 111 (92–131) | 145 (122–168) | 223 (204–243) | 258 (252–264) |
| Cloudflare Stream WHIP/WHEP | 98 | 132 (127–137) | 207 (203–210) | 303 (223–383) |
| LiveKit, SFU in each region | 88 (83–93) | 126 (121–130) | 188 (170–205) | 214 (209–219) |
| LiveKit, multi-region set | 100 (99–100) | 125 (121–130) | 204 (203–204) | 221 (218–224) |
| LiveKit Cloud | 114 (109–119) | 152 (147–156) | 226 (221–231) | 235 (234–236) |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 806 (446–1166) | 659 (315–1004) | 1088 (1086–1090) | 930 (570–1291) |
| Cloudflare Realtime | 138 | 217 | 1541 | 955 |
| Cloudflare Stream WHIP/WHEP | 156 | 1076 (168–1984) | 1593 (401–2784) | – |
| LiveKit, SFU in each region | 94 (91–97) | 132 (128–135) | 202 (201–203) | 220 (217–224) |
| LiveKit, multi-region set | 98 (91–105) | 131 (128–134) | 202 (201–203) | 223 (222–224) |
| LiveKit Cloud | 100 (99–101) | 146 (145–147) | 221 (220–221) | 234 (233–234) |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1020 (972–1068) | 1107 (1006–1208) | 1548 (1275–1821) | 483 (277–688) |
| Cloudflare Realtime | 203 (121–285) | 349 (159–540) | 233 | 297 |
| Cloudflare Stream WHIP/WHEP | 162 | 183 | 308 | 388 |
| LiveKit, SFU in each region | 97 (96–97) | 130 (125–134) | 209 (208–209) | 222 (222–223) |
| LiveKit, multi-region set | 112 (112–113) | 142 (141–143) | 224 (224–225) | 238 (237–239) |
| LiveKit Cloud | 111 (108–114) | 148 (143–153) | 231 (226–236) | 240 (239–241) |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1192 (307–2077) | 1254 (294–2214) | 2362 (2286–2439) | 1476 (657–2294) |
| LiveKit, SFU in each region | 115 (112–118) | 140 (140–141) | 273 (266–280) | 236 (236–237) |
| LiveKit, multi-region set | 181 (178–185) | 189 (189–190) | 327 (305–349) | 290 (285–294) |
| LiveKit Cloud | 118 (112–122) | 159 (158–160) | 246 (242–251) | 243 (240–246) |

Chrome: time frozen (ms in each minute):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 0 |
| Cloudflare Realtime | 1456 (0–2911) | 0 | 1472 (0–2945) | 2166 (0–4332) |
| Cloudflare Stream WHIP/WHEP | 1160 (0–2319) | 0 | 0 | 9195 (0–18390) |
| LiveKit, SFU in each region | 0 | 0 | 0 | 0 |
| LiveKit, multi-region set | 0 | 0 | 0 | 0 |
| LiveKit Cloud | 0 | 0 | 0 | 446 (0–891) |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 1485 (0–2970) | 0 |
| Cloudflare Realtime | 3503 | 4009 | 785 | 5139 |
| Cloudflare Stream WHIP/WHEP | 4620 | 19150 (0–38301) | 9353 (1157–17549) | – |
| LiveKit, SFU in each region | 0 | 0 | 0 | 0 |
| LiveKit, multi-region set | 0 | 0 | 0 | 0 |
| LiveKit Cloud | 0 | 0 | 0 | 0 |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 1554 (0–3109) | 0 |
| Cloudflare Realtime | 26752 (2695–50810) | 29007 (3152–54862) | 0 | 2134 |
| Cloudflare Stream WHIP/WHEP | 22427 | 4680 | 13315 | 41786 |
| LiveKit, SFU in each region | 0 | 0 | 0 | 0 |
| LiveKit, multi-region set | 0 | 0 | 0 | 0 |
| LiveKit Cloud | 0 | 0 | 0 | 0 |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 4580 (0–9161) | 1864 (0–3727) | 0 |
| LiveKit, SFU in each region | 0 | 0 | 0 | 0 |
| LiveKit, multi-region set | 0 | 0 | 0 | 0 |
| LiveKit Cloud | 0 | 0 | 0 | 0 |

### Time to the first frame

At 5 Mbit/s, from the listener's start to its first complete key frame (ms). The clip has a key frame every two seconds; a WebRTC sender answers a request for one within a third of a second.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 2347 (1218–3531) | 3399 (1703–3708) | 3283 (3112–3424) | 1170 (1014–1327) |
| Needletail, LL-HLS | 1998 (744–3522) | 2079 (688–3693) | 3257 (3097–3414) | 853 (797–883) |
| Cloudflare Realtime | 1647 (1569–1725) | 2010 (1903–2119) | 4054 (3618–4494) | 3111 (2895–4392) |
| Cloudflare Stream WHIP/WHEP | 2354 (2249–2460) | 2549 (2456–2642) | 5263 (4827–5703) | 4638 (4594–5358) |
| LiveKit, SFU in each region | 389 (336–436) | 574 (534–614) | 621 (579–658) | 525 (494–556) |
| LiveKit, multi-region set | 366 (327–407) | 1555 (1213–1578) | 4483 (3905–4919) | 4952 (4149–5255) |
| LiveKit Cloud | 459 (398–521) | 1173 (610–1737) | 2389 (2337–2441) | 2278 (2221–2335) |

### Processor of the nodes, sweep 1 (Needletail wave 1)

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 5 | 10 | 16 | 25 | 40 | 81 | 122 | 164 | 248 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 12 | 10 | 11 | 14 | 21 | 38 | 57 | 71 | 118 |
| Needletail primary relay | 10 | 13 | 21 | 39 | 71 | 123 | 120 | 120 | 118 |
| Needletail secondary relay | 9 | 10 | 17 | 31 | 66 | 112 | 112 | 115 | 112 |
| Needletail distributor, London | 10 | 14 | 16 | 23 | 37 | 52 | 50 | 43 | 29 |
| Needletail edge 1, London | 13 | 17 | 19 | 24 | 26 | 34 | 25 | 33 | 12 |
| Needletail distributor, Sydney | 10 | 14 | 23 | 28 | 50 | 67 | 55 | 34 | 25 |

LiveKit, SFU in each region:

| Node | 5 | 10 | 16 | 25 | 40 | 81 | 122 | 164 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LiveKit node, London | 9 | 16 | 23 | 31 | 31 | 55 | 70 | 83 |
| LiveKit node, Sydney | 6 | 12 | 17 | 26 | 27 | 44 | 56 | 71 |

LiveKit, multi-region set:

| Node | 5 | 10 | 16 | 25 | 40 | 81 | 122 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LiveKit node, London (serves every region) | 29 | 45 | 61 | 90 | 77 | 122 | 122 |

### Processor of the nodes, sweep 2 (Needletail wave 2)

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 5 | 10 | 16 | 25 | 40 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 84 | 89 | 90 | 94 | 93 |
| Needletail primary relay | 62 | 68 | 83 | 107 | 114 |
| Needletail secondary relay | 120 | 119 | 120 | 121 | 118 |
| Needletail distributor, London | 23 | 26 | 30 | 34 | 41 |
| Needletail edge 1, London | 6 | 9 | 13 | 18 | 24 |
| Needletail distributor, Sydney | 22 | 26 | 30 | 33 | 40 |

LiveKit, SFU in each region:

| Node | 5 | 10 | 16 | 25 | 40 | 81 | 122 | 164 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LiveKit node, London | 9 | 16 | 22 | 32 | 34 | 55 | 73 | 82 |
| LiveKit node, Sydney | 6 | 12 | 17 | 25 | 25 | 46 | 66 | 79 |

LiveKit, multi-region set:

| Node | 5 | 10 | 16 | 25 | 40 | 81 | 122 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LiveKit node, London (serves every region) | 30 | 44 | 62 | 94 | 78 | 120 | 120 |

### Sweep 3: Needletail wave 3, alone

Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest with every rung below it carried too. Each session is one sweep.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 81 | 81 | 81 | 40 |
| Needletail, LL-HLS | 81 | 81 | 81 | 81 |

#### Every rung

Frames on time (%) at the better of the two listeners of each region, and in brackets the bit rate that listener received (Mbit/s).

Needletail, RaptorQ FEC over UDP from the edge:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) | 100.0 (6) | 100.0 (6) | 100.0 (6) |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | 100.0 (10) |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | 100.0 (16) |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | 100.0 (25) |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | 100.0 (40) |
| 81 | 100.0 (81) | 100.0 (81) | 100.0 (80) | 99.2 (80) |
| 122 | 87.2 (118) | 56.4 (113) | 83.3 (115) | 79.7 (119) |
| 164 | 1.3 (105) | 1.4 (97) | 3.1 (102) | 4.4 (113) |

Needletail, LL-HLS:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) | 100.0 (6) | 100.0 (6) | 100.0 (6) |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | 100.0 (10) |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | 100.0 (16) |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | 100.0 (25) |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | 100.0 (40) |
| 81 | 100.0 (81) | 100.0 (81) | 100.0 (80) | 100.0 (80) |
| 122 | 73.6 (115) | 74.4 (110) | 66.5 (112) | 67.7 (112) |
| 164 | 0 (0) | 0 (0) | 6.8 (0) | 0 (0) |

#### Latency of what was carried

Delivery latency at the listener's socket, median (ms): from the moment a frame is due at the source to its last byte at the listener:

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 6 | 37 | 116 | 132 |
| Needletail, LL-HLS | 6 | 37 | 117 | 133 |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 8 | 40 | 119 | 135 |
| Needletail, LL-HLS | 8 | 40 | 120 | 135 |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 11 | 45 | 124 | 139 |
| Needletail, LL-HLS | 12 | 45 | 126 | 139 |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 19 | 52 | 131 | 145 |
| Needletail, LL-HLS | 20 | 52 | 133 | 145 |

Glass to glass in headless Chrome, median (ms):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 6156 | 2270 | 2760 | 2757 |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1162 | 1000 | 1275 | 563 |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1177 | 1198 | 1281 | 1294 |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1991 | 2204 | 2287 | 2301 |

Chrome: time frozen (ms in each minute):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 51610 | 5040 | 5022 |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 0 |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 0 |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 0 |

#### Time to the first frame

At 5 Mbit/s, from the listener's start to its first complete key frame (ms). The clip has a key frame every two seconds; a WebRTC sender answers a request for one within a third of a second.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 1137 (1132–1141) | 1333 (1332–1335) | 1046 (1045–1047) | 946 |
| Needletail, LL-HLS | 745 (740–749) | 675 (674–677) | 771 | 807 (803–811) |
| Cloudflare Realtime | – | – | – | – |
| Cloudflare Stream WHIP/WHEP | – | – | – | – |
| LiveKit, SFU in each region | – | – | – | – |
| LiveKit, multi-region set | – | – | – | – |
| LiveKit Cloud | – | – | – | – |

#### Processor of the nodes

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 5 | 10 | 16 | 25 | 40 | 81 | 122 | 164 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 14 | 7 | 9 | 13 | 19 | 33 | 50 | 62 |
| Needletail primary relay | 12 | 12 | 18 | 28 | 43 | 86 | 132 | 140 |
| Needletail secondary relay | 15 | 6 | 8 | 10 | 15 | 24 | 36 | 62 |
| Needletail distributor, London | 10 | 9 | 12 | 16 | 25 | 45 | 66 | 65 |
| Needletail edge 1, London | 8 | 12 | 17 | 26 | 40 | 77 | 112 | 93 |
| Needletail distributor, Sydney | 10 | 8 | 12 | 17 | 24 | 42 | 65 | 82 |

### Sweep 4: Needletail wave 4, alone

Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest with every rung below it carried too. Each session is one sweep.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 81 | 122 | 122 | 40 |
| Needletail, LL-HLS | 122 | 122 | 122 | 122 |

#### Every rung

Frames on time (%) at the better of the two listeners of each region, and in brackets the bit rate that listener received (Mbit/s).

Needletail, RaptorQ FEC over UDP from the edge:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) | 100.0 (6) | 100.0 (6) | 100.0 (6) |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | 100.0 (10) |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | 100.0 (16) |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | 100.0 (25) |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | 100.0 (40) |
| 81 | 100.0 (81) | 100.0 (81) | 100.0 (82) | 99.2 (81) |
| 122 | 97.6 (123) | 100.0 (124) | 100.0 (122) | 22.2 (105) |
| 164 | 3.8 (112) | 20.6 (130) | 14.7 (142) | 1.6 (74) |
| 248 | 2.5 (94) | 0.9 (37) | 1.0 (62) | 0.3 (32) |
| 333 | 0 (0) | 2.5 (0) | 1.3 (0) | 2.7 (0) |

Needletail, LL-HLS:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 5 | 100.0 (6) | 100.0 (6) | 100.0 (6) | 100.0 (6) |
| 10 | 100.0 (10) | 100.0 (10) | 100.0 (10) | 100.0 (10) |
| 16 | 100.0 (16) | 100.0 (16) | 100.0 (16) | 100.0 (16) |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | 100.0 (25) |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | 100.0 (40) |
| 81 | 100.0 (81) | 100.0 (81) | 100.0 (82) | 100.0 (82) |
| 122 | 100.0 (124) | 100.0 (124) | 100.0 (124) | 100.0 (124) |
| 164 | 97.8 (167) | 46.1 (0) | 34.8 (5) | 69.9 (161) |
| 248 | 41.2 (0) | 0 (0) | 0 (0) | 0 (0) |
| 333 | 0 (0) | 0 (0) | 0 (0) | 0 (0) |

#### Latency of what was carried

Delivery latency at the listener's socket, median (ms): from the moment a frame is due at the source to its last byte at the listener:

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 6 | 37 | 116 | 133 |
| Needletail, LL-HLS | 6 | 38 | 117 | 133 |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 9 | 39 | 119 | 136 |
| Needletail, LL-HLS | 8 | 40 | 120 | 136 |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 12 | 42 | 123 | 141 |
| Needletail, LL-HLS | 10 | 44 | 125 | 140 |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 18 | 49 | 129 | 148 |
| Needletail, LL-HLS | 14 | 52 | 132 | 147 |

Glass to glass in headless Chrome, median (ms):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 326 | 990 | 580 | 1169 |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 574 | 478 | 786 | 741 |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 963 | 834 | 1100 | 1290 |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 1494 | 1199 | 2148 | 2103 |

Chrome: time frozen (ms in each minute):

5 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 1290 |

10 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 0 |

16 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 0 |

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 0 |

#### Time to the first frame

At 5 Mbit/s, from the listener's start to its first complete key frame (ms). The clip has a key frame every two seconds; a WebRTC sender answers a request for one within a third of a second.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 696 (690–701) | 877 (875–878) | 602 (598–605) | 515 (500–530) |
| Needletail, LL-HLS | 573 (570–575) | 491 (489–492) | 423 (417–428) | 487 (485–489) |
| Cloudflare Realtime | – | – | – | – |
| Cloudflare Stream WHIP/WHEP | – | – | – | – |
| LiveKit, SFU in each region | – | – | – | – |
| LiveKit, multi-region set | – | – | – | – |
| LiveKit Cloud | – | – | – | – |

#### Processor of the nodes

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 5 | 10 | 16 | 25 | 40 | 81 | 122 | 164 | 248 | 333 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 5 | 6 | 8 | 12 | 18 | 125 | 139 | 151 | 193 | 252 |
| Needletail primary relay | – | – | – | – | – | – | – | – | – | – |
| Needletail secondary relay | 4 | 4 | 6 | 8 | 12 | 150 | 153 | 166 | -1764 | 163 |
| Needletail distributor, London | – | – | – | – | – | – | – | – | – | – |
| Needletail edge 1, London | 10 | 14 | 22 | 32 | 51 | 96 | 128 | 158 | 54 | 1 |
| Needletail distributor, Sydney | 6 | 8 | 11 | 16 | 26 | 86 | 159 | 187 | 109 | 62 |

### Sweep 5: Needletail wave 4 with its 4K UDP GSO off, alone

Mbit/s of 4K video: the highest rung that was carried; in brackets, when lower, the highest with every rung below it carried too. Each session is one sweep.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 122 | 122 | 122 | 122 |
| Needletail, LL-HLS | 122 | 122 | 122 | 122 |

#### Every rung

Frames on time (%) at the better of the two listeners of each region, and in brackets the bit rate that listener received (Mbit/s).

Needletail, RaptorQ FEC over UDP from the edge:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | 100.0 (25) |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | 100.0 (40) |
| 81 | 100.0 (81) | 100.0 (81) | 100.0 (80) | 100.0 (80) |
| 122 | 100.0 (120) | 100.0 (120) | 100.0 (121) | 100.0 (120) |
| 164 | 4.4 (120) | 4.6 (119) | 5.2 (115) | 7.1 (120) |

Needletail, LL-HLS:

| Mbit/s | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| 25 | 100.0 (25) | 100.0 (25) | 100.0 (25) | 100.0 (25) |
| 40 | 100.0 (40) | 100.0 (40) | 100.0 (40) | 100.0 (40) |
| 81 | 100.0 (81) | 100.0 (81) | 100.0 (80) | 100.0 (80) |
| 122 | 100.0 (121) | 100.0 (121) | 100.0 (121) | 100.0 (122) |
| 164 | 4.0 (0) | 4.4 (0) | 3.6 (0) | 3.6 (0) |

#### Latency of what was carried

Delivery latency at the listener's socket, median (ms): from the moment a frame is due at the source to its last byte at the listener:

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 19 | 53 | 132 | 146 |
| Needletail, LL-HLS | 22 | 53 | 134 | 148 |

Glass to glass in headless Chrome, median (ms):

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 2156 | 1543 | 2427 | 2174 |

Chrome: time frozen (ms in each minute):

25 Mbit/s:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 0 | 0 | 0 | 0 |

#### Time to the first frame

At 5 Mbit/s, from the listener's start to its first complete key frame (ms). The clip has a key frame every two seconds; a WebRTC sender answers a request for one within a third of a second.

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | – | – | – | – |
| Needletail, LL-HLS | – | – | – | – |
| Cloudflare Realtime | – | – | – | – |
| Cloudflare Stream WHIP/WHEP | – | – | – | – |
| LiveKit, SFU in each region | – | – | – | – |
| LiveKit, multi-region set | – | – | – | – |
| LiveKit Cloud | – | – | – | – |

#### Processor of the nodes

Percent of one vCPU used by the service at each bit rate (the highest session).

Needletail, LL-HLS:

| Node | 25 | 40 | 81 | 122 | 164 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail contributor | 13 | 18 | 34 | 57 | 62 |
| Needletail primary relay | 28 | 42 | 76 | 127 | 142 |
| Needletail secondary relay | 11 | 17 | 26 | 36 | 44 |
| Needletail distributor, London | 16 | 25 | 42 | 66 | 88 |
| Needletail edge 1, London | 27 | 42 | 72 | 109 | 103 |
| Needletail distributor, Sydney | 19 | 27 | 50 | 67 | 79 |

### Baseline at 5 Mbit/s

Delivery latency, median / 99th percentile (ms), and frames on time (%), the better viewer of each region; then glass to glass in headless Chrome, median (ms):

| System | London | New York | Tokyo | Sydney | Chrome, glass to glass |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 7 / 19, 100.0% | 38 / 48, 100.0% | 113 / 124, 100.0% | 133 / 145, 100.0% | 248 / 2390 / 278 / 2452 |
| Cloudflare Realtime | 23 / 68, 97.9% | 57 / 101, 100.0% | 135 / 179, 100.0% | 161 / 205, 100.0% | 92 / 121 / 204 / 234 |
| Cloudflare Stream WHIP/WHEP | 21 / 65, 100.0% | 56 / 99, 100.0% | 130 / 174, 100.0% | 162 / 207, 100.0% | 90 / 120 / 202 / 409 |
| LiveKit, SFU in each region | 18 / 63, 100.0% | 48 / 93, 100.0% | 124 / 170, 100.0% | 144 / 190, 100.0% | 81 / 111 / 186 / 215 |
| LiveKit, multi-region set | 18 / 64, 100.0% | 54 / 100, 100.0% | 127 / 173, 100.0% | 142 / 189, 100.0% | 88 / 118 / 200 / 214 |
| LiveKit Cloud | 32 / 78, 100.0% | 74 / 120, 100.0% | 149 / 197, 100.0% | 157 / 202, 100.0% | 103 / 141 / 223 / 229 |

### At 5 Mbit/s: time to the first frame

A viewer joins the running stream at 5 Mbit/s, eight times in a row in each region: time from its start to its first complete key frame (ms), lowest and highest:

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 13–18 | 15–17 | 13–55 | 15–17 |
| Cloudflare Realtime | 727–1446 | 1490–3578 | 1209–3329 | 1136–5221 |
| Cloudflare Stream WHIP/WHEP | 1643–1791 | 1818–2971 | 1532–3650 | 1487–3920 |
| LiveKit, SFU in each region | 256–656 | 288–596 | 481–667 | 483–791 |
| LiveKit, multi-region set | 290–444 | 1115–1294 | 3248–4003 | 3786–4879 |
| LiveKit Cloud | 288–617 | 431–619 | 167–463 | 151–552 |

### At 5 Mbit/s: loss and delay at the viewer

The same `tc netem` on every viewer host. Frames on time (%) and the longest stall (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 100%, 0 / 100%, 0 | 100%, 0 / 100%, 0 | 92%, 801 / 94%, 546 | 69%, 1389 / 72%, 3250 | 8%, 23044 / 8%, 23189 |
| Cloudflare Realtime | 98%, 1083 / 100%, 0 | 12%, 32756 / 11%, 32627 | 96%, 395 / 0%, 0 | 10%, 35546 / 10%, 36720 | 100%, 0 / 100%, 0 |
| Cloudflare Stream WHIP/WHEP | 100%, 0 / 100%, 0 | 99%, 652 / 98%, 442 | 0%, 35518 / 4%, 33850 | 20%, 15934 / 9%, 16958 | 12%, 35583 / 30%, 5292 |
| LiveKit, SFU in each region | 100%, 0 / 100%, 0 | 100%, 0 / 100%, 0 | 100%, 0 / 100%, 0 | 95%, 2000 / 99%, 250 | 100%, 0 / 100%, 0 |
| LiveKit, multi-region set | 100%, 0 / 100%, 0 | 100%, 0 / 82%, 281 | 100%, 0 / 85%, 1333 | 100%, 0 / 69%, 2000 | 100%, 0 / 100%, 0 |
| LiveKit Cloud | 100%, 0 / 100%, 0 | 100%, 0 / 100%, 0 | 100%, 0 / 100%, 0 | 100%, 83 / 100%, 125 | 100%, 0 / 100%, 0 |

Delivery latency, 99th percentile (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 19 / 145 | 23 / 147 | 880 / 741 | 1559 / 3348 | 16129 / 16271 |
| Cloudflare Realtime | 68 / 205 | 5926 / 5585 | 547 / – | 5717 / 6561 | 163 / 313 |
| Cloudflare Stream WHIP/WHEP | 65 / 207 | 172 / 479 | 10556 / 4391 | 17337 / 17434 | 14685 / 2601 |
| LiveKit, SFU in each region | 63 / 190 | 87 / 213 | 89 / 214 | 93 / 255 | 161 / 288 |
| LiveKit, multi-region set | 64 / 189 | 86 / 462 | 127 / 676 | 88 / 672 | 162 / 290 |
| LiveKit Cloud | 78 / 202 | 115 / 224 | 197 / 265 | 180 / 268 | 179 / 303 |

### At 5 Mbit/s: crowds

10 viewers (3, 3, 2 and 2) on the larger host of each region: viewers with video, the median viewer's median delivery latency and the worst viewer's 99th percentile (ms), the median first key frame (ms):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 3/3, 7.8 / 1395.56, 15 | 3/3, 38.2 / 1203.22, 2425 | 2/2, 113.2 / 123.89, 2450 | 2/2, 133.0 / 1718.52, 333 |
| Cloudflare Realtime | 3/3, 23.4 / 70.45, 1697 | 3/3, 58.3 / 105.77, 1904 | 2/2, 132.6 / 179.88, 3736 | 2/2, 319.6 / 1352.97, 3639 |
| Cloudflare Stream WHIP/WHEP | 3/3, 23.7 / 70.78, 2006 | 3/3, 58.0 / 104.69, 2215 | 2/2, 131.7 / 178.29, 4051 | 2/2, 164.6 / 1555.56, 4952 |
| LiveKit, SFU in each region | 3/3, 19.6 / 65.8, 370 | 3/3, 50.2 / 95.77, 374 | 2/2, 125.4 / 171, 393 | 2/2, 146.4 / 192.45, 626 |
| LiveKit, multi-region set | 3/3, 19.5 / 66.16, 436 | 3/3, 55.3 / 101.13, 1298 | 2/2, 126.1 / 173.44, 3909 | 2/2, 145.4 / 192.2, 4619 |
| LiveKit Cloud | 3/3, 31.1 / 77.87, 368 | 3/3, 73.1 / 119.69, 455 | 2/2, 148.3 / 195.44, 1618 | 2/2, 151.8 / 199.17, 1161 |

100 viewers (25 in each region) on the larger host of each region: viewers with video, the median viewer's median delivery latency and the worst viewer's 99th percentile (ms), the median first key frame (ms):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, LL-HLS | 25/25, 9.3 / 1570.07, 17 | 25/25, 39.7 / 1597.81, 14 | 25/25, 115.3 / 1552.58, 17 | 25/25, 134.7 / 1666.23, 15 |
| Cloudflare Realtime | 25/25, 563.9 / 11520.37, 1729 | 25/25, 625.5 / 10853.68, 2353 | 25/25, 673.4 / 26036.97, 2127 | 20/25, 338.1 / 11283.56, 2173 |
| Cloudflare Stream WHIP/WHEP | 25/25, 121.8 / 8562.42, 1469 | 25/25, 209.3 / 7992.18, 2048 | 25/25, 327.7 / 8361.11, 2168 | 23/25, 481.6 / 12038.71, 2872 |
| LiveKit, SFU in each region | 25/25, 18.9 / 63.18, 389 | 25/25, 49.3 / 93.5, 397 | 21/21, 124.7 / 168.6, 445 | 25/25, 145.7 / 189.95, 359 |
| LiveKit, multi-region set | 25/25, 33 / 991.09, 391 | 25/25, 64.9 / 1000, 1387 | 25/25, 141.8 / 1220.67, 4117 | 25/25, 159.2 / 1244.15, 4768 |
| LiveKit Cloud | 25/25, 32.3 / 79.83, 382 | 25/25, 74.0 / 121.79, 342 | 25/25, 149.6 / 196.69, 273 | 25/25, 157.2 / 204.38, 349 |

Processor of the nodes (% of one vCPU) with 10 and with 100 viewers:

| Node | 10 viewers | 100 viewers |
| --- | ---: | ---: |
| Needletail contributor | 87 | 85 |
| Needletail primary relay | 69 | 66 |
| Needletail distributor, London | 23 | 22 |
| Needletail edge 1, London | 5 | 14 |
| Needletail edge 2, London | 5 | 16 |
| Needletail edge 1, Sydney | 5 | 13 |
| LiveKit SFU, London | 10 | 41 |
| LiveKit SFU, Sydney | 6 | 37 |
| LiveKit multi-region, London node | 22 | 151 |
