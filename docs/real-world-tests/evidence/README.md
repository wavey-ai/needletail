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
