# 18 July 2026: eight-track Opus LL-HLS capacity

This run measures the real DAW-to-listener path in one GCP zone: eight Lori
Asha `CONFIRMATION` stereo stems enter DAW Nexus as PCM, are encoded by the
pure-Rust Opus path, framed as encrypted SoundKit v2, protected with RaptorQ,
recovered by `av-contrib`, replicated through the two-parent DAG, and served as
opaque 5 ms LL-HLS parts over certificate-verified persistent H3.

The machine-readable record is
[`20260718T163240Z-opus-h3-capacity.json`](evidence/20260718T163240Z-opus-h3-capacity.json).
Raw reports and CPU samples remain under
`target/gcp-qualification/results/capacity-*`.

## Result

On an `n2-standard-2` edge, the strict steady-state boundary is four complete
eight-track customers. Five customers still receive every valid part, but miss
the 2 ms cache-to-client p99 target.

| Customers | Customers/vCPU | H3 connections | Requests/s | Availability p99 | Cache-to-client p99 | Complete | Strict pass |
| ---: | ---: | ---: | ---: | ---: | ---: | :---: | :---: |
| 2 | 1.0 | 16 | 3,200 | 13.104 ms | 0.748 ms | yes | yes |
| 4 | **2.0** | 32 | 6,400 | 14.026 ms | 1.673 ms | yes | **yes** |
| 5 | 2.5 | 40 | 8,000 | 16.569 ms | 2.254 ms | yes | no |
| 8 | 4.0 | 64 | 12,800 | 71.735 ms | 4.906 ms | yes | no |
| 9 | 4.5 | 72 | 14,400 | 153.833 ms | 7.155 ms | yes | no |
| 10 | 5.0 | 80 | 16,000 | 1,207.734 ms | 234.275 ms | no: 868 parts missing | no |
| 12 | 6.0 | 96 | 19,200 | 995.417 ms | 112.449 ms | no: 28,136 parts missing | no |

One customer means eight parallel track tails. The current probe uses one
persistent H3 connection per track, so the strict result is also 16 concurrent
track tails, 16 H3 connections, and 3,200 five-millisecond media requests per
second per edge vCPU. A client that multiplexes all eight tracks on one H3
connection has not yet been separately qualified.

The hard complete-delivery boundary is nine customers passing and ten failing:
14,400 requests/s versus 16,000 requests/s on two vCPUs. This is not a fixed
H3 connection limit. A broader diagnostic ramp completed every handshake at
1,024 simultaneous connections, and the isolated edge reached 197% process CPU
before media became incomplete.

This result is not a playlist-cache ceiling. The underlying cache reaches
4.7–9.2 million reads/s and the optimized production router reaches 1.112
million cached part responses/s on one worker without H3. Each measured Opus
customer instead creates 1,600 live H3 request streams/s, including cache
waiter/wakeup, H3/QPACK, QUIC, encryption, UDP, and stream cleanup. Four
customers is the strict 2 ms cache-to-client p99 boundary; it is not a claim
that the edge can only hold four connections or serve four complete customers.
See the canonical
[current performance state and gaps](../performance/current-state-and-gaps.md)
for the cross-boundary comparison and ordered investigation.

## Integrity and latency canary

Before load, a three-second late-join window requested 600 parts from each of
the eight tracks in parallel. All 4,800 parts arrived contiguously, every part
was valid SoundKit v2 Opus, all tracks decoded, and waveform correlation ranged
from 0.986556 to 0.998075. Maximum per-track p99 was 12.326 ms from the declared
audio clock and 0.604 ms from edge-cache commit to reader arrival.

The first clock readings were invalid: hosts reported NTP synchronized while
their offsets ranged from -48.7 to +66.6 ms. Chrony against GCP's metadata
clock reduced observed offsets below 0.1 ms. All earlier zero-latency samples
are rejected.

## Method

- DAW, contributor, both relays, edge, and reader were all in
  `europe-west2-c` to remove geography from this capacity test.
- The reader was a separate `n2-standard-4` VM and ran no Needletail service.
- Each isolated tier started from a fresh edge process, randomized customer
  joins over 750 ms with seed `424242`, and held every tail for ten seconds.
- A strict pass required all bytes, contiguous timestamps, valid Opus, no
  deadline miss, availability p99 at or below 20 ms, and cache delivery p99 at
  or below 2 ms.
- The reader stayed well below saturation in the broad ramp; the edge was the
  limiter. At four customers edge process CPU averaged 99.3% of one core and
  peaked at 143%; at five it averaged 107.6% and peaked at 149%.

An initial back-to-back ladder was rejected for steady capacity: disconnected
client cleanup carried into the following short tier and produced
non-monotonic p99. That behavior is useful evidence of a connection-churn
cleanup problem, but it is not the isolated concurrency number reported above.

## PCM comparison

The previous 16-channel PCM run passed 10,000 part requests/s and failed at
12,800. This much smaller Opus workload passes complete delivery at 14,400 and
fails at 16,000 on the current build. The exact figures are not a codec-only
A/B test because the revisions and customer geometry differ, but they are in
the same range despite a large byte-rate reduction. Request dispatch and QUIC
packet CPU, rather than media bandwidth, is therefore the dominant edge limit.

## Remaining reliability work

DAW startup currently emits 56 explicit-erasure packaging errors while eight
track formats register sequentially. All qualification windows begin three
seconds into the session and contain zero erasures or invalid bytes, but a
track-manifest/startup barrier is required before this becomes an end-to-end
startup reliability claim.

The next distribution work is:

1. fix startup track-map declaration and rerun the zero-offset canary;
2. diagnose H3 connection cleanup under rapid customer churn;
3. qualify one shared H3 connection per eight-track customer; and
4. repeat the four-customer tier for endurance before using it as production
   sizing without headroom.
