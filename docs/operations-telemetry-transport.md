# Operations telemetry transport

Status: design contract. The dashboard changes can ship against the existing
bounded status endpoints; node transport changes require implementation and
qualification in the owning service and transport repositories.

## Requirements

Operations telemetry must provide a useful update within five seconds without
changing media latency, queueing, or deadline outcomes. It must continue to
arrive through ordinary packet loss and short path interruptions, but media and
control traffic always take priority.

The transport does not carry application logs. Nodes export bounded counters,
gauges, fixed-bucket histograms, topology state, and WARN/ERROR events. TRACE,
DEBUG, per-packet INFO records, stack traces, and unbounded labels are excluded.

## Collection at the service

Hot paths update atomics, bounded fixed-bucket histograms, and bounded recent
error rings. They do not allocate a telemetry event for each packet and do not
write telemetry to disk.

Every five seconds, a low-priority task copies the current values into a
snapshot and computes deltas from the previous acknowledged base. Topology and
build metadata are sent on change or every 30 seconds. WARN/ERROR events may
request an earlier flush, but flushes are coalesced to at most one per second.

Each snapshot is bounded before encoding:

- 32 KiB maximum uncompressed payload;
- 16 nodes, 12 streams, 12 edge services, and 16 events per source snapshot;
- stable identifiers only; no viewer, packet, object, or request ID labels;
- fixed histogram bounds shared by producers and the collector;
- strings truncated at the schema boundary.

If collection misses its budget, the task keeps the newest counters and drops
older unsent snapshots. It never waits in a media task or takes a lock held by a
media task.

## Wire envelope

The versioned envelope contains:

```text
schema_version
source_node_id
source_boot_id
sequence
base_sequence
observed_unix_ms
period_ms
snapshot_kind
payload_length
payload_crc32
payload
```

`source_boot_id` and `sequence` give the collector idempotent deduplication.
`base_sequence` identifies the counter base for a delta. A collector that lacks
the base requests or waits for the next full snapshot; it never invents a rate
from incompatible counters.

The payload should use a compact schema with explicit numeric field IDs and
bounded repeated fields. JSON remains the browser-facing representation, not
the node wire representation.

## FEC delivery

Telemetry reuses the generic-data path in `raptorq-datagram-fec` and the
authenticated `RelayTransport` sessions owned by `relay-session`:

```text
bounded telemetry envelope
-> generic RaptorQ data block
-> telemetry-priority RelaySession queue
-> existing authenticated datagram carrier
-> regional collector
```

Several small source snapshots may be batched into one coding block to avoid a
one-symbol block paying a full-symbol repair floor. A five-second batch has a
small fixed repair budget and a short expiry. The collector accepts the first
valid decode and discards duplicate symbols by source, boot, and sequence.

Telemetry uses a distinct subscription and queue from media. Its queue has room
for at most two coding blocks. Admission uses `try_send`; on congestion, the
newest snapshot replaces the oldest queued snapshot. Telemetry repair symbols
are the first telemetry work discarded. No telemetry send, repair, retry, or
collector outage may backpressure media source symbols, media repair symbols,
route control, or failover heartbeats.

Initial per-node limits:

- 32 Kbit/s sustained telemetry token bucket;
- 64 KiB maximum queued encoded data;
- two in-flight coding blocks;
- one full snapshot every 30 seconds, with five-second deltas between them;
- no reliable replay after the data is older than 30 seconds.

Dual delivery may use the node's existing independent parent sessions. It must
not create contributor-to-viewer links or a new all-to-all telemetry overlay.
Regional collectors aggregate and forward telemetry to the operations service.

## Collector and browser

The collector keeps the current snapshot and a short rate ring in memory. It
exposes one bounded fleet snapshot to the browser. Browser updates remain at a
five-second default cadence and may later use an aggregated event stream, but
the browser never opens one feed per node.

Raw five-second snapshots are not inserted into a database one row at a time.
If history is required, the collector creates one-minute aggregates in memory
and writes a batch in one transaction. The initial retention target is 15
minutes of raw in-memory samples and 30 days of one-minute aggregates. WAL and
database size are monitored as product metrics.

## Qualification gates

The transport is not ready for production until a media soak proves all of the
following with telemetry enabled and disabled under the same load:

- no measurable regression in media deadline misses or expired objects;
- relay processing p95 remains within the delivery-class budget;
- media queue p95 changes by less than 0.5 ms;
- telemetry collection and encoding consume less than 0.5 percent of one CPU
  core per node at the normal cadence;
- telemetry stays below its token-bucket and memory limits;
- sustained telemetry loss or collector outage cannot grow queues or memory;
- a 5 percent datagram-loss test still delivers at least 99 percent of
  five-second fleet snapshots within ten seconds;
- duplicate, reordered, reset, and missing-base sequences do not create false
  throughput spikes;
- database writes, when enabled, occur in one-minute batches rather than per
  node sample.

## Ownership and rollout

`av-contrib` and `av-mesh` own the bounded source snapshots. `raptor-fec` owns
generic FEC framing and repair geometry. `relay-session` owns queue admission,
priority, authentication, pacing, and expiry. Needletail owns aggregation,
deployment policy, qualification, and the operations UI.

Rollout order:

1. Record collection cost with transport disabled.
2. Send FEC telemetry to a shadow collector and compare it with current status
   endpoints.
3. Add loss, congestion, and collector-outage qualification.
4. Make the aggregated snapshot the default UI feed while retaining endpoint
   fallback for one release.
5. Enable one-minute persistence only after write-rate and retention gates pass.
