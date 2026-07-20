# Current performance state and gaps

This is the canonical snapshot of Needletail's measured media-delivery state as
of 20 July 2026. It separates latency, cache and router throughput, HTTP/3
transport capacity, live-tail customer capacity, replication correctness, and
remaining work. Dated test records remain the authority for exact revisions and
raw evidence.

## Executive summary

- Persistent TLS 1.3/H3 LL-HLS with 5 ms parts adds only 2.390–2.452 ms at p50
  over the raw UDP lane in the measured London-to-New York, Tokyo, and Sydney
  GCP topology. This is publication-to-client availability, not
  browser-to-speaker latency.
- A standalone cache lookup is not the current viewer-capacity bottleneck. The
  underlying cache reaches 4.7–9.2 million reads/s and the optimized production
  router reaches 1.112 million cached part responses/s on one worker without H3
  or the network. The deployed live path now performs generation-safe bounded
  range reads and can bundle eight synchronized track tails in one response.
- The current limit is the edge's live cache-to-H3 path. With 5 ms responses,
  one eight-track Opus customer makes 1,600 media requests/s. With 200 ms
  responses, one connection multiplexes the same tracks in 40 responses/s but
  the edge still performs 1,600 cache-unit reads/s/customer.
- On a two-vCPU `n2-standard-2` edge, four eight-track customers meet the strict
  2 ms cache-to-client p99 target. Nine still receive every part; delivery
  becomes incomplete at ten. Four is therefore the strict latency boundary,
  not a connection or cache correctness limit.
- The new `AV_LL_HLS_RESPONSE_MS=200` policy returns 40 exact 5 ms units per
  response. It raises complete delivery from nine to fourteen customers, but
  final-part p99 crosses 50 ms between three and four customers and total edge
  CPU still flattens near one core. Response count alone was not the bottleneck.
- The 19 July bundled path keeps a 5 ms response cadence but carries all eight
  tracks on one H3 connection. On the same two-vCPU edge, 24 customers repeated
  three times at 16.578–18.051 ms availability p99 and about 57.4–57.8% host
  CPU, with zero missing, discontinuous, late, or invalid Opus parts. Twenty-eight
  was the first provisional 20 ms latency-gate miss; 32 was the first
  approximate 30%-CPU-headroom miss while still delivering every part.
- A matched 60-second profile at 24 customers removed repeated canonical
  object work, accelerated the unchanged IEEE CRC-32, skipped unused AEP1
  parsing, and replaced joined waits with exact sequential reads under one
  deadline. Edge CPU fell from 59.415% to 34.765% of the two-vCPU host. A later
  exact-envelope handoff removed another encode, decode, and hash cycle.
- Clock-gated diagnostics then found that serial metadata checks and percentile
  sorting in the load probe overlapped customers that were still receiving
  media. The corrected probe performs metadata checks before each timed window
  and defers sorting until all media tasks stop. The accepted exact-envelope
  build then passed twice: each run delivered all 2,304,000 parts with zero late
  bundle. Availability p99 was 13.628 and 13.694 ms, cache-to-client p99 was
  4.947 and 5.058 ms, and host CPU was 32.951% and 34.084%.
- A shared group-waiter candidate used 4.707% less mean CPU than the accepted
  build but was slower in both p99 latency measures and passed only one of two
  corrected repetitions. It is rejected for the current release. The accepted
  exact-envelope build now has a repeatable strict short-window result but no
  endurance or production-sizing claim.
- Replication through a dual-parent DAG, independent regional caches, late join,
  RaptorQ recovery, and parent failover are proven in deployed tests. Endurance
  at the 24-customer bundled candidate, cancellation churn without restart, and
  startup reliability are not yet qualified.

## Current media path

```text
DAW or other producer
  -> av-contrib: recover once and apply the declared packaging policy
  -> dual-parent relay DAG: replicate each publication once per edge
  -> regional av-mesh playback cache
       |-> mandatory format-preserving LL-HLS over persistent H3
       |-> optional WebTransport datagrams
       `-> optional native UDP plus FEC tap
```

The contributor is a per-publication origin, not a geographical relay or
viewer server. The mesh stores and replicates opaque immutable bytes; it does
not require SoundKit, Opus, PCM, or FLAC semantics. Every accepted format must
reach LL-HLS. Producer-authored public Opus CMAF/fMP4 remains a separate future
rendition for generic players.

Five milliseconds is the canonical realtime media unit. It does not have to be
the HTTP response cadence for every viewer. `AV_LL_HLS_RESPONSE_MS` sets the
edge-wide default response duration while preserving 5 ms cache units; a
controlled client can override the number of units for an A/B test. The body is
the byte-exact concatenation of consecutive units, so aggregation requires a
self-delimiting cached bytestream such as SoundKit v2.

For synchronized multitrack clients, `/live/tail-bundle` can carry one bounded
range from each requested stream in an `NTB1` envelope. The measured 5 ms mode
returns one unit from each of eight tracks every 5 ms, reducing H3 response
streams 8x without changing media cadence or cache identity.

## What is measured

### Wide-area latency

The clean 16-channel S24 PCM run published in London, crossed the dual-parent
GCP DAG, and read the local cache in each viewer region:

| City | Raw UDP p50 | LL-HLS p50 | LL-HLS premium | Cache-to-client p99 |
| --- | ---: | ---: | ---: | ---: |
| New York | 53.338 ms | 55.728 ms | 2.390 ms | 1.510 ms |
| Tokyo | 125.054 ms | 127.506 ms | 2.452 ms | 1.274 ms |
| Sydney | 146.129 ms | 148.549 ms | 2.420 ms | 1.460 ms |

Most publication-to-client latency is the intercontinental network path.
Inside a region, the persistent-H3 LL-HLS delivery premium is a few
milliseconds. These results do not include browser decode, audio buffering, or
device output latency.

### Capacity by boundary

These figures describe different work and must not be presented as one
interchangeable requests-per-second claim:

| Boundary | Work retained | Current result | Interpretation |
| --- | --- | ---: | --- |
| Chunk cache | immutable cache read | 4.7–9.2 million reads/s | Storage lookup is not the current edge limit. |
| Cached playlist route | playlist lookup and router, no H3 | 243,000–431,000 responses/s | Playlist reads are distinct from active media delivery. |
| Optimized cached-part router | production `AppRouter`, no H3 | 1,112,332 responses/s, one worker | Application routing is far above the live edge rate. |
| Static H3, 64-byte body | TLS, H3, QUIC, kernel UDP | 89,544 responses/s at 16 connections; 100,480 peak at 32 | Transport costs are orders of magnitude above cache lookup. |
| Static H3, 5,760-byte body | PCM-shaped response at realtime cadence | 15,984 responses/s exact at 40 customer equivalents | Packet and byte processing matter even without the mesh. |
| Live eight-track Opus, 5 ms responses | cache wait/wakeup, router, H3, QUIC, real DAG | 6,400 responses/s at strict p99; 14,400 complete; 16,000 incomplete | One request stream per 5 ms track unit is expensive. |
| Live eight-track Opus, 200 ms responses | 40 cache reads/response, router, H3, QUIC, real DAG | 3 customers below 50 ms final-part p99; 14 complete; 15 incomplete | Historical result: 40x fewer responses improved completeness but exposed serialized cache batching and cleanup work. |
| Live eight-track Opus, bundled 5 ms responses | generation-safe range reads, eight tracks/response, exact waiters, router, H3, real DAG | 24 customers repeated below provisional 20 ms availability p99 with >42% host CPU headroom; 28 first latency miss; 32 first approximate headroom miss | Current short-window candidate; every tested part remained correct through 32 customers, but endurance is pending. |

The connection count is not the observed ceiling. The production server
completed 1,000 persistent connections at low request rate, and the Opus
diagnostic completed all handshakes through 1,024 connections. Connection
count, request rate, payload rate, blocked reload count, and memory are separate
capacity dimensions.

### Real eight-track Opus result

The same-zone GCP test used eight Lori Asha `CONFIRMATION` stereo stems. DAW
Nexus read PCM, encoded pure-Rust Opus, produced encrypted SoundKit v2 frames
with RaptorQ protection, and sent them through `av-contrib`, two relay parents,
one playback edge, and a separate reader host.

One customer tailed eight independent tracks:

```text
200 requests/s/track x 8 tracks = 1,600 H3 requests/s/customer
```

The 5 ms probe uses one persistent H3 connection per track. On the two-vCPU
edge:

| Customers | H3 connections | Requests/s | Cache-to-client p99 | Delivery |
| ---: | ---: | ---: | ---: | --- |
| 4 | 32 | 6,400 | 1.673 ms | complete; strict pass |
| 5 | 40 | 8,000 | 2.254 ms | complete; misses 2 ms target |
| 9 | 72 | 14,400 | 7.155 ms | complete; latency degraded |
| 10 | 80 | 16,000 | 234.275 ms | incomplete; 868 missing parts |

The strict sizing result is therefore two eight-track customers/vCPU, or 16
active track tails and 3,200 media requests/s/vCPU. It is not four customers
per CPU, and it is not a production sizing claim with endurance headroom.

The integrity canary delivered all 4,800 expected parts contiguously. Every
part was valid Opus, all eight tracks decoded, and waveform correlation ranged
from 0.986556 to 0.998075.

### Two-hundred-millisecond response policy

The aggregation probe multiplexes all eight tracks on one H3 connection per
customer. Each response carries 40 ordered 5 ms SoundKit v2 units:

```text
200 cache units/s/track x 8 tracks = 1,600 cache reads/s/customer
5 responses/s/track x 8 tracks = 40 H3 responses/s/customer
```

| Customers | H3 connections | H3 responses/s | Final-part p99 | Delivery |
| ---: | ---: | ---: | ---: | --- |
| 3 | 3 | 120 | 46.533 ms | complete; no deadline misses |
| 4 | 4 | 160 | 224.518 ms | complete; latency knee |
| 14 | 14 | 560 | 2,143.531 ms | complete; unusable backlog |
| 15 | 15 | 600 requested | 2,100.879 ms | incomplete; 177,400 units missing |

This is not a pure A/B because the earlier probe used eight connections per
customer. It does prove that response stream count alone is not the dominant
cost: aggregation cuts it 40x, yet the practical latency boundary does not
improve. The handler still performs 40 cache slot reads, unit decodes, and
copies per response. Edge CPU rose from 0.455 core with no consumers to 0.853
at four customers and flattened at 0.953 by twelve despite two Tokio workers.

### Bundled five-millisecond track tails

The follow-up path implements the bounded range read and synchronizes all eight
tracks in one response. One customer now uses one persistent H3 connection and
200 bundle responses/s rather than eight connections and 1,600 single-track
responses/s. It still carries 1,600 exact cache units/s.

| Customers | H3 connections | Track readers | Availability p99 | Edge host CPU | Delivery |
| ---: | ---: | ---: | ---: | ---: | --- |
| 24, repeat range | 24 | 192 | 16.578–18.051 ms | 57.434–57.804% | 2,304,000/2,304,000 parts; candidate |
| 28 | 28 | 224 | 20.759 ms | 63.180% | 896,000/896,000; latency-gate miss |
| 32 | 32 | 256 | 22.967 ms | about 70.45% | 1,024,000/1,024,000; latency and headroom miss |

All valid tiers had zero missing parts, PTS discontinuities, deadline misses,
Opus mismatches, and kernel UDP-buffer errors. The three 24-customer p99 results
varied by 8.60% of their mean and edge CPU by 0.64%. Load and media stayed on
private GCP IPv4 paths.

The subsequent matched 60-second profiles held geometry constant at 24
customers and a strict 20 ms deadline:

| Build | Edge host CPU | Availability p99 | Cache-to-client p99 | Late bundles |
| --- | ---: | ---: | ---: | ---: |
| baseline | 59.415% | 14.633 ms | 7.351 ms | 209 |
| indexed canonical slot | 42.782% | 14.157 ms | 5.772 ms | 147 |
| plus accelerated CRC | 41.679% | 14.039 ms | 5.409 ms | 53 |
| plus zero-consumer AEP1 discard | 38.981% | 14.090 ms | 5.314 ms | 33 |
| plus sequential exact bundle waits | 34.765% | 10.801 ms | 8.399 ms | 9 |

Every row delivered all 2,304,000 parts without PTS or Opus mismatch. The final
row is 41.49% less CPU than baseline and has 65.24% host CPU headroom, but its
9 late bundles keep the strict gate failed. The final row's cache-to-client p99
rose while end-to-end availability improved, so the next run must retain both
metrics rather than treating either one as a substitute for the other.

Host state changed before the exact-envelope follow-up, so an interleaved v11
control provides the valid CPU comparison:

| Build/run | Edge host CPU | Availability p99 | Cache samples | Late bundles |
| --- | ---: | ---: | ---: | ---: |
| v12 matched | 39.136% | 20.835 ms | 24/192 | 6,144 |
| v11 adjacent control | 42.007% | 11.556 ms | 0/192 | 0 |
| v12 final repeat | 40.380% | 11.561 ms | 0/192 | 0 |

The final v12 repeat used 3.873% less CPU than the adjacent control, with only
0.005 ms difference in availability p99. A first v12 media run also had no late
bundles, but its call-chain profile makes its CPU result incomparable. Every
attempt delivered all 2,304,000 parts. At that point, one v12 deadline failure
prevented a strict repeatability claim. The final control and repeat also had no
valid cache-to-client samples; those results are unavailable, not zero.

Clock qualification and corrected client work then restored valid cache
coverage and removed probe work from other customers' timed windows:

| Accepted v12 run | Edge host CPU | Availability p99 | Cache-to-client p99 | Cache samples | Late bundles |
| --- | ---: | ---: | ---: | ---: | ---: |
| corrected repeat 1 | 32.951% | 13.628 ms | 4.947 ms | 192/192 | 0 |
| corrected repeat 2 | 34.084% | 13.694 ms | 5.058 ms | 192/192 | 0 |

Each corrected run delivered 288,000 bundle responses and all 2,304,000 exact
track parts. Both passed the explicit 20 ms deadline. Six-host chrony checks
before and after each run stayed below the 1 ms offset and dispersion gates.
This supersedes the earlier repeatability and cache-coverage gaps. The older
attempts remain valid diagnostic evidence, not release results.

This candidate does not inherit the old 2 ms cache-to-client claim: the metric
and connection geometry changed. It is also not production-sized until a
30-minute run proves stable RSS and the same latency, correctness, and headroom.

## Where the edge CPU goes

A fixed PCM-shaped H3 profile found the following overlapping inclusive costs:

- 21.2% of samples below system calls;
- 14.8% in the `sendmsg` family and 12.2% below `udp_sendmsg`;
- visible software UDP segmentation because transmit UDP segmentation is fixed
  off on the tested GCP virtual NIC;
- roughly 4% in AES-GCM, AES, and GHASH;
- 3.1% flat in `malloc` plus `free`;
- 1.3% flat in stateless QPACK string encoding; and
- 0.4% flat in Tokio task scheduling after request scheduling was fixed.

Opus bodies are much smaller than PCM bodies but fail in approximately the same
5 ms request-rate range. The 200 ms test now removes H3 response rate as the
only explanation: response streams fall 40x, while 40 individual cache reads,
decodes, and copies remain. The near-one-core plateau points to a serialized
live cache/response boundary. QPACK, allocation and copying, QUIC bookkeeping,
kernel UDP work, waiter/wakeup, and cancellation still matter, but the next
profile must begin at the cache range-read boundary.

The 19 July matched profile then removed that leading boundary. A fixed internal
slot index retained the exact canonical envelope while serving prevalidated
byte ranges, accelerated CRC removed the bitwise checksum from the flat top,
and zero-consumer leaf discard removed the former 3.15% AEP1 ingress closure.
Sequential exact reads under one absolute deadline then removed the prior
2.11% `join_all::MaybeDone` flat symbol. The final flat profile is led by
allocation/free, SHA, router dispatch, QPACK, and kernel work. The next
optimization must target those measured costs while preserving exact bundle
and strict deadline semantics.

The exact-envelope handoff from `RelaySession` to the cache is locally
qualified, deployed, and measured on the private GCP topology. It retains
canonical parsing, payload-hash, identity, replay, and immutable-conflict
checks. Canonical encode disappeared from the final flat profile and SHA-256
fell from 2.33% to 1.22% of samples. The final adjacent comparison used 3.873%
less CPU. The corrected, clock-qualified v12 series then passed the strict
short-window gate twice with complete cache evidence.

## Improvements already proven

| Change | Measured effect |
| --- | --- |
| Remove preflight-only CORS fields from ordinary H3 media responses | +10.78% saturated tiny-response throughput; 12.49% fewer wire bytes/response |
| Replace one detached Tokio task/request with connection-local in-flight futures | +11.35% tiny-response throughput, -12.49% CPU ticks, p99 about 16.1 -> 12.5 ms |
| Remove owned route strings and global async demand lock | Part of the one-worker router gain from 792,037 to 1,112,332 responses/s |
| Sample successful diagnostics and update one histogram bucket/request | Removes per-success mutex and cumulative-atomic contention while preserving exact counters |
| Compare Quinn with tokio-quiche | Quinn remained the default; tokio-quiche used about 5.9% more CPU and emitted about 4.8% more packets |
| Raise the tested tokio-quiche UDP payload ceiling | No measurable packetization or capacity change |
| Add generation-safe bounded consecutive cache reads | Resolves one stream generation per range and returns only an all-or-nothing ordered immutable range across wrap and retirement. |
| Replace broad live-tail wakeups with sharded exact waiters | Removes unrelated per-commit wakeups; canceled requests retain no strong waiter work. |
| Bundle eight synchronized tracks per 5 ms H3 response | Cuts H3 responses and connections/customer 8x; produces the repeatable 24-customer short-window candidate. |
| Index validated canonical live slots | Reduces matched edge CPU from 59.415% to 42.782% of the host and cache-to-client p99 from 7.351 to 5.772 ms without changing exact envelope semantics. |
| Use accelerated IEEE CRC-32 | Removes the bitwise CRC hot spot; matched host CPU falls to 41.679% and late bundles from 147 to 53. |
| Skip AEP1 parsing on a zero-consumer leaf | Removes the remaining audio-ingress hot closure; matched host CPU falls to 38.981% and late bundles from 53 to 33. |
| Replace joined per-track waits with sequential exact waits under one deadline | Removes the 2.11% `join_all::MaybeDone` flat symbol; matched host CPU falls to 34.765%, availability p99 to 10.801 ms, and late bundles from 33 to 9. |
| Transfer the verified exact canonical envelope from `RelaySession` | Removes the duplicate encode/decode/hash cycle; canonical encode disappears, SHA-256 falls from 2.33% to 1.22% of flat samples, and the corrected clock-qualified build repeats the strict 20 ms result twice. |
| Keep probe metadata and reporting outside another customer's timed window | Restores all 192 cache samples and removes the artificial completion-tail load; two v12 repetitions deliver every part with zero late bundle. |

These fixes rule out the cache, router, a global 256-connection limit, and
Quinn alone as explanations for the remaining gap.

## Current gaps, in order

1. **Qualify the strict candidate under endurance.** Run the accepted geometry
   for at least 30 minutes on private GCP paths. Require stable RSS, complete
   media, the same strict latency and CPU headroom, and clean process exit.
   Declare a production tier only after all gates pass together.
2. **Measure connection-churn cleanup without restart.** Weak exact-waiter
   registrations remove retained request work, but the recorded tiers restarted
   the edge. Exercise connect, cancel, timeout, and slow-reader churn while
   exposing bounded waiter, task, stream, timer, and connection counts.
3. **Reduce the remaining measured hot costs.** Allocation/free, SHA, router
   dispatch, QPACK, and kernel work now lead the flat profile. The rejected
   group waiter reduced mean CPU by 4.707%, but it did not improve p99 and did
   not repeat the strict gate. Use that only as an isolated future lead.
4. **Finish publication startup reliability.** The measured 5 ms DAW epoch hold
   removes false erasures under independent track pacing. Add a declared track
   manifest/start barrier and rerun a retained zero-offset canary.
5. **Complete scale dimensions independently.** Measure large idle-connection
   sets, blocked reloads, slow readers, one shared client UDP endpoint, flow
   control, and cancellation without conflating them with active media rate.
6. **Repeat the final build geographically.** After same-zone endurance and
   churn pass, rerun the multi-region DAG and size 16-, 128-, and 256-channel
   publications without duplicating contributor work.

## Claims and non-claims

Supported by current evidence:

- 5 ms persistent-H3 LL-HLS can stay within a few milliseconds of raw UDP after
  the same regional cache;
- lossless PCM and private framed Opus both reach mandatory LL-HLS without
  transcoding;
- the dual-parent DAG provides byte-identical replication, independent regional
  caches, late join, cross-parent RaptorQ recovery, and bounded failover;
- standalone cache and router capacity are not the current active-media
  boundary;
- the current two-vCPU edge supports the documented Opus tiers for the measured
  short isolated windows;
- `AV_LL_HLS_RESPONSE_MS=200` preserves exact 5 ms units and reduces H3
  responses 40x for the tested self-delimiting SoundKit v2 stream; and
- synchronized eight-track H3 bundling with generation-safe range reads and the
  exact-envelope handoff supports a repeatable strict 20 ms, 24-customer
  short-window result on the measured private-GCP topology; and
- the corrected load probe retains complete 192-reader cache evidence without
  adding metadata or report work to another customer's timed media window.

Not yet supported:

- millions of active media customers or H3 connections;
- 24 bundled eight-track customers as an endurance-qualified production size;
- clean eight-track delivery from the first source epoch;
- a production-qualified 200 ms aggregation tier with useful latency;
- final capacity for 128- or 256-channel publications; or
- browser-to-speaker latency equal to publication-to-client availability.

## Evidence map

- [Clock-qualified Opus H3 tail repeatability](../real-world-tests/2026-07-20-opus-h3-clock-qualified-tail.md)
- [Eight-track Opus capacity](../real-world-tests/2026-07-18-opus-h3-capacity.md)
- [Two-hundred-millisecond Opus response aggregation](../real-world-tests/2026-07-18-opus-h3-200ms-aggregation.md)
- [Bundled eight-track Opus H3 tails](../real-world-tests/2026-07-19-opus-h3-tail-bundle.md)
- [HTTP/3 and router isolation](../real-world-tests/2026-07-18-h3-capacity-isolation.md)
- [Raw PCM DAG latency and capacity](../real-world-tests/2026-07-17-pcm-h3-capacity.md)
- [Lossless three-lane latency](../real-world-tests/2026-07-17-lossless-h3.md)
- [Multi-region DAG replication](../real-world-tests/2026-07-17-linode-dag-replication.md)
- [Detailed latency tables](latency-performance.md)
- [Boundary qualification and gates](distribution-capacity-isolation.md)
- [Versioned evidence index](../real-world-tests/evidence/README.md)
- [Web-service HTTP/3 profile](https://github.com/wavey-ai/web-services/blob/main/docs/http3-capacity-investigation.md)
