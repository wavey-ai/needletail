# Soundkit v2 FLAC and Opus fixture replay results: 2026-08-20

## Decision

The prepared-fixture method successfully removed live encoding and `av-contrib`
capacity from the measured path. The current Azure deployment is not yet
production-qualified at 8 or more synchronized stereo tracks under the strict
mesh-wide zero-expiry gate.

Eight tracks did deliver every object to all 12 playback edges for 120 seconds.
One object nevertheless expired at the primary backbone. This is a useful
edge-delivery baseline, not a production capacity claim.

The first proven bottleneck is the East Asia warm-secondary backbone. At 32
tracks it approaches one fully occupied CPU core and begins expiring objects.
At 64 tracks it records kernel receive-buffer loss and fails most objects. The
primary backbone remains clean at 64 tracks, so the limit is specifically the
secondary decode/repair path rather than the replay source or primary fanout.

## Scope

- Date: 2026-08-20 UTC
- Cloud scope: 24 already-running Azure VMs in 12 regions
- Topology generation: `2026082001`
- Deployment classification: `single_provider_qualification`
- Network classification: `controlled_public_udp`
- Origin: Canada East
- Primary backbones: Central US and East US 2
- Warm-secondary backbone: East Asia
- Regional relays: 8
- Playback edges: 12, one in every region
- GCP resources started: none
- VMs stopped or deallocated: none

The measured path was:

```text
immutable Soundkit v2 FLAC and Opus records
        |
        v
fresh 50 ms canonical audio objects, one per codec
        |
        v
RelaySession plus RaptorQ source/repair datagrams
        |
        v
Needletail backbone, regional relay, and playback-edge caches
```

This implementation groups every synchronized track for a codec into one
canonical object. Object scheduling therefore remains two objects per 50 ms
part while protected bytes and RaptorQ symbols scale with track count.

This run did not traverse the native edge AEP1 playback egress. The current
edge receiver commits replicated RelaySession objects to its cache but does not
rebroadcast those objects through the native AEP1 subscriber socket. Speaker,
browser decode, and LL-HLS playback are therefore outside this result.

## Artifacts

| Artifact | Value |
|---|---|
| Fixture | `target/multicloud-qualification/fixtures/lori-flac-opus-v2.ntv2fix` |
| Fixture file SHA-256 | `fa37d7ba49d2acb232a48d41e7aecb5d7344ca39cc97ff5ec3bd56e3f18c32b7` |
| Fixture content digest | `b9a97e614e71f1c7228288a6ac38aede403b9fb1e67cd15deefbb03279eed126` |
| Producer digest | `7e536d1909a38b410ffef12961307eaca858a28f300352a0627cb9da6c501e6f` |
| Replay binary SHA-256 | `3bc5b42b822ca3d383e63903cd9894f06ef005361a9c15c8751870d1af06b780` |
| Raw run directory | `target/multicloud-qualification/runs/fixture-24-20260820T222151Z` |
| Compiled plan | `target/multicloud-qualification/runtime-24/compiled-plan.json` |

The five-second fixture contains 1,000 synchronized AEP1 epochs and 2,000
Soundkit v2 records at 48 kHz stereo: one FLAC and one Opus representation for
each epoch. Replay normalizes the transport cadence to 240 frames per 5 ms
epoch while retaining the codec-valid Soundkit payload bytes.

## Direct v2 ingress baseline

Before RelaySession load multiplication, a direct AEP1/RaptorQ v2 lane test at
Central US received all expected FLAC and Opus epochs with zero deadline misses.
Render-ready latency was 8.55 ms minimum, 14.17 ms p50, 18.53 ms p95, 19.93 ms
p99, and 20.58 ms maximum. This is a direct ingress baseline, not mesh-wide or
speaker latency.

## Offered-load ladder

Each short point ran for 10 seconds and completed without source UDP send
errors.

| Tracks | FLAC + Opus representation epochs | Wire bytes | Approx. wire rate | Source pacing p50 / p99 / max |
|---:|---:|---:|---:|---:|
| 8 | 32,000 | 39,093,664 | 28.4 Mb/s | 1.05 / 1.98 / 3.24 ms |
| 32 | 128,000 | 147,459,192 | 107.2 Mb/s | 1.08 / 5.27 / 7.43 ms |
| 64 | 256,000 | 288,768,024 | 210.0 Mb/s | 1.20 / 10.64 / 13.46 ms |
| 128 | 512,000 | 572,830,176 | 416.4 Mb/s | 1.56 / 23.01 / 26.30 ms |
| 256 | 1,024,000 | 1,137,808,872 | 805.5 Mb/s | 177.20 / 335.39 / 349.38 ms |

The 256-track source itself no longer maintains real-time pacing. It is an
overload point, not a delivered-capacity result.

## Isolated sustained results

Expected objects are two codec objects per 50 ms part. CPU values in the raw
TSV files are Linux process tick deltas, not host-wide CPU percentages.

| Tracks | Duration | Expected objects per node | Result |
|---:|---:|---:|---|
| 128 | 15 s | 600 | Failed: primary and secondary kernel receive-buffer drops were 110,226 and 166,468; primary decoded 324 and expired 133; secondary decoded 101 and expired 143. |
| 64 | 30 s | 1,200 | Failed redundancy: primary decoded 1,200/1,200 with zero expiry and kernel drops; secondary dropped 90,772 kernel datagrams, decoded 276, and expired 386. |
| 32 | 60 s | 2,400 | Near boundary but failed: zero kernel drops; 19/23 nodes were complete. Secondary and three edges expired 1-5 objects. |
| 16 | 60 s | 2,400 | Failed strict gate: zero kernel and deadline drops, but the Canada Central relay and edge each decoded 2,399 and expired one object. |
| 8 | 120 s | 4,800 | Playback-edge pass: all 12 edges decoded 4,800/4,800 with zero expiry, kernel loss, deadline drops, known gaps, or lag. Strict mesh-wide gate failed because the primary backbone decoded 4,799 and expired one object. |
| 4 | 600 s | 24,000 | Failed strict gate: source p99 pacing was 1.94 ms with zero source, kernel, or deadline drops. Eight of 288,000 playback-edge objects expired: Brazil South 2, Canada Central 5, and East Asia 1. The other nine edges were complete. |

RaptorQ recovery is active. The Canada Central relay's cumulative counters
recorded nine FEC-recovered objects and 104 reconstructed source symbols. The
isolated one-object loss at 16 tracks shows that the current repair budget and
public-path timing do not guarantee recovery before every object deadline.

The 10-minute four-track run makes the low-rate defect measurable rather than
anecdotal. Its playback-edge object expiry rate was approximately 0.0028% even
though every process remained active and no kernel receive-buffer or deadline
drop increased. The failures propagated from the Brazil South and Canada
Central relays; East Asia recorded one edge-local expiry. This is a repair
coverage/public-path reliability failure, not CPU saturation.

### Minimum-repair comparison

A five-minute four-track comparison retained 12% proportional repair but raised
the minimum from one to four repair symbols per object. All 12 playback edges,
all eight regional relays, the secondary backbone, and the tertiary backbone
decoded 12,000/12,000 objects with zero expiry, deadline drops, or kernel drop
deltas. The primary backbone decoded 11,999/12,000 because its source-only
origin lane receives no repair symbols.

The repair increase changed five-minute wire volume from an approximately
half-duration baseline of 608.8 MB to 618.8 MB, about 1.6%, and preserved source
pacing at 1.94 ms p99 and 3.47 ms maximum. Four minimum repair symbols are the
better playback-edge policy for these small objects. The source-only origin to
primary-backbone lane still needs its own repair budget before the strict
mesh-wide gate can pass.

## Regional activation delay at 8 tracks

The latest source-epoch activation delay ranged from 55.8 to 168.7 ms for FLAC
and 65.7 to 178.5 ms for Opus across the 12 playback edges. Every edge reported
zero known gaps and zero lag parts after the run.

These gauges measure first canonical activation for the replay epoch. They are
not a p50/p95/p99 publication-latency distribution and do not include browser,
decoder, audio-device, or speaker buffering.

## Defects and improvements, in priority order

1. Optimize or parallelize warm-secondary RaptorQ decode. It reaches roughly
   one CPU core at 32 tracks and is the first deterministic saturation point.
2. Increase and verify service and kernel UDP receive-buffer sizing, but do not
   treat buffer growth as a substitute for fixing sustained decode throughput.
3. Add repair symbols to the origin-to-primary-backbone lane; secondary-path
   repair cannot heal loss before the primary backbone.
4. Add a native edge bridge from replicated canonical cache objects to AEP1 or
   the chosen playback publication path. Without it, a player cannot consume
   this exact mesh replay.
5. Attach a usable synchronized source clock to replayed MediaObjects so
   `publication_to_available` histograms produce p50, p95, p99, and maximum
   end-to-end mesh latency rather than only epoch-activation gauges.
6. Run the same topology on private inter-region networking. Public UDP is a
   qualification convenience, not the intended production trust or loss model.
7. Repeat the highest clean point for one hour, then apply controlled loss,
   jitter, reorder, and burst profiles.
8. Add per-run counter-window collection to the replay runner so an operator
   cannot accidentally compare cumulative drop counters.
9. Capture UI evidence with a functioning browser automation surface. Fleet
   telemetry now advertises all 24 nodes and 44 measured links publicly.

## Operational restoration

The temporary replay peers used ports `22450` and `22451`. After the measured
runs, both backbone plans were restored and their services were restarted
active on the compiled contributor peers `20.175.49.9:22400` and
`20.175.49.9:22401`. The contributor remained active. No VM was shut down.
