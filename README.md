# Needletail

Needletail is the product-level constellation of Wavey realtime media services.
This repository owns how the services are composed, observed, qualified, and
operated; the services and reusable transport crates remain independently
versioned components.

Current components include:

- `av-contrib`: contributor-facing RIST/SRT/RTMP/raw ingest and FEC forwarding;
- `av-mesh`: regional playback edge, LL-HLS cache adapter, telemetry, and product-asset hosting;
- `media-object`: canonical immutable object identity and bounded v1 envelope;
- `raptor-fec`: adaptive RaptorQ coding, scheduling, and recovery policy;
- `relay-session`: authenticated subscriptions, carrier sessions, and dual-parent forwarding;
- `playlists`: reusable bounded chunk and manifest caches;
- `av-service`: shared HTTP, HLS, and upload-response services.

## Repository boundary

Needletail owns multi-service topology, the local supervisor, deployed canary
and impairment gates, product observability, and deployment composition.
Component repos own service code, protocol behavior, service tests, and
service-specific container images. `mission-control/` is the Needletail-owned
Leptos/WASM product UI for contributor ingest, compiled delivery routes,
RelaySession lanes, RaptorQ recovery, publication continuity, and realtime
latency.

The Rust supervisor runs the local development constellation. Needletail's
production lifecycle target consists of a desired-state controller, a small
agent on each host, and `systemd` supervision of native binaries.

## Local constellation

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

Run two local playback edges plus one contributor ingress with:

```sh
make local
```

The local constellation wires controlled RelaySession qualification by default:
contributor source traffic uses `22301 → 22001`, warm-secondary repair uses
`22302 → 22201`, and both lanes share desired-state generation/subscription `1`
with canonical media-object deadlines.

Use `make local-fast` after the component release binaries and Mission Control assets have
already been built. Component roots can be overridden with `AV_CONTRIB_ROOT`
and `AV_MESH_ROOT`.

Mission Control builds as part of `make local`. Direct UI workflows are:

```sh
make mission-control-check
make mission-control-test
make mission-control-build
```

## Realtime qualification

`make realtime-benchmark` measures an already-running contributor and one or
more mesh edges. `make realtime-qualification` owns the local controlled-loss
topology. Both preserve `wavey.realtime-*.v1` artifact schemas and `av_*` metric
names across the orchestration extraction.

The short-lived GCP lab has a deployed gate for the London contributor,
Amsterdam and Osaka parents, and Tokyo playback edge:

```sh
GOOGLE_APPLICATION_CREDENTIALS=/path/to/google-cloud-key.json \
  make gcp-intercontinental-qualification
```

It stops and restores the primary relay to prove stable lane-health reporting,
bounded warm-parent promotion, uninterrupted decoding, and make-before-break
recovery. A restarted relay must establish its live subscription at the first
canonical media object it observes, restore a gap-free contiguous LL-HLS
watermark, and reconverge within four canonical objects of the stream head.
The gate then injects controlled loss on the primary source path and
requires repair-assisted RaptorQ completion with no expiry, rejection, or
deadline-drop regression. Evidence is written below
`target/gcp-qualification/runs/`; cleanup restores the relay and packet filter
even when a gate fails. The deployed qualification plan seeds that same
controlled-loss profile into the adaptive RaptorQ policy; the gate rejects a
plan whose observed loss input does not match the injected condition.
Both relay routes are measured from the deployed hosts. Their RTT and jitter
feed the compiled parent observations, Mission Control, and the qualification
artifact; either route exceeding the default `1.15x` direct-path stretch gate
fails qualification.

For an authorized deployed canary only:

```sh
CONTRIB_URL=https://contrib-canary.example \
MESH_URLS=https://uk-canary.example,https://us-canary.example \
SOAK_SECONDS=3600 ROUND_SECONDS=60 \
  make realtime-soak
```

The soak performs load and observation against explicit targets, uses verified
TLS by default, applies simultaneous load, probes exact-byte
propagation, captures raw metrics/counter deltas, and writes `soak.json` under
`target/realtime-soak/`.

## Observability

The `observability/` bundle is product policy spanning `av-contrib` and
`av-mesh`. It provisions Prometheus rules, Alertmanager, and Grafana. Local
ports bind to loopback.

```sh
make observability-check
make observability-up
```

Production provides authenticated access, trusted TLS, pinned images, and real
notification receivers. A stated hardware/geography/bitrate/viewer-load soak
graduates the included qualification thresholds into production SLOs.

## Native control plane

The production control plane stores versioned desired state and audit history
durably, issues idempotent reconcile generations to mTLS-authenticated node
agents, and uses leases/fencing for membership and rollouts. Provider adapters
create hosts and networking; cloud-init installs identity and the agent;
`systemd` owns process supervision. See `deploy/README.md`.

The media data plane uses controller-compiled dual-parent forwarding DAGs plus
a direct/one-hop fast-path class for interactive cohorts. See
`docs/relay-fabric.md` for routing invariants, latency budgets, and release
gates.

## Development

```sh
make fmt
make check
make test
bash -n scripts/*.sh
```

Repository publication remains an explicit operator action.
