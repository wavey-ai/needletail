# Needletail operations dashboard

The Needletail operations dashboard is the product operations UI. The interface
preserves a fast realtime overview and provides dedicated, anchored surfaces
for:

- live streams and contiguous publication;
- contributor listeners, sessions, fMP4 output, codecs, and errors;
- nodes and playback-edge services;
- compiled dual-parent-DAG and low-latency route assignments;
- contributor and LL-HLS latency, RaptorQ recovery, deadlines, and clock
  confidence;
- alerts and recent activity from both services.

It reads bounded, low-cardinality snapshots from:

- `av-contrib` `GET /api/status` for contributor health, RelaySession carrier
  assignments, primary source traffic, warm-secondary repair traffic, RaptorQ
  emission, deadline headroom, publication heads, and forwarding latency;
- `av-mesh` `GET /api/mesh` for playback-edge health, RelaySession ingress,
  RaptorQ recovery, publication watermarks, and LL-HLS handler latency.

Every snapshot field uses a Serde default. A rolling component deployment can
therefore present a partial snapshot while the operations dashboard marks the
controller or telemetry fields that are still arriving. Stream, node, session,
edge, alert, and activity arrays are capped before rendering.

The UI consumes the current service shapes. The current backends do not yet
expose the following values, so their corresponding cells remain `pending`:
their corresponding cells can move from `pending` to measured:

- delivery class, fabric, desired-state generation, installed route state, and
  per-stream/cohort route inventory;
- primary and warm-secondary node identities, failure-domain independence,
  RTT, jitter, loss, deadline-miss rate, and path stretch;
- contributor deadline hit/miss and sender-expiry totals;
- contributor and edge contiguous publication watermarks and known-gap totals;
- detailed RIST/SRT session RTT, jitter, loss, reconnect, and end-reason
  telemetry.

Configured RelaySession carrier targets are displayed as configured carriers.
The dashboard displays the route-state value emitted by the controller.

Run locally:

```sh
make serve
```

`make build` uses Trunk when available and otherwise performs a deterministic
WASM release build with the pinned local `wasm-bindgen` CLI, then assembles the
same static-host asset contract in `dist/`.

The default feeds are same-origin `/api/mesh` and
`https://local.bitneedle.com:19443/api/status`. Override either endpoint with
the controls in the header or query parameters:

```text
/mesh?edge=https://edge.example/api/mesh&contrib=https://ingress.example/api/status
```

Build the assets served by an `av-mesh` playback edge:

```sh
make build
NEEDLETAIL_MISSION_CONTROL_DIST=/path/to/needletail/mission-control/dist \
  needletail --no-mission-control-build ...
```

`needletail` builds this directory and supplies the dist path to each supervised
edge automatically.

Validation:

```sh
cargo test --locked --all-targets
cargo check --locked --target wasm32-unknown-unknown
cargo clippy --locked --all-targets -- -D warnings
cargo clippy --locked --target wasm32-unknown-unknown -- -D warnings
./scripts/build.sh
```
