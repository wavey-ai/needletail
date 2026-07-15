# Needletail relay fabric

Needletail compiles a secure forwarding graph for every stream and destination
cohort. The graph combines a scalable dual-parent DAG with a tightly connected
backbone overlay for the lowest-latency delivery class.

## Graphs and sessions

The controller manages two related structures:

1. The session overlay describes authenticated carrier connectivity between
   trusted relays. Three to five backbone relays may keep a complete or
   near-complete session overlay.
2. The per-stream forwarding graph selects one primary and up to one secondary
   upstream for each relay or playback edge. Levels strictly increase from
   origin to backbone, regional relay, and playback edge.

This separation keeps failover candidates warm while every media object follows
an explicit acyclic route.

## Forwarding invariants

Each desired-state generation satisfies these controller gates:

- exactly one origin for a stream graph at level 0;
- one primary upstream for every downstream node;
- up to one secondary upstream;
- parents at an earlier level than their children;
- provider, region, ASN, and physical-zone diversity between dual parents;
- an origin child limit, initially four;
- a configured downstream child limit for every relay;
- at most `2 × (node count - 1)` upstream relationships;
- explicit stream subscriptions before object forwarding;
- idempotent generation and subscription application;
- make-before-break parent changes with generation fencing.

The executable policy lives in `src/relay_topology.rs`. Desired state failing an
invariant stays outside the agent reconciliation path.

## Parent roles

The primary parent carries live source symbols. The secondary parent holds the
subscription and object state warm and may provide:

- RaptorQ repair symbols selected by current deadline risk;
- reliable fetch of an immutable missing object;
- immediate primary takeover;
- duplicated initialization, configuration, discontinuity, and keyframe
  objects;
- repair traffic routed across an independent provider and ASN.

Healthy delta objects use the primary path. Protection grows with object
importance, observed loss, jitter, queue depth, and time remaining to the media
deadline.

## Delivery classes

Needletail compiles routes per stream, rendition, destination region, ASN, and
latency class. Many nearby viewers share a cohort route and playback edge.

| Class | Forwarding shape | Playback lane | Initial hard limits |
| --- | --- | --- | --- |
| Interactive | direct or one-backbone-hop dual-parent fast path | interactive edge protocol | 1 inter-region relay, 1.15× path stretch, relay processing p95 ≤1 ms, media queue p95 ≤5 ms |
| Premium live | dual-parent regional DAG | object delivery or tightly tuned LL-HLS | 2 inter-region relays, 1.25× stretch, processing p95 ≤1 ms, queue p95 ≤5 ms |
| Mass broadcast | bounded multi-level dual-parent DAG | LL-HLS/H3 | 2 inter-region relays, 1.50× stretch, processing p95 ≤2 ms, queue p95 ≤10 ms |
| Resilient contribution | best regional ingress plus independent upstream | SRT, RIST, or WHIP | 2 inter-region relays, 1.25× stretch, processing p95 ≤1 ms, queue p95 ≤5 ms |

Direct ingress-to-edge delivery remains a candidate for interactive cohorts.
The controller chooses it whenever measured performance wins and keeps the best
independent relay path warm.

## Route compilation

Agents continuously report path observations with synchronized monotonic and
wall-clock context:

- smoothed RTT and p50/p95/p99 RTT;
- jitter and loss;
- congestion window, pacing rate, and bytes in flight;
- relay processing and media-queue p50/p95/p99;
- object deadline misses and expired-object drops;
- repair demand and successful recovery by parent;
- provider, ASN, region, and physical failure domain.

The controller first filters candidates through the class limits, then scores
the remaining candidates by end-to-end deadline performance, selected-path
RTT, jitter, and loss in that order. The initial interactive qualification
filter admits jitter p95 up to 5 ms, path loss up to 5%, and deadline misses up
to 1,000 ppm; the impairment and soak results tune those operating limits.
Route changes are issued as a new desired-state generation. The new primary
reaches warm state before the previous primary drains.

Path stretch is calculated as:

```text
selected path RTT / fastest measured direct RTT
```

Placement uses measured network behavior as the authority. Geography supplies
candidate discovery and failure-domain context.

## RaptorQ media plane

RaptorQ is the primary live-media recovery system. The hot path is:

```text
canonical media object
→ RaptorQ source and repair symbols
→ deadline and path scheduler
→ RelayTransport datagram carrier
→ dual-parent forwarding DAG
```

The scheduler owns adaptive repair amount, source-first ordering, keyframe and
audio priority, path selection, expiry, and the choice between additional FEC
and reliable fetch. Symbols from an independent secondary path can complete the
same coding object. Obsolete symbols leave the queue before newer decodable
groups.

Carrier comparisons use identical loss, RTT, jitter, bandwidth, queue, and
congestion scenarios. Deadline-hit rate and p99 media latency select the winning
policy.

## Core crate boundaries

- `media-object` owns canonical immutable identity, the bounded v1 envelope,
  payload integrity, dependencies, deadlines, and source-known timestamps.
- `raptor-fec` owns RaptorQ geometry, adaptive repair, source-first scheduling,
  deadline outcomes, and the FEC-versus-fetch recovery decision.
- `relay-session` owns authenticated carrier sessions, subscriptions, symbol
  forwarding, reliable object fetch, queue admission, and expiry.
- `playlists` owns bounded chunk/manifest caching and immutable slot-write
  semantics used by playback edges.
- Needletail owns desired topology, route compilation, deployment generations,
  product observability, and qualification gates.

## RelayTransport and RelaySession

`RelayTransport` is the carrier abstraction. It provides datagram send/receive,
path MTU, pacing and congestion feedback, peer identity, and bounded session
lifecycle. Initial backends are:

- long-lived QUIC Datagram sessions for public relay links, providing mTLS
  identity, encryption, pacing, congestion control, and path management;
- private UDP for controlled networks and benchmarks, paired with managed
  network identity and WireGuard where encryption crosses hosts.

`RelaySession` sits above either carrier and provides:

- bounded, versioned framing;
- subscribe, renew, unsubscribe, and generation fencing;
- source-symbol and repair-symbol delivery;
- reliable immutable-object fetch by canonical media-object key and hash;
- receiver deadline feedback and repair requests;
- priority for init/config/discontinuity/keyframe objects;
- bounded queues with immediate expiry of obsolete media;
- per-session and per-stream admission limits.

Live source and repair symbols use the datagram interface. Reliable QUIC streams
carry subscription control, initialization/configuration objects, catalogs, and
late backfill. Either authorized parent may contribute compatible RaptorQ
symbols for the same canonical media object.

## Scaling and latency gates

A 100-node qualification topology must demonstrate:

- no more than 198 upstream relationships for one-origin dual-parent routing;
- origin fanout within the configured two-to-four backbone-relay limit;
- origin egress independent of playback-edge count;
- zero repeated-object forwarding across levels;
- subscription-scoped delivery to interested cohorts;
- primary loss recovered through the warm secondary within the object deadline;
- reliable fetch completing late joins and missing-object recovery;
- interactive routes within 1.15× of the fastest measured direct path;
- relay processing below 1 ms p95 for interactive and premium lanes;
- media queues below 5 ms p95 for interactive and premium lanes;
- expired frames and repair symbols removed before newer groups;
- p50/p95/p99 capture-to-player latency reported by delivery class and route;
- bounded memory, queues, sessions, subscriptions, frames, and FEC work;
- authenticated and authorized peer, subscription, fetch, write, and control
  operations.

The lab records direct-path and compiled-route baselines across at least five
regions, multiple providers, and independent ASNs. Production qualification
adds a seven-day observation window with congestion, loss, failover, late join,
route churn, and origin-loss scenarios.

## Migration sequence

1. Introduce the canonical media-object envelope and preserve source order,
   dependencies, deadlines, and timestamps end to end.
2. Introduce `RelayTransport`, preserve the private UDP carrier, and establish
   authenticated paced QUIC Datagram sessions for public relay links.
3. Publish controller-generated topology and subscription state in shadow mode
   and compare predicted routes with measured paths.
4. Carry selected canary streams through primary source-symbol sessions,
   secondary repair/fetch sessions, and bounded deadline queues on both
   carriers.
5. Compare carriers under identical impairment and prove origin fanout,
   relationship count, acyclicity, exact-byte delivery, deadline behavior, and
   failover gates.
6. Graduate each delivery class to the compiled relay fabric and retain direct
   routes selected by latency policy.
