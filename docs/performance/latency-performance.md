# Latency performance

This page owns the detailed geographic latency tables and physical-path
comparison. Use the canonical
[current performance state and gaps](current-state-and-gaps.md) for the latest
cross-layer capacity interpretation, current bottleneck, supported claims, and
ordered next work. The
[isolated LL-HLS distribution-capacity qualification](distribution-capacity-isolation.md)
defines the boundary methodology.

Latest runs:

- H3 isolation and first fixed capacity bottleneck: [18 July 2026 record](../real-world-tests/2026-07-18-h3-capacity-isolation.md)
- Eight-track SoundKit Opus GCP DAG capacity: [18 July 2026 record](../real-world-tests/2026-07-18-opus-h3-capacity.md)
- Raw PCM GCP DAG and H3 edge capacity: [`20260717T222106Z`](../real-world-tests/evidence/20260717T222106Z-pcm-h3-capacity.json)
- Raw PCM run note: [`2026-07-17 PCM H3 capacity`](../real-world-tests/2026-07-17-pcm-h3-capacity.md)
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

Latest clean 48 kHz 16-channel S24 PCM result, published from London through
the dual-parent GCP DAG and consumed from regional playback caches over
persistent H3:

| City | UDP p50/p99 | WebTransport p50/p99 | LL-HLS p50/p99 | LL-HLS p50 premium | Cache→client p50/p99 |
| --- | ---: | ---: | ---: | ---: | ---: |
| New York | 53.338 / 55.853 ms | 53.595 / 57.021 ms | 55.728 / 57.137 ms | 2.390 ms | 0.387 / 1.510 ms |
| Tokyo | 125.054 / 127.841 ms | 125.130 / 128.388 ms | 127.506 / 128.824 ms | 2.452 ms | 0.383 / 1.274 ms |
| Sydney | 146.129 / 150.046 ms | 146.268 / 150.252 ms | 148.549 / 150.265 ms | 2.420 ms | 0.371 / 1.460 ms |

The percentile-to-percentile p99 difference was 0.220–1.284 ms. Percentiles
are not paired samples, so the p50 lane premium is the clearer architectural
comparison. The post-deploy two-rendition New York canary independently
measured 1.032–1.374 ms cache-to-H3 p99.

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

- [`20260717T222106Z-pcm-h3-capacity.json`](../real-world-tests/evidence/20260717T222106Z-pcm-h3-capacity.json)
- [`20260717T145432Z-linode-dag.json`](../real-world-tests/evidence/20260717T145432Z-linode-dag.json)
- [`local-20260717T162832Z-multichannel-llhls-sizing.json`](../real-world-tests/evidence/local-20260717T162832Z-multichannel-llhls-sizing.json)
- [`local-20260717T053347Z-lossless.json`](../real-world-tests/evidence/local-20260717T053347Z-lossless.json)
- [`20260717T054206Z.json`](../real-world-tests/evidence/20260717T054206Z.json)
- [`local-20260716T001959Z.json`](../real-world-tests/evidence/local-20260716T001959Z.json)
- [`20260716T023139Z.json`](../real-world-tests/evidence/20260716T023139Z.json)
- [`evidence index`](../real-world-tests/evidence/README.md)
