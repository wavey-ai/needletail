Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w12-soak; 0 win, 0 tie, 0 loss, 16 not measured of 16 rows.

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
|  |  | Delay after due, median (ms) | 42.4 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 99.7 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 82.8 ‡ | – | not measured |
|  |  | Longest stall (ms) | 395.0 ‡ | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 223.3 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 212.5 ‡ | – | not measured |
|  |  | Longest stall (ms) | 92.0 ‡ | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 240 Mbit/s |
| --- | ---: |
| lon-dist-a | 1267 → 1423 |
| lon-dist-b | 1245 → 1417 |
| lon-edge-1 | 468 → 905 |
| lon-edge-2 | 439 → 864 |
| new-dist-a | 1117 → 1163 |
| new-dist-b | 1229 → 1263 |
| new-edge-1 | 433 → 884 |
| new-edge-2 | 431 → 905 |
| syd-dist-a | 1106 → 1152 |
| syd-dist-b | 932 → 965 |
| syd-edge-1 | 458 → 921 |
| syd-edge-2 | 445 → 949 |
| tok-dist-a | 958 → 998 |
| tok-dist-b | 1158 → 1185 |
| tok-edge-1 | 432 → 999 |
| tok-edge-2 | 476 → 940 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 240 Mbit/s |
| --- | ---: |
| contrib-lon | – |
| lon-dist-a | 650 (809 MB) |
| lon-dist-b | 651 (809 MB) |
| lon-edge-1 | 312 (402 MB) |
| lon-edge-2 | 312 (405 MB) |
| new-dist-a | 323 (407 MB) |
| new-dist-b | 323 (407 MB) |
| new-edge-1 | 308 (407 MB) |
| new-edge-2 | 312 (407 MB) |
| syd-dist-a | 322 (406 MB) |
| syd-dist-b | 322 (406 MB) |
| syd-edge-1 | 292 (407 MB) |
| syd-edge-2 | 298 (407 MB) |
| tok-dist-a | 323 (406 MB) |
| tok-dist-b | 323 (407 MB) |
| tok-edge-1 | 312 (407 MB) |
| tok-edge-2 | 312 (403 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 240 Mbit/s |
| --- | --- |
| lon-dist-a | 51% / 0 (0) / 0 |
| lon-dist-b | 51% / 0 (0) / 0 |
| lon-edge-1 | 72% / 0 (0) / 0; nothing_to_shed_episodes 1, shed_hints 1, shed_rounds 1, udp_shed_moves 1 |
| lon-edge-2 | 46% / 0 (0) / 0 |
| new-dist-a | 68% / 0 (0) / 0 |
| new-dist-b | 60% / 0 (0) / 0 |
| new-edge-1 | 55% / 0 (0) / 0; shed_answered 700 |
| new-edge-2 | 74% / 0 (0) / 0; nothing_to_shed_episodes 6, shed_hints 1, shed_rounds 1, udp_shed_moves 1 |
| syd-dist-a | 56% / 0 (0) / 0 |
| syd-dist-b | 61% / 0 (0) / 0 |
| syd-edge-1 | 83% / 0 (0) / 0; nothing_to_shed_episodes 19, not_requested_shed 2, shed_answered 6.11119e+06 |
| syd-edge-2 | 79% / 0 (0) / 0; nothing_to_shed_episodes 2, shed_hints 1, shed_rounds 1, udp_shed_moves 1 |
| tok-dist-a | 64% / 0 (0) / 0 |
| tok-dist-b | 57% / 0 (0) / 0 |
| tok-edge-1 | 82% / 0 (0) / 0; nothing_to_shed_episodes 14, shed_hints 1, shed_rounds 1, udp_shed_moves 1, tee_dropped 130753, not_requested_shed 244, shed_answered 8.25505e+06, shed_recorded 97 |
| tok-edge-2 | 79% / 0 (0) / 0; nothing_to_shed_episodes 4, not_requested_shed 6, shed_answered 225048 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 240 | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2+syd-edge-1+syd-edge-2+tok-edge-1+tok-edge-2, LL-HLS – | nano: UDP edge-1+edge-2+tok-edge-1+tok-edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP syd-edge-2+edge-1+edge-2, LL-HLS – | nano: UDP edge-1+edge-2+tok-edge-1+tok-edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS – |

