# Local realtime qualification — 2026-07-15 UTC

## Purpose

This run exercised the same compiled contributor → primary/warm-secondary
relay → playback-edge path on one arm64 macOS host. Four `udp-netem` links
provided independent delay, jitter, and loss. The gate combined HTTP/2 load,
automatic failover, exact RaptorQ reconstruction, and error-counter assertions.

- Run ID: `20260715T235439Z`
- Raw artifacts: `target/realtime-qualification/20260715T235439Z`
- Versioned summary: `evidence/local-20260715T235439Z.json`

The load client used eight HTTP/2 connections with four streams per
connection, a 4,096-byte ingest payload, and 15 seconds per endpoint in each
profile.

## Network profiles

| Link | Baseline | Impaired |
| --- | --- | --- |
| Contributor → primary | no injected impairment | 10 ms delay, 2 ms jitter, 1% loss |
| Contributor → secondary | no injected impairment | 10 ms delay, 2 ms jitter, 0% loss |
| Primary → edge | no injected impairment | 35 ms delay, 5 ms jitter, 1% loss |
| Secondary → edge | no injected impairment | 35 ms delay, 5 ms jitter, 0% loss |

The impaired profile dropped 60 contributor-to-primary datagrams, 62
primary-to-edge datagrams during load, and three primary-to-edge datagrams
during recovery. The secondary path recorded zero loss drops. All links
recorded zero overflow drops and send errors.

## Load measurements

| Endpoint | Profile | Successful requests | Failed | p50 | p95 | p99 | Effective rate |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Contributor ingest | Baseline | 88,390 | 0 | 4.728 ms | 10.815 ms | 19.750 ms | 5,919.6 req/s |
| Contributor ingest | Impaired | 105,005 | 0 | 3.973 ms | 8.939 ms | 16.823 ms | 7,024.5 req/s |
| Edge playlist | Baseline | 734,730 | 0 | 0.526 ms | 1.108 ms | 1.827 ms | 51,677.8 req/s |
| Edge playlist | Impaired | 806,109 | 0 | 0.479 ms | 1.015 ms | 1.669 ms | 56,732.3 req/s |

Impaired/baseline p95 ratios were `0.827×` for contributor ingest and `0.916×`
for playlist delivery, below the `3×` gate. Impaired service histograms placed
contributor forwarding p95 at or below 2.5 ms and edge handler p95 at or below
0.1 ms. The client gates were 15 ms for ingest and 5 ms for playlist delivery.

## Failover and RaptorQ

Stopping the primary produced the state sequence healthy → promoted → healthy:

| Measurement | Result | Gate |
| --- | ---: | ---: |
| Primary-loss detection | 111.073 ms | ≤ 250 ms |
| Promotion to source | 32.327 ms | ≤ 250 ms |
| Decoded-media gap | 150.068 ms | ≤ 250 ms |
| Warm source datagrams replayed | 63 | positive |
| Decoded objects during outage | 6 | positive |
| Expired objects | 0 | 0 |
| Rejected datagrams | 0 | 0 |
| Deadline drops | 0 | 0 |

The impaired interval decoded 536 objects with absent source data and RaptorQ
reconstructed 915 source symbols. The edge received 7,118 source datagrams and
1,206 repair datagrams; the warm secondary forwarded all 1,206 repair
datagrams. Repair-assisted decodes also totalled 536. Forwarding errors,
receiver rejections, and deadline drops were zero.

## Revisions and result

The tested binary SHA-256 values are preserved in the versioned JSON summary.
The source revisions were Needletail `5b8a957`, av-contrib `9aa15ea`, and av-mesh
`730cc1e` plus the exact-recovery/warm-replay working-tree patch.

Every load, latency, exact-recovery, failover, and integrity gate passed. The
qualification trap stopped the local services, media source, and all four
network-emulation processes at exit.
