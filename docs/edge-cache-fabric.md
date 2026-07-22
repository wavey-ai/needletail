# Regional LL-HLS edge cache fabric

This document defines the Needletail design for regional LL-HLS edge caches.

## Goal

Each edge serves viewers from its local cache. The edge does not forward cache data to another region.

Each region has a small distributor tier. Distributors receive media from the relay DAG and feed local edges.

The distributor tier prevents a viewer edge from becoming a replication hub. It also keeps origin fanout bounded.

## Terms

An edge is a leaf service that serves LL-HLS requests to viewers.

A distributor is a regional cache service that feeds edges and retains a warm copy.

A parent is a distributor that sends a cache object to a child.

A region is one provider location that has a private network.

The relay DAG is a directed acyclic graph with bounded fanout and dual parents.

## Topology

Each active edge uses two parents in its region:

```text
origin → relay DAG → regional distributor A ─┐
                         regional distributor B ─┼→ edge cache
                                               └→ edge cache
```

The first parent sends normal source objects. The second parent stays warm for repair and failover.

The controller selects parents with different provider, zone, and failure-domain attributes.

The controller limits distributor fanout. It adds another distributor before a distributor reaches its limit.

The mesh uses at most two parents and eight children per distributor by default.
Operators can change these limits with `--cache-mesh-max-parents` and
`--cache-mesh-max-children`.

Edges do not advertise themselves as distributors. Edges do not gossip peers across regions.

Start a distributor with `--cache-mesh-role distributor`. Start a playback leaf
with `--cache-mesh-role edge`. The production command enables same-region peer
validation for both roles. Set `--cache-mesh-region` to the provider region for
the cache plane when telemetry uses another region name.

The cache protocol accepts objects only from a configured regional parent. It rejects a cross-region peer.

Private IPv4 or private VLAN links carry distributor-to-edge replication when the provider supports them.

The control plane may use public addresses for bootstrap. Media replication uses the private address after enrollment.

## Replication policy

The controller places one warm distributor copy in each active region.

The controller creates a local edge copy when demand crosses the configured threshold.

The controller sends each object once per parent and once per distributor child.

An edge miss requests the retained window from a regional distributor. An edge does not request another edge.

The protocol coalesces requests for the same stream and range. A request has a minimum interval of one second.

The cache keeps the latest window. It drops expired objects before it accepts newer objects.

The controller keeps the two parent subscriptions active during a parent change.

The controller removes the old subscription after the new parent reports warm state.

## Egress admission

The edge measures response bytes in a bounded rolling window. The measure tracks throughput, not monthly transfer allowance.

The default capacity is 4 Gbit/s. This matches the smallest current Linode dedicated plan used by the deployment.

The edge admits new playback requests until the rolling throughput reaches 85 percent of capacity.

The edge requires three seconds of sustained load before it rejects a new connection.

The edge resumes admission below 75 percent of capacity. These two limits prevent rapid state changes.

An overloaded edge keeps each admitted CMCD session active.
It rejects a new or anonymous session with HTTP status `429`.
It also returns `Retry-After: 1`.

The response includes one `Link` header entry for each healthy same-region edge.

The response also includes `X-Needletail-Alternate-Edges` for clients that do not parse `Link`.

The alternate list uses the original path and query.
It excludes draining, stale, unhealthy, and overloaded edges.

The Multivariant Playlist contains equivalent variants for healthy same-region edges.
The edge omits itself from each playlist while its alarm is active.

The edge resumes new-session admission after measured egress falls below 75 percent.
The recovered edge then becomes eligible for future Multivariant Playlists.

The edge reports observed throughput, thresholds, and rejected requests in `/api/mesh` and `/metrics`.

## Lifecycle

The controller samples demand, egress, cache freshness, and parent health every five seconds.

The controller provisions an edge when regional demand exceeds the admission target for two samples.

The controller assigns a private address and a regional DNS name during provisioning.

The controller waits for health, private enrollment, and warm parent state before advertising the edge.

The controller marks an idle edge draining after fifteen minutes without active readers.

The controller removes DNS advertisement before it stops the edge.

The controller deletes the provider resource after the drain grace period.

The controller keeps one spare distributor in a region when a stream is live.

The edge lifecycle planner uses the lowest node identifier as the regional
worker. This rule prevents duplicate provider actions when telemetry is shared.

Provisioning is idempotent. A retry reuses a resource with the same generation and region labels.

## Provider adapters

The Linode adapter creates a dedicated instance, attaches the regional VLAN, and allocates a DNS record.

The GCP adapter creates a regional instance, attaches the regional VPC subnet, and allocates a DNS record.

Both adapters return the instance identifier, private address, public address, region, and DNS name.

The provider adapters support create and delete operations. The node bootstrap
performs health checks before the controller advertises a new edge.

Provider credentials remain in environment variables. They never enter telemetry, logs, or generated documentation.

## Failure handling

The controller does not depend on one global endpoint for playback.

Each region has independent distributors and independent control workers.

A new session can use an alternate URL when its request is rejected.
An admitted session continues on its current edge.

If both parents fail, the distributor requests the retained window from another regional distributor.

If a region loses all distributors, the controller starts a distributor before it advertises a new edge.

## Qualification

Qualification runs at least four small edges per region. Each edge uses two vCPUs or fewer.

Use the smallest supported Linode and GCP machine profiles for the first run.
Set `--linode-instance-type` and `--gcp-machine-type` in the qualification plan.

The run measures cache-to-client p50, p95, and p99 latency.

The run measures distributor fanout, duplicate object count, and private replication bytes.

The run holds origin fanout constant while it adds viewer edges.

The run drives each edge above its sustained egress limit and checks the `429` response headers.
The run also checks existing-session continuity and alternate-edge admission.

The run stops one parent and checks repair completion before the object deadline.

The run drains one edge and checks DNS removal, connection closure, and resource deletion.

The run repeats the tests on Linode and GCP in the same region.

## Rollout

First, deploy one distributor pair and two edges in one region.

Next, enable private parent links and regional peer validation.

Next, enable egress admission with a 15 percent reserve.

Next, enable demand provisioning and idle edge draining.

Finally, add regions and providers after the local failure and latency gates pass.
