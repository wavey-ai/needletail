# 19 July 2026: bundled eight-track Opus H3 tails

This run measures the first deployed live-tail path after the cache and H3
optimizations identified on 18 July. The playback edge now resolves a bounded,
generation-safe consecutive cache range, waits on exact stream/sequence keys,
and returns all eight requested tracks in one synchronized H3 response every
5 ms. The canonical cache unit remains 5 ms and no media is transcoded.

The machine-readable record is
[`20260719T185313Z-opus-h3-tail-bundle.json`](evidence/20260719T185313Z-opus-h3-tail-bundle.json).
Raw reports are under
`target/gcp-qualification/live-tail-serialization/20260719T170646Z-resume/runs`.

## Result

Twenty-four eight-track customers are the repeatable short-window candidate on
the tested two-vCPU `n2-standard-2` edge. Three fresh-process repetitions each
delivered every part with zero deadline, continuity, Opus, or kernel UDP-buffer
errors. Their availability p99 values were 18.051, 16.738, and 16.578 ms. The
edge used approximately 57.4–57.8% of the two-vCPU host during the active media
interval, retaining roughly 42% CPU headroom.

Twenty-eight customers still delivered every byte, but availability p99 rose
to 20.759 ms and crossed the provisional 20 ms gate. Thirty-two customers also
remained byte-complete, at 22.967 ms p99, while approximate edge CPU reached
70.45% of the host. This makes 28 the first latency-gate miss and 32 the first
CPU-headroom miss. Neither is a correctness failure.

| Customers | H3 connections | Track tails | Parts received | Bundle responses | Availability p99 | Edge host CPU | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 24, run 1 | 24 | 192 | 768,000 | 96,000 | 18.051 ms | 57.434% | candidate pass |
| 24, run 2 | 24 | 192 | 768,000 | 96,000 | 16.738 ms | 57.804% | candidate pass |
| 24, run 3 | 24 | 192 | 768,000 | 96,000 | 16.578 ms | 57.747% | candidate pass |
| 28 | 28 | 224 | 896,000 | 112,000 | 20.759 ms | 63.180% | latency gate miss |
| 32 | 32 | 256 | 1,024,000 | 128,000 | 22.967 ms | about 70.45% | latency and headroom miss |

The three 24-customer runs carried 2,304,000 exact parts in 288,000 bundle
responses. Availability-p99 spread was 8.60% of the mean and edge-CPU spread
was 0.64%, both inside the 10% repeatability gate.

This is a latency/headroom candidate, not a production tier. It has not yet run
for 30 minutes, the edge was restarted for each recorded tier, and the
provisional 20 ms availability gate is not the earlier strict 2 ms
cache-to-client gate.

## Strict 60-second profile follow-up

A matched private-GCP profile then held the candidate at 24 customers for a
60-second media window: 24 persistent H3 connections, eight tracks/customer,
one 5 ms part/track/bundle, a deterministic 750 ms arrival window, and a strict
20 ms per-response deadline. The first run identified repeated canonical media
object decoding, allocation, and SHA work in the hot cache-to-client path.

Three bounded changes were tested independently:

- a fixed internal live-slot index retains the complete canonical envelope for
  exact conflict/replication semantics while exposing prevalidated `Bytes`
  ranges for hot reads;
- the existing IEEE CRC-32 wire value uses the accelerated `crc32fast`
  implementation, with reference-equivalence and split-update tests; and
- a leaf with no downstream relay, WebTransport audio receiver, or native-audio
  subscription consumes AEP1 traffic before parse/copy/session tracking. Active
  subscribers and forwarding relays still use the validated path.

A fourth change replaced eight heap-backed joined exact-wait futures with
sequential exact reads under one shared absolute deadline. Each registration
still rechecks the cache before waiting, so tracks can arrive in any order
without a lost wakeup or an extended bundle deadline.

| Build | Edge CPU, one core | Two-vCPU host | Availability p99 | Cache-to-client p99 | Late bundles | Exact parts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| pre-profile baseline | 118.831% | 59.415% | 14.633 ms | 7.351 ms | 209 | 2,304,000/2,304,000 |
| indexed canonical slot | 85.563% | 42.782% | 14.157 ms | 5.772 ms | 147 | 2,304,000/2,304,000 |
| indexed slot + accelerated CRC | 83.357% | 41.679% | 14.039 ms | 5.409 ms | 53 | 2,304,000/2,304,000 |
| plus zero-consumer AEP1 discard | 77.963% | 38.981% | 14.090 ms | 5.314 ms | 33 | 2,304,000/2,304,000 |
| plus sequential exact bundle waits | 69.529% | 34.765% | 10.801 ms | 8.399 ms | 9 | 2,304,000/2,304,000 |

The final build reduced edge CPU 41.49%, availability p99 26.19%, and late
bundles 95.69% from the matched baseline. It retains 65.24% host CPU headroom.
The sequential-wait change alone reduced CPU another 10.82% and late bundles
from 33 to 9; the former `futures_util::join_all::MaybeDone` flat-profile symbol
disappeared. Cache-to-client p99 increased from 5.314 to 8.399 ms even as
end-to-end availability improved, so that scheduling tradeoff remains an
explicit follow-up rather than being hidden by the net result.

Run `20260719T231836Z-24x8-strict20-sequential-bundle-v11` returned all 288,000
bundle responses and 2,304,000 track parts with zero missing parts, PTS errors,
Opus mismatches, HTTP errors, or not-found responses. Its strict result remains
false only because 9 of 288,000 bundles (0.003125%) exceeded 20 ms. The raw
six-node evidence is under
`target/gcp-qualification/live-tail-serialization/profile/20260719T231836Z-24x8-strict20-sequential-bundle-v11`;
the sanitized summary is
[`20260719T231836Z-opus-h3-tail-profile.json`](evidence/20260719T231836Z-opus-h3-tail-profile.json).
The immediately preceding retained profile,
[`20260719T225507Z-opus-h3-tail-profile.json`](evidence/20260719T225507Z-opus-h3-tail-profile.json),
is the zero-consumer AEP1 row used for the direct v11 comparison.

The exact retained run and profiler ledger is:

| Run | Deployed `av-mesh` SHA-256 | CPU event ns | Profile duration | Connection p99 | Wire bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| `20260719T215454Z-24x8-strict20-perf-baseline-v3` | not captured; retained as an explicit evidence gap | 77,165,819,500 | 64,937.574 ms | 5.166495 ms | 479,231,145 |
| `20260719T221714Z-24x8-strict20-indexed-slot-v5` | `8cc6f75305d62033d121abc59a4cf2e9a9b63cf0d08f689c030772af3e32a81f` | 55,577,882,500 | 64,955.196 ms | 5.263337 ms | 479,374,696 |
| `20260719T222955Z-24x8-strict20-indexed-crc-v6` | `3756ae6aeb260ac7ca6d726e7450250e5c61a9077a8f6dc2a475021be85d7ab1` | 54,115,571,125 | 64,920.217 ms | 5.237304 ms | 479,284,389 |
| `20260719T225507Z-24x8-strict20-zero-consumer-v9` | `0a50ab21504914f52780a4428d0784f9d0ad3289b04b001953e6413ec29c43ff` | 50,597,983,625 | 64,900.381 ms | 5.493971 ms | 479,141,454 |
| `20260719T231836Z-24x8-strict20-sequential-bundle-v11` | `cd05a9d03f12b00dddfa9158e978f0d89b5721c39be5bc419e01246716ae4278` | 45,140,697,875 | 64,923.582 ms | 5.474853 ms | 479,577,949 |

Every row sampled Linux `cpu-clock` at 199 Hz for approximately 65 seconds,
beginning three seconds after the source start. One-core CPU is event
nanoseconds divided by profile duration; host CPU divides that result by the
two available vCPUs. The reader used 24 persistent H3 connections, one per
customer, with eight tracks/connection, a 60-second measured window inside an
80-second process, one 5 ms part/track/response, a 20 ms response deadline,
5,000 ms source offset, and deterministic arrival seed `424242` spread over
750 ms. The source epoch came from a GCP host's realtime clock and was passed
unchanged to source and reader. A late response accounts for eight
deadline-missed track parts, so the track counter is divided by eight in the
table above. All five rows contain exactly 288,000 responses and 2,304,000
validated parts.

The tested Linux binary is SHA-256
`cd05a9d03f12b00dddfa9158e978f0d89b5721c39be5bc419e01246716ae4278`.
It was deployed on the London relays and edge for this profile. The prior
binary remains at
`/opt/needletail/backups/av-mesh-pre-sequential-bundle-20260719T2315Z/av-mesh`.
The operations UI, mesh API, and contributor feed remained healthy. The public
telemetry carrier was not enabled.

## What changed

The unbundled 5 ms probe opened one H3 connection per track and made 1,600 H3
responses/customer/s. The bundled probe uses one persistent H3 connection per
customer and makes 200 responses/customer/s; each response carries one exact
5 ms unit from all eight tracks. Cache media work remains 1,600 units/customer/s,
but response stream count falls 8x.

The underlying cache now:

- resolves stream index and generation once for a bounded consecutive range;
- verifies every immutable slot against the same generation before returning;
- waits on sharded exact stream/sequence notifications instead of a global
  live-tail wakeup; and
- stores only weak waiter registrations, so dropping a canceled request drops
  its strong request work immediately.

The edge performs eight exact track reads under one shared absolute deadline,
takes one shared availability state lock for the completed bundle, and emits
the bounded `NTB1` envelope.
This removes the historical near-one-core plateau: the valid 24- and
28-customer runs used more than one process core and scaled across both edge
workers.

The comparison with the older four-customer strict result is directional, not
a pure A/B. Connection geometry, response body geometry, and the declared
latency gate differ. The supported claim is that bundling and cache-range work
moved the short-window boundary materially while preserving every tested media
unit through 32 customers.

## Integrity canary

The retained one-customer canary used one H3 connection for eight tracks and
received all 32,000 expected Opus parts in 4,000 bundle responses. There were
zero missing or non-contiguous parts, deadline misses, and Opus packet
mismatches. Availability p99 was 13.259 ms.

Every capacity tier also checked playlist and initialization retrieval, exact
part count, PTS continuity, Opus media framing, and kernel UDP error deltas.
All valid tiers passed those checks.

## Private GCP path

All six roles ran in `europe-west2-c` on separate two-vCPU
`n2-standard-2` instances. Source-to-contributor and reader-to-edge load traffic
used the `10.84.0.0/16` private network. No load or media traffic crossed the
public Internet. Public Needletail Operations is low-rate observability and is
not part of the measured data plane.

The lab VMs use a six-hour `maxRunDuration`. They restarted before the strict
profile series while retaining their persistent disks and stable private IPs;
the Cloudflare records were updated to the replacement public edge and
contributor addresses. That reboot removed the reader's deliberately temporary
TLS trust file and caused excluded v10. The trust file was restored before
v11. After v11, public Needletail Operations, the mesh API, and the contributor
feed all returned HTTP 200. Public FEC telemetry remained disabled.

## Exact-envelope handoff follow-up

The next build lets `RelaySession` transfer one exact canonical envelope to
`av-mesh`. The envelope has already passed parsing and payload-hash checks.
The cache commits the verified object and its exact envelope directly. This
removes a second encode plus decode/hash cycle. Identity, announcement, replay,
and immutable-conflict checks remain active.

The Linux binary is SHA-256
`58fd48fb1c59905bac55a4d18c89b553e6d50c6ab41b96cad4afa55578f8fd0b`.
Its build ID is `1138155b20dfa09ffdc1177d9268254c957c004c`. The exact source
archive is SHA-256
`214d137dcc845ef28567b14607f90fa6851e4aa8ab79f7ead4a7a29341a6969b`.
The committed source revisions and per-file hashes are in
[`20260720T001022Z-opus-h3-canonical-envelope-profile.json`](evidence/20260720T001022Z-opus-h3-canonical-envelope-profile.json).

The deployment used the same private topology and 24-customer geometry. It
included an interleaved v11 control because host CPU changed from the earlier
profile series.

| Run | Build | Flat profile | Host CPU | Availability p99 | Cache sample coverage | Late bundles | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `20260719T235616Z-24x8-strict20-canonical-envelope-v12` | v12 | no; callchains enabled | excluded | 11.514 ms | 177/192 | 0 | media pass; CPU comparison excluded |
| `20260720T000115Z-24x8-strict20-canonical-envelope-v12-matched` | v12 | yes | 39.136% | 20.835 ms | 24/192 | 6,144 | deadline fail |
| `20260720T000528Z-24x8-strict20-v11-control` | v11 | yes | 42.007% | 11.556 ms | 0/192 | 0 | control pass |
| `20260720T001022Z-24x8-strict20-canonical-envelope-v12-repeat` | v12 | yes | 40.380% | 11.561 ms | 0/192 | 0 | run pass; series not repeatable |

Every row delivered all 2,304,000 parts in 288,000 responses. No row had a
missing part, PTS error, Opus mismatch, reader failure, HTTP error, or not-found
response. The first v12 run used call-chain capture. Its media result is valid,
but its CPU value is not comparable with the flat v11 profiles.

The final v12 repeat used 3.873% less CPU than the adjacent v11 control.
Availability p99 changed by only 0.005 ms. Canonical encode disappeared from
the flat profile. SHA-256 fell from 2.33% to 1.22% of flat samples. These
results confirm the intended CPU direction under the current host state.

The series does not pass the strict repeatability gate. One flat v12 run had a
global 20.835 ms availability p99 and 6,144 late bundles. The other two v12
media runs had no late bundles. The final control and repeat also had no valid
cache-to-client samples. Their cache p99 values are unavailable, not zero.
These two evidence gaps block an endurance claim.

The v12 binary remains on the London relays and edge. The v11 rollback is at
`/opt/needletail/backups/av-mesh-pre-canonical-envelope-20260719T2352Z/av-mesh`.
Public Needletail Operations, the mesh API, and the contributor feed returned
HTTP 200. Public FEC telemetry remained disabled.

## Invalid attempts

The following timestamped attempts are deliberately retained but excluded:

- `20260719T175825Z-1x8-private-canary` started before a usable publication
  window and received no media;
- `20260719T182357Z-32x8-private-bundle` started its reader after publication
  had stopped and received no media.
- `20260719T225029Z-24x8-strict20-zero-consumer-v7` was canceled because the
  controller expanded remote PID variables while composing the launch command;
  all processes and services were reset before another run.
- `20260719T225205Z-24x8-strict20-zero-consumer-v8` used the controller host's
  skewed clock for the source epoch. Its uniform 4.371-second clock error and
  52,400 opening-window misses make it invalid for optimization comparison.
- `20260719T231535Z-24x8-strict20-sequential-bundle-v10` launched after the
  reader VM reboot had removed its temporary TLS certificate. It made no edge
  requests and is excluded; the certificate and service state were restored
  before v11.

The valid reruns changed only orchestration timing. The 32-customer CPU sampler
captured lifetime `ps` CPU instead of process tick deltas, so that tier's CPU
value is approximate; its latency result and all media counters are retained.

## Next gates

1. Attribute the global 20.835 ms tail excursion. Restore valid
   cache-to-client samples before another optimization comparison.
2. Repeat the strict short-window series without a deadline failure. Do not
   call the 24-customer tier repeatable while the retained failure remains.
3. After repeatability passes, run the candidate for at least 30 minutes. The
   run must have stable RSS, zero
   loss, and at least 30% edge CPU headroom.
4. Run repeated connect, cancel, timeout, and slow-reader churn without an edge
   restart. Add bounded waiter, task, and connection counts so cleanup is
   measured rather than inferred from weak-reference semantics.
5. Profile the bundled path at 24 and 28 customers before changing the next hot
   boundary.
6. Qualify zero-offset startup with a declared track manifest or start barrier.
7. Repeat the final build geographically and size channel count separately
   from viewer count.

The GCP lab remains active for these follow-up tests. Test source and reader
processes exited after each retained run; the native Needletail services remain
supervised and healthy.

## Follow-up

The [20 July clock-qualified series](2026-07-20-opus-h3-clock-qualified-tail.md)
resolved the first two gates above. It attributed the apparent completion tail
to probe metadata and reporting work, restored all 192 cache samples, and
repeated the accepted v12 build twice with zero late bundle. The 30-minute
endurance gate remains open.
