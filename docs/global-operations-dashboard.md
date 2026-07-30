# Global Needletail Operations

## Outcome

The target state has one active global telemetry collector at a time. Static
UI assets may be served from every playback edge, but only the elected
collector accepts fleet snapshots and publishes the global `/api/mesh` view. A
follower redirects a browser to the current collector or returns a bounded
unavailable response when the cluster cannot prove ownership.

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

## Global entry point

Use `https://<global-operations-host>/.well-known/needletail-operations` as the
stable discovery URL. `GET /` has the same behavior for an operator.

The `needletail-ops-entrypoint` service reads one controller assignment from a
local file. The controller must replace the file atomically after a quorum
commit. The assignment contains these values:

```text
schema version
authority
term and fencing generation
leader node and region
online and total voters
commit time and lease expiry
public Operations endpoint
```

The service returns `307 Temporary Redirect` only for a safe assignment. It
checks the odd voter set, majority, term, generation, clock limit, and lease
limit. It also checks the public endpoint against a fixed administrator
allowlist. The endpoint must use HTTPS and the exact `/mesh` path.

The service returns `503 Service Unavailable` if it cannot prove the
assignment. It sends `Cache-Control: no-store` with all responses. It does not
select a leader and it does not extend a lease.

Run this HTTP service behind the global TLS endpoint. Do not expose its
loopback listener directly. Configure the service with
`deploy/operations-entrypoint.env.example`.

Run one service instance in each discovery region. The TLS endpoint can select
any instance. An instance returns `503` when its local committed state is stale.

### Current integration boundary

The fail-closed discovery service and canonical browser contract are present,
and the repository now provides an atomic `OperationsAssignmentPublisher`
handoff. The handoff can publish or withdraw a decision made elsewhere; it
cannot prove a quorum commit and is not an election implementation. The durable
controller and node agent that call it do not exist in this repository, so
nothing currently writes a live assignment to
`/run/needletail/operations-collector.json`.

The current mesh service also does not yet assemble the embedded `contributor`,
committed `collector`, and complete `topology_links` fields required by
`schema: "needletail.operations-snapshot.v1"`. Until those producers are
integrated, the discovery endpoint correctly returns `503` and the UI marks the
missing state unavailable. Do not substitute DNS affinity, local scoring, or a
second lease authority to make it redirect.

The smallest external integration must pass these gates:

1. A maintained consensus implementation commits the term, fencing generation,
   leader, endpoints, voter proof, commit time, and bounded lease.
2. Its node agent projects that record into
   `OperationsCollectorAssignment` and calls `publish_committed` only after the
   commit is durable. Quorum loss calls `withdraw`; recovery advances the fence.
3. The elected collector rejects ingest and aggregate writes carrying any
   other generation, de-duplicates `(node id, boot id, sequence)`, and emits the
   canonical snapshot rather than a service-local compatibility shape.
4. Integration tests partition the old leader, remove one voter, replay an old
   generation, retry duplicate sequences, and prove there is never an overlap
   in accepted generations. Contract tests deserialize the resulting
   `/api/mesh` response with the strict dashboard model.

Snapshot assembly belongs in the owning mesh service repository; adding it to
Needletail would cross the product boundary documented in `AGENTS.md`.

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
