# GCP PCM/H3 endurance ladder

`scripts/gcp-pcm-h3-endurance.sh` keeps one 16-channel, 48 kHz S24 PCM
publication live across the six-node GCP DAG. Each logical customer reads both
eight-channel LL-HLS renditions over persistent TLS 1.3/H3 connections.

This page documents the PCM endurance harness, not the current edge-capacity
claim. The later real eight-track Opus workload has different request geometry.
Use [Current performance state and gaps](current-state-and-gaps.md) for the
current result and the remaining endurance gate.

Before contacting either cloud, the launcher copies itself to
`RESULT_DIR/harness.sh`, removes write permission, and re-executes that retained
copy. A workspace edit during a long run therefore cannot change the Bash still
waiting to collect its final reports. The launcher refuses to overwrite an
existing run's harness snapshot.

## Thirty-minute baseline

Use five-minute observations to fit five sustained load steps into a 30-minute
run:

```sh
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json \
NEEDLETAIL_LOAD_HOST=203.0.113.10 \
ENDURANCE_DURATION_SECONDS=1800 \
ENDURANCE_OBSERVATION_INTERVAL_SECONDS=300 \
ENDURANCE_SUSTAINED_READER_STEPS=1,4,8,16,24 \
  scripts/gcp-pcm-h3-endurance.sh
```

The values are cumulative targets for **added** customers, not cohort sizes.
With the default one-customer baseline, the example behaves as follows:

| Step | New cohort | Added customers active | Total customers | H3 connections |
| ---: | ---: | ---: | ---: | ---: |
| Baseline | 0 | 0 | 1 | 2 |
| 1 | 1 | 1 | 2 | 4 |
| 2 | 3 | 4 | 5 | 10 |
| 3 | 4 | 8 | 9 | 18 |
| 4 | 8 | 16 | 17 | 34 |
| 5 | 8 | 24 | 25 | 50 |

Every cohort remains connected from its actual, part-aligned start offset until
the publication ends. This makes the load genuinely sustained while avoiding
duplicate readers: the script launches only `target - previous_target` readers
at each step. The load probe keeps bounded latency samples, so report memory is
bounded by active readers rather than publication duration. The script accepts
at most 64 strictly increasing targets and at most 4,096 total active customers.

## Qualification gates

The run passes only when all of the following are true:

- the 16-channel PCM source completes every expected 5 ms epoch;
- both baseline renditions deliver every part to every baseline customer with
  no gaps, deadline misses, PCM-size mismatch, or process failure;
- every added cohort delivers its entire remaining publication window exactly
  on both renditions with verified `ipcm_s24le`, LL-HLS, TLS 1.3, and persistent
  H3 connections;
- every configured target launches, every reader process stays alive until its
  expected completion, and no service exits early; and
- kernel `Udp.RcvbufErrors` does not increase on the contributor, either relay,
  or any of the Tokyo, New York, and Sydney edges for the whole run. Each cohort
  also retains its own New York edge delta for viewer-load attribution.

`result.json` uses `needletail.gcp-pcm-h3-endurance.v2`. It separates
`baseline_continuous_renditions` from `sustained_reader_steps`. Each step records
the cohort size, cumulative added-reader target, total active customers, actual
start and end offsets, expected parts per reader, both complete rendition
reports, and its kernel UDP-drop delta. The step reports are also collected in
`reader-steps.json`; raw reports remain under `reader-steps/NN/`.
`kernel_udp_receive_drops.roles` records start, end, delta, and pass state for
all six DAG roles. The raw counter maps are retained in
`udp-rcvbuf-errors-start.json` and `udp-rcvbuf-errors-end.json`.

Final evidence collection is best-effort and uses bounded SSH liveness timers.
An early source or reader exit still produces `result.json`, available metrics,
journals, stderr, and any complete reports. Missing reports become `{}` and
fail qualification; a non-empty truncated report is also preserved with an
`.invalid` or `.partial` suffix before the normalized placeholder is written.

For compatibility, `ENDURANCE_BURST_READER_STEPS` remains an alias when the new
environment variable is unset. `run.json` retains `readers` and
`burst_reader_steps`; `result.json` retains `continuous_renditions` and
`capacity_bursts` and the former NYC-only `edge_kernel_udp_receive_drops`;
`bursts.json` mirrors `reader-steps.json`. These aliases contain sustained-step
data, so new evidence consumers should use the v2 names.

The exit trap terminates the contributor source, baseline readers, and every
nested reader-step process after either success or failure.
