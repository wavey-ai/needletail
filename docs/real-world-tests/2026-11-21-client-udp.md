# 21 November 2026: RaptorQ FEC UDP to the client

A client either tails LL-HLS over HTTP, or receives RaptorQ FEC UDP from an
edge. The second mode delivered nothing in the comparison of 21 November
(`2026-11-21-audio-against-cloudflare-and-livekit.md`, "Not measured"):
`aep1-48k-probe receive-udp` at a distributor's relay port, and
`receive-webtransport` at a distributor, reported 0 of 2,000 epochs. This
record says why, what was built, and how it was proved.

## Result

UDP is a working client mode at an edge. A client is assigned an edge
exactly as an LL-HLS reader is, reads the playlist of its stream from that
edge, subscribes by UDP, and receives every part of the stream bit for bit:
4K video (fMP4, H.264), SoundKit Opus, FLAC and 24-bit PCM, with 1 % and 5 %
random loss recovered by FEC, and through the drain of its edge.

| Lab | Steps | Streams | Parts received by UDP | Missing | Identical to LL-HLS |
| --- | --- | --- | ---: | ---: | ---: |
| Local, edges of `regional-llhls-cache` (before) | no loss | 4 | 0 | all | - |
| Local, edges of `client-udp` (after) | 0 %, 1 %, 5 %, drain | 4 | 47,700 | 0 | all |
| Linode, dedicated, London (after) | 0 %, 1 %, 5 %, drain | 4 | 107,815 | 0 | all |

## Why the client received nothing

Two things, one in the measurement and one in the product.

1. **The zero of the comparison was the probe.** `aep1-48k-probe` took
   `--session-id`, the start time of the session, as the start of its
   measurement: it listened until `session_id + duration + tail`. The
   comparison started its probes a minute or more after the session
   started, so each probe was acknowledged by the distributor and then
   stopped at once, reporting every epoch missing and no datagram received.
   The relay path itself delivered every epoch. Reproduced locally with the
   DAW source: a probe 15 s late returned in 76 ms with 0 of 2,000; an
   on-time probe at the same distributor received every epoch. The same was
   true of `receive-webtransport` (0 of 1,000 late, 1,000 of 1,000 fixed).
   The compare session reached the same conclusion on its dedicated lab
   (`cmpd1`): with a correctly armed probe, no epoch missing in London and
   Sydney, about 4 ms to London and 130 ms to Sydney.
2. **An edge had no way to send anything to a UDP client.** The native
   audio relay of a node forwards only AEP1 datagrams that arrive on its own
   relay socket. In the regional cache deployment only the backbone relays
   and the distributors are on the relay lane; an edge receives its media
   as cache-mesh objects, never as AEP1. A UDP client of an edge was
   acknowledged and then received nothing, and there was no video at all on
   that lane. Mode 2 of the product did not exist at the role that serves
   clients.

Four code tracers, one per hypothesis (relay path, session id and decoding,
edge role, network and NAT), each checked by two skeptics, agreed: the relay
path, the session ids and the network were refuted; the late probe window was
sufficient alone for the zero; the edge had no data path for UDP clients.

## What was built

All on branches `client-udp`; nothing on `main`.

- **Probe** (`av-contrib` `f05298a`): the window of `receive-udp` and
  `receive-webtransport` starts at the later of the session start and the
  moment of joining, and only epochs captured inside it are counted (a report
  had also counted the tail: 2,600 "received" of 2,000).
- **Edge client UDP** (`av-mesh` `8caa413`, `8eed26c`): `--client-udp-bind`
  on any distributor or edge.
  - The edge sends each part that its cache holds, the same bytes as its
    LL-HLS part, as one RaptorQ object (`raptorq-datagram-fec`): source
    symbols sized to the part, and repair symbols. The block id is the part
    number; the object carries the part number, the times of availability
    and publication, a CRC-32 and the number of datagrams sent. fMP4 streams
    get their init segment; a part that the edge never received is sent as a
    gap.
  - One sender for each stream follows the cache as an LL-HLS request for
    the newest part does (same wait, same hole repair from the parents) and
    encodes each part once for each repair policy that its subscribers asked
    for.
  - A subscription is reader demand: it subscribes the edge to the playlist
    at its distributors and keeps it held. A draining edge, or one that has
    lost its parents, sends `MOVE`; a moving client names the part that it
    needs next and resumes there if the region still holds it.
  - A client measures its datagram loss (each object says how many datagrams
    were sent) and reports it; the edge gives each part the exact binomial
    repair that keeps its loss below 1e-7 at that loss.
  - Media goes only to an address that echoed a token, a keyed hash of the
    address and the minute. A first request carries padding, so the token
    answer is never larger than the request: a forged source address cannot
    make an edge a reflector.
  - The assignment API names the client UDP port and the counters of the
    service.
- **`needletail-udp-client`** (`av-mesh`): the client. Assignment from an
  entry point, the playlist from the assigned cache, subscription by UDP,
  adaptive repair, a move on `MOVE`, `REFUSED`, no answer or silence, a new
  decoder for each edge, `--verify-http` to compare every part with the same
  edge's LL-HLS part, and a JSON report.
- **Needletail** (this branch): `scripts/client-udp-lab.py` (the end-to-end
  test in the local lab), `scripts/linode-client-udp.py` (the same steps on
  Linode), `deploy/linode-small-mesh/topology-client-udp.json`, the deploy
  setting `client_udp_port` (default 19446), and `linode-small-mesh-lab.py`
  now refuses any plan that is not dedicated (it allowed only Nanode and
  g6-standard for mesh nodes).

### The client protocol

One line of text per control datagram (`NTU/1 ...`):

| from | message |
| --- | --- |
| client | `SUB <stream> <from part or -> <repair ‰> <min repair> <loss ppm> <token or -> [padding]` |
| client | `UNSUB <stream> <token>` |
| edge | `TOKEN <stream> <token>`, `OK <stream> <next part>`, `WAIT <stream> <next part>`, `REFUSED <stream> <reason>`, `MOVE <stream> <reason>` |

A client repeats `SUB` every 2 s (a subscription lives 10 s) and the edge
answers each with `OK`; a quiet stream gets `WAIT` every 300 ms. Media
datagrams are RaptorQ datagrams, one object per part. One socket carries one
stream.

A listener today:

    needletail-udp-client --entry https://<any node>:19444 --stream 3001 \
        --tls-ca ca.crt --server-name mesh.needletail.test --duration-seconds 60 \
        [--verify-http] [--write-parts out.bin]

## Tests

- `av-contrib`: `a_receiver_that_joins_a_running_session_listens_for_its_whole_duration`
  fails on `86ad98b` (window anchored to the session) and passes on
  `f05298a`. All 42 probe tests pass.
- `av-mesh`: `client_udp` unit tests (8: protocol, token size, exact repair
  for parts of 1 to 340 symbols at 0.1 % to 10 % loss, symbol sizing, a 400 kB
  part decoded bit for bit at the loss its repair allows, two edges' symbols
  never producing wrong bytes, loss measurement) and
  `an_edge_sends_its_cached_parts_to_a_udp_client_bit_for_bit` (token
  handshake, every cached part and the init segment identical to the LL-HLS
  responses with a third of the datagrams lost, a new part following live,
  `MOVE` on drain). The whole suite passes: 51 library, 193 binary (3
  ignored as before).
- `needletail`: `scripts/client-udp-lab.py run`, the end-to-end test below.
  Against the edges of `regional-llhls-cache` (`av-mesh` `07e7d4a`, the only
  UDP port that an edge had: 12001) it fails: no part reaches any UDP
  client, each moves on silence 9 to 10 times in 15 s, while LL-HLS readers
  of the same streams miss nothing. Against `client-udp` it passes.

## Local lab

`edge_lab.py`: two regions in network namespaces, each service in a cgroup
of one CPU and 1 GB (the contributor four CPUs). Sources: 4K over SRT, the
DAW test source (Opus 3001, FLAC 2001), the AEP1 sender (PCM 4001). Entry
point `lon-dist-a`; clients on `lon-readers`, LL-HLS readers beside them on
`lon-readers-2`. Loss is netem on the client host's link in both directions.
20 s per step. Evidence: `evidence/local-20261121-client-udp-before.json`,
`evidence/local-20261121-client-udp-after.json`.

| Step | Stream | Received | Missing | Recovered by FEC | Measured loss | p50 publish to arrival |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 0 % | Opus / FLAC / PCM / 4K | 3,958 / 3,988 / 3,924 / 95 | 0 | 2 / 1 / 1 / 1 | 0 | 6.3 / 5.7 / 5.6 / 74 ms |
| 1 % | Opus / FLAC / PCM / 4K | 3,997 / 3,997 / 3,995 / 101 | 0 | 30 / 36 / 78 / 89 | 1.0 % | |
| 5 % | Opus / FLAC / PCM / 4K | 3,995 / 3,986 / 3,874 / 100 | 0 | 209 / 271 / 376 / 100 | 4.9 to 5.0 % | 6.5 / 5.8 / 5.1 / 87 ms |
| drain | Opus / FLAC / PCM / 4K | 3,980 / 3,985 / 3,983 / 99 | 0 | | 0 | the 4K client moved, nothing lost |

Every part received was identical to the edge's LL-HLS part. No LL-HLS
reader beside them missed a part.

## Linode

One short run, 21 November 18:35 to 18:55 UTC, run id `cudp1`, every node
dedicated:

| Node | Plan | Region |
| --- | --- | --- |
| contrib-lon | g6-dedicated-4 | gb-lon |
| relay-primary | g6-dedicated-2 | gb-lon |
| relay-secondary | g6-dedicated-2 | nl-ams (the relay plan needs the backbone relays in distinct failure domains) |
| lon-dist-a, lon-dist-b | g6-dedicated-2 | gb-lon |
| lon-edge-1, lon-edge-2 | g6-dedicated-2 | gb-lon |
| lon-load (sources and clients) | g6-dedicated-4 | gb-lon |

Steal was 0.00 to 0.03 % on every node. The load host sent 4K over RTMP
(the Linux contributor has no SRT), the DAW source and the AEP1 PCM sender
to the contributor, and ran one UDP client and one LL-HLS reader for each
stream against the entry point `lon-dist-a` at its public address. Loss was
a netem on the ingress of the load host that matched only datagrams from the
edges' client UDP port. 45 s per step, under the lab watchdog; the lab was
deleted at once (0.33 h, about $0.54 of instance time). Evidence:
`evidence/20261121T184928Z-client-udp.json`.

| Step | Stream | Received | Missing | Recovered by FEC | Measured loss | p50 publish to arrival | p50 edge to client |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 % | Opus | 8,505 | 0 | 1 | 0.01 % | 4.5 ms | 0.18 ms |
| 0 % | FLAC | 8,519 | 0 | 0 | 0 | 4.6 ms | 0.36 ms |
| 0 % | PCM | 8,567 | 0 | 0 | 0 | 4.1 ms | 0.25 ms |
| 0 % | 4K | 221 | 0 | 0 | 0 | 64 ms | 18 ms |
| 1 % | Opus | 8,910 | 0 | 83 | 1.03 % | 4.2 ms | 0.0 ms |
| 1 % | FLAC | 8,909 | 0 | 131 | 1.03 % | 4.2 ms | 0.22 ms |
| 1 % | PCM | 8,901 | 0 | 173 | 1.02 % | 3.8 ms | 0.03 ms |
| 1 % | 4K | 226 | 0 | 214 | 1.01 % | 58 ms | 18 ms |
| 5 % | Opus | 8,999 | 0 | 415 | 4.95 % | 4.2 ms | 0.07 ms |
| 5 % | FLAC | 9,000 | 0 | 621 | 5.09 % | 4.3 ms | 0.19 ms |
| 5 % | PCM | 8,996 | 0 | 900 | 4.89 % | 3.6 ms | 0.11 ms |
| 5 % | 4K | 226 | 0 | 225 | 4.92 % | 59 ms | 17 ms |
| drain | Opus / FLAC / PCM | 9,001 / 9,003 / 9,000 | 0 | | 0 | 3.5 to 4.5 ms | |
| drain | 4K | 226 | 0 | | 0 | 53 ms | 13 ms |

- Every part received by UDP was identical to the same edge's LL-HLS part
  (107,815 parts checked, 0 different, 0 HTTP errors).
- Every subscription was served by an edge: `lon-edge-1` 6, `lon-edge-2` 11,
  the distributors none.
- The drain of `lon-edge-1` 15 s into the step moved the 4K client to
  `lon-edge-2`, which resumed at part 829, the part it needed; no part was
  lost. The audio clients had been assigned `lon-edge-2`.
- LL-HLS readers beside the UDP clients missed no part. In the first step
  they had 15 to 24 short stalls (longest 339 ms) while the sources started;
  none after.
- "p50 edge to client" is from the part's arrival in the edge's cache to its
  arrival at the client; for 4K it includes encoding a 400 kB part. The
  publish-to-arrival p99 of the first step (250 to 300 ms) is the start of
  the sources; in the other steps it is 48 to 56 ms for audio.

## Cost on the wire

A 5 ms audio part is one or two symbols, so repair is most of its datagrams:
with the default floor of three repair symbols the wire carries 2.5 (PCM) to
4 (Opus) times the source datagrams with no loss, and 5 to 7 times at 5 %
loss. 4K is 1.25 times. Symbols are sized to the part, so a datagram of an
Opus part is about 130 bytes, not 1,200. A client on a clean path may ask for
`--min-repair 1`; the adaptive repair raises it when loss appears.

## Adversarial review

A review of the new code by three reviewers, each finding checked by two
skeptics, found 23 surviving findings. Fixed in `8eed26c`: spoofed
subscriptions (reflection), unbounded admission, symbols of two edges mixed
in one decoder after a move, a sender blocked 3 s by a missing part (all
clients moving at once), a lost `OK` never re-sent, a resume part ignored, no
`UNSUB` on a move, repair short of its target for parts of 10 to 60 symbols,
one client's repair raised for all, audio padded to 1,200-byte symbols, loss
not counted for undecoded blocks, missing parts uncounted at the start, a
sender that fell out of the window never catching up, and a client that
stopped on a transient error. Still open below.

## Still open

- **WebTransport as a browser's carrier** of the same objects: not built. A
  browser cannot receive this lane yet.
- **Pacing.** A 4K part goes out as one burst of about 400 datagrams, and the
  catch-up of a resuming client as a burst of its parts. On a slow access
  link that is loss that FEC must repair.
- **Edge admission.** UDP subscriptions are not counted in the edge's egress
  admission or session load, so assignment can overcommit an edge with UDP
  clients.
- **Unknown stream ids** take a playlist slot for a while, as an HTTP
  playlist request for them does; with the token this needs a client that
  receives at its address.
- **The DAW and AEP1 lanes at a distributor** (WAVEY-DAW-SUBSCRIBE,
  WAVEY-AUDIO-EPOCH) are unchanged; they work, but carry only audio epochs and
  are not offered at edges.
- **Scale.** One client per stream here. The sender is one task per stream
  that sends serially; many 4K subscribers of one edge were not measured.
- **Loss shape.** Only random loss was tried. Burst loss longer than a part's
  repair loses that part.

## Cut-through at the edge (second round, same day)

The owner then asked for the UDP client mode to forward datagrams as they
arrive (source symbols first, no wait for a block), to carry 2.5 ms Opus and
parts of 1 ms or less, and to have no timer or batching on its hot path.

**What changed** (`av-mesh` `bcc885f` to `7497fc2`, `playlists` `client-udp`
`a225609`):

- `playlists` gives the cache mesh a receive observer: told of every
  datagram before it is decoded, and of every block once it decodes.
- The cache mesh carries a part as one systematic RaptorQ block of a chunk
  frame, so each source symbol is a slice of the frame. When one arrives at
  the edge, the edge sends it on at once to the clients of the stream, cut
  into client symbols (a divisor of the mesh symbol that fits a client
  datagram: 16 kB 4K symbols go out as 1,000-byte symbols, so nothing is IP
  fragmented on the client's path; a part in one symbol goes out at its own
  size). This runs inline on the mesh receive task with non-blocking sends:
  no timer, no queue, no batching.
- When the edge has decoded the block it sends each client the symbols that
  it never received and the repair of the client's current policy, from
  the same frame in the same symbols, on a blocking thread. If the other
  distributor's block of the part decodes first, that block is sent whole.
  The cache sender skips the parts that cut-through carries.
- The client decodes a cut-through object (a chunk frame) to the LL-HLS
  part bytes with the edge's own slot rules, and reads the publication time
  from the part's media object. It keeps one decoder per block id space.
- `--client-udp-cut-through false` (deploy `client_udp_cut_through`) turns
  it off.

**Small parts** (`small_parts_reach_a_udp_client_cut_through_at_their_rate`:
a distributor and an edge in one process, a real cache mesh over loopback,
parts published at their rate, a UDP client). Every part arrived identical
to the LL-HLS part, all by cut-through:

| Parts | Rate | Publish to client p50 | p99 | Edge's share (first datagram at the edge to client) |
| --- | ---: | ---: | ---: | ---: |
| 2.5 ms Opus | 400/s | 0.51 to 0.69 ms | 0.70 to 0.94 ms | about 0.1 ms |
| 1 ms PCM | 1,000/s | 0.49 to 0.65 ms | 1.1 to 1.5 ms | about 0.1 ms |
| 0.25 ms PCM | 4,000/s | 12 to 177 ms | 39 to 357 ms | 0.03 ms |
| 4K, one frame | 24/s | 2.8 to 4.1 ms | 3.2 to 7.6 ms | 1.6 ms (the rest of the part arriving) |

At 4,000 parts/s the parts queue at the distributor, before they reach the
edge. That is the distributor's publication path, owned by the latency
round (its wave 2 targets it). The producers still make 5 ms parts; 2.5 ms
Opus end to end waits for that round's `--epoch-samples`.

**Local lab**: the end-to-end test passed twice more with cut-through
(4 streams, 0 %, 1 %, 5 % loss and a drain: about 63,000 parts, none missing,
every one identical to LL-HLS). Evidence:
`evidence/local-20261121-client-udp-cut-through.json`. The local lab is too
loaded to show the fraction of a millisecond that cut-through saves on
audio.

**Linode**, a second short run, run id `cudp2`, the same topology, every
node dedicated, 19:30 to 19:55 UTC (0.40 h, about $0.54):

- The first two runs failed: 1 and 2 4K parts missing at 5 % and 1 % loss.
  A second adversarial review found the cause: the client kept one RaptorQ
  decoder for every block id, and a decoder forgets blocks far below the
  last one it decodes, so an init segment or a part sent whole threw away
  the 4K parts being cut through. Fixed in `7497fc2`, with a test that fails
  without the fix; the same review's other findings were fixed with it.
- Then two runs passed every step: 4K, Opus, FLAC and PCM, 0 %, 1 % and 5 %
  loss and a drain, no part missing, every part identical to LL-HLS
  (`evidence/20261121T194453Z-client-udp.json`,
  `evidence/20261121T194816Z-client-udp.json`). The edge sent 117,632 parts
  by cut-through in one run, 3,103 of them completed from the other
  distributor's block; no send found its socket full.
- Then the same instances with cut-through off
  (`evidence/20261121T195150Z-client-udp.json`). Publish to arrival, p50
  and p99 in ms:

| Step | Stream | Cut-through (two runs) | Cache path |
| --- | --- | --- | --- |
| 0 % | Opus | 2.72 to 2.75 / 59 to 73 | 3.00 / 49 |
| 0 % | FLAC | 2.90 to 2.93 / 48 to 62 | 3.55 / 53 |
| 0 % | PCM | 2.22 to 2.23 / 52 to 62 | 2.94 / 64 |
| 0 % | 4K | 42.6 to 42.9 / 105 to 113 | 50.9 / 167 |
| 5 % | Opus | 2.86 / 62 to 67 | 3.22 / 44 |
| 5 % | FLAC | 3.00 / 44 to 45 | 3.60 / 45 |
| 5 % | PCM | 2.31 to 2.38 / 48 to 49 | 2.88 / 47 |
| 5 % | 4K | 60.7 to 61.2 / 135 to 145 | 54.4 / 201 |

Cut-through takes 0.3 to 0.7 ms off the audio median and 8 ms off the 4K
median on a clean path. With 5 % loss a lost 4K source symbol waits for the
edge to decode the whole part before its repair goes out, and the 4K median
is 6 ms worse than the cache path's. The p99 of 44 to 73 ms on audio is the
same with or without it and is made upstream of the edge. Against the first
Linode run (same day, before the latency round's mesh changes were merged)
the audio median fell from 3.6 to 4.6 ms to 2.2 to 3.0 ms.

Still open from this round: the edge sends to each client inline on the
mesh receive task (one system call per client symbol per client); a lost 4K
source symbol waits for the whole block; init segments are not sent to
cut-through clients (a client fetches `init.mp4` over HTTP, as it reads the
playlist); 2.5 ms Opus and 1 ms PCM end to end wait for the producers.

## Smallest frames on the UDP lane (third round, same day)

The owner: smallest frames by default, and "keep testing in the meantime
with the existing 2.5 ms" Opus. The producer side of the defaults (the AEP1
epoch, part durations in microseconds, the DAW source, deploy and docs
defaults, the lossless halving curve) is the latency round's, by agreement
between the two sessions; until it lands, `aep1-48k-probe send` here has a
stop-gap `--epoch-samples` and `--payload opus` (av-contrib `client-udp`
`4b8a12d`, which also merges the latency round's av-contrib). With the
contributor's opaque packager at `fmp4_part_ms=1` each epoch is one part.
`scripts/client-udp-lab.py run --smallest [--epoch-samples opus=120,pcm=48,flac=48]`
runs the end-to-end test with these sources and one video frame per part
(`fmp4_video_part_ms=42`).

| Frames | Steps | Result |
| --- | --- | --- |
| Opus 2.5 ms (120 samples), PCM and FLAC 5 ms, 4K one frame per part | 0 %, 1 %, 5 %, drain | PASS: no part missing, every part identical to LL-HLS (Opus about 32,000 parts at 400 per second) |
| as above, PCM 1 ms (48 samples) | 0 %, 5 % | FAIL upstream of the edge: LL-HLS readers miss the same parts |
| Opus 2.5 ms, PCM and FLAC 1 ms | 0 % | FAIL upstream of the edge |

Evidence: `evidence/local-20261121-client-udp-opus-2.5ms.json`,
`evidence/local-20261121-client-udp-pcm-1ms.json`,
`evidence/local-20261121-client-udp-lossless-1ms.json`.

With 1 ms lossless parts (1,000 parts a second per stream) the distributors
drop thousands of relay datagrams as expired (their deadline is publication
plus 1 s), and with the contributor of this branch before the merge its
publication queue was full. The edge and the UDP client deliver everything
that reaches the edge; the parts are lost in the distribution tier, which the
latency round's wave 2 is rebuilding for these rates (its target: publish to
edge p99 under 2 ms at 4,000 parts a second). The in-process test above
already carries 1 ms and 0.25 ms parts through the edge to a client. So:
2.5 ms Opus works on the UDP lane end to end today; 1 ms lossless works from
the edge to the client and waits for the distribution tier. No further
Linode run was made for this round.

## Revisions

| Repository | Branch | Commit | On |
| --- | --- | --- | --- |
| `av-mesh` | `client-udp` | `7497fc2` | `regional-llhls-cache` `07e7d4a`, with `low-latency` `113386f` merged |
| `playlists` | `client-udp` | `a225609` | `low-latency` `3317b29` |
| `av-contrib` | `client-udp` | `4b8a12d` | `edge-hardening` `12abbef`, with `low-latency` merged |
| `needletail` | `client-udp` | this commit | `regional-llhls-cache` `9a633d1` |

Built as in the edge hardening record (local `playlists`, `raptor-fec` and
`boxer` checkouts by `url.insteadOf`); the Linux binaries with
`cargo zigbuild --target x86_64-unknown-linux-gnu.2.34`, the contributor with
`--no-default-features`.
