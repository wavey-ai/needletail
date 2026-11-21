# 21 November 2026: audio against Cloudflare and LiveKit

The owner asked whether Needletail is the fastest network yet, and then put
audio first: Opus for the lowest latency, PCM for lossless latency. This
record compares Needletail's regional LL-HLS cache mesh with Cloudflare
Realtime, Cloudflare Stream WHIP/WHEP, self-hosted LiveKit and LiveKit Cloud.
Every system carried the same audio from the same host in London to the same
listeners in London, New York, Tokyo and Sydney, at the same time.

The 4K video comparison is a separate record.

## Answer

**Needletail is not the fastest for Opus, and it is not the fastest at the
median for lossless PCM. It is the only one that carries lossless audio as
audio, and the only one that loses nothing when the listener's network loses
packets.**

Mouth to ear is the age of a sample when a listener that plays audio on
arrival would play it. Median of three sessions, in milliseconds:

| Lane | System | Frame | London | New York | Tokyo | Sydney |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Opus | LiveKit, self-hosted, SFU in each region | 2.5 ms | 1.9 | 32.6 | 109.1 | 127.8 |
| Opus | Cloudflare Realtime | 2.5 ms | 6.3 | 40.4 | 115.5 | 129.4 |
| Opus | **Needletail** | 5 ms | 12.0 | 47.1 | 118.6 | 138.5 |
| Opus | LiveKit Cloud | 2.5 ms | 15.9 | 58.2 | 131.3 | 140.8 |
| PCM | LiveKit, self-hosted, data channel | 2.5 ms | 2.5 | 38.0 | 111.1 | 128.1 |
| PCM | Cloudflare Realtime, data channel | 2.5 ms | 6.6 | 40.9 | 116.2 | 128.3 |
| PCM | **Needletail** | 5 ms | 10.5 | 45.7 | 116.7 | 136.5 |
| PCM | LiveKit Cloud, data channel | 2.5 ms | 16.1 | 57.7 | 133.0 | 140.7 |

Where Needletail loses:

- **Opus latency.** Needletail is about 10 to 15 ms behind self-hosted LiveKit
  in every region and 3 to 9 ms behind Cloudflare. It is 2 to 13 ms ahead of
  LiveKit Cloud. Its frame is 5 ms where the others forward 2.5 ms, which is
  1.25 ms of the gap. The rest is the path: contributor, relay, distributor
  and edge, against one SFU.
- **Jitter.** On quiet hosts, outside London, the 99th percentile of
  Needletail is 7 to 10 ms above its median. The SFUs are 1.4 to 3 ms above
  theirs.
- **The reader as shipped.** `needletail-reader` asks for one 5 ms part at a
  time. With 5% loss at the listener it fell 22 seconds behind in 35 seconds.
  With 100 ms of delay each way it fell 30 seconds behind. With 25 listeners
  on one host it fell 3 to 9 seconds behind. A reader that keeps requests in
  flight over HTTP/3 has none of these faults. That reader is in the harness,
  not in the product.
- **One contributor Nanode carries one audio rendition.** One rendition takes
  30% to 42% of its vCPU. Opus, FLAC and PCM together take 71% to 78%, and a
  standing backlog formed.
- **Noisy hosts.** Needletail puts four 1 vCPU nodes in the path. With the
  nodes as Linode placed them, the contributor and the primary relay were on
  hosts with bursts of steal time, and every lane in every region had a spike
  of about one second about once a minute.
- **A browser.** Chrome plays every WebRTC lane (66 to 89 ms mouth to ear in
  London, 202 to 273 ms in Sydney). No browser plays Needletail's Opus or PCM
  parts without a player written for them.
- **The datagram lanes.** The native UDP and WebTransport taps, Needletail's
  own low-latency lanes, returned no datagram in this deployment.

Where Needletail wins:

- **Lossless audio.** Needletail delivered 24-bit PCM and FLAC bit for bit,
  as audio renditions. Cloudflare Realtime, Cloudflare Stream and LiveKit all
  refused an uncompressed track (`L16` and `L24`). Cloudflare Realtime and
  LiveKit can carry PCM bit for bit on a data channel, to a client written
  for it. Cloudflare Stream has no such route.
- **Loss.** With 1% loss, 5% loss and burst loss at the listener, Needletail
  delivered every unit. Every SFU lost what the network lost: about 1%, 5%
  and 5% of the Opus packets, and the same of the PCM blocks. With the
  pipelined HTTP/3 reader the 99th percentile of Needletail under 5% loss
  was 26 ms in London and 175 ms in Sydney.
- **Time to first audio.** A Needletail listener had lossless audio 35 to
  70 ms after it started. LiveKit with an SFU in the region took 50 to
  190 ms, LiveKit Cloud 0.4 to 1.5 seconds, Cloudflare Realtime 0.9 to 3.5
  seconds.
- **A crowd, with the pipelined reader.** 100 listeners of Opus, 25 in each
  region, were at 20, 54, 127 and 159 ms. One LiveKit Nanode with 100
  listeners of 2.5 ms Opus lost 8% to 10% of the audio.

What Needletail should fix to win, in order:

1. Ship the pipelined reader, over HTTP/3. It removes the three reader
   faults at no cost in latency.
2. Find the 10 ms. Measure each hop of the audio path: the contributor's
   recovery and packaging, the relay, the distributor's cache-mesh interval
   (2 ms here), the edge's reply.
3. Let a producer choose 2.5 ms parts.
4. Make the native UDP and WebTransport lanes work in the regional cache
   deployment, and measure them here. They are the fair opponent of RTP.
5. Give the contributor more than one vCPU, or one contributor for each
   rendition.
6. Qualify hosts before use: a 20-second processor burn finds a noisy host
   (`scripts/compare/steal-check.py`).
7. Ship a browser player for the audio lanes.

## What was compared

| System | How the audio went in | How a listener got it |
| --- | --- | --- |
| Needletail | Its own producers on `lon-src`: the DAW Nexus test source for SoundKit Opus and FLAC, and the AEP1 sender for 24-bit PCM, to the contributor in London. One rendition at a time. | LL-HLS from the edge of the listener's region, in 5 ms parts: stream 3001 (Opus), 2001 (FLAC), 4001 (PCM). |
| Cloudflare Realtime | One Opus track to the SFU through its HTTPS API. For PCM, one data channel. | Pull of the track, or of the data channel, through the same API. |
| Cloudflare Stream WHIP/WHEP | One Opus track by WHIP. | WHEP. It has no data channel. |
| LiveKit, SFU in each region | `livekit-server` 1.13.11 on one Nanode in each region, no Redis. The source publishes to all four. | The SFU of the listener's region. |
| LiveKit, multi-region set | The same four Nanodes as one set on one Redis, with the region-aware node selector. The source publishes through the London node. | Signal through the node of the listener's region. The room lives on the London node, so the media comes from London. |
| LiveKit Cloud | The owner's project. | LiveKit Cloud chose the edge: Germany for London, US East, Japan and Australia. |

Cloudflare Stream Live with RTMPS in and HLS or LL-HLS out is not here. It
carries AAC, not Opus or PCM, and the account cannot use it at all: see
[Not measured](#not-measured).

All Needletail code is from the pushed heads: `needletail`
`regional-llhls-cache` at `23dd320`, `av-mesh` at `a9cb1dd` (built on Debian 13
with cargo 1.100.0), `playlists` at `151eaee`, `av-contrib` from
`probe-soundkit-opus-plain` (`412014d`, binary of 19 November), `daw-nexus`
`qualification-source-loop` at `49cfceb`.

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
with chrony: the source and the listeners were within 0.13 ms of true time, and every node
within 0.24 ms.

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

### Three readers of Needletail

A Needletail audio part is 5 ms. `needletail-reader` and the LL-HLS reader of
`aep1-48k-probe` ask for one part, wait for it, and ask for the next. That
works while a round trip to the edge is shorter than a part. The listeners of
this lab are in the same data centre as their edge (about 0.5 ms).

A Needletail edge holds a request for a part that is not published yet and
answers the moment it is. That was checked up to 600 parts ahead (3 seconds).
A reader can therefore keep requests in flight. Three readers were measured:

| Reader | What it does |
| --- | --- |
| as shipped | One request for each part, HTTP/2. What `needletail-reader` does. |
| pipelined, HTTP/2 | 40 requests in flight (200 ms of parts), one TCP connection. |
| pipelined, HTTP/3 | 40 requests in flight, QUIC. |

The pipelined readers are in `tools/ntcmp` (`ntcmp hls --window 40 [--http3]`).
They are not in the product.

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

### Listeners

In each of London, New York, Tokyo and Sydney: one Nanode (1 vCPU, 1 GB) and
one host with four vCPUs (dedicated, except in Sydney, where Linode had only a
shared plan of that size). The Nanode runs the probes. The larger host runs
the probes too, headless Chrome, and the crowds of 25. A Nanode cannot run
Chrome with a 4K decoder, and the same hosts serve the video comparison.

Every system is heard by the same listeners at the same time, so all of them
see the same network. Systems that share a node are not loaded together in the
crowd steps.

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

| Session | Start (UTC, 21 November) | Hosts | Readers of Needletail |
| --- | --- | --- | --- |
| A | 14:08 | as Linode placed them | as shipped |
| B | 14:48 | as Linode placed them | all three |
| C | 15:38 | after ten Nanodes on noisy hosts were replaced | all three |

See [Noisy hosts](#noisy-hosts) for why the hosts matter. A cell in a table is
the median of the sessions, with the lowest and the highest session in
brackets when they differ.

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
| Needletail | Contributor | `daw_hls_packaging` `opaque`. `daw_hls_formats` one of `opus`, `flac`, `pcm`: one rendition at a time (see [The contributor](#the-contributor)). |
| Needletail | Opus | The DAW source's SoundKit Opus: 5 ms frames, 60-byte packets (96 kbit/s) with a 20-byte SoundKit header. |
| Chrome | Listener | Headless Chrome 158.0.8127.36, `jitterBufferTarget = 0`. |

## Results

Each cell is the median of the sessions. The lowest and highest session are
in brackets when they differ. All tables are made from the evidence file by
`scripts/compare/compare-audio-report.py`, and they follow under
[Tables](#tables).

### Opus

Every WebRTC system forwarded Opus frames of 2.5, 5, 10 and 20 ms unchanged:
every packet at every listener was bit for bit the packet sent. None of them
forces a frame length on a native sender. Chrome cannot send a frame shorter
than 10 ms, so a browser source is 3.75 ms worse than these figures at the
median and 7.5 ms worse at the top.
Needletail's frame is 5 ms: its producers fix it.

Frame length costs half its length at the median. Self-hosted LiveKit in
London: 1.9, 3.2, 6.3 and 12.5 ms for frames of 2.5, 5, 10 and 20 ms.

Self-hosted LiveKit is the floor. In London the packet is at the listener
0.7 ms after its last sample. To New York, Tokyo and Sydney it is the path
from London plus about a millisecond. The single-region and multi-region
deployments are the same to within 2 ms: in both, the media of a far listener
crosses the ocean once.

Cloudflare is 2 to 8 ms behind self-hosted LiveKit. Realtime and Stream
WHIP/WHEP are the same network: they differ by less than a millisecond.

LiveKit Cloud is the slowest in every region. It served the London listener
from Germany.

Needletail is between Cloudflare and LiveKit Cloud. Its three readers have
the same median: the pipelined readers gain nothing while the round trip to
the edge is half a millisecond.

In Chrome the jitter buffer adds 14 to 56 ms and the decoder and output the
rest: 66 to 89 ms mouth to ear in London, 101 to 128 ms in New York, 177 to
199 ms in Tokyo, 202 to 273 ms in Sydney. Self-hosted LiveKit with an SFU in
the region is the lowest in every region.

### Lossless

Each system was asked for an audio track of `audio/L16` and of `audio/L24`,
48 kHz, stereo:

| System | Answer |
| --- | --- |
| Cloudflare Realtime | `unable to start track, codec is not supported by remote` |
| Cloudflare Stream WHIP | `unable to start track, codec is not supported by remote` |
| LiveKit 1.13.11, self-hosted | The track is published in signalling, then `could not set remote description: unable to start track, codec is not supported by remote` |
| LiveKit Cloud | The same as self-hosted |

Their documentation agrees: Cloudflare lists Opus and G.711, and LiveKit
registers Opus, RED, PCMU and PCMA. So none of them carries lossless PCM as
audio.

Two routes remain. Opus at 510 kbit/s is carried by all of them and is not
lossless. A data channel carries PCM bit for bit through Cloudflare Realtime
and through LiveKit, self-hosted and Cloud: 100% of the blocks that arrived
were bit-exact. Cloudflare Realtime lost 0.03% to 0.1% of the blocks on a
clean network. Cloudflare Stream has no data channel.

Needletail's PCM and FLAC renditions were bit-exact at every listener, with
no part missing. FLAC and PCM have the same latency to within 2 ms.

Needletail's lossless tail is long. The 99th percentile of PCM was 85 to
330 ms above the median in London in all three sessions, and 8 to 75 ms
elsewhere. The cause is not determined: see [Open](#open).

### Impairment

With loss, an SFU and Needletail fail in opposite ways.

An SFU sends each packet once. With 1% loss at the listener about 1% of the
audio is gone, with 5% loss about 5%, with bursts about 5% in gaps of up to
25 ms.
What arrives is on time: the 99th percentile does not move. Chrome conceals
the gaps. With 5% loss it concealed 18% of the samples from Cloudflare and
1.5% to 2.8% from LiveKit. LiveKit's figure is below the loss, which is
consistent with LiveKit answering Chrome's requests for retransmission of
audio; that was not checked in the server.

Needletail delivers every unit, and pays in time. How much depends on the
reader:

- As shipped, the reader falls behind and does not recover within the step:
  0.7 to 1.5 seconds at 1% loss, 22 seconds at 5%, 2 to 16 seconds with
  bursts.
- Pipelined over HTTP/2, one lost packet still holds every later part: 0.3
  to 0.4 seconds at 5% loss.
- Pipelined over HTTP/3, the 99th percentile at 5% loss was 26 ms in London
  and 175 ms in Sydney, and 48 and 179 ms with bursts. The median did not
  move.

The 1% loss column of the pipelined readers has a wide range because session
B, on noisy hosts, had a one-second spike in that step. Session C had 37 and
158 ms.

With 100 ms of delay each way, an SFU is 100 ms later and nothing else
changes. Needletail's reader as shipped cannot keep up at all: a round trip
of 200 ms allows five requests a second and the stream has 200 parts a
second. The pipelined readers held 40 requests in flight, which is exactly
200 ms of parts and not enough: they ran 0.5 to 1.7 seconds late. A window
longer than the round trip is needed.

LiveKit Cloud dropped 88% of the PCM blocks on its data channel when the
listener was 100 ms away, in every session. Self-hosted LiveKit did not.

### Listeners

One and ten listeners: every system served them all, at the latency of one.

The figures of a crowd are unit latencies as measured. For Needletail's Opus
they have the DAW buffer in them (5.3 ms at the median).

One hundred listeners, 25 on one host in each region:

- **Needletail, reader as shipped:** all 100 had audio, 3 to 9 seconds late
  for Opus and 12 seconds late for PCM. No edge was above 57% of a vCPU. The limit is the reader: one request for each 5 ms part leaves no
  room for a reply that takes longer than 5 ms.
- **Needletail, pipelined over HTTP/3:** all 100 had audio. Opus was at 20,
  54, 127 and 159 ms, 3 to 21 ms more than ten listeners. PCM was at 46, 81,
  154 and 182 ms. In Sydney the worst listener's 99th percentile was above
  two seconds in one session of two.
- **Cloudflare:** 89, 96 and 100 of 100 Opus listeners connected. The others
  did not connect within 20 seconds. The first session started them over 4
  seconds, the others over 10. Those that connected were at the latency of
  one listener.
- **LiveKit, an SFU in each region:** all 100, at the latency of one.
- **LiveKit, multi-region set:** the room is on one Nanode in London, so it
  served all 100. With 2.5 ms Opus it lost 8% to 10% of the audio and the
  99th percentile passed one second. With PCM it ran 6.5 seconds late. The
  Nanode was at 78% and 97% of its vCPU. LiveKit's own load tester, with its
  default 20 ms packets, put 100 listeners on the same Nanode with no loss:
  the limit is packets a second, and 2.5 ms frames are eight times as many.
- **LiveKit Cloud:** 96 of 96, at the latency of one. The free plan allows
  100 connections.

### The contributor

Needletail's contributor is one Nanode. Processor of `av-contrib` with each
set of renditions, at 200 parts a second for each:

| Renditions published | Processor |
| --- | ---: |
| PCM | 27% to 40% |
| Opus (the DAW source also sends FLAC, which is not published) | 35% to 42% |
| FLAC and PCM | 64% to 70% |
| Opus, FLAC and PCM | 71% to 78% |

With all three, in the first trial, the contributor logged `publication
queue is full`, and Opus stayed 210 ms late, FLAC 143 ms and PCM 25 to 80 ms
for the whole step. The sessions therefore publish one rendition at a time.

### Noisy hosts

In sessions A and B every Needletail lane, in every region, had a spike of
about one second about once a minute. The samplers on the nodes showed the
cause: steal time. In session B the worst five-second sample was 54% on the
contributor, 82% on the primary relay and 78% on a Sydney distributor. New
York and Tokyo had none.

A 20-second processor burn on each Nanode found seven noisy hosts. Ten
Nanodes of Needletail and LiveKit and one listener Nanode were replaced
before session C, the primary relay three times before it had a quiet host.
Outside London, Needletail's jitter was 31 to 36 ms in session B and 7 to
10 ms in session C.

Self-hosted LiveKit has one node in the path, and its London Nanode happened
to be quiet. Needletail has four, and each one is a chance of a noisy host.

## Not measured

- **Needletail's native UDP and WebTransport lanes.** `aep1-48k-probe
  receive-udp` at a distributor's relay port and `receive-webtransport` at a
  distributor received no datagram: 0 of 2,000 epochs in London and Sydney
  over the public addresses, and 0 of 1,200 over the London VLAN, with the
  DAW source publishing. LL-HLS from the same nodes worked throughout. The
  cause was not looked for.
- **Needletail in a browser.** The Opus parts are SoundKit frames and the PCM
  parts are raw samples. A browser needs a player written for them.
- **Sound quality.** Every lane delivered its packets unchanged, so quality
  is the codec's: 256 kbit/s Opus on the SFUs, 96 kbit/s SoundKit Opus on
  Needletail, and no loss at all on the lossless lanes. No listening score
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

- The 99th percentile of Needletail's PCM in London was 85 to 330 ms above
  its median in every session, on noisy and on quiet hosts, and with every
  reader. Opus in the same sessions was not. Not explained.
- The pipelined readers need a window longer than the round trip. With 40
  parts in flight and a 200 ms round trip they ran late. A window of 100
  parts was not measured.
- The DAW source's 512-sample buffer was taken off by calculation. Needletail
  was not measured with a producer that hands it 5 ms blocks of Opus.
- `needletail-reader` itself was not run on the audio streams with 25
  readers. The reader as shipped is `ntcmp hls --window 1`, which reads as it
  does: one request for each part.
- Session C's London Nanode listener had one spike of about 0.2 seconds that
  every system shows in the Opus baseline (the 99th percentile ranges of the
  London column).
- LiveKit Cloud's loss of 88% of data-channel blocks with a 100 ms delay was
  not investigated.

## Evidence and harness

- Evidence: [`20261121-compare-audio.json`](evidence/20261121-compare-audio.json).
  For each session, step, host and lane: the figures of the probe, of Chrome,
  and the processor of each Nanode. Needletail's Opus and FLAC have both the
  measured values (`raw`) and the values without the DAW buffer.
- Raw records (every unit of every listener) are under
  `target/compare/runs/audio-{a,b,c}/` and are not versioned.
- Harness, on branch `compare-cloudflare`:
  - `tools/ntcmp`: the paced source, the WebRTC and data-channel probes, the
    HLS probe with its three readers;
  - `scripts/compare/compare-audio.py`: the session runner;
  - `scripts/compare/compare-audio-analyse.py` and
    `compare-audio-report.py`: the evidence file and these tables;
  - `scripts/compare/player/`: the Chrome listener;
  - `scripts/compare/netem.sh`, `steal-check.py`, `cloudflare.py`,
    `compare_lab.py`, `prepare-media.py`.

To run it again, with the lab of
`deploy/compare-cloudflare/topology-compare.json` up and Needletail deployed:

```sh
scripts/compare/cloudflare.py setup
scripts/compare/compare_lab.py livekit
scripts/compare/compare_lab.py prepare
scripts/compare/compare_lab.py audio
scripts/compare/compare_lab.py secrets
scripts/compare/steal-check.py            # replace the nodes it names
scripts/compare/compare-audio.py --session audio-x
scripts/compare/compare-audio-analyse.py --out evidence.json audio-x
scripts/compare/compare-audio-report.py evidence.json
scripts/compare/cloudflare.py teardown
```

## Tables

### Opus, shortest frame (2.5 ms; Needletail 5 ms)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 5 ms | 12.0 (11.9–13.0) | 47.1 (41.5–48.0) | 118.6 (118.6–120.6) | 138.5 (137.0–139.1) |
| Needletail, pipelined reader, HTTP/2 (opus) | 5 ms | 12.2 (11.8–12.6) | 44.6 (41.9–47.3) | 119.3 (118.3–120.2) | 138.1 (137.8–138.5) |
| Needletail, pipelined reader, HTTP/3 (opus) | 5 ms | 12.1 (11.7–12.4) | 44.9 (41.9–47.9) | 119.5 (118.9–120.2) | 138.2 (137.7–138.6) |
| Cloudflare Realtime | 2.5 ms | 6.3 (5.7–6.9) | 40.4 (40.1–40.7) | 115.5 (115.3–119.4) | 129.4 (129.0–129.5) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 6.4 (5.8–6.8) | 40.4 (39.4–40.8) | 115.4 (114.9–115.6) | 128.7 (128.0–129.8) |
| LiveKit, SFU in each region | 2.5 ms | 1.9 (1.9–2.4) | 32.6 (32.6–32.7) | 109.1 (108.2–111.0) | 127.8 (127.8–128.0) |
| LiveKit, multi-region set | 2.5 ms | 1.9 (1.8–2.4) | 32.8 (32.6–38.1) | 111.2 (110.7–111.2) | 127.9 |
| LiveKit Cloud | 2.5 ms | 15.9 | 58.2 (58.0–58.4) | 131.3 (127.5–132.8) | 140.8 (140.6–141.2) |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 5 ms | 20.7 (19.3–21.2) | 52.9 (46.5–55.6) | 125.3 (125.2–127.3) | 143.6 (141.5–148.0) |
| Needletail, pipelined reader, HTTP/2 (opus) | 5 ms | 20.0 | 50.9 (47.0–54.8) | 125.6 (124.9–126.3) | 144.7 (142.7–146.8) |
| Needletail, pipelined reader, HTTP/3 (opus) | 5 ms | 21.3 (20.6–22.1) | 51.1 (46.9–55.4) | 126.0 (124.9–127.1) | 144.6 (142.8–146.3) |
| Cloudflare Realtime | 2.5 ms | 8.7 (8.1–9.0) | 41.7 (41.4–42.1) | 117.1 (117.1–120.9) | 131.0 (130.5–131.2) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 8.6 (8.5–8.8) | 41.8 (40.9–42.2) | 117.3 (116.5–117.5) | 130.2 (129.8–131.4) |
| LiveKit, SFU in each region | 2.5 ms | 3.9 (3.2–4.0) | 33.9 (33.8–34.0) | 110.4 (109.7–112.3) | 129.2 (129.1–129.5) |
| LiveKit, multi-region set | 2.5 ms | 3.7 (3.2–3.9) | 34.1 (33.9–39.3) | 112.4 (112.0–112.9) | 129.2 (129.2–129.4) |
| LiveKit Cloud | 2.5 ms | 18.0 (18.0–18.4) | 60.6 (60.3–60.9) | 134.0 (130.0–135.2) | 143.3 (143.1–143.7) |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 5 ms | 57.0 (22.3–358.4) | 55.7 (51.5–79.7) | 128.2 (128.1–155.2) | 146.5 (144.6–174.9) |
| Needletail, pipelined reader, HTTP/2 (opus) | 5 ms | 145.2 (46.7–243.7) | 65.1 (51.9–78.3) | 140.2 (127.7–152.6) | 158.7 (146.6–170.7) |
| Needletail, pipelined reader, HTTP/3 (opus) | 5 ms | 159.3 (51.7–266.8) | 65.6 (51.9–79.3) | 140.6 (127.7–153.4) | 157.6 (146.7–168.4) |
| Cloudflare Realtime | 2.5 ms | 14.4 (11.5–222.7) | 42.6 (42.4–43.6) | 122.0 (118.9–124.5) | 134.0 (132.1–134.0) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 12.4 (11.2–237.2) | 42.9 (41.9–43.3) | 118.7 (117.9–125.0) | 133.2 (131.4–133.5) |
| LiveKit, SFU in each region | 2.5 ms | 6.8 (4.6–217.6) | 34.3 (34.0–34.5) | 113.1 (110.6–115.5) | 131.1 (130.7–132.8) |
| LiveKit, multi-region set | 2.5 ms | 6.8 (4.5–296.3) | 34.7 (34.1–39.9) | 112.8 (112.6–119.2) | 130.6 (130.2–132.2) |
| LiveKit Cloud | 2.5 ms | 19.9 (19.0–241.9) | 61.4 (61.2–61.6) | 136.0 (130.8–139.7) | 144.9 (144.2–145.1) |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 5 ms | 43.7 (10.0–345.4) | 9.7 (8.1–31.5) | 9.2 (7.1–36.3) | 7.4 (7.4–35.7) |
| Needletail, pipelined reader, HTTP/2 (opus) | 5 ms | 132.2 (34.2–230.2) | 20.4 (9.9–31.0) | 20.5 (6.8–34.2) | 20.4 (8.6–32.2) |
| Needletail, pipelined reader, HTTP/3 (opus) | 5 ms | 147.2 (39.8–254.7) | 20.6 (10.0–31.3) | 20.8 (7.2–34.3) | 19.3 (8.8–29.8) |
| Cloudflare Realtime | 2.5 ms | 8.0 (4.5–217.4) | 2.0 (1.9–2.7) | 3.2 (2.2–9.2) | 4.3 (2.9–4.6) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 6.0 (4.2–231.3) | 2.2 (2.1–2.3) | 3.0 (2.8–9.5) | 3.3 (3.2–4.8) |
| LiveKit, SFU in each region | 2.5 ms | 4.3 (2.5–214.2) | 1.4 (0.7–1.4) | 1.8 (0.8–7.3) | 3.1 (2.7–4.7) |
| LiveKit, multi-region set | 2.5 ms | 4.2 (2.6–294.2) | 1.4 (0.7–1.5) | 1.7 (0.8–7.7) | 2.5 (2.2–4.2) |
| LiveKit Cloud | 2.5 ms | 3.9 (2.7–226.3) | 2.6 | 2.7 (2.6–8.5) | 3.2 (3.1–4.0) |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, pipelined reader, HTTP/2 (opus) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, pipelined reader, HTTP/3 (opus) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Cloudflare Realtime | 2.5 ms | 0.015 (0.000–0.133) | 0.005 (0.000–0.005) | 0.000 (0.000–0.010) | 0.041 (0.025–0.100) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 0.030 (0.020–0.076) | 0.010 (0.005–0.051) | 0.005 (0.000–0.053) | 0.053 (0.000–0.272) |
| LiveKit, SFU in each region | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit, multi-region set | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit Cloud | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, pipelined reader, HTTP/2 (opus) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, pipelined reader, HTTP/3 (opus) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Realtime | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, SFU in each region | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, multi-region set | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit Cloud | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 5 ms | 373.2 (45.5–1238.6) | 260.0 (27.6–285.8) | 346.1 (57.4–652.0) | 64.5 (51.2–213.0) |
| Needletail, pipelined reader, HTTP/2 (opus) | 5 ms | 808.6 (406.2–1211.0) | 145.4 (38.5–252.4) | 438.1 (335.4–540.9) | 116.0 |
| Needletail, pipelined reader, HTTP/3 (opus) | 5 ms | 781.7 (339.1–1224.2) | 154.9 (48.0–261.8) | 513.8 (329.6–698.0) | 189.9 (80.2–299.7) |
| Cloudflare Realtime | 2.5 ms | 1040.7 (1033.9–1545.2) | 1758.9 (1200.6–2177.7) | 3520.9 (2557.0–3721.3) | 2674.4 (2574.4–4221.0) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 1305.0 (1231.1–1577.9) | 2454.1 (2422.3–2566.9) | 4815.4 (4750.0–5078.8) | 4500.6 (4006.6–5734.2) |
| LiveKit, SFU in each region | 2.5 ms | 178.0 (174.2–179.2) | 163.7 (158.7–169.1) | 187.6 (173.0–202.8) | 177.0 (169.0–202.8) |
| LiveKit, multi-region set | 2.5 ms | 187.5 (181.3–282.1) | 1044.8 (1031.8–1247.3) | 3968.7 (3860.4–3968.7) | 4771.7 (4630.7–4773.7) |
| LiveKit Cloud | 2.5 ms | 450.0 (306.8–723.8) | 440.6 (424.7–469.3) | 807.0 (760.9–1709.6) | 1103.6 (651.4–1556.2) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 5 ms | 128.0 | 128.0 | 128.0 | 128.0 |
| Needletail, pipelined reader, HTTP/2 (opus) | 5 ms | 128.1 | 128.0 | 128.0 | 128.0 |
| Needletail, pipelined reader, HTTP/3 (opus) | 5 ms | 128.1 | 128.0 | 128.0 | 128.0 |
| Cloudflare Realtime | 2.5 ms | 256.0 (255.7–256.0) | 256.0 | 256.0 | 255.9 (255.7–255.9) |
| Cloudflare Stream WHIP/WHEP | 2.5 ms | 255.9 | 256.0 | 256.0 | 255.9 (255.3–256.0) |
| LiveKit, SFU in each region | 2.5 ms | 256.0 | 256.0 | 256.0 | 256.0 |
| LiveKit, multi-region set | 2.5 ms | 256.0 | 256.0 | 256.0 | 256.0 |
| LiveKit Cloud | 2.5 ms | 256.0 | 256.0 | 256.0 | 256.0 |

Chrome mouth to ear at the marker, median (ms):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 79.3 (66.2–86.3) | 120.1 (99.8–122.5) | 189.1 (181.9–198.3) | 245.4 (244.3–245.4) |
| Cloudflare Stream WHIP/WHEP | 89.1 (74.0–91.0) | 115.2 (108.3–120.3) | 196.7 (195.8–212.5) | 218.2 (213.4–271.1) |
| LiveKit, SFU in each region | 65.5 (64.3–76.5) | 101.0 (91.6–111.2) | 176.8 (175.8–177.2) | 202.4 (200.3–401.1) |
| LiveKit, multi-region set | 80.6 (79.4–85.9) | 112.0 (107.5–125.4) | 177.9 (172.6–183.2) | 204.1 (199.7–205.7) |
| LiveKit Cloud | 78.8 (75.7–105.3) | 128.2 (126.5–152.8) | 199.3 (195.4–200.5) | 272.9 (231.2–300.9) |

Chrome jitter buffer, mean (ms):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 20.2 (16.3–21.7) | 25.7 (23.8–29.3) | 26.9 (15.6–28.0) | 32.9 (26.3–41.9) |
| Cloudflare Stream WHIP/WHEP | 24.5 (18.6–27.9) | 19.5 (16.9–25.6) | 25.1 (22.2–31.5) | 32.2 (21.9–45.1) |
| LiveKit, SFU in each region | 14.2 (11.9–17.7) | 19.8 (11.0–29.5) | 14.0 (12.4–14.8) | 21.4 (17.9–118.8) |
| LiveKit, multi-region set | 24.2 (24.0–28.8) | 23.1 (17.3–26.5) | 18.9 (10.6–19.8) | 21.4 (19.7–111.3) |
| LiveKit Cloud | 14.0 (11.4–30.0) | 19.8 (18.8–31.8) | 16.4 (15.3–18.6) | 55.7 (30.1–84.9) |

Chrome concealed samples (%):

| System | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 0.000 | 0.000 (0.000–0.060) | 0.054 (0.035–0.193) | 0.000 (0.000–0.657) |
| Cloudflare Stream WHIP/WHEP | 0.000 (0.000–0.263) | 0.000 (0.000–0.171) | 0.066 (0.000–0.221) | 0.086 (0.000–1.622) |
| LiveKit, SFU in each region | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit, multi-region set | 0.000 (0.000–0.072) | 0.000 (0.000–0.051) | 0.000 | 0.000 |
| LiveKit Cloud | 0.000 (0.000–0.048) | 0.000 | 0.000 | 0.000 |

### Frame length

Mouth to ear, played on arrival, median (ms), London / Sydney:

| System | 2.5 ms | 5 ms | 10 ms | 20 ms | 10 ms at 510 kbit/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped | 12.0 / 138.5 (opus) | 12.1 / 138.7 (opus) | 12.3 / 139.1 (opus) | 11.2 / 138.4 (flac) | 10.1 / 136.5 (flac) |
| Needletail, pipelined reader, HTTP/2 | 12.2 / 138.1 (opus) | 12.8 / 138.5 (opus) | 12.4 / 138.0 (opus) | 15.9 / 142.1 (flac) | 10.9 / 136.9 (flac) |
| Needletail, pipelined reader, HTTP/3 | 12.1 / 138.2 (opus) | 12.5 / 138.7 (opus) | 12.1 / 138.5 (opus) | 15.8 / 142.3 (flac) | 10.8 / 137.1 (flac) |
| Cloudflare Realtime | 6.3 / 129.4 | 7.2 / 137.1 | 10.9 / 140.4 | 16.6 / 175.3 | 10.2 / 149.5 |
| Cloudflare Stream WHIP/WHEP | 6.4 / 128.7 | 8.1 / 129.3 | 10.2 / 134.0 | 17.1 / 139.3 | 11.0 / 133.6 |
| LiveKit, SFU in each region | 1.9 / 127.8 | 3.2 / 129.1 | 6.3 / 132.3 | 12.5 / 138.6 | 6.3 / 132.3 |
| LiveKit, multi-region set | 1.9 / 127.9 | 3.3 / 129.2 | 6.3 / 132.1 | 12.5 / 138.6 | 6.4 / 132.2 |
| LiveKit Cloud | 15.9 / 140.8 | 16.9 / 141.2 | 19.1 / 143.6 | 25.2 / 145.6 | 19.4 / 144.4 |

### Lossless (PCM; SFUs by data channel)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 5 ms | 10.5 (10.3–12.1) | 45.7 (40.4–46.2) | 116.7 (116.5–118.7) | 136.5 (136.2–137.7) |
| Needletail, pipelined reader, HTTP/2 (pcm) | 5 ms | 10.9 (10.4–11.5) | 43.3 (40.4–46.2) | 117.7 (116.8–118.7) | 136.9 (136.2–137.7) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 5 ms | 11.1 (10.2–11.9) | 43.2 (40.0–46.5) | 117.8 (117.2–118.3) | 136.8 (136.1–137.5) |
| Cloudflare Realtime | 2.5 ms | 6.6 (6.2–7.1) | 40.9 (40.4–41.1) | 116.2 (115.6–119.2) | 128.3 (127.5–129.7) |
| LiveKit, SFU in each region | 2.5 ms | 2.5 (2.2–2.6) | 38.0 (32.9–38.0) | 111.1 (108.1–111.3) | 128.1 (128.0–128.8) |
| LiveKit, multi-region set | 2.5 ms | 2.7 (2.5–2.9) | 33.1 (33.0–33.1) | 111.3 (108.3–111.4) | 128.3 (128.3–128.6) |
| LiveKit Cloud | 2.5 ms | 16.1 (15.8–16.6) | 57.7 (57.3–57.9) | 133.0 (127.0–133.3) | 140.7 (136.8–141.4) |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 5 ms | 39.1 (27.8–147.0) | 53.0 (43.8–60.7) | 124.7 (123.4–134.4) | 151.0 (145.7–156.5) |
| Needletail, pipelined reader, HTTP/2 (pcm) | 5 ms | 32.5 (19.9–45.1) | 52.0 (43.9–60.1) | 128.3 (123.3–133.3) | 148.0 (142.1–154.0) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 5 ms | 40.0 (22.3–57.7) | 52.0 (43.4–60.7) | 129.5 (123.3–135.7) | 148.9 (142.5–155.4) |
| Cloudflare Realtime | 2.5 ms | 9.1 (7.8–10.1) | 42.5 (42.0–43.2) | 118.8 (117.5–120.8) | 130.9 (129.1–131.7) |
| LiveKit, SFU in each region | 2.5 ms | 4.9 (3.9–5.1) | 39.3 (34.1–39.3) | 112.7 (109.3–113.0) | 129.8 (129.5–1573.5) |
| LiveKit, multi-region set | 2.5 ms | 6.0 (4.5–6.1) | 35.0 (34.6–35.1) | 113.0 (110.8–114.2) | 131.0 (130.0–131.7) |
| LiveKit Cloud | 2.5 ms | 18.6 (18.3–18.8) | 60.1 (59.6–60.4) | 135.6 (129.5–136.0) | 143.4 (139.5–143.8) |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 5 ms | 263.7 (177.7–336.9) | 70.7 (48.2–102.2) | 141.9 (131.7–178.8) | 198.6 (166.1–211.6) |
| Needletail, pipelined reader, HTTP/2 (pcm) | 5 ms | 129.4 (126.2–132.6) | 74.5 (48.3–100.7) | 151.2 (129.3–173.2) | 175.7 (156.2–195.1) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 5 ms | 114.2 (95.1–133.2) | 75.3 (47.8–102.9) | 153.4 (132.8–174.1) | 190.1 (186.1–194.2) |
| Cloudflare Realtime | 2.5 ms | 12.7 (9.7–15.6) | 44.0 (43.2–44.9) | 122.3 (119.6–122.7) | 134.3 (130.9–136.1) |
| LiveKit, SFU in each region | 2.5 ms | 11.2 (5.7–12.9) | 40.0 (34.4–40.0) | 114.7 (109.8–117.5) | 132.5 (132.4–1664.9) |
| LiveKit, multi-region set | 2.5 ms | 15.1 (7.6–15.6) | 39.2 (36.9–42.4) | 115.9 (115.3–123.4) | 137.3 (133.0–148.2) |
| LiveKit Cloud | 2.5 ms | 23.3 (19.4–25.6) | 60.9 (60.6–61.3) | 137.2 (130.4–139.7) | 144.8 (143.3–146.0) |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 5 ms | 251.5 (167.9–326.2) | 25.2 (8.0–55.8) | 25.8 (12.9–62.5) | 60.6 (29.9–74.3) |
| Needletail, pipelined reader, HTTP/2 (pcm) | 5 ms | 118.9 (114.7–123.1) | 31.2 (8.2–54.3) | 33.5 (10.4–56.7) | 38.7 (20.3–57.1) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 5 ms | 103.1 (84.8–121.5) | 32.3 (8.0–56.6) | 35.7 (14.2–57.1) | 52.7 (49.0–56.3) |
| Cloudflare Realtime | 2.5 ms | 6.1 (3.4–8.4) | 3.0 (2.6–3.7) | 3.8 (2.9–6.3) | 4.7 (3.3–7.8) |
| LiveKit, SFU in each region | 2.5 ms | 8.6 (3.2–10.7) | 1.6 (1.0–1.7) | 3.5 (1.2–6.3) | 4.4 (4.2–1536.1) |
| LiveKit, multi-region set | 2.5 ms | 12.2 (4.6–13.2) | 6.2 (3.6–9.5) | 7.8 (3.7–12.1) | 8.9 (4.4–19.6) |
| LiveKit Cloud | 2.5 ms | 7.0 (2.4–9.8) | 2.9 (2.7–2.9) | 3.9 (2.7–6.2) | 5.2 (3.1–6.5) |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, pipelined reader, HTTP/2 (pcm) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, pipelined reader, HTTP/3 (pcm) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Cloudflare Realtime | 2.5 ms | 0.073 (0.029–0.074) | 0.075 (0.035–0.075) | 0.076 (0.031–0.081) | 0.082 (0.031–0.103) |
| LiveKit, SFU in each region | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit, multi-region set | 2.5 ms | 0.000 | 0.000 | 0.000 | 0.000 (0.000–0.021) |
| LiveKit Cloud | 2.5 ms | 0.000 (0.000–0.019) | 0.000 | 0.000 | 0.000 (0.000–0.200) |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, pipelined reader, HTTP/2 (pcm) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, pipelined reader, HTTP/3 (pcm) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Realtime | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, SFU in each region | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, multi-region set | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit Cloud | 2.5 ms | 100.000 | 100.000 | 100.000 | 100.000 |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 5 ms | 57.4 (50.3–61.4) | 34.6 (29.2–40.0) | 69.7 (43.6–77.4) | 44.7 (39.2–63.6) |
| Needletail, pipelined reader, HTTP/2 (pcm) | 5 ms | 76.5 (75.9–77.0) | 49.5 (38.1–60.9) | 84.8 (52.0–117.6) | 82.8 (62.2–103.3) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 5 ms | 84.0 (70.0–98.1) | 50.9 (42.1–59.7) | 70.2 (57.9–82.5) | 82.2 (73.3–91.1) |
| Cloudflare Realtime | 2.5 ms | 896.5 (804.3–1081.7) | 1630.5 (1491.5–1650.4) | 2741.1 (2592.6–2782.2) | 3210.6 (3112.7–3305.8) |
| LiveKit, SFU in each region | 2.5 ms | 93.2 (74.9–170.2) | 50.5 (30.3–83.9) | 82.8 (43.2–154.6) | 129.4 (57.7–180.8) |
| LiveKit, multi-region set | 2.5 ms | 94.0 (78.6–128.5) | 1161.7 (1131.4–1175.6) | 3978.2 (3797.6–4059.1) | 4841.0 (4659.6–4873.0) |
| LiveKit Cloud | 2.5 ms | 386.4 (296.8–404.2) | 511.4 (362.7–531.7) | 1534.9 (690.7–1647.0) | 1217.1 (1201.3–1668.9) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 5 ms | 2304.0 (2303.9–2304.1) | 2303.9 | 2304.0 (2303.9–2304.1) | 2304.0 (2303.9–2304.1) |
| Needletail, pipelined reader, HTTP/2 (pcm) | 5 ms | 2303.9 | 2304.1 | 2303.9 (2303.8–2304.1) | 2303.9 (2303.8–2304.1) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 5 ms | 2304.1 (2303.8–2304.3) | 2304.0 | 2304.1 (2303.9–2304.2) | 2303.9 (2303.8–2304.1) |
| Cloudflare Realtime | 2.5 ms | 2302.3 (2302.2–2303.3) | 2302.3 (2302.2–2303.1) | 2302.3 (2302.1–2303.1) | 2302.1 (2301.5–2303.2) |
| LiveKit, SFU in each region | 2.5 ms | 2304.0 | 2304.0 | 2303.9 | 2304.0 |
| LiveKit, multi-region set | 2.5 ms | 2304.0 | 2304.0 (2303.9–2304.1) | 2304.0 | 2304.0 (2303.6–2304.0) |
| LiveKit Cloud | 2.5 ms | 2303.9 (2303.6–2303.9) | 2304.0 | 2304.0 (2303.9–2304.1) | 2303.8 (2299.3–2304.2) |

### Needletail FLAC (beside 20 ms Opus on the SFUs)

Mouth to ear, played on arrival, median (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (flac) | 5 ms | 11.2 (10.5–20.9) | 46.2 (46.2–51.0) | 117.2 (117.2–129.2) | 138.4 (137.4–146.6) |
| Needletail, pipelined reader, HTTP/2 (flac) | 5 ms | 15.9 (11.0–20.7) | 48.3 (45.9–50.8) | 122.9 (116.9–128.9) | 142.1 (137.8–146.4) |
| Needletail, pipelined reader, HTTP/3 (flac) | 5 ms | 15.8 (10.9–20.6) | 48.5 (45.9–51.2) | 122.9 (116.9–128.9) | 142.3 (137.8–146.9) |
| Cloudflare Realtime | 20 ms | 16.6 (16.6–18.0) | 51.0 (50.8–51.7) | 126.0 (125.8–126.3) | 175.3 (156.3–178.3) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 17.1 (16.6–17.6) | 50.1 (50.0–50.1) | 123.6 (123.2–126.6) | 139.3 (137.0–146.4) |
| LiveKit, SFU in each region | 20 ms | 12.5 (12.4–12.9) | 43.4 (43.3–48.6) | 121.6 (121.2–121.7) | 138.6 (138.5–138.9) |
| LiveKit, multi-region set | 20 ms | 12.5 (12.4–12.9) | 43.4 | 121.5 (118.0–121.7) | 138.6 (138.5–138.8) |
| LiveKit Cloud | 20 ms | 25.2 (24.6–25.5) | 66.6 (66.5–67.0) | 139.7 (139.4–140.2) | 145.6 (144.7–150.3) |

Mouth to ear, played on arrival, p95 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (flac) | 5 ms | 30.9 (21.4–189.1) | 61.0 (52.5–220.2) | 139.2 (124.9–292.5) | 314.9 (161.4–607.0) |
| Needletail, pipelined reader, HTTP/2 (flac) | 5 ms | 26.4 (21.9–30.9) | 56.6 (52.1–61.1) | 131.7 (124.4–139.1) | 156.9 (155.5–158.2) |
| Needletail, pipelined reader, HTTP/3 (flac) | 5 ms | 26.3 (21.9–30.8) | 56.7 (52.1–61.3) | 131.7 (124.4–139.0) | 156.8 (155.5–158.0) |
| Cloudflare Realtime | 20 ms | 23.5 (22.9–24.9) | 57.3 (57.2–58.1) | 132.5 (132.4–132.7) | 181.5 (163.2–184.8) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 23.8 (23.0–24.3) | 56.4 (56.2–56.4) | 129.9 (129.5–132.8) | 145.4 (143.2–152.6) |
| LiveKit, SFU in each region | 20 ms | 18.6 (18.4–18.9) | 49.4 (49.3–54.6) | 127.6 (127.2–127.8) | 144.6 (144.6–144.9) |
| LiveKit, multi-region set | 20 ms | 18.6 (18.3–18.9) | 49.3 (49.3–49.5) | 127.5 (124.1–127.8) | 144.5 (144.5–144.8) |
| LiveKit Cloud | 20 ms | 32.6 (32.5–33.2) | 75.5 (75.5–76.0) | 148.0 (147.7–148.3) | 154.4 (153.5–159.2) |

Mouth to ear, played on arrival, p99 (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (flac) | 5 ms | 151.5 (34.2–349.6) | 64.0 (55.3–376.7) | 142.5 (128.3–447.2) | 886.7 (512.8–1099.5) |
| Needletail, pipelined reader, HTTP/2 (flac) | 5 ms | 86.0 (34.0–137.9) | 59.3 (54.8–63.9) | 135.0 (127.3–142.7) | 480.5 (198.8–762.2) |
| Needletail, pipelined reader, HTTP/3 (flac) | 5 ms | 46.9 (34.1–59.7) | 59.6 (54.8–64.3) | 135.0 (127.5–142.5) | 484.6 (201.6–767.5) |
| Cloudflare Realtime | 20 ms | 25.5 (24.2–30.3) | 59.0 (58.0–59.1) | 134.4 (133.6–134.8) | 186.0 (182.8–763.5) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 25.4 (23.9–27.1) | 56.8 (56.7–57.1) | 130.9 (130.3–134.1) | 146.3 (144.0–733.3) |
| LiveKit, SFU in each region | 20 ms | 19.6 (18.7–20.7) | 49.6 (49.6–54.9) | 128.4 (127.9–128.8) | 146.0 (145.4–706.7) |
| LiveKit, multi-region set | 20 ms | 19.9 (18.6–20.6) | 49.7 (49.5–49.9) | 127.9 (125.2–128.9) | 145.2 (144.9–750.6) |
| LiveKit Cloud | 20 ms | 33.6 (33.1–35.1) | 76.6 (76.5–77.0) | 149.4 (149.1–149.7) | 155.5 (154.6–846.3) |

Jitter: p99 minus median of the unit latency (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (flac) | 5 ms | 141.2 (12.8–339.7) | 12.5 (8.6–330.2) | 13.0 (10.6–329.7) | 740.1 (375.8–961.3) |
| Needletail, pipelined reader, HTTP/2 (flac) | 5 ms | 69.7 (12.7–126.6) | 10.4 (8.4–12.4) | 11.7 (10.0–13.4) | 338.0 (60.6–615.3) |
| Needletail, pipelined reader, HTTP/3 (flac) | 5 ms | 31.2 (13.0–49.5) | 10.4 (8.4–12.3) | 11.8 (10.2–13.3) | 342.1 (64.2–620.1) |
| Cloudflare Realtime | 20 ms | 6.1 (3.9–12.4) | 2.6 (2.4–5.0) | 3.6 (2.8–7.8) | 3.4 (3.1–606.5) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 3.8 (2.2–6.9) | 1.5 (1.3–1.6) | 2.7 (2.3–3.9) | 2.3 (1.8–586.2) |
| LiveKit, SFU in each region | 20 ms | 3.3 (0.8–5.4) | 1.3 (0.6–1.3) | 2.8 (1.2–3.5) | 4.0 (2.8–557.2) |
| LiveKit, multi-region set | 20 ms | 3.2 (0.8–5.5) | 1.3 (0.7–1.4) | 2.7 (1.3–3.1) | 2.2 (1.9–596.2) |
| LiveKit Cloud | 20 ms | 2.6 (2.1–7.5) | 2.4 (2.1–2.8) | 3.5 (3.0–4.1) | 3.3 (2.5–698.9) |

Units missing (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (flac) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Needletail, pipelined reader, HTTP/2 (flac) | 5 ms | 0.005 (0.000–0.010) | 0.000 | 0.000 | 0.000 |
| Needletail, pipelined reader, HTTP/3 (flac) | 5 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| Cloudflare Realtime | 20 ms | 0.000 | 0.000 | 0.000 (0.000–0.041) | 0.000 |
| Cloudflare Stream WHIP/WHEP | 20 ms | 0.000 (0.000–0.040) | 0.000 (0.000–0.040) | 0.000 | 0.041 (0.000–0.043) |
| LiveKit, SFU in each region | 20 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit, multi-region set | 20 ms | 0.000 | 0.000 | 0.000 | 0.000 |
| LiveKit Cloud | 20 ms | 0.000 | 0.000 | 0.000 | 0.000 (0.000–0.039) |

Units bit-exact (%):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (flac) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, pipelined reader, HTTP/2 (flac) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Needletail, pipelined reader, HTTP/3 (flac) | 5 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Realtime | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| Cloudflare Stream WHIP/WHEP | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, SFU in each region | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit, multi-region set | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |
| LiveKit Cloud | 20 ms | 100.000 | 100.000 | 100.000 | 100.000 |

Time from the listener's start to its first audio (ms):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (flac) | 5 ms | 96.1 (85.1–271.4) | 58.5 (49.3–73.1) | 253.5 (53.4–282.5) | 61.7 (49.3–63.3) |
| Needletail, pipelined reader, HTTP/2 (flac) | 5 ms | 114.0 (91.9–136.0) | 65.5 (54.7–76.3) | 265.4 (259.0–271.9) | 69.5 (54.0–85.0) |
| Needletail, pipelined reader, HTTP/3 (flac) | 5 ms | 132.0 (98.2–165.8) | 64.1 (58.1–70.1) | 240.8 (233.7–247.8) | 74.9 (72.8–77.1) |
| Cloudflare Realtime | 20 ms | 910.3 (891.6–1262.5) | 1893.4 (1802.5–1987.0) | 3542.0 (3450.2–3577.8) | 3572.4 (3539.8–4437.0) |
| Cloudflare Stream WHIP/WHEP | 20 ms | 1295.7 (1271.3–1694.8) | 2236.7 (1853.1–2355.9) | 4272.2 (3275.5–4624.3) | 3554.2 (3453.8–5694.1) |
| LiveKit, SFU in each region | 20 ms | 186.7 (180.6–256.8) | 180.8 (162.0–184.1) | 184.0 (178.0–184.1) | 180.1 (171.1–190.6) |
| LiveKit, multi-region set | 20 ms | 193.4 (171.8–221.1) | 1120.0 (1039.8–1269.6) | 3965.7 (3395.5–3995.5) | 4731.2 (4664.4–4751.2) |
| LiveKit Cloud | 20 ms | 341.6 (323.2–512.8) | 508.9 (450.6–512.3) | 1354.5 (825.5–1478.6) | 914.9 (843.4–1004.8) |

Bit rate at the listener (kbit/s):

| System | Frame | London | New York | Tokyo | Sydney |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (flac) | 5 ms | 1852.6 (1851.6–1858.0) | 1852.6 (1852.2–1855.8) | 1852.7 (1851.6–1857.5) | 1852.6 (1851.9–1860.1) |
| Needletail, pipelined reader, HTTP/2 (flac) | 5 ms | 1852.4 (1851.4–1853.4) | 1852.4 (1852.2–1852.6) | 1852.1 (1851.6–1852.6) | 1852.0 (1851.6–1852.5) |
| Needletail, pipelined reader, HTTP/3 (flac) | 5 ms | 1853.2 (1851.7–1854.6) | 1852.2 (1852.1–1852.4) | 1852.0 (1851.6–1852.5) | 1851.9 (1851.6–1852.3) |
| Cloudflare Realtime | 20 ms | 256.0 (256.0–256.1) | 256.0 (256.0–256.1) | 256.0 | 256.0 |
| Cloudflare Stream WHIP/WHEP | 20 ms | 256.0 (255.9–256.1) | 255.9 (255.8–256.0) | 256.0 (256.0–256.1) | 255.9 (255.9–256.1) |
| LiveKit, SFU in each region | 20 ms | 256.0 | 256.0 | 256.0 (256.0–256.1) | 256.0 |
| LiveKit, multi-region set | 20 ms | 256.0 | 256.1 (256.0–256.1) | 256.0 | 256.0 (256.0–256.1) |
| LiveKit Cloud | 20 ms | 255.9 | 256.0 | 256.0 (255.9–256.1) | 255.9 (255.8–256.0) |

### Impairment, opus-2.5

Units missing at the listener (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| Needletail, pipelined reader, HTTP/2 (opus) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.01 (0.00–0.02) / 0.00 | 0.00 / 0.00 |
| Needletail, pipelined reader, HTTP/3 (opus) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| Cloudflare Realtime | 0.02 (0.00–0.13) / 0.04 (0.03–0.10) | 1.01 (0.86–1.06) / 1.02 (0.89–1.09) | 5.16 (4.65–5.53) / 5.14 (5.01–5.50) | 4.92 (4.16–5.14) / 5.17 (4.83–5.52) | 0.00 (0.00–0.01) / 0.03 (0.00–0.08) |
| Cloudflare Stream WHIP/WHEP | 0.03 (0.02–0.08) / 0.05 (0.00–0.27) | 1.14 (0.83–1.19) / 0.98 | 5.04 (5.03–5.28) / 5.10 (4.58–5.17) | 5.11 (4.96–5.23) / 5.21 (5.08–5.34) | 0.00 (0.00–0.02) / 0.00 (0.00–0.12) |
| LiveKit, SFU in each region | 0.00 / 0.00 | 0.99 (0.94–1.04) / 0.94 (0.80–1.06) | 5.36 (5.09–5.43) / 5.30 (4.88–5.40) | 5.02 (4.44–5.09) / 5.37 (5.05–6.50) | 0.00 / 0.00 |
| LiveKit, multi-region set | 0.00 / 0.00 | 1.02 (0.99–1.05) / 1.11 (0.93–1.34) | 4.97 (4.67–5.01) / 4.93 (4.47–5.33) | 4.88 (4.56–5.10) / 5.25 (5.03–5.47) | 0.00 / 0.00 |
| LiveKit Cloud | 0.00 / 0.00 | 0.96 (0.90–1.01) / 1.04 (0.91–1.21) | 4.94 (4.84–4.97) / 5.05 (4.91–5.08) | 4.77 (4.47–5.01) / 5.15 (5.03–5.26) | 0.00 / 0.00 (0.00–0.01) |

Longest gap (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| Needletail, pipelined reader, HTTP/2 (opus) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 2.5 (0.0–5.0) / 0.0 | 0.0 / 0.0 |
| Needletail, pipelined reader, HTTP/3 (opus) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| Cloudflare Realtime | 2.5 (0.0–20.0) / 5.0 (2.5–5.0) | 5.0 (2.5–5.0) / 5.0 (2.5–5.0) | 7.5 (7.5–20.0) / 10.0 (7.5–10.0) | 12.5 (10.0–15.0) / 15.0 (10.0–20.0) | 0.0 (0.0–2.5) / 2.5 (0.0–5.0) |
| Cloudflare Stream WHIP/WHEP | 5.0 (5.0–20.0) / 5.0 (0.0–20.0) | 5.0 / 2.5 (2.5–5.0) | 7.5 (5.0–7.5) / 7.5 | 17.5 (15.0–17.5) / 16.2 (12.5–20.0) | 0.0 (0.0–5.0) / 0.0 (0.0–5.0) |
| LiveKit, SFU in each region | 0.0 / 0.0 | 5.0 (2.5–5.0) / 5.0 (2.5–7.5) | 7.5 (7.5–10.0) / 7.5 (5.0–7.5) | 20.0 (15.0–22.5) / 20.0 (15.0–25.0) | 0.0 / 0.0 (0.0–2.5) |
| LiveKit, multi-region set | 0.0 / 0.0 | 5.0 (2.5–5.0) / 5.0 | 7.5 (7.5–10.0) / 7.5 | 15.0 (12.5–20.0) / 16.2 (12.5–20.0) | 0.0 / 0.0 |
| LiveKit Cloud | 0.0 / 0.0 | 5.0 (2.5–5.0) / 5.0 | 7.5 / 7.5 | 12.5 (12.5–15.0) / 20.0 (15.0–25.0) | 0.0 / 0.0 (0.0–2.5) |

Mouth to ear, played on arrival, p99 (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 57 (22–358) / 147 (145–175) | 1420 (682–1545) / 998 (800–1473) | 21828 (21797–22674) / 21735 (21627–22565) | 9779 (6312–15921) / 6472 (1993–10695) | 29844 (29736–29970) / 29905 (29874–30052) |
| Needletail, pipelined reader, HTTP/2 (opus) | 145 (47–244) / 159 (147–171) | 597 (36–1157) / 718 (158–1277) | 415 (404–426) / 330 (327–333) | 845 (396–1294) / 464 (375–554) | 733 (729–737) / 855 (842–868) |
| Needletail, pipelined reader, HTTP/3 (opus) | 159 (52–267) / 158 (147–168) | 596 (37–1154) / 724 (158–1290) | 26 (24–28) / 175 (165–185) | 48 (41–55) / 179 (171–187) | 585 (524–647) / 843 (746–940) |
| Cloudflare Realtime | 14 (12–223) / 134 (132–134) | 10 (9–13) / 133 (132–134) | 11 (10–11) / 141 (131–168) | 9 (8–11) / 151 (135–167) | 111 (111–113) / 235 (233–238) |
| Cloudflare Stream WHIP/WHEP | 12 (11–237) / 133 (131–134) | 10 (9–15) / 132 (131–133) | 9 (9–11) / 133 (131–134) | 9 (8–10) / 131 (131–132) | 112 (109–114) / 238 (231–269) |
| LiveKit, SFU in each region | 7 (5–218) / 131 (131–133) | 4 (3–9) / 130 (129–131) | 4 / 131 (130–131) | 4 (3–7) / 130 (130–135) | 104 (103–106) / 232 (231–234) |
| LiveKit, multi-region set | 7 (5–296) / 131 (130–132) | 5 (3–9) / 130 (130–131) | 4 / 131 (130–131) | 4 (3–7) / 130 | 104 (103–106) / 231 (231–241) |
| LiveKit Cloud | 20 (19–242) / 145 | 19 (18–22) / 144 (140–145) | 19 (18–19) / 140 (140–144) | 18 (18–19) / 142 (141–144) | 119 / 245 (244–248) |

Chrome concealed samples (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 0.00 / 0.00 (0.00–0.66) | 4.12 (3.39–4.63) / 3.85 (3.42–4.46) | 17.54 (15.60–19.49) / 19.00 (18.66–19.34) | 14.64 (13.14–15.18) / 14.37 (11.44–15.24) | 0.00 (0.00–0.06) / 0.00 (0.00–0.07) |
| Cloudflare Stream WHIP/WHEP | 0.00 (0.00–0.26) / 0.09 (0.00–1.62) | 3.63 (2.60–4.30) / 3.88 (3.72–4.40) | 17.97 (17.69–18.64) / 18.13 (16.76–18.36) | 10.62 (9.42–11.30) / 10.90 (9.62–11.41) | 0.09 (0.00–0.16) / 0.15 (0.00–0.29) |
| LiveKit, SFU in each region | 0.00 / 0.00 | 0.97 (0.88–1.13) / 1.21 (1.02–1.37) | 1.69 (1.38–1.80) / 1.53 (1.42–1.54) | 2.11 (1.53–2.11) / 1.89 (1.73–2.54) | 0.00 / 0.00 (0.00–0.09) |
| LiveKit, multi-region set | 0.00 (0.00–0.07) / 0.00 | 1.01 (0.72–1.09) / 2.25 (1.88–2.44) | 1.59 (1.37–1.62) / 2.83 (2.83–2.86) | 2.14 (1.73–2.27) / 4.39 (2.77–4.46) | 0.00 (0.00–0.06) / 0.00 (0.00–0.07) |
| LiveKit Cloud | 0.00 (0.00–0.05) / 0.00 | 1.75 (1.66–1.78) / 1.28 (1.19–1.46) | 2.15 (1.90–2.19) / 1.90 (1.54–2.47) | 3.01 (2.73–3.48) / 1.94 (1.33–2.49) | 0.00 / 0.00 |

Chrome mouth to ear at the marker, median (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cloudflare Realtime | 79 (66–86) / 245 (244–245) | 344 (286–431) / 464 (301–477) | 396 (380–413) / 551 (497–606) | 357 (202–365) / 495 (440–555) | 189 (183–203) / 337 (305–641) |
| Cloudflare Stream WHIP/WHEP | 89 (74–91) / 218 (213–271) | 233 (180–376) / 402 (340–464) | 321 (317–527) / 496 (405–688) | 267 (198–275) / 429 (413–541) | 190 (176–196) / 390 (324–637) |
| LiveKit, SFU in each region | 66 (64–77) / 202 (200–401) | 228 (174–235) / 347 (342–357) | 277 (273–281) / 398 (372–415) | 237 (204–248) / 399 (342–412) | 172 (166–181) / 305 (302–310) |
| LiveKit, multi-region set | 81 (79–86) / 204 (200–206) | 195 (168–209) / 449 (440–457) | 294 (278–298) / 500 (477–523) | 262 (221–287) / 395 (285–448) | 176 (175–186) / 307 (296–449) |
| LiveKit Cloud | 79 (76–105) / 273 (231–301) | 195 (192–210) / 321 (320–326) | 287 (280–305) / 413 (406–416) | 209 (202–225) / 333 (330–464) | 180 (179–182) / 308 (304–314) |

### Impairment, pcm

Units missing at the listener (%), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| Needletail, pipelined reader, HTTP/2 (pcm) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.01 (0.00–0.02) / 0.00 | 0.00 / 0.00 |
| Needletail, pipelined reader, HTTP/3 (pcm) | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| Cloudflare Realtime | 0.07 (0.03–0.07) / 0.08 (0.03–0.10) | 1.10 (0.97–1.11) / 0.98 (0.93–1.12) | 5.13 (4.73–5.42) / 5.19 (4.94–5.38) | 5.31 (5.27–5.43) / 4.70 (3.88–5.52) | 0.04 (0.02–0.08) / 0.06 (0.01–0.09) |
| LiveKit, SFU in each region | 0.00 / 0.00 | 0.97 (0.89–1.04) / 0.99 (0.94–0.99) | 5.02 (4.64–5.19) / 5.08 (5.08–5.12) | 5.45 (5.35–5.66) / 4.92 (4.60–5.25) | 0.00 / 0.00 |
| LiveKit, multi-region set | 0.00 / 0.00 (0.00–0.02) | 1.04 (1.00–1.07) / 0.93 (0.85–1.06) | 5.09 (4.97–5.35) / 5.10 (5.00–5.31) | 5.60 (4.60–5.60) / 5.41 (4.90–5.93) | 0.00 / 0.00 (0.00–0.01) |
| LiveKit Cloud | 0.00 (0.00–0.02) / 0.00 (0.00–0.20) | 0.98 (0.97–1.30) / 1.10 (0.89–1.83) | 5.02 (4.75–5.30) / 5.10 (5.02–5.30) | 5.57 (5.34–5.68) / 8.70 (5.66–11.74) | 88.63 (88.62–88.79) / 88.06 (88.04–88.07) |

Longest gap (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| Needletail, pipelined reader, HTTP/2 (pcm) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 2.5 (0.0–5.0) / 0.0 | 0.0 / 0.0 |
| Needletail, pipelined reader, HTTP/3 (pcm) | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| Cloudflare Realtime | 5.0 (2.5–7.5) / 5.0 (2.5–7.5) | 5.0 (5.0–7.5) / 2.5 (2.5–7.5) | 7.5 (5.0–7.5) / 7.5 (5.0–7.5) | 17.5 (15.0–17.5) / 35.0 | 2.5 (2.5–5.0) / 2.5 (2.5–5.0) |
| LiveKit, SFU in each region | 0.0 / 0.0 | 5.0 (2.5–5.0) / 2.5 | 5.0 (5.0–7.5) / 7.5 (5.0–7.5) | 25.0 (12.5–65.0) / 32.5 (17.5–47.5) | 0.0 / 0.0 (0.0–2.5) |
| LiveKit, multi-region set | 0.0 / 0.0 (0.0–2.5) | 5.0 (2.5–5.0) / 5.0 | 7.5 / 7.5 (5.0–7.5) | 22.5 (12.5–30.0) / 13.8 (12.5–15.0) | 0.0 (0.0–2.5) / 0.0 (0.0–2.5) |
| LiveKit Cloud | 0.0 (0.0–10.0) / 0.0 (0.0–90.0) | 5.0 (2.5–47.5) / 5.0 (2.5–197.5) | 7.5 (5.0–7.5) / 7.5 | 22.5 (15.0–25.0) / 773.8 (12.5–1535.0) | 152.5 (152.5–157.5) / 142.5 (122.5–145.0) |

Mouth to ear, played on arrival, p99 (ms), London / Sydney:

| System | none | 1% loss | 5% loss | burst loss | 100 ms delay |
| --- | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 264 (178–337) / 199 (166–212) | 610 (419–851) / 530 (388–764) | 14234 (12541–14685) / 16215 (15882–16589) | 8524 (6424–9296) / 8628 (7475–9781) | 29857 (29820–29919) / 29959 (29890–29972) |
| Needletail, pipelined reader, HTTP/2 (pcm) | 129 (126–133) / 176 (156–195) | 27 (23–32) / 208 (181–234) | 351 (271–431) / 542 (350–733) | 1186 (846–1527) / 1052 (506–1599) | 1129 (944–1315) / 1184 (1106–1262) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 114 (95–133) / 190 (186–194) | 28 (24–33) / 175 (159–191) | 90 (86–93) / 207 (202–212) | 88 (70–106) / 1029 (270–1789) | 1670 (1273–2068) / 1421 (1403–1440) |
| Cloudflare Realtime | 13 (10–16) / 134 (131–136) | 12 (10–26) / 135 (131–135) | 11 (11–14) / 171 (133–8473) | 12 (10–21) / 5022 (1679–8365) | 118 (111–119) / 237 (232–9179) |
| LiveKit, SFU in each region | 11 (6–13) / 132 (132–1665) | 6 (6–33) / 133 (130–1733) | 7 (5–12) / 131 (131–5619) | 18 (6–10998) / 4792 (1624–7961) | 113 (107–125) / 234 (232–236) |
| LiveKit, multi-region set | 15 (8–16) / 137 (133–148) | 9 (7–59) / 135 (132–144) | 12 (6–14) / 132 (132–143) | 26 (8–6562) / 884 (138–1631) | 114 (111–138) / 239 (235–264) |
| LiveKit Cloud | 23 (19–26) / 145 (143–146) | 19 (19–52) / 146 (145–147) | 19 (19–25) / 145 (144–147) | 19 (18–28) / 289 (142–436) | 125 (119–130) / 245 (242–251) |

### Listeners, opus-2.5

Listeners with audio, of those started in the region:

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, pipelined reader, HTTP/3 (opus) | 1/1, 1/1 | 3/3, 3/3 | 2/2, 2/2 | 25/25, 25/25 | 25/25, 25/25 | 25/25, 25/25 | 25/25, 25/25 |
| Cloudflare Realtime | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 24/25, 25/25, 25/25 | 22/25, 24/25, 25/25 | 25/25, 25/25, 25/25 | 18/25, 22/25, 25/25 |
| Cloudflare Stream WHIP/WHEP | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 23/25, 25/25, 25/25 | 24/25, 25/25, 25/25 | 16/25, 25/25, 24/25 |
| LiveKit, SFU in each region | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit, multi-region set | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 9/25, 25/25, 25/25 | 2/25, 25/25, 25/25 |
| LiveKit Cloud | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 |

Unit latency, median listener's median (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 9.1 (8.1–9.5) | 15.0 (14.4–15.4) | 142.9 (140.6–143.3) | 3158.9 (803.2–4061.0) | 5061.6 (4891.2–6158.7) | 4852.6 (2031.5–4882.4) | 9115.6 (5701.5–10198.5) |
| Needletail, pipelined reader, HTTP/3 (opus) | 9.3 (8.6–10.0) | 14.9 (13.7–16.2) | 142.9 (140.7–145.1) | 19.9 (19.3–20.6) | 54.0 (51.7–56.3) | 126.8 (126.1–127.5) | 158.6 (151.3–165.9) |
| Cloudflare Realtime | 5.2 (4.4–5.3) | 4.5 (4.1–5.2) | 146.6 (128.2–146.9) | 5.4 (5.0–5.5) | 39.6 (38.9–39.8) | 115.3 (114.3–115.7) | 129.2 (129.1–165.0) |
| Cloudflare Stream WHIP/WHEP | 4.6 (3.2–5.1) | 4.5 (4.0–5.2) | 127.9 (127.0–128.8) | 5.3 (4.6–6.2) | 39.5 (38.3–39.9) | 115.1 (114.2–116.3) | 128.4 (128.2–129.5) |
| LiveKit, SFU in each region | 1.0 (0.8–1.1) | 1.0 (0.8–1.1) | 127.0 (126.7–127.0) | 1.6 (1.5–1.8) | 32.0 (31.8–32.1) | 110.2 (107.2–110.4) | 127.5 (127.3–127.8) |
| LiveKit, multi-region set | 1.0 (0.8–1.1) | 1.1 (0.8–1.1) | 127.0 (126.9–127.2) | 2.1 (2.0–2.3) | 33.3 (33.1–33.6) | 110.3 (109.8–110.9) | 128.0 (127.9–128.4) |
| LiveKit Cloud | 14.6 (14.2–15.1) | 14.7 (14.5–15.2) | 139.9 (136.0–139.9) | 14.9 (14.8–15.5) | 57.5 (57.1–57.6) | 130.0 (129.8–132.5) | 140.8 (140.1–140.9) |

Unit latency, worst listener's p99 (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 20 (13–55) | 55 (30–587) | 720 (155–3150) | 4079 (1899–6151) | 6733 (6059–9333) | 6445 (2845–7388) | 16517 (8598–20731) |
| Needletail, pipelined reader, HTTP/3 (opus) | 60 (19–101) | 56 (20–93) | 259 (188–330) | 53 (47–59) | 98 (95–102) | 154 (152–156) | 3172 (269–6075) |
| Cloudflare Realtime | 7 (7–8) | 8 (7–21) | 169 (169–174) | 17 (13–18) | 45 (45–47) | 129 (121–133) | 3043 (171–3654) |
| Cloudflare Stream WHIP/WHEP | 7 | 10 (8–23) | 133 (132–164) | 14 (13–16) | 46 (44–46) | 128 (125–130) | 3378 (136–3864) |
| LiveKit, SFU in each region | 2 | 2 | 138 (130–168) | 7 (6–485) | 36 (36–37) | 114 (112–115) | 641 (138–2261) |
| LiveKit, multi-region set | 2 | 65 (24–72) | 197 (189–198) | 1057 (995–1140) | 1090 (1033–1169) | 1245 (1101–1857) | 1271 (1122–1879) |
| LiveKit Cloud | 16 | 17 | 145 (139–177) | 20 (19–21) | 63 | 136 (135–137) | 509 (146–1741) |

Units missing (%):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (opus) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Needletail, pipelined reader, HTTP/3 (opus) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.03 (0.00–0.07) |
| Cloudflare Realtime | 0.00 | 0.02 (0.00–0.05) | 0.05 (0.01–0.07) | 0.00 | 0.00 | 0.00 | 0.03 (0.00–0.04) |
| Cloudflare Stream WHIP/WHEP | 0.01 (0.00–0.04) | 0.00 (0.00–0.03) | 0.07 (0.00–0.09) | 0.01 | 0.00 | 0.00 (0.00–0.02) | 0.08 (0.03–0.21) |
| LiveKit, SFU in each region | 0.00 | 0.00 | 0.00 (0.00–0.01) | 0.00 (0.00–2.85) | 0.00 | 0.00 | 0.00 (0.00–0.07) |
| LiveKit, multi-region set | 0.00 | 0.00 | 0.00 | 8.00 (7.70–13.43) | 7.91 (7.34–13.23) | 9.12 (8.65–10.20) | 9.72 (9.26–10.39) |
| LiveKit Cloud | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

### Listeners, pcm

Listeners with audio, of those started in the region:

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| Needletail, pipelined reader, HTTP/3 (pcm) | 1/1, 1/1 | 3/3, 3/3 | 2/2, 2/2 | 25/25, 25/25 | 25/25, 25/25 | 25/25, 25/25 | 25/25, 25/25 |
| Cloudflare Realtime | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 23/25, 23/25, 25/25 |
| LiveKit, SFU in each region | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 | 25/25, 25/25, 25/25 |
| LiveKit, multi-region set | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 19/25, 25/25, 25/25 | 20/25, 25/25, 25/25 | 1/25, 25/25, 24/25 | 0/25, 22/25, 23/25 |
| LiveKit Cloud | 1/1, 1/1, 1/1 | 3/3, 3/3, 3/3 | 2/2, 2/2, 2/2 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 | 24/24, 24/24, 24/24 |

Unit latency, median listener's median (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 54.1 (13.6–57.2) | 54.4 (18.4–57.6) | 181.1 (145.9–183.7) | 12647.4 (11126.4–12788.0) | 12833.6 (11550.0–14676.5) | 12090.5 (9501.8–12685.7) | 11797.5 (10039.0–15620.5) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 35.3 (13.3–57.4) | 40.5 (23.5–57.4) | 166.2 (149.3–183.1) | 46.2 (32.6–59.7) | 80.9 (69.9–91.8) | 154.1 (138.5–169.7) | 182.2 (163.2–201.1) |
| Cloudflare Realtime | 5.5 (4.8–5.8) | 4.8 (4.6–5.4) | 128.9 (126.8–3526.2) | 7.4 (6.5–8.2) | 41.4 (40.9–41.9) | 117.6 (115.9–118.9) | 136.4 (130.7–4547.8) |
| LiveKit, SFU in each region | 1.1 (1.0–1.3) | 1.1 (1.1–1.3) | 127.2 | 3.5 (3.2–1464.3) | 38.9 (34.1–39.0) | 109.1 (108.9–111.7) | 129.5 (129.1–131.5) |
| LiveKit, multi-region set | 1.1 (1.0–1.3) | 1.5 (1.3–1.6) | 127.4 (127.3–127.6) | 6485.8 (6347.5–7095.2) | 6487.2 (6438.9–7165.5) | 6586.4 (6337.3–7246.3) | 6839.4 (6498.9–7179.8) |
| LiveKit Cloud | 14.8 (14.6–15.0) | 14.5 (14.2–15.0) | 140.6 (135.8–140.6) | 15.5 (15.5–16.1) | 57.9 (57.2–58.1) | 130.9 (127.1–133.0) | 137.3 (136.7–141.9) |

Unit latency, worst listener's p99 (ms):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 56 (43–66) | 81 (71–164) | 1330 (209–3605) | 17560 (16732–18234) | 18457 (17648–20699) | 16512 (14619–17501) | 17349 (16339–21278) |
| Needletail, pipelined reader, HTTP/3 (pcm) | 97 (67–128) | 160 (77–243) | 286 (190–382) | 78 | 120 (113–127) | 185 (172–198) | 2043 (260–3826) |
| Cloudflare Realtime | 8 (7–10) | 9 (8–10) | 160 (131–8894) | 31 (27–33) | 69 (68–79) | 148 (146–5962) | 1066 (193–9245) |
| LiveKit, SFU in each region | 2 | 3 (2–4) | 143 (132–160) | 32 (32–1957) | 73 (68–77) | 141 (137–145) | 165 (162–1263) |
| LiveKit, multi-region set | 2 (2–120) | 70 (15–86) | 208 (177–862) | 16275 (12780–18823) | 15980 (13442–16090) | 27419 (11192–28369) | 21449 (15747–27151) |
| LiveKit Cloud | 17 | 17 | 150 (144–183) | 38 (35–42) | 83 (77–91) | 150 (146–153) | 168 (165–178) |

Units missing (%):

| System | 1, London | 10: London | 10: Sydney | 100: London | 100: New York | 100: Tokyo | 100: Sydney |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Needletail, reader as shipped (pcm) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Needletail, pipelined reader, HTTP/3 (pcm) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| Cloudflare Realtime | 0.11 (0.01–0.15) | 0.05 (0.02–0.23) | 0.82 (0.05–1.03) | 0.20 (0.02–0.21) | 0.15 (0.02–0.16) | 0.16 (0.02–0.23) | 0.26 (0.12–0.37) |
| LiveKit, SFU in each region | 0.01 (0.00–0.01) | 0.00 | 0.41 (0.03–0.57) | 0.01 (0.00–0.11) | 0.00 | 0.00 (0.00–0.08) | 0.23 (0.07–0.31) |
| LiveKit, multi-region set | 0.00 | 0.00 | 0.10 (0.06–0.53) | 0.03 (0.01–0.20) | 0.00 | 0.00 | 0.14 (0.11–0.17) |
| LiveKit Cloud | 0.00 | 0.00 | 0.36 (0.00–0.40) | 0.04 (0.00–0.04) | 0.00 | 0.00 (0.00–0.05) | 0.05 (0.01–0.11) |

### Processor of the Nanodes, opus-2.5

Percent of one vCPU used by the service, during the step:

| Node | 1 per host | 10 | 100, first pass | 100, second pass |
| --- | ---: | ---: | ---: | ---: |
| Needletail contributor | 38 (35–42) | 45 (34–56) | 39 | 39 |
| Needletail backbone relay | 25 (22–29) | 25 (22–28) | 34 (27–41) | 24 (23–24) |
| Needletail distributor, London | 15 (13–16) | 16 (13–19) | 16 (15–18) | 17 (14–20) |
| Needletail edge 1, London | 8 (5–11) | 7 (1–13) | 29 (1–57) | 39 (1–78) |
| Needletail edge 2, London | 9 (7–10) | 8 (1–14) | 27 (1–54) | 35 (1–69) |
| Needletail edge 1, Sydney | 9 (6–11) | 6 (2–11) | 20 (2–38) | 33 (2–64) |
| LiveKit, London Nanode (both servers) | 12 | 9 | 24 | 78 (78–79) |
| LiveKit, Sydney Nanode (both servers) | 5 (5–6) | 6 | 20 (18–22) | 3 |

### Processor of the Nanodes, pcm

Percent of one vCPU used by the service, during the step:

| Node | 1 per host | 10 | 100, first pass | 100, second pass |
| --- | ---: | ---: | ---: | ---: |
| Needletail contributor | 35 (31–40) | 40 (32–47) | 33 (30–35) | 32 (29–36) |
| Needletail backbone relay | 21 | 18 (16–20) | 26 (16–37) | 20 (16–24) |
| Needletail distributor, London | 23 (20–25) | 21 | 21 (20–23) | 21 (20–23) |
| Needletail edge 1, London | 25 (22–27) | 21 (20–22) | 40 (31–49) | 60 (59–61) |
| Needletail edge 2, London | 31 (28–34) | 18 | 33 (27–39) | 57 (55–59) |
| Needletail edge 1, Sydney | 38 (37–39) | 11 (2–19) | 22 (2–43) | 32 (2–63) |
| LiveKit, London Nanode (both servers) | 65 | 20 | 73 | 97 |
| LiveKit, Sydney Nanode (both servers) | 13 | 14 (14–15) | 72 (67–77) | 7 (6–8) |

