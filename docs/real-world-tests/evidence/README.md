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
- `20260717T222106Z-pcm-h3-capacity.json`: raw 16-channel S24 PCM through the six-node GCP DAG, strict two-vCPU H3 edge capacity ladder, and post-deploy PCM readiness canary.
- `20260718T163240Z-opus-h3-capacity.json`: real eight-stem DAW Nexus pure-Rust Opus through the same-zone two-parent GCP DAG, strict p99 capacity, hard request-throughput boundary, and clock/churn diagnostics.
- `20260718T221533Z-opus-h3-200ms-aggregation.json`: service-configured 200 ms H3 responses over exact 5 ms Opus units, one multiplexed connection/customer, complete-delivery and latency knees, source stability, near-one-core edge serialization, and cancellation diagnostics.
- `20260719T185313Z-opus-h3-tail-bundle.json`: private-GCP generation-safe cache range reads and synchronized eight-track H3 bundles, a three-repeat 24-customer latency/headroom candidate, first 28-customer latency-gate miss, first approximate 32-customer CPU-headroom miss, and explicit endurance limits.
- `20260719T225507Z-opus-h3-tail-profile.json`: matched 60-second private-GCP profiling of indexed canonical live slots, accelerated IEEE CRC-32, and zero-consumer AEP1 discard; exact media remained complete while 33 bundle responses retained the strict 20 ms gate as open work.
- `20260719T231836Z-opus-h3-tail-profile.json`: matched private-GCP follow-up replacing joined per-track waits with sequential exact reads under one shared deadline; edge host CPU fell to 34.765%, all 2,304,000 parts remained exact, and 9 bundle responses retained the strict 20 ms gate as open work.
- `20260720T001022Z-opus-h3-canonical-envelope-profile.json`: private-GCP exact-envelope handoff profile with an adjacent v11 control; the final v12 run used 3.873% less host CPU and had no late bundles, but another v12 attempt had 6,144 late bundles, so strict repeatability remains failed.
- `20260720T021843Z-opus-h3-clock-qualified-tail.json`: clock-gated private-GCP diagnostics and corrected-probe A/B; the accepted v12 exact-envelope build repeated 2,304,000 exact parts with zero late bundle twice, while the shared group-waiter candidate was rejected.
