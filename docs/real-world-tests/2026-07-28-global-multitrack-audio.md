# Global multitrack FLAC audio test

Date: July 28, 2026

Run: `20260728T113000Z-linode16-8track-recovery20-combined`

Result: **Strict qualification failed.**

This run is the current global audio performance record.
It shows useful latency, recovery, and capacity results.
It does not prove zero-loss rendered audio or a production service level agreement.

## Terms

An **epoch** is one 5 ms audio time unit for one track.

An **edge-track epoch** is one epoch measured at one playback edge.

**FEC** means forward error correction.
FEC adds repair data to a realtime UDP stream.

**LL-HLS** means Low-Latency HTTP Live Streaming.
This run used 250 ms FLAC and Opus LL-HLS parts.

## Test method

The source sent eight independent stereo tracks for 600 seconds.
The complete source contained 16 channels at 48 kHz.

The test used prepared S24LE PCM input files.
The timed source path did not use FFmpeg.
The source media path created FLAC and Opus output.

The test measured UDP with FEC and LL-HLS at the same time.
All five edges used one source clock.
The probes started before the source.

The UDP probes measured 5 ms epochs.
The LL-HLS probes measured 250 ms parts.
Each LL-HLS lossless stream also had an Opus companion stream.

## Tested topology

The mesh had ten nodes.
Six nodes were in GCP.
Four nodes were in Azure.
The source was a separate 16-vCPU Linode host in London.

| Node | Provider | Region | Role | Machine |
| --- | --- | --- | --- | --- |
| `contrib-london` | GCP | `europe-west2` | Origin | `n2-standard-2` |
| `relay-primary-amsterdam` | GCP | `europe-west4` | Backbone | `n1-standard-1` |
| `relay-secondary-japan` | Azure | `japaneast` | Backbone | Not in captured inventory |
| `relay-regional-osaka` | GCP | `asia-northeast2` | Regional relay | `n1-standard-1` |
| `relay-regional-australia` | Azure | `australiaeast` | Regional relay | Not in captured inventory |
| `edge-london` | GCP | `europe-west2` | Playback edge | `n2-standard-2` |
| `edge-tokyo` | GCP | `asia-northeast1` | Playback edge | `n2-standard-2` |
| `edge-sydney` | GCP | `australia-southeast1` | Playback edge | `n2-standard-2` |
| `edge-australia` | Azure | `australiaeast` | Playback edge | Not in captured inventory |
| `edge-japan` | Azure | `japaneast` | Playback edge | Not in captured inventory |

![Global qualification mesh](../performance/charts/2026-07-28-global-mesh.svg)

## Tested builds

The run used these base revisions:

| Component | Base revision |
| --- | --- |
| Needletail | `c340509df41d7500234bd61f50e86c01748c4a20` |
| av-contrib | `e30c054d4a37958a50e6d1fcad737f4a6dfb49b1` |
| av-mesh | `7b75c679ab774c6a4744f4abda2f084e3e5a0abf` |
| DAW Nexus | `323a994a4205fb37b44b0d5898d83a56e4986328` |
| Playlists | `2d3043376dec86bca0a8dea33c6fe614089b3ca1` |

The run also included uncommitted working-tree changes.
The deployed binaries had these SHA-256 values:

| Binary | SHA-256 |
| --- | --- |
| DAW test source | `9bfd9cf6593fd97497a6065bd66d952eaf86554587152ab0fa9c2edd938fcc69` |
| Probe | `9247236b7ac10ea84b8eeb974be902ab7144d1dc9fd1ee31ab73c78775fdf935` |
| av-contrib | `9ee0a997a9875038bfeec3f2834bffd5c44f4d62a57869627ef282601eb5b084` |
| av-mesh | `df296dabfd716ca4098b958e45e5c2d8a577fbd01fb6079941a99826756e03e0` |

## Source capacity

The source passed all 15 capacity gates.
It produced 960,000 encoded track packets.
It had zero send errors, dropped frames, and overload warnings.

The source host CPU P99 was 4.378 percent.
The source process capacity P99 was 4.437 percent of the 16-vCPU host.
The minimum encoder rate was 1,596.393 packets each second.
This value was 99.775 percent of the required rate.

The minimum available memory was 93.554 percent.
The maximum resident memory was 4.191 percent.
The source did not limit this run.

## Contributor result

The contributor passed its gates.
It completed all 1,920,000 expected audio groups.
RaptorQ recovered seven missing source fragments before LL-HLS packaging.

The contributor had zero HLS worker errors.
It had zero HLS, mesh, and ingress queue drops.
It also had zero ingress errors and kernel UDP socket drops.

## UDP with FEC

The UDP lane delivered 4,799,996 of 4,800,000 edge-track epochs.
This result is 99.999917 percent complete.
The missing fraction is 0.000083 percent.

One correlated event occurred at the Tokyo edge during source second 385.
Tracks 4 through 7 each missed one 5 ms epoch.
All other UDP edge-track lanes were complete.

The UDP probes reported zero erasure epochs and zero deadline misses.
The services reported zero socket errors, service errors, and queue drops.
The event did not show a capacity collapse.

| Edge | UDP P50 range | UDP P99 range |
| --- | ---: | ---: |
| Tokyo | 137.279-138.600 ms | 184.654-186.796 ms |
| Azure Australia | 185.383-186.604 ms | 230.584-232.631 ms |
| Sydney | 197.246-198.567 ms | 244.837-247.089 ms |
| Azure Japan | 235.797-237.062 ms | 281.725-283.864 ms |
| London | 246.005-247.352 ms | 293.785-295.921 ms |

For the UDP lane alone, this is acceptable loss.
UDP with FEC gives bounded latency but cannot give absolute delivery.
More mesh-to-edge recovery symbols could reduce the remaining loss probability.
They would add bandwidth.
Waiting for more symbols can also add recovery delay.
No number of FEC symbols can guarantee UDP delivery.

HTTP/3 provides reliable delivery for each successful LL-HLS response.
This run did not test an acknowledged repair handoff into the realtime render path.
That path must fill an omission before final rendering.
The current test did not prove that final rendered PCM had zero missing samples.

## FLAC LL-HLS

The FLAC probes expected 96,000 parts and received 95,986 parts.
Thirty-one of 40 edge-track lanes were complete.
Nine lanes missed 14 parts in the final one-second test window.

The probes recorded 87 deadline misses during the first three seconds.
Complete lanes each reported two source-start discontinuity markers.
The 31 complete lanes reported 62 markers in total.

All 40 FLAC initialization sections identified the FLAC codec.
The current probe did not compare each FLAC packet with a source hash.
Therefore, this run does not prove byte-exact FLAC reconstruction.

| Edge | Complete lanes | Missing parts | Startup deadline misses | FLAC P50 range | FLAC P99 range |
| --- | ---: | ---: | ---: | ---: | ---: |
| Tokyo | 6/8 | 3 | 17 | 392.381-422.071 ms | 433.740-469.819 ms |
| Azure Australia | 4/8 | 7 | 33 | 440.776-469.769 ms | 481.140-524.841 ms |
| Sydney | 8/8 | 0 | 0 | 452.772-482.262 ms | 494.337-531.712 ms |
| Azure Japan | 6/8 | 3 | 17 | 491.948-520.930 ms | 533.231-569.623 ms |
| London | 7/8 | 1 | 20 | 501.308-530.948 ms | 543.052-576.113 ms |

![UDP and FLAC LL-HLS latency](../performance/charts/2026-07-28-global-flac-latency.svg)

The chart uses ten-second medians.
Each input point is a one-second P99 value for one track.

## Opus companion result

The Opus probes expected 96,000 parts and received 95,987 parts.
Twenty-seven of 40 edge-track lanes were complete.
The probes recorded 13 missing final parts and 117 startup deadline misses.

All validated Opus media packets matched their expected packet structure.
The probe reported zero Opus packet mismatches.
The Opus result did not pass its strict gate.

## Player observation

The London player produced good FLAC audio during the run.
Playback stopped when the 600-second source ended at 11:50:55 UTC.
This observation did not qualify the player.

The player needs a clear end state and a retained timeline.
These requirements are in the current TODO.

## Gate result

| Gate | Result | Evidence |
| --- | --- | --- |
| Source capacity | PASS | All 15 gates passed |
| Contributor | PASS | All groups completed; no queue or socket drops |
| UDP strict zero loss | FAIL | Four 5 ms edge-track epochs were missing |
| FLAC LL-HLS complete delivery | FAIL | Fourteen final-window parts were missing |
| Opus LL-HLS complete delivery | FAIL | Thirteen final-window parts were missing |
| Multitrack sample alignment | FAIL | The probe did not record per-epoch PTS values |
| Overall strict qualification | FAIL | One or more required gates failed |

## Interpretation

This result supports a demonstrated ten-node, two-cloud global mesh.
It also supports the measured latency, FEC recovery, and source capacity results.

This result does not support a production service level agreement.
It does not support a zero-loss rendered-audio claim.
It does not qualify endurance, video performance, or player behavior.

The result is suitable for technical diligence if these limits remain visible.
It is not suitable as evidence that all production gates passed.

## Cleanup and evidence

The test process exited with the expected strict failure status.
Cleanup completed successfully.
The system preserved the remote result directories.

The [machine-readable evidence](evidence/20260728T113000Z-linode16-8track-recovery20-combined.json) contains the sanitized totals.
The local raw directory contains the probe output, metrics, logs, topology, and time-series data.

See the current [TODO](../../TODO.md) for the required recovery, probe, player, operations, and video work.
