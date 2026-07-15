# GCP intercontinental qualification — 2026-07-15

## Test bed

- Provider: Google Cloud, using the authorized trial credits.
- Hosts: four `e2-standard-2` VMs with a six-hour maximum run duration.
- Contributor: London (`europe-west2-b`).
- Primary relay: Amsterdam (`europe-west4-a`).
- Warm secondary relay: Osaka (`asia-northeast2-b`).
- Playback edge: Tokyo (`asia-northeast1-b`).
- Topology: source-seeded dual-parent acyclic relay path, with a primary source
  lane and independent warm-secondary RaptorQ repair lane.
- Carrier: controlled private UDP for the private-cloud qualification network.
- Media: synthetic 960×540 30 fps H.264 plus 48 kHz AAC, produced in real time
  by FFmpeg and packaged as 50 ms LL-HLS parts.
- Fault profile: 2% random loss on the primary relay ingress; the same 2% path
  observation feeds the adaptive RaptorQ policy.
- Route gate: both selected routes must remain at or below `1.15×` the measured
  direct contributor-to-edge RTT.
- Component savepoints at the start of testing: Needletail `f50202d`,
  av-contrib `a0b7853`, av-mesh `ebd5e6b`, media-object `93e82ed`, raptor-fec
  `2722922`, relay-session `41fbf36`, and playlists `028b83e`.

Provider credentials remained in the local credential file and were excluded
from source archives, result artifacts, logs, and commits.

## Runs and findings

### Initial canonical-publication deployment

The four services and Mission Control deployed successfully. Measured routes:

| Path | RTT | Stretch |
| --- | ---: | ---: |
| Direct London → Tokyo | 237.788 ms | `1.000×` |
| Amsterdam relay route | 244.237 ms | `1.027×` |
| Osaka relay route | 251.933 ms | `1.059×` |

All HTTP health endpoints returned success and both RelaySession lanes were
healthy. Live telemetry exposed a canonical-publication defect after the
contributor process restarted: every visible node retained head object `22477`,
the contiguous watermark was absent, and the retained window contained 24
known gaps. The source process had restarted its sequence at zero while relays
still held the older sequence domain.

Corrective implementation:

- contributor process incarnations now receive a Unix-microsecond source epoch;
- reconnects within one process share a monotonic per-stream object sequence;
- relay publication windows switch atomically on a newer epoch;
- delayed objects from an older epoch are rejected;
- Mission Control, the mesh API, Prometheus, and alerts expose epoch agreement,
  contiguous watermarks, and known gaps.

### Source-epoch deployment

The corrected contributor, relay, and Mission Control build deployed
successfully. Measured routes:

| Path | RTT | Stretch |
| --- | ---: | ---: |
| Direct London → Tokyo | 240.818 ms | `1.000×` |
| Amsterdam relay route | 244.197 ms | `1.014×` |
| Osaka relay route | 252.320 ms | `1.048×` |

Live post-deploy evidence:

- contributor and all three visible node views agreed on source epoch
  `1784153252993770`;
- edge, primary, and secondary known-gap counts were zero;
- contiguous watermarks equalled their local canonical heads;
- maximum observed canonical lag was four objects;
- the aggregate epoch-divergence metric was zero;
- the Tokyo LL-HLS manifest advertised 50 ms parts and a 150 ms part hold-back;
- contributor and both relay lanes reported healthy state.

### Contributor-restart qualification attempt

The gate restarted the contributor and synthetic source. The source epoch
advanced from `1784153252993770` to `1784153339330794`. All three nodes
converged on the new epoch with zero gaps:

| Node | Head | Contiguous | Lag |
| --- | ---: | ---: | ---: |
| Tokyo edge | 335 | 335 | 0 |
| Amsterdam primary | 331 | 331 | 4 |
| Osaka secondary | 334 | 334 | 1 |

The media-plane checks passed, but the qualification stopped before relay
failover because separate local Python processes exposed process-relative
monotonic clocks and produced a negative polling interval. The exit trap kept
the contributor and source services active; the primary relay and loss filter
had not yet been modified.

Corrective implementation:

- each relay now records source-epoch activation delay when it accepts the
  first canonical object of a new contributor incarnation;
- qualification gates the maximum relay-side activation delay rather than SSH
  polling latency;
- Mission Control shows activation delay per node and as a fleet maximum;
- Prometheus exposes per-stream and aggregate gauges, with a ten-second alert;
- the mesh API emits `canonical_epoch_activation_slow` for the same condition;
- local observation time uses a cross-process wall clock and remains diagnostic.

### Deployment-tooling interruption

A subsequent deploy stopped before host mutation when a newly added,
in-progress `capability-controller` workspace member referenced the sibling
`media-object` repository one directory too high. The dependency path was
corrected. The component release archive excludes this controller crate, and
the Needletail compiler again runs successfully.

The following deploy compiled the new relay successfully, then stopped before
host mutation when the contributor's newly added capability verifier used a
macOS-tolerated `Needletail` path and the Linux builder expected that exact
case. The dependency now uses the repository's canonical lowercase path, and
the deployment source archive explicitly carries the shared
`needletail/crates/media-capability` verifier used by `av-contrib`.

A third deploy stopped on the Linux builder before host mutation because the
capability-auth feature, integration-test dependency, and related source had
landed after the previous lockfile snapshot. The lockfile was regenerated and
validated locally. The contributor now passes all 50 tests and strict Clippy
across all targets and features before a deployment is allowed to continue.

### Epoch-activation deployment

The next deployment completed on all four nodes. Measured routes were:

| Path | RTT | Stretch |
| --- | ---: | ---: |
| Direct London → Tokyo | 240.572 ms | `1.000×` |
| Amsterdam relay route | 263.528 ms | `1.095×` |
| Osaka relay route | 251.472 ms | `1.045×` |

The live mesh exposed the new aggregate and per-stream activation metrics. On
the initial source epoch, the maximum activation delay was 1.660 seconds. All
three nodes agreed on the epoch, retained zero known gaps, and stayed within
four objects of the freshest node. The LL-HLS manifest retained 50 ms parts
and a 150 ms part hold-back.

An operator probe requested an invalid manifest path and intentionally reset
the Tokyo edge afterward so that the resulting HTTP 404 counter could not
contaminate qualification. This exposed a measurement edge case: a relay that
starts during an older source epoch reports the age of that epoch as its
activation delay. A fresh contributor epoch restored a clean baseline, but a
relay restart during formal failover reproduced the false slow-activation
alert. The activation metric must therefore distinguish first observation of
an inherited epoch from activation of a newly created epoch.

### Full qualification attempt 1 — recovery gate failed

Artifact directory: `target/gcp-qualification/runs/20260715T223437Z`

The contributor-restart and route gates passed:

| Measurement | Result | Gate |
| --- | ---: | ---: |
| Maximum relay-side epoch activation | 1.771 s | ≤ 10 s |
| Local observer elapsed time | 9.961 s | diagnostic |
| Primary route stretch | `1.095×` | ≤ `1.15×` |
| Secondary route stretch | `1.045×` | ≤ `1.15×` |

Primary-outage telemetry also remained inside the fast-failover budgets:

| Measurement | Result | Gate |
| --- | ---: | ---: |
| Source-loss detection | 119.992 ms | ≤ 250 ms |
| Secondary activation | 12.158 ms | ≤ 250 ms |
| Decoded-media gap | 133.766 ms | ≤ 250 ms |
| Decoded objects during outage | 306 | positive |
| RaptorQ-repaired objects during outage | 38 | positive |
| Expired objects | 1 | ≤ 1 |
| Rejected datagrams | 0 | 0 |
| Deadline drops | 0 | 0 |

The run stopped at primary recovery, before controlled-loss injection. Restarting
the Amsterdam relay raised the inherited-epoch activation alert described
above. The outage also left the edge cache's published contiguous watermark
behind an object that had already fallen outside the retained rolling window;
the retained-window gap count was zero, but the publication watermark could no
longer advance. This needs a bounded retained-window fast-forward in the shared
playlist cache before the next live run.

Cleanup was verified after the failed gate: the Amsterdam relay service was
active, the qualification iptables chain was absent, and both London
contributor services were active.

### Qualification 2 redeploy interruption

The recovery fixes passed local tests and the replacement relay compiled on
Linux. The deployment then stopped before host mutation because the committed
contributor capability dependency had again used the macOS-tolerated uppercase
path `../Needletail/...`; the Linux source archive contains the canonical
lowercase `needletail` directory. The dependency was corrected, and deployment
now performs a fail-fast case-sensitivity check before cloud authentication,
route probes, or compilation.

This stopped attempt still produced a useful contemporaneous route sample:

| Path | RTT | Stretch |
| --- | ---: | ---: |
| Direct London → Tokyo | 356.836 ms | `1.000×` |
| Amsterdam relay route | 257.488 ms | `0.722×` |
| Osaka relay route | 253.323 ms | `0.710×` |

In this sample both relay routes were faster than the measured direct path,
illustrating why route choice is based on current measurements rather than a
fixed preference for direct delivery.

The next retry stopped at the same pre-mutation Linux build gate because the
mesh service's new subscribe-capability dependency contained the same uppercase
path. Its route sample was 249.215 ms direct, 257.415 ms through Amsterdam, and
251.199 ms through Osaka. Both service manifests now use lowercase paths, and
the fail-fast guard scans every Cargo manifest included in the deployment
archive rather than checking only the contributor manifest.

### Full qualification attempt 2 — passed

Artifact directory: `target/gcp-qualification/runs/20260715T225642Z`

The corrected build deployed with a 368.530 ms direct route measurement,
257.637 ms Amsterdam route, and 252.203 ms Osaka route. The formal gate then
passed end to end:

| Phase | Measurement | Result | Gate |
| --- | --- | ---: | ---: |
| Contributor restart | Maximum relay-side activation | 1.782 s | ≤ 10 s |
| Contributor restart | Observer elapsed time | 20.138 s | diagnostic |
| Routing | Primary stretch | `0.699×` | ≤ `1.15×` |
| Routing | Secondary stretch | `0.684×` | ≤ `1.15×` |
| Primary outage | Detection | 108.607 ms | ≤ 250 ms |
| Primary outage | Secondary activation | 31.994 ms | ≤ 250 ms |
| Primary outage | Media gap | 142.060 ms | ≤ 250 ms |
| Primary outage | Decoded objects | 330 | positive |
| Primary outage | Expired objects | 1 | ≤ 1 |
| Primary outage | Rejected datagrams | 0 | 0 |
| Primary outage | Deadline drops | 0 | 0 |
| Publication recovery | Maximum node lag | 4 objects | ≤ 4 |
| Publication recovery | Known retained gaps | 0 | 0 |
| Controlled 2% loss | Dropped datagrams | 67 | positive |
| Controlled 2% loss | RaptorQ decoded objects | 355 | positive |
| Controlled 2% loss | RaptorQ-repaired objects | 57 | ≥ 1 |
| Controlled 2% loss | Expired objects | 0 | 0 |
| Controlled 2% loss | Rejected datagrams | 0 | 0 |
| Controlled 2% loss | Deadline drops | 0 | 0 |

The failover state sequence was healthy → promoted → healthy, with one
promotion and one make-before-break demotion. Fourteen objects used the
surviving lane and no object lost both lanes. After the run, all mesh and
contributor APIs were alert-free, every publication view had zero gaps, the
Amsterdam relay and both London services were active, and the qualification
iptables chain was absent.

### Full qualification attempt 3 — strict expiry gate failed

Artifact directory: `target/gcp-qualification/runs/20260715T230001Z`

The repeated contributor restart converged with a maximum relay-side activation
delay of 1.577 seconds; the local observer elapsed time was 12.148 seconds. The
same deployed route sample remained within policy at `0.699×` primary stretch
and `0.684×` secondary stretch.

The primary outage remained within every latency budget:

| Measurement | Result | Gate |
| --- | ---: | ---: |
| Source-loss detection | 118.696 ms | ≤ 250 ms |
| Secondary activation | 77.435 ms | ≤ 250 ms |
| Decoded-media gap | 197.535 ms | ≤ 250 ms |
| Decoded objects | 334 | positive |
| RaptorQ-repaired objects | 40 | positive |
| Rejected datagrams | 0 | 0 |
| Deadline drops | 0 | 0 |

Two in-flight objects expired, exceeding the strict maximum of one, so the run
correctly failed before controlled-loss injection. Publication recovery itself
completed before the integrity assertion: the edge and Osaka relay were both
at object 690 during the outage with zero retained gaps, and the recovered
constellation returned to zero gaps and at most four objects of lag.

Cleanup was again explicit: the Amsterdam service and both London services were
active, the qualification iptables chain was absent, Mission Control had no
alerts, and the failover controller was healthy.

### Full qualification attempt 4 — passed

Artifact directory: `target/gcp-qualification/runs/20260715T230319Z`

The next immediate repeat passed every gate:

| Phase | Measurement | Result | Gate |
| --- | --- | ---: | ---: |
| Contributor restart | Maximum relay-side activation | 1.724 s | ≤ 10 s |
| Contributor restart | Observer elapsed time | 19.867 s | diagnostic |
| Routing | Primary stretch | `0.699×` | ≤ `1.15×` |
| Routing | Secondary stretch | `0.684×` | ≤ `1.15×` |
| Primary outage | Detection | 113.025 ms | ≤ 250 ms |
| Primary outage | Secondary activation | 19.915 ms | ≤ 250 ms |
| Primary outage | Media gap | 134.517 ms | ≤ 250 ms |
| Primary outage | Decoded objects | 339 | positive |
| Primary outage | Expired objects | 1 | ≤ 1 |
| Primary outage | Rejected datagrams | 0 | 0 |
| Primary outage | Deadline drops | 0 | 0 |
| Publication recovery | Maximum node lag | 3 objects | ≤ 4 |
| Publication recovery | Known retained gaps | 0 | 0 |
| Controlled 2% loss | Dropped datagrams | 76 | positive |
| Controlled 2% loss | RaptorQ decoded objects | 369 | positive |
| Controlled 2% loss | RaptorQ-repaired objects | 369 | ≥ 1 |
| Controlled 2% loss | Expired objects | 0 | 0 |
| Controlled 2% loss | Rejected datagrams | 0 | 0 |
| Controlled 2% loss | Deadline drops | 0 | 0 |

Fifteen objects used the surviving lane, no object lost both lanes, and the
state sequence again completed healthy → promoted → healthy. Cleanup confirmed
the Amsterdam relay and both London services active with no qualification loss
chain remaining.

### Full qualification attempt 5 — passed

Artifact directory: `target/gcp-qualification/runs/20260715T230643Z`

The third complete pass produced:

| Phase | Measurement | Result | Gate |
| --- | --- | ---: | ---: |
| Contributor restart | Maximum relay-side activation | 2.702 s | ≤ 10 s |
| Contributor restart | Observer elapsed time | 26.009 s | diagnostic |
| Routing | Primary stretch | `0.699×` | ≤ `1.15×` |
| Routing | Secondary stretch | `0.684×` | ≤ `1.15×` |
| Primary outage | Detection | 112.530 ms | ≤ 250 ms |
| Primary outage | Secondary activation | 22.140 ms | ≤ 250 ms |
| Primary outage | Media gap | 136.180 ms | ≤ 250 ms |
| Primary outage | Decoded objects | 302 | positive |
| Primary outage | Expired objects | 1 | ≤ 1 |
| Primary outage | Rejected datagrams | 0 | 0 |
| Primary outage | Deadline drops | 0 | 0 |
| Publication recovery | Maximum node lag | 4 objects | ≤ 4 |
| Publication recovery | Known retained gaps | 0 | 0 |
| Controlled 2% loss | Dropped datagrams | 78 | positive |
| Controlled 2% loss | RaptorQ decoded objects | 370 | positive |
| Controlled 2% loss | RaptorQ-repaired objects | 369 | ≥ 1 |
| Controlled 2% loss | Expired objects | 0 | 0 |
| Controlled 2% loss | Rejected datagrams | 0 | 0 |
| Controlled 2% loss | Deadline drops | 0 | 0 |

Thirteen objects used the surviving lane and none lost both lanes. Final cleanup
left the Amsterdam relay and both London services active, removed the loss
chain, returned the controller to healthy, and left all three publication views
alert-free with zero gaps and at most three objects of live lag.

### Corrected-build series summary

Across four consecutive invocations there were three complete passes and one
strict failure caused by two expiries against a one-expiry maximum. Every run
remained within the 250 ms detection, activation, and media-gap budgets:

| Measurement | Observed range |
| --- | ---: |
| Source-epoch activation | 1.577–2.702 s |
| Primary-loss detection | 108.607–118.696 ms |
| Warm-secondary activation | 19.915–77.435 ms |
| Decoded-media gap | 134.517–197.535 ms |
| Publication gaps after recovery | 0 in every run |
| Maximum publication lag | 4 objects |

The three successful 2% loss phases dropped 67–78 datagrams, decoded 355–370
objects, and attributed 57–369 objects to RaptorQ repair. All three finished
with zero expired objects, rejected datagrams, or deadline drops during the
controlled-loss interval. The wide repaired-object range is retained as a
follow-up observability question rather than normalized away.

## Current qualification status

The corrected build has three complete passing qualifications and one preserved
strict-threshold variance failure. The live system is healthy and clean after
the series. The remaining optimization targets are the occasional second
in-flight expiry and the large variation in RaptorQ repaired-object attribution.
