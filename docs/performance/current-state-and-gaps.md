# Current performance state and gaps

This is the canonical snapshot of Needletail's measured media-delivery state as
of 18 July 2026. It separates latency, cache and router throughput, HTTP/3
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
  or the network. The live path's repeated range access is not yet batched.
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
- Replication through a dual-parent DAG, independent regional caches, late join,
  RaptorQ recovery, and parent failover are proven in deployed tests. Endurance
  at the final Opus latency tier and startup reliability are not yet qualified.

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
| Live eight-track Opus, 200 ms responses | 40 cache reads/response, router, H3, QUIC, real DAG | 3 customers below 50 ms final-part p99; 14 complete; 15 incomplete | 40x fewer responses improved completeness but exposed serialized cache batching and cleanup work. |

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

## Where the edge CPU goes

The current evidence names the broad boundary but does not yet provide a
flamegraph of the real Opus live-tail tier. A fixed PCM-shaped H3 profile found
the following overlapping inclusive costs:

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

## Improvements already proven

| Change | Measured effect |
| --- | --- |
| Remove preflight-only CORS fields from ordinary H3 media responses | +10.78% saturated tiny-response throughput; 12.49% fewer wire bytes/response |
| Replace one detached Tokio task/request with connection-local in-flight futures | +11.35% tiny-response throughput, -12.49% CPU ticks, p99 about 16.1 -> 12.5 ms |
| Remove owned route strings and global async demand lock | Part of the one-worker router gain from 792,037 to 1,112,332 responses/s |
| Sample successful diagnostics and update one histogram bucket/request | Removes per-success mutex and cumulative-atomic contention while preserving exact counters |
| Compare Quinn with tokio-quiche | Quinn remained the default; tokio-quiche used about 5.9% more CPU and emitted about 4.8% more packets |
| Raise the tested tokio-quiche UDP payload ceiling | No measurable packetization or capacity change |

These fixes rule out the cache, router, a global 256-connection limit, and
Quinn alone as explanations for the remaining gap.

## Current gaps, in order

1. **Batch consecutive cache reads.** Add a bounded range-read API that resolves
   one stream generation and returns consecutive immutable units without 40
   separate top-level lookups, decodes, and global-state updates.
2. **Fix connection churn cleanup.** Back-to-back tiers show cleanup from
   disconnected clients affecting the following tier. Cancellation must return
   waiter, stream, timer, and connection memory to a stable band promptly.
3. **Profile the remaining serialized boundary.** Rerun the 200 ms ladder after
   cache batching and profile allocation, tasks, waiters, syscalls, packets,
   per-stream locks, and response copying at three and four customers.
4. **Finish publication startup reliability.** The measured 5 ms DAW epoch hold
   removes false erasures under independent track pacing. Add a declared track
   manifest/start barrier and rerun a retained zero-offset canary.
5. **Qualify endurance and headroom.** Repeat the best latency-qualified Opus
   tier for at least 30 minutes, then declare a production tier only below 70%
   CPU and link ceilings with stable RSS and zero missing parts.
6. **Complete scale dimensions independently.** Measure large idle-connection
   sets, blocked reloads, slow readers, one shared client UDP endpoint, flow
   control, and cancellation without conflating them with active media rate.
7. **Repeat the final build geographically.** After the same-zone edge is
   understood, rerun the multi-region DAG and size 16-, 128-, and 256-channel
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
  short isolated windows; and
- `AV_LL_HLS_RESPONSE_MS=200` preserves exact 5 ms units and reduces H3
  responses 40x for the tested self-delimiting SoundKit v2 stream.

Not yet supported:

- millions of active media customers or H3 connections;
- four eight-track customers as an endurance-qualified production size;
- clean eight-track delivery from the first source epoch;
- a production-qualified 200 ms aggregation tier with useful latency;
- final capacity for 128- or 256-channel publications; or
- browser-to-speaker latency equal to publication-to-client availability.

## Evidence map

- [Eight-track Opus capacity](../real-world-tests/2026-07-18-opus-h3-capacity.md)
- [Two-hundred-millisecond Opus response aggregation](../real-world-tests/2026-07-18-opus-h3-200ms-aggregation.md)
- [HTTP/3 and router isolation](../real-world-tests/2026-07-18-h3-capacity-isolation.md)
- [Raw PCM DAG latency and capacity](../real-world-tests/2026-07-17-pcm-h3-capacity.md)
- [Lossless three-lane latency](../real-world-tests/2026-07-17-lossless-h3.md)
- [Multi-region DAG replication](../real-world-tests/2026-07-17-linode-dag-replication.md)
- [Detailed latency tables](latency-performance.md)
- [Boundary qualification and gates](distribution-capacity-isolation.md)
- [Versioned evidence index](../real-world-tests/evidence/README.md)
- [Web-service HTTP/3 profile](https://github.com/wavey-ai/web-services/blob/main/docs/http3-capacity-investigation.md)
