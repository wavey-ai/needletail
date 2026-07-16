# 2026-07-16 relay latency qualification

This record covers the local and GCP qualification runs after adding measured
relay application-processing latency and correcting publication-to-available
timing to observe verified cache availability after the cache commit completes.

## Tested revisions

- Needletail: `6bb2fc8e65ffae31cdf23daea6a6a5fd2d4ffa20` plus working-tree
  changes for Mission Control, Prometheus/Grafana rules, qualification scripts,
  and evidence handling.
- av-mesh: `20512dcf61208d5a5361951d8a06375e2925e7e2` plus working-tree
  changes for post-commit availability timing and relay processing histograms.
- av-contrib: `9aa15eaeb4d6602f35e750f1e91318201ce355b6`.

## Local run `20260716T001959Z`

Raw artifacts:
`target/realtime-qualification/20260716T001959Z`.

Versioned evidence:
`docs/real-world-tests/evidence/local-20260716T001959Z.json`.

Topology:

- Contributor to primary relay: 10 ms delay, 2 ms jitter, 1% loss.
- Contributor to secondary relay: 10 ms delay, 2 ms jitter, no loss.
- Primary relay to playback edge: 35 ms delay, 5 ms jitter, 1% loss.
- Secondary relay to playback edge: 35 ms delay, 5 ms jitter, no loss.
- 30 seconds per benchmark endpoint, `h2load`, 8 connections, 4 streams per
  connection.

Results:

| Gate | Result | Budget |
| --- | ---: | ---: |
| Baseline contributor ingest p95 | 7.808 ms | <= 15 ms |
| Impaired contributor ingest p95 | 10.337 ms | <= 15 ms |
| Baseline playlist p95 | 1.287 ms | <= 5 ms |
| Impaired playlist p95 | 1.031 ms | <= 5 ms |
| Relay processing p95 | 1000 us | <= 1000 us |
| Publication-to-cache p99 | 150 ms | <= 500 ms |
| Failover detection | 106.234 ms | diagnostic |
| Promotion to source | 31.851 ms | <= 250 ms |
| Maximum media gap | 142.776 ms | <= 250 ms |

RaptorQ and failover:

- Exact RaptorQ recovery: 1,120 objects and 1,941 source symbols.
- Repair-assisted decodes: 1,120 objects.
- Primary forwarded source datagrams: 12,003.
- Secondary forwarded repair datagrams: 2,480.
- Automatic failover sequence: healthy -> promoted -> healthy.
- Warm source replay during promotion: 50 datagrams.
- Expired objects, rejected datagrams, deadline drops, and forward errors: 0.

## GCP run `20260716T002843Z`

Raw artifacts:
`target/gcp-qualification/runs/20260716T002843Z`.

Versioned evidence:
`docs/real-world-tests/evidence/20260716T002843Z.json`.

Topology:

- Contributor: London, `europe-west2-b`.
- Primary relay: Amsterdam, `europe-west4-a`.
- Secondary relay: Osaka, `asia-northeast2-b`.
- Playback edge: Tokyo, `asia-northeast1-b`.
- Machine class: four `e2-standard-2` instances.
- Carrier: controlled private UDP with primary source and warm-secondary repair.

Measured routes:

| Path | Measured RTT | Stretch limit | Observed stretch |
| --- | ---: | ---: | ---: |
| Direct contributor to edge | 359.292 ms | - | 1.000x |
| Contributor -> Amsterdam -> Tokyo | 257.431 ms | <= 1.15x | 0.716x |
| Contributor -> Osaka -> Tokyo | 252.239 ms | <= 1.15x | 0.702x |

Results:

| Gate | Result | Budget |
| --- | ---: | ---: |
| Contributor restart max relay activation | 1.745706 s | <= 10 s |
| Failover detection | 110.419 ms | <= 250 ms |
| Promotion to source | 9.936 ms | <= 250 ms |
| Maximum media gap | 122.738 ms | <= 250 ms |
| Relay processing p95 | 500 us | <= 1000 us |
| Publication-to-cache p99 | 150 ms | <= 500 ms |
| Canonical publication max lag after recovery | 3 objects | <= 4 objects |

RaptorQ and failover:

- Controlled primary-path loss dropped 66 datagrams.
- Exact RaptorQ recovery: 52 objects and 58 source symbols.
- Repair-assisted decodes: 52 objects.
- Automatic failover sequence: healthy -> promoted -> healthy.
- Warm source replay during promotion: 38 datagrams.
- Expired objects, rejected datagrams, and deadline drops: 0.

Cleanup audit:

- All four GCP instances remained running after the run.
- Primary relay service active: true.
- Contributor and media services active: true.
- Qualification packet-filter chain absent after cleanup: true.
- Edge alerts: 0.
- Final stream gap count: 0.
- Final audit maximum live lag: 5 objects. The qualification recovery gate
  measured 3 objects; the audit was taken later while the live head continued to
  advance.

## Follow-up

- Keep relay processing p95 as a hard qualification gate for local and GCP runs.
- Continue recording publication-to-cache p99, using the corrected post-commit
  availability timing.
- Preserve RaptorQ as the recovery mechanism; QUIC remains a carrier option, not
  a replacement for FEC.
