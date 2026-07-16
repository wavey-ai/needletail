# Latency performance

Latest runs:

- Local controlled-loss run: [`local-20260716T001959Z`](../real-world-tests/evidence/local-20260716T001959Z.json)
- GCP intercontinental run: [`20260716T002843Z`](../real-world-tests/evidence/20260716T002843Z.json)
- Full run note: [`2026-07-16 relay latency`](../real-world-tests/2026-07-16-relay-latency.md)

## Charts

![Relay latency](charts/relay-latency.svg)

![Failover latency](charts/failover-latency.svg)

![GCP route RTT](charts/route-rtt.svg)

![RaptorQ recovery](charts/raptorq-recovery.svg)

## Numbers

| Metric | Local | GCP | Target |
| --- | ---: | ---: | ---: |
| Relay processing p95 | 1.0 ms | 0.5 ms | ≤ 1.0 ms |
| Publication-to-cache p99 | 150 ms | 150 ms | ≤ 500 ms |
| Failover detection | 106.234 ms | 110.419 ms | ≤ 250 ms |
| Failover activation | 31.851 ms | 9.936 ms | ≤ 250 ms |
| Media gap during failover | 142.776 ms | 122.738 ms | ≤ 250 ms |
| RaptorQ recovered objects | 1,120 | 52 | 0 errors |
| RaptorQ recovered source symbols | 1,941 | 58 | 0 errors |

## Evidence

- [`local-20260716T001959Z.json`](../real-world-tests/evidence/local-20260716T001959Z.json)
- [`20260716T002843Z.json`](../real-world-tests/evidence/20260716T002843Z.json)
- [`evidence index`](../real-world-tests/evidence/README.md)
