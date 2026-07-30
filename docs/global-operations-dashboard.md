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
canonical snapshot endpoint
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

Each mesh node exposes one bounded local snapshot at `/api/mesh/local`. Only the
quorum-elected collector polls those local documents and the contributor status
endpoint. Followers do not poll the fleet; they copy the elected leader's
canonical `/api/mesh` document. This leaves one fleet poller, one canonical
snapshot, and no static telemetry-peer graph.

The collector validates the configured node identity against every response,
deduplicates objects and events by stable keys, caps retained arrays, and marks
missing or old sources stale. It assembles
`needletail.operations-snapshot.v1` with the contributor, all configured map
nodes, explicit topology links, and the current term, generation, quorum, and
lease proof.

Raw telemetry is not replicated between collectors and is not written per
sample to a database. A new collector rebuilds the fleet view from the current
local snapshots. If later history is required, the active collector may write
one aggregated minute batch guarded by the fencing generation; storage must
reject a stale generation.

## Split-brain fences

A node may call itself the global collector only while all of these are true:

1. its term and generation are committed by quorum;
2. its quorum lease is still valid outside a conservative clock/error margin;
3. the published browser API carries its current generation;
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

### Qualification implementation

The GCP/Azure composition uses an mTLS etcd quorum and
`needletail-controller-agent` on every mesh node. Three configured voter nodes
may campaign. Every node observes the same election record, validates it through
`OperationsAssignmentPublisher`, and atomically projects the committed
assignment and controller state under `/run/needletail`.

`needletail-operations-collector` runs on every mesh node. The elected instance
polls the fleet once; followers copy its canonical document. `av-mesh` exposes
the raw local document only at `/api/mesh/local`, serves the validated canonical
file at `/api/mesh`, and returns `503` when the proof, freshness, or effective
lease is unsafe. Its well-known route returns `307` only to the endpoint carried
by that validated snapshot.

The current qualification entry point is
`https://needletail-london-20260727.bitneedle.com/.well-known/needletail-operations`.
The same hostname serves the follower-safe global UI at `/mesh` on port 443.
Candidate regional masters use the allowlisted ports committed in
`deploy/multicloud-qualification/node-runtime.json`.

The qualification voter placement is two GCP nodes and one Azure node. It
tolerates one voter loss, including the Azure voter, but deliberately loses
Operations control-plane availability on a complete GCP outage. Media
forwarding remains independent. Production needs a third-provider witness or a
different explicitly accepted provider-outage policy.

This is the Operations election and composition authority, not the complete
production desired-state controller described in `deploy/README.md`. Durable
service reconciliation, audit storage, and conditional aggregate persistence
remain separate work.

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
