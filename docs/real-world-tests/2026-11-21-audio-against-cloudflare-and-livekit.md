# 21–22 November 2026: audio against Cloudflare and LiveKit

The owner put audio first: Opus for the lowest latency, PCM for lossless
latency. This record compares Needletail with Cloudflare Realtime, Cloudflare
Stream WHIP/WHEP, self-hosted LiveKit and LiveKit Cloud. Every system carried
the same audio from the same host in London to the same listeners in London,
New York, Tokyo and Sydney, at the same time, on a lab of dedicated-CPU
instances only.

The headline is Needletail's `low-latency` build of 21 November, evening
("wave 2"): the smallest frames by default (Opus in 2.5 ms parts, lossless
PCM in 1 ms parts) and both of its client modes, RaptorQ FEC over UDP from
the edge and LL-HLS over HTTP. The same lab measured an earlier build with
5 ms parts ("wave 1"), summarised under [The earlier build](#the-earlier-build-wave-1).
A first pass on shared Nanodes is superseded; it is a side note at the end.

The 4K video comparison is a separate record.

## Answer

**Self-hosted LiveKit with an SFU in each region is still ahead, by half a
millisecond or so, in London, New York and Sydney, for Opus and for lossless
audio alike; Needletail's UDP client mode is ahead in Tokyo. With the SFUs'
PCM data channels in their default 2.5 ms blocks Needletail's 1 ms PCM had
the lowest median in every region; given the same 1 ms blocks, LiveKit's SFU
was 0.5 to 0.8 ms ahead in London, New York and Sydney and 2.2 ms behind in
Tokyo (one session). LiveKit cannot carry lossless audio as audio at all. Against LiveKit Cloud, the
like-for-like mesh, Needletail is 12 to 25 ms ahead everywhere. Under 1% and
5% random loss Needletail's UDP mode lost nothing and its 99th percentile
stayed within 2 ms of the clean run; every WebRTC system lost what the
network lost. Needletail's largest loss is crowds of lossless listeners at
1 ms parts: with 25 UDP and 25 HTTP/3 listeners sharing two 2-vCPU edges in a
region, the UDP listeners ran seconds late and lost parts in London (and in
some sessions in Tokyo and Sydney).**

**Wave 3**, pushed just before midnight and checked in one session at 02:48
(with self-hosted LiveKit and Cloudflare Realtime beside it), took another
1 to 1.5 ms off Needletail in London: 2.5 ms Opus over UDP at 4.2, 35.9,
111.3 and 131.2 ms against LiveKit's SFU at 3.8, 40.5, 110.5 and 130.6; 1 ms
PCM at 1.7, 32.9, 108.1 and 128.0 against LiveKit's 2.5 ms blocks at 2.2,
33.3, 108.4 and 128.5. See [Wave 3](#wave-3-one-session). Wave 4, checked at 03:29, was level with
wave 3 against LiveKit: Opus over UDP 5.6, 36.2, 112.0 and 131.8 ms against
LiveKit's best 5.0, 35.6, 114.1 and 131.2.

Mouth to ear is the age of a sample when a listener that plays audio on
arrival would play it. Median of three sessions (wave 2), in milliseconds:

| Lane | System | Frame | London | New York | Tokyo | Sydney |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| PCM | **Needletail mesh, RaptorQ FEC over UDP** | 1 ms | **2.7** | **33.3** | **108.5** | **128.5** |
| PCM | Needletail mesh, LL-HLS, pipelined HTTP/3 reader (harness) | 1 ms | 6.5 | 36.5 | 110.4 | 130.4 |
| PCM | LiveKit, self-hosted, SFU in each region, data channel | 2.5 ms | 3.1 | 38.7 | 111.7 | 128.9 |
| PCM | LiveKit, self-hosted, multi-region set, data channel | 2.5 ms | 3.2 | 33.8 | 112.0 | 129.9 |
| PCM | Cloudflare Realtime, data channel | 2.5 ms | 5.6 | 40.2 | 115.8 | 3,239 |
| PCM, one session | LiveKit, SFU in each region, data channel, 1 ms blocks | 1 ms | 2.1 | 32.7 | 110.9 | 127.5 |
| PCM, the same session | Needletail mesh, RaptorQ FEC over UDP | 1 ms | 2.6 | 33.4 | 108.7 | 128.3 |
| PCM | **LiveKit Cloud mesh**, data channel | 2.5 ms | 17.0 | 58.6 | 133.8 | 142.1 |
| Opus | **Needletail mesh, RaptorQ FEC over UDP** | 2.5 ms | 5.7 | 36.3 | **111.5** | 131.6 |
| Opus | Needletail mesh, LL-HLS, pipelined HTTP/3 reader (harness) | 2.5 ms | 6.3 | 36.8 | 112.0 | 132.1 |
| Opus | LiveKit, self-hosted, SFU in each region (4 uploads, 1 hop) | 2.5 ms | **5.2** | 40.9 | 113.8 | **130.8** |
| Opus | LiveKit, self-hosted, multi-region set (1 upload, media from London) | 2.5 ms | 5.3 | **35.6** | 113.9 | 131.0 |
| Opus | Cloudflare Stream WHIP/WHEP | 2.5 ms | 9.6 | 43.1 | 119.3 | 133.5 |
| Opus | Cloudflare Realtime | 2.5 ms | 9.8 | 42.4 | 117.3 | 148.8 |
| Opus | **LiveKit Cloud mesh** | 2.5 ms | 18.3 | 60.5 | 133.4 | 143.8 |

Cloudflare Realtime's PCM to Sydney was seconds late in two sessions of
three (146 ms in the third). Needletail's PCM parts are 1 ms because the PCM
curve found nothing smaller that works (below); LiveKit and Cloudflare carried
PCM in 2.5 ms blocks on a data channel. The HTTP rows are the harness's
pipelined reader; the one-request-per-part reader is in the tables.

At the 99th percentile: PCM, Needletail UDP 7.8, 38.7, 114.2 and 137.2 ms
against LiveKit's best 7.3, 38.3, 116.1 and 134.1; Opus, 8.0, 38.7, 113.9
and 134.5 against 7.1, 37.6, 116.0 and 132.3.

**Mesh against mesh.** LiveKit Cloud is LiveKit's own SFU mesh: the source
uploads once, to its nearest edge, and LiveKit relays the media to the edge
of each listener, as Needletail's mesh does. Against it Needletail's UDP mode
is ahead in every region: 12 to 24 ms for Opus, 14 to 25 ms for PCM.
Self-hosted LiveKit with an SFU in each region is not a mesh: the source
uploads to all four SFUs, and every listener is one SFU hop from the source.

| Setup | Uploads by the source | Hops from source to listener | Where the media of the room lives |
| --- | ---: | --- | --- |
| Needletail | 1 (to the contributor, London) | contributor, backbone relay, distributor, edge: 4 | cached at the edges of each region |
| LiveKit, SFU in each region | 4 (one to each region) | 1 SFU | the SFU of each region |
| LiveKit, multi-region set | 1 (London node) | 1 SFU, in London | the London node; listeners elsewhere get it from London |
| LiveKit Cloud | 1 (its edge in Germany) | 1 SFU for London (Germany); 2 elsewhere: the source's edge and the listener's (US East, Japan, Australia) | LiveKit's mesh |
| Cloudflare Realtime and Stream WHIP/WHEP | 1 (the nearest Cloudflare location) | Cloudflare's network; not visible | Cloudflare's network |

Where Needletail loses:

- **Lossless with the same frame.** Given 1 ms data-channel blocks (one
  session), self-hosted LiveKit's SFU was 0.5 to 0.8 ms ahead of Needletail's
  1 ms PCM in London, New York and Sydney (2.1, 32.7 and 127.5 ms against
  2.6, 33.4 and 128.3); Needletail was 2.2 ms ahead in Tokyo.
- **Opus, against self-hosted LiveKit.** 0.5 ms behind in London, 0.7 ms in
  New York (against the multi-region set; 4.6 ms ahead of the SFU in New
  York), 0.8 ms in Sydney. Its jitter (99th percentile less median of the
  unit latency) is 1.9 to 3.0 ms against LiveKit's 1.1 to 2.0.
- **Lossless jitter.** Needletail's 1 ms PCM has a jitter of 5.2 to 8.8 ms
  against 3.9 to 4.4 ms for LiveKit's SFU in each region (4.2 to 5.6 for its
  multi-region set), with 2.5 ms blocks, and its 99th percentile
  is 0.4 to 3.1 ms behind LiveKit's except in Tokyo.
- **Crowds at the smallest parts.** With 25 UDP and 25 HTTP/3 listeners of
  1 ms PCM sharing the two edges of each region, the UDP listeners ran
  3.5 seconds late in London and lost 71% of the parts (38% to 76% across
  sessions), and in some sessions up to 56% in Tokyo and 17% in Sydney; New
  York was served. The busier London edge was at 148% to 167% of one vCPU,
  of two. The product reader
  `needletail-reader` kept up with only 16 to 22 of 25 PCM listeners, up to
  23 seconds behind. Opus crowds were served, over UDP and by
  `needletail-reader` (below); the harness's one-request-per-part HTTP
  readers fell behind even with Opus. Self-hosted LiveKit with an SFU in each region
  served 100 listeners of both, in a pass of its own: Opus at about the
  latency of a few, PCM about 2 ms later in London (worst listener's 99th
  percentile 32 ms).
- **Burst loss on the UDP mode.** Gilbert-Elliott bursts (4.8% lost, five
  packets at a time on average) lost 0.5% of the Opus parts and 0.27% to
  0.37% of the PCM parts, the longest gap 1 to 5 ms. The SFUs lost about 5%.
- **Time to first audio, at a cold edge.** A Needletail edge holds a stream
  only while someone reads it. The first listener at an edge that did not
  hold the stream waited 0.3 to 2.0 seconds; LiveKit's SFU in the region
  answered in 48 to 161 ms. (Cloudflare took 0.7 to 4.8 s, LiveKit Cloud
  0.27 to 1.7 s, LiveKit's multi-region set up to 4.9 s far from London.)
- **Smaller than 1 ms.** 0.5 ms PCM parts ran about 2 seconds late and lost
  38% to 90% of the parts; 0.25 ms delivered nothing usable. The contributor
  (139% and 264% of one vCPU) and the primary relay (114%) were the limit.
- **A browser.** Chrome plays every WebRTC Opus lane (72 to 86 ms mouth to
  ear in London, 195 to 227 ms in Sydney). No browser plays Needletail's
  parts without a player written for them; nor the SFUs' PCM data channels.

Where Needletail wins:

- **Lossless audio as audio.** 2.7, 33.3, 108.5 and 128.5 ms, bit for bit,
  ahead of every SFU with its default 2.5 ms data-channel blocks (and of
  LiveKit's 1 ms blocks in Tokyo). Cloudflare Realtime, Cloudflare Stream and LiveKit (self-hosted
  and Cloud) refused an uncompressed audio track (`L16`, `L24`); Stream has
  no data channel, and the others' data channels carry PCM only to a client
  written for it and lose what the network loses.
- **Random loss, at no cost in latency.** At 1% and 5% loss the UDP mode lost
  nothing and its 99th percentile stayed at 8 to 10 ms in London and 133 to
  134 ms in Sydney. Every SFU lost about 1% and 5%; Chrome concealed 1% to
  21% of the samples (medians; up to 38% in single sessions).
- **Opus in Tokyo**, 111.5 ms against LiveKit's 113.8 (2.3 ms ahead).
- **Mesh against mesh**: 12 to 25 ms ahead of LiveKit Cloud everywhere, and
  ahead of Cloudflare in every region, for Opus (by 1.9 to 6.1 ms against
  the better of Cloudflare's two lanes) and for PCM.
- **An Opus crowd over UDP.** 100 listeners, 25 in each region, sharing the
  edges with 25 HTTP/3 listeners, at 4.7, 35.1, 110.5 and 130.0 ms (unit
  latency of the median listener), worst listener's 99th percentile 11, 41,
  117 and 136 ms, nothing lost: ahead of self-hosted LiveKit's SFUs (4.4,
  40.0, 113.0 and 129.5; worst 9, 44, 118 and 134) in New York and Tokyo,
  0.3 to 0.5 ms behind in London and Sydney. `needletail-reader` itself kept
  a crowd of 100 within 24 ms of live with no underrun.
- **The contributor.** Alone on a g6-dedicated-4 it published Opus, FLAC and
  PCM renditions and logged no `publication queue is full` in these
  sessions: 31% of one vCPU with 2.5 ms Opus, 81% with 1 ms PCM.

What Needletail should fix to win everywhere, in order of what it buys:

1. **The edge's cost per part.** At 1 ms parts two g6-dedicated-2 edges
   could not serve 25 UDP listeners of PCM in a region. Send each part to
   all subscribers in one batch (`sendmmsg`, GSO), encode its FEC once.
2. **HTTP readers at 1 ms parts.** One request per 1 ms part does not scale
   to a crowd or to a long round trip. Give HTTP readers aggregated parts
   while UDP keeps the smallest frames, and keep a window longer than the
   round trip.
3. **The last half millisecond of Opus and the jitter.** 0.5 to 0.8 ms and
   about 1 ms of jitter remain against one SFU hop. The per-hop measurement
   in the 4K record places them.
4. **Bursts.** Spread repair across parts, or let a client ask again, so a
   burst of five packets does not lose a part.
5. **A cold edge.** Subscribe an edge to a stream before its first listener,
   or answer the first listener from the distributor while the edge fills.
6. **Below 1 ms.** The contributor and the relay, not the network, stopped
   0.5 ms parts.
7. A browser player (WebTransport carrying the UDP mode's datagrams).

## What was compared

| System | How the audio went in | How a listener got it |
| --- | --- | --- |
| Needletail | Its AEP1 sender on `lon-src`, to the contributor in London: Opus coded live in 2.5 ms CELT frames (stream 3001), and 24-bit PCM in 1 ms epochs (stream 4001), one frame or epoch per part. The contributor publishes Opus, FLAC and PCM renditions. | Two client modes, from the edge of the listener's region: **RaptorQ FEC over UDP** (`needletail-udp-client`: each part one RaptorQ object), and **LL-HLS over HTTP** (three readers, below, and `needletail-reader` itself in a crowd). |
| Cloudflare Realtime | One Opus track to the SFU through its HTTPS API. For PCM, one data channel. | Pull of the track, or of the data channel, through the same API. |
| Cloudflare Stream WHIP/WHEP | One Opus track by WHIP. | WHEP. It has no data channel. |
| LiveKit, SFU in each region | `livekit-server` 1.13.11 on one g6-dedicated-2 in each region, no Redis. The source publishes to all four. | The SFU of the listener's region. |
| LiveKit, multi-region set | The same four nodes as one set on one Redis (its own g6-dedicated-2), with the region-aware node selector. The source publishes through the London node. | Signal through the node of the listener's region. The room lives on the London node, so the media comes from London. |
| LiveKit Cloud | The owner's project. | LiveKit Cloud chose the edge: Germany for London, US East, Japan and Australia. |

Cloudflare Stream Live with RTMPS in and HLS or LL-HLS out is not here. It
carries AAC, not Opus or PCM, and the account cannot use it at all: see
[Not measured](#not-measured).

Needletail (wave 2) is `low-latency` as pushed at 21:30 UTC: `av-mesh`
`f378b12` with one measurement option cherry-picked on `compare-low-latency-2`
(`34d260c`: the UDP client writes the arrival of each part), `playlists`
`ff09196`, `av-contrib` `f86c995`, built with cargo 1.100.0 on a Debian 13
g6-dedicated-8. The deploy tool is `low-latency`'s merged into `needletail`
`compare-cloudflare`, with its defaults: caches' part 2.5 ms
(`part_us` 2500), one AEP1 epoch per audio part, one frame per video part,
1 s segments, a 12 s window, `client_udp_port` 19446. Wave 1 was `av-mesh`
`c5d75c8`, `av-contrib` `7a41cea`, 5 ms parts. The Nanode pass ran
`regional-llhls-cache` (`av-mesh` `a9cb1dd`).

## Method

### One source, locked to the clock

The source is 64 seconds of the Lori Asha `WESTSIDE` confirmation mix, 48 kHz,
24-bit, stereo, with a marker: a burst of 3 kHz that starts on every whole
second (10 ms long, 30 ms at second 0). `scripts/compare/prepare-media.py`
makes it.

Every sender plays the 64 seconds in a loop that is locked to the wall clock:
sample `s` of the loop is spoken when the Unix time is `s / 48000` seconds past
a multiple of 64. A listener that recognises a unit of audio therefore knows
when each of its samples was spoken, with no side channel. The hosts keep time
with chrony; the largest offset seen was 0.23 ms (from the deploy tool's
status, not kept in the evidence file).

A live sender cannot send a unit before its encoder has the last input sample
it needs. Every sender here, ntcmp's WebRTC senders and Needletail's AEP1 and
DAW senders alike, sends unit j at (j+1)·N, N its length in samples: a PCM
block when its last sample has been spoken, an Opus packet when the last
input sample of its frame has, which is the encoder's look-ahead (2.5 ms for
CELT low-delay) after the last sample the packet carries. So frame length and
look-ahead cost what they cost in a live system (see [A live sender](#a-live-sender)).

### Units and what a listener records

A unit is one Opus packet, one block of PCM, or one Needletail part. The
listener (`ntcmp sub`, `ntcmp hls`) records the arrival time of every unit and
which unit it is.

- An Opus packet or a PCM block is recognised by a hash of all its bytes. A
  recognised unit is bit for bit the unit that was sent. This is what
  "bit-exact" means in the tables.
- A Needletail Opus or FLAC part is recognised by its part number, not its
  bytes: its sender codes it live (the AEP1 sender's Opus in wave 2, the DAW
  source's Opus and FLAC in wave 1). For those lanes the "bit-exact" column
  counts parts received; it is a byte check only for the WebRTC lanes and for
  Needletail's PCM. The DAW source's mapping was checked once by decoding
  (the marker 119 samples after a whole second for Opus, the 2.5 ms
  look-ahead, and 1 sample after it for FLAC; `scripts/compare/validate/`).
  The AEP1 sender's Opus is libopus CELT low-delay coding frame k from
  samples [k·120, (k+1)·120), so it has the same 120-sample look-ahead; its
  index counts it, as every WebRTC Opus index does.

**Unit latency** is the arrival of a unit minus the moment its last sample was
spoken.

**Mouth to ear, played on arrival** is the age of a sample at a listener that
plays every unit the moment it arrives: the unit latency plus the time the
sample waited for the rest of its unit. It is taken over every sample, so a
frame of 5 ms adds 2.5 ms at the median and 5 ms at the top. It has no jitter
buffer, no decoder and no sound card in it. It is the floor that the network
and the system set, and it is the same quantity for every system.

**Chrome mouth to ear** is measured, not derived. Headless Chrome 158 plays
the track, an AudioWorklet finds the marker in the decoded audio, and the
output timestamp of the audio context gives the time. It has Chrome's jitter
buffer and decoder in it. The receiver's `jitterBufferTarget` is 0, the lowest
that Chrome accepts. The marker is 2.5 ms into its packet for every frame
length, so it waits for the rest of its frame: 2.5, 2.5, 7.5 and 17.5 ms for
frames of 2.5, 5, 10 and 20 ms.

The first five seconds of a listener and the last three of a step are left
out, twelve seconds for the listeners of a crowd.

### Needletail's DAW buffer (wave 1)

Needletail's Opus and FLAC come from its DAW Nexus test source. It hands audio
to the plug-in in buffers of 512 samples, as a DAW does, and delivers a buffer
when it is complete. A sample therefore waits up to 10.7 ms (5.3 ms at the
median) before Needletail has it. The other systems, and Needletail's PCM
sender, are fed without such a buffer.

Wave 1's figures give Needletail's DAW-source Opus and FLAC **without** that wait: the wait of
each part is known exactly from its position and is taken off. The evidence
file has the measured values as `raw`. A producer inside a DAW pays the buffer
with every system.

### A live sender

A live sender cannot send a unit before its encoder has the last input sample
it needs. Every sender here, ntcmp's WebRTC senders and Needletail's AEP1
sender alike, sends unit j at (j+1)·N, N its length in samples: a PCM block
when its last sample has been spoken, an Opus packet when the last input
sample of its frame has, which is the encoder's look-ahead (120 samples,
2.5 ms, for CELT low-delay) after the last sample the packet carries. A
listener measures every unit from the moment the last sample it carries was
spoken, so every Opus lane pays its look-ahead and every lane pays its frame.
Until 23:00 UTC on 21 November ntcmp's WebRTC Opus senders sent the
look-ahead earlier than a live encoder can; the latency session found it.
Wave 2's first session ran before the fix; its WebRTC Opus figures are moved
later by exactly the look-ahead (the evidence marks them with
`webrtc_opus_send_corrected_ms`). After the move they fall within the spread
of sessions 2 and 3, measured with the fix, for most lanes; LiveKit's SFU in
Tokyo was 3.6 ms faster in session 1 than in the other two.

### Needletail's two client modes

A Needletail client either tails LL-HLS over HTTP, or receives RaptorQ FEC
over UDP. Both are assigned an edge by the same entry point, from the same
playlist.

**RaptorQ FEC over UDP** (`needletail-udp-client`, from `client-udp`): the
client subscribes to its stream at the edge; the edge sends each part, the
same bytes as its LL-HLS part, as one RaptorQ object of source and repair
datagrams; the client measures its datagram loss and the edge sizes the
repair to it. The client writes the arrival time and the bytes of every part;
`ntcmp udp-ingest` measures them with the same index as every other lane. This
is the mode that is compared with WebRTC's RTP over UDP.

### Three LL-HLS readers of Needletail

A Needletail audio part is one frame: 2.5 ms for Opus and 1 ms for PCM in
wave 2 (5 ms in wave 1). A reader that asks for one part and waits for it
reads one part per round trip; the listeners of this lab are in the data
centre of their edge (about 0.5 ms).

A Needletail edge holds a request for a part that is not published yet and
answers the moment it is. That was checked up to 600 parts ahead (3 seconds).
A reader can therefore keep requests in flight. Three readers were measured:

| Reader | What it does |
| --- | --- |
| as shipped | 5 ms of parts in flight (2 at 2.5 ms, 5 at 1 ms), HTTP/2: what the one-request reader of 5 ms parts did. |
| pipelined, HTTP/2 | 40 requests in flight (100 ms of 2.5 ms parts, 40 ms of 1 ms parts), one TCP connection. |
| pipelined, HTTP/3 | 40 requests in flight, QUIC. |

The pipelined readers are in `tools/ntcmp` (`ntcmp hls --window 40 [--http3]`).
They are not in the product. The product's own `needletail-reader` (with a
1000 ms buffer) also ran, as a crowd of 25 in each region in a pass of its
own (lane `nt-reader`); it reports parts received and how far behind it was,
not the latency of each part.

### Lossless on a WebRTC SFU

An SFU forwards audio tracks. Each system was asked for an uncompressed track
(`audio/L16` and `audio/L24`, 48 kHz, stereo), and each refused: see
[Lossless](#lossless). The route that does carry PCM is a data channel with a
client written for it: the source sends 2.5 ms blocks of PCM (720 bytes and an
8-byte number) on an unordered channel that sends each message once, as live
audio is sent. That is the PCM lane of the SFUs in the tables. No browser
plays it without code written for it, and what the network loses is gone.

`opus-max510` is the nearest that an audio track comes: Opus at 510 kbit/s,
the top of its range. It is not lossless.

### The lab: dedicated CPU only

Every instance is a dedicated-CPU Linode; none shares its processor with
another tenant (owner, 21 November: "do not use shared instances"). The lab
tool refuses any other plan and checks the live type of every instance.

| Role | Instances | Plan |
| --- | --- | --- |
| Needletail contributor | 1, London, nothing else on it | g6-dedicated-4 |
| Needletail backbone relays | London, Amsterdam | g6-dedicated-2 |
| Needletail distributors | 2 in each region | g6-dedicated-2 |
| Needletail edges | 2 in each region | g6-dedicated-2 |
| LiveKit SFU | 1 in each region; Redis of the set in London | g6-dedicated-2 |
| Source | `lon-src`, London | g6-dedicated-4 |
| Listeners | in each region one g6-dedicated-2 and one g6-dedicated-4 (Chrome, crowds) | |

`scripts/compare/steal-check.py` ran before each session: no instance had
steal time above 0.2% (its output is not in the evidence). During the steps,
the node samplers kept steal at or below 0.2% except in 13 node-steps of
wave 2, the highest 5.4% on a Sydney distributor for one step; no edge went
above 1%.

### Listeners

In each of London, New York, Tokyo and Sydney: one g6-dedicated-2 and one
g6-dedicated-4. Both run the probes; the larger one also runs headless
Chrome and the crowds of 25. (The tables and the evidence still call the
smaller host `-nano`, the name it had in the first pass.)

Every system is heard by the same listeners at the same time, so all of them
see the same network. Systems that share a node are not loaded together in the
crowd steps, but Needletail's two client modes are: its 25 UDP and 25 HTTP/3
listeners in each region share the same edges.

### Impairment

`scripts/compare/netem.sh` puts the same `tc netem` on every listener host, in
both directions, for IPv4 and IPv6, with SSH left out:

| Name | netem | Effect |
| --- | --- | --- |
| 1% loss | `loss 1%` | Each packet in and out is dropped with probability 0.01. |
| 5% loss | `loss 5%` | The same at 0.05. |
| burst loss | `loss gemodel 1% 20%` | Gilbert-Elliott: bursts of 5 packets on average, 4.8% lost. |
| 100 ms delay | `delay 100ms` | 100 ms each way: 200 ms more on a round trip. |

### Sessions

| Session | Start (UTC) | Configurations |
| --- | --- | --- |
| Wave 2, 1 | 21 Nov 22:19 | 2.5 ms Opus, FLAC beside 20 ms Opus (no FLAC source ran: see [Not measured](#not-measured)), 1 ms PCM; impairment and crowds. WebRTC Opus sent before the live-rule fix, corrected. |
| PCM curve | 22:49 and 22:54 | 0.5 ms and 0.25 ms PCM, clean and impaired |
| Wave 2, 2 | 23:00 | the same as session 1, live rule |
| Wave 2, 3 | 23:31 | the same as session 2 |
| SFU PCM in 1 ms blocks | 22 Nov 02:08 | PCM only; the SFUs' data channels carry 1 ms blocks (`--sfu-pcm-ms 1`) |
| Wave 3 | 22 Nov 02:48 | 2.5 ms Opus and 1 ms PCM, clean and impaired, beside Cloudflare Realtime and self-hosted LiveKit (see [Wave 3](#wave-3-one-session)) |
| Wave 4 | 22 Nov 03:29 | 2.5 ms Opus and 1 ms PCM, clean and impaired, beside Cloudflare Realtime and self-hosted LiveKit |
| Wave 1, 1 to 3 | 19:44, 20:25, 20:56 | 5 ms parts (see [The earlier build](#the-earlier-build-wave-1)) |

Before each session `steal-check.py` found no instance with steal time above
0.2%. A cell in a table is the median of the three wave 2 sessions, with the
lowest and the highest session in brackets when they differ.

## Settings

| System | Setting | Value |
| --- | --- | --- |
| All WebRTC lanes | Opus | libopus through ffmpeg 7.1.5, stereo, 256 kbit/s constant, `-application lowdelay` (CELT only, 2.5 ms look-ahead), frames of 2.5 ms (and 20 ms beside Needletail's FLAC step); wave 1's first session also 5 and 10 ms. `opus-max510`: 510 kbit/s, 10 ms, `-application audio` (6.5 ms look-ahead). |
| All WebRTC lanes | Sender | Pion, pre-encoded packets, one RTP packet for each Opus packet, no DTX, no RED, no in-band FEC. |
| All WebRTC lanes | Probe | Pion with NACK, receiver reports and transport-wide congestion control feedback. No jitter buffer. |
| Cloudflare | Client | Pion `webrtc` v4.2.27. STUN `stun.cloudflare.com:3478`. |
| LiveKit | Client | LiveKit `server-sdk-go` v2.20.0 on LiveKit's fork of Pion (v4.2.25-warp.3), `livekit-client` 2.25.0 in Chrome. |
| LiveKit, self-hosted | `livekit-server` | 1.13.11. UDP 50000 to 54999 (55000 to 59999 for the multi-region set), `node_ip` the public address, TURN off, defaults otherwise. Multi-region: Redis 8.0.2 in London, `node_selector.kind: regionaware`. |
| Needletail | Mesh | The deploy tool's defaults of `low-latency`: `part_us` 2500 (one epoch per audio part), `segment_ms` 1000, `window_ms` 12000, `mesh_sync_interval_ms` 2, `mesh_symbol_size` 900 up to 16,000, `cache_mesh_replication` `demand`, `telemetry_interval_ms` 500. `playback_address` `public`: listeners reach the edges over the public address, as they reach the other systems. |
| Needletail | Contributor | `daw_hls_packaging` `opaque`, `daw_hls_formats` `flac,opus,pcm`: all three renditions; each configuration runs one producer (see [The contributor and the nodes](#the-contributor-and-the-nodes)). |
| Needletail | UDP client | `client_udp_port` 19446 on every distributor and edge; client defaults: repair adapted to the measured loss, a move after 1 s of silence. |
| Needletail | Opus | The AEP1 sender's Opus: libopus CELT low-delay, 2.5 ms frames (`--epoch-samples 120`), 26-byte packets with a 20-byte SoundKit header (147 kbit/s at the listener). |
| Needletail | PCM | The AEP1 sender's 24-bit stereo PCM, 1 ms epochs (`--epoch-samples 48`), 288 bytes a part. |
| Chrome | Listener | Headless Chrome 158.0.8127.36, `jitterBufferTarget = 0`. |

## Results

Each cell is the median of the three wave 2 sessions; the lowest and highest
session are in brackets when they differ. All tables are made from the
evidence file by `scripts/compare/compare-audio-report.py` and follow under
[Tables](#tables).

### Opus

Every WebRTC system forwarded 2.5 ms Opus unchanged: every packet that
arrived was bit for bit the packet sent. Needletail's Opus here is its AEP1
sender's, 2.5 ms CELT frames coded live from the same loop, one frame per
part (stream 3001). Every Opus lane pays its encoder's 2.5 ms look-ahead (see
[A live sender](#a-live-sender)).

LiveKit's SFU in each region is the floor in London (5.2 ms) and Sydney
(130.8 ms), its multi-region set in New York (35.6 ms), where its media came
from London over LiveKit's own route. Needletail's UDP mode is the floor in
Tokyo (111.5 ms), and within 0.5 to 0.8 ms of the floor elsewhere. Its two client modes differ by 0.4 to 0.6 ms at the median; the
HTTP readers' tail is longer.

Cloudflare Realtime and Stream WHIP/WHEP are 4 to 8 ms behind Needletail's
UDP mode in London, New York and Tokyo; in Sydney 1.9 ms (Stream) and 17 ms
(Realtime). LiveKit Cloud is the slowest in London, New York and Tokyo; its
source in London uploaded to its edge in Germany, and its listeners were
served from Germany, US East, Japan and Australia (in a few impairment steps
EU West, US Central or Japan).

In Chrome (the SFUs only; Needletail was not played in Chrome), mouth to ear
is 72 to 86 ms in London, 108 to 128 ms in New York,
177 to 204 ms in Tokyo and 195 to 227 ms in Sydney.

### Lossless

Each system was asked, in the first dedicated sessions, for an audio track
of `audio/L16` and `audio/L24`, 48 kHz, stereo:

| System | Answer |
| --- | --- |
| Cloudflare Realtime | `unable to start track, codec is not supported by remote` |
| Cloudflare Stream WHIP | `unable to start track, codec is not supported by remote` |
| LiveKit 1.13.11, self-hosted | The track is published in signalling, then no codec is negotiated |
| LiveKit Cloud | The same as self-hosted |

So the SFUs' lossless lane is a data channel: blocks of PCM, sent once, to a
client written for it. Needletail carries PCM as an audio rendition, bit for
bit, in 1 ms parts. The three sessions gave the SFUs 2.5 ms blocks; a fourth
session (`wsfu1ms`, 22 November 02:08) gave them Needletail's 1 ms blocks.
Mouth to ear, median (ms):

| System, 1 ms blocks | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP | 2.6 | 33.4 | 108.7 | 128.3 |
| LiveKit, SFU in each region | **2.1** | **32.7** | 110.9 | **127.5** |
| LiveKit, multi-region set | 2.4 | 33.1 | 111.4 | 127.9 |
| Cloudflare Realtime | 4.2 | 38.5 | 113.5 | 163.2 |
| LiveKit Cloud | 15.6 | 57.9 | 133.5 | 136.4 |

Every data channel carried 1,000 blocks a second. With the same frame,
self-hosted LiveKit's SFU is 0.5 to 0.8 ms ahead of Needletail outside Tokyo,
as for Opus. Under 1% and 5% loss the SFUs lost about 1% and 5% of the blocks
and Needletail's UDP mode none; LiveKit Cloud's data channel lost 83% with 5%
loss in Sydney and 90% with 100 ms of delay.

The PCM curve, one session each, mouth to ear median / 99th percentile (ms)
and parts lost, Needletail UDP mode:

| PCM part | London | New York | Tokyo | Sydney | Contributor / primary relay processor |
| --- | --- | --- | --- | --- | --- |
| 1 ms (48 samples), session 1 | 3.4 / 8.9, 0% | 33.3 / 39.3, 0% | 108.5 / 114.2, 0% | 128.6 / 134.0, 0% | 81% / 52% of one vCPU |
| 0.5 ms (24) | 1,920 / 2,000, 38% | 1,963 / 2,124, 53% | 2,087 / 3,012, 90% | 1,969 / 3,032, 90% | 139% / 114% |
| 0.25 ms (12) | nothing usable | | | | 264% / 113% |

So 1 ms is the smallest part that works, and it is the default.

### Impairment

With random loss the UDP mode recovers every part from the repair datagrams
that travel with it: at 1% and 5% loss its 99th percentile was 8 to 10 ms in
London and 133 to 134 ms in Sydney, for Opus and PCM (8 ms and 135 to 137 ms
with no impairment). One Sydney session lost 2.25% of the Opus parts in a
1.1 s gap with no impairment, as every SFU did in that session. With bursts it lost 0.27% to 0.54% of the parts, in gaps of at most
5 ms. With 100 ms of delay each way it was 100 ms later and nothing else
changed; so was every SFU with Opus, but not LiveKit Cloud's PCM data channel
(below).

The SFUs send each packet once: with 1% loss about 1% of the audio is gone,
with 5% about 5%, with bursts about 5%. Chrome concealed 4% to 21% of the
samples from Cloudflare (medians; up to 38% in one session) and 1% to 5% from LiveKit (which retransmits some
audio on request; not checked in the server). LiveKit Cloud's data channel
lost 88% of the PCM blocks with 100 ms of delay, in every session.

Needletail's HTTP readers lost almost nothing (at most 0.16% of the parts in
any session) and paid in time. Pipelined over HTTP/3 at 5% loss, the Opus 99th
percentile stayed at 13 and 140 ms (London and Sydney); the PCM 99th
percentile rose to 118 ms in London and was 178 ms in Sydney. With 100 ms of
delay a window of 40 parts is 40 ms of 1 ms PCM or 100 ms of 2.5 ms Opus,
shorter than the round trip: the pipelined readers fell 15 to 25 seconds
behind, the reader with 5 ms of parts in flight 28.5 to 30 seconds.

### Listeners

One and ten listeners: every system served them all at about the latency of
one, except Cloudflare in Sydney, where Opus was up to 17 ms later in some
sessions and Realtime's PCM seconds late in one.

One hundred listeners, 25 on one host in each region. The UDP listeners ran
together with 25 HTTP/3 listeners of the same stream and with the LiveKit
multi-region set's crowd; the other crowds ran in a pass of their own. Unit
latency of the median listener:

- **Opus, Needletail UDP:** 4.7, 35.1, 110.5 and 130.0 ms, nothing lost.
  The harness's HTTP readers (one request per part, and pipelined) fell 7 to
  16 seconds behind; `needletail-reader` itself, in its own pass, kept all
  100 within 24 ms of live with no underrun.
- **PCM, Needletail UDP:** 3.5 seconds late in London with 71% lost (38% to
  76%); 34 ms in New York with nothing lost; in Tokyo 0.39 s at the median
  (112 ms to 1.8 s) with 6.6% lost (2% to 56%); in Sydney 131 ms (256 ms in
  one session) with 6.9% lost (0.7% to 17%). The busier London edge was at
  148% to 167% of one vCPU. The HTTP readers fell 20 to 31 seconds behind,
  and only 11 to 14 of the 25 pipelined HTTP/3 listeners in each region got
  audio; `needletail-reader` kept 16 to 22 of 25, up to 23 s behind.
- **LiveKit, SFU in each region:** Opus 4.4, 40.0, 113.0 and 129.5 ms; PCM
  3.4, 38.5, 112.2 and 128.8 ms. Its London node was at 105% of one vCPU with
  both LiveKit servers on it.
- **LiveKit, multi-region set:** Opus served at 5.2, 35.9, 111.2 and
  130.4 ms (worst listener's 99th percentile up to 169 ms); PCM 3.9 to
  6.2 seconds late with 10.6% lost in London, the London node at 198%.
- **Cloudflare:** Opus at the latency of one; one or two of 25 Sydney
  listeners did not connect on Realtime in every session, and two on Stream
  in one. PCM to Sydney seconds late in two sessions, and to Tokyo in one.
- **LiveKit Cloud:** 96 of 96 (the free plan allows 100 connections), at the
  latency of one.

### Wave 3 (one session)

Needletail `av-mesh` `8c4196d` (+ the arrivals option, `89ecc5f`), `av-contrib`
`bbb69dc`, `playlists` `89964dc`, the deploy tool of `ae17535` (class-isolated
lanes). One session (`waudio-w3`, 22 November 02:48), 2.5 ms Opus and 1 ms PCM,
clean and impaired, beside Cloudflare Realtime and both self-hosted LiveKit
setups. Mouth to ear, median (ms):

| System | Lane | London | New York | Tokyo | Sydney |
| --- | --- | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP | Opus 2.5 ms | 4.2 | 35.9 | 111.3 | 131.2 |
| LiveKit, SFU in each region | Opus 2.5 ms | **3.8** | 40.5 | **110.5** | **130.6** |
| LiveKit, multi-region set | Opus 2.5 ms | 3.8 | 40.6 | 114.1 | 130.7 |
| Cloudflare Realtime | Opus 2.5 ms | 8.0 | 43.8 | 117.5 | 152.8 |
| Needletail, RaptorQ FEC over UDP | PCM 1 ms | **1.7** | **32.9** | 108.1 | **128.0** |
| LiveKit, SFU in each region | PCM 2.5 ms, data channel | 2.2 | 33.3 | 110.7 | 128.5 |
| LiveKit, multi-region set | PCM 2.5 ms, data channel | 2.3 | 33.5 | **108.4** | 128.6 |
| Cloudflare Realtime | PCM 2.5 ms, data channel | 4.9 | 40.0 | 114.5 | 128.8 |

With 1% and 5% loss Needletail's UDP mode lost nothing and its 99th
percentile moved by at most 6 ms; with bursts it lost 0.6% of the Opus parts
in Sydney; LiveKit and Cloudflare lost about 1% and 5%. With equal 1 ms
blocks (the `wsfu1ms` session on wave 2) LiveKit's SFU led PCM by 0.5 to
0.8 ms outside Tokyo; that was not repeated on wave 3.

### The contributor and the nodes

The contributor ran alone on a g6-dedicated-4 and published Opus, FLAC and
PCM renditions (`daw_hls_formats` `flac,opus,pcm`); each configuration ran
one producer. `av-contrib` used 31% of one vCPU with 2.5 ms Opus and 81% with
1 ms PCM, and logged no `publication queue is full` from the deploy of wave 2
(22:09) to the end of the sessions. The primary relay used 18% and 57%, a
London distributor 14% and 29%, an edge 12% to 29% (Opus) and up to 176%
(1 ms PCM) with one to ten listeners.

## The earlier build (wave 1)

The same dedicated lab measured `av-mesh` `c5d75c8` / `av-contrib` `7a41cea`
in three sessions (19:44 to 21:30 UTC, 21 November) with 5 ms parts:
Needletail's Opus from its DAW source (its 512-sample host buffer taken off by
calculation) and PCM from the AEP1 sender. Mouth to ear, median, London /
New York / Tokyo / Sydney (WebRTC Opus corrected to the live rule):

| Lane | Needletail UDP | Needletail LL-HLS | LiveKit, best self-hosted setup | LiveKit Cloud |
| --- | --- | --- | --- | --- |
| Opus | 8.6 / 39.6 / 116.4 / 134.7 | 9.2 / 40.1 / 117.1 / 135.5 | 4.8 / 35.8 / 112.5 / 130.8 | 18.2 / 60.1 / 135.7 / 143.6 |
| PCM | 5.4 / 36.1 / 112.8 / 131.3 | 6.1 / 36.8 / 113.5 / 132.0 | 2.8 / 33.4 / 107.7 / 128.5 | 16.0 / 58.1 / 131.6 / 141.7 |

Wave 2 took 2.7 to 4.3 ms off Needletail's lossless audio and 2.9 to 4.9 ms
off its Opus, against the same LiveKit. Wave 1's crowds of 5 ms PCM over UDP
ran late in London when 25 UDP and 25 HTTP/3 listeners shared two edges, as
wave 2's 1 ms PCM does. Its evidence is kept as
[`20261121-compare-audio-wave1.json`](evidence/20261121-compare-audio-wave1.json).

## Side note: the first pass, on shared Nanodes (superseded)

The first pass (sessions A, B and C, 14:08 to 16:17 UTC) ran every Needletail
role, every LiveKit node and the smaller listeners on Nanodes, shared-CPU
instances of 1 vCPU, with Needletail's `regional-llhls-cache` build and LL-HLS
only. The owner ruled that shared instances measure the neighbours, not the
system, and those results are superseded. What they showed, for the record:

- Steal time on shared hosts put a spike of about a second, once a minute,
  into every Needletail lane until ten Nanodes were replaced. Needletail has
  four nodes in the path; each was a chance of a noisy host.
- On that build Needletail's Opus was 12.0, 47.1, 118.6 and 138.5 ms mouth to
  ear (LL-HLS) against self-hosted LiveKit's 1.9, 32.6, 109.1 and 127.8
  (4.4, 35.1, 111.6 and 130.3 with the look-ahead that its sender skipped;
  the first pass's evidence is not corrected). On the dedicated lab, wave 1
  was 9.2, 40.1, 117.1 and 135.5 over LL-HLS and 8.6, 39.6, 116.4 and 134.7
  over UDP; wave 2 is in the headline.
- A contributor on one shared vCPU could publish one audio rendition, not
  three.

Its evidence is kept as
[`20261121-compare-audio-nanode.json`](evidence/20261121-compare-audio-nanode.json).

## Not measured

- **Needletail in a browser.** The Opus parts are SoundKit frames and the PCM
  parts are raw samples. A browser needs a player written for them; the UDP
  mode needs WebTransport to carry its datagrams.
- **Needletail's FLAC in wave 2.** In the three sessions the lab tool had
  copied an older `daw-test-source` over the wave 2 build, and it refused the
  wave 2 flags, so no DAW source ran (fixed: `NEEDLETAIL_DAW_NEXUS_ROOT`). A
  later FLAC session (`wflac`) ran the right binary, but wave 2's DAW source
  sends its lossless audio as PCM unless `AUDIO_LOSSLESS_PAYLOAD=flac` is
  set, so no FLAC stream existed. FLAC is measured in wave 1 only.
- **Opus frames shorter than 2.5 ms.** Custom CELT frames of 1 to 1.5 ms are
  on another session's branch (`opus-custom-1ms`), not yet pushed and
  proven; standard 2.5 ms Opus is the default measured here.
- **Sound quality.** Every lane delivered its packets unchanged, so quality
  is the codec's: 256 kbit/s Opus on the SFUs, 83 kbit/s of Opus on
  Needletail (26-byte packets every 2.5 ms), and no coding loss on the lossless lanes: every PCM unit that
  arrived was bit for bit the unit sent. No listening score
  was computed.
- **A listener far from an edge.** Every listener was in the data centre of
  its edge or, for Cloudflare and LiveKit Cloud, as near as their networks
  put it. The impairment steps stand in for a real access network.
- **Cloudflare Stream Live with RTMPS or SRT in and HLS or LL-HLS out.**
  Cloudflare reset the connection of every ingest to a live input with
  recording on, which HLS playback needs, and accepted ingest to an input
  with recording off. The account's Stream storage allowance is 0 minutes:
  it has no Stream subscription. None was bought.
- **Other providers.** AWS IVS, Mux, Agora, Dolby OptiView, Ant Media and
  Phenix need accounts that do not exist. Published figures are in the video
  record.

## Open

- In wave 2 Needletail's 2.5 ms Opus is 3.0 to 3.1 ms later than its 1 ms
  PCM over UDP. The frames (0.75 ms) and the look-ahead (2.5 ms) would make
  3.25 ms, so Opus's parts crossed the mesh about 0.2 ms faster; why is not
  known.
- The UDP listeners of 1 ms PCM in the London crowd fell behind while the
  busier London edge was at 148% to 167% of one vCPU, shared with 25
  pipelined HTTP/3 readers. In the first crowd pass, with 25 HTTP readers and
  no UDP listener, one London edge was already at 144% to 147%. The UDP
  sender's own share was not measured; the 4K record's tier test measures
  UDP listeners alone. Whether the edge's UDP
  sender or the 25 HTTP/3 readers sharing the edges took the processor was
  not separated; the tier record measures UDP listeners alone.
- The LL-HLS readers need a window longer than the round trip in time; 40
  parts of 1 ms or 2.5 ms are 40 or 100 ms.
- LiveKit Cloud's loss of 88% of data-channel blocks with a 100 ms delay was
  not investigated.
- Cloudflare Realtime's data channel to Sydney ran seconds late in two
  sessions of three (3.2 to 10.6 s at the median in most steps of sessions 1
  and 2); session 3 was normal.

## Evidence and harness

- Evidence: [`20261122-compare-audio.json`](evidence/20261122-compare-audio.json):
  the three wave 2 sessions and the PCM curve, for each step, host and lane
  the figures of the probe and of Chrome, and the processor of each node.
  WebRTC Opus figures of the first session carry
  `webrtc_opus_send_corrected_ms`.
- Raw records (every unit of every listener) are under
  `target/compare/runs/{waudio-1,waudio-2,waudio-3,wcurve-24,wcurve-12}/` and
  are not versioned.
- Harness, on branch `compare-cloudflare`:
  - `tools/ntcmp`: the paced source, the WebRTC and data-channel probes, the
    HLS probe with its three readers, and `udp-ingest` for the UDP mode;
  - `scripts/compare/compare-audio.py`: the session runner;
  - `scripts/compare/compare-audio-analyse.py` and
    `compare-audio-report.py`: the evidence file and these tables;
  - `scripts/compare/player/`: the Chrome listener;
  - `scripts/compare/netem.sh`, `steal-check.py`, `cloudflare.py`,
    `compare_lab.py`, `prepare-media.py`.

To run it again, with the lab of
`deploy/compare-cloudflare/topology-dedicated.json` up and Needletail deployed
from the builds named above:

```sh
scripts/compare/cloudflare.py setup
scripts/compare/compare_lab.py livekit
scripts/compare/compare_lab.py prepare
scripts/compare/compare_lab.py audio
scripts/compare/compare_lab.py secrets
scripts/compare/steal-check.py
scripts/compare/compare-audio.py --session x --configs opus-2.5,opus-20,pcm \
    --phases baseline,impair,fanout --product-reader \
    --lanes cf,whep,lkregion,lkmulti,lkcloud,nt-popus,nt-flac,nt-pcm,nt-uc
scripts/compare/compare-audio.py --session x-curve --configs pcm --nt-pcm-samples 24 --phases baseline,impair
scripts/compare/compare-audio-analyse.py --out evidence.json x
scripts/compare/compare-audio-report.py evidence.json
scripts/compare/cloudflare.py teardown
```

## Tables

### Opus, shortest frame (2.5 ms)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.5 ms | 5.7 (5.5–6.0) | 36.3 (36.2–36.3) | 111.5 (111.4–111.6) | 131.6 (131.1–131.8) |
| Needletail, LL-HLS, reader as shipped (popus) | 2.5 ms | 6.3 (6.0–6.5) | 36.7 (36.7–36.8) | 111.9 (111.9–112.1) | 132.1 (131.7–132.3) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 2.5 ms | 6.0 (6.0–6.5) | 36.8 (36.8–36.9) | 112.1 (111.9–112.1) | 132.0 (131.7–132.3) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 2.5 ms | 6.3 (5.8–6.6) | 36.8 (36.8–36.9) | 112.0 (111.9–112.2) | 132.1 (131.6–132.4) |
| Cloudflare Realtime | 2.5 ms | 9.8 (7.7–9.9) | 42.4 (42.3–43.8) | 117.3 (117.2–124.1) | 148.8 (133.1–149.4) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 9.6 (9.3–10.2) | 43.1 (42.5–43.5) | 119.3 (117.0–121.2) | 133.5 (131.8–149.7) |
| LiveKit, SFU in each region | 2.5 ms | 5.2 (4.9–5.4) | 40.9 (40.9–41.0) | 113.8 (110.2–113.9) | 130.8 (130.5–131.1) |
| LiveKit, multi-region set | 2.5 ms | 5.3 (5.0–5.5) | 35.6 (35.6–35.7) | 113.9 (112.9–114.1) | 131.0 (130.5–132.3) |
| LiveKit Cloud | 2.5 ms | 18.3 (18.2–19.0) | 60.5 (60.2–60.9) | 133.4 (133.4–133.6) | 143.8 (143.1–144.1) |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.5 ms | 6.8 (6.5–7.0) | 37.4 (37.3–37.5) | 112.6 (112.4–112.8) | 132.7 (132.2–135.4) |
| Needletail, LL-HLS, reader as shipped (popus) | 2.5 ms | 8.3 (7.5–8.3) | 38.5 (38.4–39.0) | 113.8 (113.6–114.0) | 133.9 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 2.5 ms | 7.6 (7.3–8.3) | 38.7 (38.6–39.1) | 114.0 (113.9–114.1) | 133.9 (133.7–135.4) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 2.5 ms | 8.5 (7.3–8.6) | 38.9 (38.7–39.2) | 113.9 (113.7–114.2) | 133.8 (133.2–134.8) |
| Cloudflare Realtime | 2.5 ms | 11.4 (9.0–11.4) | 43.9 (43.6–45.4) | 118.8 (118.6–125.6) | 150.4 (134.6–150.7) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 11.1 (10.6–11.9) | 44.4 (43.9–45.2) | 122.2 (118.3–122.7) | 136.2 (133.2–151.1) |
| LiveKit, SFU in each region | 2.5 ms | 6.4 (6.2–6.7) | 42.2 (42.2–42.3) | 115.2 (111.5–115.3) | 132.0 (131.8–132.5) |
| LiveKit, multi-region set | 2.5 ms | 6.5 (6.3–6.8) | 36.9 (36.9–37.0) | 115.2 (114.2–115.5) | 132.6 (131.8–133.5) |
| LiveKit Cloud | 2.5 ms | 20.3 (20.1–21.0) | 62.9 (62.6–63.4) | 135.9 (135.7–136.1) | 146.1 (145.3–146.4) |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.5 ms | 8.0 (7.5–8.0) | 38.7 (38.6–38.8) | 113.9 (113.7–114.4) | 134.5 (133.6–1682.9) |
| Needletail, LL-HLS, reader as shipped (popus) | 2.5 ms | 11.3 (10.5–11.7) | 41.6 (40.7–43.5) | 116.7 (116.4–117.5) | 136.8 (136.5–140.5) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 2.5 ms | 10.2 (9.0–11.3) | 40.9 (40.8–42.7) | 116.3 (116.2–117.1) | 139.8 (137.2–1832.4) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 2.5 ms | 11.2 (9.5–11.2) | 41.2 (41.0–42.8) | 116.6 (116.5–117.1) | 137.2 (135.5–1813.1) |
| Cloudflare Realtime | 2.5 ms | 12.7 (9.8–12.7) | 45.0 (44.3–46.6) | 119.9 (119.4–126.7) | 151.5 (135.5–151.5) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 12.3 (11.4–13.1) | 45.4 (45.1–46.3) | 123.2 (119.4–124.0) | 152.2 (134.2–164.4) |
| LiveKit, SFU in each region | 2.5 ms | 7.1 (6.6–7.1) | 42.9 (42.8–43.0) | 116.0 (112.2–116.1) | 132.3 (132.1–133.9) |
| LiveKit, multi-region set | 2.5 ms | 7.2 (6.8–7.3) | 37.6 (37.5–37.8) | 116.4 (115.9–122.2) | 133.4 (132.1–134.1) |
| LiveKit Cloud | 2.5 ms | 20.8 (20.8–21.5) | 63.9 (63.5–64.2) | 136.7 (136.6–137.0) | 147.1 (146.2–147.1) |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.5 ms | 1.9 (1.8–2.2) | 2.2 (2.1–2.4) | 2.3 (2.1–2.7) | 3.0 (2.4–1549.1) |
| Needletail, LL-HLS, reader as shipped (popus) | 2.5 ms | 5.1 (4.5–5.2) | 4.8 (3.9–6.7) | 4.8 (4.4–5.5) | 4.6 (4.0–8.7) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 2.5 ms | 4.2 (3.0–4.8) | 4.0 (4.0–6.0) | 4.3 (4.0–5.0) | 8.1 (5.3–1699.8) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 2.5 ms | 4.6 (3.6–5.0) | 4.4 (4.1–6.0) | 4.7 (4.4–4.9) | 5.2 (3.9–1680.7) |
| Cloudflare Realtime | 2.5 ms | 2.6 (1.9–2.7) | 2.3 (1.7–2.6) | 2.3 (1.7–2.4) | 2.1 (1.6–2.4) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 2.5 (1.8–2.6) | 2.4 (2.0–2.5) | 2.6 (2.0–3.8) | 2.3 (2.2–30.9) |
| LiveKit, SFU in each region | 2.5 ms | 1.3 (1.2–1.6) | 1.7 (1.7–1.8) | 1.9 (1.7–2.1) | 1.1 (1.1–3.0) |
| LiveKit, multi-region set | 2.5 ms | 1.4 (1.3–1.6) | 1.7 (1.6–1.7) | 2.0 (1.8–9.3) | 1.4 (1.2–2.0) |
| LiveKit Cloud | 2.5 ms | 1.9 (1.7–2.1) | 2.7 (2.6–2.9) | 2.7 (2.7–3.0) | 2.6 (2.6–2.8) |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 (0.000–2.251) |
| Needletail, LL-HLS, reader as shipped (popus) | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 (0.000–0.020) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 (0.000–0.094) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 (0.000–0.097) |
| Cloudflare Realtime | 2.5 ms | 0.000 (0.000–0.010) | 0.000 (0.000–0.005) | 0.005 (0.000–0.010) | 0.005 (0.000–6.624) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 0.000 (0.000–0.044) | 0.005 (0.000–0.045) | 0.005 (0.000–3.064) | 0.000 (0.000–6.409) |
| LiveKit, SFU in each region | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 (0.000–2.959) |
| LiveKit, multi-region set | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 (0.000–4.579) |
| LiveKit Cloud | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, reader as shipped (popus) | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Realtime | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, SFU in each region | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, multi-region set | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit Cloud | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.5 ms | 141.4 (108.4–1074.1) | 1510.0 (1244.4–1837.0) | 86.8 (52.0–117.9) | 1473.5 (1136.4–1883.1) |
| Needletail, LL-HLS, reader as shipped (popus) | 2.5 ms | 1073.0 (145.6–1672.1) | 1507.5 (1238.6–1838.8) | 77.5 (37.4–131.3) | 1468.7 (144.9–1852.3) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 2.5 ms | 1331.2 (1068.2–1670.6) | 1507.6 (1241.6–1834.3) | 93.7 (31.4–131.6) | 99.4 (54.8–145.2) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 2.5 ms | 150.8 (60.0–1671.5) | 1504.7 (1232.8–1841.2) | 75.0 (49.7–121.8) | 1136.1 (52.5–1465.2) |
| Cloudflare Realtime | 2.5 ms | 910.7 (766.4–1115.5) | 1824.9 (1711.8–1859.6) | 3655.1 (3544.9–3853.6) | 4797.1 (2792.6–4814.7) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 1193.5 (1077.3–1499.7) | 2419.2 (2389.3–2551.6) | 4752.6 (4471.5–4873.2) | 3975.1 (3943.0–5728.7) |
| LiveKit, SFU in each region | 2.5 ms | 159.8 (156.7–160.0) | 160.3 (157.3–163.0) | 161.1 (158.4–163.8) | 160.1 |
| LiveKit, multi-region set | 2.5 ms | 166.6 (161.8–168.1) | 1225.3 (1190.5–1274.4) | 3961.0 (3958.7–3995.0) | 4681.6 (4448.2–4691.4) |
| LiveKit Cloud | 2.5 ms | 267.8 (247.1–279.5) | 467.7 (465.2–471.2) | 1407.5 (1168.5–1651.6) | 967.0 (793.9–1785.3) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.5 ms | 147.2 | 147.2 | 147.2 | 147.2 (143.9–147.2) |
| Needletail, LL-HLS, reader as shipped (popus) | 2.5 ms | 147.2 | 147.2 | 147.2 | 147.2 (109.7–147.2) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 2.5 ms | 147.2 | 147.2 | 147.2 | 147.2 (113.2–147.2) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 2.5 ms | 147.2 | 147.2 | 147.2 | 147.2 (112.3–147.2) |
| Cloudflare Realtime | 2.5 ms | 256.0 | 256.0 | 256.0 (256.0–256.1) | 256.0 (239.0–256.0) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 256.0 | 256.0 | 256.0 (248.2–256.0) | 256.0 (239.6–256.0) |
| LiveKit, SFU in each region | 2.5 ms | 256.0 | 256.0 | 256.0 | 256.0 (248.4–256.0) |
| LiveKit, multi-region set | 2.5 ms | 256.0 | 256.0 | 256.0 | 256.0 (244.3–256.0) |
| LiveKit Cloud | 2.5 ms | 256.0 | 256.0 | 256.0 | 256.0 |

Chrome mouth to ear at the marker, median (ms):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 71.5 (70.0–92.2) | 115.8 (111.0–133.5) | 197.8 (189.1–204.8) | 216.2 (194.8–229.8) |
| Cloudflare Stream WHIP/WHEP | 85.8 (74.8–86.7) | 128.1 (109.4–132.7) | 193.2 (176.8–204.5) | 227.0 (208.9–245.4) |
| LiveKit, SFU in each region | 72.0 (71.5–74.9) | 120.5 (104.2–123.2) | 177.3 (172.9–180.7) | 197.2 (193.4–212.5) |
| LiveKit, multi-region set | 84.7 (65.6–85.9) | 108.1 (104.8–109.8) | 180.1 (178.8–182.8) | 195.3 (189.1–196.6) |
| LiveKit Cloud | 84.9 (84.4–85.1) | 122.3 (120.1–146.7) | 203.6 (198.2–207.8) | 214.7 (209.0–221.2) |

Chrome jitter buffer, mean (ms):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 16.5 (15.6–27.6) | 14.5 (13.7–35.2) | 25.9 (22.3–28.7) | 19.1 (16.8–30.8) |
| Cloudflare Stream WHIP/WHEP | 23.6 (22.2–25.9) | 24.0 (16.1–32.7) | 21.6 (15.0–35.9) | 26.4 (19.0–96.0) |
| LiveKit, SFU in each region | 14.0 (11.9–19.8) | 21.9 (11.7–31.6) | 11.5 (10.6–19.2) | 12.2 (12.0–31.3) |
| LiveKit, multi-region set | 29.5 (12.2–29.6) | 12.2 (11.7–15.2) | 18.0 (14.4–19.3) | 14.6 (14.2–19.8) |
| LiveKit Cloud | 11.7 (11.2–13.8) | 14.2 (12.4–30.4) | 15.2 (12.5–20.0) | 19.8 (12.6–21.1) |

Chrome concealed samples (%):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 0.026 (0.000–0.050) | 0.000 (0.000–0.122) | 0.055 (0.000–0.078) | 0.000 (0.000–0.075) |
| Cloudflare Stream WHIP/WHEP | 0.156 (0.000–0.185) | 0.000 (0.000–0.224) | 0.000 (0.000–0.180) | 0.000 (0.000–14.234) |
| LiveKit, SFU in each region | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit, multi-region set | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit Cloud | 0.000 | 0.000 | 0.000 | 0.000 |

### Frame length

Mouth to ear, played on arrival, median (ms), London / Sydney:

| System | 2.5 ms | 5 ms | 10 ms | 20 ms | 10 ms at 510 kbit/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 5.7 / 131.6 (uc) | – / – (uc) | – / – (uc) | – / – (uc) | – / – (uc) |
| Needletail, LL-HLS, reader as shipped | 6.3 / 132.1 (popus) | – / – (opus) | – / – (opus) | – / – (flac) | – / – (flac) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 | 6.0 / 132.0 (popus) | – / – (opus) | – / – (opus) | – / – (flac) | – / – (flac) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 | 6.3 / 132.1 (popus) | – / – (opus) | – / – (opus) | – / – (flac) | – / – (flac) |
| Cloudflare Realtime | 9.8 / 148.8 | – / – | – / – | – / – | – / – |
| Cloudflare Stream WHIP/WHEP | 9.6 / 133.5 | – / – | – / – | – / – | – / – |
| LiveKit, SFU in each region | 5.2 / 130.8 | – / – | – / – | – / – | – / – |
| LiveKit, multi-region set | 5.3 / 131.0 | – / – | – / – | – / – | – / – |
| LiveKit Cloud | 18.3 / 143.8 | – / – | – / – | – / – | – / – |

### Lossless (PCM; SFUs by data channel)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | 2.7 (2.7–3.4) | 33.3 (33.1–33.3) | 108.5 | 128.5 (128.1–128.6) |
| Needletail, LL-HLS, reader as shipped (pcm) | 1 ms | 183.3 (4.1–1033.0) | 46.8 (35.0–2677.2) | 112.6 (110.7–129.5) | 131.6 (129.2–492.8) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 1 ms | 6.6 (3.9–7.7) | 36.9 (34.8–38.6) | 110.6 (110.1–112.0) | 130.2 (129.7–131.7) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1 ms | 6.5 (3.7–7.4) | 36.5 (34.5–38.2) | 110.4 (109.9–111.7) | 130.4 (129.7–131.4) |
| Cloudflare Realtime | 2.5 ms | 5.6 (5.4–7.4) | 40.2 (39.5–41.1) | 115.8 (114.1–115.8) | 3238.6 (146.2–6979.8) |
| LiveKit, SFU in each region | 2.5 ms | 3.1 (2.9–3.8) | 38.7 (33.7–38.9) | 111.7 (110.9–111.9) | 128.9 (128.9–130.0) |
| LiveKit, multi-region set | 2.5 ms | 3.2 (3.0–3.9) | 33.8 (33.6–39.0) | 112.0 (109.0–112.1) | 129.9 (129.2–130.2) |
| LiveKit Cloud | 2.5 ms | 17.0 (16.5–17.2) | 58.6 (58.5–59.8) | 133.8 (127.8–134.2) | 142.1 (142.0–142.3) |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | 5.6 (5.4–6.4) | 36.4 (35.9–36.8) | 111.4 (111.4–112.1) | 131.4 (131.0–131.8) |
| Needletail, LL-HLS, reader as shipped (pcm) | 1 ms | 728.3 (29.1–6202.9) | 159.8 (49.3–3962.7) | 146.8 (132.0–680.9) | 169.4 (137.0–5273.1) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 1 ms | 26.2 (15.2–31.5) | 56.3 (46.2–60.2) | 129.6 (122.8–135.3) | 153.1 (152.9–179.2) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1 ms | 26.9 (14.2–31.6) | 56.0 (44.6–59.8) | 128.2 (124.8–134.4) | 161.4 (153.3–167.2) |
| Cloudflare Realtime | 2.5 ms | 7.6 (7.3–10.1) | 42.3 (41.6–43.4) | 117.8 (116.2–5620.3) | 8224.5 (148.4–10732.7) |
| LiveKit, SFU in each region | 2.5 ms | 5.2 (4.9–5.8) | 40.8 (35.8–40.9) | 113.9 (113.1–114.1) | 131.1 (130.8–131.9) |
| LiveKit, multi-region set | 2.5 ms | 5.3 (5.0–6.0) | 36.1 (35.9–41.2) | 114.3 (111.2–115.0) | 132.4 (131.1–132.7) |
| LiveKit Cloud | 2.5 ms | 19.4 (18.8–19.6) | 61.4 (61.3–62.6) | 136.6 (130.4–137.2) | 144.9 (144.7–145.1) |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | 7.8 (7.8–8.9) | 38.7 (38.2–39.2) | 114.2 (113.8–116.5) | 137.2 (134.0–203.8) |
| Needletail, LL-HLS, reader as shipped (pcm) | 1 ms | 840.8 (38.8–7106.1) | 345.9 (58.4–4107.6) | 167.5 (143.6–807.0) | 185.6 (143.2–5425.9) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 1 ms | 37.6 (25.2–43.9) | 71.8 (55.5–72.3) | 142.8 (133.4–217.0) | 165.0 (164.8–459.1) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1 ms | 37.9 (23.4–45.0) | 71.9 (51.8–72.5) | 140.5 (139.1–309.9) | 340.1 (164.8–957.7) |
| Cloudflare Realtime | 2.5 ms | 9.7 (9.0–15.0) | 44.5 (43.5–45.5) | 119.6 (118.2–5747.7) | 8351.2 (183.9–10856.6) |
| LiveKit, SFU in each region | 2.5 ms | 7.3 (6.9–8.0) | 42.8 (37.8–42.9) | 116.1 (115.4–116.4) | 134.1 (132.7–155.4) |
| LiveKit, multi-region set | 2.5 ms | 7.6 (7.1–8.2) | 38.3 (38.1–43.6) | 116.6 (113.5–215.8) | 135.8 (133.2–336.6) |
| LiveKit Cloud | 2.5 ms | 21.3 (20.6–21.5) | 63.3 (63.0–64.4) | 138.4 (132.2–139.7) | 147.5 (146.2–162.9) |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | 5.2 (5.2–5.5) | 5.5 (5.2–6.1) | 5.8 (5.5–8.1) | 8.8 (5.5–75.7) |
| Needletail, LL-HLS, reader as shipped (pcm) | 1 ms | 657.4 (34.7–6073.0) | 298.9 (23.3–1430.4) | 54.9 (32.9–677.5) | 54.0 (14.1–4933.0) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 1 ms | 31.0 (21.3–36.3) | 33.6 (20.7–35.0) | 32.3 (23.3–105.2) | 35.4 (34.6–327.2) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1 ms | 31.4 (19.8–37.6) | 34.3 (17.3–35.5) | 30.1 (29.2–198.4) | 208.7 (35.2–827.4) |
| Cloudflare Realtime | 2.5 ms | 4.0 (3.5–7.7) | 4.2 (3.8–4.3) | 4.0 (3.7–5632.2) | 3876.8 (38.0–5111.7) |
| LiveKit, SFU in each region | 2.5 ms | 4.1 (3.8–4.2) | 4.0 (4.0–4.1) | 4.4 (4.3–4.5) | 3.9 (3.6–26.8) |
| LiveKit, multi-region set | 2.5 ms | 4.2 (4.0–4.3) | 4.4 (4.4–4.6) | 4.7 (4.3–102.5) | 5.6 (3.9–207.2) |
| LiveKit Cloud | 2.5 ms | 4.1 (3.9–4.2) | 4.4 | 4.5 (4.2–5.4) | 5.0 (3.9–20.6) |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, reader as shipped (pcm) | 1 ms | 0.000 (0.000–0.002) | 0.000 (0.000–0.004) | 0.000 | 0.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 1 ms | 0.008 (0.000–0.027) | 0.000 (0.000–0.012) | 0.000 (0.000–0.004) | 0.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1 ms | 0.002 (0.000–0.020) | 0.000 (0.000–0.002) | 0.000 | 0.004 (0.000–0.004) |
| Cloudflare Realtime | 2.5 ms | 0.024 (0.005–0.044) | 0.020 (0.005–0.040) | 0.046 (0.005–0.464) | 0.038 (0.005–0.138) |
| LiveKit, SFU in each region | 2.5 ms | 0.000 (0.000–0.005) | 0.000 | 0.000 (0.000–0.005) | 0.000 (0.000–0.005) |
| LiveKit, multi-region set | 2.5 ms | 0.010 (0.005–0.015) | 0.020 (0.015–0.025) | 0.026 (0.021–0.026) | 0.048 (0.011–0.059) |
| LiveKit Cloud | 2.5 ms | 0.000 (0.000–0.005) | 0.000 | 0.000 (0.000–0.249) | 0.519 (0.000–1.493) |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, reader as shipped (pcm) | 1 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 1 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Realtime | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, SFU in each region | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, multi-region set | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit Cloud | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | 523.6 (297.2–1072.9) | 373.2 (253.8–441.7) | 1076.8 (491.9–1961.4) | 411.5 (385.2–433.6) |
| Needletail, LL-HLS, reader as shipped (pcm) | 1 ms | 50.3 (45.0–582.0) | 726.3 (39.3–787.9) | 33.7 (21.6–1496.5) | 1363.9 (37.3–1458.5) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 1 ms | 561.6 (47.0–1067.7) | 682.2 (35.1–815.9) | 26.5 (16.7–1396.7) | 26.5 (21.5–1353.5) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1 ms | 535.3 (29.5–1063.6) | 705.0 (39.4–848.5) | 530.3 (28.5–1412.0) | 27.9 (27.0–1429.1) |
| Cloudflare Realtime | 2.5 ms | 703.9 (678.1–809.4) | 1672.7 (1506.4–1700.7) | 2722.0 (2667.4–2940.8) | 3730.1 (3267.8–3777.5) |
| LiveKit, SFU in each region | 2.5 ms | 69.9 (47.0–109.1) | 54.5 (54.2–90.4) | 51.3 (45.7–57.0) | 47.6 (39.7–57.7) |
| LiveKit, multi-region set | 2.5 ms | 81.2 (62.9–97.2) | 1252.5 (1146.3–1327.7) | 4183.6 (4122.2–4329.1) | 4887.4 (4883.2–4889.1) |
| LiveKit Cloud | 2.5 ms | 275.8 (272.0–347.0) | 475.7 (424.2–531.1) | 1728.5 (1298.5–1776.7) | 1385.5 (1260.2–1831.7) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | 2304.0 | 2304.0 | 2304.0 (2303.9–2304.2) | 2304.0 |
| Needletail, LL-HLS, reader as shipped (pcm) | 1 ms | 2304.0 (2031.2–2336.4) | 2303.7 (2196.4–2306.0) | 2304.0 (2303.4–2304.0) | 2304.0 (2070.5–2304.0) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 1 ms | 2303.3 (2303.1–2304.0) | 2303.7 (2303.7–2304.4) | 2303.9 (2303.3–2304.3) | 2304.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1 ms | 2303.8 (2302.9–2304.0) | 2303.7 (2303.7–2304.0) | 2303.9 (2303.4–2304.0) | 2303.8 (2302.3–2304.0) |
| Cloudflare Realtime | 2.5 ms | 2303.6 (2302.9–2303.9) | 2303.5 (2303.1–2303.9) | 2303.9 (2303.0–2469.4) | 2500.1 (2303.8–2505.0) |
| LiveKit, SFU in each region | 2.5 ms | 2303.9 (2303.8–2304.0) | 2303.9 | 2303.9 (2303.8–2304.1) | 2303.9 |
| LiveKit, multi-region set | 2.5 ms | 2304.0 (2303.6–2304.0) | 2303.5 (2303.3–2303.6) | 2303.4 (2303.2–2303.7) | 2303.0 (2302.7–2303.7) |
| LiveKit Cloud | 2.5 ms | 2304.1 (2303.9–2304.1) | 2304.1 (2303.9–2304.2) | 2304.1 (2298.1–2304.1) | 2292.0 (2269.7–2304.1) |

### Needletail FLAC (beside 20 ms Opus on the SFUs)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1 ms | -1.0 | -1.0 | -1.0 | -1.0 |
| Needletail, LL-HLS, reader as shipped (flac) | 1 ms | -1.0 | -1.0 | -1.0 | -1.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 1 ms | -1.0 | -1.0 | -1.0 | -1.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 1 ms | -1.0 | -1.0 | -1.0 | -1.0 |
| Cloudflare Realtime | 20 ms | 842.6 (654.7–847.4) | 1772.2 (1461.8–1796.5) | 3494.7 (3452.4–3946.9) | 3383.5 (2546.2–4634.9) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 1169.4 (1090.3–1320.0) | 2435.8 (2398.4–2618.8) | 4902.5 (4384.7–5050.4) | 4290.2 (3895.2–5447.3) |
| LiveKit, SFU in each region | 20 ms | 175.5 (170.4–175.6) | 167.5 (157.5–168.0) | 175.1 (158.8–175.5) | 171.1 (162.5–173.9) |
| LiveKit, multi-region set | 20 ms | 172.8 (164.5–179.5) | 1246.7 (1230.0–1266.0) | 4016.6 (3652.7–4083.3) | 4728.4 (4698.1–5019.2) |
| LiveKit Cloud | 20 ms | 250.4 (224.7–256.1) | 473.6 (443.0–475.2) | 1303.4 (1299.4–1750.6) | 1299.2 (1255.5–1367.0) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |

### Impairment, opus-2.5

Units missing at the listener (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.00 / 0.00 (0.00–2.25) | 0.00 / 0.00 | 0.00 / 0.00 | 0.51 (0.48–0.57) / 0.54 (0.44–0.56) | 0.00 / 0.00 |
| Needletail, LL-HLS, reader as shipped (popus) | 0.00 / 0.00 (0.00–0.02) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 0.00 / 0.00 (0.00–0.09) | 0.00 / 0.00 | 0.00 / 0.00 (0.00–0.02) | 0.00 / 0.00 | 0.00 / 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 0.00 / 0.00 (0.00–0.10) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 (0.00–0.02) / 0.00 | 0.00 / 0.00 |
| Cloudflare Realtime | 0.00 / 0.01 (0.00–6.62) | 1.03 (0.90–1.26) / 1.15 (0.90–1.22) | 4.83 (4.62–4.90) / 4.91 (4.81–4.96) | 4.68 (4.59–4.82) / 5.02 (4.67–5.18) | 0.00 (0.00–0.01) / 0.00 |
| Cloudflare Stream WHIP/WHEP | 0.00 (0.00–0.04) / 0.00 (0.00–6.41) | 1.05 (0.97–1.19) / 0.91 (0.90–12.24) | 5.25 (5.08–5.25) / 5.05 (4.93–9.16) | 4.78 (4.69–5.00) / 5.14 (4.76–12.13) | 0.01 (0.00–0.01) / 0.00 (0.00–0.01) |
| LiveKit, SFU in each region | 0.00 / 0.00 (0.00–2.96) | 1.02 (0.95–1.04) / 1.19 (1.06–1.19) | 4.92 (4.49–5.03) / 4.73 (4.63–5.22) | 4.86 (4.61–5.01) / 5.11 (4.75–5.19) | 0.00 / 0.00 |
| LiveKit, multi-region set | 0.00 / 0.00 (0.00–4.58) | 0.87 (0.86–0.92) / 0.94 (0.89–1.00) | 5.26 (5.11–5.31) / 5.12 (5.00–5.37) | 4.71 (4.63–4.82) / 4.82 (4.82–5.26) | 0.00 / 0.00 |
| LiveKit Cloud | 0.00 / 0.00 | 1.01 (0.87–1.05) / 1.06 (1.04–1.15) | 5.00 (4.70–5.15) / 5.03 (4.93–5.14) | 4.99 (4.21–5.11) / 5.09 (4.79–5.42) | 0.00 / 0.00 |

Longest gap (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.0 / 0.0 (0.0–1097.5) | 0.0 / 0.0 | 0.0 / 0.0 | 5.0 / 5.0 (2.5–5.0) | 0.0 / 0.0 |
| Needletail, LL-HLS, reader as shipped (popus) | 0.0 / 0.0 (0.0–2.5) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 0.0 / 0.0 (0.0–20.0) | 0.0 / 0.0 | 0.0 / 0.0 (0.0–2.5) | 0.0 / 0.0 | 0.0 / 0.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 0.0 / 0.0 (0.0–20.0) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 (0.0–5.0) / 0.0 | 0.0 / 0.0 |
| Cloudflare Realtime | 0.0 (0.0–2.5) / 2.5 (0.0–785.0) | 5.0 / 5.0 (2.5–5.0) | 10.0 (7.5–15.0) / 7.5 | 10.0 (7.5–10.0) / 7.5 | 0.0 (0.0–2.5) / 0.0 |
| Cloudflare Stream WHIP/WHEP | 0.0 (0.0–17.5) / 0.0 (0.0–785.0) | 2.5 (2.5–5.0) / 2.5 (2.5–10.0) | 7.5 (5.0–20.0) / 7.5 (5.0–20.0) | 10.0 / 7.5 (7.5–400.0) | 2.5 (0.0–2.5) / 0.0 (0.0–2.5) |
| LiveKit, SFU in each region | 0.0 / 0.0 (0.0–1095.0) | 5.0 (2.5–5.0) / 2.5 (2.5–5.0) | 5.0 (5.0–7.5) / 7.5 (5.0–7.5) | 7.5 / 7.5 (7.5–10.0) | 0.0 / 0.0 |
| LiveKit, multi-region set | 0.0 / 0.0 (0.0–1765.0) | 2.5 (2.5–5.0) / 5.0 (2.5–5.0) | 7.5 (7.5–10.0) / 5.0 (5.0–7.5) | 7.5 / 7.5 (7.5–10.0) | 0.0 / 0.0 |
| LiveKit Cloud | 0.0 / 0.0 | 5.0 (2.5–5.0) / 5.0 | 7.5 (5.0–7.5) / 7.5 (5.0–7.5) | 7.5 (7.5–10.0) / 10.0 (7.5–10.0) | 0.0 / 0.0 |

Mouth to ear, played on arrival, p99 (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 8 / 135 (134–1683) | 8 / 133 | 8 / 134 (133–136) | 8 / 134 | 107 / 234 (233–235) |
| Needletail, LL-HLS, reader as shipped (popus) | 11 (10–12) / 137 (137–141) | 149 (10–203) / 240 (136–306) | 1258 (876–2353) / 2985 (1894–3035) | 1339 (743–1388) / 754 (685–1643) | 29137 (29125–29920) / 28519 (28512–30028) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (popus) | 10 (9–11) / 140 (137–1832) | 80 (10–104) / 180 (138–233) | 536 (213–1153) / 425 (342–970) | 310 (206–1250) / 593 (345–649) | 15304 (14862–15352) / 15712 (15612–15739) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 11 (9–11) / 137 (136–1813) | 11 (11–13) / 136 (136–138) | 13 (12–14) / 140 (139–142) | 53 (31–152) / 168 (157–178) | 15714 (15204–15740) / 15118 (14920–15839) |
| Cloudflare Realtime | 13 (10–13) / 152 (136–152) | 12 (10–13) / 151 (151–170) | 11 (10–13) / 152 (151–171) | 11 (11–12) / 151 (151–170) | 111 (110–112) / 251 (235–251) |
| Cloudflare Stream WHIP/WHEP | 12 (11–13) / 152 (134–164) | 14 (13–16) / 137 (134–172) | 11 (10–13) / 137 (133–152) | 12 (12–13) / 152 (133–247) | 113 (111–113) / 251 (233–252) |
| LiveKit, SFU in each region | 7 / 132 (132–134) | 7 / 132 | 7 / 133 | 7 / 132 | 107 / 232 |
| LiveKit, multi-region set | 7 / 133 (132–134) | 7 / 133 (132–133) | 7 / 133 (132–133) | 7 / 134 (132–134) | 107 / 232 |
| LiveKit Cloud | 21 / 147 | 21 / 148 (147–193) | 21 / 147 (146–194) | 21 (21–35) / 147 (146–148) | 121 / 247 (246–247) |

Chrome concealed samples (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 0.03 (0.00–0.05) / 0.00 (0.00–0.07) | 4.15 (3.79–4.48) / 3.87 (3.73–4.63) | 20.55 (19.75–21.35) / 19.02 (16.63–29.00) | 16.55 (16.14–17.53) / 18.20 (17.53–25.53) | 0.00 / 0.00 (0.00–0.09) |
| Cloudflare Stream WHIP/WHEP | 0.16 (0.00–0.18) / 0.00 (0.00–14.23) | 4.63 (3.72–5.21) / 4.81 (3.22–38.43) | 18.27 (17.13–18.72) / 18.67 (18.12–29.00) | 13.62 (13.00–14.12) / 14.58 (10.94–14.94) | 0.00 (0.00–0.09) / 0.00 |
| LiveKit, SFU in each region | 0.00 / 0.00 | 1.15 (1.11–1.28) / 1.37 (1.13–1.41) | 1.65 (1.50–1.85) / 1.56 (1.44–1.87) | 1.81 (1.50–2.15) / 2.06 (1.60–2.69) | 0.00 / 0.00 (0.00–0.06) |
| LiveKit, multi-region set | 0.00 / 0.00 | 1.05 (1.05–1.10) / 2.33 (2.11–2.44) | 1.65 (1.59–1.90) / 3.35 (3.17–4.29) | 2.33 (1.94–2.61) / 5.34 (5.22–6.51) | 0.00 / 0.00 |
| LiveKit Cloud | 0.00 / 0.00 | 1.20 (1.18–1.37) / 1.36 (1.06–1.42) | 1.84 (1.84–2.13) / 2.02 (1.56–2.18) | 2.30 (2.26–3.03) / 3.79 (3.26–4.33) | 0.00 / 0.00 |

Chrome mouth to ear at the marker, median (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 71 (70–92) / 216 (195–230) | 300 (223–334) / 405 (382–423) | 354 (264–443) / 535 (507–656) | 405 (398–451) / 450 (398–514) | 171 (171–172) / 328 (302–334) |
| Cloudflare Stream WHIP/WHEP | 86 (75–87) / 227 (209–245) | 246 (230–324) / 423 (416–425) | 294 (276–418) / 425 (340–535) | 293 (257–383) / 484 (358–651) | 191 (180–192) / 325 (299–328) |
| LiveKit, SFU in each region | 72 (71–75) / 197 (193–213) | 145 (129–148) / 292 (265–309) | 273 (268–296) / 390 (361–400) | 264 (250–264) / 382 (382–401) | 176 (168–176) / 292 (290–318) |
| LiveKit, multi-region set | 85 (66–86) / 195 (189–197) | 194 (191–199) / 443 (353–449) | 250 (180–257) / 490 (453–500) | 227 (187–237) / 420 (394–439) | 170 (166–171) / 297 (291–298) |
| LiveKit Cloud | 85 / 215 (209–221) | 197 (196–210) / 312 (305–312) | 310 (290–314) / 450 (419–451) | 217 (210–220) / 309 (278–356) | 178 (176–182) / 310 (306–313) |

### Impairment, pcm

Units missing at the listener (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.27 (0.24–0.29) / 0.37 (0.29–0.38) | 0.00 / 0.00 |
| Needletail, LL-HLS, reader as shipped (pcm) | 0.00 / 0.00 | 0.00 (0.00–0.02) / 0.00 | 0.01 (0.00–0.02) / 0.01 (0.00–0.03) | 0.01 (0.00–0.03) / 0.00 (0.00–0.01) | 0.00 / 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 0.01 (0.00–0.03) / 0.00 | 0.01 (0.00–0.01) / 0.00 | 0.03 (0.00–0.06) / 0.08 (0.00–0.08) | 0.01 (0.00–0.13) / 0.00 | 0.00 / 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 0.00 (0.00–0.02) / 0.00 | 0.02 (0.00–0.03) / 0.00 | 0.00 (0.00–0.16) / 0.01 (0.00–0.02) | 0.00 / 0.00 (0.00–0.02) | 0.00 / 0.00 |
| Cloudflare Realtime | 0.02 (0.00–0.04) / 0.04 (0.01–0.14) | 0.96 (0.94–1.05) / 1.03 (1.02–1.08) | 4.98 (4.83–5.21) / 4.84 (4.53–5.29) | 4.91 (4.56–5.07) / 4.68 (4.67–4.78) | 0.02 (0.00–0.03) / 0.01 (0.01–0.06) |
| LiveKit, SFU in each region | 0.00 / 0.00 | 0.94 (0.90–1.05) / 1.02 (0.93–1.11) | 4.99 (4.91–5.19) / 4.97 (4.93–5.05) | 4.74 (4.62–4.84) / 4.71 (4.48–4.88) | 0.04 (0.01–0.08) / 0.06 (0.01–0.08) |
| LiveKit, multi-region set | 0.01 / 0.05 (0.01–0.06) | 1.04 (0.89–1.20) / 1.12 (1.03–1.22) | 5.00 (4.93–5.07) / 5.00 (4.66–5.12) | 4.53 (4.50–5.31) / 4.59 (4.43–5.01) | 0.05 (0.03–0.06) / 0.08 (0.00–0.18) |
| LiveKit Cloud | 0.00 / 0.52 (0.00–1.49) | 0.99 (0.90–0.99) / 0.96 (0.95–1.00) | 5.06 (4.84–5.22) / 4.90 (4.88–5.08) | 4.68 (4.63–4.82) / 4.85 (4.84–5.07) | 88.64 (88.60–88.66) / 88.04 (88.00–88.06) |

Longest gap (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.0 / 0.0 | 0.0 / 0.0 | 1.0 (0.0–1.0) / 0.0 | 2.0 (1.0–3.0) / 2.0 (2.0–3.0) | 0.0 / 0.0 |
| Needletail, LL-HLS, reader as shipped (pcm) | 0.0 (0.0–1.0) / 0.0 | 0.0 (0.0–2.0) / 1.0 (0.0–1.0) | 1.0 (0.0–3.0) / 2.0 (0.0–3.0) | 1.0 (0.0–2.0) / 0.0 (0.0–2.0) | 0.0 / 0.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 2.0 (0.0–8.0) / 0.0 | 1.0 (0.0–2.0) / 0.0 | 6.0 (0.0–15.0) / 4.0 (0.0–8.0) | 2.0 (0.0–35.0) / 0.0 (0.0–1.0) | 0.0 / 0.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1.0 (0.0–9.0) / 1.0 (0.0–2.0) | 4.0 (0.0–5.0) / 1.0 (0.0–1.0) | 1.0 (0.0–21.0) / 1.0 (1.0–3.0) | 0.0 (0.0–1.0) / 0.0 (0.0–3.0) | 0.0 / 0.0 |
| Cloudflare Realtime | 2.5 (2.5–5.0) / 5.0 (2.5–25.0) | 2.5 (2.5–5.0) / 5.0 (2.5–5.0) | 7.5 (5.0–10.0) / 7.5 | 7.5 / 7.5 (7.5–22.5) | 2.5 (0.0–5.0) / 2.5 (2.5–5.0) |
| LiveKit, SFU in each region | 0.0 (0.0–2.5) / 0.0 (0.0–2.5) | 5.0 (2.5–5.0) / 5.0 | 7.5 (5.0–10.0) / 7.5 (5.0–10.0) | 10.0 (7.5–10.0) / 5.0 (5.0–7.5) | 5.0 (2.5–5.0) / 2.5 (2.5–5.0) |
| LiveKit, multi-region set | 2.5 (2.5–5.0) / 5.0 (2.5–15.0) | 5.0 (2.5–5.0) / 5.0 (2.5–57.5) | 7.5 / 7.5 | 7.5 / 7.5 (5.0–10.0) | 2.5 (2.5–5.0) / 2.5 (0.0–5.0) |
| LiveKit Cloud | 0.0 (0.0–2.5) / 245.0 (0.0–752.5) | 2.5 (2.5–5.0) / 5.0 (2.5–5.0) | 5.0 (5.0–7.5) / 7.5 | 10.0 (7.5–10.0) / 10.0 | 155.0 (122.5–167.5) / 147.5 (145.0–152.5) |

Mouth to ear, played on arrival, p99 (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 8 (8–9) / 137 (134–204) | 10 (9–10) / 134 (133–136) | 10 (8–11) / 134 (133–135) | 11 (9–13) / 134 (133–145) | 106 (106–107) / 231 (231–234) |
| Needletail, LL-HLS, reader as shipped (pcm) | 841 (39–7106) / 186 (143–5426) | 17988 (206–18500) / 518 (328–14337) | 19635 (9123–20586) / 9720 (2321–17755) | 19530 (4833–19738) / 2613 (1102–16715) | 29648 (29553–29746) / 29863 (29693–29947) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 38 (25–44) / 165 (165–459) | 169 (74–198) / 306 (204–309) | 5176 (1145–6878) / 1719 (1195–2279) | 611 (544–1033) / 756 (701–1009) | 24565 (24544–24606) / 24661 (24565–24752) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 38 (23–45) / 340 (165–958) | 59 (36–96) / 174 (166–195) | 118 (46–179) / 178 (167–193) | 99 (90–122) / 212 (205–233) | 24884 (24522–24920) / 25015 (24938–25018) |
| Cloudflare Realtime | 10 (9–15) / 8351 (184–10857) | 11 / 9921 (168–11065) | 9 (8–10) / 168 (168–11095) | 10 (10–11) / 173 (170–4054) | 108 / 9854 (248–9860) |
| LiveKit, SFU in each region | 7 (7–8) / 134 (133–155) | 8 (7–9) / 133 (132–391) | 7 (6–7) / 135 (132–2270) | 7 / 133 (132–134) | 106 / 231 (230–232) |
| LiveKit, multi-region set | 8 (7–8) / 136 (133–337) | 8 (7–8) / 134 (131–134) | 7 / 134 (134–10429) | 8 (7–8) / 133 (131–134) | 107 (107–108) / 232 (232–234) |
| LiveKit Cloud | 21 / 148 (146–163) | 21 / 147 (146–147) | 21 (20–21) / 146 | 21 (21–34) / 146 (142–147) | 120 / 245 (241–245) |

### Listeners, opus-2.5

Listeners with audio, of those started in the region:

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, LL-HLS, reader as shipped (popus) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 19/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Cloudflare Realtime | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 23/25, 23/25, 24/25 |
| Cloudflare Stream WHIP/WHEP | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 23/25 |
| LiveKit, SFU in each region | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit, multi-region set | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit Cloud | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 |

Unit latency, median listener's median (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 4.3 | 4.3 (4.2–4.4) | 129.6 (129.6–130.0) | 4.7 | 35.1 (35.1–35.3) | 110.5 (110.4–110.5) | 130.0 (129.9–130.4) |
| Needletail, LL-HLS, reader as shipped (popus) | 4.5 (4.4–4.5) | 4.6 | 129.8 (129.8–130.2) | 14566.2 (14365.9–15338.9) | 11968.3 (11312.8–12628.0) | 15767.7 (14759.4–16540.6) | 10351.9 (10105.3–12235.0) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 4.7 | 4.8 | 130.0 (129.9–130.4) | 8962.8 (8803.5–8978.3) | 6655.4 (6392.7–8959.3) | 11083.8 (11022.1–12037.2) | 6986.3 (6082.7–7971.6) |
| Cloudflare Realtime | 7.1 (7.0–8.2) | 7.2 (6.8–8.3) | 148.5 (147.2–148.9) | 8.0 (7.7–8.8) | 42.3 (41.8–43.0) | 116.5 (116.3–118.0) | 149.0 (132.4–152.1) |
| Cloudflare Stream WHIP/WHEP | 7.3 (7.0–7.4) | 8.1 (8.0–8.1) | 147.6 (130.1–165.7) | 8.2 (7.9–9.4) | 42.3 (42.1–43.4) | 116.8 (116.5–117.8) | 148.1 (130.2–148.6) |
| LiveKit, SFU in each region | 3.8 | 3.8 | 128.9 (128.9–129.6) | 4.4 | 40.0 (40.0–40.2) | 113.0 (109.5–113.2) | 129.5 (129.4–130.1) |
| LiveKit, multi-region set | 3.8 | 3.9 | 129.5 (128.6–129.8) | 5.2 (5.2–5.3) | 35.9 (35.8–40.3) | 111.2 (111.1–113.5) | 130.4 (130.3–130.7) |
| LiveKit Cloud | 17.8 (16.6–18.9) | 17.4 (17.4–19.0) | 142.2 (141.9–142.6) | 17.7 (17.4–19.0) | 59.3 (59.2–60.0) | 134.6 (132.8–134.8) | 142.5 (142.4–142.7) |

Unit latency, worst listener's p99 (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 | 5 (5–8) | 131 (131–134) | 11 | 41 | 117 (117–118) | 136 |
| Needletail, LL-HLS, reader as shipped (popus) | 6 | 7 | 134 (132–134) | 19935 (19671–21297) | 16006 (15165–17447) | 21961 (19823–22198) | 14982 (14641–17087) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 6 | 8 (8–10) | 134 (133–135) | 16244 (15649–16829) | 13080 (12658–15059) | 20260 (17777–21400) | 13577 (12374–13913) |
| Cloudflare Realtime | 11 (9–11) | 9 (8–11) | 150 (149–156) | 17 (14–17) | 50 (49–55) | 130 (129–132) | 156 (153–175) |
| Cloudflare Stream WHIP/WHEP | 9 (9–10) | 11 | 152 (133–168) | 15 (14–16) | 50 (48–56) | 132 (127–184) | 159 (136–172) |
| LiveKit, SFU in each region | 4 | 5 | 130 (129–131) | 9 | 44 | 118 (115–119) | 134 |
| LiveKit, multi-region set | 4 | 5 | 130 (130–131) | 44 (41–48) | 80 (76–83) | 152 (148–157) | 169 (167–174) |
| LiveKit Cloud | 19 (19–22) | 19 (19–22) | 145 | 23 (23–24) | 64 (64–65) | 140 (138–140) | 147 |

Units missing (%):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Needletail, LL-HLS, reader as shipped (popus) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (popus) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 (0.00–0.05) |
| Cloudflare Realtime | 0.00 (0.00–0.02) | 0.01 (0.00–0.04) | 0.00 (0.00–0.01) | 0.00 | 0.00 | 0.01 (0.00–0.04) | 0.00 |
| Cloudflare Stream WHIP/WHEP | 0.03 (0.00–0.08) | 0.00 (0.00–0.06) | 0.00 (0.00–0.06) | 0.02 (0.01–0.15) | 0.02 (0.00–0.13) | 0.11 (0.02–0.17) | 0.02 (0.00–0.13) |
| LiveKit, SFU in each region | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 (0.00–0.03) | 0.00 |
| LiveKit, multi-region set | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 (0.00–0.04) | 0.00 |
| LiveKit Cloud | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 (0.00–0.04) | 0.00 |

### Listeners, pcm

Listeners with audio, of those started in the region:

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, LL-HLS, reader as shipped (pcm) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 11/25, 11/25, 14/25 | 14/25, 13/25, 13/25 | 12/25, 12/25, 13/25 | 12/25, 11/25, 12/25 |
| Cloudflare Realtime | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit, SFU in each region | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit, multi-region set | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit Cloud | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 |

Unit latency, median listener's median (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1.9 | 2.0 (2.0–2.1) | 127.2 (127.2–127.6) | 3504.3 (2475.1–3712.6) | 34.4 (34.2–34.6) | 385.6 (112.2–1777.4) | 130.8 (128.0–256.1) |
| Needletail, LL-HLS, reader as shipped (pcm) | 2.0 (1.9–2.1) | 2.2 (2.2–2.3) | 127.4 (127.4–127.8) | 24431.3 (24088.4–25037.7) | 23337.2 (22955.4–24359.4) | 24122.8 (23545.1–24772.4) | 22696.0 (21623.4–22820.8) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 2.2 (2.2–2.3) | 2.5 (2.5–2.7) | 127.6 (127.5–128.1) | 30918.0 (27861.6–32900.8) | 26535.4 (25416.7–27287.1) | 28650.3 (28329.2–29311.9) | 20230.6 (19992.8–21351.4) |
| Cloudflare Realtime | 4.8 (4.5–4.8) | 4.6 (4.4–4.7) | 152.9 (145.1–5044.1) | 5.4 (5.4–6.3) | 40.0 (39.4–40.6) | 117.5 (114.0–1435.3) | 5206.3 (129.1–5908.2) |
| LiveKit, SFU in each region | 1.5 | 1.6 (1.6–1.7) | 127.2 (127.2–128.0) | 3.4 (3.4–3.6) | 38.5 (33.5–39.0) | 112.2 (111.5–112.4) | 128.8 (128.6–129.6) |
| LiveKit, multi-region set | 1.5 | 1.8 | 127.4 (126.8–127.6) | 6192.0 (3710.0–6206.1) | 3896.9 (784.1–5302.1) | 4204.9 (1773.1–5400.5) | 3862.2 (1716.5–5145.2) |
| LiveKit Cloud | 14.8 (14.8–15.2) | 15.4 (15.1–15.6) | 140.2 (136.5–140.6) | 16.1 (16.1–16.2) | 57.8 (57.4–58.7) | 131.6 (131.3–134.0) | 140.9 (137.0–141.4) |

Unit latency, worst listener's p99 (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 4 (3–5) | 5 (5–6) | 131 (129–131) | 5003 (4758–5550) | 65 (65–242) | 2862 (2571–16532) | 2852 (2809–3654) |
| Needletail, LL-HLS, reader as shipped (pcm) | 8 (8–9) | 12 (12–14) | 137 (136–144) | 34569 (34509–34610) | 33611 (31888–33666) | 33857 (33775–34716) | 31870 (31299–33555) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 11 | 20 (18–22) | 140 (139–142) | 37082 (36909–37660) | 35477 (35207–36328) | 36916 (36631–37986) | 32523 (30747–36277) |
| Cloudflare Realtime | 7 (6–7) | 7 | 164 (164–11082) | 30 (27–31) | 59 (56–59) | 6418 (157–6481) | 9739 (178–11075) |
| LiveKit, SFU in each region | 2 | 3 | 128 | 32 (28–34) | 58 (51–61) | 143 (142–143) | 149 (149–151) |
| LiveKit, multi-region set | 2 | 7 (6–12) | 131 (131–132) | 22530 (13568–23109) | 8855 (3604–10984) | 11430 (9040–22169) | 14108 (13621–22437) |
| LiveKit Cloud | 17 | 18 | 143 (139–143) | 43 (40–43) | 74 (73–80) | 159 (156–165) | 159 (158–160) |

Units missing (%):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.00 | 0.00 | 0.00 | 70.82 (38.14–75.69) | 0.00 | 6.59 (2.09–55.86) | 6.91 (0.74–17.46) |
| Needletail, LL-HLS, reader as shipped (pcm) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Cloudflare Realtime | 0.00 (0.00–0.04) | 0.02 (0.02–0.03) | 0.17 (0.04–2.14) | 0.02 | 0.01 (0.01–0.03) | 0.02 (0.01–0.05) | 0.03 (0.02–9.38) |
| LiveKit, SFU in each region | 0.00 | 0.00 | 0.00 | 0.03 (0.01–0.04) | 0.00 (0.00–0.02) | 0.01 (0.00–0.04) | 0.03 |
| LiveKit, multi-region set | 0.00 | 0.02 (0.00–0.03) | 0.03 (0.01–0.06) | 10.58 (0.01–19.82) | 0.03 (0.00–0.06) | 0.05 (0.01–1.68) | 0.02 (0.01–3.50) |
| LiveKit Cloud | 0.00 | 0.00 | 0.00 | 0.00 (0.00–0.01) | 0.00 | 0.00 | 0.00 |

### Processor of the nodes, opus-2.5

Percent of one vCPU used by the service, during the step:

| Node | 1 per host | 10 | 100, first pass | 100, second pass |
| --- | ---: | ---: | ---: | ---: |
| Needletail contributor | 31 (30–31) | 31 (30–31) | 31 (30–32) | 31 (30–32) |
| Needletail backbone relay | 18 | 18 | 18 | 18 |
| Needletail distributor, London | 13 | 14 | 14 | 15 (14–15) |
| Needletail edge 1, London | 16 (12–27) | 24 (1–24) | 104 (1–105) | 45 (45–89) |
| Needletail edge 2, London | 22 (8–29) | 1 (1–25) | 1 (1–102) | 89 (56–89) |
| Needletail edge 1, Sydney | 17 (16–20) | 14 (13–19) | 58 (55–113) | 67 (36–85) |
| LiveKit, London node (both servers) | 17 | 12 (12–13) | 32 | 100 (99–102) |
| LiveKit, Sydney node (both servers) | 4 | 5 | 22 | 3 |

### Processor of the nodes, pcm

Percent of one vCPU used by the service, during the step:

| Node | 1 per host | 10 | 100, first pass | 100, second pass |
| --- | ---: | ---: | ---: | ---: |
| Needletail contributor | 81 | 81 (80–83) | 81 | 82 |
| Needletail backbone relay | 57 (52–60) | 58 (55–58) | 57 (56–58) | 58 (57–58) |
| Needletail distributor, London | 29 (28–30) | 29 (27–32) | 29 (28–30) | 31 (30–34) |
| Needletail edge 1, London | 106 (2–164) | 66 (1–70) | 144 (1–147) | 151 (148–165) |
| Needletail edge 2, London | 115 (2–176) | 1 (1–72) | 1 (1–145) | 166 (161–167) |
| Needletail edge 1, Sydney | 122 (33–135) | 32 (1–32) | 95 (1–107) | 102 (52–126) |
| LiveKit, London node (both servers) | 92 (90–92) | 30 | 105 (101–107) | 198 (192–198) |
| LiveKit, Sydney node (both servers) | 16 | 15 | 86 (86–88) | 6 |
