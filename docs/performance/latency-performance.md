# Latency performance

Latest runs:

- Local controlled-loss run: [`local-20260716T001959Z`](../real-world-tests/evidence/local-20260716T001959Z.json)
- GCP intercontinental run: [`20260716T023139Z`](../real-world-tests/evidence/20260716T023139Z.json)
- GCP dashboard screenshots: [`20260716T023139Z`](real-world-load-screenshots.md)
- Full run note: [`2026-07-16 relay latency`](../real-world-tests/2026-07-16-relay-latency.md)

## Charts

![Relay latency](charts/relay-latency.svg)

![Failover latency](charts/failover-latency.svg)

![GCP route RTT](charts/route-rtt.svg)

![RaptorQ recovery](charts/raptorq-recovery.svg)

## Numbers

| Metric | Local | GCP | Target |
| --- | ---: | ---: | ---: |
| Relay processing p95 | 1.0 ms | 1.0 ms | ≤ 1.0 ms |
| Publication-to-cache p99 | 150 ms | 150 ms | ≤ 500 ms |
| Failover detection | 106.234 ms | 106.940 ms | ≤ 250 ms |
| Failover activation | 31.851 ms | 10.703 ms | ≤ 250 ms |
| Media gap during failover | 142.776 ms | 120.034 ms | ≤ 250 ms |
| RaptorQ recovered objects | 1,120 | 381 | 0 errors |
| RaptorQ recovered source symbols | 1,941 | 1,195 | 0 errors |

## Speed-of-light factor

The intercontinental RTT numbers are not sub-millisecond. The sub-millisecond
numbers are relay processing time inside the service.

| Path | Distance | Measured RTT | Vacuum lower bound | Ideal-fiber lower bound | Factor vs vacuum | Factor vs ideal fiber |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| London -> Tokyo direct | 9,558.6 km | 250.915 ms | 63.8 ms | 93.7 ms | 3.93x | 2.68x |
| London -> Amsterdam -> Tokyo | 9,645.9 km | 244.127 ms | 64.4 ms | 94.6 ms | 3.79x | 2.58x |
| London -> Osaka -> Tokyo | 9,891.8 km | 251.904 ms | 66.0 ms | 97.0 ms | 3.82x | 2.60x |

One-way observed latency is roughly 122-126 ms. An ideal straight fiber path is
roughly 47-49 ms one-way, so the extra 75-79 ms is provider routing, cloud
network path, queueing, and host overhead.

## Evidence

- [`local-20260716T001959Z.json`](../real-world-tests/evidence/local-20260716T001959Z.json)
- [`20260716T023139Z.json`](../real-world-tests/evidence/20260716T023139Z.json)
- [`evidence index`](../real-world-tests/evidence/README.md)
