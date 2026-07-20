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

The goal is still active because endurance and cancellation churn remain. The
persistent-response build now passes every short-window gate twice at 32
customers x 8 tracks. This is 128 realtime tails/vCPU. The 64-customer tier
passed once at 256 tails/vCPU but did not repeat the zero-miss gate.

### How close we are

The accepted measured tier is 32 customers, each tailing eight independent
Opus tracks over one persistent H3 connection and one persistent response:

```text
32 customers x 8 tracks = 256 realtime track tails
256 tails / 2 edge vCPUs = 128 tails/vCPU
1,024,000 validated 5 ms track units in each 20-second measured window
two consecutive accepted runs
```

| Goal gate | Latest evidence | State |
| --- | --- | --- |
| 128 realtime track tails/vCPU | 256 tails on one 2-vCPU `n2-standard-2` edge, repeated | Pass |
| Media integrity | 2 x 1,024,000 valid; zero missing, duplicate, malformed, or non-contiguous units | Pass |
| Final-part p99 <=20 ms | 12.626702 and 12.733804 ms | Pass |
| 30% CPU headroom | 14.336% and 16.779% host CPU; at least 83.220% headroom | Pass |
| Zero deadline misses | Zero in both accepted runs | Pass |
| Stable memory and cancellation cleanup | Short samplers passed and waiter registrations returned to zero; no sustained proof | Not yet proven |

The persistent response removed repeated route dispatch, task creation, QPACK,
and H3 request-stream lifecycle work. Against the matched 32 x 8
request-per-bundle baseline, mean edge-host CPU fell 42.69% and wire bytes fell
about 18.95%. Exact waiter registrations fell from 256 to 32.

The remaining rare-tail boundary is above the accepted target. At 64 x 8, one
run passed and one had 254 late bundles. The 96 x 8 tier had 565 late bundles,
and 128 x 8 crossed the p99 gate. Every tier remained byte-perfect. Next work
is a 30-minute 32 x 8 soak, cancellation and slow-reader churn, then scheduling
tail work above 32 customers.

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

This historical candidate was deployed and measured. It improved normal p99
and CPU but did not remove the rare tail. A compact-response A/B then reduced
wire bytes 3.46% and edge CPU about 5.3% relative to the request-per-bundle
baseline, but it also failed the zero-miss gate.

#### 5. Keep one bundle response open per customer

The current working-tree candidate adds `/live/tail-bundle-stream`. One request
receives repeated four-byte-length-framed `NTB1` envelopes. It removes route,
task, QPACK, and request-stream lifecycle work from the 5 ms cadence while the
existing endpoint remains compatible.

At 32 x 8, the candidate cut mean edge-host CPU from 27.147% to 15.558%, cut
wire bytes about 18.95%, and passed the zero-miss gate twice. All shared framing
tests, mesh route tests, and 20 probe tests pass locally.

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

- Tested `av-mesh` base: `8d87e80` plus the current duration, compact-response,
  and persistent-response working-tree patch.
- Tested edge and relay Linux SHA-256:
  `3e76ce6a7cb29990dbb9d3768631b35efbdd5c06ca9554af05e6cd5905910724`.
- Tested `av-contrib` base: `10f829e` plus the current probe and fMP4 fixes.
- Tested reader Linux SHA-256:
  `824a5523c2593195e840086c91a10b645e6d953191ee8be9ae534caa21d9ae15`.
- Raw persistent-response evidence root:
  `target/gcp-qualification/live-tail-serialization/profile/`.
- Versioned record:
  `docs/real-world-tests/evidence/20260720T045417Z-opus-h3-persistent-bundle-stream.json`.
- All media and load ran on same-zone private addresses. IAP carried only
  orchestration and evidence.

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
