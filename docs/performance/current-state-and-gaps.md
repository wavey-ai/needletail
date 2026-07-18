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
- The cache is not the current viewer-capacity bottleneck. The underlying cache
  reaches 4.7–9.2 million reads/s and the optimized production router reaches
  1.112 million cached part responses/s on one worker without H3 or the network.
- The current limit is the edge's live H3/QUIC request path, amplified by one
  request per track every 5 ms. The real eight-track Opus test makes 1,600 media
  requests/s/customer.
- On a two-vCPU `n2-standard-2` edge, four eight-track customers meet the strict
  2 ms cache-to-client p99 target. Nine still receive every part; delivery
  becomes incomplete at ten. Four is therefore the strict latency boundary,
  not a connection or cache correctness limit.
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
the HTTP response cadence for every viewer. The planned aggregation path should
let a viewer that accepts 100 ms of delivery latency receive 20 consecutive
self-delimiting units in one response while the cache and ultra-low-latency
path retain 5 ms units.

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
| Live eight-track Opus | cache wait/wakeup, router, H3, QUIC, real DAG | 6,400 requests/s at strict p99; 14,400 complete; 16,000 incomplete | Live request throughput and latency are the current tested boundary. |

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

The current probe uses one persistent H3 connection per track. On the two-vCPU
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
request-rate range. That makes request-stream lifecycle, QPACK, allocation and
copying, QUIC bookkeeping, kernel UDP work, and live cache waiter/wakeup work
stronger suspects than payload bandwidth. The exact split must be measured on
the live Opus path.

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

1. **Attribute the live-tail gap.** Run the same Opus request geometry against
   preseeded ready parts and future parts that register and wake waiters. Profile
   the edge at four, five, and nine customers with CPU, allocation, task,
   syscall, packet, and waiter counters.
2. **Reduce avoidable request geometry.** Compare eight persistent connections
   with one connection multiplexing all eight tracks. This removes connection
   machinery but not the 1,600 request streams/s/customer.
3. **Add viewer-selected response aggregation.** Keep 5 ms cache units and let
   latency-tolerant clients request 20 units/100 ms response. Measure the
   expected 20x request-rate reduction. Consider an optional synchronized
   multi-track epoch response for clients that always consume the same track
   set; do not force bundling on independently addressable tracks.
4. **Fix connection churn cleanup.** Back-to-back tiers show cleanup from
   disconnected clients affecting the following tier. Cancellation must return
   waiter, stream, timer, and connection memory to a stable band promptly.
5. **Fix publication startup.** Eight track formats currently register
   sequentially and create 56 explicit-erasure packaging errors before the
   stable window. Add a track-manifest/start barrier and rerun from offset zero.
6. **Qualify endurance and headroom.** Repeat the four-customer Opus tier for at
   least 30 minutes, then declare a production tier only below 70% CPU and link
   ceilings with stable RSS and zero missing parts.
7. **Complete scale dimensions independently.** Measure large idle-connection
   sets, blocked reloads, slow readers, one shared client UDP endpoint, flow
   control, and cancellation without conflating them with active media rate.
8. **Repeat the final build geographically.** After the same-zone edge is
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
- cache and router capacity are not the current active-media boundary; and
- the current two-vCPU edge supports the documented Opus tiers for the measured
  ten-second isolated windows.

Not yet supported:

- millions of active media customers or H3 connections;
- four eight-track customers as an endurance-qualified production size;
- clean eight-track delivery from the first source epoch;
- final capacity for one multiplexed H3 connection/customer;
- final capacity for 128- or 256-channel publications; or
- browser-to-speaker latency equal to publication-to-client availability.

## Evidence map

- [Eight-track Opus capacity](../real-world-tests/2026-07-18-opus-h3-capacity.md)
- [HTTP/3 and router isolation](../real-world-tests/2026-07-18-h3-capacity-isolation.md)
- [Raw PCM DAG latency and capacity](../real-world-tests/2026-07-17-pcm-h3-capacity.md)
- [Lossless three-lane latency](../real-world-tests/2026-07-17-lossless-h3.md)
- [Multi-region DAG replication](../real-world-tests/2026-07-17-linode-dag-replication.md)
- [Detailed latency tables](latency-performance.md)
- [Boundary qualification and gates](distribution-capacity-isolation.md)
- [Versioned evidence index](../real-world-tests/evidence/README.md)
- [Web-service HTTP/3 profile](https://github.com/wavey-ai/web-services/blob/main/docs/http3-capacity-investigation.md)
