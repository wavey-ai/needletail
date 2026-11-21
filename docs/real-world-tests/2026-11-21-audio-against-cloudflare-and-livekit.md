# 21 November 2026: audio against Cloudflare and LiveKit

The owner put audio first: Opus for the lowest latency, PCM for lossless
latency. This record compares Needletail with Cloudflare Realtime, Cloudflare
Stream WHIP/WHEP, self-hosted LiveKit and LiveKit Cloud. Every system carried
the same audio from the same host in London to the same listeners in London,
New York, Tokyo and Sydney, at the same time.

The headline results are from a lab of dedicated-CPU instances only (no
shared instance anywhere), on the Needletail build that has both of its
client modes: RaptorQ FEC over UDP from the edge, and LL-HLS over HTTP. A
first pass on shared Nanodes, with LL-HLS only, is superseded; it is
summarised at the end as a side note.

The 4K video comparison is a separate record.

## Answer

**Self-hosted LiveKit is still the fastest at the median: ahead of
Needletail by 2.6 to 2.8 ms for lossless PCM (5.1 ms in Tokyo) and by 6.3 to
6.4 ms for Opus, against its faster setup in each region. For PCM,
Needletail's UDP client mode comes next, ahead of Cloudflare Realtime and
LiveKit Cloud in every region. For Opus it is level with Cloudflare in New
York and Tokyo, behind it in London and Sydney, and ahead of LiveKit Cloud
everywhere. Needletail is the only system that carries lossless audio as
audio, and its UDP mode the only lane that lost nothing to random loss of 1%
or 5%, at no cost in latency.**

Mouth to ear is the age of a sample when a listener that plays audio on
arrival would play it. Median of three sessions, in milliseconds:

| Lane | System | Frame | London | New York | Tokyo | Sydney |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Opus | LiveKit, self-hosted, SFU in each region | 2.5 ms | **2.3** | 38.4 | **110.0** | **128.3** |
| Opus | LiveKit, self-hosted, multi-region set | 2.5 ms | 2.4 | **33.3** | 111.6 | 128.4 |
| Opus | Cloudflare Realtime | 2.5 ms | 5.6 | 40.7 | 116.8 | 131.0 |
| Opus | Cloudflare Stream WHIP/WHEP | 2.5 ms | 6.5 | 40.8 | 115.6 | 129.4 |
| Opus | **Needletail, RaptorQ FEC over UDP** | 5 ms | 8.6 | 39.6 | 116.4 | 134.7 |
| Opus | Needletail, LL-HLS over HTTP | 5 ms | 9.2 | 40.1 | 117.1 | 135.5 |
| Opus | LiveKit Cloud | 2.5 ms | 15.7 | 57.6 | 133.2 | 141.1 |
| PCM | LiveKit, self-hosted, data channel | 2.5 ms | **2.8** | **33.4** | 110.4 | **128.5** |
| PCM | LiveKit, multi-region set, data channel | 2.5 ms | 2.9 | 33.4 | **107.7** | 128.7 |
| PCM | **Needletail, RaptorQ FEC over UDP** | 5 ms | 5.4 | 36.1 | 112.8 | 131.3 |
| PCM | Needletail, LL-HLS over HTTP | 5 ms | 6.1 | 36.8 | 113.5 | 132.0 |
| PCM | Cloudflare Realtime, data channel | 2.5 ms | 6.6 | 40.5 | 115.4 | 148.1 |
| PCM | LiveKit Cloud, data channel | 2.5 ms | 16.0 | 58.1 | 131.6 | 141.7 |

At the 99th percentile the gap to self-hosted LiveKit is wider: Needletail
UDP was 13.2, 44.1, 122.6 and 139.3 ms for Opus against LiveKit's 4.2, 40.3,
111.8 and 130.1; and 8.7, 39.6, 116.4 and 135.6 ms for PCM against 5.0, 35.5,
112.5 and 131.1.

Where Needletail loses:

- **Latency, against self-hosted LiveKit.** Against LiveKit's faster setup
  in each region, Needletail's UDP mode is 2.6 to 2.8 ms behind for PCM
  (5.1 ms in Tokyo, where the multi-region set was faster) and 6.3 to 6.4 ms
  behind for Opus. Its frame is 5 ms where the others forward 2.5 ms, which
  is 1.25 ms of the gap at the median and 2.5 ms at the top. The rest is the
  path: contributor, relay, distributor and edge, against one SFU.
  Needletail's Opus is 3.1 to 3.6 ms later than its PCM over the same path:
  1.0 to 1.2 ms of it lies between the two producers (FLAC from the DAW
  source against PCM from the AEP1 sender), about 2.3 ms between Opus and
  FLAC from the same DAW source. Neither step was isolated.
- **Opus, against Cloudflare.** 2.1 to 3.0 ms behind Cloudflare in London and
  3.7 to 5.3 ms behind in Sydney.
- **Jitter.** The 99th percentile of Needletail's unit latency is 3.5 to
  3.8 ms above its median for Opus, 2.5 to 3.6 ms for PCM. Self-hosted
  LiveKit's (an SFU in each region) is 1.2 to 2.0 ms: 2.0 to 2.6 ms less for
  Opus, 0.7 to 1.9 ms less for PCM.
- **Time to first audio, at a cold edge.** A Needletail edge holds a stream
  only while someone reads it. The first listener at an edge that does not
  hold the stream waited about 0.9 to 2.0 seconds; at an edge that holds it,
  13 to about 280 ms. Self-hosted LiveKit with an SFU in the region: 20 to
  52 ms for PCM and 157 to 178 ms for Opus. Cloudflare (0.75 to 6 s), the
  LiveKit multi-region set (1.2 to 4.9 s outside London) and LiveKit Cloud
  (0.25 to 2 s) were mostly as slow as a cold Needletail edge or slower.
- **Lossless crowds at one edge pair.** With 25 UDP listeners and 25 HTTP/3
  listeners of PCM in each region at once, the UDP listeners in London were
  about a second late in the two sessions of three in which one London edge
  reached 171% and 174% of one vCPU (86% and 88% of its two), losing up to
  5.3% of the parts; in Sydney one session lost 12.6%. The same crowd of
  Opus was served with nothing lost (unit latency 12, 43, 119 and 138 ms
  with the DAW source's buffer, about 5 ms less without it).
  Self-hosted LiveKit with an SFU in each region served 100 PCM listeners at
  about the latency of one; its multi-region set was 7.7 to 7.9 s late.
- **Burst loss on the UDP mode.** With Gilbert-Elliott bursts (4.8% lost,
  five packets at a time on average), the UDP mode lost 0.15% to 0.30% of the
  PCM parts and 0.4% to 0.7% of the Opus parts, in gaps of 5 to 10 ms. The
  SFUs lost about 5%. LL-HLS lost nothing in all but one run, late.
- **A browser.** Chrome plays every WebRTC Opus lane (72 to 91 ms mouth to
  ear in London, 188 to 229 ms in Sydney). No browser plays Needletail's Opus
  or PCM parts without a player written for them; nor the SFUs' PCM data
  channels.

Where Needletail wins:

- **Lossless audio.** Needletail delivered 24-bit PCM bit for bit, and every
  FLAC part, as audio, in both client modes. Cloudflare Realtime, Cloudflare Stream and
  LiveKit (self-hosted and Cloud) refused an uncompressed audio track
  (`L16` and `L24`), again in this pass. Cloudflare Realtime and LiveKit
  carry PCM bit for bit only on a data channel, to a client written for it,
  and lose what the network loses.
- **Random loss, with no cost in latency.** With 1% and 5% loss at the
  listener, the UDP mode delivered every part and its 99th percentile did not
  move: Opus 13 and 139 ms in London and Sydney at 0%, 1% and 5% loss. Every
  SFU lost about 1% and 5%, and Chrome concealed 1.1% to 18.5% of the
  samples.
- **Cloudflare and LiveKit Cloud.** For PCM, Needletail's UDP mode is ahead
  of Cloudflare Realtime at the median in every region: 1.2 to 4.4 ms in
  London, New York and Tokyo, 17 ms in Sydney (not consistent there:
  Cloudflare was 148 ms in one session, 3.3 s in another, 1.4 ms ahead in the
  third). It is ahead of LiveKit Cloud by 10 to 22 ms. For Opus it is level
  with Cloudflare in New York and Tokyo and 6 to 18 ms ahead of LiveKit
  Cloud.
- **The product reader in a crowd.** `needletail-reader` itself (with its
  1-second buffer), 25 in each region, received every part, with no underrun
  and at most 93 ms behind, in every session.
- **The contributor.** On its own g6-dedicated-4 the contributor published
  Opus, FLAC and PCM at once from two producers at 61% of one vCPU, with no
  publication backlog in two hours.

What Needletail should fix to win, in order of what it buys:

1. **2.5 ms Opus parts and smaller PCM blocks by default.** Worth 1.25 ms at
   the median and 2.5 ms at the top. In progress on `low-latency`.
2. **The hop path and its jitter.** About 1.2 to 1.5 ms of PCM and 5 ms of
   Opus at the median remain after the frame, and 0.7 to 2.6 ms more jitter
   than LiveKit. The per-hop measurement in the 4K record places them.
3. **A cold edge.** Subscribe an edge to a stream before its first listener,
   or answer the first listener from the distributor while the edge fills.
4. **UDP sending at an edge.** With 25 PCM UDP listeners and 25 HTTP/3
   listeners in a region, one g6-dedicated-2 edge reached 88% of its
   processor and its UDP listeners fell about a second behind. Batch sends
   (`sendmmsg`, GSO) and encode each part once for all subscribers.
5. **Bursts.** Spread repair across parts, or let the client ask for a part
   again, so that a burst of five packets does not lose a part.
6. **The LL-HLS reader.** One request for each 5 ms part, as
   `needletail-reader` asks, falls behind with any loss or delay (15 to 22 s
   at 5% loss in ntcmp's emulation; the product reader itself kept up in a
   clean crowd but was not run under impairment). Ship the pipelined HTTP/3
   reader with a window longer than the round trip.
7. A browser player for the audio lanes (WebTransport carrying the UDP
   mode's datagrams).

## What was compared

| System | How the audio went in | How a listener got it |
| --- | --- | --- |
| Needletail | Its own producers on `lon-src`: the DAW Nexus test source for SoundKit Opus and FLAC, and the AEP1 sender for 24-bit PCM, to the contributor in London. All three renditions at once. | Two client modes, from the edge of the listener's region, in 5 ms parts of stream 3001 (Opus), 2001 (FLAC) or 4001 (PCM): **RaptorQ FEC over UDP** (`needletail-udp-client`: each part one RaptorQ object), and **LL-HLS over HTTP** (three readers, below). |
| Cloudflare Realtime | One Opus track to the SFU through its HTTPS API. For PCM, one data channel. | Pull of the track, or of the data channel, through the same API. |
| Cloudflare Stream WHIP/WHEP | One Opus track by WHIP. | WHEP. It has no data channel. |
| LiveKit, SFU in each region | `livekit-server` 1.13.11 on one g6-dedicated-2 in each region, no Redis. The source publishes to all four. | The SFU of the listener's region. |
| LiveKit, multi-region set | The same four nodes as one set on one Redis (its own g6-dedicated-2), with the region-aware node selector. The source publishes through the London node. | Signal through the node of the listener's region. The room lives on the London node, so the media comes from London. |
| LiveKit Cloud | The owner's project. | LiveKit Cloud chose the edge: Germany for London, US East, Japan and Australia. |

Cloudflare Stream Live with RTMPS in and HLS or LL-HLS out is not here. It
carries AAC, not Opus or PCM, and the account cannot use it at all: see
[Not measured](#not-measured).

Needletail is `av-mesh` `c5d75c8` (the merge of `low-latency` into
`client-udp`, pushed at 19:08) with one measurement option on
`compare-low-latency` (`9c34c98`: the UDP client writes the arrival of each
part), `av-contrib` `7a41cea`, `playlists` `a225609`, built with cargo 1.100.0 on a Debian 13 g6-dedicated-8. `daw-nexus`
`qualification-source-loop` `49cfceb`. The deploy tool and the harness are on
`needletail` `compare-cloudflare`. The Nanode sessions of the first pass ran
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

A live sender cannot send a unit before its last sample exists. Every sender
here sends a unit at the moment its last sample has been spoken, and not
before: an Opus packet of 2.5 ms leaves 2.5 ms after its first sample, less
the look-ahead that the encoder already had. So frame length costs what it
costs in a live system.

### Units and what a listener records

A unit is one Opus packet, one block of PCM, or one Needletail part. The
listener (`ntcmp sub`, `ntcmp hls`) records the arrival time of every unit and
which unit it is.

- An Opus packet or a PCM block is recognised by a hash of all its bytes. A
  recognised unit is bit for bit the unit that was sent. This is what
  "bit-exact" means in the tables.
- A Needletail Opus or FLAC part is recognised by its part number. The DAW
  source encodes them, so their bytes are not known in advance. The mapping
  was checked once by decoding: the parts of a reader were decoded and the
  marker was found 119 samples after a whole second for Opus (the 2.5 ms
  look-ahead of the encoder) and 1 sample after it for FLAC
  (`scripts/compare/validate/`).

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

### Needletail's DAW buffer

Needletail's Opus and FLAC come from its DAW Nexus test source. It hands audio
to the plug-in in buffers of 512 samples, as a DAW does, and delivers a buffer
when it is complete. A sample therefore waits up to 10.7 ms (5.3 ms at the
median) before Needletail has it. The other systems, and Needletail's PCM
sender, are fed without such a buffer.

The tables give Needletail's Opus and FLAC **without** that wait: the wait of
each part is known exactly from its position and is taken off. The evidence
file has the measured values as `raw`. A producer inside a DAW pays the buffer
with every system.

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

A Needletail audio part is 5 ms. `needletail-reader` and the LL-HLS reader of
`aep1-48k-probe` ask for one part, wait for it, and ask for the next. That
works while a round trip to the edge is shorter than a part. The listeners of
this lab are in the same data centre as their edge (about 0.5 ms).

A Needletail edge holds a request for a part that is not published yet and
answers the moment it is. That was checked up to 600 parts ahead (3 seconds).
A reader can therefore keep requests in flight. Three readers were measured:

| Reader | What it does |
| --- | --- |
| as shipped | One request for each part, HTTP/2: ntcmp's emulation of `needletail-reader`'s requests. |
| pipelined, HTTP/2 | 40 requests in flight (200 ms of parts), one TCP connection. |
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
the node samplers kept steal at or below 0.2% except in 14 node-steps, the
highest 4% on a Sydney edge for one step.

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

| Session | Start (UTC, 21 November) | Configurations |
| --- | --- | --- |
| 1 | 19:44 | every frame length, lossless attempts, impairment and crowds for 2.5 ms Opus and PCM |
| 2 | 20:25 | 2.5 ms Opus, FLAC beside 20 ms Opus, PCM; impairment and crowds |
| 3 | 20:56 | the same as session 2 |

A cell in a table is the median of the sessions, with the lowest and the highest
session in brackets when they differ.

## Settings

| System | Setting | Value |
| --- | --- | --- |
| All WebRTC lanes | Opus | libopus through ffmpeg 7.1.5, stereo, 256 kbit/s constant, `-application lowdelay` (CELT only, 2.5 ms look-ahead), frames of 2.5, 5, 10 and 20 ms. `opus-max510`: 510 kbit/s, 10 ms, `-application audio` (6.5 ms look-ahead). |
| All WebRTC lanes | Sender | Pion, pre-encoded packets, one RTP packet for each Opus packet, no DTX, no RED, no in-band FEC. |
| All WebRTC lanes | Probe | Pion with NACK, receiver reports and transport-wide congestion control feedback. No jitter buffer. |
| Cloudflare | Client | Pion `webrtc` v4.2.27. STUN `stun.cloudflare.com:3478`. |
| LiveKit | Client | LiveKit `server-sdk-go` v2.20.0 on LiveKit's fork of Pion (v4.2.25-warp.3), `livekit-client` 2.25.0 in Chrome. |
| LiveKit, self-hosted | `livekit-server` | 1.13.11. UDP 50000 to 54999 (55000 to 59999 for the multi-region set), `node_ip` the public address, TURN off, defaults otherwise. Multi-region: Redis 8.0.2 in London, `node_selector.kind: regionaware`. |
| Needletail | Mesh | As the regional cache record of 19 November: `part_ms` 5, `mesh_sync_interval_ms` 2, `mesh_symbol_size` 900 up to 16,000, `cache_mesh_replication` `demand`, `telemetry_interval_ms` 500. `playback_address` `public`: listeners reach the edges over the public address, as they reach the other systems. |
| Needletail | Contributor | `daw_hls_packaging` `opaque`, `daw_hls_formats` `flac,opus,pcm`: all three renditions at once, from both producers (see [The contributor](#the-contributor)). |
| Needletail | UDP client | `client_udp_port` 19446 on every distributor and edge; client defaults: repair adapted to the measured loss, a move after 1 s of silence. |
| Needletail | Opus | The DAW source's SoundKit Opus: 5 ms frames, 60-byte packets (96 kbit/s) with a 20-byte SoundKit header. |
| Chrome | Listener | Headless Chrome 158.0.8127.36, `jitterBufferTarget = 0`. |

## Results

Each cell is the median of the three sessions. The lowest and highest
session are in brackets when they differ. All tables are made from the
evidence file by `scripts/compare/compare-audio-report.py`, and they follow
under [Tables](#tables).

### Opus

Every WebRTC system forwarded Opus frames of 2.5, 5, 10 and 20 ms unchanged:
every packet at every listener was bit for bit the packet sent. None of them
forces a frame length on a native sender. Chrome cannot send a frame shorter
than 10 ms, so a browser source is 3.75 ms worse than these figures at the
median and 7.5 ms worse at the top. Every packet that arrived was bit for bit
the packet sent (the 5, 10 and 20 ms frames ran in session 1 only;
Cloudflare Realtime's 5 ms session in Sydney failed to start). Needletail's
Opus frame here is 5 ms, the DAW source's setting, not the owner's default of
2.5 ms, which waits for the producer of `low-latency`; against 2.5 ms it
adds about 1.25 ms at the median and 2.5 ms at the top. (For the 20 ms and
510 kbit/s columns of the frame-length table, Needletail's lane is FLAC.)

Self-hosted LiveKit is the floor. In London the packet is at the listener
about 1 ms after its last sample. The single-region and multi-region
deployments are the same to within 2 ms, except in New York, where the
multi-region set's media, relayed from London, was 5 ms earlier than the New
York SFU's own copy from the source.

Cloudflare Realtime and Stream WHIP/WHEP are within 0.1 to 1.6 ms of each
other. LiveKit Cloud is the slowest in every region; it served
the London listener from Germany.

Needletail's two client modes have nearly the same median: the UDP mode is
0.5 to 0.8 ms earlier than LL-HLS. Its jitter is lower too, and it does not
change under loss.

In Chrome, mouth to ear is 72 to 91 ms in London, 97 to 128 ms in New York,
175 to 203 ms in Tokyo and 188 to 229 ms in Sydney. The jitter buffer holds
11 to 29 ms of it.

### Lossless

Each system was asked again for an audio track of `audio/L16` and of
`audio/L24`, 48 kHz, stereo:

| System | Answer |
| --- | --- |
| Cloudflare Realtime | `unable to start track, codec is not supported by remote` |
| Cloudflare Stream WHIP | `unable to start track, codec is not supported by remote` |
| LiveKit 1.13.11, self-hosted | The track is published in signalling, then no codec is negotiated (`could not set remote description: unable to start track, codec is not supported by remote`) |
| LiveKit Cloud | The same as self-hosted |

Their documentation agrees: Cloudflare lists Opus and G.711, and LiveKit
registers Opus, RED, PCMU and PCMA. None of them carries lossless PCM as
audio. On a data channel, what arrived of the PCM through Cloudflare Realtime
and LiveKit was bit for bit, and what the network lost was gone. Without
impairment, Cloudflare Realtime's PCM was 17 ms later than its Opus in Sydney
(3.3 s in one session), and LiveKit Cloud lost 3.7% of it in Tokyo in one
session. Cloudflare Stream has no data channel.

Needletail's PCM was bit-exact at every listener, in both client modes, and
every FLAC part arrived (FLAC parts are recognised by number, so their bytes
were not compared). FLAC was about 1 ms later than PCM at the median (it comes from the DAW
source, PCM from the AEP1 sender).

### Impairment

With random loss the UDP mode recovers every part from the repair datagrams
that travel with it, so nothing is late: the 99th percentile was the same at
0%, 1% and 5% loss. Its client measures the datagram loss and its edge sizes
the repair to it. It pays in packets: in the client's own report (not in the
evidence file), a London listener of 5 ms Opus received about four datagrams
for each part, one source and three repair.

With bursts, five packets at a time on average, the repair of one part was
sometimes not enough: 0.15% to 0.30% of PCM parts and 0.4% to 0.7% of Opus
parts were lost, in gaps of 5 to 10 ms.

An SFU sends each packet once. With 1% loss about 1% of the audio is gone,
with 5% about 5%, with bursts about 5%, the longest gaps 7.5 to 15 ms at the
median (up to 52.5 ms in one session). What arrives is mostly on time:
LiveKit's Opus 99th percentile did not move, Cloudflare Stream's in Sydney
rose by 19 to 23 ms, and single data-channel PCM sessions through Cloudflare
and LiveKit had a 99th percentile of 3 to 10 seconds with bursts. Chrome
concealed about 4% and 18% of the samples from Cloudflare at 1% and 5% loss,
and only 1.1% to 3.5% from LiveKit, which is consistent with
LiveKit answering Chrome's requests to retransmit audio (not checked in the
server).

Needletail's LL-HLS mode delivered every part in all but three of its
impaired runs (0.64% in 50 ms gaps once with bursts on the pipelined HTTP/2
reader, up to 0.08% twice on HTTP/3), and pays in time:

- the one-request-for-each-part reader falls behind: a 99th percentile of
  0.4 to 0.9 seconds at 1% loss (1.6 s in one session) and 15 to 22 seconds
  at 5%;
- pipelined over HTTP/3, the 99th percentile at 5% loss was 21 and 148 ms
  in London and Sydney for Opus, 49 and 167 ms for PCM.

With 100 ms of delay each way, every SFU and the UDP mode were 100 ms later.
Apart from LiveKit Cloud's PCM data channel (below), nothing else changed. The LL-HLS readers fell behind: a window of 40
parts in flight is 200 ms of audio, the round trip was 200 ms.

LiveKit Cloud dropped 88% of the PCM blocks on its data channel when the
listener was 100 ms away, in all three sessions, as in the first pass.

### Listeners

One and ten listeners: every system served them all, at about the latency of
one; Cloudflare was 5 to 9 ms later in Sydney with ten.

A listener of a crowd keeps percentiles, not every unit, so the DAW source's
buffer cannot be taken off part by part: in the crowd tables Needletail's
Opus and FLAC figures (both client modes) include it, 5.2 ms at the median.
The one-listener column has it taken off; the difference between that column
and the crowd columns is this buffer, not the crowd.

One hundred listeners, 25 on one host in each region. The UDP listeners ran
together with 25 HTTP/3 listeners of the same stream and with the LiveKit
multi-region set's crowd; the other systems' crowds ran in a pass of their
own. The product's own `needletail-reader` ran as a third crowd pass of 25 in
each region.

Figures are the unit latency of the median listener.

- **Needletail, UDP mode:** Opus at 12, 43, 119 and 138 ms with the DAW
  buffer (about 7, 38, 114 and 133 ms without it), nothing lost.
  PCM in London about a second late in two sessions of three (up to 5.3%
  lost), and 34, 111 and 129 ms elsewhere, with 12.6% lost in Sydney in one
  session. In those sessions one London edge was at 171% and 174% of one
  vCPU (of two), the other below 50%.
- **Needletail, LL-HLS:** ntcmp's one-request-for-each-part reader, in the
  same pass as the Cloudflare and LiveKit crowds, fell 3.6 to 5.2 seconds
  behind with Opus in London and Tokyo and 8 to 14 seconds with PCM.
  `needletail-reader` itself, 25 in each region in a pass of its own,
  received every part, never more than 93 ms behind (its latency was not
  measured). The pipelined HTTP/3 reader held Opus at 21, 50, 136 and 144 ms and fell behind with PCM.
- **LiveKit, an SFU in each region:** all 100, Opus at 1.5, 37.6, 109.1 and
  127.4 ms, PCM at 3.0, 33.6, 110.7 and 128.6 ms.
- **LiveKit, multi-region set:** the room is on the London node. Opus was
  served; PCM was 7.7 to 7.9 seconds late in every region, with the London node at 198% of a vCPU.
- **Cloudflare:** Realtime's Opus at the latency of one, Stream WHIP/WHEP's
  11 ms later in Sydney. One or two of 100 Realtime
  listeners did not connect in two sessions, and two or three of 25 Stream
  WHIP/WHEP listeners in Sydney in every session. PCM to Tokyo and Sydney had a worst listener
  6.8 and 8.7 seconds late in two sessions of three.
- **LiveKit Cloud:** 96 of 96 (the free plan allows 100 connections), at the
  latency of one.

### The contributor

The contributor ran alone on a g6-dedicated-4 (4 dedicated vCPUs) and
published all three renditions at once, from both producers (the DAW source's
Opus and FLAC, the AEP1 sender's PCM), 200 parts a second each. `av-contrib`
used 60% to 62% of one vCPU, 15% of the instance, in every step. Its journal
(`journalctl -u needletail-contrib`, counted by minute; not in the evidence
file) has no `publication queue is full` from the deploy of the build measured
(19:36) to the end of the sessions (21:30). On the earlier
`regional-llhls-cache` build, on the same instance, it logged 359 of them in
one minute (18:48) during a crowd step.
Four vCPUs are more than audio needs; the 4K record says what video needs.

The primary relay used 38% to 39% of one vCPU of its g6-dedicated-2, a London
distributor 19% to 23%, an edge 8% to 48% with one to ten listeners.

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
  ear (LL-HLS) against self-hosted LiveKit's 1.9, 32.6, 109.1 and 127.8; on
  the dedicated lab, with the newer build, it is 9.2, 40.1, 117.1 and 135.5
  over LL-HLS and 8.6, 39.6, 116.4 and 134.7 over UDP, against LiveKit's
  2.3, 38.4, 110.0 and 128.3 (LiveKit's New York route was 5.8 ms slower in
  this lab).
- A contributor on one shared vCPU could publish one audio rendition, not
  three.

Its evidence is kept as
[`20261121-compare-audio-nanode.json`](evidence/20261121-compare-audio-nanode.json).

## Not measured

- **Needletail in a browser.** The Opus parts are SoundKit frames and the PCM
  parts are raw samples. A browser needs a player written for them; the UDP
  mode needs WebTransport to carry its datagrams.
- **2.5 ms Opus and smaller PCM blocks on Needletail.** The producers of the
  build measured make 5 ms parts. The owner's default of 2.5 ms Opus, and PCM
  blocks from 1 ms down while they measure faster, need the producer options
  pushed on `low-latency` after these sessions; this record will be rerun on
  them.
- **Sound quality.** Every lane delivered its packets unchanged, so quality
  is the codec's: 256 kbit/s Opus on the SFUs, 96 kbit/s SoundKit Opus on
  Needletail, and no coding loss on the lossless lanes: every PCM unit that
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

- Needletail's Opus is 3.1 to 3.6 ms later than its PCM over the same path
  and client mode: 1.0 to 1.2 ms between the producers (FLAC from the DAW
  source against PCM from the AEP1 sender), about 2.3 ms between Opus and FLAC
  from the same source. Neither step was isolated.
- The UDP listeners of PCM in the London crowd were about a second late in
  the two sessions in which one of the two London edges was near its
  processor limit (171% and 174% of one vCPU, of two); the other edge was
  below 50%. Whether the edge's UDP
  sender or the 25 HTTP/3 readers sharing the edges took the processor was
  not separated; the tier record measures UDP listeners alone.
- The LL-HLS readers need a window longer than the round trip. With 40
  parts in flight and a 200 ms round trip they ran late.
- LiveKit Cloud's loss of 88% of data-channel blocks with a 100 ms delay was
  not investigated.
- Cloudflare Realtime's data channel to Sydney ran seconds late in two
  sessions of three: 3.3 s at the median in session 1's clean step and 8 to
  10 s in its loss and delay steps; 9.3 s in session 3's burst step.

## Evidence and harness

- Evidence: [`20261121-compare-audio.json`](evidence/20261121-compare-audio.json).
  For each of the three dedicated sessions, each step, host and lane: the
  figures of the probe, of Chrome, and the processor of each node. Needletail's
  Opus and FLAC have both the measured values (`raw`) and the values without
  the DAW buffer.
- Raw records (every unit of every listener) are under
  `target/compare/runs/laudio-{1,2,3}/` and are not versioned.
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
scripts/compare/compare-audio.py --session laudio-x --product-reader
scripts/compare/compare-audio-analyse.py --out evidence.json laudio-x
scripts/compare/compare-audio-report.py evidence.json
scripts/compare/cloudflare.py teardown
```

## Tables

### Opus, shortest frame (2.5 ms)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 8.6 (8.5–8.9) | 39.6 (39.5–39.6) | 116.4 (116.2–118.0) | 134.7 (134.4–134.9) |
| Needletail, LL-HLS, reader as shipped (opus) | 5 ms | 9.2 (9.0–9.5) | 40.1 (40.0–40.2) | 117.1 (116.9–118.8) | 135.5 (135.1–135.8) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 5 ms | 9.1 (8.9–9.5) | 40.1 (40.0–40.2) | 117.0 (116.9–118.3) | 135.2 (135.0–135.5) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 5 ms | 9.3 (9.2–9.6) | 40.2 | 117.1 (117.0–118.4) | 135.5 (135.1–135.8) |
| Cloudflare Realtime | 2.5 ms | 5.6 (5.4–7.8) | 40.7 (40.4–41.5) | 116.8 (114.7–117.7) | 131.0 (130.0–136.3) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 6.5 (6.0–6.7) | 40.8 (40.2–41.2) | 115.6 (114.9–116.9) | 129.4 (127.6–166.0) |
| LiveKit, SFU in each region | 2.5 ms | 2.3 (2.3–2.6) | 38.4 (38.3–38.5) | 110.0 (109.9–111.6) | 128.3 (128.2–128.5) |
| LiveKit, multi-region set | 2.5 ms | 2.4 (2.3–2.6) | 33.3 (33.0–38.5) | 111.6 (109.3–111.8) | 128.4 (128.2–128.6) |
| LiveKit Cloud | 2.5 ms | 15.7 (15.7–16.7) | 57.6 (57.4–57.8) | 133.2 (131.2–133.3) | 141.1 (140.8–141.7) |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 11.7 (11.7–12.0) | 42.7 (42.7–42.8) | 119.7 (119.4–121.2) | 137.8 (137.6–138.1) |
| Needletail, LL-HLS, reader as shipped (opus) | 5 ms | 12.4 (12.1–12.7) | 43.4 (43.3–43.5) | 120.5 (120.2–122.0) | 138.7 (138.5–139.3) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 5 ms | 12.4 (12.1–12.6) | 43.3 (43.3–43.5) | 120.3 (120.2–121.5) | 138.7 (138.5–138.7) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 5 ms | 12.5 (12.5–12.8) | 43.5 (43.5–43.6) | 120.5 (120.3–121.7) | 138.7 (138.7–139.3) |
| Cloudflare Realtime | 2.5 ms | 7.2 (7.1–9.8) | 42.3 (42.0–43.3) | 118.5 (116.3–119.3) | 132.7 (131.5–137.8) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 8.3 (7.5–8.6) | 42.6 (41.6–42.7) | 117.4 (116.3–118.5) | 130.8 (129.3–167.3) |
| LiveKit, SFU in each region | 2.5 ms | 3.6 (3.6–3.9) | 39.7 (39.6–39.8) | 111.2 (111.2–112.9) | 129.5 (129.4–129.8) |
| LiveKit, multi-region set | 2.5 ms | 3.7 (3.6–3.9) | 34.5 (34.3–39.8) | 112.9 (110.6–113.1) | 129.6 (129.5–129.9) |
| LiveKit Cloud | 2.5 ms | 17.7 (17.5–18.6) | 59.9 (59.8–60.2) | 135.5 (133.6–135.7) | 143.5 (143.1–144.0) |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 13.2 (12.9–13.3) | 44.1 (44.1–44.2) | 122.6 (120.9–123.6) | 139.3 (139.2–139.7) |
| Needletail, LL-HLS, reader as shipped (opus) | 5 ms | 14.0 (13.5–14.2) | 44.9 | 123.6 (121.7–126.5) | 141.4 (140.7–143.6) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 5 ms | 13.9 (13.6–14.0) | 44.9 (44.8–44.9) | 122.0 (121.6–123.0) | 140.3 (140.0–250.2) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 5 ms | 14.1 (13.9–14.3) | 45.0 (45.0–45.1) | 122.1 (121.8–123.3) | 142.7 (140.3–230.8) |
| Cloudflare Realtime | 2.5 ms | 10.5 (8.4–11.4) | 43.5 (43.4–44.8) | 120.1 (117.6–125.8) | 134.3 (132.9–139.0) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 9.4 (8.5–10.9) | 43.7 (42.6–44.8) | 119.8 (119.7–120.9) | 132.5 (131.8–167.9) |
| LiveKit, SFU in each region | 2.5 ms | 4.2 (4.1–4.5) | 40.3 (40.1–40.3) | 111.8 (111.8–113.4) | 130.1 (130.0–130.3) |
| LiveKit, multi-region set | 2.5 ms | 4.2 (4.1–4.4) | 35.2 (34.9–40.4) | 113.5 (111.2–113.8) | 130.2 (130.0–130.3) |
| LiveKit Cloud | 2.5 ms | 18.3 (18.2–19.4) | 60.8 (60.6–61.1) | 136.4 (134.5–136.6) | 144.4 (144.2–144.9) |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 3.5 (3.4–3.6) | 3.6 | 3.7 (3.6–7.3) | 3.8 (3.6–3.8) |
| Needletail, LL-HLS, reader as shipped (opus) | 5 ms | 3.6 (3.6–4.0) | 3.9 (3.7–4.0) | 4.0 (3.9–8.9) | 5.9 (4.6–7.7) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 5 ms | 3.7 (3.6–4.0) | 3.8 (3.7–4.1) | 3.9 (3.8–4.0) | 3.9 (3.8–115.9) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 5 ms | 3.8 (3.7–4.0) | 4.0 (3.9–4.1) | 4.0 (4.0–4.2) | 6.8 (4.0–95.1) |
| Cloudflare Realtime | 2.5 ms | 3.4 (2.6–5.1) | 2.7 (2.6–3.1) | 3.1 (2.6–7.9) | 2.7 (2.5–3.1) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 2.5 (2.3–4.4) | 2.3 (2.0–3.8) | 4.2 (2.6–5.8) | 2.1 (1.4–4.9) |
| LiveKit, SFU in each region | 2.5 ms | 1.5 (1.4–1.5) | 1.5 (1.4–1.5) | 1.5 | 1.2 (1.2–1.7) |
| LiveKit, multi-region set | 2.5 ms | 1.4 (1.4–1.5) | 1.6 (1.5–1.6) | 1.5 (1.5–1.6) | 1.3 (1.2–1.6) |
| LiveKit Cloud | 2.5 ms | 2.1 (1.9–2.1) | 2.7 (2.6–2.8) | 2.9 (2.8–3.0) | 2.9 (2.7–2.9) |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, reader as shipped (opus) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Cloudflare Realtime | 2.5 ms | 0.000 (0.000–0.118) | 0.000 (0.000–0.120) | 0.005 (0.000–0.119) | 0.000 (0.000–0.117) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 0.010 (0.000–0.015) | 0.000 (0.000–0.010) | 0.000 (0.000–0.010) | 0.005 (0.000–0.011) |
| LiveKit, SFU in each region | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit, multi-region set | 2.5 ms | 0.000 | 0.000 | 0.000 (0.000–0.083) | 0.000 |
| LiveKit Cloud | 2.5 ms | 0.000 | 0.000 | 0.000 (0.000–0.069) | 0.000 (0.000–0.130) |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, reader as shipped (opus) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Realtime | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, SFU in each region | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, multi-region set | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit Cloud | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 1728.5 (96.0–1851.6) | 1945.5 (1903.8–2011.3) | 193.5 (82.1–277.7) | 84.8 (78.7–102.1) |
| Needletail, LL-HLS, reader as shipped (opus) | 5 ms | 1729.2 (86.0–1775.3) | 1885.5 (1012.2–1950.6) | 73.3 (64.5–198.7) | 1540.4 (88.5–1584.3) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 5 ms | 101.0 (87.0–1771.1) | 1893.3 (1008.9–1949.4) | 167.5 (73.9–193.2) | 94.0 (71.5–1643.1) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 5 ms | 1763.7 (1709.5–1838.8) | 1942.9 (1881.1–2003.3) | 165.9 (59.7–189.3) | 1540.0 (1537.3–1643.9) |
| Cloudflare Realtime | 2.5 ms | 858.9 (748.2–884.8) | 1757.6 (1629.8–1788.5) | 2068.9 (1891.7–3568.6) | 2791.4 (2560.4–4209.8) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 1379.9 (1165.9–1426.6) | 2479.0 (2320.6–2506.5) | 4714.7 (3186.4–5071.8) | 5930.9 (5444.2–5991.1) |
| LiveKit, SFU in each region | 2.5 ms | 159.3 (159.3–161.1) | 160.7 (159.0–161.7) | 157.7 (156.9–160.4) | 158.9 (157.8–161.1) |
| LiveKit, multi-region set | 2.5 ms | 163.4 (162.7–168.9) | 1206.6 (1189.8–1243.2) | 3931.5 (3564.7–3933.2) | 4567.1 (4414.4–4666.4) |
| LiveKit Cloud | 2.5 ms | 252.8 (247.0–269.3) | 485.2 (289.7–491.7) | 1193.2 (789.7–1214.2) | 1349.3 (1134.5–1911.2) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 128.0 | 128.0 | 128.0 | 128.0 |
| Needletail, LL-HLS, reader as shipped (opus) | 5 ms | 128.0 | 128.0 | 128.0 | 128.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 5 ms | 128.0 | 128.0 | 128.0 | 128.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 5 ms | 128.0 | 128.0 | 128.0 | 128.0 |
| Cloudflare Realtime | 2.5 ms | 256.0 (255.7–256.0) | 256.0 (255.7–256.0) | 256.0 (255.7–256.0) | 256.0 (255.7–256.0) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 256.0 | 256.0 | 256.0 | 256.0 |
| LiveKit, SFU in each region | 2.5 ms | 256.0 | 256.0 | 256.0 | 256.0 |
| LiveKit, multi-region set | 2.5 ms | 256.0 | 256.0 | 256.0 (255.8–256.0) | 256.0 |
| LiveKit Cloud | 2.5 ms | 256.0 | 256.0 | 256.0 (255.8–256.0) | 256.0 (255.6–256.0) |

Chrome mouth to ear at the marker, median (ms):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 87.0 (72.6–96.5) | 122.2 (109.6–134.4) | 199.9 (180.8–204.6) | 200.9 (196.5–214.2) |
| Cloudflare Stream WHIP/WHEP | 91.0 (69.7–97.0) | 110.0 (109.6–118.3) | 186.7 (178.0–196.8) | 229.3 (193.2–232.8) |
| LiveKit, SFU in each region | 71.9 (66.5–72.2) | 117.7 (113.0–120.6) | 182.1 (169.3–184.1) | 201.3 (192.9–206.2) |
| LiveKit, multi-region set | 77.4 (64.8–78.2) | 97.4 (96.6–105.3) | 174.6 (171.4–174.7) | 187.9 (187.8–189.4) |
| LiveKit Cloud | 86.6 (81.7–86.7) | 128.3 (123.5–128.5) | 202.8 (198.3–204.3) | 202.0 (199.0–215.2) |

Chrome jitter buffer, mean (ms):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 29.0 (16.3–34.5) | 22.4 (18.0–38.9) | 26.4 (14.6–28.9) | 20.0 (16.1–22.2) |
| Cloudflare Stream WHIP/WHEP | 24.0 (13.7–30.3) | 20.4 (16.8–28.6) | 19.1 (13.8–22.2) | 24.2 (13.8–24.5) |
| LiveKit, SFU in each region | 18.5 (12.4–19.1) | 19.8 (19.8–30.2) | 20.8 (12.0–29.6) | 19.8 (14.5–19.8) |
| LiveKit, multi-region set | 19.2 (14.6–19.8) | 12.3 (12.3–12.5) | 12.9 (11.9–17.6) | 10.9 (10.0–12.0) |
| LiveKit Cloud | 19.8 (12.9–19.9) | 16.6 (13.7–16.7) | 18.1 (18.0–21.0) | 13.9 (13.3–21.2) |

Chrome concealed samples (%):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 0.000 (0.000–0.254) | 0.122 (0.000–0.194) | 0.169 (0.000–0.204) | 0.048 (0.000–0.220) |
| Cloudflare Stream WHIP/WHEP | 0.000 (0.000–0.051) | 0.000 | 0.000 | 0.000 |
| LiveKit, SFU in each region | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit, multi-region set | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit Cloud | 0.000 | 0.000 (0.000–0.038) | 0.000 (0.000–0.040) | 0.000 |

### Frame length

Mouth to ear, played on arrival, median (ms), London / Sydney:

| System | 2.5 ms | 5 ms | 10 ms | 20 ms | 10 ms at 510 kbit/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge | 8.6 / 134.7 (uc) | 8.8 / 135.0 (uc) | 8.6 / 135.1 (uc) | 6.5 / 132.3 (uc) | 6.5 / 132.9 (uc) |
| Needletail, LL-HLS, reader as shipped | 9.2 / 135.5 (opus) | 9.2 / 135.6 (opus) | 9.0 / 135.7 (opus) | 7.3 / 133.1 (flac) | 7.3 / 133.8 (flac) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 | 9.1 / 135.2 (opus) | 9.1 / 135.6 (opus) | 9.0 / 135.5 (opus) | 7.2 / 133.1 (flac) | 7.3 / 133.8 (flac) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 | 9.3 / 135.5 (opus) | 9.5 / 135.7 (opus) | 9.3 / 135.6 (opus) | 7.2 / 133.1 (flac) | 7.3 / 133.8 (flac) |
| Cloudflare Realtime | 5.6 / 131.0 | 7.8 / – | 11.0 / 133.7 | 17.7 / 141.9 | 12.7 / 136.5 |
| Cloudflare Stream WHIP/WHEP | 6.5 / 129.4 | 8.0 / 129.8 | 11.3 / 134.9 | 17.1 / 140.7 | 10.1 / 134.5 |
| LiveKit, SFU in each region | 2.3 / 128.3 | 3.7 / 129.9 | 6.8 / 133.2 | 13.1 / 139.0 | 6.8 / 133.2 |
| LiveKit, multi-region set | 2.4 / 128.4 | 3.7 / 130.1 | 6.8 / 133.2 | 13.1 / 138.9 | 6.9 / 133.2 |
| LiveKit Cloud | 15.7 / 141.1 | 16.9 / 142.7 | 20.2 / 145.2 | 25.4 / 149.6 | 19.7 / 141.3 |

### Lossless (PCM; SFUs by data channel)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 5.4 (5.3–5.5) | 36.1 (36.0–36.2) | 112.8 (112.7–113.1) | 131.3 (131.2–131.8) |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 ms | 6.1 (5.9–6.2) | 36.8 (36.7–36.8) | 113.5 (113.4–113.9) | 132.0 (131.9–132.5) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 5 ms | 6.1 (5.7–6.2) | 36.9 (36.8–37.0) | 113.6 (113.4–113.9) | 132.1 (131.9–132.4) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 ms | 6.2 (5.7–6.2) | 36.9 (36.8–37.0) | 113.6 (113.4–113.9) | 132.1 (131.8–132.5) |
| Cloudflare Realtime | 2.5 ms | 6.6 (5.3–6.7) | 40.5 (39.5–41.0) | 115.4 (114.8–116.7) | 148.1 (130.0–3318.8) |
| LiveKit, SFU in each region | 2.5 ms | 2.8 (2.6–2.8) | 33.4 (33.2–38.5) | 110.4 (106.8–112.0) | 128.5 (128.4–129.0) |
| LiveKit, multi-region set | 2.5 ms | 2.9 (2.7–2.9) | 33.4 (33.4–33.5) | 107.7 (107.5–110.2) | 128.7 (128.6–129.2) |
| LiveKit Cloud | 2.5 ms | 16.0 (15.6–17.2) | 58.1 (58.0–58.3) | 131.6 (127.3–133.3) | 141.7 (141.0–141.7) |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 7.7 (7.6–7.8) | 38.3 (38.2–38.4) | 115.0 (115.0–115.3) | 133.8 (133.4–134.1) |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 ms | 8.6 (8.4–8.6) | 39.3 (39.2–39.4) | 116.0 (115.9–116.4) | 134.9 (134.4–135.0) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 5 ms | 8.6 (8.2–8.7) | 39.5 (39.4–39.5) | 116.1 (116.0–116.5) | 135.0 (134.5–135.2) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 ms | 8.7 (8.2–8.8) | 39.5 (39.5–39.6) | 116.2 (116.0–116.5) | 135.0 (134.3–135.2) |
| Cloudflare Realtime | 2.5 ms | 8.7 (6.8–8.7) | 42.2 (40.9–42.8) | 117.2 (116.4–118.2) | 149.5 (132.2–8263.8) |
| LiveKit, SFU in each region | 2.5 ms | 4.1 (4.0–4.2) | 34.7 (34.5–39.8) | 111.7 (108.1–113.3) | 129.9 (129.7–130.3) |
| LiveKit, multi-region set | 2.5 ms | 4.2 (4.1–4.3) | 34.8 (34.8–34.9) | 109.1 (109.0–111.6) | 130.0 (129.9–130.5) |
| LiveKit Cloud | 2.5 ms | 18.1 (17.5–19.0) | 60.6 (60.4–60.6) | 134.3 (134.1–136.7) | 144.0 (143.4–144.1) |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 8.7 (8.7–9.1) | 39.6 (39.3–39.6) | 116.4 (116.0–116.4) | 135.6 (134.6–135.7) |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 ms | 9.7 (9.5–9.9) | 40.6 (40.5–40.9) | 117.4 (117.1–117.7) | 136.3 (136.0–412.6) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 5 ms | 9.8 (9.4–10.0) | 40.9 (40.7–41.1) | 117.5 (117.3–118.1) | 137.4 (136.0–342.8) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 ms | 9.9 (9.6–10.1) | 41.0 (40.8–41.2) | 117.6 (117.6–117.9) | 137.2 (135.6–390.0) |
| Cloudflare Realtime | 2.5 ms | 10.5 (8.1–10.5) | 44.2 (41.7–44.5) | 119.0 (118.0–119.1) | 150.3 (140.0–8386.9) |
| LiveKit, SFU in each region | 2.5 ms | 5.0 (4.9–5.2) | 35.5 (35.4–40.6) | 112.5 (109.0–114.2) | 131.1 (130.5–244.5) |
| LiveKit, multi-region set | 2.5 ms | 5.3 (5.2–5.6) | 36.1 (36.1–36.3) | 110.5 (110.4–113.0) | 131.3 (131.2–132.5) |
| LiveKit Cloud | 2.5 ms | 18.8 (18.4–19.6) | 61.5 (61.4–61.6) | 136.0 (135.0–403.4) | 144.9 (144.4–145.1) |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 2.7 (2.5–3.1) | 2.5 (2.4–3.1) | 2.6 (2.4–3.2) | 3.6 (2.8–4.0) |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 ms | 2.8 (2.8–3.3) | 3.4 (3.0–3.6) | 3.1 (2.8–3.5) | 3.6 (3.3–278.5) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 5 ms | 3.0 (2.7–3.4) | 3.5 (3.2–3.8) | 3.5 (3.0–3.7) | 4.8 (3.5–209.1) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 ms | 3.2 (3.0–3.4) | 3.4 (3.2–4.0) | 3.5 (3.3–3.6) | 4.3 (3.1–255.5) |
| Cloudflare Realtime | 2.5 ms | 3.7 (2.5–3.8) | 3.4 (1.9–3.6) | 2.9 (2.2–3.5) | 10.0 (1.7–5068.3) |
| LiveKit, SFU in each region | 2.5 ms | 2.0 | 1.8 (1.7–1.9) | 1.9 | 1.7 (1.7–116.3) |
| LiveKit, multi-region set | 2.5 ms | 2.2 (2.1–2.5) | 2.4 (2.4–2.5) | 2.5 (2.4–2.6) | 2.5 (2.4–3.1) |
| LiveKit Cloud | 2.5 ms | 2.2 (2.1–2.3) | 2.8 (2.7–2.9) | 8.4 (3.1–269.1) | 2.9 (2.6–3.0) |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Cloudflare Realtime | 2.5 ms | 0.039 (0.000–0.054) | 0.015 (0.000–0.045) | 0.015 (0.005–0.041) | 0.031 (0.019–0.716) |
| LiveKit, SFU in each region | 2.5 ms | 0.010 (0.000–0.010) | 0.000 | 0.005 (0.000–0.005) | 0.005 (0.000–0.048) |
| LiveKit, multi-region set | 2.5 ms | 0.000 | 0.035 (0.030–0.035) | 0.032 (0.021–0.042) | 0.027 (0.016–0.032) |
| LiveKit Cloud | 2.5 ms | 0.000 | 0.000 | 0.000 (0.000–3.728) | 0.035 (0.000–0.411) |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Realtime | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, SFU in each region | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, multi-region set | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit Cloud | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 1389.7 (224.8–1910.9) | 1428.5 (1085.3–1569.5) | 216.9 (96.8–1136.1) | 1206.8 (1061.1–1714.3) |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 ms | 71.7 (71.1–1798.6) | 1270.9 (13.0–1416.9) | 49.1 (32.7–1194.0) | 70.1 (15.4–1518.0) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 5 ms | 86.8 (72.3–101.9) | 1313.4 (956.3–1424.2) | 1014.7 (33.6–1128.3) | 29.6 (27.4–1082.7) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 ms | 84.1 (43.9–1262.9) | 1283.9 (944.3–1405.9) | 992.7 (27.1–1119.5) | 916.1 (80.8–1539.9) |
| Cloudflare Realtime | 2.5 ms | 868.8 (766.9–1001.8) | 1649.8 (1590.5–1679.6) | 2744.5 (2653.2–2776.9) | 3428.1 (3256.1–3717.8) |
| LiveKit, SFU in each region | 2.5 ms | 52.2 (40.8–52.4) | 27.2 (20.0–30.6) | 37.6 (31.0–38.3) | 32.7 (26.1–46.2) |
| LiveKit, multi-region set | 2.5 ms | 55.8 (40.1–75.1) | 1215.0 (1109.1–1262.7) | 4164.6 (3695.1–4331.1) | 4932.6 (4834.0–4939.6) |
| LiveKit Cloud | 2.5 ms | 237.2 (232.2–247.0) | 517.3 (474.3–626.7) | 1761.7 (1283.4–1963.1) | 1910.7 (1462.3–1998.0) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 2304.0 (2303.9–2304.2) | 2304.2 (2303.8–2304.2) | 2304.0 (2303.8–2304.1) | 2304.0 (2303.9–2304.1) |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 ms | 2304.0 (2303.9–2304.1) | 2304.0 (2303.9–2304.5) | 2303.9 (2303.9–2304.1) | 2304.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 5 ms | 2304.0 | 2304.0 | 2304.0 | 2304.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 ms | 2304.1 (2303.9–2304.2) | 2304.0 (2303.9–2304.1) | 2303.9 (2303.9–2304.1) | 2304.0 (2303.9–2304.1) |
| Cloudflare Realtime | 2.5 ms | 2303.1 (2302.7–2304.0) | 2303.7 (2302.9–2304.0) | 2303.7 (2303.0–2303.9) | 2303.2 (2287.4–2500.4) |
| LiveKit, SFU in each region | 2.5 ms | 2303.8 (2303.8–2304.0) | 2304.0 | 2303.9 | 2303.9 (2302.8–2304.0) |
| LiveKit, multi-region set | 2.5 ms | 2304.0 | 2303.3 (2303.2–2303.4) | 2303.2 (2303.1–2303.5) | 2303.4 (2303.3–2303.6) |
| LiveKit Cloud | 2.5 ms | 2304.1 | 2304.0 | 2303.6 (2217.8–2303.9) | 2303.2 (2294.4–2304.1) |

### Needletail FLAC (beside 20 ms Opus on the SFUs)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 6.5 (6.4–6.7) | 37.3 (37.1–37.3) | 114.0 (113.7–115.9) | 132.3 (132.3–132.9) |
| Needletail, LL-HLS, reader as shipped (flac) | 5 ms | 7.3 (7.3–7.4) | 37.9 (37.6–38.0) | 114.7 (114.5–116.4) | 133.1 (132.9–133.9) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 5 ms | 7.2 (7.0–7.2) | 38.0 (37.6–38.0) | 114.7 (114.5–116.4) | 133.1 (133.0–133.8) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 5 ms | 7.2 (7.1–7.3) | 38.0 (37.8–38.1) | 114.8 (114.5–116.6) | 133.1 (133.0–133.8) |
| Cloudflare Realtime | 20 ms | 17.7 (15.9–18.1) | 51.9 (51.4–52.1) | 128.4 (124.0–128.8) | 141.9 (138.2–147.6) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 17.1 (16.6–17.2) | 51.5 (50.6–52.2) | 126.8 (126.3–129.8) | 140.7 (140.2–175.8) |
| LiveKit, SFU in each region | 20 ms | 13.1 (13.1–13.2) | 48.2 (43.8–49.0) | 120.5 (116.9–122.4) | 139.0 (138.9–139.5) |
| LiveKit, multi-region set | 20 ms | 13.1 (13.1–13.3) | 43.9 (43.7–49.2) | 120.6 (118.9–122.1) | 138.9 (138.9–139.4) |
| LiveKit Cloud | 20 ms | 25.4 (25.3–25.8) | 67.2 (67.0–67.3) | 141.8 (140.4–142.2) | 149.6 (146.4–150.4) |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 9.6 (9.5–9.8) | 40.4 (40.1–40.4) | 117.1 (116.7–119.2) | 135.4 (135.3–136.0) |
| Needletail, LL-HLS, reader as shipped (flac) | 5 ms | 10.6 (10.4–10.6) | 41.0 (40.7–41.2) | 117.9 (117.7–119.5) | 136.2 (136.1–137.2) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 5 ms | 10.3 (10.1–10.4) | 41.2 (40.7–41.2) | 117.8 (117.6–119.6) | 136.3 (136.2–137.1) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 5 ms | 10.4 (10.2–10.5) | 41.1 (40.9–41.2) | 118.0 (117.7–119.8) | 136.3 (136.1–137.1) |
| Cloudflare Realtime | 20 ms | 24.0 (22.2–24.6) | 58.2 (57.7–72.8) | 134.7 (132.2–135.1) | 148.3 (144.5–153.9) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 23.8 (23.0–24.2) | 57.9 (57.4–58.5) | 133.1 (133.1–136.2) | 147.0 (147.0–182.1) |
| LiveKit, SFU in each region | 20 ms | 19.1 (19.0–19.2) | 54.3 (49.9–55.1) | 126.5 (122.9–128.4) | 145.0 (145.0–145.5) |
| LiveKit, multi-region set | 20 ms | 19.1 (19.1–19.3) | 49.9 (49.7–55.2) | 126.6 (124.9–128.1) | 144.9 (144.9–145.4) |
| LiveKit Cloud | 20 ms | 33.3 (32.7–33.7) | 75.4 (75.3–75.6) | 150.2 (148.9–151.0) | 157.9 (154.8–159.3) |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 10.9 (10.8–11.3) | 41.8 (41.4–41.8) | 118.6 (118.1–121.0) | 137.2 (136.9–137.5) |
| Needletail, LL-HLS, reader as shipped (flac) | 5 ms | 12.1 (11.9–12.3) | 42.5 (42.2–42.8) | 119.3 (119.2–121.0) | 138.0 (138.0–138.9) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 5 ms | 11.8 (11.6–11.9) | 42.6 (42.1–42.6) | 119.3 (119.2–121.5) | 138.3 (137.8–138.7) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 5 ms | 11.9 (11.7–12.0) | 42.5 (42.4–42.8) | 119.4 (119.2–121.3) | 138.0 (137.9–138.7) |
| Cloudflare Realtime | 20 ms | 24.7 (22.7–25.8) | 58.9 (58.4–88.2) | 135.4 (132.7–135.8) | 148.9 (144.9–154.6) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 25.4 (23.8–25.6) | 58.6 (58.5–59.3) | 134.7 (133.8–136.9) | 148.3 (147.7–182.9) |
| LiveKit, SFU in each region | 20 ms | 19.4 (19.4–19.6) | 54.6 (50.2–55.4) | 126.9 (123.2–128.8) | 145.3 (145.2–145.9) |
| LiveKit, multi-region set | 20 ms | 19.5 (19.4–19.6) | 50.2 (50.0–55.6) | 126.9 (125.3–128.7) | 145.2 (145.1–145.8) |
| LiveKit Cloud | 20 ms | 33.5 (33.4–34.0) | 76.4 (76.4–76.6) | 151.2 (150.3–152.1) | 159.2 (156.3–160.3) |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 3.5 (3.2–3.6) | 3.4 (3.4–3.5) | 3.5 (3.3–4.0) | 3.6 (3.6–4.0) |
| Needletail, LL-HLS, reader as shipped (flac) | 5 ms | 3.9 (3.7–4.1) | 3.7 (3.6–3.8) | 3.8 | 4.2 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 5 ms | 3.7 (3.6–3.7) | 3.6 (3.4–3.8) | 3.7 (3.7–4.2) | 4.1 (3.9–4.4) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 5 ms | 3.8 | 3.7 (3.5–3.8) | 3.7 (3.7–3.8) | 4.0 |
| Cloudflare Realtime | 20 ms | 2.0 (1.8–4.2) | 2.1 (1.8–36.7) | 1.9 (1.8–2.3) | 1.6 (1.4–2.1) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 3.2 (2.4–5.5) | 2.2 (2.0–3.0) | 2.0 (1.9–3.6) | 1.9 (1.9–3.4) |
| LiveKit, SFU in each region | 20 ms | 1.5 (1.3–1.5) | 1.4 (1.1–1.4) | 1.4 (1.2–1.4) | 1.0 (1.0–1.4) |
| LiveKit, multi-region set | 20 ms | 1.4 | 1.3 (1.2–1.3) | 1.3 (1.1–1.6) | 0.9 (0.9–1.2) |
| LiveKit Cloud | 20 ms | 1.8 (1.5–2.0) | 2.5 (2.2–3.0) | 2.8 (2.4–2.9) | 2.9 (2.8–3.2) |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, reader as shipped (flac) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Cloudflare Realtime | 20 ms | 0.000 (0.000–0.039) | 0.000 (0.000–0.040) | 0.000 | 0.000 |
| Cloudflare Stream WHIP/WHEP | 20 ms | 0.000 (0.000–0.040) | 0.000 | 0.000 | 0.000 |
| LiveKit, SFU in each region | 20 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit, multi-region set | 20 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit Cloud | 20 ms | 0.000 | 0.000 | 0.000 | 0.000 |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, reader as shipped (flac) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Realtime | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Stream WHIP/WHEP | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, SFU in each region | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, multi-region set | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit Cloud | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 1132.4 (165.6–1134.0) | 223.6 (180.2–236.3) | 1019.5 (137.6–2017.1) | 158.5 (131.7–179.3) |
| Needletail, LL-HLS, reader as shipped (flac) | 5 ms | 1137.2 (158.5–1138.9) | 226.2 (197.4–254.8) | 1020.5 (49.6–1227.6) | 170.7 (147.2–171.6) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 5 ms | 1138.3 (1126.4–1325.4) | 232.0 (179.0–257.2) | 150.0 (54.2–1002.2) | 169.6 (140.2–177.6) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 5 ms | 1132.3 (1127.1–1322.8) | 212.5 (199.1–230.8) | 1210.8 (1012.7–2024.3) | 150.3 (127.1–169.0) |
| Cloudflare Realtime | 20 ms | 920.6 (882.4–967.9) | 1935.0 (1173.9–1962.5) | 3688.1 (3583.1–3804.4) | 2942.6 (2806.8–4761.8) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 1260.0 (1196.5–1308.3) | 2454.8 (2283.8–2505.6) | 4557.7 (2706.8–4623.6) | 3782.9 (3738.7–3886.8) |
| LiveKit, SFU in each region | 20 ms | 171.9 (160.0–172.6) | 170.6 (166.4–175.7) | 165.7 (163.9–169.8) | 166.5 (163.2–178.1) |
| LiveKit, multi-region set | 20 ms | 172.2 (163.0–184.4) | 1161.3 (1159.2–1258.0) | 4029.5 (4019.3–4048.5) | 4767.2 (4732.5–4774.7) |
| LiveKit Cloud | 20 ms | 262.6 (255.1–279.5) | 456.7 (456.2–487.0) | 1449.6 (1305.0–1695.3) | 1267.0 (1166.9–1722.8) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 ms | 1853.1 (1853.1–1853.2) | 1853.0 (1852.8–1853.2) | 1852.6 (1851.4–1852.7) | 1852.2 (1852.1–1852.4) |
| Needletail, LL-HLS, reader as shipped (flac) | 5 ms | 1853.1 (1853.1–1853.2) | 1853.0 (1852.7–1853.0) | 1852.6 (1852.0–1853.3) | 1852.3 (1852.2–1852.4) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (flac) | 5 ms | 1853.1 (1853.0–1853.4) | 1853.0 (1852.8–1853.0) | 1852.5 (1852.0–1852.8) | 1852.3 (1852.1–1852.3) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (flac) | 5 ms | 1853.3 (1853.2–1853.4) | 1853.0 (1852.8–1853.1) | 1852.7 (1851.2–1853.4) | 1852.3 |
| Cloudflare Realtime | 20 ms | 256.0 (255.9–256.1) | 256.0 (255.8–256.0) | 256.0 (255.9–256.1) | 256.0 (256.0–256.1) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 256.0 (255.9–256.1) | 256.0 | 256.0 | 256.0 (256.0–256.1) |
| LiveKit, SFU in each region | 20 ms | 256.0 | 256.1 (255.9–256.1) | 256.0 | 256.0 (255.9–256.1) |
| LiveKit, multi-region set | 20 ms | 256.0 (255.9–256.1) | 256.0 | 256.1 (256.0–256.1) | 256.0 (256.0–256.1) |
| LiveKit Cloud | 20 ms | 256.0 (255.9–256.1) | 256.0 (256.0–256.1) | 256.0 | 256.0 (255.9–256.1) |

### Impairment, opus-2.5

Units missing at the listener (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.67 (0.38–0.67) / 0.67 (0.42–0.73) | 0.00 / 0.00 |
| Needletail, LL-HLS, reader as shipped (opus) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 (0.00–0.64) / 0.00 | 0.00 / 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 (0.00–0.02) | 0.00 / 0.00 | 0.00 / 0.00 |
| Cloudflare Realtime | 0.00 (0.00–0.12) / 0.00 (0.00–0.12) | 0.98 (0.86–0.99) / 0.93 (0.73–0.98) | 5.16 (5.13–5.50) / 5.04 (4.56–5.15) | 4.97 (4.94–5.31) / 4.81 (4.59–5.35) | 0.00 (0.00–0.06) / 0.01 (0.00–0.07) |
| Cloudflare Stream WHIP/WHEP | 0.01 (0.00–0.01) / 0.01 (0.00–0.01) | 1.02 (0.96–1.23) / 1.12 (0.89–1.17) | 5.14 (4.74–5.35) / 4.98 (4.53–5.01) | 5.25 (4.93–5.32) / 4.93 (4.86–5.16) | 0.00 / 0.00 |
| LiveKit, SFU in each region | 0.00 / 0.00 | 1.07 (0.97–1.08) / 1.11 (0.88–1.11) | 4.82 (4.65–5.04) / 4.83 (4.72–5.42) | 4.99 (4.80–5.59) / 4.93 (4.86–5.17) | 0.00 / 0.00 |
| LiveKit, multi-region set | 0.00 / 0.00 | 1.10 (0.93–1.15) / 0.91 | 5.04 (4.85–5.10) / 5.09 (4.97–5.16) | 5.30 (4.78–5.39) / 4.90 (4.84–5.00) | 0.00 / 0.00 |
| LiveKit Cloud | 0.00 / 0.00 (0.00–0.13) | 1.03 (0.95–1.06) / 0.97 (0.95–1.03) | 5.03 (4.86–5.29) / 5.05 (4.84–5.17) | 5.17 (4.85–5.26) / 4.96 (4.95–5.20) | 0.00 / 0.00 |

Longest gap (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 10.0 (5.0–10.0) / 10.0 | 0.0 / 0.0 |
| Needletail, LL-HLS, reader as shipped (opus) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 (0.0–50.0) / 0.0 | 0.0 / 0.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 (0.0–5.0) | 0.0 / 0.0 | 0.0 / 0.0 |
| Cloudflare Realtime | 0.0 (0.0–15.0) / 0.0 (0.0–15.0) | 2.5 / 5.0 | 15.0 (10.0–22.5) / 15.0 (7.5–22.5) | 10.0 (10.0–12.5) / 15.0 (10.0–17.5) | 0.0 (0.0–15.0) / 2.5 (0.0–15.0) |
| Cloudflare Stream WHIP/WHEP | 5.0 (0.0–5.0) / 2.5 (0.0–5.0) | 2.5 (2.5–5.0) / 5.0 (2.5–5.0) | 7.5 (5.0–7.5) / 7.5 (5.0–7.5) | 12.5 (12.5–15.0) / 10.0 (10.0–12.5) | 0.0 / 0.0 |
| LiveKit, SFU in each region | 0.0 / 0.0 | 2.5 (2.5–5.0) / 5.0 | 7.5 (5.0–7.5) / 7.5 | 12.5 (10.0–12.5) / 12.5 (10.0–15.0) | 0.0 / 0.0 |
| LiveKit, multi-region set | 0.0 / 0.0 | 2.5 / 2.5 (2.5–5.0) | 7.5 (5.0–7.5) / 7.5 (5.0–7.5) | 10.0 (10.0–12.5) / 10.0 | 0.0 / 0.0 |
| LiveKit Cloud | 0.0 / 0.0 (0.0–65.0) | 5.0 (2.5–5.0) / 5.0 (2.5–5.0) | 7.5 / 7.5 (5.0–7.5) | 12.5 (10.0–12.5) / 12.5 | 0.0 / 0.0 |

Mouth to ear, played on arrival, p99 (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 13 / 139 | 13 / 139 | 13 / 139 | 13 / 140 (139–204) | 113 / 239 |
| Needletail, LL-HLS, reader as shipped (opus) | 14 / 141 (141–144) | 860 (855–1626) / 714 (702–839) | 22056 (21861–22216) / 21663 (20281–23031) | 8568 (6768–8920) / 9089 (7953–9465) | 28919 (28325–29900) / 29275 (28660–30026) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (opus) | 14 / 140 (140–250) | 20 (14–98) / 142 (141–146) | 530 (227–542) / 515 (332–544) | 215 (204–479) / 584 (559–654) | 672 (667–683) / 769 (764–831) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 14 / 143 (140–231) | 15 (15–16) / 141 (141–142) | 21 / 148 (147–149) | 65 (58–140) / 293 (165–714) | 520 (513–554) / 1059 (829–1063) |
| Cloudflare Realtime | 11 (8–11) / 134 (133–139) | 10 (10–11) / 133 (132–139) | 10 (8–12) / 132 (132–139) | 10 (9–11) / 132 (131–133) | 109 (109–110) / 233 (231–252) |
| Cloudflare Stream WHIP/WHEP | 9 (9–11) / 132 (132–168) | 13 (11–30) / 151 (132–153) | 10 (9–12) / 133 (131–176) | 11 (9–15) / 155 (139–169) | 109 (108–110) / 232 (231–253) |
| LiveKit, SFU in each region | 4 / 130 | 4 / 130 | 4 / 130 | 4 / 130 (130–133) | 104 / 230 |
| LiveKit, multi-region set | 4 / 130 | 4 / 130 | 4 / 130 | 4 / 130 | 104 / 230 |
| LiveKit Cloud | 18 (18–19) / 144 | 18 / 144 (140–145) | 18 (18–21) / 144 (140–191) | 18 (18–32) / 144 (140–146) | 119 / 244 (240–244) |

Chrome concealed samples (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 0.00 (0.00–0.25) / 0.05 (0.00–0.22) | 4.40 (4.06–4.76) / 4.13 (3.17–5.57) | 17.89 (17.56–18.05) / 18.45 (16.97–19.13) | 14.49 (14.37–17.24) / 14.32 (13.60–16.64) | 0.00 (0.00–0.18) / 0.00 (0.00–0.18) |
| Cloudflare Stream WHIP/WHEP | 0.00 (0.00–0.05) / 0.00 | 4.17 (3.73–4.22) / 3.58 (3.40–3.76) | 17.27 (16.54–17.82) / 18.36 (18.11–18.61) | 13.31 (12.41–15.31) / 12.18 (10.50–12.20) | 0.00 (0.00–0.06) / 0.00 (0.00–0.05) |
| LiveKit, SFU in each region | 0.00 / 0.00 | 1.28 (1.19–1.50) / 1.39 (1.16–1.54) | 1.55 (1.42–2.03) / 1.59 (1.35–1.61) | 1.97 (1.38–2.21) / 1.95 (1.42–2.77) | 0.00 / 0.00 (0.00–0.08) |
| LiveKit, multi-region set | 0.00 / 0.00 | 1.08 (1.03–1.19) / 2.29 (2.28–2.51) | 2.02 (1.76–2.15) / 3.51 (3.15–3.62) | 2.40 (1.71–2.46) / 5.09 (5.04–5.39) | 0.00 / 0.00 |
| LiveKit Cloud | 0.00 / 0.00 | 1.52 (1.42–1.52) / 1.30 (1.28–1.33) | 1.76 (1.70–1.84) / 1.92 (1.64–2.28) | 2.97 (2.70–5.31) / 2.25 (2.07–3.32) | 0.00 / 0.00 |

Chrome mouth to ear at the marker, median (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 87 (73–97) / 201 (197–214) | 274 (238–355) / 425 (383–429) | 342 (268–395) / 508 (411–566) | 384 (341–396) / 500 (442–503) | 188 (176–188) / 305 (297–313) |
| Cloudflare Stream WHIP/WHEP | 91 (70–97) / 229 (193–233) | 256 (249–437) / 428 (421–451) | 334 (326–373) / 379 (366–389) | 359 (343–436) / 394 (302–477) | 180 (178–189) / 311 (311–313) |
| LiveKit, SFU in each region | 72 (66–72) / 201 (193–206) | 125 (100–161) / 281 (272–294) | 250 (246–309) / 372 (359–379) | 254 (250–261) / 380 (297–412) | 168 (163–173) / 300 (289–315) |
| LiveKit, multi-region set | 77 (65–78) / 188 (188–189) | 201 (188–215) / 442 (436–451) | 268 (251–269) / 456 (451–461) | 204 (202–240) / 398 (379–464) | 169 (167–171) / 293 (293–307) |
| LiveKit Cloud | 87 (82–87) / 202 (199–215) | 198 (190–217) / 307 (306–330) | 307 (290–347) / 408 (396–433) | 203 (154–211) / 313 | 182 (173–183) / 305 (302–308) |

### Impairment, pcm

Units missing at the listener (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.17 (0.15–0.18) / 0.24 (0.15–0.30) | 0.00 / 0.00 |
| Needletail, LL-HLS, reader as shipped (pcm) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 (0.00–0.08) |
| Cloudflare Realtime | 0.04 (0.00–0.05) / 0.03 (0.02–0.72) | 1.08 (1.08–1.23) / 1.07 (1.05–1.48) | 5.00 (4.76–5.28) / 4.99 (4.96–5.22) | 5.16 (4.61–5.38) / 4.82 (4.72–5.04) | 0.08 (0.08–0.09) / 0.07 (0.03–0.21) |
| LiveKit, SFU in each region | 0.01 / 0.00 (0.00–0.05) | 0.95 (0.94–1.01) / 0.88 (0.81–0.91) | 4.99 (4.96–5.11) / 5.01 (4.52–5.20) | 5.25 (4.49–5.27) / 4.86 (4.75–5.02) | 0.09 (0.07–0.16) / 0.15 (0.02–0.21) |
| LiveKit, multi-region set | 0.00 / 0.03 (0.02–0.03) | 1.04 (0.99–1.06) / 0.98 (0.89–1.18) | 5.08 (4.92–5.10) / 5.11 (4.84–5.50) | 5.19 (4.77–5.47) / 4.66 (4.29–5.11) | 0.05 (0.05–0.08) / 0.02 (0.00–0.04) |
| LiveKit Cloud | 0.00 / 0.04 (0.00–0.41) | 1.04 (0.99–1.07) / 1.02 (0.81–1.03) | 4.84 (4.41–4.94) / 5.07 (4.75–5.09) | 5.19 (4.64–5.62) / 5.07 (4.82–5.08) | 88.69 (88.66–88.70) / 87.99 (87.96–88.01) |

Longest gap (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 5.0 / 5.0 | 0.0 / 0.0 |
| Needletail, LL-HLS, reader as shipped (pcm) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 (0.0–20.0) |
| Cloudflare Realtime | 2.5 (0.0–5.0) / 10.0 (7.5–327.5) | 5.0 (5.0–25.0) / 5.0 (5.0–25.0) | 7.5 / 7.5 (5.0–7.5) | 7.5 (7.5–12.5) / 12.5 (10.0–52.5) | 2.5 (2.5–7.5) / 2.5 (2.5–32.5) |
| LiveKit, SFU in each region | 2.5 (0.0–2.5) / 2.5 (0.0–22.5) | 2.5 (2.5–5.0) / 5.0 (2.5–7.5) | 7.5 (5.0–7.5) / 7.5 (7.5–12.5) | 12.5 (10.0–25.0) / 10.0 (7.5–10.0) | 7.5 (2.5–7.5) / 5.0 (2.5–7.5) |
| LiveKit, multi-region set | 0.0 / 2.5 (2.5–5.0) | 2.5 (2.5–5.0) / 5.0 (2.5–7.5) | 7.5 / 5.0 (5.0–10.0) | 10.0 (10.0–12.5) / 10.0 (7.5–32.5) | 2.5 (2.5–5.0) / 2.5 (0.0–2.5) |
| LiveKit Cloud | 0.0 / 17.5 (0.0–200.0) | 2.5 (2.5–5.0) / 5.0 | 7.5 (5.0–7.5) / 7.5 (5.0–12.5) | 12.5 (10.0–12.5) / 10.0 (10.0–12.5) | 165.0 (152.5–172.5) / 125.0 (117.5–155.0) |

Mouth to ear, played on arrival, p99 (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 9 / 136 (135–136) | 9 / 135 | 9 (9–12) / 136 (135–138) | 9 / 135 | 109 / 236 (235–241) |
| Needletail, LL-HLS, reader as shipped (pcm) | 10 / 136 (136–413) | 381 (243–461) / 434 (401–511) | 15261 (13711–15663) / 14778 (13568–14836) | 6279 (4473–7264) / 2715 (1622–4186) | 29911 (29893–29925) / 30041 (30041–30043) |
| Needletail, LL-HLS, pipelined reader, HTTP/2 (pcm) | 10 / 137 (136–343) | 74 (11–100) / 138 (138–224) | 1026 (436–1228) / 540 (510–619) | 207 (206–1439) / 447 (331–553) | 799 (774–805) / 958 (916–1033) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 10 / 137 (136–390) | 13 (12–13) / 139 (138–139) | 49 (41–60) / 167 (165–194) | 49 (38–56) / 175 (172–194) | 1005 (978–1021) / 1485 (1080–3245) |
| Cloudflare Realtime | 10 (8–11) / 150 (140–8387) | 9 (8–10) / 132 (130–10894) | 9 (8–9) / 150 (132–9949) | 9 (9–10) / 168 (150–9877) | 109 (108–109) / 232 (231–8749) |
| LiveKit, SFU in each region | 5 / 131 (130–245) | 5 / 131 | 5 / 130 | 5 (5–3078) / 131 | 105 / 231 |
| LiveKit, multi-region set | 5 / 131 (131–132) | 6 / 131 | 5 / 131 | 5 / 131 (131–9794) | 106 (106–108) / 232 |
| LiveKit Cloud | 19 (18–20) / 145 | 19 (19–20) / 145 | 19 (19–33) / 145 (144–145) | 19 (19–33) / 145 (144–145) | 119 (119–120) / 245 |

### Listeners, opus-2.5

Listeners with audio, of those started in the region:

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, LL-HLS, reader as shipped (opus) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Cloudflare Realtime | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 24/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 24/25, 24/25 |
| Cloudflare Stream WHIP/WHEP | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 22/25, 22/25, 23/25 |
| LiveKit, SFU in each region | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit, multi-region set | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit Cloud | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 |

Unit latency, median listener's median (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5.9 | 11.3 (11.1–11.3) | 137.3 (137.2–137.3) | 11.9 (11.8–12.0) | 42.8 (42.5–42.9) | 119.4 (119.3–121.3) | 137.8 (137.8–137.9) |
| Needletail, LL-HLS, reader as shipped (opus) | 6.1 (6.1–6.3) | 11.6 | 137.5 (137.4–137.7) | 3638.3 (3612.1–4890.6) | 64.8 (48.6–1510.6) | 5214.1 (4563.4–5867.9) | 142.2 (141.9–150.1) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 6.3 | 11.9 | 137.8 (137.8–137.9) | 20.5 (19.6–20.7) | 49.9 (49.5–50.5) | 136.3 (133.6–140.7) | 144.1 (144.0–144.2) |
| Cloudflare Realtime | 5.2 (4.3–6.4) | 4.5 (4.4–6.0) | 135.0 (127.6–138.6) | 5.3 (5.3–6.6) | 39.9 (39.7–40.9) | 114.9 (114.7–115.9) | 130.2 (127.9–135.4) |
| Cloudflare Stream WHIP/WHEP | 4.9 (4.7–5.0) | 5.7 (4.3–6.0) | 137.1 (129.0–149.0) | 5.9 (5.3–5.9) | 40.1 (39.6–40.5) | 115.0 (114.2–116.8) | 139.1 (128.9–147.5) |
| LiveKit, SFU in each region | 1.0 (0.9–1.2) | 1.1 (1.0–1.2) | 126.9 (126.9–127.0) | 1.5 (1.5–1.7) | 37.6 (37.5–37.7) | 109.1 (109.0–110.8) | 127.4 (127.3–127.4) |
| LiveKit, multi-region set | 1.0 (0.9–1.2) | 1.1 (1.1–1.3) | 127.1 (127.0–127.1) | 2.0 (1.9–2.2) | 33.2 (33.0–33.4) | 109.6 (109.5–110.6) | 128.0 (127.8–128.0) |
| LiveKit Cloud | 14.2 (14.1–15.8) | 14.7 (14.6–14.8) | 139.6 (135.4–139.8) | 15.0 (14.8–15.0) | 56.9 (56.9–57.0) | 130.0 (126.2–130.0) | 140.1 (135.8–140.2) |

Unit latency, worst listener's p99 (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 9 | 16 | 142 | 19 | 49 | 126 (126–128) | 145 (144–147) |
| Needletail, LL-HLS, reader as shipped (opus) | 9 | 17 | 143 (143–145) | 5129 (4415–6335) | 1316 (792–2317) | 7101 (6238–7316) | 346 (256–2298) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 10 | 17 | 143 | 289 (254–289) | 307 (263–310) | 499 (492–499) | 317 (276–321) |
| Cloudflare Realtime | 8 (8–17) | 10 (7–11) | 138 (131–150) | 14 (12–15) | 48 (45–49) | 128 (128–129) | 156 (154–1610) |
| Cloudflare Stream WHIP/WHEP | 7 (7–8) | 8 (7–11) | 148 (131–176) | 13 (12–16) | 45 (44–46) | 128 (127–129) | 169 (134–1833) |
| LiveKit, SFU in each region | 2 | 2 | 128 | 6 | 41 | 114 (113–115) | 131 (131–1264) |
| LiveKit, multi-region set | 2 | 3 | 128 | 39 (38–42) | 74 (74–77) | 149 (148–152) | 165 (164–172) |
| LiveKit Cloud | 16 (16–18) | 17 | 142 (138–142) | 20 (19–22) | 61 | 135 (131–135) | 144 (140–1305) |

Units missing (%):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Needletail, LL-HLS, reader as shipped (opus) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (opus) | 0.00 | 0.00 | 0.00 | 0.00 (0.00–0.01) | 0.00 | 0.00 (0.00–0.01) | 0.00 |
| Cloudflare Realtime | 0.01 (0.00–0.13) | 0.05 (0.00–0.23) | 0.00 (0.00–0.23) | 0.06 (0.05–0.10) | 0.07 (0.04–0.09) | 0.04 (0.04–0.06) | 0.11 (0.07–1.06) |
| Cloudflare Stream WHIP/WHEP | 0.00 (0.00–0.02) | 0.00 | 0.00 | 0.00 (0.00–0.02) | 0.00 (0.00–0.02) | 0.00 (0.00–0.02) | 0.00 (0.00–0.93) |
| LiveKit, SFU in each region | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 (0.00–0.54) |
| LiveKit, multi-region set | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| LiveKit Cloud | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 (0.00–1.05) |

### Listeners, pcm

Listeners with audio, of those started in the region:

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, LL-HLS, reader as shipped (pcm) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Cloudflare Realtime | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit, SFU in each region | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit, multi-region set | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit Cloud | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 |

Unit latency, median listener's median (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 2.4 (2.3–2.5) | 2.5 (2.4–2.5) | 128.4 (128.4–128.5) | 1058.1 (3.7–1181.4) | 33.9 (33.7–34.3) | 111.1 (110.9–111.7) | 129.1 (129.1–130.2) |
| Needletail, LL-HLS, reader as shipped (pcm) | 2.7 (2.5–2.8) | 2.9 (2.7–3.0) | 128.6 (128.6–128.8) | 13666.9 (13443.7–13823.5) | 9246.8 (8914.1–9375.2) | 13786.4 (12766.6–14393.1) | 8007.1 (6820.0–8943.4) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 2.9 (2.7–3.0) | 3.2 (3.1–3.2) | 128.9 (128.8–128.9) | 1721.0 (1083.2–1786.5) | 61.4 (54.6–96.0) | 382.4 (314.9–1646.9) | 326.5 (275.5–462.8) |
| Cloudflare Realtime | 4.1 (3.5–5.2) | 4.6 (3.8–5.3) | 164.1 (146.2–3333.9) | 6.6 (5.3–7.2) | 40.8 (39.7–41.7) | 116.2 (115.0–1620.6) | 147.3 (130.2–166.3) |
| LiveKit, SFU in each region | 1.2 (1.1–1.3) | 1.3 (1.2–1.4) | 127.1 (127.1–127.2) | 3.0 (3.0–3.1) | 33.6 (33.5–38.6) | 110.7 (107.4–112.3) | 128.6 |
| LiveKit, multi-region set | 1.2 (1.1–1.3) | 1.5 (1.4–1.5) | 127.3 (127.3–127.5) | 7723.3 (7682.0–7770.0) | 7658.3 (7655.9–7698.4) | 7875.6 (7499.4–7929.2) | 7915.6 (6626.2–7966.1) |
| LiveKit Cloud | 14.8 (14.1–15.7) | 14.7 (14.4–15.1) | 139.9 (135.7–140.5) | 15.6 (15.4–16.4) | 57.7 (57.6–58.1) | 133.6 (133.4–133.8) | 140.9 (140.7–141.2) |

Unit latency, worst listener's p99 (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 5 | 5 | 131 (131–133) | 2080 (15–2451) | 41 (40–42) | 119 (119–550) | 137 (137–3487) |
| Needletail, LL-HLS, reader as shipped (pcm) | 5 (5–6) | 5 | 131 (131–138) | 20016 (18926–20068) | 13505 (12688–13628) | 19207 (17877–19512) | 11690 (10653–13126) |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 5 | 6 | 133 (132–134) | 12361 (6618–15771) | 1143 (984–1274) | 3508 (3332–9514) | 2788 (2095–23838) |
| Cloudflare Realtime | 11 (5–11) | 7 (5–8) | 167 (147–8613) | 39 (29–41) | 63 (55–78) | 6752 (141–6851) | 8681 (163–10042) |
| LiveKit, SFU in each region | 2 | 2 | 128 | 33 (29–33) | 53 (52–57) | 141 (136–142) | 150 (149–150) |
| LiveKit, multi-region set | 2 | 6 (5–7) | 131 (130–132) | 13684 (11196–14085) | 13733 (11312–14119) | 14196 (13815–16487) | 14237 (13824–24356) |
| LiveKit Cloud | 17 (16–18) | 17 | 143 (138–143) | 40 (39–48) | 75 (74–76) | 160 (159–161) | 160 (159–160) |

Units missing (%):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, RaptorQ FEC over UDP from the edge (uc) | 0.00 | 0.00 | 0.00 | 2.01 (0.00–5.25) | 0.00 | 0.00 | 0.00 (0.00–12.64) |
| Needletail, LL-HLS, reader as shipped (pcm) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Needletail, LL-HLS, pipelined reader, HTTP/3 (pcm) | 0.00 | 0.00 | 0.00 | 0.00 | 0.01 (0.00–0.04) | 0.04 (0.00–0.04) | 0.04 (0.02–0.36) |
| Cloudflare Realtime | 0.00 (0.00–0.02) | 0.03 (0.01–0.03) | 0.03 (0.01–0.12) | 0.02 | 0.01 (0.00–0.02) | 0.01 (0.00–0.02) | 0.02 (0.00–0.04) |
| LiveKit, SFU in each region | 0.00 | 0.00 | 0.00 | 0.01 (0.00–0.04) | 0.03 (0.00–0.06) | 0.04 (0.00–0.04) | 0.01 (0.00–0.02) |
| LiveKit, multi-region set | 0.00 (0.00–0.02) | 0.00 (0.00–0.02) | 0.02 (0.01–0.10) | 0.01 | 0.02 (0.01–0.02) | 0.03 (0.00–0.13) | 0.01 (0.00–11.37) |
| LiveKit Cloud | 0.00 | 0.00 | 0.00 (0.00–0.01) | 0.00 | 0.00 | 0.01 | 0.00 |

### Processor of the nodes, opus-2.5

Percent of one vCPU used by the service, during the step:

| Node | 1 per host | 10 | 100, first pass | 100, second pass |
| --- | ---: | ---: | ---: | ---: |
| Needletail contributor | 61 | 61 | 61 (61–62) | 61 (60–61) |
| Needletail backbone relay | 39 | 38 (38–39) | 39 (38–40) | 38 (38–39) |
| Needletail distributor, London | 19 | 22 | 22 | 22 |
| Needletail edge 1, London | 9 (9–12) | 12 | 63 (61–64) | 72 (63–82) |
| Needletail edge 2, London | 13 | 1 | 1 | 47 (47–53) |
| Needletail edge 1, Sydney | 14 (9–15) | 8 (1–12) | 35 (1–59) | 62 (62–71) |
| LiveKit, London node (both servers) | 16 | 11 | 30 | 100 (98–104) |
| LiveKit, Sydney node (both servers) | 4 | 5 | 21 (20–22) | 3 |

### Processor of the nodes, pcm

Percent of one vCPU used by the service, during the step:

| Node | 1 per host | 10 | 100, first pass | 100, second pass |
| --- | ---: | ---: | ---: | ---: |
| Needletail contributor | 61 | 60 (60–62) | 61 | 61 |
| Needletail backbone relay | 39 | 39 | 39 | 39 |
| Needletail distributor, London | 23 | 22 | 22 | 23 |
| Needletail edge 1, London | 32 (25–41) | 12 (2–14) | 31 (2–48) | 123 (46–171) |
| Needletail edge 2, London | 34 (18–35) | 9 (1–14) | 20 (1–47) | 100 (42–174) |
| Needletail edge 1, Sydney | 31 (27–35) | 8 | 27 (22–33) | 88 (80–96) |
| LiveKit, London node (both servers) | 95 (94–96) | 29 | 101 (98–102) | 198 |
| LiveKit, Sydney node (both servers) | 16 | 15 (15–16) | 87 (86–88) | 6 |
