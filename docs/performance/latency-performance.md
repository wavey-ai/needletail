# Latency performance

Latest runs:

- Linode six-node DAG: [`20260717T145432Z`](../real-world-tests/evidence/20260717T145432Z-linode-dag.json)
- Linode run note: [`2026-07-17 multi-region DAG`](../real-world-tests/2026-07-17-linode-dag-replication.md)
- Local multichannel LL-HLS sizing: [`local-20260717T162832Z`](../real-world-tests/evidence/local-20260717T162832Z-multichannel-llhls-sizing.json)
- Local persistent-H3 5 ms lossless run: [`local-20260717T053347Z`](../real-world-tests/evidence/local-20260717T053347Z-lossless.json)
- GCP 5 ms lossless run: [`20260717T054206Z`](../real-world-tests/evidence/20260717T054206Z.json)
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

Latest clean 48 kHz FLAC result, published from London through the dual-parent
DAG and consumed from the local playback cache over persistent H3:

| City | UDP p50/p99 | WebTransport p50/p99 | LL-HLS p50/p99 | LL-HLS p50 premium | Cache→client p50/p99 |
| --- | ---: | ---: | ---: | ---: | ---: |
| New York | 40.258 / 41.180 ms | 40.333 / 41.307 ms | 43.360 / 63.320 ms | 3.103 ms | 0.222 / 0.654 ms |
| Tokyo | 115.673 / 116.584 ms | 115.788 / 116.787 ms | 118.799 / 138.740 ms | 3.125 ms | 0.220 / 0.674 ms |
| Sydney | 129.821 / 131.853 ms | 129.945 / 132.252 ms | 132.887 / 153.049 ms | 3.067 ms | 0.227 / 0.785 ms |

The clean p99 LL-HLS premium was 21.196–22.156 ms. The “UDP plus a few
milliseconds” summary refers to the measured median, not the tail.

Earlier relay/failover gates:

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

The later Linode DAG measured primary-route RTTs of 82.179 ms to New York,
228.613 ms to Tokyo, and 256.948 ms to Sydney. Half-RTT propagation proxies
were 41.090, 114.307, and 128.474 ms. Median publication-to-cache latency was
43.114, 118.501, and 132.634 ms, leaving an architecture-plus-clock residual of
2.025–4.195 ms. This residual is measured from node clocks and is not a
physical speed-of-light bound.

## Evidence

- [`20260717T145432Z-linode-dag.json`](../real-world-tests/evidence/20260717T145432Z-linode-dag.json)
- [`local-20260717T162832Z-multichannel-llhls-sizing.json`](../real-world-tests/evidence/local-20260717T162832Z-multichannel-llhls-sizing.json)
- [`local-20260717T053347Z-lossless.json`](../real-world-tests/evidence/local-20260717T053347Z-lossless.json)
- [`20260717T054206Z.json`](../real-world-tests/evidence/20260717T054206Z.json)
- [`local-20260716T001959Z.json`](../real-world-tests/evidence/local-20260716T001959Z.json)
- [`20260716T023139Z.json`](../real-world-tests/evidence/20260716T023139Z.json)
- [`evidence index`](../real-world-tests/evidence/README.md)
