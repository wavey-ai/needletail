Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w12-repair; 0 win, 0 tie, 0 loss, 16 not measured of 16 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 240 | none | not measured | none |
| New York | none | none | not measured | none |
| Tokyo | none | none | not measured | none |
| Sydney | none | none | not measured | none |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 240 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 49.0 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 93.7 | – | not measured |
|  |  | Delay after due, median (ms) | 89.6 | – | not measured |
|  |  | Longest stall (ms) | 3747.0 | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 94.4 | – | not measured |
|  |  | Delay after due, median (ms) | 163.6 | – | not measured |
|  |  | Longest stall (ms) | 4279.0 | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 93.2 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 179.5 ‡ | – | not measured |
|  |  | Longest stall (ms) | 7663.0 ‡ | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 240 Mbit/s |
| --- | ---: |
| lon-dist-a | 1510 → 1948 |
| lon-dist-b | 1490 → 1524 |
| lon-edge-1 | 332 → 879 |
| lon-edge-2 | 343 → 886 |
| new-dist-a | 938 → 1116 |
| new-dist-b | 901 → 1023 |
| new-edge-1 | 350 → 963 |
| new-edge-2 | 356 → 901 |
| syd-dist-a | 1006 → 1098 |
| syd-dist-b | 943 → 975 |
| syd-edge-1 | 347 → 903 |
| syd-edge-2 | 360 → 893 |
| tok-dist-a | 928 → 1036 |
| tok-dist-b | 937 → 1059 |
| tok-edge-1 | 365 → 899 |
| tok-edge-2 | 340 → 891 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 240 Mbit/s |
| --- | ---: |
| contrib-lon | – |
| lon-dist-a | 652 (808 MB) |
| lon-dist-b | 652 (809 MB) |
| lon-edge-1 | 312 (407 MB) |
| lon-edge-2 | 312 (406 MB) |
| new-dist-a | 323 (406 MB) |
| new-dist-b | 323 (407 MB) |
| new-edge-1 | 312 (407 MB) |
| new-edge-2 | 313 (378 MB) |
| syd-dist-a | 323 (407 MB) |
| syd-dist-b | 323 (407 MB) |
| syd-edge-1 | 312 (405 MB) |
| syd-edge-2 | 311 (401 MB) |
| tok-dist-a | 323 (406 MB) |
| tok-dist-b | 323 (407 MB) |
| tok-edge-1 | 311 (404 MB) |
| tok-edge-2 | 311 (376 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 240 Mbit/s |
| --- | --- |
| lon-dist-a | 58% / 0 (0) / 0; repair_shed_bytes 3.02197e+08 |
| lon-dist-b | 53% / 0 (0) / 0; repair_shed_bytes 3.19537e+08 |
| lon-edge-1 | 41% / 0 (0) / 0 |
| lon-edge-2 | 70% / 0 (0) / 0 |
| new-dist-a | 76% / 0 (0) / 0; repair_shed_bytes 2.94268e+08 |
| new-dist-b | 67% / 0 (0) / 0; repair_shed_bytes 1.50899e+08 |
| new-edge-1 | 72% / 0 (0) / 0; nothing_to_shed_episodes 1, shed_hints 1, shed_rounds 1, udp_shed_moves 1 |
| new-edge-2 | 65% / 0 (0) / 0; shed_hints 1, shed_rounds 1, udp_shed_moves 1 |
| syd-dist-a | 61% / 0 (0) / 0; repair_shed_bytes 9.10971e+07 |
| syd-dist-b | 66% / 0 (0) / 0; repair_shed_bytes 9.95233e+07 |
| syd-edge-1 | 79% / 0 (0) / 0; nothing_to_shed_episodes 6, shed_hints 1, shed_rounds 1, udp_shed_moves 1 |
| syd-edge-2 | 72% / 0 (0) / 0; nothing_to_shed_episodes 1, tee_dropped 2813, not_requested_shed 4, shed_answered 23905, shed_recorded 2 |
| tok-dist-a | 71% / 0 (0) / 0; repair_shed_bytes 1.14084e+08 |
| tok-dist-b | 66% / 0 (0) / 0; repair_shed_bytes 9.64106e+07 |
| tok-edge-1 | 73% / 0 (0) / 0; nothing_to_shed_episodes 3, tee_dropped 3910, not_requested_shed 4, shed_recorded 2 |
| tok-edge-2 | 80% / 0 (0) / 0; nothing_to_shed_episodes 5, shed_hints 1, shed_rounds 1, udp_shed_moves 1 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 240 | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS – | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-2 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS – |

