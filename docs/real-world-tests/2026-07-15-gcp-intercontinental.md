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
| Repair-assisted decodes during outage (legacy `repaired_objects`) | 38 | positive |
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
| Controlled 2% loss | Repair-assisted decodes (legacy `repaired_objects`) | 57 | ≥ 1 |
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
| Repair-assisted decodes (legacy `repaired_objects`) | 40 | positive |
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
| Controlled 2% loss | Repair-assisted decodes (legacy `repaired_objects`) | 369 | ≥ 1 |
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
| Controlled 2% loss | Repair-assisted decodes (legacy `repaired_objects`) | 369 | ≥ 1 |
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
objects, and admitted repair symbols before decode for 57–369 objects. All three finished
with zero expired objects, rejected datagrams, or deadline drops during the
controlled-loss interval. The wide repair-assisted-decode range is retained as a
follow-up observability question rather than normalized away.

The July 15 receiver counter named `repaired_objects` measured repair-assisted
decode: at least one repair symbol arrived before completion. It did not prove
that a source symbol was missing. The July 16 instrumentation separates that
legacy attribution from exact `fec_recovered_objects` and
`fec_recovered_source_symbols` counters; new qualification gates use the exact
counters.

### Exact RaptorQ attribution and zero-expiry failover correction

The previous series identified two separate observability and recovery issues:

- `repaired_objects` counted any decode that admitted a repair symbol. It could
  not distinguish a repair-assisted decode from reconstruction of an absent
  source symbol.
- the warm secondary kept subscription and repair state but discarded source
  datagrams until promotion. Objects already in flight at the playback edge
  could therefore expire even though the secondary path was warm.

The receiver now counts the source-symbol deficit at decode time and exports
`fec_recovered_objects` plus `fec_recovered_source_symbols`. The legacy
attribution has the precise name `repair_assisted_objects`. Qualification gates
use the exact recovery counters.

Each warm child now retains a deadline-bounded source replay window: at most the
latest four objects, 2,048 datagrams, and 4 MiB. Promotion changes the lane to
source-plus-repair and immediately replays only unexpired source datagrams. The
buffer exposes current bytes/datagrams, replay totals, expiry removals, rolling
window retirements, and hard-bound evictions through the mesh API, Prometheus,
Grafana, and the operations dashboard.

The deployment tested these revisions:

| Repository | Revision tested |
| --- | --- |
| Needletail | `5b8a957` plus working-tree diff SHA-256 `a578bd0e68aeb0d027040e829bff532325e05ecb2a16d23db8b9e53bb26dc1ed` |
| av-mesh | `730cc1e` plus working-tree diff SHA-256 `cb8703788e1d6246117413f02639395506d7904bc4074667eb6b528b7a7a68fa` |
| av-contrib | `9aa15ea` |
| media-object | `881f1fe` |
| raptor-fec | `2722922` |
| relay-session | `41fbf36` |
| playlists | `d0b4a09` |

The Linux build completed on the London host and deployed to London,
Amsterdam, Osaka, and Tokyo. The deployment measured 241.231 ms direct RTT,
259.508 ms through Amsterdam (`1.075766×` stretch), and 250.794 ms through
Osaka (`1.039643×` stretch). Both selected routes satisfied the `1.15×` gate.

### Exact-recovery qualification runs 6–8 — all passed

Artifact directories:

- `target/gcp-qualification/runs/20260715T234426Z`
- `target/gcp-qualification/runs/20260715T234654Z`
- `target/gcp-qualification/runs/20260715T234910Z`

Every invocation restarted the contributor, stopped the Amsterdam primary,
verified warm-secondary promotion and publication convergence, restored the
primary with make-before-break demotion, then injected 2% random loss for 15
seconds on the primary RaptorQ ingress.

| Run | Epoch activation | Detection | Warm activation | Media gap | Decoded during outage | Warm source replay | Expired | Max publication lag | Gaps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `20260715T234426Z` | 1.763 s | 112.392 ms | 9.815 ms | 124.811 ms | 313 | 47 datagrams | 0 | 3 objects | 0 |
| `20260715T234654Z` | 1.627 s | 103.467 ms | 9.704 ms | 114.679 ms | 328 | 38 datagrams | 0 | 4 objects | 0 |
| `20260715T234910Z` | 1.773 s | 123.068 ms | 9.963 ms | 135.308 ms | 409 | 46 datagrams | 0 | 3 objects | 0 |

All failover measurements remained inside their 250 ms budgets. The strict
expiry gate was zero, and all three invocations met it. Each state sequence was
healthy → promoted → healthy with one promotion and one make-before-break
demotion.

The controlled-loss phases directly proved missing-source reconstruction:

| Run | Dropped datagrams | Decoded objects | Exact FEC-recovered objects | Reconstructed source symbols | Repair-assisted objects | Expired / rejected / deadline drops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `20260715T234426Z` | 78 | 368 | 223 | 399 | 223 | 0 / 0 / 0 |
| `20260715T234654Z` | 81 | 384 | 384 | 1,202 | 384 | 0 / 0 / 0 |
| `20260715T234910Z` | 76 | 381 | 188 | 330 | 188 | 0 / 0 / 0 |

The exact reconstruction totals vary with loss placement and the number of
source symbols absent when each object completes. The integrity results were
stable: all three loss phases reconstructed source data, and all completed with
zero expiry, rejection, and deadline-drop deltas.

The next invocation's clean baseline verified cleanup after each of the first
two runs. A separate final audit verified the Amsterdam relay active, both
London services active, the qualification iptables chain absent, zero alerts,
a healthy failover controller, zero publication gaps, and a maximum live lag
of two objects.

## Current qualification status

The exact-recovery build has three consecutive complete passes under the
zero-expiry failover gate. The versioned v3 evidence preserves per-run exact
RaptorQ recovery, warm-source replay, route stretch, publication convergence,
and cleanup state.
