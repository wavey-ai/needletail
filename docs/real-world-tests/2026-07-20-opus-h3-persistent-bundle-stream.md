# 20 July 2026: persistent H3 Opus bundle stream

This series removes one HTTP/3 request and response-header block per 5 ms
bundle. One request now keeps one response open for the customer's session.
The body carries repeated frames with a four-byte network-order length followed
by the existing self-describing `NTB1` bundle. Ordinary LL-HLS and
`/live/tail-bundle` URLs remain compatible.

The machine record is
[`20260720T045417Z-opus-h3-persistent-bundle-stream.json`](evidence/20260720T045417Z-opus-h3-persistent-bundle-stream.json).
Raw reports, service captures, clock checks, resource samples, and profiles are
under `target/gcp-qualification/live-tail-serialization/profile`.

## Accepted result

The 32-customer tier passed twice. Each customer tailed eight real Opus tracks
at 5 ms cadence over one H3 connection and one persistent response.

| Run | Tails | Tails/vCPU | Valid units | Late bundles | Availability p99 | Cache p99 | Edge host CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `20260720T044208Z...32x8-r1` | 256 | 128 | 1,024,000 | 0 | 12.627 ms | 3.768 ms | 14.336% |
| `20260720T045417Z...32x8-r2` | 256 | 128 | 1,024,000 | 0 | 12.734 ms | 3.874 ms | 16.779% |

Both runs had zero missing units, duplicate or non-contiguous PTS, invalid Opus
packets, failed readers, service restarts, or responses beyond the explicit
20 ms capture-to-complete-frame deadline. Resource sampling ended with no exact
waiter registrations and stayed inside the RSS bounds.

This is a repeatable short-window result at 128 realtime Opus track tails per
edge vCPU. It is not an endurance or production-sizing result.

## Measured improvement

The two preceding 32 x 8 request-per-bundle runs averaged 27.147% edge-host
CPU and did not pass the zero-miss gate. The two persistent-response runs
averaged 15.558%, a 42.69% relative CPU reduction. Wire traffic fell by about
18.95%, from about 208.89 MB to 169.32 MB per run. Exact waiter registrations
fell from a peak of 256 to 32 because each customer now waits for one next
bundle instead of keeping eight future requests in flight.

The compact-header candidate alone reduced CPU and bytes but did not remove
the rare tail. The persistent response removes repeated route dispatch, request
task creation, QPACK field sections, and QUIC request-stream lifecycle work.

## Capacity boundary

Every tier remained byte-perfect. The strict zero-outlier gate, not media loss
or CPU saturation, set the current boundary.

| Customers x tracks | Tails/vCPU | Valid units | Availability p99 | Edge host CPU | Strict result |
| --- | ---: | ---: | ---: | ---: | --- |
| 32 x 8, two runs | 128 | 2 x 1,024,000 | 12.627–12.734 ms | 14.336–16.779% | pass twice |
| 64 x 8, run 1 | 256 | 2,048,000 | 13.037 ms | 21.270% | pass |
| 64 x 8, run 2 | 256 | 2,048,000 | 13.245 ms | 20.807% | fail: 254 late bundles |
| 96 x 8 | 384 | 3,072,000 | 13.839 ms | 27.776% | fail: 565 late bundles |
| 128 x 8 | 512 | 4,096,000 | 22.948 ms | 33.921% | fail: 3,881 late bundles |

The probe retains at most 512 detailed late-bundle rows. Counts above that
bound come from the exact track deadline counter divided by eight.

The 64-customer tier proves that 256 tails/vCPU is attainable in a clean run,
but its repeat failed the zero-miss gate. It remains a provisional capacity
canary. The 96-customer p99 stayed well below 20 ms while rare responses missed,
which shows that the next optimization is scheduling-tail control rather than
bulk throughput.

## Topology and workload

All six roles ran on separate two-vCPU `n2-standard-2` instances in
`europe-west2-c`:

```text
10.84.10.4 DAW source
  -> 10.84.10.5 contributor
  -> 10.84.10.7 and 10.84.10.8 relays
  -> 10.84.10.6 playback edge
  -> 10.84.10.9 reader
```

Media and load stayed on private `10.84.0.0/16` addresses. IAP carried only
orchestration and evidence. The source used five unique Lori Asha
`CONFIRMATION` stems and three repeated slots. Each slot was encoded,
protected, published, replicated, tailed, and validated independently.

Each run used a 20-second measured media window, 750 ms deterministic customer
arrival spread with seed `424242`, a 5,000 ms source offset, and a strict 20 ms
deadline. Chrony offset and dispersion gates ran on all hosts before and after
each attempt.

## Qualification and limits

Local correctness covered the framing codec, split and coalesced transport
chunks, zero and oversized frame rejection, mesh route behavior, and all 20
probe tests. The GCP test exercised the actual H3 response stream and complete
private media path. Strict probe Clippy reaches an unrelated existing large
enum warning in `audio_epoch_hls.rs`; the changed code adds no warning.

The result does not yet prove a 30-minute soak, repeated 64-customer strict
behavior, slow-reader handling, cancellation churn, or production sizing.
Those remain the next gates.

