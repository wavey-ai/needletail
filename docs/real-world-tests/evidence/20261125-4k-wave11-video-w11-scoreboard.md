Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w11; 0 win, 0 tie, 0 loss, 49 not measured of 49 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 320 (160) | none | not measured | 320 (160) |
| New York | 160 | none | not measured | 160 |
| Tokyo | 160 | none | not measured | 160 |
| Sydney | 160 | none | not measured | 160 |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 80 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 16.8 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 160 Mbit/s | London | Frames on time (%) | 99.9 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 28.5 ‡ | – | not measured |
|  |  | Longest stall (ms) | 49.0 ‡ | – | not measured |
| 240 Mbit/s | London | Frames on time (%) | 98.8 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 44.9 ‡ | – | not measured |
|  |  | Longest stall (ms) | 680.0 ‡ | – | not measured |
| 320 Mbit/s | London | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 58.9 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 80 Mbit/s | New York | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 55.0 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 160 Mbit/s | New York | Frames on time (%) | 99.9 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 71.3 ‡ | – | not measured |
|  |  | Longest stall (ms) | 41.0 ‡ | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 96.5 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 87.7 ‡ | – | not measured |
|  |  | Longest stall (ms) | 3999.0 ‡ | – | not measured |
| 320 Mbit/s | New York | Frames on time (%) | 88.6 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 107.7 ‡ | – | not measured |
|  |  | Longest stall (ms) | 6935.0 ‡ | – | not measured |
| 80 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 129.0 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 160 Mbit/s | Tokyo | Frames on time (%) | 99.9 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 144.9 ‡ | – | not measured |
|  |  | Longest stall (ms) | 45.0 ‡ | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 96.4 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 163.1 ‡ | – | not measured |
|  |  | Longest stall (ms) | 4004.0 ‡ | – | not measured |
| 320 Mbit/s | Tokyo | Frames on time (%) | 97.3 ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 178.9 ‡ | – | not measured |
|  |  | Longest stall (ms) | 2508.0 ‡ | – | not measured |
| 80 Mbit/s | Sydney | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 148.0 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 160 Mbit/s | Sydney | Frames on time (%) | 99.9 W | – | not measured |
|  |  | Delay after due, median (ms) | 165.8 | – | not measured |
|  |  | Longest stall (ms) | 42.0 | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 96.5 | – | not measured |
|  |  | Delay after due, median (ms) | 184.4 | – | not measured |
|  |  | Longest stall (ms) | 3990.0 | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 80 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s | idle after |
| --- | ---: | ---: | ---: | ---: | ---: |
| lon-dist-a | 626 → 736 | 667 → 894 | 1117 → 1228 | 1144 → 1252 | 1255 → 1262 |
| lon-dist-b | 630 → 748 | 665 → 888 | 1090 → 1218 | 1159 → 1237 | 1239 → 1246 |
| lon-edge-1 | 513 → 660 | 638 → 890 | 444 → 940 | 480 → 899 | 590 → 611 |
| lon-edge-2 | 488 → 654 | 636 → 870 | 467 → 950 | 453 → 999 | 578 → 631 |
| new-dist-a | 547 → 711 | 638 → 872 | 1245 → 1289 | 1256 → 1367 | 1271 → 1274 |
| new-dist-b | 566 → 698 | 650 → 874 | 1057 → 1213 | 1221 → 1338 | 958 → 994 |
| new-edge-1 | 513 → 662 | 628 → 869 | 506 → 979 | 478 → 944 | 593 → 605 |
| new-edge-2 | 524 → 661 | 634 → 856 | 436 → 950 | 453 → 949 | 566 → 607 |
| syd-dist-a | 605 → 722 | 638 → 859 | 1174 → 1258 | 1212 → 1432 | 1425 → 1430 |
| syd-dist-b | 553 → 700 | 619 → 873 | 1102 → 1202 | 1228 → 1342 | 949 → 983 |
| syd-edge-1 | 461 → 655 | 618 → 857 | 437 → 918 | 513 → 944 | 509 → 556 |
| syd-edge-2 | 507 → 669 | 628 → 891 | 475 → 1012 | 455 → 1018 | 587 → 642 |
| tok-dist-a | 582 → 728 | 642 → 870 | 1121 → 1277 | 1209 → 1499 | 1499 → 1500 |
| tok-dist-b | 555 → 709 | 639 → 887 | 1119 → 1212 | 1217 → 1352 | 939 → 983 |
| tok-edge-1 | 495 → 658 | 627 → 853 | 474 → 970 | 459 → 950 | 524 → 524 |
| tok-edge-2 | 475 → 660 | 638 → 895 | 429 → 944 | 514 → 927 | 552 → 597 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 80 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s |
| --- | ---: | ---: | ---: | ---: |
| contrib-lon | – | – | – | – |
| lon-dist-a | 695 (288 MB) | 479 (407 MB) | 323 (407 MB) | 239 (406 MB) |
| lon-dist-b | 698 (296 MB) | 479 (407 MB) | 323 (406 MB) | 239 (407 MB) |
| lon-edge-1 | 497 (227 MB) | 479 (408 MB) | 315 (407 MB) | 234 (407 MB) |
| lon-edge-2 | 497 (227 MB) | 479 (407 MB) | 315 (407 MB) | 246 (406 MB) |
| new-dist-a | 692 (300 MB) | 479 (407 MB) | 323 (408 MB) | 239 (402 MB) |
| new-dist-b | 688 (285 MB) | 478 (407 MB) | 323 (406 MB) | 239 (407 MB) |
| new-edge-1 | 497 (227 MB) | 479 (407 MB) | 272 (385 MB) | 245 (392 MB) |
| new-edge-2 | 497 (227 MB) | 478 (407 MB) | 317 (407 MB) | 234 (407 MB) |
| syd-dist-a | 685 (303 MB) | 479 (407 MB) | 323 (407 MB) | 239 (401 MB) |
| syd-dist-b | 688 (297 MB) | 479 (407 MB) | 323 (407 MB) | 239 (407 MB) |
| syd-edge-1 | 497 (227 MB) | 478 (407 MB) | 282 (338 MB) | 231 (395 MB) |
| syd-edge-2 | 498 (227 MB) | 478 (407 MB) | 294 (386 MB) | 245 (392 MB) |
| tok-dist-a | 681 (302 MB) | 479 (408 MB) | 323 (407 MB) | 239 (401 MB) |
| tok-dist-b | 701 (297 MB) | 479 (407 MB) | 323 (406 MB) | 239 (407 MB) |
| tok-edge-1 | 497 (227 MB) | 479 (407 MB) | 277 (343 MB) | 245 (392 MB) |
| tok-edge-2 | 497 (227 MB) | 478 (407 MB) | 315 (407 MB) | 217 (366 MB) |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 80 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s |
| --- | --- | --- | --- | --- |
| lon-dist-a | 32% / 0 (0) / 0 | 46% / 0 (0) / 0 | 62% / 0 (0) / 0 | 71% / 0 (0) / 0 |
| lon-dist-b | 61% / 0 (0) / 0 | 65% / 0 (0) / 0 | 64% / 0 (0) / 0 | 72% / 0 (0) / 0 |
| lon-edge-1 | 29% / 0 (0) / 0 | 48% / 0 (0) / 0 | 56% / 0 (0) / 0 | 37% / 0 (0) / 0 |
| lon-edge-2 | 41% / 0 (0) / 0 | 54% / 0 (0) / 0 | 71% / 25916 (25916) / 0 | 56% / 0 (0) / 0 |
| new-dist-a | 41% / 0 (0) / 0 | 60% / 0 (0) / 0 | 88% / 0 (0) / 0 | 93% / 0 (0) / 0; tee_dropped 1.35564e+06 |
| new-dist-b | 53% / 0 (0) / 0 | 51% / 0 (0) / 0 | 81% / 0 (0) / 0 | 77% / 0 (0) / 0 |
| new-edge-1 | 59% / 0 (0) / 0 | 83% / 0 (0) / 0 | 93% / 227399 (227399) / 0; tee_dropped 1.32824e+06 | 66% / 76458 (76458) / 0; tee_dropped 2.14822e+06 |
| new-edge-2 | 35% / 0 (0) / 0 | 53% / 0 (0) / 0 | 78% / 0 (0) / 0 | 58% / 0 (0) / 0; tee_dropped 401757 |
| syd-dist-a | 50% / 0 (0) / 0 | 56% / 0 (0) / 0 | 79% / 0 (0) / 0 | 88% / 0 (0) / 0; tee_dropped 328071 |
| syd-dist-b | 28% / 0 (0) / 0 | 44% / 0 (0) / 0 | 66% / 0 (0) / 0 | 72% / 0 (0) / 0 |
| syd-edge-1 | 47% / 0 (0) / 0 | 69% / 0 (0) / 0 | 84% / 268728 (268728) / 0; tee_dropped 679368 | 60% / 24013 (24013) / 0; tee_dropped 670990 |
| syd-edge-2 | 43% / 0 (0) / 0 | 64% / 0 (0) / 0 | 81% / 0 (0) / 0; tee_dropped 73712 | 62% / 33170 (33170) / 0; tee_dropped 1.55198e+06 |
| tok-dist-a | 58% / 0 (0) / 0 | 65% / 0 (0) / 0 | 82% / 0 (0) / 0 | 93% / 0 (0) / 0; tee_dropped 621493 |
| tok-dist-b | 30% / 0 (0) / 0 | 47% / 0 (0) / 0 | 71% / 0 (0) / 0 | 75% / 0 (0) / 0 |
| tok-edge-1 | 50% / 0 (0) / 0 | 71% / 0 (0) / 0 | 88% / 390492 (390492) / 0; tee_dropped 838314 | 65% / 3276 (3276) / 0; tee_dropped 2.32645e+06 |
| tok-edge-2 | 51% / 0 (0) / 0 | 67% / 0 (0) / 0 | 75% / 7004 (7004) / 0 | 59% / 0 (0) / 0; tee_dropped 1.4367e+06 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 80 | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-1, LL-HLS edge-1 (shared) | nano: UDP edge-1, LL-HLS edge-2; view: UDP edge-1, LL-HLS edge-1 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-2 |
| 160 | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-1, LL-HLS edge-1 (shared) | nano: UDP edge-1, LL-HLS edge-2; view: UDP edge-1, LL-HLS edge-1 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-2 |
| 240 | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-2, LL-HLS edge-1 | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-1, LL-HLS edge-2; view: UDP edge-1, LL-HLS edge-2 | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1+edge-2, LL-HLS edge-2 (shared) |
| 320 | nano: UDP edge-2, LL-HLS –; view: UDP edge-2, LL-HLS edge-1 | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1+edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-1+edge-2, LL-HLS –; view: UDP edge-1, LL-HLS edge-1 (shared) | nano: UDP –, LL-HLS edge-1; view: UDP –, LL-HLS edge-2 |

