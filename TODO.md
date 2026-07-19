# Needletail engineering TODO

## Live Opus cache-to-H3 capacity qualification

Status checkpoint: 19 July 2026

### Objective

Eliminate live cache-to-H3 serialization and cancellation bottlenecks on
isolated same-region GCP machines and prove all of the following at the same
time:

- at least 128 realtime Opus track tails per edge vCPU;
- zero missing, duplicate, malformed, or non-contiguous media units;
- zero responses later than the 20 ms deadline;
- final-part p99 no greater than 20 ms;
- stable memory and cleanup under sustained load; and
- at least 30% edge CPU headroom.

The goal is still active. The short high-load test passes scale, media
integrity, p99, and CPU headroom. It does not yet pass the zero-deadline-miss
gate or prove sustained memory stability.

### How close we are

The latest measured tier is 32 customers, each tailing eight independent Opus
tracks over one persistent bundled H3 connection:

```text
32 customers x 8 tracks = 256 realtime track tails
256 tails / 2 edge vCPUs = 128 tails/vCPU
1,024,000 validated 5 ms track units in the 20-second measured windows
```

| Goal gate | Latest evidence | State |
| --- | --- | --- |
| 128 realtime track tails/vCPU | 256 tails on one 2-vCPU `n2-standard-2` edge | Pass for the short run |
| Media integrity | 1,024,000/1,024,000 valid; zero missing, duplicate, malformed, or non-contiguous units | Pass |
| Final-part p99 <=20 ms | 13.714731 ms with body-completion timestamps | Pass |
| 30% CPU headroom | 60.1% machine CPU, or 39.9% headroom | Pass for the short run |
| Zero deadline misses | 1,616 late track observations, representing 202 late eight-track bundle responses | Fail |
| Stable memory and cancellation cleanup | About 68 MB RSS and three threads in short snapshots; no sustained proof | Not yet proven |

The remaining deadline defect is small but real: 202 of 128,000 bundle
responses were later than 20 ms, a 0.158% miss rate. The p99 is comfortably
inside the gate, so this is a rare-tail problem rather than a throughput
collapse. Four of the six runtime gates pass at the full target load. We are
one targeted client A/B and one sustained qualification away if the committed
H3 pipeline removes the rare tail; further server work is required if it does
not.

Do not describe the goal as production-qualified until every gate passes in
one sustained run.

### Meaningful fixes already proven

#### 1. Remove retained-cache scans from the live request path

`av-mesh` commit `0fca9db` (`Remove live cache scans from tail path`) is pushed.

The old path synchronously planned replication for each fresh tail request,
scanned the retained media window several times for mesh snapshots, and ran a
global `HashMap::retain` on every committed part. The fix:

- bypasses replication planning for a complete, fresh local stream;
- maintains a bounded per-stream `BTreeMap` part index and retained-byte
  counters;
- makes normal gap checks O(1) and mesh snapshots O(streams), not O(parts);
- stops telemetry from rescanning complete media windows; and
- keeps cache accounting exact as units are inserted and evicted.

Identical eight-track before/after canary:

| Metric | Before | After |
| --- | ---: | ---: |
| Final-part p99 | 138.556947 ms | 12.611894 ms |
| Cache-to-client p99 | 141.948370 ms | 7.043710 ms |
| Deadline misses | 2,648 | 0 |
| Valid media | not the limiting issue | 32,000/32,000 |

This was the largest improvement: it removed an accidental dependency between
request latency and retained-cache size.

#### 2. Remove repeated per-track bundle locks and metadata writes

`av-mesh` commit `936f396` (`Reduce bundled live-tail lock contention`) is
pushed.

The bundled route previously performed eight separate freshness checks, eight
global media-kind write-lock acquisitions, and eight separate availability
state locks for each eight-track response. The fix:

- checks freshness for every requested track under one state lock;
- avoids rewriting already-known media metadata on every bundle response; and
- reads availability for the complete track bundle under one state lock.

At 32 customers x 8 tracks on the two-vCPU edge:

| Metric | Before | After |
| --- | ---: | ---: |
| Machine CPU | 69.6% | 60.1% |
| CPU headroom | 30.4% | 39.9% |
| Final-part p99 | 13.687004 ms | 13.549974 ms |
| Cache-to-client p99 | 9.303397 ms | 8.055379 ms |
| Track deadline observations | 1,712 | 1,512 |

This is a 14% relative reduction in server CPU at the full target load.

#### 3. Correct the load probe's receive timestamp

`av-contrib` commit `4eafa1c` records H3 body completion before bundle decode,
validation, and task rescheduling. This stops client parsing or preemption from
being reported as server/network delivery latency. The corrected 32 x 8 rerun
still measured 13.714731 ms final-part p99 and 202 late bundle responses, so
probe parsing was not the cause of the remaining tail.

#### 4. Pipeline future exact H3 bundle requests

`av-contrib` commit `4eafa1c` also replaces the bundled probe's serialized
request/response loop with eight in-flight exact future-tail requests on the
same persistent H3 connection. Playlist qualification now runs after the timed
media loop instead of pausing live delivery for eight serial metadata reads.

This matches the intended multiplexed H3 access pattern. The direct per-part
probe already used a depth-eight pipeline; the bundled path accidentally did
not. All 19 `aep1-48k-probe` tests pass locally.

This commit is clean but one commit ahead of `origin/main`. It has not been
pushed, built for Linux, deployed, or measured on GCP. Its performance effect
therefore remains unproven.

### Supporting measurements

The same fixed server build remained inside the 20 ms p99 gate at lower tiers:

| Customers x tracks | Total tails | Tails/vCPU | Valid units | Final p99 | Cache-to-client p99 | Late bundles |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 x 8 | 128 | 64 | 512,000 | 13.003 ms | 5.821 ms | 48 |
| 24 x 8 | 192 | 96 | 768,000 | 13.335 ms | 7.055 ms | 70 |
| 32 x 8 | 256 | 128 | 1,024,000 | 13.715 ms | 7.215 ms | 202 |

The 24-customer stage telemetry also showed:

- all 48,024 observed publications reached the edge;
- zero unusable publication clocks;
- relay processing at or below 4.325 ms in the captured maximum;
- publication-to-edge at or below 9.038 ms in the captured maximum; and
- most H3 responses at or below 10 ms, with a small 10-25 ms tail.

This clears source loss, relay loss, malformed Opus, and general mesh backlog
as explanations for the remaining rare deadline misses.

The test uses eight track slots populated from five unique Lori Asha
`CONFIRMATION` stems plus three symlinked repeats. Every slot is independently
encoded, protected, published, replicated, tailed, and validated; do not claim
that the source material contains eight unique performances.

### Rejected experiment

A single-waiter bundle experiment tried to wait on only one missing stream per
bundle. It regressed because tracks arrive in variable order:

- edge CPU increased to 65.3%;
- final-part p99 increased to 14.835 ms; and
- track deadline observations increased to 1,848.

The experiment was reverted and was not committed. Do not reintroduce it
without a true group-completion barrier.

### Current revisions and infrastructure

- Proven edge and relay source: `av-mesh` `936f396` with `0fca9db` immediately
  below it; the repositories are clean and pushed.
- Proven edge/relay Linux binary SHA-256:
  `60c653040b82576da63d8ff0ce9c27d4bfb7a9ff5419d6f4858a68148aeaadde`.
- Previously deployed reader binary SHA-256:
  `ab52454b714c9392c6d18d40042eb808d941f8b3637c053dea5df6f3616fca7d`.
  It contains the receive-timestamp correction but not the new bundle
  pipeline.
- Pending probe source: `av-contrib` `4eafa1c`, clean and one commit ahead of
  `origin/main`.
- Local evidence root:
  `target/gcp-qualification/live-tail-serialization/20260719T004643Z/`.
- Exact private `gen-id` Cargo Git objects are packaged for the next Linux
  build at `build/cargo-gen-id-cache.tar.gz` under that evidence root. Its
  SHA-256 is
  `4aafb5d6ece92490274bc60375df8216a5e6671617fc8b92d10fbe60f52637de`.
- The most recent short-run JSON and source logs remain on the persistent GCP
  boot disks under `/tmp`; retrieve and normalize them after the next start.
  The summary numbers above were captured from the live command outputs, but
  the raw files are not yet versioned evidence.

All Needletail GCP test instances were stopped and confirmed `TERMINATED` on
19 July 2026:

- `nt-daw-lon`
- `nt-contrib-lon`
- `nt-relay-a-lon`
- `nt-relay-b-lon`
- `nt-edge-lon`
- `nt-opus-reader-lon`

The older Tokyo, Sydney, Amsterdam, and Osaka Needletail instances are also
terminated. The two temporary Linode load machines were deleted earlier. GCP
boot disks remain, so storage still incurs a small charge even though compute
is stopped.

### Important test-incarnation rule

Back-to-back DAW sessions are not new canonical publication incarnations while
`av-contrib` remains running. Its canonical source epoch and sequence space are
process-lifetime state by design. A probe that starts again at canonical
sequence zero without restarting the contributor is invalid and previously
produced a false 1.6-second p99 result.

For every clean qualification, stop the contributor, edge, and both relays,
then start them in this order:

1. both relays;
2. edge;
3. contributor;
4. DAW test source and isolated reader with a future session timestamp.

Explicit publication-incarnation signaling remains a separate reliability
improvement.

### Outstanding work, in order

- [ ] Push `av-contrib` commit `4eafa1c` when ready to resume.
- [ ] Produce the Linux probe binary from exactly `4eafa1c` and record its
  SHA-256. The stopped reader was prepared with Rust/build tools, but its build
  could not authenticate the private SSH `gen-id` dependency. The exact Git
  object cache is now packaged in the evidence root; transfer it into the
  reader's Cargo home before building. Do not change dependency revisions
  merely to make the build pass.
- [ ] Start only the six London test instances and retrieve the raw result
  files currently on their persistent disks before running anything else.
- [ ] Deploy the new probe to `nt-opus-reader-lon`; keep the proven
  `936f396` server binary unchanged for a controlled A/B.
- [ ] Clean-restart the media path and repeat 32 customers x 8 tracks at least
  three times. Each run must show 1,024,000 valid units, zero integrity errors,
  zero deadline misses, final-part p99 <=20 ms, and edge CPU <=70%.
- [ ] If misses remain, run the connection/track isolation matrix:
  - 32 customers x 8 tracks: 256 tails, 32 H3 connections;
  - 32 customers x 4 tracks: 128 tails, 32 H3 connections; and
  - 64 customers x 4 tracks: 256 tails, 64 H3 connections.
  This separates per-track/cache work from per-connection H3 work without
  lowering the final 128-tails/vCPU target.
- [ ] If the pipeline does not remove the rare tail, capture an edge flamegraph
  and per-stage response histogram at the 32 x 8 tier. Inspect exact waiter
  wakeup, future polling, response body construction, QPACK, allocation, QUIC
  send, and kernel UDP time before changing architecture again.
- [ ] Run a connect/disconnect sawtooth at the passing tier and prove waiter,
  timer, connection, task, and RSS counts return to a stable baseline after
  each wave. This is the explicit cancellation-cleanup gate.
- [ ] Run the passing 256-tail tier for at least 30 minutes with per-minute CPU,
  RSS, thread, connection, waiter, deadline, integrity, packet-drop, and restart
  counters. Require zero missing data, zero deadline misses, p99 <=20 ms, RSS
  within a stable band, no restarts, and CPU <=70% for the whole run.
- [ ] Copy raw evidence off every machine, generate a versioned summary under
  `docs/real-world-tests/evidence/`, and update
  `docs/performance/current-state-and-gaps.md`. That document is dated 18 July
  and still describes the pre-fix four-customer boundary; it must not be used
  as the current best-capacity result.
- [ ] Commit and push the final evidence and documentation, then stop the GCP
  instances again and verify that no Needletail compute remains running.
