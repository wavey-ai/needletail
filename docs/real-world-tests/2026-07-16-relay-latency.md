# 2026-07-16 relay latency

This record covers the local and GCP runs after adding measured
relay application-processing latency and correcting publication-to-available
timing to observe verified cache availability after the cache commit completes.

## Tested revisions

- Needletail: `6bb2fc8e65ffae31cdf23daea6a6a5fd2d4ffa20` plus working-tree
  changes for Mission Control, Prometheus/Grafana rules, test scripts,
  and evidence handling.
- av-mesh: `20512dcf61208d5a5361951d8a06375e2925e7e2` plus working-tree
  changes for post-commit availability timing and relay processing histograms.
- av-contrib: `9aa15eaeb4d6602f35e750f1e91318201ce355b6`.

## Local run `20260716T001959Z`

Raw artifacts are in the local `target/` run directory.

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

| Check | Result | Budget |
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

Raw artifacts are in the local `target/` run directory.

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

| Check | Result | Budget |
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
- Test packet-filter chain absent after cleanup: true.
- Edge alerts: 0.
- Final stream gap count: 0.
- Final audit maximum live lag: 5 objects. The recovery check measured 3
  objects; the audit was taken later while the live head continued to advance.

## GCP run `20260716T023139Z`

Raw artifacts:
`target/gcp-qualification/runs/20260716T023139Z`.

Versioned evidence:
`docs/real-world-tests/evidence/20260716T023139Z.json`.

Dashboard screenshots:
`docs/performance/real-world-load-screenshots.md`.

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
| Direct contributor to edge | 250.915 ms | - | 1.000x |
| Contributor -> Amsterdam -> Tokyo | 244.127 ms | <= 1.15x | 0.972947x |
| Contributor -> Osaka -> Tokyo | 251.904 ms | <= 1.15x | 1.003942x |

Physics factor:

| Path | Distance | Vacuum RTT lower bound | Ideal-fiber RTT lower bound | Observed factor |
| --- | ---: | ---: | ---: | ---: |
| London -> Tokyo direct | 9,558.6 km | 63.8 ms | 93.7 ms | 3.93x vacuum / 2.68x ideal fiber |
| London -> Amsterdam -> Tokyo | 9,645.9 km | 64.4 ms | 94.6 ms | 3.79x vacuum / 2.58x ideal fiber |
| London -> Osaka -> Tokyo | 9,891.8 km | 66.0 ms | 97.0 ms | 3.82x vacuum / 2.60x ideal fiber |

Results:

| Check | Result | Budget |
| --- | ---: | ---: |
| Contributor restart max relay activation | 1.628260 s | <= 10 s |
| Failover detection | 106.940 ms | <= 250 ms |
| Promotion to source | 10.703 ms | <= 250 ms |
| Maximum media gap | 120.034 ms | <= 250 ms |
| Relay processing p95 | 1000 us | <= 1000 us |
| Publication-to-cache p99 | 150 ms | <= 500 ms |
| Canonical publication max lag after recovery | 4 objects | <= 4 objects |

RaptorQ and failover:

- Controlled primary-path loss dropped 70 datagrams.
- Exact RaptorQ recovery: 381 objects and 1,195 source symbols.
- Repair-assisted decodes: 381 objects.
- Automatic failover sequence: healthy -> promoted -> healthy.
- Warm source replay during promotion: 38 datagrams.
- Expired objects, rejected datagrams, and deadline drops during the gated
  fault phases: 0.

Dashboard load:

- `h2load` ran for 60 seconds through the local SSH tunnel to the Tokyo edge.
- Requests: 6,520 total, 6,520 succeeded, 0 failed, 0 errored, 0 timed out.
- Throughput: 108.67 requests/s.
- Mean request time: 290.71 ms.
- Max request time: 976.55 ms.

Post-load observation:

- Final API snapshot: 0 alerts, edge contiguous at head, rejected datagrams 0,
  deadline drops 0.
- Cumulative dashboard counters after the gate and dashboard load: 6 expired
  objects and maximum historical failover gap 2.618526 s. These were not the
  gated failover values; they are retained as follow-up operating data.

Cleanup audit:

- Primary relay service active before teardown: true.
- Contributor and media services active before teardown: true.
- Test packet-filter chain absent before teardown: true.
- GCP lab teardown was run after documentation capture.

## Follow-up

- Keep relay processing p95 as a hard release check for local and GCP runs.
- Continue recording publication-to-cache p99, using the corrected post-commit
  availability timing.
- Preserve RaptorQ as the recovery mechanism; QUIC remains a carrier option, not
  a replacement for FEC.
- Investigate the post-dashboard-load cumulative expired-object counter and
  historical maximum failover gap separately from the gated fault pass.
