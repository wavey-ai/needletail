# Versioned test evidence

This directory contains sanitized machine-readable summaries of real provider
tests. One JSON file represents one run, including failed
runs. It records the raw local artifact directory so an operator with
the same workspace can inspect the full API snapshots and Prometheus captures.

These summaries deliberately contain no credential paths, private keys,
tokens, authorization headers, or secret material. A passing result is copied
from the run result JSON. When a run exits before producing that
file, the summary is assembled from the immutable before/during/after captures
and names the exact failed check.

Evidence index:

- [2026-11-24-transport-comparison](2026-11-24-transport-comparison/README.md) contains charts, 972 per-case results, and provenance for the standalone RIST, SRT, and RaptorQ comparison.
- [20261127-integration-6-main.json](20261127-integration-6-main.json) records the integration 6 pins, Linode archive, regenerated Wave 12 results, and merge verification.
- [20261126-4k-wave12-audio-scoreboard.json](20261126-4k-wave12-audio-scoreboard.json) and [20261126-4k-wave12-audio-scoreboard-lkcloud.json](20261126-4k-wave12-audio-scoreboard-lkcloud.json) retain the four-region audio comparisons.
- [20261126-4k-wave12-video-w12-soak-scoreboard.json](20261126-4k-wave12-video-w12-soak-scoreboard.json) retains the 300-second 4K cell, including per-region viewer results.
- `20261121T184928Z-client-udp.json`, `20261121T194453Z-client-udp.json`, `20261121T194816Z-client-udp.json` and `20261121T195150Z-client-udp.json` retain the earlier Linode client UDP qualification and comparison runs. [Dated report](../2026-11-21-client-udp.md)
- `20261119T021701Z-linode-small-mesh.json`: 27-Nanode Linode series with regional distributors and private-VLAN LL-HLS edges: tuning sweeps, loss, relay failover, 200 concurrent readers, a 10-minute soak with a strict failure in Tokyo and Sydney, and ECMP source-port selection.
- `20261119T200101Z-regional-cache.json`: 15-instance Linode run of the regional LL-HLS cache features on Nanodes: demand replication, load balance with admission, failover, stall and drain without a missing part, reader assignment and pinning, for 4K video and lossless audio, with a cache-mesh symbol size comparison.
- `20261121T044850Z-edge-campaign.json`: 15-instance Linode run of the edge fault campaign: 40 scenarios of node failure, cache-network loss, clock skew, full disk, capacity of each role on a Nanode, admission, flash crowd, churn and hls.js playback, each with its question, criterion, checks and result. The result is `failed`: 24 pass, 3 are named gaps and 13 fail, and the record says why for each.
- `20260728T113000Z-linode16-8track-recovery20-combined.json`: current ten-node GCP and Azure multitrack FLAC run with UDP and LL-HLS latency, source capacity, one Tokyo recovery-unit loss, and strict boundary failures.
- `20260715T223437Z.json`: recovery failed before the controlled-loss phase;
- `20260715T225642Z.json`: complete passing restart, failover, recovery, and 2% loss run;
- `20260715T230001Z.json`: repeat met latency gates but failed the strict one-object expiry bound;
- `20260715T230319Z.json`: second complete passing run after the strict variance failure;
- `20260715T230643Z.json`: third complete passing run;
- `20260715-corrected-series-summary.json`: aggregate ranges across the four corrected-build repeats;
- `20260715T234426Z.json`: first zero-expiry pass with exact RaptorQ attribution and warm-source replay;
- `20260715T234654Z.json`: second consecutive zero-expiry exact-recovery pass;
- `20260715T234910Z.json`: third consecutive zero-expiry exact-recovery pass;
- `20260715-warm-source-replay-series-summary.json`: aggregate ranges and counter semantics across the three v3 runs;
- `local-20260715T235439Z.json`: local controlled-impairment load, failover, and exact-RaptorQ run;
- `local-20260716T001959Z.json`: local relay-processing and corrected publication-latency run;
- `20260716T002843Z.json`: GCP relay-processing and corrected publication-latency run;
- `20260716T023139Z.json`: GCP intercontinental failover, RaptorQ loss recovery, dashboard load, screenshots, and speed-of-light factor run;
- `local-20260717T053347Z-lossless.json`: final local 5 ms lossless UDP, WebTransport, and certificate-verified persistent-H3 LL-HLS run;
- `20260717T054206Z.json`: passing GCP 5 ms lossless clean/impaired three-lane qualification;
- `20260717T054847Z.json`: lossless phase passed, but the broader integrated invocation failed its pre-restart convergence gate;
- `20260717-lossless-latency-series-summary.json`: 50 ms, 20 ms, and 5 ms cadence results plus the diagnostic-attempt ledger;
- `20260717T145432Z-linode-dag.json`: complete six-node Linode clean/impaired three-lane DAG replication, exact cache identity, cache independence, failover, latency split, CPU, and idle-stream-retirement qualification;
- `local-20260717T162832Z-multichannel-llhls-sizing.json`: partial local 16/32/64/128-channel LL-HLS sizing and PCM-vs-FLAC-source bottleneck isolation.
- `local-20261121T0650Z-edge-campaign.json`: the edge fault campaign in the local namespace lab under Nanode limits: reader, cache-network and wide-area impairments, failures and stalls of every role, capacity, audience, LL-HLS rule checks, hls.js and ffmpeg playback, the ladder, and churn. The result is `failed`: the record lists each scenario that passed, each named gap and each failure.
- `20260717T222106Z-pcm-h3-capacity.json`: raw 16-channel S24 PCM through the six-node GCP DAG, strict two-vCPU H3 edge capacity ladder, and post-deploy PCM readiness canary.
- `20260718T163240Z-opus-h3-capacity.json`: real eight-stem DAW Nexus pure-Rust Opus through the same-zone two-parent GCP DAG, strict p99 capacity, hard request-throughput boundary, and clock/churn diagnostics.
- `20260718T221533Z-opus-h3-200ms-aggregation.json`: service-configured 200 ms H3 responses over exact 5 ms Opus units, one multiplexed connection/customer, complete-delivery and latency knees, source stability, near-one-core edge serialization, and cancellation diagnostics.
- `20260719T185313Z-opus-h3-tail-bundle.json`: private-GCP generation-safe cache range reads and synchronized eight-track H3 bundles, a three-repeat 24-customer latency/headroom candidate, first 28-customer latency-gate miss, first approximate 32-customer CPU-headroom miss, and explicit endurance limits.
- `20260719T225507Z-opus-h3-tail-profile.json`: matched 60-second private-GCP profiling of indexed canonical live slots, accelerated IEEE CRC-32, and zero-consumer AEP1 discard; exact media remained complete while 33 bundle responses retained the strict 20 ms gate as open work.
- `20260719T231836Z-opus-h3-tail-profile.json`: matched private-GCP follow-up replacing joined per-track waits with sequential exact reads under one shared deadline; edge host CPU fell to 34.765%, all 2,304,000 parts remained exact, and 9 bundle responses retained the strict 20 ms gate as open work.
- `20260720T001022Z-opus-h3-canonical-envelope-profile.json`: private-GCP exact-envelope handoff profile with an adjacent v11 control; the final v12 run used 3.873% less host CPU and had no late bundles, but another v12 attempt had 6,144 late bundles, so strict repeatability remains failed.
- `20260720T021843Z-opus-h3-clock-qualified-tail.json`: clock-gated private-GCP diagnostics and corrected-probe A/B; the accepted v12 exact-envelope build repeated 2,304,000 exact parts with zero late bundle twice, while the shared group-waiter candidate was rejected.
- `20260720T034744Z-h264-fmp4-llhls-4k-8k.json`: private-GCP realtime H.264/AAC ingest, contributor CMAF fMP4 packaging, dual-parent replication, LL-HLS playlist-duration correction, and strict 4K plus derived-8K decode.
- `20260720T045417Z-opus-h3-persistent-bundle-stream.json`: one persistent H3 bundle response per customer, a repeated strict 128 Opus tails/vCPU result, a provisional 256 tails/vCPU pass, and the measured 64-128 customer rare-tail boundary.
- `20260721T033000Z-opus-h3-response-ab.json`: matched 5 ms, 100 ms, and 200 ms H3 response durations with exact media, aligned CPU samples, and a 90-second repeat.
- `20260721T061329Z-h264-4k-viewer-capacity.json`: two 60-second 100-viewer 4K LL-HLS runs on one two-vCPU edge.
- `20260721T124900Z-h264-4k-loaded-player.json`: loaded HLS.js playback, timeline rewind, Operations captures, and 100-viewer load evidence.
- `20260722T001300Z-edge-capacity-failover.json`: authoritative GCP edge-cache admission, HLS failover, replication, and HTTP/3 evidence.
- `20261122-compare-audio.json`: the audio comparison on Needletail's `low-latency` build (wave 2: 2.5 ms Opus and 1 ms PCM parts, RaptorQ FEC UDP and LL-HLS client modes) against Cloudflare Realtime, Cloudflare Stream WHIP/WHEP, self-hosted LiveKit and LiveKit Cloud on a dedicated-CPU Linode lab: three sessions, the PCM part curve (0.5 and 0.25 ms) and a session with the SFUs' PCM in 1 ms blocks, mouth-to-ear latency, jitter, gaps and bit-exactness for every listener, impairment, crowds of 100, and the processor of every node.
- `20261122-compare-4k.json`: the 4K comparison: two throughput sweeps of every system (Needletail waves 1 and 2), every rung, region and viewer, the Chrome players and node processor; and the 5 Mbit/s latency, first-frame, loss, delay and crowd phases.
- `20261122-compare-mesh.json`: Needletail's mesh hop by hop from the node journals, with loss and delay on the long haul only, beside self-hosted LiveKit with the same on its source uploads.
- `20261122-compare-tiers.json`: the tier test: node processor and bytes out as the audience (1 to 1,000), the streams and the SFU's audience change.
- `20261121-compare-audio-wave1.json`: the same comparison on the earlier build (5 ms parts), three sessions; WebRTC Opus figures corrected to the live send rule.
- `20261121-compare-audio-nanode.json`: the first pass of the same comparison on shared Nanodes (LL-HLS only, `regional-llhls-cache` build); superseded, kept for the record.
