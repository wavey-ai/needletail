# 20 July 2026: clock-qualified Opus H3 tail repeatability

This series resolves the open strict-tail and cache-sample gaps from the 19 July
profile. It also tests a shared group waiter and rejects it for the current
release. The accepted v12 exact-envelope build passed the final private-GCP
workload twice with no late bundle.

The machine-readable record is
[`20260720T021843Z-opus-h3-clock-qualified-tail.json`](evidence/20260720T021843Z-opus-h3-clock-qualified-tail.json).
Raw reports remain under
`target/gcp-qualification/live-tail-serialization/profile`.

## Accepted result

Both final v12 repetitions passed the declared 20 ms response deadline. Each
run used 24 customers, eight tracks per customer, and one persistent H3
connection per customer. Every response carried one 5 ms part for all eight
tracks.

| Run | Parts | Responses | Late bundles | Availability p99 | Cache-to-client p99 | Edge host CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `20260720T021203Z-24x8-strict20-corrected-client-v12-r1` | 2,304,000/2,304,000 | 288,000 | 0 | 13.628 ms | 4.947 ms | 32.951% |
| `20260720T021843Z-24x8-strict20-corrected-client-v12-r2` | 2,304,000/2,304,000 | 288,000 | 0 | 13.694 ms | 5.058 ms | 34.084% |

Both runs had all 192 cache samples. They had zero missing parts, PTS errors,
Opus packet mismatches, reader failures, and deadline misses. Their maximum
availability-p99 spread is 0.066 ms.

This qualifies short-window repeatability for the tested 24-customer geometry.
It does not qualify endurance or a production tier.

## Private topology and clock gate

All six roles ran on separate two-vCPU `n2-standard-2` instances in
`europe-west2-c`. The active media path was:

```text
10.84.10.4 DAW source
  -> 10.84.10.5 contributor
  -> 10.84.10.7 and 10.84.10.8 relays
  -> 10.84.10.6 playback edge
  -> 10.84.10.9 reader
```

Media and load traffic stayed on the private `10.84.0.0/16` VPC. IAP carried
only orchestration and evidence retrieval. No test media crossed the public
Internet or an inter-region link.

Chrony on every host used the GCP metadata time server at `169.254.169.254`.
The harness rejected a run unless absolute system offset and root dispersion
were each at most 1 ms before and after the test. Final run 1 stayed within
0.003088 ms offset and 0.145413 ms dispersion. Final run 2 stayed within
0.003014 ms offset and 0.135204 ms dispersion.

The reusable setup is in `deploy/gcp-lab/configure-clock.sh` and
`deploy/gcp-lab/chrony-gcp.conf`. `scripts/gcp-live-tail-profile.sh` captures
the six-host clock, service, journal, API, load, and flat-profile evidence.

## Workload

The matched workload used:

- 24 persistent H3 connections;
- 192 track readers;
- 60 measured seconds inside an 80-second reader process;
- one 5 ms part per track and bundle response;
- 288,000 bundle responses and 2,304,000 exact track parts;
- explicit 20 ms capture-to-completed-response deadline;
- deterministic 750 ms arrival spread with seed `424242`;
- 5,000 ms publication offset;
- SoundKit v2 Opus validation for every part; and
- Linux `cpu-clock` sampling at 199 Hz for about 65 seconds.

The deadline counter is per track part. One late eight-track response therefore
adds eight misses. Tables in this record divide that counter by eight.

## Why the earlier failures were misleading

The first clock-qualified v12 runs restored all 192 cache samples. They still
reported 89 and 73 late bundles. A shared group-waiter candidate then reported
42 and 60 late bundles. All runs remained byte-complete and had availability
p99 below 13.83 ms.

A bounded v6 diagnostic added at most 512 late-response rows. Each row records
capture, cache-availability, and reader-arrival times. The diagnostic observed
41 late bundles:

| Stage | Minimum | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| publication to edge cache | 5.005 ms | 9.499 ms | 9.889 ms | 9.889 ms |
| edge cache to reader | 12.969 ms | 15.326 ms | 16.544 ms | 16.845 ms |
| full availability | 20.009 ms | 23.821 ms | 26.349 ms | 26.734 ms |

All 41 events were delivery-dominant. They affected 18 customers and only six
sequences from 13034 through 13131. These sequences fell inside the staggered
customer-completion interval.

The probe performed eight serial playlist checks and multiple percentile sorts
inside each customer task after that customer's media ended. Those operations
overlapped media work for customers that started later. This created reader
and edge work inside another customer's measured interval.

Probe v6 now:

- requires `--deadline-ms` on every load invocation;
- verifies playlists before each customer's timed media window;
- defers percentile sorting until every media receive task has stopped; and
- retains a bounded late-bundle stage split when a deadline is missed.

The first corrected v14 run reduced 328 track misses to 8. This is one bundle.
Its repeat had zero. The corrected v12 control then passed twice with zero.

## Reader profile

Run `20260720T012756Z-24x8-strict20-instrumented-reader-v14-r2` attached Linux
`perf` and a one-second process sampler to the reader. The media result remained
exact, but the run is diagnostic only because the observer changed the client.
The harness also completed retention manually after `perf` returned 143 when
the reader exited normally.

The reader used 37.454% of one logical CPU, or 18.727% of the two-vCPU host,
over 68.483 seconds. Its flat profile was led by Tokio timeout polling, H3
QPACK decode, allocation, packet CRC validation, soft IRQ work, Quinn transmit,
and `recvmmsg`. The reader was not continuously CPU-saturated.

The harness now accepts reader `perf` status 143 as a normal profiled-process
exit. It still fails on any other nonzero profiler status.

## Group-waiter A/B decision

The v13 and v14 candidates replaced eight sequential exact waits with one
shared stream-group barrier. The implementation had fixed capacities, weak
registrations, replay-safe exact identities, arbitrary commit order, and no
retained group state after each run.

The corrected probe produced this direct A/B:

| Build | Repetitions passed | Late bundles | Mean availability p99 | Mean cache p99 | Mean edge host CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| v12 sequential exact waits | 2/2 | 0 | 13.661 ms | 5.003 ms | 33.518% |
| v14 shared group waiter | 1/2 | 1 | 13.760 ms | 5.050 ms | 31.940% |

V14 used 4.707% less mean host CPU. It was 0.724% slower by availability p99
and 0.941% slower by cache-to-client p99. More importantly, it did not repeat
the strict zero-miss result.

The group waiter is rejected for the current release. Its CPU direction is a
future optimization lead, not a release result. The accepted v12 binary is
SHA-256
`58fd48fb1c59905bac55a4d18c89b553e6d50c6ab41b96cad4afa55578f8fd0b`.
It is active on both London relays and the edge.

## Invalid and excluded attempts

- `20260720T003849Z-24x8-strict20-clock-qualified-v12-r1` stopped after the
  before-clock capture. It launched no source, reader, or profiler and has no
  media result.
- `20260720T012451Z-24x8-strict20-instrumented-reader-v14` stopped before
  instrumentation because the controller expanded a remote `awk` field. It
  has no media result.
- `20260720T012756Z-24x8-strict20-instrumented-reader-v14-r2` completed all
  media checks but is excluded from candidate comparison because reader `perf`
  was active and retention needed manual completion.

Every retained run names its binary, probe schema, clock bounds, media totals,
latency, CPU, and inclusion status in the JSON evidence.

## Local qualification

Local testing covered correctness, not load:

- playlists: 56 tests passed;
- `av-mesh`: 43 library, 104 service, and 8 authorization tests passed;
- three release-only `av-mesh` capacity tests remained ignored by design;
- the changed `av-mesh` target passed strict Clippy;
- the probe passed compile checks and all 20 tests; and
- playlists passed all-target, all-feature strict Clippy.

The probe's strict Clippy run suppressed two existing repository lints: one
large enum in `audio_epoch_hls.rs` and one legacy argument-count warning. No
new probe warning remained.

## Deployment and cleanup

The rejected v14 binaries remain in rollback directories. The active relays
and edge use the accepted v12 binary and are supervised by `systemd`. All test
source and reader processes exited after each retained run.

The GCP lab remains available for endurance work. Public FEC telemetry remains
disabled. The public operations UI and low-rate API are not part of the media
load path.

## Next gates

1. Run the accepted v12 geometry for at least 30 minutes. Record periodic RSS,
   CPU, waiter occupancy, and exact media counters.
2. Exercise connect, cancel, timeout, and slow-reader churn without an edge
   restart. Prove that waiter, request, stream, and memory state remain bounded.
3. Repeat the accepted build geographically after endurance and churn pass.
4. Revisit the v14 CPU direction only as an isolated strict-latency candidate.

Twenty-four customers remain a short-window candidate until the endurance and
churn gates pass.
