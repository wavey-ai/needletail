# 18 July 2026: 200 ms Opus H3 response aggregation

This run answers whether the LL-HLS edge can keep its canonical 5 ms cache
units while serving latency-tolerant listeners less often. The deployed edge
used `AV_LL_HLS_RESPONSE_MS=200`: every blocking tail response waited for and
returned 40 exact consecutive 5 ms SoundKit v2 units. No media was transcoded,
reboxed, or converted to fMP4.

The machine-readable record is
[`20260718T221533Z-opus-h3-200ms-aggregation.json`](evidence/20260718T221533Z-opus-h3-200ms-aggregation.json).
Raw capacity reports are under
`target/gcp-qualification/artifacts/2026-07-18-opus-h3-200ms`.

## Result

The service setting works and preserves media integrity. One H3 connection per
customer multiplexed all eight independent track tails. That changed each
customer's HTTP geometry from 1,600 five-millisecond responses/s to 40
two-hundred-millisecond responses/s, a 40x reduction. The edge still performed
1,600 underlying cache-unit reads/s/customer.

The response-rate reduction increased the complete-delivery ceiling from nine
to fourteen customers, but did not improve the useful latency tier. Three
customers stayed below 50 ms p99 after subtracting the intentional 195 ms wait
from first-part latency; four jumped to 224.5 ms. All parts still arrived
through fourteen customers, but with seconds of accumulated delay. Fifteen
customers were incomplete.

| Customers | H3 connections | Cache units/s | Responses/s | Final-part p99 | Missing units | Deadline misses |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1 | 1,600 | 40 | 27.687 ms | 0 | 0 |
| 2 | 2 | 3,200 | 80 | 43.243 ms | 0 | 0 |
| 3 | 3 | 4,800 | 120 | **46.533 ms** | 0 | 0 |
| 4 | 4 | 6,400 | 160 | 224.518 ms | 0 | 0 |
| 8 | 8 | 12,800 | 320 | 823.054 ms | 0 | 4 |
| 12 | 12 | 19,200 | 480 | 2,180.571 ms | 0 | 3,617 |
| 14 | 14 | 22,400 | 560 | 2,143.531 ms | 0 | 7,421 |
| 15 | 15 | 24,000 | 600 requested | 2,100.879 ms | 177,400 | 7,410 |
| 16 | 16 | 25,600 | 640 requested | 2,264.801 ms | 189,200 | 6,466 |

The previous 5 ms test used eight H3 connections per customer, so this is not a
pure response-duration A/B. It is nevertheless decisive about the current
architecture: cutting H3 response streams by 40x did not cut the dominant
live-tail work by 40x.

## Service contract

`AV_LL_HLS_RESPONSE_MS` is an edge-wide default, with `--response-ms` as the
CLI equivalent. It must be a positive multiple of the configured 5 ms part
duration and may represent at most 200 units. A controlled client can use
`parts=<count>` to override the default for an A/B test.

The body is the byte-exact concatenation of consecutive cache units, so an
aggregated stream must be self-delimiting. SoundKit v2 satisfies that contract.
The response carries the start sequence, end sequence, final cursor, unit
count, unit duration, and aggregate duration in headers. The handler registers
for the exact final sequence, rechecks the cache to close the lost-wakeup race,
and uses no polling sleep.

## Integrity and source stability

A valid eight-track canary delivered all 6,400 expected SoundKit v2 units in
exactly 160 H3 responses. Every Opus packet was valid, all tracks decoded, and
waveform correlation ranged from 0.9897 to 0.9981. First-part p99 was 209.197
ms; final-part p99, which removes the intentional aggregation interval, was
14.197 ms.

The raw canary report was lost when the reader VM was recreated. Its metrics
were captured in the operator ledger, but this is weaker provenance than the
retained capacity reports and is identified as such in the evidence JSON.

The source then ran for 25.5 minutes from the Lori Asha `CONFIRMATION` stems.
With `AUDIO_EPOCH_HOLD_US=5000`, its 6,098 log lines contained zero errors,
warnings, or explicit erasures. The previous 1 ms default was too short for
eight independently paced DAW track callbacks and generated false erasures.
Complete stable epochs do not wait for this deadline; it only bounds an
incomplete epoch before missing tracks become explicit erasures.

## Where the time goes

Fresh-edge CPU samples were approximately 0.455 core with the source active and
no consumers, 0.853 core at four customers, 0.929 at eight, and 0.953 at
twelve. The process had two Tokio workers and was eligible for both vCPUs, yet
aggregate work flattened near one core.

The current aggregation handler still fetches, decodes, and copies 40 cache
slots separately for each response. The first unit also establishes stream
metadata, while immutable reads serve the rest; that removed repeated global
state writes but did not create a cache-level batch read. The remaining
serialized cache/response section, not playlist lookup and not H3 response rate
alone, is the leading bottleneck.

Connection cleanup is the second proven reliability gap. A back-to-back
overloaded tier made a later fourteen-customer run starve playlist and init
work. Restarting only the edge restored clean delivery. Disconnected, expired,
or canceled tail work is therefore surviving long enough to poison the next
load window.

## Next changes

1. Add a bounded cache range-read API that resolves consecutive immutable units
   under one stream lookup and returns cheap byte references to the edge.
2. Cancel a tail handler's cache waiter, timer, and response work immediately
   when its H3 stream or connection disappears.
3. Profile the resulting path and shard any remaining stream-global delivery
   state before repeating the same randomized-arrival ladder.
4. Run endurance only after a latency-qualified tier has at least 30% CPU
   headroom and stable RSS.

All six London test VMs were stopped after collection. Their persistent disks
and images remain available for the next iteration.
