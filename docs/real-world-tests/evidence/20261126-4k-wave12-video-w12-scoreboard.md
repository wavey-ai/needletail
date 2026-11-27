Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w12; 0 win, 0 tie, 0 loss, 52 not measured of 52 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 320 (160) | none | not measured | 160 |
| New York | 240 | none | not measured | 160 |
| Tokyo | 240 | none | not measured | 160 |
| Sydney | 240 | none | not measured | 160 |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 80 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 18.7 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 160 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 33.7 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | London | Frames on time (%) | 99.4 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 50.5 ‡ | – | not measured |
|  |  | Longest stall (ms) | 122.0 ‡ | – | not measured |
| 320 Mbit/s | London | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 61.0 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 80 Mbit/s | New York | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 48.9 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 160 Mbit/s | New York | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 64.4 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 82.6 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 320 Mbit/s | New York | Frames on time (%) | 90.8 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 103.8 ‡ | – | not measured |
|  |  | Longest stall (ms) | 7377.0 ‡ | – | not measured |
| 80 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 124.2 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 160 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 145.6 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 158.2 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 320 Mbit/s | Tokyo | Frames on time (%) | 97.6 | – | not measured |
|  |  | Delay after due, median (ms) | 181.2 | – | not measured |
|  |  | Longest stall (ms) | 549.0 | – | not measured |
| 80 Mbit/s | Sydney | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 144.7 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 160 Mbit/s | Sydney | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 161.1 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 179.7 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 320 Mbit/s | Sydney | Frames on time (%) | 66.8 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 243.3 ‡ | – | not measured |
|  |  | Longest stall (ms) | 15831.0 ‡ | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 80 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s |
| --- | ---: | ---: | ---: | ---: |
| lon-dist-a | 783 → 996 | 1428 → 1449 | 1390 → 1399 | 1406 → 1429 |
| lon-dist-b | 781 → 993 | 1411 → 1426 | 1379 → 1398 | 1400 → 1430 |
| lon-edge-1 | 361 → 751 | 440 → 734 | 440 → 854 | 436 → 942 |
| lon-edge-2 | 358 → 642 | 440 → 702 | 431 → 865 | 474 → 916 |
| new-dist-a | 699 → 856 | 902 → 924 | 944 → 971 | 1022 → 1080 |
| new-dist-b | 697 → 857 | 969 → 981 | 980 → 1026 | 1168 → 1202 |
| new-edge-1 | 362 → 653 | 458 → 729 | 447 → 858 | 472 → 922 |
| new-edge-2 | 357 → 646 | 448 → 723 | 461 → 888 | 510 → 993 |
| syd-dist-a | 697 → 861 | 935 → 944 | 929 → 956 | 1094 → 1099 |
| syd-dist-b | 697 → 851 | 903 → 912 | 922 → 949 | 984 → 1011 |
| syd-edge-1 | 360 → 656 | 473 → 730 | 443 → 859 | 516 → 931 |
| syd-edge-2 | 351 → 641 | 462 → 751 | 473 → 920 | 485 → 966 |
| tok-dist-a | 697 → 855 | 922 → 933 | 976 → 1015 | 996 → 1032 |
| tok-dist-b | 702 → 855 | 888 → 909 | 944 → 990 | 1142 → 1166 |
| tok-edge-1 | 355 → 718 | 463 → 743 | 445 → 898 | 534 → 964 |
| tok-edge-2 | 354 → 652 | 443 → 726 | 469 → 897 | 446 → 936 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 80 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s |
| --- | ---: | ---: | ---: | ---: |
| contrib-lon | – | – | – | – |
| lon-dist-a | 1000 (451 MB) | 986 (809 MB) | 652 (809 MB) | 482 (809 MB) |
| lon-dist-b | 999 (451 MB) | 987 (808 MB) | 652 (809 MB) | 482 (809 MB) |
| lon-edge-1 | 505 (227 MB) | 329 (274 MB) | 312 (404 MB) | 234 (406 MB) |
| lon-edge-2 | 503 (227 MB) | 309 (277 MB) | 312 (405 MB) | 234 (406 MB) |
| new-dist-a | 972 (430 MB) | 481 (407 MB) | 322 (406 MB) | 239 (406 MB) |
| new-dist-b | 969 (431 MB) | 480 (407 MB) | 323 (407 MB) | 239 (407 MB) |
| new-edge-1 | 504 (227 MB) | 336 (276 MB) | 313 (404 MB) | 234 (405 MB) |
| new-edge-2 | 504 (227 MB) | 324 (298 MB) | 313 (407 MB) | 234 (407 MB) |
| syd-dist-a | 973 (431 MB) | 481 (407 MB) | 323 (407 MB) | 239 (406 MB) |
| syd-dist-b | 967 (430 MB) | 480 (407 MB) | 323 (407 MB) | 239 (406 MB) |
| syd-edge-1 | 504 (227 MB) | 302 (275 MB) | 312 (403 MB) | 233 (407 MB) |
| syd-edge-2 | 504 (227 MB) | 318 (278 MB) | 303 (407 MB) | 233 (408 MB) |
| tok-dist-a | 972 (429 MB) | 481 (407 MB) | 323 (406 MB) | 239 (406 MB) |
| tok-dist-b | 973 (431 MB) | 481 (407 MB) | 323 (407 MB) | 239 (407 MB) |
| tok-edge-1 | 504 (227 MB) | 307 (282 MB) | 313 (383 MB) | 234 (406 MB) |
| tok-edge-2 | 503 (227 MB) | 314 (274 MB) | 312 (404 MB) | 233 (405 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 80 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s |
| --- | --- | --- | --- | --- |
| lon-dist-a | 25% / 0 (0) / 0 | 41% / 0 (0) / 0 | 53% / 0 (0) / 0 | 63% / 0 (0) / 0 |
| lon-dist-b | 34% / 0 (0) / 0 | 38% / 0 (0) / 0 | 50% / 0 (0) / 0 | 62% / 0 (0) / 0 |
| lon-edge-1 | 37% / 0 (0) / 0 | 43% / 0 (0) / 0 | 56% / 0 (0) / 0 | 78% / 0 (0) / 0; nothing_to_shed_episodes 5, shed_hints 1, shed_rounds 1, udp_shed_moves 1 |
| lon-edge-2 | 43% / 0 (0) / 0 | 48% / 0 (0) / 0 | 66% / 0 (0) / 0 | 76% / 0 (0) / 0; nothing_to_shed_episodes 4 |
| new-dist-a | 52% / 0 (0) / 0 | 56% / 0 (0) / 0 | 68% / 0 (0) / 0 | 81% / 0 (0) / 0 |
| new-dist-b | 27% / 0 (0) / 0 | 43% / 0 (0) / 0 | 61% / 0 (0) / 0 | 73% / 0 (0) / 0 |
| new-edge-1 | 37% / 0 (0) / 0 | 53% / 0 (0) / 0 | 50% / 0 (0) / 0 | 85% / 0 (0) / 0; nothing_to_shed_episodes 5, shed_hints 3, shed_rounds 3, udp_shed_moves 3, tee_dropped 338972, not_requested_shed 462, shed_answered 3.31593e+06, shed_recorded 225 |
| new-edge-2 | 50% / 0 (0) / 0 | 50% / 0 (0) / 0 | 79% / 0 (0) / 0; nothing_to_shed_episodes 7, shed_hints 1, shed_rounds 1, udp_shed_moves 1 | 79% / 0 (0) / 0; nothing_to_shed_episodes 5, shed_hints 2, shed_rounds 2, udp_shed_moves 2 |
| syd-dist-a | 26% / 0 (0) / 0 | 41% / 0 (0) / 0 | 54% / 0 (0) / 0 | 66% / 0 (0) / 0 |
| syd-dist-b | 32% / 0 (0) / 0 | 47% / 0 (0) / 0 | 60% / 0 (0) / 0 | 71% / 0 (0) / 0 |
| syd-edge-1 | 61% / 0 (0) / 0 | 49% / 0 (0) / 0 | 59% / 0 (0) / 0 | 89% / 0 (0) / 0; nothing_to_shed_episodes 3, tee_dropped 870177, not_requested_shed 1212, shed_answered 4.53812e+06, shed_recorded 588 |
| syd-edge-2 | 26% / 0 (0) / 0 | 68% / 0 (0) / 0 | 81% / 0 (0) / 0; nothing_to_shed_episodes 3, shed_hints 1, shed_rounds 1, udp_shed_moves 1 | 87% / 0 (0) / 0; nothing_to_shed_episodes 5, shed_hints 1, shed_rounds 1, udp_shed_moves 1 |
| tok-dist-a | 36% / 0 (0) / 0 | 51% / 0 (0) / 0 | 64% / 0 (0) / 0 | 77% / 0 (0) / 0 |
| tok-dist-b | 30% / 0 (0) / 0 | 43% / 0 (0) / 0 | 58% / 0 (0) / 0 | 69% / 0 (0) / 0 |
| tok-edge-1 | 58% / 0 (0) / 0 | 75% / 0 (0) / 0; shed_hints 1, shed_rounds 1, udp_shed_moves 1 | 72% / 0 (0) / 0 | 87% / 0 (0) / 0; nothing_to_shed_episodes 4, shed_hints 2, shed_rounds 2, udp_shed_moves 2, tee_dropped 916985, not_requested_shed 1317, shed_answered 7.20965e+06, shed_recorded 615 |
| tok-edge-2 | 57% / 0 (0) / 0 | 52% / 0 (0) / 0 | 78% / 0 (0) / 0; nothing_to_shed_episodes 1, shed_hints 1, shed_rounds 1, udp_shed_moves 1 | 87% / 0 (0) / 0; nothing_to_shed_episodes 2, shed_hints 1, shed_rounds 1, udp_shed_moves 1, tee_dropped 27268, not_requested_shed 38, shed_answered 338245, shed_recorded 19 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 80 | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-2 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) |
| 160 | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-2 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) |
| 240 | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS – | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS – | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS – | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) |
| 320 | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1, LL-HLS – | nano: UDP edge-1+edge-2+syd-edge-1+syd-edge-2+tok-edge-1, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-2 (shared) | nano: UDP syd-edge-1+syd-edge-2+edge-1+edge-2, LL-HLS –; view: UDP syd-edge-1+syd-edge-2+edge-1+edge-2, LL-HLS edge-2 (shared) | nano: UDP edge-1+edge-2+tok-edge-1+tok-edge-2, LL-HLS –; view: UDP edge-1+edge-2+tok-edge-1+tok-edge-2, LL-HLS edge-1 (shared) |

