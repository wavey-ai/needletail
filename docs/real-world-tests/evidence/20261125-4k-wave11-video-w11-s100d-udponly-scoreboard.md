Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w11-s100d-udponly; 0 win, 0 tie, 0 loss, 16 not measured of 16 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 240 | none | not measured | none |
| New York | 240 | none | not measured | none |
| Tokyo | 240 | none | not measured | none |
| Sydney | 240 | none | not measured | none |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 240 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 34.9 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 86.1 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 163.2 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 180.1 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 240 Mbit/s |
| --- | ---: |
| lon-dist-a | 1537 → 1551 |
| lon-dist-b | 1546 → 1546 |
| lon-edge-1 | 851 → 1062 |
| lon-edge-2 | 822 → 1067 |
| new-dist-a | 1442 → 1456 |
| new-dist-b | 1469 → 1469 |
| new-edge-1 | 888 → 1022 |
| new-edge-2 | 862 → 1067 |
| syd-dist-a | 1486 → 1486 |
| syd-dist-b | 1495 → 1501 |
| syd-edge-1 | 818 → 1048 |
| syd-edge-2 | 904 → 1113 |
| tok-dist-a | 1513 → 1513 |
| tok-dist-b | 1475 → 1501 |
| tok-edge-1 | 820 → 976 |
| tok-edge-2 | 857 → 1047 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 240 Mbit/s |
| --- | ---: |
| contrib-lon | – |
| lon-dist-a | 322 (407 MB) |
| lon-dist-b | 323 (407 MB) |
| lon-edge-1 | 312 (405 MB) |
| lon-edge-2 | 312 (407 MB) |
| new-dist-a | 323 (407 MB) |
| new-dist-b | 323 (407 MB) |
| new-edge-1 | 253 (307 MB) |
| new-edge-2 | 312 (407 MB) |
| syd-dist-a | 323 (407 MB) |
| syd-dist-b | 323 (407 MB) |
| syd-edge-1 | 312 (407 MB) |
| syd-edge-2 | 313 (407 MB) |
| tok-dist-a | 323 (407 MB) |
| tok-dist-b | 323 (407 MB) |
| tok-edge-1 | 245 (344 MB) |
| tok-edge-2 | 283 (391 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 240 Mbit/s |
| --- | --- |
| lon-dist-a | 57% / 0 (0) / 0 |
| lon-dist-b | 69% / 0 (0) / 0 |
| lon-edge-1 | 61% / 0 (0) / 0 |
| lon-edge-2 | 54% / 0 (0) / 0 |
| new-dist-a | 92% / 0 (0) / 0 |
| new-dist-b | 80% / 0 (0) / 0 |
| new-edge-1 | 98% / 792069 (792069) / 0; tee_dropped 1.30574e+06 |
| new-edge-2 | 78% / 0 (0) / 0 |
| syd-dist-a | 69% / 0 (0) / 0 |
| syd-dist-b | 56% / 0 (0) / 0 |
| syd-edge-1 | 82% / 0 (0) / 0 |
| syd-edge-2 | 83% / 0 (0) / 0 |
| tok-dist-a | 75% / 0 (0) / 0 |
| tok-dist-b | 64% / 0 (0) / 0 |
| tok-edge-1 | 90% / 0 (0) / 0; tee_dropped 399498 |
| tok-edge-2 | 72% / 0 (0) / 0 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 240 | nano: UDP edge-1, LL-HLS –; view: UDP edge-2, LL-HLS – | nano: UDP edge-2, LL-HLS –; view: UDP edge-1, LL-HLS – | nano: UDP edge-1, LL-HLS –; view: UDP edge-1, LL-HLS – | nano: UDP edge-2, LL-HLS –; view: UDP edge-1, LL-HLS – |

