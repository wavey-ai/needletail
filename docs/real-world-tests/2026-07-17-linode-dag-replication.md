# 17 July 2026: Linode multi-region DAG replication

## Result

Run `20260717T145432Z` passed the complete multi-edge qualification. One
deterministic 48 kHz stereo FLAC AEP1 publication started in London, fanned out
to two independent relay parents, and materialized independent LL-HLS caches in
New York, Tokyo, and Sydney. Every destination simultaneously exposed:

- native UDP with RaptorQ FEC;
- WebTransport datagrams;
- mandatory FLAC fMP4 LL-HLS with 5 ms parts over one persistent,
  certificate-verified TLS 1.3/H3 connection.

Each clean and impaired lane delivered all 2,400 expected epochs or parts with
zero gaps and zero deadline misses. Late join delivered all 1,400 expected
parts from the requested 5-second offset. The run also passed exact cache
identity, cache independence, bounded origin fanout, cross-parent repair, and
make-before-break failover gates.

Versioned evidence:

- [`20260717T145432Z-linode-dag.json`](evidence/20260717T145432Z-linode-dag.json).

Raw captures remain under
`target/linode-qualification/dag-runs/20260717T145432Z/` and are intentionally
not versioned.

## Topology and replication algorithm

The test used six Linode `g6-dedicated-2` instances, each with two dedicated
vCPUs:

```text
                         /-> Amsterdam primary ---\
London contributor ----<                         +--> New York cache
                         \-> Osaka secondary -----+--> Tokyo cache
                                                   \-> Sydney cache
```

The topology is a dual-parent directed acyclic graph (DAG). The contributor
sends one source copy to Amsterdam and one independent warm/repair copy to
Osaka, so origin fanout stays at two rather than increasing with viewer-region
count. Both parents feed every edge. The primary supplies normal source
traffic; the secondary supplies independent repair and can be promoted before
the primary route is removed. The test observed three playback edges while the
origin still had exactly two children.

| Role | Location | Linode region |
| --- | --- | --- |
| Contributor | London | `gb-lon` |
| Primary relay | Amsterdam | `nl-ams` |
| Secondary relay | Osaka | `jp-osa` |
| Playback edge | New York | `us-east` |
| Playback edge | Tokyo | `ap-northeast` |
| Playback edge | Sydney | `ap-southeast` |

## Measurement boundary

Latency starts at the sample-derived publication timestamp and stops when the
client receives the AEP1 epoch or LL-HLS part. All nodes were NTP synchronized.
For LL-HLS, an edge-generated availability timestamp splits the result into:

1. publication to local edge-cache commit;
2. edge-cache commit to H3 client receipt.

The `estimated_render_latency_ms` field adds a configured 150 ms allowance. It
is not a measurement of decoding, a browser audio pipeline, an operating-system
mixer, an audio interface, or a speaker.

## Clean-path latency

| City | Native UDP p50/p95/p99 | WebTransport p50/p95/p99 | LL-HLS p50/p95/p99 |
| --- | ---: | ---: | ---: |
| New York | 40.258 / 40.835 / 41.180 ms | 40.333 / 40.959 / 41.307 ms | 43.360 / 46.510 / 63.320 ms |
| Tokyo | 115.673 / 116.262 / 116.584 ms | 115.788 / 116.406 / 116.787 ms | 118.799 / 121.997 / 138.740 ms |
| Sydney | 129.821 / 130.528 / 131.853 ms | 129.945 / 130.758 / 132.252 ms | 132.887 / 136.687 / 153.049 ms |

At p50, LL-HLS was 3.103 ms behind native UDP in New York, 3.125 ms in
Tokyo, and 3.067 ms in Sydney. At p99, the clean-path premium was 22.140,
22.156, and 21.196 ms respectively. The median result supports the
“raw UDP plus a few milliseconds” description; the p99 tail is reported
separately rather than hidden by that summary.

### Publication, cache, and network split

| City | Primary route RTT / half-RTT | Publish→cache p50/p95/p99 | Cache→client p50/p95/p99 | Architecture + clock residual p50 |
| --- | ---: | ---: | ---: | ---: |
| New York | 82.179 / 41.090 ms | 43.114 / 46.234 / 63.116 ms | 0.222 / 0.404 / 0.654 ms | 2.025 ms |
| Tokyo | 228.613 / 114.307 ms | 118.501 / 121.677 / 138.538 ms | 0.220 / 0.436 / 0.674 ms | 4.195 ms |
| Sydney | 256.948 / 128.474 ms | 132.634 / 136.356 / 152.753 ms | 0.227 / 0.481 / 0.785 ms | 4.160 ms |

Half of measured route RTT is a propagation proxy, not a physical
speed-of-light lower bound. The residual includes node clock error, host and
kernel scheduling, relay processing, FEC/object reconstruction, FLAC/fMP4
packaging, and cache commit. Local H3 cache delivery was below 0.8 ms at p99;
most intercontinental latency was therefore in the route before the local
cache, not in serving LL-HLS from that cache.

Direct London-to-edge RTT was 61.513 ms to New York, 212.968 ms to Tokyo, and
250.746 ms to Sydney. The primary DAG route measured 82.179, 228.613, and
256.948 ms respectively. The resulting primary path stretch was 1.336×,
1.073×, and 1.025×.

## Controlled-loss result

The impaired profile dropped primary-path datagrams independently at every
edge. Both datagram clients and the LL-HLS packaging path still completed all
media.

| City | Deliberately dropped | RaptorQ source shards recovered | UDP p99 | WebTransport p99 | LL-HLS p99 | Edge CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| New York | 201 | 38 | 193.421 ms | 193.696 ms | 198.715 ms | 5.163% |
| Tokyo | 186 | 51 | 117.691 ms | 118.025 ms | 127.554 ms | 5.906% |
| Sydney | 195 | 43 | 167.435 ms | 167.616 ms | 172.534 ms | 5.698% |

The secondary route from Osaka is much longer than the primary route to New
York. Its route RTT is 375.502 ms versus 82.179 ms through Amsterdam, a
one-way differential of 146.662 ms. When a sequential H3 client waits on a
missing primary part, later cached parts drain after that repair arrives. This
produced a 149.254 ms cache-to-client p99 in New York. Sydney observed the same
effect at 33.599 ms. These are repair-path head-of-line measurements, not local
H3 request-processing time; clean cache-to-client p99 stayed below 0.8 ms.

The impaired gate is derived from the clean 25 ms cache-delivery budget plus
half of the measured secondary-minus-primary RTT differential. Its New York,
Tokyo, and Sydney limits were 171.662, 25.843, and 60.914 ms.

## Failover

The qualification stopped the Amsterdam primary while a 60-second publication
remained live, observed promotion of Osaka, restored Amsterdam, and required a
make-before-break return to the healthy state.

| City | State sequence | Detection | Promotion to source | Media gap | Expired/rejected/deadline drops |
| --- | --- | ---: | ---: | ---: | ---: |
| New York | healthy → promoted → healthy | 125.067 ms | 157.416 ms | 160.161 ms | 0 / 0 / 0 |
| Tokyo | healthy → promoted → healthy | 103.057 ms | 8.666 ms | 7.728 ms | 0 / 0 / 0 |
| Sydney | healthy → promoted → healthy | 114.198 ms | 106.974 ms | 7.335 ms | 0 / 0 / 0 |

Every detection, activation, and media-gap value remained inside the 250 ms
release budget. Each edge recorded one promotion and one make-before-break
demotion.

## Exact identity and cache independence

The latest eight media parts, the initialization segment, and the playlist had
matching SHA-256 digests in New York, Tokyo, and Sydney. Because the fMP4 bytes
were identical, their codec configuration, sample PTS, session identity, and
timeline were identical as well. Every cache reported a contiguous canonical
head with zero gaps.

The harness then stopped the New York edge service. Tokyo and Sydney continued
to serve their local init segment and latest part, proving that playback was
not being proxied through the stopped cache. New York restarted successfully.

## CPU, queues, and wire

CPU uses the service CPU-time delta divided by the matching wall-clock capture
interval. This corrects an earlier harness defect that divided a much longer
process snapshot interval by only the 12-second media duration.

| Profile | Contributor CPU | Edge CPU range | HLS queue maximum/capacity | Queue drops/errors | Source wire ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| Clean | 11.744% | 4.347–4.387% | 23 / 4,096 | 0 / 0 | 2.375× |
| Impaired | 16.462% | 5.163–5.906% | 4,096 / 4,096 | 0 / 0 | 3.568× |

The impaired profile emits two repair datagrams per source epoch, explaining
its higher wire ratio. Queue admission remained lossless, and all service CPU
values stayed well inside the configured release budgets.

## Idle-stream retention fix

The media-part cache was already bounded to 16 streams, but several auxiliary
per-stream maps were not retired. They retained stream epochs, sequence state,
initialization data, media kinds, availability timestamps, and commit locks.
That was a real reachable-state leak across successive stream identities, and
the 250 ms telemetry scan made the retained state visible as high idle CPU.

The tested build now bounds part-availability timestamps to the media window,
retires every dynamic stream map and cache slot after five idle minutes, and
removes an unused per-stream commit lock without racing a new commit. Telemetry
sampling now defaults to 1 second.

The live post-run audit observed 4 retirements in Amsterdam, 5 in Osaka, 5 in
Tokyo, 3 in New York, and 5 in Sydney. All three edge APIs then exposed only the
permanent default stream, with zero active relay objects. Edge memory was
56,987,648–68,345,856 bytes with three service tasks. The focused unit test also
asserts removal from the chunk cache, playlist cache, every auxiliary map, and
the commit-lock map.

## Release gates

The final evidence records every gate as passing:

- one publication reached three independent edge caches while origin fanout
  remained two;
- clean and impaired UDP, WebTransport, LL-HLS, and late-join clients received
  complete media with zero deadline misses;
- lossless FLAC LL-HLS used 5 ms fMP4 parts over verified persistent TLS 1.3/H3;
- publication-to-cache and cache-to-client latency were measured separately;
- RaptorQ recovered source data across the independent parents;
- failover and make-before-break recovery stayed inside 250 ms;
- init, playlist, media bytes, timeline, and sample PTS matched across caches;
- corruption, duplicate media, queue loss, cache gaps, and service instability
  remained zero;
- CPU, wire, route-stretch, latency, and bounded-origin-fanout budgets passed.

## Cleanup

After services stopped and each filesystem was synced, all six root disks were
captured as private Linode images. The API reported every image as available;
together they occupy 18,101 MiB. Image identifiers remain in the unversioned
local teardown artifact rather than the public evidence.

The six powered-off instances and qualification firewall were then deleted.
A provider-side query found zero instances and zero firewalls with the run tag,
while all six private images remained available. The exact lab-state file was
also removed. The retained images do not expire automatically and continue to
incur image-storage charges until explicitly deleted.
