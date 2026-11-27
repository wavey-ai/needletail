Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w11-soak; 0 win, 0 tie, 0 loss, 10 not measured of 10 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | none | none | not measured | none |
| New York | none | none | not measured | none |
| Tokyo | none | none | not measured | none |
| Sydney | none | none | not measured | none |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 240 Mbit/s | New York | Frames on time (%) | 99.9 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 89.0 ‡ | – | not measured |
|  |  | Longest stall (ms) | 10000.0 ‡ | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 50.2 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 164.1 ‡ | – | not measured |
|  |  | Longest stall (ms) | 138042.0 ‡ | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 240 Mbit/s |
| --- | ---: |
| lon-dist-a | 1129 → 1460 |
| lon-dist-b | 1080 → 1457 |
| lon-edge-1 | 631 → 1027 |
| lon-edge-2 | 631 → 1048 |
| new-dist-a | 1125 → 1442 |
| new-dist-b | 1080 → 1304 |
| new-edge-1 | 632 → 992 |
| new-edge-2 | 607 → 1019 |
| syd-dist-a | 1027 → 1471 |
| syd-dist-b | 1059 → 1345 |
| syd-edge-1 | 605 → 1012 |
| syd-edge-2 | 643 → 1009 |
| tok-dist-a | 1116 → 1440 |
| tok-dist-b | 1044 → 1310 |
| tok-edge-1 | 590 → 1012 |
| tok-edge-2 | 638 → 1037 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 240 Mbit/s |
| --- | ---: |
| contrib-lon | – |
| lon-dist-a | 323 (405 MB) |
| lon-dist-b | 323 (407 MB) |
| lon-edge-1 | 305 (407 MB) |
| lon-edge-2 | 310 (399 MB) |
| new-dist-a | 320 (406 MB) |
| new-dist-b | 321 (407 MB) |
| new-edge-1 | 292 (372 MB) |
| new-edge-2 | 307 (407 MB) |
| syd-dist-a | 321 (406 MB) |
| syd-dist-b | 320 (406 MB) |
| syd-edge-1 | 312 (392 MB) |
| syd-edge-2 | 302 (363 MB) |
| tok-dist-a | 320 (407 MB) |
| tok-dist-b | 321 (407 MB) |
| tok-edge-1 | 273 (339 MB) |
| tok-edge-2 | 312 (407 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 240 Mbit/s |
| --- | --- |
| lon-dist-a | 60% / 0 (0) / 0 |
| lon-dist-b | 65% / 0 (0) / 0 |
| lon-edge-1 | 41% / 0 (0) / 0 |
| lon-edge-2 | 38% / 0 (0) / 0 |
| new-dist-a | 83% / 0 (0) / 0; tee_dropped 55835 |
| new-dist-b | 58% / 0 (0) / 0 |
| new-edge-1 | 59% / 91312 (91312) / 0; tee_dropped 745068 |
| new-edge-2 | 55% / 0 (0) / 0 |
| syd-dist-a | 68% / 0 (0) / 0 |
| syd-dist-b | 56% / 0 (0) / 0 |
| syd-edge-1 | 56% / 0 (0) / 0 |
| syd-edge-2 | 49% / 0 (0) / 0 |
| tok-dist-a | 78% / 0 (0) / 0 |
| tok-dist-b | 61% / 0 (0) / 0 |
| tok-edge-1 | 62% / 423730 (423730) / 0; tee_dropped 746651 |
| tok-edge-2 | 55% / 0 (0) / 0 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 240 | view: UDP –, LL-HLS edge-1 | nano: UDP edge-1, LL-HLS –; view: UDP –, LL-HLS edge-2 | nano: UDP edge-1, LL-HLS –; view: UDP –, LL-HLS edge-1+edge-2 | nano: UDP –, LL-HLS edge-2 |

