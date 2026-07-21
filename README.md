<p align="center">
  <img src="image.png" alt="Needletail logo" width="260">
</p>

# Needletail

Needletail delivers live video and professional audio from contribution source to viewer.
It accepts RIST, SRT, and RTMP input and publishes one canonical media stream.
An adaptive dual-parent relay fabric carries that stream to regional playback edges.
The Needletail Player and Operations view give viewers and operators direct control of the live experience.

Needletail brings together:

- broadcast-ready contribution input;
- source-side recovery and packaging;
- adaptive RaptorQ delivery across independent relay parents;
- regional caching for low-latency playback;
- CMAF-compatible fragmented MP4 for browser video; and
- live visibility for ingest, delivery, recovery, and latency.

## Contribution

Needletail works with compatible encoders and production tools.

| Protocol | Contribution role |
| --- | --- |
| RIST | Reliable Internet Stream Transport for resilient MPEG-TS contribution. |
| SRT | Secure Reliable Transport for resilient MPEG-TS contribution. |
| RTMP | Real-Time Messaging Protocol input for FLV publishers. |

RIST and SRT deliver MPEG-TS to `av-contrib`.
RTMP delivers encoded access units through its FLV input.
`av-contrib` recovers each contribution and creates the declared media format.

For supported H.264 and AAC input, `av-contrib` creates Common Media Application Format (CMAF) fragmented MP4 parts.
Needletail uses `fMP4` as the short name for fragmented MP4.
The contributor publishes each part as a bounded, immutable media object.
Each object carries stream identity, timing, dependencies, and integrity data.

## Live Delivery

Needletail uses RaptorQ forward error correction for live media recovery.
The primary parent carries source symbols.
The independent secondary parent carries compatible repair symbols and supports warm route changes.

The recovery policy responds to media importance, packet loss, jitter, queue pressure, and the delivery deadline.
Reliable object fetch supplies bounded backfill for media that needs another delivery path.
Expired work leaves the queue so newer media keeps its delivery budget.

Each relay and playback edge receives a primary parent and an independent warm secondary parent.
Route selection uses round-trip time, jitter, loss, queue state, deadline behavior, and failure-domain diversity.
Generation fencing keeps route changes ordered.
Make-before-break changes the route before the current route closes.

The relay fabric carries canonical media objects between the source, relay parents, and playback edges.
This arrangement performs protocol recovery and media packaging once near the source.
It preserves the encoded media while delivery adapts to current network conditions.

```text
RIST, SRT, or RTMP source
  -> contribution recovery and media packaging
  -> immutable media objects
  -> adaptive RaptorQ dual-parent delivery
  -> regional playback cache
  -> LL-HLS player or interactive media lane
```

## Playback

The playback edge serves Low-Latency HTTP Live Streaming (LL-HLS) at:

```text
/live/<stream-id>/stream.m3u8
```

Viewers open `/<stream-id>` in the Needletail Player.
The standard video path uses CMAF-compatible fMP4 parts.
The player selects native HLS when the browser supports it.
Other browsers use the bundled HLS.js implementation.

The player provides:

- a Native and HLS.js player choice;
- a live-delay target from 100 ms to 5 seconds;
- current delay with a one-second rolling average;
- playback, buffer, and live-edge progress on the timeline; and
- seeking within the retained live window.

## Operations

Needletail Operations shows the complete route from contribution to playback.
It presents active protocols, stream continuity, relay parents, RaptorQ recovery, cache health, latency, and actionable alerts.

The view combines contributor and playback-edge snapshots into one live service picture.
Operators can move from the overview to streams, routes, nodes, performance, and activity.

![Needletail operations overview](docs/release/screenshots/operations.png)

## Measured Results

Measurements completed on 20 July 2026 establish the current short-window delivery baselines.

| Delivery area | Current result | Measurement |
| --- | --- | --- |
| Wide-area LL-HLS | 2.390-2.452 ms additional p50 latency over raw UDP | 5 ms parts from London to New York, Tokyo, and Sydney. The metric measures publication-to-client availability. |
| Eight-track Opus | 128 real-time track tails per vCPU | 32 customers with eight tracks each on a two-vCPU edge. All 2,048,000 track units were exact. Availability p99 was 12.627-12.734 ms. |
| 4K LL-HLS video | 350 concurrent viewer tails at 3.626-3.659 Gbit/s | Two repeated 60-second runs on one `n2-standard-2` edge. fMP4 part p99 was 198.95-199.05 ms. |

These values provide an engineering baseline for the measured stream and edge configurations.
Production sizing uses longer endurance profiles and the target deployment topology.

The [current performance record](docs/performance/current-state-and-gaps.md) contains the latest capacity boundaries.
The [real-world evidence index](docs/real-world-tests/README.md) contains dated methods and source records.

## Service Composition

Needletail composes the services that move media from source to viewer.

| Service or crate | Product role |
| --- | --- |
| `av-contrib` | Accept contribution protocols, recover input, package media, and publish canonical objects. |
| `media-object` | Define object identity, integrity, dependencies, deadlines, and timestamps. |
| `raptor-fec` | Select RaptorQ geometry, repair policy, and deadline-aware scheduling. |
| `relay-session` | Manage authenticated carriers, subscriptions, symbols, and object fetch. |
| `av-mesh` | Relay media, maintain regional caches, serve LL-HLS, and publish telemetry. |
| `playlists` | Maintain bounded media and manifest caches with generation-safe writes. |
| `mission-control` | Present ingest, topology, recovery, latency, health, and alerts. |
| `player` | Play live streams and expose delay, buffer, live-edge, and player controls. |

## Local Development

Needletail uses published registry crates from crates.io.
The repository contains the orchestration tools, Operations assets, Player assets, and release checks.

Build and check the repository with:

```sh
make fmt
make check
make test
make player-check
make mission-control-check
```

Build the browser assets with:

```sh
make player-build
make mission-control-build
```

Cargo manifests use published registry crates from crates.io.

## Documentation

- [Relay fabric](docs/relay-fabric.md)
- [Contributor origin boundary](docs/contributor-origin-boundary.md)
- [Audio delivery lanes](docs/audio-delivery-lanes.md)
- [Operations telemetry transport](docs/operations-telemetry-transport.md)
- [Current performance record](docs/performance/current-state-and-gaps.md)
- [Real-world evidence](docs/real-world-tests/README.md)

## License

Needletail is available under the [MIT License](LICENSE).
