# Real-world test records

Needletail keeps durable notes for every test that reaches deployed hosts,
provider networks, or public Internet paths. Full raw captures are written
locally under `target/`. Because `target/` is not
versioned, each completed or failed run also receives a sanitized,
machine-readable summary under [`evidence/`](evidence/README.md). The dated
narrative captures the context needed to reproduce and interpret it without
storing credentials, private keys, access tokens, or host secrets.

For the current interpretation across all dated runs, start with
[Current performance state and gaps](../performance/current-state-and-gaps.md).
This index records evidence; the overview separates cache, router, H3,
live-tail, latency, replication, and endurance claims so historical results are
not compared as if they used the same workload.

Each dated record includes:

- the product and component revisions, including any tested working-tree patch;
- provider, regions, roles, host count, machine class, and test duration;
- delivery topology, carrier, RaptorQ policy, bitrate, and fault profile;
- measured direct and selected-route RTT/path stretch;
- contributor restart, source-epoch, publication, failover, and recovery results;
- exact RaptorQ-recovered objects and source symbols, plus repair-assisted decode attribution;
- warm-secondary source-buffer replay, expiry, retirement, and eviction counters;
- LL-HLS part cadence and relevant p50/p95/p99 latency observations;
- every failed check or tooling defect, its diagnosis, and the corrective rerun;
- final service, packet-filter, and test-resource cleanup state.

Add results to the matching dated record during the run. Create a new dated
record when the topology, provider, or UTC calendar day encoded in the run id
changes.

Before declaring a run recorded, verify all three layers exist:

1. raw API, metrics, and result files in the local run directory;
2. a versioned JSON summary in `docs/real-world-tests/evidence/`;
3. a narrative entry explaining checks, anomalies, diagnosis, and cleanup.

Validate the versioned evidence, index, narrative coverage, cleanup assertions,
and absence of secret-shaped fields with:

```sh
./scripts/validate-real-world-evidence.sh
```

Current response-duration record: [2026-07-21 matched Opus H3 response duration](2026-07-21-opus-h3-response-duration.md).
Authoritative edge-cache record: [2026-07-22 GCP edge capacity and HLS failover](2026-07-22-gcp-edge-capacity-failover.md).
Current bundle-capacity record: [2026-07-20 persistent H3 Opus bundle stream](2026-07-20-opus-h3-persistent-bundle-stream.md).
Current video record: [2026-07-20 H.264 to fMP4 LL-HLS at 4K and 8K](2026-07-20-h264-fmp4-llhls-4k-8k.md).
Previous tail record: [2026-07-20 clock-qualified Opus H3 tail repeatability](2026-07-20-opus-h3-clock-qualified-tail.md).
Previous bundle record: [2026-07-19 bundled eight-track Opus H3 tails](2026-07-19-opus-h3-tail-bundle.md).
Previous aggregation record: [2026-07-18 200 ms Opus H3 response aggregation](2026-07-18-opus-h3-200ms-aggregation.md).
Current 5 ms Opus record: [2026-07-18 eight-track Opus LL-HLS capacity](2026-07-18-opus-h3-capacity.md).
Current raw PCM record: [2026-07-17 raw PCM DAG latency and H3 edge capacity](2026-07-17-pcm-h3-capacity.md).
Current FLAC DAG record: [2026-07-17 Linode multi-region DAG replication](2026-07-17-linode-dag-replication.md).
Current local/GCP cadence record: [2026-07-17 lossless H3 latency](2026-07-17-lossless-h3.md).
Current local multichannel sizing record: [2026-07-17 multichannel LL-HLS sizing](2026-07-17-multichannel-llhls-sizing.md).
Previous relay-latency record: [2026-07-16 relay latency](2026-07-16-relay-latency.md).
Previous GCP intercontinental record: [2026-07-15 GCP intercontinental test](2026-07-15-gcp-intercontinental.md).
Previous local controlled-impairment record: [2026-07-15 realtime test](2026-07-15-local-realtime.md).
