Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w12-stagger; 0 win, 0 tie, 0 loss, 16 not measured of 16 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 240 | none | not measured | 240 |
| New York | none | none | not measured | none |
| Tokyo | none | none | not measured | none |
| Sydney | none | none | not measured | none |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 240 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 47.3 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 98.1 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 82.8 ‡ | – | not measured |
|  |  | Longest stall (ms) | 804.0 ‡ | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 97.5 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 163.4 ‡ | – | not measured |
|  |  | Longest stall (ms) | 3645.0 ‡ | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 96.9 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 187.9 ‡ | – | not measured |
|  |  | Longest stall (ms) | 3640.0 ‡ | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 240 Mbit/s |
| --- | ---: |
| lon-dist-a | 1527 → 1533 |
| lon-dist-b | 1457 → 1464 |
| lon-edge-1 | 461 → 872 |
| lon-edge-2 | 444 → 885 |
| new-dist-a | 1307 → 1330 |
| new-dist-b | 1269 → 1298 |
| new-edge-1 | 476 → 909 |
| new-edge-2 | 500 → 919 |
| syd-dist-a | 1309 → 1331 |
| syd-dist-b | 991 → 1013 |
| syd-edge-1 | 559 → 917 |
| syd-edge-2 | 505 → 927 |
| tok-dist-a | 1043 → 1046 |
| tok-dist-b | 1250 → 1269 |
| tok-edge-1 | 429 → 942 |
| tok-edge-2 | 448 → 911 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 240 Mbit/s |
| --- | ---: |
| contrib-lon | – |
| lon-dist-a | 652 (809 MB) |
| lon-dist-b | 652 (809 MB) |
| lon-edge-1 | 312 (405 MB) |
| lon-edge-2 | 312 (376 MB) |
| new-dist-a | 323 (407 MB) |
| new-dist-b | 323 (406 MB) |
| new-edge-1 | 304 (395 MB) |
| new-edge-2 | 304 (407 MB) |
| syd-dist-a | 323 (406 MB) |
| syd-dist-b | 323 (407 MB) |
| syd-edge-1 | 310 (395 MB) |
| syd-edge-2 | 312 (390 MB) |
| tok-dist-a | 323 (406 MB) |
| tok-dist-b | 323 (407 MB) |
| tok-edge-1 | 300 (394 MB) |
| tok-edge-2 | 306 (371 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 240 Mbit/s |
| --- | --- |
| lon-dist-a | 51% / 0 (0) / 0 |
| lon-dist-b | 52% / 0 (0) / 0 |
| lon-edge-1 | 51% / 0 (0) / 0 |
| lon-edge-2 | 51% / 0 (0) / 0 |
| new-dist-a | 68% / 0 (0) / 0 |
| new-dist-b | 60% / 0 (0) / 0 |
| new-edge-1 | 76% / 0 (0) / 0; nothing_to_shed_episodes 5, shed_hints 3, shed_rounds 3, udp_shed_moves 3, not_requested_shed 110, shed_answered 1.49329e+06 |
| new-edge-2 | 67% / 0 (0) / 0; nothing_to_shed_episodes 10, shed_hints 2, shed_rounds 2, udp_shed_moves 2 |
| syd-dist-a | 55% / 0 (0) / 0 |
| syd-dist-b | 60% / 0 (0) / 0 |
| syd-edge-1 | 87% / 0 (0) / 0; nothing_to_shed_episodes 8, shed_hints 2, shed_rounds 2, udp_shed_moves 2, not_requested_shed 56, shed_answered 4.66099e+06 |
| syd-edge-2 | 77% / 0 (0) / 0; nothing_to_shed_episodes 1 |
| tok-dist-a | 65% / 0 (0) / 0 |
| tok-dist-b | 58% / 0 (0) / 0 |
| tok-edge-1 | 82% / 0 (0) / 0; nothing_to_shed_episodes 11, shed_hints 1, shed_rounds 1, udp_shed_moves 1, tee_dropped 121479, not_requested_shed 567, shed_answered 8.00722e+06, shed_recorded 51 |
| tok-edge-2 | 83% / 0 (0) / 0; nothing_to_shed_episodes 3, shed_answered 164777 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 240 | nano: UDP edge-1, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-2 (shared) | nano: UDP edge-1+tok-edge-1, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-2 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP syd-edge-2+edge-1+edge-2, LL-HLS edge-2 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2+tok-edge-2, LL-HLS edge-2 (shared) |

