# 17 July 2026: raw PCM DAG latency and H3 edge capacity

## Result

Needletail now carries 48 kHz S24 PCM to LL-HLS as PCM. It does not convert
PCM to FLAC. A 16-channel publication is split into two synchronized
eight-channel `ipcm_s24le` fMP4 renditions; both remain mandatory LL-HLS while
native UDP+FEC and WebTransport remain optional taps.

The clean GCP DAG run `20260717T213603Z` delivered all 1,600 five-millisecond
parts in both renditions to independent New York, Tokyo, and Sydney caches.
The external capacity ladder `20260717T222106Z` established a strict short-run
edge boundary: 25 simultaneous customers passed on a two-vCPU
`n2-standard-2`; 32 customers failed. This is a measured floor and failure
boundary, not an endurance or production-SLA claim.

Versioned evidence:

- [`20260717T222106Z-pcm-h3-capacity.json`](evidence/20260717T222106Z-pcm-h3-capacity.json)

Raw artifacts remain under:

- `target/gcp-qualification/dag-runs/20260717T213603Z/clean`;
- `target/linode-qualification/h3-load-ladders/20260717T222106Z`;
- `target/gcp-qualification/readiness-canaries/20260717T225518Z`.

## Latency through the real mesh

The publication originated in London, crossed both warm DAG parents, and was
served from regional edge caches. All three lanes carried the same source
timeline.

| City | Raw UDP p50 | WebTransport p50 | LL-HLS p50 | LL-HLS premium | LL-HLS p99 | Local cache→H3 p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| New York | 53.338 ms | 53.595 ms | 55.728 ms | 2.390 ms | 57.137 ms | 1.510 ms |
| Tokyo | 125.054 ms | 125.130 ms | 127.506 ms | 2.452 ms | 128.824 ms | 1.274 ms |
| Sydney | 146.129 ms | 146.268 ms | 148.549 ms | 2.420 ms | 150.265 ms | 1.460 ms |

The network path accounts for almost all of those values. Once a part is in
the regional cache, persistent TLS 1.3/H3 adds roughly one millisecond at p99
in the clean DAG run. The post-deployment canary repeated the actual London →
DAG → New York cache → local H3 path and delivered 400/400 parts in both
renditions with cache-to-client p99 of 1.032 and 1.374 ms.

These measurements stop when the receiver has the part. They do not include a
browser decoder, audio worklet, operating-system mixer, interface buffer, or
speaker.

## PCM wire and contributor cost

The eight-second GCP source contained 18,432,000 bytes of raw 16-channel S24
PCM. AEP1 source and repair traffic used 24,038,400 wire bytes, a 1.3042×
wire-to-PCM ratio. The contributor used 0.875 core per publication-second,
published to exactly two ingress parents, and recorded:

- 22,400 LL-HLS handoffs with zero drops or worker errors;
- maximum handoff and origin queue depth of 14;
- 0.081 ms mean and 0.250 ms p99-upper-bound origin queue age;
- zero kernel UDP receive drops.

That CPU figure includes recovery, PCM fMP4 packaging, two renditions, FEC,
and two-parent publication. It is not FLAC encode cost: there is no PCM-to-FLAC
encode in this run.

## Two-vCPU edge capacity boundary

Each customer held two persistent H3 connections, one per eight-channel
rendition. Every passing tier required a valid LL-HLS playlist and PCM init,
exact media-part geometry, contiguous PTS, zero missing parts, and zero
deadline misses.

| Customers | H3 connections | Expected / received parts | Worst cache→client p99 | Result |
| ---: | ---: | ---: | ---: | --- |
| 1 | 2 | 1,600 / 1,600 | 5.431 ms | pass |
| 10 | 20 | 16,000 / 16,000 | 6.386 ms | pass |
| 25 | 50 | 40,000 / 40,000 | 10.342 ms | pass |
| 32 | 64 | 51,200 / 46,071 | 2,830.147 ms | fail |
| 50 | 100 | 80,000 / 39,399 | 2,811.223 ms | fail |

At 25 customers the edge served 10,000 five-millisecond part responses per
second, about 460.8 Mbit/s of source PCM or 510.56 Mbit/s of fMP4 application
payload. At overload, serving interfered with mesh ingress continuity as well
as viewer delivery, so 32 is a real failure point rather than a soft latency
warning.

The supported statement from this four-second ladder is therefore: one
two-vCPU `n2-standard-2` edge has demonstrated at least 25 simultaneous
16-channel PCM customers, and has not demonstrated 32. A sustained run at or
below 25 is still required before assigning a production capacity number.

## What made the earlier deployment unreliable

The failures were separate and reproducible:

1. The GCP firewall retained deleted load-generator addresses. Existing rules
   are now updated idempotently whenever the lab is brought up.
2. The H3 probe issued one request and waited for its response before issuing
   the next, accidentally paying an RTT per five-millisecond part. It now
   multiplexes exact part requests on one persistent H3 connection.
3. Every new cache part woke every blocked request. Exact stream-and-sequence
   waiters removed the thundering herd and the old 10 ms polling sleep.
4. Cache-mesh replication writes initially bypassed those waiters. The waiter
   registry now belongs to the shared chunk cache, so every write path wakes
   the correct request.
5. A process could report topology-ready immediately after restart before a
   publication had crossed both renditions. The new
   `scripts/gcp-pcm-readiness-canary.sh` refuses load testing until both
   renditions deliver 400/400 exact PCM H3 parts.

The final readiness canary `20260717T225518Z` passed after a clean rebuild and
redeploy. The disposable Linode load VM was deleted after capture; its private
reader image was preserved. The six GCP nodes remain active for the next
qualification round.

## Historical records

The earlier [multichannel sizing record](2026-07-17-multichannel-llhls-sizing.md)
measured the old PCM-to-FLAC implementation on a shared laptop and is retained
as historical evidence. The earlier [lossless H3 record](2026-07-17-lossless-h3.md)
is still valid for FLAC input. Neither should be used as evidence for the
current PCM-to-PCM path.
