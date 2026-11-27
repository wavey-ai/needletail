Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w11-240b; 0 win, 0 tie, 0 loss, 16 not measured of 16 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 240 | none | not measured | 240 |
| New York | 240 | none | not measured | none |
| Tokyo | 240 | none | not measured | none |
| Sydney | 240 | none | not measured | 240 |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 240 Mbit/s | London | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 40.0 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 87.0 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 164.5 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 178.3 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 240 Mbit/s |
| --- | ---: |
| lon-dist-a | 1333 → 1352 |
| lon-dist-b | 1313 → 1333 |
| lon-edge-1 | 810 → 1053 |
| lon-edge-2 | 784 → 1070 |
| new-dist-a | 1307 → 1353 |
| new-dist-b | 1336 → 1368 |
| new-edge-1 | 878 → 1035 |
| new-edge-2 | 841 → 1055 |
| syd-dist-a | 1299 → 1346 |
| syd-dist-b | 1339 → 1352 |
| syd-edge-1 | 746 → 989 |
| syd-edge-2 | 880 → 1087 |
| tok-dist-a | 1374 → 1389 |
| tok-dist-b | 1321 → 1333 |
| tok-edge-1 | 801 → 1037 |
| tok-edge-2 | 804 → 1033 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 240 Mbit/s |
| --- | ---: |
| contrib-lon | – |
| lon-dist-a | 323 (407 MB) |
| lon-dist-b | 322 (407 MB) |
| lon-edge-1 | 312 (404 MB) |
| lon-edge-2 | 312 (407 MB) |
| new-dist-a | 323 (407 MB) |
| new-dist-b | 323 (407 MB) |
| new-edge-1 | 293 (376 MB) |
| new-edge-2 | 313 (407 MB) |
| syd-dist-a | 322 (407 MB) |
| syd-dist-b | 323 (407 MB) |
| syd-edge-1 | 289 (375 MB) |
| syd-edge-2 | 312 (407 MB) |
| tok-dist-a | 323 (407 MB) |
| tok-dist-b | 323 (407 MB) |
| tok-edge-1 | 306 (407 MB) |
| tok-edge-2 | 273 (384 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 240 Mbit/s |
| --- | --- |
| lon-dist-a | 57% / 0 (0) / 0 |
| lon-dist-b | 62% / 0 (0) / 0 |
| lon-edge-1 | 62% / 0 (0) / 0 |
| lon-edge-2 | 59% / 0 (0) / 0 |
| new-dist-a | 85% / 0 (0) / 0 |
| new-dist-b | 74% / 0 (0) / 0 |
| new-edge-1 | 92% / 845133 (845133) / 0; tee_dropped 913367 |
| new-edge-2 | 79% / 0 (0) / 0 |
| syd-dist-a | 83% / 0 (0) / 0 |
| syd-dist-b | 68% / 0 (0) / 0 |
| syd-edge-1 | 90% / 456433 (456433) / 0; tee_dropped 1.1961e+06 |
| syd-edge-2 | 72% / 0 (0) / 0 |
| tok-dist-a | 73% / 0 (0) / 0 |
| tok-dist-b | 60% / 0 (0) / 0 |
| tok-edge-1 | 82% / 0 (0) / 0 |
| tok-edge-2 | 88% / 0 (0) / 0 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 240 | nano: UDP edge-2, LL-HLS –; view: UDP edge-1, LL-HLS edge-2 | nano: UDP edge-1, LL-HLS –; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS – | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-1, LL-HLS – |

