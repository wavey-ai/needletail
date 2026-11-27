Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w11-s100a; 0 win, 0 tie, 0 loss, 16 not measured of 16 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | none | none | not measured | none |
| New York | 240 | none | not measured | none |
| Tokyo | 240 | none | not measured | none |
| Sydney | none | none | not measured | none |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 240 Mbit/s | London | Frames on time (%) | 95.3 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 50.6 ‡ | – | not measured |
|  |  | Longest stall (ms) | 7232.0 ‡ | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 89.5 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 162.8 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 96.4 | – | not measured |
|  |  | Delay after due, median (ms) | 182.2 | – | not measured |
|  |  | Longest stall (ms) | 184.0 | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 240 Mbit/s |
| --- | ---: |
| lon-dist-a | 1179 → 1281 |
| lon-dist-b | 1163 → 1295 |
| lon-edge-1 | 530 → 999 |
| lon-edge-2 | 510 → 979 |
| new-dist-a | 1156 → 1302 |
| new-dist-b | 1064 → 1251 |
| new-edge-1 | 478 → 971 |
| new-edge-2 | 538 → 1031 |
| syd-dist-a | 1159 → 1278 |
| syd-dist-b | 1120 → 1327 |
| syd-edge-1 | 523 → 983 |
| syd-edge-2 | 504 → 1066 |
| tok-dist-a | 1138 → 1252 |
| tok-dist-b | 1086 → 1225 |
| tok-edge-1 | 432 → 1001 |
| tok-edge-2 | 557 → 1023 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 240 Mbit/s |
| --- | ---: |
| contrib-lon | – |
| lon-dist-a | 323 (407 MB) |
| lon-dist-b | 323 (406 MB) |
| lon-edge-1 | 312 (405 MB) |
| lon-edge-2 | 310 (407 MB) |
| new-dist-a | 323 (406 MB) |
| new-dist-b | 323 (407 MB) |
| new-edge-1 | 269 (359 MB) |
| new-edge-2 | 313 (407 MB) |
| syd-dist-a | 323 (406 MB) |
| syd-dist-b | 323 (407 MB) |
| syd-edge-1 | 312 (406 MB) |
| syd-edge-2 | 304 (407 MB) |
| tok-dist-a | 323 (407 MB) |
| tok-dist-b | 319 (407 MB) |
| tok-edge-1 | 295 (362 MB) |
| tok-edge-2 | 310 (407 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 240 Mbit/s |
| --- | --- |
| lon-dist-a | 58% / 0 (0) / 0 |
| lon-dist-b | 60% / 0 (0) / 0 |
| lon-edge-1 | 62% / 0 (0) / 0 |
| lon-edge-2 | 71% / 0 (0) / 0 |
| new-dist-a | 89% / 0 (0) / 0 |
| new-dist-b | 82% / 0 (0) / 0 |
| new-edge-1 | 97% / 963683 (963683) / 0; tee_dropped 1.15538e+06 |
| new-edge-2 | 91% / 0 (0) / 0 |
| syd-dist-a | 68% / 0 (0) / 0 |
| syd-dist-b | 56% / 0 (0) / 0 |
| syd-edge-1 | 70% / 0 (0) / 0 |
| syd-edge-2 | 85% / 0 (0) / 0 |
| tok-dist-a | 72% / 0 (0) / 0 |
| tok-dist-b | 59% / 0 (0) / 0 |
| tok-edge-1 | 85% / 0 (0) / 0 |
| tok-edge-2 | 91% / 0 (0) / 0 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 240 | nano: UDP edge-1, LL-HLS –; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-1, LL-HLS –; view: UDP edge-2, LL-HLS edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS – | nano: UDP edge-1+edge-2, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS – |

