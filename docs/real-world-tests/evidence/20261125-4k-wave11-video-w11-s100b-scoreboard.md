Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w11-s100b; 0 win, 0 tie, 0 loss, 16 not measured of 16 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 240 | none | not measured | 240 |
| New York | none | none | not measured | none |
| Tokyo | 240 | none | not measured | none |
| Sydney | 240 | none | not measured | none |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 240 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 35.5 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 94.0 | – | not measured |
|  |  | Delay after due, median (ms) | 87.2 | – | not measured |
|  |  | Longest stall (ms) | 6509.0 | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 164.1 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 178.7 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 240 Mbit/s |
| --- | ---: |
| lon-dist-a | 1361 → 1392 |
| lon-dist-b | 1324 → 1337 |
| lon-edge-1 | 505 → 1011 |
| lon-edge-2 | 463 → 1012 |
| new-dist-a | 1290 → 1313 |
| new-dist-b | 1274 → 1298 |
| new-edge-1 | 477 → 898 |
| new-edge-2 | 508 → 970 |
| syd-dist-a | 1317 → 1342 |
| syd-dist-b | 1316 → 1343 |
| syd-edge-1 | 489 → 912 |
| syd-edge-2 | 486 → 987 |
| tok-dist-a | 1326 → 1363 |
| tok-dist-b | 1268 → 1306 |
| tok-edge-1 | 559 → 908 |
| tok-edge-2 | 477 → 1029 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 240 Mbit/s |
| --- | ---: |
| contrib-lon | – |
| lon-dist-a | 323 (407 MB) |
| lon-dist-b | 322 (407 MB) |
| lon-edge-1 | 313 (406 MB) |
| lon-edge-2 | 312 (400 MB) |
| new-dist-a | 323 (407 MB) |
| new-dist-b | 322 (407 MB) |
| new-edge-1 | 233 (317 MB) |
| new-edge-2 | 316 (407 MB) |
| syd-dist-a | 323 (406 MB) |
| syd-dist-b | 323 (406 MB) |
| syd-edge-1 | 269 (359 MB) |
| syd-edge-2 | 312 (407 MB) |
| tok-dist-a | 323 (407 MB) |
| tok-dist-b | 323 (406 MB) |
| tok-edge-1 | 307 (390 MB) |
| tok-edge-2 | 292 (408 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 240 Mbit/s |
| --- | --- |
| lon-dist-a | 60% / 0 (0) / 0 |
| lon-dist-b | 62% / 0 (0) / 0 |
| lon-edge-1 | 60% / 62969 (62969) / 0 |
| lon-edge-2 | 73% / 0 (0) / 0 |
| new-dist-a | 88% / 0 (0) / 0 |
| new-dist-b | 77% / 0 (0) / 0 |
| new-edge-1 | 96% / 236099 (236099) / 0; tee_dropped 927073 |
| new-edge-2 | 79% / 0 (0) / 0 |
| syd-dist-a | 91% / 0 (0) / 0 |
| syd-dist-b | 78% / 0 (0) / 0 |
| syd-edge-1 | 98% / 399090 (399090) / 0; tee_dropped 1.29804e+06 |
| syd-edge-2 | 76% / 0 (0) / 0 |
| tok-dist-a | 75% / 0 (0) / 0 |
| tok-dist-b | 67% / 0 (0) / 0 |
| tok-edge-1 | 87% / 10352 (10352) / 0; tee_dropped 140475 |
| tok-edge-2 | 93% / 0 (0) / 0; tee_dropped 12528 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 240 | nano: UDP edge-1, LL-HLS –; view: UDP edge-2, LL-HLS edge-1 | nano: UDP edge-1, LL-HLS –; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS – | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS edge-2 (shared) |

