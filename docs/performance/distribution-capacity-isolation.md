# Isolated LL-HLS distribution-capacity qualification

This qualification precedes the next distributed mesh endurance run. It tests
the playback edge at its code boundaries so cache lookup, route construction,
HTTP/3, encryption, kernel UDP, network bandwidth, and the load generator are
not mistaken for one another.

The test uses 48 kHz S24 PCM throughout. It does not deploy a contributor,
relay DAG, or remote mesh until the isolated gates pass.

## Copyable implementation prompt

```text
Work in the wavey.ai workspace and build a reproducible, pure-Rust capacity
qualification for Needletail's LL-HLS distribution edge. Do not deploy a mesh
or use cloud capacity during the isolation phase. Preserve unrelated worktree
changes and follow every applicable AGENTS.md.

Test the real production code in playlists, av-mesh, and
the canonical `web-service` crate from `wavey-ai/web-services`. Do not replace
the edge with nginx, a synthetic HTTP
server, wrk, h2load, or another implementation. A static-response control may
use the production H2H3Server with a minimal Rust Router. The load generator
must also be Rust and must hold persistent TLS 1.3 HTTP/3 connections.

Use this exact media geometry:

- 48,000 samples/second, signed 24-bit PCM;
- 16 channels split into two 8-channel LL-HLS renditions;
- 5 ms parts, hence 200 part requests/second/rendition;
- 5,760 payload bytes/part/rendition before fMP4 and transport overhead;
- 400 part requests/second and 2,304,000 payload bytes/second per customer;
- 10,000 part requests/second and 57,600,000 payload bytes/second for 25
  customer equivalents.

Build one qualification command that runs the following boundaries separately
and writes one versioned JSON report with the component revisions, host CPU,
logical cores, duration, concurrency, payload bytes, requests, errors,
latency percentiles, CPU time, context switches, and resident-memory change:

1. ChunkCache: preseed realistic 5,760-byte Bytes values and measure exact
   get_for_stream_id hits under 1, 2, 4, and all available worker threads.
2. Playlist cache: preseed a realistic 5 ms LL-HLS window and measure cached
   playlist reads independently from playlist regeneration after a commit.
3. Router: preseed LiveTsCache and call AppRouter::route directly for playlist
   and part hits, with transport absent. Attribute demand tracking, response
   telemetry, path parsing, header construction, and cache lookup separately.
4. Static H3: run the production H2H3Server over loopback with a fixed Bytes
   response. Measure HTTP/3 stream/task/QPACK/QUIC/TLS cost without av-mesh.
5. Seeded edge H3: run one av-mesh playback edge with its cache populated by a
   deterministic in-process qualification fixture. Disable mesh peers and all
   contributor ingress. Fetch the real /live/{stream}/stream.m3u8,
   init.mp4, and future partN.mp4 routes over persistent H3.
6. Two-host H3 control, only after loopback gates pass: one playback edge and
   one Rust load generator, still with no contributor, relay, or mesh. This
   separates NIC/kernel/network limits from the application limits.

For media delivery, step customer equivalents through
1, 4, 8, 16, 24, 32, 48, 64, 96, and 128. Each customer must use two H3
connections/readers and consume both 8-channel renditions at realtime cadence.
Do not publish a second source when load increases. Stop a step when it has any
missing/non-contiguous part, deadline miss, H3 error, kernel UDP drop, or when
the server or generator reaches its CPU or link ceiling. Repeat the last clean
step for 10 minutes and report the first failed step; never hide it by quoting
only aggregate requests per second.

Run independent playlist-only tests for:

- request throughput on a stable cached playlist;
- regeneration throughput while parts commit every 5 ms;
- 1, 10, 100, 1,000, 10,000, and then progressively more concurrent blocked
  reloads, subject to available memory;
- idle persistent H3 connection count separately from active request rate.

Add functional, property, and fuzz coverage before trusting performance:

- cache generation/stream identity isolation across ring wrap and retirement;
- no stale bytes or hash after slot reuse;
- exact part bytes under concurrent readers and writers;
- no lost wakeup when registration races a part commit;
- fan-out of many waiters for one part and isolation between unrelated parts;
- bounded waiter cleanup after cancellation and timeout;
- manifest media sequence, part sequence, preload hint, init map, retention,
  duration, and discontinuity invariants for arbitrary valid commit sequences;
- malformed and extreme route paths, queries, sequence numbers, and stream IDs;
- H3 cancellation, slow-reader isolation, out-of-order stream completion,
  connection closure, and flow-control pressure;
- stable memory under a 30-minute write/read churn test.

Use property tests for state-space coverage and targeted fuzzers for untrusted
parsers. Use a deterministic concurrency regression or loom model for the
lookup/register/recheck waiter protocol. Fuzzing is not a substitute for the
throughput tests.

Test these current architectural hypotheses explicitly rather than assuming
the cache is at fault:

- AppRouter awaits request_replica_for_stream on every cached playlist and
  part request; DemandTracker takes a write lock even when its one-second gate
  declines the request.
- record_edge_response takes one shared mutex and allocates/clones strings for
  the recent-response ring on every /live request.
- cached playlists are returned as cloned String values, whereas part bodies
  use cheap Bytes clones.
- the H3 server spawns a Tokio task for every request stream.

For each hypothesis, measure the unchanged production path, isolate the cost,
then make the smallest architecture-correct improvement and rerun the same
matrix. Cache hits must not synchronously depend on replication planning.
Observability must remain bounded and useful without introducing a global
per-request serialization point. Keep exact correctness and TLS verification.

The report must distinguish:

- playlist reads/second;
- part responses/second;
- payload and wire Gbit/second;
- customer equivalents at the stated PCM geometry;
- cycles or CPU-nanoseconds per request and per payload byte;
- server saturation from load-generator saturation;
- single-core contention from total multicore capacity;
- p50/p95/p99/p99.9 service time and end-to-end response latency.

The isolated phase passes only when all correctness tests pass, measurements
are repeatable within 10%, no unbounded memory growth appears, the first
saturation point has a named resource and code boundary, and the chosen edge
size has at least 30% CPU and link headroom at its declared customer capacity.
Commit the test code and sanitized report to their owning repositories and add
a Needletail evidence note. Do not claim millions of active PCM customers;
report idle connections, blocked playlist reloads, playlist request rate, and
active media customers as separate capacities.
```

## Workload arithmetic

One eight-channel rendition carries:

```text
48,000 samples/s × 8 channels × 3 bytes = 1,152,000 bytes/s
1,152,000 bytes/s ÷ 200 parts/s = 5,760 bytes/part
```

A customer consuming both renditions therefore carries 2,304,000 payload
bytes/s (18.432 Mbit/s) and makes 400 part requests/s. The following figures
exclude fMP4 boxes, HTTP/3 framing, QUIC, TLS, UDP, IP, and retransmission:

| Customer equivalents | Part responses/s | PCM payload | PCM payload rate |
| ---: | ---: | ---: | ---: |
| 1 | 400 | 2.304 MB/s | 18.432 Mbit/s |
| 4 | 1,600 | 9.216 MB/s | 73.728 Mbit/s |
| 8 | 3,200 | 18.432 MB/s | 147.456 Mbit/s |
| 16 | 6,400 | 36.864 MB/s | 294.912 Mbit/s |
| 24 | 9,600 | 55.296 MB/s | 442.368 Mbit/s |
| 25 | 10,000 | 57.600 MB/s | 460.800 Mbit/s |
| 32 | 12,800 | 73.728 MB/s | 589.824 Mbit/s |
| 64 | 25,600 | 147.456 MB/s | 1.180 Gbit/s |
| 128 | 51,200 | 294.912 MB/s | 2.359 Gbit/s |

Channel count and viewer count are independent dimensions. A 256-channel
source split into 32 eight-channel renditions is 294.912 Mbit/s and 6,400
parts/s once on each mesh replication link. A viewer subscribing to all 256
channels adds the same payload and request rate at the edge. The mesh should
replicate each rendition once per edge regardless of geographical viewer
count; edge H3 egress scales with active viewers.

## Boundary matrix

| Boundary | Production code retained | Deliberately removed | What it proves |
| --- | --- | --- | --- |
| B0 arithmetic | PCM geometry and pacing | All services | Load generator is asking for the intended work |
| B1 chunk cache | `ChunkCache` ring and `Bytes` | Router, H3, network | Hit/write/retirement cost and correctness |
| B2 playlist cache | real manifest and cached playlist | Router, H3, network | Cached lookup versus regeneration cost |
| B3 router | `AppRouter`, telemetry, demand gate | H3 and kernel network | Application request cost and lock contention |
| B4 static H3 | production `H2H3Server`, TLS/QUIC | av-mesh cache/router | Transport request and byte ceiling |
| B5 seeded edge H3 | complete playback route | contributor, relays, mesh | Single-edge capacity without replication |
| B6 two-host edge | complete playback route and NICs | contributor, relays, mesh | Kernel/NIC/network ceiling and generator independence |
| B7 DAG | full system | Nothing | End-to-end behavior after isolated gates pass |

Every boundary writes the same result schema so the first material drop in
throughput or increase in CPU/latency is attributable to the layer just added.

## Functional and fuzz gates

The performance runner must refuse to publish a passing capacity when any of
these gates fail:

1. Every successful part response matches the requested stream, sequence,
   codec initialization, byte count, and payload digest.
2. Every customer receives a contiguous part interval with no duplicates.
3. A cache miss, future part, evicted part, malformed path, and retired stream
   produce their specified status without exposing another generation.
4. Concurrent commit/read/retire operations do not panic, deadlock, lose a
   wakeup, grow the waiter table without bound, or return stale data.
5. Slow or canceled H3 streams do not delay unrelated streams on the same or
   another connection.
6. Memory returns to a stable band after clients disconnect and after cache
   generations rotate.

Property and fuzz inputs are saved as regression fixtures. Random seeds,
component revisions, compiler version, target, and runner arguments are part of
the report.

## Performance gates

A clean capacity step requires:

- zero failed requests, missing parts, discontinuities, deadline misses, and
  payload mismatches;
- zero new kernel receive/send buffer errors and zero service restarts;
- p99.9 bounded by the test deadline with no connection starved by another;
- stable RSS after warm-up and bounded task/waiter counts after cancellation;
- the server below 70% of its CPU and measured link ceilings for a declared
  production capacity, leaving at least 30% headroom;
- a load-generator control showing the generator had at least 30% headroom;
- three repetitions whose throughput and CPU cost agree within 10%.

The test may intentionally exceed the gate to identify saturation. That first
failed step remains in the evidence and names the limiting resource.

## Current code hypotheses

These are inspection findings, not conclusions:

- Cached part bodies are `Bytes` clones, so media lookup itself should not copy
  5,760-byte payloads.
- Cached playlists clone a `String` per response, which allocates and copies.
- Every live request enters a write-locked demand gate before a cache hit can be
  served.
- Every live response enters a shared recent-response mutex and constructs
  owned telemetry strings.
- Each H3 request stream is handled in a newly spawned Tokio task.

B1 through B5 are designed to quantify these costs independently. Optimization
starts only after the measurements identify which one is material.

## Current qualification status: 18 July 2026

B4/B6 has now found and fixed one production H3 capacity bug. Ordinary media
GET responses carried two CORS fields intended for `OPTIONS` preflight
responses. On separate London generator and Amsterdam server hosts, both GCP
`n2-standard-2`, the method-aware fix raised the saturated 64-byte response
rate from a two-run mean of `71,946` to `79,702` responses/s (`+10.78%`) at the
same roughly `195%` server CPU. An immediate old-build reversal reproduced the
old ceiling. Wire bytes per response fell by `12.49%`.

At the 5,760-byte PCM geometry, two 40-customer runs remained exact and server
CPU fell from about `150.8%` to `147.2%`. A 56-customer run improved to
`99.9536%` completion but failed strict qualification with 104 missing requests
and ten generator backpressure events. It is not a capacity pass.

The same server completed exact 100-, 500-, and 1,000-connection steps at one
request/s/connection. This establishes that the configured 256 H3 streams are
not a global connection ceiling. It does not establish million-connection
capacity; the next connection-count runner must share its client UDP endpoint
and record server RSS and task count.

The Quinn/tokio-quiche comparison also completed. At the exact 40-customer
workload tokio-quiche used about `5.9%` more server CPU and emitted about `4.8%`
more packets; changing its configured UDP payload ceiling from 1,350 to 1,400
bytes had no measurable effect. Quinn remains the default and owns
WebTransport. Details and raw-artifact locations are in the
[18 July H3 isolation record](../real-world-tests/2026-07-18-h3-capacity-isolation.md).

## Resume condition for the mesh test

Resume the six-node 30-minute mesh baseline after B1 through B5 pass and B6 has
either passed or named the two-host network ceiling. The distributed harness
then uses one contributor publication, one replication copy per rendition per
edge, and viewer-only late-join bursts at 1, 4, 8, 16, and 24 customer
equivalents. It must not create a second publication to simulate viewers.
