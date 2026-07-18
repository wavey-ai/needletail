<p align="center">
  <img src="image.png" alt="Needletail logo" width="260">
</p>

<p align="center">
  The Wavey Goose is our mascot, but the Needletail is the fastest bird in level flight...
</p>

# Needletail

Needletail is the product-level repo for Wavey realtime media delivery. It
composes, runs, observes, and tests the service constellation. Core services
and reusable crates stay in their own repos.

**Measured 48 kHz lossless latency:** persistent TLS 1.3/H3 LL-HLS with 5 ms
parts added **2.390–2.452 ms at p50** over raw UDP in the London-origin,
dual-parent GCP run. LL-HLS reached New York, Tokyo, and Sydney in 55.728,
127.506, and 148.549 ms at p50; regional cache-to-client delivery stayed below
1.51 ms at p99. These are publication-to-client availability results, not
browser-to-speaker latency.

**Measured eight-track Opus capacity:** the underlying cache reaches millions
of reads/s and the optimized router exceeds one million cached-part responses/s;
they are not the current limit. One real customer instead creates 1,600 live H3
requests/s by tailing eight 5 ms tracks. On a two-vCPU GCP edge, four customers
met the strict 2 ms cache-to-client p99 target, nine still received every part,
and delivery became incomplete at ten. The current boundary is the combined
live H3/QUIC path, including future-part waiter/wakeup, not playlist lookup or
a low connection limit. See the canonical
[current performance state and gaps](docs/performance/current-state-and-gaps.md).

Needletail owns:

- multi-service topology and desired-state generation;
- local and deployed orchestration;
- the operations dashboard;
- product observability;
- real-world impairment, failover, latency, and RaptorQ recovery tests;
- deployment composition around native binaries and `systemd`.

Contributor-product integrations live in their owning app repos and integrate
through Needletail's generic ingest, capability, and session APIs.

## Service constellation

Current components:

| Component | Owner responsibility |
| --- | --- |
| `av-contrib` | Per-stream origin ingest, FEC recovery, route-selected opaque publication or media boxing, and bounded publication to a dedicated mesh ingress. |
| `av-mesh` | Playback edge, LL-HLS cache adapter, relay-node behavior, telemetry, and product-asset hosting. |
| `media-object` | Canonical immutable media-object identity, bounded v1 envelope, payload integrity, dependencies, deadlines, and source-known timestamps. |
| `raptor-fec` | Adaptive RaptorQ geometry, source-first scheduling, repair policy, deadline outcomes, and FEC-versus-fetch decisions. |
| `relay-session` | Authenticated carrier sessions, subscriptions, symbol forwarding, reliable object fetch, queue admission, and expiry. |
| `playlists` | Bounded chunk/manifest caches and immutable slot-write semantics used by playback edges. |
| `av-service` | Shared HTTP, HLS, and upload-response services. |

The hot path is:

```text
Contributor ingest
  -> canonical media object
  -> RaptorQ source and repair symbols
  -> deadline and path scheduler
  -> RelayTransport datagram carrier
  -> dual-parent relay DAG or direct fast path
  -> playback edge cache
  -> LL-HLS or interactive delivery
```

RaptorQ is the live-media recovery system. QUIC Datagram is an optional carrier
for authenticated, encrypted, paced datagrams. Reliable streams are for control,
initialization, and backfill.

48 kHz Audio Epoch publications have three simultaneous delivery lanes:
mandatory format-preserving LL-HLS, optional browser WebTransport datagrams,
and optional native UDP+FEC subscriptions at a relay or playback edge. An
ingress route can publish producer-framed bytes unchanged or explicitly ask
`av-contrib` to box supported elementary media. The mesh caches and replicates
either result as immutable bytes without interpreting the payload. See
[Audio delivery lanes](docs/audio-delivery-lanes.md) for the wire contracts,
format behavior, and local/GCP qualification commands. The contributor performs
stream-dependent work once and never doubles as a relay; see the
[Contributor origin boundary](docs/contributor-origin-boundary.md).

## Operations dashboard

Mission Control is the Needletail-owned operations UI. It renders contributor
ingest, compiled delivery routes, RelaySession lanes, RaptorQ recovery,
publication continuity, latency, and alerts from bounded service snapshots.

It reads:

- `av-contrib` `GET /api/status`;
- `av-mesh` `GET /api/mesh`.

Default same-origin edge feed: `/api/mesh`.
Default contributor feed: `https://local.bitneedle.com:19443/api/status`.

Override feeds with:

```text
/mesh?edge=https://edge.example/api/mesh&contrib=https://ingress.example/api/status
```

Build and check the dashboard:

```sh
make mission-control-check
make mission-control-test
make mission-control-build
```

### Dashboard screenshots from the latest GCP run

Run: `20260716T023139Z`.
Topology: London contributor, Amsterdam primary relay, Osaka secondary relay,
Tokyo playback edge. Four `e2-standard-2` GCP instances.

#### Overview

![Needletail overview dashboard](docs/performance/screenshots/20260716T023139Z-overview.png)

#### Routes

![Needletail routes dashboard](docs/performance/screenshots/20260716T023139Z-routes.png)

#### Performance

![Needletail performance dashboard](docs/performance/screenshots/20260716T023139Z-performance.png)

## Current performance

The canonical [current performance state and gaps](docs/performance/current-state-and-gaps.md)
keeps the different capacity boundaries separate and names the next work. In
brief:

| Question | Current answer |
| --- | --- |
| How close is 5 ms LL-HLS to UDP? | 2.390–2.452 ms p50 premium in the measured multi-region GCP run. |
| Is playlist or part-cache lookup the limit? | No. Cache reads reach millions/s and the optimized router exceeds one million cached part responses/s without H3. |
| What limits the current edge? | The combined live H3/QUIC path, including future-part waiter/wakeup, at high 5 ms per-track request rates. |
| What is the strict Opus tier? | Four eight-track customers on two vCPUs for the measured ten-second window; endurance is pending. |
| What remains complete beyond the strict tier? | Nine customers delivered every part; ten became incomplete. |

Dated narratives and sanitized JSON live in the
[real-world test index](docs/real-world-tests/README.md). Detailed geographic
latency tables and charts remain in
[latency performance](docs/performance/latency-performance.md).

## Relay fabric

Needletail compiles a forwarding graph for every stream and destination cohort.
It selects one primary and up to one independent warm secondary parent for
each relay or playback edge. Levels increase from the contributor origin to
backbone relays and regional edges, keeping every forwarding route acyclic and
origin egress independent of viewer count. Route choice uses measured RTT,
jitter, loss, queueing, deadline behavior, and failure-domain diversity.

See [Needletail relay fabric](docs/relay-fabric.md) for forwarding invariants,
delivery classes, route compilation, session behavior, and scale gates.

## RaptorQ media plane

RaptorQ is the live recovery mechanism. The primary path sends source symbols;
an independent secondary can provide compatible repair symbols, reliable
missing-object fetches, or immediate takeover. Deadline-aware scheduling drops
obsolete work before it delays newer media. `RelaySession` adds authenticated
subscriptions, generation fencing, bounded queues, and reliable control and
backfill around either QUIC Datagram or managed private-UDP carriers. The full
contract and ownership boundaries are documented in
[Needletail relay fabric](docs/relay-fabric.md#raptorq-media-plane).

## Native control plane

Component repositories produce native service binaries. Needletail deploys and
supervises them on explicitly provisioned hosts.

The target production control path:

1. Provider adapters create hosts, private networking, DNS, and storage.
2. Cloud-init installs a short-lived bootstrap identity and the Needletail node
   agent.
3. The agent establishes mTLS to the Needletail controller and exchanges a
   certificate-bound node identity for short-lived workload credentials.
4. The controller publishes a versioned desired-state generation: approved
   artifact hashes, service roles, relay parents, stream placement, limits,
   drain state, and rollout policy.
5. The agent reconciles native binaries and `systemd` units idempotently, then
   reports observed state, command IDs, health, and failure reasons.
6. Durable leases and fencing prevent a replaced or partitioned node from
   continuing to publish or control traffic.

The first controller store may be PostgreSQL behind a storage trait. It holds
desired state, observed generations, leases, idempotency keys, and an
append-only audit log. Realtime media flows directly between ingress, relays,
and edges.

## Local development

The default checkout layout places Needletail beside its component repos:

```text
wavey.ai/
  needletail/
    mission-control/
  av-contrib/
  av-mesh/
  av-service/
  media-object/
  relay-session/
  playlists/
  raptor-fec/
  tls/
```

Run two local playback edges plus one contributor ingress:

```sh
make local
```

Use the fast path after component release binaries and dashboard assets have
already been built:

```sh
make local-fast
```

Component roots can be overridden:

```sh
AV_CONTRIB_ROOT=/path/to/av-contrib AV_MESH_ROOT=/path/to/av-mesh make local
```

The local constellation wires controlled RelaySession lanes by default:

- contributor source traffic: `22301 -> 22001`;
- warm-secondary repair traffic: `22302 -> 22201`;
- desired-state generation/subscription: `1`;
- canonical media-object deadlines enabled.

Observability commands:

```sh
make observability-check
make observability-up
```

Validation commands:

```sh
make fmt
make check
make test
bash -n scripts/*.sh
./scripts/validate-real-world-evidence.sh
./scripts/validate-product-boundary.sh
```

## License

Needletail is licensed under the [MIT License](LICENSE).
