# 17 July 2026: local multichannel LL-HLS sizing

## Result

> Historical implementation note: this record measured the former
> PCM-to-FLAC path on a shared laptop. PCM now remains PCM in `ipcm`/`fpcm`
> LL-HLS. Use the
> [raw PCM GCP and H3 capacity record](2026-07-17-pcm-h3-capacity.md) for the
> current implementation and server boundary.

Run `local-20260717T162832Z-multichannel-llhls-sizing` is a local pre-cloud
sizing record for 16-, 32-, 64-, and 128-channel 48 kHz lossless AEP1 streams
ending in mandatory 5 ms FLAC LL-HLS over certificate-verified persistent H3.

It is not a completed cloud mesh qualification. It was run on one Apple M1
developer workstation over loopback, so sender, contributor, H3 server, and
readers contended for the same CPU. The result is useful for finding local code
bottlenecks before spending on dedicated cloud hosts; it is not a provider
server-size claim for 64- or 128-channel streams.

Versioned evidence:

- [`local-20260717T162832Z-multichannel-llhls-sizing.json`](evidence/local-20260717T162832Z-multichannel-llhls-sizing.json)

Raw terminal output was not retained as a separate `target/` artifact because
these were ad-hoc local loopback checks during implementation. The sanitized
measurements needed to reproduce the conclusion are embedded in the evidence
file.

## Measurement boundary

Every tested logical stream used 48 kHz S24LE audio, 5 ms epochs, and 5 ms
LL-HLS parts. FLAC supports at most 8 channels per elementary stream, so wide
logical streams were split into synchronized 8-channel renditions:

| Logical stream | LL-HLS renditions |
| ---: | ---: |
| 16 channels | 2 |
| 32 channels | 4 |
| 64 channels | 8 |
| 128 channels | 16 |

The probe sampled the first and last rendition for each wide stream. That
checks the lowest and highest stream IDs without making the local reader load
dominate the test.

## PCM-source result

PCM-source is the server-side FLAC-encode test: the AEP1 source carries PCM,
and `av-contrib` must recover the audio groups, encode each 8-channel group to
FLAC, package fMP4, update LL-HLS state, and serve parts over H3.

After sharding LL-HLS packaging per rendition, local PCM-source results were:

| Logical stream | Source datagrams | Wire bytes | HLS sampled parts | p50 | p95 | p99 | Missing |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16ch | 12,000 | 13,944,000 | 2,000 / 2,000 | 4.734-4.755 ms | 5.919-5.949 ms | 6.757-6.816 ms | 0 |
| 32ch | 24,000 | 26,808,000 | 2,000 / 2,000 | 7.265-8.300 ms | 15.638-18.521 ms | 24.600-28.313 ms | 0 |
| 64ch | 48,000 | 52,536,000 | 2,000 / 2,000 | 256.034-261.113 ms | 674.259-679.096 ms | 689.763-694.348 ms | 0 |
| 128ch | 96,000 | 103,992,000 | 1,303 / 2,000 | 2053.275-2062.693 ms | 4595.416-4599.891 ms | 4716.588-4720.890 ms | 697 |

That establishes the current local low-latency envelope:

- 16-channel logical streams are healthy locally.
- 32-channel logical streams are still usable locally after sharding.
- 64-channel logical streams complete in the best local run, but are not
  low-latency.
- 128-channel logical streams exceed this local setup.

## FLAC-source comparison

FLAC-source removes server-side FLAC encoding: the source sends FLAC AEP1
groups and `av-contrib` only recovers, packages, and serves them as LL-HLS.

The FLAC-source comparison did not rescue 64/128-channel local latency:

| Logical stream | Source datagrams | Wire bytes | HLS sampled parts | p50 | p95 | p99 | Missing |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 64ch | 44,876 | 45,733,792 | 1,652 / 2,000 | 496.737-514.200 ms | 3565.792-3598.278 ms | 3816.499-3847.845 ms | 348 |
| 128ch | 90,849 | 95,305,589 | 957 / 2,000 | 2312.133-2313.640 ms | 5213.314-5224.722 ms | 5532.349-5543.008 ms | 1043 |

The server counters for the FLAC-source run showed zero HLS handoff drops and
all 24,000 expected groups completed:

| Counter | Value |
| --- | ---: |
| HLS queue capacity | 32,768 |
| HLS queue enqueued | 137,725 |
| HLS queue dropped | 0 |
| HLS queue max depth | 24,851 |
| Worker datagrams | 137,725 |
| Groups completed | 24,000 |
| Worker errors | 0 |

This means FLAC encoding is not the only bottleneck. On the local workstation,
the full system falls behind because the sender, high-rate UDP ingest, mesh
egress, recovery, packaging, and H3 readers all share the same CPU. The
deterministic 64/128-channel signal also barely compressed in 5 ms FLAC frames,
so moving FLAC work to the sender mostly moved CPU contention rather than
removing it from the test.

## Code changes made during the sizing pass

- `av-contrib/src/audio_epoch_hls.rs`: sharded AEP1-to-LL-HLS packaging by
  rendition. This removed the single global FLAC/fMP4 worker bottleneck.
- `av-contrib/src/bin/av-contrib.rs`: sampled overload logging, skipped
  session-inspection work when no UDP tap subscribers exist, and handed LL-HLS
  work off before mesh egress.
- `av-contrib/src/bin/aep1-48k-probe.rs`: added logical 16-128 channel stream
  generation with 8-channel FLAC-safe groups.
- `soundkit/soundkit-flac/src/frame_codec.rs`: raised the defensive packet cap
  for realtime 8-channel FLAC frames.

## Tests run

The implementation build and focused tests passed:

```sh
cargo check --bin av-contrib --bin aep1-48k-probe
cargo test --lib audio_epoch_hls -- --nocapture
cargo test --bin av-contrib audio_epoch_hls_drop_logging_is_sampled_under_overload -- --nocapture
cargo build --release --bin av-contrib --bin aep1-48k-probe
```

## Conclusion

Do not size 64- or 128-channel service capacity from this laptop run. Use a
dedicated cloud split with separate sender, contributor, and reader/load hosts.

For the current local code path:

- one 16-channel 5 ms LL-HLS stream is safe;
- one 32-channel 5 ms LL-HLS stream is plausible after the sharding fix;
- 64 channels requires dedicated-host validation;
- 128 channels needs either more host capacity and/or further ingest/datagram
  reduction work before it can be claimed.

The next server-sizing test should run on dedicated cloud hosts and separately
record:

- PCM-source publication, which tests server-side FLAC encoding;
- FLAC-source publication, which tests recovery/package/H3 serving without
  server-side FLAC encoding;
- reader/load fanout from a separate host;
- mesh forwarding enabled, because production publication does not stop at the
  origin LL-HLS cache.

## GCP status at the time of this historical run

The requested six-node GCP DAG plus separate reader/load VM was then blocked by
project CPU quota. The project quota observed during this work was 12 vCPU, and
an unrelated existing VM, `yl-encodec-1`, was using 4 vCPU. The partial
Needletail GCP resources created earlier were torn down. No cloud test
resources from this local sizing pass were left running. That quota condition
was later cleared and the current PCM-to-PCM GCP DAG qualification completed.
