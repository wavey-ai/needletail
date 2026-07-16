# Real-world qualification records

Needletail keeps durable notes for every test that reaches deployed hosts,
provider networks, or public Internet paths. Full raw captures are written
locally under `target/gcp-qualification/runs/`. Because `target/` is not
versioned, each completed or failed run also receives a sanitized,
machine-readable summary under [`evidence/`](evidence/README.md). The dated
narrative captures the context needed to reproduce and interpret it without
storing credentials, private keys, access tokens, or host secrets.

Each dated record includes:

- the product and component revisions, including any tested working-tree patch;
- provider, regions, roles, host count, machine class, and test duration;
- delivery topology, carrier, RaptorQ policy, bitrate, and fault profile;
- measured direct and selected-route RTT/path stretch;
- contributor restart, source-epoch, publication, failover, and recovery results;
- exact RaptorQ-recovered objects and source symbols, plus repair-assisted decode attribution;
- warm-secondary source-buffer replay, expiry, retirement, and eviction counters;
- LL-HLS part cadence and relevant p50/p95/p99 latency observations;
- every failed gate or tooling defect, its diagnosis, and the corrective rerun;
- final service, packet-filter, and test-resource cleanup state.

Add results to the matching dated record during the run. Create a new dated
record when the topology, provider, or UTC calendar day encoded in the run id
changes.

Before declaring a run recorded, verify all three layers exist:

1. raw API, metrics, and result files in the local run directory;
2. a versioned JSON summary in `docs/real-world-tests/evidence/`;
3. a narrative entry explaining gates, anomalies, diagnosis, and cleanup.

Validate the versioned evidence, index, narrative coverage, cleanup assertions,
and absence of secret-shaped fields with:

```sh
./scripts/validate-real-world-evidence.sh
```

Current record: [2026-07-16 relay latency qualification](2026-07-16-relay-latency-qualification.md).
Previous GCP intercontinental record: [2026-07-15 GCP intercontinental qualification](2026-07-15-gcp-intercontinental.md).
Previous local controlled-impairment record: [2026-07-15 realtime qualification](2026-07-15-local-realtime.md).
