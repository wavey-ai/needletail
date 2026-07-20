# Operations telemetry transport

Status: implemented on `main` for local and controlled-private qualification.

The lane is opt-in in `av-mesh`. The Needletail supervisor configures the two
remote relays to send snapshots to the playback edge, which remains the single
fleet aggregator and browser feed.

## Runtime behavior

Each node takes one bounded snapshot every 5 seconds. The same producer serves
the compatibility TCP path and the FEC path, so enabling both does not collect
the snapshot twice. TCP admission and UDP transmission are non-blocking with
respect to the producer.

The FEC payload is named MessagePack. The browser-facing `/api/mesh` response
remains JSON. The versioned envelope contains:

```text
magic and schema version
snapshot kind
source node id
source boot id
monotonic sequence
observed Unix time
period
payload length
payload CRC32
payload
```

The complete envelope is limited to 32 KiB before FEC encoding. Empty payloads,
invalid node ids, unsupported versions or kinds, length mismatches, bad CRCs,
and unexpected RaptorQ geometry are rejected.

## FEC delivery

`av-mesh` uses the shared `raptorq-datagram-fec` encoder and decoder with
1,152-byte symbols and a default repair budget of 20 percent. Source symbols are
sent before repair symbols.

The sender retains at most two envelopes and 64 KiB. A new snapshot replaces
the oldest queued snapshot at the bound. If a new snapshot arrives while repair
symbols are being sent, the remaining repair work is skipped. UDP uses
`try_send_to`; socket backpressure drops telemetry instead of waiting.

The total send rate across configured collectors defaults to 32 Kbit/s. The
pacer spaces datagrams and does not accumulate a catch-up burst after a delay.
There are no per-datagram log records and this path performs no database writes.

The collector bounds state before allocating a decoder:

- 256 active UDP peers;
- two in-flight FEC blocks per peer;
- 15-second incomplete-block expiry;
- 32 KiB declared transfer length;
- fixed 1,152-byte symbol geometry;
- one stable node identity per UDP source;
- duplicate suppression by node, boot id, and sequence.

Completed snapshots enter the existing in-memory `TelemetryAggregator`.
`/api/mesh` and `/metrics` expose queue, replacement, encode, send, receive,
decode, duplicate, and error counters. The browser does not open per-node
connections.

## Configuration

Collector:

```text
--telemetry-fec-bind 0.0.0.0:27300
```

Node:

```text
--telemetry-fec-target COLLECTOR_IP:27300
--telemetry-snapshots-fec-only
```

`--telemetry-snapshots-fec-only` disables snapshot publication on TCP but keeps
the TLS/TCP feed active for control commands. Without that flag, TCP and FEC
snapshots run together for shadow comparison.

The current direct UDP lane is for controlled private networking. Public-path
delivery must move the same bounded envelope and queue policy onto an
authenticated RelaySession datagram carrier before it is enabled.

## Resource policy

Hot paths update existing atomics, fixed histograms, and bounded state. The
snapshot task reads those values every 5 seconds. TRACE logging is not required
or enabled by this feature. Snapshot queue saturation, socket congestion,
malformed traffic, and collector outages cannot backpressure media, route
control, or failover work.

Raw snapshots remain in memory. There is no telemetry database in this path. If
history is added later, persistence must aggregate in memory and use one batched
transaction per minute rather than inserting each node sample.

## Tests and production gates

Automated coverage currently proves:

- envelope round trips, CRC rejection, and the 32 KiB bound;
- one lost source symbol is recovered by repair;
- a 200-snapshot deterministic 5 percent loss corpus delivers at least 99
  percent of snapshots;
- source-only ordering decodes without repair;
- duplicate sequences are rejected and a new boot id is accepted;
- peer, in-flight block, queue-byte, and queue-block bounds;
- stale peer and block expiry;
- oversized FEC geometry is rejected before decoder state is retained;
- real UDP sender-to-collector ingestion reaches the existing aggregator;
- the configured wire-rate pacer delays transmission as expected;
- TCP snapshots and control commands remain compatible.

On July 19, 2026, the release-mode isolated encoder qualification on an Apple
M1 completed 1,000 MessagePack + envelope CRC + RaptorQ encodes in 222.8365 ms:
222.836 microseconds per snapshot and 4,752 encoded wire bytes per iteration.
At one snapshot every 5 seconds, the measured encode work is about 0.0045
percent of one core. Snapshot collection and an enabled-versus-disabled media
soak remain separate production gates.

Production enablement still requires an enabled-versus-disabled media soak with
the same traffic and impairment trace. Required gates:

- no regression in media deadline misses or expired objects;
- relay processing p95 remains within its delivery-class budget;
- media queue p95 changes by less than 0.5 ms;
- collection and encoding use less than 0.5 percent of one CPU core per node at
  the normal cadence;
- a collector outage cannot grow queue or memory beyond the fixed limits;
- 5 percent datagram loss delivers at least 99 percent of snapshots within 10
  seconds;
- no database writes are introduced;
- public rollout uses an authenticated RelaySession carrier.

## Rollout

1. Run TCP and FEC snapshots together and compare aggregate output.
2. Run controlled loss, congestion, and collector-outage qualification.
3. Enable FEC-only snapshots on controlled private nodes.
4. Move the envelope to authenticated RelaySession carriers before public use.
5. Add one-minute persistence only after write-rate and retention gates pass.
