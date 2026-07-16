<p align="center">
  <img src="image.png" alt="Needletail logo" width="260">
</p>

<p align="center">
  The Wavey Goose is our mascot, but the Needletail is the fastest bird in level flight...
</p>

# Needletail

Needletail is the product-level repo for Wavey realtime media delivery.
It composes, runs, observes, and tests the service constellation. Core services
and reusable crates stay in their own repos.

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
Contributor products integrate through the generic ingest, capability, and
session APIs. They stay outside this repository.

The Rust supervisor runs the local development constellation. Needletail's
production lifecycle target consists of a desired-state controller, a small
agent on each host, and `systemd` supervision of native binaries.

## Documentation

- [Latency performance charts](docs/performance/latency-performance.md)
- [Real-world load dashboard screenshots](docs/performance/real-world-load-screenshots.md)
- [Relay fabric](docs/relay-fabric.md)
- [Deployment control plane](deploy/README.md)
- [Mission Control](mission-control/README.md)
- [Real-world test evidence](docs/real-world-tests/README.md)

## License

Needletail is licensed under the [MIT License](LICENSE).

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

The local constellation wires controlled RelaySession test lanes by default:
contributor source traffic uses `22301 → 22001`, warm-secondary repair uses
`22302 → 22201`, and both lanes share desired-state generation/subscription `1`
with canonical media-object deadlines.

Use `make local-fast` after the component release binaries and operations dashboard assets have
already been built. Component roots can be overridden with `AV_CONTRIB_ROOT`
and `AV_MESH_ROOT`.

The operations dashboard builds as part of `make local`. Direct UI workflows are:

```sh
make mission-control-check
make mission-control-test
make mission-control-build
```

## Latency performance

Latest charts:

- [Relay latency](docs/performance/charts/relay-latency.svg)
- [Failover latency](docs/performance/charts/failover-latency.svg)
- [GCP route RTT](docs/performance/charts/route-rtt.svg)
- [RaptorQ recovery](docs/performance/charts/raptorq-recovery.svg)
- [Numbers and raw evidence links](docs/performance/latency-performance.md)

Latest results:

- GCP intercontinental: relay processing p95 `1.0 ms`; publication-to-cache p99
  `150 ms`; failover media gap `120.034 ms`; RaptorQ recovered `381` media
  objects and `1,195` source symbols under controlled loss.
- Local controlled-loss: relay processing p95 `1.0 ms`; publication-to-cache p99
  `150 ms`; RaptorQ recovered `1,120` media objects.

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
graduates the included test thresholds into production SLOs.

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
