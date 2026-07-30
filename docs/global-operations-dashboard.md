# Global Needletail Operations dashboard

## Outcome

Needletail Ops has one active global telemetry collector at a time. Static UI
assets may be served from every playback edge, but only the elected collector
accepts fleet snapshots and publishes the global `/api/mesh` view. A follower
redirects a browser to the current collector or returns a bounded unavailable
response when the cluster cannot prove ownership.

The design prefers stale or unavailable operations data over two active
collectors. Media forwarding and playback remain independent of dashboard
leadership.

## Coordination model

Run an odd-sized controller quorum on three or five node agents in distinct
failure domains. The controller log stores one `operations_collector` record:

```text
term
leader node id
lease expiry in the controller clock domain
fencing generation
public operations endpoint
telemetry ingest endpoint
```

Use a maintained Raft implementation rather than a peer-scored election or
last-writer-wins lease. A candidate becomes collector only after a quorum
commits its new term and fencing generation. It stops accepting snapshots and
serving a healthy global view as soon as it loses quorum or cannot renew before
the local safety margin.

Three voters are sufficient for qualification and tolerate one node failure.
Five voters are preferable for production maintenance. With only GCP and Azure,
a quorum cannot remain available through the complete loss of either provider
without giving one provider a majority. Production therefore needs a third
failure-domain witness or must explicitly choose which whole-provider outage
loses control-plane availability. This is a CAP tradeoff, not an election-timer
setting.

## Telemetry flow

Each service creates one bounded snapshot at the existing coarse cadence. The
node agent keeps the latest unsent snapshot and the source identity tuple:

```text
node id
boot id
monotonic sequence
observed time
schema version
```

The committed collector assignment contains one ingest target. A node sends
each sequence to that target only. On a leadership change it drops the old
connection, resolves the newly committed target, and may retry its latest
snapshot. The collector de-duplicates `(node id, boot id, sequence)`, so a retry
cannot double-count counters or events.

Raw telemetry is not replicated between collectors and is not written per
sample to a database. A new collector rebuilds the fleet view from fresh node
snapshots within two normal collection intervals. If later history is required,
the active collector writes one aggregated minute batch with the fencing
generation; storage rejects a stale generation.

## Split-brain fences

A node may call itself the global collector only while all of these are true:

1. its term and generation are committed by quorum;
2. its quorum lease is still valid outside a conservative clock/error margin;
3. the requested ingest and browser API carry its current generation;
4. the node agent has not observed a higher term;
5. any durable aggregate write succeeds conditionally on that generation.

An isolated former leader self-fences. It closes ingest listeners, marks its
local view unavailable, and returns the new endpoint when known. DNS or a load
balancer is discovery only; it never grants leadership.

## Needletail Ops changes

Add a compact collector status block near data freshness:

- collector node and region;
- term and fencing generation;
- quorum health and voter count;
- lease time remaining;
- last leadership change and reason;
- nodes current, stale, and awaiting first snapshot.

Add election and transport events to the existing Activity page. Keep the full
term history and voter detail on Nodes. The Overview page shows only the current
state and the next useful action.

## Qualification plan

Use the ten-node GCP/Azure lab for the first implementation and run these gates:

1. **Normal operation:** exactly one collector accepts snapshots; every source
   sends one snapshot per cadence; the global view contains ten unique nodes.
2. **Leader process failure:** a new collector is committed within the target
   failover window and the view rebuilds within two collection intervals.
3. **Old-leader partition:** the isolated leader self-fences before a majority
   leader accepts traffic. There is no overlapping accepted generation.
4. **Minority partition:** minority candidates cannot open ingest or publish a
   healthy view.
5. **Delayed packets and retries:** old-term and duplicate sequences are
   rejected without changing counters.
6. **Clock disturbance:** lease safety remains correct across the qualified
   clock-error bound; an uncertain node fences itself.
7. **Collector outage soak:** media deadlines, queues, and memory remain within
   the existing gates while telemetry is unavailable.
8. **Churn soak:** repeated elections keep retained state bounded and produce no
   duplicate events or per-sample database writes.

The initial GCP/Azure deployment proves single-node failures and network
partitions. A third-provider witness is required before claiming
whole-provider control-plane availability.

## Cloudflare control-surface option

Cloudflare is suitable for the HTTP operations surface, not for Needletail's
media nodes. A Worker can serve the UI and route API requests, and a Container
can run an existing Linux HTTP aggregator. Direct inbound UDP, QUIC, RIST, and
SRT listeners remain on Rocky Linux hosts. Container disk and placement must
also be treated as ephemeral.

A Durable Object could replace the node Raft quorum as the single lease and
fencing authority if an externally hosted authority is acceptable. It must not
run as a second concurrent authority beside Raft. The implementation chooses
exactly one authority, includes its generation on every ingest and aggregate
write, and leaves the media plane operating when that authority is unavailable.
